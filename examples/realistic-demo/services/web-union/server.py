#!/usr/bin/env python3
"""Intentionally vulnerable local CTF service: SQL injection in an inventory API."""

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB = sqlite3.connect(":memory:", check_same_thread=False)
DB.executescript(
    """
    CREATE TABLE assets(id INTEGER, name TEXT, owner TEXT, note TEXT, public INTEGER);
    INSERT INTO assets VALUES(17, 'guest-laptop', 'guest', 'Issued for contractor onboarding', 1);
    INSERT INTO assets VALUES(18, 'qa-tablet', 'qa', 'rhombus{union_training_record}', 1);
    INSERT INTO assets VALUES(41, 'incident-vault', 'security', 'rhombus{parameterize_every_database_query}', 0);
    INSERT INTO assets VALUES(52, 'backup-hsm', 'infrastructure', 'Rotation complete', 0);
    """
)


class Handler(BaseHTTPRequestHandler):
    server_version = "AcmeInventory/2.8"

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
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self.reply(200, "ok\n")
        if parsed.path == "/":
            body = """<!doctype html><title>Acme Asset Inventory</title>
            <h1>Asset Inventory</h1><p>Guest asset lookup:</p>
            <form action='/api/assets'><input name='owner' value='guest'><button>Search</button></form>
            <p>The API response contains four columns: id, name, owner, and note.</p>"""
            return self.reply(200, body, "text/html; charset=utf-8")
        if parsed.path == "/api/assets":
            owner = parse_qs(parsed.query).get("owner", ["guest"])[0]
            # Vulnerability: attacker input is interpolated directly into SQL.
            query = f"SELECT id,name,owner,note FROM assets WHERE owner = '{owner}' AND public = 1"
            try:
                rows = [dict(zip(("id", "name", "owner", "note"), row)) for row in DB.execute(query)]
                body = json.dumps({"query_status": "ok", "assets": rows}, indent=2)
                return self.reply(200, body, "application/json")
            except sqlite3.Error as error:
                return self.reply(400, json.dumps({"query_status": "error", "detail": str(error)}), "application/json")
        self.reply(404, "not found\n")

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8083), Handler).serve_forever()
