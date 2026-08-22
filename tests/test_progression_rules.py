import unittest

from services.progression_rules import (
    EXP_SCALE,
    attribute_exp_required,
    character_catchup_multiplier,
    clamp_skill_potential,
    decay_attribute_potential,
    decay_skill_potential,
    decay_spell_potential,
    legacy_level_exp_required,
    level_daily_exp_budget,
    level_exp_required,
    migrate_exp_preserving_progress,
    migrate_level_exp_preserving_progress,
    migrate_v10_skill_exp_preserving_progress,
    potential_recovery_per_point,
    recover_potential,
    recover_skill_potential,
    scaled_exp_gain,
    scaled_skill_exp_gain,
    skill_exp_required,
    skill_level_cap,
    skill_potential_recovery_per_point,
    spell_exp_required,
    spell_level_cap,
    target_days_for_next_level,
    v10_skill_exp_required,
)


class ProgressionRuleTests(unittest.TestCase):
    def test_scaled_requirements_match_calibration_milestones(self):
        self.assertEqual(attribute_exp_required(1), 11640)
        self.assertEqual(attribute_exp_required(10), 30000)
        self.assertEqual(attribute_exp_required(20), 58000)
        self.assertEqual(skill_exp_required(1), 3308)
        self.assertEqual(skill_exp_required(10), 6800)
        self.assertEqual(skill_exp_required(20), 12200)
        self.assertEqual(spell_exp_required(1), 7435)
        self.assertEqual(spell_exp_required(10), 23500)
        self.assertEqual(spell_exp_required(20), 48000)

    def test_curves_are_monotonic_and_convex(self):
        for required in (
            attribute_exp_required,
            skill_exp_required,
            spell_exp_required,
        ):
            values = [required(level) for level in range(1, 101)]
            self.assertTrue(all(a < b for a, b in zip(values, values[1:])))
            differences = [b - a for a, b in zip(values, values[1:])]
            self.assertTrue(
                all(a <= b for a, b in zip(differences, differences[1:]))
            )

    def test_fixed_point_gain_preserves_one_percent_potential(self):
        self.assertEqual(EXP_SCALE, 100)
        self.assertEqual(scaled_exp_gain(1, 1), 1)
        self.assertEqual(scaled_exp_gain(20, 100), 2000)
        self.assertEqual(scaled_exp_gain(20, 400), 8000)

    def test_skill_gain_uses_a_bounded_effective_potential_band(self):
        self.assertEqual(clamp_skill_potential(1), 50)
        self.assertEqual(clamp_skill_potential(450), 200)
        self.assertEqual(scaled_skill_exp_gain(20, 1), 1000)
        self.assertEqual(scaled_skill_exp_gain(20, 100), 2000)
        self.assertEqual(scaled_skill_exp_gain(20, 450), 4000)

    def test_potential_decay_and_recovery_use_mobile_structure(self):
        self.assertEqual(decay_skill_potential(200), 184)
        self.assertEqual(decay_skill_potential(1), 50)
        self.assertEqual(decay_spell_potential(250), 240)
        self.assertEqual(decay_spell_potential(400), 384)
        self.assertEqual(decay_spell_potential(1), 1)
        self.assertEqual(decay_attribute_potential(100), 96)
        self.assertEqual(potential_recovery_per_point(100), 18)
        self.assertEqual(potential_recovery_per_point(250), 4)
        self.assertEqual(recover_potential(100), 118)
        self.assertEqual(recover_potential(399), 400)
        self.assertEqual(skill_potential_recovery_per_point(100), 25)
        self.assertEqual(recover_skill_potential(100), 125)
        self.assertEqual(recover_skill_potential(199), 200)

    def test_level_curve_is_budgeted_by_explicit_pacing_segments(self):
        self.assertAlmostEqual(
            sum(target_days_for_next_level(level) for level in range(1, 10)),
            7.0,
        )
        self.assertEqual(level_exp_required(1), 53)
        self.assertEqual(level_exp_required(10), 320)
        self.assertEqual(level_exp_required(29), 1096)
        self.assertEqual(level_exp_required(30), 1120)
        self.assertEqual(level_exp_required(59), 3632)
        self.assertEqual(level_exp_required(60), 3680)
        self.assertEqual(level_exp_required(99), 10410)
        values = [level_exp_required(level) for level in range(1, 100)]
        self.assertTrue(all(a < b for a, b in zip(values, values[1:])))
        for level in (10, 20, 29, 30, 45, 59, 60, 80, 99):
            actual_days = level_exp_required(level) / level_daily_exp_budget(
                level
            )
            self.assertAlmostEqual(
                actual_days,
                target_days_for_next_level(level),
                delta=0.01,
            )

    def test_group_catchup_is_soft_and_capped(self):
        self.assertEqual(character_catchup_multiplier(20, 10), 1.0)
        self.assertAlmostEqual(character_catchup_multiplier(20, 30), 1.2247, places=4)
        self.assertEqual(character_catchup_multiplier(1, 100), 1.6)

    def test_progress_migration_preserves_fraction_and_clamps(self):
        self.assertEqual(migrate_exp_preserving_progress(50, 100, 10000), 5000)
        self.assertEqual(migrate_exp_preserving_progress(999, 100, 10000), 9900)
        self.assertEqual(migrate_exp_preserving_progress(-5, 100, 10000), 0)
        old_required = legacy_level_exp_required(10)
        old_exp = old_required // 2
        migrated = migrate_level_exp_preserving_progress(10, old_exp)
        self.assertAlmostEqual(
            migrated / level_exp_required(10),
            old_exp / old_required,
            delta=0.005,
        )
        skill_old_required = v10_skill_exp_required(20)
        skill_old_exp = skill_old_required * 3 // 4
        skill_migrated = migrate_v10_skill_exp_preserving_progress(
            20,
            skill_old_exp,
        )
        self.assertAlmostEqual(
            skill_migrated / skill_exp_required(20),
            skill_old_exp / skill_old_required,
            delta=0.005,
        )

    def test_scaled_level_caps(self):
        self.assertEqual(skill_level_cap(5, 10), 36)
        self.assertEqual(skill_level_cap(1000, 1000), 100)
        self.assertEqual(spell_level_cap(1), 50)
        self.assertEqual(spell_level_cap(70), 70)
        self.assertEqual(spell_level_cap(150), 100)


if __name__ == "__main__":
    unittest.main()
