# Signal Ridge CTF — realistic local demo

A local Rhombus instance with sixteen solvable challenges and deliberate decoy flags.

| Challenge | Category | Starting points | Runtime |
|---|---:|---:|---|
| Ticket Trouble | Web | 150 | Docker, port 8081 |
| Forwarded Forgery | Web | 175 | Docker, port 8082 |
| Union Benefits | Web | 275 | Docker, port 8083 |
| Echo Service | Pwn | 300 | Docker, port 1337 |
| Format Forecast | Pwn | 325 | Docker, port 1338 |
| Reset the Stream | Crypto | 200 | Static artifact |
| Common Ground | Crypto | 275 | Static artifact |
| Clockwork Signatures | Crypto | 375 | Static artifact |
| Bytecode Breakout | Reverse engineering | 225 | Static artifact |
| Layer Cake | Reverse engineering | 200 | Static artifact |
| Native Dance | Reverse engineering | 325 | Static artifact |
| DNS Diary | Forensics | 250 | Static artifact |
| Script Block Party | Forensics | 225 | Static artifact |
| Thumbnail Tail | Forensics | 175 | Static artifact |
| Commitment Issues | Misc | 125 | Static artifact |
| Dependency Detour | Misc | 250 | Static artifact |

## Dynamic scoring

All sixteen challenges use quadratic dynamic scoring. The table lists their starting values; each challenge has a minimum of **20% of its starting value** and reaches that floor at **20 team solves**, counted across divisions. Earlier solvers' scores decrease too.

For example, Ticket Trouble starts at 150 points, is worth 120 after 10 solves, and reaches its 30-point minimum after 20 solves. Integer rounding can leave the value unchanged on some solves.

Changing an existing demo from static to dynamic does not delete accounts or solves. Rhombus preserves each existing challenge's current stored points on reload; its next accepted solve applies the dynamic curve using the accumulated solve count and adjusts prior solvers' totals. Fresh databases use the configured starting values immediately.

## Start on Windows (PowerShell)

From this directory:

```powershell
python tools/build_artifacts.py
python tools/build_native_artifacts.py
docker compose up -d
$env:PROTOC = "$HOME\.local\rhombus-tools\protoc\bin\protoc.exe"
$env:PATH = "$HOME\.local\rhombus-tools\deno;$HOME\.local\rhombus-tools\protoc\bin;$env:PATH"
cargo run -p realistic-demo
```

Then open <http://localhost:3000>, create a credentials account, and form a team.

## Start on Linux/macOS

Install Rust, Deno, Protocol Buffers (`protoc`), Python 3, Git, and Docker. Then:

```sh
python3 tools/build_artifacts.py
python3 tools/build_native_artifacts.py
docker compose up -d
cargo run -p realistic-demo
```

## Stop

Stop Rhombus with Ctrl+C, then stop the challenge services:

```sh
docker compose down
```

## Enabled platform features

The local demo exercises credentials registration, team/division creation, per-IP request tracking, the optional token-bucket rate limiter, challenge health checks, local attachment uploads, in-memory caching, flag submissions, scoring, and the root API key. The limiter permits a burst of 12 requests and replenishes one every 100ms.

Integrations that require real third-party credentials or infrastructure—Discord, email, CTFtime OAuth, S3, and remote databases—remain disabled deliberately.

## Organizer notes

- `solutions/` contains reference solvers and spoilers.
- All generated artifacts are deterministic. Re-run `tools/build_artifacts.py` after editing their source data.
- `tools/build_native_artifacts.py` extracts the exact ELF files used by the native pwn/reversing challenges.
- The platform and vulnerable services bind only to `127.0.0.1` by default.
- `config.yaml` contains intentionally disposable local secrets. Rotate them before any non-local deployment.
