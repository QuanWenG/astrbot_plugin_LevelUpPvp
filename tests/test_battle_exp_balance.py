import sys
import types


def _install_dependency_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.logger = types.SimpleNamespace(exception=lambda *args, **kwargs: None)

    astrbot_event = types.ModuleType("astrbot.api.event")
    astrbot_event.AstrMessageEvent = object

    components = types.ModuleType("astrbot.api.message_components")

    class At:
        def __init__(self, qq, name=""):
            self.qq = qq
            self.name = name

    class Node:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Image:
        def __init__(self, file=""):
            self.file = file

    components.At = At
    components.Node = Node
    components.Plain = Plain
    components.Image = Image

    io_module = types.ModuleType("astrbot.core.utils.io")
    io_module.save_temp_img = lambda image: "temp.png"
    font_module = types.ModuleType("astrbot.core.utils.t2i.local_strategy")
    font_module.FontManager = types.SimpleNamespace(get_font=lambda size: None)

    pil = types.ModuleType("PIL")
    pil.Image = types.SimpleNamespace()
    pil.ImageDraw = types.SimpleNamespace(ImageDraw=object)

    modules = {
        "astrbot": astrbot,
        "astrbot.api": astrbot_api,
        "astrbot.api.event": astrbot_event,
        "astrbot.api.message_components": components,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
        "astrbot.core.utils.io": io_module,
        "astrbot.core.utils.t2i": types.ModuleType("astrbot.core.utils.t2i"),
        "astrbot.core.utils.t2i.local_strategy": font_module,
        "PIL": pil,
    }
    for name, module in modules.items():
        sys.modules.setdefault(name, module)


_install_dependency_stubs()

import os
import unittest
from unittest.mock import patch
import shutil
import uuid


class WorkspaceTemporaryDirectory:
    def __init__(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
        os.makedirs(root, exist_ok=True)
        self.name = os.path.join(root, f"case-{uuid.uuid4().hex}")
        os.makedirs(self.name, exist_ok=True)

    def cleanup(self):
        shutil.rmtree(self.name, ignore_errors=True)

from models.battle import BattleResult
from models.user import LevelDownEvent, LevelUpEvent, User, UserIdentity
from services import config
from services.battle_service import BattleService
from services.db import connect_db, init_db
from services.user_service import UserService


def _user(*, user_id="user", level=1, exp=0, stat_points=0, hp=10, atk=5):
    return User(
        id=1,
        platform="test",
        group_id="group-1",
        user_id=user_id,
        nickname=user_id,
        level=level,
        exp=exp,
        total_exp=0,
        stat_points=stat_points,
        level_up_count=max(0, level - 1),
        hp=hp,
        atk=atk,
        defense=5,
        speed=5,
        luck=5,
        wins=0,
        losses=0,
        created_at="2026-07-08T00:00:00",
        updated_at="2026-07-08T00:00:00",
    )


class BattleExpFormulaTests(unittest.TestCase):
    def setUp(self):
        self.service = BattleService(":memory:", None, None)

    def test_same_level_exp_is_a_single_transfer(self):
        winner = _user(level=15)
        loser = _user(user_id="loser", level=15)
        required = config.exp_required_for_next_level(15)

        with patch("services.battle_service.random.uniform", return_value=0):
            loss = self.service._roll_loser_exp_loss(winner, loser)
        gain = self.service._winner_exp_gain_from_loss(winner, loss)

        self.assertEqual(loss, round(required * 0.24))
        self.assertEqual(gain, loss)

    def test_high_level_beating_low_level_has_no_farmable_reward(self):
        high = _user(level=20)
        low = _user(user_id="low", level=10)

        with patch("services.battle_service.random.uniform", return_value=-0.04):
            loss = self.service._roll_loser_exp_loss(high, low)
        gain = self.service._winner_exp_gain_from_loss(high, loss)

        self.assertEqual(loss, round(config.exp_required_for_next_level(10) * 0.02))
        self.assertEqual(gain, loss)
        self.assertLess(gain, config.exp_required_for_next_level(20) * 0.01)

    def test_low_level_beating_high_level_can_fill_one_level(self):
        low = _user(level=10)
        high = _user(user_id="high", level=20)

        with patch("services.battle_service.random.uniform", return_value=0):
            loss = self.service._roll_loser_exp_loss(low, high)
        gain = self.service._winner_exp_gain_from_loss(low, loss)

        self.assertEqual(loss, round(config.exp_required_for_next_level(20) * 0.84))
        self.assertEqual(gain, config.exp_required_for_next_level(10))

    def test_winner_never_receives_more_than_loser_actually_lost(self):
        winner = _user(level=15)

        self.assertEqual(self.service._winner_exp_gain_from_loss(winner, 0), 0)
        self.assertEqual(self.service._winner_exp_gain_from_loss(winner, 80), 80)
        self.assertEqual(
            self.service._winner_exp_gain_from_loss(winner, 999999),
            config.exp_required_for_next_level(15),
        )


class UserExpDowngradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = WorkspaceTemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "balance.db")
        await init_db(self.db_path)
        self.user_service = UserService(self.db_path)
        self.identity = UserIdentity(
            platform="test",
            group_id="group-1",
            user_id="user-1",
            nickname="user-1",
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_deduct_exp_without_downgrade(self):
        user = await self._prepare_user(level=3, exp=50)
        async with await connect_db(self.db_path) as db:
            result = await self.user_service.deduct_exp_in_db(db, user, 20)
            await db.commit()

        self.assertEqual(result.exp_delta, -20)
        self.assertEqual(result.user.level, 3)
        self.assertEqual(result.user.exp, 30)
        self.assertEqual(result.level_downs, [])

    async def test_downgrade_freezes_growth_and_unspent_points(self):
        user = await self._prepare_user(level=3, exp=5, stat_points=3, hp=12, atk=6)
        await self._insert_level_log(user.id, from_level=2, to_level=3, growth={"strength": 2, "perception": 1})

        async with await connect_db(self.db_path) as db:
            result = await self.user_service.deduct_exp_in_db(db, user, 10)
            await db.commit()

        self.assertEqual(result.exp_delta, -10)
        self.assertEqual(result.user.level, 2)
        self.assertEqual(result.user.exp, config.exp_required_for_next_level(2) - 5)
        self.assertEqual(result.user.hp, 10)
        self.assertEqual(result.user.atk, 5)
        self.assertEqual(result.user.stat_points, 0)
        self.assertEqual(result.level_downs[0].frozen_stats, {"strength": 2, "perception": 1})
        self.assertEqual(result.level_downs[0].frozen_stat_points, 3)
        self.assertEqual(result.user.frozen_levels, [3])
        self.assertEqual(result.user.frozen_stats, {"strength": 2, "perception": 1})
        self.assertEqual(result.user.frozen_stat_points, 3)

    async def test_multi_level_downgrade_and_level_one_clamp(self):
        user = await self._prepare_user(level=3, exp=0, stat_points=0, hp=13, atk=6)
        await self._insert_level_log(user.id, from_level=1, to_level=2, growth={"hp": 1})
        await self._insert_level_log(user.id, from_level=2, to_level=3, growth={"strength": 2, "perception": 1})

        async with await connect_db(self.db_path) as db:
            result = await self.user_service.deduct_exp_in_db(db, user, 300)
            await db.commit()

        self.assertEqual(result.user.level, 1)
        self.assertEqual(result.user.exp, 0)
        self.assertEqual(result.exp_delta, -(config.exp_required_for_next_level(2) + config.exp_required_for_next_level(1)))
        self.assertEqual([event.from_level for event in result.level_downs], [3, 2])

        async with await connect_db(self.db_path) as db:
            clamped = await self.user_service.deduct_exp_in_db(db, result.user, 50)
            await db.commit()

        self.assertEqual(clamped.exp_delta, 0)
        self.assertEqual(clamped.user.level, 1)
        self.assertEqual(clamped.user.exp, 0)

    async def test_relevel_releases_freeze_without_new_growth_log(self):
        user = await self._prepare_user(level=3, exp=0, stat_points=3, hp=12, atk=6)
        await self._insert_level_log(user.id, from_level=2, to_level=3, growth={"strength": 2, "perception": 1})

        async with await connect_db(self.db_path) as db:
            downgraded = await self.user_service.deduct_exp_in_db(db, user, 1)
            await db.commit()

        self.assertEqual(downgraded.user.level, 2)
        before_logs = await self._count_level_logs(to_level=3)

        async with await connect_db(self.db_path) as db:
            restored = await self.user_service.add_exp_in_db(db, downgraded.user, 2)
            await db.commit()

        self.assertEqual(restored.user.level, 3)
        self.assertEqual(restored.user.exp, 1)
        self.assertEqual(restored.user.hp, 12)
        self.assertEqual(restored.user.atk, 6)
        self.assertEqual(restored.user.stat_points, 3)
        self.assertEqual(restored.user.frozen_levels, [])
        self.assertEqual(len(restored.level_ups), 1)
        self.assertTrue(restored.level_ups[0].restored_from_freeze)
        self.assertEqual(restored.level_ups[0].auto_growth, {"strength": 2, "perception": 1})
        self.assertEqual(restored.level_ups[0].stat_points_gain, 3)
        self.assertEqual(await self._count_level_logs(to_level=3), before_logs)
        self.assertEqual(await self._count_active_freezes(), 0)

    async def _prepare_user(
        self,
        *,
        level=1,
        exp=0,
        stat_points=0,
        hp=10,
        atk=5,
    ):
        async with await connect_db(self.db_path) as db:
            user, _ = await self.user_service.get_or_create_user_in_db(db, self.identity)
            await db.execute(
                """
                UPDATE users
                SET level = ?, exp = ?, total_exp = ?, stat_points = ?,
                    level_up_count = ?, hp = ?, atk = ?, defense = 5,
                    speed = 5, luck = 5
                WHERE id = ?
                """,
                (level, exp, 0, stat_points, max(0, level - 1), hp, atk, user.id),
            )
            await db.commit()
            return await self.user_service.get_user_by_pk_in_db(db, user.id)

    async def _insert_level_log(self, user_id, *, from_level, to_level, growth):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO level_up_logs (
                    user_pk, from_level, to_level, auto_growth_json,
                    stat_points_gain, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    from_level,
                    to_level,
                    __import__("json").dumps(growth),
                    config.STAT_POINTS_PER_LEVEL,
                    "2026-07-08T00:00:00",
                ),
            )
            await db.commit()

    async def _count_level_logs(self, *, to_level):
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM level_up_logs WHERE to_level = ?",
                (to_level,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return int(row["count"])

    async def _count_active_freezes(self):
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM level_freezes WHERE status = 'frozen'"
            )
            row = await cursor.fetchone()
            await cursor.close()
            return int(row["count"])


class BattleResultFormattingTests(unittest.TestCase):
    def test_formats_freezes_and_unfreezes(self):
        from handles.command_handler import LevelUpPvpCommandHandler

        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=None,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )
        attacker = _user(user_id="attacker", level=3)
        defender = _user(user_id="defender", level=2)
        result = BattleResult(
            attacker=attacker,
            defender=defender,
            winner=attacker,
            loser=defender,
            attacker_strategy="attack",
            defender_strategy="defend",
            attacker_strategy_random=False,
            defender_strategy_random=False,
            attacker_win_rate=0.6,
            roll_value=0.2,
            winner_exp_gain=100,
            loser_exp_loss=80,
            analysis="",
            level_ups=[
                LevelUpEvent(
                    from_level=2,
                    to_level=3,
                    auto_growth={"hp": 2},
                    stat_points_gain=3,
                    restored_from_freeze=True,
                )
            ],
            level_downs=[
                LevelDownEvent(
                    from_level=3,
                    to_level=2,
                    frozen_stats={"atk": 2},
                    frozen_stat_points=1,
                )
            ],
        )

        text = handler._format_battle_result(result)

        self.assertIn("\u89e3\u51bb\u6062\u590d", text)
        self.assertIn("\u964d\u7ea7\u51bb\u7ed3", text)
        self.assertIn("\u611f\u77e5 -2", text)
        self.assertIn("\u81ea\u5b9a\u4e49\u5c5e\u6027\u70b9 -1", text)
