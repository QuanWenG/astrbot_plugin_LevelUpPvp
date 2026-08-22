"""Pure v11 PvP rating and growth-economy decisions.

This module deliberately knows nothing about SQLite or ``BattleService``.  A
caller supplies the already observed daily counters, persists the returned
decision transactionally, and uses the stable keys as idempotency keys.  Keeping
the policy pure makes retries safe and lets a replay explain *why* a duel was a
rated match, a rewarded match, or only a spar.

V11 PvP never transfers experience between players.  The loser receives a
small participation reward and can never lose character experience.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date

try:  # The v11 public name; keep the transitional name import-compatible.
    from services.progression_rules import (
        character_daily_budget as _character_daily_budget,
    )
except ImportError:  # pragma: no cover - exercised only during staged rollout
    from services.progression_rules import (
        level_daily_exp_budget as _character_daily_budget,
    )


ECONOMY_RULESET_ID = "pvp-economy-v11"
GROWTH_OPPONENT_LIMIT = 3


def _round_half_up(value: float) -> int:
    return math.floor(float(value) + 0.5)


def character_daily_budget(level: int) -> int:
    """Return the shared progression budget with a defensive integer boundary."""

    return max(0, int(_character_daily_budget(max(1, int(level)))))


@dataclass(frozen=True)
class RatingDecision:
    """One zero-sum Elo update.

    ``winner_delta`` and ``loser_delta`` are always exact opposites.  A match
    uses the provisional K-factor while either participant is provisional;
    otherwise an established player could exploit low-volatility opponents to
    slow a new account's calibration.
    """

    winner_delta: int
    loser_delta: int
    winner_expected_score: float
    k_factor: int

    def __post_init__(self) -> None:
        if self.winner_delta != -self.loser_delta:
            raise ValueError("Elo deltas must be zero-sum")
        if self.winner_delta < 0:
            raise ValueError("a winner Elo delta cannot be negative")


@dataclass(frozen=True)
class RatingPolicy:
    """Standard zero-sum Elo with a short provisional calibration period."""

    initial_rating: int = 1000
    provisional_games: int = 10
    provisional_k: int = 32
    established_k: int = 24
    rating_scale: float = 400.0
    rated_level_gap_cap: int = 10

    def __post_init__(self) -> None:
        if self.provisional_games < 0:
            raise ValueError("provisional_games cannot be negative")
        if self.provisional_k <= 0 or self.established_k <= 0:
            raise ValueError("K-factors must be positive")
        if self.rating_scale <= 0:
            raise ValueError("rating_scale must be positive")
        if self.rated_level_gap_cap < 0:
            raise ValueError("rated_level_gap_cap cannot be negative")

    def k_factor(self, games_played: int) -> int:
        games = max(0, int(games_played))
        if games < self.provisional_games:
            return self.provisional_k
        return self.established_k

    def expected_score(self, rating: int, opponent_rating: int) -> float:
        exponent = (int(opponent_rating) - int(rating)) / self.rating_scale
        # These limits are already beyond any meaningful Elo distinction and
        # avoid floating-point overflow if imported legacy ratings are corrupt.
        if exponent >= 16.0:
            return 0.0
        if exponent <= -16.0:
            return 1.0
        return 1.0 / (1.0 + 10.0 ** exponent)

    def rate(
        self,
        *,
        winner_rating: int,
        loser_rating: int,
        winner_games_played: int,
        loser_games_played: int,
    ) -> RatingDecision:
        """Return a standard integer Elo result for one decisive duel."""

        expected = self.expected_score(winner_rating, loser_rating)
        match_k = max(
            self.k_factor(winner_games_played),
            self.k_factor(loser_games_played),
        )
        delta = _round_half_up(match_k * (1.0 - expected))
        return RatingDecision(
            winner_delta=delta,
            loser_delta=-delta,
            winner_expected_score=expected,
            k_factor=match_k,
        )


@dataclass(frozen=True)
class RewardContext:
    """All observations needed to decide one PvP settlement.

    Daily counters are values *before* this duel.  ``pair_battles_today`` must
    count the unordered pair, so reversing challenger/defender cannot create a
    second rated match.  Growth-opponent counters count distinct opponents that
    already granted character XP to that participant today.
    """

    group_id: str
    battle_date: str
    winner_id: str
    loser_id: str
    winner_level: int
    loser_level: int
    winner_checkin_days: int
    loser_checkin_days: int
    pair_battles_today: int = 0
    winner_growth_opponents_today: int = 0
    loser_growth_opponents_today: int = 0
    winner_daily_exp_earned: int = 0
    loser_daily_exp_earned: int = 0
    winner_rating: int = 1000
    loser_rating: int = 1000
    winner_games_played: int = 0
    loser_games_played: int = 0
    ruleset_id: str = ECONOMY_RULESET_ID

    def __post_init__(self) -> None:
        for field_name in (
            "group_id",
            "battle_date",
            "winner_id",
            "loser_id",
            "ruleset_id",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must be non-empty")
        try:
            date.fromisoformat(self.battle_date)
        except (TypeError, ValueError) as error:
            raise ValueError("battle_date must be an ISO date (YYYY-MM-DD)") from error
        if self.winner_id == self.loser_id:
            raise ValueError("winner and loser must be different users")
        if self.winner_level < 1 or self.loser_level < 1:
            raise ValueError("levels must be positive")
        for field_name in (
            "winner_checkin_days",
            "loser_checkin_days",
            "pair_battles_today",
            "winner_growth_opponents_today",
            "loser_growth_opponents_today",
            "winner_daily_exp_earned",
            "loser_daily_exp_earned",
            "winner_games_played",
            "loser_games_played",
        ):
            if int(getattr(self, field_name)) < 0:
                raise ValueError(f"{field_name} cannot be negative")


@dataclass(frozen=True)
class RewardDecision:
    """Complete, persistence-ready decision for one duel."""

    rated: bool
    mode: str
    winner_rating_delta: int
    loser_rating_delta: int
    winner_exp_gain: int
    loser_exp_gain: int
    loser_exp_loss: int
    reward_key_parts: tuple[str, ...]
    rating_reward_key: str
    winner_growth_reward_key: str
    loser_growth_reward_key: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mode not in {"rated", "spar"}:
            raise ValueError("mode must be 'rated' or 'spar'")
        if self.rated != (self.mode == "rated"):
            raise ValueError("rated and mode disagree")
        if self.winner_rating_delta != -self.loser_rating_delta:
            raise ValueError("rating deltas must be zero-sum")
        if min(self.winner_exp_gain, self.loser_exp_gain, self.loser_exp_loss) < 0:
            raise ValueError("experience fields cannot be negative")
        if self.loser_exp_loss != 0:
            raise ValueError("v11 PvP never deducts loser experience")

    @property
    def rewarded(self) -> bool:
        return self.winner_exp_gain > 0 or self.loser_exp_gain > 0


def _qualified_for_rated(level: int, checkin_days: int) -> bool:
    # Only an account below both thresholds is restricted to sparring.
    return int(level) >= 5 or int(checkin_days) >= 3


def _level_gap_multiplier(own_level: int, opponent_level: int) -> float:
    """Reward harder opponents modestly, capped to a transparent +/-20%."""

    gap = max(-10, min(10, int(opponent_level) - int(own_level)))
    return 1.0 + gap * 0.02


def _growth_gain(
    *,
    level: int,
    opponent_level: int,
    share: float,
    daily_exp_earned: int,
    growth_opponents_today: int,
) -> tuple[int, str]:
    if int(growth_opponents_today) >= GROWTH_OPPONENT_LIMIT:
        return 0, "distinct_opponent_limit_reached"

    budget = character_daily_budget(level)
    remaining = max(0, budget - int(daily_exp_earned))
    if remaining <= 0:
        return 0, "daily_exp_budget_exhausted"

    proposed = _round_half_up(
        budget
        * float(share)
        * _level_gap_multiplier(level, opponent_level)
    )
    gain = max(0, min(remaining, proposed))
    if gain <= 0:
        return 0, "daily_exp_budget_exhausted"
    if gain < proposed:
        return gain, "growth_granted_budget_capped"
    return gain, "growth_granted"


def _base_reward_key_parts(context: RewardContext) -> tuple[str, ...]:
    first_user, second_user = sorted((context.winner_id, context.loser_id))
    return (
        "pvp",
        context.ruleset_id,
        context.group_id,
        context.battle_date,
        first_user,
        second_user,
    )


def stable_reward_key(parts: tuple[str, ...], purpose: str) -> str:
    """Build a compact deterministic idempotency key from explicit parts."""

    payload = "\x00".join((*parts, str(purpose))).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f"{ECONOMY_RULESET_ID}:{purpose}:{digest}"


def decide_pvp_economy(
    context: RewardContext,
    rating_policy: RatingPolicy | None = None,
) -> RewardDecision:
    """Decide rating, XP, idempotency keys, and human-readable reasons.

    The first duel of an unordered pair on a calendar day can be rated and
    rewarded.  Later duels remain fully playable as spars but cannot mint XP or
    manipulate Elo.  Among first-pair duels, only the first three distinct
    growth opponents for each participant grant character XP.
    """

    policy = rating_policy or RatingPolicy()
    key_parts = _base_reward_key_parts(context)
    rating_key = stable_reward_key(key_parts, "rating")
    winner_growth_key = stable_reward_key(
        (*key_parts, context.winner_id), "growth"
    )
    loser_growth_key = stable_reward_key(
        (*key_parts, context.loser_id), "growth"
    )
    reasons: list[str] = []

    unqualified: list[str] = []
    if not _qualified_for_rated(
        context.winner_level, context.winner_checkin_days
    ):
        unqualified.append("winner")
    if not _qualified_for_rated(
        context.loser_level, context.loser_checkin_days
    ):
        unqualified.append("loser")

    if unqualified:
        reasons.extend(f"{role}_account_not_qualified" for role in unqualified)
        reasons.append("spar_no_rating_or_growth")
        return RewardDecision(
            rated=False,
            mode="spar",
            winner_rating_delta=0,
            loser_rating_delta=0,
            winner_exp_gain=0,
            loser_exp_gain=0,
            loser_exp_loss=0,
            reward_key_parts=key_parts,
            rating_reward_key=rating_key,
            winner_growth_reward_key=winner_growth_key,
            loser_growth_reward_key=loser_growth_key,
            reasons=tuple(reasons),
        )

    level_gap = abs(int(context.winner_level) - int(context.loser_level))
    if level_gap > policy.rated_level_gap_cap:
        reasons.extend(
            (
                "rated_level_gap_exceeded",
                f"level_gap_{level_gap}_cap_{policy.rated_level_gap_cap}",
                "spar_no_rating_or_growth",
            )
        )
        return RewardDecision(
            rated=False,
            mode="spar",
            winner_rating_delta=0,
            loser_rating_delta=0,
            winner_exp_gain=0,
            loser_exp_gain=0,
            loser_exp_loss=0,
            reward_key_parts=key_parts,
            rating_reward_key=rating_key,
            winner_growth_reward_key=winner_growth_key,
            loser_growth_reward_key=loser_growth_key,
            reasons=tuple(reasons),
        )

    if context.pair_battles_today > 0:
        reasons.extend(("repeat_pair_today", "spar_no_rating_or_growth"))
        return RewardDecision(
            rated=False,
            mode="spar",
            winner_rating_delta=0,
            loser_rating_delta=0,
            winner_exp_gain=0,
            loser_exp_gain=0,
            loser_exp_loss=0,
            reward_key_parts=key_parts,
            rating_reward_key=rating_key,
            winner_growth_reward_key=winner_growth_key,
            loser_growth_reward_key=loser_growth_key,
            reasons=tuple(reasons),
        )

    rating = policy.rate(
        winner_rating=context.winner_rating,
        loser_rating=context.loser_rating,
        winner_games_played=context.winner_games_played,
        loser_games_played=context.loser_games_played,
    )
    winner_gain, winner_growth_reason = _growth_gain(
        level=context.winner_level,
        opponent_level=context.loser_level,
        share=0.18,
        daily_exp_earned=context.winner_daily_exp_earned,
        growth_opponents_today=context.winner_growth_opponents_today,
    )
    loser_gain, loser_growth_reason = _growth_gain(
        level=context.loser_level,
        opponent_level=context.winner_level,
        share=0.10,
        daily_exp_earned=context.loser_daily_exp_earned,
        growth_opponents_today=context.loser_growth_opponents_today,
    )
    reasons.extend(
        (
            "first_pair_duel_rated",
            f"winner_{winner_growth_reason}",
            f"loser_{loser_growth_reason}",
            "loser_experience_never_deducted",
        )
    )
    return RewardDecision(
        rated=True,
        mode="rated",
        winner_rating_delta=rating.winner_delta,
        loser_rating_delta=rating.loser_delta,
        winner_exp_gain=winner_gain,
        loser_exp_gain=loser_gain,
        loser_exp_loss=0,
        reward_key_parts=key_parts,
        rating_reward_key=rating_key,
        winner_growth_reward_key=winner_growth_key,
        loser_growth_reward_key=loser_growth_key,
        reasons=tuple(reasons),
    )


__all__ = [
    "ECONOMY_RULESET_ID",
    "GROWTH_OPPONENT_LIMIT",
    "RatingDecision",
    "RatingPolicy",
    "RewardContext",
    "RewardDecision",
    "character_daily_budget",
    "decide_pvp_economy",
    "stable_reward_key",
]
