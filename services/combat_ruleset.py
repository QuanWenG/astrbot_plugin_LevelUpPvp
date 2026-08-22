"""Versioned, immutable rules for deterministic combat simulation.

The combat engine historically owned a large collection of unrelated constants.
This module gives those constants a stable identity and a single vocabulary.  A
saved battle can therefore say *which* rules produced it, instead of depending on
whatever happens to be current when the replay is opened.

Only exact rule-set ids are accepted.  In particular there is deliberately no
``latest`` alias: aliases make old replays and balance reports silently change
meaning after a deployment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping


@dataclass(frozen=True)
class StatRules:
    """Compression and level scaling applied before combat formulas.

    The two soft caps keep late progression useful without allowing one primary
    attribute to invalidate an opponent's whole build.  A value of 90, for
    example, contributes ``60.0`` effective points under the v11 defaults.
    """

    soft_cap_first: float = 30.0
    soft_cap_second: float = 60.0
    first_band_slope: float = 1.0
    second_band_slope: float = 0.65
    final_band_slope: float = 0.35
    level_scale_per_level: float = 0.025
    level_scale_cap: int = 100
    mastery_log_reference: float = 10.0
    mastery_log_scale: float = 10.0


@dataclass(frozen=True)
class HitRules:
    """Readable hit-rate bounds and the opposed-rating curve.

    The low physical floor is only reached by extreme accuracy/evasion gaps;
    ordinary reachable builds remain much closer to the curve's centre.  This
    lets dodge create a real identity without making it absolute, while spells
    retain a higher reliability floor.
    """

    base_chance: float = 0.80
    physical_floor: float = 0.35
    spell_floor: float = 0.50
    ceiling: float = 0.97
    opposed_rating_anchor: float = 100.0
    spell_evasion_weight: float = 0.70
    guaranteed_hit_ceiling: float = 1.0
    physical_logit_bias: float = 1.35
    spell_logit_bias: float = 1.55
    physical_evasion_weight: float = 1.10
    physical_offset_base: float = 45.0
    physical_offset_per_level: float = 0.25
    spell_offset_base: float = 40.0
    spell_offset_per_level: float = 0.20
    physical_scale_base: float = 40.0
    physical_scale_per_level: float = 0.30
    spell_scale_base: float = 38.0
    spell_scale_per_level: float = 0.28
    logit_modifier_cap: float = 0.75
    spell_ceiling: float = 0.985


@dataclass(frozen=True)
class DamageRules:
    """Damage variance, mitigation anchors, and hard safety limits.

    The v11 armor/resistance anchors do not grow with the attacker's level.  Gear
    therefore keeps its defensive meaning instead of becoming obsolete merely
    because the opponent levelled up.  Total mitigation is capped at 72% so a
    defensive build can extend a fight without becoming immortal.
    """

    variance_low: float = 0.88
    variance_high: float = 1.12
    armor_anchor: float = 100.0
    armor_level_coefficient: float = 0.0
    resistance_anchor: float = 150.0
    resistance_level_coefficient: float = 0.0
    total_reduction_cap: float = 0.72
    vulnerability_cap: float = 1.35
    critical_multiplier: float = 1.65
    critical_chance_cap: float = 0.40
    minimum_damage: int = 1
    physical_level_intercept: float = 1.344
    physical_level_slope: float = 0.01232
    spell_level_intercept: float = 1.389
    spell_level_slope: float = 0.00448
    spell_invocation_intercept: float = 1.0
    spell_invocation_slope: float = 1.0
    offense_compression_amplitude: float = 0.45
    offense_compression_scale: float = 0.80
    effect_variance_floor: float = 0.75
    effect_variance_ceiling: float = 1.25
    reduction_conversion_cap: float = 0.55
    resistance_floor: float = 0.20
    burst_soft_cap_ratio: float = 0.45
    burst_tail_ratio: float = 0.18
    split_followup_floor: float = 0.15
    split_followup_ceiling: float = 0.35
    split_followup_mastery_cap: int = 40
    physical_guard_multiplier: float = 0.55
    active_magic_guard_multiplier: float = 0.75


@dataclass(frozen=True)
class TempoRules:
    """Action-speed limits and the three dramatic phases of a duel.

    Speed is bounded to a 0.75x--1.35x action-rate window.  It remains a build
    identity, but it cannot grant enough consecutive actions to remove counterplay.
    """

    base_action_interval_ticks: int = 16
    speed_multiplier_floor: float = 0.75
    speed_multiplier_ceiling: float = 1.35
    opener_last_tick: int = 30
    endgame_force_tick: int = 90
    endgame_hp_ratio: float = 0.45
    target_median_ticks_low: int = 55
    target_median_ticks_high: int = 90
    speed_reference: float = 100.0
    speed_curve_strength: float = 0.60
    speed_log_divisor: float = 0.75
    ranged_preferred_range_floor: float = 0.72
    ranged_preferred_range_ceiling: float = 0.88
    ranged_spacing_mastery_cap: int = 40
    spell_preferred_range_floor: float = 0.68
    spell_preferred_range_ceiling: float = 0.82
    spell_spacing_mastery_cap: int = 40


@dataclass(frozen=True)
class ResourceRules:
    """Starting resources and bounded recovery/overcast behaviour.

    Rated PvP starts from full resources so yesterday's dungeon cannot decide
    today's duel.  Overcasting is allowed as an Elona-like emergency decision,
    while the backlash cap prevents a single accounting edge case from deleting
    more than a quarter of maximum HP.
    """

    rated_start_hp_ratio: float = 1.0
    rated_start_mp_ratio: float = 1.0
    rated_start_stamina_ratio: float = 1.0
    stamina_pool_reference: int = 100
    stamina_restoration_floor: int = 6
    stamina_restoration_ceiling: int = 12
    emergency_hp_ratio: float = 0.35
    overcast_enabled: bool = True
    overcast_hp_per_missing_mp: float = 2.0
    overcast_backlash_hp_ratio_cap: float = 0.25
    overcast_debt_limit_ratio: float = 0.50
    overcast_backlash_base_ratio: float = 0.04
    overcast_backlash_debt_scale: float = 0.20
    overcast_backlash_debt_exponent: float = 1.50


@dataclass(frozen=True)
class StatusRules:
    """Status reliability and anti-lock diminishing returns."""

    chance_floor: float = 0.05
    chance_ceiling: float = 0.85
    hard_control_duration_cap_ticks: int = 4
    repeated_control_multiplier: float = 0.65
    post_control_immunity_ticks: int = 1
    hard_control_chance_floor: float = 0.15
    hard_control_chance_ceiling: float = 0.70
    soft_status_chance_floor: float = 0.20
    soft_status_chance_ceiling: float = 0.95
    contest_scale_base: float = 35.0
    contest_scale_per_level: float = 0.35
    spell_interrupt_base_hp_ratio: float = 0.07
    spell_interrupt_focus_per_point: float = 0.0008
    spell_interrupt_guard_bonus_ratio: float = 0.03
    spell_interrupt_hp_ratio_cap: float = 0.18


@dataclass(frozen=True)
class StrategyRules:
    """Bounded action-economy outputs for the six-family tactic system.

    Tactics never multiply final damage.  They may only shift AI utility, guard
    log-odds, action tempo, and the stamina price of a real counter action.
    """

    utility_cap: float = 0.12
    guard_logit_cap: float = 0.32
    initiative_cap: float = 0.10
    counter_sp_cost_cap: float = 0.18
    counter_stamina_ratio: float = 0.05


@dataclass(frozen=True)
class EnvironmentRules:
    """Boundary between a fair rated duel and persistent PvE state."""

    rated_level_gap_cap: int = 10
    rated_reset_resources: bool = True
    rated_clear_temporary_statuses: bool = True
    rated_persists_combat_state: bool = False
    pve_persists_combat_state: bool = True
    summon_limit_per_side: int = 2
    environmental_damage_hp_ratio_cap: float = 0.15


@dataclass(frozen=True)
class FortuneRules:
    """Luck as capped, visible insurance rather than opaque raw damage.

    Luck 100 grants one fortune charge; 140 grants two and 180 grants the cap of
    three.  A charge may reroll a clearly bad event once, never recursively.
    """

    luck_floor: int = 60
    luck_baseline: int = 100
    luck_ceiling: int = 180
    charges_at_baseline: int = 1
    luck_per_extra_charge: int = 40
    charge_cap: int = 3
    critical_chance_per_luck: float = 0.0005
    critical_bonus_cap: float = 0.04
    severe_miss_hit_chance_threshold: float = 0.75
    severe_status_chance_threshold: float = 0.60
    rerolls_per_event_cap: int = 1
    target_win_rate_shift_cap: float = 0.06


@dataclass(frozen=True)
class TimeoutRules:
    """Finite fights and an explicit quality target for balance reports."""

    hard_tick_limit: int = 160
    sudden_death_start_tick: int = 50
    sudden_death_damage_growth_per_tick: float = 0.07
    sudden_death_damage_growth_cap: float = 2.00
    sudden_death_minimum_hit_ratio: float = 0.04
    sudden_death_minimum_hit_ratio_growth: float = 0.003
    sudden_death_minimum_hit_ratio_cap: float = 0.15
    sudden_death_healing_multiplier: float = 0.20
    resolve_by_normalized_hp: bool = True
    target_timeout_rate: float = 0.03


@dataclass(frozen=True)
class CombatRuleSet:
    """The complete immutable contract needed to reproduce one combat."""

    ruleset_id: str
    display_name: str
    stats: StatRules = field(default_factory=StatRules)
    hit: HitRules = field(default_factory=HitRules)
    damage: DamageRules = field(default_factory=DamageRules)
    tempo: TempoRules = field(default_factory=TempoRules)
    resource: ResourceRules = field(default_factory=ResourceRules)
    status: StatusRules = field(default_factory=StatusRules)
    strategy: StrategyRules = field(default_factory=StrategyRules)
    environment: EnvironmentRules = field(default_factory=EnvironmentRules)
    fortune: FortuneRules = field(default_factory=FortuneRules)
    timeout: TimeoutRules = field(default_factory=TimeoutRules)

    def __post_init__(self) -> None:
        if not self.ruleset_id or self.ruleset_id.strip() != self.ruleset_id:
            raise ValueError("ruleset_id must be a non-empty exact id")
        if self.ruleset_id.casefold() == "latest":
            raise ValueError("'latest' is not a reproducible ruleset id")


def _sideview_v10() -> CombatRuleSet:
    """Describe the legacy constants so old records have an explicit identity."""

    return CombatRuleSet(
        ruleset_id="sideview-v10",
        display_name="Side-view legacy v10",
        stats=StatRules(
            soft_cap_first=1_000_000.0,
            soft_cap_second=2_000_000.0,
            second_band_slope=1.0,
            final_band_slope=1.0,
            level_scale_per_level=0.0,
        ),
        hit=HitRules(
            base_chance=0.50,
            physical_floor=0.60,
            spell_floor=0.55,
            ceiling=0.98,
            opposed_rating_anchor=50.0,
            spell_evasion_weight=0.70,
        ),
        damage=DamageRules(
            variance_low=0.85,
            variance_high=1.15,
            armor_anchor=50.0,
            armor_level_coefficient=5.0,
            resistance_anchor=0.0,
            resistance_level_coefficient=5.0,
            total_reduction_cap=0.75,
            vulnerability_cap=1.50,
            critical_multiplier=1.50,
            critical_chance_cap=0.35,
            split_followup_floor=0.35,
            split_followup_ceiling=0.35,
            active_magic_guard_multiplier=1.0,
        ),
        tempo=TempoRules(
            speed_multiplier_floor=0.50,
            speed_multiplier_ceiling=2.00,
            opener_last_tick=20,
            endgame_force_tick=90,
            target_median_ticks_low=35,
            target_median_ticks_high=100,
            ranged_preferred_range_floor=0.70,
            ranged_preferred_range_ceiling=0.70,
            spell_preferred_range_floor=0.0,
            spell_preferred_range_ceiling=0.0,
        ),
        resource=ResourceRules(overcast_backlash_hp_ratio_cap=1.0),
        status=StatusRules(
            chance_ceiling=0.95,
            hard_control_duration_cap_ticks=6,
            repeated_control_multiplier=1.0,
            post_control_immunity_ticks=0,
            spell_interrupt_base_hp_ratio=0.0,
            spell_interrupt_focus_per_point=0.0,
            spell_interrupt_guard_bonus_ratio=0.0,
            spell_interrupt_hp_ratio_cap=0.0,
        ),
        strategy=StrategyRules(
            utility_cap=0.0,
            guard_logit_cap=0.0,
            initiative_cap=0.0,
            counter_sp_cost_cap=0.0,
            counter_stamina_ratio=0.0,
        ),
        environment=EnvironmentRules(
            rated_level_gap_cap=100,
            rated_reset_resources=False,
            rated_clear_temporary_statuses=False,
            rated_persists_combat_state=True,
        ),
        fortune=FortuneRules(
            charges_at_baseline=0,
            charge_cap=0,
            critical_chance_per_luck=0.0,
            critical_bonus_cap=0.0,
            rerolls_per_event_cap=0,
            target_win_rate_shift_cap=0.0,
        ),
        timeout=TimeoutRules(
            hard_tick_limit=120,
            sudden_death_start_tick=120,
            sudden_death_damage_growth_per_tick=0.0,
            sudden_death_damage_growth_cap=0.0,
            sudden_death_healing_multiplier=1.0,
            target_timeout_rate=0.10,
        ),
    )


SIDEVIEW_V10_RULESET = _sideview_v10()
SIDEVIEW_V11_RULESET = CombatRuleSet(
    ruleset_id="sideview-v11",
    display_name="Elona chat PvP v11",
)


class RuleSetRegistry:
    """Registry requiring exact version ids; no mutable 'current' pointer."""

    _FORBIDDEN_ALIASES = frozenset({"latest"})

    def __init__(
        self,
        rulesets: Iterable[CombatRuleSet] | None = None,
        *,
        include_defaults: bool = True,
    ) -> None:
        self._rulesets: dict[str, CombatRuleSet] = {}
        if include_defaults:
            self.register(SIDEVIEW_V10_RULESET)
            self.register(SIDEVIEW_V11_RULESET)
        if rulesets is not None:
            for ruleset in rulesets:
                self.register(ruleset)

    @classmethod
    def _exact_id(cls, ruleset_id: str) -> str:
        if not isinstance(ruleset_id, str):
            raise TypeError("ruleset_id must be a string")
        if not ruleset_id or ruleset_id.strip() != ruleset_id:
            raise ValueError("ruleset_id must be a non-empty exact id")
        if ruleset_id.casefold() in cls._FORBIDDEN_ALIASES:
            raise ValueError(
                "'latest' is forbidden; persist and require an exact ruleset id"
            )
        return ruleset_id

    def register(
        self,
        ruleset: CombatRuleSet,
        *,
        replace: bool = False,
    ) -> None:
        if not isinstance(ruleset, CombatRuleSet):
            raise TypeError("ruleset must be a CombatRuleSet")
        ruleset_id = self._exact_id(ruleset.ruleset_id)
        if not replace and ruleset_id in self._rulesets:
            raise ValueError(f"ruleset already registered: {ruleset_id}")
        self._rulesets[ruleset_id] = ruleset

    def require(self, ruleset_id: str) -> CombatRuleSet:
        exact_id = self._exact_id(ruleset_id)
        try:
            return self._rulesets[exact_id]
        except KeyError as error:
            known = ", ".join(self.ids()) or "(none)"
            raise KeyError(
                f"unknown ruleset {exact_id!r}; registered: {known}"
            ) from error

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._rulesets))

    def snapshot(self) -> Mapping[str, CombatRuleSet]:
        """Return a read-only copy suitable for diagnostics and admin output."""

        return MappingProxyType(dict(self._rulesets))

    def __contains__(self, ruleset_id: object) -> bool:
        return isinstance(ruleset_id, str) and ruleset_id in self._rulesets


DEFAULT_RULESET_REGISTRY = RuleSetRegistry()


def require_ruleset(ruleset_id: str) -> CombatRuleSet:
    """Resolve a persisted exact id from the process-wide built-in registry."""

    return DEFAULT_RULESET_REGISTRY.require(ruleset_id)
