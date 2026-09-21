"""Password change and reset. See AUTH-77 and AUTH-90."""

import hashlib
import secrets

_HASHES: dict[str, str] = {}

RESET_TEMPLATE = """\
<html><body><p>Reset your password: {link}</p></body></html>
"""


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${digest}"


def change_password(user_id: str, new_password: str) -> None:
    """AUTH-77: stores the new hash but never invalidates live sessions."""
    _HASHES[user_id] = hash_password(new_password)


def render_reset_email(link: str) -> str:
    """AUTH-90 (closed): the template had no doctype, so some clients showed
    the raw source."""
    return "<!doctype html>\n" + RESET_TEMPLATE.format(link=link)
