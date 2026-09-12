"""
Centralized application configuration.

Every environment variable the project reads used to be a scattered
`os.getenv(...)` call in db.py, storage.py, deepseek_parser.py, security.py,
and web_app.py, each with its own inline default and no validation - a
typo'd variable name silently becomes "not configured" instead of a
startup error, and nothing documents the full set of knobs in one place.

This module is the single source of truth: one `Settings` object, built
once at import time via `pydantic-settings`, validated at process start
(fails fast and loud instead of failing confusingly at request time), and
every other module imports `settings` from here instead of calling
`os.getenv` directly.

Existing env var names are preserved exactly (DB_PATH, DEEPSEEK_API_KEY,
R2_ACCOUNT_ID, etc.) so this is a drop-in change - no .env file needs to
change, only the code that reads it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- App / environment -------------------------------------------
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "console"

    # --- Server ---------------------------------------------------------
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    # Comma-separated in the env var; parsed into a list below. Tighten
    # this to your real frontend domain(s) before going to production -
    # "*" (the old hardcoded default) allows any site to call the API
    # from a browser, which is fine for local dev only.
    CORS_ORIGINS: str = "*"

    # --- Persistence ------------------------------------------------
    DB_PATH: str = "nl_to_cad.db"
    OUTPUT_DIR: str = "./output"

    # --- Rate limiting (slowapi / limits syntax, e.g. "20/minute") ---
    RATE_LIMIT_GENERATE: str = "20/minute"
    RATE_LIMIT_KEY_ISSUE: str = "5/hour"

    # --- DeepSeek parser ------------------------------------------------
    # Railway's actual service variables are named LLM_API_KEY/LLM_BASE_URL/
    # LLM_MODEL, not DEEPSEEK_*, leftover from an earlier provider-agnostic
    # naming convention (predates this file's DEEPSEEK_* names, which the
    # module docstring above claims are the "existing" names - they weren't,
    # in this deployment). Since case_sensitive=True and there's no fuzzy
    # matching, DEEPSEEK_API_KEY silently resolved to None the whole time,
    # no error, no warning, just a permanent silent fallback to the regex
    # parser. Accept both naming conventions instead of picking one and
    # requiring a Railway dashboard rename.
    DEEPSEEK_API_KEY: str | None = None
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_DEFAULT_MODEL: str = "deepseek-v4-flash"
    DEEPSEEK_TIMEOUT_SECONDS: float = 20.0
    LLM_API_KEY: str | None = None
    LLM_BASE_URL: str | None = None
    LLM_MODEL: str | None = None

    # --- Cloudflare R2 storage (optional; falls back to local disk) ----
    R2_ACCOUNT_ID: str | None = None
    R2_ACCESS_KEY_ID: str | None = None
    R2_SECRET_ACCESS_KEY: str | None = None
    R2_BUCKET_NAME: str | None = None
    R2_PUBLIC_URL: str | None = None
    R2_PRESIGNED_EXPIRY_SECONDS: int = 7 * 24 * 3600

    # --- OKX / x402 (a2mcp/) ------------------------------------------
    OKX_API_KEY: str | None = None
    OKX_SECRET_KEY: str | None = None
    OKX_PASSPHRASE: str | None = None
    PAY_TO_ADDRESS: str | None = None

    # --- BOT Chain native-BOT payments (botchain_pay.py, a2mcp_botchain/) --
    # Separate rail from OKX/x402 above - BOT Chain has no live AgentPay
    # protocol yet (still roadmap per BOT Chain's own materials as of this
    # writing), so this is a manual pay-to-treasury-then-prove-it-with-a-
    # tx-hash flow, not an SDK integration.
    #
    # BOTCHAIN_ENVIRONMENT picks which of the two chain configs below is
    # live - one switch controls both the frontend wallet prompt
    # (/config/chain) and the backend verifier (botchain_pay._w3()), so
    # they can't drift apart.
    BOTCHAIN_ENVIRONMENT: Literal["testnet", "mainnet"] = "testnet"
    TREASURY_ADDRESS: str | None = None

    # Testnet - chain id 968, confirmed against BOT Chain's testnet
    # explorer default (scan.bohr.life also appears as a fallback default
    # in ShieldGuard's chainVerify.js, an independent corroboration).
    BOTCHAIN_TESTNET_CHAIN_ID_HEX: str = "0x3c8"  # 968
    BOTCHAIN_TESTNET_RPC_URL: str = "https://rpc.bohr.life"
    BOTCHAIN_TESTNET_EXPLORER_URL: str = "https://scan.bohr.life"

    # Mainnet - chain id 677 / 0x2a5, confirmed via chainlist.org/chain/677
    # AND ShieldGuard's own live packages/backend/.env.example
    # (RPC_URL=https://rpc.botchain.ai, CHAIN_ID=677), not just one source.
    BOTCHAIN_MAINNET_CHAIN_ID_HEX: str = "0x2a5"  # 677
    BOTCHAIN_MAINNET_RPC_URL: str = "https://rpc.botchain.ai"
    BOTCHAIN_MAINNET_EXPLORER_URL: str = "https://scan.botchain.ai"

    BOTCHAIN_KEY_ISSUE_PRICE_BOT: float = 5.0
    BOTCHAIN_PER_CALL_PRICE_BOT: float = 0.2
    BOTCHAIN_CONFIRMATION_BLOCKS: int = 1

    # Free STL-only preview (/preview) - no wallet, no API key, no BOT
    # payment gating it, so it needs its own hard per-IP limit. A real
    # BOT payment is a natural throttle on /generate; nothing throttles
    # this route except this setting. Deliberately stricter than
    # RATE_LIMIT_GENERATE.
    RATE_LIMIT_PREVIEW: str = "5/hour"

    # --- On-chain design provenance (contracts/DesignRegistry.sol) --------
    # DESIGN_REGISTRY_ADDRESS is set AFTER deploying - see
    # scripts/deploy_design_registry.py. Unset means anchoring is
    # silently skipped (see design_registry.py) - a missing anchor never
    # blocks a paid export from being delivered.
    #
    # ANCHOR_WALLET_PRIVATE_KEY is a DIFFERENT wallet from
    # TREASURY_ADDRESS above. TREASURY_ADDRESS only ever *receives*
    # BOT from users - it has no private key on this server at all.
    # This one is the backend's own wallet: it SIGNS and PAYS GAS for
    # anchorDesign() calls, since anchoring is something the server
    # does on its own after a paid export, not something a user signs
    # in that moment. Needs its own small BOT balance for gas, funded
    # and held separately from the treasury.
    DESIGN_REGISTRY_ADDRESS: str | None = None
    ANCHOR_WALLET_PRIVATE_KEY: str | None = None

    # Railway auto-injects this at deploy time - no manual setting
    # needed there. Falls back to "unknown" for local runs. This is
    # what makes "same params + same templateVersion reproduces the
    # same hash" a checkable claim instead of an assumption - see
    # DesignRegistry.sol's own note on why it's required, not optional.
    @property
    def template_version(self) -> str:
        import os
        return os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown")

    @property
    def botchain_rpc_url(self) -> str:
        return self.BOTCHAIN_TESTNET_RPC_URL if self.BOTCHAIN_ENVIRONMENT == "testnet" else self.BOTCHAIN_MAINNET_RPC_URL

    @property
    def botchain_chain_id_hex(self) -> str:
        return self.BOTCHAIN_TESTNET_CHAIN_ID_HEX if self.BOTCHAIN_ENVIRONMENT == "testnet" else self.BOTCHAIN_MAINNET_CHAIN_ID_HEX

    @property
    def botchain_explorer_url(self) -> str:
        return self.BOTCHAIN_TESTNET_EXPLORER_URL if self.BOTCHAIN_ENVIRONMENT == "testnet" else self.BOTCHAIN_MAINNET_EXPLORER_URL

    @field_validator("LOG_LEVEL")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = v.upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v not in valid:
            raise ValueError(f"LOG_LEVEL must be one of {valid}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _fall_back_to_llm_env_names(self) -> "Settings":
        """DEEPSEEK_API_KEY takes priority if both are set. LLM_* is the
        fallback for deployments (like Railway here) that only defined the
        provider-agnostic names."""
        if self.DEEPSEEK_API_KEY is None and self.LLM_API_KEY is not None:
            self.DEEPSEEK_API_KEY = self.LLM_API_KEY
        if self.LLM_BASE_URL is not None:
            self.DEEPSEEK_BASE_URL = self.LLM_BASE_URL
        if self.LLM_MODEL is not None:
            self.DEEPSEEK_DEFAULT_MODEL = self.LLM_MODEL
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def r2_configured(self) -> bool:
        return bool(self.R2_ACCOUNT_ID and self.R2_ACCESS_KEY_ID and self.R2_SECRET_ACCESS_KEY)


@lru_cache
def get_settings() -> Settings:
    """Cached so `Settings()` (which reads the environment and .env file)
    only runs once per process; call `get_settings.cache_clear()` in tests
    that need to reload with different env vars."""
    return Settings()


settings = get_settings()
