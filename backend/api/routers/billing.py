"""Stripe Checkout + webhook (WA0007 §11 Payments, §12 launch checklist).

Implemented against the Stripe REST API via httpx so we don't introduce the
`stripe` SDK as a dependency.

Env vars (all optional in dev — without them, /checkout returns a stub URL):
  STRIPE_SECRET_KEY      sk_live_... / sk_test_...
  STRIPE_PRICE_ID        price_... for the €29/month plan
  STRIPE_WEBHOOK_SECRET  whsec_...
  MZQA_PUBLIC_BASE_URL   https://app.mzqa.example (for return URLs)

Endpoints:
  POST /api/stripe/checkout         create Checkout Session, return URL
  POST /api/stripe/portal           create billing-portal session
  POST /api/stripe/webhook          consume Stripe events, update mzqa_subscription
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth_utils import SessionUser, require_user
from ..db import acquire

router = APIRouter()
logger = logging.getLogger("mzqa.billing")

STRIPE_API = "https://api.stripe.com/v1"


def _stripe_key() -> str | None:
    return os.environ.get("STRIPE_SECRET_KEY") or None


def _base_url() -> str:
    return os.environ.get("MZQA_PUBLIC_BASE_URL", "http://127.0.0.1:3001").rstrip("/")


async def _stripe_post(path: str, form: dict[str, str]) -> dict:
    key = _stripe_key()
    if not key:
        raise HTTPException(status_code=503, detail="Stripe is not configured in this environment")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{STRIPE_API}{path}",
            data=form,
            auth=(key, ""),
            headers={"Stripe-Version": "2024-09-30.acacia"},
        )
    if r.status_code >= 400:
        logger.warning("stripe %s -> %s: %s", path, r.status_code, r.text[:300])
        raise HTTPException(status_code=502, detail=f"Stripe error: {r.status_code}")
    return r.json()


async def _ensure_customer(user_id: int, email: str) -> str:
    """Return the user's Stripe customer ID, creating one if needed."""
    async with acquire() as conn:
        existing = await conn.fetchval(
            "SELECT stripe_customer_id FROM sec.mzqa_user WHERE id = $1", user_id,
        )
    if existing:
        return existing
    cust = await _stripe_post("/customers", {"email": email, "metadata[user_id]": str(user_id)})
    cust_id = cust["id"]
    async with acquire() as conn:
        await conn.execute(
            "UPDATE sec.mzqa_user SET stripe_customer_id = $1, updated_at = NOW() WHERE id = $2",
            cust_id, user_id,
        )
    return cust_id


# ---------------------------------------------------------------------------
# /checkout
# ---------------------------------------------------------------------------

@router.post("/checkout")
async def checkout(u: SessionUser = Depends(require_user)) -> dict:
    """Create a Stripe Checkout session for the €29/month plan."""
    if not _stripe_key():
        # Dev fallback: return a stub URL so the UI is clickable without keys.
        return {"url": f"{_base_url()}/app/dashboard?stripe=stub"}
    price_id = os.environ.get("STRIPE_PRICE_ID")
    if not price_id:
        raise HTTPException(status_code=500, detail="STRIPE_PRICE_ID not set")
    cust_id = await _ensure_customer(u.id, u.email)
    form = {
        "mode": "subscription",
        "customer": cust_id,
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        # 7-day Stripe-side trial mirrors the trial we set on signup.
        "subscription_data[trial_period_days]": "7",
        "success_url": f"{_base_url()}/app/dashboard?checkout=success",
        "cancel_url": f"{_base_url()}/pricing?checkout=canceled",
        "allow_promotion_codes": "true",
        "client_reference_id": str(u.id),
    }
    session = await _stripe_post("/checkout/sessions", form)
    return {"url": session["url"], "session_id": session["id"]}


# ---------------------------------------------------------------------------
# /portal — "Manage billing" link for active subscribers
# ---------------------------------------------------------------------------

@router.post("/portal")
async def portal(u: SessionUser = Depends(require_user)) -> dict:
    if not _stripe_key():
        return {"url": f"{_base_url()}/app/dashboard?stripe=stub"}
    cust_id = await _ensure_customer(u.id, u.email)
    session = await _stripe_post(
        "/billing_portal/sessions",
        {"customer": cust_id, "return_url": f"{_base_url()}/app/dashboard"},
    )
    return {"url": session["url"]}


# ---------------------------------------------------------------------------
# /webhook — Stripe -> us. Updates mzqa_subscription on each event.
# ---------------------------------------------------------------------------

def _verify_webhook(payload: bytes, sig_header: str | None, secret: str) -> bool:
    """Verify Stripe-Signature per https://stripe.com/docs/webhooks/signatures."""
    if not sig_header:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        return False
    if abs(time.time() - int(timestamp)) > 600:  # 10 min replay window
        return False
    signed = f"{timestamp}.{payload.decode('utf-8', errors='replace')}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _upsert_subscription(sub: dict) -> None:
    """Project a Stripe subscription object into sec.mzqa_subscription."""
    cust_id = sub.get("customer")
    if not cust_id:
        return
    async with acquire() as conn:
        user_id = await conn.fetchval(
            "SELECT id FROM sec.mzqa_user WHERE stripe_customer_id = $1", cust_id,
        )
        if not user_id:
            logger.warning("stripe webhook: unknown customer %s", cust_id)
            return
        items = sub.get("items", {}).get("data") or []
        price_id = items[0].get("price", {}).get("id") if items else None

        def _ts(s: int | None) -> datetime | None:
            return datetime.fromtimestamp(s, tz=timezone.utc) if s else None

        await conn.execute(
            """
            INSERT INTO sec.mzqa_subscription (
                user_id, stripe_subscription_id, status, price_id,
                current_period_end, cancel_at, canceled_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (stripe_subscription_id) DO UPDATE SET
                status              = EXCLUDED.status,
                price_id            = EXCLUDED.price_id,
                current_period_end  = EXCLUDED.current_period_end,
                cancel_at           = EXCLUDED.cancel_at,
                canceled_at         = EXCLUDED.canceled_at,
                updated_at          = NOW()
            """,
            int(user_id), sub["id"], sub["status"], price_id,
            _ts(sub.get("current_period_end")),
            _ts(sub.get("cancel_at")),
            _ts(sub.get("canceled_at")),
        )


@router.post("/webhook")
async def webhook(request: Request) -> dict:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    payload = await request.body()
    if secret:
        sig = request.headers.get("stripe-signature")
        if not _verify_webhook(payload, sig, secret):
            raise HTTPException(status_code=400, detail="invalid signature")
    try:
        event = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")
    etype = event.get("type", "")
    data = (event.get("data") or {}).get("object") or {}
    if etype.startswith("customer.subscription."):
        await _upsert_subscription(data)
    elif etype == "checkout.session.completed":
        # Checkout success — Stripe also fires customer.subscription.created
        # right after, so we don't double-handle the subscription here.
        logger.info("stripe checkout completed for customer=%s", data.get("customer"))
    return {"received": True, "type": etype}
