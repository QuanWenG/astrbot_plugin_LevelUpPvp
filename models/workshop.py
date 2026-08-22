"""Immutable values returned by the equipment workshop workflow."""

from __future__ import annotations

from dataclasses import dataclass

try:
    from .equipment import EquipmentItem
except ImportError:
    from models.equipment import EquipmentItem


@dataclass(frozen=True)
class WorkshopWallet:
    user_pk: int
    scrap_balance: int
    lifetime_earned: int
    lifetime_spent: int
    season_tokens: int = 0


@dataclass(frozen=True)
class SalvageResult:
    equipment_id: int
    equipment_name: str
    quality: str
    item_level: int
    scrap_gained: int
    balance_after: int


@dataclass(frozen=True)
class DominatedSalvageItem:
    """Why one conservative cleanup candidate is replaceable right now."""

    equipment_id: int
    equipment_name: str
    quality: str
    item_level: int
    slot_label: str
    direction_labels: tuple[str, ...]
    keeper_id: int
    keeper_name: str
    keeper_quality: str
    keeper_level: int
    candidate_fingerprint: str = ""
    keeper_fingerprint: str = ""


@dataclass(frozen=True)
class BulkSalvagePreview:
    """Immutable exact-token snapshot for an explicit cleanup policy."""

    user_pk: int
    quality: str
    quality_label: str
    items: tuple[tuple[int, str, int], ...]
    scrap_total: int
    confirmation_token: str
    policy_id: str = "common"
    dominated_items: tuple[DominatedSalvageItem, ...] = ()

    @property
    def item_count(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class BulkSalvageResult:
    """Result of one atomic, explicitly confirmed inventory cleanup."""

    user_pk: int
    quality: str
    item_count: int
    equipment_ids: tuple[int, ...]
    scrap_gained: int
    balance_after: int


@dataclass(frozen=True)
class ReworkCost:
    quality_base: int
    level_surcharge: int
    season_tokens: int = 0

    @property
    def total(self) -> int:
        return self.quality_base + self.level_surcharge


@dataclass(frozen=True)
class ReworkPreview:
    equipment_id: int
    equipment_name: str
    direction: str
    direction_label: str
    cost: ReworkCost
    balance_after: int
    candidate_affixes: tuple[dict, ...]
    match_score: int
    miss_streak_before: int
    miss_streak_after: int
    pity_guaranteed: bool
    mode: str = "standard"
    target_guaranteed: bool = False
    season_tokens_after: int = 0


@dataclass(frozen=True)
class ReworkDecision:
    equipment_id: int
    accepted: bool
    direction: str
    scrap_spent: int
    balance: int
    match_score: int
    miss_streak: int
    item: EquipmentItem
    mode: str = "standard"
    season_tokens_spent: int = 0
    season_tokens_balance: int = 0
