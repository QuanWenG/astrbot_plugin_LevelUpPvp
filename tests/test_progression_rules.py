import unittest

from services.progression_rules import (
    EXP_SCALE,
    attribute_exp_required,
    decay_attribute_potential,
    decay_skill_potential,
    migrate_exp_preserving_progress,
    potential_recovery_per_point,
    recover_potential,
    scaled_exp_gain,
    skill_exp_required,
    skill_level_cap,
    spell_exp_required,
    spell_level_cap,
)


class ProgressionRuleTests(unittest.TestCase):
    def test_scaled_requirements_match_calibration_milestones(self):
        self.assertEqual(attribute_exp_required(1), 11640)
        self.assertEqual(attribute_exp_required(10), 30000)
        self.assertEqual(attribute_exp_required(20), 58000)
        self.assertEqual(skill_exp_required(1), 6230)
        self.assertEqual(skill_exp_required(10), 20000)
        self.assertEqual(skill_exp_required(20), 41000)
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

    def test_potential_decay_and_recovery_use_mobile_structure(self):
        self.assertEqual(decay_skill_potential(200), 180)
        self.assertEqual(decay_skill_potential(1), 1)
        self.assertEqual(decay_attribute_potential(100), 96)
        self.assertEqual(potential_recovery_per_point(100), 18)
        self.assertEqual(potential_recovery_per_point(250), 4)
        self.assertEqual(recover_potential(100), 118)
        self.assertEqual(recover_potential(399), 400)

    def test_progress_migration_preserves_fraction_and_clamps(self):
        self.assertEqual(migrate_exp_preserving_progress(50, 100, 10000), 5000)
        self.assertEqual(migrate_exp_preserving_progress(999, 100, 10000), 9900)
        self.assertEqual(migrate_exp_preserving_progress(-5, 100, 10000), 0)

    def test_scaled_level_caps(self):
        self.assertEqual(skill_level_cap(5, 10), 36)
        self.assertEqual(skill_level_cap(1000, 1000), 100)
        self.assertEqual(spell_level_cap(1), 50)
        self.assertEqual(spell_level_cap(70), 70)
        self.assertEqual(spell_level_cap(150), 100)


if __name__ == "__main__":
    unittest.main()
