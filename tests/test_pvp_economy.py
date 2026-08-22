import unittest
from dataclasses import FrozenInstanceError, replace

from services.pvp_economy import (
    RatingPolicy,
    RewardContext,
    character_daily_budget,
    decide_pvp_economy,
)


def _context(**overrides):
    values = {
        "group_id": "group-1",
        "battle_date": "2026-08-10",
        "winner_id": "alice",
        "loser_id": "bob",
        "winner_level": 10,
        "loser_level": 10,
        "winner_checkin_days": 3,
        "loser_checkin_days": 3,
    }
    values.update(overrides)
    return RewardContext(**values)


class RatingPolicyTests(unittest.TestCase):
    def test_defaults_and_k_factor_boundary(self):
        policy = RatingPolicy()

        self.assertEqual(policy.initial_rating, 1000)
        self.assertEqual(policy.k_factor(0), 32)
        self.assertEqual(policy.k_factor(9), 32)
        self.assertEqual(policy.k_factor(10), 24)

    def test_equal_provisional_ratings_are_standard_zero_sum_elo(self):
        result = RatingPolicy().rate(
            winner_rating=1000,
            loser_rating=1000,
            winner_games_played=0,
            loser_games_played=0,
        )

        self.assertEqual(result.winner_expected_score, 0.5)
        self.assertEqual(result.k_factor, 32)
        self.assertEqual(result.winner_delta, 16)
        self.assertEqual(result.loser_delta, -16)
        self.assertIsInstance(result.winner_delta, int)

    def test_equal_established_ratings_use_k24(self):
        result = RatingPolicy().rate(
            winner_rating=1000,
            loser_rating=1000,
            winner_games_played=10,
            loser_games_played=100,
        )

        self.assertEqual(result.k_factor, 24)
        self.assertEqual((result.winner_delta, result.loser_delta), (12, -12))

    def test_either_provisional_player_calibrates_match_at_k32(self):
        result = RatingPolicy().rate(
            winner_rating=1200,
            loser_rating=1000,
            winner_games_played=100,
            loser_games_played=9,
        )

        self.assertEqual(result.k_factor, 32)
        self.assertEqual(result.winner_delta, -result.loser_delta)
        self.assertGreater(result.winner_expected_score, 0.5)
        self.assertLess(result.winner_delta, 16)

    def test_extreme_imported_ratings_do_not_overflow_or_break_zero_sum(self):
        policy = RatingPolicy()
        upset = policy.rate(
            winner_rating=-10**9,
            loser_rating=10**9,
            winner_games_played=10,
            loser_games_played=10,
        )
        expected_win = policy.rate(
            winner_rating=10**9,
            loser_rating=-10**9,
            winner_games_played=10,
            loser_games_played=10,
        )

        self.assertEqual((upset.winner_delta, upset.loser_delta), (24, -24))
        self.assertEqual(
            (expected_win.winner_delta, expected_win.loser_delta), (0, 0)
        )


class EligibilityAndAntiFarmTests(unittest.TestCase):
    def test_account_below_both_thresholds_can_only_spar(self):
        decision = decide_pvp_economy(
            _context(winner_level=4, winner_checkin_days=2)
        )

        self.assertFalse(decision.rated)
        self.assertEqual(decision.mode, "spar")
        self.assertEqual(decision.winner_rating_delta, 0)
        self.assertEqual(decision.loser_rating_delta, 0)
        self.assertEqual(decision.winner_exp_gain, 0)
        self.assertEqual(decision.loser_exp_gain, 0)
        self.assertIn("winner_account_not_qualified", decision.reasons)

    def test_level_or_checkin_threshold_each_qualifies(self):
        by_level = decide_pvp_economy(
            _context(
                winner_level=5,
                winner_checkin_days=0,
                loser_level=5,
                loser_checkin_days=0,
            )
        )
        by_checkin = decide_pvp_economy(
            _context(
                winner_level=1,
                winner_checkin_days=3,
                loser_level=1,
                loser_checkin_days=3,
            )
        )

        self.assertTrue(by_level.rated)
        self.assertTrue(by_checkin.rated)

    def test_only_first_unordered_pair_duel_each_day_is_rated_or_rewarded(self):
        first = decide_pvp_economy(_context(pair_battles_today=0))
        repeat = decide_pvp_economy(_context(pair_battles_today=1))

        self.assertTrue(first.rated)
        self.assertTrue(first.rewarded)
        self.assertFalse(repeat.rated)
        self.assertFalse(repeat.rewarded)
        self.assertEqual(repeat.loser_exp_loss, 0)
        self.assertIn("repeat_pair_today", repeat.reasons)

    def test_level_gap_above_ten_is_playable_only_as_spar(self):
        decision = decide_pvp_economy(
            _context(winner_level=25, loser_level=11)
        )

        self.assertFalse(decision.rated)
        self.assertFalse(decision.rewarded)
        self.assertEqual(decision.winner_rating_delta, 0)
        self.assertEqual(decision.loser_rating_delta, 0)
        self.assertIn("rated_level_gap_exceeded", decision.reasons)

    def test_level_gap_at_ten_is_still_rated(self):
        decision = decide_pvp_economy(
            _context(winner_level=20, loser_level=10)
        )

        self.assertTrue(decision.rated)

    def test_fourth_distinct_opponent_is_rated_but_grants_no_growth(self):
        decision = decide_pvp_economy(
            _context(
                winner_growth_opponents_today=3,
                loser_growth_opponents_today=3,
            )
        )

        self.assertTrue(decision.rated)
        self.assertEqual(decision.winner_exp_gain, 0)
        self.assertEqual(decision.loser_exp_gain, 0)
        self.assertIn(
            "winner_distinct_opponent_limit_reached", decision.reasons
        )
        self.assertIn(
            "loser_distinct_opponent_limit_reached", decision.reasons
        )

    def test_limit_is_per_participant(self):
        decision = decide_pvp_economy(
            _context(
                winner_growth_opponents_today=3,
                loser_growth_opponents_today=2,
            )
        )

        self.assertEqual(decision.winner_exp_gain, 0)
        self.assertGreater(decision.loser_exp_gain, 0)


class GrowthEconomyTests(unittest.TestCase):
    def test_same_level_uses_18_and_10_percent_daily_budget_shares(self):
        decision = decide_pvp_economy(_context())
        budget = character_daily_budget(10)

        self.assertEqual(decision.winner_exp_gain, round(budget * 0.18))
        self.assertEqual(decision.loser_exp_gain, round(budget * 0.10))
        self.assertEqual(decision.loser_exp_loss, 0)

    def test_level_gap_adjustment_is_bounded_to_plus_or_minus_twenty_percent(self):
        underdog = decide_pvp_economy(
            _context(winner_level=20, loser_level=30)
        )
        favorite = decide_pvp_economy(
            _context(winner_level=20, loser_level=10)
        )
        budget = character_daily_budget(20)

        self.assertEqual(
            underdog.winner_exp_gain,
            int(budget * 0.18 * 1.20 + 0.5),
        )
        self.assertEqual(
            favorite.winner_exp_gain,
            int(budget * 0.18 * 0.80 + 0.5),
        )

    def test_daily_budget_caps_each_participant_independently(self):
        winner_budget = character_daily_budget(10)
        loser_budget = character_daily_budget(10)
        decision = decide_pvp_economy(
            _context(
                winner_daily_exp_earned=winner_budget - 3,
                loser_daily_exp_earned=loser_budget,
            )
        )

        self.assertEqual(decision.winner_exp_gain, 3)
        self.assertEqual(decision.loser_exp_gain, 0)
        self.assertIn("winner_growth_granted_budget_capped", decision.reasons)
        self.assertIn("loser_daily_exp_budget_exhausted", decision.reasons)

    def test_exhausted_or_overreported_budget_never_creates_negative_exp(self):
        budget = character_daily_budget(10)
        decision = decide_pvp_economy(
            _context(
                winner_daily_exp_earned=budget + 999,
                loser_daily_exp_earned=budget + 999,
            )
        )

        self.assertEqual(decision.winner_exp_gain, 0)
        self.assertEqual(decision.loser_exp_gain, 0)
        self.assertEqual(decision.loser_exp_loss, 0)


class IdempotencyAndContractTests(unittest.TestCase):
    def test_key_parts_are_explicit_and_pair_order_is_stable(self):
        original = decide_pvp_economy(_context())
        reversed_outcome = decide_pvp_economy(
            _context(winner_id="bob", loser_id="alice")
        )

        self.assertEqual(
            original.reward_key_parts,
            (
                "pvp",
                "pvp-economy-v11",
                "group-1",
                "2026-08-10",
                "alice",
                "bob",
            ),
        )
        self.assertEqual(
            original.rating_reward_key,
            reversed_outcome.rating_reward_key,
        )
        self.assertEqual(
            original.winner_growth_reward_key,
            reversed_outcome.loser_growth_reward_key,
        )
        self.assertEqual(
            original.loser_growth_reward_key,
            reversed_outcome.winner_growth_reward_key,
        )

    def test_same_context_retries_make_identical_decisions(self):
        context = _context()
        self.assertEqual(
            decide_pvp_economy(context),
            decide_pvp_economy(context),
        )

    def test_context_and_decision_are_frozen(self):
        context = _context()
        decision = decide_pvp_economy(context)

        with self.assertRaises(FrozenInstanceError):
            context.winner_level = 99
        with self.assertRaises(FrozenInstanceError):
            decision.rated = False

    def test_invalid_identity_date_and_counter_are_rejected(self):
        with self.assertRaises(ValueError):
            _context(winner_id="alice", loser_id="alice")
        with self.assertRaises(ValueError):
            _context(battle_date="10/08/2026")
        with self.assertRaises(ValueError):
            _context(pair_battles_today=-1)

    def test_replacing_only_daily_count_does_not_change_reward_keys(self):
        context = _context()
        first = decide_pvp_economy(context)
        retry_after_counter_update = decide_pvp_economy(
            replace(context, pair_battles_today=1)
        )

        self.assertEqual(
            first.rating_reward_key,
            retry_after_counter_update.rating_reward_key,
        )
        self.assertFalse(retry_after_counter_update.rated)


if __name__ == "__main__":
    unittest.main()
