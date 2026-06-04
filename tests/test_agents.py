"""
Tests for pricer and negotiator agents.

All tests run without any Anthropic or Telegram network calls.
DEMO_MODE=true is set before importing any application modules.
"""
from __future__ import annotations

import os
import unittest

from pydantic import ValidationError

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-123")
os.environ.setdefault("DEMO_MODE", "true")

from agents.negotiator import (
    NegotiationTurn,
    OpeningMessage,
    _extract_amount,
    _fallback_opening,
    _safe_opening,
    _safe_turn,
    deal_summary,
    _extract_explicit_price,
    _get_buyer_ceiling,
    _get_target_ceiling,
    _demo_continue,
)
from agents.pricer import PricerResult, _demo_estimate, _has_minimum_vehicle_info, extract_json
from conversation import ConversationState, SessionKey, Turn


# ---------------------------------------------------------------------------
# Pricer model validation
# ---------------------------------------------------------------------------

class PricerModelTests(unittest.TestCase):
    def test_extract_json_handles_preamble_and_fences(self) -> None:
        self.assertEqual(extract_json('Here:\n```json\n{"ok": true}\n```'), {"ok": True})

    def test_extract_json_raises_on_no_braces(self) -> None:
        with self.assertRaises(ValueError):
            extract_json("no json here")

    def test_pricer_rejects_estimate_without_prices(self) -> None:
        with self.assertRaises(ValidationError):
            PricerResult(
                status="estimated",
                low_price=None,
                high_price=None,
                currency=None,
                reasoning="missing prices",
            )

    def test_pricer_rejects_inverted_range(self) -> None:
        with self.assertRaises(ValidationError):
            PricerResult(
                status="estimated",
                low_price=20000,
                high_price=10000,
                currency="EUR",
                reasoning="bad range",
            )

    def test_pricer_rejects_negative_prices(self) -> None:
        with self.assertRaises(ValidationError):
            PricerResult(
                status="estimated",
                low_price=-5000,
                high_price=10000,
                currency="EUR",
                reasoning="negative",
            )

    def test_pricer_requires_clarifying_question(self) -> None:
        with self.assertRaises(ValidationError):
            PricerResult(status="insufficient_info", reasoning="missing question")

    def test_pricer_normalizes_currency(self) -> None:
        result = PricerResult(
            status="estimated",
            low_price=10000,
            high_price=12000,
            currency="eur",
            reasoning="ok",
        )
        self.assertEqual(result.currency, "EUR")

    def test_pricer_rejects_bad_currency(self) -> None:
        with self.assertRaises(ValidationError):
            PricerResult(
                status="estimated",
                low_price=10000,
                high_price=12000,
                currency="EUROS",
                reasoning="bad currency",
            )

    def test_insufficient_info_clears_prices(self) -> None:
        result = PricerResult(
            status="insufficient_info",
            reasoning="need more info",
            clarifying_question="What year?",
        )
        self.assertIsNone(result.low_price)
        self.assertIsNone(result.high_price)
        self.assertIsNone(result.currency)


# ---------------------------------------------------------------------------
# Demo pricer heuristic
# ---------------------------------------------------------------------------

class DemoPricerTests(unittest.TestCase):
    _FULL = "2018 BMW 320d, 95k km, good condition, asking EUR 18000"
    _VAGUE = "Selling a car, interested?"

    def test_minimum_info_check_passes(self) -> None:
        self.assertTrue(_has_minimum_vehicle_info(self._FULL))

    def test_minimum_info_check_fails_for_vague(self) -> None:
        self.assertFalse(_has_minimum_vehicle_info(self._VAGUE))

    def test_demo_estimate_returns_estimated(self) -> None:
        result = _demo_estimate(self._FULL)
        self.assertEqual(result.status, "estimated")
        self.assertIsNotNone(result.low_price)
        self.assertIsNotNone(result.high_price)
        self.assertLess(result.low_price, result.high_price)  # type: ignore[operator]
        self.assertEqual(result.currency, "EUR")

    def test_demo_estimate_returns_insufficient_for_vague(self) -> None:
        result = _demo_estimate(self._VAGUE)
        self.assertEqual(result.status, "insufficient_info")
        self.assertIsNotNone(result.clarifying_question)

    def test_demo_estimate_luxury_premium(self) -> None:
        bmw = _demo_estimate("2020 BMW 5 Series, 60k km, excellent condition")
        vw = _demo_estimate("2020 VW Golf, 60000 km, good condition")
        self.assertGreater(bmw.low_price, vw.low_price)  # type: ignore[operator]

    def test_demo_estimate_damage_discount(self) -> None:
        good = _demo_estimate(self._FULL)
        damaged = _demo_estimate("2018 BMW 320d, 95k km, accident history, asking EUR 18000")
        self.assertLess(damaged.low_price, good.low_price)  # type: ignore[operator]

    def test_demo_estimate_with_photo_simulates_italian_libretto(self) -> None:
        result = _demo_estimate("vague text", photo_path="dummy_photo.jpg")
        self.assertEqual(result.status, "estimated")
        self.assertIsNotNone(result.extracted_description)
        self.assertIn("BMW 320d", result.extracted_description)
        self.assertIn("libretto", result.extracted_description)


# ---------------------------------------------------------------------------
# Negotiator model validation
# ---------------------------------------------------------------------------

class NegotiatorModelTests(unittest.TestCase):
    def test_negotiation_requires_deal_price(self) -> None:
        with self.assertRaises(ValidationError):
            NegotiationTurn(message="Deal.", status="deal_agreed")

    def test_negotiation_rejects_blank_message(self) -> None:
        with self.assertRaises(ValidationError):
            NegotiationTurn(message="   ", status="negotiating")

    def test_opening_message_normalizes_currency(self) -> None:
        opening = OpeningMessage(message="Would you take EUR 10,000?", offer_price=10000, currency="eur")
        self.assertEqual(opening.currency, "EUR")

    def test_opening_message_rejects_zero_offer(self) -> None:
        with self.assertRaises(ValidationError):
            OpeningMessage(message="Would you take EUR 0?", offer_price=0, currency="EUR")

    def test_negotiation_rejects_negative_offer(self) -> None:
        with self.assertRaises(ValidationError):
            NegotiationTurn(message="How about -500?", status="negotiating", offered_price=-500)


# ---------------------------------------------------------------------------
# Safety wrapper tests
# ---------------------------------------------------------------------------

def _make_estimate(low: float, high: float, currency: str = "EUR") -> PricerResult:
    return PricerResult(
        status="estimated",
        low_price=low,
        high_price=high,
        currency=currency,
        reasoning="test",
    )


class SafetyWrapperTests(unittest.TestCase):
    def _make_state(self, low: float, high: float, description: str = "") -> ConversationState:
        state = ConversationState(key=SessionKey(chat_id=-123, user_id=42))
        state.estimate = _make_estimate(low, high)
        state.description = description
        return state

    def test_safe_opening_rejects_overbid(self) -> None:
        estimate = _make_estimate(10000, 12000)
        overbid = OpeningMessage(message="Hi, I offer 11000", offer_price=11000, currency="EUR")
        result = _safe_opening(overbid, estimate, "2018 BMW")
        # Should fall back to a deterministic safe offer
        self.assertLess(result.offer_price, estimate.low_price)  # type: ignore[operator]

    def test_safe_opening_rejects_currency_mismatch(self) -> None:
        estimate = _make_estimate(10000, 12000, "EUR")
        wrong_currency = OpeningMessage(message="I offer GBP 8000", offer_price=8000, currency="GBP")
        result = _safe_opening(wrong_currency, estimate, "2018 BMW")
        self.assertEqual(result.currency, "EUR")

    def test_safe_opening_accepts_valid(self) -> None:
        estimate = _make_estimate(10000, 12000)
        valid = OpeningMessage(message="I offer EUR 8500", offer_price=8500, currency="EUR")
        result = _safe_opening(valid, estimate, "2018 BMW")
        self.assertEqual(result.offer_price, 8500)

    def test_safe_opening_respects_asking_price_ceiling(self) -> None:
        estimate = _make_estimate(10000, 12000)
        # Even if offer is < low_price (10000), if it's >= asking price (e.g. 9000), it should be rejected.
        overbid = OpeningMessage(message="I offer EUR 9500", offer_price=9500, currency="EUR")
        result = _safe_opening(overbid, estimate, "2018 BMW, asking 9000")
        self.assertLess(result.offer_price, 9000)

    def test_safe_turn_rejects_over_ceiling(self) -> None:
        state = self._make_state(10000, 12000, "2018 BMW")
        turn = NegotiationTurn(
            message="I'll go to 12500",
            status="negotiating",
            offered_price=12500,
            currency="EUR",
        )
        result = _safe_turn(turn, state, "no")
        self.assertEqual(result.status, "walk_away")

    def test_safe_turn_accepts_under_ceiling(self) -> None:
        state = self._make_state(10000, 12000, "2018 BMW")
        turn = NegotiationTurn(
            message="I'll go to 11500",
            status="negotiating",
            offered_price=11500,
            currency="EUR",
        )
        result = _safe_turn(turn, state, "no")
        self.assertEqual(result.offered_price, 11500)

    def test_safe_turn_respects_asking_price_ceiling(self) -> None:
        # Asking price 11000 is lower than estimate high_price 12000.
        state = self._make_state(10000, 12000, "2018 BMW asking EUR 11000")
        turn = NegotiationTurn(
            message="I'll go to 11500",
            status="negotiating",
            offered_price=11500,
            currency="EUR",
        )
        result = _safe_turn(turn, state, "no")
        self.assertEqual(result.status, "walk_away")

    def test_safe_turn_respects_seller_counter_offer(self) -> None:
        state = self._make_state(10000, 12000, "2018 BMW")
        # Seller counters with 10500 in latest message.
        turn = NegotiationTurn(
            message="I'll go to 10800",
            status="negotiating",
            offered_price=10800,
            currency="EUR",
        )
        result = _safe_turn(turn, state, "I want 10500")
        self.assertEqual(result.status, "walk_away")

    def test_extract_explicit_price(self) -> None:
        self.assertEqual(_extract_explicit_price("asking EUR 18,000"), 18000.0)
        self.assertEqual(_extract_explicit_price("price 15000"), 15000.0)
        self.assertEqual(_extract_explicit_price("18000 €"), 18000.0)
        # Verify it ignores years and mileages
        self.assertIsNone(_extract_explicit_price("2018 BMW, 95000 km"))
        # Test complex mixed string
        self.assertEqual(_extract_explicit_price("2018 BMW 320d, 95k km, manual, asking EUR 18,000"), 18000.0)

    def test_get_buyer_and_target_ceiling(self) -> None:
        state = self._make_state(10000, 12000, "2018 BMW, asking EUR 11000")
        # Buyer ceiling should be min of 12000 and 11000 -> 11000
        self.assertEqual(_get_buyer_ceiling(state), 11000.0)
        # Target ceiling should also be 11000 (no seller messages yet)
        self.assertEqual(_get_target_ceiling(state), 11000.0)

        # Add seller turns to history
        state.history.append(Turn(speaker="seller", text="How about 10500?"))
        self.assertEqual(_get_buyer_ceiling(state), 11000.0)
        self.assertEqual(_get_target_ceiling(state), 10500.0)

        # Test latest message
        self.assertEqual(_get_target_ceiling(state, "No, 10200 is my best"), 10200.0)

        # Description has no asking price
        state_no_ask = self._make_state(10000, 12000, "2018 BMW")
        self.assertEqual(_get_buyer_ceiling(state_no_ask), 12000.0)
        self.assertEqual(_get_target_ceiling(state_no_ask), 12000.0)

        # Add first seller turn to history - this establishes the initial asking price / buyer ceiling
        state_no_ask.history.append(Turn(speaker="seller", text="I want 11000"))
        self.assertEqual(_get_buyer_ceiling(state_no_ask), 11000.0)
        self.assertEqual(_get_target_ceiling(state_no_ask), 11000.0)


# ---------------------------------------------------------------------------
# Amount extraction helper
# ---------------------------------------------------------------------------

class ExtractAmountTests(unittest.TestCase):
    def test_extracts_plain_number(self) -> None:
        self.assertEqual(_extract_amount("I want 15000"), 15000.0)

    def test_extracts_eur_prefixed(self) -> None:
        self.assertEqual(_extract_amount("Asking EUR 18,000"), 18000.0)

    def test_returns_none_for_no_number(self) -> None:
        self.assertIsNone(_extract_amount("No price mentioned"))


# ---------------------------------------------------------------------------
# Deal summary
# ---------------------------------------------------------------------------

class DealSummaryTests(unittest.TestCase):
    def _make_state(self) -> ConversationState:
        state = ConversationState(key=SessionKey(chat_id=-123, user_id=42))
        state.estimate = _make_estimate(10000, 12000)
        state.description = "2018 BMW 320d, 95k km"
        return state

    def test_deal_agreed_summary_contains_price(self) -> None:
        state = self._make_state()
        turn = NegotiationTurn(message="Deal.", status="deal_agreed", offered_price=10500, currency="EUR")
        summary = deal_summary(state, turn)
        self.assertIn("10,500", summary)
        self.assertIn("agreed", summary.lower())

    def test_walk_away_summary_mentions_walk(self) -> None:
        state = self._make_state()
        turn = NegotiationTurn(message="Walking away.", status="walk_away")
        summary = deal_summary(state, turn)
        self.assertIn("walk", summary.lower())


class DemoContinueTests(unittest.TestCase):
    def _make_state(self, low: float, high: float, description: str = "") -> ConversationState:
        state = ConversationState(key=SessionKey(chat_id=-123, user_id=42))
        state.estimate = _make_estimate(low, high)
        state.description = description
        return state

    def test_demo_continue_accepts_plain_agreement(self) -> None:
        state = self._make_state(10000, 12000, "2018 BMW, asking EUR 11000")
        state.history.append(Turn(speaker="buyer", text="I offer 9000", offered_price=9000.0, currency="EUR"))
        
        # Seller says "ok"
        turn = _demo_continue(state, "ok")
        self.assertEqual(turn.status, "deal_agreed")
        self.assertEqual(turn.offered_price, 9000.0)

    def test_demo_continue_rejects_agreement_with_different_price(self) -> None:
        state = self._make_state(10000, 12000, "2018 BMW, asking EUR 11000")
        state.history.append(Turn(speaker="buyer", text="I offer 9000", offered_price=9000.0, currency="EUR"))
        
        # Seller says "okay, 10500" - this should NOT agree at 9000, it's a counter-offer
        turn = _demo_continue(state, "okay, 10500")
        self.assertEqual(turn.status, "negotiating")
        self.assertGreater(turn.offered_price, 9000.0)
        self.assertLess(turn.offered_price, 10500.0)

    def test_demo_continue_accepts_agreement_with_offered_price(self) -> None:
        state = self._make_state(10000, 12000, "2018 BMW, asking EUR 11000")
        state.history.append(Turn(speaker="buyer", text="I offer 9000", offered_price=9000.0, currency="EUR"))
        
        # Seller says "okay, 9000" - matches our offered price, so agreement
        turn = _demo_continue(state, "okay, 9000")
        self.assertEqual(turn.status, "deal_agreed")
        self.assertEqual(turn.offered_price, 9000.0)

    def test_demo_continue_bidding_caps_at_asking_price(self) -> None:
        # High market estimate is 23700, but seller asks 18000
        state = self._make_state(18600, 23700, "2018 BMW, asking EUR 18000")
        # Start negotiation: buyer opens at 15300
        state.history.append(Turn(speaker="buyer", text="I offer 15300", offered_price=15300.0, currency="EUR"))
        
        # Seller pushes back
        turn = _demo_continue(state, "no")
        # Buyer should bid more than 15300 but strictly below 18000
        self.assertEqual(turn.status, "negotiating")
        self.assertGreater(turn.offered_price, 15300.0)
        self.assertLess(turn.offered_price, 18000.0)


if __name__ == "__main__":
    unittest.main()
