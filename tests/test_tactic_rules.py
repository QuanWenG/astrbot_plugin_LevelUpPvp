import dataclasses
import unittest

from services import config
from services.tactic_rules import (
    COUNTER_MATRIX,
    COUNTER_SP_COST_CAP,
    EDGE_CAP,
    FIT_CAP,
    GUARD_LOGIT_CAP,
    INITIATIVE_CAP,
    LEGACY_STRATEGY_FAMILIES,
    UTILITY_CAP,
    BuildSignals,
    CombatPhase,
    PhaseTacticGain,
    TacticFamily,
    TacticPlan,
    build_fit,
    counter_value,
    explain_counter,
    family_for_legacy_strategy,
    phase_for_state,
    phase_gain,
    resolve_plan_phase,
    tactical_edge,
    tactical_edge_for_builds,
)


class LegacyTacticMigrationTests(unittest.TestCase):
    def test_all_eighteen_builtin_strategies_are_mapped_once(self):
        self.assertEqual(len(LEGACY_STRATEGY_FAMILIES), 18)
        self.assertEqual(
            set(LEGACY_STRATEGY_FAMILIES),
            set(config.BATTLE_STRATEGY_NAMES),
        )
        self.assertEqual(set(LEGACY_STRATEGY_FAMILIES.values()), set(TacticFamily))

    def test_single_legacy_strategy_migrates_to_all_three_phases(self):
        plan = TacticPlan.from_legacy("速度拉扯")

        self.assertEqual(plan.opening, TacticFamily.SKIRMISH)
        self.assertEqual(plan.midgame, TacticFamily.SKIRMISH)
        self.assertEqual(plan.endgame, TacticFamily.SKIRMISH)

    def test_unknown_free_text_has_safe_fallback_and_optional_strict_mode(self):
        self.assertEqual(
            family_for_legacy_strategy("我的自定义策略"),
            TacticFamily.SUSTAIN,
        )
        custom_fallback = TacticPlan.from_legacy(
            "我的自定义策略",
            default=TacticFamily.GAMBIT,
        )
        self.assertEqual(custom_fallback.opening, TacticFamily.GAMBIT)
        with self.assertRaises(ValueError):
            TacticPlan.from_legacy("我的自定义策略", strict=True)

    def test_plan_accepts_enum_english_and_chinese_family_names(self):
        plan = TacticPlan("pressure", "游击", TacticFamily.CONTROL)

        self.assertEqual(plan.for_phase("开局"), TacticFamily.PRESSURE)
        self.assertEqual(plan.for_phase("midgame"), TacticFamily.SKIRMISH)
        self.assertEqual(plan.for_phase(CombatPhase.ENDGAME), TacticFamily.CONTROL)


class CounterMatrixTests(unittest.TestCase):
    def test_matrix_is_complete_antisymmetric_and_ternary(self):
        for own in TacticFamily:
            self.assertEqual(set(COUNTER_MATRIX[own]), set(TacticFamily))
            for opponent in TacticFamily:
                value = counter_value(own, opponent)
                self.assertIn(value, {-1, 0, 1})
                self.assertEqual(value, -counter_value(opponent, own))

    def test_every_family_has_two_wins_two_losses_and_a_nonmirror_neutral(self):
        for own in TacticFamily:
            values = [
                counter_value(own, opponent)
                for opponent in TacticFamily
                if opponent != own
            ]
            self.assertEqual(values.count(1), 2, own)
            self.assertEqual(values.count(-1), 2, own)
            self.assertEqual(values.count(0), 1, own)
            self.assertEqual(counter_value(own, own), 0)

    def test_known_and_neutral_matchups_match_the_design(self):
        self.assertEqual(counter_value("反制", "压制"), 1)
        self.assertEqual(counter_value("压制", "反制"), -1)
        self.assertEqual(counter_value("压制", "游击"), 0)

    def test_explanations_are_player_readable_for_win_loss_and_neutral(self):
        self.assertIn("反制克制压制", explain_counter("counter", "pressure"))
        self.assertIn("被反制克制", explain_counter("pressure", "counter"))
        self.assertIn("互不克制", explain_counter("pressure", "skirmish"))
        self.assertIn("同路对局", explain_counter("control", "control"))


class PhaseTests(unittest.TestCase):
    def test_phase_boundaries_preserve_opening_then_hp_or_tick_endgame(self):
        self.assertEqual(phase_for_state(0, 0.1, 0.1), CombatPhase.OPENING)
        self.assertEqual(phase_for_state(30, 0.1, 0.1), CombatPhase.OPENING)
        self.assertEqual(phase_for_state(31, 1.0, 1.0), CombatPhase.MIDGAME)
        self.assertEqual(phase_for_state(31, 0.45, 1.0), CombatPhase.ENDGAME)
        self.assertEqual(phase_for_state(100, 1.0, 1.0), CombatPhase.MIDGAME)
        self.assertEqual(phase_for_state(101, 1.0, 1.0), CombatPhase.ENDGAME)


class BuildFitAndEdgeTests(unittest.TestCase):
    def test_neutral_signals_produce_zero_fit_and_zero_mirror_edge(self):
        for family in TacticFamily:
            self.assertAlmostEqual(build_fit(family, BuildSignals()), 0.0)
            self.assertAlmostEqual(tactical_edge(family, family), 0.0)

    def test_tanh_fit_is_bounded_and_rewards_matching_build(self):
        pressure_build = BuildSignals(
            burst=1,
            mobility=1,
            endurance=0,
            variance=1,
        )
        aligned = build_fit(TacticFamily.PRESSURE, pressure_build)
        mismatched = build_fit(
            TacticFamily.PRESSURE,
            BuildSignals(burst=0, mobility=0, endurance=1, variance=0),
        )

        self.assertGreater(aligned, 0)
        self.assertLess(mismatched, 0)
        self.assertLessEqual(abs(aligned), FIT_CAP)
        self.assertLessEqual(abs(mismatched), FIT_CAP)
        self.assertLessEqual(
            abs(build_fit("gambit", {"variance": 999, "endurance": -999})),
            FIT_CAP,
        )

    def test_edge_is_bounded_and_antisymmetric_including_fit(self):
        own_fit = build_fit("counter", {"retaliation": 1, "endurance": 1})
        opponent_fit = build_fit("pressure", {"burst": 1, "mobility": 1})
        forward = tactical_edge(
            "counter",
            "pressure",
            own_fit=own_fit,
            opponent_fit=opponent_fit,
        )
        reverse = tactical_edge(
            "pressure",
            "counter",
            own_fit=opponent_fit,
            opponent_fit=own_fit,
        )

        self.assertAlmostEqual(forward, -reverse)
        self.assertLessEqual(abs(forward), EDGE_CAP)
        self.assertLessEqual(
            abs(tactical_edge("counter", "pressure", own_fit=99, opponent_fit=-99)),
            EDGE_CAP,
        )

    def test_build_convenience_function_keeps_neutral_noncounter_neutral(self):
        edge = tactical_edge_for_builds(
            "pressure",
            "skirmish",
            BuildSignals(),
            BuildSignals(),
        )
        self.assertAlmostEqual(edge, 0.0)


class PhaseGainTests(unittest.TestCase):
    def test_zero_edge_is_a_truly_neutral_gain(self):
        for phase in CombatPhase:
            for family in TacticFamily:
                self.assertEqual(phase_gain(phase, family, 0), PhaseTacticGain())

    def test_gain_fields_are_limited_to_decision_and_action_economy(self):
        field_names = {field.name for field in dataclasses.fields(PhaseTacticGain)}
        self.assertEqual(
            field_names,
            {"utility", "guard_logit", "initiative", "counter_sp_cost"},
        )
        self.assertNotIn("damage", field_names)
        self.assertNotIn("damage_multiplier", field_names)

    def test_every_gain_is_clamped_to_its_contract(self):
        for phase in CombatPhase:
            for family in TacticFamily:
                for edge in (-999, -EDGE_CAP, EDGE_CAP, 999):
                    gain = phase_gain(phase, family, edge)
                    self.assertLessEqual(abs(gain.utility), UTILITY_CAP)
                    self.assertLessEqual(abs(gain.guard_logit), GUARD_LOGIT_CAP)
                    self.assertLessEqual(abs(gain.initiative), INITIATIVE_CAP)
                    self.assertLessEqual(
                        abs(gain.counter_sp_cost),
                        COUNTER_SP_COST_CAP,
                    )

    def test_resolution_uses_each_plans_family_for_the_requested_phase(self):
        own = TacticPlan("pressure", "control", "gambit")
        opponent = TacticPlan("sustain", "counter", "skirmish")
        result = resolve_plan_phase(own, opponent, CombatPhase.ENDGAME)

        self.assertEqual(result.phase, CombatPhase.ENDGAME)
        self.assertEqual(result.own_family, TacticFamily.GAMBIT)
        self.assertEqual(result.opponent_family, TacticFamily.SKIRMISH)
        self.assertEqual(result.matchup, 1)
        self.assertGreater(result.edge, 0)
        self.assertIsInstance(result.gain, PhaseTacticGain)
        self.assertIn("奇策克制游击", result.explanation)


if __name__ == "__main__":
    unittest.main()
