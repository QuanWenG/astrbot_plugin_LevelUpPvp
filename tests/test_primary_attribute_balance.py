import unittest

from scripts.primary_attribute_balance import (
    AGGREGATE_TARGET,
    MIRROR_TARGET,
    PAIR_TARGET,
    run_benchmark,
)


class PrimaryAttributeBalanceBenchmarkTests(unittest.TestCase):
    def test_fixed_seed_benchmark_is_reproducible_and_classifies_thresholds(self):
        first = run_benchmark(matrix_seed_count=2, mirror_seed_count=4)
        second = run_benchmark(matrix_seed_count=2, mirror_seed_count=4)

        self.assertEqual(first, second)
        self.assertEqual(first["targets"]["aggregate"], AGGREGATE_TARGET)
        self.assertEqual(first["targets"]["pair"], PAIR_TARGET)
        self.assertEqual(first["targets"]["mirror"], MIRROR_TARGET)
        self.assertEqual(set(first["tiers"]), {"5", "20", "50"})
        for tier in first["tiers"].values():
            self.assertEqual(len(tier["aggregate_rates"]), 7)
            self.assertEqual(len(tier["pair_rates"]), 21)
            self.assertEqual(len(tier["mirror_attacker_rates"]), 7)
            self.assertIn("aggregate_violations", tier)
            self.assertIn("pair_violations", tier)
            self.assertIn("mirror_violations", tier)


if __name__ == "__main__":
    unittest.main()
