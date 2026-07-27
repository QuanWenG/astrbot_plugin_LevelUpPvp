import json
import re
from pathlib import Path

try:
    from ..models.attributes import DAMAGE_TYPES, PRIMARY_ATTRIBUTE_IDS
    from ..models.combat import AIProfile
    from ..models.monster import MonsterCatalogSnapshot, MonsterTemplate
    from .ability_catalog import ACTIVE_ABILITY_DEFINITIONS
    from .skill_catalog import SKILL_DEFINITIONS
except ImportError:
    from models.attributes import DAMAGE_TYPES, PRIMARY_ATTRIBUTE_IDS
    from models.combat import AIProfile
    from models.monster import MonsterCatalogSnapshot, MonsterTemplate
    from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS
    from services.skill_catalog import SKILL_DEFINITIONS


DEFAULT_MONSTER_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "monster_catalog.json"
)
_TEMPLATE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_ATTRIBUTE_ORIGINS = {
    "mobile_exact", "pc_reference", "race_class_inferred"
}
_LOCALIZATION_ORIGINS = {"official", "community", "translated"}
_WEAPON_TYPES = {
    "", "unarmed", "shortsword", "longsword", "spear", "axe", "scythe",
    "blunt", "staff", "bow", "crossbow", "throwing", "firearm",
}
_HAND_MODES = {"one_hand", "two_hand_melee", "two_hand_ranged"}
_ARMOR_STYLES = {"light", "medium", "heavy"}


def _mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是对象")
    return value


def _number(value, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是数值")
    result = float(value)
    if not low <= result <= high:
        raise ValueError(f"{label}必须在{low}到{high}之间")
    return result


class MonsterCatalog:
    """Validated, atomically reloadable monster data."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or DEFAULT_MONSTER_CATALOG_PATH)
        self._snapshot = self._load(self.path)

    @property
    def snapshot(self) -> MonsterCatalogSnapshot:
        return self._snapshot

    def get(self, template_id: str) -> MonsterTemplate:
        try:
            return self._snapshot.by_template_id[template_id]
        except KeyError as exc:
            raise KeyError(f"未知怪物模板：{template_id}") from exc

    def reload(self, path: str | Path | None = None) -> MonsterCatalogSnapshot:
        candidate_path = Path(path or self.path)
        candidate = self._load(candidate_path)
        self.path = candidate_path
        self._snapshot = candidate
        return candidate

    def _load(self, path: Path) -> MonsterCatalogSnapshot:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if data.get("schema_version") != 1:
            raise ValueError("怪物目录仅支持schema_version 1")
        defaults = _mapping(data.get("defaults", {}), "defaults")
        ranks = self._rank_map(data.get("ranks"))
        ai_profiles = self._ai_map(data.get("ai_profiles"))
        races = self._profile_map(data.get("races"), "种族")
        classes = self._profile_map(data.get("classes"), "职业")
        for label, profiles in (("种族", races), ("职业", classes)):
            for profile_id, profile in profiles.items():
                self._validate_advanced(
                    profile.get("advanced", {}),
                    f"{label}.{profile_id}",
                )
                self._validate_combat(
                    _mapping(
                        profile.get("combat", {}),
                        f"{label}.{profile_id}.combat",
                    ),
                    ai_profiles,
                    f"{label}.{profile_id}",
                )
        self._validate_combat(
            _mapping(defaults.get("combat", {}), "defaults.combat"),
            ai_profiles,
            "defaults",
        )
        self._validate_resistances(
            defaults.get("resistances", {}), "defaults"
        )
        self._validate_weights(
            defaults.get("attribute_weights", {}), "defaults"
        )
        self._validate_skills(defaults.get("skills", {}), "defaults")
        self._validate_abilities(
            defaults.get("abilities", []), "defaults"
        )
        self._validate_advanced(defaults.get("advanced", {}), "defaults")
        raw_monsters = data.get("monsters")
        if not isinstance(raw_monsters, list) or not raw_monsters:
            raise ValueError("monsters必须是非空数组")

        monsters: list[MonsterTemplate] = []
        by_id: dict[int, MonsterTemplate] = {}
        by_template_id: dict[str, MonsterTemplate] = {}
        for index, raw in enumerate(raw_monsters):
            item = self._monster(
                _mapping(raw, f"monsters[{index}]"),
                races,
                classes,
                ranks,
                ai_profiles,
            )
            if item.catalog_id in by_id:
                raise ValueError(f"重复怪物图鉴ID：{item.catalog_id}")
            if item.template_id in by_template_id:
                raise ValueError(f"重复怪物模板ID：{item.template_id}")
            by_id[item.catalog_id] = item
            by_template_id[item.template_id] = item
            monsters.append(item)
        return MonsterCatalogSnapshot(
            schema_version=1,
            scaling_version=str(data.get("scaling_version", "monster-v1")),
            defaults=dict(defaults),
            races=races,
            classes=classes,
            ai_profiles=ai_profiles,
            ranks=ranks,
            monsters=tuple(monsters),
            by_id=by_id,
            by_template_id=by_template_id,
        )

    def _profile_map(self, raw, label: str) -> dict[str, dict]:
        profiles = _mapping(raw, label)
        if not profiles:
            raise ValueError(f"{label}不能为空")
        result: dict[str, dict] = {}
        for profile_id, profile in profiles.items():
            if not _TEMPLATE_ID.match(profile_id):
                raise ValueError(f"非法{label}ID：{profile_id}")
            value = dict(_mapping(profile, f"{label}.{profile_id}"))
            if label == "种族":
                if not str(value.get("name", "")).strip():
                    raise ValueError(f"{profile_id}缺少种族中文名")
                if not str(value.get("source_name_ja", "")).strip():
                    raise ValueError(f"{profile_id}缺少种族日文原名")
                if (
                    value.get("localization_origin")
                    not in _LOCALIZATION_ORIGINS
                ):
                    raise ValueError(f"{profile_id}的种族本地化来源无效")
            self._validate_weights(value.get("attribute_weights", {}), label)
            self._validate_skills(value.get("skills", {}), label)
            self._validate_abilities(value.get("abilities", []), label)
            self._validate_resistances(value.get("resistances", {}), label)
            result[profile_id] = value
        return result

    def _rank_map(self, raw) -> dict[str, dict]:
        values = _mapping(raw, "ranks")
        if set(values) != {"normal", "elite", "boss"}:
            raise ValueError("ranks必须且只能包含normal、elite、boss")
        keys = (
            "attribute_multiplier", "hp_multiplier", "offense_multiplier",
            "defense_multiplier", "status_resistance_multiplier",
        )
        result = {}
        for rank_id, profile in values.items():
            profile = dict(_mapping(profile, f"ranks.{rank_id}"))
            for key in keys:
                _number(profile.get(key), f"{rank_id}.{key}", 0.01, 10.0)
            result[rank_id] = profile
        return result

    def _ai_map(self, raw) -> dict[str, AIProfile]:
        values = _mapping(raw, "ai_profiles")
        result = {}
        for profile_id, profile in values.items():
            profile = _mapping(profile, f"ai_profiles.{profile_id}")
            if not _TEMPLATE_ID.match(profile_id):
                raise ValueError(f"非法AI ID：{profile_id}")
            kwargs = {
                "aggression": _number(
                    profile.get("aggression"), "aggression", 0, 1
                ),
                "guard_tendency": _number(
                    profile.get("guard_tendency"), "guard_tendency", 0, 1
                ),
                "chase_tendency": _number(
                    profile.get("chase_tendency"), "chase_tendency", 0, 1
                ),
                "preferred_range": int(_number(
                    profile.get("preferred_range"), "preferred_range", 0, 500
                )),
                "retreat_tendency": _number(
                    profile.get("retreat_tendency"),
                    "retreat_tendency", 0, 1,
                ),
                "low_hp_risk": _number(
                    profile.get("low_hp_risk"), "low_hp_risk", 0, 1
                ),
            }
            result[profile_id] = AIProfile(**kwargs)
        if not result:
            raise ValueError("ai_profiles不能为空")
        return result

    def _monster(self, raw, races, classes, ranks, ai_profiles) -> MonsterTemplate:
        catalog_id = int(raw.get("id", 0))
        template_id = str(raw.get("template_id", ""))
        if catalog_id <= 0:
            raise ValueError("怪物图鉴ID必须为正整数")
        if not _TEMPLATE_ID.match(template_id):
            raise ValueError(f"非法怪物模板ID：{template_id}")
        level = int(raw.get("base_level", 0))
        if not 1 <= level <= 280:
            raise ValueError(f"{template_id}基准等级必须在1到280之间")
        race_id, class_id = raw.get("race_id"), raw.get("class_id")
        rank = raw.get("rank", "normal")
        if race_id not in races:
            raise ValueError(f"{template_id}引用未知种族：{race_id}")
        if class_id not in classes:
            raise ValueError(f"{template_id}引用未知职业：{class_id}")
        if rank not in ranks:
            raise ValueError(f"{template_id}引用未知阶级：{rank}")
        self._validate_weights(raw.get("attribute_weights", {}), template_id)
        self._validate_skills(raw.get("skills", {}), template_id)
        self._validate_abilities(raw.get("abilities", []), template_id)
        self._validate_resistances(raw.get("resistances", {}), template_id)
        removed = tuple(raw.get("removed_ability_ids", []))
        for ability_id in removed:
            if ability_id not in ACTIVE_ABILITY_DEFINITIONS:
                raise ValueError(f"{template_id}删除未知能力：{ability_id}")
        combat = dict(_mapping(raw.get("combat", {}), f"{template_id}.combat"))
        self._validate_combat(combat, ai_profiles, template_id)
        provenance = dict(
            _mapping(raw.get("provenance"), f"{template_id}.provenance")
        )
        if int(provenance.get("wiki_no", 0)) != catalog_id:
            raise ValueError(f"{template_id}的wiki_no必须等于图鉴ID")
        if provenance.get("attribute_origin") not in _ATTRIBUTE_ORIGINS:
            raise ValueError(f"{template_id}的属性来源无效")
        if provenance.get("localization_origin") not in _LOCALIZATION_ORIGINS:
            raise ValueError(f"{template_id}的本地化来源无效")
        if not provenance.get("source_url"):
            raise ValueError(f"{template_id}缺少来源链接")
        source_stats = dict(raw.get("source_stats", {}))
        self._validate_source_stats(source_stats, template_id)
        if provenance["attribute_origin"] in {
            "mobile_exact", "pc_reference"
        } and "attributes" not in source_stats:
            raise ValueError(f"{template_id}的精确属性来源缺少原始六维")
        if (
            provenance["attribute_origin"] == "pc_reference"
            and not provenance.get("attribute_source_url")
        ):
            raise ValueError(f"{template_id}缺少原作属性来源链接")
        name = str(raw.get("name", "")).strip()
        source_name = str(raw.get("source_name_ja", "")).strip()
        if not name or not source_name:
            raise ValueError(f"{template_id}缺少中日名称")
        if provenance.get("original_name_ja") != source_name:
            raise ValueError(f"{template_id}的来源日文名不一致")
        return MonsterTemplate(
            catalog_id=catalog_id,
            template_id=template_id,
            name=name,
            source_name_ja=source_name,
            base_level=level,
            race_id=race_id,
            class_id=class_id,
            rank=rank,
            hostile=bool(raw.get("hostile", True)),
            capturable=bool(raw.get("capturable", True)),
            attribute_weights=dict(raw.get("attribute_weights", {})),
            skill_coefficients=dict(raw.get("skills", {})),
            abilities=tuple(dict(x) for x in raw.get("abilities", [])),
            removed_ability_ids=removed,
            combat=combat,
            resistances=dict(raw.get("resistances", {})),
            source_effects=tuple(str(x) for x in raw.get("source_effects", [])),
            source_stats=source_stats,
            provenance=provenance,
        )

    def _validate_weights(self, raw, label):
        raw = _mapping(raw, f"{label}.attribute_weights")
        for key, value in raw.items():
            if key not in PRIMARY_ATTRIBUTE_IDS:
                raise ValueError(f"{label}引用未知六维：{key}")
            _number(value, f"{label}.{key}", 0.000001, 1000)

    def _validate_skills(self, raw, label):
        raw = _mapping(raw, f"{label}.skills")
        for skill_id, value in raw.items():
            if skill_id not in SKILL_DEFINITIONS:
                raise ValueError(f"{label}引用未知技能：{skill_id}")
            value = _mapping(value, f"{label}.{skill_id}")
            _number(value.get("coefficient", 0), "coefficient", 0, 10)
            _number(value.get("flat", 0), "flat", -150, 150)

    def _validate_abilities(self, raw, label):
        if not isinstance(raw, list):
            raise ValueError(f"{label}.abilities必须是数组")
        seen = set()
        for item in raw:
            item = _mapping(item, f"{label}.abilities")
            ability_id = item.get("ability_id")
            if ability_id not in ACTIVE_ABILITY_DEFINITIONS:
                raise ValueError(f"{label}引用未知能力：{ability_id}")
            if ability_id in seen:
                raise ValueError(f"{label}重复引用能力：{ability_id}")
            seen.add(ability_id)
            _number(item.get("min_level", 1), "min_level", 1, 280)
            _number(item.get("priority", 0), "priority", -1000, 1000)

    def _validate_resistances(self, raw, label):
        raw = _mapping(raw, f"{label}.resistances")
        for damage_type, value in raw.items():
            if damage_type not in DAMAGE_TYPES:
                raise ValueError(f"{label}引用未知伤害类型：{damage_type}")
            _number(value, f"{label}.{damage_type}", -100, 100)

    def _validate_combat(self, combat, ai_profiles, label):
        if "ai_profile_id" in combat:
            if combat["ai_profile_id"] not in ai_profiles:
                raise ValueError(
                    f"{label}引用未知AI：{combat['ai_profile_id']}"
                )
        if "weapon_type" in combat and combat["weapon_type"] not in _WEAPON_TYPES:
            raise ValueError(f"{label}使用未知武器类型")
        if "weapon_mode" in combat and combat["weapon_mode"] not in _HAND_MODES:
            raise ValueError(f"{label}使用未知攻击模式")
        if "armor_style" in combat and combat["armor_style"] not in _ARMOR_STYLES:
            raise ValueError(f"{label}使用未知护甲定位")
        ranges = {
            "attack_coefficient": (0.01, 10),
            "toughness_coefficient": (0, 10),
            "attack_range": (1, 500),
            "windup": (0, 1000),
            "recovery": (0, 1000),
            "cooldown": (1, 1000),
            "stamina_cost": (0, 1000),
            "weapon_weight": (0, 10000),
            "penetration": (0, 1),
            "block_rate": (0, 1),
            "melee_followup": (0, 1),
            "ranged_followup": (0, 1),
            "status_resistance": (0, 1),
            "movement_multiplier": (0.01, 10),
            "damage_multiplier": (0.01, 10),
            "stamina_regen": (0, 1000),
            "max_stamina": (1, 10000),
            "knockback_resistance": (0, 1),
        }
        for key, bounds in ranges.items():
            if key in combat:
                _number(combat[key], f"{label}.{key}", *bounds)
        elemental = combat.get("elemental_damage", {})
        for damage_type, value in _mapping(
            elemental, f"{label}.elemental_damage"
        ).items():
            if damage_type not in DAMAGE_TYPES:
                raise ValueError(f"{label}引用未知元素附伤：{damage_type}")
            _number(value, f"{label}.{damage_type}", 0, 1000)
        for effect_id, value in _mapping(
            combat.get("combat_effects", {}),
            f"{label}.combat_effects",
        ).items():
            _number(value, f"{label}.{effect_id}", -10000, 10000)
        self._validate_advanced(combat.get("advanced", {}), label)

    def _validate_advanced(self, raw, label):
        advanced = _mapping(raw, f"{label}.advanced")
        for key, value in advanced.items():
            bounds = {
                "life_growth": (3, 250),
                "mana_growth": (3, 250),
                "speed": (1, 2000),
                "luck": (1, 150),
            }.get(key)
            if bounds is None:
                raise ValueError(f"{label}引用未知高级属性：{key}")
            _number(value, f"{label}.{key}", *bounds)

    def _validate_source_stats(self, stats, label):
        if not isinstance(stats, dict):
            raise ValueError(f"{label}.source_stats必须是对象")
        attributes = stats.get("attributes")
        if attributes is not None:
            attributes = _mapping(attributes, f"{label}.source_stats.attributes")
            if set(attributes) != set(PRIMARY_ATTRIBUTE_IDS):
                raise ValueError(f"{label}的来源六维必须完整")
            for key, value in attributes.items():
                _number(value, f"{label}.source_stats.{key}", 0.01, 10000)
        for key in ("life_growth", "mana_growth"):
            if key in stats:
                _number(stats[key], f"{label}.{key}", 3, 250)
        if "speed" in stats:
            _number(stats["speed"], f"{label}.speed", 1, 2000)
