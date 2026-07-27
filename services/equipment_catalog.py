from __future__ import annotations

import json
import random
from dataclasses import dataclass, field, replace
from pathlib import Path

try:
    from ..models.equipment import (
        EQUIPMENT_SLOTS,
        EquipmentItem,
        EquipmentTemplate,
    )
    from .equipment_affixes import skill_level_affix_cap
    from .material_catalog import material_for, validate_base_weight
except ImportError:
    from models.equipment import EQUIPMENT_SLOTS, EquipmentItem, EquipmentTemplate
    from services.equipment_affixes import skill_level_affix_cap
    from services.material_catalog import material_for, validate_base_weight


QUALITY_LABELS = {
    "common": "普通",
    "excellent": "优秀",
    "rare": "精良",
    "epic": "史诗",
    "mythic": "传说",
    "legendary": "传奇",
}
QUALITY_MULTIPLIERS = {
    "common": 0.67,
    "excellent": 0.83,
    "rare": 1.00,
    "epic": 1.25,
    "mythic": 1.40,
    "legendary": 1.0,
}
QUALITY_COLORS = {
    "common": "白色",
    "excellent": "绿色",
    "rare": "蓝色",
    "epic": "紫色",
    "mythic": "金黄色",
    "legendary": "红色",
}
ITEM_TYPES = {"weapon", "shield", "armor", "accessory"}
WEAPON_TYPES = {
    "longsword",
    "shortsword",
    "axe",
    "spear",
    "unarmed",
    "scythe",
    "blunt",
    "staff",
    "throwing",
    "bow",
    "crossbow",
    "firearm",
}
HAND_MODES = {
    "none",
    "one_hand",
    "shield",
    "two_hand_heavy",
    "two_hand_melee",
    "two_hand_ranged",
}
ARMOR_TYPES = {"", "light", "medium", "heavy"}
BLESSING_STATES = {"normal", "blessed", "cursed", "corrupted"}
STAR_TYPES = {"none", "white_star", "black_star"}
DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "equipment_catalog.json"
)


@dataclass(frozen=True)
class EquipmentCatalogEntry:
    catalog_id: int
    mode: str
    template: EquipmentTemplate
    bound: bool = True
    starter_grant: bool = False
    starter_equip_slots: tuple[str, ...] = ()
    fixed: dict = field(default_factory=dict)
    generation: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EquipmentCatalogSnapshot:
    schema_version: int
    entries: tuple[EquipmentCatalogEntry, ...]
    by_id: dict[int, EquipmentCatalogEntry]
    by_template_id: dict[str, EquipmentCatalogEntry]

    @property
    def starter_entries(self) -> tuple[EquipmentCatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.starter_grant)


class EquipmentCatalog:
    """Load and atomically reload the immutable equipment catalog snapshot."""

    def __init__(self, path: str | Path = DEFAULT_CATALOG_PATH):
        self.path = Path(path)
        self._snapshot = load_equipment_catalog(self.path)

    @property
    def snapshot(self) -> EquipmentCatalogSnapshot:
        return self._snapshot

    def get(self, catalog_id: int) -> EquipmentCatalogEntry:
        try:
            return self._snapshot.by_id[int(catalog_id)]
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"装备表中不存在 ID {catalog_id}") from None

    def reload(self) -> EquipmentCatalogSnapshot:
        candidate = load_equipment_catalog(self.path)
        self._snapshot = candidate
        return candidate


class EquipmentFactory:
    AFFIX_TYPES = (
        "stat_flat",
        "advanced_stat",
        "skill_level",
        "element_resistance",
        "block_rate",
        "knockback_resistance",
        "melee_followup",
        "ranged_followup",
        "accuracy",
        "evasion",
        "critical_rate",
        "resistance_fire",
        "resistance_cold",
        "resistance_lightning",
        "resistance_shadow",
        "resistance_nature",
        "resistance_mind",
        "resistance_hell",
        "resistance_magic",
        "damage_magic",
        "damage_fire",
        "damage_cold",
        "damage_lightning",
        "damage_shadow",
        "damage_nature",
        "damage_mind",
        "damage_hell",
        "armor_penetration",
        "life_steal",
        "spell_power",
        "status_immunity",
        "max_hp",
        "trigger_ability",
        "status_resistance",
        "status_resistance_paralysis",
        "status_resistance_confusion",
        "stamina_steal",
        "mana_steal",
        "execute_chance",
    )

    def create_from_catalog(
        self,
        owner_pk: int,
        entry: EquipmentCatalogEntry,
        seed: int,
    ) -> EquipmentItem:
        if entry.mode == "fixed":
            fixed = entry.fixed
            return EquipmentItem(
                None,
                owner_pk,
                entry.template.template_id,
                entry.template.name,
                entry.template.item_type,
                entry.template.equip_slot,
                entry.template.hand_mode,
                entry.template.weapon_type,
                entry.template.armor_type,
                int(fixed["item_level"]),
                str(fixed["quality"]),
                str(fixed["star_type"]),
                entry.template.material,
                str(fixed["blessing_state"]),
                int(fixed["enhancement_level"]),
                float(entry.template.weight),
                int(fixed["enchant_capacity"]),
                int(fixed["used_capacity"]),
                dict(entry.template.base_stats),
                tuple(entry.template.inherent_affixes),
                tuple(fixed["random_affixes"]),
                tuple(fixed["fusion_affixes"]),
                entry.bound,
                entry.template.description,
                entry.template.source_effects,
            )

        rng = random.Random(seed)
        generation = entry.generation
        item_level = rng.randint(
            int(generation["level_min"]),
            int(generation["level_max"]),
        )
        qualities = generation["qualities"]
        quality = rng.choices(
            [item["quality"] for item in qualities],
            weights=[float(item["weight"]) for item in qualities],
            k=1,
        )[0]
        materials = generation.get("materials", ())
        material = (
            rng.choices(
                [item["material"] for item in materials],
                weights=[float(item["weight"]) for item in materials],
                k=1,
            )[0]
            if materials
            else entry.template.material
        )
        item = self.generate(
            owner_pk,
            replace(entry.template, material=material),
            item_level,
            quality,
            rng.getrandbits(63),
        )
        return replace(item, bound=entry.bound)

    def generate(
        self,
        owner_pk: int,
        template: EquipmentTemplate,
        item_level: int,
        quality: str,
        seed: int,
    ) -> EquipmentItem:
        material_for(template.material)
        validate_base_weight(
            template,
            allow_exception=template.weight_range_exception,
        )
        if not 0 <= item_level <= 100:
            raise ValueError("装备等级必须在0到100之间")
        if quality not in QUALITY_MULTIPLIERS:
            raise ValueError("未知装备品质")
        if quality == "legendary":
            raise ValueError("黑星装备必须来自固定模板")
        rng = random.Random(seed)
        star_type = (
            "white_star" if quality in {"epic", "mythic"} else "none"
        )
        affix_count = {
            "common": 0,
            "excellent": 1,
            "rare": 2,
            "epic": 3,
            "mythic": 4,
        }[quality]
        random_affixes = []
        generated_types = tuple(
            item
            for item in self.AFFIX_TYPES
            if item not in {
                "advanced_stat",
                "element_resistance",
                "spell_power",
                "status_immunity",
                "trigger_ability",
                "status_resistance",
                "status_resistance_paralysis",
                "status_resistance_confusion",
                "stamina_steal",
                "mana_steal",
                "execute_chance",
                "max_hp",
            }
        )
        for _ in range(affix_count):
            kind = rng.choice(generated_types)
            affix = {"type": kind, "capacity": 1}
            if kind == "stat_flat":
                affix.update(
                    stat=rng.choice(
                        (
                            "strength",
                            "constitution",
                            "dexterity",
                            "perception",
                            "magic",
                            "willpower",
                        )
                    ),
                    value=rng.randint(1, 3),
                )
            elif kind == "skill_level":
                affix.update(
                    skill_id=rng.choice(
                        (
                            "longsword",
                            "shortsword",
                            "axe",
                            "spear",
                            "bow",
                            "firearm",
                            "throwing",
                            "tactics",
                        )
                    ),
                    value=rng.randint(1, skill_level_affix_cap(item_level)),
                )
            elif kind.startswith("resistance_"):
                affix["value"] = rng.randint(10, 50)
            elif kind in {"accuracy", "evasion"} or kind.startswith("damage_"):
                affix["value"] = rng.randint(1, 5)
            elif kind in {"armor_penetration", "life_steal"}:
                affix["value"] = round(rng.uniform(0.02, 0.08), 3)
            else:
                affix["value"] = round(rng.uniform(0.02, 0.10), 3)
            random_affixes.append(affix)
        capacity = {
            "common": 0,
            "excellent": 2,
            "rare": 4,
            "epic": 7,
            "mythic": 10,
        }[quality]
        return EquipmentItem(
            None,
            owner_pk,
            template.template_id,
            template.name,
            template.item_type,
            template.equip_slot,
            template.hand_mode,
            template.weapon_type,
            template.armor_type,
            item_level,
            quality,
            star_type,
            template.material,
            "normal",
            0,
            template.weight,
            capacity,
            affix_count,
            dict(template.base_stats),
            template.inherent_affixes,
            tuple(random_affixes),
            (),
            True,
            template.description,
            template.source_effects,
        )


def load_equipment_catalog(path: str | Path) -> EquipmentCatalogSnapshot:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法加载装备表 {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") not in {1, 2}:
        raise ValueError("装备表 schema_version 必须为 1 或 2")
    schema_version = int(raw["schema_version"])
    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("装备表 items 必须是非空数组")

    entries = []
    ids = set()
    template_ids = set()
    occupied_starter_slots = set()
    for index, raw_item in enumerate(raw_items):
        label = f"items[{index}]"
        if not isinstance(raw_item, dict):
            raise ValueError(f"{label} 必须是对象")
        catalog_id = raw_item.get("id")
        if not isinstance(catalog_id, int) or isinstance(catalog_id, bool) or catalog_id <= 0:
            raise ValueError(f"{label}.id 必须是正整数")
        if catalog_id in ids:
            raise ValueError(f"装备表 ID 重复: {catalog_id}")
        ids.add(catalog_id)

        template_id = str(raw_item.get("template_id", "")).strip()
        if not template_id:
            raise ValueError(f"{label}.template_id 不能为空")
        if template_id in template_ids:
            raise ValueError(f"装备表 template_id 重复: {template_id}")
        template_ids.add(template_id)

        mode = str(raw_item.get("mode", ""))
        if mode not in {"fixed", "generated"}:
            raise ValueError(f"{label}.mode 必须是 fixed 或 generated")
        item_type = str(raw_item.get("item_type", ""))
        if item_type not in ITEM_TYPES:
            raise ValueError(f"{label}.item_type 无效")
        equip_slot = str(raw_item.get("equip_slot", ""))
        if equip_slot not in EQUIPMENT_SLOTS:
            raise ValueError(f"{label}.equip_slot 无效")
        hand_mode = str(raw_item.get("hand_mode", "none"))
        if hand_mode not in HAND_MODES:
            raise ValueError(f"{label}.hand_mode 无效")
        armor_type = str(raw_item.get("armor_type", ""))
        if armor_type not in ARMOR_TYPES:
            raise ValueError(f"{label}.armor_type 无效")
        weapon_type = str(raw_item.get("weapon_type", ""))
        if item_type == "weapon" and weapon_type not in WEAPON_TYPES:
            raise ValueError(f"{label}.weapon_type 无效")
        if item_type != "weapon" and weapon_type:
            raise ValueError(f"{label} 非武器不能设置 weapon_type")
        if item_type == "weapon" and hand_mode in {"none", "shield"}:
            raise ValueError(f"{label} 武器 hand_mode 无效")
        if item_type == "weapon" and equip_slot not in {"main_hand", "off_hand"}:
            raise ValueError(f"{label} 武器必须使用手部槽位")
        if hand_mode == "shield" and item_type != "shield":
            raise ValueError(f"{label} 盾牌模式只能用于 shield")
        if item_type == "shield" and (
            hand_mode != "shield" or equip_slot != "off_hand"
        ):
            raise ValueError(f"{label} 盾牌必须使用 shield 模式和 off_hand")
        if item_type == "armor" and not armor_type:
            raise ValueError(f"{label}.armor_type 不能为空")
        if item_type != "armor" and armor_type:
            raise ValueError(f"{label} 非护甲不能设置 armor_type")
        if hand_mode.startswith("two_hand") and equip_slot != "main_hand":
            raise ValueError(f"{label} 双手武器必须使用 main_hand")

        base_stats = raw_item.get("base_stats", {})
        if not isinstance(base_stats, dict) or not all(
            isinstance(key, str)
            and isinstance(value, int | float)
            and not isinstance(value, bool)
            for key, value in base_stats.items()
        ):
            raise ValueError(f"{label}.base_stats 必须是数值对象")
        inherent_affixes = _validate_affixes(
            raw_item.get("inherent_affixes", []),
            f"{label}.inherent_affixes",
        )
        description = raw_item.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"{label}.description 必须是字符串")
        raw_source_effects = raw_item.get("source_effects", [])
        if (
            not isinstance(raw_source_effects, list)
            or not all(
                isinstance(effect, str) and effect.strip()
                for effect in raw_source_effects
            )
        ):
            raise ValueError(f"{label}.source_effects 必须是非空字符串数组")
        source_effects = tuple(effect.strip() for effect in raw_source_effects)
        if len(set(source_effects)) != len(source_effects):
            raise ValueError(f"{label}.source_effects 不能重复")
        template = EquipmentTemplate(
            template_id=template_id,
            name=str(raw_item.get("name", "")).strip(),
            item_type=item_type,
            equip_slot=equip_slot,
            hand_mode=hand_mode,
            weapon_type=weapon_type,
            armor_type=armor_type,
            material=str(raw_item.get("material", "")),
            weight=float(raw_item.get("weight", -1)),
            base_stats=dict(base_stats),
            inherent_affixes=inherent_affixes,
            weight_range_exception=bool(
                raw_item.get("weight_range_exception", False)
            ),
            description=description.strip(),
            source_effects=source_effects,
        )
        if not template.name:
            raise ValueError(f"{label}.name 不能为空")
        material_for(template.material)
        validate_base_weight(
            template,
            allow_exception=template.weight_range_exception,
        )

        starter_grant = raw_item.get("starter_grant", False)
        if not isinstance(starter_grant, bool):
            raise ValueError(f"{label}.starter_grant 必须是布尔值")
        starter_slots_raw = raw_item.get("starter_equip_slots", [])
        if not isinstance(starter_slots_raw, list):
            raise ValueError(f"{label}.starter_equip_slots 必须是数组")
        starter_slots = tuple(str(slot) for slot in starter_slots_raw)
        if not starter_grant and starter_slots:
            raise ValueError(f"{label} 非新手装备不能设置默认穿戴槽")
        if any(slot not in EQUIPMENT_SLOTS for slot in starter_slots):
            raise ValueError(f"{label}.starter_equip_slots 包含未知槽位")
        for slot in starter_slots:
            if slot in occupied_starter_slots:
                raise ValueError(f"新手默认穿戴槽冲突: {slot}")
            occupied_starter_slots.add(slot)
        allowed_starter_slots = {equip_slot}
        if hand_mode in {"two_hand_heavy", "two_hand_melee", "two_hand_ranged"}:
            allowed_starter_slots = {"main_hand", "off_hand"}
        elif hand_mode == "shield":
            allowed_starter_slots = {"off_hand"}
        elif item_type == "weapon":
            allowed_starter_slots = {"main_hand", "off_hand"}
        elif equip_slot in {"left_finger", "right_finger"}:
            allowed_starter_slots = {"left_finger", "right_finger"}
        if not set(starter_slots).issubset(allowed_starter_slots):
            raise ValueError(f"{label}.starter_equip_slots 与装备槽位冲突")

        bound = raw_item.get("bound", True)
        if not isinstance(bound, bool):
            raise ValueError(f"{label}.bound 必须是布尔值")

        fixed = {}
        generation = {}
        if mode == "fixed":
            fixed = _validate_fixed(raw_item.get("fixed"), template, label)
        else:
            generation = _validate_generation(
                raw_item.get("generation"),
                label,
                require_materials=schema_version >= 2,
            )

        entries.append(
            EquipmentCatalogEntry(
                catalog_id=catalog_id,
                mode=mode,
                template=template,
                bound=bound,
                starter_grant=starter_grant,
                starter_equip_slots=starter_slots,
                fixed=fixed,
                generation=generation,
            )
        )

    entries_tuple = tuple(entries)
    return EquipmentCatalogSnapshot(
        schema_version=schema_version,
        entries=entries_tuple,
        by_id={entry.catalog_id: entry for entry in entries_tuple},
        by_template_id={
            entry.template.template_id: entry for entry in entries_tuple
        },
    )


def _validate_fixed(raw_fixed, template, label: str) -> dict:
    if not isinstance(raw_fixed, dict):
        raise ValueError(f"{label}.fixed 必须是对象")
    integer_fields = (
        "item_level",
        "enhancement_level",
        "enchant_capacity",
        "used_capacity",
    )
    if any(
        not isinstance(raw_fixed.get(field), int)
        or isinstance(raw_fixed.get(field), bool)
        for field in integer_fields
    ):
        raise ValueError(f"{label}.fixed 的等级、强化和容量必须是整数")
    fixed = {
        "item_level": int(raw_fixed.get("item_level", -1)),
        "quality": str(raw_fixed.get("quality", "")),
        "star_type": str(raw_fixed.get("star_type", "")),
        "blessing_state": str(raw_fixed.get("blessing_state", "")),
        "enhancement_level": int(raw_fixed.get("enhancement_level", 0)),
        "enchant_capacity": int(raw_fixed.get("enchant_capacity", 0)),
        "used_capacity": int(raw_fixed.get("used_capacity", 0)),
        "random_affixes": _validate_affixes(
            raw_fixed.get("random_affixes", []),
            f"{label}.fixed.random_affixes",
        ),
        "fusion_affixes": _validate_affixes(
            raw_fixed.get("fusion_affixes", []),
            f"{label}.fixed.fusion_affixes",
        ),
    }
    if fixed["quality"] not in QUALITY_MULTIPLIERS:
        raise ValueError(f"{label}.fixed.quality 无效")
    if fixed["star_type"] not in STAR_TYPES:
        raise ValueError(f"{label}.fixed.star_type 无效")
    if fixed["blessing_state"] not in BLESSING_STATES:
        raise ValueError(f"{label}.fixed.blessing_state 无效")
    if fixed["quality"] == "legendary" and fixed["star_type"] != "black_star":
        raise ValueError(f"{label} legendary 必须是 black_star")
    if fixed["star_type"] == "black_star" and fixed["quality"] != "legendary":
        raise ValueError(f"{label} black_star 必须是 legendary")
    if min(
        fixed["enhancement_level"],
        fixed["enchant_capacity"],
        fixed["used_capacity"],
    ) < 0:
        raise ValueError(f"{label}.fixed 的强化和容量不能为负数")
    affix_capacity = sum(
        int(affix.get("capacity", 0))
        for affix in fixed["random_affixes"] + fixed["fusion_affixes"]
    )
    if fixed["used_capacity"] != affix_capacity:
        raise ValueError(
            f"{label}.fixed.used_capacity 必须等于随机及融合词条容量之和"
        )
    EquipmentItem(
        None,
        0,
        template.template_id,
        template.name,
        template.item_type,
        template.equip_slot,
        template.hand_mode,
        template.weapon_type,
        template.armor_type,
        fixed["item_level"],
        fixed["quality"],
        fixed["star_type"],
        template.material,
        fixed["blessing_state"],
        fixed["enhancement_level"],
        template.weight,
        fixed["enchant_capacity"],
        fixed["used_capacity"],
        dict(template.base_stats),
        template.inherent_affixes,
        fixed["random_affixes"],
        fixed["fusion_affixes"],
        True,
        template.description,
        template.source_effects,
    )
    return fixed


def _validate_generation(
    raw_generation,
    label: str,
    *,
    require_materials: bool = False,
) -> dict:
    if not isinstance(raw_generation, dict):
        raise ValueError(f"{label}.generation 必须是对象")
    level_min = raw_generation.get("level_min")
    level_max = raw_generation.get("level_max")
    if (
        not isinstance(level_min, int)
        or isinstance(level_min, bool)
        or not isinstance(level_max, int)
        or isinstance(level_max, bool)
        or not 0 <= level_min <= level_max <= 100
    ):
        raise ValueError(f"{label}.generation 等级范围必须位于 0 到 100")
    qualities = raw_generation.get("qualities")
    if not isinstance(qualities, list) or not qualities:
        raise ValueError(f"{label}.generation.qualities 必须是非空数组")
    validated = []
    seen = set()
    for quality_item in qualities:
        if not isinstance(quality_item, dict):
            raise ValueError(f"{label}.generation.qualities 项必须是对象")
        quality = str(quality_item.get("quality", ""))
        weight = quality_item.get("weight")
        if (
            quality not in QUALITY_MULTIPLIERS
            or quality == "legendary"
            or quality in seen
        ):
            raise ValueError(f"{label}.generation 品质无效或重复: {quality}")
        if (
            not isinstance(weight, int | float)
            or isinstance(weight, bool)
            or weight <= 0
        ):
            raise ValueError(f"{label}.generation 品质权重必须大于 0")
        seen.add(quality)
        validated.append({"quality": quality, "weight": float(weight)})
    raw_materials = raw_generation.get("materials")
    if raw_materials is None and not require_materials:
        materials = ()
    else:
        if not isinstance(raw_materials, list) or not raw_materials:
            raise ValueError(f"{label}.generation.materials 必须是非空数组")
        materials_list = []
        seen_materials = set()
        for material_item in raw_materials:
            if not isinstance(material_item, dict):
                raise ValueError(
                    f"{label}.generation.materials 项必须是对象"
                )
            material = str(material_item.get("material", ""))
            weight = material_item.get("weight")
            material_for(material)
            if material in seen_materials:
                raise ValueError(
                    f"{label}.generation.materials 材质重复: {material}"
                )
            if (
                not isinstance(weight, int | float)
                or isinstance(weight, bool)
                or weight <= 0
            ):
                raise ValueError(
                    f"{label}.generation.materials 权重必须大于 0"
                )
            seen_materials.add(material)
            materials_list.append(
                {"material": material, "weight": float(weight)}
            )
        materials = tuple(materials_list)
    return {
        "level_min": level_min,
        "level_max": level_max,
        "qualities": tuple(validated),
        "materials": materials,
    }


def _validate_affixes(raw_affixes, label: str) -> tuple[dict, ...]:
    if not isinstance(raw_affixes, list):
        raise ValueError(f"{label} 必须是数组")
    validated = []
    for index, affix in enumerate(raw_affixes):
        if not isinstance(affix, dict):
            raise ValueError(f"{label}[{index}] 必须是对象")
        kind = str(affix.get("type", ""))
        if kind not in EquipmentFactory.AFFIX_TYPES:
            raise ValueError(f"{label}[{index}].type 无效: {kind}")
        value = affix.get("value")
        capacity = affix.get("capacity", 0)
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not isinstance(capacity, int)
            or isinstance(capacity, bool)
            or capacity < 0
        ):
            raise ValueError(f"{label}[{index}] 的 value/capacity 无效")
        if kind in {"stat_flat", "advanced_stat"} and not affix.get("stat"):
            raise ValueError(f"{label}[{index}] 缺少 stat")
        if kind == "skill_level" and not affix.get("skill_id"):
            raise ValueError(f"{label}[{index}] 缺少 skill_id")
        if kind == "trigger_ability":
            if not str(affix.get("ability_id", "")).strip():
                raise ValueError(f"{label}[{index}] 缺少 ability_id")
            target = str(affix.get("target", "enemy"))
            if target not in {"self", "enemy"}:
                raise ValueError(f"{label}[{index}].target 无效")
            source_power = affix.get("source_power", 0)
            if (
                not isinstance(source_power, int)
                or isinstance(source_power, bool)
                or source_power < 0
            ):
                raise ValueError(f"{label}[{index}].source_power 无效")
            params = affix.get("params", {})
            if not isinstance(params, dict):
                raise ValueError(f"{label}[{index}].params 必须是对象")
        validated.append(dict(affix))
    return tuple(validated)


DEFAULT_EQUIPMENT_CATALOG = EquipmentCatalog()
STARTER_TEMPLATES = tuple(
    entry.template for entry in DEFAULT_EQUIPMENT_CATALOG.snapshot.starter_entries
)
STARTER_BY_ID = {
    item.template_id: item for item in STARTER_TEMPLATES
}
BLACK_STAR_TEMPLATES = {
    entry.template.template_id: entry.template
    for entry in DEFAULT_EQUIPMENT_CATALOG.snapshot.entries
    if entry.mode == "fixed" and entry.fixed.get("star_type") == "black_star"
}


def starter_item(
    owner_pk: int,
    template: EquipmentTemplate,
) -> EquipmentItem:
    entry = DEFAULT_EQUIPMENT_CATALOG.snapshot.by_template_id.get(
        template.template_id
    )
    if entry is None or not entry.starter_grant:
        raise ValueError("装备不是新手目录装备")
    return EquipmentFactory().create_from_catalog(owner_pk, entry, 0)
