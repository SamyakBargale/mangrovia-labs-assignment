# AI Car Negotiator

A Telegram bot — with a Telegram Mini App dashboard — that reads a used-car listing, estimates a realistic market price range, and negotiates as a polite buyer. Supports a zero-cost demo mode and a real Anthropic-powered mode.

---

## What It Does

- **Chat interface**: accepts text listings, photo captions, and car photos (including Italian *libretto di circolazione*) in one Telegram chat.
- **AI pricing**: calls Anthropic Claude to estimate a realistic low/high market range, or uses a local heuristic in demo mode.
- **Photo ingestion**: uploads are base64-encoded and sent to the multimodal Claude model, which extracts make, model, year, engine, fuel type, Euro class, and mileage from registration documents or car photos.
- **Smart negotiation**: enforces price-safety rules in code — the bot never exceeds its internal high estimate regardless of what the model says.
- **Session management**: per-user/chat/thread state persisted in SQLite; sessions survive between exchanges and expire after configurable inactivity.
- **Telegram Mini App**: a glassmorphic, dark-mode dashboard that opens natively inside Telegram, featuring drag-and-drop uploads, a real-time price slider gauge, live chat log, and spec badges extracted from the listing text.
- **Commands**: `/start`, `/help`, `/status`, `/reset`, `/cancel`.
- **Tests**: 79 unit and integration tests that run with no Telegram or Anthropic network calls.

---

## Architecture

```
main.py                   ← entry point: init DB, start bot polling / webhook
telegram_handler.py       ← message & command handlers, rate limiting, photo download
agents/
  pricer.py               ← price estimator (Anthropic multimodal or demo heuristic)
  negotiator.py           ← negotiation turn generator (Anthropic or demo)
conversation.py           ← in-memory session keyed by (chat_id, user_id, thread_id)
db.py                     ← async SQLite persistence (aiosqlite)
config.py                 ← settings via pydantic-settings / .env
dashboard/
  server.py               ← FastAPI server serving the Mini App and REST API
  templates/index.html    ← Telegram Mini App UI (single-page app)
  static/style.css        ← premium dark glassmorphic stylesheet
  static/app.js           ← client-side SPA controller
tests/
  test_agents.py          ← pricer + negotiator unit tests
  test_db.py              ← SQLite persistence tests
  test_telegram_handler.py← Telegram handler integration tests
  test_dashboard.py       ← FastAPI dashboard endpoint tests
```

---

## Setup

**Requires Python 3.12 or newer.**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`. At minimum set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

For a free first test, keep `DEMO_MODE=true`.  
For real AI pricing and negotiation:

```env
DEMO_MODE=false
ANTHROPIC_API_KEY=your_key_here
ANTHROPIC_MODEL=claude-haiku-4-5
```

---

## Telegram Setup

1. Message `@BotFather` → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Add the bot to your chat or message it directly.
3. Find the chat ID:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
4. Copy the `chat.id` value into `TELEGRAM_CHAT_ID`.

**Privacy mode in groups**: to let the bot read every message, disable privacy mode in BotFather. If you prefer privacy mode on, set:

```env
GROUP_TRIGGER_MODE=reply_or_mention
BOT_USERNAME=your_bot_username
```

---

## Running the Bot

```bash
source .venv/bin/activate
python main.py
```

Send a test listing:

```
2018 BMW 320d, 95k km, manual, good service history, asking EUR 18,000
```

You can also send a **photo** of the car or an Italian registration certificate (*libretto di circolazione*) — the bot extracts specs automatically.

---

## Telegram Mini App Dashboard

The bot ships with a local FastAPI web server that powers a Telegram Mini App. The dashboard shows all your negotiations, a price slider gauge, real-time chat, and drag-and-drop photo upload.

### 1. Start the dashboard server

```bash
source .venv/bin/activate
python dashboard/server.py
```

The server starts on `http://localhost:8000`.

### 2. Expose it via HTTPS (required by Telegram)

Use any SSH-based tunnel — no install needed on macOS:

```bash
ssh -R 80:localhost:8000 nokey@localhost.run
```

Copy the `https://xxxx.lhr.life` URL it prints.

Alternatively, install `ngrok` and run `ngrok http 8000`.

### 3. Set `WEBAPP_URL` in `.env`

```env
WEBAPP_URL=https://xxxx.lhr.life/
```

Restart `python main.py`. Now `/start` shows a **🚀 Open Negotiator App** button and a persistent **Car Negotiator** menu button in the chat — both open the dashboard directly inside Telegram.

> **Local browser fallback**: if `WEBAPP_URL` is `http://`, the bot automatically falls back to a regular browser link — no crash.

---

## Commands

| Command | Effect |
|---------|--------|
| `/start` | Welcome message + Mini App button |
| `/help`  | Full command list + Mini App button |
| `/status` | Current session phase and price range |
| `/reset` | Cancel active negotiation |
| `/cancel` | Alias for `/reset` |

---

## Tests

```bash
source .venv/bin/activate
pytest tests/
```

All 79 tests run offline — no Telegram or Anthropic calls.

---

## Project Files

| File | Purpose |
|------|---------|
| `README.md` | This file |
| `APPROACH.md` | Full design and implementation narrative |
| `BUGS.md` | Bug audit — found, fixed, and remaining |
| `ROADMAP.md` | Longer-term feature and architecture ideas |
| `.env.example` | Template for all environment variables |
| `Dockerfile` | Container build (polling mode) |
