from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    database_url: str = Field(default="postgresql://postgres@127.0.0.1:5432/xbrl_sec")
    db_schema: str = Field(default="sec")
    allowed_origins: str = Field(default="http://localhost:3027,http://127.0.0.1:3027")
    log_level: str = Field(default="INFO")
    statement_timeout_ms: int = Field(default=120000)
    pool_min: int = Field(default=2)
    pool_max: int = Field(default=10)
    use_statement_assembler: bool = Field(default=False, alias="MZQA_USE_STATEMENT_ASSEMBLER")

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
