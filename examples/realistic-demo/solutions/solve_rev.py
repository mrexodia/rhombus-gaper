#!/usr/bin/env python3
from pathlib import Path
import runpy

artifact = Path(__file__).parents[1] / "challenges" / "rev-bytecode-breakout" / "license_checker.pyc"
namespace = runpy.run_path(str(artifact), run_name="organizer_solver")
expected = namespace["EXPECTED"]
flag = "".join(chr(((value - i * 7) & 0xFF) ^ 0x5A) for i, value in enumerate(expected))
assert namespace["valid"](flag)
print(flag)
