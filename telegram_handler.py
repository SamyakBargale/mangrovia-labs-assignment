"""
Telegram message and command handlers.

Architecture notes:
- All DB calls are awaited (aiosqlite is non-blocking).
- State mutations happen after successful LLM calls and Telegram sends to keep
  in-memory state consistent with what the user actually saw.
- The seller's latest message is NOT appended to state.history before the LLM
  call — it is passed separately so it appears exactly once in the prompt.
- Rate limiting is enforced per user_id with a sliding window counter.
- Typing indicators are sent before every LLM call; failures are swallowed
  (best-effort — never block on them).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from telegram import Message, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


import conversation
import db
from agents.negotiator import continue_negotiation, deal_summary, write_opening_message
from agents.pricer import estimate_price
from config import settings
from conversation import ConversationState, SessionKey, Turn

logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 3900

# ---------------------------------------------------------------------------
# Per-user rate limiting (in-memory sliding window)
# ---------------------------------------------------------------------------

# user_id -> list of UTC timestamps (as float) of recent messages
_rate_window: dict[int, list[float]] = defaultdict(list)


def _within_rate_limit(user_id: int) -> bool:
    """
    Return True if the user has not exceeded RATE_LIMIT_MESSAGES within the
    last RATE_LIMIT_WINDOW_SECONDS. Returns True unconditionally when
    RATE_LIMIT_MESSAGES == 0 (rate limiting disabled).
    """
    if settings.RATE_LIMIT_MESSAGES == 0:
        return True
    now = datetime.now(timezone.utc).timestamp()
    window_start = now - settings.RATE_LIMIT_WINDOW_SECONDS
    # Evict expired timestamps and check the count
    recent = [t for t in _rate_window[user_id] if t > window_start]
    _rate_window[user_id] = recent
    if len(recent) >= settings.RATE_LIMIT_MESSAGES:
        return False
    _rate_window[user_id].append(now)
    return True


# ---------------------------------------------------------------------------
# Message normalization helpers
# ---------------------------------------------------------------------------

def _message_text(message: Message) -> str | None:
    """Return the text or caption from a message, stripped of whitespace, or None."""
    text = message.text or message.caption
    if text is None:
        return None
    text = text.strip()
    return text or None


def _thread_id(message: Message) -> int | None:
    return getattr(message, "message_thread_id", None)


def _user_id(message: Message) -> int:
    """Return the Telegram user ID, or 0 for anonymous/channel-origin messages."""
    return message.from_user.id if message.from_user else 0


def _session_key(message: Message) -> SessionKey:
    return SessionKey(chat_id=message.chat_id, user_id=_user_id(message), thread_id=_thread_id(message))


def _authorized_chat(message: Message) -> bool:
    return message.chat_id == settings.TELEGRAM_CHAT_ID


def _triggered_for_group(message: Message, text: str) -> bool:
    """
    Decide whether the bot should respond to this message.

    GROUP_TRIGGER_MODE="all"             — respond to every non-command message.
    GROUP_TRIGGER_MODE="reply_or_mention"— respond only to replies to the bot or @mentions.
    """
    if settings.GROUP_TRIGGER_MODE == "all":
        return True
    if message.reply_to_message is not None:
        return True
    if settings.BOT_USERNAME and f"@{settings.BOT_USERNAME.lower()}" in text.lower():
        return True
    return False


# ---------------------------------------------------------------------------
# Telegram reply helpers
# ---------------------------------------------------------------------------

async def _reply_text(message: Message, text: str) -> None:
    """Send a reply, chunking at TELEGRAM_TEXT_LIMIT and replacing blank text with a fallback."""
    cleaned = text.strip()
    if not cleaned:
        cleaned = "I hit a blank response. Please try again."
    for start in range(0, len(cleaned), TELEGRAM_TEXT_LIMIT):
        await message.reply_text(cleaned[start : start + TELEGRAM_TEXT_LIMIT])


async def _send_typing(message: Message) -> None:
    """Send a 'typing…' chat action as a best-effort UX hint during slow LLM calls."""
    try:
        await message.get_bot().send_chat_action(
            chat_id=message.chat_id, action=ChatAction.TYPING
        )
    except Exception:
        pass  # Never block processing on a cosmetic indicator


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

async def _ensure_session(state: ConversationState) -> int:
    if state.session_id is None:
        state.session_id = await db.get_or_create_session(
            chat_id=state.key.chat_id,
            user_id=state.key.user_id,
            thread_id=state.key.thread_id,
            phase=state.phase,
            description=state.description,
        )
    return state.session_id


# ---------------------------------------------------------------------------
# Core flow handlers
# ---------------------------------------------------------------------------

async def _run_pricer(message: Message, text: str, state: ConversationState, photo_path: str | None = None) -> None:
    """
    Price the accumulated description and either ask for clarification or
    send an opening negotiation offer.

    State transition to 'negotiating' happens ONLY after the opening message
    is successfully generated, persisted, and sent. A failure at any earlier
    step leaves the phase in idle/awaiting_info so the user can retry.
    """
    await _send_typing(message)

    session_id = await _ensure_session(state)

    estimate = await estimate_price(state.description, photo_path=photo_path)
    
    if estimate.extracted_description:
        state.description = estimate.extracted_description
        state.touch()

    car_request_id = await db.save_car_request(
        telegram_message_id=message.message_id,
        chat_id=message.chat_id,
        user_id=state.key.user_id,
        thread_id=state.key.thread_id,
        description=state.description,
        raw_text=text,
        photo_path=photo_path,
    )

    estimate_id = await db.save_estimate(
        car_request_id=car_request_id,
        status=estimate.status,
        low_price=estimate.low_price,
        high_price=estimate.high_price,
        currency=estimate.currency,
        reasoning=estimate.reasoning,
        raw_response=estimate.raw_response,
    )

    if estimate.status == "insufficient_info":
        question = estimate.clarifying_question or "Could you share a little more detail about the car?"
        await _reply_text(message, question)
        state.phase = "awaiting_info"
        state.estimate = None
        state.estimate_id = estimate_id
        state.touch()
        await db.update_session(session_id, state.phase, state.description, estimate_id)
        logger.info(
            "Clarifying question posted session=%s chat=%s user=%s",
            session_id, message.chat_id, state.key.user_id,
        )
        return

    await _send_typing(message)
    opening = await write_opening_message(state.description, estimate)
    await _reply_text(message, opening.message)

    await db.save_negotiation(
        estimate_id=estimate_id,
        opening_message=opening.message,
        opening_offer=opening.offer_price,
        currency=opening.currency,
    )
    await db.save_turn(
        session_id=session_id,
        speaker="buyer",
        text=opening.message,
        model_status="opening",
        offered_price=opening.offer_price,
        currency=opening.currency,
    )

    # Commit state transition only after successful send
    state.estimate = estimate
    state.estimate_id = estimate_id
    state.phase = "negotiating"
    state.history.append(
        Turn(
            speaker="buyer",
            text=opening.message,
            offered_price=opening.offer_price,
            currency=opening.currency,
        )
    )
    state.touch()
    await db.update_session(session_id, state.phase, state.description, estimate_id)
    logger.info(
        "Opening offer sent session=%s chat=%s user=%s offer=%s%s",
        session_id, message.chat_id, state.key.user_id,
        opening.currency, opening.offer_price,
    )


async def _run_negotiation_turn(message: Message, text: str, state: ConversationState) -> None:
    """
    Process a seller reply and generate the next buyer response.

    IMPORTANT: the seller's latest message is NOT in state.history when
    continue_negotiation() is called — it is appended only after a
    successful LLM call and send. This ensures the seller message appears
    exactly once in the LLM prompt (BUG-2 fix).
    """
    if state.estimate is None:
        await _reply_text(
            message,
            "I lost the price estimate for this negotiation, so I reset the session. "
            "Please send the listing again.",
        )
        conversation.reset(state)
        return

    await _send_typing(message)
    session_id = await _ensure_session(state)

    turn = await continue_negotiation(state, text)
    await _reply_text(message, turn.message)

    # Commit both turns to history and DB only after successful send
    state.history.append(Turn(speaker="seller", text=text))
    state.history.append(
        Turn(speaker="buyer", text=turn.message, offered_price=turn.offered_price, currency=turn.currency)
    )
    state.touch()

    await db.save_turn(
        session_id=session_id,
        speaker="seller",
        text=text,
        telegram_message_id=message.message_id,
    )
    await db.save_turn(
        session_id=session_id,
        speaker="buyer",
        text=turn.message,
        model_status=turn.status,
        offered_price=turn.offered_price,
        currency=turn.currency,
        raw_response=turn.raw_response,
    )
    logger.info(
        "Negotiation turn posted session=%s status=%s chat=%s user=%s",
        session_id, turn.status, message.chat_id, state.key.user_id,
    )

    if turn.status in ("deal_agreed", "walk_away"):
        summary = deal_summary(state, turn)
        await _reply_text(message, summary)
        await db.update_session(
            session_id, "idle", state.description, state.estimate_id, status=turn.status
        )
        conversation.reset(state)
        logger.info(
            "Negotiation ended session=%s result=%s chat=%s",
            session_id, turn.status, message.chat_id,
        )
    else:
        await db.update_session(session_id, state.phase, state.description, state.estimate_id)


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    text = _message_text(message)
    if text is None and not getattr(message, "photo", None):
        return

    text_to_process = text or ""

    if not _authorized_chat(message):
        return

    if not _triggered_for_group(message, text_to_process):
        return

    user_id = _user_id(message)

    # Rate limiting — protect against spam and runaway API costs
    if not _within_rate_limit(user_id):
        logger.warning(
            "Rate limit exceeded chat=%s user=%s", message.chat_id, user_id
        )
        await _reply_text(
            message,
            f"You're sending messages too fast. Please wait a moment and try again.",
        )
        return

    key = _session_key(message)
    state = conversation.get_state(key)

    # Session expiry check
    if state.phase != "idle" and state.expired(settings.SESSION_TIMEOUT_MINUTES):
        logger.info(
            "Session expired session=%s chat=%s user=%s",
            state.session_id, message.chat_id, user_id,
        )
        await _reply_text(
            message,
            "That negotiation expired, so I reset it. Send the listing again when you're ready.",
        )
        conversation.reset(state)
        return

    # Idempotency guard — skip duplicate Telegram updates
    if not await db.mark_message_processed(message.chat_id, message.message_id, update.update_id):
        logger.info(
            "Duplicate message skipped chat=%s message_id=%s", message.chat_id, message.message_id
        )
        return

    logger.info(
        "Message received phase=%s session=%s chat=%s user=%s text=%.80s",
        state.phase, state.session_id, message.chat_id, user_id, text_to_process,
    )

    photo_path = None
    if getattr(message, "photo", None):
        await _send_typing(message)
        try:
            photo_file = await message.photo[-1].get_file()
            import os
            os.makedirs("photos", exist_ok=True)
            photo_path = f"photos/{message.chat_id}_{message.message_id}.jpg"
            await photo_file.download_to_drive(custom_path=photo_path)
            logger.info("Downloaded photo to %s", photo_path)
        except Exception as exc:
            logger.error("Failed to download photo: %s", exc)

    try:
        if state.phase == "idle":
            state.description = text_to_process
            state.touch()
            await _run_pricer(message, text_to_process, state, photo_path=photo_path)

        elif state.phase == "awaiting_info":
            if text_to_process:
                state.description = f"{state.description}\n{text_to_process}"
            state.touch()
            await _run_pricer(message, text_to_process, state, photo_path=photo_path)

        elif state.phase == "negotiating":
            await _run_negotiation_turn(message, text_to_process, state)

    except Exception:
        logger.exception(
            "Error processing message phase=%s session=%s chat=%s user=%s",
            state.phase, state.session_id, message.chat_id, user_id,
        )
        await _reply_text(
            message,
            "Something went wrong while I was processing that. "
            "I reset this negotiation so you can try again.",
        )
        conversation.reset(state)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not _authorized_chat(message):
        return

    url_is_https = settings.WEBAPP_URL.startswith("https://")

    # Try setting the Menu Button to open the Web App (only allowed for HTTPS links)
    if url_is_https:
        try:
            await context.bot.set_chat_menu_button(
                chat_id=message.chat_id,
                menu_button=MenuButtonWebApp(
                    text="Car Negotiator",
                    web_app=WebAppInfo(url=settings.WEBAPP_URL)
                )
            )
        except Exception as exc:
            logger.error("Failed to set chat menu button: %s", exc)
    else:
        logger.warning(
            "Skipping WebApp Menu Button setting: WEBAPP_URL '%s' is not HTTPS. "
            "To test inside Telegram as a Mini App, configure an HTTPS URL (e.g. ngrok).",
            settings.WEBAPP_URL
        )

    # Use WebAppInfo only if URL is HTTPS, otherwise fallback to opening in browser
    if url_is_https:
        button = InlineKeyboardButton(
            text="🚀 Open Negotiator App",
            web_app=WebAppInfo(url=settings.WEBAPP_URL)
        )
    else:
        button = InlineKeyboardButton(
            text="🌐 Open Negotiator (Browser)",
            url=settings.WEBAPP_URL
        )

    keyboard = [[button]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_text(
        "👋 Welcome to CarBot!\n\n"
        "Send a used-car listing with make, model, year, mileage, condition, and asking price, or upload a photo of the car/Italian registration (libretto).\n\n"
        "Or, tap the button below to launch the interactive Negotiator App to upload photos and negotiate visually!",
        reply_markup=reply_markup,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not _authorized_chat(message):
        return

    url_is_https = settings.WEBAPP_URL.startswith("https://")

    if url_is_https:
        button = InlineKeyboardButton(
            text="🚀 Open Negotiator App",
            web_app=WebAppInfo(url=settings.WEBAPP_URL)
        )
    else:
        button = InlineKeyboardButton(
            text="🌐 Open Negotiator (Browser)",
            url=settings.WEBAPP_URL
        )

    keyboard = [[button]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_text(
        "Commands:\n"
        "• /start — welcome message and listing format\n"
        "• /help — show this help\n"
        "• /status — show current negotiation phase and details\n"
        "• /reset — reset the current session\n"
        "• /cancel — cancel and reset the active negotiation\n\n"
        "📸 You can send a photo with a captioned listing too.\n"
        "🔔 In group mode set to 'reply_or_mention', reply to the bot or @mention it.",
        reply_markup=reply_markup,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not _authorized_chat(message):
        return
    state = conversation.get_state(_session_key(message))
    detail = f"Current phase: *{state.phase}*."
    if state.phase == "awaiting_info":
        detail += "\nWaiting for more details about the car."
    elif state.phase == "negotiating":
        detail += (
            f"\nDescription: {len(state.description)} chars."
            f"\nTurns so far: {len(state.history)}."
        )
        if state.estimate and state.estimate.low_price and state.estimate.currency:
            detail += (
                f"\nInternal range: {state.estimate.currency} "
                f"{state.estimate.low_price:,.0f} – {state.estimate.high_price:,.0f}."
            )
    await _reply_text(message, detail)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not _authorized_chat(message):
        return
    state = conversation.get_state(_session_key(message))
    if state.session_id is not None:
        await db.update_session(
            state.session_id, "idle", state.description, state.estimate_id, status="reset"
        )
    conversation.reset(state)
    await _reply_text(message, "✅ Reset done. Send a fresh listing when you're ready.")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias for /reset — lets users type a more natural /cancel during a negotiation."""
    await reset_command(update, context)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def build_application() -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION | filters.PHOTO) & ~filters.COMMAND, handle_message)
    )
    return app
