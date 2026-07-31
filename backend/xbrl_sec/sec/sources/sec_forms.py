"""US SEC form policy for the core fundamentals + corporate-action pipelines."""
from __future__ import annotations


CORE_ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})
CORE_QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})
CORE_FUNDAMENTAL_FORMS = CORE_ANNUAL_FORMS | CORE_QUARTERLY_FORMS
FORM_ALIASES = {
    "10K": "10-K",
    "10K/A": "10-K/A",
    "10Q": "10-Q",
    "10Q/A": "10-Q/A",
}

# Form 8-K = "current report" — material events filed within 4 business days.
# Includes Item 5.03 (Amendments to Articles → splits) and Item 8.01 (Other Events).
CORPORATE_ACTION_FORMS = frozenset({"8-K", "8-K/A"})

# 8-K item codes of particular interest for corporate-action parsing.
SPLIT_RELEVANT_ITEMS = frozenset({"5.03", "8.01"})


def normalize_form(value: object) -> str:
    text = str(value or "").strip().upper()
    return FORM_ALIASES.get(text, text)


def is_core_fundamental_form(value: object) -> bool:
    return normalize_form(value) in CORE_FUNDAMENTAL_FORMS


def is_corporate_action_form(value: object) -> bool:
    return normalize_form(value) in CORPORATE_ACTION_FORMS
