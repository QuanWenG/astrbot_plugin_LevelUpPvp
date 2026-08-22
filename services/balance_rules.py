"""Version 11 combat curves for the QQ-group side-view battle system.

The old rules copied Elona's *shape* and then normalised every formula in a
different way.  That made defence and resistance lose value when the attacker
levelled, while accuracy floors erased dodge builds.  V11 uses one small set of
soft-capped curves instead:

* primary stats and mastery have diminishing returns;
* armour and percentage reduction share one mitigation budget;
* resistance is independent from the attacker's level;
* hit chance is a logistic contest rather than a high hard floor;
* speed changes action frequency within an explicit 0.75-1.50 band.

All functions remain pure and keep the v10 call signatures so old snapshots,
ability definitions and replay payloads can be adapted without data loss.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

try:
    from .combat_ruleset import CombatRuleSet, SIDEVIEW_V11_RULESET
except ImportError:
    from services.combat_ruleset import CombatRuleSet, SIDEVIEW_V11_RULESET


PHYSICAL_DAMAGE_MIN = 1
PHYSICAL_MULTIPLIER_MIN = 0.65
PHYSICAL_MULTIPLIER_MAX = 2.20
SPELL_MULTIPLIER_MIN = 0.65
SPELL_MULTIPLIER_MAX = 2.20
REFERENCE_ATTRIBUTE = 20.0

# Fixed constants are deliberate.  A point of defence/resistance must not
# become worse merely because the *attacker* gained a level.
ARMOR_K = 100.0
RESISTANCE_K = 150.0
MAX_ARMOR_REDUCTION = 0.72
MAX_VULNERABILITY = 1.35


def _ruleset(ruleset: CombatRuleSet | None) -> CombatRuleSet:
    return ruleset or SIDEVIEW_V11_RULESET


def physical_level_scalar(
    level: float,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Keep equal-level weapon damage abreast of HP and armour growth.

    Reachable v11 characters gain substantially more durability than raw
    attack power between levels 10 and 100.  The old 0.90--1.10 lane therefore
    made late fights end by the tick limit.  This deliberately visible linear
    term is progression pacing, not an attacker-vs-defender level-gap bonus.
    """
    damage = _ruleset(ruleset).damage
    return damage.physical_level_intercept + damage.physical_level_slope * clamp(
        1.0, 100.0, float(level)
    )


def spell_level_scalar(
    level: float,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Give early spells a complete baseline while compressing their late rise.

    Spell base power already grows with spell mastery and casters avoid the
    stamina downtime of weapon users.  A flatter level component prevents that
    second growth source from turning into a late-game multiplier stack.
    """
    damage = _ruleset(ruleset).damage
    return damage.spell_level_intercept + damage.spell_level_slope * clamp(
        1.0, 100.0, float(level)
    )


def spell_invocation_power(
    base_power: float,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Keep novice spells useful without front-loading their whole curve.

    A learned spell still has a dependable floor, while the steeper mastery
    term gives repeated reading and spell use a reason to matter later.
    """
    damage = _ruleset(ruleset).damage
    return damage.spell_invocation_intercept + (
        damage.spell_invocation_slope * max(1.0, float(base_power))
    )


def clamp(minimum: float, maximum: float, value: float) -> float:
    return max(minimum, min(maximum, value))


def sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    forward = math.exp(value)
    return forward / (1.0 + forward)


def logit(probability: float) -> float:
    probability = clamp(0.000_001, 0.999_999, float(probability))
    return math.log(probability / (1.0 - probability))


def effective_stat(value: float) -> float:
    """Compress a project-scale primary stat without invalidating old builds.

    The first 30 points are fully effective, the next 30 retain 65%, and later
    investment retains 35%.  Specialisation still matters but can no longer
    create an exponential gap which no tactical choice can cross.
    """
    value = max(0.0, float(value))
    if value <= 30.0:
        return value
    if value <= 60.0:
        return 30.0 + (value - 30.0) * 0.65
    return 49.5 + (value - 60.0) * 0.35


def mastery_curve(level: float) -> float:
    """Elona-like use-to-grow mastery with strong late diminishing returns."""
    return 10.0 * math.log1p(max(0.0, float(level)) / 10.0)


def primary_curve(value: float) -> float:
    """Compatibility name for callers that need the effective stat curve."""
    return max(1.0, effective_stat(value))


def mode_curve(value: float) -> float:
    # Mode attributes are supporting stats and therefore contribute less.
    return math.sqrt(max(1.0, effective_stat(value)) * REFERENCE_ATTRIBUTE)


def primary_attribute_factor(value: float) -> float:
    return 0.65 + 0.35 * primary_curve(value) / primary_curve(
        REFERENCE_ATTRIBUTE
    )


def secondary_attribute_bonus(value: float, scale: float = 0.20) -> float:
    return scale * (
        primary_curve(value) - primary_curve(1)
    ) / primary_curve(REFERENCE_ATTRIBUTE)


def attack_mode_attribute(
    weapon_mode: str,
    weapon_type: str,
    attributes: Mapping[str, float],
) -> float:
    if weapon_mode in {"two_hand_melee", "two_hand_heavy", "sword_shield"}:
        return float(attributes.get("strength", 1))
    if weapon_mode in {"dual_wield", "unarmed"} or weapon_type == "unarmed":
        return float(attributes.get("dexterity", 1))
    if weapon_mode == "two_hand_ranged" or weapon_type in {
        "bow", "crossbow", "firearm", "throwing",
    }:
        return float(attributes.get("perception", 1))
    return 1.0


def physical_offense_multiplier(
    *,
    weapon_primary: float,
    mode_attribute: float,
    combat_skill_level: int,
    weapon_skill_level: int,
    weapon_weight: float,
    style_multiplier: float = 1.0,
) -> float:
    """Build an offence rating; final damage compresses this rating again.

    Skill levels use a logarithmic mastery curve.  This keeps use-based growth
    satisfying without letting a level-100 skill deal several times the damage
    of an otherwise identical attainable build.
    """
    mode_bonus = 0.16 * (
        mode_curve(mode_attribute) - mode_curve(1)
    ) / mode_curve(REFERENCE_ATTRIBUTE)
    skill_bonus = 0.014 * mastery_curve(combat_skill_level)
    weapon_skill_bonus = 0.009 * mastery_curve(weapon_skill_level)
    weight_bonus = min(0.12, max(0.0, float(weapon_weight)) * 0.015)
    value = (
        primary_attribute_factor(weapon_primary)
        + max(0.0, mode_bonus)
        + skill_bonus
        + weapon_skill_bonus
        + weight_bonus
    )
    return clamp(
        PHYSICAL_MULTIPLIER_MIN,
        PHYSICAL_MULTIPLIER_MAX,
        value * clamp(0.65, 1.15, float(style_multiplier)),
    )


def compressed_offense_multiplier(
    multiplier: float,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Put all pre-computed offence multipliers into one bounded damage lane."""
    multiplier = max(0.0, float(multiplier))
    damage = _ruleset(ruleset).damage
    return 1.0 + damage.offense_compression_amplitude * math.tanh(
        (multiplier - 1.0) / max(0.001, damage.offense_compression_scale)
    )


def _reduction_as_rating(k: float, reduction: float, cap: float) -> float:
    reduction = clamp(0.0, cap, float(reduction))
    if reduction <= 0:
        return 0.0
    return k * reduction / max(0.001, 1.0 - reduction)


def physical_defense_multiplier(
    defense: float,
    attacker_level: int,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Return armour mitigation; ``attacker_level`` is retained for API safety."""
    damage = _ruleset(ruleset).damage
    rating = max(0.0, float(defense))
    anchor = max(
        0.001,
        damage.armor_anchor
        + damage.armor_level_coefficient * max(0, int(attacker_level)),
    )
    return clamp(
        1.0 - damage.total_reduction_cap,
        1.0,
        anchor / (anchor + rating),
    )


def physical_damage_amount(
    *,
    attack_power: float,
    offense_multiplier: float,
    effect_multiplier: float,
    variance: float,
    defense: float,
    attacker_level: int,
    physical_reduction: float,
    ruleset: CombatRuleSet | None = None,
) -> int:
    active = _ruleset(ruleset)
    damage = active.damage
    combined_defense = max(0.0, float(defense)) + _reduction_as_rating(
        damage.armor_anchor,
        physical_reduction,
        damage.reduction_conversion_cap,
    )
    level_scalar = physical_level_scalar(attacker_level, ruleset=active)
    amount = (
        max(1.0, float(attack_power))
        * compressed_offense_multiplier(offense_multiplier, ruleset=active)
        * max(0.0, float(effect_multiplier))
        * clamp(
            damage.effect_variance_floor,
            damage.effect_variance_ceiling,
            float(variance),
        )
        * level_scalar
        * physical_defense_multiplier(
            combined_defense,
            attacker_level,
            ruleset=active,
        )
    )
    return max(damage.minimum_damage, round(amount))


def hit_chance(
    accuracy: float,
    evasion: float,
    *,
    is_spell: bool,
    combat_level: int = 50,
    logit_modifier: float = 0.0,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Resolve accuracy on a logistic curve with room for real dodge builds."""
    hit = _ruleset(ruleset).hit
    accuracy = max(0.0, float(accuracy))
    evasion = max(0.0, float(evasion))
    level = clamp(1.0, 100.0, float(combat_level))
    modifier = clamp(
        -hit.logit_modifier_cap,
        hit.logit_modifier_cap,
        float(logit_modifier),
    )
    if is_spell:
        z = (
            hit.spell_logit_bias
            + (
                accuracy
                - hit.spell_evasion_weight * evasion
                - (hit.spell_offset_base + hit.spell_offset_per_level * level)
            )
            / (hit.spell_scale_base + hit.spell_scale_per_level * level)
            + modifier
        )
        return clamp(hit.spell_floor, hit.spell_ceiling, sigmoid(z))
    z = (
        hit.physical_logit_bias
        + (
            accuracy
            - hit.physical_evasion_weight * evasion
            - (hit.physical_offset_base + hit.physical_offset_per_level * level)
        )
        / (hit.physical_scale_base + hit.physical_scale_per_level * level)
        + modifier
    )
    return clamp(hit.physical_floor, hit.ceiling, sigmoid(z))


def resistance_multiplier(
    resistance: float,
    attacker_level: int,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Elona-shaped fixed resistance curve, independent from attacker level."""
    damage = _ruleset(ruleset).damage
    anchor = max(
        0.001,
        damage.resistance_anchor
        + damage.resistance_level_coefficient * max(0, int(attacker_level)),
    )
    resistance = float(resistance)
    if resistance >= 0:
        return clamp(
            damage.resistance_floor,
            1.0,
            anchor / (anchor + resistance),
        )
    vulnerability = 1.0 + (damage.vulnerability_cap - 1.0) * (
        1.0 - math.exp(-abs(resistance) / anchor)
    )
    return clamp(1.0, damage.vulnerability_cap, vulnerability)


SPELL_ATTRIBUTE_PAIRS = {
    "magic_training": ("magic", "perception"),
    "barrier": ("magic", "perception"),
    "elemental_guidance": ("magic", "perception"),
    "shadow_magic": ("magic", "perception"),
    "natural_knowledge": ("perception", "magic"),
    "blessing": ("willpower", "magic"),
    "restoration": ("willpower", "magic"),
    "necromancy": ("willpower", "magic"),
    "mind_control": ("willpower", "magic"),
}


def spell_attribute_pair(school_id: str) -> tuple[str, str]:
    return SPELL_ATTRIBUTE_PAIRS.get(school_id, ("magic", "perception"))


def spell_power_multiplier(
    *,
    school_id: str,
    school_level: int,
    attributes: Mapping[str, float],
) -> float:
    primary_id, secondary_id = spell_attribute_pair(school_id)
    value = (
        primary_attribute_factor(attributes.get(primary_id, 1))
        + secondary_attribute_bonus(attributes.get(secondary_id, 1))
        + mastery_curve(school_level) * 0.012
    )
    return clamp(SPELL_MULTIPLIER_MIN, SPELL_MULTIPLIER_MAX, value)


def spell_damage_amount(
    *,
    base_power: float,
    effect_multiplier: float,
    spell_multiplier: float,
    variance: float,
    resistance: float,
    attacker_level: int,
    magical_reduction: float,
    ruleset: CombatRuleSet | None = None,
) -> int:
    active = _ruleset(ruleset)
    damage = active.damage
    combined_resistance = float(resistance) + _reduction_as_rating(
        damage.resistance_anchor,
        magical_reduction,
        damage.reduction_conversion_cap,
    )
    level_scalar = spell_level_scalar(attacker_level, ruleset=active)
    amount = (
        spell_invocation_power(base_power, ruleset=active)
        * max(0.0, float(effect_multiplier))
        * compressed_offense_multiplier(spell_multiplier, ruleset=active)
        * clamp(
            damage.effect_variance_floor,
            damage.effect_variance_ceiling,
            float(variance),
        )
        * level_scalar
        * resistance_multiplier(
            combined_resistance,
            attacker_level,
            ruleset=active,
        )
    )
    return max(damage.minimum_damage, round(amount))


def tempo_multiplier(
    speed: float,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Map Elona-style speed to bounded action frequency."""
    tempo_rules = _ruleset(ruleset).tempo
    speed = max(1.0, float(speed))
    tempo = math.exp(
        tempo_rules.speed_curve_strength
        * math.tanh(
            math.log(speed / max(0.001, tempo_rules.speed_reference))
            / max(0.001, tempo_rules.speed_log_divisor)
        )
    )
    return clamp(
        tempo_rules.speed_multiplier_floor,
        tempo_rules.speed_multiplier_ceiling,
        tempo,
    )


def ranged_preferred_range_fraction(
    marksmanship: int,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Let trained ranged users exploit more of their weapon's reach."""

    tempo = _ruleset(ruleset).tempo
    progress = clamp(
        0.0,
        1.0,
        max(0, int(marksmanship))
        / max(1, tempo.ranged_spacing_mastery_cap),
    )
    return tempo.ranged_preferred_range_floor + progress * (
        tempo.ranged_preferred_range_ceiling
        - tempo.ranged_preferred_range_floor
    )


def spell_preferred_range_fraction(
    school_mastery: int,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Keep staff users at a learned spell's range instead of melee range."""

    tempo = _ruleset(ruleset).tempo
    progress = clamp(
        0.0,
        1.0,
        max(0, int(school_mastery))
        / max(1, tempo.spell_spacing_mastery_cap),
    )
    return tempo.spell_preferred_range_floor + progress * (
        tempo.spell_preferred_range_ceiling
        - tempo.spell_preferred_range_floor
    )


def split_arrow_followup_multiplier(
    marksmanship: int,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Scale the single-target echo without front-loading a permanent +35%."""

    damage = _ruleset(ruleset).damage
    progress = clamp(
        0.0,
        1.0,
        max(0, int(marksmanship))
        / max(1, damage.split_followup_mastery_cap),
    )
    return damage.split_followup_floor + progress * (
        damage.split_followup_ceiling - damage.split_followup_floor
    )


def triangular_variance(
    first_roll: float,
    second_roll: float,
    *,
    ruleset: CombatRuleSet | None = None,
) -> float:
    damage = _ruleset(ruleset).damage
    half_range = (damage.variance_high - damage.variance_low) / 2.0
    return damage.variance_low + half_range * (
        clamp(0.0, 1.0, first_roll) + clamp(0.0, 1.0, second_roll)
    )


def status_chance(
    *,
    base_chance: float,
    potency: float,
    tenacity: float,
    combat_level: int,
    hard_control: bool = False,
    ruleset: CombatRuleSet | None = None,
) -> float:
    """Contest status potency and tenacity instead of treating resistance as %."""
    status = _ruleset(ruleset).status
    scale = status.contest_scale_base + status.contest_scale_per_level * clamp(
        1.0, 100.0, float(combat_level)
    )
    chance = sigmoid(
        logit(base_chance) + (float(potency) - float(tenacity)) / scale
    )
    return clamp(
        (
            status.hard_control_chance_floor
            if hard_control
            else status.soft_status_chance_floor
        ),
        (
            status.hard_control_chance_ceiling
            if hard_control
            else status.soft_status_chance_ceiling
        ),
        chance,
    )


def spell_interrupt_damage_threshold(
    *,
    max_hp: int,
    focus: float,
    guarded: bool = False,
    ruleset: CombatRuleSet | None = None,
) -> int:
    """Return the hit size required to break an in-progress spell.

    Magic, willpower and the spell's own school protect against chip damage;
    committed hits still interrupt.  The HP-ratio cap prevents concentration
    from becoming unconditional immunity.
    """

    status = _ruleset(ruleset).status
    ratio = status.spell_interrupt_base_hp_ratio + max(
        0.0,
        float(focus),
    ) * status.spell_interrupt_focus_per_point
    if guarded:
        ratio += status.spell_interrupt_guard_bonus_ratio
    ratio = clamp(0.0, status.spell_interrupt_hp_ratio_cap, ratio)
    return max(0, round(max(0, int(max_hp)) * ratio))


def pvp_burst_cap(
    damage: float,
    target_max_hp: int,
    *,
    ruleset: CombatRuleSet | None = None,
) -> int:
    """Soft-cap one non-execution strike above 45% of maximum HP."""
    damage_rules = _ruleset(ruleset).damage
    maximum = max(1.0, float(target_max_hp))
    ratio = max(0.0, float(damage)) / maximum
    start = damage_rules.burst_soft_cap_ratio
    tail = max(0.001, damage_rules.burst_tail_ratio)
    if ratio > start:
        ratio = start + tail * (1.0 - math.exp(-(ratio - start) / tail))
    return max(0, round(ratio * maximum))


def mana_overcast_within_limit(
    projected_mana: float,
    max_mp: int,
    *,
    ruleset: CombatRuleSet | None = None,
) -> bool:
    """Whether a spell may enter the ruleset's bounded negative-MP pool."""
    resource = _ruleset(ruleset).resource
    if projected_mana >= 0:
        return True
    if not resource.overcast_enabled or max_mp <= 0:
        return False
    return projected_mana >= -resource.overcast_debt_limit_ratio * max_mp


def mana_overcast_backlash(
    *,
    max_hp: int,
    max_mp: int,
    projected_mana: float,
    reduction: float = 0.0,
    ruleset: CombatRuleSet | None = None,
) -> int:
    """Canonical HP price used by both AI planning and combat settlement."""
    if projected_mana >= 0 or max_hp <= 0 or max_mp <= 0:
        return 0
    resource = _ruleset(ruleset).resource
    debt = min(
        resource.overcast_debt_limit_ratio,
        abs(float(projected_mana)) / max(1, max_mp),
    )
    ratio = min(
        resource.overcast_backlash_hp_ratio_cap,
        resource.overcast_backlash_base_ratio
        + resource.overcast_backlash_debt_scale
        * debt ** resource.overcast_backlash_debt_exponent,
    )
    return max(
        1,
        math.ceil(
            max_hp
            * ratio
            * (1.0 - max(0.0, min(1.0, float(reduction))))
        ),
    )
