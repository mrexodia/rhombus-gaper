#!/usr/bin/env python3
from pathlib import Path
import base64
import struct

path = Path(__file__).parents[1] / "challenges" / "forensics-dns-diary" / "workstation.pcap"
data = path.read_bytes()
offset = 24
chunks = {}
while offset < len(data):
    _, _, captured, _ = struct.unpack_from("<IIII", data, offset)
    packet = data[offset + 16 : offset + 16 + captured]
    offset += 16 + captured
    # Ethernet (14), IPv4 without options (20), UDP (8), DNS header (12).
    cursor = 54
    labels = []
    while cursor < len(packet) and packet[cursor]:
        length = packet[cursor]
        cursor += 1
        labels.append(packet[cursor : cursor + length].decode("ascii"))
        cursor += length
    if len(labels) >= 4 and labels[-3:] == ["telemetry", "example", "com"]:
        chunks[int(labels[0])] = labels[1]

encoded = "".join(chunks[i] for i in sorted(chunks)).upper()
encoded += "=" * (-len(encoded) % 8)
print(base64.b32decode(encoded).decode())
