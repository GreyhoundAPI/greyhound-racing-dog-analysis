"""A small GreyhoundAPI client: X-API-Key auth, pacing from the key's own limits, honest errors."""

import json
import socket
import time
from collections import deque
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import REPO_URL, TOOL, __version__

PACE_SHARE = 0.85
FALLBACK_PER_MINUTE = 50


class ApiError(Exception):
    """A request the API refused, or one that could not be completed."""

    def __init__(self, status, code, message, payload=None):
        Exception.__init__(self, message)
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload if isinstance(payload, dict) else {}

    def summary(self):
        if self.status:
            return "%d %s: %s" % (self.status, self.code, self.message)
        return "%s: %s" % (self.code, self.message)

    def as_dict(self):
        return {"status": self.status, "code": self.code, "message": self.message}


def seconds_until(value):
    """Read X-RateLimit-Reset as epoch seconds, seconds from now, or an ISO instant."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        if number > 1e9:
            return max(0.0, number - time.time())
        return max(0.0, number)
    cleaned = text.replace("T", " ").replace("Z", "")[:19]
    try:
        moment = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


class Client(object):
    """Every call goes through get(), which paces itself against the key's per-minute limit."""

    def __init__(self, key, base, on_wait=None, timeout=45):
        self.key = key
        self.base = base.rstrip("/")
        self.on_wait = on_wait
        self.timeout = timeout
        self.calls = 0
        self.by_endpoint = {}
        self.minute_limit = None
        self.remaining = None
        self.reset_in = None
        self.recent = deque()

    def per_minute(self):
        if self.minute_limit:
            return max(1, int(self.minute_limit * PACE_SHARE))
        return FALLBACK_PER_MINUTE

    def _wait(self, seconds, reason):
        if seconds <= 0:
            return
        if self.on_wait is not None:
            self.on_wait(seconds, reason)
        else:
            time.sleep(seconds)

    def _pace(self):
        now = time.monotonic()
        while self.recent and now - self.recent[0] >= 60.0:
            self.recent.popleft()
        allowed = self.per_minute()
        if len(self.recent) >= allowed:
            self._wait(60.0 - (now - self.recent[0]) + 0.1, "pacing at %d calls a minute" % allowed)
        if self.remaining is not None and self.remaining <= 0 and self.reset_in and self.reset_in <= 65:
            self._wait(self.reset_in, "this minute's allowance is used up")
            self.remaining = None

    def _limits(self, headers):
        if headers is None:
            return
        limit = headers.get("X-RateLimit-Limit")
        try:
            value = int(float(limit)) if limit is not None else None
        except ValueError:
            value = None
        # Only a per-minute sized figure is used for pacing; /v1/usage sets it first.
        if value and value <= 1000 and not self.minute_limit:
            self.minute_limit = value
        remaining = headers.get("X-RateLimit-Remaining")
        try:
            self.remaining = int(float(remaining)) if remaining is not None else None
        except ValueError:
            self.remaining = None
        self.reset_in = seconds_until(headers.get("X-RateLimit-Reset"))

    def get(self, path, params=None, endpoint=None, counted=True):
        query = [(k, v) for k, v in (params or {}).items() if v is not None]
        url = self.base + path
        if query:
            url += "?" + urlencode(query)
        tries = {"network": 0, "busy": 0, "server": 0}
        while True:
            if counted:
                self._pace()
            request = Request(
                url,
                headers={
                    "X-API-Key": self.key,
                    "Accept": "application/json",
                    "User-Agent": "%s/%s (+%s)" % (TOOL, __version__, REPO_URL),
                },
            )
            status, body, headers = 0, b"", None
            try:
                response = urlopen(request, timeout=self.timeout)
                try:
                    status = response.getcode()
                    body = response.read()
                    headers = response.headers
                finally:
                    response.close()
            except HTTPError as exc:
                status = exc.code
                headers = exc.headers
                try:
                    body = exc.read() or b""
                except Exception:
                    body = b""
            except (URLError, socket.timeout, ConnectionError, OSError) as exc:
                tries["network"] += 1
                reason = getattr(exc, "reason", None) or exc
                if tries["network"] > 3:
                    raise ApiError(0, "network", "Could not reach %s (%s)." % (self.base, reason))
                self._wait(3.0 * tries["network"], "network trouble (%s), retrying" % reason)
                continue

            if counted:
                self.recent.append(time.monotonic())
                self.calls += 1
                name = endpoint or path
                self.by_endpoint[name] = self.by_endpoint.get(name, 0) + 1
            self._limits(headers)

            payload = None
            if body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    payload = None

            if 200 <= status < 300:
                if not isinstance(payload, dict):
                    raise ApiError(
                        status,
                        "not_json",
                        "The API answered %d, but the body was not JSON (%d bytes)." % (status, len(body)),
                    )
                return payload

            error = payload.get("error") if isinstance(payload, dict) else None
            error = error if isinstance(error, dict) else {}
            code = error.get("code") or ("http_%d" % status)
            message = error.get("message") or ""

            if status == 401 and not error and headers is not None:
                gate = headers.get("WWW-Authenticate") or ""
                if gate.lower().startswith("basic"):
                    raise ApiError(
                        401,
                        "basic_auth_gate",
                        "A Basic auth prompt (%s) answered instead of the API, so the request never "
                        "reached it. That is server configuration in front of the API, not the key." % gate.strip(),
                    )

            if status == 429 and code != "quota_exceeded":
                tries["busy"] += 1
                if tries["busy"] <= 4:
                    wait = self.reset_in if self.reset_in else 20.0
                    self._wait(min(max(wait, 2.0), 65.0), "the API asked for a slower pace")
                    continue

            if status >= 500:
                tries["server"] += 1
                if tries["server"] <= 3:
                    self._wait((2.0, 6.0, 15.0)[tries["server"] - 1], "the API answered %d, retrying" % status)
                    continue

            if not message:
                message = "HTTP %d with a %d byte body." % (status, len(body))
            raise ApiError(status, code, message, payload)
