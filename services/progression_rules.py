"""Central progression math for attributes, skills, and spells.

Experience is stored as fixed-point integers.  ``EXP_SCALE`` internal points
represent one legacy/user-facing experience point, which lets 1% potential
keep making progress without a separate fractional column.
"""

import math


EXP_SCALE = 100
MIN_POTENTIAL = 1
MAX_POTENTIAL = 400
MIN_SKILL_POTENTIAL = 50
MAX_SKILL_POTENTIAL = 200
LEGACY_RULESET_ID = "legacy-linear-v1"
RULESET_ID = "elona-scaled-v2"
LEVEL_RULESET_ID = "qq-daily-budget-v11"
SKILL_RULESET_ID = "elona-skill-potential-v11"


def level_daily_exp_budget(level: int) -> int:
    """Total character XP an active player is expected to earn in one day.

    Check-in only grants a share of this budget.  PvP, adventures, and daily
    objectives can consume the rest without coupling every reward to the size
    of the current experience bar.
    """
    value = min(100, max(1, int(level)))
    return 100 + 6 * value


def target_days_for_next_level(level: int) -> float:
    """Pacing target for the level beginning at ``level``.

    Levels 1-9 form a seven-day tutorial arc.  The later segments deliberately
    become broader: 2-4, 4-8, then 8-15 active days per level.  The visible
    break at level 10 is a chapter boundary rather than an exponential wall.
    """
    value = min(99, max(1, int(level)))
    if value <= 9:
        # Arithmetic series: sum(target_days(1..9)) == 7 days.
        return 0.5 + (value - 1) * (2.5 / 36)
    if value <= 29:
        return 2.0 + (value - 10) * (2.0 / 19)
    if value <= 59:
        return 4.0 + (value - 30) * (4.0 / 29)
    return 8.0 + (value - 60) * (7.0 / 39)


def level_exp_required(level: int) -> int:
    """XP for the next level under the v11 daily-budget progression curve."""
    return round_half_up(
        level_daily_exp_budget(level) * target_days_for_next_level(level)
    )


def legacy_level_exp_required(level: int) -> int:
    """The pre-v11 exponential level curve, kept only for pure migration."""
    value = max(1, int(level))
    return math.floor(100 * (1.18 ** (value - 1)))


def character_catchup_multiplier(
    level: int,
    reference_level: int | None,
    maximum: float = 1.60,
) -> float:
    """Soft catch-up against the active group median, never a hard level skip."""
    own = max(1, int(level))
    reference = own if reference_level is None else max(1, int(reference_level))
    if reference <= own:
        return 1.0
    return min(float(maximum), math.sqrt(reference / own))


def attribute_exp_required(attribute_value: int) -> int:
    value = max(0, int(attribute_value))
    return 10000 + 1600 * value + 40 * value * value


def skill_exp_required(level: int) -> int:
    value = max(0, int(level))
    # A battle-trained skill should stay usable after the tutorial instead of
    # inheriting the old highly convex grind.  Internal XP remains fixed-point.
    return 3000 + 300 * value + 8 * value * value


def v10_skill_exp_required(level: int) -> int:
    """The immediately previous skill curve for percentage-safe migration."""
    value = max(0, int(level))
    return 5000 + 1200 * value + 30 * value * value


def spell_exp_required(level: int) -> int:
    value = max(0, int(level))
    return 6000 + 1400 * value + 35 * value * value


def legacy_attribute_exp_required(attribute_value: int) -> int:
    return 100 + max(0, int(attribute_value)) * 20


def legacy_skill_exp_required(level: int) -> int:
    return 50 + max(0, int(level)) * 15


def legacy_spell_exp_required(level: int) -> int:
    return legacy_skill_exp_required(level)


def round_half_up(value: float) -> int:
    return math.floor(float(value) + 0.5)


def clamp_potential(potential: int) -> int:
    return min(MAX_POTENTIAL, max(MIN_POTENTIAL, int(potential)))


def clamp_skill_potential(potential: int) -> int:
    """Return the v11 *effective* skill potential stored/displayed to players."""
    return min(
        MAX_SKILL_POTENTIAL,
        max(MIN_SKILL_POTENTIAL, int(potential)),
    )


def scaled_exp_gain(
    raw_exp: float,
    potential: int,
    efficiency: float = 1.0,
) -> int:
    if raw_exp <= 0 or efficiency <= 0:
        return 0
    return max(
        1,
        round_half_up(
            float(raw_exp)
            * EXP_SCALE
            * clamp_potential(potential)
            / 100
            * float(efficiency)
        ),
    )


def scaled_skill_exp_gain(
    raw_exp: float,
    potential: int,
    efficiency: float = 1.0,
) -> int:
    """Fixed-point skill XP using the bounded 50%-200% potential band."""
    if raw_exp <= 0 or efficiency <= 0:
        return 0
    return max(
        1,
        round_half_up(
            float(raw_exp)
            * EXP_SCALE
            * clamp_skill_potential(potential)
            / 100
            * float(efficiency)
        ),
    )


def display_exp(internal_exp: int) -> int:
    """Return a stable legacy-scale value for public result objects."""
    if internal_exp <= 0:
        return 0
    return max(1, round_half_up(int(internal_exp) / EXP_SCALE))


def progress_percent(exp: int, required: int) -> float:
    if required <= 0:
        return 0.0
    return max(0.0, min(100.0, int(exp) * 100 / int(required)))


def decay_skill_potential(potential: int) -> int:
    return max(
        MIN_SKILL_POTENTIAL,
        math.floor(clamp_skill_potential(potential) * 0.92),
    )


def decay_spell_potential(potential: int) -> int:
    """Decay spell potential without collapsing its 50%-400% book domain."""

    return max(MIN_POTENTIAL, math.floor(clamp_potential(potential) * 0.96))


def decay_attribute_potential(potential: int) -> int:
    return max(MIN_POTENTIAL, math.floor(clamp_potential(potential) * 0.96))


def potential_recovery_per_point(potential: int) -> int:
    current = clamp_potential(potential)
    return max(4, round_half_up(30 - current / 8))


def recover_potential(potential: int, multiplier: float = 1.0) -> int:
    current = clamp_potential(potential)
    gain = max(
        1,
        round_half_up(potential_recovery_per_point(current) * multiplier),
    )
    return min(MAX_POTENTIAL, current + gain)


def skill_potential_recovery_per_point(potential: int) -> int:
    """Skill-point training is meaningful even near the top of the band."""
    current = clamp_skill_potential(potential)
    return max(
        10,
        round_half_up((MAX_SKILL_POTENTIAL - current) * 0.25),
    )


def recover_skill_potential(
    potential: int,
    multiplier: float = 1.0,
) -> int:
    current = clamp_skill_potential(potential)
    gain = max(
        1,
        round_half_up(
            skill_potential_recovery_per_point(current) * multiplier
        ),
    )
    return min(MAX_SKILL_POTENTIAL, current + gain)


def migrate_exp_preserving_progress(
    exp: int,
    old_required: int,
    new_required: int,
) -> int:
    if old_required <= 0 or new_required <= 0:
        return 0
    bounded_old = max(0, min(int(exp), int(old_required) - 1))
    converted = round_half_up(bounded_old * int(new_required) / int(old_required))
    return max(0, min(converted, int(new_required) - 1))


def migrate_level_exp_preserving_progress(level: int, exp: int) -> int:
    """Pure legacy-v10 -> v11 conversion for one user's current level bar.

    This intentionally performs no database writes.  A migration runner can
    call it transactionally and preserve the exact level while carrying the
    same percentage of progress into the new curve.
    """
    return migrate_exp_preserving_progress(
        exp,
        legacy_level_exp_required(level),
        level_exp_required(level),
    )


def migrate_v10_skill_exp_preserving_progress(level: int, exp: int) -> int:
    """Pure v10 -> v11 conversion for a learned skill's current level bar."""
    return migrate_exp_preserving_progress(
        exp,
        v10_skill_exp_required(level),
        skill_exp_required(level),
    )


def skill_level_cap(
    magic: int,
    governing_value: int,
    maximum: int = 100,
) -> int:
    return min(
        int(maximum),
        max(
            1,
            math.floor(
                34
                + max(0, int(magic)) * 0.21
                + max(0, int(governing_value)) * 0.125
            ),
        ),
    )


def spell_level_cap(school_level: int, maximum: int = 100) -> int:
    return min(int(maximum), max(50, max(0, int(school_level))))
