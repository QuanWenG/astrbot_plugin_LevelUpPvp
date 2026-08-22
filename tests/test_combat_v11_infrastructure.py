import random
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest import mock

from models.combat import AIProfile, BattleState, FighterSnapshot

from services.balance_rules import (
    hit_chance,
    physical_damage_amount,
    triangular_variance,
)
from services.combat_engine import SideviewCombatEngine
from services.combat_random import KeyedEntropy
from services.combat_ruleset import (
    CombatRuleSet,
    DEFAULT_RULESET_REGISTRY,
    RuleSetRegistry,
    SIDEVIEW_V10_RULESET,
    SIDEVIEW_V11_RULESET,
)


class CombatRuleSetTests(unittest.TestCase):
    def test_default_registry_contains_both_exact_versions(self):
        registry = RuleSetRegistry()

        self.assertEqual(
            registry.ids(),
            ("sideview-v10", "sideview-v11"),
        )
        self.assertIs(registry.require("sideview-v10"), SIDEVIEW_V10_RULESET)
        self.assertIs(registry.require("sideview-v11"), SIDEVIEW_V11_RULESET)
        self.assertIs(
            DEFAULT_RULESET_REGISTRY.require("sideview-v11"),
            SIDEVIEW_V11_RULESET,
        )

    def test_registry_forbids_latest_and_unknown_versions(self):
        registry = RuleSetRegistry()

        with self.assertRaisesRegex(ValueError, "latest"):
            registry.require("latest")
        with self.assertRaisesRegex(KeyError, "sideview-v99"):
            registry.require("sideview-v99")

    def test_registry_can_be_built_without_defaults_and_rejects_duplicates(self):
        custom = CombatRuleSet("test-v1", "test rules")
        registry = RuleSetRegistry(include_defaults=False)
        registry.register(custom)

        self.assertIs(registry.require("test-v1"), custom)
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(custom)
        with self.assertRaises(TypeError):
            registry.register(object())

    def test_rules_and_nested_groups_are_immutable(self):
        rules = SIDEVIEW_V11_RULESET

        with self.assertRaises(FrozenInstanceError):
            rules.display_name = "mutated"
        with self.assertRaises(FrozenInstanceError):
            rules.hit.ceiling = 1.0
        with self.assertRaises(FrozenInstanceError):
            rules.strategy.initiative_cap = 99.0
        with self.assertRaises(TypeError):
            DEFAULT_RULESET_REGISTRY.snapshot()["sideview-v11"] = SIDEVIEW_V10_RULESET

    def test_v11_limits_express_the_balance_contract(self):
        rules = SIDEVIEW_V11_RULESET

        self.assertEqual(
            (rules.hit.physical_floor, rules.hit.spell_floor, rules.hit.ceiling),
            (0.35, 0.50, 0.97),
        )
        self.assertEqual(rules.damage.total_reduction_cap, 0.72)
        self.assertEqual(
            (rules.tempo.speed_multiplier_floor, rules.tempo.speed_multiplier_ceiling),
            (0.75, 1.35),
        )
        self.assertEqual(
            (rules.fortune.luck_floor, rules.fortune.luck_ceiling),
            (60, 180),
        )
        self.assertEqual(rules.fortune.charge_cap, 3)
        self.assertEqual(
            (
                rules.strategy.utility_cap,
                rules.strategy.guard_logit_cap,
                rules.strategy.initiative_cap,
                rules.strategy.counter_sp_cost_cap,
                rules.strategy.counter_stamina_ratio,
            ),
            (0.12, 0.32, 0.10, 0.18, 0.05),
        )
        self.assertEqual(
            (
                rules.status.hard_control_duration_cap_ticks,
                rules.status.repeated_control_multiplier,
                rules.status.post_control_immunity_ticks,
            ),
            (4, 0.65, 1),
        )
        self.assertEqual(
            (
                SIDEVIEW_V10_RULESET.status.hard_control_duration_cap_ticks,
                SIDEVIEW_V10_RULESET.status.repeated_control_multiplier,
                SIDEVIEW_V10_RULESET.status.post_control_immunity_ticks,
            ),
            (6, 1.0, 0),
        )
        self.assertTrue(rules.environment.rated_reset_resources)
        self.assertFalse(rules.environment.rated_persists_combat_state)
        self.assertEqual(rules.timeout.hard_tick_limit, 160)
        self.assertEqual(rules.timeout.sudden_death_start_tick, 50)
        self.assertEqual(
            (
                rules.timeout.sudden_death_damage_growth_per_tick,
                rules.timeout.sudden_death_damage_growth_cap,
            ),
            (0.07, 2.0),
        )
        self.assertEqual(
            (
                rules.timeout.sudden_death_minimum_hit_ratio,
                rules.timeout.sudden_death_minimum_hit_ratio_growth,
                rules.timeout.sudden_death_minimum_hit_ratio_cap,
            ),
            (0.04, 0.003, 0.15),
        )
        self.assertEqual(rules.timeout.sudden_death_healing_multiplier, 0.20)

    def test_custom_ruleset_drives_formula_helpers_and_ability_runtime(self):
        custom = replace(
            SIDEVIEW_V11_RULESET,
            ruleset_id="formula-proof-v1",
            display_name="formula proof",
            hit=replace(SIDEVIEW_V11_RULESET.hit, physical_floor=0.77),
            damage=replace(
                SIDEVIEW_V11_RULESET.damage,
                armor_anchor=20.0,
                variance_low=0.50,
                variance_high=0.70,
            ),
        )
        registry = RuleSetRegistry((custom,), include_defaults=False)
        engine = SideviewCombatEngine(custom.ruleset_id, registry)

        self.assertIs(engine.ability_runtime.ruleset, custom)
        self.assertEqual(
            hit_chance(
                1,
                10_000,
                is_spell=False,
                ruleset=custom,
            ),
            0.77,
        )
        self.assertAlmostEqual(
            triangular_variance(0.0, 0.0, ruleset=custom),
            0.50,
        )
        common = dict(
            attack_power=100,
            offense_multiplier=1.0,
            effect_multiplier=1.0,
            variance=1.0,
            defense=100,
            attacker_level=20,
            physical_reduction=0.0,
        )
        self.assertLess(
            physical_damage_amount(**common, ruleset=custom),
            physical_damage_amount(**common, ruleset=SIDEVIEW_V11_RULESET),
        )

    def test_engine_ai_uses_its_own_ruleset_runtime(self):
        engine = SideviewCombatEngine()
        own = engine._fighter_from_initial(
            FighterSnapshot(
                1, "甲", 20, 10, 10, 10, 10, 10, "稳扎稳打"
            ),
            200,
            None,
        )
        opponent = engine._fighter_from_initial(
            FighterSnapshot(
                2, "乙", 20, 10, 10, 10, 10, 10, "稳扎稳打"
            ),
            800,
            None,
        )
        state = BattleState(1, own, opponent, [], 7)

        with mock.patch.object(
            engine.ability_runtime, "action_blocked", return_value=True
        ) as action_blocked:
            intent = engine._intent_for_phase(
                state,
                own,
                opponent,
                AIProfile(),
                AIProfile(),
                None,
                random.Random(1),
            )

        self.assertEqual(intent.action, "stunned")
        action_blocked.assert_called_once_with(own)


class KeyedEntropyTests(unittest.TestCase):
    def setUp(self):
        self.first = KeyedEntropy("sideview-v11", 20260810)
        self.second = KeyedEntropy("sideview-v11", 20260810)
        self.coordinates = {
            "stream": "combat.hit",
            "tick": 37,
            "actor": "qq:10001",
            "action_seq": 4,
            "subindex": 2,
        }

    def test_cross_instance_reproduction_for_all_draw_shapes(self):
        self.assertEqual(
            self.first.random(**self.coordinates),
            self.second.random(**self.coordinates),
        )
        self.assertEqual(
            self.first.uniform(0.92, 1.08, **self.coordinates),
            self.second.uniform(0.92, 1.08, **self.coordinates),
        )
        self.assertEqual(
            self.first.randint(1, 20, **self.coordinates),
            self.second.randint(1, 20, **self.coordinates),
        )
        self.assertEqual(
            self.first.choice(("hit", "miss", "graze"), **self.coordinates),
            self.second.choice(("hit", "miss", "graze"), **self.coordinates),
        )
        self.assertEqual(
            self.first.weighted_choice(
                ("common", "rare", "legendary"),
                (80, 18, 2),
                **self.coordinates,
            ),
            self.second.weighted_choice(
                ("common", "rare", "legendary"),
                (80, 18, 2),
                **self.coordinates,
            ),
        )

    def test_named_streams_are_isolated(self):
        hit = self.first.random(
            stream="combat.hit", tick=10, actor=1, action_seq=3,
        )
        critical = self.first.random(
            stream="combat.critical", tick=10, actor=1, action_seq=3,
        )
        reward = self.first.random(
            stream="reward.loot", tick=10, actor=1, action_seq=3,
        )

        self.assertEqual(len({hit, critical, reward}), 3)

    def test_adding_an_unrelated_draw_does_not_shift_existing_draws(self):
        hit_before = self.first.random(
            stream="combat.hit", tick=50, actor="a", action_seq=8,
        )

        self.first.weighted_choice(
            ("plain", "dramatic"),
            (3, 1),
            stream="narration.flavour",
            tick=50,
            actor="a",
            action_seq=8,
        )

        hit_after = self.first.random(
            stream="combat.hit", tick=50, actor="a", action_seq=8,
        )
        self.assertEqual(hit_before, hit_after)

    def test_subindex_addresses_multiple_draws_without_hidden_state(self):
        draws = [
            self.first.random(
                stream="combat.damage",
                tick=5,
                actor="a",
                action_seq=1,
                subindex=index,
            )
            for index in range(4)
        ]

        self.assertEqual(len(set(draws)), 4)
        self.assertEqual(
            draws,
            [
                self.second.random(
                    stream="combat.damage",
                    tick=5,
                    actor="a",
                    action_seq=1,
                    subindex=index,
                )
                for index in range(4)
            ],
        )

    def test_choice_validation_and_bounds(self):
        self.assertGreaterEqual(
            self.first.uniform(-2, 3, stream="bounds"),
            -2,
        )
        self.assertLess(
            self.first.uniform(-2, 3, stream="bounds"),
            3,
        )
        self.assertIn(self.first.randint(2, 4, stream="integer"), (2, 3, 4))
        with self.assertRaises(IndexError):
            self.first.choice((), stream="empty")
        with self.assertRaises(ValueError):
            self.first.weighted_choice(("a",), (0,), stream="zero")
        with self.assertRaises(ValueError):
            self.first.weighted_choice(("a", "b"), (1,), stream="length")
        with self.assertRaises(ValueError):
            self.first.randint(3, 2, stream="reversed")


if __name__ == "__main__":
    unittest.main()
