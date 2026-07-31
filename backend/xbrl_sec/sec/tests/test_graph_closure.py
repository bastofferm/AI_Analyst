from decimal import Decimal

from xbrl_sec.sec.std.graph_closure import _Edge, close_graph


def test_pre_top_down_residual_prevents_partial_subtotal_identity_break():
    edges = [
        _Edge("total_assets", "total_current_assets", 1, "corp", "US_GAAP", 1),
        _Edge("total_assets", "total_noncurrent_assets", 1, "corp", "US_GAAP", 2),
        _Edge("total_noncurrent_assets", "property_plant_equipment_net", 1, "corp", "US_GAAP", 1),
        _Edge("total_noncurrent_assets", "right_of_use_assets", 1, "corp", "US_GAAP", 2),
        _Edge("total_noncurrent_assets", "goodwill", 1, "corp", "US_GAAP", 3),
        _Edge("total_noncurrent_assets", "intangible_assets_net", 1, "corp", "US_GAAP", 4),
        _Edge("total_noncurrent_assets", "equity_method_investments", 1, "corp", "US_GAAP", 5),
        _Edge("total_noncurrent_assets", "deferred_tax_assets", 1, "corp", "US_GAAP", 6),
        _Edge("total_noncurrent_assets", "restricted_cash", 1, "corp", "US_GAAP", 7),
        _Edge("total_noncurrent_assets", "long_term_investments", 1, "corp", "US_GAAP", 8),
        _Edge(
            "total_noncurrent_assets",
            "other_noncurrent_assets",
            1,
            "corp",
            "US_GAAP",
            9,
            "catch_all",
            "residual",
        ),
    ]
    identity_checks = [
        {
            "check_id": "assets_current_plus_noncurrent",
            "lhs_item_id": "total_assets",
            "rhs_item_ids": ["total_current_assets", "total_noncurrent_assets"],
            "rhs_signs": [1, 1],
            "tolerance_bp": 1,
            "sector_scope": "corp",
            "accounting_standard": "US_GAAP",
        }
    ]
    filed = {
        "total_assets": Decimal("50143000000"),
        "total_current_assets": Decimal("25754000000"),
        "property_plant_equipment_net": Decimal("4690000000"),
        "right_of_use_assets": Decimal("735000000"),
        "goodwill": Decimal("11358000000"),
        "intangible_assets_net": Decimal("1148000000"),
        "equity_method_investments": Decimal("163000000"),
        "deferred_tax_assets": Decimal("743000000"),
        "restricted_cash": Decimal("2323000000"),
    }

    derived, violations = close_graph(filed, edges, identity_checks, "corp", "US_GAAP")

    assert derived["total_noncurrent_assets"] == (Decimal("24389000000"), "RESIDUAL")
    assert derived["other_noncurrent_assets"] == (Decimal("3229000000"), "RESIDUAL")
    assert violations == []
