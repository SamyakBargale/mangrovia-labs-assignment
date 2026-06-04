from __future__ import annotations

import asyncio
import logging
import sys

import db
from config import settings
from telegram_handler import build_application


def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    )


def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

    # db.init_db() is async (aiosqlite); run it synchronously before the bot starts.
    asyncio.run(db.init_db())
    logger.info("Database initialised at %s", settings.DATABASE_PATH)

    app = build_application()

    mode = "DEMO" if settings.DEMO_MODE else f"AI ({settings.ANTHROPIC_MODEL})"
    logger.info(
        "Bot starting — chat=%s mode=%s",
        settings.TELEGRAM_CHAT_ID,
        mode,
    )

    if settings.WEBHOOK_URL:
        # ── Webhook mode (production) ────────────────────────────────────────
        # Requires a public HTTPS URL. Telegram pushes updates to:
        #   {WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}
        # Supported ports: 443, 80, 88, 8443
        logger.info(
            "Starting webhook listener on port %s → %s",
            settings.WEBHOOK_PORT,
            settings.WEBHOOK_URL,
        )
        app.run_webhook(
            listen="0.0.0.0",
            port=settings.WEBHOOK_PORT,
            url_path=settings.TELEGRAM_BOT_TOKEN,
            webhook_url=f"{settings.WEBHOOK_URL}/{settings.TELEGRAM_BOT_TOKEN}",
            secret_token=settings.WEBHOOK_SECRET_TOKEN or None,
            allowed_updates=["message"],
        )
    else:
        # ── Long-polling mode (local development) ────────────────────────────
        # python-telegram-bot installs its own SIGINT/SIGTERM handlers inside
        # run_polling() and performs a clean event-loop shutdown. We do NOT
        # install conflicting signal handlers here.
        logger.info("Starting long-polling")
        app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
