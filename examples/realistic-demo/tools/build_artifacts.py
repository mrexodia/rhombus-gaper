#!/usr/bin/env python3
"""Build deterministic, offline artifacts for the realistic demo challenges."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import py_compile
import shutil
import socket
import struct
import subprocess
import tempfile
import textwrap
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHALLENGES = ROOT / "challenges"


def xor_bytes(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def stream_bytes(length: int) -> bytes:
    # This models a stream cipher whose key/nonce pair is incorrectly reset for
    # every packet. The construction itself is not the intended vulnerability.
    seed = b"signal-ridge-sensor-key|boot-nonce-0000"
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(output[:length])


def build_crypto() -> None:
    known = b"KNOWN: packet[0]=telemetry-v1; status=nominal; seq=0001"
    messages = {
        "known": known,
        "ops": b"rhombus{factory_test_fixture}; pressure=101.3; seq=0002",
        "secret": b"rhombus{never_reuse_a_stream_cipher_nonce}",
        "noise": b"ALERT: enclosure opened; dispatch technician; seq=0004",
    }
    longest = max(map(len, messages.values()))
    stream = stream_bytes(longest)
    lines = [
        "# Signal Ridge sensor capture",
        "# Each record begins at stream offset zero after a device reboot.",
    ]
    lines.extend(f"{name}={xor_bytes(value, stream).hex()}" for name, value in messages.items())
    target = CHALLENGES / "crypto-reset-the-stream" / "transmissions.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_reverse() -> None:
    flag = "rhombus{bytecode_is_not_a_black_box}"
    expected = [((ord(ch) ^ 0x5A) + i * 7) & 0xFF for i, ch in enumerate(flag)]
    source = textwrap.dedent(
        f'''\
        # Distributed as bytecode only. These strings are deliberate bait.
        SUPPORT_TOKEN = "rhombus{{decompiler_demo_license}}"
        LEGACY_TEST = "flag{{bytecode_strings_are_not_the_answer}}"
        EXPECTED = {expected!r}

        def transform(candidate):
            return [((ord(ch) ^ 0x5A) + i * 7) & 0xFF for i, ch in enumerate(candidate)]

        def valid(candidate):
            return len(candidate) == len(EXPECTED) and transform(candidate) == EXPECTED

        if __name__ == "__main__":
            candidate = input("License key: ").strip()
            print("accepted" if valid(candidate) else "denied")
        '''
    )
    target_dir = CHALLENGES / "rev-bytecode-breakout"
    with tempfile.TemporaryDirectory() as temp:
        source_path = Path(temp) / "license_checker.py"
        source_path.write_text(source, encoding="utf-8")
        py_compile.compile(
            str(source_path),
            cfile=str(target_dir / "license_checker.pyc"),
            dfile="license_checker.py",
            doraise=True,
            optimize=0,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def dns_query(name: str, txid: int) -> bytes:
    labels = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split("."))
    return struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0) + labels + b"\0" + struct.pack("!HH", 16, 1)


def udp_ipv4_packet(payload: bytes, src: str, dst: str, sport: int, dport: int, ident: int) -> bytes:
    src_raw = socket.inet_aton(src)
    dst_raw = socket.inet_aton(dst)
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    header_without_sum = struct.pack(
        "!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), ident, 0, 64, 17, 0, src_raw, dst_raw
    )
    ip = header_without_sum[:10] + struct.pack("!H", checksum(header_without_sum)) + header_without_sum[12:]
    ethernet = bytes.fromhex("00112233445566778899aabb0800")
    return ethernet + ip + udp


def build_pcap() -> None:
    packets: list[bytes] = []
    packets.append(udp_ipv4_packet(dns_query("updates.example.com", 0x1000), "10.20.30.44", "10.20.30.1", 53000, 53, 1))
    decoy = b"analyst-note=rhombus{pcap_strings_are_often_bait}"
    packets.append(udp_ipv4_packet(decoy, "10.20.30.44", "10.20.30.99", 49152, 9999, 2))

    encoded = base64.b32encode(b"rhombus{dns_labels_can_tell_a_story}").decode("ascii").rstrip("=").lower()
    chunks = [encoded[i : i + 13] for i in range(0, len(encoded), 13)]
    for sequence, chunk in enumerate(chunks):
        name = f"{sequence:02d}.{chunk}.telemetry.example.com"
        packets.append(
            udp_ipv4_packet(
                dns_query(name, 0x2000 + sequence),
                "10.20.30.44",
                "10.20.30.1",
                53100 + sequence,
                53,
                10 + sequence,
            )
        )
    packets.append(udp_ipv4_packet(dns_query("cdn.example.com", 0x3000), "10.20.30.44", "10.20.30.1", 53999, 53, 99))

    target = CHALLENGES / "forensics-dns-diary" / "workstation.pcap"
    with target.open("wb") as output:
        output.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        timestamp = 1_750_000_000
        for i, packet in enumerate(packets):
            output.write(struct.pack("<IIII", timestamp + i, i * 1000, len(packet), len(packet)))
            output.write(packet)


def git(repo: Path, *args: str, env: dict[str, str]) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def build_git_archive() -> None:
    target = CHALLENGES / "misc-commitment-issues" / "project-backup.zip"
    with tempfile.TemporaryDirectory() as temp:
        repo = Path(temp) / "acme-status-page"
        repo.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Acme Developer",
                "GIT_AUTHOR_EMAIL": "developer@example.invalid",
                "GIT_COMMITTER_NAME": "Acme Developer",
                "GIT_COMMITTER_EMAIL": "developer@example.invalid",
                "GIT_AUTHOR_DATE": "2026-04-12T10:00:00Z",
                "GIT_COMMITTER_DATE": "2026-04-12T10:00:00Z",
            }
        )
        git(repo, "init", "-q", "-b", "main", env=env)
        (repo / "README.md").write_text("# Acme Status Page\n\nInternal deployment helper.\n", encoding="utf-8")
        (repo / "deploy.py").write_text('print("deploying status page")\n', encoding="utf-8")
        git(repo, "add", ".", env=env)
        git(repo, "commit", "-q", "-m", "Initial deployment helper", env=env)

        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "2026-04-12T10:08:00Z"
        (repo / ".env.production").write_text(
            "DEPLOYMENT_RECOVERY_TOKEN=rhombus{deleted_does_not_mean_gone}\n", encoding="utf-8"
        )
        git(repo, "add", ".env.production", env=env)
        git(repo, "commit", "-q", "-m", "Add production deployment environment", env=env)

        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "2026-04-12T10:11:00Z"
        (repo / ".env.production").unlink()
        (repo / "example.env").write_text(
            "DEPLOYMENT_RECOVERY_TOKEN=rhombus{replace_me_before_release}\n", encoding="utf-8"
        )
        git(repo, "add", "-A", env=env)
        git(repo, "commit", "-q", "-m", "Remove accidentally committed environment", env=env)

        # The Git index contains filesystem timestamps and is not needed for
        # history inspection. Exclude it and pin ZIP metadata so rebuilding the
        # challenge produces byte-for-byte identical output.
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(repo.rglob("*")):
                if not path.is_file() or path == repo / ".git" / "index":
                    continue
                relative = (Path(repo.name) / path.relative_to(repo)).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(2026, 4, 12, 10, 12, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())


def is_probable_prime(candidate: int) -> bool:
    if candidate < 2:
        return False
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if candidate % small == 0:
            return candidate == small
    exponent = candidate - 1
    shifts = 0
    while exponent % 2 == 0:
        shifts += 1
        exponent //= 2
    for base in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        value = pow(base, exponent, candidate)
        if value in (1, candidate - 1):
            continue
        for _ in range(shifts - 1):
            value = pow(value, 2, candidate)
            if value == candidate - 1:
                break
        else:
            return False
    return True


def deterministic_prime(label: bytes, bits: int = 384) -> int:
    counter = 0
    while True:
        material = bytearray()
        block = 0
        while len(material) * 8 < bits:
            material.extend(hashlib.sha256(label + counter.to_bytes(4, "big") + block.to_bytes(4, "big")).digest())
            block += 1
        candidate = int.from_bytes(material[: bits // 8], "big")
        candidate |= (1 << (bits - 1)) | 1
        if is_probable_prime(candidate):
            return candidate
        counter += 1


def build_rsa() -> None:
    shared = deterministic_prime(b"signal-ridge-rsa-shared")
    q1 = deterministic_prime(b"signal-ridge-rsa-q1")
    q2 = deterministic_prime(b"signal-ridge-rsa-q2")
    p3 = deterministic_prime(b"signal-ridge-rsa-p3")
    q3 = deterministic_prime(b"signal-ridge-rsa-q3")
    exponent = 65537
    records = []
    for name, p, q, plaintext in (
        ("badge-controller", shared, q1, b"rhombus{shared_primes_share_private_keys}"),
        ("door-controller", shared, q2, b"enrollment=door-controller-07"),
        ("qa-controller", p3, q3, b"rhombus{rsa_factory_acceptance_test}"),
    ):
        modulus = p * q
        message = int.from_bytes(plaintext, "big")
        records.append(
            {
                "device": name,
                "n": hex(modulus),
                "e": exponent,
                "ciphertext": hex(pow(message, exponent, modulus)),
            }
        )
    target = CHALLENGES / "crypto-common-ground" / "enrollments.json"
    target.write_text(json.dumps({"algorithm": "RSA", "records": records}, indent=2) + "\n", encoding="utf-8")


def secp_add(left, right):
    prime = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % prime == 0:
        return None
    if left == right:
        slope = (3 * x1 * x1) * pow(2 * y1, -1, prime) % prime
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, prime) % prime
    x3 = (slope * slope - x1 - x2) % prime
    return x3, (slope * (x1 - x3) - y1) % prime


def secp_multiply(scalar: int, point):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = secp_add(result, addend)
        addend = secp_add(addend, addend)
        scalar >>= 1
    return result


def build_ecdsa() -> None:
    order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    generator = (
        0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
        0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
    )
    private = int.from_bytes(hashlib.sha256(b"signal-ridge-wallet-private").digest(), "big") % order
    nonce = int.from_bytes(hashlib.sha256(b"clock-reset-nonce").digest(), "big") % order
    public = secp_multiply(private, generator)
    messages = [b"approve audit batch 184", b"approve recovery export 185"]
    signatures = []
    for message in messages:
        digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
        r = secp_multiply(nonce, generator)[0] % order
        s = (digest + r * private) * pow(nonce, -1, order) % order
        signatures.append({"message": message.decode(), "r": hex(r), "s": hex(s)})
    flag = b"rhombus{reused_ecdsa_nonces_reveal_the_key}"
    digest_pad = hashlib.sha256(private.to_bytes(32, "big")).digest()
    ciphertext = bytes(value ^ digest_pad[i % len(digest_pad)] for i, value in enumerate(flag))
    artifact = {
        "curve": "secp256k1",
        "public_key": {"x": hex(public[0]), "y": hex(public[1])},
        "signatures": signatures,
        "encrypted_recovery_record": ciphertext.hex(),
        "operator_note": "rhombus{signature_validation_test_record}",
    }
    target = CHALLENGES / "crypto-clockwork-signatures" / "signatures.json"
    target.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")


def build_layer_cake() -> None:
    flag = b"rhombus{obfuscation_is_not_encryption}"
    transformed = bytes(value ^ 0x23 for value in flag)[::-1]
    blob = base64.b64encode(transformed).decode("ascii")
    script = textwrap.dedent(
        f'''\
        # Recovered scheduled-task payload. Analyze; do not execute.
        $TestFixture = "rhombus{{powershell_logging_test}}"
        $Layer = "{blob}"
        $Bytes = [Convert]::FromBase64String($Layer)
        [Array]::Reverse($Bytes)
        $Output = foreach ($Value in $Bytes) {{ [char]($Value -bxor 0x23) }}
        $OperatorToken = -join $Output
        if ($env:ACME_DEBUG -eq "print") {{ Write-Output $OperatorToken }}
        '''
    )
    target = CHALLENGES / "rev-layer-cake" / "updater.ps1"
    target.write_text(script, encoding="utf-8", newline="\n")


def build_windows_events() -> None:
    command = "$a='rhombus{utf16';$b='le_hides_in_';$c='plain_sight}';Write-Output ($a+$b+$c)"
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    events = [
        {"time": "2026-05-14T02:13:01Z", "event_id": 4688, "user": "ACME\\backup", "process": "powershell.exe", "command_line": "powershell -File C:\\Ops\\backup.ps1"},
        {"time": "2026-05-14T02:14:19Z", "event_id": 1116, "provider": "Defender", "message": "Training indicator rhombus{eicar_pipeline_test}"},
        {"time": "2026-05-14T02:15:02Z", "event_id": 4698, "task": "TelemetryRepair", "action": f"powershell.exe -NoProfile -EncodedCommand {encoded}"},
        {"time": "2026-05-14T02:15:03Z", "event_id": 4104, "task": "TelemetryRepair", "script_block": f"powershell.exe -NoP -enc {encoded}"},
        {"time": "2026-05-14T02:17:40Z", "event_id": 4689, "user": "ACME\\backup", "process": "powershell.exe", "exit_code": 0},
    ]
    target = CHALLENGES / "forensics-script-block" / "windows-events.jsonl"
    target.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def deterministic_zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 5, 14, 2, 20, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return output.getvalue()


def build_thumbnail() -> None:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    pixels = b"\x00\x22\x88\xcc\x22\x88\xcc\x00\xcc\x88\x22\xcc\x88\x22"
    png = (
        signature
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"tEXt", b"Comment\x00rhombus{metadata_can_be_misleading}")
        + png_chunk(b"IDAT", zlib.compress(pixels))
        + png_chunk(b"IEND", b"")
    )
    tail = deterministic_zip(
        {
            "case-notes/incident.txt": b"Recovered operator marker: rhombus{files_can_have_more_than_one_ending}\n",
            "case-notes/old-note.txt": b"rhombus{thumbnail_archive_test}\n",
        }
    )
    target = CHALLENGES / "forensics-thumbnail-tail" / "recovered-thumbnail.png"
    target.write_bytes(png + tail)


def build_vendor_cache() -> None:
    packages = {
        "vendor-cache/requests-2.31.0/package.json": b'{"name":"requests","version":"2.31.0"}\n',
        "vendor-cache/lodash-4.17.21/package.json": b'{"name":"lodash","version":"4.17.21"}\n',
        "vendor-cache/chalk-5.3.0/test/fixture.txt": b"rhombus{package_test_fixture}\n",
        "vendor-cache/requsets-2.31.0/package.json": b'{"name":"requsets","version":"2.31.0","scripts":{"postinstall":"node install.js"}}\n',
        "vendor-cache/requsets-2.31.0/install.js": (
            b"const a = 'rhombus{typosquatting_';\n"
            b"const b = 'the_build_';\n"
            b"const c = 'pipeline}';\n"
            b"console.log(Buffer.from(a + b + c).toString('base64'));\n"
        ),
        "vendor-cache/package-lock.json": b'{"packages":{"node_modules/requests":{"version":"2.31.0"},"node_modules/requsets":{"version":"2.31.0","hasInstallScript":true}}}\n',
    }
    target = CHALLENGES / "misc-dependency-detour" / "vendor-cache.zip"
    target.write_bytes(deterministic_zip(packages))


def copy_pwn_source() -> None:
    source = ROOT / "services" / "pwn" / "echo-service.c"
    target = CHALLENGES / "pwn-echo-service" / "echo-service.c"
    shutil.copyfile(source, target)


def main() -> None:
    build_crypto()
    build_reverse()
    build_pcap()
    build_git_archive()
    build_rsa()
    build_ecdsa()
    build_layer_cake()
    build_windows_events()
    build_thumbnail()
    build_vendor_cache()
    copy_pwn_source()
    print("Built deterministic static challenge artifacts.")
    print("Build/extract native ELF artifacts with tools/build_native_artifacts.py.")


if __name__ == "__main__":
    main()
