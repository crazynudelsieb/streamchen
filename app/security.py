"""Tokens, secrets and CSRF.

No accounts (concept §14). Identity is a random session id in a cookie; host
privilege is a bearer secret the host's browser keeps, of which the server
stores only an Argon2 hash.
"""

from __future__ import annotations

import hmac
import re
import secrets
import uuid

from argon2 import PasswordHasher

# OWASP's minimum recommended profile (19 MiB, t=2, p=1). Host secrets carry
# 256 bits of entropy, so this hash defends a leaked database rather than a
# guessable password -- and it is verified on every host action, so it also
# has to stay cheap enough to sit in a request path.
_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)

# Room tokens are urlsafe base64, so the mount point derived from them is safe
# to put in a URL path unescaped.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def new_session_id() -> str:
    return str(uuid.uuid4())


def new_room_token() -> str:
    """``/r/X7mT8fQ29A`` — concept §6."""
    return secrets.token_urlsafe(24)


def new_host_secret() -> str:
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    return _hasher.hash(secret)


def verify_secret(secret_hash: str, secret: str) -> bool:
    # argon2 raises a small family of exceptions for "wrong secret" and one
    # more for "not a hash at all". Callers only ever want the boolean, and a
    # malformed stored hash must read as "no" rather than as a 500.
    try:
        return _hasher.verify(secret_hash, secret)
    except Exception:
        return False


def tokens_equal(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left, right)


def is_valid_room_token(token: str) -> bool:
    return bool(TOKEN_RE.match(token or ""))
