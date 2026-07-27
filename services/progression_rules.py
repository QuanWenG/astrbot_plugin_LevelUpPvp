"""Central progression math for attributes, skills, and spells.

Experience is stored as fixed-point integers.  ``EXP_SCALE`` internal points
represent one legacy/user-facing experience point, which lets 1% potential
keep making progress without a separate fractional column.
"""

import math


EXP_SCALE = 100
MIN_POTENTIAL = 1
MAX_POTENTIAL = 400
LEGACY_RULESET_ID = "legacy-linear-v1"
RULESET_ID = "elona-scaled-v2"


def attribute_exp_required(attribute_value: int) -> int:
    value = max(0, int(attribute_value))
    return 10000 + 1600 * value + 40 * value * value


def skill_exp_required(level: int) -> int:
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
    return max(MIN_POTENTIAL, math.floor(clamp_potential(potential) * 0.90))


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
