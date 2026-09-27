#!/usr/bin/env python3
"""Local-only Rhombus integration simulator; launches only bundled fake workers."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from rhombus_player import RhombusAPI, RhombusError, RateLimitError, _safe_filename
from http_telemetry import CONTEXT, RequestCancelled, Trace

CONTROL_FIELDS = {
    "stop_worker": {"action", "challenge_key", "worker", "pid"},
    "stop_challenge": {"action", "challenge_key"},
    "stop_all": {"action"},
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("use a literal loopback URL, e.g. http://127.0.0.1:3000")
    return url.rstrip("/")


class Gate:
    """Shared request admission and 429 cooldown across ALL request threads."""
    def __init__(self, interval):
        self.interval = interval
        self.next_request = 0.0
        self.cooldown = 0.0
        self.blocked = False
        self.lock = threading.Lock()

    def wait(self, cancel=None):
        while True:
            if cancel is not None and cancel.is_set():
                raise RequestCancelled("operator stopped this worker before request admission")
            with self.lock:
                if self.blocked:
                    raise RhombusError("request admission stopped: server cooldown exceeds the demo wait budget")
                now = time.monotonic()
                delay = max(self.next_request, self.cooldown) - now
                if delay <= 0:
                    self.next_request = now + self.interval
                    return
            time.sleep(min(delay, 0.1))

    def defer(self, seconds):
        with self.lock:
            self.cooldown = max(self.cooldown, time.monotonic() + seconds)

    def block(self):
        with self.lock:
            self.blocked = True


def private_json(path, value):
    """Atomic replacement, owner-only mode on POSIX. Use a private Windows ACL."""
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Journal:
    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()

    def record(self, kind, **fields):
        # Candidate flags go only to submissions.jsonl, not the general event log.
        name = ("http.jsonl" if kind.startswith("http_") else
                "submissions.jsonl" if kind in {"queued", "pending", "result"} else "events.jsonl")
        with self.lock:
            fd = os.open(self.root / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": kind, "time": time.time(), **fields}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())


class LocalAPI(RhombusAPI):
    """Uses the published routes, but forbids redirects/proxies/remote attachments."""
    def __init__(self, url, gate, journal):
        super().__init__(local_url(url), timeout=10, max_retries=5, max_retry_wait=30)
        self.gate = gate
        self.journal = journal

    def _request(self, path_or_url, *, data=None, headers=None, authenticated=True):
        url = urllib.parse.urljoin(self.base_url + "/", path_or_url)
        if not self._same_origin(url):
            raise RhombusError("demo simulator refuses cross-origin requests")
        request_headers = {"User-Agent": "rhombus-demo-orchestrator/1", "Accept-Language": "en", **(headers or {})}
        if authenticated and self.token:
            request_headers["Cookie"] = "rhombus-token=" + self.token
        operation_id = uuid.uuid4().hex
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(url, data=data, headers=request_headers)
            trace = Trace(self.journal, request, attempt + 1, operation_id, self.token)
            try:
                self.gate.wait(CONTEXT.get().get("cancel"))
            except Exception as error:
                trace.failure(error, blocked=True)
                raise
            # Per-call opener: no shared mutable urllib state between threads.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            try:
                return trace.open(opener, self.timeout)
            except urllib.error.HTTPError as error:
                try:
                    if error.code != 429:
                        raise RhombusError(f"HTTP {error.code} from local platform") from error
                    wait = self._retry_after(error.headers, min(0.5 * 2**attempt, 16))
                    if not math.isfinite(wait):
                        wait = 31
                    wait += 0.05
                    self.journal.record("rate_limit", attempt=attempt + 1, wait=wait)
                    if wait > self.max_retry_wait:
                        self.gate.block()
                    else:
                        self.gate.defer(wait)
                    if attempt == self.max_retries or wait > self.max_retry_wait:
                        raise RateLimitError(url, wait, attempt + 1) from error
                finally:
                    error.close()
            except urllib.error.URLError as error:
                raise RhombusError(f"local request failed: {error.reason}") from error
        raise AssertionError("unreachable")

    def submit_flag(self, challenge_id, flag):
        result = super().submit_flag(challenge_id, flag)
        # Stock Rhombus can report solve throttles/backend errors with HTTP 200.
        # Only the known English demo rejection means a definitively wrong flag.
        if not result.correct and result.message.strip().lower() != "incorrect flag":
            raise RhombusError("submission outcome requires review: " + result.message)
        return result


class Orchestrator:
    def __init__(self, api, root, *, answers=None, profiles=("burst", "steady", "never"),
                 seed=1, downloads=4, processes=12, worker_timeout=120):
        self.api, self.root = api, Path(root)
        self.journal = api.journal
        self.answers = answers or {}
        self.profiles, self.seed = profiles, seed
        self.download_slots = asyncio.Semaphore(downloads)
        self.process_slots = asyncio.Semaphore(processes)
        self.worker_timeout = worker_timeout
        self.challenges = {}
        self.solved = {}
        self.locks = {}
        self.states = {}
        self.submissions = set()
        self.review = set()
        self.children = set()
        self.handles = {}
        self.stopped_workers = set()
        self.stopped_challenges = set()
        self.stop_all = False
        self.control_error = None
        self.closing = False

    async def submit(self, key, flag, worker, request_id):
        # Shield journaling + network completion from worker cancellation.
        task = asyncio.create_task(self._submit(key, flag, worker, request_id))
        self.submissions.add(task)
        task.add_done_callback(self.submissions.discard)
        return await asyncio.shield(task)

    async def _submit(self, key, flag, worker, request_id):
        if key not in self.challenges or not isinstance(flag, str) or not 0 < len(flag) <= 4096:
            return {"request_id": request_id, "status": "error", "message": "invalid submission"}
        result = {"request_id": request_id, "challenge_key": key}
        ticket = uuid.uuid4().hex

        def notify_review(error):
            self.review.add(key)
            print(f"Manual review: {json.dumps({'challenge_key': key, 'flag': flag, 'error': str(error)})}",
                  file=sys.stderr, flush=True)

        try:
            self.journal.record("queued", ticket=ticket, challenge_key=key, flag=flag,
                                worker=worker, request_id=request_id)
        except OSError as error:
            notify_review(error)
            return {**result, "status": "error", "message": "journal failure; submission not sent"}
        async with self.locks[key]:
            cancelled = worker != "operator" and self.worker_stopped(key, worker)
            if self.solved[key].is_set() or cancelled:
                result.update(status="cancelled" if cancelled else "already_solved",
                              message="operator stop; candidate not sent" if cancelled else "race finished; candidate not validated")
                try:
                    self.journal.record("result", ticket=ticket, **result)
                except OSError as error:
                    notify_review(error)
                return result
            try:
                self.journal.record("pending", ticket=ticket, challenge_key=key, flag=flag,
                                    worker=worker, request_id=request_id)
            except OSError as error:
                # Do not send anything when the recovery intent cannot be made durable.
                notify_review(error)
                return {**result, "status": "error", "message": "journal failure; submission not sent"}
            handle = self.handles.get((key, worker), {})
            context = CONTEXT.set({"challenge_key": key, "worker": worker, "submission_ticket": ticket,
                                   "cancel": handle.get("cancel")})
            try:
                answer = await asyncio.to_thread(self.api.submit_flag, key, flag)
                result.update(status="correct" if answer.correct else "incorrect", message=answer.message)
            except RequestCancelled as error:
                result.update(status="cancelled", message=str(error))
            except Exception as error:
                result.update(status="error", message=str(error))
                # Never misreport network errors as wrong flags.
                notify_review(error)
            finally:
                CONTEXT.reset(context)
            try:
                self.journal.record("result", ticket=ticket, **result)
            except OSError as error:
                notify_review(error)
                result["persistence_warning"] = "response could not be journaled; reconcile manually"
            if result["status"] == "correct":
                self.states[key] = {"status": "solved", "winner": worker}
                self.solved[key].set()
                try:
                    self.journal.record("solved", challenge_key=key, winner=worker)
                except OSError as error:
                    notify_review(error)
            return result

    async def download(self, attachment, destination, key):
        async with self.download_slots:
            self.journal.record("download_started", challenge_key=key, file=destination.name)
            context = CONTEXT.set({"challenge_key": key})
            try:
                await asyncio.to_thread(self.api.download_attachment, attachment, destination)
            finally:
                CONTEXT.reset(context)
            self.journal.record("download_finished", challenge_key=key, file=destination.name,
                                bytes=destination.stat().st_size)

    async def challenge(self, challenge):
        key = challenge.id
        # Hash IDs instead of trusting organizer-controlled paths or slug uniqueness.
        folder = self.root / "challenges" / hashlib.sha256(key.encode()).hexdigest()
        folder.mkdir(parents=True)
        try:
            files = [folder / f"{index:03d}-{_safe_filename(a.name)}"
                     for index, a in enumerate(challenge.attachments)]
            await asyncio.gather(*(self.download(a, path, key) for a, path in zip(challenge.attachments, files)))
            manifest = {"version": 1, "challenge_key": key, "challenge": challenge.to_dict(),
                        "folder": str(folder.resolve()), "files": [str(path.resolve()) for path in files]}
            private_json(folder / "challenge.json", manifest)
            self.states[key] = {"status": "ready"}
            self.journal.record("ready", challenge_key=key)
            workers = [asyncio.create_task(self.worker(manifest, profile, index))
                       for index, profile in enumerate(self.profiles)]
            try:
                await asyncio.gather(*workers)
            finally:
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            if not self.solved[key].is_set():
                stopped_count = sum(k == key for k, _ in self.stopped_workers)
                stopped = self.stop_all or key in self.stopped_challenges or stopped_count == len(self.profiles)
                self.states[key] = {"status": "needs_review" if key in self.review else "stopped" if stopped else "unsolved",
                                    "operator_stopped_workers": stopped_count}
        except Exception as error:
            self.states[key] = {"status": "error", "message": str(error)}
            self.journal.record("challenge_error", challenge_key=key, message=str(error))

    async def worker(self, manifest, profile, index):
        key = manifest["challenge_key"]
        worker = f"{profile}-{index}"
        async with self.process_slots:
            if self.solved[key].is_set() or self.worker_stopped(key, worker):
                return
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-I", str(Path(__file__).with_name("fake_worker.py").resolve()),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=manifest["folder"], limit=65536,
            ))
            try:
                proc = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                proc = await spawning
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                await proc.wait()
                if proc.stdin:
                    proc.stdin.close()
                raise
            self.children.add(proc)
            handle = {"proc": proc, "stop": asyncio.Event(), "cancel": threading.Event(), "done": asyncio.Event()}
            self.handles[(key, worker)] = handle
            if self.worker_stopped(key, worker):
                handle["stop"].set()
                handle["cancel"].set()
            self.journal.record("worker_started", challenge_key=key, worker=worker, pid=proc.pid)
            driver = winner = stopper = None
            reason = "exited"
            try:
                # Deliberate simulation fixture, not a solved flag discovered from the artifact.
                job = {**manifest, "profile": profile, "seed": self.seed,
                       "worker": worker, "test_answer": self.answers.get(key)}
                proc.stdin.write((json.dumps(job) + "\n").encode())
                await proc.stdin.drain()

                async def drive():
                    for _ in range(32):  # hard ceiling on fake-worker submissions
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        message = json.loads(line)
                        if message.get("type") != "submit" or message.get("challenge_key") != key:
                            raise ValueError("worker protocol violation")
                        response = await self.submit(key, message.get("flag"), worker, message.get("request_id"))
                        proc.stdin.write((json.dumps(response) + "\n").encode())
                        await proc.stdin.drain()
                    await proc.wait()

                driver = asyncio.create_task(drive())
                winner = asyncio.create_task(self.solved[key].wait())
                stopper = asyncio.create_task(handle["stop"].wait())
                done, _ = await asyncio.wait({driver, winner, stopper}, timeout=self.worker_timeout,
                                            return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    reason = "operator_stop"
                elif winner in done:
                    reason = "challenge_solved"
                elif driver in done:
                    driver.result()
                    if proc.returncode not in (None, 0):
                        reason = "error"
                        self.journal.record("worker_error", challenge_key=key, worker=worker,
                                            message=f"Worker exited with code {proc.returncode}")
                else:
                    reason = "timeout"
                    self.journal.record("worker_timeout", challenge_key=key, worker=worker)
            except (BrokenPipeError, ConnectionResetError):
                reason = "pipe_closed"
            except asyncio.CancelledError:
                reason = "interrupted"
                raise
            except Exception as error:
                reason = "error"
                self.journal.record("worker_error", challenge_key=key, worker=worker, message=str(error))
            finally:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                await proc.wait()
                for task in (driver, winner, stopper):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(*(t for t in (driver, winner, stopper) if t is not None), return_exceptions=True)
                if proc.stdin:
                    proc.stdin.close()
                self.children.discard(proc)
                self.handles.pop((key, worker), None)
                handle["done"].set()
                self.journal.record("worker_stopped", challenge_key=key, worker=worker,
                                    pid=proc.pid, code=proc.returncode, reason=reason)

    def worker_stopped(self, key, worker):
        return self.stop_all or key in self.stopped_challenges or (key, worker) in self.stopped_workers

    def apply_control(self, value):
        action = value.get("action")
        key, worker, pid = value.get("challenge_key"), value.get("worker"), value.get("pid")
        if action not in CONTROL_FIELDS or set(value) - {"time"} != CONTROL_FIELDS[action]:
            raise ValueError("unsupported control action or fields")
        if action != "stop_all" and key not in self.challenges:
            raise ValueError("unknown challenge")
        if action == "stop_worker":
            handle = self.handles.get((key, worker))
            if type(pid) is not int or not handle or handle["proc"].pid != pid or handle["proc"].returncode is not None:
                raise ValueError("worker/PID no longer active; refresh before stopping")
            targets = [((key, worker), handle)]
        elif action == "stop_challenge":
            targets = [(identity, h) for identity, h in self.handles.items() if identity[0] == key]
        else:
            targets = list(self.handles.items())
        # Persist the accepted intent before changing process state.
        self.journal.record("operator_stop", action=action, challenge_key=key, worker=worker, pid=pid,
                            matched_workers=len(targets))
        if action == "stop_challenge":
            self.stopped_challenges.add(key)
        elif action == "stop_all":
            self.stop_all = True
        matched = []
        for (challenge_key, worker_name), handle in targets:
            self.stopped_workers.add((challenge_key, worker_name))
            handle["cancel"].set()
            handle["stop"].set()
            matched.append({"challenge_key": challenge_key, "worker": worker_name, "pid": handle["proc"].pid})
        return {"status": "acknowledged", "matched": matched,
                "message": "Stop acknowledged. Watch worker exits; already-sent requests may still complete."}

    async def control_inbox(self):
        try:
            await self._control_loop()
        except Exception as error:
            self.control_error = str(error)
            print(f"Operator controls unavailable: {error}", file=sys.stderr, flush=True)
            try:
                self.journal.record("control_error", message=str(error))
            except OSError:
                pass

    async def _control_loop(self):
        while not self.closing:
            for path in sorted((self.root / "control_requests").glob("*.json")):
                result_path = self.root / "control_results" / path.name
                if result_path.exists():
                    continue
                try:
                    if path.is_symlink():
                        raise ValueError("symlink control requests are not allowed")
                    value = json.loads(path.read_text(encoding="utf-8"))
                    result = self.apply_control(value)
                except Exception as error:
                    result = {"status": "rejected", "message": str(error), "matched": []}
                private_json(result_path, {**result, "time": time.time()})
                self.journal.record("control_result", control_id=path.stem, status=result["status"], message=result["message"])
            await asyncio.sleep(0.1)

    async def manual_inbox(self):
        while not self.closing:
            for path in sorted((self.root / "inbox").glob("*.json")):
                if (self.root / "outbox" / path.name).exists():
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    response = await self.submit(value["challenge_key"], value["flag"], "operator", path.stem)
                except Exception as error:
                    response = {"status": "error", "message": str(error)}
                private_json(self.root / "outbox" / path.name, response)
            await asyncio.sleep(0.1)

    async def heartbeat(self):
        while not self.closing:
            private_json(self.root / "live.json", {"time": time.time(), "pid": os.getpid(), "states": self.states,
                                                   "control_protocol": 0 if self.control_error else 1,
                                                   "control_error": self.control_error, "stop_all": self.stop_all})
            await asyncio.sleep(1)

    async def run(self):
        challenges = sorted(await asyncio.to_thread(self.api.challenges), key=lambda c: c.id)
        if len({c.id for c in challenges}) != len(challenges):
            raise ValueError("duplicate challenge IDs")
        for c in challenges:
            self.challenges[c.id] = c
            self.solved[c.id] = asyncio.Event()
            self.locks[c.id] = asyncio.Lock()
            self.states[c.id] = {"status": "downloading"}
        for name in ("control_requests", "control_results"):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        private_json(self.root / "catalog.json", [c.to_dict() for c in challenges])
        private_json(self.root / "run.json", {"status": "running", "source": self.api.base_url,
                                              "challenge_keys": list(self.challenges)})
        pulse = asyncio.create_task(self.heartbeat())
        controls = asyncio.create_task(self.control_inbox())
        inbox = asyncio.create_task(self.manual_inbox())
        tasks = [asyncio.create_task(self.challenge(c)) for c in challenges]
        try:
            await asyncio.gather(*tasks)
        finally:
            self.closing = True
            inbox.cancel()
            pulse.cancel()
            controls.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(inbox, pulse, controls, *tasks, return_exceptions=True)
            # A started POST can still complete after its worker exits. Preserve its outcome.
            if self.submissions:
                await asyncio.gather(*list(self.submissions), return_exceptions=True)
            for key, state in self.states.items():
                if state["status"] in {"downloading", "ready"}:
                    self.states[key] = {"status": "interrupted"}
                elif key in self.review and state["status"] == "unsolved":
                    self.states[key] = {"status": "needs_review"}
            private_json(self.root / "summary.json", self.states)
            private_json(self.root / "run.json", {"status": "stopped", "source": self.api.base_url,
                                                  "challenge_keys": list(self.challenges)})
        return self.states


def queue_manual(root, key, flag, timeout):
    root = Path(root)
    state = json.loads((root / "run.json").read_text(encoding="utf-8"))
    if state["status"] != "running" or key not in state["challenge_keys"]:
        raise ValueError("no active run for this challenge; use the player CLI for a standalone submission")
    ticket = uuid.uuid4().hex
    private_json(root / "inbox" / (ticket + ".json"), {"challenge_key": key, "flag": flag})
    deadline = time.monotonic() + timeout
    response = root / "outbox" / (ticket + ".json")
    while time.monotonic() < deadline:
        if response.exists():
            return json.loads(response.read_text(encoding="utf-8"))
        if json.loads((root / "run.json").read_text(encoding="utf-8"))["status"] != "running":
            break
        time.sleep(0.1)
    return {"status": "error", "message": f"outcome unknown; candidate retained in inbox/{ticket}.json"}


def review_candidates(root):
    """Join durable intents with results; also include unprocessed manual requests."""
    root = Path(root)
    pending, results = {}, {}
    journal = root / "submissions.jsonl"
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                print("Warning: incomplete journal line; inspect submissions.jsonl", file=sys.stderr)
                continue
            if item["event"] in {"queued", "pending"}:
                pending[item["ticket"]] = item
            elif item["event"] == "result":
                results[item["ticket"]] = item
    candidates = []
    for ticket, item in pending.items():
        result = results.get(ticket, {"status": "unknown", "message": "no durable response recorded"})
        if result["status"] in {"error", "unknown"}:
            candidates.append({"ticket": ticket, "challenge_key": item["challenge_key"], "flag": item["flag"],
                               "status": result["status"], "message": result.get("message", "")})
    for path in sorted((root / "inbox").glob("*.json")):
        if not (root / "outbox" / path.name).exists():
            candidates.append({**json.loads(path.read_text(encoding="utf-8")), "ticket": path.stem,
                               "status": "unknown", "message": "manual request has no response; check journal before retrying"})
    return candidates


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("url", help="literal loopback URL only")
    run.add_argument("-u", "--username", required=True)
    run.add_argument("--password-stdin", action="store_true")
    run.add_argument("--output", type=Path, required=True, help="new private run directory")
    run.add_argument("--answers", type=Path, help="private JSON object: challenge ID -> test answer; null never solves")
    run.add_argument("--profiles", nargs="+", choices=["burst", "steady", "never"], default=["burst", "steady", "never"])
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--downloads", type=int, default=4)
    run.add_argument("--processes", type=int, default=12)
    run.add_argument("--request-interval", type=float, default=0.1, help="seconds between requests; 0 for bounded local stress test")
    run.add_argument("--worker-timeout", type=float, default=120)
    submit = sub.add_parser("submit", help="manually submit into an active run (also stops that challenge's workers)")
    submit.add_argument("--run-dir", type=Path, required=True)
    submit.add_argument("challenge_key")
    submit.add_argument("--flag-stdin", action="store_true")
    submit.add_argument("--timeout", type=float, default=120)
    review = sub.add_parser("review", help="print sensitive flags needing operator review; no network requests")
    review.add_argument("--run-dir", type=Path, required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "review":
            if not args.run_dir.is_dir():
                raise ValueError("run directory does not exist")
            print(json.dumps(review_candidates(args.run_dir), indent=2))
            return 0
        if args.command == "submit":
            flag = sys.stdin.readline().rstrip("\r\n") if args.flag_stdin else getpass.getpass("Flag: ")
            if not flag or len(flag) > 4096 or not math.isfinite(args.timeout) or args.timeout <= 0:
                raise ValueError("nonempty flag (max 4096 chars) and finite positive timeout required")
            try:
                result = queue_manual(args.run_dir, args.challenge_key, flag, args.timeout)
            except Exception:
                print("Manual recovery flag: " + json.dumps(flag), file=sys.stderr)
                raise
            print(json.dumps(result))
            if result["status"] == "error":
                print("Manual recovery flag: " + json.dumps(flag), file=sys.stderr)
            return 0 if result["status"] in {"correct", "already_solved"} else (2 if result["status"] == "incorrect" else 1)
        url = local_url(args.url)
        if not 1 <= args.downloads <= 16 or not 1 <= args.processes <= 32 or len(args.profiles) > 8:
            raise ValueError("downloads: 1..16; processes: 1..32; profiles: at most 8")
        if not math.isfinite(args.request_interval) or args.request_interval < 0:
            raise ValueError("request interval must be finite and nonnegative")
        if not math.isfinite(args.worker_timeout) or not 0 < args.worker_timeout <= 600:
            raise ValueError("worker timeout must be in (0, 600]")
        answers = json.loads(args.answers.read_text(encoding="utf-8")) if args.answers else {}
        if not isinstance(answers, dict) or any(not isinstance(k, str) or
                (v is not None and (not isinstance(v, str) or not 0 < len(v) <= 4096)) for k, v in answers.items()):
            raise ValueError("answers must map challenge keys to strings or null")
        args.output = args.output.resolve()
        args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name in ("inbox", "outbox", "challenges"):
            (args.output / name).mkdir(mode=0o700)
        journal = Journal(args.output)
        api = LocalAPI(url, Gate(args.request_interval), journal)
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Password: ")
        api.login(args.username, password)
        del password
        states = asyncio.run(Orchestrator(api, args.output, answers=answers, profiles=args.profiles,
                                         seed=args.seed, downloads=args.downloads, processes=args.processes,
                                         worker_timeout=args.worker_timeout).run())
        print(json.dumps(states, indent=2))
        return 0 if all(s["status"] == "solved" for s in states.values()) else 2
    except KeyboardInterrupt:
        print("Interrupted; review the private run journal for pending submissions.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RhombusError, KeyError, http.client.HTTPException) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
