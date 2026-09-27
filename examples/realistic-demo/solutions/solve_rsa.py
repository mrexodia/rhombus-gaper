#!/usr/bin/env python3
from pathlib import Path
import json
import math

path = Path(__file__).parents[1] / "challenges" / "crypto-common-ground" / "enrollments.json"
records = json.loads(path.read_text())["records"]
for i, left in enumerate(records):
    for right in records[i + 1 :]:
        n1, n2 = int(left["n"], 16), int(right["n"], 16)
        shared = math.gcd(n1, n2)
        if shared == 1:
            continue
        for record in (left, right):
            modulus = int(record["n"], 16)
            other = modulus // shared
            phi = (shared - 1) * (other - 1)
            private = pow(record["e"], -1, phi)
            message = pow(int(record["ciphertext"], 16), private, modulus)
            plaintext = message.to_bytes((message.bit_length() + 7) // 8, "big")
            if b"rhombus{" in plaintext:
                print(plaintext.decode())
