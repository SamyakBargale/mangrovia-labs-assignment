# BUGS.md

## Overview
This file documents the key bugs identified during the audit of the original prototype and how they were addressed in the subsequent development phases. The goal is to provide a clear, concise record for future maintainers.

## Fixed Bugs (Critical / High)

| ID | Severity | Description | Fix Implemented |
|----|----------|-------------|-----------------|
| B001 | Critical | Synchronous `anthropic.Anthropic` client blocked the event loop, causing the bot to become unresponsive. | Switched to `anthropic.AsyncAnthropic` and added retry logic. |
| B002 | Critical | Global `ConversationState` shared across all chats, leading to cross‑user contamination. | Replaced with a dictionary keyed by `SessionKey(chat_id, user_id, thread_id)`. |
| B003 | High | State transition to `negotiating` occurred before DB write and Telegram send, leaving sessions in an inconsistent state on failure. | State changes now commit only after LLM call, DB write, and message send succeed. |
| B004 | High | Unstructured JSON parsing caused crashes on any extra text or malformed responses. | Introduced `extract_json()` and strict Pydantic models with validators. |
| B005 | High | No persistence – all data lost on restart. | Implemented async SQLite schema with tables for requests, estimates, sessions, turns, and idempotency. |
| B006 | High | No rate limiting, allowing a single user to flood the bot and hit API limits. | Added `_within_rate_limit()` sliding‑window limiter (default 10 msgs/60 s). |
| B007 | High | Demo mode could not run without an Anthropic API key. | Added `DEMO_MODE` with heuristic pricing and negotiation logic. |
| B008 | High | Photo messages were ignored, missing valuable vehicle data. | Updated handler to download photos, store `photo_path`, and feed images to the LLM (or demo extraction). |

## Fixed Bugs (Medium / Low) 

| ID | Severity | Description | Fix Implemented |
|----|----------|-------------|-----------------|
| B009 | Medium | Duplicate seller message appeared in prompts. | Cleaned message aggregation before LLM calls. |
| B010 | Medium | Missing `/reset` and `/status` commands. | Implemented command handlers with proper session handling. |
| B011 | Medium | Idempotency not guaranteed – duplicate Telegram updates could rerun LLM calls. | Added `processed_messages` table with unique constraints. |
| B012 | Low | Hard‑coded file paths for temporary images. | Saved images under `photos/<chat_id>_<msg_id>.jpg`. |
| B013 | Low | README lacked clear setup steps. | Updated README with environment setup, demo mode, and tunnel instructions. |
| B014 | Low | No test coverage for database migrations. | Added migration helper `_add_column_if_missing` and tests. |

## Remaining Open Issues (Future Work)

- **Bug B015 (Medium)**: Proper handling of Telegram forum topics (thread IDs) in group chats. *Planned for Phase 4.*
- **Bug B016 (Low)**: Graceful fallback when image OCR fails in real multimodal mode. *To be added in Phase 5.*
- **Bug B017 (Medium)**: Improve error messages for LLM rate‑limit responses (e.g.,429). *Scheduled for Phase 6.*

---

*The above list reflects the major defects discovered during the audit and the concrete steps taken to resolve them. Minor stylistic or lint issues have been addressed as part of routine code clean‑up.*
