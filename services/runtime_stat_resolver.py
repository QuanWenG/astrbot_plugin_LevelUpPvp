from __future__ import annotations

import math
from dataclasses import replace

try:
    from ..models.attributes import (
        PRIMARY_ATTRIBUTE_IDS,
        AdvancedAttributes,
        PrimaryAttributes,
    )
    from .attribute_service import AttributeService
    from .material_catalog import armor_style_for_weight, weight_accuracy_multipliers
except ImportError:
    from models.attributes import (
        PRIMARY_ATTRIBUTE_IDS,
        AdvancedAttributes,
        PrimaryAttributes,
    )
    from services.attribute_service import AttributeService
    from services.material_catalog import armor_style_for_weight, weight_accuracy_multipliers


ARMOR_MOVEMENT = {"light": 1.0, "medium": 0.9, "heavy": 0.75}
ARMOR_STAMINA_REGEN = {"light": 10, "medium": 8, "heavy": 6}


class RuntimeStatResolver:
    """Resolve mutable combat stats from an immutable fighter snapshot."""

    def __init__(self, attribute_service: AttributeService | None = None):
        self.attribute_service = attribute_service or AttributeService()

    def initialize(self, fighter) -> None:
        fighter.current_attributes = fighter.snapshot.attributes
        fighter.current_derived = fighter.snapshot.derived
        fighter.runtime_effective_skills = self._effective_skills(fighter)
        equipment = fighter.snapshot.equipment
        if equipment:
            fighter.runtime_weight = equipment.total_weight
            fighter.runtime_armor_style = equipment.armor_style
            fighter.runtime_overloaded = equipment.overloaded

    def refresh(self, fighter) -> None:
        old_max_hp = fighter.max_hp
        old_max_mp = fighter.max_mp
        old_max_sp = fighter.max_sp
        old_hp = fighter.current_hp
        old_mana = fighter.mana
        old_stamina = fighter.stamina
        old_frozen = fighter.frozen_mana
        old_frozen_capacity = fighter.frozen_mana_capacity

        attributes = self._attributes(fighter)
        skills = self._effective_skills(fighter)
        equipment = self._runtime_equipment(fighter, attributes, skills)
        fighter.current_attributes = attributes
        fighter.runtime_effective_skills = skills
        if equipment and attributes:
            fighter.current_derived = self.attribute_service.derive(
                level=fighter.snapshot.level,
                attributes=attributes,
                equipment=equipment,
                advanced=(
                    fighter.snapshot.advanced_attributes
                    or AdvancedAttributes()
                ),
                effective_skills=skills,
            )
        else:
            fighter.current_derived = fighter.snapshot.derived

        self._rescale_resources(
            fighter,
            old_max_hp=old_max_hp,
            old_max_mp=old_max_mp,
            old_max_sp=old_max_sp,
            old_hp=old_hp,
            old_mana=old_mana,
            old_stamina=old_stamina,
            old_frozen=old_frozen,
            old_frozen_capacity=old_frozen_capacity,
        )

    def _attributes(self, fighter) -> PrimaryAttributes | None:
        base = fighter.snapshot.attributes
        if not base:
            return None
        modifiers = {attribute_id: 0.0 for attribute_id in PRIMARY_ATTRIBUTE_IDS}
        for status in fighter.statuses.values():
            for attribute_id in PRIMARY_ATTRIBUTE_IDS:
                modifiers[attribute_id] += float(status.params.get(attribute_id, 0.0))
        return PrimaryAttributes(
            *(
                max(0, round(base.get(attribute_id) * (1 + modifiers[attribute_id])))
                for attribute_id in PRIMARY_ATTRIBUTE_IDS
            )
        )

    def _effective_skills(self, fighter) -> dict[str, int]:
        levels = dict(
            fighter.snapshot.skills.effective_levels
            if fighter.snapshot.skills else {}
        )
        rebound = fighter.statuses.get("mental_rebound")
        if rebound:
            levels["mind_control"] = min(
                150,
                levels.get("mind_control", 0)
                + int(rebound.params.get("mind_control_level", rebound.magnitude)),
            )
        return levels

    def _runtime_equipment(self, fighter, attributes, skills):
        equipment = fighter.snapshot.equipment
        if not equipment or not attributes:
            return equipment
        floating = fighter.statuses.get("floating")
        weight_reduction = (
            float(floating.params.get("weight_reduction", floating.magnitude))
            if floating else 0.0
        )
        weight = max(
            0.0, equipment.total_weight * max(0.0, 1 - weight_reduction)
        )
        armor_style = armor_style_for_weight(weight)
        base_attributes = fighter.snapshot.attributes
        capacity = equipment.carry_capacity
        if base_attributes:
            capacity += (
                attributes.strength - base_attributes.strength
            ) * 0.5
            capacity += (
                attributes.constitution - base_attributes.constitution
            ) * 0.5
        overloaded = weight > capacity
        movement = ARMOR_MOVEMENT[armor_style]
        stamina_regen = ARMOR_STAMINA_REGEN[armor_style]
        if overloaded:
            movement *= max(0.5, capacity / max(weight, 0.1))
            stamina_regen = max(1, stamina_regen // 2)
        physical_accuracy, spell_accuracy = weight_accuracy_multipliers(
            armor_style, overloaded
        )
        original_movement = max(0.01, equipment.movement_multiplier)
        base_action_speed = equipment.action_speed / original_movement
        action_speed = max(50.0, min(180.0, base_action_speed * movement))
        fighter.runtime_weight = round(weight, 2)
        fighter.runtime_armor_style = armor_style
        fighter.runtime_overloaded = overloaded
        return replace(
            equipment,
            armor_style=armor_style,
            total_weight=round(weight, 2),
            carry_capacity=round(capacity, 2),
            overloaded=overloaded,
            movement_multiplier=movement,
            stamina_regen=stamina_regen,
            action_speed=action_speed,
            physical_accuracy_multiplier=physical_accuracy,
            spell_accuracy_multiplier=spell_accuracy,
        )

    def _rescale_resources(
        self,
        fighter,
        *,
        old_max_hp: int,
        old_max_mp: int,
        old_max_sp: int,
        old_hp: int,
        old_mana: int,
        old_stamina: int,
        old_frozen: int,
        old_frozen_capacity: int,
    ) -> None:
        fighter.current_hp = self._scaled_positive(
            old_hp, old_max_hp, fighter.max_hp, keep_zero=True
        )
        fighter.stamina = self._scaled_positive(
            old_stamina, old_max_sp, fighter.max_sp
        )

        if fighter.stance_id:
            new_frozen_capacity = math.ceil(fighter.max_mp * 0.25)
            fighter.frozen_mana = self._scaled_positive(
                old_frozen, old_frozen_capacity, new_frozen_capacity
            )
            fighter.frozen_mana_capacity = new_frozen_capacity
            if old_mana < 0:
                fighter.mana = old_mana
            else:
                old_available = max(0, old_max_mp - old_frozen_capacity)
                new_available = max(0, fighter.max_mp - new_frozen_capacity)
                fighter.mana = self._scaled_positive(
                    old_mana, old_available, new_available
                )
        else:
            fighter.frozen_mana = 0
            fighter.frozen_mana_capacity = 0
            fighter.mana = (
                old_mana
                if old_mana < 0
                else self._scaled_positive(
                    old_mana, old_max_mp, fighter.max_mp
                )
            )

    @staticmethod
    def _scaled_positive(
        value: int, old_maximum: int, new_maximum: int, *, keep_zero: bool = False
    ) -> int:
        if value <= 0:
            return 0 if keep_zero or value == 0 else value
        if old_maximum <= 0:
            return min(new_maximum, value)
        scaled = max(
            0, min(new_maximum, round(new_maximum * value / old_maximum))
        )
        return max(1, scaled) if keep_zero and new_maximum > 0 else scaled
