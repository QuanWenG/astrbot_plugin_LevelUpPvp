import random
import types
import unittest
from unittest import mock

from models.combat import AIProfile, BattleState, FighterSnapshot, FighterState
from services.combat_ai import STRATEGY_PROFILES, choose_action
from services.combat_engine import SideviewCombatEngine


def _fighter(user_pk: int, *, hp=10, atk=5, defense=5, speed=5, luck=5):
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
        first_windup = next(event for event in events if event.kind == "attack_windup")
        release = next(
            event
            for event in events
            if event.kind == "attack" and event.actor_pk == first_windup.actor_pk
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
        self.assertEqual(first.duration_ticks, 10 + self.engine.ATTACK_WINDUP_TICKS)

    def test_evade_and_critical_chances_are_capped(self):
        weak = FighterState(_fighter(1, speed=0, luck=0), 100, 200)
        strong = FighterState(_fighter(2, speed=1000, luck=1000), 100, 800)

        self.assertEqual(self.engine._evade_chance(weak, strong), 0.20)
        self.assertEqual(self.engine._evade_chance(strong, weak), 0.0)
        self.assertEqual(self.engine._critical_chance(strong, weak), 0.25)
        self.assertEqual(self.engine._critical_chance(weak, strong), 0.05)

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
