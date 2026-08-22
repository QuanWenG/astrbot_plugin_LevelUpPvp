"""Immutable contracts for low-noise growth discovered during normal group chat.

The chat listener is deliberately not part of this module.  It supplies a
stable message id and a small, transport-neutral context; the domain service
returns either silence or a durable reward intent.  Settlement is a separate
transactional boundary so retrying a delivered message can never grant twice.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


CHAT_ACTIVITY_RULESET_ID = "chat-serendipity-v12"
_CHAT_SEED_PERSONALIZATION = b"qq-chat-v12"


def stable_chat_seed(*parts: object) -> int:
    """Return a portable seed whose coordinates cannot be concatenation-ambiguous."""

    payload = bytearray()
    for part in parts:
        encoded = str(part).encode("utf-8")
        payload.extend(len(encoded).to_bytes(4, "big"))
        payload.extend(encoded)
    return int.from_bytes(
        hashlib.blake2b(
            payload,
            digest_size=8,
            person=_CHAT_SEED_PERSONALIZATION,
        ).digest(),
        "big",
    )


@dataclass(frozen=True)
class ChatMessageContext:
    """One already-attributed group message.

    ``event_key`` must be the platform's stable message id with a platform
    namespace (for example ``qq:group-message:123``).  A redelivered event uses
    the same key and therefore receives the same decision.
    """

    event_key: str
    group_id: str
    user_pk: int
    content: str
    occurred_at_ts: int
    is_bot: bool = False
    is_command: bool = False
    is_group_message: bool = True


@dataclass(frozen=True)
class ChatRewardIntent:
    """A deterministic request which has not necessarily been granted yet."""

    reward_key: str
    ruleset_id: str
    group_id: str
    day_key: str
    user_pk: int
    valid_message_index: int
    experience: int = 0
    equipment_seed: int | None = None
    spell_id: str | None = None
    spell_name: str | None = None
    spellbook_seed: int | None = None
    story_id: str = ""
    story_text: str = ""

    @property
    def has_equipment(self) -> bool:
        return self.equipment_seed is not None

    @property
    def has_spellbook(self) -> bool:
        return self.spell_id is not None and self.spellbook_seed is not None

    @property
    def has_reward(self) -> bool:
        return self.experience > 0 or self.has_equipment or self.has_spellbook


@dataclass(frozen=True)
class ChatActivityDecision:
    """Preparation result; rejected/no-result messages should remain silent."""

    event_key: str
    accepted: bool
    reason: str
    day_key: str = ""
    valid_message_index: int | None = None
    reward_roll_index: int | None = None
    intent: ChatRewardIntent | None = None
    replayed: bool = False
    equipment_probability: float = 0.0
    spellbook_probability: float = 0.0

    @property
    def should_settle(self) -> bool:
        return self.intent is not None and self.intent.has_reward

    @property
    def should_announce(self) -> bool:
        # Preparation itself never proves that a grant committed.
        return False


@dataclass(frozen=True)
class ChatActivitySettlementResult:
    reward_key: str
    applied: bool
    experience: int = 0
    equipment: object | None = None
    spellbook: object | None = None
    spell_name: str | None = None
    level_ups: tuple[object, ...] = ()
    story_text: str = ""

    @property
    def should_announce(self) -> bool:
        return self.applied and (
            self.experience > 0
            or self.equipment is not None
            or self.spellbook is not None
        )


__all__ = [
    "CHAT_ACTIVITY_RULESET_ID",
    "ChatActivityDecision",
    "ChatActivitySettlementResult",
    "ChatMessageContext",
    "ChatRewardIntent",
    "stable_chat_seed",
]
