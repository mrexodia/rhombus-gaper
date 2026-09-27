#!/usr/bin/env python3
"""Token-protected loopback dashboard with scoped fake-worker stop controls."""
from __future__ import annotations

import argparse
from collections import Counter
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import time
import urllib.parse
import uuid
import webbrowser

from demo_orchestrator import CONTROL_FIELDS, private_json

ASSETS = Path(__file__).with_name("dashboard")
MAX_FILE = 16 * 1024 * 1024


class Snapshot:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.warnings = []

    def read(self, relative, lines=False, default=None):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            self.warnings.append("Refused a file outside the selected run directory")
            return [] if lines else default
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE + 1)
            if len(raw) > MAX_FILE:
                self.warnings.append(f"{relative}: exceeds dashboard read limit; inspect locally")
                return [] if lines else default
            text = raw.decode("utf-8")
            if not lines:
                return json.loads(text)
            records = []
            for line in text.splitlines():
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        records.append(item)
                except json.JSONDecodeError:
                    self.warnings.append(f"{relative}: incomplete or malformed line; will retry")
            return records
        except FileNotFoundError:
            return [] if lines else default
        except (OSError, UnicodeError, ValueError):
            self.warnings.append(f"{relative}: temporarily unreadable")
            return [] if lines else default

    def build(self, reveal=False):
        run = self.read("run.json", default={})
        live = self.read("live.json", default={})
        summary = self.read("summary.json", default={})
        catalog = self.read("catalog.json", default=[])
        events = self.read("events.jsonl", lines=True)
        records = self.read("submissions.jsonl", lines=True)
        http_events = self.read("http.jsonl", lines=True)
        now = time.time()
        if live.get("control_error"):
            self.warnings.append("Operator controls unavailable: " + str(live["control_error"]))
        state = run.get("status", "waiting")
        heartbeat = live.get("time")
        if state == "running" and (not isinstance(heartbeat, (int, float)) or abs(now - heartbeat) > 5):
            state = "stale"
        challenges = {key: {"id": key, "name": key, "category": "unknown", "status": "downloading",
                             "workers": [], "files": 0, "downloaded": 0, "submissions": 0}
                      for key in run.get("challenge_keys", [])}
        for item in catalog:
            key = item.get("id")
            if key in challenges:
                challenges[key].update(name=item.get("name", key), category=item.get("category", "unknown"),
                                       points=item.get("points"), description=item.get("description", ""),
                                       files=len(item.get("attachments", [])))
        # Backward compatibility for runs created before catalog/heartbeat telemetry.
        if not catalog:
            for path in sorted((self.root / "challenges").glob("*/challenge.json")):
                manifest = self.read(path.relative_to(self.root), default={})
                item = manifest.get("challenge", {})
                key = manifest.get("challenge_key")
                if key in challenges:
                    challenges[key].update(name=item.get("name", key), category=item.get("category", "unknown"),
                                           files=len(manifest.get("files", [])), downloaded=len(manifest.get("files", [])))
        workers = {}
        rate_limits = []
        for event in events:
            key, kind = event.get("challenge_key"), event.get("event")
            challenge = challenges.get(key)
            identity = (key, event.get("worker"))
            if kind == "worker_started":
                workers[identity] = {"challenge_key": key, "worker": event.get("worker"),
                                     "pid": event.get("pid"), "time": event.get("time"), "status": "active"}
            elif kind == "worker_stopped" and identity in workers:
                workers[identity].update(status="stopped", code=event.get("code"),
                                         reason=event.get("reason", "exited"), ended_at=event.get("time"))
            elif kind == "rate_limit":
                rate_limits.append(event)
            if challenge:
                if kind == "ready":
                    challenge["status"] = "ready"
                elif kind == "solved":
                    challenge.update(status="solved", winner=event.get("winner"))
                elif kind == "challenge_error":
                    challenge.update(status="error", message=event.get("message"))
                elif kind == "download_finished":
                    challenge["downloaded"] += 1
        for key, value in (summary or live.get("states", {})).items():
            if key in challenges:
                # Live heartbeat can lag authoritative solve events by one second.
                if challenges[key]["status"] != "solved" or value.get("status") == "solved":
                    challenges[key].update(value)
        active = []
        for worker in workers.values():
            if worker["status"] == "active":
                worker["status"] = "active" if state == "running" else "unconfirmed"
                active.append(worker)
                if worker["challenge_key"] in challenges:
                    c = challenges[worker["challenge_key"]]
                    c["workers"].append(worker)
                    if c["status"] == "ready" and state == "running":
                        c["status"] = "racing"
        submissions = {}
        for record in records:
            ticket = record.get("ticket")
            if not ticket:
                continue
            item = submissions.setdefault(ticket, {"ticket": ticket})
            if record.get("event") in {"queued", "pending"}:
                first_time = item.get("time", record.get("time"))
                item.update(record)
                item["time"] = first_time
                item["status"] = "queued" if record["event"] == "queued" else "in_flight"
            elif record.get("event") == "result":
                item.update({k: v for k, v in record.items() if k not in {"event", "time"}})
                item["finished_at"] = record.get("time")
        manual_ids = {s.get("request_id") for s in submissions.values() if s.get("worker") == "operator"}
        for path in sorted((self.root / "inbox").glob("*.json")):
            if path.stem in manual_ids:
                continue
            item = self.read(path.relative_to(self.root), default={})
            response = self.read(Path("outbox") / path.name, default={})
            if item:
                submissions["manual-" + path.stem] = {**item, **response, "ticket": "manual-" + path.stem,
                                                    "worker": "operator", "status": response.get("status", "queued")}
        for item in submissions.values():
            if item.get("status") in {"queued", "in_flight"} and state != "running":
                item.update(status="unknown", message="No confirmed result. Reconcile before retrying.")
            challenge = challenges.get(item.get("challenge_key"))
            if challenge:
                challenge["submissions"] += 1
        requests = {}
        for event in http_events:
            request_id = event.get("request_id")
            if not request_id:
                continue
            item = requests.setdefault(request_id, {"request_id": request_id, "time": event.get("time")})
            kind = event.get("event", "")
            item.update({k: v for k, v in event.items() if k not in {"event", "time"}})
            item["updated_at"] = event.get("time")
            item["stage"] = {"http_queued": "queued", "http_sent": "sent", "http_response": "response",
                             "http_error": "error", "http_blocked": "blocked", "http_complete": "complete"}.get(kind, kind)
            if item.get("read_error"):
                item["stage"] = "error"
        for item in requests.values():
            if state != "running" and item["stage"] in {"queued", "sent", "response"}:
                item["observed_stage"] = item["stage"]
                item["stage"] = "unknown"
        controls = []
        for path in sorted((self.root / "control_requests").glob("*.json")):
            request = self.read(path.relative_to(self.root), default={})
            response = self.read(Path("control_results") / path.name, default={})
            entry = {**request, **response, "control_id": path.stem,
                     "status": response.get("status", "queued" if state == "running" else "unknown")}
            if entry["status"] == "acknowledged":
                matched = entry.get("matched", [])
                if all(any(w["challenge_key"] == m["challenge_key"] and w["worker"] == m["worker"]
                           and w["pid"] == m["pid"] and w["status"] == "stopped" for w in workers.values()) for m in matched):
                    entry["status"] = "completed"
                    entry["message"] = "Matched workers exited; scoped future worker starts are suppressed. In-flight requests may still settle."
                elif state != "running":
                    entry["status"] = "unknown"
                    entry["message"] = "Acknowledged, but not all worker exits were observed. Inspect the event log."
            controls.append(entry)
        counts = Counter(s.get("status", "unknown") for s in submissions.values())
        latest_rate = max((e.get("time", 0) + e.get("wait", 0) for e in rate_limits), default=0)
        payload = {"generated_at": now, "run_name": self.root.name, "run_status": state,
                   "source": run.get("source", "Awaiting run"), "heartbeat": heartbeat,
                   "challenges": list(challenges.values()), "workers": active,
                   "worker_history": list(workers.values()),
                   "can_control": state == "running" and live.get("control_protocol") == 1 and not live.get("stop_all"),
                   "controls": sorted(controls, key=lambda c: c.get("time", 0), reverse=True),
                   "requests": sorted(requests.values(), key=lambda r: r.get("time", 0), reverse=True),
                   "request_count": len(requests),
                   "submissions": sorted(submissions.values(), key=lambda s: s.get("time") or 0, reverse=True),
                   "events": events[-300:][::-1], "event_count": len(events),
                   "counts": dict(counts), "rate_limits": len(rate_limits),
                   "cooldown": max(0, latest_rate - now) if state == "running" else 0,
                   "warnings": list(dict.fromkeys(self.warnings)), "revealed": reveal}
        if not reveal:
            flags = {s["flag"] for s in submissions.values() if isinstance(s.get("flag"), str) and s["flag"]}

            def redact(value, key=None):
                if isinstance(value, dict):
                    return {k: ("••••••••" if k == "flag" else redact(v, k)) for k, v in value.items()}
                if isinstance(value, list):
                    return [redact(v, key) for v in value]
                # Keep protocol statuses/IDs intact even if a candidate is literally "error".
                if isinstance(value, str) and key in {"message", "error", "persistence_warning"}:
                    for flag in sorted(flags, key=len, reverse=True):
                        value = value.replace(flag, "[redacted]")
                return value

            payload = redact(payload)
        return payload


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root, port=8765, token=None):
        self.root = Path(root).resolve()
        self.token = token or secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log authorization headers, candidate flags, or browser URLs.

    def respond(self, code, body, content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(body)

    def authorize(self, token=False, write=False):
        if self.headers.get("Host") != urllib.parse.urlsplit(self.server.origin).netloc:
            self.respond(403, "Invalid host")
            return False
        origin = self.headers.get("Origin")
        if (write and origin != self.server.origin) or (origin and origin != self.server.origin):
            self.respond(403, "Cross-origin access denied")
            return False
        authorization = self.headers.get("Authorization", "")
        if token and not hmac.compare_digest(authorization.encode(), ("Bearer " + self.server.token).encode()):
            self.respond(401, "Dashboard token required")
            return False
        return True

    def do_GET(self):
        if not self.authorize():
            return
        path = urllib.parse.urlsplit(self.path)
        if path.path == "/api/state":
            if not self.authorize(token=True):
                return
            try:
                reveal = urllib.parse.parse_qs(path.query).get("reveal") == ["1"]
                body = json.dumps(Snapshot(self.server.root).build(reveal=reveal), ensure_ascii=True)
                return self.respond(200, body, "application/json")
            except Exception:
                return self.respond(503, "Telemetry temporarily unavailable; inspect local run files")
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/favicon.svg": ("favicon.svg", "image/svg+xml"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path.path in assets:
            name, mime = assets[path.path]
            return self.respond(200, (ASSETS / name).read_bytes(), mime)
        self.respond(404, "Not found")

    def do_POST(self):
        if not self.authorize(token=True, write=True):
            return
        if self.path != "/api/control":
            return self.respond(404, "Not found")
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            return self.respond(415, "JSON with a Content-Length is required")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                return self.respond(413, "Control payload must be 1..4096 bytes")
            self.connection.settimeout(5)
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict) or set(value) - {"action", "challenge_key", "worker", "pid"}:
                raise ValueError("unsupported control fields")
            action = value.get("action")
            if action not in CONTROL_FIELDS or set(value) != CONTROL_FIELDS[action]:
                raise ValueError("only scoped stop actions with exact fields are supported")
            snapshot = Snapshot(self.server.root).build()
            if not snapshot["can_control"]:
                return self.respond(409, "Run is stopped/stale, stopping globally, or does not support controls")
            if action != "stop_all" and value.get("challenge_key") not in {c["id"] for c in snapshot["challenges"]}:
                raise ValueError("unknown challenge")
            if action == "stop_worker" and (type(value.get("pid")) is not int or not any(
                    w["challenge_key"] == value.get("challenge_key") and w["worker"] == value.get("worker")
                    and w["pid"] == value["pid"] for w in snapshot["workers"])):
                return self.respond(409, "Worker/PID no longer active; refresh first")
            folder = (self.server.root / "control_requests").resolve()
            if not folder.is_relative_to(self.server.root) or not folder.is_dir():
                return self.respond(409, "Control inbox unavailable")
            if sum(c["status"] == "queued" for c in snapshot["controls"]) >= 100:
                return self.respond(429, "Too many unacknowledged operator controls")
            control_id = uuid.uuid4().hex
            private_json(folder / (control_id + ".json"), {**value, "time": time.time()})
            self.respond(202, json.dumps({"control_id": control_id, "status": "queued",
                                         "message": "Queued, not yet acknowledged. Watch control results and worker exits."}), "application/json")
        except (ValueError, TypeError):
            self.respond(400, "Invalid control payload")
        except OSError:
            self.respond(503, "Control could not be persisted; no stop is confirmed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="existing or planned private run folder")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open the private dashboard link in your browser")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be 0..65535 (0 chooses an available port)")
    with DashboardServer(args.run_dir, args.port) as server:
        url = server.origin + "/#token=" + server.token
        print("SIGNAL // COMMAND — local simulation telemetry", flush=True)
        print("Private access link (do not share): " + url, flush=True)
        print("Scoped fake-worker stop controls enabled. Ctrl+C stops only the dashboard.", flush=True)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
