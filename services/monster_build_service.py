import math
from dataclasses import replace

try:
    from ..models.attributes import (
        DAMAGE_TYPES,
        PRIMARY_ATTRIBUTE_IDS,
        AdvancedAttributes,
        PrimaryAttributes,
    )
    from ..models.combat import FighterSnapshot
    from ..models.equipment import EquipmentBuild
    from ..models.monster import MonsterBuild, MonsterSpawnSpec
    from ..models.skill import SkillBuild
    from .ability_catalog import ACTIVE_ABILITY_DEFINITIONS
    from .attribute_service import AttributeService, skill_level_cap
    from .monster_catalog import MonsterCatalog
    from .skill_catalog import SKILL_DEFINITIONS
except ImportError:
    from models.attributes import (
        DAMAGE_TYPES,
        PRIMARY_ATTRIBUTE_IDS,
        AdvancedAttributes,
        PrimaryAttributes,
    )
    from models.combat import FighterSnapshot
    from models.equipment import EquipmentBuild
    from models.monster import MonsterBuild, MonsterSpawnSpec
    from models.skill import SkillBuild
    from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS
    from services.attribute_service import AttributeService, skill_level_cap
    from services.monster_catalog import MonsterCatalog
    from services.skill_catalog import SKILL_DEFINITIONS


def _clamp(value, low, high):
    return max(low, min(high, value))


def _merge_dicts(*values) -> dict:
    result = {}
    for value in values:
        if value:
            result.update(value)
    return result


def _largest_remainder(
    total_budget: int, weights: dict[str, float]
) -> PrimaryAttributes:
    """Allocate a six-stat budget deterministically with a per-stat cap of 100."""
    remaining = max(0, total_budget - len(PRIMARY_ATTRIBUTE_IDS))
    normalized = {
        key: max(0.1, float(weights.get(key, 1.0)))
        for key in PRIMARY_ATTRIBUTE_IDS
    }
    total_weight = sum(normalized.values())
    exact = {
        key: remaining * normalized[key] / total_weight
        for key in PRIMARY_ATTRIBUTE_IDS
    }
    values = {
        key: min(100, 1 + math.floor(exact[key]))
        for key in PRIMARY_ATTRIBUTE_IDS
    }
    assigned = sum(value - 1 for value in values.values())
    leftovers = max(0, remaining - assigned)
    ordering = sorted(
        PRIMARY_ATTRIBUTE_IDS,
        key=lambda key: (-(exact[key] - math.floor(exact[key])), key),
    )
    while leftovers:
        changed = False
        for key in ordering:
            if values[key] < 100:
                values[key] += 1
                leftovers -= 1
                changed = True
                if not leftovers:
                    break
        if not changed:
            break
    return PrimaryAttributes(**values)


class MonsterBuildService:
    """Pure monster composition and scaling; deliberately has no database access."""

    def __init__(
        self,
        catalog: MonsterCatalog | None = None,
        attribute_service: AttributeService | None = None,
    ):
        self.catalog = catalog or MonsterCatalog()
        self.attribute_service = attribute_service or AttributeService()

    def build(self, spec: MonsterSpawnSpec) -> MonsterBuild:
        template = self.catalog.get(spec.template_id)
        level = template.base_level if spec.level is None else int(spec.level)
        if not 1 <= level <= 280:
            raise ValueError("怪物生成等级必须在1到280之间")
        rank_id = spec.rank or template.rank
        snapshot_data = self.catalog.snapshot
        if rank_id not in snapshot_data.ranks:
            raise ValueError(f"未知怪物阶级：{rank_id}")
        combatant_pk = (
            -template.catalog_id
            if spec.combatant_pk is None else int(spec.combatant_pk)
        )
        if combatant_pk >= 0:
            raise ValueError("怪物战斗实例ID必须为负数")

        race = snapshot_data.races[template.race_id]
        monster_class = snapshot_data.classes[template.class_id]
        defaults = snapshot_data.defaults
        rank = snapshot_data.ranks[rank_id]
        weights = self._attribute_weights(
            template, defaults, race, monster_class
        )
        budget = round(
            round(6 + 1.35 * level) * rank["attribute_multiplier"]
        )
        attributes = _largest_remainder(max(6, budget), weights)
        advanced = self._advanced(
            template, defaults, race, monster_class
        )
        combat = self._merge_combat(
            defaults.get("combat", {}),
            race.get("combat", {}),
            monster_class.get("combat", {}),
            template.combat,
        )
        resistances = self._resistances(
            defaults, race, monster_class, template, rank
        )
        skills = self._skills(
            template, defaults, race, monster_class, level, attributes
        )
        ability_ids = self._abilities(
            template, defaults, race, monster_class, level
        )

        attack_coefficient = float(combat.get("attack_coefficient", 1.0))
        toughness_coefficient = float(
            combat.get("toughness_coefficient", 1.0)
        )
        weapon_power = _clamp(
            round(
                (2 + 0.75 * math.sqrt(level))
                * attack_coefficient
                * rank["offense_multiplier"]
            ),
            1,
            18,
        )
        armor_power = _clamp(
            round(
                (1 + 0.65 * math.sqrt(level))
                * toughness_coefficient
                * rank["defense_multiplier"]
            ),
            0,
            18,
        )
        equipment = self._equipment(
            combat,
            advanced,
            resistances,
            weapon_power,
            armor_power,
            float(rank["status_resistance_multiplier"]),
        )
        skill_build = SkillBuild(
            skills={},
            effective_levels=skills,
            active_skill_ids=ability_ids,
            active_definitions={
                ability_id: ACTIVE_ABILITY_DEFINITIONS[ability_id]
                for ability_id in ability_ids
            },
            level_caps={
                skill_id: skill_level_cap(
                    attributes,
                    SKILL_DEFINITIONS[skill_id].governing_attributes,
                    skill_id,
                )
                for skill_id in skills
            },
        )
        derived = self.attribute_service.derive(
            level=level,
            attributes=attributes,
            equipment=equipment,
            advanced=advanced,
            effective_skills=skills,
        )
        derived = replace(
            derived,
            max_hp=max(1, round(derived.max_hp * rank["hp_multiplier"])),
        )
        snapshot = FighterSnapshot(
            user_pk=combatant_pk,
            name=template.name,
            level=level,
            hp=attributes.strength,
            atk=attributes.perception,
            defense=attributes.constitution,
            speed=attributes.dexterity,
            luck=advanced.luck,
            strategy=str(combat.get("strategy", "balanced")),
            skill_ids=("basic_attack",) + ability_ids,
            equipment=equipment,
            skills=skill_build,
            attributes=attributes,
            advanced_attributes=advanced,
            derived=derived,
            combatant_kind="monster",
            source_template_id=template.template_id,
            rank=rank_id,
        )
        ai_profile_id = str(combat.get("ai_profile_id", "balanced_ai"))
        return MonsterBuild(
            template=template,
            level=level,
            rank=rank_id,
            attributes=attributes,
            advanced_attributes=advanced,
            skill_levels=skills,
            ability_ids=ability_ids,
            ai_profile=snapshot_data.ai_profiles[ai_profile_id],
            weapon_power=float(weapon_power),
            armor_power=float(armor_power),
            snapshot=snapshot,
            provenance={
                "scaling_version": snapshot_data.scaling_version,
                "attribute_origin": template.provenance["attribute_origin"],
                "source_stats_used_as_weights": bool(
                    template.source_stats.get("attributes")
                ),
                "merge_order": (
                    "defaults", template.race_id,
                    template.class_id, template.template_id,
                ),
            },
        )

    @staticmethod
    def _attribute_weights(template, defaults, race, monster_class):
        exact = template.source_stats.get("attributes")
        if exact:
            return {key: float(exact[key]) for key in PRIMARY_ATTRIBUTE_IDS}
        result = {key: 1.0 for key in PRIMARY_ATTRIBUTE_IDS}
        for layer in (
            defaults.get("attribute_weights", {}),
            race.get("attribute_weights", {}),
            monster_class.get("attribute_weights", {}),
            template.attribute_weights,
        ):
            for key, value in layer.items():
                result[key] = max(0.1, result[key] * float(value))
        return result

    @staticmethod
    def _advanced(template, defaults, race, monster_class):
        values = _merge_dicts(
            defaults.get("advanced", {}),
            race.get("advanced", {}),
            monster_class.get("advanced", {}),
            template.combat.get("advanced", {}),
        )
        source = template.source_stats
        for key in ("life_growth", "mana_growth", "speed", "luck"):
            if key in source:
                values[key] = source[key]
        return AdvancedAttributes(
            life_growth=int(_clamp(round(values.get("life_growth", 100)), 3, 250)),
            mana_growth=int(_clamp(round(values.get("mana_growth", 100)), 3, 250)),
            speed=int(_clamp(round(values.get("speed", 100)), 50, 180)),
            luck=int(_clamp(round(values.get("luck", 100)), 1, 150)),
        )

    @staticmethod
    def _resistances(defaults, race, monster_class, template, rank):
        result = {damage_type: 0.0 for damage_type in DAMAGE_TYPES}
        for layer in (
            defaults.get("resistances", {}),
            race.get("resistances", {}),
            monster_class.get("resistances", {}),
            template.resistances,
        ):
            for damage_type, value in layer.items():
                result[damage_type] += float(value)
        multiplier = float(rank["status_resistance_multiplier"])
        return {
            damage_type: _clamp(value * multiplier, -100.0, 100.0)
            for damage_type, value in result.items()
        }

    @staticmethod
    def _skills(template, defaults, race, monster_class, level, attributes):
        merged = {}
        for layer in (
            defaults.get("skills", {}),
            race.get("skills", {}),
            monster_class.get("skills", {}),
            template.skill_coefficients,
        ):
            merged.update(layer)
        result = {}
        for skill_id, config in sorted(merged.items()):
            target = round(
                level * float(config.get("coefficient", 0))
                + float(config.get("flat", 0))
            )
            cap = skill_level_cap(
                attributes,
                SKILL_DEFINITIONS[skill_id].governing_attributes,
                skill_id,
            )
            result[skill_id] = int(_clamp(target, 1, min(150, cap)))
        return result

    @staticmethod
    def _abilities(template, defaults, race, monster_class, level):
        merged = {}
        for layer in (
            defaults.get("abilities", []),
            race.get("abilities", []),
            monster_class.get("abilities", []),
            template.abilities,
        ):
            for item in layer:
                merged[item["ability_id"]] = item
        for ability_id in template.removed_ability_ids:
            merged.pop(ability_id, None)
        eligible = [
            item for item in merged.values()
            if int(item.get("min_level", 1)) <= level
        ]
        eligible.sort(
            key=lambda item: (
                -float(item.get("priority", 0)),
                item["ability_id"],
            )
        )
        return tuple(item["ability_id"] for item in eligible[:4])

    @staticmethod
    def _merge_combat(*layers):
        result = {}
        additive = {"elemental_damage", "combat_effects"}
        for layer in layers:
            for key, value in layer.items():
                if key in additive:
                    bucket = result.setdefault(key, {})
                    for effect_id, amount in value.items():
                        bucket[effect_id] = (
                            float(bucket.get(effect_id, 0)) + float(amount)
                        )
                elif key == "advanced":
                    result.setdefault(key, {}).update(value)
                else:
                    result[key] = value
        return result

    @staticmethod
    def _equipment(
        combat, advanced, resistances, weapon_power, armor_power,
        status_resistance_multiplier,
    ):
        weapon_mode = str(combat.get("weapon_mode", "one_hand"))
        weapon_type = str(combat.get("weapon_type", "unarmed"))
        effects = {
            f"resistance_{damage_type}": value
            for damage_type, value in resistances.items()
        }
        effects["armor_penetration"] = float(combat.get("penetration", 0))
        effects["status_resistance"] = _clamp(
            float(combat.get("status_resistance", 0))
            + max(0.0, status_resistance_multiplier - 1.0),
            0.0,
            0.75,
        )
        for damage_type, value in combat.get("elemental_damage", {}).items():
            effects[f"damage_{damage_type}"] = float(value)
        effects.update({
            str(key): float(value)
            for key, value in combat.get("combat_effects", {}).items()
        })
        weapon_weight = float(combat.get("weapon_weight", 1.0))
        return EquipmentBuild(
            items=(),
            slots={},
            stat_modifiers={},
            skill_modifiers={},
            weapon_mode=weapon_mode,
            weapon_type=weapon_type,
            armor_style=str(combat.get("armor_style", "light")),
            total_weight=weapon_weight,
            carry_capacity=10000.0,
            overloaded=False,
            attack_range=int(combat.get(
                "attack_range",
                230 if weapon_mode == "two_hand_ranged" else 65,
            )),
            damage_multiplier=float(combat.get("damage_multiplier", 1.0)),
            attack_windup=int(combat.get("windup", 8)),
            attack_recovery=int(combat.get("recovery", 12)),
            attack_cooldown=int(combat.get("cooldown", 24)),
            attack_stamina=int(combat.get("stamina_cost", 8)),
            movement_multiplier=float(combat.get("movement_multiplier", 1.0)),
            stamina_regen=int(combat.get("stamina_regen", 2)),
            max_stamina=int(combat.get("max_stamina", 100)),
            block_rate=float(combat.get("block_rate", 0)),
            knockback_resistance=float(
                combat.get("knockback_resistance", 0)
            ),
            melee_followup=float(combat.get("melee_followup", 0)),
            ranged_followup=float(combat.get("ranged_followup", 0)),
            reserved_effects={},
            weapon_power=float(weapon_power),
            armor_power=float(armor_power),
            weapon_weight=weapon_weight,
            action_speed=float(advanced.speed),
            combat_effects=effects,
        )
