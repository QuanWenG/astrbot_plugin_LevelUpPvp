"""JSON codec for persistent random-Nefia adventure snapshots.

The dungeon domain deliberately has no database dependency.  This codec is the
single compatibility boundary between its immutable dataclasses and SQLite.
It stores enough combat information for replay/handler summaries while a
restored adventure continues from its explicit ``FighterContinuationState``.
"""

from __future__ import annotations

import json

try:
    from ..models.ability import ActionEffect, ActiveAbilityDefinition, UserSpell
    from ..models.attributes import (
        AdvancedAttributes,
        DerivedStats,
        PrimaryAttributes,
    )
    from ..models.combat import (
        BattleEvent,
        FighterContinuationState,
        FighterSnapshot,
        SimulationResult,
    )
    from ..models.equipment import EquipmentBuild, EquipmentItem, EquipmentProc
    from ..models.skill import SkillBuild, UserSkill
    from ..models.dungeon import (
        DungeonAdventure,
        DungeonAffix,
        DungeonDiscovery,
        DungeonEncounterRecord,
        DungeonEnvironment,
        DungeonFloor,
        DungeonRewardIntent,
        DungeonRiskChoice,
        DungeonRouteOption,
    )
except ImportError:
    from models.ability import ActionEffect, ActiveAbilityDefinition, UserSpell
    from models.attributes import AdvancedAttributes, DerivedStats, PrimaryAttributes
    from models.combat import (
        BattleEvent,
        FighterContinuationState,
        FighterSnapshot,
        SimulationResult,
    )
    from models.equipment import EquipmentBuild, EquipmentItem, EquipmentProc
    from models.skill import SkillBuild, UserSkill
    from models.dungeon import (
        DungeonAdventure,
        DungeonAffix,
        DungeonDiscovery,
        DungeonEncounterRecord,
        DungeonEnvironment,
        DungeonFloor,
        DungeonRewardIntent,
        DungeonRiskChoice,
        DungeonRouteOption,
    )


SNAPSHOT_SCHEMA_VERSION = 1


def dump_adventure(adventure: DungeonAdventure) -> str:
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "adventure": {
            "adventure_id": adventure.adventure_id,
            "settlement_key": adventure.settlement_key,
            "owner_key": adventure.owner_key,
            "group_key": adventure.group_key,
            "dungeon_id": adventure.dungeon_id,
            "cycle_key": adventure.cycle_key,
            "seed": adventure.seed,
            "player_level": adventure.player_level,
            "difficulty": adventure.difficulty,
            "floors": [_floor_to_dict(item) for item in adventure.floors],
            "phase": adventure.phase,
            "floor_index": adventure.floor_index,
            "selected_route_id": adventure.selected_route_id,
            "selected_risk_id": adventure.selected_risk_id,
            "continuation_state": (
                adventure.continuation_state.to_dict()
                if adventure.continuation_state else None
            ),
            "encounters": [
                {
                    "floor_index": item.floor_index,
                    "route_id": item.route_id,
                    "risk_id": item.risk_id,
                    "monster_template_id": item.monster_template_id,
                    "monster_rank": item.monster_rank,
                    "environment_id": item.environment_id,
                    "affix_ids": list(item.affix_ids),
                    "won": item.won,
                    "simulation": (
                        item.simulation.to_dict() if item.simulation else None
                    ),
                    "narrative": item.narrative,
                }
                for item in adventure.encounters
            ],
            "reward_intents": [item.to_dict() for item in adventure.reward_intents],
            "equipment_misses": adventure.equipment_misses,
            "spellbook_misses": adventure.spellbook_misses,
            "strategy": adventure.strategy,
            "version": adventure.version,
        },
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def load_adventure(payload: str) -> DungeonAdventure:
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError("奈菲亚存档不是JSON对象")
    if int(document.get("schema_version", 0)) != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("不支持的奈菲亚存档版本")
    raw = document.get("adventure")
    if not isinstance(raw, dict):
        raise ValueError("奈菲亚存档缺少adventure")
    adventure = DungeonAdventure(
        adventure_id=str(raw["adventure_id"]),
        settlement_key=str(raw["settlement_key"]),
        owner_key=str(raw["owner_key"]),
        group_key=str(raw["group_key"]),
        dungeon_id=str(raw["dungeon_id"]),
        cycle_key=str(raw["cycle_key"]),
        seed=int(raw["seed"]),
        player_level=int(raw["player_level"]),
        difficulty=int(raw["difficulty"]),
        floors=tuple(_floor_from_dict(item) for item in raw.get("floors", ())),
        phase=str(raw.get("phase", "route_choice")),  # type: ignore[arg-type]
        floor_index=int(raw.get("floor_index", 0)),
        selected_route_id=_optional_text(raw.get("selected_route_id")),
        selected_risk_id=_optional_text(raw.get("selected_risk_id")),
        continuation_state=_continuation_from_dict(raw.get("continuation_state")),
        encounters=tuple(
            _encounter_from_dict(item) for item in raw.get("encounters", ())
        ),
        reward_intents=tuple(
            _reward_intent_from_dict(item)
            for item in raw.get("reward_intents", ())
        ),
        equipment_misses=max(0, int(raw.get("equipment_misses", 0))),
        spellbook_misses=max(0, int(raw.get("spellbook_misses", 0))),
        strategy=str(raw.get("strategy", "")),
        version=int(raw.get("version", 0)),
    )
    if not adventure.floors:
        raise ValueError("奈菲亚存档没有楼层")
    return adventure


def _floor_to_dict(floor: DungeonFloor) -> dict:
    return {
        "floor_index": floor.floor_index,
        "routes": [_route_to_dict(route) for route in floor.routes],
    }


def _floor_from_dict(raw: dict) -> DungeonFloor:
    routes = tuple(_route_from_dict(item) for item in raw.get("routes", ()))
    if len(routes) != 2:
        raise ValueError("奈菲亚楼层必须恰好有两条路线")
    return DungeonFloor(int(raw["floor_index"]), routes)  # type: ignore[arg-type]


def _route_to_dict(route: DungeonRouteOption) -> dict:
    return {
        "option_id": route.option_id,
        "name": route.name,
        "description": route.description,
        "node_kind": route.node_kind,
        "monster_template_id": route.monster_template_id,
        "monster_level": route.monster_level,
        "monster_rank": route.monster_rank,
        "environment": {
            "environment_id": route.environment.environment_id,
            "name": route.environment.name,
            "description": route.environment.description,
            "combat_environment_id": route.environment.combat_environment_id,
            "threat_multiplier": route.environment.threat_multiplier,
            "reward_multiplier": route.environment.reward_multiplier,
        },
        "affixes": [
            {
                "affix_id": item.affix_id,
                "name": item.name,
                "description": item.description,
                "level_delta": item.level_delta,
                "aggression_delta": item.aggression_delta,
                "guard_delta": item.guard_delta,
                "reward_multiplier": item.reward_multiplier,
            }
            for item in route.affixes
        ],
        "risk_choices": [
            {
                "risk_id": item.risk_id,
                "name": item.name,
                "description": item.description,
                "monster_level_delta": item.monster_level_delta,
                "reward_multiplier": item.reward_multiplier,
                "entry_hp_cost_ratio": item.entry_hp_cost_ratio,
                "entry_mp_cost_ratio": item.entry_mp_cost_ratio,
            }
            for item in route.risk_choices
        ],
        "terrain_id": route.terrain_id,
        "terrain_name": route.terrain_name,
        "discovery": (
            {
                "discovery_id": route.discovery.discovery_id,
                "discovery_type": route.discovery.discovery_type,
                "name": route.discovery.name,
                "description": route.discovery.description,
                "reward_multiplier": route.discovery.reward_multiplier,
                "skill_id": route.discovery.skill_id,
                "skill_threshold": route.discovery.skill_threshold,
                "unlock_any": list(route.discovery.unlock_any),
            }
            if route.discovery else None
        ),
        "base_reward_multiplier": route.base_reward_multiplier,
    }


def _route_from_dict(raw: dict) -> DungeonRouteOption:
    environment = raw["environment"]
    risks = tuple(DungeonRiskChoice(**item) for item in raw.get("risk_choices", ()))
    if len(risks) != 2:
        raise ValueError("奈菲亚路线必须恰好有两个风险选项")
    discovery_raw = raw.get("discovery")
    discovery = None
    if discovery_raw:
        discovery = DungeonDiscovery(
            discovery_id=str(discovery_raw["discovery_id"]),
            discovery_type=str(discovery_raw["discovery_type"]),  # type: ignore[arg-type]
            name=str(discovery_raw["name"]),
            description=str(discovery_raw["description"]),
            reward_multiplier=float(discovery_raw.get("reward_multiplier", 1.0)),
            skill_id=_optional_text(discovery_raw.get("skill_id")),
            skill_threshold=int(discovery_raw.get("skill_threshold", 0)),
            unlock_any=tuple(str(item) for item in discovery_raw.get("unlock_any", ())),
        )
    return DungeonRouteOption(
        option_id=str(raw["option_id"]),
        name=str(raw["name"]),
        description=str(raw["description"]),
        node_kind=str(raw["node_kind"]),  # type: ignore[arg-type]
        monster_template_id=str(raw["monster_template_id"]),
        monster_level=int(raw["monster_level"]),
        monster_rank=str(raw["monster_rank"]),  # type: ignore[arg-type]
        environment=DungeonEnvironment(
            environment_id=str(environment["environment_id"]),
            name=str(environment["name"]),
            description=str(environment["description"]),
            combat_environment_id=str(environment["combat_environment_id"]),
            threat_multiplier=float(environment.get("threat_multiplier", 1.0)),
            reward_multiplier=float(environment.get("reward_multiplier", 1.0)),
        ),
        affixes=tuple(DungeonAffix(**item) for item in raw.get("affixes", ())),
        risk_choices=risks,  # type: ignore[arg-type]
        terrain_id=str(raw.get("terrain_id", "cave")),  # type: ignore[arg-type]
        terrain_name=str(raw.get("terrain_name", "洞窟")),
        discovery=discovery,
        base_reward_multiplier=float(raw.get("base_reward_multiplier", 1.0)),
    )


def _reward_intent_from_dict(raw: dict) -> DungeonRewardIntent:
    return DungeonRewardIntent(
        source_key=str(raw["source_key"]),
        reward_type=str(raw["reward_type"]),  # type: ignore[arg-type]
        quantity=int(raw["quantity"]),
        random_seed=int(raw["random_seed"]),
        item_level_min=int(raw.get("item_level_min", 0)),
        item_level_max=int(raw.get("item_level_max", 0)),
        catalog_id_min=int(raw.get("catalog_id_min", 0)),
        catalog_id_max=int(raw.get("catalog_id_max", 0)),
        spell_pool=tuple(str(item) for item in raw.get("spell_pool", ())),
        quality_bonus=float(raw.get("quality_bonus", 0.0)),
        metadata=dict(raw.get("metadata", {})),
    )


def _encounter_from_dict(raw: dict) -> DungeonEncounterRecord:
    simulation_raw = raw.get("simulation")
    return DungeonEncounterRecord(
        floor_index=int(raw["floor_index"]),
        route_id=str(raw["route_id"]),
        risk_id=str(raw["risk_id"]),
        monster_template_id=str(raw["monster_template_id"]),
        monster_rank=str(raw["monster_rank"]),
        environment_id=str(raw["environment_id"]),
        affix_ids=tuple(str(item) for item in raw.get("affix_ids", ())),
        won=bool(raw["won"]),
        simulation=(
            _simulation_from_dict(simulation_raw) if simulation_raw else None
        ),
        narrative=str(raw.get("narrative", "")),
    )


def _snapshot_from_dict(raw: dict) -> FighterSnapshot:
    return FighterSnapshot(
        user_pk=int(raw["user_pk"]),
        name=str(raw.get("name", "")),
        level=int(raw.get("level", 1)),
        hp=int(raw.get("hp", 1)),
        atk=int(raw.get("atk", 1)),
        defense=int(raw.get("defense", 1)),
        speed=int(raw.get("speed", 1)),
        luck=int(raw.get("luck", 1)),
        strategy=str(raw.get("strategy", "")),
        equipment_modifiers={
            str(key): int(value)
            for key, value in dict(raw.get("equipment_modifiers", {})).items()
        },
        skill_ids=tuple(str(item) for item in raw.get("skill_ids", ("basic_attack",))),
        equipment=_equipment_build_from_dict(raw.get("equipment")),
        skills=_skill_build_from_dict(raw.get("skills")),
        attributes=_dataclass_from_dict(PrimaryAttributes, raw.get("attributes")),
        advanced_attributes=_dataclass_from_dict(
            AdvancedAttributes,
            raw.get("advanced_attributes"),
        ),
        derived=_derived_from_dict(raw.get("derived")),
        combatant_kind=str(raw.get("combatant_kind", "player")),
        source_template_id=str(raw.get("source_template_id", "")),
        rank=str(raw.get("rank", "normal")),
    )


def _dataclass_from_dict(model, raw):
    if not isinstance(raw, dict):
        return None
    fields = model.__dataclass_fields__
    return model(**{key: value for key, value in raw.items() if key in fields})


def _equipment_item_from_dict(raw: dict) -> EquipmentItem:
    payload = {
        key: value
        for key, value in raw.items()
        if key in EquipmentItem.__dataclass_fields__
    }
    for key in ("inherent_affixes", "random_affixes", "fusion_affixes"):
        payload[key] = tuple(dict(item) for item in raw.get(key, ()))
    payload["source_effects"] = tuple(
        str(item) for item in raw.get("source_effects", ())
    )
    return EquipmentItem(**payload)


def _equipment_proc_from_dict(raw: dict) -> EquipmentProc:
    payload = {
        key: value
        for key, value in raw.items()
        if key in EquipmentProc.__dataclass_fields__
    }
    payload["params"] = dict(raw.get("params", {}))
    return EquipmentProc(**payload)


def _equipment_build_from_dict(raw) -> EquipmentBuild | None:
    if not isinstance(raw, dict):
        return None
    payload = {
        key: value
        for key, value in raw.items()
        if key in EquipmentBuild.__dataclass_fields__
    }
    payload["items"] = tuple(
        _equipment_item_from_dict(item) for item in raw.get("items", ())
    )
    payload["slots"] = {
        str(key): int(value) for key, value in dict(raw.get("slots", {})).items()
    }
    for key in (
        "stat_modifiers",
        "skill_modifiers",
        "advanced_stat_modifiers",
    ):
        payload[key] = {
            str(name): int(value)
            for name, value in dict(raw.get(key, {})).items()
        }
    for key in ("reserved_effects", "combat_effects"):
        payload[key] = {
            str(name): float(value)
            for name, value in dict(raw.get(key, {})).items()
        }
    payload["item_weights"] = {
        int(key): float(value)
        for key, value in dict(raw.get("item_weights", {})).items()
    }
    payload["equipment_procs"] = tuple(
        _equipment_proc_from_dict(item)
        for item in raw.get("equipment_procs", ())
    )
    return EquipmentBuild(**payload)


def _action_effect_from_dict(raw: dict) -> ActionEffect:
    payload = {
        key: value
        for key, value in raw.items()
        if key in ActionEffect.__dataclass_fields__
    }
    payload["params"] = dict(raw.get("params", {}))
    return ActionEffect(**payload)


def _ability_definition_from_dict(raw: dict) -> ActiveAbilityDefinition:
    payload = {
        key: value
        for key, value in raw.items()
        if key in ActiveAbilityDefinition.__dataclass_fields__
    }
    payload["compatible_weapon_types"] = tuple(
        str(item) for item in raw.get("compatible_weapon_types", ())
    )
    payload["compatible_weapon_modes"] = tuple(
        str(item) for item in raw.get("compatible_weapon_modes", ())
    )
    payload["effects"] = tuple(
        _action_effect_from_dict(item) for item in raw.get("effects", ())
    )
    payload["ai_tags"] = tuple(str(item) for item in raw.get("ai_tags", ()))
    return ActiveAbilityDefinition(**payload)


def _skill_build_from_dict(raw) -> SkillBuild | None:
    if not isinstance(raw, dict):
        return None
    return SkillBuild(
        skills={
            str(key): UserSkill(**value)
            for key, value in dict(raw.get("skills", {})).items()
        },
        effective_levels={
            str(key): int(value)
            for key, value in dict(raw.get("effective_levels", {})).items()
        },
        active_skill_ids=tuple(
            str(item) for item in raw.get("active_skill_ids", ())
        ),
        active_definitions={
            str(key): _ability_definition_from_dict(value)
            for key, value in dict(raw.get("active_definitions", {})).items()
        },
        level_caps={
            str(key): int(value)
            for key, value in dict(raw.get("level_caps", {})).items()
        },
        spells={
            str(key): UserSpell(**value)
            for key, value in dict(raw.get("spells", {})).items()
        },
    )


def _derived_from_dict(raw) -> DerivedStats | None:
    derived = _dataclass_from_dict(DerivedStats, raw)
    if derived is None:
        return None
    return derived


def _continuation_from_dict(raw) -> FighterContinuationState | None:
    if raw is None:
        return None
    return FighterContinuationState(
        hp_ratio=float(raw.get("hp_ratio", 1.0)),
        mana_ratio=float(raw.get("mana_ratio", 1.0)),
        stamina_ratio=float(raw.get("stamina_ratio", 1.0)),
        hp_regen_buffer=float(raw.get("hp_regen_buffer", 0.0)),
        mp_regen_buffer=float(raw.get("mp_regen_buffer", 0.0)),
        sp_regen_buffer=float(raw.get("sp_regen_buffer", 0.0)),
        recovery_turn_phase=int(raw.get("recovery_turn_phase", 0)),
        statuses=tuple(dict(item) for item in raw.get("statuses", ())),
        skill_cooldowns={
            str(key): int(value)
            for key, value in dict(raw.get("skill_cooldowns", {})).items()
        },
        attack_cooldown=int(raw.get("attack_cooldown", 0)),
        recovery_ticks=int(raw.get("recovery_ticks", 0)),
        hitstun_ticks=int(raw.get("hitstun_ticks", 0)),
        counter_cooldown=int(raw.get("counter_cooldown", 0)),
        hard_control_immunity_ticks=max(
            0, int(raw.get("hard_control_immunity_ticks", 0))
        ),
        stance_id=_optional_text(raw.get("stance_id")),
        frozen_mana_ratio=float(raw.get("frozen_mana_ratio", 0.0)),
        frozen_mana_capacity_ratio=float(raw.get("frozen_mana_capacity_ratio", 0.0)),
        lethal_survival_used=bool(raw.get("lethal_survival_used", False)),
        defeated=bool(raw.get("defeated", False)),
        updated_at_ts=int(raw.get("updated_at_ts", 0)),
        version=int(raw.get("version", 0)),
    )


def _simulation_from_dict(raw: dict) -> SimulationResult:
    return SimulationResult(
        attacker=_snapshot_from_dict(raw["attacker"]),
        defender=_snapshot_from_dict(raw["defender"]),
        winner_pk=int(raw["winner_pk"]),
        loser_pk=int(raw["loser_pk"]),
        duration_ticks=int(raw["duration_ticks"]),
        finish_reason=str(raw["finish_reason"]),
        attacker_remaining_hp=int(raw["attacker_remaining_hp"]),
        defender_remaining_hp=int(raw["defender_remaining_hp"]),
        attacker_damage_dealt=int(raw["attacker_damage_dealt"]),
        defender_damage_dealt=int(raw["defender_damage_dealt"]),
        events=tuple(BattleEvent(**item) for item in raw.get("events", ())),
        random_seed=int(raw["random_seed"]),
        engine_version=str(raw.get("engine_version", "sideview-v9")),
        attacker_remaining_stamina=int(raw.get("attacker_remaining_stamina", 0)),
        defender_remaining_stamina=int(raw.get("defender_remaining_stamina", 0)),
        attacker_remaining_mana=int(raw.get("attacker_remaining_mana", 0)),
        defender_remaining_mana=int(raw.get("defender_remaining_mana", 0)),
        attacker_frozen_mana=int(raw.get("attacker_frozen_mana", 0)),
        defender_frozen_mana=int(raw.get("defender_frozen_mana", 0)),
        attacker_final_statuses=tuple(raw.get("attacker_final_statuses", ())),
        defender_final_statuses=tuple(raw.get("defender_final_statuses", ())),
        final_entities=tuple(raw.get("final_entities", ())),
        final_zones=tuple(raw.get("final_zones", ())),
        attacker_final_state=_continuation_from_dict(raw.get("attacker_final_state")),
        defender_final_state=_continuation_from_dict(raw.get("defender_final_state")),
        ruleset_id=str(raw.get("ruleset_id", "sideview-v11")),
        environment_id=str(raw.get("environment_id", "calm")),
    )


def _optional_text(value) -> str | None:
    return None if value is None else str(value)
