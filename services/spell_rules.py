from __future__ import annotations

import math
from dataclasses import dataclass

try:
    from .balance_rules import spell_power_multiplier
except ImportError:
    from services.balance_rules import spell_power_multiplier


@dataclass(frozen=True)
class SpellRule:
    reading_difficulty: int
    reading_attribute: str
    base_mana_cost: float
    mana_cost_mode: str = "scaled"


@dataclass(frozen=True)
class ManaCostBreakdown:
    base_cost: float
    spell_level: int
    level_cost: int
    reduction_ratio: float
    final_cost: int
    spell_power: float


SCHOOL_READING_ATTRIBUTES = {
    "magic_training": "magic",
    "barrier": "magic",
    "elemental_guidance": "magic",
    "shadow_magic": "magic",
    "blessing": "willpower",
    "restoration": "willpower",
    "necromancy": "willpower",
    "mind_control": "willpower",
    "natural_knowledge": "perception",
}


def _rule(school: str, difficulty: int, mana: float, mode: str = "scaled") -> SpellRule:
    return SpellRule(
        difficulty,
        SCHOOL_READING_ATTRIBUTES[school],
        mana,
        mode,
    )


SPELL_RULES = {
    # 魔法修行
    "magic_arrow": _rule("magic_training", 120, 6),
    "mana_ray": _rule("magic_training", 950, 72),
    "mana_storm": _rule("magic_training", 1400, 147),
    "mana_scar": _rule("magic_training", 1360, 71),
    "confusion_spell": _rule("magic_training", 300, 15),
    "sage_blessing": _rule("magic_training", 350, 68),
    "insight_spell": _rule("magic_training", 450, 24),
    "blink": _rule("magic_training", 120, 12),
    "teleport": _rule("magic_training", 400, 36),
    # 结界术
    "armor_spell": _rule("barrier", 130, 33),
    "holy_shield": _rule("barrier", 150, 39),
    "mana_barrier": _rule("barrier", 600, 155),
    "shining_word": _rule("barrier", 280, 72),
    "gravity_barrier": _rule("barrier", 1395, 365),
    "dispel": _rule("barrier", 730, 188),
    "full_purification": _rule("barrier", 850, 219),
    # 元素引导
    "fire_ray": _rule("elemental_guidance", 220, 34),
    "ice_ray": _rule("elemental_guidance", 220, 34),
    "lightning_ray": _rule("elemental_guidance", 220, 34),
    "freezing_wave": _rule("elemental_guidance", 450, 69),
    "scorching_storm": _rule("elemental_guidance", 450, 69),
    "elemental_protection": _rule("elemental_guidance", 350, 54),
    "elemental_scar": _rule("elemental_guidance", 600, 93),
    "elemental_affinity": _rule("elemental_guidance", 1080, 186.5),
    "feather_float": _rule("elemental_guidance", 870, 128),
    # 暗影术
    "shadow_arrow": _rule("shadow_magic", 200, 18),
    "dark_ray": _rule("shadow_magic", 350, 36),
    "vulnerability_fog": _rule("shadow_magic", 300, 31),
    "silence_fog": _rule("shadow_magic", 620, 64),
    "poison_weapon": _rule("shadow_magic", 400, 42),
    "provoke": _rule("shadow_magic", 660, 27),
    "ninjutsu": _rule("shadow_magic", 870, 90),
    "obscuring_fog": _rule("shadow_magic", 1500, 157),
    "shadow_cloak": _rule("shadow_magic", 835, 87),
    # 祝福术
    "cleansing_light": _rule("blessing", 400, 38),
    "remove_curse": _rule("blessing", 700, 60),
    "slowness": _rule("blessing", 450, 46),
    "holy_justice": _rule("blessing", 150, 16),
    "protective_prayer": _rule("blessing", 555, 57),
    "judgment_bind": _rule("blessing", 210, 22),
    "paralysis_chain": _rule("blessing", 700, 73),
    "haste": _rule("blessing", 1050, 120),
    "holy_light_blessing": _rule("blessing", 900, 94),
    # 恢复术
    "minor_heal": _rule("restoration", 80, 8),
    "critical_heal": _rule("restoration", 350, 26),
    "healing_hand": _rule("restoration", 400, 31),
    "eris_heal": _rule("restoration", 800, 83),
    "jure_heal": _rule("restoration", 1300, 241),
    "disease_cure": _rule("restoration", 1850, 175),
    # 招魂术
    "hell_breath": _rule("necromancy", 400, 41),
    "hell_ray": _rule("necromancy", 625, 82),
    "death_shadow": _rule("necromancy", 660, 68),
    "evil_fear": _rule("necromancy", 1360, 105),
    "full_dispel": _rule("necromancy", 1430, 150),
    "void_embrace": _rule("necromancy", 975, 150.5),
    "dim_light": _rule("necromancy", 940, 98),
    # 精神控制
    "hero": _rule("mind_control", 80, 20),
    "mind_ray": _rule("mind_control", 360, 42),
    "paralysis_arrow": _rule("mind_control", 400, 46),
    "nightmare": _rule("mind_control", 500, 57),
    "roaring_wave": _rule("mind_control", 700, 78),
    "mind_barrier": _rule("mind_control", 765, 102),
    "mental_guard": _rule("mind_control", 800, 88),
    "mental_rebound": _rule("mind_control", 975, 110),
    "body_slow": _rule("mind_control", 555, 62),
    "mind_slow": _rule("mind_control", 870, 70),
    "mental_snare": _rule("mind_control", 835, 92),
    "limit_break": _rule("mind_control", 625, 1, "fixed"),
    "free_thought": _rule("mind_control", 1150, 126),
    "fanaticism": _rule("mind_control", 1045, 1, "fixed"),
    # 自然学识（鉴定本次不加入）
    "healing_rain": _rule("natural_knowledge", 800, 56.5),
    "regeneration": _rule("natural_knowledge", 400, 32),
    "fire_wall": _rule("natural_knowledge", 640, 49),
    "web": _rule("natural_knowledge", 150, 15),
    "acid_sea": _rule("natural_knowledge", 480, 18),
    "thorn_entangle": _rule("natural_knowledge", 835, 62),
    "beast_claw": _rule("natural_knowledge", 350, 29),
    "tree_skin": _rule("natural_knowledge", 730, 55),
    "oak_blessing": _rule("natural_knowledge", 450, 75),
    "elm_blessing": _rule("natural_knowledge", 450, 118),
    "poison_resistance": _rule("natural_knowledge", 625, 47),
    "earthquake": _rule("natural_knowledge", 1500, 180),
    "poison_cure": _rule("natural_knowledge", 1360, 143),
    # 特殊武器魔法
    "storm_strike": _rule("elemental_guidance", 1325, 97),
}


def spell_level_for(definition, fighter) -> int:
    spell = (
        fighter.snapshot.skills.spells.get(definition.ability_id)
        if fighter.snapshot.skills else None
    )
    return max(1, spell.level if spell else 1)


def spell_power_for(definition, fighter, spell_level: int) -> float:
    return spell_base_power(definition, fighter, spell_level) * (
        spell_multiplier_for(definition, fighter)
    )


def _fighter_attributes(fighter) -> dict[str, float]:
    return {
        attribute_id: fighter.primary(attribute_id)
        for attribute_id in (
            "strength", "constitution", "dexterity",
            "perception", "magic", "willpower",
        )
    }


def spell_base_power(definition, fighter, spell_level: int) -> float:
    equipment_power = 0.0
    if fighter.snapshot.equipment:
        equipment_power = float(
            fighter.snapshot.equipment.combat_effects.get("spell_power", 0.0)
        )
    return 8 + spell_level * 1.5 + equipment_power


def healing_base_power(spell_level: int) -> float:
    return 10 + spell_level * 2


def spell_multiplier_for(definition, fighter) -> float:
    school_level = fighter.skill_level(definition.unlock_skill_id)
    return spell_power_multiplier(
        school_id=definition.unlock_skill_id,
        school_level=school_level,
        attributes=_fighter_attributes(fighter),
    )


def calculate_mana_cost(definition, fighter, void_embrace_active: bool) -> ManaCostBreakdown:
    level = spell_level_for(definition, fighter)
    base = float(definition.base_mana_cost)
    if definition.mana_cost_mode == "fixed":
        level_cost = math.ceil(base)
    else:
        level_cost = math.ceil(base + (base * 0.03 + 0.2) * level)
    spell_power = spell_power_for(definition, fighter, level)
    ratio = 1.0
    if void_embrace_active and definition.ability_id != "void_embrace":
        ratio = max(
            0.25,
            (700 - spell_power / 50) / (700 + spell_power),
        )
    return ManaCostBreakdown(
        base,
        level,
        level_cost,
        ratio,
        max(1, math.ceil(level_cost * ratio)),
        spell_power,
    )
