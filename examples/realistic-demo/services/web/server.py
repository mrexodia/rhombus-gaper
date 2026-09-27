#!/usr/bin/env python3
"""Intentionally vulnerable local CTF service: insecure direct object reference."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

TICKETS = {
    1001: {
        "owner": "guest",
        "subject": "VPN client fails to update",
        "body": "Resolved after clearing the local package cache.",
        "visibility": "public",
    },
    1002: {
        "owner": "qa-bot",
        "subject": "Flag rendering regression",
        "body": "Test fixture only: rhombus{sample_flag_do_not_submit}",
        "visibility": "public",
    },
    1003: {
        "owner": "mira",
        "subject": "Badge access",
        "body": "Please reactivate building access for the weekend exercise.",
        "visibility": "private",
    },
    1042: {
        "owner": "incident-response",
        "subject": "SEV-1 containment notes",
        "body": "Temporary recovery token: rhombus{idor_means_someone_elses_ticket}",
        "visibility": "restricted",
    },
}

INDEX = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Acme Support</title>
<style>body{font:16px system-ui;max-width:760px;margin:3rem auto;padding:0 1rem}code,pre{background:#eee;padding:.2rem}.ticket{border:1px solid #bbb;padding:1rem;border-radius:.5rem}</style></head>
<body><h1>Acme Support Portal</h1><p>Welcome, <strong>guest</strong>. Your assigned ticket is shown below.</p>
<div id="ticket" class="ticket">Loading ticket 1001...</div>
<script>
fetch('/api/tickets/1001').then(r=>r.json()).then(t=>{
 document.querySelector('#ticket').innerHTML=`<h2>${t.subject}</h2><p>${t.body}</p><small>Owner: ${t.owner}</small>`;
});
</script><p><small>Portal build 2026.09 &middot; API v1</small></p></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AcmeSupport/2026.09"

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send_bytes(200, INDEX, "text/html; charset=utf-8")
        if path == "/health":
            return self.send_bytes(200, b"ok\n", "text/plain")
        if path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /internal\n# rhombus{robots_are_not_access_control}\n"
            return self.send_bytes(200, body, "text/plain")
        if path.startswith("/api/tickets/"):
            try:
                ticket_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                ticket_id = -1
            ticket = TICKETS.get(ticket_id)
            if ticket is None:
                body = json.dumps({"error": "ticket not found"}).encode()
                return self.send_bytes(404, body, "application/json")
            # Vulnerability: the API retrieves arbitrary records without checking
            # whether the authenticated guest owns the requested ticket.
            body = json.dumps({"id": ticket_id, **ticket}, indent=2).encode()
            return self.send_bytes(200, body, "application/json")
        self.send_bytes(404, b"not found\n", "text/plain")

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}", flush=True)


if __name__ == "__main__":
    print("Acme Support listening on 0.0.0.0:8081", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
