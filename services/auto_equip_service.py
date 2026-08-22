from __future__ import annotations

from collections import defaultdict

try:
    from ..models.attributes import PRIMARY_ATTRIBUTE_IDS
    from .equipment_affixes import effective_inherent_affixes
    from .equipment_catalog import QUALITY_MULTIPLIERS
    from .material_catalog import material_for
    from .attribute_service import WEAPON_PRIMARY_WEIGHTS
except ImportError:
    from models.attributes import PRIMARY_ATTRIBUTE_IDS
    from services.equipment_affixes import effective_inherent_affixes
    from services.equipment_catalog import QUALITY_MULTIPLIERS
    from services.material_catalog import material_for
    from services.attribute_service import WEAPON_PRIMARY_WEIGHTS


class AutoEquipService:
    """Select and persist a deterministic loadout for a player.

    The selector is intentionally independent from message handling so scheduled
    automation and the public ``/一键穿戴`` command cannot drift apart. Locked
    equipment is treated as a player constraint: equipped locked items remain in
    place and locked inventory items are never selected automatically.
    """

    _BASE_SLOTS = (
        "head", "neck", "back", "body", "wrist", "waist", "feet",
    )
    _RING_SLOTS = ("left_finger", "right_finger")

    def __init__(self, build_service):
        self.build_service = build_service

    @staticmethod
    def dominant_attribute(attributes) -> str:
        def value(name: str):
            try:
                return attributes.get(name)
            except TypeError:
                return attributes.get(name, 0)

        return max(PRIMARY_ATTRIBUTE_IDS, key=value)

    def score_item(self, item, user, dominant_attr: str) -> float:
        material = material_for(item.material)
        level_factor = max(
            0.50, 1.0 - max(0, item.item_level - user.level) * 0.03
        )
        quality_factor = QUALITY_MULTIPLIERS.get(item.quality, 1.0)
        score = 0.0
        for stat, raw_value in item.base_stats.items():
            raw_value = float(raw_value)
            if stat in {"atk", "weapon_power"} and item.item_type == "weapon":
                value = (
                    raw_value * material.attack_factor * quality_factor
                    + item.enhancement_level
                    + item.item_level // 10
                ) * level_factor
                score += value * 3.0
            elif stat in {"defense", "armor_power"}:
                value = (
                    raw_value * material.defense_factor * quality_factor
                    + item.enhancement_level * 2
                ) * level_factor
                score += value * 2.0
            elif stat == "accuracy":
                score += (
                    raw_value * material.accuracy_factor
                    * quality_factor * level_factor * 0.5
                )
            elif stat == "evasion":
                score += (
                    raw_value * material.evasion_factor
                    * quality_factor * level_factor * 0.5
                )
            elif stat in PRIMARY_ATTRIBUTE_IDS:
                value = raw_value * quality_factor * level_factor
                weight = 2.0 if stat == dominant_attr else 0.5
                score += value * weight
            elif stat == "hp":
                score += raw_value * quality_factor * level_factor * 10 * 0.1
            elif stat == "speed":
                score += raw_value * quality_factor * level_factor * 3 * 0.2
            elif stat == "luck":
                score += raw_value * quality_factor * level_factor * 0.5

        effective_inherent = effective_inherent_affixes(
            item.inherent_affixes, user.level, item.item_level
        )
        for affix in effective_inherent + item.random_affixes + item.fusion_affixes:
            kind = str(affix.get("type", ""))
            value = float(affix.get("value", 0))
            if kind == "stat_flat":
                stat = str(affix.get("stat", ""))
                if stat in {"hp", "defense", "speed", "atk", "luck"}:
                    stat = {
                        "hp": "strength",
                        "defense": "constitution",
                        "speed": "dexterity",
                        "atk": "perception",
                        "luck": "magic",
                    }.get(stat, stat)
                if stat in PRIMARY_ATTRIBUTE_IDS:
                    weight = 2.0 if stat == dominant_attr else 0.5
                    score += value * weight
            elif kind == "advanced_stat":
                score += value * 0.5
            elif kind == "skill_level":
                score += value * 5.0
            elif kind == "trigger_ability":
                score += value * 3.0
            elif kind == "element_resistance" or kind.startswith("resistance_"):
                score += value * 0.3
            else:
                score += value
        if item.item_type == "weapon":
            weights = WEAPON_PRIMARY_WEIGHTS.get(
                item.weapon_type, WEAPON_PRIMARY_WEIGHTS[""]
            )
            score *= 0.1 + weights.get(dominant_attr, 0)
        return score

    def select_optimal_loadout(
        self,
        items,
        user,
        skills,
        dominant_attr: str | None = None,
        *,
        locked_slots: dict[str, object] | None = None,
    ):
        dominant_attr = dominant_attr or self.dominant_attribute(
            {
                "strength": user.strength,
                "constitution": user.constitution,
                "dexterity": user.dexterity,
                "perception": user.perception,
                "magic": user.magic,
                "willpower": user.willpower,
            }
        )
        locked_slots = dict(locked_slots or {})
        locked_ids = {
            int(item.id)
            for item in locked_slots.values()
            if getattr(item, "id", None) is not None
        }
        # Keep equipped locked items fixed. Locked inventory items are excluded
        # from automatic selection, but do not affect normal candidate scoring.
        candidates = [
            item for item in items
            if not getattr(item, "is_locked", False)
            or (getattr(item, "id", None) in locked_ids)
        ]
        locked_items = {
            int(item.id): item
            for item in locked_slots.values()
            if getattr(item, "id", None) is not None
        }
        by_slot = defaultdict(list)
        weapons = []
        shields = []
        rings = []
        occupied = set(locked_slots)
        for item in candidates:
            if item.id in locked_items:
                continue
            if item.hand_mode == "shield":
                shields.append(item)
            elif item.item_type == "weapon":
                weapons.append(item)
            elif item.equip_slot in self._RING_SLOTS:
                rings.append(item)
            else:
                by_slot[item.equip_slot].append(item)

        base_slots = dict(locked_slots)
        for slot in self._BASE_SLOTS:
            if slot in occupied:
                continue
            values = by_slot.get(slot)
            if values:
                base_slots[slot] = max(
                    values,
                    key=lambda item: (
                        self.score_item(item, user, dominant_attr),
                        -int(item.id or 0),
                    ),
                )

        free_ring_slots = [slot for slot in self._RING_SLOTS if slot not in occupied]
        scored_rings = sorted(
            rings,
            key=lambda item: (
                self.score_item(item, user, dominant_attr),
                -int(item.id or 0),
            ),
            reverse=True,
        )
        for slot, ring in zip(free_ring_slots, scored_rings):
            base_slots[slot] = ring

        free_hand_slots = {slot for slot in ("main_hand", "off_hand") if slot not in occupied}
        hand_options = self._generate_hand_options(
            weapons,
            shields,
            user,
            dominant_attr,
            free_hand_slots,
        )
        best_score = float("-inf")
        best_slots = dict(base_slots)
        for hand_slots, _hand_items in hand_options:
            test_slots = dict(base_slots)
            test_slots.update(hand_slots)
            slot_ids = {
                slot: item.id for slot, item in test_slots.items()
                if item.id is not None
            }
            unique_items = list({item.id: item for item in test_slots.values()}.values())
            build = self.build_service.resolve_equipment(
                user, slot_ids, unique_items, skills
            )
            score = self._score_build(build, dominant_attr)
            tie_break = tuple(
                int(test_slots[slot].id or 0)
                for slot in ("main_hand", "off_hand")
                if slot in test_slots
            )
            if score > best_score or (
                score == best_score
                and tie_break < tuple(
                    int(best_slots[slot].id or 0)
                    for slot in ("main_hand", "off_hand")
                    if slot in best_slots
                )
            ):
                best_score = score
                best_slots = test_slots

        seen_ids = set()
        assignments = []
        for slot, item in best_slots.items():
            if item.id in seen_ids or item.id is None:
                continue
            seen_ids.add(item.id)
            if slot in {"main_hand", "off_hand", "left_finger", "right_finger"}:
                assignments.append((item.id, slot))
            else:
                assignments.append((item.id, ""))
        return assignments

    async def select_for_user(self, user, *, respect_locked: bool = True):
        items = await self.build_service.equipment_service.list_items(user.id)
        if not items:
            return []
        skills, _ = await self.build_service.skill_service.get_skills(user)
        attributes = self.build_service.attribute_service.attributes_for_user(user)
        dominant = self.dominant_attribute(attributes)
        locked_slots = {}
        if respect_locked:
            slots, loadout_items = await self.build_service.equipment_service.get_loadout(user.id)
            by_id = {int(item.id): item for item in loadout_items if item.id is not None}
            locked_slots = {
                slot: by_id[item_id]
                for slot, item_id in slots.items()
                if item_id in by_id and getattr(by_id[item_id], "is_locked", False)
            }
        return self.select_optimal_loadout(
            items,
            user,
            skills,
            dominant,
            locked_slots=locked_slots,
        )

    async def auto_equip_user(self, user, *, respect_locked: bool = True):
        assignments = await self.select_for_user(
            user,
            respect_locked=respect_locked,
        )
        if not assignments:
            return []
        slots, _ = await self.build_service.equipment_service.get_loadout(user.id)
        selected_ids = {int(item_id) for item_id, _ in assignments}
        current_ids = {int(item_id) for item_id in slots.values()}
        if selected_ids == current_ids:
            return []
        return await self.build_service.equipment_service.auto_equip(
            user.id,
            assignments,
        )

    def _generate_hand_options(
        self,
        weapons,
        shields,
        user,
        dominant_attr,
        free_hand_slots: set[str],
    ):
        if not free_hand_slots:
            return [({}, [])]
        options = []
        two_hand = [w for w in weapons if w.hand_mode.startswith("two_hand")]
        one_hand = [w for w in weapons if not w.hand_mode.startswith("two_hand")]
        if free_hand_slots == {"main_hand", "off_hand"}:
            for weapon in two_hand:
                options.append(({"main_hand": weapon, "off_hand": weapon}, [weapon]))
            if one_hand and shields:
                best_w = max(
                    one_hand,
                    key=lambda item: self.score_item(item, user, dominant_attr),
                )
                best_s = max(
                    shields,
                    key=lambda item: self.score_item(item, user, dominant_attr),
                )
                options.append(
                    ({"main_hand": best_w, "off_hand": best_s}, [best_w, best_s])
                )
            if len(one_hand) >= 2:
                scored = sorted(
                    one_hand,
                    key=lambda item: self.score_item(item, user, dominant_attr),
                    reverse=True,
                )
                options.append(
                    (
                        {"main_hand": scored[0], "off_hand": scored[1]},
                        [scored[0], scored[1]],
                    )
                )
            if one_hand:
                best_w = max(
                    one_hand,
                    key=lambda item: self.score_item(item, user, dominant_attr),
                )
                options.append(({"main_hand": best_w}, [best_w]))
            if shields:
                best_s = max(
                    shields,
                    key=lambda item: self.score_item(item, user, dominant_attr),
                )
                options.append(({"off_hand": best_s}, [best_s]))
        elif "main_hand" in free_hand_slots:
            # A locked off-hand occupies half of the two-handed pair; never
            # offer a two-handed candidate that would evict that protected item.
            for weapon in one_hand:
                options.append(({"main_hand": weapon}, [weapon]))
            if shields and "off_hand" in free_hand_slots:
                best_s = max(
                    shields,
                    key=lambda item: self.score_item(item, user, dominant_attr),
                )
                options.append(({"off_hand": best_s}, [best_s]))
        elif "off_hand" in free_hand_slots and shields:
            best_s = max(
                shields,
                key=lambda item: self.score_item(item, user, dominant_attr),
            )
            options.append(({"off_hand": best_s}, [best_s]))
        return options or [({}, [])]

    @staticmethod
    def _score_build(equipment, dominant_attr: str) -> float:
        weapon_match = 1.0
        if equipment.weapon_type:
            weights = WEAPON_PRIMARY_WEIGHTS.get(
                equipment.weapon_type, WEAPON_PRIMARY_WEIGHTS[""]
            )
            weapon_match = 0.3 + weights.get(dominant_attr, 0) * 0.7
        score = equipment.weapon_power * 3.0 * equipment.damage_multiplier * weapon_match
        score += equipment.armor_power * 2.0
        for attr in PRIMARY_ATTRIBUTE_IDS:
            value = equipment.stat_modifiers.get(attr, 0)
            score += value * (2.0 if attr == dominant_attr else 0.5)
        for value in equipment.skill_modifiers.values():
            score += value * 3.0
        effects = equipment.combat_effects
        score += effects.get("max_hp", 0) * 0.1
        score += effects.get("accuracy", 0) * 0.5
        score += effects.get("evasion", 0) * 0.5
        score += effects.get("critical_rate", 0) * 50.0
        score += effects.get("critical_damage", 0) * 20.0
        score += effects.get("block_rate", 0) * 30.0
        score += effects.get("knockback_resistance", 0) * 20.0
        if equipment.overloaded:
            score *= 0.5
        return score


__all__ = ["AutoEquipService"]
