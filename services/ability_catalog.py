from __future__ import annotations

from dataclasses import replace

try:
    from ..models.ability import ActionEffect, ActiveAbilityDefinition
    from .spell_rules import SPELL_RULES
except ImportError:
    from models.ability import ActionEffect, ActiveAbilityDefinition
    from services.spell_rules import SPELL_RULES


TECHNIQUE_TIERS = {
    10: (15, 10, 1, 2),
    20: (20, 12, 2, 2),
    50: (30, 20, 2, 3),
    80: (40, 30, 3, 4),
}
SPELL_THRESHOLDS = (1, 20, 50, 80)
SPELL_COSTS = (10, 18, 30, 45)
SPELL_COOLDOWNS = (8, 12, 20, 30)
SPELL_WINDUPS = (2, 3, 4, 5)
SPELL_RECOVERIES = (2, 3, 4, 5)


def effect(effect_type: str, **kwargs) -> ActionEffect:
    return ActionEffect(effect_type=effect_type, **kwargs)


def physical(
    multiplier: float = 1.0,
    *,
    target: str = "enemy",
    params: dict | None = None,
) -> ActionEffect:
    return effect(
        "physical_damage",
        target=target,
        value=multiplier,
        params=params or {},
    )


def magic_damage(
    damage_type: str,
    multiplier: float = 1.0,
    *,
    target: str = "enemy",
    params: dict | None = None,
) -> ActionEffect:
    return effect(
        "magic_damage",
        target=target,
        value=multiplier,
        damage_type=damage_type,
        params=params or {},
    )


def status(
    status_id: str,
    duration: int,
    chance: float = 1.0,
    magnitude: float = 0.0,
    *,
    target: str = "enemy",
    beneficial: bool = False,
    params: dict | None = None,
) -> ActionEffect:
    values = dict(params or {})
    values["beneficial"] = beneficial
    return effect(
        "apply_status",
        target=target,
        value=magnitude,
        duration_ticks=duration,
        chance=chance,
        status_id=status_id,
        params=values,
    )


def stance(
    status_id: str,
    *,
    magnitude: float = 0.0,
    params: dict | None = None,
) -> ActionEffect:
    return effect(
        "activate_stance",
        target="self",
        value=magnitude,
        status_id=status_id,
        params=params or {},
    )


def heal(multiplier: float = 1.0, *, target: str = "self") -> ActionEffect:
    return effect("heal", target=target, value=multiplier)


def technique(
    ability_id: str,
    name: str,
    unlock_skill_id: str,
    unlock_level: int,
    effects: tuple[ActionEffect, ...],
    *,
    weapon_types: tuple[str, ...] = (),
    weapon_modes: tuple[str, ...] = (),
    cast_range: int = 100,
    targeting: str = "single",
    ability_type: str = "technique",
    exclusive_group: str = "",
    freezes_mana: bool = False,
    description: str = "",
    ai_tags: tuple[str, ...] = ("damage",),
) -> ActiveAbilityDefinition:
    sp, cooldown, windup, recovery = TECHNIQUE_TIERS[unlock_level]
    return ActiveAbilityDefinition(
        ability_id,
        name,
        ability_type,
        unlock_skill_id,
        unlock_level,
        "sp",
        sp,
        cooldown,
        windup,
        recovery,
        cast_range,
        targeting,
        weapon_types,
        weapon_modes,
        effects,
        exclusive_group,
        freezes_mana,
        description,
        ai_tags,
    )


TECHNIQUE_DEFINITIONS = {
    item.ability_id: item
    for item in (
        technique("whirlwind_slash", "旋风斩", "tactics", 10, (physical(1.0),), cast_range=150, targeting="self_aoe"),
        technique("warrior_totem", "勇士图腾", "tactics", 50, (
            effect("summon", target="self", duration_ticks=40, radius=250, params={"entity_id": "warrior_totem", "aura_status": "warrior_totem_aura", "physical_damage": 0.20}),
        ), cast_range=0, targeting="self", ability_type="summon", ai_tags=("summon", "buff")),
        technique("despair_roar", "绝望咆哮", "tactics", 80, (
            status("despair_regen", 30, magnitude=0.5, target="self", beneficial=True, params={"requires_negative_status": True}),
        ), cast_range=0, targeting="self", ai_tags=("heal", "buff")),

        technique("flying_slash", "飞斩", "longsword", 20, (physical(1.0),), weapon_types=("longsword",), cast_range=250, targeting="line"),
        technique("courage_charge", "勇气冲锋", "longsword", 50, (
            physical(1.50, params={"bonus_above_hp": 0.50, "bonus_multiplier": 1.40}),
        ), weapon_types=("longsword",), cast_range=180),
        technique("soldier_thrust", "士兵之刺", "longsword", 80, (
            physical(1.80, params={"bonus_if_stance": 1.60}),
        ), weapon_types=("longsword",)),

        technique("disrupting_strike", "扰乱打击", "shortsword", 20, (
            physical(1.25), status("haze", 20, 0.60),
        ), weapon_types=("shortsword",)),
        technique("blind_stab", "盲目刺击", "shortsword", 50, (
            physical(1.50, params={"bonus_if_status": "haze", "bonus_multiplier": 1.35}),
            status("blind", 20, 0.70),
        ), weapon_types=("shortsword",)),
        technique("fear_judgment", "恐惧审判", "shortsword", 80, (
            physical(1.80, params={"bonus_if_both_status": ("haze", "blind"), "bonus_multiplier": 1.50}),
            effect("restore_resource", target="self", value=20, params={"resource": "sp", "on_hit": True}),
        ), weapon_types=("shortsword",)),

        technique("smash", "猛击", "axe", 20, (physical(1.60),), weapon_types=("axe",)),
        technique("barbarian_rage", "野蛮人之怒", "two_handed", 20, (
            stance("barbarian_rage", params={"strength_damage_cap": 0.30, "followup": 0.20, "sp_absorb_chance": 0.20, "sp_absorb": 5}),
        ), weapon_modes=("one_hand", "two_hand_melee", "two_hand_heavy"), cast_range=0, targeting="self", ability_type="stance", exclusive_group="combat_stance", freezes_mana=True, ai_tags=("stance", "buff")),

        technique("dark_lotus", "暗黑莲花", "dual_wield", 20, (
            stance("dark_lotus", params={"skill_damage_cap": 0.20, "block_rate": 0.15, "crit_blind_chance": 0.50}),
        ), weapon_modes=("dual_wield",), cast_range=0, targeting="self", ability_type="stance", exclusive_group="combat_stance", freezes_mana=True, ai_tags=("stance", "buff")),
        technique("phantom_smoke", "迷幻烟雾", "dual_wield", 50, (
            status("phantom_smoke", 30, magnitude=0.70, target="self", beneficial=True),
        ), cast_range=0, targeting="self", ai_tags=("buff", "control")),
        technique("stun_grenade", "眩晕手雷", "dual_wield", 80, (
            status("blind", 25, 1.0),
        ), cast_range=250, targeting="area", ai_tags=("control",)),

        technique("wind_spirit", "追风精灵", "marksmanship", 20, (
            effect("summon", target="self", duration_ticks=40, radius=250, params={"entity_id": "wind_spirit", "aura_status": "wind_spirit_aura", "ranged_speed": 0.20, "ranged_followup": 0.15}),
        ), cast_range=0, targeting="self", ability_type="summon", ai_tags=("summon", "buff")),
        technique("hunting_moment", "狩猎时刻", "marksmanship", 50, (
            status("hunting_moment", 30, magnitude=0.20, target="self", beneficial=True, params={"ranged_followup": 0.15, "penetration": 0.20, "dexterity_damage_cap": 0.20}),
        ), cast_range=0, targeting="self", ai_tags=("buff",)),

        technique("split_arrow", "分裂箭", "bow", 20, (
            stance("split_arrow", params={"splash_radius": 120, "splash_scale": 0.35}),
        ), weapon_types=("bow",), cast_range=0, targeting="self", ability_type="stance", exclusive_group="combat_stance", freezes_mana=True, ai_tags=("stance", "buff")),
        technique("thorn_arrow", "荆棘箭", "bow", 50, (
            stance("thorn_arrow", params={"on_hit_status": "bleed", "status_chance": 1.0}),
        ), weapon_types=("bow",), cast_range=0, targeting="self", ability_type="stance", exclusive_group="combat_stance", freezes_mana=True, ai_tags=("stance", "buff")),
        technique("prepared_shot", "预备射击", "bow", 80, (
            physical(1.80, params={"bonus_above_hp": 0.50, "bonus_multiplier": 1.40}),
        ), weapon_types=("bow",), cast_range=450),

        technique("destructive_shot", "破坏射击", "crossbow", 20, (
            physical(1.30), status("accuracy_down", 25, magnitude=0.25),
        ), weapon_types=("crossbow",), cast_range=450),
        technique("single_breakthrough", "一点突破", "crossbow", 50, (
            physical(1.55), status("bleed", 30, 1.0, magnitude=0.12),
        ), weapon_types=("crossbow",), cast_range=450),

        technique("ferocious_shot", "凶暴射击", "firearm", 20, (
            physical(1.30), status("haze", 20, 0.70),
        ), weapon_types=("firearm",), cast_range=500),
        technique("armor_piercing_shot", "穿甲射击", "firearm", 50, (
            physical(1.55), status("defense_down", 30, magnitude=0.30),
        ), weapon_types=("firearm",), cast_range=500),

        technique("martial_awakening", "格斗觉醒", "unarmed", 20, (
            stance("martial_awakening", params={"stun_chance": 0.25, "level60_followup": 0.20, "level80_absolute_evade": 0.10, "level80_damage": 0.25}),
        ), weapon_types=("unarmed",), cast_range=0, targeting="self", ability_type="stance", exclusive_group="combat_stance", freezes_mana=True, ai_tags=("stance", "buff")),
        technique("scythe_awakening", "镰刀觉醒", "scythe", 20, (
            stance("scythe_awakening", params={"hell_followup": 0.20, "level60_shadow_followup": 0.20, "level80_slow_chance": 0.30}),
        ), weapon_types=("scythe",), cast_range=0, targeting="self", ability_type="stance", exclusive_group="combat_stance", freezes_mana=True, ai_tags=("stance", "buff")),

        technique("shield_wall", "盾墙", "shield", 20, (
            status("shield_wall", 20, magnitude=0.50, target="self", beneficial=True, params={"lethal_survival_chance": 0.25, "lethal_uses": 1}),
        ), weapon_modes=("sword_shield",), cast_range=0, targeting="self", exclusive_group="defense_barrier", ai_tags=("defense",)),
        technique("bull_endurance", "牛之忍耐", "shield", 50, (
            status("bull_endurance", 30, magnitude=0.20, target="self", beneficial=True, params={"healing_bonus": 0.30}),
        ), cast_range=0, targeting="self", ai_tags=("defense", "heal")),

        technique("spear_thrust", "突刺", "spear", 20, (
            physical(1.25), status("blind", 15, 0.15),
        ), weapon_types=("spear",), cast_range=150),
        technique("never_retreat", "绝不后退", "spear", 50, (
            status("never_retreat", 30, magnitude=0.20, target="self", beneficial=True, params={"movement_locked": True, "counter_chance": 0.30}),
        ), weapon_types=("spear",), cast_range=0, targeting="self", ai_tags=("defense", "counter")),
        technique("hold_the_line", "寸步不让", "spear", 80, (
            stance("hold_the_line", params={"counter_cooldown": 6}),
        ), weapon_types=("spear",), cast_range=0, targeting="self", ability_type="stance", exclusive_group="combat_stance", freezes_mana=True, ai_tags=("stance", "counter")),
    )
}


def spell(
    ability_id: str,
    name: str,
    school: str,
    tier_index: int,
    effects: tuple[ActionEffect, ...],
    *,
    cast_range: int = 400,
    targeting: str = "single",
    exclusive_group: str = "",
    weapon_types: tuple[str, ...] = (),
    description: str = "",
    ai_tags: tuple[str, ...] = ("damage",),
) -> ActiveAbilityDefinition:
    return ActiveAbilityDefinition(
        ability_id,
        name,
        "spell",
        school,
        SPELL_THRESHOLDS[tier_index],
        "mp",
        SPELL_COSTS[tier_index],
        SPELL_COOLDOWNS[tier_index],
        SPELL_WINDUPS[tier_index],
        SPELL_RECOVERIES[tier_index],
        cast_range,
        targeting,
        weapon_types,
        (),
        effects,
        exclusive_group,
        False,
        description,
        ai_tags,
    )


def spell_group(school: str, specs: tuple[dict, ...]) -> tuple[ActiveAbilityDefinition, ...]:
    count = len(specs)
    result = []
    for index, spec in enumerate(specs):
        tier_index = min(3, index * 4 // count)
        result.append(spell(school=school, tier_index=tier_index, **spec))
    return tuple(result)


SPELL_GROUPS = {
    "magic_training": (
        dict(ability_id="magic_arrow", name="魔法箭", effects=(magic_damage("magic", 0.85, params={"high_accuracy": True}),)),
        dict(ability_id="mana_ray", name="聚魔射线", effects=(magic_damage("magic", 0.80),), targeting="line"),
        dict(ability_id="mana_storm", name="魔力风暴", effects=(magic_damage("magic", 0.80),), targeting="self_aoe", cast_range=150),
        dict(ability_id="mana_scar", name="魔力伤痕", effects=(status("resistance_magic_down", 30, magnitude=25),), ai_tags=("debuff",)),
        dict(ability_id="confusion_spell", name="困惑咒文", effects=(status("confusion", 25, 0.70),), ai_tags=("control",)),
        dict(ability_id="sage_blessing", name="智者加护", effects=(status("sage_blessing", 40, magnitude=0.25, target="self", beneficial=True, params={"magic": 0.25, "willpower": 0.25, "reading": 20}),), targeting="self", cast_range=0, exclusive_group="wisdom_blessing", ai_tags=("buff",)),
        dict(ability_id="insight_spell", name="洞察咒文", effects=(status("insight", 40, magnitude=0.50, target="self", beneficial=True),), targeting="self", cast_range=0, ai_tags=("buff", "resource")),
        dict(ability_id="blink", name="闪现", effects=(effect("teleport", target="self", value=100, params={"mode": "random_short"}),), targeting="self", cast_range=0, ai_tags=("mobility",)),
        dict(ability_id="teleport", name="瞬间移动", effects=(effect("teleport", target="self", value=500, params={"mode": "random_long"}),), targeting="self", cast_range=0, ai_tags=("mobility",)),
    ),
    "barrier": (
        dict(ability_id="armor_spell", name="护甲术", effects=(status("armor_spell", 40, magnitude=0.20, target="self", beneficial=True, params={"defense": 0.25, "evasion": 0.15, "physical_reduction": 0.15}),), targeting="self", cast_range=0, exclusive_group="defense_barrier", ai_tags=("defense",)),
        dict(ability_id="holy_shield", name="神圣之盾", effects=(status("holy_shield", 40, magnitude=0.35, target="self", beneficial=True, params={"defense": 0.35}),), targeting="self", cast_range=0, exclusive_group="defense_barrier", ai_tags=("defense",)),
        dict(ability_id="mana_barrier", name="魔力结界", effects=(status("mana_barrier", 40, magnitude=0.30, target="self", beneficial=True, params={"resistance_magic": 30}),), targeting="self", cast_range=0, ai_tags=("defense",)),
        dict(ability_id="shining_word", name="闪耀圣言", effects=(status("paralysis", 12, 0.65),), targeting="self_aoe", cast_range=150, ai_tags=("control",)),
        dict(ability_id="gravity_barrier", name="重力结界", effects=(status("gravity", 30, 0.80, magnitude=0.30),), targeting="self_aoe", cast_range=180, ai_tags=("control", "debuff")),
        dict(ability_id="dispel", name="驱散", effects=(effect("dispel", target="enemy", value=1),), ai_tags=("dispel",)),
        dict(ability_id="full_purification", name="全净化", effects=(effect("cleanse", target="self", params={"mode": "all_negative"}), status("status_resistance", 20, magnitude=0.40, target="self", beneficial=True)), targeting="self", cast_range=0, ai_tags=("cleanse",)),
    ),
    "elemental_guidance": (
        dict(ability_id="fire_ray", name="火焰射线", effects=(magic_damage("fire", 1.0), status("burn", 25, 0.55, magnitude=0.12)), targeting="line"),
        dict(ability_id="ice_ray", name="冰冻射线", effects=(magic_damage("cold", 1.0), status("slow", 20, 0.35, magnitude=0.20)), targeting="line"),
        dict(ability_id="lightning_ray", name="雷光射线", effects=(magic_damage("lightning", 1.0), status("paralysis", 10, 0.35)), targeting="line"),
        dict(ability_id="freezing_wave", name="冰结波动", effects=(magic_damage("cold", 0.80), status("slow", 20, 0.45, magnitude=0.20)), targeting="self_aoe", cast_range=150),
        dict(ability_id="scorching_storm", name="灼热风暴", effects=(magic_damage("fire", 0.80), status("burn", 25, 0.65, magnitude=0.12)), targeting="self_aoe", cast_range=150),
        dict(ability_id="elemental_protection", name="元素保护", effects=(status("elemental_protection", 40, magnitude=0.30, target="self", beneficial=True, params={"resistance_fire": 30, "resistance_cold": 30, "resistance_lightning": 30}),), targeting="self", cast_range=0, ai_tags=("defense",)),
        dict(ability_id="elemental_scar", name="元素伤痕", effects=(status("elemental_scar", 35, magnitude=0.25, params={"resistance_fire": -25, "resistance_cold": -25, "resistance_lightning": -25}),), ai_tags=("debuff",)),
        dict(ability_id="elemental_affinity", name="元素亲和", effects=(status("elemental_affinity", 40, magnitude=0.15, target="self", beneficial=True),), targeting="self", cast_range=0, ai_tags=("buff", "resource")),
        dict(ability_id="feather_float", name="羽毛漂浮", effects=(status("floating", 40, magnitude=0.25, target="self", beneficial=True, params={"weight_reduction": 0.25}),), targeting="self", cast_range=0, ai_tags=("buff", "mobility")),
    ),
    "shadow_magic": (
        dict(ability_id="shadow_arrow", name="暗影箭", effects=(magic_damage("shadow", 1.0), status("blind", 20, 0.55))),
        dict(ability_id="dark_ray", name="暗黑射线", effects=(magic_damage("shadow", 0.90), status("blind", 20, 0.65)), targeting="line"),
        dict(ability_id="vulnerability_fog", name="脆弱之雾", effects=(status("vulnerable", 30, magnitude=0.30, params={"defense": -0.30, "evasion": -0.30}),), ai_tags=("debuff",)),
        dict(ability_id="silence_fog", name="沉默之雾", effects=(status("silence", 25, 0.70),), ai_tags=("control",)),
        dict(ability_id="poison_weapon", name="武器涂毒", effects=(status("poison_weapon", 40, magnitude=0.12, target="self", beneficial=True),), targeting="self", cast_range=0, ai_tags=("buff",)),
        dict(ability_id="provoke", name="挑拨", effects=(status("slow", 20, 0.80, magnitude=0.25), status("poison", 25, 0.70, magnitude=0.12)), cast_range=120, ai_tags=("control", "debuff")),
        dict(ability_id="ninjutsu", name="忍术", effects=(effect("teleport", target="self", value=150, params={"mode": "ideal_distance"}),), targeting="self", cast_range=0, ai_tags=("mobility",)),
        dict(ability_id="obscuring_fog", name="遮蔽之雾", effects=(status("healing_block", 30, magnitude=0.50, params={"stop_regen": True}),), ai_tags=("debuff",)),
        dict(ability_id="shadow_cloak", name="暗影斗篷", effects=(status("shadow_cloak", 40, magnitude=0.30, target="self", beneficial=True, params={"resistance_cold": 30, "resistance_shadow": 30, "paralysis_immunity": True}),), targeting="self", cast_range=0, ai_tags=("defense",)),
    ),
    "blessing": (
        dict(ability_id="cleansing_light", name="清净之光", effects=(effect("cleanse", target="self", params={"mode": "one_negative"}), status("status_resistance", 20, magnitude=0.25, target="self", beneficial=True)), targeting="self", cast_range=0, ai_tags=("cleanse",)),
        dict(ability_id="remove_curse", name="解除诅咒", effects=(effect("cleanse", target="self", params={"mode": "curse"}),), targeting="self", cast_range=0, ai_tags=("cleanse",)),
        dict(ability_id="slowness", name="迟缓", effects=(status("slow", 30, 0.80, magnitude=0.30),), ai_tags=("control",)),
        dict(ability_id="holy_justice", name="神圣正义", effects=(status("holy_justice", 50, magnitude=0.25, target="self", beneficial=True, params={"physical_damage": 0.25}),), targeting="self", cast_range=0, exclusive_group="physical_blessing", ai_tags=("buff",)),
        dict(ability_id="protective_prayer", name="加护祈祷", effects=(status("protective_prayer", 40, magnitude=0.25, target="self", beneficial=True, params={"evasion": 0.25}),), targeting="self", cast_range=0, exclusive_group="defense_barrier", ai_tags=("defense",)),
        dict(ability_id="judgment_bind", name="裁决束缚", effects=(status("bind", 25, 0.75),), ai_tags=("control",)),
        dict(ability_id="paralysis_chain", name="麻痹连锁", effects=(status("paralysis", 12, 0.85), status("paralysis", 12, 1.0, target="self")), ai_tags=("control",)),
        dict(ability_id="haste", name="加速", effects=(status("haste", 40, magnitude=0.35, target="self", beneficial=True),), targeting="self", cast_range=0, ai_tags=("buff", "mobility")),
        dict(ability_id="holy_light_blessing", name="圣光加护", effects=(status("status_resistance", 40, magnitude=0.45, target="self", beneficial=True),), targeting="self", cast_range=0, ai_tags=("defense",)),
    ),
    "restoration": (
        dict(ability_id="minor_heal", name="轻伤治疗", effects=(heal(0.70), effect("cleanse", target="self", params={"mode": "bleed_poison"})), targeting="self", cast_range=0, ai_tags=("heal", "cleanse")),
        dict(ability_id="critical_heal", name="致命伤治疗", effects=(heal(1.0), effect("cleanse", target="self", params={"mode": "bleed_poison"})), targeting="self", cast_range=0, ai_tags=("heal", "cleanse")),
        dict(ability_id="healing_hand", name="治愈之手", effects=(heal(1.0, target="ally"), effect("cleanse", target="ally", params={"mode": "bleed_poison"})), targeting="ally", cast_range=120, ai_tags=("heal", "cleanse")),
        dict(ability_id="eris_heal", name="艾里斯治疗", effects=(heal(1.40), effect("cleanse", target="self", params={"mode": "bleed_poison"})), targeting="self", cast_range=0, ai_tags=("heal", "cleanse")),
        dict(ability_id="jure_heal", name="朱亚治疗", effects=(heal(1.90), effect("cleanse", target="self", params={"mode": "bleed_poison"})), targeting="self", cast_range=0, ai_tags=("heal", "cleanse")),
        dict(ability_id="disease_cure", name="疫病治疗", effects=(effect("cleanse", target="self", params={"mode": "disease"}),), targeting="self", cast_range=0, ai_tags=("cleanse",)),
    ),
    "necromancy": (
        dict(ability_id="hell_breath", name="地狱吐息", effects=(magic_damage("hell", 1.0, params={"life_steal": 0.25}), status("disease", 30, 0.45, magnitude=0.30))),
        dict(ability_id="hell_ray", name="地狱射线", effects=(magic_damage("hell", 0.90),), targeting="line"),
        dict(ability_id="death_shadow", name="死亡阴影", effects=(status("resistance_hell_down", 35, magnitude=25),), ai_tags=("debuff",)),
        dict(ability_id="evil_fear", name="邪恶恐惧", effects=(status("evil_fear", 30, 0.75, magnitude=0.35, params={"slow": 0.35, "healing_reduction": 0.50}),), ai_tags=("control", "debuff")),
        dict(ability_id="full_dispel", name="全驱散", effects=(effect("dispel", target="enemy", params={"mode": "all"}),), ai_tags=("dispel",)),
        dict(ability_id="void_embrace", name="虚无拥抱", effects=(status("void_embrace", 40, magnitude=0.30, target="self", beneficial=True, params={"mp_cost_reduction": 0.30}),), targeting="self", cast_range=0, ai_tags=("buff", "resource")),
        dict(ability_id="dim_light", name="昏暗之光", effects=(status("dim_light", 40, magnitude=0.35, target="self", beneficial=True, params={"resistance_hell": 35}),), targeting="self", cast_range=0, ai_tags=("defense",)),
    ),
    "mind_control": (
        dict(ability_id="hero", name="英雄", effects=(status("hero", 40, magnitude=0.25, target="self", beneficial=True, params={"strength": 0.25, "dexterity": 0.25, "confusion_immunity": True}),), targeting="self", cast_range=0, exclusive_group="body_blessing", ai_tags=("buff",)),
        dict(ability_id="mind_ray", name="精神射线", effects=(magic_damage("mind", 0.90), status("mental_random", 20, 0.55, params={"choices": ("paralysis", "confusion", "haze")})), targeting="line"),
        dict(ability_id="paralysis_arrow", name="麻痹箭", effects=(magic_damage("mind", 0.80), status("paralysis", 15, 0.75))),
        dict(ability_id="nightmare", name="噩梦", effects=(status("nightmare", 35, magnitude=0.25, params={"resistance_shadow": -25, "resistance_hell": -25, "resistance_mind": -25}),), ai_tags=("debuff",)),
        dict(ability_id="roaring_wave", name="轰鸣波动", effects=(magic_damage("mind", 0.80), status("confusion", 25, 0.70)), targeting="self_aoe", cast_range=150),
        dict(ability_id="mind_barrier", name="心灵屏障", effects=(status("mind_barrier", 40, magnitude=0.40, target="self", beneficial=True, params={"physical_reduction": 0.40, "damage_penalty": 0.25}),), targeting="self", cast_range=0, exclusive_group="defense_barrier", ai_tags=("defense",)),
        dict(ability_id="mental_guard", name="精神监护", effects=(status("mental_guard", 40, magnitude=0.35, target="self", beneficial=True, params={"resistance_mind": 35}),), targeting="self", cast_range=0, ai_tags=("defense",)),
        dict(ability_id="mental_rebound", name="精神反弹", effects=(status("mental_rebound", 40, magnitude=20, target="self", beneficial=True, params={"mind_control_level": 20}),), targeting="self", cast_range=0, ai_tags=("buff",)),
        dict(ability_id="body_slow", name="肉体迟缓", effects=(status("body_slow", 35, magnitude=0.20, params={"strength": -0.20, "constitution": -0.20, "dexterity": -0.20}),), ai_tags=("debuff",)),
        dict(ability_id="mind_slow", name="意识迟缓", effects=(status("mind_slow", 35, magnitude=0.20, params={"perception": -0.20, "magic": -0.20, "willpower": -0.20}),), ai_tags=("debuff",)),
        dict(ability_id="mental_snare", name="精神圈套", effects=(status("snare_random", 25, 0.80, params={"choices": ("slow", "bind")}),), ai_tags=("control",)),
        dict(ability_id="limit_break", name="突破极限", effects=(heal(0.85), status("mp_regen_frozen", 20, target="self")), targeting="self", cast_range=0, ai_tags=("heal",)),
        dict(ability_id="free_thought", name="自由思想", effects=(status("free_thought", 40, magnitude=0.25, target="self", beneficial=True, params={"magic": 0.25, "willpower": 0.25, "haze_duration_reduction": 0.50}),), targeting="self", cast_range=0, exclusive_group="wisdom_blessing", ai_tags=("buff",)),
        dict(ability_id="fanaticism", name="狂热", effects=(effect("drain_resource", target="enemy", value=30, params={"resource": "mp"}),), targeting="self_aoe", cast_range=180, ai_tags=("resource", "debuff")),
    ),
    "natural_knowledge": (
        dict(ability_id="healing_rain", name="治愈之雨", effects=(heal(0.90, target="ally_area"), effect("cleanse", target="ally_area", params={"mode": "bleed_poison"})), targeting="ally_area", cast_range=300, ai_tags=("heal", "cleanse")),
        dict(ability_id="regeneration", name="再生", effects=(status("regeneration", 40, magnitude=0.60, target="self", beneficial=True),), targeting="self", cast_range=0, ai_tags=("heal", "buff")),
        dict(ability_id="fire_wall", name="火墙", effects=(effect("create_zone", target="ground", duration_ticks=30, radius=100, damage_type="fire", params={"zone_id": "fire_wall", "periodic_damage": 0.30, "status": "burn"}),), targeting="ground", cast_range=350, ai_tags=("zone", "damage")),
        dict(ability_id="web", name="蛛网术", effects=(effect("create_zone", target="ground", duration_ticks=30, radius=100, params={"zone_id": "web", "status": "bind"}),), targeting="ground", cast_range=350, ai_tags=("zone", "control")),
        dict(ability_id="acid_sea", name="酸之海", effects=(effect("create_zone", target="ground", duration_ticks=30, radius=100, damage_type="nature", params={"zone_id": "acid_sea", "periodic_damage": 0.25, "status": "wet"}),), targeting="ground", cast_range=350, ai_tags=("zone", "damage")),
        dict(ability_id="thorn_entangle", name="荆棘缠绕", effects=(status("slow", 25, 0.85, magnitude=0.30), status("poison", 30, 0.75, magnitude=0.12)), ai_tags=("control", "debuff")),
        dict(ability_id="beast_claw", name="野兽之爪", effects=(status("beast_claw", 50, magnitude=0.25, target="self", beneficial=True, params={"physical_damage": 0.25}),), targeting="self", cast_range=0, exclusive_group="physical_blessing", ai_tags=("buff",)),
        dict(ability_id="tree_skin", name="树肤术", effects=(status("tree_skin", 40, magnitude=0.25, target="self", beneficial=True, params={"defense": 0.25, "resistance_cold": 25, "bleed_resistance": 0.50}),), targeting="self", cast_range=0, ai_tags=("defense",)),
        dict(ability_id="oak_blessing", name="橡树祝福", effects=(status("oak_blessing", 40, magnitude=0.20, target="self", beneficial=True, params={"dexterity": 0.20, "constitution": 0.20, "perception": 0.20}),), targeting="self", cast_range=0, exclusive_group="body_blessing", ai_tags=("buff",)),
        dict(ability_id="elm_blessing", name="榆树祝福", effects=(status("elm_blessing", 40, magnitude=0.20, target="self", beneficial=True, params={"perception": 0.20, "magic": 0.20, "willpower": 0.20}),), targeting="self", cast_range=0, exclusive_group="wisdom_blessing", ai_tags=("buff",)),
        dict(ability_id="poison_resistance", name="毒液抵抗", effects=(status("poison_resistance", 40, magnitude=0.35, target="self", beneficial=True, params={"resistance_nature": 35}),), targeting="self", cast_range=0, ai_tags=("defense",)),
        dict(ability_id="earthquake", name="地震", effects=(physical(0.80, params={"self_damage_ratio": 0.25}), status("confusion", 20, 0.55)), targeting="self_aoe", cast_range=180),
        dict(ability_id="poison_cure", name="中毒治疗", effects=(effect("cleanse", target="self", params={"mode": "poison"}),), targeting="self", cast_range=0, ai_tags=("cleanse",)),
    ),
}


SPELL_DEFINITIONS = {
    item.ability_id: item
    for school, specs in SPELL_GROUPS.items()
    for item in spell_group(school, specs)
}
SPELL_DEFINITIONS["storm_strike"] = spell(
    "storm_strike",
    "暴风打击",
    "elemental_guidance",
    1,
    (physical(1.30), magic_damage("lightning", 0.50), status("paralysis", 10, 0.45)),
    cast_range=100,
    weapon_types=("blunt",),
)

if set(SPELL_DEFINITIONS) != set(SPELL_RULES):
    missing = sorted(set(SPELL_DEFINITIONS) - set(SPELL_RULES))
    extra = sorted(set(SPELL_RULES) - set(SPELL_DEFINITIONS))
    raise RuntimeError(f"法术规则表与目录不一致: missing={missing}, extra={extra}")

SPELL_DEFINITIONS = {
    spell_id: replace(
        definition,
        reading_difficulty=SPELL_RULES[spell_id].reading_difficulty,
        reading_attribute=SPELL_RULES[spell_id].reading_attribute,
        base_mana_cost=SPELL_RULES[spell_id].base_mana_cost,
        mana_cost_mode=SPELL_RULES[spell_id].mana_cost_mode,
    )
    for spell_id, definition in SPELL_DEFINITIONS.items()
}

POWER_STRIKE = ActiveAbilityDefinition(
    "power_strike", "强击", "legacy", "power_strike", 1, "sp", 25,
    12, 2, 3, 100, "single", (),
    ("one_hand", "sword_shield", "dual_wield", "two_hand_melee", "two_hand_heavy"),
    (physical(1.60, params={"bonus_knockback": 15}),),
    description="兼容旧版的高伤害、高击退近战攻击。",
    ai_tags=("damage",),
)

ACTIVE_ABILITY_DEFINITIONS = {
    **TECHNIQUE_DEFINITIONS,
    **SPELL_DEFINITIONS,
    POWER_STRIKE.ability_id: POWER_STRIKE,
}
ABILITY_NAME_TO_ID = {
    definition.name: ability_id
    for ability_id, definition in ACTIVE_ABILITY_DEFINITIONS.items()
}
ABILITY_NAME_TO_ID.update({
    "回旋斩": "whirlwind_slash", "跃斩": "flying_slash",
    "魔法之箭": "magic_arrow", "灼热射线": "fire_ray",
    "暗影之箭": "shadow_arrow", "黑暗射线": "dark_ray",
    "邪恶之箭": "hell_breath", "幻影射线": "mind_ray",
    "麻痹之箭": "paralysis_arrow", "梦魇": "nightmare",
    "秘法伤痕": "mana_scar", "神圣之握": "judgment_bind",
    "麻痹连接": "paralysis_chain", "治疗中毒": "poison_cure",
    "清净圣光": "cleansing_light", "艾莉丝治疗": "eris_heal",
    "秘法结界": "mana_barrier", "武器淬毒": "poison_weapon",
    "拥抱虚空": "void_embrace", "心灵护盾": "mind_barrier",
    "超越极限": "limit_break", "风暴打击": "storm_strike",
})


def ability_id_for(value: str) -> str | None:
    value = (value or "").strip()
    if value in ACTIVE_ABILITY_DEFINITIONS:
        return value
    return ABILITY_NAME_TO_ID.get(value)


def ability_is_unlocked(definition, skills, spells) -> bool:
    """Check permanent progression only; equipment bonuses never unlock abilities."""
    if definition.ability_type == "spell":
        return definition.ability_id in spells
    skill = skills.get(definition.unlock_skill_id)
    if not skill:
        return False
    return skill.level >= definition.unlock_level

def spell_exp_required(level: int) -> int:
    return 50 + level * 15
