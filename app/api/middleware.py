"""Session cookie, CSRF and cache headers.

All three are cross-cutting and none of them belong in a handler, so they live
here as one pass over every HTTP request.
"""

from __future__ import annotations

import uuid

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings
from app.security import new_csrf_token, new_session_id, tokens_equal

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths whose content is decided by the URL itself and can therefore never go
# stale: versioned static assets, and the generated avatars.
IMMUTABLE_PREFIXES = ("/static/", "/a/")


def _valid_session(value: str | None) -> bool:
    """Only ever trust our own format, so a hand-written cookie cannot be used
    to probe with odd values."""
    if not value:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class SessionMiddleware(BaseHTTPMiddleware):
    """Issues the anonymous session, enforces double-submit CSRF, and sets the
    cache policy for every response (concept §12)."""

    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):
        settings = self.settings

        incoming_session = request.cookies.get(settings.session_cookie)
        session_id = incoming_session if _valid_session(incoming_session) else new_session_id()

        incoming_csrf = request.cookies.get(settings.csrf_cookie)
        csrf_token = incoming_csrf or new_csrf_token()

        request.state.session_id = session_id
        request.state.csrf_token = csrf_token

        if request.method not in SAFE_METHODS and request.url.path.startswith("/api"):
            if not tokens_equal(request.headers.get("X-CSRF-Token"), incoming_csrf):
                return self._with_cookies(
                    JSONResponse(
                        {"detail": "CSRF token missing or invalid"},
                        status_code=status.HTTP_403_FORBIDDEN,
                    ),
                    session_id,
                    csrf_token,
                )

        response = await call_next(request)

        if request.url.path.startswith(IMMUTABLE_PREFIXES):
            # Content-addressed URLs — a versioned asset (?v=<release>) or an
            # avatar drawn from its own seed — so the bytes behind one never
            # change and it can be cached for as long as the browser likes.
            response.headers["Cache-Control"] = "public,max-age=31536000,immutable"
        else:
            # Room state, queue state, rendered pages: everything else is a
            # view of live state and must never be stored (concept §12).
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"

        return self._with_cookies(response, session_id, csrf_token)

    def _with_cookies(self, response, session_id: str, csrf_token: str):
        settings = self.settings
        max_age = settings.session_days * 24 * 3600

        response.set_cookie(
            settings.session_cookie,
            session_id,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
            path="/",
        )
        # Readable by JS on purpose: the client echoes it back in a header,
        # which is the "double submit" half of the CSRF defence.
        response.set_cookie(
            settings.csrf_cookie,
            csrf_token,
            max_age=max_age,
            httponly=False,
            samesite="lax",
            secure=settings.secure_cookies,
            path="/",
        )
        return response
