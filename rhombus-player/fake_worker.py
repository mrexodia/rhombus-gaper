#!/usr/bin/env python3
"""Fixed simulation worker. No network access, solving, or child processes."""
import hashlib
import json
import random
import sys
import time


def plan(seed, key, profile, answer):
    digest = hashlib.sha256(f"{seed}:{key}:{profile}".encode()).digest()
    rng = random.Random(int.from_bytes(digest, "big"))
    count = {"burst": 12, "steady": 3, "never": 5}[profile]
    steps = []
    for index in range(count):
        delay = 0 if profile == "burst" else round(rng.uniform(0.05, 0.25), 4)
        steps.append((delay, f"rhombus{{simulated_decoy_{profile}_{index}}}"))
    if answer is not None and profile != "never":
        steps.append((round(rng.uniform(0.05, 0.25), 4), answer))
    return steps


def main():
    job = json.loads(sys.stdin.readline())
    for index, (delay, flag) in enumerate(plan(
        job["seed"], job["challenge_key"], job["profile"], job["test_answer"]
    )):
        time.sleep(delay)
        print(json.dumps({"type": "submit", "request_id": str(index),
                          "challenge_key": job["challenge_key"], "flag": flag}), flush=True)
        line = sys.stdin.readline()
        if not line:
            return
        result = json.loads(line)
        if result["status"] in {"correct", "already_solved", "error"}:
            return


if __name__ == "__main__":
    main()
