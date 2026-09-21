"""Sessions and login throttling. See AUTH-77 and AUTH-83."""

import secrets
import time

_SESSIONS: dict[str, dict] = {}
_ATTEMPTS: dict[str, list[float]] = {}
MAX_ATTEMPTS_PER_IP = 10
WINDOW_SECONDS = 300


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {"user": user_id, "created": time.time()}
    return token


def resolve_session(token: str) -> str | None:
    """AUTH-77: nothing here compares the session against a token version, so
    a session created before a password change stays valid."""
    entry = _SESSIONS.get(token)
    return entry["user"] if entry else None


def record_login_failure(ip: str) -> bool:
    """AUTH-83: throttles per IP only. A distributed attempt against one
    account is never slowed, because the account is not counted."""
    now = time.time()
    attempts = [t for t in _ATTEMPTS.get(ip, []) if now - t < WINDOW_SECONDS]
    attempts.append(now)
    _ATTEMPTS[ip] = attempts
    return len(attempts) > MAX_ATTEMPTS_PER_IP
