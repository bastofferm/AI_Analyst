"""Consumer auth endpoints (WA0007 §11, MVP launch checklist).

  POST /api/auth/signup    create user, start 7-day trial, set session cookie
  POST /api/auth/login     verify password, set session cookie
  POST /api/auth/logout    clear session cookie
  GET  /api/auth/me        return current user + access (trial/sub) status

All write paths set the session cookie httponly+samesite=lax. The Next.js
server components read this cookie via cookies().get('mzqa_session').
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from ..auth_utils import (
    SESSION_COOKIE,
    SessionUser,
    current_user,
    hash_password,
    issue_token,
    verify_password,
)
from ..db import acquire

router = APIRouter()
logger = logging.getLogger("mzqa.auth")

_MIN_PASSWORD = 8
_PWD_RE = re.compile(r"^.{8,}$")
# Permissive RFC-5322-ish check — Stripe + downstream do the real validation;
# this only catches obvious typos pre-INSERT.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=_MIN_PASSWORD, max_length=256)
    lang: str = Field(default="en")

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email format")
        return v


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        return v.strip().lower()


class AccessStatus(BaseModel):
    has_access: bool
    trial_end: Optional[str]
    subscription_status: Optional[str]
    subscription_period_end: Optional[str]


class UserResponse(BaseModel):
    id: int
    email: str
    lang: str
    view_mode: str
    trial_started_at: Optional[str]
    trial_end: Optional[str]
    access: AccessStatus


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        # `secure=True` should be added behind HTTPS in prod via reverse proxy.
        secure=False,
        path="/",
    )


async def _fetch_user_with_access(user_id: int) -> UserResponse | None:
    sql = """
        SELECT u.id, u.email, u.lang, u.view_mode,
               u.trial_started_at, u.trial_end,
               s.status AS sub_status, s.current_period_end AS sub_period_end
        FROM   sec.mzqa_user u
        LEFT JOIN LATERAL (
            SELECT status, current_period_end
            FROM sec.mzqa_subscription
            WHERE user_id = u.id
            ORDER BY updated_at DESC
            LIMIT 1
        ) s ON TRUE
        WHERE u.id = $1
    """
    async with acquire() as conn:
        r = await conn.fetchrow(sql, user_id)
    if r is None:
        return None
    now = datetime.now(timezone.utc)
    trial_active = r["trial_end"] is not None and r["trial_end"] > now
    sub_active = r["sub_status"] in {"trialing", "active"}
    return UserResponse(
        id=r["id"],
        email=r["email"],
        lang=r["lang"] or "en",
        view_mode=r["view_mode"] or "simple",
        trial_started_at=r["trial_started_at"].isoformat() if r["trial_started_at"] else None,
        trial_end=r["trial_end"].isoformat() if r["trial_end"] else None,
        access=AccessStatus(
            has_access=trial_active or sub_active,
            trial_end=r["trial_end"].isoformat() if r["trial_end"] else None,
            subscription_status=r["sub_status"],
            subscription_period_end=r["sub_period_end"].isoformat() if r["sub_period_end"] else None,
        ),
    )


# ---------------------------------------------------------------------------
# /signup
# ---------------------------------------------------------------------------

@router.post("/signup", response_model=UserResponse)
async def signup(req: SignupRequest, response: Response) -> UserResponse:
    if not _PWD_RE.match(req.password):
        raise HTTPException(status_code=400, detail=f"password must be ≥{_MIN_PASSWORD} characters")
    email = req.email.strip().lower()
    lang = "de" if req.lang.lower() == "de" else "en"
    pwd_hash = hash_password(req.password)
    async with acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM sec.mzqa_user WHERE lower(email) = $1", email,
        )
        if existing:
            raise HTTPException(status_code=409, detail="email already registered")
        user_id = await conn.fetchval(
            """
            INSERT INTO sec.mzqa_user (email, password_hash, lang, last_login_at)
            VALUES ($1, $2, $3, NOW())
            RETURNING id
            """,
            email, pwd_hash, lang,
        )
    token = issue_token(int(user_id), email)
    _set_session_cookie(response, token)
    user = await _fetch_user_with_access(int(user_id))
    if user is None:
        raise HTTPException(status_code=500, detail="post-signup lookup failed")
    return user


# ---------------------------------------------------------------------------
# /login
# ---------------------------------------------------------------------------

@router.post("/dev-login", response_model=UserResponse)
async def dev_login(response: Response) -> UserResponse:
    if os.environ.get("MZQA_DEV_AUTH") != "1":
        raise HTTPException(status_code=404, detail="not found")
    email = "demo@mzqa.local"
    pwd_hash = hash_password("demo1234")
    async with acquire() as conn:
        user_id = await conn.fetchval(
            """
            INSERT INTO sec.mzqa_user (
                email, password_hash, lang, trial_started_at, trial_end, last_login_at
            )
            VALUES ($1, $2, 'de', NOW(), NOW() + INTERVAL '90 days', NOW())
            ON CONFLICT (email) DO UPDATE SET
                password_hash = EXCLUDED.password_hash,
                trial_end = NOW() + INTERVAL '90 days',
                last_login_at = NOW(),
                updated_at = NOW()
            RETURNING id
            """,
            email,
            pwd_hash,
        )
    token = issue_token(int(user_id), email)
    _set_session_cookie(response, token)
    user = await _fetch_user_with_access(int(user_id))
    if user is None:
        raise HTTPException(status_code=500, detail="post-login lookup failed")
    return user


@router.post("/login", response_model=UserResponse)
async def login(req: LoginRequest, response: Response) -> UserResponse:
    email = req.email.strip().lower()
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, password_hash FROM sec.mzqa_user WHERE lower(email) = $1",
            email,
        )
    if row is None or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid credentials")
    async with acquire() as conn:
        await conn.execute(
            "UPDATE sec.mzqa_user SET last_login_at = NOW(), updated_at = NOW() WHERE id = $1",
            row["id"],
        )
    token = issue_token(int(row["id"]), row["email"])
    _set_session_cookie(response, token)
    user = await _fetch_user_with_access(int(row["id"]))
    if user is None:
        raise HTTPException(status_code=500, detail="post-login lookup failed")
    return user


# ---------------------------------------------------------------------------
# /logout
# ---------------------------------------------------------------------------

@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserResponse | None)
async def me(u: Optional[SessionUser] = Depends(current_user)) -> UserResponse | None:
    if u is None:
        return None
    return await _fetch_user_with_access(u.id)
