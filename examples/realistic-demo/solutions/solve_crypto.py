#!/usr/bin/env python3
from pathlib import Path

KNOWN = b"KNOWN: packet[0]=telemetry-v1; status=nominal; seq=0001"
path = Path(__file__).parents[1] / "challenges" / "crypto-reset-the-stream" / "transmissions.txt"
records = {}
for line in path.read_text().splitlines():
    if line and not line.startswith("#"):
        name, value = line.split("=", 1)
        records[name] = bytes.fromhex(value)

keystream = bytes(a ^ b for a, b in zip(records["known"], KNOWN))
plaintext = bytes(a ^ b for a, b in zip(records["secret"], keystream))
print(plaintext.decode())
