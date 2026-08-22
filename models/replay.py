"""Read-only, player-facing battle replay models.

The combat engine owns simulation data and the battle service owns settlement.
These small immutable views deliberately own neither: they are the stable
boundary used by replay commands, operation hooks and tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ReplayFighter:
    side: str
    user_pk: int
    name: str
    level: int
    remaining_hp: int | None = None
    max_hp: int | None = None
    remaining_mana: int | None = None
    max_mana: int | None = None
    remaining_stamina: int | None = None
    max_stamina: int | None = None
    damage_dealt: int = 0
    final_statuses: tuple[str, ...] = ()

    @property
    def hp_ratio(self) -> float | None:
        if self.remaining_hp is None or not self.max_hp:
            return None
        return max(0.0, min(1.0, self.remaining_hp / self.max_hp))


@dataclass(frozen=True, slots=True)
class ReplayTacticPlan:
    opening: str = "unknown"
    midgame: str = "unknown"
    endgame: str = "unknown"
    source: str = "missing"

    @property
    def complete(self) -> bool:
        return all(
            value and value != "unknown"
            for value in (self.opening, self.midgame, self.endgame)
        )

    def as_tuple(self) -> tuple[str, str, str]:
        return self.opening, self.midgame, self.endgame


@dataclass(frozen=True, slots=True)
class ReplayMoment:
    category: str
    tick: int
    kind: str
    summary: str
    actor_pk: int | None = None
    target_pk: int | None = None
    value: int = 0
    skill_id: str = ""
    status_id: str = ""


@dataclass(frozen=True, slots=True)
class ReplaySettlement:
    rated: bool = False
    reward_reason: str = ""
    winner_exp_gain: int = 0
    loser_exp_gain: int = 0
    loser_exp_loss: int = 0
    attacker_exp_delta: int = 0
    defender_exp_delta: int = 0
    attacker_rating_before: float | None = None
    attacker_rating_after: float | None = None
    defender_rating_before: float | None = None
    defender_rating_after: float | None = None

    @property
    def attacker_rating_delta(self) -> float | None:
        if self.attacker_rating_before is None or self.attacker_rating_after is None:
            return None
        return self.attacker_rating_after - self.attacker_rating_before

    @property
    def defender_rating_delta(self) -> float | None:
        if self.defender_rating_before is None or self.defender_rating_after is None:
            return None
        return self.defender_rating_after - self.defender_rating_before


@dataclass(frozen=True, slots=True)
class ReplayRecipe:
    """All persisted inputs needed to attempt a deterministic engine replay."""

    engine_call: str
    ruleset_id: str
    engine_version: str
    random_seed: int | None
    environment_id: str
    attacker_snapshot: Mapping[str, Any] = field(default_factory=dict)
    defender_snapshot: Mapping[str, Any] = field(default_factory=dict)
    attacker_tactic_plan: ReplayTacticPlan = field(default_factory=ReplayTacticPlan)
    defender_tactic_plan: ReplayTacticPlan = field(default_factory=ReplayTacticPlan)
    audit_complete: bool = False
    reproducible: bool = False
    missing_inputs: tuple[str, ...] = ()
    execution_blockers: tuple[str, ...] = ()
    command_info: str = ""


@dataclass(frozen=True, slots=True)
class ReplayView:
    battle_id: int
    group_id: str
    created_at: str
    attacker: ReplayFighter
    defender: ReplayFighter
    winner_pk: int
    loser_pk: int
    ruleset_id: str
    engine_version: str
    random_seed: int | None
    environment_id: str
    duration_ticks: int
    finish_reason: str
    attacker_tactic_plan: ReplayTacticPlan
    defender_tactic_plan: ReplayTacticPlan
    turning_points: tuple[ReplayMoment, ...]
    settlement: ReplaySettlement
    recipe: ReplayRecipe
    compatibility_notes: tuple[str, ...] = ()

    @property
    def winner(self) -> ReplayFighter:
        if self.defender.user_pk == self.winner_pk:
            return self.defender
        return self.attacker

    @property
    def loser(self) -> ReplayFighter:
        if self.attacker.user_pk == self.loser_pk:
            return self.attacker
        return self.defender

    @property
    def seed(self) -> int | None:
        """Short compatibility alias for command/operation callers."""

        return self.random_seed

    @property
    def reproduction(self) -> ReplayRecipe:
        """Readable alias used by command adapters."""

        return self.recipe

    @property
    def critical_moments(self) -> tuple[ReplayMoment, ...]:
        return self.turning_points

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "ReplayFighter",
    "ReplayMoment",
    "ReplayRecipe",
    "ReplaySettlement",
    "ReplayTacticPlan",
    "ReplayView",
]
