"""13F institutional-ownership evidence (market structure, not a valuation driver).

Reuses ``ai_analyst.tools.get_institutional_holders`` (which already resolves the
latest 13F quarter, top holders, QoQ share changes and manager classification) and
summarizes it into: top holders, net accumulation/reduction, active-vs-passive
concentration, and notable adds/reduces.
"""
from __future__ import annotations

from typing import Any

_PASSIVE_HINTS = ("index", "traditional", "passive")


def ownership_summary(ticker: str, top_n: int = 12) -> dict[str, Any]:
    try:
        from .. import tools
        data = tools.get_institutional_holders(ticker)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": f"13F unavailable: {exc.__class__.__name__}"}
    rows = (data or {}).get("rows") or []
    if not rows:
        return {"available": False, "note": (data or {}).get("error") or "no 13F holdings"}

    holders = []
    net_add_shares = 0.0
    passive_mv = active_mv = 0.0  # concentration by reported market value ($), not weight_pct
    for r in rows:
        chg = _num(r.get("shares_changed"))
        mv = _num(r.get("market_value_usd")) or 0.0
        mtype = str(r.get("manager_type") or "").lower()
        cls = str(r.get("classification_label") or "")
        is_passive = mtype == "traditional" or any(h in cls.lower() for h in _PASSIVE_HINTS)
        if chg is not None:
            net_add_shares += chg
        if is_passive:
            passive_mv += mv
        else:
            active_mv += mv
        holders.append({
            "manager": r.get("manager_name"),
            "classification": cls,
            "manager_type": mtype,
            "shares_held": _num(r.get("shares_held")),
            "market_value_usd": mv,
            "weight_pct": _num(r.get("weight_pct")) or 0.0,
            "shares_changed": chg,
            "is_passive": is_passive,
        })

    changed = [h for h in holders if h.get("shares_changed") is not None]
    top_adds = sorted([h for h in changed if h["shares_changed"] > 0], key=lambda h: -h["shares_changed"])[:5]
    top_reduces = sorted([h for h in changed if h["shares_changed"] < 0], key=lambda h: h["shares_changed"])[:5]
    total_mv = passive_mv + active_mv
    return {
        "available": True,
        "quarter": (data or {}).get("quarter"),
        "top_holders": holders[:top_n],
        "holder_count": len(rows),
        "net_share_change": net_add_shares,
        "net_direction": "accumulating" if net_add_shares > 0 else "reducing" if net_add_shares < 0 else "flat",
        "passive_value_usd": passive_mv,
        "active_value_usd": active_mv,
        "passive_share_of_reported_pct": round(passive_mv / total_mv * 100.0, 1) if total_mv else None,
        "notable_adds": [{"manager": h["manager"], "shares_changed": h["shares_changed"]} for h in top_adds],
        "notable_reduces": [{"manager": h["manager"], "shares_changed": h["shares_changed"]} for h in top_reduces],
    }


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None
