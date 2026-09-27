import json
import os
import re
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_dashboard import DashboardServer, Snapshot
from demo_orchestrator import Journal, private_json


TEST_FLAG = "rhombus{dashboard_test_fixture_only}"


def populate(root):
    (root / "inbox").mkdir()
    (root / "outbox").mkdir()
    catalog = [
        {"id": "clockwork", "name": "Clockwork Signatures", "category": "crypto", "attachments": [{"name": "capture.bin"}]},
        {"id": "ticket", "name": "Ticket Trouble", "category": "web", "attachments": []},
        {"id": "stream", "name": "Reset the Stream", "category": "crypto", "attachments": []},
        {"id": "dns", "name": "DNS Diary", "category": "forensics", "attachments": []},
        {"id": "layer", "name": "Layer Cake", "category": "reversing", "attachments": []},
        {"id": "echo", "name": "Echo Service", "category": "pwn", "attachments": []},
    ]
    private_json(root / "catalog.json", catalog)
    private_json(root / "run.json", {"status": "running", "source": "http://127.0.0.1:3000", "challenge_keys": [c["id"] for c in catalog]})
    private_json(root / "live.json", {"time": time.time(), "states": {"dns": {"status": "needs_review"}}})
    journal = Journal(root)
    for c in catalog:
        journal.record("ready", challenge_key=c["id"])
    journal.record("download_finished", challenge_key="clockwork", file="000-capture.bin", bytes=1024)
    for key in ["clockwork", "ticket", "stream"]:
        for index, profile in enumerate(["burst", "steady"]):
            journal.record("worker_started", challenge_key=key, worker=profile + "-" + str(index), pid=4200 + index)
    journal.record("queued", ticket="q1", challenge_key="ticket", worker="burst-0", flag=TEST_FLAG, request_id="1")
    journal.record("pending", ticket="q2", challenge_key="clockwork", worker="steady-1", flag="candidate-two", request_id="2")
    journal.record("pending", ticket="err", challenge_key="dns", worker="operator", flag=TEST_FLAG, request_id="manual")
    journal.record("result", ticket="err", status="error", message="Connection lost; candidate " + TEST_FLAG)
    journal.record("pending", ticket="win", challenge_key="layer", worker="steady-1", flag="replay-answer")
    journal.record("result", ticket="win", status="correct", message="Challenge solved")
    journal.record("solved", challenge_key="layer", winner="steady-1")
    journal.record("rate_limit", wait=1.05, attempt=1)
    private_json(root / "inbox" / "manual.json", {"challenge_key": "dns", "flag": TEST_FLAG})
    private_json(root / "inbox" / "fresh.json", {"challenge_key": "echo", "flag": "manual-candidate"})


class SnapshotTests(unittest.TestCase):
    def test_queue_join_counts_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            populate(root)
            data = Snapshot(root).build()
            self.assertEqual(data["run_status"], "running")
            self.assertEqual(data["counts"], {"queued": 2, "in_flight": 1, "error": 1, "correct": 1})
            self.assertEqual(len(data["workers"]), 6)
            self.assertEqual(data["rate_limits"], 1)
            self.assertEqual(len(data["submissions"]), 5)  # operator inbox deduplicated
            self.assertNotIn(TEST_FLAG, json.dumps(data))
            self.assertIn(TEST_FLAG, json.dumps(Snapshot(root).build(reveal=True)))
            self.assertEqual(next(c for c in data["challenges"] if c["id"] == "layer")["status"], "solved")

    def test_short_candidate_does_not_corrupt_protocol_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            populate(root)
            journal = Journal(root)
            journal.record("queued", ticket="short", challenge_key="dns", flag="error")
            journal.record("result", ticket="short", status="error", message="error")
            item = next(s for s in Snapshot(root).build()["submissions"] if s["ticket"] == "short")
            self.assertEqual(item["status"], "error")
            self.assertEqual(item["flag"], "••••••••")
            self.assertEqual(item["message"], "[redacted]")

    def test_stale_run_never_claims_processes_are_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            populate(root)
            private_json(root / "live.json", {"time": time.time() - 20, "states": {}})
            data = Snapshot(root).build()
            self.assertEqual(data["run_status"], "stale")
            self.assertTrue(all(w["status"] == "unconfirmed" for w in data["workers"]))
            self.assertEqual(data["counts"]["unknown"], 3)
            self.assertEqual(data["cooldown"], 0)

    def test_waiting_partial_lines_and_outside_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(Snapshot(root).build()["run_status"], "waiting")
            (root / "events.jsonl").write_text('{"event":"ready"}\n{"event":', encoding="utf-8")
            data = Snapshot(root).build()
            self.assertEqual(data["event_count"], 1)
            self.assertTrue(data["warnings"])
            reader = Snapshot(root)
            self.assertIsNone(reader.read("../outside.json"))
            self.assertTrue(reader.warnings)

    def test_stopped_summary_and_legacy_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            populate(root)
            (root / "catalog.json").unlink()
            folder = root / "challenges" / "example"
            folder.mkdir(parents=True)
            private_json(folder / "challenge.json", {"challenge_key": "layer", "challenge": {"name": "Layer Cake", "category": "rev"}, "files": ["artifact.bin"]})
            private_json(root / "summary.json", {"layer": {"status": "solved", "winner": "steady-1"}})
            run = json.loads((root / "run.json").read_text())
            run["status"] = "stopped"
            private_json(root / "run.json", run)
            data = Snapshot(root).build()
            self.assertEqual(data["run_status"], "stopped")
            layer = next(c for c in data["challenges"] if c["id"] == "layer")
            self.assertEqual(layer["downloaded"], 1)
            self.assertEqual(layer["name"], "Layer Cake")


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        populate(self.root)
        self.server = DashboardServer(self.root, port=0, token="test-access-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, path, auth=True, **headers):
        if auth:
            headers["Authorization"] = "Bearer " + self.server.token
        return self.opener.open(urllib.request.Request(self.server.origin + path, headers=headers), timeout=5)

    def test_auth_origin_host_and_no_arbitrary_files(self):
        for path, headers, expected in [
            ("/api/state", {}, 401),
            ("/api/state", {"Origin": "https://example.com"}, 403),
            ("/api/state", {"Host": "example.com"}, 403),
            ("/../submissions.jsonl", {}, 404),
            ("/submissions.jsonl", {}, 404),
        ]:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path, auth=False, **headers)
            self.assertEqual(caught.exception.code, expected)
            caught.exception.close()
        with self.request("/api/state") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn(TEST_FLAG, response.read().decode())
        with self.request("/api/state?reveal=1") as response:
            self.assertIn(TEST_FLAG, response.read().decode())
        with self.request("/", auth=False) as response:
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertNotIn(TEST_FLAG, response.read().decode())
        with self.request("/app.js", auth=False) as response:
            self.assertNotIn("innerHTML", response.read().decode())


@unittest.skipUnless(os.environ.get("RHOMBUS_BROWSER_TESTS") == "1", "optional Playwright browser checks")
class BrowserTests(HTTPTests):
    def test_desktop_mobile_live_queue_and_sensitive_controls(self):
        from playwright.sync_api import sync_playwright, expect
        with sync_playwright() as playwright:
            executable = os.environ.get("RHOMBUS_BROWSER_EXE", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            stop = threading.Event()

            def heartbeat():
                while not stop.wait(0.5):
                    private_json(self.root / "live.json", {"time": time.time(), "states": {"dns": {"status": "needs_review"}}})

            pulse = threading.Thread(target=heartbeat, daemon=True)
            pulse.start()
            try:
                page = browser.new_page(viewport={"width": 1512, "height": 1100})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                page.goto(self.server.origin + "/#token=" + self.server.token)
                expect(page.locator('#submission-count')).to_have_text('5 / 5')
                self.assertEqual(page.locator(".challenge").count(), 6)
                self.assertNotIn(TEST_FLAG, page.locator("body").inner_text())
                self.assertNotIn("token=", page.url)
                page.get_by_role("button", name="Queue", exact=True).click()
                self.assertEqual(page.locator("#submissions tr").count(), 3)
                page.get_by_role("button", name="Needs review", exact=True).click()
                self.assertEqual(page.locator("#submissions tr").count(), 1)
                page.get_by_label("Reveal flags").check()
                expect(page.locator('.flag-value').first).to_contain_text('dashboard_test_fixture')
                page.get_by_role("button", name="Pause feed", exact=True).click()
                page.get_by_label("Reveal flags").uncheck()
                self.assertNotIn(TEST_FLAG, page.locator("body").inner_text())
                page.get_by_role("button", name="Resume feed", exact=True).click()
                page.get_by_role("button", name="All", exact=True).click()
                Journal(self.root).record("result", ticket="q1", status="incorrect", message="Incorrect flag")
                expect(page.locator('#m-queue')).to_have_text('2')
                # Organizer-controlled text must be inert, not HTML.
                catalog = json.loads((self.root / "catalog.json").read_text())
                catalog[0]["name"] = '<img src=x onerror="window.bad=1">'
                private_json(self.root / "catalog.json", catalog)
                expect(page.locator('.challenge h3').first).to_have_text(re.compile(r'<img'))
                self.assertIsNone(page.evaluate("window.bad"))
                self.assertEqual(page.locator(".challenge img").count(), 0)
                catalog[0]["name"] = "Clockwork Signatures"
                private_json(self.root / "catalog.json", catalog)
                expect(page.locator('.challenge h3').first).to_have_text('Clockwork Signatures')
                output = os.environ.get("RHOMBUS_DASHBOARD_SCREENSHOTS")
                if output:
                    Path(output).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(output) / "dashboard-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                if output:
                    page.screenshot(path=str(Path(output) / "dashboard-mobile.png"), full_page=True)
                self.assertFalse(errors, errors)
            finally:
                stop.set()
                pulse.join()
                browser.close()
