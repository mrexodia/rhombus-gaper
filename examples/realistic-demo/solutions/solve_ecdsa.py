#!/usr/bin/env python3
from pathlib import Path
import hashlib
import json

ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
path = Path(__file__).parents[1] / "challenges" / "crypto-clockwork-signatures" / "signatures.json"
data = json.loads(path.read_text())
first, second = data["signatures"]
r = int(first["r"], 16)
s1, s2 = int(first["s"], 16), int(second["s"], 16)
z1 = int.from_bytes(hashlib.sha256(first["message"].encode()).digest(), "big")
z2 = int.from_bytes(hashlib.sha256(second["message"].encode()).digest(), "big")
nonce = (z1 - z2) * pow((s1 - s2) % ORDER, -1, ORDER) % ORDER
private = (s1 * nonce - z1) * pow(r, -1, ORDER) % ORDER
pad = hashlib.sha256(private.to_bytes(32, "big")).digest()
ciphertext = bytes.fromhex(data["encrypted_recovery_record"])
plaintext = bytes(value ^ pad[i % len(pad)] for i, value in enumerate(ciphertext))
print(plaintext.decode())
