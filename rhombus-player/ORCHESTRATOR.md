# Local demo orchestrator

A dependency-free **integration simulator** for the local Rhombus demo. Requires Python 3.10+ and works natively on Windows. It launches only the bundled `fake_worker.py`; it does not launch real AI agents, analyze vulnerabilities, execute artifacts, or support arbitrary worker commands.

## Run

Start the realistic demo at `http://127.0.0.1:3000`. Use literal loopback addresses, not `localhost`. Only same-origin requests are allowed; redirects and environment proxies are disabled. Remote platform URLs and remote attachment/CDN URLs are intentionally unsupported in this simulator.

From the repository root, in PowerShell:

```powershell
# Organizer-only replay answers. NEVER distribute this file to players.
python rhombus-player/make_demo_fixture.py "$env:TEMP\demo-replay-answers.json"

# Output must be a NEW directory. Password is prompted, not put on the command line.
python rhombus-player/demo_orchestrator.py run http://127.0.0.1:3000 `
  -u simulation-player --output "$env:TEMP\ctf-run-001" `
  --answers "$env:TEMP\demo-replay-answers.json" --seed 42
```

`--password-stdin` is available for automation. Login uses Rhombus credentials registration/sign-in. Testing creates normal team/solve/tracking records and can affect the demo scoreboard; use a dedicated test account.

The optional private answer file is a JSON object:

```json
{"ticket-trouble": "organizer-supplied-test-answer", "dns-diary": null}
```

Missing/null answers never produce a correct fixture candidate. The fixture helper reads the 16 committed organizer YAML definitions in their current simple format; it is not a solver or general YAML parser. Test answers are passed only to fake workers through stdin, never inserted into downloaded challenge manifests.

## Scheduling and configuration

1. Authenticate and sort visible challenges by stable ID.
2. Download attachments concurrently (default **4** transfers). A challenge becomes ready only when all its files have downloaded atomically.
3. Start its competing subprocesses immediately when ready, subject to a global **12**-process cap. Other challenges need not have finished downloading.
4. Receive candidate flags over private pipes; submit centrally using the published web form.
5. On a confirmed correct response, stop **all workers for that challenge**, including its winner. Other challenge races continue. Queued workers for a solved challenge do not start.
6. Wait for already-started submissions to finish journaling, reap subprocesses, and write `summary.json`.

Flags are never inferred correct from HTTP status alone. A success requires Rhombus's success event. The English demo's `Incorrect flag` is a known rejection; other responses, authentication failures, unexpected HTML, and network errors become operator-review cases.

Options:

| Option | Default | Meaning |
|---|---|---|
| `--profiles` | `burst steady never` | Fixed fake-worker configurations; multiple can race per challenge |
| `--seed` | `1` | Stable SHA-256-derived seeds, independent of Python hash randomization |
| `--downloads` | `4` | Concurrent transfers, maximum 16 |
| `--processes` | `12` | Concurrent subprocesses, maximum 32 |
| `--request-interval` | `0.1` | Global minimum interval between request starts |
| `--worker-timeout` | `120` | Seconds per started worker; maximum 600 |

Profiles:

- `burst`: 12 immediate decoys, then the fixture answer if provided.
- `steady`: 3 decoys with seeded delays, then the fixture answer if provided.
- `never`: 5 delayed decoys; never supplies the answer.

Each fake worker waits for a response before sending another candidate. Workers exit after a submission error. They cannot submit for another challenge. There is a hard 32-message ceiling per worker and at most 8 profiles per challenge.

**Deterministic means reproducible candidate sequences and planned delays, not reproducible wall-clock ordering or winners.** Subprocess scheduling, network timing, and server state can change winners. No ordering claim depends on OS timing.

## Rate limits

Every request uses one shared admission gate, including login, challenge listing, downloads, manual submissions, and fake-worker submissions. A 429 pauses all future admissions using `Retry-After` (seconds or HTTP date) or `X-RateLimit-After`, with exponential minimum waits for missing/zero values. Already-in-flight requests cannot be recalled. Retries are bounded to 5; waits above 30 seconds stop further request admission for this run rather than being shortened and retried prematurely.

For a **bounded local stress test**, use `--request-interval 0`. This produces a short initial burst and exercises the demo's real 429 responses; it does not disable or bypass the shared cooldown.

Rhombus also has a separate **solve-burst throttle**, which can return HTTP 200 with an error after three rapid solves. The simulator does not disable that protection, wait indefinitely, or label those flags incorrect: it records `needs_review` with the original candidates. Thus supplying all 16 correct replay answers does not guarantee 16 accepted solves in one short run.

## Manual submission during a run

In another terminal:

```powershell
python rhombus-player/demo_orchestrator.py submit `
  --run-dir "$env:TEMP\ctf-run-001" ticket-trouble
```

The flag is prompted without echo; `--flag-stdin` is also available. The command atomically queues it into the running orchestrator and waits for a structured response. It uses the same rate gate and challenge lock as workers. A correct manual flag ends that challenge's race.

Response statuses: `correct`, `incorrect`, `error`, `already_solved`. **`already_solved` means the race is finished; it does not validate the supplied candidate.** A timeout means the outcome is unknown, not that the submission was cancelled; its candidate stays in the inbox. The run exits when all challenge jobs finish—it does not remain open indefinitely for manual submissions.

For a stopped run, use the existing player CLI or browser to submit manually:

```powershell
python rhombus-player/rhombus_player.py login http://127.0.0.1:3000 -u simulation-player
python rhombus-player/rhombus_player.py submit ticket-trouble
```

## Operator recovery and privacy

```powershell
# WARNING: prints candidate FLAGS intentionally, for the human operator.
python rhombus-player/demo_orchestrator.py review --run-dir "$env:TEMP\ctf-run-001"
```

Each network submission has a flushed/fsynced `pending` journal entry containing the challenge key and candidate **before** the request begins, followed by a result entry. Review lists errors and intents without durable results, plus manual inbox requests without responses. A connection failure can occur after the server accepts a flag: check platform solve state before retrying. Manual requests appearing in both the inbox and journal may be shown twice; inspect their request IDs before resubmitting.

Submission failures also print the candidate to operator stderr. Do not send this stream to player-visible logs.

Run directory:

```text
run.json                 run state and stable challenge keys
summary.json             solved / unsolved / needs_review / error / interrupted
challenges/<ID hash>/    challenge.json plus indexed attachments
inbox/                   private manual candidate requests
outbox/                  corresponding structured responses
submissions.jsonl        SENSITIVE candidates and durable outcomes
events.jsonl             readiness, rate limits, worker lifecycle (no candidate field)
```

Filesystem hashes prevent challenge-ID path traversal/collisions from slug normalization. Indexed attachment names avoid overwrite collisions. New run directories prevent accidental reuse of old inboxes/results.

Use a **private local folder**. POSIX files/directories use owner-only modes; on Windows they inherit directory ACLs, so choose a user-private folder and verify its ACL. Neither `chmod` nor `-I` makes a Windows security sandbox. The bundled workers run as your OS user, but contain no network calls or process spawning. This is not an isolation boundary for untrusted programs. Credentials/session tokens stay in the parent process and are not written to run files or passed in worker jobs. Manifests and artifact contents remain untrusted organizer data.

Ctrl+C terminates/reaps fixed workers and drains already-started submission operations before returning. Forced process termination/power loss cannot guarantee cleanup or exactly-once server effects; the durable journal supports manual reconciliation, not automatic crash resume.

Run exit codes: `0` all solved, `2` one or more non-solved outcomes, `1` setup/fatal error, `130` interrupted. Manual submit: `0` correct/already solved, `2` incorrect, `1` error/unknown.

## Worker protocol (version 1, simulation only)

Parent sends one JSON line over stdin containing:

```json
{
  "version": 1,
  "challenge_key": "ticket-trouble",
  "challenge": {"id": "ticket-trouble", "name": "Ticket Trouble", "category": "web", "description": "...", "attachments": []},
  "folder": "C:/private/run/challenges/<hash>",
  "files": [],
  "worker": "steady-1",
  "profile": "steady",
  "seed": 42,
  "test_answer": "PRIVATE SIMULATION FIXTURE ONLY"
}
```

Worker emits one JSON line on stdout per candidate:

```json
{"type":"submit","request_id":"0","challenge_key":"ticket-trouble","flag":"rhombus{simulated_decoy}"}
```

Parent responds on stdin:

```json
{"request_id":"0","challenge_key":"ticket-trouble","status":"incorrect","message":"Incorrect flag"}
```

One request is outstanding per worker; stdout is protocol-only. A winning response is attempted before terminating the race, but workers must also tolerate EOF/termination—the journal is authoritative. No listening submission endpoint or platform credential is exposed to workers. There is deliberately no configurable executable or real-agent integration in this version.

## Verification

```powershell
python -m unittest discover -s rhombus-player -p "test_*.py" -v
```

Tests cover concurrent/streaming downloads, subprocess races and cleanup, manual acceptance, seeded plans, timeout/cancellation, review persistence, HTTP-200 application errors, bounded 429 retries/shared cooldowns, and remote/redirect refusal. They use temporary folders and a loopback mock server, not the running demo's database.
