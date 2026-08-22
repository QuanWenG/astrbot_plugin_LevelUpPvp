import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.elona_balance_matrix import (
    ARCHETYPES,
    LEVELS,
    run_benchmark,
    run_parallel_benchmark,
)
from services.combat_engine import SideviewCombatEngine


class ElonaBalanceMatrixTests(unittest.TestCase):
    def test_matrix_is_reproducible_and_covers_all_archetypes(self):
        first = run_benchmark(seed_count=2)
        second = run_benchmark(seed_count=2)
        self.assertEqual(first, second)
        self.assertEqual(
            first["engine_version"], SideviewCombatEngine.ENGINE_VERSION
        )
        self.assertEqual(set(first["tiers"]), {str(level) for level in LEVELS})
        self.assertEqual(set(first["archetypes"]), set(ARCHETYPES))
        for tier in first["tiers"].values():
            self.assertEqual(len(tier["aggregate_rates"]), 10)
            self.assertEqual(len(tier["directed_rates"]), 90)
            self.assertEqual(len(tier["mirror_attacker_rates"]), 10)
            self.assertIn("timeout_rate", tier)
            self.assertIn("shape_violations", tier)

    def test_timeout_rate_counts_engine_timeout_reason_family(self):
        timeout_result = SimpleNamespace(
            winner_pk=1,
            duration_ticks=SideviewCombatEngine.MAX_TICKS,
            finish_reason="timeout_remaining_hp",
        )
        with patch.object(
            SideviewCombatEngine,
            "simulate",
            return_value=timeout_result,
        ):
            report = run_benchmark(seed_count=1, levels=(1,))

        self.assertEqual(report["tiers"]["1"]["timeout_rate"], 1.0)

    def test_parallel_runner_matches_sequential_runner(self):
        self.assertEqual(
            run_parallel_benchmark(seed_count=1, max_workers=2),
            run_benchmark(seed_count=1),
        )


if __name__ == "__main__":
    unittest.main()
