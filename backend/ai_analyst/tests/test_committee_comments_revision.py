from ai_analyst import committee_comments, committee_revision


def test_specialist_comments_prefer_structured_verdicts():
    state = {
        "config": {
            "extra_analysts": [
                {
                    "key": "growth_extrapolator",
                    "name": "Growth Extrapolator",
                    "origin": "specialist",
                    "focus": "growth durability",
                }
            ]
        },
        "specialist_verdicts": [
            {
                "analyst_key": "growth_extrapolator",
                "analyst": "Growth Extrapolator",
                "thesis": "Growth is more durable than the base case assumes. Margins still need monitoring.",
                "risk_flags": ["Cloud deceleration"],
                "confidence": 0.72,
            }
        ],
        "committee_chat_history": [
            {"role": "growth_extrapolator", "content": "Fallback prose should not replace structured output."}
        ],
    }

    comments = committee_comments.build_specialist_comments(state)

    assert comments[0]["analyst_key"] == "growth_extrapolator"
    assert comments[0]["focus"] == "growth durability"
    assert comments[0]["bullets"][0] == "Growth is more durable than the base case assumes."
    assert comments[0]["confidence"] == 0.72


def test_specialist_comments_fall_back_to_history():
    state = {
        "committee_chat_history": [
            {"role": "advocate", "content": "Core advocate."},
            {"role": "macro_regime_strategist", "content": "- Rates are a multiple headwind.\n- USD strength remains a risk."},
        ]
    }

    comments = committee_comments.build_specialist_comments(state)

    assert len(comments) == 1
    assert comments[0]["analyst"] == "Macro Regime Strategist"
    assert comments[0]["bullets"] == ["Rates are a multiple headwind.", "USD strength remains a risk."]


def test_compact_current_result_removes_report_html_and_caps_evidence():
    result = {
        "ticker": "MSFT",
        "report_html": "<html>large</html>",
        "memo": {"en": "word " * 2000, "de": "kurz"},
        "evidence_bundle": {
            "counts": {"mda": 1},
            "warnings": [],
            "cards": [{"card_id": f"ev-{i:016x}", "kind": "mda", "title": "T", "summary": "s" * 400} for i in range(20)],
        },
    }

    compact = committee_revision.compact_current_result(result)

    assert "report_html" not in compact
    assert len(compact["evidence_bundle"]["cards"]) == 12
    assert compact["memo"]["en"].endswith("...")


def test_no_key_iteration_response_is_non_throwing():
    response = committee_revision.no_key_iteration_response(
        2,
        "Please revisit margins.",
        prompt_template_id="valuation_sensitivity",
        prompt_template_label="Valuation sensitivity",
    )

    assert response["iteration_number"] == 2
    assert response["iteration_status"] == "fallback"
    assert response["received_user_comment"] == "Please revisit margins."
    assert response["prompt_template_id"] == "valuation_sensitivity"
    assert response["warnings"]
    assert "Please revisit margins." in response["response_markdown"]


def test_iteration_fallback_preserves_comment_and_metadata():
    response = committee_revision.iteration_fallback_response(
        3,
        "Challenge data quality.",
        "Revision model call failed: parse error",
        prompt_template_label="Data quality challenge",
    )

    assert response["iteration_number"] == 3
    assert response["iteration_status"] == "fallback"
    assert response["received_user_comment"] == "Challenge data quality."
    assert response["prompt_template_label"] == "Data quality challenge"
    assert response["warnings"] == ["Revision model call failed: parse error"]
