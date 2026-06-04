from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal, TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator, model_validator

from agents.pricer import PricerResult, _get_client, extract_json, response_text
from config import settings

if TYPE_CHECKING:
    from conversation import ConversationState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

OPENING_SYSTEM_PROMPT = """You are a skilled but friendly car buyer negotiating with a private seller.

You will be given:
- The car description as posted by the seller
- An internal price estimate that you must NOT reveal

Return only a JSON object:
{
  "message": "<single Telegram-ready message to the seller>",
  "offer_price": <float>,
  "currency": "<ISO currency code>"
}

Rules:
- Open with a concrete offer below the low end of the internal estimate.
- Never reveal the internal estimate or say that you have one.
- Be concise, natural, polite, and in the same language as the listing.
- No markdown, no preamble, no extra keys."""


CONTINUE_SYSTEM_PROMPT = """You are a skilled but friendly car buyer in an ongoing negotiation with a private seller.

You will be given:
- The car description
- Your internal price estimate, including the hard ceiling
- The transcript before the seller's latest reply
- The seller's latest reply

Return only a JSON object:
{
  "message": "<your Telegram-ready reply to the seller>",
  "status": "negotiating" | "deal_agreed" | "walk_away",
  "offered_price": <float or null>,
  "currency": "<ISO currency code or null>"
}

Rules:
- Never reveal the internal estimate or hard ceiling.
- Never offer at or above the hard ceiling.
- Increase gradually only if justified by seller pushback.
- If the seller will not come below the ceiling, walk away politely.
- If the seller accepts a price within range, mark deal_agreed.
- Reply in the seller's language.
- No markdown, no preamble, no extra keys."""

# ---------------------------------------------------------------------------
# Pydantic output models
# ---------------------------------------------------------------------------

class OpeningMessage(BaseModel):
    message: str = Field(min_length=1, max_length=3900)
    offer_price: float = Field(gt=0)
    currency: str

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("message cannot be blank")
        return cleaned

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise ValueError("currency must be a 3-letter ISO code")
        return normalized


class NegotiationTurn(BaseModel):
    message: str = Field(min_length=1, max_length=3900)
    status: Literal["negotiating", "deal_agreed", "walk_away"]
    offered_price: float | None = None
    currency: str | None = None
    raw_response: str = ""

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("message cannot be blank")
        return cleaned

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise ValueError("currency must be a 3-letter ISO code")
        return normalized

    @model_validator(mode="after")
    def validate_terminal_offer(self) -> "NegotiationTurn":
        if self.status == "deal_agreed" and self.offered_price is None:
            raise ValueError("deal_agreed requires offered_price")
        if self.offered_price is not None and self.offered_price <= 0:
            raise ValueError("offered_price must be positive")
        return self

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def money(amount: float, currency: str) -> str:
    return f"{currency} {amount:,.0f}"


def deal_summary(state: "ConversationState", turn: NegotiationTurn) -> str:
    """
    Build a short deal-outcome summary sent after the negotiation reaches a
    terminal status (deal_agreed or walk_away).
    """
    vehicle = state.description[:120].strip().replace("\n", " ")
    if len(state.description) > 120:
        vehicle += "…"

    if turn.status == "deal_agreed" and turn.offered_price and turn.currency:
        return (
            f"✅ *Deal agreed* at {money(turn.offered_price, turn.currency)}\n"
            f"Vehicle: {vehicle}\n"
            f"Turns: {len(state.history) + 2}"
        )
    else:
        last_offer = _latest_buyer_offer(state)
        offer_line = (
            f"Last offer: {money(last_offer, state.estimate.currency)}"
            if last_offer and state.estimate and state.estimate.currency
            else "No offer reached"
        )
        return (
            f"🚶 *Walk away* — no deal reached\n"
            f"Vehicle: {vehicle}\n"
            f"{offer_line}\n"
            f"Turns: {len(state.history) + 2}"
        )


# ---------------------------------------------------------------------------
# Safety wrappers — enforce price-ceiling rules in code, not just in prompts
# ---------------------------------------------------------------------------

def _extract_explicit_price(text: str) -> float | None:
    text_lower = text.lower()
    # Pattern 1: currency/price indicator followed by number
    pattern1 = r"(?:eur|€|gbp|£|\$|asking|price|pricing)\s*(?:of|is|at|for|:)?\s*(\d{1,3}(?:[,.]\d{3})*|\d{3,6})\b"
    # Pattern 2: number followed by currency indicator
    pattern2 = r"\b(\d{1,3}(?:[,.]\d{3})*|\d{3,6})\s*(?:eur|€|gbp|£|\$)"
    
    matches1 = re.findall(pattern1, text_lower)
    matches2 = re.findall(pattern2, text_lower)
    
    all_matches = matches1 + matches2
    prices = []
    for m in all_matches:
        cleaned = m.replace(",", "").replace(".", "")
        try:
            val = float(cleaned)
            if val > 100:
                prices.append(val)
        except ValueError:
            continue
    return prices[-1] if prices else None


def _get_buyer_ceiling(state: "ConversationState") -> float:
    estimate = state.estimate
    if estimate is None or estimate.high_price is None:
        raise ValueError("negotiation state has no estimate or high price")
    
    ceiling = estimate.high_price
    
    desc_price = _extract_explicit_price(state.description)
    if desc_price is not None:
        return min(ceiling, desc_price)
        
    for turn in state.history:
        if turn.speaker == "seller":
            p = _extract_amount(turn.text)
            if p is not None:
                return min(ceiling, p)
                
    return ceiling


def _get_target_ceiling(state: "ConversationState", latest_seller_message: str | None = None) -> float:
    buyer_ceiling = _get_buyer_ceiling(state)
    
    lowest_seller_price = None
    for turn in state.history:
        if turn.speaker == "seller":
            p = _extract_amount(turn.text)
            if p is not None:
                if lowest_seller_price is None or p < lowest_seller_price:
                    lowest_seller_price = p
                    
    if latest_seller_message:
        p = _extract_amount(latest_seller_message)
        if p is not None:
            if lowest_seller_price is None or p < lowest_seller_price:
                lowest_seller_price = p
                
    if lowest_seller_price is not None:
        return min(buyer_ceiling, lowest_seller_price)
        
    return buyer_ceiling


def _fallback_opening(estimate: PricerResult, description: str) -> OpeningMessage:
    assert estimate.low_price is not None
    assert estimate.currency is not None
    
    asking_price = _extract_explicit_price(description)
    base_price = estimate.low_price
    if asking_price is not None:
        base_price = min(base_price, asking_price)
        
    offer = round(base_price * settings.INITIAL_OFFER_RATIO, -2)
    return OpeningMessage(
        message=(
            f"Thanks for the details. Based on what you've shared, "
            f"I could open at {money(offer, estimate.currency)}. Would that be workable?"
        ),
        offer_price=float(offer),
        currency=estimate.currency,
    )


def _safe_opening(opening: OpeningMessage, estimate: PricerResult, description: str) -> OpeningMessage:
    """
    Reject an LLM-generated opening that violates the price-floor constraint.
    Falls back to a deterministic safe offer if the LLM over-bids or uses the
    wrong currency.
    """
    if estimate.low_price is None or estimate.currency is None:
        raise ValueError("estimated result missing low price or currency")
        
    asking_price = _extract_explicit_price(description)
    ceiling = estimate.low_price
    if asking_price is not None:
        ceiling = min(ceiling, asking_price)
        
    if opening.currency != estimate.currency:
        logger.warning(
            "Opening currency mismatch (got %s, expected %s); using fallback",
            opening.currency,
            estimate.currency,
        )
        return _fallback_opening(estimate, description)
    if opening.offer_price >= ceiling:
        logger.warning(
            "Opening offer %.0f >= ceiling %.0f; using fallback",
            opening.offer_price,
            ceiling,
        )
        return _fallback_opening(estimate, description)
    return opening


def _fallback_turn(message: str, status: Literal["negotiating", "deal_agreed", "walk_away"]) -> NegotiationTurn:
    return NegotiationTurn(message=message, status=status, offered_price=None, currency=None)


def _safe_turn(turn: NegotiationTurn, state: "ConversationState", seller_message: str) -> NegotiationTurn:
    """
    Reject a negotiation turn that would send an offer at or above the target
    ceiling, or above the buyer's absolute ceiling if agreeing a deal.
    Replaces it with a polite walk-away.
    """
    buyer_ceiling = _get_buyer_ceiling(state)
    target_ceiling = _get_target_ceiling(state, seller_message)
    
    if turn.offered_price is not None:
        if turn.status == "deal_agreed":
            if turn.offered_price > buyer_ceiling:
                logger.warning(
                    "Agreed price %.0f > buyer ceiling %.0f; forcing walk_away",
                    turn.offered_price,
                    buyer_ceiling,
                )
                return _fallback_turn(
                    "I appreciate it, but I do not think I can make the numbers work at that level. I will step back for now.",
                    "walk_away",
                )
        else:  # negotiating
            if turn.offered_price >= target_ceiling:
                logger.warning(
                    "Negotiation offer %.0f >= target ceiling %.0f; forcing walk_away",
                    turn.offered_price,
                    target_ceiling,
                )
                return _fallback_turn(
                    "I appreciate it, but I do not think I can make the numbers work at that level. I will step back for now.",
                    "walk_away",
                )
    return turn



# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _latest_buyer_offer(state: "ConversationState") -> float | None:
    for turn in reversed(state.history):
        if turn.speaker == "buyer" and turn.offered_price is not None:
            return turn.offered_price
    return None


def _extract_amount(text: str) -> float | None:
    matches = re.findall(r"(?:eur|€|gbp|£|\$)?\s*(\d{1,3}(?:[,.]\d{3})+|\d{4,6})", text.lower())
    if not matches:
        return None
    cleaned = matches[-1].replace(",", "").replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Demo-mode (no Anthropic calls)
# ---------------------------------------------------------------------------

def _demo_opening(estimate: PricerResult, description: str) -> OpeningMessage:
    return _fallback_opening(estimate, description)


def _demo_continue(state: "ConversationState", seller_message: str) -> NegotiationTurn:
    estimate = state.estimate
    if estimate is None or estimate.low_price is None or estimate.high_price is None or estimate.currency is None:
        raise ValueError("negotiation state has no estimate")

    text = seller_message.lower()
    initial_asking = _extract_explicit_price(state.description)
    base_price = estimate.low_price
    if initial_asking is not None:
        base_price = min(base_price, initial_asking)
    latest_offer = _latest_buyer_offer(state) or round(base_price * settings.INITIAL_OFFER_RATIO, -2)
    
    seller_amount = _extract_amount(seller_message)
    buyer_ceiling = _get_buyer_ceiling(state)
    target_ceiling = _get_target_ceiling(state, seller_message)

    has_agreement_word = bool(re.search(r"\b(yes|ok|okay|deal|accepted|accept|works|fine)\b", text))
    is_agreed = False
    if seller_amount is not None and seller_amount <= latest_offer:
        is_agreed = True
    elif has_agreement_word:
        if seller_amount is None or seller_amount <= latest_offer:
            is_agreed = True

    if is_agreed:
        return NegotiationTurn(
            message=f"Great, thanks. Let's call it agreed at {money(latest_offer, estimate.currency)}.",
            status="deal_agreed",
            offered_price=latest_offer,
            currency=estimate.currency,
        )

    if seller_amount is not None and seller_amount >= buyer_ceiling:
        return NegotiationTurn(
            message="Thanks, but that is above where I can sensibly be on this car. I will leave it for now.",
            status="walk_away",
            offered_price=None,
            currency=estimate.currency,
        )

    next_offer = min(
        round(latest_offer + (target_ceiling - latest_offer) * 0.35, -2),
        target_ceiling * 0.98,
    )
    if next_offer <= latest_offer or next_offer >= target_ceiling:
        return NegotiationTurn(
            message="I appreciate it, but I do not think I can make the numbers work at that level. I will step back for now.",
            status="walk_away",
            offered_price=None,
            currency=estimate.currency,
        )

    return NegotiationTurn(
        message=f"I can move a bit. The best I can do right now is {money(next_offer, estimate.currency)}.",
        status="negotiating",
        offered_price=float(next_offer),
        currency=estimate.currency,
    )


# ---------------------------------------------------------------------------
# LLM calls with retry/backoff
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.5  # seconds


async def _call_with_retry(coro_factory, *, label: str):
    """
    Call an async coroutine factory up to _MAX_RETRIES times with exponential
    back-off on transient errors (rate limits, server errors). Raises the last
    exception if all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "%s attempt %d/%d failed (%s); retrying in %.1fs",
                    label, attempt, _MAX_RETRIES, exc, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("%s failed after %d attempts: %s", label, _MAX_RETRIES, exc)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def write_opening_message(description: str, estimate: PricerResult) -> OpeningMessage:
    """
    Generate the opening buyer message.  In demo mode uses a local heuristic.
    In real mode calls the Anthropic API with retry/backoff and validates the
    result against the price-floor constraint before returning.
    """
    if settings.DEMO_MODE:
        return _demo_opening(estimate, description)

    async def _call():
        response = await _get_client().messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=256,
            system=OPENING_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Car description:\n{description}\n\n"
                        f"Internal estimate: {estimate.currency} {estimate.low_price} - {estimate.high_price}\n\n"
                        "Write the opening negotiation JSON now."
                    ),
                }
            ],
        )
        raw = response_text(response)
        opening = OpeningMessage.model_validate(extract_json(raw))
        return _safe_opening(opening, estimate, description)

    return await _call_with_retry(_call, label="write_opening_message")


async def continue_negotiation(state: "ConversationState", seller_message: str) -> NegotiationTurn:
    """
    Generate the next negotiation turn for the buyer.

    The transcript passed to the LLM is built from ``state.history`` *before*
    the seller's latest message is appended, so the seller message appears only
    once (in the separate ``Seller's latest reply`` section). This avoids the
    double-counting bug from the original prototype.

    In demo mode uses a local heuristic. In real mode calls the Anthropic API
    with retry/backoff and validates the result against the price-ceiling
    constraint before returning.
    """
    if state.estimate is None:
        raise ValueError("cannot continue negotiation without an estimate")
    if settings.DEMO_MODE:
        return _safe_turn(_demo_continue(state, seller_message), state, seller_message)

    # Build transcript from existing history (seller's latest reply is NOT yet
    # in state.history at this point — it is added by the handler after success).
    transcript_lines = [f"{turn.speaker.upper()}: {turn.text}" for turn in state.history]
    if len(transcript_lines) > settings.MAX_HISTORY_TURNS:
        omitted = len(transcript_lines) - settings.MAX_HISTORY_TURNS
        transcript_lines = (
            [f"(Earlier transcript omitted: {omitted} turns)"]
            + transcript_lines[-settings.MAX_HISTORY_TURNS :]
        )
    transcript = "\n".join(transcript_lines) if transcript_lines else "(no prior turns)"

    async def _call():
        response = await _get_client().messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=512,
            system=CONTINUE_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Car description:\n{state.description}\n\n"
                        f"Internal estimate (hard ceiling is high): "
                        f"{state.estimate.currency} {state.estimate.low_price} - {state.estimate.high_price}\n\n"
                        f"Transcript before seller's latest reply:\n{transcript}\n\n"
                        f"Seller's latest reply:\n{seller_message}\n\n"
                        "Return the negotiation JSON now."
                    ),
                }
            ],
        )
        raw = response_text(response)
        turn = NegotiationTurn.model_validate(extract_json(raw))
        turn.raw_response = raw
        return _safe_turn(turn, state, seller_message)

    return await _call_with_retry(_call, label="continue_negotiation")
