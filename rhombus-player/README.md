# Rhombus Player CLI/API

A dependency-free Python client for the **published, unmodified Rhombus web interface**. It logs in through the normal credentials form, lists the challenges visible to that player, and downloads attachments into organized folders.

It never uses the admin API and never receives challenge answer keys.

## Requirements

- Python 3.10 or newer
- A Rhombus instance with `credentials` authentication enabled

No packages need to be installed.

## CLI usage

From the repository root:

```powershell
# The first credentials login also registers a new account.
python rhombus-player/rhombus_player.py login http://localhost:3000 --username alice

python rhombus-player/rhombus_player.py list
python rhombus-player/rhombus_player.py sync --output player-challenges
python rhombus-player/rhombus_player.py submit ticket-trouble 'rhombus{your_answer}'
```

For scripts and CI, supply the password over standard input rather than as a command-line argument:

```powershell
$env:RHOMBUS_PASSWORD | python rhombus-player/rhombus_player.py login http://localhost:3000 -u alice --password-stdin
```

Additional commands:

```text
list --json                 Print the visible challenge inventory as JSON
sync --force                Re-download existing attachments
submit ID FLAG              Submit a flag; exits 0 when correct, 2 when rejected
submit ID --flag-stdin      Read a flag from standard input
logout                      Remove the saved local session
--session PATH              Use a non-default session file
```

The session is normally stored at:

- Windows: `%APPDATA%\rhombus-player\session.json`
- Linux/macOS: `$XDG_CONFIG_HOME/rhombus-player/session.json` or `~/.config/rhombus-player/session.json`

The file contains the short-lived `rhombus-token` session token, not the password. Treat it as sensitive. If the session expires, run `login` again.

## Rate limiting

The client detects HTTP `429 Too Many Requests` on login, listing, downloads, and flag submissions. It honors Rhombus's `Retry-After`/`X-RateLimit-After` headers, applies an exponential floor when the server rounds a sub-second wait to zero, waits, and safely retries.

Retries are bounded to avoid hanging forever:

```text
--max-retries 5          Maximum retries after the initial request
--max-retry-wait 300     Reject any server-requested wait above 300 seconds
```

The CLI reports each wait on stderr. Python callers can configure `max_retries`, `max_retry_wait`, and an `on_rate_limit` callback on `RhombusAPI`.

The realistic demo enables Rhombus's per-IP limiter with a burst of 12 requests and replenishment of one request every 100ms, allowing this behavior to be exercised locally.

## Download layout

```text
player-challenges/
├── index.json
├── cryptography/
│   └── common-ground/
│       ├── README.md
│       └── enrollments.json
└── binary-exploitation/
    └── echo-service/
        ├── README.md
        ├── echo-service
        └── echo-service.c
```

Existing attachments are preserved unless `--force` is supplied. Challenge `README.md` files and `index.json` are refreshed on every sync.

## Python API

The same file can be imported as a module:

```python
from pathlib import Path
from rhombus_player import RhombusAPI

api = RhombusAPI("http://localhost:3000")
token = api.login("alice", "correct horse battery staple")

for challenge in api.challenges():
    print(challenge.category, challenge.name)

result = api.submit_flag("ticket-trouble", "rhombus{your_answer}")
print(result.correct, result.message)

api.sync(Path("player-challenges"))
```

When importing from elsewhere, either copy `rhombus_player.py` beside your program or add the `rhombus-player` directory to `PYTHONPATH`.

## Compatibility and limitations

The client parses the normal `/challenges` HTML because published Rhombus does not expose a sanitized player challenge API. A future change to Rhombus's page structure may require updating the parser.

Credentials authentication can be automated. Discord and CTFtime OAuth intentionally require an interactive browser flow and are not handled by this tool.
