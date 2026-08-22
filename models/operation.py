"""Domain models for the deterministic QQ-group operation loop.

The operation layer deliberately describes *intent*.  It never inserts items or
changes a wallet: callers may inspect a :class:`RewardIntent`, then hand it to a
separate settlement service with its reward key as the idempotency boundary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


_SEED_PERSONALIZATION = b"qq-pvp-ops-v11"


def stable_operation_seed(*parts: object) -> int:
    """Hash length-prefixed operation coordinates into a portable unsigned seed."""

    payload = bytearray()
    for part in parts:
        encoded = str(part).encode("utf-8")
        payload.extend(len(encoded).to_bytes(4, "big"))
        payload.extend(encoded)
    return int.from_bytes(
        hashlib.blake2b(
            payload,
            digest_size=8,
            person=_SEED_PERSONALIZATION,
        ).digest(),
        "big",
    )


@dataclass(frozen=True)
class PeriodWindow:
    kind: str
    key: str
    start_at_ts: int
    end_at_ts: int

    def contains(self, timestamp: int) -> bool:
        return self.start_at_ts <= int(timestamp) < self.end_at_ts


@dataclass(frozen=True)
class OperationPeriods:
    daily: PeriodWindow
    weekly: PeriodWindow
    season: PeriodWindow


@dataclass(frozen=True)
class OperationEffect:
    """One bounded modifier; ``cap`` is always displayed beside its magnitude."""

    key: str
    label: str
    magnitude: float
    unit: str
    cap: float
    applies_to: str = "both"

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.label.strip():
            raise ValueError("effect key and label must not be empty")
        if self.cap <= 0:
            raise ValueError("effect cap must be positive")
        if abs(self.magnitude) > self.cap + 1e-12:
            raise ValueError(
                f"effect {self.key} magnitude exceeds its transparent cap"
            )

    @property
    def cap_text(self) -> str:
        if self.unit == "ratio":
            return f"{self.magnitude:+.0%}（上限±{self.cap:.0%}）"
        if self.unit == "percentage_point":
            return f"{self.magnitude:+.0f}个百分点（上限±{self.cap:.0f}）"
        return f"{self.magnitude:+g}（上限±{self.cap:g}）"


@dataclass(frozen=True)
class EnvironmentRule:
    environment_id: str
    name: str
    description: str
    effects: tuple[OperationEffect, ...]


@dataclass(frozen=True)
class RiskChoice:
    choice_id: str
    title: str
    description: str
    risk: str
    reward_multiplier: float
    effects: tuple[OperationEffect, ...] = ()

    def __post_init__(self) -> None:
        # Choice rewards should feel meaningful without making the correct click
        # more important than the player's build.
        if not 0.85 <= self.reward_multiplier <= 1.25:
            raise ValueError("choice reward multiplier must stay within 0.85-1.25")


@dataclass(frozen=True)
class RiskEvent:
    event_id: str
    name: str
    prompt: str
    choices: tuple[RiskChoice, RiskChoice]

    def __post_init__(self) -> None:
        if len(self.choices) != 2:
            raise ValueError("risk events must expose exactly two choices")


@dataclass(frozen=True)
class BossAffix:
    affix_id: str
    name: str
    telegraph: str
    effects: tuple[OperationEffect, ...]


@dataclass(frozen=True)
class BossEncounter:
    boss_id: str
    name: str
    affixes: tuple[BossAffix, ...]


@dataclass(frozen=True)
class DailyNefia:
    group_id: str
    ruleset_id: str
    daily_key: str
    group_seed: int
    environment: EnvironmentRule
    risk_event: RiskEvent
    boss: BossEncounter

    @property
    def nodes(self) -> tuple[object, object, object]:
        """The fixed three-node route: rule, choice, elite/boss."""

        return self.environment, self.risk_event, self.boss

    def drop_seed_for(self, user_pk: int | str) -> int:
        """A retry-invariant per-player loot seed for this shared daily map."""

        return stable_operation_seed(
            self.group_id,
            self.daily_key,
            self.ruleset_id,
            user_pk,
        )


@dataclass(frozen=True)
class OperationTask:
    task_id: str
    name: str
    description: str
    target: int
    event_type: str

    def __post_init__(self) -> None:
        if self.target <= 0:
            raise ValueError("operation task target must be positive")


@dataclass(frozen=True)
class OperationProgress:
    user_pk: int
    group_id: str
    period_kind: str
    period_key: str
    operation_key: str
    progress: int
    target: int
    completed: bool
    claimed: bool
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class ProgressUpdate:
    record: OperationProgress
    applied: bool


@dataclass(frozen=True)
class RewardIntent:
    """A settlement request, not an already granted reward."""

    reward_key: str
    source: str
    reason: str
    experience: int = 0
    scrap: int = 0
    loot_rolls: int = 0
    season_tokens: int = 0
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class OperationRewardDefinition:
    """Canonical contents of one claimable operation reward bundle.

    Keeping the definition beside :class:`RewardIntent` gives both the claim
    producer and the transactional settlement boundary one source of truth.
    The intent is a replayable command, while the claimed progress row is its
    durable authorization.
    """

    period_kind: str
    bundle_key: str
    source: str
    reason: str
    experience: int = 0
    scrap: int = 0
    loot_rolls: int = 0
    season_tokens: int = 0

    def build_intent(
        self,
        *,
        user_pk: int,
        group_id: str,
        period_key: str,
        ruleset_id: str,
    ) -> RewardIntent:
        digest = stable_operation_seed(
            int(user_pk),
            str(group_id),
            str(period_key),
            self.period_kind,
        )
        return RewardIntent(
            reward_key=(
                f"operation:{self.period_kind}:{period_key}:{digest:016x}"
            ),
            source=self.source,
            reason=self.reason,
            experience=self.experience,
            scrap=self.scrap,
            loot_rolls=self.loot_rolls,
            season_tokens=self.season_tokens,
            metadata=(("ruleset_id", str(ruleset_id)),),
        )


OPERATION_REWARD_DEFINITIONS: tuple[OperationRewardDefinition, ...] = (
    OperationRewardDefinition(
        period_kind="daily",
        bundle_key="daily:choice-two",
        source="daily_operation",
        reason="每日3项委托任选2项完成",
        scrap=15,
        loot_rolls=1,
        season_tokens=5,
    ),
    OperationRewardDefinition(
        period_kind="weekly",
        bundle_key="weekly:five-of-seven",
        source="weekly_operation",
        reason="周任务完成5/7即可领取全部奖励",
        scrap=80,
        loot_rolls=2,
        season_tokens=30,
    ),
)


def operation_reward_definition(period_kind: str) -> OperationRewardDefinition:
    requested = str(period_kind).strip()
    for definition in OPERATION_REWARD_DEFINITIONS:
        if definition.period_kind == requested:
            return definition
    raise ValueError("unsupported operation reward period")


def operation_reward_intent(
    *,
    period_kind: str,
    user_pk: int,
    group_id: str,
    period_key: str,
    ruleset_id: str,
) -> RewardIntent:
    """Build the exact stable intent authorized by an operation claim."""

    if isinstance(user_pk, bool) or not isinstance(user_pk, int) or user_pk <= 0:
        raise ValueError("user_pk must be a positive integer")
    for label, value in (
        ("group_id", group_id),
        ("period_key", period_key),
        ("ruleset_id", ruleset_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must not be empty")
    return operation_reward_definition(period_kind).build_intent(
        user_pk=user_pk,
        group_id=group_id,
        period_key=period_key,
        ruleset_id=ruleset_id,
    )


@dataclass(frozen=True)
class ClaimResult:
    eligible: bool
    granted: bool
    already_claimed: bool
    completed_count: int
    required_count: int
    reward_intent: RewardIntent | None = None


@dataclass(frozen=True)
class WeeklySimulationResult:
    accepted: bool
    duplicate: bool
    attempts_used: int
    attempts_limit: int
    submitted_score: int
    best_scores: tuple[int, ...]

    @property
    def scored_total(self) -> int:
        return sum(self.best_scores)


@dataclass(frozen=True)
class SeasonSummary:
    season_id: int
    key: str
    status: str
    day_number: int
    total_days: int = 28
    rating: int | None = None
    games: int = 0
    wins: int = 0
    losses: int = 0


@dataclass(frozen=True)
class OperationTaskState:
    """Player-facing progress for one currently selected rotating task."""

    task_id: str
    progress: int
    target: int
    completed: bool


@dataclass(frozen=True)
class OperationOverview:
    periods: OperationPeriods
    nefia: DailyNefia
    daily_tasks: tuple[OperationTask, ...]
    daily_completed: int
    daily_claimed: bool
    weekly_tasks: tuple[OperationTask, ...]
    weekly_completed: int
    weekly_claimed: bool
    weekly_simulation: WeeklySimulationResult
    season: SeasonSummary
    daily_task_states: tuple[OperationTaskState, ...] = ()
    weekly_task_states: tuple[OperationTaskState, ...] = ()


@dataclass(frozen=True)
class OperationSettlementResult:
    """Concrete result of idempotently settling one reward intent."""

    reward_key: str
    applied: bool
    experience: int = 0
    scrap: int = 0
    season_tokens: int = 0
    equipment: tuple[object, ...] = ()
