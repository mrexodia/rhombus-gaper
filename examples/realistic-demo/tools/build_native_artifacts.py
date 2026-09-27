#!/usr/bin/env python3
"""Build native challenge images and extract the exact player ELF files."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = (
    (
        "echo-service",
        "rhombus-realistic-demo-echo-service:local",
        "/app/echo-service",
        ROOT / "challenges" / "pwn-echo-service" / "echo-service",
    ),
    (
        "format-forecast",
        "rhombus-realistic-demo-format-forecast:local",
        "/app/format-forecast",
        ROOT / "challenges" / "pwn-format-forecast" / "format-forecast",
    ),
    (
        "native-checker-artifact",
        "rhombus-realistic-demo-native-checker:local",
        "/badge-checker",
        ROOT / "challenges" / "rev-native-dance" / "badge-checker",
    ),
)


def output(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def main() -> None:
    subprocess.run(
        ["docker", "compose", "build", *(item[0] for item in ARTIFACTS)],
        cwd=ROOT,
        check=True,
    )
    for _, image_name, container_path, target in ARTIFACTS:
        image = output("docker", "image", "inspect", image_name, "--format", "{{.Id}}")
        container = output("docker", "create", image, container_path)
        try:
            subprocess.run(["docker", "cp", f"{container}:{container_path}", str(target)], check=True)
        finally:
            subprocess.run(["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL)
        print(f"Extracted {target}")


if __name__ == "__main__":
    main()
