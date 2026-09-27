import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_orchestrator import Gate, Journal, LocalAPI, Orchestrator, local_url, queue_manual, review_candidates
from fake_worker import plan
from rhombus_player import Attachment, Challenge, RhombusError, SubmissionResult


def workspace(root):
    for name in ("inbox", "outbox", "challenges"):
        (root / name).mkdir()


class MockAPI:
    base_url = "http://127.0.0.1:3000"

    def __init__(self, root, challenges):
        self.journal = Journal(root)
        self.items = challenges
        self.calls = []
        self.lock = threading.Lock()
        self.downloads_active = self.downloads_peak = 0
        self.download_release = None

    def challenges(self):
        return self.items

    def download_attachment(self, attachment, destination):
        with self.lock:
            self.downloads_active += 1
            self.downloads_peak = max(self.downloads_peak, self.downloads_active)
        if self.download_release is not None:
            self.download_release.wait(5)
        else:
            time.sleep(0.2)
        destination.write_bytes(b"test artifact")
        with self.lock:
            self.downloads_active -= 1
        self.journal.record("mock_download_done")

    def submit_flag(self, key, flag):
        self.calls.append((key, flag))
        if key == "error":
            raise RhombusError("simulated outage")
        return SubmissionResult(flag == "fixture-answer", "ok" if flag == "fixture-answer" else "Incorrect flag")


class SimulatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_downloads_races_unsolved_errors_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            attachment = Attachment("data.bin", "http://127.0.0.1:3000/file")
            api = MockAPI(root, [Challenge("solve", "Solve", "misc", "", (attachment, attachment)),
                                 Challenge("unsolved", "Unsolved", "misc", ""),
                                 Challenge("error", "Error", "misc", "")])
            runner = Orchestrator(api, root, answers={"solve": "fixture-answer"}, processes=9)
            api.download_release = threading.Event()
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                running = asyncio.create_task(runner.run())
                try:
                    for _ in range(300):
                        if runner.children and api.downloads_active == 2:
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(runner.children)
                    self.assertEqual(api.downloads_active, 2)
                finally:
                    api.download_release.set()
                states = await running
            self.assertEqual(states["solve"]["status"], "solved")
            self.assertEqual(states["unsolved"]["status"], "unsolved")
            self.assertEqual(states["error"]["status"], "needs_review")
            self.assertTrue(review_candidates(root))
            self.assertIn("Manual review", errors.getvalue())
            self.assertFalse(runner.children)
            from demo_dashboard import Snapshot
            snapshot = Snapshot(root).build()
            self.assertEqual(snapshot["run_status"], "stopped")
            self.assertFalse(snapshot["workers"])
            self.assertEqual(snapshot["counts"]["correct"], 1)
            self.assertFalse(snapshot["counts"].get("unknown", 0))
            self.assertEqual(sum(flag == "fixture-answer" for _, flag in api.calls), 1)
            self.assertEqual(api.downloads_peak, 2)
            events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            first_worker = next(i for i, e in enumerate(events) if e["event"] == "worker_started")
            first_download = next(i for i, e in enumerate(events) if e["event"] == "mock_download_done")
            self.assertLess(first_worker, first_download)
            self.assertEqual(sum(e["event"] == "worker_started" for e in events),
                             sum(e["event"] == "worker_stopped" for e in events))
            journal = [json.loads(line) for line in (root / "submissions.jsonl").read_text().splitlines()]
            pending = {e["ticket"] for e in journal if e["event"] in {"queued", "pending"}}
            results = {e["ticket"] for e in journal if e["event"] == "result"}
            self.assertEqual(pending, results)
            self.assertTrue(any(e.get("status") == "error" for e in journal))
            self.assertTrue(all("fixture-answer" not in p.read_text() for p in root.glob("challenges/*/challenge.json")))

    async def test_operator_stops_exact_worker_and_gets_durable_ack(self):
        from demo_orchestrator import private_json
        from demo_dashboard import Snapshot
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            api = MockAPI(root, [Challenge("key", "Key", "misc", "")])
            runner = Orchestrator(api, root, profiles=("never", "never"), processes=2)
            running = asyncio.create_task(runner.run())
            try:
                for _ in range(300):
                    if len(runner.handles) == 2:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(len(runner.handles), 2)
                identity, handle = next(iter(runner.handles.items()))
                command = {"action": "stop_worker", "challenge_key": identity[0], "worker": identity[1], "pid": handle["proc"].pid}
                with self.assertRaises(ValueError):
                    runner.apply_control({**command, "pid": -1})
                private_json(root / "control_requests" / "operator-test.json", command)
                await asyncio.wait_for(handle["done"].wait(), 3)
                self.assertIsNotNone(handle["proc"].returncode)
                states = await running
                self.assertEqual(states["key"]["status"], "unsolved")
                self.assertEqual(states["key"]["operator_stopped_workers"], 1)
                snapshot = Snapshot(root).build()
                self.assertEqual(snapshot["controls"][0]["status"], "completed")
                self.assertTrue(any(w.get("reason") == "operator_stop" for w in snapshot["worker_history"]))
                self.assertFalse(runner.children)
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    async def test_control_failures_are_visible_and_disable_controls(self):
        from demo_dashboard import Snapshot
        from demo_orchestrator import private_json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            api = MockAPI(root, [])
            runner = Orchestrator(api, root)
            with mock.patch.object(api.journal, "record", side_effect=OSError("journal denied")):
                with self.assertRaises(OSError):
                    runner.apply_control({"action": "stop_all"})
            self.assertFalse(runner.stop_all)
            with mock.patch.object(runner, "_control_loop", side_effect=OSError("inbox denied")), contextlib.redirect_stderr(io.StringIO()):
                await runner.control_inbox()
            self.assertEqual(runner.control_error, "inbox denied")
            private_json(root / "run.json", {"status": "running", "challenge_keys": []})
            pulse = asyncio.create_task(runner.heartbeat())
            try:
                await asyncio.sleep(0.01)
                snapshot = Snapshot(root).build()
                self.assertFalse(snapshot["can_control"])
                self.assertTrue(any("inbox denied" in warning for warning in snapshot["warnings"]))
            finally:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)

    async def test_stop_challenge_does_not_stop_other_challenges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            api = MockAPI(root, [Challenge("a", "A", "misc", ""), Challenge("b", "B", "misc", "")])
            runner = Orchestrator(api, root, profiles=("never",), processes=2)
            running = asyncio.create_task(runner.run())
            try:
                for _ in range(300):
                    if len(runner.handles) == 2:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(len(runner.handles), 2)
                runner.apply_control({"action": "stop_challenge", "challenge_key": "a"})
                self.assertFalse(runner.handles[("b", "never-0")]["stop"].is_set())
                states = await asyncio.wait_for(running, 5)
                self.assertEqual(states["a"]["status"], "stopped")
                self.assertEqual(states["b"]["status"], "unsolved")
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    async def test_inflight_acceptance_after_stop_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            api = MockAPI(root, [Challenge("a", "A", "misc", "")])
            started, release = threading.Event(), threading.Event()

            def inflight(key, flag):
                started.set()
                release.wait(5)
                return SubmissionResult(True, "Accepted by the mock server after stop")

            api.submit_flag = inflight
            runner = Orchestrator(api, root, profiles=("never",))
            running = asyncio.create_task(runner.run())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                handle = runner.handles[("a", "never-0")]
                runner.apply_control({"action": "stop_all"})
                await asyncio.wait_for(handle["done"].wait(), 3)
                release.set()
                states = await asyncio.wait_for(running, 5)
                self.assertEqual(states["a"]["status"], "solved")
                results = [json.loads(line) for line in (root / "submissions.jsonl").read_text().splitlines()]
                self.assertTrue(any(r.get("status") == "correct" for r in results))
            finally:
                release.set()
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    async def test_stop_all_prevents_queued_worker_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            api = MockAPI(root, [Challenge("a", "A", "misc", ""), Challenge("b", "B", "misc", "")])
            runner = Orchestrator(api, root, profiles=("never", "never", "never"), processes=1)
            running = asyncio.create_task(runner.run())
            try:
                for _ in range(300):
                    if runner.handles:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(runner.handles)
                runner.apply_control({"action": "stop_all"})
                states = await asyncio.wait_for(running, 5)
                self.assertTrue(all(s["status"] == "stopped" for s in states.values()))
                events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
                self.assertEqual(sum(e["event"] == "worker_started" for e in events), 1)
                self.assertFalse(runner.children)
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    async def test_candidates_are_visible_while_waiting_for_submission_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            api = MockAPI(root, [])
            runner = Orchestrator(api, root)
            runner.challenges["key"] = Challenge("key", "Key", "misc", "")
            runner.solved["key"] = asyncio.Event()
            runner.locks["key"] = asyncio.Lock()
            started, release = threading.Event(), threading.Event()

            def delayed_submit(key, flag):
                started.set()
                release.wait(5)
                return SubmissionResult(False, "Incorrect flag")

            api.submit_flag = delayed_submit
            first = asyncio.create_task(runner.submit("key", "first", "steady", "1"))
            second = None
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                second = asyncio.create_task(runner.submit("key", "second", "burst", "2"))
                for _ in range(100):
                    rows = [json.loads(line) for line in (root / "submissions.jsonl").read_text().splitlines()]
                    if sum(r["event"] == "queued" for r in rows) == 2:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(sum(r["event"] == "queued" for r in rows), 2)
                self.assertEqual(sum(r["event"] == "pending" for r in rows), 1)
            finally:
                release.set()
                await asyncio.gather(*(task for task in (first, second) if task is not None))

    async def test_manual_submission_stops_active_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            api = MockAPI(root, [Challenge("manual", "Manual", "misc", "")])
            runner = Orchestrator(api, root, profiles=("never", "never"))
            task = asyncio.create_task(runner.run())
            while not (root / "run.json").exists():
                await asyncio.sleep(0.01)
            response = await asyncio.to_thread(queue_manual, root, "manual", "fixture-answer", 10)
            states = await task
            self.assertEqual(response["status"], "correct")
            self.assertEqual(states["manual"]["winner"], "operator")
            self.assertFalse(runner.children)

    async def test_journal_failure_does_not_send_and_displays_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            api = MockAPI(root, [])
            runner = Orchestrator(api, root)
            runner.challenges["key"] = Challenge("key", "Key", "misc", "")
            runner.solved["key"] = asyncio.Event()
            runner.locks["key"] = asyncio.Lock()
            with mock.patch.object(api.journal, "record", side_effect=OSError("disk full")), contextlib.redirect_stderr(io.StringIO()) as errors:
                response = await runner.submit("key", "recover-this", "operator", "1")
            self.assertEqual(response["status"], "error")
            self.assertIn("recover-this", errors.getvalue())
            self.assertEqual(api.calls, [])

    async def test_worker_timeout_is_bounded_and_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            api = MockAPI(root, [Challenge("timeout", "Timeout", "misc", "")])
            runner = Orchestrator(api, root, profiles=("never",), worker_timeout=0.001)
            states = await runner.run()
            self.assertEqual(states["timeout"]["status"], "unsolved")
            self.assertFalse(runner.children)
            self.assertIn("worker_timeout", (root / "events.jsonl").read_text())

    async def test_cancel_reaps_all_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            api = MockAPI(root, [Challenge("cancel", "Cancel", "misc", "")])
            runner = Orchestrator(api, root, profiles=("never", "never"))
            task = asyncio.create_task(runner.run())
            while not runner.children:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(runner.children)
            self.assertEqual(json.loads((root / "run.json").read_text())["status"], "stopped")


class ProtocolTests(unittest.TestCase):
    def test_seeded_plan_is_repeatable_and_never_profile_never_solves(self):
        self.assertEqual(plan(42, "a", "steady", "answer"), plan(42, "a", "steady", "answer"))
        self.assertNotEqual(plan(42, "a", "steady", "answer"), plan(43, "a", "steady", "answer"))
        self.assertNotIn("answer", [flag for _, flag in plan(42, "a", "never", "answer")])

    def test_only_literal_loopback_urls(self):
        for url in ("http://example.com", "http://localhost", "http://user:password@127.0.0.1", "ftp://127.0.0.1"):
            with self.assertRaises(ValueError):
                local_url(url)
        self.assertEqual(local_url("http://127.0.0.1:3000/"), "http://127.0.0.1:3000")

    def test_gate_publishes_shared_cooldown(self):
        gate = Gate(0)
        gate.defer(0.04)
        start = time.monotonic()
        threads = [threading.Thread(target=gate.wait) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertGreaterEqual(time.monotonic() - start, 0.035)

    def test_excessive_cooldown_stops_future_admission(self):
        import email.message
        import urllib.error
        with tempfile.TemporaryDirectory() as directory:
            api = LocalAPI("http://127.0.0.1:3000", Gate(0), Journal(Path(directory)))
            headers = email.message.Message()
            headers["Retry-After"] = "3600"
            error = urllib.error.HTTPError(api.base_url, 429, "test", headers, io.BytesIO())
            with mock.patch("demo_orchestrator.urllib.request.build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(RhombusError):
                    api._request("/")
                with self.assertRaisesRegex(RhombusError, "admission stopped"):
                    api._request("/")
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_http_200_errors_are_not_reported_as_incorrect(self):
        with tempfile.TemporaryDirectory() as directory:
            api = LocalAPI("http://127.0.0.1:3000", Gate(0), Journal(Path(directory)))
            with mock.patch.object(LocalAPI.__mro__[1], "submit_flag", return_value=SubmissionResult(False, "Too many solves")):
                with self.assertRaisesRegex(RhombusError, "Too many solves"):
                    api.submit_flag("key", "answer")

    def test_review_includes_crash_pending_but_not_known_incorrect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            journal = Journal(root)
            journal.record("pending", ticket="crash", challenge_key="key", flag="keep-me")
            journal.record("pending", ticket="wrong", challenge_key="key", flag="decoy")
            journal.record("result", ticket="wrong", status="incorrect")
            candidates = review_candidates(root)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["flag"], "keep-me")
            self.assertEqual(candidates[0]["status"], "unknown")

    def test_bounded_429_and_non_429_no_retry(self):
        import email.message
        import urllib.error
        with tempfile.TemporaryDirectory() as directory:
            api = LocalAPI("http://127.0.0.1:3000", Gate(0), Journal(Path(directory)))
            api.max_retries = 2
            headers = email.message.Message()
            headers["Retry-After"] = "0"
            def failure(code):
                return urllib.error.HTTPError(api.base_url, code, "test", headers, io.BytesIO())
            with mock.patch.object(api.gate, "wait"), mock.patch("demo_orchestrator.urllib.request.build_opener") as build:
                build.return_value.open.side_effect = [failure(429) for _ in range(3)]
                with self.assertRaises(RhombusError):
                    api._request("/")
                self.assertEqual(build.return_value.open.call_count, 3)
                build.return_value.open.reset_mock()
                build.return_value.open.side_effect = [failure(500)]
                with self.assertRaises(RhombusError):
                    api._request("/")
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_live_http_429_recovery_and_redirect_refusal(self):
        class Handler(BaseHTTPRequestHandler):
            count = 0

            def log_message(self, *args):
                pass

            def do_GET(self):
                Handler.count += 1
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://example.com/")
                elif Handler.count == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                else:
                    self.send_response(200)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                api = LocalAPI(f"http://127.0.0.1:{server.server_port}", Gate(0), Journal(Path(directory)))
                with api._request("/") as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(Handler.count, 2)
                with self.assertRaises(RhombusError):
                    api._request("/redirect")
                with self.assertRaises(RhombusError):
                    api._request("http://example.com/file")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
