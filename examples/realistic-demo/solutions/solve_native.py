#!/usr/bin/env python3
from pathlib import Path
import os
import subprocess

expected = bytes([
    0x00, 0x10, 0x68, 0x68, 0x58, 0x60, 0x01, 0x31, 0x88, 0x68, 0x80, 0x58,
    0xF0, 0x78, 0x42, 0x01, 0xB0, 0x88, 0xA0, 0x6A, 0xA8, 0x91, 0x19, 0x11,
    0x09, 0x32, 0xD8, 0x01, 0x81, 0xBA, 0xF0, 0xD9, 0x41, 0x19, 0xC1, 0x91,
])
key = b"ridge"

def ror8(value, amount):
    return ((value >> amount) | (value << (8 - amount))) & 0xFF

flag = bytes(((ror8(value, 3) - i) & 0xFF) ^ key[i % len(key)] for i, value in enumerate(expected))
print(flag.decode())

# Optional verification when running on Linux/WSL.
binary = Path(__file__).parents[1] / "challenges" / "rev-native-dance" / "badge-checker"
if os.name != "nt" and subprocess.run([str(binary), flag.decode()], capture_output=True).returncode != 0:
    raise RuntimeError("recovered code was rejected")
