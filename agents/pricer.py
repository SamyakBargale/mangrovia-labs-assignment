from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, field_validator, model_validator

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert in the European used-car market.

Return only a JSON object with this exact structure:
{
  "status": "estimated" | "insufficient_info",
  "low_price": <float or null>,
  "high_price": <float or null>,
  "currency": "<ISO currency code, e.g. EUR, GBP> or null",
  "reasoning": "<brief explanation of your pricing logic>",
  "clarifying_question": "<single question to ask the seller> or null",
  "extracted_description": "<if a photo/document is provided, generate a detailed vehicle spec description (make, model, year of registration, engine displacement/power, fuel type, Euro emissions standard, mileage, and condition) extracted from it; otherwise null>"
}

Pricing rules:
- Estimate a realistic market RANGE, not an exact sale price.
- It is enough to estimate when you know the make/model, approximate year or generation, rough mileage, and visible condition/context.
- If a photo of a registration certificate (e.g. Italian "libretto di circolazione" or "carta di circolazione") is provided, parse Section B (first registration date), Section D (make/model), Section E (chassis/VIN), Section P.1/P.2 (engine displacement/power), Section P.5/P.3 (fuel type), and Section V.9 (Euro emissions standard).
- Ask for one clarifying detail only when a key field is missing and the range would otherwise be too wide to negotiate responsibly.
- Prefer EUR unless the seller clearly implies another currency or market.
- The price range should reflect market value, not simply the seller's asking price.
- Return only valid JSON. No markdown fences, no preamble."""

# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class PricerResult(BaseModel):
    status: Literal["estimated", "insufficient_info"]
    low_price: float | None = None
    high_price: float | None = None
    currency: str | None = None
    reasoning: str = Field(min_length=1)
    clarifying_question: str | None = None
    extracted_description: str | None = None
    raw_response: str = ""

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
    def validate_status_fields(self) -> "PricerResult":
        if self.status == "estimated":
            if self.low_price is None or self.high_price is None or self.currency is None:
                raise ValueError("estimated results require low_price, high_price, and currency")
            if self.low_price <= 0 or self.high_price <= 0:
                raise ValueError("prices must be positive")
            if self.low_price >= self.high_price:
                raise ValueError("low_price must be lower than high_price")
        else:
            if not self.clarifying_question:
                raise ValueError("insufficient_info results require clarifying_question")
            # Clear price fields for insufficient_info responses
            self.low_price = None
            self.high_price = None
            self.currency = None
        return self

# ---------------------------------------------------------------------------
# Anthropic client (lazy singleton — no import-time network calls)
# ---------------------------------------------------------------------------

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is required when DEMO_MODE=false")
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client

# ---------------------------------------------------------------------------
# JSON parsing helpers (shared with negotiator)
# ---------------------------------------------------------------------------

def extract_json(raw: str) -> dict[str, Any]:
    """
    Extract the first complete JSON object from a string.
    Handles markdown fences, preamble text, and trailing prose.
    Raises ValueError if no JSON object is found.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")
    return json.loads(raw[start : end + 1])


def response_text(response: Any) -> str:
    """Extract concatenated text content from an Anthropic message response."""
    parts: list[str] = []
    for block in getattr(response, "content", []):
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    if not parts:
        raise ValueError("LLM response did not contain text content")
    return "\n".join(parts).strip()

# ---------------------------------------------------------------------------
# Demo-mode pricing (no Anthropic calls, no cost)
# ---------------------------------------------------------------------------

def _has_minimum_vehicle_info(description: str) -> bool:
    """
    Heuristic check for whether the description contains enough info to price.
    Requires a plausible year, a mileage figure, and at least 3 alphabetic words.
    """
    text = description.lower()
    has_year = bool(re.search(r"\b(19|20)\d{2}\b", text))
    has_mileage = bool(re.search(r"\b\d{2,3}[,.]?\d{0,3}\s*(km|kms|kilometers|miles|mi|k)\b", text))
    has_make_modelish = len(re.findall(r"[a-zA-Z]{3,}", text)) >= 3
    return has_year and has_mileage and has_make_modelish


def _demo_estimate(description: str, photo_path: str | None = None) -> PricerResult:
    """
    Deterministic pricing heuristic for demo mode.
    Based on age, mileage, brand segment, and condition keywords.
    """
    extracted_desc = None
    if photo_path and not _has_minimum_vehicle_info(description):
        extracted_desc = (
            "2018 BMW 320d, 95k km, manual, good service history, "
            "Italian registration (libretto), asking EUR 18,000"
        )
        description = extracted_desc

    if not _has_minimum_vehicle_info(description):
        return PricerResult(
            status="insufficient_info",
            reasoning="The description is missing at least one key pricing input.",
            clarifying_question="Could you share the car's make, model, year, mileage, and general condition?",
            raw_response='{"demo": true}',
        )

    year_match = re.search(r"\b(19|20)\d{2}\b", description)
    year = int(year_match.group(0)) if year_match else 2018
    age = max(0, 2026 - year)

    mileage_match = re.search(
        r"\b(\d{2,3})(?:[,.]?(\d{3}))?\s*(km|kms|kilometers|miles|mi|k)\b",
        description.lower(),
    )
    mileage = 90000
    if mileage_match:
        first = int(mileage_match.group(1))
        second = mileage_match.group(2)
        unit = mileage_match.group(3)
        if second:
            mileage = int(f"{first}{second}")
        elif unit == "k":
            mileage = first * 1000
        else:
            mileage = first

    base = 28000 - age * 1050 - mileage * 0.035
    if re.search(r"\b(bmw|mercedes|audi|porsche|lexus|tesla)\b", description.lower()):
        base *= 1.3
    if re.search(r"\b(accident|damaged|needs work|salvage)\b", description.lower()):
        base *= 0.72

    low = max(1500, round(base * 0.88, -2))
    high = max(low + 1000, round(base * 1.12, -2))
    return PricerResult(
        status="estimated",
        low_price=float(low),
        high_price=float(high),
        currency=settings.DEFAULT_CURRENCY,
        reasoning="Demo-mode heuristic based on age, mileage, and broad brand/condition signals.",
        clarifying_question=None,
        extracted_description=extracted_desc,
        raw_response='{"demo": true}',
    )

# ---------------------------------------------------------------------------
# LLM call with retry/backoff
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.5  # seconds


async def _call_with_retry(coro_factory, *, label: str):
    """
    Call an async coroutine factory up to _MAX_RETRIES times with exponential
    back-off on transient errors (rate limits, server errors).
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

async def estimate_price(description: str, photo_path: str | None = None) -> PricerResult:
    """
    Estimate a market price range for the described vehicle.

    In demo mode returns a deterministic local heuristic result at no cost.
    In real mode calls the Anthropic API with retry/backoff, validates the
    structured response with Pydantic, and attaches the raw response text.
    """
    if settings.DEMO_MODE:
        return _demo_estimate(description, photo_path=photo_path)

    async def _call():
        content_list: list[dict[str, Any]] = []
        
        if photo_path:
            import base64
            from pathlib import Path
            p = Path(photo_path)
            if p.exists():
                with open(photo_path, "rb") as f:
                    img_data = base64.b64encode(f.read()).decode("utf-8")
                mime_type = "image/jpeg"
                if p.suffix.lower() == ".png":
                    mime_type = "image/png"
                content_list.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": img_data,
                    }
                })
        
        content_list.append({
            "type": "text",
            "text": description if description else "Please analyze this image and estimate the vehicle price."
        })

        response = await _get_client().messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content_list}],
        )
        raw = response_text(response)
        data = extract_json(raw)
        result = PricerResult.model_validate(data)
        result.raw_response = raw
        return result

    return await _call_with_retry(_call, label="estimate_price")
