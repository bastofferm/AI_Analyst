"""Prompt-Template für die ETF-Provider-Klassifizierung.

Wird vom Klassifizierer in tools/classify_unknown_etf_providers_deepseek.py
benutzt; Output-Schema: ProviderClassificationBatch.
"""
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

SYSTEM_PROMPT = """You classify European-listed ETF funds into ETF providers.
Return JSON only that matches the provided schema.
Existing providers are context, not a whitelist.
If the ETF is clearly issued by a provider not yet present, create a new provider_id and provider_label.
Use unknown_provider only when the provider is genuinely not inferable from the fund name, short name, issuer, or fund family.
Prefer visible issuer/brand prefixes in full_name or short_name.
provider_id must be lowercase snake_case, stable, and brand-based."""


USER_TEMPLATE = """Existing provider master data (context only):
{master_providers}

Funds to classify (one decision per ISIN):
{funds}

Output JSON shape:
{format_instructions}
"""


ETF_PROVIDER_CLASSIFICATION_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_PROMPT),
        ("user", USER_TEMPLATE),
    ]
)
