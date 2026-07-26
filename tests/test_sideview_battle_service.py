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
        self.assertEqual(result.simulation.engine_version, "sideview-v10")
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
        self.assertEqual(row["engine_version"], "sideview-v10")
        self.assertEqual(row["random_seed"], result.simulation.random_seed)
        self.assertEqual(row["duration_ticks"], result.simulation.duration_ticks)
        self.assertEqual(row["finish_reason"], result.simulation.finish_reason)
        self.assertEqual(payload["winner_pk"], result.winner.id)
        self.assertTrue(payload["events"])

    async def test_qq_official_uses_image_report(self):
        class Event:
            def get_platform_name(self):
                return "qq_official"
            def plain_result(self, text):
                return ("plain", text)
            def get_self_id(self):
                return "0"
            def image_result(self, url):
                return ("image", url)

        from models.user import User
        attrs = dict(id=1, platform="test", group_id="g", user_id="a", nickname="Alice",
                     level=1, exp=0, total_exp=0, stat_points=0, level_up_count=0,
                     hp=10, atk=5, defense=5, speed=5, luck=5, wins=0, losses=0,
                     willpower=5, life_growth=100, mana_growth=100, advanced_speed=100,
                     advanced_luck=100, created_at="", updated_at="",
                     frozen_stats={}, frozen_stat_points=0, frozen_skill_points=0,
                     frozen_levels=[], skill_points=0)
        a = User(**attrs)
        b = User(**{**attrs, "id": 2, "user_id": "b", "nickname": "Bob"})
        result = type("Result", (), dict(
            attacker=a, defender=b, winner=a, loser=b,
            attacker_strategy="strat", defender_strategy="strat2",
            attacker_strategy_random=False, defender_strategy_random=False,
            winner_exp_gain=50, loser_exp_loss=25,
            battle_log=["line1"], analysis="",
            level_ups=[], level_downs=[],
            skill_growths=[], spell_growths=[], attribute_growths=[],
            simulation=None, is_counterattack=False, source="local",
        ))

        handler = LevelUpPvpCommandHandler(
            context=None, user_service=None, checkin_service=None,
            stat_service=None, battle_service=None,
        )
        report_image = object()
        with unittest.mock.patch(
            "handles.command_handler.render_battle_report",
            return_value=report_image,
        ), unittest.mock.patch(
            "handles.command_handler.save_temp_img",
            return_value="temp.png",
        ) as save_temp_img:
            result_val = await handler._battle_result(Event(), result)
        save_temp_img.assert_called_once_with(report_image)
        self.assertEqual(("image", "temp.png"), result_val)

    async def test_aiocqhttp_forward_contains_image_report(self):
        class Event:
            def get_platform_name(self):
                return "aiocqhttp"

            def get_self_id(self):
                return "bot"

            def chain_result(self, chain):
                return chain

        result = await self.service.battle(
            UserIdentity("test", "group", "attacker", "攻击方"),
            UserIdentity("test", "group", "defender", "防守方"),
            "稳扎稳打",
        )
        handler = LevelUpPvpCommandHandler(
            context=None, user_service=None, checkin_service=None,
            stat_service=None, battle_service=None,
        )

        with unittest.mock.patch(
            "handles.command_handler.render_battle_report",
            return_value=b"png",
        ):
            chain = await handler._battle_result(Event(), result)

        self.assertEqual(1, len(chain))
        self.assertEqual("LevelUpPvp 战报", chain[0].name)
        self.assertEqual("temp.png", chain[0].content[0].file)

    async def test_battle_renderer_failure_uses_generic_text_image(self):
        class Event:
            def get_platform_name(self):
                return "qq_official"

            def plain_result(self, text):
                return ("plain", text)

            def image_result(self, url):
                return ("image", url)

        result = await self.service.battle(
            UserIdentity("test", "group", "attacker", "攻击方"),
            UserIdentity("test", "group", "defender", "防守方"),
            "稳扎稳打",
        )
        handler = LevelUpPvpCommandHandler(
            context=None, user_service=None, checkin_service=None,
            stat_service=None, battle_service=None,
        )

        with unittest.mock.patch(
            "handles.command_handler.render_battle_report",
            side_effect=RuntimeError("render failed"),
        ), unittest.mock.patch(
            "handles.command_handler.render_text_card",
            return_value=object(),
        ), unittest.mock.patch(
            "handles.command_handler.save_temp_img",
            return_value="generic.png",
        ):
            result_value = await handler._battle_result(Event(), result)

        self.assertEqual(("image", "generic.png"), result_value)

    async def test_all_image_rendering_failures_fall_back_to_plain_text(self):
        class Event:
            def get_platform_name(self):
                return "qq_official"

            def plain_result(self, text):
                return ("plain", text)

        result = await self.service.battle(
            UserIdentity("test", "group", "attacker", "攻击方"),
            UserIdentity("test", "group", "defender", "防守方"),
            "稳扎稳打",
        )
        handler = LevelUpPvpCommandHandler(
            context=None, user_service=None, checkin_service=None,
            stat_service=None, battle_service=None,
        )

        with unittest.mock.patch(
            "handles.command_handler.render_battle_report",
            side_effect=RuntimeError("battle render failed"),
        ), unittest.mock.patch(
            "handles.command_handler.render_text_card",
            side_effect=RuntimeError("generic render failed"),
        ):
            result_value = await handler._battle_result(Event(), result)

        self.assertEqual("plain", result_value[0])
        self.assertIn("战报：", result_value[1])

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
        self.assertNotIn("🏆", text)
        self.assertNotIn("⏱️", text)


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
        prompt = service._build_simulation_result_prompt(simulation, original)

        self.assertNotIn("Emoji", prompt)
        self.assertNotIn("emoji", prompt)
        self.assertEqual(
            service._validate_simulation_battle_log(original, original, simulation),
            original,
        )
        optional_emoji = list(original)
        optional_emoji[0] = "⚔️ " + optional_emoji[0]
        self.assertEqual(
            service._validate_simulation_battle_log(
                optional_emoji,
                original,
                simulation,
            ),
            [],
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
