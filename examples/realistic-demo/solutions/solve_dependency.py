#!/usr/bin/env python3
from pathlib import Path
import re
import zipfile

path = Path(__file__).parents[1] / "challenges" / "misc-dependency-detour" / "vendor-cache.zip"
with zipfile.ZipFile(path) as archive:
    suspicious = [name for name in archive.namelist() if "/requsets-" in name and name.endswith("install.js")]
    script = archive.read(suspicious[0]).decode()
parts = re.findall(r"const [abc] = '([^']+)'", script)
print("".join(parts))
