import json
import os
import shutil
import unittest
import uuid

from tests.test_battle_exp_balance import _install_dependency_stubs

_install_dependency_stubs()

from handles.command_handler import LevelUpPvpCommandHandler
from models.user import UserIdentity
from services.battle_service import BattleService
from services.db import connect_db, init_db
from services.llm_service import LLMService
from services.user_service import UserService


class _NoopLLM:
    async def analyze_custom_strategy(self, *args, **kwargs):
        return None

    async def describe_simulation_result(self, *args, **kwargs):
        return []


class SideviewBattleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(root, f"sideview-{uuid.uuid4().hex}")
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "battle.db")
        await init_db(self.db_path)
        self.user_service = UserService(self.db_path)
        self.service = BattleService(self.db_path, self.user_service, _NoopLLM())

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_battle_settles_and_persists_replay_metadata(self):
        result = await self.service.battle(
            UserIdentity("test", "group", "attacker", "攻击方"),
            UserIdentity("test", "group", "defender", "防守方"),
            "全力猛攻",
        )

        self.assertIsNotNone(result.simulation)
        self.assertEqual(result.simulation.engine_version, "sideview-v9")
        self.assertEqual(result.winner.id, result.simulation.winner_pk)
        self.assertGreaterEqual(len(result.battle_log), 6)
        self.assertLessEqual(len(result.battle_log), 10)

        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT battle_mode, engine_version, random_seed, duration_ticks,
                       finish_reason, simulation_json
                FROM battles
                """
            )
            row = await cursor.fetchone()
            await cursor.close()

        payload = json.loads(row["simulation_json"])
        self.assertEqual(row["battle_mode"], "sideview")
        self.assertEqual(row["engine_version"], "sideview-v9")
        self.assertEqual(row["random_seed"], result.simulation.random_seed)
        self.assertEqual(row["duration_ticks"], result.simulation.duration_ticks)
        self.assertEqual(row["finish_reason"], result.simulation.finish_reason)
        self.assertEqual(payload["winner_pk"], result.winner.id)
        self.assertTrue(payload["events"])

    async def test_qq_official_uses_plain_text_report(self):
        class Event:
            def get_platform_name(self):
                return "qq_official"

            def plain_result(self, text):
                return ("plain", text)

        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=None,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )

        self.assertEqual(
            await handler._battle_result(Event(), "文字战报"),
            ("plain", "文字战报"),
        )
    async def test_formatted_sideview_report_hides_legacy_probability(self):
        result = await self.service.battle(
            UserIdentity("test", "group", "attacker", "攻击方"),
            UserIdentity("test", "group", "defender", "防守方"),
            "稳扎稳打",
        )
        handler = LevelUpPvpCommandHandler(context=None, user_service=None, checkin_service=None, stat_service=None, battle_service=None)
        text = handler._format_battle_result(result)

        self.assertNotIn("攻击方胜率", text)
        self.assertNotIn("随机值", text)
        self.assertIn("战报：", text)
        self.assertTrue("🏆" in text or "⏱️" in text)


class SimulationLLMValidationTests(unittest.TestCase):
    def test_rejects_changed_numbers_and_accepts_canonical_lines(self):
        from models.combat import FighterSnapshot
        from services.battle_report import BattleReportBuilder
        from services.combat_ai import STRATEGY_PROFILES
        from services.combat_engine import SideviewCombatEngine

        attacker = FighterSnapshot(1, "甲", 1, 10, 5, 5, 5, 5, "稳扎稳打")
        defender = FighterSnapshot(2, "乙", 1, 10, 5, 5, 5, 5, "防守反击")
        simulation = SideviewCombatEngine().simulate(
            attacker,
            defender,
            STRATEGY_PROFILES[attacker.strategy],
            STRATEGY_PROFILES[defender.strategy],
            7,
        )
        original = BattleReportBuilder().build(simulation)
        service = LLMService()

        self.assertEqual(
            service._validate_simulation_battle_log(original, original, simulation),
            original,
        )
        changed = list(original)
        changed[1] = changed[1].replace("秒", "999秒", 1)
        self.assertEqual(
            service._validate_simulation_battle_log(changed, original, simulation),
            [],
        )
        unapproved_emoji = list(original)
        unapproved_emoji[0] += "😀"
        self.assertEqual(
            service._validate_simulation_battle_log(
                unapproved_emoji,
                original,
                simulation,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
