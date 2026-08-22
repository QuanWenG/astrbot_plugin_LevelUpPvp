"""Pure v11 tactic rules for phased combat.

The old battle system exposed eighteen strategy names whose effects were
scattered across configuration, AI profiles and pre-battle win-rate maths.
This module gives those names one small, deterministic rules vocabulary:

* six tactic families with an explicit counter matrix;
* an opening/midgame/endgame plan;
* bounded build fit and tactical edge calculations;
* bounded decision modifiers which never multiply final damage.

It intentionally knows nothing about the combat engine.  An engine adapter may
translate :class:`PhaseTacticGain` into AI utility, guard probability, action
initiative and counter stamina cost without making this module stateful.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class CombatPhase(str, Enum):
    """The three tactical chapters of one battle."""

    OPENING = "opening"
    MIDGAME = "midgame"
    ENDGAME = "endgame"


class TacticFamily(str, Enum):
    """A compact behavior family used by plans and the simulation adapter."""

    PRESSURE = "pressure"
    COUNTER = "counter"
    SKIRMISH = "skirmish"
    CONTROL = "control"
    SUSTAIN = "sustain"
    GAMBIT = "gambit"


FAMILY_LABELS = MappingProxyType(
    {
        TacticFamily.PRESSURE: "压制",
        TacticFamily.COUNTER: "反制",
        TacticFamily.SKIRMISH: "游击",
        TacticFamily.CONTROL: "控制",
        TacticFamily.SUSTAIN: "坚守",
        TacticFamily.GAMBIT: "奇策",
    }
)

PHASE_LABELS = MappingProxyType(
    {
        CombatPhase.OPENING: "开局",
        CombatPhase.MIDGAME: "中盘",
        CombatPhase.ENDGAME: "终局",
    }
)


# Every built-in v10 name appears exactly once.  Custom/free-text strategies
# safely migrate to SUSTAIN unless the caller asks for strict validation.
LEGACY_STRATEGY_FAMILIES = MappingProxyType(
    {
        "稳扎稳打": TacticFamily.SUSTAIN,
        "全力猛攻": TacticFamily.PRESSURE,
        "防守反击": TacticFamily.COUNTER,
        "游走消耗": TacticFamily.SKIRMISH,
        "先手压制": TacticFamily.PRESSURE,
        "诱敌深入": TacticFamily.COUNTER,
        "持久消耗": TacticFamily.SUSTAIN,
        "奇袭爆发": TacticFamily.GAMBIT,
        "以守为攻": TacticFamily.COUNTER,
        "速度拉扯": TacticFamily.SKIRMISH,
        "幸运赌局": TacticFamily.GAMBIT,
        "破防强攻": TacticFamily.PRESSURE,
        "闪避拖延": TacticFamily.SKIRMISH,
        "控制节奏": TacticFamily.CONTROL,
        "血量压制": TacticFamily.PRESSURE,
        "精准打击": TacticFamily.CONTROL,
        "扰乱节奏": TacticFamily.CONTROL,
        "背水一战": TacticFamily.GAMBIT,
    }
)


def _family(value: TacticFamily | str) -> TacticFamily:
    if isinstance(value, TacticFamily):
        return value
    text = str(value).strip().lower()
    for family, label in FAMILY_LABELS.items():
        if text in {family.value, family.name.lower(), label}:
            return family
    raise ValueError(f"未知战术族：{value}")


def _phase(value: CombatPhase | str) -> CombatPhase:
    if isinstance(value, CombatPhase):
        return value
    text = str(value).strip().lower()
    for phase, label in PHASE_LABELS.items():
        if text in {phase.value, phase.name.lower(), label}:
            return phase
    raise ValueError(f"未知战斗阶段：{value}")


def family_for_legacy_strategy(
    strategy: str,
    *,
    default: TacticFamily | str = TacticFamily.SUSTAIN,
    strict: bool = False,
) -> TacticFamily:
    """Return the v11 family for a v10 strategy name.

    Free-text tactics existed before v11.  They use the neutral, readable
    SUSTAIN fallback during migration; admin/import tools can pass ``strict``
    to surface unknown stored values instead.
    """

    family = LEGACY_STRATEGY_FAMILIES.get(str(strategy).strip())
    if family is not None:
        return family
    if strict:
        raise ValueError(f"无法迁移旧策略：{strategy}")
    return _family(default)


@dataclass(frozen=True, slots=True)
class TacticPlan:
    """The family selected for each battle phase."""

    opening: TacticFamily
    midgame: TacticFamily
    endgame: TacticFamily

    def __post_init__(self) -> None:
        object.__setattr__(self, "opening", _family(self.opening))
        object.__setattr__(self, "midgame", _family(self.midgame))
        object.__setattr__(self, "endgame", _family(self.endgame))

    def for_phase(self, phase: CombatPhase | str) -> TacticFamily:
        phase = _phase(phase)
        return {
            CombatPhase.OPENING: self.opening,
            CombatPhase.MIDGAME: self.midgame,
            CombatPhase.ENDGAME: self.endgame,
        }[phase]

    @classmethod
    def from_legacy(
        cls,
        strategy: str,
        *,
        default: TacticFamily | str = TacticFamily.SUSTAIN,
        strict: bool = False,
    ) -> "TacticPlan":
        """Migrate one old strategy without silently changing it by phase."""

        family = family_for_legacy_strategy(
            strategy,
            default=default,
            strict=strict,
        )
        return cls(family, family, family)


@dataclass(frozen=True, slots=True)
class PhaseThresholds:
    opening_ticks: int = 30
    endgame_tick: int = 100
    endgame_hp_ratio: float = 0.45

    def __post_init__(self) -> None:
        if self.opening_ticks < 0:
            raise ValueError("opening_ticks 不能小于 0")
        if self.endgame_tick <= self.opening_ticks:
            raise ValueError("endgame_tick 必须晚于开局阶段")
        if not 0.0 <= self.endgame_hp_ratio <= 1.0:
            raise ValueError("endgame_hp_ratio 必须在 0 到 1 之间")


DEFAULT_PHASE_THRESHOLDS = PhaseThresholds()


def phase_for_state(
    tick: int,
    own_hp_ratio: float,
    opponent_hp_ratio: float,
    thresholds: PhaseThresholds = DEFAULT_PHASE_THRESHOLDS,
) -> CombatPhase:
    """Resolve a phase with a guaranteed 30-tick readable opening.

    After the opening, low HP immediately starts the endgame.  Tick 101 is the
    hard endgame fallback, preventing two sustain builds from staying in the
    midgame forever.
    """

    tick = max(0, int(tick))
    if tick <= thresholds.opening_ticks:
        return CombatPhase.OPENING
    if (
        tick > thresholds.endgame_tick
        or min(float(own_hp_ratio), float(opponent_hp_ratio))
        <= thresholds.endgame_hp_ratio
    ):
        return CombatPhase.ENDGAME
    return CombatPhase.MIDGAME


# Design-consistent six-family matrix.  Each family has two favorable, two
# unfavorable and one neutral non-mirror matchup.  ``matrix[a][b]`` is from a's
# perspective and the matrix is deliberately antisymmetric.
_WINS = {
    TacticFamily.PRESSURE: {TacticFamily.SUSTAIN, TacticFamily.GAMBIT},
    TacticFamily.COUNTER: {TacticFamily.PRESSURE, TacticFamily.GAMBIT},
    TacticFamily.SKIRMISH: {TacticFamily.COUNTER, TacticFamily.SUSTAIN},
    TacticFamily.CONTROL: {TacticFamily.PRESSURE, TacticFamily.SKIRMISH},
    TacticFamily.SUSTAIN: {TacticFamily.COUNTER, TacticFamily.CONTROL},
    TacticFamily.GAMBIT: {TacticFamily.SKIRMISH, TacticFamily.CONTROL},
}


def _make_counter_matrix() -> Mapping[TacticFamily, Mapping[TacticFamily, int]]:
    rows: dict[TacticFamily, Mapping[TacticFamily, int]] = {}
    for own in TacticFamily:
        row: dict[TacticFamily, int] = {}
        for opponent in TacticFamily:
            if opponent in _WINS[own]:
                value = 1
            elif own in _WINS[opponent]:
                value = -1
            else:
                value = 0
            row[opponent] = value
        rows[own] = MappingProxyType(row)
    return MappingProxyType(rows)


COUNTER_MATRIX = _make_counter_matrix()


def counter_value(
    own: TacticFamily | str,
    opponent: TacticFamily | str,
) -> int:
    """Return exactly -1, 0 or +1 from ``own``'s perspective."""

    return COUNTER_MATRIX[_family(own)][_family(opponent)]


@dataclass(frozen=True, slots=True)
class BuildSignals:
    """Normalized build traits used only for tactic compatibility.

    Every value is interpreted on a 0..1 scale.  ``0.5`` is deliberately
    neutral, so incomplete or legacy builds do not receive a hidden penalty.
    """

    burst: float = 0.5
    retaliation: float = 0.5
    mobility: float = 0.5
    disruption: float = 0.5
    endurance: float = 0.5
    variance: float = 0.5

    @classmethod
    def from_mapping(cls, values: Mapping[str, float]) -> "BuildSignals":
        return cls(
            burst=float(values.get("burst", 0.5)),
            retaliation=float(values.get("retaliation", 0.5)),
            mobility=float(values.get("mobility", 0.5)),
            disruption=float(values.get("disruption", 0.5)),
            endurance=float(values.get("endurance", 0.5)),
            variance=float(values.get("variance", 0.5)),
        )

    def normalized(self) -> Mapping[str, float]:
        return {
            name: max(0.0, min(1.0, float(getattr(self, name))))
            for name in (
                "burst",
                "retaliation",
                "mobility",
                "disruption",
                "endurance",
                "variance",
            )
        }


_FIT_WEIGHTS = {
    TacticFamily.PRESSURE: {
        "burst": 1.00,
        "mobility": 0.25,
        "endurance": -0.25,
        "variance": 0.10,
    },
    TacticFamily.COUNTER: {
        "retaliation": 1.00,
        "endurance": 0.25,
        "mobility": -0.15,
        "burst": -0.10,
    },
    TacticFamily.SKIRMISH: {
        "mobility": 1.00,
        "endurance": 0.15,
        "burst": 0.10,
        "retaliation": -0.15,
    },
    TacticFamily.CONTROL: {
        "disruption": 1.00,
        "endurance": 0.15,
        "burst": -0.10,
        "variance": -0.15,
    },
    TacticFamily.SUSTAIN: {
        "endurance": 1.00,
        "retaliation": 0.15,
        "variance": -0.20,
        "mobility": -0.10,
    },
    TacticFamily.GAMBIT: {
        "variance": 1.00,
        "burst": 0.35,
        "endurance": -0.25,
        "disruption": 0.10,
    },
}

FIT_CAP = 0.45
FIT_SHARPNESS = 1.60


def build_fit(
    family: TacticFamily | str,
    signals: BuildSignals | Mapping[str, float] | None = None,
) -> float:
    """Return bounded build/tactic fit using a smooth ``tanh`` curve.

    A weighted average keeps the result independent of the number of traits in
    one family.  The output is in ``[-FIT_CAP, FIT_CAP]`` and cannot become a
    second raw-stat multiplier.
    """

    family = _family(family)
    if signals is None:
        signals = BuildSignals()
    elif not isinstance(signals, BuildSignals):
        signals = BuildSignals.from_mapping(signals)
    normalized = signals.normalized()
    weights = _FIT_WEIGHTS[family]
    weighted = sum(
        weight * (2.0 * normalized[name] - 1.0)
        for name, weight in weights.items()
    )
    scale = sum(abs(weight) for weight in weights.values())
    raw_fit = weighted / scale if scale else 0.0
    return FIT_CAP * math.tanh(FIT_SHARPNESS * raw_fit)


EDGE_CAP = 0.75
COUNTER_EDGE_WEIGHT = 0.70
FIT_EDGE_WEIGHT = 0.55


def tactical_edge(
    own: TacticFamily | str,
    opponent: TacticFamily | str,
    *,
    own_fit: float = 0.0,
    opponent_fit: float = 0.0,
) -> float:
    """Blend matchup and build fit into one bounded, antisymmetric edge."""

    fit_delta = max(-2 * FIT_CAP, min(2 * FIT_CAP, own_fit - opponent_fit))
    raw_edge = (
        COUNTER_EDGE_WEIGHT * counter_value(own, opponent)
        + FIT_EDGE_WEIGHT * fit_delta
    )
    return EDGE_CAP * math.tanh(raw_edge)


def tactical_edge_for_builds(
    own: TacticFamily | str,
    opponent: TacticFamily | str,
    own_build: BuildSignals | Mapping[str, float] | None = None,
    opponent_build: BuildSignals | Mapping[str, float] | None = None,
) -> float:
    own_family = _family(own)
    opponent_family = _family(opponent)
    return tactical_edge(
        own_family,
        opponent_family,
        own_fit=build_fit(own_family, own_build),
        opponent_fit=build_fit(opponent_family, opponent_build),
    )


@dataclass(frozen=True, slots=True)
class PhaseTacticGain:
    """Bounded inputs for the AI/action economy, never final damage.

    ``utility`` adjusts action scoring, ``guard_logit`` adjusts the log-odds of
    guarding, ``initiative`` adjusts the initiative budget, and
    ``counter_sp_cost`` is an additive multiplier for counter-action SP cost.
    A negative cost value is a discount.
    """

    utility: float = 0.0
    guard_logit: float = 0.0
    initiative: float = 0.0
    counter_sp_cost: float = 0.0


UTILITY_CAP = 0.12
GUARD_LOGIT_CAP = 0.32
INITIATIVE_CAP = 0.10
COUNTER_SP_COST_CAP = 0.18


# The coefficient shape decides *where* an advantage is expressed.  It cannot
# increase the damage result itself.  Coefficients are already within the hard
# caps below; clamping remains a contract for future data edits.
_PHASE_GAIN_COEFFICIENTS = {
    (CombatPhase.OPENING, TacticFamily.PRESSURE): (0.12, -0.08, 0.10, 0.00),
    (CombatPhase.MIDGAME, TacticFamily.PRESSURE): (0.10, -0.06, 0.06, 0.00),
    (CombatPhase.ENDGAME, TacticFamily.PRESSURE): (0.08, -0.04, 0.04, 0.00),
    (CombatPhase.OPENING, TacticFamily.COUNTER): (0.06, 0.28, 0.02, -0.16),
    (CombatPhase.MIDGAME, TacticFamily.COUNTER): (0.08, 0.32, 0.01, -0.18),
    (CombatPhase.ENDGAME, TacticFamily.COUNTER): (0.10, 0.30, 0.00, -0.15),
    (CombatPhase.OPENING, TacticFamily.SKIRMISH): (0.08, 0.02, 0.10, -0.02),
    (CombatPhase.MIDGAME, TacticFamily.SKIRMISH): (0.11, 0.02, 0.08, -0.03),
    (CombatPhase.ENDGAME, TacticFamily.SKIRMISH): (0.09, 0.04, 0.07, -0.02),
    (CombatPhase.OPENING, TacticFamily.CONTROL): (0.10, 0.10, 0.04, -0.04),
    (CombatPhase.MIDGAME, TacticFamily.CONTROL): (0.12, 0.16, 0.03, -0.06),
    (CombatPhase.ENDGAME, TacticFamily.CONTROL): (0.11, 0.14, 0.02, -0.04),
    (CombatPhase.OPENING, TacticFamily.SUSTAIN): (0.05, 0.24, -0.02, -0.03),
    (CombatPhase.MIDGAME, TacticFamily.SUSTAIN): (0.08, 0.28, 0.00, -0.04),
    (CombatPhase.ENDGAME, TacticFamily.SUSTAIN): (0.12, 0.32, 0.02, -0.06),
    (CombatPhase.OPENING, TacticFamily.GAMBIT): (0.12, -0.10, 0.09, 0.02),
    (CombatPhase.MIDGAME, TacticFamily.GAMBIT): (0.10, -0.08, 0.06, 0.01),
    (CombatPhase.ENDGAME, TacticFamily.GAMBIT): (0.12, -0.12, 0.08, 0.02),
}


def _bounded(value: float, cap: float) -> float:
    return max(-cap, min(cap, float(value)))


def phase_gain(
    phase: CombatPhase | str,
    family: TacticFamily | str,
    edge: float,
    *,
    utility_cap: float = UTILITY_CAP,
    guard_logit_cap: float = GUARD_LOGIT_CAP,
    initiative_cap: float = INITIATIVE_CAP,
    counter_sp_cost_cap: float = COUNTER_SP_COST_CAP,
) -> PhaseTacticGain:
    """Route a tactical edge into safe phase-specific decision modifiers."""

    phase = _phase(phase)
    family = _family(family)
    edge = max(-EDGE_CAP, min(EDGE_CAP, float(edge)))
    utility, guard, initiative, counter_cost = _PHASE_GAIN_COEFFICIENTS[
        (phase, family)
    ]
    return PhaseTacticGain(
        utility=_bounded(edge * utility, max(0.0, utility_cap)),
        guard_logit=_bounded(
            edge * guard, max(0.0, guard_logit_cap)
        ),
        initiative=_bounded(
            edge * initiative, max(0.0, initiative_cap)
        ),
        counter_sp_cost=_bounded(
            edge * counter_cost,
            max(0.0, counter_sp_cost_cap),
        ),
    )


_WIN_EXPLANATIONS = {
    (TacticFamily.PRESSURE, TacticFamily.SUSTAIN): "抢在防线成形前连续施压",
    (TacticFamily.PRESSURE, TacticFamily.GAMBIT): "打断高风险招式的准备窗口",
    (TacticFamily.COUNTER, TacticFamily.PRESSURE): "守住主动进攻并利用暴露的破绽",
    (TacticFamily.COUNTER, TacticFamily.GAMBIT): "识破蓄势明显的高风险动作",
    (TacticFamily.SKIRMISH, TacticFamily.COUNTER): "用走位拒绝正面交换，让反制落空",
    (TacticFamily.SKIRMISH, TacticFamily.SUSTAIN): "用持续换位绕开厚重防线",
    (TacticFamily.CONTROL, TacticFamily.PRESSURE): "封锁连续进攻所需的行动窗口",
    (TacticFamily.CONTROL, TacticFamily.SKIRMISH): "限制活动空间，使拉扯难以展开",
    (TacticFamily.SUSTAIN, TacticFamily.COUNTER): "不急于出手，让等待反击的一方失去目标",
    (TacticFamily.SUSTAIN, TacticFamily.CONTROL): "依靠恢复与韧性熬过控制周期",
    (TacticFamily.GAMBIT, TacticFamily.SKIRMISH): "用非常规爆发抓住拉扯的节奏空隙",
    (TacticFamily.GAMBIT, TacticFamily.CONTROL): "用不可预测的变招打乱控制次序",
}


def explain_counter(
    own: TacticFamily | str,
    opponent: TacticFamily | str,
) -> str:
    """Return a short player-facing Chinese explanation of the matchup."""

    own = _family(own)
    opponent = _family(opponent)
    value = counter_value(own, opponent)
    own_label = FAMILY_LABELS[own]
    opponent_label = FAMILY_LABELS[opponent]
    if value > 0:
        reason = _WIN_EXPLANATIONS[(own, opponent)]
        return f"{own_label}克制{opponent_label}：{reason}。"
    if value < 0:
        reason = _WIN_EXPLANATIONS[(opponent, own)]
        return f"{own_label}被{opponent_label}克制：对方会{reason}。"
    if own == opponent:
        return f"{own_label}对{opponent_label}是同路对局，战术互不克制。"
    return f"{own_label}与{opponent_label}互不克制，胜负更看构筑适配和临场行动。"


@dataclass(frozen=True, slots=True)
class PhaseTacticResolution:
    phase: CombatPhase
    own_family: TacticFamily
    opponent_family: TacticFamily
    matchup: int
    own_fit: float
    opponent_fit: float
    edge: float
    gain: PhaseTacticGain
    explanation: str


def resolve_plan_phase(
    own_plan: TacticPlan,
    opponent_plan: TacticPlan,
    phase: CombatPhase | str,
    own_build: BuildSignals | Mapping[str, float] | None = None,
    opponent_build: BuildSignals | Mapping[str, float] | None = None,
    *,
    utility_cap: float = UTILITY_CAP,
    guard_logit_cap: float = GUARD_LOGIT_CAP,
    initiative_cap: float = INITIATIVE_CAP,
    counter_sp_cost_cap: float = COUNTER_SP_COST_CAP,
) -> PhaseTacticResolution:
    """Convenience result for a future combat-engine adapter."""

    phase = _phase(phase)
    own_family = own_plan.for_phase(phase)
    opponent_family = opponent_plan.for_phase(phase)
    own_fit = build_fit(own_family, own_build)
    opponent_fit = build_fit(opponent_family, opponent_build)
    edge = tactical_edge(
        own_family,
        opponent_family,
        own_fit=own_fit,
        opponent_fit=opponent_fit,
    )
    return PhaseTacticResolution(
        phase=phase,
        own_family=own_family,
        opponent_family=opponent_family,
        matchup=counter_value(own_family, opponent_family),
        own_fit=own_fit,
        opponent_fit=opponent_fit,
        edge=edge,
        gain=phase_gain(
            phase,
            own_family,
            edge,
            utility_cap=utility_cap,
            guard_logit_cap=guard_logit_cap,
            initiative_cap=initiative_cap,
            counter_sp_cost_cap=counter_sp_cost_cap,
        ),
        explanation=explain_counter(own_family, opponent_family),
    )
