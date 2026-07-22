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


def _effect(effect_type: str, target: str, value: float) -> MaterialEffect:
    return MaterialEffect(effect_type, target, value)


def _material(
    material_id: str,
    name: str,
    weight: float,
    *effects: MaterialEffect,
) -> MaterialDefinition:
    return MaterialDefinition(material_id, name, weight, tuple(effects))


MATERIAL_DEFINITIONS = {
    item.material_id: item
    for item in (
        _material("paper", "纸", 0.10, _effect("skill", "dodge", 2)),
        _material("cloth", "布", 0.20, _effect("resistance", "cold", 0.12)),
        _material("silk", "丝绸", 0.40, _effect("resistance", "mind", 0.25)),
        _material("mica", "云母", 0.40, _effect("advanced", "luck", 3)),
        _material("spirit_cloth", "灵布", 0.40, _effect("advanced", "speed", 5)),
        _material("nightweave", "宵晒", 0.45, _effect("advanced", "mana_growth", 5)),
        _material("zylon", "纤维（Zylon）", 0.50, _effect("resistance", "nature", 0.12)),
        _material("griffin_scale", "狮鹫鳞", 0.70, _effect("skill", "dodge", 2)),
        _material("ether", "以太", 0.80, _effect("advanced", "speed", 5)),
        _material("organic", "食材/生物", 1.00),
        _material("leather", "皮革", 1.00),
        _material("bone", "骨", 1.20, _effect("resistance", "hell", 0.50)),
        _material("obsidian", "黑曜石", 1.60),
        _material("glass", "玻璃", 1.80, _effect("advanced", "speed", 4)),
        _material("scale", "鳞", 1.80, _effect("resistance", "fire", 0.25)),
        _material("coral", "珊瑚", 1.80, _effect("resistance", "lightning", 0.50)),
        _material("bronze", "青铜", 2.00, _effect("resistance", "lightning", 0.25)),
        _material("crystal", "水晶", 2.00, _effect("primary", "magic", 3)),
        _material("titanium", "钛", 2.00, _effect("primary", "strength", 3)),
        _material("chain", "铁锁", 2.00, _effect("resistance", "shadow", 0.25)),
        _material(
            "dragon_scale", "龙鳞", 2.20,
            _effect("resistance", "fire", 0.25),
            _effect("resistance", "cold", 0.25),
        ),
        _material("silver", "银", 2.30, _effect("resistance", "hell", 0.25)),
        _material("mithril", "秘银", 2.40, _effect("skill", "magic_training", 2)),
        _material("pearl", "珍珠", 2.40, _effect("primary", "perception", 3)),
        _material("emerald", "绿宝石", 2.40, _effect("resistance", "mind", 0.50)),
        _material("ruby", "红宝石", 2.50, _effect("advanced", "life_growth", 3)),
        _material("wood", "木", 2.50),
        _material("platinum", "铂金", 2.60, _effect("resistance", "shadow", 0.50)),
        _material("steel", "钢", 2.70, _effect("primary", "willpower", 3)),
        _material("iron", "铁", 2.80, _effect("resistance", "fire", 0.12)),
        _material("gold", "金", 3.00, _effect("primary", "strength", 3)),
        _material("lead", "铅", 3.00),
        _material("chrome", "铬", 3.20, _effect("resistance", "nature", 0.50)),
        _material("diamond", "钻石", 3.30, _effect("resistance", "lightning", 0.50)),
        _material("adamantine", "精金", 3.60, _effect("primary", "constitution", 3)),
    )
}

MATERIAL_MULTIPLIERS = {
    material_id: {"weight": definition.weight_multiplier}
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
