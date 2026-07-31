"""Auth helpers for the consumer app (WA0007 §11).

Custom JWT-based auth lives in FastAPI rather than NextAuth so the same
session check works from the Next SSR layer (via cookie) and any future
server-to-server callers. Sessions are stateless JWTs in an httponly cookie.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import jwt
from fastapi import Cookie, HTTPException

SESSION_COOKIE = "mzqa_session"
JWT_ALG = "HS256"
JWT_TTL_DAYS = 30


def _jwt_secret() -> str:
    s = os.environ.get("MZQA_JWT_SECRET")
    if s:
        return s
    # Dev fallback. Stable per-process so sessions survive a single dev session,
    # but rotates on restart — fine for local; production MUST set the env var.
    if not hasattr(_jwt_secret, "_dev"):
        _jwt_secret._dev = secrets.token_urlsafe(32)  # type: ignore[attr-defined]
    return _jwt_secret._dev  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def issue_token(user_id: int, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=JWT_TTL_DAYS)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALG)


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------------------
# session dependencies
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionUser:
    id: int
    email: str


async def current_user(
    mzqa_session: Optional[str] = Cookie(default=None),
) -> Optional[SessionUser]:
    """Return the session user if present, else None — used by /api/auth/me."""
    if not mzqa_session:
        return None
    payload = decode_token(mzqa_session)
    if not payload:
        return None
    try:
        return SessionUser(id=int(payload["sub"]), email=str(payload["email"]))
    except (KeyError, ValueError, TypeError):
        return None


async def require_user(
    mzqa_session: Optional[str] = Cookie(default=None),
) -> SessionUser:
    """401 if no session — used by endpoints that require auth."""
    u = await current_user(mzqa_session)
    if u is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return u
