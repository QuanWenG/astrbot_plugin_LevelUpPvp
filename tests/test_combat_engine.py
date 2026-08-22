import unittest

from models.combat import (
    AIProfile,
    BattleEvent,
    BattleState,
    FighterSnapshot,
    FighterState,
    SimulationResult,
)
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

    def test_caller_can_force_a_known_environment(self):
        for index, (environment_id, _) in enumerate(
            self.engine.SUPPORTED_ENVIRONMENTS
        ):
            with self.subTest(environment_id=environment_id):
                result = self.engine.simulate(
                    self.attacker,
                    self.defender,
                    STRATEGY_PROFILES[self.attacker.strategy],
                    STRATEGY_PROFILES[self.defender.strategy],
                    20260811 + index,
                    environment_id=environment_id,
                )
                self.assertEqual(result.environment_id, environment_id)
                self.assertEqual(result.events[0].kind, "battle_context")
                self.assertEqual(result.events[0].status_id, environment_id)

    def test_default_random_environment_uses_only_rated_pvp_pool(self):
        self.assertEqual(
            self.engine.DEFAULT_RATED_ENVIRONMENTS,
            (
                ("calm", 60),
                ("rain", 15),
                ("fog", 15),
                ("strong_wind", 10),
            ),
        )
        rated_ids = {
            item[0] for item in self.engine.DEFAULT_RATED_ENVIRONMENTS
        }
        self.assertEqual(
            rated_ids,
            {"calm", "rain", "fog", "strong_wind"},
        )
        observed = set()
        for seed in range(96):
            result = self.engine.simulate(
                self.attacker,
                self.defender,
                STRATEGY_PROFILES[self.attacker.strategy],
                STRATEGY_PROFILES[self.defender.strategy],
                seed,
            )
            observed.add(result.environment_id)
        self.assertEqual(observed, rated_ids)

    def test_non_rated_caller_can_request_full_supported_random_pool(self):
        result = self.engine.simulate(
            self.attacker,
            self.defender,
            STRATEGY_PROFILES[self.attacker.strategy],
            STRATEGY_PROFILES[self.defender.strategy],
            20260812,
            random_environment_pool=(("ether_disturbance", 1),),
        )
        self.assertEqual(result.environment_id, "ether_disturbance")

    def test_forced_environment_rejects_unknown_id(self):
        with self.assertRaisesRegex(ValueError, "未知战斗环境"):
            self.engine.simulate(
                self.attacker,
                self.defender,
                STRATEGY_PROFILES[self.attacker.strategy],
                STRATEGY_PROFILES[self.defender.strategy],
                20260811,
                environment_id="imaginary_weather",
            )

    def test_random_environment_pool_rejects_unknown_or_duplicate_ids(self):
        for pool in (
            (("imaginary_weather", 1),),
            (("rain", 1), ("rain", 1)),
            (),
        ):
            with self.subTest(pool=pool), self.assertRaisesRegex(
                ValueError, "随机战斗环境池"
            ):
                self.engine.simulate(
                    self.attacker,
                    self.defender,
                    STRATEGY_PROFILES[self.attacker.strategy],
                    STRATEGY_PROFILES[self.defender.strategy],
                    20260812,
                    random_environment_pool=pool,
                )

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

    def test_timeout_uses_normalized_score_not_absolute_hp(self):
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
        self.assertLessEqual(result.duration_ticks, self.engine.MAX_TICKS)
        high_hp = FighterState(
            _fighter(1, "高血", "test", hp=20),
            250,
            self.engine.ATTACKER_START,
        )
        low_hp = FighterState(
            _fighter(2, "普通", "test", hp=10),
            150,
            self.engine.DEFENDER_START,
        )
        score_state = BattleState(1, high_hp, low_hp, [], 19)
        # Both are at 100% HP with identical pressure/resources; the score
        # deliberately ignores their different absolute max-HP values.
        self.assertEqual(
            self.engine._timeout_score(score_state, high_hp, low_hp),
            self.engine._timeout_score(score_state, low_hp, high_hp),
        )

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

    def test_report_surfaces_ability_payoffs_and_backlash(self):
        def report_for(event: BattleEvent) -> str:
            result = SimulationResult(
                attacker=self.attacker,
                defender=self.defender,
                winner_pk=self.attacker.user_pk,
                loser_pk=self.defender.user_pk,
                duration_ticks=20,
                finish_reason="knockout",
                attacker_remaining_hp=80,
                defender_remaining_hp=0,
                attacker_damage_dealt=60,
                defender_damage_dealt=20,
                events=(event,),
                random_seed=7,
                attacker_remaining_stamina=70,
                defender_remaining_stamina=40,
                attacker_remaining_mana=30,
                defender_remaining_mana=10,
            )
            return "\n".join(BattleReportBuilder().build(result))

        summon_report = report_for(
            BattleEvent(
                tick=8,
                kind="summon_strike",
                actor_pk=self.attacker.user_pk,
                target_pk=self.defender.user_pk,
                value=17,
                remaining_hp=23,
                skill_id="elm_blessing",
                damage_type="natural",
                damage_breakdown={"natural": 17},
            )
        )
        self.assertIn("召唤的守卫", summon_report)
        self.assertIn("榆树祝福", summon_report)

        life_steal_report = report_for(
            BattleEvent(
                tick=12,
                kind="life_steal",
                actor_pk=self.attacker.user_pk,
                target_pk=self.defender.user_pk,
                value=9,
                remaining_hp=72,
                skill_id="hell_breath",
            )
        )
        self.assertIn("汲取9点生命", life_steal_report)
        self.assertIn("地狱吐息", life_steal_report)

        ether_report = report_for(
            BattleEvent(
                tick=4,
                kind="mana_backlash",
                actor_pk=self.attacker.user_pk,
                target_pk=self.attacker.user_pk,
                value=6,
                remaining_hp=74,
                skill_id="magic_arrow",
                damage_type="magic",
                status_id="ether_disturbance",
            )
        )
        self.assertIn("反噬", ether_report)

        corrosion_report = report_for(
            BattleEvent(
                tick=6,
                kind="status_apply",
                actor_pk=self.attacker.user_pk,
                target_pk=self.defender.user_pk,
                value=25,
                skill_id="monster_corrosive_splash",
                status_id="defense_down",
            )
        )
        self.assertIn("护甲", corrosion_report)
        self.assertIn("暂时下降", corrosion_report)

    def test_image_renderer_keeps_composite_emoji_as_one_text_unit(self):
        from services.battle_image_renderer import RENDERER_REVISION, _text_units

        self.assertEqual("astrbot-card-v3-light", RENDERER_REVISION)
        self.assertEqual(
            ["甲", "❤️‍🔥", "乙", "⚔️"],
            _text_units("甲❤️‍🔥乙⚔️"),
        )


if __name__ == "__main__":
    unittest.main()
