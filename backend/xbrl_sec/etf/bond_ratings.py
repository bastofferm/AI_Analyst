"""Multi-source bond rating resolution chain.

Replaces the brittle Moody's-only path that produces 'Too Many Requests' errors
in logs/etf_bond_ratings_backfill_*.err.log. Tries each official source in
order, falls back to a DeepSeek approximation as last resort (always tagged
with confidence_warn=True so the UI can flag it).

Used inside the bond_ratings_multi_source node of the ETF daily graph.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

import httpx

from xbrl_sec.llm import ChatDeepSeek
from xbrl_sec.llm.schemas import BondRatingResolution


_MOODYS_BASE = os.environ.get("MOODYS_RATINGS_BASE_URL") or "https://www.moodys.com/api/instruments"
_SP_BASE = os.environ.get("SP_RATINGS_BASE_URL") or "https://www.spglobal.com/ratings/api/instruments"
_FITCH_BASE = os.environ.get("FITCH_RATINGS_BASE_URL") or "https://www.fitchratings.com/api/instruments"


class _RatingProbe:
    """Result of a single source attempt. Internal to this module."""

    def __init__(
        self,
        source: str,
        rating: str | None,
        confidence: float,
        url: str | None = None,
        error: str | None = None,
    ) -> None:
        self.source = source
        self.rating = rating
        self.confidence = confidence
        self.url = url
        self.error = error

    @property
    def ok(self) -> bool:
        return bool(self.rating) and self.error is None


def _safe_get_json(url: str, params: dict | None, headers: dict | None, timeout: float = 20.0) -> tuple[int, dict | None, str | None]:
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        return 0, None, str(exc)[:200]
    if response.status_code == 429:
        return 429, None, "rate limited"
    if response.status_code != 200:
        return response.status_code, None, response.text[:200]
    try:
        return 200, response.json(), None
    except ValueError:
        return 200, None, "invalid json"


def try_moodys_rating(isin: str, *, timeout: float = 20.0) -> _RatingProbe:
    api_key = os.environ.get("MOODYS_API_KEY") or ""
    if not api_key:
        return _RatingProbe("moodys.com", None, 0.0, error="MOODYS_API_KEY missing")
    headers = {"Authorization": f"Bearer {api_key}"}
    status, data, err = _safe_get_json(
        f"{_MOODYS_BASE}/{isin}/rating",
        params=None,
        headers=headers,
        timeout=timeout,
    )
    if status == 429:
        return _RatingProbe("moodys.com", None, 0.0, error="rate limited")
    if status != 200 or not data:
        return _RatingProbe("moodys.com", None, 0.0, error=err or f"http {status}")
    rating = data.get("rating") or data.get("longTermRating")
    return _RatingProbe(
        "moodys.com",
        rating,
        confidence=0.95 if rating else 0.0,
        url=f"https://www.moodys.com/credit-ratings/{isin}",
        error=None if rating else "no rating in payload",
    )


def try_sp_rating(isin: str, *, timeout: float = 20.0) -> _RatingProbe:
    api_key = os.environ.get("SP_API_KEY") or ""
    if not api_key:
        return _RatingProbe("spglobal.com", None, 0.0, error="SP_API_KEY missing")
    headers = {"Authorization": f"Bearer {api_key}"}
    status, data, err = _safe_get_json(
        f"{_SP_BASE}/{isin}/rating",
        params=None,
        headers=headers,
        timeout=timeout,
    )
    if status != 200 or not data:
        return _RatingProbe("spglobal.com", None, 0.0, error=err or f"http {status}")
    rating = data.get("longTermLocalCurrencyRating") or data.get("rating")
    return _RatingProbe(
        "spglobal.com",
        rating,
        confidence=0.93 if rating else 0.0,
        url=f"https://www.spglobal.com/ratings/{isin}",
        error=None if rating else "no rating in payload",
    )


def try_fitch_rating(isin: str, *, timeout: float = 20.0) -> _RatingProbe:
    api_key = os.environ.get("FITCH_API_KEY") or ""
    if not api_key:
        return _RatingProbe("fitch.com", None, 0.0, error="FITCH_API_KEY missing")
    headers = {"Authorization": f"Bearer {api_key}"}
    status, data, err = _safe_get_json(
        f"{_FITCH_BASE}/{isin}/rating",
        params=None,
        headers=headers,
        timeout=timeout,
    )
    if status != 200 or not data:
        return _RatingProbe("fitch.com", None, 0.0, error=err or f"http {status}")
    rating = data.get("rating")
    return _RatingProbe(
        "fitch.com",
        rating,
        confidence=0.90 if rating else 0.0,
        url=f"https://www.fitchratings.com/entity/{isin}",
        error=None if rating else "no rating in payload",
    )


def deepseek_rating_approximation(
    isin: str,
    *,
    fund_name: str | None,
    issuer_name: str | None,
    llm: Optional[ChatDeepSeek] = None,
) -> _RatingProbe:
    """LLM-Approximation als Letzter Ausweg. confidence_warn=True markiert das in
    der finalen BondRatingResolution. Niemals als primäre Quelle verwenden."""
    if llm is None:
        llm = ChatDeepSeek(model="deepseek-v4-flash", temperature=0.0, max_tokens=400)
    prompt = (
        "Given a European bond ETF, approximate its weighted-average credit rating "
        "on the S&P scale (AAA, AA+, AA, AA-, ...). Return a single ratings symbol "
        "only — no prose. If the ETF is not bond-focused, return 'N/A'.\n\n"
        f"ISIN: {isin}\n"
        f"Fund name: {fund_name or 'unknown'}\n"
        f"Issuer: {issuer_name or 'unknown'}\n"
    )
    try:
        response = llm.invoke(prompt)
    except Exception as exc:  # noqa: BLE001 - probe failure, not pipeline failure
        return _RatingProbe("deepseek_llm", None, 0.0, error=str(exc)[:200])
    raw = response.content if isinstance(response.content, str) else str(response.content)
    rating = raw.strip().split()[0] if raw.strip() else None
    if not rating or rating.upper() in {"N/A", "NA", "UNKNOWN"}:
        return _RatingProbe("deepseek_llm", None, 0.0, error="LLM declined to approximate")
    return _RatingProbe(
        "deepseek_llm",
        rating.upper(),
        confidence=0.5,
        url=None,
        error=None,
    )


def resolve_bond_rating(
    isin: str,
    *,
    fund_name: str | None = None,
    issuer_name: str | None = None,
    use_llm_fallback: bool = True,
    probes: Optional[list[Callable[..., _RatingProbe]]] = None,
    llm: Optional[ChatDeepSeek] = None,
) -> BondRatingResolution:
    """Run the failover chain and return the first usable rating.

    Order: Moody's → S&P → Fitch → optional DeepSeek approximation.
    """
    attempts = probes or [try_moodys_rating, try_sp_rating, try_fitch_rating]
    for probe_fn in attempts:
        probe = probe_fn(isin)
        if probe.ok:
            return BondRatingResolution(
                isin=isin,
                rating=probe.rating,
                rating_scale=_scale_for_source(probe.source),
                source=probe.source,
                source_url=probe.url,
                confidence=probe.confidence,
                confidence_warn=False,
                rationale=f"resolved from {probe.source}",
            )

    if use_llm_fallback:
        approx = deepseek_rating_approximation(
            isin,
            fund_name=fund_name,
            issuer_name=issuer_name,
            llm=llm,
        )
        if approx.ok:
            return BondRatingResolution(
                isin=isin,
                rating=approx.rating,
                rating_scale="approximation",
                source=approx.source,
                source_url=None,
                confidence=approx.confidence,
                confidence_warn=True,
                rationale="all official sources unavailable; DeepSeek approximation",
            )

    return BondRatingResolution(
        isin=isin,
        rating=None,
        rating_scale="approximation",
        source="none",
        source_url=None,
        confidence=0.0,
        confidence_warn=True,
        rationale="no source returned a rating",
    )


def _scale_for_source(source: str):
    if source.startswith("moodys"):
        return "moodys"
    if source.startswith("spglobal") or source.startswith("sp."):
        return "sp"
    if source.startswith("fitch"):
        return "fitch"
    return "approximation"
