#!/usr/bin/env python3
from pathlib import Path
import base64
import json
import re

path = Path(__file__).parents[1] / "challenges" / "forensics-script-block" / "windows-events.jsonl"
for line in path.read_text().splitlines():
    event = json.loads(line)
    if event.get("task") != "TelemetryRepair" or event.get("event_id") != 4104:
        continue
    encoded = event["script_block"].rsplit(" ", 1)[1]
    script = base64.b64decode(encoded).decode("utf-16le")
    fragments = re.findall(r"\$[abc]='([^']+)'", script)
    print("".join(fragments))
