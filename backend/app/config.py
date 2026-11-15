"""Settings, read once from the environment (and `.env` in development)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    env: str = "development"
    database_url: str = "postgresql+asyncpg://codepilot:codepilot@localhost:5434/codepilot"
    redis_url: str = "redis://localhost:6380/0"
    cors_origins: str = "http://localhost:5173"
    # When set, every request except the health check needs `Authorization: Bearer <token>`.
    api_token: str = ""
    # Run the task worker inside the API process (one process to deploy) or separately.
    run_worker_in_api: bool = True
    worker_concurrency: int = 2

    # --- model providers -------------------------------------------------------------------
    llm_provider: str = "gemini"  # gemini | openai_compat
    gemini_api_key: str = ""
    # Tried in order; a model that is rate-limited or down hands over to the next one.
    llm_models: str = "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"
    openai_compat_base_url: str = ""
    openai_compat_api_key: str = ""
    llm_timeout_seconds: float = 120.0
    llm_max_attempts_per_model: int = 2

    # --- sandbox ---------------------------------------------------------------------------
    sandbox_image: str = "codepilot-sandbox:latest"
    sandbox_network: str = "codepilot-sandbox"
    egress_proxy_url: str = "http://egress-proxy:3128"
    sandbox_runtime: str = ""  # e.g. "runsc" to run sandboxes under gVisor
    sandbox_memory_mb: int = 1024
    sandbox_cpus: float = 1.0
    sandbox_pids: int = 256
    sandbox_tmp_mb: int = 256
    sandbox_workspace_mb: int = 512
    sandbox_file_mb: int = 64
    command_timeout_seconds: int = 120
    install_timeout_seconds: int = 300
    test_timeout_seconds: int = 300
    sandbox_ttl_seconds: int = 3600
    upload_max_mb: int = 20

    # --- execution budget per task -----------------------------------------------------------
    max_llm_calls: int = 90
    max_total_tokens: int = 2_000_000
    max_tool_calls: int = 250
    max_wall_seconds: int = 1800
    max_plan_revisions: int = 3
    max_debug_iterations: int = 3
    max_review_rounds: int = 1
    planner_max_steps: int = 14
    coder_max_steps: int = 40
    executor_max_steps: int = 10
    debugger_max_steps: int = 18
    reviewer_max_steps: int = 12

    log_json: bool = Field(default=False)

    @property
    def model_chain(self) -> list[str]:
        return [m.strip() for m in self.llm_models.split(",") if m.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
