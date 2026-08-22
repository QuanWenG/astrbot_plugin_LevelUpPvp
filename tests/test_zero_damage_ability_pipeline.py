from dataclasses import replace
import random
import unittest
from unittest import mock

from models.ability import BattleEntity, CombatStatus, UserSpell
from models.attributes import DerivedStats, PrimaryAttributes
from models.combat import (
    AIProfile,
    BattleEvent,
    BattleState,
    FighterSnapshot,
    SimulationResult,
)
from models.skill import SkillBuild
from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS
from services.battle_report import BattleReportBuilder
from services.combat_engine import SideviewCombatEngine
from services.replay_service import _turning_points


PURE_EFFECT_REPRESENTATIVES = {
    "activate_stance": "barbarian_rage",
    "apply_status": "armor_spell",
    "cleanse": "minor_heal",
    "create_zone": "fire_wall",
    "dispel": "full_dispel",
    "drain_resource": "fanaticism",
    "heal": "minor_heal",
    "summon": "warrior_totem",
    "teleport": "blink",
}


def _snapshot(user_pk: int, ability_id: str | None = None) -> FighterSnapshot:
    definitions = {}
    active_ids = ()
    effective_levels = {}
    spells = {}
    if ability_id:
        definition = ACTIVE_ABILITY_DEFINITIONS[ability_id]
        definitions[ability_id] = definition
        active_ids = (ability_id,)
        if definition.unlock_skill_id:
            effective_levels[definition.unlock_skill_id] = 80
        if definition.ability_type == "spell":
            spells[ability_id] = UserSpell(ability_id, 20, 0, 100)
    skills = SkillBuild(
        {},
        effective_levels,
        active_ids,
        definitions,
        {},
        spells,
    )
    derived = DerivedStats(
        max_hp=1_200,
        max_mp=600,
        max_sp=300,
        attack_power=20,
        accuracy=1_000,
        defense=20,
        evasion=0,
        critical_rate=0.05,
        critical_damage=1.5,
        action_speed=100,
        carry_capacity=100,
        resistances={},
    )
    return FighterSnapshot(
        user_pk,
        f"测试者{user_pk}",
        50,
        20,
        20,
        20,
        20,
        20,
        "稳扎稳打",
        skills=skills,
        attributes=PrimaryAttributes(40, 40, 40, 80, 80, 40),
        derived=derived,
    )


class ZeroDamageAbilityPipelineTests(unittest.TestCase):
    """Exercise pure utility abilities through the combat engine action path."""

    def setUp(self):
        self.engine = SideviewCombatEngine()

    def _state_for(self, ability_id: str) -> BattleState:
        actor = self.engine._fighter_from_initial(
            _snapshot(1, ability_id),
            250,
            None,
        )
        target = self.engine._fighter_from_initial(
            _snapshot(2),
            350,
            None,
        )
        return BattleState(
            tick=1,
            attacker=actor,
            defender=target,
            events=[],
            random_seed=71,
            ruleset_id=self.engine.ruleset.ruleset_id,
            environment_id="calm",
        )

    def _resolve_once(self, state: BattleState, ability_id: str, seed: int = 7):
        actor = state.attacker
        target = state.defender
        self.engine._begin_attack(state, actor, "use_skill", ability_id)
        self.assertTrue(actor.attack_pending, ability_id)

        raw_result = self.engine._attack_damage(
            state,
            actor,
            target,
            "resolve_attack",
            random.Random(seed),
        )
        self.assertIsNotNone(raw_result, ability_id)
        self.assertEqual(raw_result[0], 0, ability_id)

        applied_result = self.engine._apply_damage(
            state,
            actor,
            target,
            raw_result,
        )
        # This is the regression contract: zero damage is still a resolved
        # action and must retain the ability id for the shared secondary stage.
        self.assertIsNotNone(applied_result, ability_id)
        self.assertEqual(applied_result[4], ability_id)
        self.engine._apply_ability_secondary(
            state,
            actor,
            target,
            applied_result,
            random.Random(seed + 1),
        )
        return applied_result

    @staticmethod
    def _result_with(events: tuple[BattleEvent, ...]) -> SimulationResult:
        attacker = _snapshot(1)
        defender = _snapshot(2)
        return SimulationResult(
            attacker=attacker,
            defender=defender,
            winner_pk=attacker.user_pk,
            loser_pk=defender.user_pk,
            duration_ticks=20,
            finish_reason="knockout",
            attacker_remaining_hp=attacker.max_hp,
            defender_remaining_hp=0,
            attacker_damage_dealt=0,
            defender_damage_dealt=0,
            events=events,
            random_seed=71,
            attacker_remaining_stamina=100,
            defender_remaining_stamina=100,
            attacker_remaining_mana=100,
            defender_remaining_mana=100,
        )

    def test_catalog_pure_effect_kinds_have_an_engine_action_representative(self):
        pure_definitions = tuple(
            definition
            for definition in ACTIVE_ABILITY_DEFINITIONS.values()
            if not any(
                effect.effect_type in {"physical_damage", "magic_damage"}
                for effect in definition.effects
            )
        )
        pure_effect_kinds = {
            effect.effect_type
            for definition in pure_definitions
            for effect in definition.effects
        }

        self.assertEqual(
            pure_effect_kinds,
            set(PURE_EFFECT_REPRESENTATIVES),
        )
        for effect_kind, ability_id in PURE_EFFECT_REPRESENTATIVES.items():
            with self.subTest(effect_kind=effect_kind, ability_id=ability_id):
                definition = ACTIVE_ABILITY_DEFINITIONS[ability_id]
                self.assertFalse(
                    any(
                        effect.effect_type
                        in {"physical_damage", "magic_damage"}
                        for effect in definition.effects
                    )
                )
                self.assertTrue(
                    any(
                        effect.effect_type == effect_kind
                        for effect in definition.effects
                    )
                )

    def test_status_and_stance_effects_resolve_after_zero_damage(self):
        state = self._state_for("armor_spell")
        self._resolve_once(state, "armor_spell")
        self.assertIn("armor_spell", state.attacker.statuses)
        status_event = next(
            event
            for event in state.events
            if event.kind == "status_apply"
            and event.status_id == "armor_spell"
        )
        self.assertEqual(status_event.skill_id, "armor_spell")

        mana_before = state.attacker.mana
        absorbed_hit = self.engine._apply_damage(
            state,
            state.defender,
            state.attacker,
            (60, False, False, 0, None, {"physical": 60}),
        )
        barrier_event = next(
            event for event in state.events if event.kind == "mana_barrier"
        )
        self.assertLess(absorbed_hit[0], 60)
        self.assertLess(state.attacker.mana, mana_before)
        self.assertEqual(barrier_event.skill_id, "armor_spell")

        state = self._state_for("elemental_scar")
        self._resolve_once(state, "elemental_scar")
        self.assertIn("elemental_scar", state.defender.statuses)

        state = self._state_for("barbarian_rage")
        mana_before = state.attacker.mana
        self._resolve_once(state, "barbarian_rage")
        self.assertEqual(state.attacker.stance_id, "barbarian_rage")
        self.assertIn("barbarian_rage", state.attacker.statuses)
        self.assertLess(state.attacker.mana, mana_before)
        self.assertTrue(
            any(event.kind == "stance" for event in state.events)
        )

    def test_heal_cleanse_dispel_and_resource_drain_resolve(self):
        state = self._state_for("minor_heal")
        state.attacker.current_hp = state.attacker.max_hp // 2
        state.attacker.statuses["poison"] = CombatStatus(
            "poison",
            state.defender.snapshot.user_pk,
            20,
            beneficial=False,
        )
        hp_before = state.attacker.current_hp
        self._resolve_once(state, "minor_heal")
        self.assertGreater(state.attacker.current_hp, hp_before)
        self.assertNotIn("poison", state.attacker.statuses)
        self.assertTrue(
            any(event.kind == "ability_heal" for event in state.events)
        )
        cleanse_events = [
            event for event in state.events if event.kind == "cleanse"
        ]
        self.assertEqual(len(cleanse_events), 1)
        self.assertEqual(cleanse_events[0].skill_id, "minor_heal")
        self.assertEqual(cleanse_events[0].value, 1)
        report = "\n".join(
            BattleReportBuilder().build(
                self._result_with(tuple(state.events))
            )
        )
        self.assertIn("净化", report)
        self.assertEqual(report.count("净化"), 1)
        moments = _turning_points(
            tuple(event.to_dict() for event in state.events),
            {1: "测试者1", 2: "测试者2"},
            20,
            1,
            2,
            "knockout",
        )
        summaries = "\n".join(moment.summary for moment in moments)
        self.assertIn("净化", summaries)
        self.assertEqual(summaries.count("净化"), 1)

        state = self._state_for("full_dispel")
        state.defender.statuses["test_buff"] = CombatStatus(
            "test_buff",
            state.defender.snapshot.user_pk,
            20,
            beneficial=True,
        )
        state.entities.append(
            BattleEntity(
                entity_id="test_entity",
                owner_pk=state.defender.snapshot.user_pk,
                position=state.defender.position,
                remaining_ticks=20,
                aura_radius=0,
            )
        )
        self._resolve_once(state, "full_dispel")
        self.assertNotIn("test_buff", state.defender.statuses)
        self.assertEqual(state.entities, [])
        dispel_events = [
            event for event in state.events if event.kind == "dispel"
        ]
        self.assertEqual(len(dispel_events), 1)
        self.assertEqual(dispel_events[0].skill_id, "full_dispel")
        self.assertEqual(dispel_events[0].value, 2)

        state = self._state_for("fanaticism")
        target_mana_before = state.defender.mana
        self._resolve_once(state, "fanaticism")
        drain_event = next(
            event for event in state.events if event.kind == "mana_drain"
        )
        # Utility magnitude legitimately grows with learned spell level; the
        # event must agree with the resource delta instead of the level-1
        # catalog value.
        self.assertGreater(drain_event.value, 0)
        self.assertEqual(
            state.defender.mana,
            target_mana_before - drain_event.value,
        )
        self.assertEqual(drain_event.skill_id, "fanaticism")

        state = self._state_for("fear_judgment")
        state.attacker.snapshot.skills.active_definitions[
            "fear_judgment"
        ] = replace(
            ACTIVE_ABILITY_DEFINITIONS["fear_judgment"],
            compatible_weapon_types=(),
        )
        state.attacker.stamina = 100
        self.engine._begin_attack(
            state, state.attacker, "use_skill", "fear_judgment"
        )
        with mock.patch.object(
            self.engine, "_evade_chance", return_value=0.0
        ):
            raw_result = self.engine._attack_damage(
                state,
                state.attacker,
                state.defender,
                "resolve_attack",
                random.Random(17),
            )
        self.assertIsNotNone(raw_result)
        applied_result = self.engine._apply_damage(
            state, state.attacker, state.defender, raw_result
        )
        self.engine._apply_ability_secondary(
            state,
            state.attacker,
            state.defender,
            applied_result,
            random.Random(18),
        )
        restore_event = next(
            event for event in state.events
            if event.kind == "resource_restore"
        )
        self.assertEqual(restore_event.skill_id, "fear_judgment")
        self.assertEqual(restore_event.status_id, "sp")
        self.assertGreater(restore_event.value, 0)

    def test_summon_zone_and_teleport_resolve_without_phantom_knockback(self):
        state = self._state_for("warrior_totem")
        self._resolve_once(state, "warrior_totem")
        self.assertEqual(len(state.entities), 1)
        self.assertTrue(any(event.kind == "summon" for event in state.events))

        state = self._state_for("fire_wall")
        self._resolve_once(state, "fire_wall")
        self.assertEqual(len(state.zones), 1)
        self.assertTrue(
            any(event.kind == "zone_create" for event in state.events)
        )

        state = self._state_for("blink")
        actor_position_before = state.attacker.position
        target_position_before = state.defender.position
        applied_result = self._resolve_once(state, "blink", seed=11)
        teleported_position = state.attacker.position
        self.assertNotEqual(teleported_position, actor_position_before)
        teleport_event = next(
            event for event in state.events if event.kind == "teleport"
        )
        self.assertEqual(teleport_event.skill_id, "blink")
        self.assertEqual(
            teleport_event.value,
            abs(teleported_position - actor_position_before),
        )

        # Complete the same downstream stage used by simulate(). Pure utility
        # must not gain the base 20 knockback, and the pre-action coordinates
        # must not overwrite a teleport that has already resolved.
        self.engine._apply_knockbacks(
            state,
            actor_position_before,
            target_position_before,
            applied_result,
            None,
        )
        self.assertEqual(state.attacker.position, teleported_position)
        self.assertEqual(state.defender.position, target_position_before)
        self.assertFalse(
            any(event.kind == "knockback" for event in state.events)
        )

    def test_direct_shapes_evade_but_pure_status_uses_one_contest(self):
        direct_cases = (
            ("magic_arrow", "single"),
            ("mana_ray", "line"),
            ("magic_arrow", "projectile"),
        )
        for ability_id, targeting in direct_cases:
            with self.subTest(ability_id=ability_id, targeting=targeting):
                state = self._state_for(ability_id)
                if targeting == "projectile":
                    state.attacker.snapshot.skills.active_definitions[
                        ability_id
                    ] = replace(
                        ACTIVE_ABILITY_DEFINITIONS[ability_id],
                        targeting="projectile",
                    )
                self.engine._begin_attack(
                    state, state.attacker, "use_skill", ability_id
                )
                with mock.patch.object(
                    self.engine, "_evade_chance", return_value=1.0
                ):
                    result = self.engine._attack_damage(
                        state,
                        state.attacker,
                        state.defender,
                        "resolve_attack",
                        random.Random(3),
                    )
                self.assertIsNone(result)
                self.assertTrue(
                    any(
                        event.kind == "evade"
                        and event.skill_id == ability_id
                        for event in state.events
                    )
                )

        for ability_id in ("elemental_scar", "shining_word"):
            with self.subTest(pure_control=ability_id):
                state = self._state_for(ability_id)
                with (
                    mock.patch.object(
                        self.engine,
                        "_evade_chance",
                        side_effect=AssertionError(
                            "pure status must not roll direct evasion"
                        ),
                    ),
                    mock.patch(
                        "services.ability_runtime.status_chance",
                        return_value=1.0,
                    ),
                ):
                    self._resolve_once(state, ability_id)
                status_event = next(
                    event for event in state.events
                    if event.kind == "status_apply"
                )
                self.assertEqual(status_event.skill_id, ability_id)
                self.assertFalse(
                    any(event.kind == "evade" for event in state.events)
                )

        resisted = self._state_for("elemental_scar")
        with (
            mock.patch.object(
                self.engine,
                "_evade_chance",
                side_effect=AssertionError(
                    "pure status must not roll direct evasion"
                ),
            ),
            mock.patch(
                "services.ability_runtime.status_chance",
                return_value=0.0,
            ),
        ):
            self._resolve_once(resisted, "elemental_scar")
        resist_event = next(
            event for event in resisted.events
            if event.kind == "status_resist"
        )
        self.assertEqual(resist_event.skill_id, "elemental_scar")

    def test_ground_cast_skips_direct_evade_and_pulses_use_gateways(self):
        for ability_id in ("fire_wall", "web", "acid_sea"):
            with self.subTest(ability_id=ability_id):
                state = self._state_for(ability_id)
                with mock.patch.object(
                    self.engine,
                    "_evade_chance",
                    side_effect=AssertionError(
                        "ground placement must not roll target evasion"
                    ),
                ):
                    self._resolve_once(state, ability_id)
                zone_event = next(
                    event for event in state.events
                    if event.kind == "zone_create"
                )
                self.assertEqual(zone_event.skill_id, ability_id)
                self.assertFalse(
                    any(event.kind == "evade" for event in state.events)
                )

        state = self._state_for("fire_wall")
        self._resolve_once(state, "fire_wall")
        state.tick = 5
        with (
            mock.patch.object(
                self.engine,
                "_apply_damage",
                wraps=self.engine._apply_damage,
            ) as damage_gateway,
            mock.patch.object(
                self.engine.ability_runtime,
                "apply_status",
                wraps=self.engine.ability_runtime.apply_status,
            ) as status_gateway,
            mock.patch(
                "services.ability_runtime.status_chance",
                return_value=1.0,
            ),
        ):
            self.engine.ability_runtime.tick(
                state,
                random.Random(1),
                apply_damage=damage_gateway,
            )
        self.assertGreater(damage_gateway.call_count, 0)
        self.assertGreater(status_gateway.call_count, 0)
        zone_damage = next(
            event for event in state.events if event.kind == "zone_damage"
        )
        zone_status = next(
            event for event in state.events
            if event.kind == "status_apply" and event.status_id == "burn"
        )
        self.assertEqual(zone_damage.skill_id, "fire_wall")
        self.assertEqual(zone_status.skill_id, "fire_wall")

    def test_utility_outcomes_are_explained_once_in_report_and_replay(self):
        cases = (
            (
                BattleEvent(4, "cleanse", 1, 1, 1, skill_id="minor_heal"),
                "净化",
            ),
            (
                BattleEvent(4, "dispel", 1, 2, 2, skill_id="full_dispel"),
                "驱散",
            ),
            (
                BattleEvent(4, "mana_drain", 1, 2, 30, skill_id="fanaticism"),
                "吸取30点魔力",
            ),
            (
                BattleEvent(4, "teleport", 1, 1, 90, skill_id="blink"),
                "移动了90距离",
            ),
            (
                BattleEvent(
                    4,
                    "status_apply",
                    1,
                    1,
                    40,
                    skill_id="armor_spell",
                    status_id="armor_spell",
                ),
                "获得「armor_spell」状态",
            ),
        )
        names = {1: "测试者1", 2: "测试者2"}
        for event, expected in cases:
            with self.subTest(kind=event.kind):
                report = "\n".join(
                    BattleReportBuilder().build(
                        self._result_with((event,))
                    )
                )
                self.assertIn(expected, report)
                self.assertNotIn("战局随之改变", report)

                cast = BattleEvent(
                    2,
                    "spell_cast",
                    event.actor_pk,
                    event.target_pk,
                    skill_id=event.skill_id,
                )
                moments = _turning_points(
                    (cast.to_dict(), event.to_dict()),
                    names,
                    20,
                    1,
                    2,
                    "knockout",
                )
                matching = [
                    moment for moment in moments
                    if expected.replace("移动了", "")[:2]
                    in moment.summary
                ]
                self.assertEqual(len(matching), 1)

    def test_full_simulation_keeps_pure_status_barrier_and_blink_effects(self):
        profile = AIProfile(
            aggression=0.95,
            guard_tendency=0.0,
            chase_tendency=1.0,
            preferred_range=90,
            retreat_tendency=0.0,
            low_hp_risk=1.0,
            strategy_name="测试控制",
            tactic_plan=("control", "control", "control"),
        )

        armor_result = self.engine.simulate(
            _snapshot(1, "armor_spell"),
            _snapshot(2),
            profile,
            profile,
            71,
            environment_id="calm",
        )
        armor_status = next(
            event
            for event in armor_result.events
            if event.kind == "status_apply"
            and event.status_id == "armor_spell"
        )
        armor_barrier = next(
            event
            for event in armor_result.events
            if event.kind == "mana_barrier"
        )
        self.assertEqual(armor_status.skill_id, "armor_spell")
        self.assertEqual(armor_barrier.skill_id, "armor_spell")
        self.assertGreater(armor_barrier.value, 0)

        blink_result = self.engine.simulate(
            _snapshot(1, "blink"),
            _snapshot(2),
            profile,
            profile,
            71,
            environment_id="calm",
        )
        blink_index, blink_event = next(
            (index, event)
            for index, event in enumerate(blink_result.events)
            if event.kind == "teleport"
            and event.skill_id == "blink"
        )
        previous_actor_position = next(
            event.position
            for event in reversed(blink_result.events[:blink_index])
            if event.actor_pk == 1 and event.position is not None
        )
        self.assertNotEqual(blink_event.position, previous_actor_position)
        self.assertFalse(
            any(
                event.tick == blink_event.tick
                and event.kind == "knockback"
                for event in blink_result.events[blink_index + 1 :]
            )
        )

        with (
            mock.patch.object(
                self.engine, "_evade_chance", return_value=1.0
            ),
            mock.patch(
                "services.ability_runtime.status_chance", return_value=1.0
            ),
        ):
            zone_result = self.engine.simulate(
                _snapshot(1, "fire_wall"),
                _snapshot(2),
                profile,
                profile,
                71,
                environment_id="calm",
            )
        self.assertTrue(
            any(
                event.kind == "zone_create"
                and event.skill_id == "fire_wall"
                for event in zone_result.events
            )
        )
        self.assertTrue(
            any(
                event.kind == "zone_damage"
                and event.skill_id == "fire_wall"
                for event in zone_result.events
            )
        )
        self.assertFalse(
            any(
                event.kind == "evade" and event.skill_id == "fire_wall"
                for event in zone_result.events
            )
        )


if __name__ == "__main__":
    unittest.main()
