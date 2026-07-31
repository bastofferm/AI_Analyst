from ai_analyst import mda_guidance


def test_clamp_score_bounds_and_invalid_values():
    assert mda_guidance.clamp_score("1.4") == 1.0
    assert mda_guidance.clamp_score("-2") == -1.0
    assert mda_guidance.clamp_score("0.3219") == 0.322
    assert mda_guidance.clamp_score("nope") is None


def test_peer_rank_and_percentile():
    rank, percentile = mda_guidance.peer_rank_and_percentile(0.4, [-0.1, 0.2, 0.4, 0.8])

    assert rank == 2
    assert percentile == 62.5


def test_analysis_from_llm_response():
    result = mda_guidance.analysis_from_llm_response(
        ticker="MSFT",
        jurisdiction="US",
        peer_tickers=["AAPL", "GOOGL"],
        mda_text="Management highlighted cloud demand.",
        llm_data={
            "companies": [
                {
                    "ticker": "MSFT",
                    "tone_score": 0.45,
                    "guidance": "positive",
                    "summary": "Management emphasizes durable cloud growth.",
                    "buzzword_headlines": ["Cloud demand remains durable", "AI capacity investment continues"],
                    "risk_flags": ["Capacity spend may pressure margins"],
                },
                {"ticker": "AAPL", "tone_score": 0.1, "guidance": "neutral"},
                {"ticker": "GOOGL", "tone_score": 0.6, "guidance": "positive"},
            ]
        },
    )

    assert result["tone_score"] == 0.45
    assert result["guidance"] == "positive"
    assert result["peer_rank"] == 2
    assert result["peer_percentile"] == 50.0
    assert result["buzzword_headlines"][0] == "Cloud demand remains durable"


def test_no_key_analysis_warns_without_failing():
    result = mda_guidance.no_key_analysis("MSFT", "US", "Some MD&A text", 10)

    assert result["tone_score"] is None
    assert result["peer_count"] == 10
    assert result["warnings"]
