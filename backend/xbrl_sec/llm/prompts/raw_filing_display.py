"""Prompt for company-specific display specs over raw filing statement rows."""
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate


RAW_FILING_DISPLAY_PROMPT_VERSION = "raw_filing_display_v1"


RAW_FILING_DISPLAY_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            (
                "You curate compact financial statement display layouts over fixed XBRL filing rows. "
                "You must not invent numeric values. You only choose labels, hierarchy, visibility, "
                "aggregation mode, and source row bindings from the provided input packet. "
                "Prefer company-specific clarity over global standardization. "
                "Default display should be lean: root/level-1 rows only, with level-2 details marked as detail. "
                "Use source_node_keys exactly as supplied. Section rows may have no sources and aggregation='none'. "
                "For direct rows use one source. For subtotal rows use sum or subtract over listed sources."
            ),
        ),
        (
            "human",
            (
                "Build a user-friendly display spec for this filing packet.\n\n"
                "Return all relevant statements contained in the packet. "
                "Keep labels short and plain English. Do not output values.\n\n"
                "{packet_json}"
            ),
        ),
    ]
)
