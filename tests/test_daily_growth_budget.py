import asyncio
import os
import shutil
import sqlite3
import time
import unittest
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from models.user import UserIdentity
from services import config
from services.battle_service import BattleService
from services.checkin_service import CheckinService
from services.daily_growth_budget import (
    allocate_daily_growth_in_db,
    daily_growth_day_window,
    daily_growth_exp_earned_in_db,
)
from services.db import connect_db, init_db
from services.pvp_economy import decide_pvp_economy
from services.user_service import UserService


class DailyGrowthBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".test_tmp")
        )
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(root, f"growth-budget-{uuid.uuid4().hex}")
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "growth.db")
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.identity = UserIdentity(
            "qq", "growth-group", "growth-user", "成长测试者"
        )
        self.user = await self.users.get_or_create_user(self.identity)
        self.now_ts = int(time.time())

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def _grant_ledger(
        self,
        *,
        user_pk: int,
        reward_key: str,
        source: str,
        requested: int,
        at: int | None = None,
    ) -> int:
        timestamp = self.now_ts if at is None else int(at)
        async with await connect_db(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                user = await self.users.get_user_by_pk_in_db(db, user_pk)
                allocation = await allocate_daily_growth_in_db(
                    db,
                    user_pk=user_pk,
                    level=user.level,
                    requested_exp=requested,
                    at=timestamp,
                )
                await db.execute(
                    """
                    INSERT INTO reward_ledger (
                        reward_key, user_pk, battle_id, source, exp_gain,
                        currency_gain, reason, created_at_ts
                    ) VALUES (?, ?, NULL, ?, ?, 0, 'test', ?)
                    """,
                    (
                        reward_key,
                        user_pk,
                        source,
                        allocation.granted,
                        timestamp,
                    ),
                )
                await self.users.add_exp_in_db(db, user, allocation.granted)
                await db.commit()
                return allocation.granted
            except Exception:
                await db.rollback()
                raise

    def _ledger_sum(self, user_pk: int) -> int:
        connection = sqlite3.connect(self.db_path)
        try:
            return int(
                connection.execute(
                    "SELECT COALESCE(SUM(exp_gain), 0) FROM reward_ledger "
                    "WHERE user_pk = ?",
                    (user_pk,),
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def test_hong_kong_activity_day_changes_exactly_at_four(self):
        zone = ZoneInfo("Asia/Hong_Kong")
        before = datetime(2026, 8, 11, 3, 59, 59, tzinfo=zone)
        after = datetime(2026, 8, 11, 4, 0, 0, tzinfo=zone)
        self.assertEqual(daily_growth_day_window(before)[0], "2026-08-10")
        self.assertEqual(daily_growth_day_window(after)[0], "2026-08-11")

    async def test_chat_first_then_checkin_uses_only_shared_remainder(self):
        budget = config.exp_daily_budget(self.user.level)
        chat_exp = await self._grant_ledger(
            user_pk=self.user.id,
            reward_key="chat-first",
            source="chat_growth",
            requested=budget - 3,
        )
        self.assertEqual(chat_exp, budget - 3)

        service = CheckinService(self.db_path, self.users)
        with patch.object(service, "_roll_exp", return_value=20):
            result = await service.checkin(self.identity)

        self.assertEqual(
            result.exp_gain,
            config.exp_daily_budget(result.user.level) - chat_exp,
        )
        self.assertEqual(
            result.user.total_exp,
            config.exp_daily_budget(result.user.level),
        )

    async def test_checkin_first_then_other_sources_share_its_remainder(self):
        budget = config.exp_daily_budget(self.user.level)
        service = CheckinService(self.db_path, self.users)
        with patch.object(service, "_roll_exp", return_value=40):
            checkin = await service.checkin(self.identity)
        self.assertEqual(checkin.exp_gain, 40)

        chat = await self._grant_ledger(
            user_pk=self.user.id,
            reward_key="chat-after-checkin",
            source="chat_growth",
            requested=budget,
        )
        dungeon = await self._grant_ledger(
            user_pk=self.user.id,
            reward_key="dungeon-after-chat",
            source="dungeon_growth",
            requested=budget,
        )

        self.assertEqual(chat, budget - 40)
        updated = await self.users.get_user_by_pk(self.user.id)
        self.assertEqual(dungeon, config.exp_daily_budget(updated.level) - budget)
        self.assertEqual(updated.total_exp, config.exp_daily_budget(updated.level))

    async def test_pvp_context_counts_chat_operation_and_checkin_not_only_pvp(self):
        other = await self.users.get_or_create_user(
            UserIdentity("qq", "growth-group", "opponent", "对手")
        )
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE users SET level = 5 WHERE id IN (?, ?)",
                (self.user.id, other.id),
            )
            day_key, start_ts, _ = daily_growth_day_window(self.now_ts)
            await db.execute(
                """
                INSERT INTO checkins (
                    user_pk, checkin_date, streak_days, exp_gain, created_at
                ) VALUES (?, ?, 1, 25, 'now')
                """,
                (self.user.id, day_key),
            )
            await db.execute(
                """
                INSERT INTO reward_ledger (
                    reward_key, user_pk, battle_id, source, exp_gain,
                    currency_gain, reason, created_at_ts
                ) VALUES
                    ('chat-context', ?, NULL, 'chat_growth', 30, 0, '', ?),
                    ('operation-context', ?, NULL, 'daily_operation', 20, 0, '', ?)
                """,
                (self.user.id, start_ts + 10, self.user.id, start_ts + 11),
            )
            winner = await self.users.get_user_by_pk_in_db(db, self.user.id)
            loser = await self.users.get_user_by_pk_in_db(db, other.id)
            battle_service = object.__new__(BattleService)
            battle_service.combat_engine = SimpleNamespace(
                ruleset=SimpleNamespace(ruleset_id="test-ruleset")
            )
            context = await BattleService._pvp_economy_context_in_db(
                battle_service,
                db,
                winner,
                loser,
                {
                    winner.id: {"rating": 1000, "games": 0},
                    loser.id: {"rating": 1000, "games": 0},
                },
                datetime.fromtimestamp(self.now_ts),
            )
            await db.rollback()

        self.assertEqual(context.winner_daily_exp_earned, 75)
        decision = decide_pvp_economy(context)
        self.assertLessEqual(
            decision.winner_exp_gain,
            config.exp_daily_budget(5) - 75,
        )

    async def test_concurrent_sources_cannot_overspend_one_character_budget(self):
        def competing_grant(index: int) -> int:
            return asyncio.run(
                self._grant_ledger(
                    user_pk=self.user.id,
                    reward_key=f"concurrent:{index}",
                    source=(
                        "chat_growth",
                        "pvp_growth",
                        "daily_operation",
                        "dungeon_nefia",
                    )[index % 4],
                    requested=23,
                )
            )

        grants = await asyncio.gather(
            *(asyncio.to_thread(competing_grant, index) for index in range(12))
        )

        updated = await self.users.get_user_by_pk(self.user.id)
        final_budget = config.exp_daily_budget(updated.level)
        self.assertEqual(sum(grants), final_budget)
        self.assertEqual(self._ledger_sum(self.user.id), final_budget)
        self.assertEqual(updated.total_exp, final_budget)
        connection = sqlite3.connect(self.db_path)
        try:
            sources = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT source FROM reward_ledger"
                )
            }
        finally:
            connection.close()
        self.assertIn("chat_growth", sources)
        self.assertIn("pvp_growth", sources)
        self.assertIn("daily_operation", sources)
        self.assertIn("dungeon_nefia", sources)

    async def test_positive_only_accounting_does_not_let_debits_hide_growth(self):
        day_key, start_ts, end_ts = daily_growth_day_window(self.now_ts)
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO reward_ledger (
                    reward_key, user_pk, source, exp_gain, created_at_ts
                ) VALUES
                    ('positive', ?, 'chat_growth', 40, ?),
                    ('negative', ?, 'legacy_debit', -999, ?)
                """,
                (self.user.id, start_ts + 1, self.user.id, start_ts + 2),
            )
            earned = await daily_growth_exp_earned_in_db(
                db,
                user_pk=self.user.id,
                day_key=day_key,
                day_start_ts=start_ts,
                day_end_ts=end_ts,
            )
            await db.rollback()
        self.assertEqual(earned, 40)


if __name__ == "__main__":
    unittest.main()
