#!/usr/bin/env python3
from pathlib import Path
import subprocess
import tempfile
import zipfile

archive = Path(__file__).parents[1] / "challenges" / "misc-commitment-issues" / "project-backup.zip"
with tempfile.TemporaryDirectory() as temp:
    with zipfile.ZipFile(archive) as source:
        source.extractall(temp)
    repo = Path(temp) / "acme-status-page"
    history = subprocess.check_output(
        ["git", "log", "--all", "-S", "DEPLOYMENT_RECOVERY_TOKEN", "--format=%H"],
        cwd=repo,
        text=True,
    ).splitlines()
    for commit in history:
        result = subprocess.run(
            ["git", "show", f"{commit}:.env.production"],
            cwd=repo,
            text=True,
            capture_output=True,
        )
        if result.returncode == 0 and "rhombus{" in result.stdout:
            print(result.stdout.strip().split("=", 1)[1])
            break
