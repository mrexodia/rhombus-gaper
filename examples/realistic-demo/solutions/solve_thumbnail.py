#!/usr/bin/env python3
from pathlib import Path
import io
import re
import zipfile

path = Path(__file__).parents[1] / "challenges" / "forensics-thumbnail-tail" / "recovered-thumbnail.png"
data = path.read_bytes()
archive_offset = data.index(b"PK\x03\x04")
with zipfile.ZipFile(io.BytesIO(data[archive_offset:])) as archive:
    note = archive.read("case-notes/incident.txt")
print(re.search(rb"rhombus\{[^}]+\}", note).group().decode())
