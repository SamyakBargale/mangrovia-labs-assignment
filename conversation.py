from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Literal

from agents.pricer import PricerResult


Phase = Literal["idle", "awaiting_info", "negotiating"]
Speaker = Literal["seller", "buyer"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SessionKey:
    chat_id: int
    user_id: int
    thread_id: int | None = None


@dataclass
class Turn:
    speaker: Speaker
    text: str
    offered_price: float | None = None
    currency: str | None = None


@dataclass
class ConversationState:
    key: SessionKey
    phase: Phase = "idle"
    description: str = ""
    estimate: PricerResult | None = None
    history: list[Turn] = field(default_factory=list)
    session_id: int | None = None
    estimate_id: int | None = None
    updated_at: datetime = field(default_factory=utc_now)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def expired(self, timeout_minutes: int) -> bool:
        age_seconds = (utc_now() - self.updated_at).total_seconds()
        return age_seconds > timeout_minutes * 60

    def reset(self) -> None:
        self.phase = "idle"
        self.description = ""
        self.estimate = None
        self.history = []
        self.session_id = None
        self.estimate_id = None
        self.touch()


_lock = RLock()
_states: dict[SessionKey, ConversationState] = {}


def get_state(key: SessionKey) -> ConversationState:
    with _lock:
        if key not in _states:
            _states[key] = ConversationState(key=key)
        return _states[key]


def reset(key: SessionKey | ConversationState) -> None:
    state = key if isinstance(key, ConversationState) else get_state(key)
    with _lock:
        state.reset()


def clear_all() -> None:
    with _lock:
        _states.clear()
