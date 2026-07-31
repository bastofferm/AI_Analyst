"""Phase 5 cutover helper: side-by-side legacy vs m1_aggregation diff.

Runs both resolvers on the same raw facts WITHOUT writing standardized
tables. Reports per-(entity, year, period, line_item) deltas plus a
summary by jurisdiction.

Usage::

    python -m xbrl_sec.sec.scripts.compare_resolver_outputs --jurisdiction US
    python -m xbrl_sec.sec.scripts.compare_resolver_outputs \\
        --jurisdiction JP --entity-ids E12345,E67890 --tolerance 0.001 \\
        --output diff.csv

Exit codes:
  0 — parity (no material deltas above tolerance).
  1 — material deltas detected. Inspect the CSV.
"""
from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal
from pathlib import Path

from xbrl_sec.sec.std import jp_standardize, us_standardize
from xbrl_sec.sec.std.versioned_mapping import load_mapping_exceptions, load_versioned_mapping


def _us_key(row: tuple) -> tuple:
    # Match the US row tuple positions from _resolve / _resolve_via_m1.
    # (cik, jurisdiction, fy, fp, period_end, line_item, metric_type, value, ...)
    return (row[0], row[2], row[3], row[5])


def _jp_key(row: tuple) -> tuple:
    return (row[0], row[2], row[3], row[5])


def _us_value(row: tuple) -> Decimal | None:
    return row[7]


def _jp_value(row: tuple) -> Decimal | None:
    return row[7]


def _us_metric(row: tuple) -> str:
    return row[6]


def _jp_metric(row: tuple) -> str:
    return row[6]


def compare_us(entity_ids: list[str] | None, tolerance: Decimal) -> tuple[list[dict], dict]:
    mapping = load_versioned_mapping("US")
    exceptions = load_mapping_exceptions("US")
    std_paths = us_standardize._load_std_paths()
    std_unit_types = us_standardize._load_unit_types()
    raw = us_standardize._raw_rows(entity_ids)
    legacy = us_standardize._resolve(raw, mapping, exceptions, std_paths, std_unit_types)
    new = us_standardize._resolve_via_m1(raw, mapping, exceptions, std_paths, std_unit_types)
    return _diff_rows("US", legacy, new, _us_key, _us_value, _us_metric, tolerance)


def compare_jp(entity_ids: list[str] | None, tolerance: Decimal) -> tuple[list[dict], dict]:
    mapping = load_versioned_mapping("JP")
    exceptions = load_mapping_exceptions("JP")
    std_paths = jp_standardize._load_std_paths()
    std_unit_types = jp_standardize._load_unit_types()
    raw = jp_standardize._raw_rows(entity_ids)
    legacy = jp_standardize._resolve(raw, mapping, exceptions, std_paths, std_unit_types)
    new = jp_standardize._resolve_via_m1(raw, mapping, exceptions, std_paths, std_unit_types)
    return _diff_rows("JP", legacy, new, _jp_key, _jp_value, _jp_metric, tolerance)


def _diff_rows(jurisdiction, legacy, new, key_fn, value_fn, metric_fn, tolerance):
    legacy_by_key = {key_fn(r): r for r in legacy}
    new_by_key = {key_fn(r): r for r in new}
    all_keys = sorted(set(legacy_by_key) | set(new_by_key))
    deltas: list[dict] = []
    counters = {
        "legacy_only": 0,
        "new_only": 0,
        "value_diff": 0,
        "metric_diff": 0,
        "matched": 0,
        "total_keys": len(all_keys),
    }
    for key in all_keys:
        lrow = legacy_by_key.get(key)
        nrow = new_by_key.get(key)
        if lrow is None and nrow is not None:
            counters["new_only"] += 1
            deltas.append({
                "jurisdiction": jurisdiction,
                "entity_id": key[0],
                "fiscal_year": key[1],
                "fiscal_period": key[2],
                "line_item_id": key[3],
                "delta_kind": "new_only",
                "legacy_value": "",
                "new_value": str(value_fn(nrow)) if value_fn(nrow) is not None else "",
                "legacy_metric": "",
                "new_metric": metric_fn(nrow),
            })
            continue
        if nrow is None and lrow is not None:
            counters["legacy_only"] += 1
            deltas.append({
                "jurisdiction": jurisdiction,
                "entity_id": key[0],
                "fiscal_year": key[1],
                "fiscal_period": key[2],
                "line_item_id": key[3],
                "delta_kind": "legacy_only",
                "legacy_value": str(value_fn(lrow)) if value_fn(lrow) is not None else "",
                "new_value": "",
                "legacy_metric": metric_fn(lrow),
                "new_metric": "",
            })
            continue
        lv = value_fn(lrow)
        nv = value_fn(nrow)
        diff_kind = None
        if lv is None and nv is None:
            pass
        elif lv is None or nv is None:
            diff_kind = "value_diff"
        else:
            abs_diff = abs(Decimal(str(lv)) - Decimal(str(nv)))
            base = max(abs(Decimal(str(lv))), abs(Decimal(str(nv))), Decimal("1"))
            if abs_diff / base > tolerance:
                diff_kind = "value_diff"
        if diff_kind is None and metric_fn(lrow) != metric_fn(nrow):
            diff_kind = "metric_diff"
        if diff_kind is None:
            counters["matched"] += 1
            continue
        counters[diff_kind] += 1
        deltas.append({
            "jurisdiction": jurisdiction,
            "entity_id": key[0],
            "fiscal_year": key[1],
            "fiscal_period": key[2],
            "line_item_id": key[3],
            "delta_kind": diff_kind,
            "legacy_value": str(lv) if lv is not None else "",
            "new_value": str(nv) if nv is not None else "",
            "legacy_metric": metric_fn(lrow),
            "new_metric": metric_fn(nrow),
        })
    return deltas, counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jurisdiction", choices=("US", "JP", "BOTH"), default="BOTH")
    parser.add_argument("--entity-ids", default="")
    parser.add_argument("--tolerance", type=float, default=0.0001,
                        help="Relative tolerance for value diffs. Default 0.01%%.")
    parser.add_argument("--output", default="resolver_diff.csv",
                        help="CSV path for the per-row diff report.")
    args = parser.parse_args()
    tolerance = Decimal(str(args.tolerance))
    entity_ids = [x.strip() for x in args.entity_ids.split(",") if x.strip()] or None

    all_deltas: list[dict] = []
    summary: dict[str, dict] = {}

    if args.jurisdiction in ("US", "BOTH"):
        print("Diffing US...")
        deltas, counters = compare_us(entity_ids, tolerance)
        all_deltas.extend(deltas)
        summary["US"] = counters
        print(f"  US: {counters}")
    if args.jurisdiction in ("JP", "BOTH"):
        print("Diffing JP...")
        deltas, counters = compare_jp(entity_ids, tolerance)
        all_deltas.extend(deltas)
        summary["JP"] = counters
        print(f"  JP: {counters}")

    out_path = Path(args.output)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "jurisdiction", "entity_id", "fiscal_year", "fiscal_period",
                "line_item_id", "delta_kind",
                "legacy_value", "new_value", "legacy_metric", "new_metric",
            ],
        )
        writer.writeheader()
        writer.writerows(all_deltas)
    print(f"Wrote {len(all_deltas)} delta rows to {out_path}")

    material = any(
        counters["legacy_only"] + counters["new_only"] + counters["value_diff"] > 0
        for counters in summary.values()
    )
    return 1 if material else 0


if __name__ == "__main__":
    sys.exit(main())
