# Product Enhancement Ideas

## Near‑Term Polish
- **Conversation summary**: after `deal_agreed` or `walk_away`, send a compact summary containing vehicle details, last seller ask, final buyer offer, and status.
- **Strategy presets**: add negotiation mode presets – conservative, balanced, aggressive, quick‑close.
- **Human handoff**: `/handoff` command exports the current transcript and estimate to a human buyer for review.
- **Listing quality score**: algorithm that tells the operator which missing fields would most improve pricing confidence.
- **Operator notes**: private notes per negotiation that are never sent to the seller.

## Better Pricing
- **Market comps**: ingest listings from approved marketplaces or dealer feeds.
- **Region‑aware pricing**: incorporate country/city, currency, and steering‑side defaults.
- **Depreciation model**: combine deterministic heuristics with LLM explanations.
- **Damage & service modifiers**: explicit adjustments for accident history, service records, tires, brakes, owners, and import status.
- **Confidence bands**: store a confidence level and explain why a range is wide or narrow.

## Telegram Experience
- **Privacy‑safe group mode**: process only replies, mentions, or `/sell` commands.
- **Photo support**: accept images with captions, OCR listing screenshots, and save image references.
- **Inline buttons**: add Reset, Status, Human Review, and Accept Deal buttons.
- **Multi‑language templates**: deterministic fallback copy per locale.
- **Thread support**: one negotiation per Telegram forum topic.

## Business Workflow
- **CRM export**: push agreed deals into HubSpot, Airtable, Notion, or a custom webhook.
- **Appointment scheduling**: after agreement, propose viewing slots.
- **Document checklist**: request service history, registration, VIN, and inspection photos.
- **Deal scoring**: rank active negotiations by expected margin and confidence.
- **Compliance audit**: immutable log of model prompts, raw responses, validations, and sent messages.

## Production Operations
- **Webhook deployment**: HTTPS endpoint, secret token validation, health/readiness checks.
- **Observability**: metrics for LLM latency, parse failures, Telegram failures, active sessions, and deal outcomes.
- **Queueing**: process updates through a job queue to control API cost and retries.
- **Postgres migration**: durable multi‑worker storage when traffic grows.
- **Playbooks**: runbooks for Anthropic outage, Telegram outage, DB lock, and runaway cost.
