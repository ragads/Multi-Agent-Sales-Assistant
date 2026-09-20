"""Central configuration. Fails loudly at boot if anything required is missing."""
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), extra="ignore")

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        # The project's .env wins over machine-wide environment variables, so a stale key left in
        # Windows' user environment can never silently override the one in .env.
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    # LLM + Embeddings - both on OpenAI, one key covers both (cheap: gpt-4o-mini + text-embedding-3-small)
    OPENAI_API_KEY: str
    OPENAI_CHAT_MODEL: str = "gpt-4o-mini"
    # The guardrail reviews every reply before it is sent, and gpt-4o-mini reads "block ONLY for X"
    # as an invitation to find an X. It blocked a completed booking three times running, under a
    # different category each time the previous one was withdrawn. Review is the one call where a
    # false positive costs a conversation, so it gets the stronger model.
    GUARDRAIL_MODEL: str = "gpt-4o"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMS: int = 1536

    # Supabase
    # There is no SUPABASE_SERVICE_KEY here on purpose. The service-role key grants
    # unrestricted, RLS-bypassing access to every table, and nothing in this app ever
    # needed it: all database work goes over Postgres directly. See DECISIONS.md.
    SUPABASE_URL: str
    # Runtime connection, as the least-privilege `closefuture_app` role. That role is not
    # the owner of any table, so every statement it runs is filtered by the policies in
    # sql/004_rls_policies.sql. Must be the session-mode pooler (port 5432): the booking
    # flow holds a pg_advisory_lock across statements and needs a pinned connection.
    SUPABASE_DB_URL: str
    # Owner connection. Used only by `python -m app.rag.ingest`, which rewrites the
    # knowledge base, and by the SQL in sql/. Never used to serve a request.
    SUPABASE_ADMIN_DB_URL: str = ""

    # Google Calendar
    GOOGLE_CALENDAR_ID: str
    # Personal Gmail calendars need OAuth (a service account cannot invite attendees or add Meet).
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REFRESH_TOKEN: str = ""
    GOOGLE_IMPERSONATE_USER: str = ""       # Workspace domain-wide delegation alternative
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

    # Access control (see app/security.py)
    APP_SECRET: str = ""            # signs conversation links; falls back to a DB-password hash
    ADMIN_TOKEN: str = ""           # required for /api/session/{id}/end and /api/mcp/reload; blank = disabled
    ALLOWED_ORIGINS: str = ""       # extra comma-separated origins allowed to call the API (your website)
    RATE_VISITOR_PER_MIN: int = 8
    RATE_VISITOR_PER_DAY: int = 120
    RATE_IP_PER_MIN: int = 30
    RATE_GLOBAL_PER_DAY: int = 1500  # hard stop on total chat turns - protects the OpenAI budget
    # /booking/* is reached with a signed link, not a visitor key. The token bounds who may call
    # it; this bounds how often, since each call reaches the Google Calendar API.
    RATE_BOOKING_PER_MIN: int = 20

    # Retrieval tuning (see DECISIONS.md)
    CHUNK_CHARS: int = 700
    CHUNK_OVERLAP: int = 120
    TOP_K: int = 6
    # 0.35 threw away the right chunk for the most common sales question there is. "How much does it
    # cost" ranks the pricing chunk first at 0.268 and "pricing" at 0.278 - correct every time, but
    # both under the old floor, so the bot answered "I don't have that" while the rates sat in the
    # corpus. Only robotic phrasing ("hourly rate", 0.485) cleared it. No floor separates answerable
    # from unanswerable anyway: a question we should decline scores 0.318, higher than the pricing
    # question we should answer. Deciding that needs to read the text, which is the answer model's
    # job and it does it well - it declines on retrieved-but-irrelevant chunks. So the floor is set
    # to admit real matches and only exclude noise (an off-topic question scores ~0.10).
    MIN_SIMILARITY: float = 0.25
    CONFIDENCE_FLOOR: float = 0.45
    # Raised from 0.60: the classifier scored "Can you help with the thing for my app?" above the
    # old floor and answered it instead of asking what they meant, so FR-3.6 never fired in
    # testing. This is a tuning knob - lower it again if the assistant starts over-clarifying.
    INTENT_CONFIDENCE_FLOOR: float = 0.72

    @property
    def cors_origins(self) -> list[str]:
        base = [self.APP_BASE_URL.rstrip("/"), "http://localhost:8000", "http://127.0.0.1:8000"]
        extra = [o.strip().rstrip("/") for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]
        return list(dict.fromkeys(base + extra))

    @property
    def business_start(self) -> int:
        return int(self.BUSINESS_HOURS.split("-")[0].split(":")[0])

    @property
    def business_end(self) -> int:
        return int(self.BUSINESS_HOURS.split("-")[1].split(":")[0])


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
