from __future__ import annotations

import math
from dataclasses import replace

try:
    from ..models.ability import BattleEntity, BattleZone, CombatStatus
    from ..models.combat import BattleEvent
    from .attribute_service import elemental_multiplier
    from .spell_rules import calculate_mana_cost
    from .runtime_stat_resolver import RuntimeStatResolver
except ImportError:
    from models.ability import BattleEntity, BattleZone, CombatStatus
    from models.combat import BattleEvent
    from services.attribute_service import elemental_multiplier
    from services.spell_rules import calculate_mana_cost
    from services.runtime_stat_resolver import RuntimeStatResolver


STACKING_STATUSES = {"haze", "burn", "poison", "bleed"}
HARD_CONTROL = {"stun", "paralysis"}
NEGATIVE_STATUSES = {
    "haze", "burn", "poison", "bleed", "blind", "confusion", "stun",
    "paralysis", "bind", "silence", "slow", "gravity", "wet", "disease",
    "curse", "defense_down", "accuracy_down", "healing_down", "nightmare",
    "body_slow", "mind_slow", "magic_scar", "element_scar", "death_shadow",
}
SPELL_MULTIPLIER_KEYS = {
    "magic": "arcane",
    "fire": "fire",
    "cold": "cold",
    "lightning": "lightning",
    "shadow": "shadow",
    "nature": "nature",
    "mind": "mind",
    "hell": "hell",
}
SCALABLE_BENEFICIAL_PARAMS = {
    "strength", "constitution", "dexterity", "perception", "magic",
    "willpower", "defense", "evasion", "physical_damage",
    "physical_reduction", "healing_bonus", "ranged_speed",
    "ranged_followup", "followup", "reading",
    "resistance_magic", "resistance_fire", "resistance_cold",
    "resistance_lightning", "resistance_shadow", "resistance_nature",
    "resistance_mind", "resistance_hell",
}


class AbilityRuntime:
    """Data-driven action effect executor shared by techniques and spells."""

    def __init__(self, stat_resolver: RuntimeStatResolver | None = None):
        self.stat_resolver = stat_resolver or RuntimeStatResolver()

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

    def tick(self, state, rng) -> None:
        for fighter in (state.attacker, state.defender):
            for status in list(fighter.statuses.values()):
                if status.status_id in {"burn", "poison", "bleed"} and state.tick % 5 == 0:
                    damage = max(1, round(status.magnitude * status.stacks))
                    fighter.current_hp = max(0, fighter.current_hp - damage)
                    source = state.attacker if state.attacker.snapshot.user_pk == status.source_pk else state.defender
                    if source is not fighter: source.damage_dealt += damage
                    state.events.append(BattleEvent(
                        state.tick, "status_damage", status.source_pk,
                        fighter.snapshot.user_pk, value=damage,
                        remaining_hp=fighter.current_hp,
                        damage_type={"burn": "fire", "poison": "nature", "bleed": "physical"}[status.status_id],
                        status_id=status.status_id,
                    ))
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
                if fighter.snapshot.user_pk == zone.owner_pk or abs(fighter.position - zone.position) > zone.radius:
                    continue
                for effect in zone.effects:
                    if effect.effect_type == "magic_damage" and state.tick % 5 == 0:
                        damage = max(1, round(effect.value))
                        fighter.current_hp = max(0, fighter.current_hp - damage)
                        source = state.attacker if state.attacker.snapshot.user_pk == zone.owner_pk else state.defender
                        if source is not fighter: source.damage_dealt += damage
                        state.events.append(BattleEvent(state.tick, "zone_damage", zone.owner_pk, fighter.snapshot.user_pk, value=damage, remaining_hp=fighter.current_hp, damage_type=effect.damage_type, zone_id=zone.zone_id))
                    elif effect.effect_type == "apply_status":
                        self.apply_status(state, fighter, effect, zone.owner_pk, rng)
            zone.remaining_ticks -= 1
            if zone.remaining_ticks <= 0:
                state.zones.remove(zone)
                state.events.append(BattleEvent(state.tick, "zone_expire", zone.owner_pk, zone_id=zone.zone_id))
        for entity in list(state.entities):
            owner = state.attacker if state.attacker.snapshot.user_pk == entity.owner_pk else state.defender
            for aura in entity.aura_effects:
                if abs(owner.position - entity.position) <= entity.aura_radius:
                    current = owner.statuses.get(aura.status_id)
                    if current:
                        current.remaining_ticks = max(current.remaining_ticks, entity.remaining_ticks)
                    else:
                        self.apply_status(state, owner, aura, entity.owner_pk, rng)
                else:
                    self.remove_status(state, owner, aura.status_id)
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
        if status_id in {"mental_random", "snare_random"}:
            choices = tuple(effect.params.get("choices", ()))
            status_id = choices[rng.randrange(len(choices))] if choices else status_id
        if status_id == "gravity" and "floating" in target.statuses:
            self.remove_status(state, target, "floating")
        beneficial = bool(effect.params.get("beneficial", False))
        chance = effect.chance
        if not beneficial:
            if any(bool(s.params.get(f"{status_id}_immunity", False)) for s in target.statuses.values()):
                return False
            immunity = target.snapshot.equipment.combat_effects.get("status_immunity", 0) if target.snapshot.equipment else 0
            if immunity and rng.random() < min(1.0, float(immunity)):
                return False
            resistance_key = "mind" if status_id in {"confusion", "paralysis", "haze", "blind"} else "nature"
            resistance = target.current_derived.resistances.get(resistance_key, 0.0) if target.current_derived else 0.0
            status_resistance = sum(s.magnitude for s in target.statuses.values() if s.status_id == "status_resistance")
            chance *= max(0.1, 1 - max(0.0, resistance) - status_resistance)
            if status_id == "bleed" and self.has(target, "tree_skin"):
                chance *= max(
                    0.0,
                    1 - float(target.statuses["tree_skin"].params.get("bleed_resistance", 0.0)),
                )
        if rng.random() >= chance:
            return False
        duration = max(1, effect.duration_ticks)
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
        state.events.append(BattleEvent(state.tick, "status_apply", source_pk, target.snapshot.user_pk, value=duration, status_id=status_id))
        return True

    def damage_result(self, actor, target, definition, rng):
        physical_effects = [e for e in definition.effects if e.effect_type == "physical_damage"]
        magic_effects = [e for e in definition.effects if e.effect_type == "magic_damage"]
        if not physical_effects and not magic_effects:
            return (0, False, False, 0, definition.ability_id, {})
        breakdown = {}
        variance = rng.uniform(0.90, 1.10)
        for effect in physical_effects:
            derived = actor.current_derived
            target_derived = target.current_derived
            target_defense = (target_derived.defense if target_derived else target.snapshot.stat("defense")) * max(0.1, 1 + self.modifier(target, "defense"))
            base = max(1.0, (derived.attack_power * 1.8 if derived else actor.snapshot.stat("atk") * 4) - target_defense * (0.75 if target_derived else 0.8))
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
            breakdown["physical"] = breakdown.get("physical", 0) + max(1, round(base * multiplier * variance * max(0.05, 1 - reduction)))
        if physical_effects and actor.current_derived and target.current_derived:
            for damage_type, bonus in actor.current_derived.elemental_damage.items():
                if bonus <= 0:
                    continue
                resistance = target.current_derived.resistances.get(damage_type, 0.0)
                amount = round(
                    bonus * variance * elemental_multiplier(resistance)
                    * (1 - target.current_derived.magical_reduction)
                )
                if amount > 0:
                    breakdown[damage_type] = breakdown.get(damage_type, 0) + amount
        for effect in magic_effects:
            spell = actor.snapshot.skills.spells.get(definition.ability_id) if actor.snapshot.skills else None
            spell_level = spell.level if spell else 1
            school_level = actor.skill_level(definition.unlock_skill_id)
            base = 8 + spell_level * 1.5 + actor.primary("magic") * 0.8 + actor.primary("perception") * 0.5 + school_level * 0.4
            multiplier_key = SPELL_MULTIPLIER_KEYS.get(effect.damage_type, effect.damage_type)
            school_multiplier = actor.current_derived.spell_multipliers.get(multiplier_key, 1.0) if actor.current_derived else 1.0
            resistance = target.current_derived.resistances.get(effect.damage_type, 0.0) if target.current_derived else 0.0
            resistance += self.modifier(target, f"resistance_{effect.damage_type}")
            if effect.damage_type == "fire" and self.has(target, "wet"):
                resistance += 0.30
            if effect.damage_type == "lightning" and self.has(target, "wet"):
                resistance -= 0.30
            magical_reduction = target.current_derived.magical_reduction if target.current_derived else 0.0
            amount = max(1, round(base * effect.value * school_multiplier * variance * elemental_multiplier(resistance) * (1 - magical_reduction)))
            breakdown[effect.damage_type] = breakdown.get(effect.damage_type, 0) + amount
        critical = bool(physical_effects) and rng.random() < (actor.current_derived.critical_rate if actor.current_derived else 0.05)
        if critical:
            critical_damage = actor.current_derived.critical_damage if actor.current_derived else 1.5
            breakdown = {k: max(1, round(v * critical_damage)) for k, v in breakdown.items()}
        block = (target.snapshot.equipment.block_rate if target.snapshot.equipment else 0.0) + self.modifier(target, "block")
        guarded = bool(physical_effects) and (target.guarding or rng.random() < min(0.75, block))
        if guarded:
            breakdown = {k: max(1, round(v * 0.55)) for k, v in breakdown.items()}
        knockback = sum(int(e.params.get("bonus_knockback", 0)) for e in physical_effects)
        return sum(breakdown.values()), critical, guarded, knockback, definition.ability_id, breakdown

    def apply_secondary(self, state, actor, target, definition, damage_result, rng) -> None:
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
                recovery = actor.skill_level("restoration")
                base = 10 + spell_level * 2 + actor.primary("willpower") * 1.2 + actor.primary("magic") * 0.5 + recovery * 0.5
                power = actor.current_derived.healing_power if actor.current_derived else 1.0
                amount = min(recipient.max_hp - recipient.current_hp, max(1, round(base * effect.value * power * max(0.0, 1 + self.modifier(actor, "healing") + self.modifier(recipient, "healing")))))
                recipient.current_hp += amount
                state.events.append(BattleEvent(state.tick, "ability_heal", actor.snapshot.user_pk, recipient.snapshot.user_pk, value=amount, remaining_hp=recipient.current_hp, skill_id=definition.ability_id))
            elif effect.effect_type == "restore_resource":
                resource = str(effect.params.get("resource", "sp"))
                if resource == "sp":
                    actor.stamina = min(actor.max_sp, actor.stamina + round(effect.value))
                else:
                    actor.mana = min(actor.max_mp - actor.frozen_mana_capacity, actor.mana + round(effect.value))
                state.events.append(BattleEvent(state.tick, "resource_restore", actor.snapshot.user_pk, value=round(effect.value), skill_id=definition.ability_id, stamina=actor.stamina, mana=actor.mana))
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
            elif effect.effect_type == "dispel":
                buffs = sorted((s for s in target.statuses.values() if s.beneficial and s.dispellable), key=lambda s: s.status_id)
                if str(effect.params.get("mode", "one")) == "one": buffs = buffs[:1]
                for status in buffs: self.remove_status(state, target, status.status_id)
                if str(effect.params.get("mode", "one")) == "all":
                    state.entities[:] = [e for e in state.entities if e.owner_pk != target.snapshot.user_pk or not e.dispellable]
            elif effect.effect_type == "summon":
                entity_id = f"{effect.params.get('entity_id', definition.ability_id)}:{actor.snapshot.user_pk}:{state.tick}"
                entity = BattleEntity(entity_id, actor.snapshot.user_pk, actor.position, effect.duration_ticks, effect.radius, ())
                state.entities.append(entity)
                params = self._scaled_summon_params(actor, effect.params)
                params["beneficial"] = True
                aura = type(effect)("apply_status", "self", effect.value, effect.duration_ticks, 1.0, effect.damage_type, str(effect.params.get("entity_id", definition.ability_id)), effect.radius, params)
                entity.aura_effects = (aura,)
                self.apply_status(state, actor, aura, actor.snapshot.user_pk, rng)
                state.events.append(BattleEvent(state.tick, "summon", actor.snapshot.user_pk, skill_id=definition.ability_id, entity_id=entity_id))
            elif effect.effect_type == "create_zone":
                zone_id = f"{effect.params.get('zone_id', definition.ability_id)}:{actor.snapshot.user_pk}:{state.tick}"
                zone_effects = []
                if effect.params.get("periodic_damage"):
                    zone_effects.append(type(effect)("magic_damage", "enemy", float(effect.params["periodic_damage"]) * 10, 0, 1.0, effect.damage_type))
                if effect.params.get("status"):
                    zone_effects.append(type(effect)("apply_status", "enemy", 0.1, 6, 0.7, effect.damage_type, str(effect.params["status"])))
                state.zones.append(BattleZone(zone_id, actor.snapshot.user_pk, target.position, effect.radius, effect.duration_ticks, tuple(zone_effects)))
                state.events.append(BattleEvent(state.tick, "zone_create", actor.snapshot.user_pk, target.snapshot.user_pk, skill_id=definition.ability_id, zone_id=zone_id, position=target.position))
            elif effect.effect_type == "teleport":
                mode = str(effect.params.get("mode", "blink"))
                if mode in {"random_half", "random_long"}:
                    actor.position = rng.randint(50, 470) if actor is state.attacker else rng.randint(530, 950)
                elif mode in {"ideal", "ideal_distance"}:
                    ideal = actor.snapshot.equipment.attack_range if actor.snapshot.equipment else 100
                    actor.position = max(0, min(1000, target.position - ideal if actor is state.attacker else target.position + ideal))
                else:
                    actor.position = max(0, min(1000, actor.position + rng.randint(-100, 100)))
                state.events.append(BattleEvent(state.tick, "teleport", actor.snapshot.user_pk, position=actor.position, skill_id=definition.ability_id))
            elif effect.effect_type == "drain_resource":
                amount = min(max(0, target.mana), round(effect.value))
                target.mana -= amount
                actor.mana = min(actor.max_mp - actor.frozen_mana_capacity, actor.mana + amount)
                state.events.append(BattleEvent(state.tick, "mana_drain", actor.snapshot.user_pk, target.snapshot.user_pk, value=amount, mana=actor.mana, skill_id=definition.ability_id))

        if dealt > 0:
            if self.has(actor, "barbarian_rage") and rng.random() < 0.20:
                actor.stamina = min(actor.max_sp, actor.stamina + 5)
            if self.has(actor, "dark_lotus") and damage_result and damage_result[1] and rng.random() < 0.50:
                blind = type(definition.effects[0])("apply_status", "enemy", 0.0, 20, 1.0, "physical", "blind")
                self.apply_status(state, target, blind, actor.snapshot.user_pk, rng)
            if self.has(actor, "martial_awakening") and rng.random() < 0.25:
                stun = type(definition.effects[0])("apply_status", "enemy", 0.0, 5, 1.0, "physical", "stun")
                self.apply_status(state, target, stun, actor.snapshot.user_pk, rng)
            if self.has(actor, "scythe_awakening"):
                level = actor.skill_level("scythe")
                damage_types = ["hell"] + (["shadow"] if level >= 60 else [])
                for damage_type in damage_types:
                    extra = max(1, round(dealt * 0.20))
                    target.current_hp = max(0, target.current_hp - extra)
                    actor.damage_dealt += extra
                    state.events.append(BattleEvent(state.tick, "followup", actor.snapshot.user_pk, target.snapshot.user_pk, value=extra, remaining_hp=target.current_hp, damage_type=damage_type, status_id="scythe_awakening"))
                if level >= 80 and rng.random() < 0.30:
                    slow = type(definition.effects[0])("apply_status", "enemy", 0.25, 15, 1.0, "physical", "slow")
                    self.apply_status(state, target, slow, actor.snapshot.user_pk, rng)
            if definition.ability_id == "hell_breath":
                amount = min(
                    actor.max_hp - actor.current_hp,
                    round(
                        dealt * 0.25
                        * max(0.0, 1 + self.modifier(actor, "healing"))
                    ),
                )
                actor.current_hp += amount
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
                    fake = type(definition.effects[0])("apply_status", "enemy", max(1.0, dealt * 0.08), 25, chance, "physical", mapped)
                    self.apply_status(state, target, fake, actor.snapshot.user_pk, rng)
