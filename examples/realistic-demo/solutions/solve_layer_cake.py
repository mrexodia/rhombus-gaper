#!/usr/bin/env python3
from pathlib import Path
import base64
import re

path = Path(__file__).parents[1] / "challenges" / "rev-layer-cake" / "updater.ps1"
script = path.read_text()
blob = re.search(r'\$Layer = "([A-Za-z0-9+/=]+)"', script).group(1)
stage = base64.b64decode(blob)[::-1]
print(bytes(value ^ 0x23 for value in stage).decode())
