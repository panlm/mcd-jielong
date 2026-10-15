"""IDs, secrets, rate limiting, client IP."""
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque

_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def new_sid() -> str:
    return "".join(secrets.choice(_B62) for _ in range(12))


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_secret() -> tuple[str, str]:
    s = secrets.token_urlsafe(24)
    return s, hash_secret(s)


class RateLimiter:
    """Sliding window: at most `limit` hits per `window_s` per key."""

    def __init__(self, limit: int, window_s: int, clock=time.monotonic):
        self.limit, self.window_s, self._clock = limit, window_s, clock
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


class HostAuthThrottle:
    """Failed host-auth attempts per IP: `limit` failures within `window_s` mark that IP as
    blocked until enough failures age out. Blocked IPs get 429 for wrong keys; the right key
    still passes (see check_host_key). Successes never count."""

    def __init__(self, limit: int = 10, window_s: int = 600, clock=time.monotonic):
        self.limit, self.window_s, self._clock = limit, window_s, clock
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        q = self._fails[key]
        while q and q[0] <= now - self.window_s:
            q.popleft()
        if not q:
            self._fails.pop(key, None)
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._trim(key, self._clock())) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            self._trim(key, now)
            self._fails[key].append(now)


_KEY_PARAM = re.compile(r"([?&;]k=)[^&\s\"#]*")


def redact_host_key(text: str) -> str:
    return _KEY_PARAM.sub(r"\1***", text)


class RedactHostKeyFilter(logging.Filter):
    """Defense in depth. Login now uses a URL fragment (#k=, never sent to the server) plus a
    POST body, but an old or hand-typed `/host?k=<key>` would still put the key in uvicorn's
    access log. Rewrites the `k` query parameter value to `***`."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_host_key(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_host_key(a) if isinstance(a, str) else a for a in record.args)
        return True


def install_log_redaction() -> None:
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactHostKeyFilter) for f in lg.filters):
        lg.addFilter(RedactHostKeyFilter())


def client_ip(request) -> str:
    direct = request.client.host if request.client else ""
    if direct == "127.0.0.1":
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # last hop is the one our own proxy appended; earlier ones are client-controlled
            return xff.split(",")[-1].strip() or direct
    return direct


CSRF_HEADER_VALUE = "mcd-jielong"  # host.js sends X-Requested-With: mcd-jielong
MAX_HOST_KEY = 256


class HostAuthError(Exception):
    """Raised by the host guard; main.py renders it as {"error": code}."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def csrf_ok(request) -> bool:
    """I4: only our own page may call /api/host/*. A custom header cannot be sent cross-origin
    without a CORS preflight (which we never answer), and a browser that sends Sec-Fetch-Site
    must say same-origin (rejects other *.example subdomains, i.e. same-site)."""
    if request.headers.get("x-requested-with") != CSRF_HEADER_VALUE:
        return False
    site = request.headers.get("sec-fetch-site")
    return site is None or site == "same-origin"


def check_host_key(request, settings, supplied) -> None:
    """Pass if any supplied key is right, else raise HostAuthError.

    M2: the right key always passes, even from a throttled IP (an office shares one NAT IP,
    so strangers must not be able to lock the host out). A wrong key always counts as a
    failure; once the IP has `limit` failures in the window, wrong keys get 429 instead of
    401. Requests carrying no key at all are not guesses and are not counted."""
    supplied = [k for k in supplied if k]
    if any(host_key_ok(settings, k) for k in supplied):
        return
    if not supplied:
        raise HostAuthError(401, "unauthorized")
    throttle = request.app.state.host_throttle
    ip = client_ip(request)
    was_blocked = throttle.blocked(ip)
    throttle.fail(ip)
    if was_blocked:
        raise HostAuthError(429, "rate_limited")
    raise HostAuthError(401, "unauthorized")


def require_host(settings):
    """FastAPI dependency factory for /api/host/*. Demo passes (mock data, no key).
    Real: CSRF check first (I4), then cookie hk / X-Host-Key must equal host_key."""
    from fastapi import Request

    def dep(request: Request) -> None:
        if settings.mode == "demo":
            return
        if not csrf_ok(request):
            raise HostAuthError(403, "csrf")
        check_host_key(request, settings,
                       (request.cookies.get("hk"), request.headers.get("x-host-key")))

    return dep


def host_key_ok(settings, supplied) -> bool:
    expected = settings.host_key
    if not expected or not isinstance(supplied, str) or not supplied:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())
