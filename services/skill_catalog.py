try:
    from ..models.skill import SkillDefinition, SkillEffect
except ImportError:
    from models.skill import SkillDefinition, SkillEffect


def _effect(
    effect_id: str,
    per_level: float,
    max_bonus: float | None = None,
    *,
    weapon_types: tuple[str, ...] = (),
    weapon_modes: tuple[str, ...] = (),
    armor_styles: tuple[str, ...] = (),
    pve_only: bool = False,
) -> SkillEffect:
    return SkillEffect(
        effect_id,
        per_level,
        max_bonus,
        weapon_types,
        weapon_modes,
        armor_styles,
        pve_only,
    )


def _passive(
    skill_id: str,
    name: str,
    category: str,
    governing_attributes: tuple[str, ...],
    description: str,
    effects: tuple[SkillEffect, ...] = (),
    prerequisites: tuple[tuple[str, int], ...] = (),
    future_system: str = "",
) -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id,
        name=name,
        category=category,
        governing_attributes=governing_attributes,
        description=description,
        effects=effects,
        prerequisites=prerequisites,
        future_system=future_system,
    )


def _weapon_effects(weapon_type: str) -> tuple[SkillEffect, ...]:
    return (
        _effect("accuracy", 0.35, weapon_types=(weapon_type,)),
    )


MELEE_AND_THROWING = (
    "longsword", "shortsword", "axe", "spear", "unarmed", "scythe",
    "blunt", "staff", "throwing",
)
RANGED_PHYSICAL = ("bow", "crossbow", "firearm")

SKILL_DEFINITIONS = {
    item.skill_id: item
    for item in (
        # 力量系
        _passive("longsword", "长剑专精", "strength", ("strength",), "提高长剑、大剑类武器的命中与伤害。", _weapon_effects("longsword")),
        _passive("axe", "斧头专精", "strength", ("strength",), "提高斧类武器的命中与伤害。", _weapon_effects("axe")),
        _passive("unarmed", "格斗技巧", "strength", ("strength", "dexterity"), "提高空手、拳套及格斗攻击的命中与伤害。", _weapon_effects("unarmed")),
        _passive("scythe", "镰刀专精", "strength", ("strength",), "提高镰刀类武器的命中与伤害。", _weapon_effects("scythe")),
        _passive("tactics", "战术", "strength", ("strength",), "提高近战和投掷攻击的命中与伤害倍率。", (
            _effect("accuracy", 0.20, weapon_types=MELEE_AND_THROWING),
        )),
        _passive("two_handed", "双手武器", "strength", ("strength",), "单独持有一把近战武器且没有盾牌时提高攻击能力。", (
            _effect("two_handed_style", 0.003, weapon_modes=("one_hand", "two_hand_melee", "two_hand_heavy")),
        )),
        _passive("weightlifting", "举重", "strength", ("strength",), "提高负重上限，超负重战斗可训练。", (_effect("carry_capacity", 0.50),)),

        # 体质系
        _passive("blunt", "钝器专精", "constitution", ("constitution",), "提高锤、棒等钝器的命中与伤害。", _weapon_effects("blunt")),
        _passive("spear", "长枪专精", "constitution", ("constitution",), "提高长枪类武器的命中与伤害。", _weapon_effects("spear")),
        _passive("staff", "法杖专精", "constitution", ("constitution",), "提高法杖近战攻击的命中与物理伤害。", _weapon_effects("staff")),
        _passive("shield", "盾牌专精", "constitution", ("constitution",), "装备盾牌时提高格挡、防御与击退抗性。", (
            _effect("block_rate", 0.001, weapon_modes=("sword_shield",)),
            _effect("defense", 0.15, weapon_modes=("sword_shield",)),
            _effect("knockback_resistance", 0.002, 0.25, weapon_modes=("sword_shield",)),
        )),
        _passive("heavy_armor", "重装甲", "constitution", ("constitution",), "重甲路线提高防御和物理减伤。", (
            _effect("defense", 0.20, armor_styles=("heavy",)),
            _effect("physical_reduction", 0.0015, 0.20, armor_styles=("heavy",)),
        )),
        _passive("medium_armor", "中装甲", "constitution", ("constitution",), "中甲路线提高防御和物理减伤。", (
            _effect("defense", 0.15, armor_styles=("medium",)),
            _effect("physical_reduction", 0.001, 0.15, armor_styles=("medium",)),
        )),
        _passive("healing", "治愈", "constitution", ("constitution",), "提高战斗中的生命自然恢复速度。", (_effect("hp_regen_per_tick", 0.003),)),

        # 灵巧系
        _passive("shortsword", "短剑专精", "dexterity", ("dexterity",), "提高短剑类武器的命中与伤害。", _weapon_effects("shortsword")),
        _passive("bow", "弓专精", "dexterity", ("dexterity",), "提高弓类武器的命中与伤害。", _weapon_effects("bow")),
        _passive("crossbow", "弩专精", "dexterity", ("dexterity",), "提高弩类武器的命中与伤害。", _weapon_effects("crossbow")),
        _passive("throwing", "投掷", "dexterity", ("strength", "dexterity"), "提高投掷武器的命中与伤害。", _weapon_effects("throwing"), future_system="throwable_items"),
        _passive("dual_wield", "双持", "dexterity", ("dexterity",), "双持武器时降低双持与重量惩罚。", (
            _effect("dual_wield_style", 0.002, weapon_modes=("dual_wield",)),
        )),
        _passive("light_armor", "轻装甲", "dexterity", ("dexterity",), "轻甲路线提高回避、防御和物理减伤。", (
            _effect("defense", 0.08, armor_styles=("light",)),
            _effect("evasion", 0.20, armor_styles=("light",)),
            _effect("physical_reduction", 0.0005, 0.10, armor_styles=("light",)),
        )),
        _passive("dodge", "闪避", "dexterity", ("dexterity",), "提高物理回避能力。", (_effect("evasion", 0.35),)),

        # 感知系
        _passive("firearm", "枪械专精", "perception", ("perception",), "提高枪械的命中与伤害。", _weapon_effects("firearm")),
        _passive("marksmanship", "射术", "perception", ("perception",), "提高弓、弩和枪械攻击的命中与综合伤害。", (
            _effect("accuracy", 0.15, weapon_types=RANGED_PHYSICAL),
        )),
        _passive("mind_eye", "心眼", "perception", ("perception",), "提高物理攻击的暴击率和暴击伤害。", (
            _effect("critical_rate", 0.001),
            _effect("critical_damage", 0.002),
        )),
        _passive("concealment", "隐蔽", "perception", ("perception",), "降低未来PVE模式中的敌人发现率。", (_effect("pve_stealth", 0.005, 0.50, pve_only=True),), future_system="pve"),
        _passive("natural_knowledge", "自然学识", "perception", ("perception",), "自然系魔法的基础技能。", (
            _effect("spell_nature", 0.004),
            _effect("healing_power_bonus", 0.002),
        ), future_system="magic"),
        _passive("pact", "密约", "perception", ("perception",), "与元素契约等召唤能力联动。", (_effect("summon_power_bonus", 0.003),), future_system="summoning"),
        _passive("spiritualism", "通灵", "perception", ("perception",), "与精灵合体等召唤能力联动。", (_effect("summon_power_bonus", 0.003),), future_system="summoning"),

        # 魔力系
        _passive("reading", "读书", "magic", ("magic",), "提高阅读魔法书和技能书的成功率。", (_effect("reading_success", 0.005),), future_system="reading"),
        _passive("magic_training", "魔法修行", "magic", ("magic",), "通用奥术系魔法基础。", (_effect("spell_arcane", 0.004),), future_system="magic"),
        _passive("barrier", "结界术", "magic", ("magic",), "防护与结界魔法的基础技能。", (_effect("spell_barrier", 0.004),), future_system="magic"),
        _passive("elemental_guidance", "元素引导", "magic", ("magic",), "火、冰、雷元素魔法的基础技能。", (
            _effect("spell_fire", 0.004), _effect("spell_cold", 0.004), _effect("spell_lightning", 0.004),
        ), future_system="magic"),
        _passive("shadow_magic", "暗影术", "magic", ("magic",), "暗影和弱化魔法的基础技能。", (_effect("spell_shadow", 0.004),), future_system="magic"),
        _passive("ritual", "仪式", "magic", ("magic",), "与精灵献祭等召唤能力联动。", (_effect("summon_power_bonus", 0.003),), future_system="summoning"),

        # 意志系
        _passive("meditation", "冥想", "willpower", ("willpower",), "提高MP自然恢复速度。", (_effect("mp_regen_per_tick", 0.01),)),
        _passive("mana_limit", "魔力极限", "willpower", ("willpower",), "降低MP透支施法造成的反噬。", (_effect("mana_overcast_reduction", 0.005, 0.50),), future_system="magic"),
        _passive("blessing", "祝福术", "willpower", ("willpower",), "祝福、强化和部分控制魔法的基础技能。", (_effect("blessing_power_bonus", 0.004),), future_system="magic"),
        _passive("restoration", "恢复术", "willpower", ("willpower",), "治疗魔法的学习、施放和成长基础。", (_effect("healing_power_bonus", 0.005),), future_system="magic"),
        _passive("necromancy", "招魂术", "willpower", ("willpower",), "地狱、吸血和招魂魔法的基础技能。", (
            _effect("spell_hell", 0.004), _effect("summon_power_bonus", 0.004),
        ), future_system="magic"),
        _passive("mind_control", "精神控制", "willpower", ("willpower",), "精神、异常和精神屏障魔法的基础技能。", (_effect("spell_mind", 0.004),), future_system="magic"),
        _passive("silent_reading", "默读", "willpower", ("willpower",), "提高阅读时获得的魔法潜力。", (_effect("magic_potential_bonus", 0.005),), future_system="reading"),

        # 进阶武器技能
        _passive("noble_weapon", "贵族武器", "advanced", ("dexterity", "constitution"), "提高短剑和法杖的基础物理伤害。", (
            _effect("physical_damage_bonus", 0.002, weapon_types=("shortsword", "staff")),
        ), (("shortsword", 50), ("staff", 50))),
        _passive("cleric_weapon", "神官武器", "advanced", ("strength", "constitution"), "提高镰刀和钝器的基础物理伤害。", (
            _effect("physical_damage_bonus", 0.002, weapon_types=("scythe", "blunt")),
        ), (("scythe", 50), ("blunt", 50))),
        _passive("officer_weapon", "军官武器", "advanced", ("strength", "constitution"), "提高长剑和长枪的基础物理伤害。", (
            _effect("physical_damage_bonus", 0.002, weapon_types=("longsword", "spear")),
        ), (("longsword", 50), ("spear", 50))),
        _passive("hero_weapon", "勇者武器", "advanced", ("strength",), "提高斧头和格斗攻击的基础物理伤害。", (
            _effect("physical_damage_bonus", 0.002, weapon_types=("axe", "unarmed")),
        ), (("axe", 50), ("unarmed", 50))),

        SkillDefinition(
            skill_id="power_strike",
            name="强击",
            category="active",
            passive=False,
            compatible_weapon_modes=("one_hand", "sword_shield", "dual_wield", "two_hand_melee", "two_hand_heavy"),
            stamina_cost=25,
            cooldown_ticks=12,
            windup_ticks=2,
            recovery_ticks=3,
            damage_multiplier=1.6,
            bonus_knockback=15,
            governing_attributes=("strength",),
            description="消耗SP发动一次高伤害、高击退的近战攻击。",
        ),
    )
}

LEGACY_SKILL_ALIASES = {
    "长剑": "longsword", "斧": "axe", "斧头": "axe", "格斗": "unarmed",
    "镰刀": "scythe", "钝器": "blunt", "枪": "spear", "长枪": "spear",
    "法杖": "staff", "盾牌": "shield", "短剑": "shortsword", "弓": "bow",
    "弩": "crossbow", "枪械": "firearm", "轻甲": "light_armor",
    "中甲": "medium_armor", "重甲": "heavy_armor",
}
SKILL_NAME_TO_ID = {value.name: key for key, value in SKILL_DEFINITIONS.items()}
SKILL_NAME_TO_ID.update(LEGACY_SKILL_ALIASES)
INITIAL_SKILLS = (
    "longsword", "tactics", "shield", "light_armor", "dodge",
    "weightlifting", "power_strike",
)


def skill_id_for(value: str) -> str | None:
    value = (value or "").strip()
    return value if value in SKILL_DEFINITIONS else SKILL_NAME_TO_ID.get(value)


def skill_exp_required(level: int) -> int:
    return 50 + level * 15
