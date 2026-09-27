#!/usr/bin/env python3
import json
import re
import urllib.error
import urllib.request

for ticket_id in range(1001, 1050):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8081/api/tickets/{ticket_id}") as response:
            ticket = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            continue
        raise
    body = ticket.get("body", "")
    match = re.search(r"rhombus\{[^}]+\}", body)
    if match and "do_not_submit" not in match.group():
        print(match.group())
