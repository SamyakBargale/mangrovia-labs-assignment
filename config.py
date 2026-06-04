from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Required ────────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: int

    # ── Anthropic ───────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-haiku-4-5"

    # ── Demo / cost-free mode ───────────────────────────────────────────────────
    # When true the bot uses local heuristics instead of calling Anthropic.
    # Perfect for local testing — no API key required.
    DEMO_MODE: bool = True

    # ── Database ────────────────────────────────────────────────────────────────
    DATABASE_PATH: str = "./bot.db"

    # ── Logging ─────────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    # ── Telegram group trigger behaviour ────────────────────────────────────────
    # "all"              — respond to every non-command message in the chat
    # "reply_or_mention" — respond only when someone replies to the bot or @mentions it
    GROUP_TRIGGER_MODE: str = "all"

    # Bot username (without @) used for mention detection in reply_or_mention mode
    BOT_USERNAME: str = ""

    # ── Session management ───────────────────────────────────────────────────────
    # Minutes of inactivity before an active session is considered stale and reset
    SESSION_TIMEOUT_MINUTES: int = 60

    # ── Negotiation strategy ─────────────────────────────────────────────────────
    # Opening offer as a fraction of the low-end estimate (e.g. 0.85 = 15% below low)
    INITIAL_OFFER_RATIO: float = 0.85

    # Maximum number of transcript turns sent to the LLM per negotiation step
    # (older turns are summarised with a placeholder to stay within context limits)
    MAX_HISTORY_TURNS: int = 20

    # Default currency when the listing does not specify one
    DEFAULT_CURRENCY: str = "EUR"

    # ── Webhook deployment (leave blank to use polling) ──────────────────────────
    # Public HTTPS base URL of your server, e.g. https://bot.example.com
    # Leave empty to run in long-polling mode (default for local development).
    WEBHOOK_URL: str = ""

    # WebApp URL (used by Telegram menu/inline buttons)
    # For local development, this defaults to http://localhost:8000/
    WEBAPP_URL: str = "http://localhost:8000/"

    # Port for the built-in webhook server (Telegram only allows 443, 80, 88, 8443)
    WEBHOOK_PORT: int = 8443


    # Optional secret token sent by Telegram with each webhook request.
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    WEBHOOK_SECRET_TOKEN: str = ""

    # ── Rate limiting ─────────────────────────────────────────────────────────────
    # Maximum number of messages a single user may send per window.
    # Set to 0 to disable rate limiting.
    RATE_LIMIT_MESSAGES: int = 10

    # Duration of the rate-limit window in seconds.
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = Settings()
