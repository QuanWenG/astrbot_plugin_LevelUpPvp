import asyncio
import math
import os
import unittest
from datetime import date, datetime
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

from models.user import UserIdentity
from services import config
from services.checkin_service import CheckinService, current_checkin_date
from services.db import connect_db, init_db
from services.user_service import UserService


class CheckinRewardTests(unittest.TestCase):
    def setUp(self):
        self.service = CheckinService(":memory:", UserService(":memory:"))

    def test_rolls_once_per_level_and_caps_base_reward(self):
        with patch(
            "services.checkin_service.random.randint",
            side_effect=[50, 50, 50, 0],
        ) as mocked_randint:
            result = self.service._roll_exp(level=3, streak_days=1)

        required = config.exp_required_for_next_level(3)
        self.assertEqual(result, math.floor(required * 0.60))
        self.assertEqual(mocked_randint.call_count, 4)

    def test_exact_ten_percent_does_not_trigger_fallback(self):
        with (
            patch(
                "services.checkin_service.random.randint",
                side_effect=[10, 0],
            ),
            patch("services.checkin_service.random.uniform") as mocked_uniform,
        ):
            result = self.service._roll_exp(level=1, streak_days=1)

        self.assertEqual(result, 10)
        mocked_uniform.assert_not_called()

    def test_fallback_directly_replaces_a_higher_raw_roll(self):
        with (
            patch(
                "services.checkin_service.random.randint",
                side_effect=[9, 0],
            ),
            patch(
                "services.checkin_service.random.uniform",
                return_value=0.08,
            ),
        ):
            result = self.service._roll_exp(level=1, streak_days=1)

        self.assertEqual(result, 8)

    def test_streak_bonus_is_added_after_base_cap(self):
        with patch(
            "services.checkin_service.random.randint",
            side_effect=[100, 35],
        ):
            result = self.service._roll_exp(level=1, streak_days=8)

        self.assertEqual(result, 95)


class CheckinDateBoundaryTests(unittest.TestCase):
    def test_before_five_am_belongs_to_previous_checkin_day(self):
        self.assertEqual(
            current_checkin_date(datetime(2026, 7, 3, 4, 59, 59)),
            date(2026, 7, 2),
        )

    def test_five_am_starts_new_checkin_day(self):
        self.assertEqual(
            current_checkin_date(datetime(2026, 7, 3, 5, 0, 0)),
            date(2026, 7, 3),
        )


class CheckinPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = WorkspaceTemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "checkin.db")
        await init_db(self.db_path)
        self.user_service = UserService(self.db_path)
        self.service = CheckinService(self.db_path, self.user_service)
        self.identity = UserIdentity(
            platform="test",
            group_id="group-1",
            user_id="user-1",
            nickname="测试用户",
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_unregistered_user_is_created_and_only_rewarded_once(self):
        with patch.object(self.service, "_roll_exp", return_value=20):
            first = await self.service.checkin(self.identity)
            second = await self.service.checkin(self.identity)

        self.assertFalse(first.already_checked)
        self.assertTrue(second.already_checked)
        self.assertEqual(first.user.total_exp, 20)
        self.assertEqual(second.user.total_exp, 20)
        self.assertEqual(second.exp_gain, 20)
        self.assertEqual(await self._count_checkins(), 1)

    async def test_checkin_preserves_overflow_and_returns_level_up(self):
        async with await connect_db(self.db_path) as db:
            user, _ = await self.user_service.get_or_create_user_in_db(
                db,
                self.identity,
            )
            await db.execute(
                "UPDATE users SET exp = ?, total_exp = ? WHERE id = ?",
                (90, 90, user.id),
            )
            await db.commit()

        with patch.object(self.service, "_roll_exp", return_value=20):
            result = await self.service.checkin(self.identity)

        self.assertEqual(result.user.level, 2)
        self.assertEqual(result.user.exp, 10)
        self.assertEqual(result.user.total_exp, 110)
        self.assertEqual(len(result.level_ups), 1)

    async def test_concurrent_first_messages_create_one_checkin(self):
        identity = UserIdentity(
            platform="test",
            group_id="group-1",
            user_id="concurrent-user",
            nickname="并发用户",
        )
        with patch.object(self.service, "_roll_exp", return_value=20):
            results = await asyncio.gather(
                self.service.checkin(identity),
                self.service.checkin(identity),
            )

        self.assertEqual(sum(not result.already_checked for result in results), 1)
        self.assertEqual(sum(result.user.total_exp for result in results), 40)
        self.assertEqual(await self._count_checkins(user_id="concurrent-user"), 1)

    async def test_streak_increments_from_yesterday_and_resets_after_gap(self):
        async with await connect_db(self.db_path) as db:
            user, _ = await self.user_service.get_or_create_user_in_db(
                db,
                self.identity,
            )
            await db.commit()

        async with await connect_db(self.db_path) as db:
            yesterday = await self.service._calculate_streak_in_db(
                db,
                user.id,
                date(2026, 7, 3),
            )
        self.assertEqual(yesterday, 1)

        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO checkins (
                    user_pk, checkin_date, streak_days, exp_gain, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (user.id, "2026-07-02", 4, 20, "2026-07-02T08:00:00"),
            )
            await db.commit()

        async with await connect_db(self.db_path) as db:
            continued = await self.service._calculate_streak_in_db(
                db,
                user.id,
                date(2026, 7, 3),
            )
            reset = await self.service._calculate_streak_in_db(
                db,
                user.id,
                date(2026, 7, 5),
            )

        self.assertEqual(continued, 5)
        self.assertEqual(reset, 1)

    async def _count_checkins(self, user_id: str | None = None) -> int:
        async with await connect_db(self.db_path) as db:
            if user_id is None:
                cursor = await db.execute("SELECT COUNT(*) AS count FROM checkins")
            else:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM checkins AS c
                    JOIN users AS u ON u.id = c.user_pk
                    WHERE u.user_id = ?
                    """,
                    (user_id,),
                )
            row = await cursor.fetchone()
            await cursor.close()
            return int(row["count"])
