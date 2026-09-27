#!/usr/bin/env python3
"""Dependency-free player CLI and Python API for published Rhombus instances.

This client deliberately uses only public web routes. It does not require a
server plugin, an admin key, or modifications to Rhombus.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import email.utils
import getpass
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

USER_AGENT = "rhombus-player/1.0"


class RhombusError(RuntimeError):
    """Base error for player-client failures."""


class AuthenticationError(RhombusError):
    """Raised when credentials or a saved session are invalid."""


class ParseError(RhombusError):
    """Raised when published Rhombus HTML cannot be understood."""


class RateLimitError(RhombusError):
    """Raised when a server keeps returning HTTP 429 after bounded retries."""

    def __init__(self, url: str, retry_after: float, attempts: int) -> None:
        self.url = url
        self.retry_after = retry_after
        self.attempts = attempts
        super().__init__(
            f"rate limited by {url} after {attempts} attempt(s); "
            f"server requested another {retry_after:.2f}s wait"
        )


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Challenge:
    id: str
    name: str
    category: str
    description: str
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)
    points: Optional[int] = None

    def to_dict(self) -> dict[str, object]:
        result = dataclasses.asdict(self)
        result["attachments"] = [item.to_dict() for item in self.attachments]
        return result


@dataclass(frozen=True)
class SubmissionResult:
    correct: bool
    message: str


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[object] = field(default_factory=list)

    def walk(self, tag: Optional[str] = None) -> Iterator["_Node"]:
        if tag is None or self.tag == tag:
            yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk(tag)

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        pieces: list[str] = []

        def collect(node: _Node) -> None:
            for child in node.children:
                if isinstance(child, str):
                    pieces.append(child)
                elif child.tag not in {"script", "style", "svg"}:
                    collect(child)

        collect(self)
        return _normalize_text(" ".join(pieces))


class _TreeParser(HTMLParser):
    VOID_ELEMENTS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        node = _Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in self.VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_ELEMENTS:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _normalize_text(value: str) -> str:
    # Fluent localization surrounds interpolated values with bidi-isolation
    # controls. They are useful in a browser but should not enter folder names
    # or prevent point-value parsing.
    value = "".join(character for character in value if unicodedata.category(character) != "Cf")
    return re.sub(r"\s+", " ", value).strip()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "uncategorized"


def _safe_filename(value: str) -> str:
    # Attachment names are controlled by the CTF organizer. Never allow them
    # to escape the selected output directory.
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", value).strip(". ")
    return value or "attachment"


def parse_challenges_html(html: str, base_url: str) -> list[Challenge]:
    """Parse the normal authenticated `/challenges` page.

    This only reads fields that Rhombus renders for players. In particular, it
    never calls the admin gRPC API and has no access to challenge answer keys.
    """

    parser = _TreeParser()
    parser.feed(html)
    challenges: list[Challenge] = []

    for card in parser.root.walk("li"):
        if "border-l-4" not in card.classes():
            continue

        challenge_id = None
        for node in card.walk():
            target = node.attrs.get("hx-get", "")
            match = re.fullmatch(r"/challenges/([^/?#]+)", target)
            if match:
                challenge_id = urllib.parse.unquote(match.group(1))
                break
        if not challenge_id:
            continue

        spans = [node.text() for node in card.walk("span") if node.text()]
        if len(spans) < 2:
            raise ParseError(f"could not read category/name for challenge {challenge_id!r}")
        category = spans[0].rstrip("/ ").strip()
        name = spans[1].strip()

        description = ""
        for node in card.walk("div"):
            if "prose" in node.classes():
                description = node.text()
                break

        attachments: list[Attachment] = []
        for link in card.walk("a"):
            href = link.attrs.get("href")
            has_attachment_icon = any(
                "lucide-paperclip" in icon.classes() for icon in link.walk("svg")
            )
            if not href or ("/uploads/" not in href and not has_attachment_icon):
                continue
            attachment_name = link.text()
            if not attachment_name:
                attachment_name = urllib.parse.unquote(urllib.parse.urlparse(href).path.rsplit("/", 1)[-1])
            attachments.append(
                Attachment(
                    name=_safe_filename(attachment_name),
                    url=urllib.parse.urljoin(base_url.rstrip("/") + "/", href),
                )
            )

        points = None
        card_text = card.text()
        points_match = re.search(r"\b(\d+)\s+(?:points?|pts?)\b", card_text, re.IGNORECASE)
        if points_match:
            points = int(points_match.group(1))

        challenges.append(
            Challenge(
                id=challenge_id,
                name=name,
                category=category,
                description=description,
                attachments=tuple(attachments),
                points=points,
            )
        )

    if not challenges:
        raise ParseError(
            "no challenge cards found; the session may be unauthenticated or the Rhombus HTML changed"
        )
    return challenges


class RhombusAPI:
    """Small player API implemented against Rhombus's published web UI."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout: float = 30.0,
        *,
        max_retries: int = 5,
        max_retry_wait: float = 300.0,
        on_rate_limit: Optional[Callable[[float, int, int], None]] = None,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if max_retry_wait <= 0:
            raise ValueError("max_retry_wait must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_retry_wait = max_retry_wait
        self.on_rate_limit = on_rate_limit

    def _same_origin(self, url: str) -> bool:
        base = urllib.parse.urlparse(self.base_url)
        target = urllib.parse.urlparse(url)
        return (base.scheme, base.hostname, base.port) == (target.scheme, target.hostname, target.port)

    @staticmethod
    def _retry_after(headers, fallback: float) -> float:
        value = headers.get("Retry-After") or headers.get("X-RateLimit-After")
        if value:
            try:
                return max(float(value), fallback)
            except ValueError:
                try:
                    retry_at = email.utils.parsedate_to_datetime(value)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
                    remaining = (retry_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                    return max(remaining, fallback)
                except (TypeError, ValueError, OverflowError):
                    pass
        return fallback

    def _request(
        self,
        path_or_url: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        authenticated: bool = True,
    ):
        url = urllib.parse.urljoin(self.base_url + "/", path_or_url)
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        if authenticated and self.token and self._same_origin(url):
            request_headers["Cookie"] = f"rhombus-token={self.token}"

        for retry in range(self.max_retries + 1):
            # Build a fresh Request for every attempt. The form body is bytes,
            # so replay after a 429 is safe; Governor rejects before handlers
            # process credential or flag-submission POSTs.
            request = urllib.request.Request(url, data=data, headers=request_headers)
            try:
                return urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                if error.code == 429:
                    fallback = min(0.5 * (2**retry), self.max_retry_wait)
                    wait = self._retry_after(error.headers, fallback) + 0.05
                    if error.fp is not None:
                        error.close()
                    if retry >= self.max_retries or wait > self.max_retry_wait:
                        raise RateLimitError(url, wait, retry + 1) from error
                    if self.on_rate_limit:
                        self.on_rate_limit(wait, retry + 1, self.max_retries)
                    time.sleep(wait)
                    continue
                if error.code in {401, 403}:
                    raise AuthenticationError(f"authentication rejected by {url}") from error
                raise RhombusError(f"HTTP {error.code} from {url}: {error.reason}") from error
            except urllib.error.URLError as error:
                raise RhombusError(f"could not connect to {url}: {error.reason}") from error

        raise AssertionError("rate-limit retry loop terminated unexpectedly")

    def login(self, username: str, password: str) -> str:
        """Register/sign in with credentials and return the session token."""

        body = urllib.parse.urlencode({"username": username, "password": password}).encode()
        with self._request(
            "/signin/credentials",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            authenticated=False,
        ) as response:
            cookies = response.headers.get_all("Set-Cookie", [])
            redirect = response.headers.get("HX-Redirect")

        token = None
        for header in cookies:
            match = re.search(r"(?:^|;)\s*rhombus-token=([^;]+)", header)
            if match:
                token = match.group(1)
                break
        if not token or redirect != "/team":
            raise AuthenticationError("login failed; check the username and password")
        self.token = token
        return token

    def challenges(self) -> list[Challenge]:
        """Return challenges visible to the logged-in player."""

        if not self.token:
            raise AuthenticationError("no session token; log in first")
        with self._request("/challenges") as response:
            final_path = urllib.parse.urlparse(response.geturl()).path
            html = response.read().decode(response.headers.get_content_charset() or "utf-8")
        if final_path.startswith("/signin"):
            raise AuthenticationError("session expired; log in again")
        try:
            return parse_challenges_html(html, self.base_url)
        except ParseError as error:
            if "credentials" in html.lower() and "/signin/credentials" in html:
                raise AuthenticationError("session expired; log in again") from error
            raise

    def submit_flag(self, challenge_id: str, flag: str) -> SubmissionResult:
        """Submit a flag through the published challenge form."""

        if not self.token:
            raise AuthenticationError("no session token; log in first")
        if not challenge_id or "/" in challenge_id or "\\" in challenge_id:
            raise ValueError("invalid challenge ID")
        body = urllib.parse.urlencode({"flag": flag}).encode()
        endpoint = "/challenges/" + urllib.parse.quote(challenge_id, safe="")
        with self._request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as response:
            final_path = urllib.parse.urlparse(response.geturl()).path
            trigger = response.headers.get("HX-Trigger")
            response_body = response.read().decode(response.headers.get_content_charset() or "utf-8")
        if final_path.startswith("/signin"):
            raise AuthenticationError("session expired; log in again")

        if trigger:
            try:
                event = json.loads(trigger)
                toast = event.get("toast", {})
                if toast.get("kind") == "success" and event.get("manualRefresh"):
                    message = toast.get("message", "")
                    try:
                        import base64

                        message = base64.b64decode(message).decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        message = "Challenge solved"
                    return SubmissionResult(True, message or "Challenge solved")
            except json.JSONDecodeError:
                pass

        parser = _TreeParser()
        parser.feed(response_body)
        message = parser.root.text() or response_body.strip() or "Flag rejected"
        return SubmissionResult(False, message)

    def download_attachment(self, attachment: Attachment, destination: Path, *, force: bool = False) -> bool:
        """Download one attachment atomically. Returns False when skipped."""

        destination = Path(destination)
        if destination.exists() and not force:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        try:
            # Rhombus attachment URLs are public (local/database uploads or
            # presigned object-storage URLs). Do not send the session token;
            # this also prevents it crossing origins through an HTTP redirect.
            with self._request(attachment.url, authenticated=False) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return True

    def sync(self, output: Path | str, *, force: bool = False) -> list[Challenge]:
        """Synchronize visible challenges into organized local folders."""

        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        challenges = self.challenges()

        for challenge in challenges:
            folder = output / _slug(challenge.category) / _slug(challenge.id)
            folder.mkdir(parents=True, exist_ok=True)
            attachment_lines = []
            for attachment in challenge.attachments:
                filename = _safe_filename(attachment.name)
                self.download_attachment(attachment, folder / filename, force=force)
                attachment_lines.append(f"- [{filename}](./{filename})")

            points = f"\n- **Points:** {challenge.points}" if challenge.points is not None else ""
            attachments = "\n".join(attachment_lines) if attachment_lines else "_No downloadable files._"
            readme = (
                f"# {challenge.name}\n\n"
                f"- **Category:** {challenge.category}{points}\n"
                f"- **Challenge ID:** `{challenge.id}`\n\n"
                f"## Description\n\n{challenge.description}\n\n"
                f"## Attachments\n\n{attachments}\n"
            )
            (folder / "README.md").write_text(readme, encoding="utf-8", newline="\n")

        index = {
            "source": self.base_url,
            "challenges": [challenge.to_dict() for challenge in challenges],
        }
        (output / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        return challenges


def default_session_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "rhombus-player" / "session.json"


def save_session(path: Path, base_url: str, username: str, token: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"base_url": base_url, "username": username, "token": token}, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix="session-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(payload)
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_session(path: Path) -> dict[str, str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {key: data[key] for key in ("base_url", "username", "token")}
    except FileNotFoundError as error:
        raise AuthenticationError(f"no saved session at {path}; run login first") from error
    except (KeyError, json.JSONDecodeError) as error:
        raise AuthenticationError(f"invalid session file at {path}; log in again") from error


def _render_list(challenges: Iterable[Challenge]) -> None:
    grouped: dict[str, list[Challenge]] = {}
    for challenge in challenges:
        grouped.setdefault(challenge.category, []).append(challenge)
    for category in sorted(grouped):
        print(f"\n{category}")
        print("-" * len(category))
        for challenge in grouped[category]:
            points = f" — {challenge.points} points" if challenge.points is not None else ""
            files = f" — {len(challenge.attachments)} file(s)" if challenge.attachments else ""
            print(f"{challenge.id}: {challenge.name}{points}{files}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Player CLI for an unmodified Rhombus CTF")
    parser.add_argument(
        "--session",
        type=Path,
        default=default_session_path(),
        help="session file path (default: %(default)s)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="maximum retries after HTTP 429 (default: %(default)s)",
    )
    parser.add_argument(
        "--max-retry-wait",
        type=float,
        default=300.0,
        help="maximum accepted server-requested wait in seconds (default: %(default)s)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    login = subcommands.add_parser("login", help="register/sign in using credentials")
    login.add_argument("url", help="Rhombus base URL, e.g. http://localhost:3000")
    login.add_argument("-u", "--username", required=True)
    login.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from standard input instead of prompting",
    )

    listing = subcommands.add_parser("list", help="list visible challenges")
    listing.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    sync = subcommands.add_parser("sync", help="download visible challenges and attachments")
    sync.add_argument("-o", "--output", type=Path, default=Path("challenges"))
    sync.add_argument("--force", action="store_true", help="overwrite downloaded attachments")

    submit = subcommands.add_parser("submit", help="submit a flag for a challenge")
    submit.add_argument("challenge_id", help="stable challenge ID shown by list")
    submit.add_argument("flag", nargs="?", help="flag value; prompts when omitted")
    submit.add_argument("--flag-stdin", action="store_true", help="read the flag from standard input")

    subcommands.add_parser("logout", help="delete the locally saved session")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    def report_rate_limit(wait: float, attempt: int, maximum: int) -> None:
        print(
            f"Rate limited; waiting {wait:.2f}s before retry {attempt}/{maximum}...",
            file=sys.stderr,
        )

    try:
        if args.command == "login":
            password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Password: ")
            api = RhombusAPI(
                args.url,
                max_retries=args.max_retries,
                max_retry_wait=args.max_retry_wait,
                on_rate_limit=report_rate_limit,
            )
            token = api.login(args.username, password)
            save_session(args.session, api.base_url, args.username, token)
            print(f"Authenticated as {args.username} at {api.base_url}")
            print(f"Session saved to {args.session}")
            return 0

        if args.command == "logout":
            try:
                args.session.unlink()
            except FileNotFoundError:
                pass
            print(f"Removed session {args.session}")
            return 0

        session = load_session(args.session)
        api = RhombusAPI(
            session["base_url"],
            session["token"],
            max_retries=args.max_retries,
            max_retry_wait=args.max_retry_wait,
            on_rate_limit=report_rate_limit,
        )
        if args.command == "list":
            challenges = api.challenges()
            if args.json:
                print(json.dumps([challenge.to_dict() for challenge in challenges], indent=2))
            else:
                _render_list(challenges)
            return 0
        if args.command == "sync":
            challenges = api.sync(args.output, force=args.force)
            print(f"Synchronized {len(challenges)} challenges into {args.output.resolve()}")
            return 0
        if args.command == "submit":
            if args.flag_stdin:
                if args.flag is not None:
                    raise ValueError("provide the flag either as an argument or with --flag-stdin, not both")
                flag = sys.stdin.readline().rstrip("\r\n")
            elif args.flag is not None:
                flag = args.flag
            else:
                flag = input("Flag: ").strip()
            if not flag:
                raise ValueError("flag cannot be empty")
            result = api.submit_flag(args.challenge_id, flag)
            print(("Correct: " if result.correct else "Incorrect: ") + result.message)
            return 0 if result.correct else 2
        raise AssertionError(f"unhandled command {args.command}")
    except (RhombusError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
