import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from scripts.v11_balance_lab import (
    BUILD_SPECS,
    _diagnostics,
    _compatible_abilities,
    _grow_spell,
    _has_star_type,
    _mirror_analysis,
    _one_shot,
    build_reachable_snapshot,
    run_lab,
)
from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS
from services.combat_ruleset import SIDEVIEW_V11_RULESET
from services.progression_rules import (
    decay_skill_potential,
    decay_spell_potential,
)


class V11BalanceLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One seed still exercises every directed build/family pairing, every
        # mirror, every production build resolver, and the complete report.
        cls.first = run_lab(
            seed_count=1,
            levels=(10,),
            workers=1,
            base_seed=20260810,
        )
        cls.second = run_lab(
            seed_count=1,
            levels=(10,),
            workers=1,
            base_seed=20260810,
        )

    def test_small_seed_report_is_exactly_deterministic(self):
        self.assertEqual(self.first, self.second)
        self.assertEqual(
            json.dumps(self.first, ensure_ascii=False, sort_keys=True),
            json.dumps(self.second, ensure_ascii=False, sort_keys=True),
        )

    def test_one_shot_counts_the_full_first_damage_tick(self):
        result = SimpleNamespace(
            events=[
                SimpleNamespace(
                    kind="damage", target_pk=2, tick=5, remaining_hp=30
                ),
                SimpleNamespace(
                    kind="followup", target_pk=2, tick=5, remaining_hp=0
                ),
            ]
        )
        self.assertTrue(_one_shot(result))

        result.events[1] = SimpleNamespace(
            kind="summon_strike", target_pk=2, tick=6, remaining_hp=0
        )
        self.assertFalse(_one_shot(result))

    def test_samples_obey_production_construction_constraints(self):
        tier = self.first["levels"]["10"]
        self.assertEqual(
            set(tier["reachable_builds"]),
            {spec.slug for spec in BUILD_SPECS},
        )
        for build in tier["reachable_builds"].values():
            reachability = build["construction_evidence"]
            self.assertTrue(
                reachability["construction_constraints_satisfied"]
            )
            self.assertEqual(
                reachability["production_acquisition_reachability"],
                "unverified",
            )
            self.assertEqual(
                reachability["stat_points_spent"],
                reachability["stat_points_budget"],
            )
            self.assertLessEqual(
                reachability["skill_points_spent_to_learn"],
                reachability["skill_points_budget"],
            )
            self.assertFalse(
                reachability["synthetic_level_scaled_resistance"]
            )
            self.assertEqual(
                reachability["resistance_source"],
                "catalog_materials_and_affixes_only",
            )
            self.assertTrue(build["equipment"])
            for item in build["equipment"]:
                self.assertEqual(item["source"], "assets/equipment_catalog.json")
                self.assertLessEqual(item["item_level"], 10)

            trained = [
                source
                for source in build["skills"].values()
                if source["raw_usage_per_encounter"] > 0
            ]
            self.assertTrue(trained)
            # A dead matrix used skill_level == character_level.  At level 10,
            # the actual daily pacing/potential simulation is far below that.
            self.assertTrue(all(source["level"] < 10 for source in trained))
            self.assertTrue(
                all(
                    source["growth_ruleset"]
                    == "elona-skill-potential-v11"
                    for source in trained
                )
            )

    def test_report_exposes_design_metrics_and_flags_without_pass_boolean(self):
        tier = self.first["levels"]["10"]
        mirror = tier["mirror_side_bias"]
        self.assertIn("aggregate_absolute_side_bias", mirror)
        self.assertIn(
            "aggregate_attacker_win_rate_wilson_95_ci", mirror
        )
        self.assertEqual(len(mirror["per_build"]), len(BUILD_SPECS))
        for metric in mirror["per_build"].values():
            interval = metric["attacker_win_rate_wilson_95_ci"]
            self.assertEqual(set(interval), {"lower", "upper"})
            self.assertLessEqual(interval["lower"], interval["upper"])

        for section in (
            "build_balance",
            "neutral_build_balance",
            "strategy_family_balance",
        ):
            data = tier[section]
            self.assertIn("entities", data)
            self.assertIn("dominance_edges", data)
            self.assertIn("usage_projection", data)
            self.assertAlmostEqual(
                sum(data["usage_projection"]["rates"].values()),
                1.0,
                places=5,
            )

        self.assertEqual(
            tier["build_balance"]["evaluation_mode"],
            "native_tactic_per_build",
        )
        self.assertEqual(
            tier["neutral_build_balance"]["evaluation_mode"],
            "shared_sustain_control",
        )
        self.assertEqual(
            tier["build_balance"]["tactic_assignment"],
            {
                spec.slug: spec.native_family.value
                for spec in BUILD_SPECS
            },
        )
        self.assertEqual(
            set(tier["native_minus_neutral_win_rate"]),
            {spec.slug for spec in BUILD_SPECS},
        )
        self.assertIn(
            "native tactic family",
            self.first["methodology"]["build_tactic_sampling"],
        )

        runtime = tier["combat_runtime"]
        self.assertEqual(set(runtime["ttk"]), {"p10", "p50", "p90"})
        self.assertIn("timeout", runtime)
        self.assertIn("one_shot", runtime)
        self.assertIn("resource_exhaustion", runtime)
        self.assertIn("fortune", runtime)
        self.assertEqual(
            set(runtime["environment_impact"]),
            {
                "calm", "rain", "fog", "strong_wind",
                "close_quarters", "mana_tide", "ether_disturbance",
            },
        )
        self.assertTrue(
            all(
                metric["sample_count"] > 0
                for metric in runtime["environment_impact"].values()
            )
        )
        self.assertIn(
            "every supported environment",
            self.first["methodology"]["environment_sampling"],
        )
        environment_counts = [
            metric["sample_count"]
            for metric in runtime["environment_impact"].values()
        ]
        self.assertEqual(
            sum(environment_counts),
            len(BUILD_SPECS) * (len(BUILD_SPECS) - 1),
        )
        self.assertLessEqual(
            max(environment_counts) - min(environment_counts),
            1,
        )
        self.assertTrue(
            all(
                finding["severity"] in {"red", "amber"}
                for finding in tier["diagnostics"]
            )
        )
        serialized = json.dumps(self.first, ensure_ascii=False)
        self.assertNotIn('"passed"', serialized)
        self.assertNotIn('"pass"', serialized)

    def test_equipment_cohorts_expose_matched_curve_and_natural_variance(self):
        report = run_lab(
            seed_count=2,
            levels=(10,),
            workers=1,
            base_seed=20260811,
            equipment_cohort_count=2,
        )
        tier = report["levels"]["10"]
        analysis = tier["equipment_cohort_analysis"]
        self.assertEqual(analysis["cohort_count"], 2)
        self.assertEqual(
            analysis["combat_seeds_per_cohort"]["counts"], [1, 1]
        )
        self.assertEqual(
            set(analysis["paired_construction_system_curve"]["build_win_rates"]),
            {spec.slug for spec in BUILD_SPECS},
        )
        for spec in BUILD_SPECS:
            cohorts = tier["equipment_cohort_provenance"][spec.slug]
            self.assertEqual(len(cohorts), 2)
            self.assertEqual(
                [source["equipment_cohort"] for source in cohorts],
                [0, 1],
            )
            variance = analysis["constructed_catalog_roll_variance"][spec.slug]
            self.assertEqual(len(variance["cohorts"]), 2)
            self.assertEqual(
                set(variance["cohort_win_rate_distribution"]),
                {"p10", "p50", "p90", "min", "max"},
            )

    def test_ttk_diagnostic_uses_the_v11_ruleset_window(self):
        tier = self.first["levels"]["10"]
        runtime = deepcopy(tier["combat_runtime"])
        runtime["ttk"]["p50"] = (
            SIDEVIEW_V11_RULESET.tempo.target_median_ticks_high + 1
        )
        diagnostics = _diagnostics(
            tier["mirror_side_bias"],
            tier["build_balance"],
            tier["strategy_family_balance"],
            runtime,
            target_ttk=(
                SIDEVIEW_V11_RULESET.tempo.target_median_ticks_low,
                SIDEVIEW_V11_RULESET.tempo.target_median_ticks_high,
            ),
        )
        finding = next(
            item for item in diagnostics
            if item["code"] == "ttk_median"
        )
        self.assertEqual(
            finding["design_reference"],
            (
                f"{SIDEVIEW_V11_RULESET.tempo.target_median_ticks_low} .. "
                f"{SIDEVIEW_V11_RULESET.tempo.target_median_ticks_high} ticks"
            ),
        )

    def test_mirror_diagnostic_uses_wilson_interval_not_point_estimate(self):
        def mirror_with_wins(wins, sample_count):
            observations = [
                {"attacker": "sample", "attacker_won": index < wins}
                for index in range(sample_count)
            ]
            return _mirror_analysis(observations, ("sample",))

        builds = {"entities": {}}
        strategies = {"entities": {}}
        runtime = {
            "sample_count": 0,
            "ttk": {"p50": 80},
            "timeout": {"rate": 0.0},
            "one_shot": {"rate": 0.0},
            "fortune": {"trigger_count": 0},
            "environment_impact": {},
        }

        uncertain = mirror_with_wins(6, 10)
        self.assertEqual(uncertain["aggregate_attacker_win_rate"], 0.6)
        uncertain_interval = uncertain[
            "aggregate_attacker_win_rate_wilson_95_ci"
        ]
        self.assertLessEqual(uncertain_interval["lower"], 0.55)
        uncertain_findings = _diagnostics(
            uncertain, builds, strategies, runtime, target_ttk=(60, 100)
        )
        self.assertFalse(any(
            item["code"] == "mirror_side_bias"
            for item in uncertain_findings
        ))

        biased = mirror_with_wins(70, 100)
        biased_interval = biased["per_build"]["sample"][
            "attacker_win_rate_wilson_95_ci"
        ]
        self.assertGreater(biased_interval["lower"], 0.55)
        biased_findings = _diagnostics(
            biased, builds, strategies, runtime, target_ttk=(60, 100)
        )
        mirror_findings = [
            item for item in biased_findings
            if item["code"] == "mirror_side_bias"
        ]
        self.assertEqual(len(mirror_findings), 1)
        observed = mirror_findings[0]["observed"]["sample"]
        self.assertEqual(observed["attacker_win_rate"], 0.7)
        self.assertEqual(observed["absolute_side_bias"], 0.2)

    def test_non_star_markers_are_normalized_before_counting(self):
        for value in (None, "", "   ", "normal", " Normal ", "NONE", " none "):
            with self.subTest(value=value):
                self.assertFalse(_has_star_type(value))
        for value in ("legendary", " artifact ", "star"):
            with self.subTest(value=value):
                self.assertTrue(_has_star_type(value))

    def test_auto_loadout_respects_exclusive_groups_and_keeps_ranged_output(self):
        ranger = next(spec for spec in BUILD_SPECS if spec.slug == "ranger")
        snapshot, _ = build_reachable_snapshot(ranger, 100, owner_pk=99101)
        active_ids = _compatible_abilities(
            ranger,
            snapshot.equipment,
            snapshot.skills.skills,
            snapshot.skills.spells,
        )

        self.assertIn("split_arrow", active_ids)
        self.assertNotIn("thorn_arrow", active_ids)
        groups = [
            ACTIVE_ABILITY_DEFINITIONS[ability_id].exclusive_group
            for ability_id in active_ids
            if ACTIVE_ABILITY_DEFINITIONS[ability_id].exclusive_group
        ]
        self.assertEqual(len(groups), len(set(groups)))
        self.assertIn("wind_spirit", active_ids)

    def test_spell_growth_uses_the_spell_potential_decay_curve(self):
        with patch(
            "scripts.v11_balance_lab._training_encounters",
            return_value=13,
        ):
            spell, _ = _grow_spell(
                "fire_bolt",
                school_level=50,
                target_level=2,
                unlock_level=1,
                willpower=100,
            )

        self.assertEqual(spell.level, 2)
        self.assertEqual(spell.potential, decay_spell_potential(100))
        self.assertNotEqual(spell.potential, decay_skill_potential(100))


if __name__ == "__main__":
    unittest.main()
