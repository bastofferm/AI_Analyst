from ai_analyst import llm_runtime as analyst_llm_runtime
from api.ai import llm_runtime as api_llm_runtime


def test_parse_json_response_strips_fence_and_preface():
    text = 'Before:\n```json\n{"response_markdown": "ok", "warnings": []}\n```\nAfter'

    parsed = api_llm_runtime.parse_json_response(text)

    assert parsed["response_markdown"] == "ok"
    assert parsed["warnings"] == []


def test_parse_json_response_repairs_literal_newlines_in_string():
    text = '{"response_markdown": "# Title\n\nLine two", "warnings": []}'

    parsed = analyst_llm_runtime.parse_json_response(text)

    assert parsed["response_markdown"] == "# Title\n\nLine two"
    assert parsed["warnings"] == []
