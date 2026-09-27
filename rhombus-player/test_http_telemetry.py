import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_orchestrator import Gate, Journal, LocalAPI, RhombusError
from http_telemetry import CONTEXT, RequestCancelled

PASSWORD = "NeverPutThisInLogs42"
TOKEN = "super-private-session-token"
FLAG = "private_test_candidate_42"


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        class Handler(BaseHTTPRequestHandler):
            retry_count = 0

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.path == '/signin/credentials':
                    self.send_response(200)
                    self.send_header('Set-Cookie', 'rhombus-token=' + TOKEN + '; HttpOnly')
                    self.send_header('HX-Redirect', '/team')
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    self.wfile.write(b'authentication response omitted')
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain')
                    self.send_header('X-Private-Debug', TOKEN)
                    self.end_headers()
                    self.wfile.write(body + b' cookie=' + TOKEN.encode())

            def do_GET(self):
                if self.path == '/retry' and Handler.retry_count == 0:
                    Handler.retry_count += 1
                    self.send_response(429)
                    self.send_header('Retry-After', '0')
                    body, mime = b'Too many requests', 'text/plain'
                elif self.path == '/error':
                    self.send_response(500)
                    body, mime = b'Database temporarily unavailable', 'text/plain'
                elif self.path == '/partial':
                    self.send_response(200)
                    self.send_header('Content-Length', '100')
                    body, mime = b'partial', 'text/plain'
                elif self.path == '/large-text':
                    self.send_response(200)
                    body, mime = b'private_large_body_' * 400, 'text/plain'
                else:
                    self.send_response(200)
                    body, mime = (b'X' * 10000, 'application/octet-stream') if self.path == '/binary' else (b'OK', 'text/plain')
                self.send_header('Content-Type', mime)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = LocalAPI(f'http://127.0.0.1:{self.server.server_port}', Gate(0), Journal(self.root))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def events(self):
        return [json.loads(line) for line in (self.root / 'http.jsonl').read_text().splitlines()]

    def test_login_and_submission_secrets_are_never_persisted(self):
        self.api.login('private-username', PASSWORD)
        context = CONTEXT.set({'challenge_key': 'key', 'worker': 'steady-0', 'submission_ticket': 'ticket-1'})
        try:
            with self.api._request('/echo', data=urllib.parse.urlencode({'flag': FLAG}).encode(),
                                   headers={'Content-Type': 'application/x-www-form-urlencoded'}) as response:
                self.assertIn(FLAG.encode(), response.read())  # logging does not change client-visible bytes
        finally:
            CONTEXT.reset(context)
        raw = (self.root / 'http.jsonl').read_text()
        for secret in (PASSWORD, TOKEN, FLAG, 'private-username'):
            self.assertNotIn(secret, raw)
        events = self.events()
        self.assertTrue(any(e.get('submission_ticket') == 'ticket-1' for e in events))
        complete = [e for e in events if e['event'] == 'http_complete']
        self.assertEqual(complete[0]['body_omitted'], 'authentication response body omitted')
        self.assertIn('[redacted]', complete[1]['body_preview'])
        self.assertEqual(sum(e['event'] == 'http_queued' for e in events), 2)

    def test_429_retry_and_http_error_have_correlated_responses(self):
        with mock.patch.object(self.api.gate, 'wait'):
            with self.api._request('/retry') as response:
                self.assertEqual(response.read(), b'OK')
            with self.assertRaises(RhombusError):
                self.api._request('/error')
        events = self.events()
        queued = [e for e in events if e['event'] == 'http_queued']
        self.assertEqual([e['attempt'] for e in queued], [1, 2, 1])
        self.assertEqual(queued[0]['operation_id'], queued[1]['operation_id'])
        self.assertNotEqual(queued[0]['request_id'], queued[1]['request_id'])
        self.assertEqual([e['status'] for e in events if e['event'] == 'http_response'], [429, 200, 500])
        self.assertTrue(any(e.get('body_preview') == 'Database temporarily unavailable' for e in events))
        self.assertTrue(any(e.get('body_preview') == 'Too many requests' for e in events))

    def test_binary_is_not_dumped_and_original_stream_remains_intact(self):
        with self.api._request('/binary') as response:
            output = io.BytesIO()
            while chunk := response.read(1024):
                output.write(chunk)
        self.assertEqual(output.getvalue(), b'X' * 10000)
        complete = next(e for e in self.events() if e['event'] == 'http_complete')
        self.assertEqual(complete['bytes_read'], 10000)
        self.assertTrue(complete['read_complete'])
        self.assertIsNone(complete['body_preview'])
        self.assertNotIn('X' * 100, (self.root / 'http.jsonl').read_text())

    def test_partial_read_errors_and_oversized_text_are_not_dumped(self):
        import http.client
        with self.assertRaises(http.client.IncompleteRead):
            with self.api._request('/partial') as response:
                response.read()
        with self.assertRaises(http.client.IncompleteRead):
            with self.api._request('/partial') as response:
                response.read(1024)  # streamed attachments must reject premature EOF too
        with self.api._request('/large-text') as response:
            self.assertGreater(len(response.read()), 4096)
        complete = [e for e in self.events() if e['event'] == 'http_complete']
        for item in complete[:2]:
            self.assertTrue(item['read_error'])
            self.assertFalse(item['read_complete'])
            self.assertIsNone(item['body_preview'])
        self.assertIsNone(complete[2]['body_preview'])
        self.assertIn('4096', complete[2]['body_omitted'])
        self.assertNotIn('private_large_body_', (self.root / 'http.jsonl').read_text())

    def test_stop_cancels_before_admission_and_does_not_send(self):
        cancel = threading.Event()
        cancel.set()
        context = CONTEXT.set({'cancel': cancel})
        try:
            with mock.patch('demo_orchestrator.urllib.request.build_opener') as opener:
                with self.assertRaises(RequestCancelled):
                    self.api._request('/never-sent')
                opener.assert_not_called()
        finally:
            CONTEXT.reset(context)
        self.assertEqual([e['event'] for e in self.events()], ['http_queued', 'http_blocked'])

    def test_transport_errors_are_logged_without_exception_secrets(self):
        with mock.patch('demo_orchestrator.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = urllib.error.URLError(PASSWORD)
            with self.assertRaises(RhombusError):
                self.api._request('/unreachable')
        self.assertNotIn(PASSWORD, (self.root / 'http.jsonl').read_text())
        self.assertEqual(self.events()[-1]['event'], 'http_error')
