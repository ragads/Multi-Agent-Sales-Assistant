"""Central configuration. Fails loudly at boot if anything required is missing."""
from datetime import time
from functools import lru_cache
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM + Embeddings - both on OpenAI, one key covers both (cheap: gpt-4o-mini + text-embedding-3-small)
    OPENAI_API_KEY: str = ""
    # Any OpenAI-compatible provider. Blank LLM_BASE_URL = OpenAI. Google Gemini:
    #   LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/  (key from Google AI Studio)
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""              # takes precedence over OPENAI_API_KEY when set
    LLM_TIMEOUT_S: float = 30.0        # per model call; a slower call is retried by our own retry policy
    OPENAI_CHAT_MODEL: str = "gpt-4o-mini"   # chat model name at whichever provider is configured
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMS: int = 1536

    # Supabase
    # The app talks to Postgres directly through SUPABASE_DB_URL; these two are informational only
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_DB_URL: str              # app role, restricted by row-level security to one visitor/session
    SUPABASE_ADMIN_DB_URL: str = ""   # cross-session background jobs + ingest; blank = use SUPABASE_DB_URL

    # Google Calendar
    GOOGLE_CALENDAR_ID: str
    GOOGLE_SERVICE_ACCOUNT_JSON: str
    CALENDAR_OWNER_TZ: str = "Asia/Kolkata"
    BUSINESS_HOURS: str = "10:00-18:00"
    SLOT_MINUTES: int = 30

    # Email
    RESEND_API_KEY: str
    EMAIL_FROM: str = "CloseFuture Bot <bot@closefuture.io>"
    SALES_INBOX: str

    # App
    APP_BASE_URL: str = "http://localhost:8000"
    SESSION_IDLE_TIMEOUT_MIN: int = 15
    SESSION_EXPIRY_DAYS: int = 30
    MCP_CALENDAR_URL: str = "http://localhost:8931/mcp"
    MCP_EMAIL_URL: str = "http://localhost:8932/mcp"
    LOG_LEVEL: str = "INFO"

    # MCP server auth - one bearer token per server, so a leaked email token cannot book meetings
    MCP_CALENDAR_TOKEN: str
    MCP_EMAIL_TOKEN: str
    MCP_BIND_HOST: str = "127.0.0.1"   # docker-compose overrides this to 0.0.0.0 on the internal network

    # Session lease lock (see DECISIONS.md, decision 6)
    SESSION_LOCK_TTL_S: float = 60.0
    SESSION_LOCK_WAIT_S: float = 45.0

    # Observability - Langfuse is optional; tracing switches on when both keys are set
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"
    # USD per 1M tokens, used for the cost figure in our own llm_call log rows (gpt-4o-mini list price)
    OPENAI_PRICE_IN_PER_M: float = 0.15
    OPENAI_PRICE_OUT_PER_M: float = 0.60

    # Retrieval tuning (see DECISIONS.md)
    CHUNK_CHARS: int = 700
    CHUNK_OVERLAP: int = 120
    TOP_K: int = 6
    MIN_SIMILARITY: float = 0.35
    CONFIDENCE_FLOOR: float = 0.45
    INTENT_CONFIDENCE_FLOOR: float = 0.60

    @field_validator("MCP_CALENDAR_TOKEN", "MCP_EMAIL_TOKEN")
    @classmethod
    def _strong_token(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("must be at least 32 characters - generate one with "
                             "python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
        return v

    @property
    def business_start(self) -> time:
        return time.fromisoformat(self.BUSINESS_HOURS.split("-")[0].strip())

    @property
    def business_end(self) -> time:
        return time.fromisoformat(self.BUSINESS_HOURS.split("-")[1].strip())

    @model_validator(mode="after")
    def _has_llm_key(self):
        if not (self.LLM_API_KEY or self.OPENAI_API_KEY):
            raise ValueError("set LLM_API_KEY (any OpenAI-compatible provider) or OPENAI_API_KEY")
        return self

    @property
    def llm_client_kwargs(self) -> dict:
        """Arguments for every AsyncOpenAI client in the app (chat, retrieval, ingest)."""
        # A bounded timeout and no SDK-level retries: app/reliability/retry.py already retries with
        # backoff, and the SDK defaults (600 s timeout, 2 hidden retries) let one overloaded model call
        # stall a visitor's turn for minutes.
        kw = {"api_key": self.LLM_API_KEY or self.OPENAI_API_KEY,
              "timeout": self.LLM_TIMEOUT_S, "max_retries": 0}
        if self.LLM_BASE_URL:
            kw["base_url"] = self.LLM_BASE_URL
        return kw

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.LANGFUSE_PUBLIC_KEY and self.LANGFUSE_SECRET_KEY)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # pragma: no cover
        raise SystemExit(
            f"\n[CONFIG ERROR] Missing or invalid environment variables.\n"
            f"Copy .env.example to .env and fill it in.\n\nDetail: {exc}\n"
        )


settings = get_settings()
