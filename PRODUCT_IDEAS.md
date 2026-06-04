# Product Enhancement Ideas

## Near-Term Polish

1. Conversation summary: after `deal_agreed` or `walk_away`, send a compact summary with vehicle, last seller ask, final buyer offer, and status.
2. Strategy presets: conservative, balanced, aggressive, and quick-close negotiation modes.
3. Human handoff: `/handoff` exports the current transcript and estimate to a human buyer.
4. Listing quality score: tell the operator which missing fields would most improve pricing confidence.
5. Operator notes: allow private notes per negotiation that are not sent to the seller.

## Better Pricing

1. Market comps: ingest listings from approved marketplaces or dealer feeds.
2. Region-aware pricing: use country/city, currency, and steering-side defaults.
3. Depreciation model: combine deterministic heuristics with LLM explanations.
4. Damage and service modifiers: explicit adjustments for accident history, service records, tires, brakes, owners, and import status.
5. Confidence bands: store a confidence level and explain why a range is wide or narrow.

## Telegram Experience

1. Privacy-safe group mode: process only replies, mentions, or `/sell` commands.
2. Photo support: accept images with captions, OCR listing screenshots, and save image references.
3. Inline buttons: add Reset, Status, Human Review, and Accept Deal buttons.
4. Multi-language templates: deterministic fallback copy per locale.
5. Thread support: one negotiation per Telegram forum topic.

## Business Workflow

1. CRM export: push agreed deals into HubSpot, Airtable, Notion, or a custom webhook.
2. Appointment scheduling: after agreement, propose viewing slots.
3. Document checklist: request service history, registration, VIN, and inspection photos.
4. Deal scoring: rank active negotiations by expected margin and confidence.
5. Compliance audit: immutable log of model prompts, raw responses, validations, and sent messages.

## Production Operations

1. Webhook deployment: HTTPS endpoint, secret token validation, health/readiness checks.
2. Observability: metrics for LLM latency, parse failures, Telegram failures, active sessions, and deal outcomes.
3. Queueing: process updates through a job queue to control API cost and retries.
4. Postgres migration: durable multi-worker storage when traffic grows.
5. Playbooks: runbooks for Anthropic outage, Telegram outage, DB lock, and runaway cost.
