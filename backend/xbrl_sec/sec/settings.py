"""Runtime settings for the new XBRL data layer."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    schema: str
    project_root: Path
    market_data_root: Path
    # Raw-data storage root for high-volume downloads.
    # Defaults to D:\market_data now that D: is the canonical raw data drive.
    eightk_root: Path
    batch_size: int = 1000
    # Phase 3 of the M:1 refactor: route US/JP standardizers through the shared
    # m1_aggregation resolver. Default off — flip to "1" / "true" to enable.
    use_m1_resolver: bool = False
    # US companyfacts incremental refresh: floor lookback (days) used when no
    # watermark is available, and overlap added behind the watermark to catch
    # late-published daily-index entries / amendments.
    us_companyfacts_lookback_days: int = 14
    us_daily_index_overlap_days: int = 5
    # Upper bound on the daily-index catch-up scan (days) so a stale/empty watermark
    # can't trigger scanning years of index files; beyond this, run a --force refresh.
    us_max_lookback_days: int = 120
    # JP EDINET unavailable-document policy: documents older than the retention
    # window, or that hit this many consecutive logical-404s, are marked terminal
    # ('jp_unavailable') and dropped from the pending set.
    jp_retention_years: int = 5
    jp_max_404_retries: int = 3
    news_reasoning_backend: str = "qwen_ollama"
    news_ollama_url: str = "http://localhost:11434"
    news_qwen_model: str = "qwen2.5:7b-instruct"
    news_deepseek_model: str = "deepseek-chat"
    news_finbert_model: str = "ProsusAI/finbert"
    news_fetch_timeout_seconds: int = 10
    news_ingest_interval_seconds: int = 900


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    project_root = Path(os.environ.get("MZQA_ROOT", r"C:\Users\Bastian Offermann\Desktop\MZQA"))
    market_data_root = Path(os.environ.get("XBRL_SEC_MARKET_DATA_ROOT", r"D:\market_data"))
    eightk_root = Path(os.environ.get(
        "XBRL_SEC_EIGHTK_ROOT",
        r"D:\market_data\us_sec\eightk",
    ))
    database_url = os.environ.get("XBRL_SEC_DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/xbrl_sec")
    return Settings(
        database_url=database_url,
        schema=os.environ.get("XBRL_SEC_SCHEMA", "sec"),
        project_root=project_root,
        market_data_root=market_data_root,
        eightk_root=eightk_root,
        batch_size=int(os.environ.get("XBRL_SEC_BATCH_SIZE", "1000")),
        use_m1_resolver=_flag("XBRL_SEC_USE_M1_RESOLVER", default=False),
        us_companyfacts_lookback_days=int(os.environ.get("XBRL_SEC_US_LOOKBACK_DAYS", "14")),
        us_daily_index_overlap_days=int(os.environ.get("XBRL_SEC_US_INDEX_OVERLAP_DAYS", "5")),
        jp_retention_years=int(os.environ.get("XBRL_SEC_JP_RETENTION_YEARS", "5")),
        jp_max_404_retries=int(os.environ.get("XBRL_SEC_JP_MAX_404_RETRIES", "3")),
        news_reasoning_backend=os.environ.get("MZQA_NEWS_REASONING_BACKEND", "qwen_ollama"),
        news_ollama_url=os.environ.get("MZQA_NEWS_OLLAMA_URL", "http://localhost:11434"),
        news_qwen_model=os.environ.get("MZQA_NEWS_QWEN_MODEL", "qwen2.5:7b-instruct"),
        news_deepseek_model=os.environ.get("MZQA_NEWS_DEEPSEEK_MODEL", "deepseek-chat"),
        news_finbert_model=os.environ.get("MZQA_NEWS_FINBERT_MODEL", "ProsusAI/finbert"),
        news_fetch_timeout_seconds=int(os.environ.get("MZQA_NEWS_FETCH_TIMEOUT_SECONDS", "10")),
        news_ingest_interval_seconds=int(os.environ.get("MZQA_NEWS_INGEST_INTERVAL_SECONDS", "900")),
    )
