# Approach — AI Car Negotiator

This document explains every design decision, problem solved, and implementation detail across the full life of the project, from the initial prototype audit through to the final Telegram Mini App dashboard.

---

## 1. Starting Point — What the Prototype Looked Like

The original codebase was a minimal proof-of-concept with:

- A single Telegram message handler that read `message.text` and passed it to the Anthropic Claude API.
- A pricing prompt that asked the LLM to return a JSON object with a price range.
- A negotiation prompt that continued the turn-based dialogue.
- One global `ConversationState` object shared by all messages in all chats.
- In-memory state only — everything was lost on restart.
- No tests, no commands, no error handling, no session management.

The audit (`BUGS.md`) catalogued 32 bugs across critical, high, medium, and low severity.

---

## 2. Phase 1 — Stabilisation and Core Bug Fixes

### 2a. Async LLM Client

Both agents were using the **synchronous** `anthropic.Anthropic` client inside `async` handlers. Every LLM call blocked the entire Telegram event loop. Switched both to `anthropic.AsyncAnthropic` and added `_call_with_retry()` — a shared helper that retries up to three times with exponential backoff on transient errors.

### 2b. Structured JSON Contracts

The original parsers stripped markdown fences with a regex and called `json.loads`. Any preamble, trailing prose, or malformed response crashed the handler.

Replaced with `extract_json()` — a brace-bounded extractor that finds the first `{` and last `}` in the response text. Both agents now return Pydantic models (`PricerResult`, `NegotiationTurn`) with strict `@model_validator` and `@field_validator` rules:

- `status = "estimated"` requires non-null, positive `low_price`, `high_price`, `currency`, and `low_price < high_price`.
- `status = "insufficient_info"` requires a non-null `clarifying_question`.
- `currency` must be a three-letter ISO code.
- All numeric price fields are clamped in code before being sent to Telegram.

### 2c. Price-Safety Enforcement in Code

The README claimed the bot "never offers above the high estimate." The original code only stated this in prompts. Added programmatic extraction of `offered_price` from the structured negotiation JSON, and validation that the offer is within bounds before any Telegram send occurs.

### 2d. Session Keying and Persistence

Replaced the global singleton with a `dict` keyed by `SessionKey(chat_id, user_id, thread_id)`. This allows parallel negotiations from different users, different group threads, and the web dashboard — all isolated from each other.

Added a full SQLite schema:
- `car_requests` — every incoming listing, with raw text and optional photo path.
- `estimates` — every pricing result, linked to the request.
- `negotiations` — the opening offer record.
- `sessions` — one row per conversation, tracking `phase`, `description`, `estimate_id`, `status`, and timestamps.
- `negotiation_turns` — every buyer and seller turn, with speaker, text, `model_status`, `offered_price`, `currency`, and `created_at`.
- `processed_messages` — idempotency guard with `UNIQUE(chat_id, message_id)`.

### 2e. State Transition Safety

The original code transitioned to `negotiating` *before* calling `write_opening_message()` and *before* sending the Telegram reply. Any failure left the user's session in a broken state.

New rule: **state transitions only commit after the LLM call succeeds, the DB write succeeds, and the Telegram send succeeds.** On failure the session stays in its prior phase and the user receives a clear error message with a reset prompt.

Similarly, seller turns are staged locally and only appended to `state.history` after the negotiation turn is returned, persisted, and sent — preventing the LLM from seeing the same seller message twice.

### 2f. Commands and Rate Limiting

Added `/start`, `/help`, `/status`, `/reset`, and `/cancel` with per-chat/user/thread scope. Added `_within_rate_limit()` — a sliding-window counter per user that defaults to 10 messages per 60 seconds and can be disabled by setting `RATE_LIMIT_MESSAGES=0`.

### 2g. Demo Mode

Added `DEMO_MODE=true` (the default). In demo mode, `_demo_estimate()` uses a local heuristic based on brand segment, age, and mileage — no Anthropic API key required. `_demo_negotiation_turn()` uses a simple script with configurable raise/drop steps. The full Telegram flow can be tested completely offline and for free.

---

## 3. Phase 2 — Photo and Registration Document Ingestion

### The problem

Car buyers and sellers often share photos of the car or its registration document (in Italy: *libretto di circolazione* / *carta di circolazione*) rather than typing out specs. The original bot ignored all photo messages.

### What was added

**Telegram handler (`telegram_handler.py`):**
- Updated message filter to `filters.TEXT | filters.CAPTION | filters.PHOTO`.
- `_message_text()` returns `message.text or message.caption`, normalising both paths.
- When a photo is present, the bot downloads the highest-resolution version via `photo[-1].get_file()` and saves it to `photos/<chat_id>_<message_id>.jpg`.

**Pricer agent (`agents/pricer.py`):**
- `estimate_price(description, photo_path=None)` — if `photo_path` is provided, reads the image, base64-encodes it, and builds a multimodal `content` list for the Anthropic API (image block + text block).
- System prompt extended: if a photo is present, Claude should parse the Italian *libretto* sections (B = first registration date, D = make/model, E = VIN, P.1/P.2 = engine displacement/power, P.5/P.3 = fuel type, V.9 = Euro class) and return an `extracted_description` string.
- `PricerResult` gained an optional `extracted_description` field. If populated, the handler overwrites `state.description` so all downstream negotiation steps know the real vehicle details.
- Demo mode simulates this: if a `photo_path` is provided but the text description is vague, it substitutes a fixed realistic description so the full demo flow can be exercised.

**Database (`db.py`):**
- Added `photo_path TEXT` column to `car_requests` via self-healing migration (`_add_column_if_missing`) so existing databases are upgraded automatically on next startup.

---

## 4. Phase 3 — Telegram Mini App Dashboard

### Why a Mini App?

The user wanted a visual interface — drag-and-drop photo uploads, a price slider gauge, a real-time chat log — but **within Telegram**, not a separate website that requires switching apps. Telegram Mini Apps open as a native webview sheet inside the Telegram client, giving a premium feel while keeping the user in the same context.

### Architecture

```text
Telegram Client
    └── Mini App webview (opens the FastAPI server URL)
            ├── GET  /api/sessions?user_id=X    → list this user's negotiations
            ├── POST /api/sessions/new           → start negotiation (text or photo)
            ├── GET  /api/sessions/{id}/turns    → full message history
            ├── POST /api/sessions/{id}/message  → send seller reply, get counter-offer
            ├── POST /api/sessions/{id}/clarify  → provide clarifying details
            └── POST /api/sessions/{id}/reset    → cancel session
FastAPI (dashboard/server.py)
    └── Shares the same SQLite DB and Python agents as the Telegram bot
```

### backend — `dashboard/server.py`

A FastAPI application that:
- Serves the HTML/CSS/JS files via `StaticFiles` and `HTMLResponse`.
- Accepts `user_id` and `chat_id` from the Mini App (populated from `Telegram.WebApp.initDataUnsafe.user`) so sessions are correctly scoped to the Telegram user.
- Resets any previously active session before creating a new one, to avoid violating the `UNIQUE` active-session index.
- Hydrates `ConversationState` objects from the database on demand (no shared in-memory state with the Telegram bot process — both read/write the same SQLite file).
- Handles multipart file uploads for photo drag-and-drop.

### frontend — `index.html` + `style.css` + `app.js`

**Design system** (dark mode, glassmorphism):
- Base palette: obsidian `#090a0f` background, `#111219` surface panels.
- Accent colours: electric blue `#2563eb`, glowing cyan `#06b6d4`, success emerald `#10b981`.
- Cards use `backdrop-filter: blur(12px)` + semi-transparent borders for glassmorphic depth.
- Typography: Outfit (headings) + Inter (body) from Google Fonts.
- Micro-animations: pulsing typing dots, glowing spinner with a bouncing car icon, hover scale transitions on session list items.

**SPA layout:**
- Desktop: persistent sidebar (session list) + main content pane side by side.
- Mobile: single-pane view with class-toggle navigation (`show-sidebar` / `show-chat` / `show-new`).

**Price gauge widget:**
- Visualises the estimated `low_price` to `high_price` range as a gradient fill bar.
- A circular pin marker is positioned along the track based on the latest offered price.
- Pin colour changes: green (offer below low estimate — great deal), blue (offer in fair range), red (offer above high estimate — too expensive).

**Spec badge extraction:**
- `extractSpecs(text)` runs heuristic regex patterns on the description to extract make, year, mileage, transmission, and fuel type, then renders them as glowing pill badges in the chat header.

**Telegram WebApp SDK integration:**
- `Telegram.WebApp.ready()` and `.expand()` are called on load to maximise the view and signal readiness.
- `WebApp.initDataUnsafe.user.id` and `.chat.id` are read to scope API calls to the correct user.
- Falls back to a hardcoded development user ID when running outside Telegram (browser dev mode).

---

## 5. Testing Strategy

All 79 tests run completely offline with no Telegram or Anthropic network calls.

| File | Coverage |
|------|----------|
| `tests/test_agents.py` | Pricer demo heuristic, insufficient-info flow, photo mock extraction, negotiator opening/counter/terminal statuses, JSON parsing, validation errors |
| `tests/test_db.py` | Connection management, foreign key enforcement, idempotency, session lifecycle, turn persistence |
| `tests/test_telegram_handler.py` | Full message flow (idle → awaiting_info → negotiating → deal/walkaway), rate limiting, session expiry, photo handler, command handlers |
| `tests/test_dashboard.py` | Session listing with user filtering, session creation from text, active-session reset on new session |

Tests use `tempfile.TemporaryDirectory` for isolated SQLite databases per test class, `unittest.mock.AsyncMock` for all Telegram bot methods, and `fastapi.testclient.TestClient` for dashboard API endpoints.

---

## 6. File Map

```
.
├── README.md                ← setup, run, commands, Mini App guide
├── APPROACH.md              ← this file — design and implementation narrative
├── BUGS.md                  ← structured bug audit with status markers
├── ROADMAP.md               ← longer-term product and architecture ideas
├── PRODUCT_IDEAS.md         ← feature brainstorm
├── .env.example             ← all environment variables with comments
├── .gitignore               ← excludes .venv, .env, __pycache__, *.db, .DS_Store
├── Dockerfile               ← container build for polling mode
├── pyproject.toml           ← Python ≥3.10, pytest config
├── requirements.txt         ← pinned runtime + test dependencies
├── config.py                ← pydantic-settings Settings class
├── main.py                  ← entry point: init DB → build app → polling/webhook
├── telegram_handler.py      ← message + command handlers
├── conversation.py          ← in-memory session state (dict-keyed)
├── db.py                    ← async SQLite layer (aiosqlite)
├── agents/
│   ├── __init__.py
│   ├── pricer.py            ← price estimator (multimodal Anthropic or demo)
│   └── negotiator.py        ← negotiation turn generator (Anthropic or demo)
├── dashboard/
│   ├── server.py            ← FastAPI server (REST API + static file serving)
│   ├── templates/
│   │   └── index.html       ← Telegram Mini App SPA
│   └── static/
│       ├── style.css        ← dark glassmorphic stylesheet
│       └── app.js           ← SPA controller (state, API calls, gauge, drag-drop)
├── tests/
│   ├── __init__.py
│   ├── test_agents.py
│   ├── test_db.py
│   ├── test_telegram_handler.py
│   └── test_dashboard.py
└── photos/                  ← downloaded car/libretto images (gitignored)
```"
