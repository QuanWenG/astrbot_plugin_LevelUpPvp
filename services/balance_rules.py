"""Elona Mobile-inspired combat curves scaled for this project's 1-100 stats.

The source game uses much larger values.  These functions preserve the shape and
attribute responsibilities while normalising stat 20 as the project's baseline.
They are intentionally pure so every combat entry point uses identical maths.
"""

from __future__ import annotations

import math
from collections.abc import Mapping


PHYSICAL_DAMAGE_MIN = 1
PHYSICAL_MULTIPLIER_MIN = 0.65
PHYSICAL_MULTIPLIER_MAX = 3.0
SPELL_MULTIPLIER_MIN = 0.65
SPELL_MULTIPLIER_MAX = 3.0
REFERENCE_ATTRIBUTE = 20.0


def clamp(minimum: float, maximum: float, value: float) -> float:
    return max(minimum, min(maximum, value))


def primary_curve(value: float) -> float:
    value = max(1.0, float(value))
    return value * 0.86 + math.sqrt(value) * 8.35


def mode_curve(value: float) -> float:
    value = max(1.0, float(value))
    return value * 0.44 + math.sqrt(value) * 5.0


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
    mode_bonus = 0.25 * (
        mode_curve(mode_attribute) - mode_curve(1)
    ) / mode_curve(REFERENCE_ATTRIBUTE)
    weight_bonus = min(0.30, max(0.0, float(weapon_weight)) * 0.03)
    value = (
        primary_attribute_factor(weapon_primary)
        + max(0.0, mode_bonus)
        + max(0, int(combat_skill_level)) * 0.006
        + max(0, int(weapon_skill_level)) * 0.002
        + weight_bonus
    )
    return clamp(
        PHYSICAL_MULTIPLIER_MIN,
        PHYSICAL_MULTIPLIER_MAX,
        value * max(0.1, float(style_multiplier)),
    )


def physical_defense_multiplier(defense: float, attacker_level: int) -> float:
    k = 50.0 + 5.0 * max(1, int(attacker_level))
    return k / (k + max(0.0, float(defense)))


def physical_damage_amount(
    *,
    attack_power: float,
    offense_multiplier: float,
    effect_multiplier: float,
    variance: float,
    defense: float,
    attacker_level: int,
    physical_reduction: float,
) -> int:
    reduction = clamp(0.0, 0.75, physical_reduction)
    amount = (
        max(1.0, attack_power)
        * max(PHYSICAL_MULTIPLIER_MIN, offense_multiplier)
        * max(0.0, effect_multiplier)
        * max(0.0, variance)
        * physical_defense_multiplier(defense, attacker_level)
        * (1.0 - reduction)
    )
    return max(PHYSICAL_DAMAGE_MIN, round(amount))


def hit_chance(accuracy: float, evasion: float, *, is_spell: bool) -> float:
    accuracy = max(0.0, float(accuracy))
    evasion_weight = 0.7 if is_spell else 1.0
    raw = (accuracy + 50.0) / (
        accuracy + max(0.0, float(evasion)) * evasion_weight + 50.0
    )
    return clamp(0.55 if is_spell else 0.60, 0.98, raw)


def resistance_multiplier(resistance: float, attacker_level: int) -> float:
    k = max(5.0, 5.0 * max(1, int(attacker_level)))
    effective = max(-0.5 * k, float(resistance))
    return clamp(0.20, 1.50, k / (k + effective))


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
        + max(0, int(school_level)) * 0.004
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
) -> int:
    amount = (
        max(1.0, base_power)
        * max(0.0, effect_multiplier)
        * clamp(SPELL_MULTIPLIER_MIN, SPELL_MULTIPLIER_MAX, spell_multiplier)
        * max(0.0, variance)
        * resistance_multiplier(resistance, attacker_level)
        * (1.0 - clamp(0.0, 0.75, magical_reduction))
    )
    return max(1, round(amount))
