import math
import random
import types
import unittest
from unittest import mock

from models.ability import CombatStatus, UserSpell
from models.attributes import DerivedStats
from models.combat import AIProfile, BattleState, FighterSnapshot, FighterState
from models.equipment import EquipmentBuild
from models.skill import SkillBuild
from services.ability_catalog import SPELL_DEFINITIONS
from services.combat_ai import STRATEGY_PROFILES, choose_action
from services.combat_engine import SideviewCombatEngine


def _fighter(
    user_pk: int,
    *,
    hp=10,
    atk=5,
    defense=5,
    speed=5,
    luck=5,
    combatant_kind="player",
):
    return FighterSnapshot(
        user_pk=user_pk,
        name=str(user_pk),
        level=1,
        hp=hp,
        atk=atk,
        defense=defense,
        speed=speed,
        luck=luck,
        strategy="test",
        combatant_kind=combatant_kind,
    )


def _spell_fighter(*, mana: int = 100) -> FighterState:
    definition = SPELL_DEFINITIONS["magic_arrow"]
    skills = SkillBuild(
        {},
        {definition.unlock_skill_id: 80},
        (definition.ability_id,),
        {definition.ability_id: definition},
        {},
        {definition.ability_id: UserSpell(definition.ability_id)},
    )
    snapshot = FighterSnapshot(
        1,
        "法师",
        20,
        20,
        20,
        10,
        10,
        10,
        "test",
        skills=skills,
    )
    return FighterState(
        snapshot,
        snapshot.max_hp,
        250,
        mana=mana,
        stamina=100,
    )


def _weather_equipment(*, ranged: bool = False) -> EquipmentBuild:
    return EquipmentBuild(
        items=(),
        slots={},
        stat_modifiers={},
        skill_modifiers={},
        weapon_mode="two_hand_ranged" if ranged else "one_hand",
        weapon_type="bow" if ranged else "longsword",
        armor_style="light",
        total_weight=1.0,
        carry_capacity=100.0,
        overloaded=False,
        attack_range=350 if ranged else 100,
        damage_multiplier=1.0,
        attack_windup=2,
        attack_recovery=2,
        attack_cooldown=6,
        attack_stamina=8,
        movement_multiplier=1.0,
        stamina_regen=10,
        max_stamina=100,
        weapon_weight=1.0,
        adverse_weather_immunity=True,
    )


class CombatRuleTests(unittest.TestCase):
    def setUp(self):
        self.engine = SideviewCombatEngine()
        self.aggressive = AIProfile(1.0, 0.0, 1.0, 90, 0.0, 1.0)

    def test_collision_distance_is_preserved_at_field_edge(self):
        attacker_snapshot = _fighter(1)
        defender_snapshot = _fighter(2)
        state = BattleState(
            1,
            FighterState(attacker_snapshot, attacker_snapshot.max_hp, 990),
            FighterState(defender_snapshot, defender_snapshot.max_hp, 1000),
            [],
            1,
        )

        self.engine._resolve_movement(state, "advance", "retreat")

        self.assertEqual(
            state.defender.position - state.attacker.position,
            self.engine.MIN_DISTANCE,
        )
        self.assertLessEqual(state.defender.position, self.engine.FIELD_MAX)
    def test_attack_uses_windup_recovery_hitstun_and_knockback(self):
        result = self.engine.simulate(
            _fighter(1, atk=12),
            _fighter(2, defense=4),
            self.aggressive,
            self.aggressive,
            21,
        )
        events = list(result.events)
        release = next(
            event for event in events if event.kind == "attack"
        )
        first_windup = next(
            event
            for event in reversed(events[:events.index(release)])
            if event.kind == "attack_windup"
            and event.actor_pk == release.actor_pk
        )

        self.assertEqual(
            release.tick - first_windup.tick,
            self.engine.ATTACK_WINDUP_TICKS,
        )
        self.assertTrue(
            any(
                event.kind == "recovery"
                and event.actor_pk == release.actor_pk
                and event.tick == release.tick
                for event in events
            )
        )
        damage = next(event for event in events if event.kind == "damage")
        self.assertTrue(
            any(
                event.kind == "hitstun"
                and event.target_pk == damage.target_pk
                and event.tick == damage.tick
                for event in events
            )
        )
        self.assertFalse(
            any(
                event.actor_pk == damage.target_pk
                and event.tick == damage.tick + 1
                and event.kind in {"move", "guard", "attack_windup"}
                for event in events
            )
        )
        knockback = next(
            event
            for event in events
            if event.kind == "knockback"
            and event.target_pk == damage.target_pk
            and event.tick == damage.tick
        )
        self.assertGreater(knockback.value, 0)
        self.assertGreaterEqual(knockback.position, self.engine.FIELD_MIN)
        self.assertLessEqual(knockback.position, self.engine.FIELD_MAX)

    def test_tactic_initiative_changes_action_tempo_without_damage_bonus(self):
        slow = FighterState(
            _fighter(1), 100, 200, tactic_initiative=-0.10
        )
        neutral = FighterState(_fighter(2), 100, 200)
        fast = FighterState(
            _fighter(3), 100, 200, tactic_initiative=0.10
        )

        self.assertGreater(
            self.engine._scaled_ticks(slow, 20),
            self.engine._scaled_ticks(neutral, 20),
        )
        self.assertLess(
            self.engine._scaled_ticks(fast, 20),
            self.engine._scaled_ticks(neutral, 20),
        )

    def test_tactic_counter_edge_changes_real_counter_stamina_cost(self):
        def counter_cost(modifier: float) -> tuple[int, int]:
            reactor = FighterState(
                _fighter(1, atk=12),
                100,
                450,
                stamina=100,
                tactic_counter_sp_cost=modifier,
            )
            reactor.statuses["hold_the_line"] = CombatStatus(
                "hold_the_line", 1, 30, beneficial=True
            )
            target = FighterState(_fighter(2), 100, 500, stamina=100)
            state = BattleState(1, reactor, target, [], 7)

            self.engine._try_counterattack(
                state,
                reactor,
                target,
                (10, False, False),
                random.Random(1),
            )

            event = next(
                item for item in state.events
                if item.kind == "counter_trigger"
            )
            return 100 - reactor.stamina, event.stamina

        discounted, discounted_remaining = counter_cost(-0.18)
        expensive, expensive_remaining = counter_cost(0.18)

        self.assertEqual(discounted, 4)
        self.assertEqual(expensive, 6)
        self.assertEqual(discounted_remaining, 96)
        self.assertEqual(expensive_remaining, 94)

    def test_hit_during_windup_interrupts_the_pending_attack(self):
        attacker_snapshot = _fighter(1)
        target_snapshot = _fighter(2)
        attacker = FighterState(attacker_snapshot, attacker_snapshot.max_hp, 450)
        target = FighterState(
            target_snapshot,
            target_snapshot.max_hp,
            500,
            windup_ticks=1,
            attack_pending=True,
        )
        state = BattleState(4, attacker, target, [], 7)

        self.engine._apply_damage(state, attacker, target, (10, False, False))

        self.assertFalse(target.attack_pending)
        self.assertEqual(target.windup_ticks, 0)
        self.assertEqual(target.recovery_ticks, 1)
        self.assertTrue(
            any(event.kind == "attack_interrupted" for event in state.events)
        )

    def test_rain_weakens_fire_and_amplifies_lightning_damage(self):
        attacker_snapshot = _fighter(1, hp=100)
        target_snapshot = _fighter(2, hp=100)
        attacker = FighterState(
            attacker_snapshot,
            attacker_snapshot.max_hp,
            450,
        )
        target = FighterState(
            target_snapshot,
            target_snapshot.max_hp,
            500,
        )
        state = BattleState(
            1,
            attacker,
            target,
            [],
            17,
            environment_id="rain",
        )

        self.engine._apply_damage(
            state,
            attacker,
            target,
            (
                200,
                False,
                False,
                0,
                "storm_strike",
                {"fire": 100, "lightning": 100},
            ),
        )

        event = next(item for item in state.events if item.kind == "damage")
        self.assertEqual(
            event.damage_breakdown,
            {"fire": 90, "lightning": 115},
        )
        self.assertEqual(event.value, 205)

    def test_weather_immunity_skips_rain_penalty_but_keeps_lightning_bonus(self):
        actor_snapshot = FighterSnapshot(
            **{
                **_fighter(1, hp=100).__dict__,
                "equipment": _weather_equipment(),
            }
        )
        target_snapshot = _fighter(2, hp=100, combatant_kind="monster")
        state = BattleState(
            1,
            FighterState(actor_snapshot, actor_snapshot.max_hp, 450),
            FighterState(target_snapshot, target_snapshot.max_hp, 500),
            [],
            19,
            environment_id="rain",
        )

        self.engine._apply_damage(
            state,
            state.attacker,
            state.defender,
            (
                200,
                False,
                False,
                0,
                "storm_strike",
                {"fire": 100, "lightning": 100},
            ),
        )

        event = next(item for item in state.events if item.kind == "damage")
        self.assertEqual(event.damage_breakdown, {"fire": 100, "lightning": 115})
        self.assertEqual(event.value, 215)

    def test_weather_immunity_skips_only_pve_fog_and_wind_penalties(self):
        ranged_snapshot = FighterSnapshot(
            **{
                **_fighter(1, hp=100).__dict__,
                "equipment": _weather_equipment(ranged=True),
            }
        )
        target_snapshot = _fighter(2, hp=100, combatant_kind="monster")
        ranged = FighterState(ranged_snapshot, ranged_snapshot.max_hp, 450)
        target = FighterState(target_snapshot, target_snapshot.max_hp, 500)
        calm_windup = _weather_equipment(ranged=True).attack_windup
        wind = BattleState(
            1, ranged, target, [], 21, environment_id="strong_wind"
        )
        self.engine._begin_attack(wind, ranged, "basic_attack")
        self.assertEqual(ranged.windup_ticks, calm_windup)

        fog = BattleState(1, ranged, target, [], 22, environment_id="fog")
        self.assertEqual(
            self.engine._evade_chance(ranged, target, state=fog),
            self.engine._evade_chance(ranged, target),
        )

    def test_weather_immunity_does_not_affect_pvp_or_ether_disturbance(self):
        ranged_snapshot = FighterSnapshot(
            **{
                **_fighter(1, hp=100).__dict__,
                "equipment": _weather_equipment(ranged=True),
            }
        )
        player_target_snapshot = _fighter(2, hp=100)
        ranged = FighterState(ranged_snapshot, ranged_snapshot.max_hp, 450)
        player_target = FighterState(
            player_target_snapshot,
            player_target_snapshot.max_hp,
            500,
        )
        wind = BattleState(
            1, ranged, player_target, [], 21, environment_id="strong_wind"
        )
        self.engine._begin_attack(wind, ranged, "basic_attack")
        self.assertGreater(
            ranged.windup_ticks,
            _weather_equipment(ranged=True).attack_windup,
        )

        fog = BattleState(
            1, ranged, player_target, [], 22, environment_id="fog"
        )
        self.assertFalse(
            self.engine._immune_to_adverse_weather(fog, ranged)
        )

        rain = BattleState(
            1, ranged, player_target, [], 24, environment_id="rain"
        )
        self.engine._apply_damage(
            rain,
            ranged,
            player_target,
            (100, False, False, 0, "fire_hit", {"fire": 100}),
        )
        rain_damage = next(
            event for event in rain.events if event.kind == "damage"
        )
        self.assertEqual(rain_damage.damage_breakdown, {"fire": 90})

        caster = _spell_fighter()
        caster.snapshot = FighterSnapshot(
            **{
                **caster.snapshot.__dict__,
                "equipment": _weather_equipment(),
            }
        )
        caster.runtime_overloaded = True
        ether_target_snapshot = _fighter(
            3,
            hp=100,
            combatant_kind="monster",
        )
        ether_target = FighterState(
            ether_target_snapshot,
            ether_target_snapshot.max_hp,
            500,
        )
        ether = BattleState(
            1, caster, ether_target, [], 23, environment_id="ether_disturbance"
        )
        self.engine._begin_attack(ether, caster, "use_skill", "magic_arrow")
        self.assertTrue(
            any(
                event.status_id == "ether_disturbance"
                for event in ether.events
            )
        )

    def test_ether_disturbance_punishes_rapid_casting_deterministically(self):
        def cast_twice(second_tick: int):
            actor = _spell_fighter()
            target_snapshot = _fighter(2)
            target = FighterState(
                target_snapshot,
                target_snapshot.max_hp,
                750,
            )
            state = BattleState(
                1,
                actor,
                target,
                [],
                23,
                environment_id="ether_disturbance",
            )
            self.engine._begin_attack(
                state,
                actor,
                "use_skill",
                "magic_arrow",
            )
            actor.attack_pending = False
            actor.pending_skill_id = None
            actor.pending_resource_details = {}
            actor.windup_ticks = 0
            state.tick = second_tick
            self.engine._begin_attack(
                state,
                actor,
                "use_skill",
                "magic_arrow",
            )
            return actor, state

        actor, state = cast_twice(self.engine.ETHER_CAST_WINDOW_TICKS)
        backlash = [
            event
            for event in state.events
            if event.kind == "mana_backlash"
            and event.status_id == "ether_disturbance"
        ]
        self.assertEqual(len(backlash), 1)
        self.assertGreater(backlash[0].value, 0)
        self.assertEqual(backlash[0].remaining_hp, actor.current_hp)

        replay_actor, replay = cast_twice(self.engine.ETHER_CAST_WINDOW_TICKS)
        replay_backlash = next(
            event
            for event in replay.events
            if event.kind == "mana_backlash"
            and event.status_id == "ether_disturbance"
        )
        self.assertEqual(backlash[0].to_dict(), replay_backlash.to_dict())
        self.assertEqual(actor.current_hp, replay_actor.current_hp)

        _, relaxed = cast_twice(self.engine.ETHER_CAST_WINDOW_TICKS + 2)
        self.assertFalse(
            any(
                event.kind == "mana_backlash"
                and event.status_id == "ether_disturbance"
                for event in relaxed.events
            )
        )

    def test_ether_disturbance_overload_backlash_is_bounded_and_nonlethal(self):
        actor = _spell_fighter()
        actor.current_hp = 2
        actor.runtime_overloaded = True
        target_snapshot = _fighter(2)
        state = BattleState(
            1,
            actor,
            FighterState(
                target_snapshot,
                target_snapshot.max_hp,
                750,
            ),
            [],
            29,
            environment_id="ether_disturbance",
        )

        self.engine._begin_attack(
            state,
            actor,
            "use_skill",
            "magic_arrow",
        )

        backlash = next(
            event
            for event in state.events
            if event.kind == "mana_backlash"
            and event.status_id == "ether_disturbance"
        )
        self.assertEqual(backlash.value, 1)
        self.assertEqual(backlash.remaining_hp, 1)
        self.assertEqual(actor.current_hp, 1)
        environment_cap = (
            self.engine.ruleset.environment.environmental_damage_hp_ratio_cap
        )
        self.assertLessEqual(
            backlash.value,
            max(
                1,
                math.ceil(
                    actor.max_hp
                    * min(
                        self.engine.ETHER_BACKLASH_HP_RATIO_CAP,
                        environment_cap,
                    )
                ),
            ),
        )

    def test_ether_disturbance_marks_mana_overcast_backlash(self):
        actor = _spell_fighter(mana=0)
        actor.current_derived = DerivedStats(
            max_hp=actor.max_hp,
            max_mp=100,
            max_sp=100,
            attack_power=20,
            accuracy=20,
            defense=10,
            evasion=10,
            critical_rate=0.05,
            critical_damage=1.5,
            action_speed=100,
            carry_capacity=50,
            mana_overcast_reduction=0.0,
        )
        target_snapshot = _fighter(2)
        state = BattleState(
            1,
            actor,
            FighterState(
                target_snapshot,
                target_snapshot.max_hp,
                750,
            ),
            [],
            31,
            environment_id="ether_disturbance",
        )

        self.engine._begin_attack(
            state,
            actor,
            "use_skill",
            "magic_arrow",
        )

        self.assertLess(actor.mana, 0)
        ether_backlash = next(
            event
            for event in state.events
            if event.kind == "mana_backlash"
            and event.status_id == "ether_disturbance"
        )
        self.assertGreater(ether_backlash.value, 0)
        self.assertGreaterEqual(actor.current_hp, 1)

    def test_equipment_life_steal_recovers_from_applied_damage(self):
        equipment = types.SimpleNamespace(
            combat_effects={"life_steal": 0.20},
            armor_style="light",
            total_weight=1.0,
            overloaded=False,
            max_stamina=100,
        )
        attacker_snapshot = FighterSnapshot(
            **{
                **_fighter(1, hp=10).__dict__,
                "equipment": equipment,
            }
        )
        target_snapshot = _fighter(2, hp=10)
        attacker = FighterState(attacker_snapshot, 100, 450)
        target = FighterState(target_snapshot, 100, 500)
        state = BattleState(1, attacker, target, [], 1)

        self.engine._apply_damage(
            state,
            attacker,
            target,
            (25, False, False),
        )

        self.assertEqual(attacker.current_hp, 105)
        self.assertEqual(target.current_hp, 75)
        self.assertTrue(any(event.kind == "life_steal" for event in state.events))

    def test_equipment_armor_penetration_reduces_effective_defense(self):
        equipment = types.SimpleNamespace(
            combat_effects={"armor_penetration": 0.50},
            armor_style="light",
            total_weight=1.0,
            overloaded=False,
            max_stamina=100,
            attack_recovery=2,
            attack_cooldown=6,
            attack_range=100,
            physical_accuracy_multiplier=1.0,
            spell_accuracy_multiplier=1.0,
            weapon_mode="one_hand",
            weapon_type="longsword",
        )
        attacker_snapshot = FighterSnapshot(
            **{
                **_fighter(1, atk=10).__dict__,
                "equipment": equipment,
            }
        )
        target_snapshot = _fighter(2, defense=100)
        attacker = FighterState(
            attacker_snapshot,
            attacker_snapshot.max_hp,
            450,
            attack_pending=True,
        )
        target = FighterState(target_snapshot, target_snapshot.max_hp, 500)
        state = BattleState(1, attacker, target, [], 1)

        with mock.patch(
            "services.combat_engine.physical_damage_amount",
            return_value=10,
        ) as calculate:
            self.engine._attack_damage(
                state,
                attacker,
                target,
                "resolve_attack",
                random.Random(1),
            )

        self.assertEqual(calculate.call_args.kwargs["defense"], 50.0)
    def test_attack_release_lunges_back_into_range(self):
        attacker_snapshot = _fighter(1)
        defender_snapshot = _fighter(2)
        attacker = FighterState(
            attacker_snapshot,
            attacker_snapshot.max_hp,
            450,
            attack_pending=True,
        )
        defender = FighterState(
            defender_snapshot,
            defender_snapshot.max_hp,
            570,
        )
        state = BattleState(2, attacker, defender, [], 1)

        self.engine._resolve_attack_lunges(
            state,
            "resolve_attack",
            "recovery",
        )

        self.assertEqual(defender.position - attacker.position, self.engine.ATTACK_RANGE)
        self.assertTrue(
            any(event.kind == "attack_lunge" for event in state.events)
        )
    def test_ai_repositions_while_attack_is_on_cooldown(self):
        own_snapshot = _fighter(1)
        opponent_snapshot = _fighter(2)
        own = FighterState(
            own_snapshot,
            own_snapshot.max_hp,
            450,
            attack_cooldown=3,
        )
        opponent = FighterState(
            opponent_snapshot,
            opponent_snapshot.max_hp,
            500,
        )

        intent = choose_action(
            own,
            opponent,
            STRATEGY_PROFILES["速度拉扯"],
            random.Random(1),
            self.engine.ATTACK_RANGE,
        )

        self.assertEqual(intent.action, "retreat")

    def test_simultaneous_knockout_has_deterministic_winner(self):
        attacker = _fighter(1, hp=0, atk=100, defense=0)
        defender = _fighter(2, hp=0, atk=100, defense=0)
        first = self.engine.simulate(attacker, defender, self.aggressive, self.aggressive, 3)
        replay = self.engine.simulate(attacker, defender, self.aggressive, self.aggressive, 3)

        self.assertEqual(first.finish_reason, "double_ko_tiebreak")
        self.assertEqual(first.winner_pk, replay.winner_pk)
        self.assertEqual(first.duration_ticks, replay.duration_ticks)
        self.assertGreater(first.duration_ticks, 0)
        self.assertLessEqual(first.duration_ticks, self.engine.MAX_TICKS)

    def test_evade_and_critical_chances_are_capped(self):
        weak = FighterState(_fighter(1, speed=0, luck=0), 100, 200)
        strong = FighterState(_fighter(2, speed=1000, luck=1000), 100, 800)

        self.assertEqual(self.engine._evade_chance(weak, strong), 0.20)
        self.assertEqual(self.engine._evade_chance(strong, weak), 0.0)
        self.assertAlmostEqual(
            self.engine._critical_chance(strong, weak),
            0.05 + self.engine.ruleset.fortune.critical_bonus_cap,
        )
        self.assertAlmostEqual(self.engine._critical_chance(weak, strong), 0.03)

    def test_guard_reduces_damage_and_cooldown_is_respected(self):
        attacker_snapshot = _fighter(1, atk=10)
        defender_snapshot = _fighter(2, defense=5)

        def attack_once(guarding: bool):
            attacker = FighterState(attacker_snapshot, attacker_snapshot.max_hp, 450)
            defender = FighterState(
                defender_snapshot,
                defender_snapshot.max_hp,
                500,
                guarding=guarding,
            )
            state = BattleState(1, attacker, defender, [], 9)
            attacker.attack_pending = True
            return self.engine._attack_damage(
                state,
                attacker,
                defender,
                "resolve_attack",
                random.Random(9),
            )

        normal_damage = attack_once(False)[0]
        guarded_damage = attack_once(True)[0]
        self.assertEqual(
            guarded_damage,
            max(1, round(normal_damage * self.engine.GUARD_DAMAGE_MULTIPLIER)),
        )

        result = self.engine.simulate(
            attacker_snapshot,
            defender_snapshot,
            self.aggressive,
            self.aggressive,
            13,
        )
        for user_pk in (1, 2):
            hit_ticks = [
                event.tick
                for event in result.events
                if event.kind == "attack" and event.actor_pk == user_pk
            ]
            self.assertTrue(
                all(current - previous >= 4 for previous, current in zip(hit_ticks, hit_ticks[1:]))
            )


if __name__ == "__main__":
    unittest.main()
