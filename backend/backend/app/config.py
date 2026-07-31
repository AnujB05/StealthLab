"""
Central configuration (MVP plan, Section 12).

All secret/environment access routes through this object rather than
scattered os.environ[] calls -- one seam to change when secrets move from
env vars to a dedicated manager.

Secrets are Optional with None defaults so importing any module for tests
or offline work doesn't require a populated .env. Call settings.require()
at the point of actual use instead, which fails loudly with a useful
message rather than an opaque None.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: Optional[str] = None

    # Heterogeneous debate panel (Section 7): three distinct model families.
    anthropic_api_key: Optional[str] = None
    fireworks_api_key: Optional[str] = None   # Kimi K3
    openai_api_key: Optional[str] = None      # third seat

    # Judge (Section 8.1 / Nirnaya) must be independent of the panel
    # (enforce_independence checks model family). The panel already uses
    # all three of the above families, so the judge needs a fourth,
    # distinct provider -- Gemini via its OpenAI-compatible endpoint.
    google_api_key: Optional[str] = None

    voyage_api_key: Optional[str] = None

    # Model IDs, overridable without touching code.
    anthropic_model: str = "claude-sonnet-4-6"
    fireworks_model: str = "accounts/fireworks/models/kimi-k3"
    openai_model: str = "gpt-4.1"
    gemini_model: str = "gemini-2.5-pro"
    embedding_model: str = "voyage-3-large"
    embedding_dimension: int = 1024  # must match VECTOR(n) in 01_ontology.sql

    # Debate parameters (Section 7).
    max_debate_rounds: int = 5
    min_supporters_for_eval: int = 2

    # Layer 1 eval gate (Section 8.1).
    groundedness_threshold: float = 0.5

    # Single-tenant placeholder (Section 12 auth seam).
    default_tenant_id: str = "00000000-0000-0000-0000-000000000001"

    def require(self, field: str) -> str:
        value = getattr(self, field, None)
        if not value:
            raise RuntimeError(
                f"Missing required setting '{field}'. Set {field.upper()} in your .env "
                f"(see .env.example)."
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
