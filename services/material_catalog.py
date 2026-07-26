from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MaterialEffect:
    effect_type: str
    target: str
    value: float


@dataclass(frozen=True)
class MaterialDefinition:
    material_id: str
    name: str
    weight_multiplier: float
    effects: tuple[MaterialEffect, ...] = ()
    attack_factor: float = 1.0
    defense_factor: float = 1.0
    accuracy_factor: float = 1.0
    evasion_factor: float = 1.0


def _effect(effect_type: str, target: str, value: float) -> MaterialEffect:
    return MaterialEffect(effect_type, target, value)


def _material(
    material_id: str,
    name: str,
    weight: float,
    *effects: MaterialEffect,
) -> MaterialDefinition:
    attack, defense, accuracy, evasion = MATERIAL_COMBAT_FACTORS.get(
        material_id, (1.0, 1.0, 1.0, 1.0)
    )
    return MaterialDefinition(
        material_id,
        name,
        weight,
        tuple(effects),
        attack,
        defense,
        accuracy,
        evasion,
    )


# Relative to iron (=1.0), compressed to the project's short-combat scale.
# Values follow the Mobile material roles: soft/light materials favour DV,
# metals favour damage/PV, and magical materials favour hit/evasion.
MATERIAL_COMBAT_FACTORS = {
    "paper": (0.65, 0.65, 0.90, 1.30),
    "cloth": (0.65, 0.70, 0.92, 1.25),
    "silk": (0.72, 0.78, 1.05, 1.30),
    "mica": (0.75, 0.75, 1.12, 1.20),
    "spirit_cloth": (0.78, 0.82, 1.15, 1.28),
    "nightweave": (0.82, 0.85, 1.18, 1.25),
    "zylon": (0.82, 0.92, 1.08, 1.22),
    "griffin_scale": (0.90, 1.00, 1.05, 1.20),
    "ether": (1.05, 0.88, 1.30, 1.30),
    "organic": (0.80, 0.80, 0.90, 1.05),
    "leather": (0.82, 0.88, 0.95, 1.12),
    "bone": (0.92, 0.98, 0.90, 0.92),
    "obsidian": (1.10, 0.92, 0.92, 0.88),
    "glass": (1.00, 0.82, 1.15, 1.12),
    "scale": (0.98, 1.08, 0.92, 0.95),
    "coral": (0.92, 0.92, 1.05, 1.05),
    "bronze": (0.92, 0.95, 0.90, 0.90),
    "crystal": (1.12, 0.98, 1.18, 1.05),
    "titanium": (1.18, 1.20, 1.02, 0.92),
    "chain": (0.95, 1.08, 0.92, 0.92),
    "dragon_scale": (1.25, 1.35, 1.05, 1.02),
    "silver": (1.02, 1.02, 1.10, 1.00),
    "mithril": (1.22, 1.15, 1.20, 1.12),
    "pearl": (0.95, 0.95, 1.18, 1.08),
    "emerald": (1.15, 1.12, 1.12, 1.00),
    "ruby": (1.18, 1.10, 1.08, 0.98),
    "wood": (0.75, 0.82, 0.90, 1.00),
    "platinum": (1.08, 1.18, 1.02, 0.90),
    "steel": (1.12, 1.15, 0.98, 0.88),
    "iron": (1.00, 1.00, 1.00, 1.00),
    "gold": (1.05, 0.95, 1.05, 0.82),
    "lead": (0.88, 1.05, 0.75, 0.75),
    "chrome": (1.30, 1.32, 1.08, 0.92),
    "diamond": (1.40, 1.45, 1.12, 0.95),
    "adamantine": (1.45, 1.45, 0.95, 0.75),
}


MATERIAL_DEFINITIONS = {
    item.material_id: item
    for item in (
        _material("paper", "纸", 0.10, _effect("skill", "dodge", 2)),
        _material("cloth", "布", 0.20, _effect("resistance", "cold", 12)),
        _material("silk", "丝绸", 0.40, _effect("resistance", "mind", 25)),
        _material("mica", "云母", 0.40, _effect("advanced", "luck", 3)),
        _material("spirit_cloth", "灵布", 0.40, _effect("advanced", "speed", 5)),
        _material("nightweave", "宵晒", 0.45, _effect("advanced", "mana_growth", 5)),
        _material("zylon", "纤维（Zylon）", 0.50, _effect("resistance", "nature", 12)),
        _material("griffin_scale", "狮鹫鳞", 0.70, _effect("skill", "dodge", 2)),
        _material("ether", "以太", 0.80, _effect("advanced", "speed", 5)),
        _material("organic", "食材/生物", 1.00),
        _material("leather", "皮革", 1.00),
        _material("bone", "骨", 1.20, _effect("resistance", "hell", 50)),
        _material("obsidian", "黑曜石", 1.60),
        _material("glass", "玻璃", 1.80, _effect("advanced", "speed", 4)),
        _material("scale", "鳞", 1.80, _effect("resistance", "fire", 25)),
        _material("coral", "珊瑚", 1.80, _effect("resistance", "lightning", 50)),
        _material("bronze", "青铜", 2.00, _effect("resistance", "lightning", 25)),
        _material("crystal", "水晶", 2.00, _effect("primary", "magic", 3)),
        _material("titanium", "钛", 2.00, _effect("primary", "strength", 3)),
        _material("chain", "铁锁", 2.00, _effect("resistance", "shadow", 25)),
        _material(
            "dragon_scale", "龙鳞", 2.20,
            _effect("resistance", "fire", 25),
            _effect("resistance", "cold", 25),
        ),
        _material("silver", "银", 2.30, _effect("resistance", "hell", 25)),
        _material("mithril", "秘银", 2.40, _effect("skill", "magic_training", 2)),
        _material("pearl", "珍珠", 2.40, _effect("primary", "perception", 3)),
        _material("emerald", "绿宝石", 2.40, _effect("resistance", "mind", 50)),
        _material("ruby", "红宝石", 2.50, _effect("advanced", "life_growth", 3)),
        _material("wood", "木", 2.50),
        _material("platinum", "铂金", 2.60, _effect("resistance", "shadow", 50)),
        _material("steel", "钢", 2.70, _effect("primary", "willpower", 3)),
        _material("iron", "铁", 2.80, _effect("resistance", "fire", 12)),
        _material("gold", "金", 3.00, _effect("primary", "strength", 3)),
        _material("lead", "铅", 3.00),
        _material("chrome", "铬", 3.20, _effect("resistance", "nature", 50)),
        _material("diamond", "钻石", 3.30, _effect("resistance", "lightning", 50)),
        _material("adamantine", "精金", 3.60, _effect("primary", "constitution", 3)),
    )
}

MATERIAL_MULTIPLIERS = {
    material_id: {
        "weight": definition.weight_multiplier,
        "attack": definition.attack_factor,
        "defense": definition.defense_factor,
        "accuracy": definition.accuracy_factor,
        "evasion": definition.evasion_factor,
    }
    for material_id, definition in MATERIAL_DEFINITIONS.items()
}

BASE_WEIGHT_RANGES = {
    "head": (0.4, 2.0),
    "neck": (0.1, 0.1),
    "back": (0.4, 1.8),
    "body": (0.9, 7.5),
    "finger": (0.1, 0.1),
    "wrist": (0.2, 1.9),
    "waist": (0.6, 1.4),
    "feet": (0.3, 1.4),
    "shield": (1.0, 3.5),
    "hand_auxiliary": (0.5, 3.5),
    "melee_weapon": (0.4, 4.2),
    "ranged_weapon": (0.4, 2.8),
}


def material_for(material_id: str) -> MaterialDefinition:
    try:
        return MATERIAL_DEFINITIONS[material_id]
    except KeyError as exc:
        raise ValueError(f"未知装备材质：{material_id}") from exc


def actual_weight(base_weight: float, material_id: str) -> float:
    return float(base_weight) * material_for(material_id).weight_multiplier


def armor_style_for_weight(total_weight: float) -> str:
    if total_weight < 15:
        return "light"
    if total_weight <= 35:
        return "medium"
    return "heavy"


def weight_accuracy_multipliers(
    armor_style: str,
    overloaded: bool,
) -> tuple[float, float]:
    physical, spell = {
        "light": (1.00, 1.00),
        "medium": (0.95, 0.90),
        "heavy": (0.85, 0.75),
    }[armor_style]
    if overloaded:
        physical *= 0.85
        spell *= 0.85
    return physical, spell


def weight_category(item) -> str | None:
    if item.item_type == "shield":
        return "shield"
    if item.item_type == "weapon":
        return "ranged_weapon" if item.hand_mode == "two_hand_ranged" else "melee_weapon"
    if item.item_type == "hand_auxiliary":
        return "hand_auxiliary"
    if item.equip_slot in {"left_finger", "right_finger"}:
        return "finger"
    return item.equip_slot if item.equip_slot in BASE_WEIGHT_RANGES else None


def validate_base_weight(item, *, allow_exception: bool = False) -> None:
    if allow_exception:
        return
    category = weight_category(item)
    if not category:
        return
    minimum, maximum = BASE_WEIGHT_RANGES[category]
    weight = float(item.weight)
    if not minimum <= weight <= maximum:
        raise ValueError(
            f"{item.name}基础重量{weight:g}超出{category}范围"
            f"{minimum:g}–{maximum:g}"
        )
