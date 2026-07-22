from dataclasses import dataclass, field

try:
    from .skill_catalog import SKILL_DEFINITIONS
except ImportError:
    from services.skill_catalog import SKILL_DEFINITIONS


@dataclass
class PassiveBonuses:
    attack_power: float = 0.0
    accuracy: float = 0.0
    defense: float = 0.0
    evasion: float = 0.0
    critical_rate: float = 0.0
    critical_damage: float = 0.0
    physical_damage_bonus: float = 0.0
    physical_reduction: float = 0.0
    block_rate: float = 0.0
    knockback_resistance: float = 0.0
    carry_capacity: float = 0.0
    hp_regen_per_tick: float = 0.0
    mp_regen_per_tick: float = 0.0
    healing_power_bonus: float = 0.0
    summon_power_bonus: float = 0.0
    blessing_power_bonus: float = 0.0
    reading_success: float = 0.0
    magic_potential_bonus: float = 0.0
    mana_overcast_reduction: float = 0.0
    pve_stealth: float = 0.0
    spell_bonuses: dict[str, float] = field(default_factory=dict)
    style_multiplier: float = 1.0


SPELL_EFFECT_PREFIX = "spell_"


def resolve_passive_bonuses(
    effective_levels: dict[str, int],
    equipment,
) -> PassiveBonuses:
    """Resolve learned passive levels against one immutable equipment build."""
    result = PassiveBonuses()
    if equipment.weapon_mode == "dual_wield":
        result.style_multiplier = max(
            0.65,
            0.80 - max(0.0, equipment.weapon_weight - 6.0) * 0.01,
        )
    elif equipment.weapon_mode in {
        "one_hand", "two_hand_melee", "two_hand_heavy",
    }:
        result.style_multiplier = 0.80
    for skill_id, raw_level in effective_levels.items():
        definition = SKILL_DEFINITIONS.get(skill_id)
        if not definition or not definition.passive:
            continue
        level = max(0, min(150, int(raw_level)))
        for effect in definition.effects:
            if effect.weapon_types and equipment.weapon_type not in effect.weapon_types:
                continue
            if effect.weapon_modes and equipment.weapon_mode not in effect.weapon_modes:
                continue
            if effect.armor_styles and equipment.armor_style not in effect.armor_styles:
                continue
            bonus = level * effect.per_level
            if effect.max_bonus is not None:
                bonus = min(effect.max_bonus, bonus)
            if effect.effect_id.startswith(SPELL_EFFECT_PREFIX):
                school = effect.effect_id[len(SPELL_EFFECT_PREFIX):]
                result.spell_bonuses[school] = result.spell_bonuses.get(school, 0.0) + bonus
            elif effect.effect_id == "dual_wield_style":
                weight_penalty = max(0.0, equipment.weapon_weight - 6.0) * 0.01
                result.style_multiplier = max(
                    0.65,
                    min(1.0, 0.80 + bonus - weight_penalty),
                )
            elif effect.effect_id == "two_handed_style":
                result.style_multiplier = min(1.10, 0.80 + bonus)
            else:
                setattr(
                    result,
                    effect.effect_id,
                    getattr(result, effect.effect_id) + bonus,
                )
    return result
