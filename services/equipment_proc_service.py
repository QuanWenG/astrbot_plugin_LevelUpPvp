from __future__ import annotations

from dataclasses import replace

try:
    from ..models.ability import ActionEffect, ActiveAbilityDefinition, BattleZone
    from ..models.combat import BattleEvent
    from .ability_catalog import ACTIVE_ABILITY_DEFINITIONS
except ImportError:
    from models.ability import ActionEffect, ActiveAbilityDefinition, BattleZone
    from models.combat import BattleEvent
    from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS


EQUIPMENT_PROC_NAMES = {
    "elemental_scar": "元素伤痕",
    "time_stop": "时间停止",
    "holy_veil": "圣光帷幕",
    "healing_rain": "治愈之雨",
    "ragnarok": "终末",
    "hero": "英雄",
    "equipment_poison": "剧毒",
    "dimensional_hand": "异次元之手",
    "lulwy_possession": "露璐薇附体",
    "haste": "加速",
    "silence_fog": "沉默",
    "roaring_wave": "轰鸣波动",
}


def _status_definition(
    ability_id: str,
    name: str,
    status_id: str,
    duration: int,
    magnitude: float,
    *,
    target: str = "enemy",
    beneficial: bool = False,
) -> ActiveAbilityDefinition:
    return ActiveAbilityDefinition(
        ability_id=ability_id,
        name=name,
        ability_type="equipment",
        targeting="self" if target == "self" else "single",
        effects=(
            ActionEffect(
                "apply_status",
                target=target,
                value=magnitude,
                duration_ticks=duration,
                chance=1.0,
                status_id=status_id,
                params={"beneficial": beneficial},
            ),
        ),
    )


EQUIPMENT_ONLY_ABILITIES = {
    "time_stop": _status_definition(
        "time_stop", "时间停止", "stun", 20, 0.0
    ),
    "holy_veil": _status_definition(
        "holy_veil",
        "圣光帷幕",
        "status_resistance",
        40,
        0.35,
        target="self",
        beneficial=True,
    ),
    "lulwy_possession": _status_definition(
        "lulwy_possession",
        "露璐薇附体",
        "lulwy_possession",
        25,
        0.20,
        target="self",
        beneficial=True,
    ),
    "equipment_poison": _status_definition(
        "equipment_poison", "剧毒", "poison", 25, 1.0
    ),
}


class EquipmentProcResolver:
    """Resolve data-driven equipment on-hit effects without owning combat flow."""

    def __init__(self, ability_runtime):
        self.ability_runtime = ability_runtime

    def resolve(
        self,
        state,
        actor,
        target,
        damage_result,
        rng,
        apply_damage,
    ) -> None:
        if not damage_result or damage_result[0] <= 0:
            return
        equipment = actor.snapshot.equipment
        if not equipment:
            return

        dealt = int(damage_result[0])
        effects = equipment.combat_effects
        self._restore_resources(state, actor, dealt, effects)
        if self._try_execute(state, actor, target, effects, rng):
            return

        for proc in equipment.equipment_procs:
            if proc.proc_type != "trigger_ability" or proc.chance <= 0:
                continue
            if rng.random() >= min(1.0, proc.chance):
                continue
            state.events.append(
                BattleEvent(
                    state.tick,
                    "equipment_proc",
                    actor.snapshot.user_pk,
                    target.snapshot.user_pk,
                    skill_id=proc.ability_id,
                )
            )
            if proc.ability_id == "dimensional_hand":
                self._pull_target(state, actor, target)
                continue
            if proc.ability_id == "ragnarok":
                self._ragnarok(state, actor, target, dealt)
                continue

            definition = (
                EQUIPMENT_ONLY_ABILITIES.get(proc.ability_id)
                or ACTIVE_ABILITY_DEFINITIONS.get(proc.ability_id)
            )
            if not definition:
                continue
            if proc.ability_id == "equipment_poison":
                poison = replace(
                    definition.effects[0],
                    value=max(1.0, dealt * 0.08),
                )
                definition = replace(definition, effects=(poison,))
            proc_damage = self.ability_runtime.damage_result(
                actor, target, definition, rng
            )
            if proc_damage[0] > 0:
                apply_damage(
                    state,
                    actor,
                    target,
                    proc_damage,
                    "equipment_proc_damage",
                    False,
                )
            self.ability_runtime.apply_secondary(
                state, actor, target, definition, proc_damage, rng
            )

    @staticmethod
    def _restore_resources(state, actor, damage: int, effects: dict) -> None:
        stamina_ratio = max(0.0, float(effects.get("stamina_steal", 0)))
        if stamina_ratio and actor.stamina < actor.max_sp:
            amount = min(
                10,
                actor.max_sp - actor.stamina,
                max(1, round(damage * stamina_ratio)),
            )
            actor.stamina += amount
            state.events.append(
                BattleEvent(
                    state.tick,
                    "stamina_steal",
                    actor.snapshot.user_pk,
                    actor.snapshot.user_pk,
                    value=amount,
                    stamina=actor.stamina,
                )
            )

        mana_ratio = max(0.0, float(effects.get("mana_steal", 0)))
        available_mana = max(0, actor.max_mp - actor.frozen_mana_capacity)
        if mana_ratio and actor.mana < available_mana:
            amount = min(
                10,
                available_mana - actor.mana,
                max(1, round(damage * mana_ratio)),
            )
            actor.mana += amount
            state.events.append(
                BattleEvent(
                    state.tick,
                    "mana_steal",
                    actor.snapshot.user_pk,
                    actor.snapshot.user_pk,
                    value=amount,
                    mana=actor.mana,
                )
            )

    @staticmethod
    def _try_execute(state, actor, target, effects: dict, rng) -> bool:
        chance = max(0.0, min(1.0, float(effects.get("execute_chance", 0))))
        if (
            not chance
            or not target.alive
            or target.hp_ratio > 0.20
            or rng.random() >= chance
        ):
            return False
        damage = target.current_hp
        target.current_hp = 0
        actor.damage_dealt += damage
        state.events.append(
            BattleEvent(
                state.tick,
                "execute",
                actor.snapshot.user_pk,
                target.snapshot.user_pk,
                value=damage,
                remaining_hp=0,
                skill_id="beheading",
            )
        )
        return True

    @staticmethod
    def _pull_target(state, actor, target) -> None:
        attack_range = (
            actor.snapshot.equipment.attack_range
            if actor.snapshot.equipment else 100
        )
        direction = 1 if target.position >= actor.position else -1
        target.position = max(
            0, min(1000, actor.position + direction * attack_range)
        )
        state.events.append(
            BattleEvent(
                state.tick,
                "equipment_pull",
                actor.snapshot.user_pk,
                target.snapshot.user_pk,
                position=target.position,
                skill_id="dimensional_hand",
            )
        )

    @staticmethod
    def _ragnarok(state, actor, target, damage: int) -> None:
        burst = max(1, round(damage * 0.60))
        target.current_hp = max(0, target.current_hp - burst)
        actor.damage_dealt += burst
        state.events.append(
            BattleEvent(
                state.tick,
                "equipment_proc_damage",
                actor.snapshot.user_pk,
                target.snapshot.user_pk,
                value=burst,
                remaining_hp=target.current_hp,
                damage_type="fire",
                skill_id="ragnarok",
                damage_breakdown={"fire": burst},
            )
        )
        periodic = max(1, round(damage * 0.15))
        zone_id = f"ragnarok:{actor.snapshot.user_pk}:{state.tick}"
        state.zones.append(
            BattleZone(
                zone_id,
                actor.snapshot.user_pk,
                target.position,
                150,
                20,
                (
                    ActionEffect(
                        "magic_damage",
                        value=periodic,
                        damage_type="fire",
                    ),
                ),
                affects_owner=True,
            )
        )
        state.events.append(
            BattleEvent(
                state.tick,
                "zone_create",
                actor.snapshot.user_pk,
                target.snapshot.user_pk,
                skill_id="ragnarok",
                zone_id=zone_id,
                position=target.position,
            )
        )
