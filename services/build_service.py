from collections import defaultdict
from dataclasses import replace

try:
    from ..models.attributes import PRIMARY_ATTRIBUTE_IDS
    from ..models.equipment import EquipmentProc
    from ..models.combat import FighterSnapshot
    from ..models.equipment import EquipmentBuild
    from ..models.skill import SkillBuild
    from .ability_catalog import ACTIVE_ABILITY_DEFINITIONS, ability_is_unlocked
    from .attribute_service import (
        LEGACY_ATTRIBUTE_MAP,
        AttributeService,
        skill_level_cap,
    )
    from .equipment_affixes import effective_inherent_affixes
    from .equipment_catalog import QUALITY_MULTIPLIERS
    from .material_catalog import (
        actual_weight, armor_style_for_weight, material_for,
        weight_accuracy_multipliers,
    )
    from .skill_catalog import SKILL_DEFINITIONS
    from .passive_effects import resolve_passive_bonuses
except ImportError:
    from models.attributes import PRIMARY_ATTRIBUTE_IDS
    from models.equipment import EquipmentProc
    from models.combat import FighterSnapshot
    from models.equipment import EquipmentBuild
    from models.skill import SkillBuild
    from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS, ability_is_unlocked
    from services.attribute_service import (
        LEGACY_ATTRIBUTE_MAP,
        AttributeService,
        skill_level_cap,
    )
    from services.equipment_affixes import effective_inherent_affixes
    from services.equipment_catalog import QUALITY_MULTIPLIERS
    from services.material_catalog import (
        actual_weight, armor_style_for_weight, material_for,
        weight_accuracy_multipliers,
    )
    from services.skill_catalog import SKILL_DEFINITIONS
    from services.passive_effects import resolve_passive_bonuses


WEAPON_RULES = {
    "unarmed": (80, 1.15, 1, 2, 5, 6),
    "one_hand": (100, 1.00, 1, 2, 6, 8),
    "sword_shield": (100, 0.85, 1, 2, 6, 9),
    "dual_wield": (100, 0.80, 1, 3, 7, 14),
    "two_hand_melee": (150, 1.15, 2, 3, 7, 16),
    "two_hand_heavy": (110, 0.70, 2, 3, 8, 18),
    "bow": (350, 0.70, 2, 2, 7, 12),
    "crossbow": (400, 0.75, 2, 3, 8, 13),
    "firearm": (450, 0.60, 1, 4, 9, 10),
    "throwing": (250, 1.05, 1, 2, 6, 10),
}


class CombatBuildService:
    def __init__(self, equipment_service, skill_service, attribute_service=None, spell_service=None):
        self.equipment_service = equipment_service
        self.skill_service = skill_service
        self.attribute_service = attribute_service or AttributeService()
        self.spell_service = spell_service

    async def snapshot_in_db(self, db, user, strategy: str) -> FighterSnapshot:
        await self.equipment_service.ensure_starter_in_db(db, user.id)
        await self.skill_service.ensure_initialized_in_db(db, user)
        await self.attribute_service.ensure_progress_in_db(db, user.id)
        slots, items = await self.equipment_service.loadout_in_db(db, user.id)
        skills = await self.skill_service.skills_in_db(db, user.id)
        spells = (
            await self.spell_service.spells_in_db(db, user.id)
            if self.spell_service else {}
        )
        configured_ids = await self.skill_service.active_slots_in_db(db, user.id)
        active_ids = tuple(
            value for value in configured_ids
            if value
            and value in ACTIVE_ABILITY_DEFINITIONS
            and ability_is_unlocked(ACTIVE_ABILITY_DEFINITIONS[value], skills, spells)
        )
        equipment = self.resolve_equipment(user, slots, items, skills)
        permanent_attributes = self.attribute_service.attributes_for_user(user)
        level_caps = {
            skill_id: skill_level_cap(
                permanent_attributes,
                SKILL_DEFINITIONS[skill_id].governing_attributes,
                skill_id,
            )
            for skill_id in skills
            if skill_id in SKILL_DEFINITIONS
        }
        effective = {
            skill_id: min(
                self.skill_service.MAX_EFFECTIVE_LEVEL,
                (skills[skill_id].level if skill_id in skills else 0)
                + equipment.skill_modifiers.get(skill_id, 0),
            )
            for skill_id in set(skills) | set(equipment.skill_modifiers)
        }

        combat_attributes = self.attribute_service.attributes_for_user(
            user, equipment.stat_modifiers
        )
        combat_advanced = self.attribute_service.advanced_attributes_for_user(
            user, equipment.advanced_stat_modifiers
        )
        derived = self.attribute_service.derive(
            level=user.level,
            attributes=combat_attributes,
            equipment=equipment,
            advanced=combat_advanced,
            effective_skills=effective,
        )
        equipment = replace(
            equipment,
            max_stamina=derived.max_sp,
            action_speed=derived.action_speed,
        )
        skill_build = SkillBuild(
            skills,
            effective,
            active_ids,
            {
                ability_id: ACTIVE_ABILITY_DEFINITIONS[ability_id]
                for ability_id in active_ids
            },
            level_caps,
            spells,
        )
        return FighterSnapshot(
            user_pk=user.id,
            name=user.nickname or user.user_id,
            level=user.level,
            hp=user.hp,
            atk=user.atk,
            defense=user.defense,
            speed=user.speed,
            luck=user.luck,
            strategy=strategy,
            equipment_modifiers=equipment.stat_modifiers,
            skill_ids=active_ids or ("basic_attack",),
            equipment=equipment,
            skills=skill_build,
            attributes=combat_attributes,
            advanced_attributes=combat_advanced,
            derived=derived,
        )

    def resolve_equipment(self, user, slots, items, skills) -> EquipmentBuild:
        stat_values = defaultdict(float)
        advanced_values = defaultdict(float)
        skill_values = defaultdict(int)
        effects = defaultdict(float)
        equipment_procs = []
        item_weights: dict[int, float] = {}
        total_weight = 0.0
        weapon_power = 0.0
        armor_power = 0.0

        for index, item in enumerate(items):
            material = material_for(item.material)
            resolved_weight = actual_weight(item.weight, item.material)
            item_weights[item.id if item.id is not None else -(index + 1)] = round(
                resolved_weight, 3
            )
            total_weight += resolved_weight
            for material_effect in material.effects:
                if material_effect.effect_type == "primary":
                    stat_values[material_effect.target] += material_effect.value
                elif material_effect.effect_type == "advanced":
                    advanced_values[material_effect.target] += material_effect.value
                elif material_effect.effect_type == "skill":
                    skill_values[material_effect.target] += int(material_effect.value)
                elif material_effect.effect_type == "resistance":
                    effects[f"resistance_{material_effect.target}"] += material_effect.value

            level_factor = max(
                0.50,
                1.0 - max(0, item.item_level - user.level) * 0.03,
            )
            quality_factor = QUALITY_MULTIPLIERS.get(item.quality, 1.0)
            factor = quality_factor * level_factor
            for stat, raw_value in item.base_stats.items():
                raw_value = float(raw_value)
                value = raw_value * factor
                if stat in {"atk", "weapon_power"} and item.item_type == "weapon":
                    value = (
                        raw_value
                        * material.attack_factor
                        * quality_factor
                        + item.enhancement_level
                        + item.item_level // 10
                    ) * level_factor
                elif stat in {"defense", "armor_power"}:
                    value = (
                        raw_value
                        * material.defense_factor
                        * quality_factor
                        + item.enhancement_level * 2
                    ) * level_factor
                elif stat == "accuracy":
                    value = (
                        raw_value * material.accuracy_factor * factor
                    )
                elif stat == "evasion":
                    value = raw_value * material.evasion_factor * factor
                if stat in PRIMARY_ATTRIBUTE_IDS:
                    stat_values[stat] += value
                elif stat == "atk":
                    if item.item_type == "weapon":
                        weapon_power += value
                    else:
                        effects["attack_power"] += value
                elif stat == "defense":
                    armor_power += value
                elif stat == "hp":
                    effects["max_hp"] += value * 10
                elif stat == "speed":
                    effects["action_speed"] += value * 3
                elif stat == "luck":
                    stat_values["magic"] += value
                elif stat == "weapon_power":
                    weapon_power += value
                elif stat == "armor_power":
                    armor_power += value
                else:
                    effects[stat] += value

            for affix in (
                effective_inherent_affixes(
                    item.inherent_affixes,
                    user.level,
                    item.item_level,
                )
                + item.random_affixes
                + item.fusion_affixes
            ):
                kind = str(affix.get("type", ""))
                value = float(affix.get("value", 0))
                if kind == "stat_flat":
                    stat = str(affix.get("stat", "perception"))
                    stat = LEGACY_ATTRIBUTE_MAP.get(stat, stat)
                    if stat in PRIMARY_ATTRIBUTE_IDS:
                        stat_values[stat] += value
                elif kind == "advanced_stat":
                    advanced_values[str(affix.get("stat", "luck"))] += value
                elif kind == "skill_level":
                    skill_values[str(affix.get("skill_id", ""))] += int(value)
                elif kind == "trigger_ability":
                    equipment_procs.append(
                        EquipmentProc(
                            source_template_id=item.template_id,
                            proc_type=kind,
                            target=str(affix.get("target", "enemy")),
                            chance=max(0.0, min(1.0, value)),
                            ability_id=str(affix.get("ability_id", "")),
                            source_power=int(affix.get("source_power", 0)),
                            params=dict(affix.get("params", {})),
                        )
                    )
                elif kind == "element_resistance":
                    for damage_type in (
                        "magic", "fire", "cold", "lightning",
                        "shadow", "nature", "mind", "hell",
                    ):
                        effects[f"resistance_{damage_type}"] += value
                else:
                    effects[kind] += value

        main = self._item_for_slot(slots, items, "main_hand")
        off = self._item_for_slot(slots, items, "off_hand")
        mode, weapon_type = self._weapon_mode(main, off)
        rule_key = weapon_type if mode == "two_hand_ranged" else mode
        attack_range, damage, windup, recovery, cooldown, stamina = (
            WEAPON_RULES.get(rule_key, WEAPON_RULES["unarmed"])
        )
        armor_style = armor_style_for_weight(total_weight)
        effective_weightlifting = min(
            150,
            (skills.get("weightlifting").level if "weightlifting" in skills else 0)
            + skill_values.get("weightlifting", 0),
        )
        strength = user.strength + stat_values["strength"]
        constitution = user.constitution + stat_values["constitution"]
        willpower = user.willpower + stat_values["willpower"]
        capacity = (
            20
            + strength * 0.5
            + constitution * 0.5
            + effective_weightlifting * 0.5
        )
        overloaded = total_weight > capacity
        movement = {"light": 1.0, "medium": 0.9, "heavy": 0.75}[armor_style]
        regen = {"light": 10, "medium": 8, "heavy": 6}[armor_style]
        if overloaded:
            overload_factor = max(0.5, capacity / max(total_weight, 0.1))
            movement *= overload_factor
            regen = max(1, regen // 2)
        physical_accuracy, spell_accuracy = weight_accuracy_multipliers(
            armor_style, overloaded
        )
        block = effects["block_rate"]
        knockback_resistance = effects["knockback_resistance"]
        if mode == "two_hand_heavy":
            knockback_resistance += 0.10
        hand_items = {
            candidate.id: candidate
            for candidate in (main, off)
            if candidate
        }.values()
        weapon_weight = sum(
            actual_weight(item.weight, item.material) for item in hand_items
        )
        action_speed = (
            user.advanced_speed
            + advanced_values["speed"]
            + effects["action_speed"]
        ) * movement
        max_stamina = max(
            1,
            round(60 + constitution * 4 + willpower * 2 + effects["max_sp"]),
        )
        combat_effects = dict(effects)
        if effects.get("max_stamina"):
            combat_effects["max_sp"] += effects["max_stamina"]

        equipment = EquipmentBuild(
            items=tuple(items),
            slots=dict(slots),
            stat_modifiers={key: round(value) for key, value in stat_values.items()},
            skill_modifiers=dict(skill_values),
            weapon_mode=mode,
            weapon_type=weapon_type,
            armor_style=armor_style,
            total_weight=round(total_weight, 2),
            carry_capacity=round(capacity, 2),
            overloaded=overloaded,
            attack_range=attack_range,
            damage_multiplier=damage,
            attack_windup=windup,
            attack_recovery=recovery,
            attack_cooldown=cooldown,
            attack_stamina=stamina,
            movement_multiplier=movement,
            stamina_regen=regen,
            max_stamina=max_stamina,
            block_rate=min(0.35, block),
            knockback_resistance=min(0.75, knockback_resistance),
            melee_followup=min(0.50, effects["melee_followup"]),
            ranged_followup=min(0.50, effects["ranged_followup"]),
            reserved_effects={
                key: value
                for key, value in effects.items()
                if key in {"status_immunity", "spell_power"}
            },
            weapon_power=round(weapon_power, 2),
            armor_power=round(armor_power, 2),
            weapon_weight=round(weapon_weight, 2),
            action_speed=round(action_speed, 2),
            combat_effects=combat_effects,
            advanced_stat_modifiers={
                key: round(value) for key, value in advanced_values.items()
            },
            item_weights=item_weights,
            physical_accuracy_multiplier=physical_accuracy,
            spell_accuracy_multiplier=spell_accuracy,
            equipment_procs=tuple(equipment_procs),
        )
        effective_levels = {
            skill_id: min(
                150,
                (skills[skill_id].level if skill_id in skills else 0)
                + skill_values.get(skill_id, 0),
            )
            for skill_id in set(skills) | set(skill_values)
        }
        passives = resolve_passive_bonuses(effective_levels, equipment)
        return replace(
            equipment,
            damage_multiplier=equipment.damage_multiplier * passives.style_multiplier,
            block_rate=min(0.35, equipment.block_rate + passives.block_rate),
            knockback_resistance=min(
                0.75,
                equipment.knockback_resistance + passives.knockback_resistance,
            ),
        )
    def resolve_derived(self, user, equipment, skills):
        effective = {
            skill_id: min(
                self.skill_service.MAX_EFFECTIVE_LEVEL,
                (skills[skill_id].level if skill_id in skills else 0)
                + equipment.skill_modifiers.get(skill_id, 0),
            )
            for skill_id in set(skills) | set(equipment.skill_modifiers)
        }
        attributes = self.attribute_service.attributes_for_user(
            user, equipment.stat_modifiers
        )
        advanced = self.attribute_service.advanced_attributes_for_user(
            user, equipment.advanced_stat_modifiers
        )
        return self.attribute_service.derive(
            level=user.level,
            attributes=attributes,
            equipment=equipment,
            advanced=advanced,
            effective_skills=effective,
        )
    def _item_for_slot(self, slots, items, slot):
        item_id = slots.get(slot)
        return next((item for item in items if item.id == item_id), None)

    def _weapon_mode(self, main, off) -> tuple[str, str]:
        if not main and not off:
            return "unarmed", "unarmed"
        two_hand = (
            main
            if main and main.hand_mode.startswith("two_hand")
            else off
            if off and off.hand_mode.startswith("two_hand")
            else None
        )
        if two_hand:
            return two_hand.hand_mode, two_hand.weapon_type
        weapons = [
            item for item in (main, off)
            if item and item.item_type == "weapon"
        ]
        shield = any(
            item and item.hand_mode == "shield" for item in (main, off)
        )
        if len(weapons) == 2:
            return "dual_wield", weapons[0].weapon_type
        if weapons and shield:
            return "sword_shield", weapons[0].weapon_type
        if weapons:
            return "one_hand", weapons[0].weapon_type
        return "unarmed", "unarmed"
