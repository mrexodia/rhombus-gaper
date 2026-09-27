#!/usr/bin/env python3
"""Extract private replay answers from this repository's fixed demo YAML format.

Organizer-only integration fixture; never distribute this output to players.
This is deliberately not a general YAML parser or a challenge solver.
"""
import argparse
from pathlib import Path
import re

from demo_orchestrator import private_json


def demo_answers():
    demo = Path(__file__).resolve().parents[1] / "examples" / "realistic-demo" / "challenges"
    answers = {}
    for path in sorted(demo.glob("*/challenge.yaml")):
        text = path.read_text(encoding="utf-8")
        key = re.search(r"^stable_id: ([a-z0-9-]+)$", text, re.M)
        flag = re.search(r"^flag: (rhombus\{[^\r\n]+\})$", text, re.M)
        if key is None or flag is None:
            raise ValueError(f"unsupported demo fixture format: {path}")
        if key[1] in answers:
            raise ValueError("duplicate stable_id")
        answers[key[1]] = flag[1]
    if len(answers) != 16:
        raise ValueError("expected the repository's 16 demo challenges")
    return answers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new private JSON file OUTSIDE player download folders")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite an existing fixture")
    private_json(args.output, demo_answers())
    print(f"Wrote private organizer replay fixture to {args.output}")


if __name__ == "__main__":
    main()
