"""Bounded, secret-redacted HTTP telemetry for the local simulator only."""
from contextvars import ContextVar
import base64
import http.client
import re
import time
import urllib.error
import urllib.parse
import uuid

from rhombus_player import RhombusError

CONTEXT = ContextVar("rhombus_http_context", default={})
PREVIEW_LIMIT = 4096


class RequestCancelled(RhombusError):
    """Request cancelled before admission; not a claim about earlier attempts."""


def safe_headers(headers, response=False):
    allowed = {"content-type", "content-length", "retry-after", "x-ratelimit-after",
               "x-ratelimit-limit", "x-ratelimit-remaining", "accept-language", "user-agent"}
    # Never persist credentials, session cookies, redirect query tokens or encoded toast payloads.
    return {key.lower(): str(value)[:256] if key.lower() in allowed else "[omitted]"
            for key, value in headers.items()}


class Trace:
    def __init__(self, journal, request, attempt, operation_id, token=None):
        self.journal = journal
        self.request = request
        self.started = time.monotonic()
        self.admitted = None
        self.id = uuid.uuid4().hex
        self.context = CONTEXT.get()
        fields = urllib.parse.parse_qs((request.data or b"").decode("utf-8", errors="replace"))
        self.secrets = {v for values in fields.values() for v in values if v}
        if token:
            self.secrets.add(token)
        # Redact common reflected encodings as well as literal form values.
        for value in list(self.secrets):
            self.secrets.update({urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value),
                                 base64.b64encode(value.encode()).decode()})
        self.login = urllib.parse.urlsplit(request.full_url).path.endswith("/signin/credentials")
        self.record("http_queued", operation_id=operation_id, attempt=attempt,
                    method=request.get_method(), path=urllib.parse.urlsplit(request.full_url).path,
                    query="[omitted]" if urllib.parse.urlsplit(request.full_url).query else "",
                    request_headers=safe_headers(dict(request.header_items())),
                    form={key: "[redacted]" for key in fields}, request_bytes=len(request.data or b""))

    def clean(self, text):
        for value in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        return re.sub(r"rhombus\{[^\r\n}]*\}", "[redacted flag]", text, flags=re.I)

    def record(self, event, **fields):
        correlation = {k: self.context[k] for k in ("challenge_key", "worker", "submission_ticket") if k in self.context}
        self.journal.record(event, request_id=self.id, **correlation, **fields)

    def open(self, opener, timeout):
        cancel = self.context.get("cancel")
        if cancel is not None and cancel.is_set():
            error = RequestCancelled("operator stopped this worker before transport dispatch")
            self.failure(error, blocked=True)
            raise error
        self.admitted = time.monotonic()
        self.record("http_sent", sent_at=time.time(), wait_ms=round((self.admitted - self.started) * 1000, 2))
        try:
            response = opener.open(self.request, timeout=timeout)
        except urllib.error.HTTPError as error:
            wrapped = self.wrap(error)
            try:
                wrapped.read(PREVIEW_LIMIT + 1)
            finally:
                wrapped.close()
            raise
        except Exception as error:
            self.failure(error)
            raise
        return self.wrap(response)

    def wrap(self, response):
        try:
            self.record("http_response", status=response.code, responded_at=time.time(),
                        latency_ms=round((time.monotonic() - self.admitted) * 1000, 2),
                        response_headers={k: self.clean(v) for k, v in safe_headers(response.headers, response=True).items()})
        except Exception:
            response.close()
            raise
        return TracedResponse(response, self)

    def failure(self, error, blocked=False):
        # Exception messages may contain untrusted response bodies or credential-bearing URLs.
        # Class/reason class are useful without persisting such messages.
        reason = getattr(error, "reason", error)
        error_code = getattr(reason, "errno", None)
        self.record("http_blocked" if blocked else "http_error", error=type(error).__name__,
                    reason_type=type(reason).__name__, errno=error_code if type(error_code) is int else None,
                    duration_ms=round((time.monotonic() - self.started) * 1000, 2))


class TracedResponse:
    def __init__(self, response, trace):
        self.response, self.trace = response, trace
        self.captured = bytearray()
        self.bytes_read = 0
        self.read_complete = False
        self.failed = False
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read(self, size=None):
        try:
            # HTTPResponse.read(None) verifies the declared length; read(-1) does
            # not have identical semantics on all supported Python versions.
            chunk = self.response.read(size)
            reached_end = size != 0 and (not chunk or size is None or size < 0 or len(chunk) < size)
            declared = self.response.headers.get("Content-Length", "")
            chunked = "chunked" in self.response.headers.get("Transfer-Encoding", "").lower()
            if reached_end and declared.isdecimal() and not chunked:
                missing = int(declared) - self.bytes_read - len(chunk)
                if missing > 0:
                    raise http.client.IncompleteRead(chunk, missing)
        except Exception as error:
            self.failed = True
            self.trace.failure(error)
            raise
        self.bytes_read += len(chunk)
        if len(self.captured) <= PREVIEW_LIMIT:
            self.captured.extend(chunk[:PREVIEW_LIMIT + 1 - len(self.captured)])
        if reached_end:
            self.read_complete = True
        return chunk

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.response.close()
        finally:
            content_type = self.response.headers.get("Content-Type", "").lower()
            reason = None
            if self.trace.login:
                reason = "authentication response body omitted"
            elif len(self.captured) > PREVIEW_LIMIT:
                reason = "body exceeds 4096-byte preview limit"
            elif not self.read_complete:
                reason = "body not fully consumed; partial preview omitted"
            elif not any(kind in content_type for kind in ("text/", "json", "xml")):
                reason = "binary or untyped body omitted"
            preview = None if reason else self.trace.clean(self.captured.decode("utf-8", errors="replace"))
            self.trace.record("http_complete", finished_at=time.time(), bytes_read=self.bytes_read, read_complete=self.read_complete,
                              body_preview=preview, body_omitted=reason, read_error=self.failed,
                              duration_ms=round((time.monotonic() - self.trace.started) * 1000, 2))
