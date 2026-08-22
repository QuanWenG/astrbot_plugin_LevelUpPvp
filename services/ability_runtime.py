from __future__ import annotations

import math
from dataclasses import replace

try:
    from ..models.ability import BattleEntity, BattleZone, CombatStatus
    from ..models.combat import BattleEvent
    from .balance_rules import (
        physical_damage_amount,
        resistance_multiplier,
        spell_damage_amount,
        status_chance,
        triangular_variance,
    )
    from .spell_rules import (
        SCHOOL_READING_ATTRIBUTES,
        calculate_mana_cost,
        healing_base_power,
        spell_base_power,
        spell_effect_scale,
        spell_multiplier_for,
    )
    from .runtime_stat_resolver import RuntimeStatResolver
    from .combat_ruleset import CombatRuleSet, SIDEVIEW_V11_RULESET
except ImportError:
    from models.ability import BattleEntity, BattleZone, CombatStatus
    from models.combat import BattleEvent
    from services.balance_rules import (
        physical_damage_amount,
        resistance_multiplier,
        spell_damage_amount,
        status_chance,
        triangular_variance,
    )
    from services.spell_rules import (
        SCHOOL_READING_ATTRIBUTES,
        calculate_mana_cost,
        healing_base_power,
        spell_base_power,
        spell_effect_scale,
        spell_multiplier_for,
    )
    from services.runtime_stat_resolver import RuntimeStatResolver
    from services.combat_ruleset import CombatRuleSet, SIDEVIEW_V11_RULESET


STACKING_STATUSES = {"haze", "burn", "poison", "bleed"}
HARD_CONTROL = {"stun", "paralysis"}
NEGATIVE_STATUSES = {
    "haze", "burn", "poison", "bleed", "blind", "confusion", "stun",
    "paralysis", "bind", "silence", "slow", "gravity", "wet", "disease",
    "curse", "defense_down", "accuracy_down", "healing_down", "nightmare",
    "body_slow", "mind_slow", "magic_scar", "element_scar", "death_shadow",
}
SCALABLE_BENEFICIAL_PARAMS = {
    "strength", "constitution", "dexterity", "perception", "magic",
    "willpower", "defense", "evasion", "physical_damage",
    "physical_reduction", "healing_bonus", "ranged_speed",
    "ranged_followup", "followup", "reading",
    "mana_shield_ratio",
    "periodic_damage",
    "resistance_magic", "resistance_fire", "resistance_cold",
    "resistance_lightning", "resistance_shadow", "resistance_nature",
    "resistance_mind", "resistance_hell",
}
SCALABLE_SPELL_PARAMS = SCALABLE_BENEFICIAL_PARAMS | {
    "damage_penalty", "mp_cost_reduction", "status_resistance",
    "slow", "healing_reduction", "block_rate", "penetration",
    "weight_reduction",
}


class AbilityRuntime:
    """Data-driven action effect executor shared by techniques and spells."""

    def __init__(
        self,
        stat_resolver: RuntimeStatResolver | None = None,
        ruleset: CombatRuleSet | None = None,
    ):
        self.stat_resolver = stat_resolver or RuntimeStatResolver()
        self.ruleset = ruleset or SIDEVIEW_V11_RULESET

    def has(self, fighter, status_id: str) -> bool:
        return status_id in fighter.statuses

    def modifier(self, fighter, key: str) -> float:
        value = sum(float(s.params.get(key, 0.0)) for s in fighter.statuses.values())
        for status in fighter.statuses.values():
            if key == "physical_reduction" and status.status_id == "shield_wall": value += 0.50
            if key == "physical_reduction" and status.status_id == "bull_endurance": value += 0.20
            if key == "block" and status.status_id == "never_retreat": value += 0.20
            if key == "block": value += float(status.params.get("block_rate", 0.0))
            if key == "healing": value += float(status.params.get("healing_bonus", 0.0))
            if key == "physical_damage" and status.status_id == "barbarian_rage":
                value += min(0.30, fighter.primary("strength") * 0.003)
            if key == "physical_damage" and status.status_id == "dark_lotus":
                level = fighter.skill_level("dual_wield")
                value += min(0.20, level * 0.002)
            if key == "physical_damage" and status.status_id == "hunting_moment" and fighter.snapshot.is_ranged:
                value += status.magnitude + min(
                    float(status.params.get("dexterity_damage_cap", 0.20)),
                    fighter.primary("dexterity") * 0.002,
                )
            if key == "physical_damage" and status.status_id == "martial_awakening":
                level = fighter.skill_level("unarmed")
                if level >= 80: value += 0.25
            if key == "followup" and status.status_id == "martial_awakening":
                level = fighter.skill_level("unarmed")
                if level >= 60: value += 0.20
            if key.startswith("resistance_") and status.status_id == key + "_down":
                value -= status.magnitude
            if key == "healing" and status.status_id in {"healing_block", "evil_fear"}:
                value -= max(status.magnitude, float(status.params.get("healing_reduction", 0.0)))
            if key == "accuracy" and status.status_id == "accuracy_down": value -= status.magnitude
            if key == "defense" and status.status_id == "defense_down": value -= status.magnitude
        return value

    def action_blocked(self, fighter) -> bool:
        return any(key in fighter.statuses for key in HARD_CONTROL)

    def compatible(self, definition, fighter) -> bool:
        if definition.compatible_weapon_types and fighter.snapshot.weapon_type not in definition.compatible_weapon_types:
            return False
        if definition.compatible_weapon_modes and fighter.snapshot.weapon_mode not in definition.compatible_weapon_modes:
            return False
        if definition.ability_type == "spell" and self.has(fighter, "silence"):
            return False
        return not self.action_blocked(fighter)

    def mana_cost_breakdown(self, definition, fighter):
        return calculate_mana_cost(
            definition, fighter, self.has(fighter, "void_embrace")
        )

    def effective_cost(self, definition, fighter) -> int:
        if definition.ability_type == "spell":
            return self.mana_cost_breakdown(definition, fighter).final_cost
        return definition.resource_cost

    def _scaled_beneficial_effect(self, definition, fighter, effect):
        if (
            definition.unlock_skill_id != "blessing"
            or effect.effect_type != "apply_status"
            or not effect.params.get("beneficial", False)
        ):
            return effect
        power = (
            fighter.current_derived.blessing_power
            if fighter.current_derived else 1.0
        )
        params = dict(effect.params)
        for key in SCALABLE_BENEFICIAL_PARAMS:
            value = params.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                params[key] = value * power
        return replace(effect, value=effect.value * power, params=params)

    def _scaled_spell_effect(self, definition, fighter, effect):
        """Apply learned spell-level growth to utility effect payloads."""
        if (
            definition.ability_type != "spell"
            or effect.effect_type in {
                "physical_damage", "magic_damage", "heal"
            }
        ):
            return effect
        scale = spell_effect_scale(definition, fighter)
        params = dict(effect.params)
        for key in SCALABLE_SPELL_PARAMS:
            value = params.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                params[key] = value * scale.magnitude_multiplier
        value = effect.value
        duration = effect.duration_ticks
        radius = effect.radius
        if effect.effect_type == "apply_status":
            value *= scale.magnitude_multiplier
            duration = max(
                1,
                round(duration * scale.duration_multiplier),
            )
            if not params.get("beneficial", False):
                params["status_power"] = (
                    float(params.get("status_power", 0.0))
                    + scale.status_power_bonus
                )
            params["source_attribute"] = SCHOOL_READING_ATTRIBUTES.get(
                definition.unlock_skill_id,
                "magic",
            )
            damage_type = (
                effect.damage_type
                if effect.damage_type != "physical" else "magic"
            )
            return replace(
                effect,
                value=value,
                duration_ticks=duration,
                damage_type=damage_type,
                params=params,
            )
        if effect.effect_type in {
            "teleport", "restore_resource", "drain_resource"
        }:
            value *= scale.distance_multiplier
        if effect.effect_type in {"summon", "create_zone"}:
            duration = max(
                1,
                round(duration * scale.duration_multiplier),
            )
            radius = max(1, round(radius * scale.distance_multiplier))
        return replace(
            effect,
            value=value,
            duration_ticks=duration,
            radius=radius,
            params=params,
        )

    def _scaled_summon_params(self, fighter, params):
        power = (
            fighter.current_derived.summon_power
            if fighter.current_derived else 1.0
        )
        result = dict(params)
        for key in SCALABLE_BENEFICIAL_PARAMS:
            value = result.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = value * power
        return result

    def _periodic_magic_damage(
        self,
        source,
        target,
        definition,
        effect,
    ) -> int:
        """Resolve a zone or summon pulse through the normal spell curve."""
        if not definition:
            return max(1, round(effect.value))
        if definition.ability_type == "monster":
            base_power = max(
                8.0,
                (
                    source.current_derived.attack_power
                    if source.current_derived
                    else source.snapshot.stat("atk") * 4
                ) * 0.45,
            )
            spell_multiplier = 1.0
        else:
            spell = (
                source.snapshot.skills.spells.get(definition.ability_id)
                if source.snapshot.skills else None
            )
            spell_level = spell.level if spell else 1
            base_power = spell_base_power(definition, source, spell_level)
            spell_multiplier = spell_multiplier_for(
                definition, source, effect.damage_type
            )
        resistance = (
            target.current_derived.resistances.get(effect.damage_type, 0.0)
            if target.current_derived else 0.0
        )
        resistance += self.modifier(
            target, f"resistance_{effect.damage_type}"
        )
        return spell_damage_amount(
            base_power=base_power,
            effect_multiplier=effect.value,
            spell_multiplier=spell_multiplier,
            variance=1.0,
            resistance=resistance,
            attacker_level=source.snapshot.level,
            magical_reduction=(
                target.current_derived.magical_reduction
                if target.current_derived else 0.0
            ),
            ruleset=self.ruleset,
        )

    @staticmethod
    def _deal_tick_damage(
        state,
        source,
        target,
        damage: int,
        *,
        event_kind: str,
        damage_type: str,
        apply_damage=None,
        skill_id: str | None = None,
        status_id: str | None = None,
        zone_id: str | None = None,
        entity_id: str | None = None,
    ) -> int:
        """Route periodic damage through the engine when one is available."""

        damage = max(0, int(damage))
        if damage <= 0:
            return 0
        if apply_damage is not None:
            applied = apply_damage(
                state,
                source,
                target,
                (
                    damage,
                    False,
                    False,
                    0,
                    skill_id,
                    {damage_type: damage},
                ),
                event_kind,
                False,
                causes_hit_reaction=False,
                credit_damage=source is not target,
                status_id=status_id,
                zone_id=zone_id,
                entity_id=entity_id,
            )
            return int(applied[0]) if applied else 0

        target.current_hp = max(0, target.current_hp - damage)
        if source is not target:
            source.damage_dealt += damage
        state.events.append(
            BattleEvent(
                state.tick,
                event_kind,
                source.snapshot.user_pk,
                target.snapshot.user_pk,
                value=damage,
                remaining_hp=target.current_hp,
                damage_type=damage_type,
                damage_breakdown={damage_type: damage},
                skill_id=skill_id,
                status_id=status_id,
                zone_id=zone_id,
                entity_id=entity_id,
            )
        )
        return damage

    def tick(self, state, rng, *, apply_damage=None) -> None:
        for fighter in (state.attacker, state.defender):
            for status in list(fighter.statuses.values()):
                if status.status_id in {"burn", "poison", "bleed"} and state.tick % 5 == 0:
                    damage = max(1, round(status.magnitude * status.stacks))
                    source = state.attacker if state.attacker.snapshot.user_pk == status.source_pk else state.defender
                    self._deal_tick_damage(
                        state,
                        source,
                        fighter,
                        damage,
                        event_kind="status_damage",
                        damage_type={"burn": "fire", "poison": "nature", "bleed": "physical"}[status.status_id],
                        apply_damage=apply_damage,
                        skill_id=str(status.params.get("source_ability_id", "")) or None,
                        status_id=status.status_id,
                    )
                if status.status_id in {"regeneration", "despair_regen"} and fighter.current_hp < fighter.max_hp:
                    healing_multiplier = max(
                        0.0, 1 + self.modifier(fighter, "healing")
                    )
                    if self.has(fighter, "healing_block"):
                        healing_multiplier = 0.0
                    amount = min(
                        fighter.max_hp - fighter.current_hp,
                        max(0, round(status.magnitude * healing_multiplier)),
                    )
                    if amount > 0:
                        fighter.current_hp += amount
                        state.events.append(BattleEvent(state.tick, "recover_hp", fighter.snapshot.user_pk, value=amount, remaining_hp=fighter.current_hp, status_id=status.status_id))
                status.remaining_ticks -= 1
                if status.remaining_ticks <= 0:
                    self.remove_status(state, fighter, status.status_id)

        for zone in list(state.zones):
            for fighter in (state.attacker, state.defender):
                if (
                    (
                        fighter.snapshot.user_pk == zone.owner_pk
                        and not zone.affects_owner
                    )
                    or abs(fighter.position - zone.position) > zone.radius
                ):
                    continue
                for effect in zone.effects:
                    if effect.effect_type == "magic_damage" and state.tick % 5 == 0:
                        source = (
                            state.attacker
                            if state.attacker.snapshot.user_pk == zone.owner_pk
                            else state.defender
                        )
                        ability_id = zone.zone_id.split(":", 1)[0]
                        definition = (
                            source.snapshot.skills.active_definitions.get(
                                ability_id
                            )
                            if source.snapshot.skills else None
                        )
                        damage = self._periodic_magic_damage(
                            source, fighter, definition, effect
                        )
                        self._deal_tick_damage(
                            state,
                            source,
                            fighter,
                            damage,
                            event_kind="zone_damage",
                            damage_type=effect.damage_type,
                            apply_damage=apply_damage,
                            skill_id=ability_id,
                            zone_id=zone.zone_id,
                        )
                    elif (
                        effect.effect_type == "apply_status"
                        and state.tick % 5 == 0
                    ):
                        self.apply_status(state, fighter, effect, zone.owner_pk, rng)
            zone.remaining_ticks -= 1
            if zone.remaining_ticks <= 0:
                state.zones.remove(zone)
                state.events.append(BattleEvent(state.tick, "zone_expire", zone.owner_pk, zone_id=zone.zone_id))
        for entity in list(state.entities):
            owner = state.attacker if state.attacker.snapshot.user_pk == entity.owner_pk else state.defender
            for aura in entity.aura_effects:
                if aura.effect_type != "apply_status":
                    continue
                if abs(owner.position - entity.position) <= entity.aura_radius:
                    current = owner.statuses.get(aura.status_id)
                    if current:
                        current.remaining_ticks = max(current.remaining_ticks, entity.remaining_ticks)
                    else:
                        self.apply_status(state, owner, aura, entity.owner_pk, rng)
                else:
                    self.remove_status(state, owner, aura.status_id)
            opponent = state.defender if owner is state.attacker else state.attacker
            if (
                opponent.alive
                and entity.attack_effects
                and entity.attack_interval > 0
                and state.tick > entity.spawned_tick
                and (state.tick - entity.spawned_tick) % entity.attack_interval == 0
                and abs(opponent.position - entity.position) <= entity.aura_radius
            ):
                definition = (
                    owner.snapshot.skills.active_definitions.get(
                        entity.source_ability_id
                    )
                    if owner.snapshot.skills else None
                )
                for effect in entity.attack_effects:
                    if effect.effect_type == "magic_damage":
                        damage = self._periodic_magic_damage(
                            owner, opponent, definition, effect
                        )
                        self._deal_tick_damage(
                            state,
                            owner,
                            opponent,
                            damage,
                            event_kind="summon_strike",
                            damage_type=effect.damage_type,
                            apply_damage=apply_damage,
                            skill_id=entity.source_ability_id,
                            entity_id=entity.entity_id,
                        )
                    elif effect.effect_type == "apply_status":
                        self.apply_status(
                            state,
                            opponent,
                            effect,
                            owner.snapshot.user_pk,
                            rng,
                        )
            entity.remaining_ticks -= 1
            if entity.remaining_ticks <= 0:
                state.entities.remove(entity)
                state.events.append(BattleEvent(state.tick, "entity_expire", entity.owner_pk, entity_id=entity.entity_id))

    def remove_status(self, state, fighter, status_id: str) -> None:
        status = fighter.statuses.pop(status_id, None)
        if not status:
            return
        if status_id == "floating":
            fighter.runtime_armor_style = ""
            fighter.runtime_weight = 0.0
            fighter.runtime_overloaded = False
        if fighter.stance_id == status_id:
            fighter.stance_id = None
            fighter.mana = min(fighter.max_mp, fighter.mana + fighter.frozen_mana)
            fighter.frozen_mana = 0
            fighter.frozen_mana_capacity = 0
        self.stat_resolver.refresh(fighter)
        state.events.append(BattleEvent(state.tick, "status_expire", fighter.snapshot.user_pk, status_id=status_id))

    def apply_status(self, state, target, effect, source_pk: int, rng) -> bool:
        status_id = effect.status_id
        source_ability_id = (
            str(effect.params.get("source_ability_id", "")) or None
        )

        def record_resist() -> None:
            state.events.append(
                BattleEvent(
                    state.tick,
                    "status_resist",
                    source_pk,
                    target.snapshot.user_pk,
                    skill_id=source_ability_id,
                    status_id=status_id,
                )
            )

        # Older equipment procs and replay adapters used small duck-typed
        # status objects without a damage type.  Treat those as physical
        # techniques, matching the pre-v11 status contest semantics.
        damage_type = getattr(effect, "damage_type", "physical")
        if status_id in {"mental_random", "snare_random"}:
            choices = tuple(effect.params.get("choices", ()))
            status_id = choices[rng.randrange(len(choices))] if choices else status_id
        if status_id == "gravity" and "floating" in target.statuses:
            self.remove_status(state, target, "floating")
        beneficial = bool(effect.params.get("beneficial", False))
        chance = effect.chance
        if not beneficial:
            if (
                status_id in HARD_CONTROL
                and state.tick < target.hard_control_immunity_until
            ):
                record_resist()
                return False
            if any(bool(s.params.get(f"{status_id}_immunity", False)) for s in target.statuses.values()):
                record_resist()
                return False
            immunity = target.snapshot.equipment.combat_effects.get("status_immunity", 0) if target.snapshot.equipment else 0
            if immunity and rng.random() < min(1.0, float(immunity)):
                record_resist()
                return False
            resistance_key = "mind" if status_id in {"confusion", "paralysis", "haze", "blind"} else "nature"
            resistance = target.current_derived.resistances.get(resistance_key, 0.0) if target.current_derived else 0.0
            status_resistance = sum(s.magnitude for s in target.statuses.values() if s.status_id == "status_resistance")
            equipment_effects = (
                target.snapshot.equipment.combat_effects
                if target.snapshot.equipment else {}
            )
            equipment_status_resistance = min(
                0.90,
                max(
                    0.0,
                    float(equipment_effects.get("status_resistance", 0))
                    + float(
                        equipment_effects.get(
                            f"status_resistance_{status_id}", 0
                        )
                    ),
                ),
            )
            source = (
                state.attacker
                if state.attacker.snapshot.user_pk == source_pk
                else state.defender
            )
            source_attribute = str(
                effect.params.get("source_attribute", "")
            )
            governing_attribute = (
                source.primary(source_attribute)
                if source_attribute in {
                    "strength", "constitution", "dexterity", "perception",
                    "magic", "willpower",
                }
                else source.primary("perception")
                if damage_type == "physical"
                else source.primary("magic")
            )
            source_skill_id = str(effect.params.get("source_skill_id", ""))
            relevant_skill = (
                source.runtime_effective_skills.get(source_skill_id, 0)
                if source_skill_id
                else max(source.runtime_effective_skills.values(), default=0)
            )
            compressed_resistance = 20.0 * math.log1p(
                max(0.0, resistance) / 20.0
            )
            potency = (
                governing_attribute
                + 0.70 * relevant_skill
                + float(effect.params.get("status_power", 0.0))
            )
            tenacity = (
                0.70 * target.primary("willpower")
                + compressed_resistance
                + 60.0 * max(
                    0.0,
                    status_resistance + equipment_status_resistance,
                )
            )
            chance = status_chance(
                base_chance=chance,
                potency=potency,
                tenacity=tenacity,
                combat_level=round(
                    (source.snapshot.level + target.snapshot.level) / 2
                ),
                hard_control=status_id in HARD_CONTROL,
                ruleset=self.ruleset,
            )
            if status_id == "bleed" and self.has(target, "tree_skin"):
                chance *= max(
                    0.0,
                    1 - float(target.statuses["tree_skin"].params.get("bleed_resistance", 0.0)),
                )
        if rng.random() >= chance:
            record_resist()
            return False
        duration = max(1, effect.duration_ticks)
        if status_id in HARD_CONTROL:
            status_rules = self.ruleset.status
            recent_controls = sum(
                event.kind == "status_apply"
                and event.target_pk == target.snapshot.user_pk
                and event.status_id in HARD_CONTROL
                and event.tick >= state.tick - 20
                for event in state.events
            )
            capped_duration = min(
                status_rules.hard_control_duration_cap_ticks, duration
            )
            duration = min(
                capped_duration,
                max(
                    1,
                    round(
                        capped_duration
                        * (
                            status_rules.repeated_control_multiplier
                            ** recent_controls
                        )
                    ),
                ),
            )
            target.hard_control_immunity_until = (
                state.tick
                + duration
                + status_rules.post_control_immunity_ticks
            )
        if status_id == "haze" and self.has(target, "free_thought"):
            duration = max(
                1,
                round(
                    duration
                    * (
                        1
                        - float(
                            target.statuses["free_thought"].params.get(
                                "haze_duration_reduction", 0.0
                            )
                        )
                    )
                ),
            )
        current = target.statuses.get(status_id)
        if current:
            current.remaining_ticks = max(current.remaining_ticks, duration)
            current.magnitude = max(current.magnitude, effect.value)
            if status_id in STACKING_STATUSES:
                current.stacks = min(3, current.stacks + 1)
        else:
            target.statuses[status_id] = CombatStatus(
                status_id, source_pk, duration, 1, effect.value,
                beneficial, True, dict(effect.params),
            )
        self.stat_resolver.refresh(target)
        state.events.append(BattleEvent(
            state.tick,
            "status_apply",
            source_pk,
            target.snapshot.user_pk,
            value=duration,
            skill_id=source_ability_id,
            status_id=status_id,
        ))
        return True

    def damage_result(self, actor, target, definition, rng):
        physical_effects = [e for e in definition.effects if e.effect_type == "physical_damage"]
        magic_effects = [e for e in definition.effects if e.effect_type == "magic_damage"]
        if not physical_effects and not magic_effects:
            return (0, False, False, 0, definition.ability_id, {})
        breakdown = {}
        variance = triangular_variance(
            rng.random(), rng.random(), ruleset=self.ruleset
        )
        for effect in physical_effects:
            derived = actor.current_derived
            target_derived = target.current_derived
            target_defense = (target_derived.defense if target_derived else target.snapshot.stat("defense")) * max(0.1, 1 + self.modifier(target, "defense"))
            equipment_effects = (
                actor.snapshot.equipment.combat_effects
                if actor.snapshot.equipment else {}
            )
            penetration = min(
                0.75,
                max(0.0, float(equipment_effects.get("armor_penetration", 0))),
            )
            target_defense *= 1.0 - penetration
            multiplier = effect.value * (derived.physical_damage_multiplier if derived else 1.0)
            multiplier *= 1 + self.modifier(actor, "physical_damage") - self.modifier(actor, "damage_penalty")
            threshold = effect.params.get("bonus_above_hp")
            if threshold is not None and target.hp_ratio > float(threshold):
                multiplier *= float(effect.params.get("bonus_multiplier", 1.0))
            if effect.params.get("bonus_if_stance") and actor.stance_id:
                multiplier *= float(effect.params["bonus_if_stance"])
            required_status = effect.params.get("bonus_if_status")
            if required_status and self.has(target, str(required_status)):
                multiplier *= float(effect.params.get("bonus_multiplier", 1.0))
            required_statuses = tuple(effect.params.get("bonus_if_both_status", ()))
            if required_statuses and all(self.has(target, str(item)) for item in required_statuses):
                multiplier *= float(effect.params.get("bonus_multiplier", 1.0))
            reduction = (target_derived.physical_reduction if target_derived else 0) + self.modifier(target, "physical_reduction")
            if self.has(actor, "hunting_moment") and actor.snapshot.is_ranged:
                reduction *= max(
                    0.0,
                    1
                    - float(
                        actor.statuses["hunting_moment"].params.get(
                            "penetration", 0.20
                        )
                    ),
                )
            amount = physical_damage_amount(
                attack_power=(
                    derived.attack_power
                    if derived else actor.snapshot.stat("atk") * 4
                ) + max(
                    0.0,
                    float(effect.params.get("flat_attack_power", 0.0)),
                ),
                offense_multiplier=(
                    derived.physical_damage_multiplier if derived else 1.0
                ),
                effect_multiplier=multiplier / (
                    derived.physical_damage_multiplier if derived else 1.0
                ),
                variance=variance,
                defense=target_defense,
                attacker_level=actor.snapshot.level,
                physical_reduction=reduction,
                ruleset=self.ruleset,
            )
            breakdown["physical"] = breakdown.get("physical", 0) + amount
        if physical_effects and actor.current_derived and target.current_derived:
            for damage_type, bonus in actor.current_derived.elemental_damage.items():
                if bonus <= 0:
                    continue
                resistance = target.current_derived.resistances.get(damage_type, 0.0)
                amount = round(
                    bonus * variance * resistance_multiplier(
                        resistance,
                        actor.snapshot.level,
                        ruleset=self.ruleset,
                    )
                    * (1 - target.current_derived.magical_reduction)
                )
                if amount > 0:
                    breakdown[damage_type] = breakdown.get(damage_type, 0) + amount
        for effect in magic_effects:
            spell = actor.snapshot.skills.spells.get(definition.ability_id) if actor.snapshot.skills else None
            spell_level = spell.level if spell else 1
            base = spell_base_power(definition, actor, spell_level)
            school_multiplier = spell_multiplier_for(
                definition, actor, effect.damage_type
            )
            resistance = target.current_derived.resistances.get(effect.damage_type, 0.0) if target.current_derived else 0.0
            resistance += self.modifier(target, f"resistance_{effect.damage_type}")
            if effect.damage_type == "fire" and self.has(target, "wet"):
                resistance += 30
            if effect.damage_type == "lightning" and self.has(target, "wet"):
                resistance -= 30
            magical_reduction = target.current_derived.magical_reduction if target.current_derived else 0.0
            effect_multiplier = effect.value
            mastery_floor = effect.params.get("mastery_multiplier_floor")
            mastery_growth = effect.params.get("mastery_multiplier_growth")
            mastery_cap = effect.params.get("mastery_multiplier_cap")
            if (
                isinstance(mastery_floor, (int, float))
                and isinstance(mastery_growth, (int, float))
                and isinstance(mastery_cap, (int, float))
            ):
                effect_multiplier = min(
                    float(mastery_cap),
                    max(
                        float(mastery_floor),
                        float(mastery_floor)
                        + float(mastery_growth) * max(0, spell_level - 1),
                    ),
                )
            amount = spell_damage_amount(
                base_power=base,
                effect_multiplier=effect_multiplier,
                spell_multiplier=school_multiplier,
                variance=variance,
                resistance=resistance,
                attacker_level=actor.snapshot.level,
                magical_reduction=magical_reduction,
                ruleset=self.ruleset,
            )
            breakdown[effect.damage_type] = breakdown.get(effect.damage_type, 0) + amount
        critical = bool(physical_effects) and rng.random() < (actor.current_derived.critical_rate if actor.current_derived else 0.05)
        if critical:
            critical_damage = actor.current_derived.critical_damage if actor.current_derived else 1.5
            breakdown = {k: max(1, round(v * critical_damage)) for k, v in breakdown.items()}
        block = (target.snapshot.equipment.block_rate if target.snapshot.equipment else 0.0) + self.modifier(target, "block")
        guarded = False
        guard_multiplier = 1.0
        if physical_effects:
            # Both an explicit guard action and passive equipment block apply
            # to weapon damage, preserving the existing shield identity.
            guarded = target.guarding or rng.random() < min(0.75, block)
            guard_multiplier = self.ruleset.damage.physical_guard_multiplier
        elif magic_effects and target.guarding:
            # Guarding against a spell is weaker, but no longer a wasted turn.
            # Passive shield block intentionally remains physical-only.
            guarded = True
            guard_multiplier = self.ruleset.damage.active_magic_guard_multiplier
        if guarded:
            breakdown = {
                kind: max(1, round(value * guard_multiplier))
                for kind, value in breakdown.items()
            }
        knockback = sum(int(e.params.get("bonus_knockback", 0)) for e in physical_effects)
        return sum(breakdown.values()), critical, guarded, knockback, definition.ability_id, breakdown

    def apply_secondary(
        self,
        state,
        actor,
        target,
        definition,
        damage_result,
        rng,
        *,
        apply_damage=None,
    ) -> None:
        conflicts = {
            "barbarian_rage": {"dark_lotus"}, "dark_lotus": {"barbarian_rage"},
            "split_arrow": {"thorn_arrow"}, "thorn_arrow": {"split_arrow"},
            "martial_awakening": {"scythe_awakening"}, "scythe_awakening": {"martial_awakening"},
            "armor_spell": {"holy_shield", "mind_barrier", "shield_wall", "protective_prayer"},
            "holy_shield": {"armor_spell", "mind_barrier", "shield_wall"},
            "mind_barrier": {"armor_spell", "holy_shield", "shield_wall"},
            "shield_wall": {"armor_spell", "holy_shield", "mind_barrier"},
            "protective_prayer": {"armor_spell"},
            "holy_justice": {"beast_claw"}, "beast_claw": {"holy_justice"},
            "hero": {"oak_blessing"}, "oak_blessing": {"hero", "elm_blessing"},
            "elm_blessing": {"oak_blessing", "sage_blessing", "free_thought"},
            "sage_blessing": {"elm_blessing", "free_thought"},
            "free_thought": {"sage_blessing", "elm_blessing"},
        }
        for status_id in conflicts.get(definition.ability_id, set()):
            self.remove_status(state, actor, status_id)
        dealt = damage_result[0] if damage_result else 0
        hit = dealt > 0 or not any(e.effect_type in {"physical_damage", "magic_damage"} for e in definition.effects)
        if not hit:
            return
        for original_effect in definition.effects:
            effect = self._scaled_beneficial_effect(
                definition, actor, original_effect
            )
            effect = self._scaled_spell_effect(definition, actor, effect)
            effect = replace(
                effect,
                params={
                    **effect.params,
                    "source_ability_id": definition.ability_id,
                    **(
                        {"source_skill_id": definition.unlock_skill_id}
                        if definition.unlock_skill_id else {}
                    ),
                },
            )
            recipient = actor if effect.target in {"self", "ally", "ally_area"} else target
            if effect.effect_type in {"physical_damage", "magic_damage"}:
                if effect.params.get("self_damage_ratio"):
                    feedback = max(1, round(dealt * float(effect.params["self_damage_ratio"])))
                    actor.current_hp = max(0, actor.current_hp - feedback)
                    state.events.append(BattleEvent(state.tick, "recoil", actor.snapshot.user_pk, actor.snapshot.user_pk, value=feedback, remaining_hp=actor.current_hp))
                continue
            if effect.effect_type == "apply_status":
                self.apply_status(state, recipient, effect, actor.snapshot.user_pk, rng)
            elif effect.effect_type == "activate_stance":
                if actor.stance_id:
                    self.remove_status(state, actor, actor.stance_id)
                freeze_capacity = math.ceil(actor.max_mp * 0.25)
                freeze = max(0, min(actor.mana, freeze_capacity))
                actor.mana -= freeze
                actor.frozen_mana = freeze
                actor.frozen_mana_capacity = freeze_capacity
                actor.stance_id = effect.status_id
                stance_effect = type(effect)("apply_status", "self", effect.value, 9999, 1.0, effect.damage_type, effect.status_id, effect.radius, {**effect.params, "beneficial": True})
                self.apply_status(state, actor, stance_effect, actor.snapshot.user_pk, rng)
                state.events.append(BattleEvent(state.tick, "stance", actor.snapshot.user_pk, value=freeze_capacity, skill_id=definition.ability_id, mana=actor.mana, status_id=effect.status_id))
            elif effect.effect_type == "heal":
                if recipient.current_hp >= recipient.max_hp:
                    continue
                spell = actor.snapshot.skills.spells.get(definition.ability_id) if actor.snapshot.skills else None
                spell_level = spell.level if spell else 1
                base = healing_base_power(spell_level)
                spell_multiplier = spell_multiplier_for(definition, actor)
                power = actor.current_derived.healing_power if actor.current_derived else 1.0
                amount = min(recipient.max_hp - recipient.current_hp, max(1, round(base * spell_multiplier * effect.value * power * max(0.0, 1 + self.modifier(actor, "healing") + self.modifier(recipient, "healing")))))
                recipient.current_hp += amount
                state.events.append(BattleEvent(state.tick, "ability_heal", actor.snapshot.user_pk, recipient.snapshot.user_pk, value=amount, remaining_hp=recipient.current_hp, skill_id=definition.ability_id))
            elif effect.effect_type == "restore_resource":
                resource = str(effect.params.get("resource", "sp"))
                if resource == "sp":
                    before = actor.stamina
                    actor.stamina = min(
                        actor.max_sp, actor.stamina + round(effect.value)
                    )
                    restored = actor.stamina - before
                else:
                    before = actor.mana
                    actor.mana = min(
                        actor.max_mp - actor.frozen_mana_capacity,
                        actor.mana + round(effect.value),
                    )
                    restored = actor.mana - before
                if restored > 0:
                    state.events.append(BattleEvent(
                        state.tick,
                        "resource_restore",
                        actor.snapshot.user_pk,
                        actor.snapshot.user_pk,
                        value=restored,
                        skill_id=definition.ability_id,
                        stamina=actor.stamina,
                        mana=actor.mana,
                        status_id=resource,
                    ))
            elif effect.effect_type == "cleanse":
                mode = str(effect.params.get("mode", "one"))
                candidates = [s for s in recipient.statuses.values() if not s.beneficial]
                if mode in {"one", "one_negative"}: candidates = candidates[:1]
                elif mode == "poison": candidates = [s for s in candidates if s.status_id == "poison"]
                elif mode == "curse": candidates = [s for s in candidates if s.status_id == "curse"]
                elif mode == "disease": candidates = [s for s in candidates if s.status_id == "disease"]
                elif mode == "bleed_poison": candidates = [s for s in candidates if s.status_id in {"bleed", "poison"}]
                for status in candidates:
                    self.remove_status(state, recipient, status.status_id)
                if candidates:
                    state.events.append(BattleEvent(
                        state.tick,
                        "cleanse",
                        actor.snapshot.user_pk,
                        recipient.snapshot.user_pk,
                        value=len(candidates),
                        skill_id=definition.ability_id,
                        status_id=(
                            candidates[0].status_id
                            if len(candidates) == 1 else mode
                        ),
                    ))
            elif effect.effect_type == "dispel":
                buffs = sorted((s for s in target.statuses.values() if s.beneficial and s.dispellable), key=lambda s: s.status_id)
                if str(effect.params.get("mode", "one")) == "one": buffs = buffs[:1]
                for status in buffs: self.remove_status(state, target, status.status_id)
                removed_entities = []
                if str(effect.params.get("mode", "one")) == "all":
                    removed_entities = [
                        entity for entity in state.entities
                        if entity.owner_pk == target.snapshot.user_pk
                        and entity.dispellable
                    ]
                    state.entities[:] = [
                        entity for entity in state.entities
                        if entity not in removed_entities
                    ]
                    for entity in removed_entities:
                        for aura in entity.aura_effects:
                            self.remove_status(
                                state, target, aura.status_id
                            )
                removed_count = len(buffs) + len(removed_entities)
                if removed_count > 0:
                    state.events.append(BattleEvent(
                        state.tick,
                        "dispel",
                        actor.snapshot.user_pk,
                        target.snapshot.user_pk,
                        value=removed_count,
                        skill_id=definition.ability_id,
                        status_id=(
                            buffs[0].status_id
                            if len(buffs) == 1 and not removed_entities
                            else str(effect.params.get("mode", "one"))
                        ),
                    ))
            elif effect.effect_type == "summon":
                entity_id = f"{effect.params.get('entity_id', definition.ability_id)}:{actor.snapshot.user_pk}:{state.tick}"
                params = self._scaled_summon_params(actor, effect.params)
                params["beneficial"] = True
                aura_status = str(
                    effect.params.get(
                        "aura_status",
                        effect.params.get("entity_id", definition.ability_id),
                    )
                )
                aura = type(effect)("apply_status", "self", effect.value, effect.duration_ticks, 1.0, effect.damage_type, aura_status, effect.radius, params)
                attack_effects = []
                if params.get("periodic_damage"):
                    attack_effects.append(
                        type(effect)(
                            "magic_damage",
                            "enemy",
                            float(params["periodic_damage"]),
                            damage_type=effect.damage_type,
                        )
                    )
                if params.get("attack_status"):
                    attack_effects.append(
                        type(effect)(
                            "apply_status",
                            "enemy",
                            0.15,
                            8,
                            float(params.get("status_chance", 0.25)),
                            effect.damage_type,
                            str(params["attack_status"]),
                            params={
                                "source_ability_id": definition.ability_id,
                                **(
                                    {"source_skill_id": definition.unlock_skill_id}
                                    if definition.unlock_skill_id else {}
                                ),
                            },
                        )
                    )
                entity = BattleEntity(
                    entity_id=entity_id,
                    owner_pk=actor.snapshot.user_pk,
                    position=actor.position,
                    remaining_ticks=effect.duration_ticks,
                    aura_radius=effect.radius,
                    aura_effects=(aura,),
                    attack_effects=tuple(attack_effects),
                    attack_interval=max(
                        1, int(params.get("attack_interval", 5))
                    ) if attack_effects else 0,
                    spawned_tick=state.tick,
                    source_ability_id=definition.ability_id,
                )
                state.entities.append(entity)
                entity.aura_effects = (aura,)
                self.apply_status(state, actor, aura, actor.snapshot.user_pk, rng)
                state.events.append(BattleEvent(state.tick, "summon", actor.snapshot.user_pk, skill_id=definition.ability_id, entity_id=entity_id))
            elif effect.effect_type == "create_zone":
                zone_id = f"{effect.params.get('zone_id', definition.ability_id)}:{actor.snapshot.user_pk}:{state.tick}"
                zone_effects = []
                if effect.params.get("periodic_damage"):
                    zone_effects.append(type(effect)("magic_damage", "enemy", float(effect.params["periodic_damage"]), 0, 1.0, effect.damage_type))
                if effect.params.get("status"):
                    zone_effects.append(type(effect)(
                        "apply_status",
                        "enemy",
                        0.1,
                        6,
                        0.7,
                        effect.damage_type,
                        str(effect.params["status"]),
                        params={
                            "source_ability_id": definition.ability_id,
                            **(
                                {"source_skill_id": definition.unlock_skill_id}
                                if definition.unlock_skill_id else {}
                            ),
                        },
                    ))
                state.zones.append(BattleZone(zone_id, actor.snapshot.user_pk, target.position, effect.radius, effect.duration_ticks, tuple(zone_effects)))
                state.events.append(BattleEvent(state.tick, "zone_create", actor.snapshot.user_pk, target.snapshot.user_pk, skill_id=definition.ability_id, zone_id=zone_id, position=target.position))
            elif effect.effect_type == "teleport":
                mode = str(effect.params.get("mode", "blink"))
                distance = max(1, round(abs(effect.value or 100)))
                previous_position = actor.position
                if mode in {"random_half", "random_long"}:
                    low = max(0, actor.position - distance)
                    high = min(1000, actor.position + distance)
                    actor.position = rng.randint(low, high)
                    if actor.position == previous_position and low < high:
                        actor.position = low if low != previous_position else high
                elif mode in {"ideal", "ideal_distance"}:
                    ideal = actor.snapshot.equipment.attack_range if actor.snapshot.equipment else 100
                    actor.position = max(0, min(1000, target.position - ideal if actor is state.attacker else target.position + ideal))
                else:
                    actor.position = max(
                        0,
                        min(
                            1000,
                            actor.position + rng.randint(-distance, distance),
                        ),
                    )
                    if actor.position == previous_position:
                        actor.position = (
                            min(1000, previous_position + distance)
                            if previous_position < 1000
                            else max(0, previous_position - distance)
                        )
                displacement = abs(actor.position - previous_position)
                if displacement > 0:
                    state.events.append(BattleEvent(
                        state.tick,
                        "teleport",
                        actor.snapshot.user_pk,
                        actor.snapshot.user_pk,
                        value=displacement,
                        position=actor.position,
                        skill_id=definition.ability_id,
                    ))
            elif effect.effect_type == "drain_resource":
                amount = min(max(0, target.mana), round(effect.value))
                target.mana -= amount
                actor.mana = min(actor.max_mp - actor.frozen_mana_capacity, actor.mana + amount)
                if amount > 0:
                    state.events.append(BattleEvent(state.tick, "mana_drain", actor.snapshot.user_pk, target.snapshot.user_pk, value=amount, mana=actor.mana, skill_id=definition.ability_id, status_id="mp"))

        if dealt > 0:
            life_steal_ratio = max(
                (
                    float(effect.params.get("life_steal", 0.0))
                    for effect in definition.effects
                ),
                default=0.0,
            )
            if life_steal_ratio > 0:
                amount = min(
                    actor.max_hp - actor.current_hp,
                    max(
                        0,
                        round(
                            dealt
                            * life_steal_ratio
                            * max(
                                0.0,
                                1 + self.modifier(actor, "healing"),
                            )
                        ),
                    ),
                )
                if amount > 0:
                    actor.current_hp += amount
                    state.events.append(
                        BattleEvent(
                            state.tick,
                            "life_steal",
                            actor.snapshot.user_pk,
                            actor.snapshot.user_pk,
                            value=amount,
                            remaining_hp=actor.current_hp,
                            skill_id=definition.ability_id,
                        )
                    )
            if self.has(actor, "barbarian_rage") and rng.random() < 0.20:
                actor.stamina = min(actor.max_sp, actor.stamina + 5)
            if self.has(actor, "dark_lotus") and damage_result and damage_result[1] and rng.random() < 0.50:
                blind = type(definition.effects[0])(
                    "apply_status", "enemy", 0.0, 20, 1.0,
                    "physical", "blind",
                    params={"source_ability_id": "dark_lotus"},
                )
                self.apply_status(state, target, blind, actor.snapshot.user_pk, rng)
            if self.has(actor, "martial_awakening") and rng.random() < 0.25:
                stun = type(definition.effects[0])(
                    "apply_status", "enemy", 0.0, 5, 1.0,
                    "physical", "stun",
                    params={"source_ability_id": "martial_awakening"},
                )
                self.apply_status(state, target, stun, actor.snapshot.user_pk, rng)
            if self.has(actor, "scythe_awakening"):
                level = actor.skill_level("scythe")
                damage_types = ["hell"] + (["shadow"] if level >= 60 else [])
                for damage_type in damage_types:
                    extra = max(1, round(dealt * 0.20))
                    if apply_damage is not None:
                        apply_damage(
                            state,
                            actor,
                            target,
                            (
                                extra,
                                False,
                                False,
                                0,
                                "scythe_awakening",
                                {damage_type: extra},
                            ),
                            "followup",
                            False,
                        )
                    else:
                        target.current_hp = max(0, target.current_hp - extra)
                        actor.damage_dealt += extra
                        state.events.append(BattleEvent(state.tick, "followup", actor.snapshot.user_pk, target.snapshot.user_pk, value=extra, remaining_hp=target.current_hp, damage_type=damage_type, status_id="scythe_awakening"))
                if level >= 80 and rng.random() < 0.30:
                    slow = type(definition.effects[0])(
                        "apply_status", "enemy", 0.25, 15, 1.0,
                        "physical", "slow",
                        params={"source_ability_id": "scythe_awakening"},
                    )
                    self.apply_status(state, target, slow, actor.snapshot.user_pk, rng)
            if self.has(actor, "elemental_affinity") and definition.unlock_skill_id == "elemental_guidance":
                affinity_heal = round(
                    max(1, dealt // 20)
                    * max(0.0, 1 + self.modifier(actor, "healing"))
                )
                actor.current_hp = min(actor.max_hp, actor.current_hp + affinity_heal)
                actor.mana = min(actor.max_mp - actor.frozen_mana_capacity, actor.mana + max(1, dealt // 20))
            for status_id in ("poison_weapon", "thorn_arrow", "phantom_smoke"):
                if self.has(actor, status_id):
                    mapped = {"poison_weapon": "poison", "thorn_arrow": "bleed", "phantom_smoke": "haze"}[status_id]
                    source = actor.statuses[status_id]
                    chance = float(source.params.get("status_chance", 0.7))
                    fake = type(definition.effects[0])(
                        "apply_status", "enemy", max(1.0, dealt * 0.08),
                        25, chance, "physical", mapped,
                        params={
                            "source_ability_id": str(
                                source.params.get(
                                    "source_ability_id", status_id
                                )
                            ) or status_id,
                        },
                    )
                    self.apply_status(state, target, fake, actor.snapshot.user_pk, rng)
