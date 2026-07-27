"""Rebuild the data-driven Elona Mobile equipment additions.

The existing starter armory and custom turtle necklace are preserved.  Ordinary
equipment follows the Mobile traveller's diary ordering and is deliberately
compressed to this project's level 1-100 PvP scale.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "assets" / "equipment_catalog.json"

QUALITIES = [
    {"quality": "common", "weight": 45},
    {"quality": "excellent", "weight": 30},
    {"quality": "rare", "weight": 15},
    {"quality": "epic", "weight": 8},
    {"quality": "mythic", "weight": 2},
]

LIGHT_MATERIALS = [
    {"material": "paper", "weight": 1},
    {"material": "cloth", "weight": 5},
    {"material": "silk", "weight": 4},
    {"material": "mica", "weight": 2},
    {"material": "spirit_cloth", "weight": 3},
    {"material": "nightweave", "weight": 2},
    {"material": "zylon", "weight": 3},
    {"material": "griffin_scale", "weight": 2},
    {"material": "ether", "weight": 1},
    {"material": "leather", "weight": 6},
    {"material": "bone", "weight": 2},
    {"material": "glass", "weight": 1},
]

HEAVY_MATERIALS = [
    {"material": "bone", "weight": 2},
    {"material": "obsidian", "weight": 2},
    {"material": "scale", "weight": 3},
    {"material": "coral", "weight": 1},
    {"material": "bronze", "weight": 4},
    {"material": "titanium", "weight": 2},
    {"material": "chain", "weight": 4},
    {"material": "dragon_scale", "weight": 1},
    {"material": "silver", "weight": 2},
    {"material": "mithril", "weight": 2},
    {"material": "platinum", "weight": 2},
    {"material": "steel", "weight": 5},
    {"material": "iron", "weight": 8},
    {"material": "gold", "weight": 1},
    {"material": "lead", "weight": 2},
    {"material": "chrome", "weight": 1},
    {"material": "diamond", "weight": 1},
    {"material": "adamantine", "weight": 1},
]

ACCESSORY_MATERIALS = [
    {"material": "mica", "weight": 3},
    {"material": "glass", "weight": 2},
    {"material": "coral", "weight": 2},
    {"material": "crystal", "weight": 3},
    {"material": "silver", "weight": 4},
    {"material": "mithril", "weight": 3},
    {"material": "pearl", "weight": 3},
    {"material": "emerald", "weight": 2},
    {"material": "ruby", "weight": 2},
    {"material": "platinum", "weight": 3},
    {"material": "gold", "weight": 4},
    {"material": "diamond", "weight": 1},
]


def affix(kind: str, value: float, **extra) -> dict:
    return {"type": kind, "value": value, "capacity": 0, **extra}


def proc(
    ability_id: str,
    chance: float,
    source_power: int = 0,
    *,
    target: str = "enemy",
) -> dict:
    return affix(
        "trigger_ability",
        chance,
        ability_id=ability_id,
        source_power=source_power,
        target=target,
    )


def generated(
    catalog_id: int,
    template_id: str,
    name: str,
    item_type: str,
    slot: str,
    *,
    weight: float,
    material: str,
    materials: list[dict],
    hand_mode: str = "none",
    weapon_type: str = "",
    armor_type: str = "",
    base_stats: dict | None = None,
    inherent: list[dict] | None = None,
) -> dict:
    return {
        "id": catalog_id,
        "template_id": template_id,
        "name": name,
        "mode": "generated",
        "item_type": item_type,
        "equip_slot": slot,
        "hand_mode": hand_mode,
        "weapon_type": weapon_type,
        "armor_type": armor_type,
        "material": material,
        "weight": weight,
        "base_stats": base_stats or {},
        "inherent_affixes": inherent or [],
        "bound": True,
        "starter_grant": False,
        "starter_equip_slots": [],
        "generation": {
            "level_min": 1,
            "level_max": 100,
            "qualities": QUALITIES,
            "materials": materials,
        },
    }


def fixed_black_star(
    catalog_id: int,
    template_id: str,
    name: str,
    item_type: str,
    slot: str,
    *,
    material: str,
    weight: float,
    base_stats: dict,
    inherent: list[dict],
    description: str,
    source_effects: list[str] | None = None,
    hand_mode: str = "none",
    weapon_type: str = "",
    armor_type: str = "",
) -> dict:
    return {
        "id": catalog_id,
        "template_id": template_id,
        "name": name,
        "description": description,
        "source_effects": source_effects or [],
        "mode": "fixed",
        "item_type": item_type,
        "equip_slot": slot,
        "hand_mode": hand_mode,
        "weapon_type": weapon_type,
        "armor_type": armor_type,
        "material": material,
        "weight": weight,
        # Fixed artifacts intentionally keep their documented exceptional
        # base weights instead of ordinary-category validation ranges.
        "weight_range_exception": True,
        "base_stats": base_stats,
        "inherent_affixes": inherent,
        "bound": True,
        "starter_grant": False,
        "starter_equip_slots": [],
        "fixed": {
            "item_level": 40,
            "quality": "legendary",
            "star_type": "black_star",
            "blessing_state": "normal",
            "enhancement_level": 0,
            "enchant_capacity": 0,
            "used_capacity": 0,
            "random_affixes": [],
            "fusion_affixes": [],
        },
    }


WEAPONS = [
    # template, name, type, weight, power, accuracy, evasion, hand mode, affixes
    ("dagger", "短剑", "shortsword", .6, 3, 4, 2, "one_hand", [affix("critical_rate", .015)]),
    ("wakizashi", "忍刀", "shortsword", .7, 4, 2, 1, "one_hand", [affix("skill_level", 2, skill_id="shortsword")]),
    ("scimitar", "海贼刀", "shortsword", .9, 3, 3, 1, "one_hand", [affix("evasion", 2)]),
    ("kitchen_knife", "菜刀", "shortsword", .4, 4, 1, 0, "one_hand", [affix("critical_rate", .03)]),
    ("broken_bottle", "碎瓶", "shortsword", .4, 2, 1, 1, "one_hand", [affix("critical_rate", .02)]),
    ("greed_blade", "贪婪之刃", "shortsword", .5, 3, 4, 2, "one_hand", [affix("critical_rate", .02)]),
    ("katana", "刀", "longsword", 1.2, 5, 0, 0, "one_hand", [affix("damage_magic", 2)]),
    ("longsword", "长剑", "longsword", 1.5, 4, 2, 0, "one_hand", [affix("block_rate", .02)]),
    ("greatsword", "大剑", "longsword", 4.0, 7, 0, 0, "two_hand_heavy", [affix("block_rate", .05)]),
    ("lightsaber", "光剑", "longsword", .6, 4, 1, 0, "one_hand", [affix("spell_power", .05), affix("armor_penetration", .30)]),
    ("trident", "三叉戟", "spear", 1.8, 4, 1, 2, "two_hand_melee", [affix("damage_nature", 3)]),
    ("long_spear", "长枪", "spear", 2.5, 4, 1, 2, "two_hand_melee", [affix("resistance_magic", 12)]),
    ("halberd", "戟", "spear", 3.8, 6, 0, 0, "two_hand_melee", [affix("skill_level", 5, skill_id="two_handed")]),
    ("broom", "扫帚", "spear", 1.8, 4, 1, 3, "two_hand_melee", [affix("evasion", 3)]),
    ("hand_axe", "手斧", "axe", .9, 4, 1, 0, "one_hand", [affix("resistance_magic", 8)]),
    ("battle_axe", "战斧", "axe", 3.7, 7, -1, 0, "one_hand", [affix("knockback_resistance", .05)]),
    ("great_axe", "大斧", "axe", 3.5, 8, -1, 0, "one_hand", [affix("skill_level", 5, skill_id="weightlifting")]),
    ("scythe", "镰刀", "scythe", 1.4, 5, 1, 0, "one_hand", [affix("critical_rate", .02)]),
    ("great_scythe", "大镰刀", "scythe", 4.0, 7, 1, 0, "two_hand_heavy", [affix("critical_rate", .03)]),
    ("believer_scythe", "信徒镰刀", "scythe", 4.0, 6, 1, 0, "two_hand_heavy", [affix("melee_followup", .06)]),
    ("club", "棍棒", "blunt", 1.0, 4, 1, 0, "one_hand", [affix("skill_level", 5, skill_id="restoration")]),
    ("great_hammer", "大锤", "blunt", 4.2, 8, -2, 0, "two_hand_heavy", [affix("advanced_stat", 3, stat="life_growth")]),
    ("cane", "棍", "blunt", 1.0, 4, 1, 0, "one_hand", [affix("skill_level", 5, skill_id="blessing")]),
    ("miners_pick", "矿工锄", "blunt", 4.2, 7, -2, 0, "two_hand_heavy", [affix("skill_level", 5, skill_id="weightlifting")]),
    ("long_staff", "长棒", "staff", .8, 3, 1, 2, "one_hand", [affix("spell_power", .05)]),
    ("staff", "法杖", "staff", .9, 3, 2, 2, "one_hand", [affix("spell_power", .10), affix("skill_level", 5, skill_id="meditation")]),
    ("scepter", "权杖", "staff", .8, 5, 2, 2, "one_hand", [affix("spell_power", .05), affix("skill_level", 3, skill_id="elemental_guidance")]),
    ("spirit_staff", "灵力法杖", "staff", .9, 3, 2, 2, "one_hand", [affix("advanced_stat", 5, stat="mana_growth"), affix("spell_power", .10)]),
    ("sage_staff", "贤者法杖", "staff", .9, 3, 2, 2, "one_hand", [affix("stat_flat", 3, stat="magic"), affix("spell_power", .10)]),
    ("cunning_staff", "狡猾法杖", "staff", .9, 3, 2, 2, "one_hand", [affix("skill_level", 5, skill_id="magic_training")]),
    ("crystal_staff", "水晶法杖", "staff", .9, 3, 2, 2, "one_hand", [affix("spell_power", .10), affix("skill_level", 5, skill_id="elemental_guidance")]),
    ("shadow_staff", "暗影法杖", "staff", .9, 3, 2, 2, "one_hand", [affix("spell_power", .10), affix("skill_level", 5, skill_id="shadow_magic")]),
    ("bone_staff", "白骨法杖", "staff", .9, 3, 2, 2, "one_hand", [affix("spell_power", .10), affix("skill_level", 5, skill_id="necromancy")]),
    ("druid_staff", "德鲁伊法杖", "staff", .9, 3, 2, 2, "one_hand", [affix("spell_power", .10), affix("skill_level", 5, skill_id="natural_knowledge")]),
    ("confusion_staff", "混乱法杖", "staff", .9, 3, 2, 2, "one_hand", [affix("spell_power", .10), affix("skill_level", 5, skill_id="mind_control")]),
    ("short_bow", "短弓", "bow", .8, 3, 3, 0, "two_hand_ranged", [affix("evasion", 2)]),
    ("long_bow", "长弓", "bow", 1.2, 5, 2, 0, "two_hand_ranged", [affix("stat_flat", 3, stat="dexterity")]),
    ("bone_bow", "骨弓", "bow", .7, 7, -2, 0, "two_hand_ranged", [affix("resistance_hell", 50), affix("critical_rate", .03)]),
    ("crossbow", "机械弩", "crossbow", 2.8, 7, 1, 0, "two_hand_ranged", [affix("skill_level", 5, skill_id="heavy_armor")]),
    ("dwarf_crossbow", "矮人战弩", "crossbow", 2.8, 8, 1, 0, "two_hand_ranged", [affix("skill_level", 5, skill_id="weightlifting")]),
    ("stone", "小石子", "throwing", 2.0, 4, 0, 0, "two_hand_ranged", []),
    ("shuriken", "手里剑", "throwing", .4, 6, 2, 0, "two_hand_ranged", [affix("critical_rate", .02)]),
    ("grenade", "手榴弹", "throwing", .8, 5, 0, 0, "two_hand_ranged", [affix("ranged_followup", .05)]),
    ("panty", "内裤", "throwing", .5, 8, 0, 0, "two_hand_ranged", [affix("damage_mind", 5)]),
    ("playing_cards", "扑克牌", "throwing", .5, 3, 1, 0, "two_hand_ranged", [affix("critical_rate", .02)]),
    ("pistol", "手枪", "firearm", .8, 6, 3, 0, "two_hand_ranged", [affix("damage_fire", 3)]),
    ("machine_gun", "机关枪", "firearm", 1.8, 6, 4, 0, "two_hand_ranged", [affix("ranged_followup", .08)]),
    ("shotgun", "散弹枪", "firearm", 1.5, 7, 1, 0, "two_hand_ranged", [affix("critical_rate", .03)]),
    ("laser_gun", "光子枪", "firearm", 1.2, 7, 2, 0, "two_hand_ranged", [affix("stat_flat", 3, stat="perception"), affix("armor_penetration", .10)]),
]


ARMORS = [
    # template, name, slot, weight, armor type, armor, evasion, accuracy, pool, affixes
    ("feather_hat", "羽帽", "head", .5, "light", 1, 3, 0, LIGHT_MATERIALS, []),
    ("magic_hat", "魔法帽", "head", .5, "light", 2, 3, 0, LIGHT_MATERIALS, [affix("spell_power", .04)]),
    ("fairy_hat", "妖精帽", "head", .5, "light", 2, 4, 0, LIGHT_MATERIALS, [affix("status_immunity", .08)]),
    ("helm", "头盔", "head", 1.6, "heavy", 3, 1, 0, HEAVY_MATERIALS, []),
    ("knight_helm", "骑士头盔", "head", 1.8, "heavy", 4, 0, 0, HEAVY_MATERIALS, []),
    ("heavy_helm", "重型头盔", "head", 2.0, "heavy", 4, 2, 0, HEAVY_MATERIALS, []),
    ("composite_helm", "合金头盔", "head", 1.8, "heavy", 5, 2, 0, HEAVY_MATERIALS, []),
    ("peridot", "橄榄石", "neck", .1, "", 0, 0, 3, ACCESSORY_MATERIALS, []),
    ("talisman", "护符", "neck", .1, "", 0, 2, 0, ACCESSORY_MATERIALS, []),
    ("neck_guard", "护颈", "neck", .1, "", 2, 0, 0, ACCESSORY_MATERIALS, []),
    ("charm", "护身符", "neck", .1, "", 0, 0, 0, ACCESSORY_MATERIALS, [affix("damage_magic", 2)]),
    ("decorative_necklace", "装饰项链", "neck", .1, "", 0, 0, 0, ACCESSORY_MATERIALS, []),
    ("polished_necklace", "精工项链", "neck", .1, "", 0, 0, 0, ACCESSORY_MATERIALS, []),
    ("engagement_necklace", "结婚项链", "neck", .1, "", 0, 0, 0, ACCESSORY_MATERIALS, [affix("stat_flat", 1, stat="willpower")]),
    ("feather_back", "羽翼", "back", .4, "light", 0, 4, 0, LIGHT_MATERIALS, [affix("evasion", 2)]),
    ("wing", "翅膀", "back", .4, "light", 2, 2, 0, LIGHT_MATERIALS, [affix("evasion", 2)]),
    ("light_cloak", "轻披风", "back", .7, "light", 2, 2, 0, LIGHT_MATERIALS, []),
    ("armored_cloak", "防护披风", "back", 1.8, "medium", 3, 2, 0, LIGHT_MATERIALS, []),
    ("cloak", "披风", "back", 1.5, "light", 2, 4, 0, LIGHT_MATERIALS, []),
    ("vindale_cloak", "维达尔披风", "back", 1.5, "light", 2, 4, 0, LIGHT_MATERIALS, [affix("resistance_magic", 20)]),
    ("robe", "法衣", "body", .9, "light", 2, 5, 0, LIGHT_MATERIALS, [affix("spell_power", .03)]),
    ("pope_robe", "法王衣", "body", 1.2, "light", 4, 8, 0, LIGHT_MATERIALS, [affix("advanced_stat", 3, stat="mana_growth")]),
    ("mail", "铠甲", "body", 4.0, "heavy", 5, 3, 0, HEAVY_MATERIALS, []),
    ("heavy_mail", "厚铠", "body", 5.0, "heavy", 6, 2, 0, HEAVY_MATERIALS, []),
    ("ring_mail", "轮铠", "body", 5.5, "heavy", 7, 3, 0, HEAVY_MATERIALS, []),
    ("composite_mail", "合成铠甲", "body", 6.0, "heavy", 8, 3, 0, HEAVY_MATERIALS, []),
    ("banded_mail", "板条铠", "body", 6.8, "heavy", 9, 3, 0, HEAVY_MATERIALS, []),
    ("plate_mail", "重层铠", "body", 7.5, "heavy", 10, 2, 0, HEAVY_MATERIALS, []),
    ("light_mail", "轻甲", "body", 1.8, "light", 4, 5, 0, LIGHT_MATERIALS, []),
    ("coat", "胸甲", "body", 2.4, "medium", 5, 6, 0, LIGHT_MATERIALS, []),
    ("protective_clothes", "防护服", "body", 2.0, "light", 6, 7, 0, LIGHT_MATERIALS, []),
    ("bulletproof_jacket", "防弹衣", "body", 1.6, "light", 8, 6, 0, LIGHT_MATERIALS, [affix("resistance_fire", 12)]),
    ("decorative_ring", "装饰戒指", "left_finger", .1, "", 0, 0, 0, ACCESSORY_MATERIALS, []),
    ("ring", "戒指", "left_finger", .1, "", 0, 0, 0, ACCESSORY_MATERIALS, []),
    ("armored_ring", "守护戒指", "left_finger", .1, "", 2, 0, 0, ACCESSORY_MATERIALS, []),
    ("composite_ring", "合金戒指", "left_finger", .1, "", 0, 0, 0, ACCESSORY_MATERIALS, [affix("damage_magic", 2)]),
    ("engagement_ring", "结婚戒指", "left_finger", .1, "", 0, 0, 0, ACCESSORY_MATERIALS, [affix("stat_flat", 1, stat="willpower")]),
    ("aurora_ring", "极光戒指", "left_finger", .1, "", 2, 2, 0, ACCESSORY_MATERIALS, [affix("resistance_cold", 25), affix("resistance_lightning", 25)]),
    ("speed_ring", "速度戒指", "left_finger", .1, "", 0, 0, 0, ACCESSORY_MATERIALS, [affix("advanced_stat", 3, stat="speed")]),
    ("faith_ring", "信仰戒指", "left_finger", .1, "", 0, 0, 0, ACCESSORY_MATERIALS, [affix("stat_flat", 2, stat="willpower")]),
    ("light_gloves", "轻手套", "wrist", .2, "light", 2, 2, 2, LIGHT_MATERIALS, []),
    ("gloves", "手套", "wrist", .4, "light", 2, 3, 3, LIGHT_MATERIALS, []),
    ("decorated_gloves", "细工护手", "wrist", .6, "light", 3, 4, 4, LIGHT_MATERIALS, []),
    ("thick_gauntlets", "厚护手", "wrist", 1.0, "heavy", 3, 1, 2, HEAVY_MATERIALS, []),
    ("composite_gauntlets", "合成护手", "wrist", 1.4, "heavy", 4, 2, 3, HEAVY_MATERIALS, []),
    ("plate_gauntlets", "重层护手", "wrist", 1.9, "heavy", 5, 2, 3, HEAVY_MATERIALS, []),
    ("girdle", "腰带", "waist", .9, "light", 2, 2, 0, LIGHT_MATERIALS, []),
    ("composite_girdle", "合成腰带", "waist", 1.1, "medium", 3, 3, 0, HEAVY_MATERIALS, []),
    ("plate_girdle", "重层腰带", "waist", 1.4, "heavy", 4, 2, 0, HEAVY_MATERIALS, []),
    ("footwear", "鞋子", "feet", .3, "light", 1, 2, 0, LIGHT_MATERIALS, [affix("advanced_stat", 1, stat="speed")]),
    ("shoes", "靴", "feet", .5, "light", 2, 3, 0, LIGHT_MATERIALS, []),
    ("tight_boots", "厚靴", "feet", .7, "light", 2, 4, 0, LIGHT_MATERIALS, []),
    ("heavy_boots", "重靴", "feet", 1.0, "heavy", 3, 1, 0, HEAVY_MATERIALS, []),
    ("composite_boots", "合成靴", "feet", 1.2, "heavy", 4, 1, 0, HEAVY_MATERIALS, []),
    ("armored_boots", "装甲靴", "feet", 1.4, "heavy", 5, 0, 0, HEAVY_MATERIALS, []),
    ("seven_league_boots", "七团靴", "feet", .7, "light", 2, 3, 0, LIGHT_MATERIALS, [affix("advanced_stat", 4, stat="speed")]),
    ("small_shield", "小盾", "off_hand", 1.0, "", 3, 1, 0, HEAVY_MATERIALS, [affix("block_rate", .20)]),
    ("round_shield", "圆盾", "off_hand", 1.4, "", 4, 2, 0, HEAVY_MATERIALS, [affix("block_rate", .20)]),
    ("shield", "盾牌", "off_hand", 1.8, "", 5, 1, 0, HEAVY_MATERIALS, [affix("block_rate", .20)]),
    ("knight_shield", "骑士盾", "off_hand", 2.2, "", 6, -1, 0, HEAVY_MATERIALS, [affix("block_rate", .20)]),
    ("composite_shield", "合成盾", "off_hand", 2.6, "", 6, 1, 0, HEAVY_MATERIALS, [affix("block_rate", .20)]),
    ("large_shield", "长盾", "off_hand", 3.5, "", 8, -1, 0, HEAVY_MATERIALS, [affix("block_rate", .20)]),
    ("tower_shield", "重层盾", "off_hand", 3.2, "", 7, -1, 0, HEAVY_MATERIALS, [affix("block_rate", .20)]),
]


BLACK_STARS = [
    ("ether_dagger", "以太匕首", "weapon", "main_hand", "ether", .2, {"weapon_power": 8, "accuracy": 5, "evasion": 4}, [affix("advanced_stat", 3, stat="speed"), affix("damage_lightning", 4), affix("resistance_lightning", 75), proc("elemental_scar", .10, 200)], "被风缠绕的短剑。", ["加速以太病的发展"], "one_hand", "shortsword", ""),
    ("lucky_dagger", "幸运短剑", "weapon", "main_hand", "mica", .4, {"weapon_power": 7, "accuracy": 5, "evasion": 6}, [affix("advanced_stat", 9, stat="luck"), affix("stamina_steal", .10)], "薄如蝉翼的短剑。", ["防止物品被盗", "识破隐形", "钓鱼技能+18"], "one_hand", "shortsword", ""),
    ("claymore", "克莱莫", "weapon", "main_hand", "silver", 6.5, {"weapon_power": 11, "accuracy": 1}, [affix("critical_rate", .05), affix("armor_penetration", .04), affix("resistance_hell", 25)], "粗犷而厚重的大剑。", ["保护持有者免受变异"], "two_hand_heavy", "longsword", ""),
    ("diabolos", "暗黑破坏神", "weapon", "main_hand", "obsidian", 2.2, {"weapon_power": 9, "accuracy": 1, "evasion": -1}, [affix("stat_flat", 3, stat="willpower"), affix("advanced_stat", 4, stat="speed"), affix("damage_mind", 5), affix("status_resistance_paralysis", .75), proc("time_stop", .04, 200)], "通体漆黑的剑。", [], "one_hand", "longsword", ""),
    ("zantetsu", "斩铁剑", "weapon", "main_hand", "silver", 1.4, {"weapon_power": 8, "accuracy": 2}, [affix("stat_flat", 4, stat="strength"), affix("armor_penetration", .08), affix("resistance_hell", 25), affix("resistance_mind", 60)], "闪耀银白光辉的刀剑。", [], "one_hand", "longsword", ""),
    ("ragnarok", "兰格纳洛克", "weapon", "main_hand", "obsidian", 4.2, {"weapon_power": 12, "accuracy": 3}, [proc("ragnarok", .05)], "会为世界带来终末的剑。", ["稀有装备发现率+15%"], "two_hand_heavy", "longsword", ""),
    ("rankis", "兰基斯", "weapon", "main_hand", "iron", 2.0, {"weapon_power": 9, "accuracy": 1, "evasion": 1}, [affix("damage_hell", 5), affix("resistance_fire", 12), affix("resistance_hell", 90), proc("time_stop", .06)], "蕴含地狱之力的长柄武器。", [], "two_hand_melee", "spear", ""),
    ("holy_lance", "圣枪", "weapon", "main_hand", "silver", 4.4, {"weapon_power": 10, "accuracy": 5, "evasion": 1}, [affix("stat_flat", 7, stat="willpower"), affix("resistance_shadow", 30), affix("resistance_hell", 60), proc("holy_veil", .16, 450, target="self"), proc("healing_rain", .14, 350, target="self")], "被神圣力量祝福的长枪。", [], "two_hand_heavy", "spear", ""),
    ("axe_of_destruction", "破坏之斧", "weapon", "main_hand", "ruby", 14.0, {"weapon_power": 14, "accuracy": -6}, [affix("max_hp", 30), affix("critical_rate", .15)], "足以粉碎万物的巨斧。", [], "two_hand_heavy", "axe", ""),
    ("void_scythe", "虚无之镰", "weapon", "main_hand", "iron", 9.0, {"weapon_power": 10}, [affix("spell_power", .25), affix("resistance_magic", 75), affix("resistance_fire", 12), affix("mana_steal", .08), affix("execute_chance", .08)], "奏响死亡旋律的镰刀。", ["使持有者漂浮"], "two_hand_heavy", "scythe", ""),
    ("kumiromi_scythe", "库米罗米镰刀", "weapon", "main_hand", "spirit_cloth", .8, {"weapon_power": 8, "evasion": 3}, [affix("stat_flat", 6, stat="strength"), affix("advanced_stat", 3, stat="speed"), affix("resistance_mind", 100), affix("execute_chance", .08)], "收获之神持有的镰刀。", ["能够消化腐烂食物", "采矿技能+17", "园艺技能+23", "烹饪技能+13"], "one_hand", "scythe", ""),
    ("hammer_of_earth", "大地之大锤", "weapon", "main_hand", "adamantine", 6.5, {"weapon_power": 12, "accuracy": -1}, [affix("stat_flat", 7, stat="strength"), affix("stat_flat", 2, stat="constitution"), affix("resistance_mind", 100), affix("skill_level", 4, skill_id="two_handed"), proc("hero", .18, 500, target="self")], "由大地孕育的巨锤。", [], "two_hand_heavy", "blunt", ""),
    ("elemental_staff", "元素法杖", "weapon", "main_hand", "obsidian", .9, {"weapon_power": 6, "armor_power": 2, "accuracy": 4, "evasion": 4}, [affix("damage_fire", 4), affix("damage_cold", 4), affix("damage_lightning", 4), affix("resistance_fire", 75), affix("resistance_cold", 75), affix("resistance_lightning", 75), proc("elemental_scar", .15, 400)], "蕴含三大元素力量的法杖。", ["稀有装备发现率+15%"], "one_hand", "staff", ""),
    ("bow_of_vindale", "异形森林之弓", "weapon", "main_hand", "mithril", 1.2, {"weapon_power": 10, "accuracy": 4}, [affix("stat_flat", 5, stat="dexterity"), affix("resistance_nature", 90), affix("skill_level", 1, skill_id="magic_training"), proc("equipment_poison", .15), proc("dimensional_hand", .08, 100)], "射击时会泛起波纹的弓。", [], "two_hand_ranged", "bow", ""),
    ("wind_bow", "风之弓", "weapon", "main_hand", "ether", .8, {"weapon_power": 9, "accuracy": 1}, [affix("advanced_stat", 7, stat="speed"), affix("resistance_lightning", 90), proc("lulwy_possession", .10, 200, target="self"), proc("haste", .10, 200, target="self")], "被风缠绕的长弓。", ["加速以太病的发展"], "two_hand_ranged", "bow", ""),
    ("winchester_premium", "温彻斯特豪华版", "weapon", "main_hand", "diamond", 2.8, {"weapon_power": 12, "accuracy": 5}, [affix("armor_penetration", .30), affix("status_resistance", .35), affix("resistance_lightning", 30), affix("resistance_mind", 90), affix("skill_level", 4, skill_id="firearm"), proc("silence_fog", .08, 100)], "自古流传的华丽散弹枪。", [], "two_hand_ranged", "firearm", ""),
    ("rail_gun", "轨道炮", "weapon", "main_hand", "ether", 8.5, {"weapon_power": 14, "accuracy": -3}, [affix("armor_penetration", .05), affix("advanced_stat", 3, stat="speed"), affix("damage_mind", 4), affix("resistance_mind", 100), proc("roaring_wave", .14, 350)], "为屠戮而生的巨型枪械。", ["加速以太病的发展"], "two_hand_ranged", "firearm", ""),
    ("sage_helm", "贤者头盔", "armor", "head", "mithril", 1.5, {"armor_power": 8, "evasion": 4}, [affix("stat_flat", 3, stat="magic"), affix("status_resistance_confusion", .75), affix("resistance_magic", 45), affix("resistance_mind", 75), affix("skill_level", 1, skill_id="magic_training")], "散发耀眼光辉的贤者头盔。", ["识破隐形", "解剖学技能+7"], "none", "", "medium"),
    ("aurora_ring_black_star", "极光戒指", "accessory", "left_finger", "mithril", .1, {"armor_power": 3, "evasion": 3}, [affix("resistance_mind", 60)], "如极光般流转着光辉的戒指。", ["免疫恶劣天气"], "none", "", ""),
    ("seven_league_boots_black_star", "七团靴", "armor", "feet", "zylon", .7, {"armor_power": 4, "evasion": 5}, [], "据说能一步跨越七里路的靴子。", ["世界地图旅行速度+63%"], "none", "", "light"),
]


def build() -> dict:
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    preserved = [item for item in raw["items"] if int(item["id"]) < 3000]
    ordinary = []
    for offset, row in enumerate(WEAPONS):
        template, name, weapon_type, weight, power, accuracy, evasion, hand_mode, inherent = row
        stats = {"weapon_power": power}
        if accuracy:
            stats["accuracy"] = accuracy
        if evasion:
            stats["evasion"] = evasion
        pool = LIGHT_MATERIALS if weight <= 1.5 else HEAVY_MATERIALS
        ordinary.append(
            generated(
                3001 + offset,
                f"elona_{template}",
                name,
                "weapon",
                "main_hand",
                weight=weight,
                material="leather" if pool is LIGHT_MATERIALS else "iron",
                materials=pool,
                hand_mode=hand_mode,
                weapon_type=weapon_type,
                base_stats=stats,
                inherent=inherent,
            )
        )
    for offset, row in enumerate(ARMORS, start=len(WEAPONS)):
        template, name, slot, weight, armor_type, armor, evasion, accuracy, pool, inherent = row
        item_type = "shield" if slot == "off_hand" else (
            "accessory" if not armor_type else "armor"
        )
        stats = {}
        if armor:
            stats["armor_power"] = armor
        if evasion:
            stats["evasion"] = evasion
        if accuracy:
            stats["accuracy"] = accuracy
        ordinary.append(
            generated(
                3001 + offset,
                f"elona_{template}",
                name,
                item_type,
                slot,
                weight=weight,
                material="silver" if item_type == "accessory" else (
                    "leather" if pool is LIGHT_MATERIALS else "iron"
                ),
                materials=pool,
                hand_mode="shield" if item_type == "shield" else "none",
                armor_type=armor_type,
                base_stats=stats,
                inherent=inherent,
            )
        )
    black_stars = []
    for offset, row in enumerate(BLACK_STARS):
        (
            template, name, item_type, slot, material, weight, stats, inherent,
            description, source_effects, hand_mode, weapon_type, armor_type,
        ) = row
        black_stars.append(
            fixed_black_star(
                4001 + offset,
                f"black_star_{template}",
                name,
                item_type,
                slot,
                material=material,
                weight=weight,
                base_stats=stats,
                inherent=inherent,
                description=description,
                source_effects=source_effects,
                hand_mode=hand_mode,
                weapon_type=weapon_type,
                armor_type=armor_type,
            )
        )
    if len(WEAPONS) != 49 or len(ARMORS) != 63:
        raise RuntimeError(
            f"unexpected ordinary counts: {len(WEAPONS)} weapons, "
            f"{len(ARMORS)} armor/accessories"
        )
    if len(BLACK_STARS) != 20:
        raise RuntimeError(f"unexpected black-star count: {len(BLACK_STARS)}")
    return {
        "schema_version": 2,
        "items": preserved + ordinary + black_stars,
    }


if __name__ == "__main__":
    catalog = build()
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(catalog['items'])} equipment entries to {CATALOG_PATH}")
