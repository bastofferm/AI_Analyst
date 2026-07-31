"""Prompt-Registry für Pipeline-LLM-Calls.

Jeder Prompt ist ein ChatPromptTemplate, versioniert über den Modulnamen.
Die Outputs sind streng typisiert (siehe xbrl_sec.llm.schemas).
"""
from __future__ import annotations

from xbrl_sec.llm.prompts.etf_provider_classification import (
    ETF_PROVIDER_CLASSIFICATION_PROMPT,
)

__all__ = ["ETF_PROVIDER_CLASSIFICATION_PROMPT"]
