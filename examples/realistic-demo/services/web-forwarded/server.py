#!/usr/bin/env python3
"""Intentionally vulnerable local CTF service: trusted proxy-header bypass."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

FLAG = "rhombus{forwarded_headers_are_user_input}"


class Handler(BaseHTTPRequestHandler):
    server_version = "AcmeDeploy/4.2"

    def reply(self, status, body, kind="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self.reply(200, "ok\n")
        if path == "/":
            body = """<!doctype html><title>Acme Deployment Dashboard</title>
            <h1>Deployment Dashboard</h1><p>Environment: production</p>
            <p>Administrative diagnostics are restricted to the internal proxy.</p>
            <a href='/debug'>Client diagnostics</a>"""
            return self.reply(200, body, "text/html; charset=utf-8")
        if path == "/debug":
            data = {
                "remote_address": self.client_address[0],
                "forwarded_for": self.headers.get("X-Forwarded-For"),
                "test_token": "rhombus{proxy_test_fixture_only}",
            }
            return self.reply(200, json.dumps(data, indent=2), "application/json")
        if path == "/admin/diagnostics":
            # Vulnerability: this service is directly reachable, but blindly
            # treats a user-controlled forwarding header as the client IP.
            client_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
            if client_ip != "127.0.0.1":
                return self.reply(403, "internal clients only\n")
            data = {"status": "maintenance override enabled", "recovery_token": FLAG}
            return self.reply(200, json.dumps(data, indent=2), "application/json")
        self.reply(404, "not found\n")

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8082), Handler).serve_forever()
