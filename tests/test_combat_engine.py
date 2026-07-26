import unittest

from models.combat import AIProfile, FighterSnapshot
from services.battle_report import BattleReportBuilder
from services.combat_ai import STRATEGY_PROFILES
from services.combat_engine import SideviewCombatEngine
from services import config


def _fighter(user_pk: int, name: str, strategy: str, **stats) -> FighterSnapshot:
    values = {"hp": 10, "atk": 5, "defense": 5, "speed": 5, "luck": 5}
    values.update(stats)
    return FighterSnapshot(
        user_pk=user_pk,
        name=name,
        level=1,
        strategy=strategy,
        **values,
    )


class SideviewCombatEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = SideviewCombatEngine()
        self.attacker = _fighter(1, "攻击方", "稳扎稳打")
        self.defender = _fighter(2, "防守方", "防守反击")

    def test_same_seed_replays_identically(self):
        first = self.engine.simulate(
            self.attacker,
            self.defender,
            STRATEGY_PROFILES[self.attacker.strategy],
            STRATEGY_PROFILES[self.defender.strategy],
            20260722,
        )
        second = self.engine.simulate(
            self.attacker,
            self.defender,
            STRATEGY_PROFILES[self.attacker.strategy],
            STRATEGY_PROFILES[self.defender.strategy],
            20260722,
        )
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_positions_stay_in_field_and_attacks_are_in_range(self):
        result = self.engine.simulate(
            self.attacker,
            self.defender,
            STRATEGY_PROFILES[self.attacker.strategy],
            STRATEGY_PROFILES[self.defender.strategy],
            11,
        )
        positions = {1: self.engine.ATTACKER_START, 2: self.engine.DEFENDER_START}
        for event in result.events:
            if event.kind in {"move", "attack_lunge", "knockback"}:
                moved_pk = event.target_pk if event.kind == "knockback" else event.actor_pk
                positions[moved_pk] = event.position
                self.assertGreaterEqual(event.position, self.engine.FIELD_MIN)
                self.assertLessEqual(event.position, self.engine.FIELD_MAX)
            if event.kind == "damage":
                self.assertLessEqual(abs(positions[1] - positions[2]), self.engine.ATTACK_RANGE)
        self.assertGreaterEqual(positions[2] - positions[1], self.engine.MIN_DISTANCE)

    def test_timeout_uses_hp_ratio_tiebreak(self):
        always_guard = AIProfile(
            aggression=0.0,
            guard_tendency=1.0,
            chase_tendency=1.0,
            preferred_range=90,
            retreat_tendency=0.0,
            low_hp_risk=0.0,
        )
        result = self.engine.simulate(
            _fighter(1, "高血", "test", hp=20),
            _fighter(2, "普通", "test", hp=10),
            always_guard,
            always_guard,
            19,
        )
        self.assertEqual(result.duration_ticks, self.engine.MAX_TICKS)
        self.assertEqual(result.finish_reason, "timeout_hp_ratio")
        # Equal remaining ratios fall through to equal damage, speed and seeded luck.
        self.assertIn(result.winner_pk, {1, 2})

    def test_every_builtin_strategy_can_simulate(self):
        self.assertEqual(set(config.BATTLE_STRATEGY_NAMES), set(STRATEGY_PROFILES))
        for index, strategy in enumerate(config.BATTLE_STRATEGY_NAMES):
            with self.subTest(strategy=strategy):
                result = self.engine.simulate(
                    _fighter(1, "甲", strategy),
                    self.defender,
                    STRATEGY_PROFILES[strategy],
                    STRATEGY_PROFILES[self.defender.strategy],
                    index,
                )
                self.assertIn(result.winner_pk, {1, 2})
                self.assertLessEqual(result.duration_ticks, self.engine.MAX_TICKS)

    def test_report_has_compact_event_lines_without_forced_emoji(self):
        result = self.engine.simulate(
            self.attacker,
            self.defender,
            STRATEGY_PROFILES[self.attacker.strategy],
            STRATEGY_PROFILES[self.defender.strategy],
            42,
        )
        lines = BattleReportBuilder().build(result)
        self.assertGreaterEqual(len(lines), 6)
        self.assertLessEqual(len(lines), 10)
        allowed = ("⚔️", "🏃", "🛡️", "💥", "✨", "❤️‍🔥", "🏆", "⏱️")
        self.assertFalse(any(emoji in "".join(lines) for emoji in allowed))
        self.assertNotIn("Tick", "".join(lines))
        self.assertIn("秒", "".join(lines))
        self.assertIn("击退", "".join(lines))
        self.assertIn("硬直", "".join(lines))
        self.assertIn("攻击方", lines[-1] + "".join(lines))

    def test_image_renderer_keeps_composite_emoji_as_one_text_unit(self):
        from services.battle_image_renderer import RENDERER_REVISION, _text_units

        self.assertEqual("astrbot-card-v2", RENDERER_REVISION)
        self.assertEqual(
            ["甲", "❤️‍🔥", "乙", "⚔️"],
            _text_units("甲❤️‍🔥乙⚔️"),
        )


if __name__ == "__main__":
    unittest.main()
