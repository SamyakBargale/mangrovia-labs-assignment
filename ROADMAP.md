# Roadmap

Audit date: 2026-06-04

Goal: make the AI car negotiator reliably usable through Telegram, then evolve it from prototype to production-ready service.

## Phase 0: Make local setup reproducible

Priority: immediate

Why: the repo should run for a new developer before deeper fixes can be trusted.

Tasks:

1. Add `.env.example` with all required settings and safe placeholder values.
2. Add a `.gitignore` exception for `.env.example` so the documented template can actually be tracked.
3. Declare Python support explicitly, preferably Python 3.12.
4. Update setup docs to use the supported interpreter instead of ambiguous `python3`.
5. Add a smoke-check command that imports modules, initializes the DB, and builds the Telegram app without real network calls.
6. Decide whether `DATABASE_PATH=:memory:` is supported. If yes, add a shared test connection. If no, document file-backed SQLite for local/test use.

Acceptance checks:

- `cp .env.example .env` works.
- A new virtualenv can install `requirements.txt`.
- `python -m compileall -q .` passes.
- A dummy-env import smoke test passes.
- DB helper tests pass against a temporary SQLite file.

## Phase 1: Fix core Telegram conversation correctness

Priority: immediate

Why: these issues directly affect seller conversations and can leave the bot stuck or inconsistent.

Tasks:

1. Fix awaiting-info persistence so the estimate links to the full combined description, not only the latest message.
2. Remove the duplicated latest seller message from negotiation prompts.
3. Reorder state transitions so `phase="negotiating"` is set only after the opening message is generated, persisted, and sent successfully.
4. Add user-facing error replies for failed LLM calls, parse failures, DB errors, and Telegram send failures.
5. Stage negotiation turns and commit history only after the model response is valid and the reply is delivered.
6. Add `/reset`, `/status`, `/start`, and `/help`.
7. Guard `message.from_user` and define behavior for anonymous or channel-origin messages.
8. Add idempotency using Telegram update/message IDs.
9. Add phase/session timeouts so stale negotiations do not capture unrelated future messages.

Acceptance checks:

- Vague listing -> clarifying question -> extra info -> estimate uses the full description.
- The latest seller message appears only once in the model prompt.
- Failed opening generation does not move the session into negotiating.
- Failed negotiation generation does not duplicate or corrupt history.
- `/reset` returns the session to idle.
- Duplicate Telegram updates do not trigger duplicate LLM calls or replies.

## Phase 2: Make LLM outputs structured and safe

Priority: high

Why: the current code trusts model text for pricing and negotiation actions that have business consequences.

Tasks:

1. Replace free-form JSON parsing with strict structured output, preferably Anthropic tool use or a single centralized parser with repair/retry.
2. Add Pydantic validators for `PricerResult` and `NegotiationTurn`.
3. Require estimated results to include positive `low_price`, `high_price`, and `currency`.
4. Require `low_price < high_price`.
5. Require insufficient-info results to include a clarifying question.
6. Return structured negotiation data: message, status, offered amount, currency, and rationale for internal logs.
7. Programmatically reject any buyer offer at or above `high_price`.
8. Programmatically verify the opening offer is below the low estimate.
9. Programmatically reject messages that reveal internal estimates.
10. Validate outgoing Telegram text is non-empty and within length limits, splitting if needed.
11. Improve the pricer prompt so it estimates ranges when minimum required fields are present instead of demanding impossible certainty.
12. Add adversarial prompt-injection tests.

Acceptance checks:

- Malformed model output is retried or fails gracefully.
- `estimated` with missing prices is rejected.
- An over-ceiling offer is never sent to Telegram.
- A prompt-injection listing cannot make the bot reveal its internal range.

## Phase 3: Persist sessions and support concurrent conversations

Priority: high

Why: the current singleton state cannot support real Telegram usage beyond one fragile conversation.

Tasks:

1. Add `sessions` table keyed by chat ID, thread ID, and user ID or chosen business identity.
2. Add `negotiation_turns` table for all seller and buyer turns.
3. Add final status, agreed price, walkaway reason, and timestamps.
4. Hydrate active sessions on startup.
5. Add per-session async locks or a queue to serialize state transitions.
6. Enable SQLite foreign keys on every connection.
7. Close SQLite connections explicitly after each operation.
8. Add DB migrations.
9. Add state-machine unit tests.
10. Add transcript summarization or truncation before full histories exceed LLM context limits.

Acceptance checks:

- Two users or threads can negotiate independently.
- Restarting the bot preserves active negotiations.
- Foreign-key violations fail in tests.
- Session transitions are deterministic under concurrent message simulation.

## Phase 4: Telegram integration hardening

Priority: medium

Why: the bot needs predictable behavior in real chats and deploy environments.

Tasks:

1. Keep polling for local development.
2. Add webhook mode for production with HTTPS, secret token validation, and health checks.
3. Add operator-only commands for diagnostics.
4. Support Telegram forum topics if group usage requires it.
5. Document BotFather privacy-mode requirements for group chats.
6. Decide whether group messages require a mention, reply, command, or active session owner before the bot acts.
7. Support captioned photo posts by normalizing `message.text` and `message.caption`.
8. Add typing indicators or "working on it" messages for slow LLM calls.
9. Add clear chat authorization rules beyond a single `TELEGRAM_CHAT_ID` if multiple chats are needed.
10. Record Telegram delivery metadata after successful sends.

Acceptance checks:

- Local polling mode still works with BotFather token and chat ID.
- Webhook mode can receive a Telegram update in a deployed environment.
- Unauthorized chats get ignored or receive a configured denial response.
- Slow requests give users visible feedback.

## Phase 5: Improve pricing quality and negotiation features

Priority: medium

Why: an LLM-only pricing agent is not reliable enough for serious vehicle negotiation.

Tasks:

1. Define minimum required listing fields: make, model, year, mileage, location/market, fuel/transmission, condition, accident history, service history, asking price.
2. Integrate market data or a deterministic pricing heuristic.
3. Store pricing source, confidence, and comparable listings where available.
4. Add configurable negotiation strategy: target discount, maximum rounds, initial offer ratio, walkaway style, tone, and locale.
5. Add deal summaries and handoff notes.
6. Add human-review mode for uncertain or high-value vehicles.

Acceptance checks:

- Price estimates cite structured inputs and confidence.
- Negotiation never exceeds configured max rounds without a terminal status.
- Final summaries include vehicle, latest seller ask, buyer offer, and status.

## Phase 6: Production readiness

Priority: after correctness and persistence

Why: production readiness depends on first having correct, testable behavior.

Tasks:

1. Add pytest coverage for DB, state machine, Telegram handler behavior, and LLM parser behavior.
2. Add ruff and a type checker.
3. Add CI.
4. Add structured logging with session IDs.
5. Add metrics: LLM latency, parse failures, retries, Telegram sends, terminal negotiation statuses, and DB errors.
6. Add secrets management guidance.
7. Add backup/restore plan for SQLite or move to Postgres if multi-instance deployment is required.
8. Add deployment docs and runbooks.

Acceptance checks:

- CI blocks regressions.
- Operators can answer: is the bot healthy, which sessions are active, what failed, and what changed recently?
- A new developer can run tests and start the bot from docs alone.

## Suggested first implementation sprint

1. Add `.env.example`, Python version declaration, and setup doc fixes.
2. Fix `.gitignore` so `.env.example` is tracked.
3. Add pytest plus smoke tests for config, DB, and app construction.
4. Fix awaiting-info persistence, duplicate seller prompts, and unsafe state transition ordering.
5. Close SQLite connections explicitly and enable foreign keys.
6. Add Pydantic validators and parser failure handling.
7. Add `/reset`, session timeout, and user-facing error messages.
8. Add idempotency for Telegram messages.
9. Update `bugs.md` with the bugs fixed and any remaining tradeoffs.

This sprint should turn the repo from a fragile prototype into a runnable, debuggable Telegram demo. Later phases can then build toward multi-session production behavior.
