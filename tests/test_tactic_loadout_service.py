import asyncio
import os
import shutil
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from services.db import connect_db, init_db
from services.tactic_loadout_service import TacticLoadoutService
from services.tactic_rules import TacticFamily, TacticPlan


class TacticLoadoutServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".test_tmp")
        )
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(
            root,
            f"tactic-loadout-{uuid.uuid4().hex}",
        )
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "loadout.db")
        await init_db(self.db_path)
        connection = sqlite3.connect(self.db_path)
        try:
            self.user_pk = connection.execute(
                """
                INSERT INTO users (
                    platform, group_id, user_id, nickname,
                    created_at, updated_at
                ) VALUES ('qq', 'group-1', 'user-1', '战术测试员', 'now', 'now')
                """
            ).lastrowid
            connection.commit()
        finally:
            connection.close()
        self.service = TacticLoadoutService(self.db_path)

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _stored_row(self):
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                """
                SELECT opening_family, midgame_family, endgame_family,
                       active_slots_json, updated_at
                FROM combat_loadouts
                WHERE user_pk = ?
                """,
                (self.user_pk,),
            ).fetchone()
        finally:
            connection.close()

    async def test_first_load_migrates_legacy_strategy_once(self):
        with patch(
            "services.tactic_loadout_service.utc_now_text",
            side_effect=("first", "second"),
        ):
            first = await self.service.load_or_migrate(
                self.user_pk,
                "全力猛攻",
            )
            first_row = self._stored_row()
            second = await self.service.load_or_migrate(
                self.user_pk,
                "防守反击",
            )

        expected = TacticPlan(
            TacticFamily.PRESSURE,
            TacticFamily.PRESSURE,
            TacticFamily.PRESSURE,
        )
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertEqual(self._stored_row(), first_row)
        self.assertEqual(
            first_row,
            ("pressure", "pressure", "pressure", "[]", "first"),
        )

    async def test_set_plan_accepts_labels_and_legacy_names(self):
        active_slots = '[1, 2, {"kind":"spell"}]'
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                """
                INSERT INTO combat_loadouts (
                    user_pk, opening_family, midgame_family, endgame_family,
                    active_slots_json, updated_at
                ) VALUES (?, 'sustain', 'sustain', 'sustain', ?, 'old')
                """,
                (self.user_pk, active_slots),
            )
            connection.commit()
        finally:
            connection.close()

        plan = await self.service.set_plan(
            self.user_pk,
            opening="压制",
            midgame="速度拉扯",
            endgame="防守反击",
        )

        self.assertEqual(
            plan,
            TacticPlan(
                TacticFamily.PRESSURE,
                TacticFamily.SKIRMISH,
                TacticFamily.COUNTER,
            ),
        )
        row = self._stored_row()
        self.assertEqual(
            row[:4],
            ("pressure", "skirmish", "counter", active_slots),
        )
        self.assertNotEqual(row[4], "old")
        self.assertEqual(await self.service.get_plan(self.user_pk), plan)

    async def test_invalid_plan_does_not_insert_or_modify(self):
        with self.assertRaisesRegex(ValueError, "未知战术"):
            await self.service.set_plan(
                self.user_pk,
                "压制",
                "并不存在的战术",
                "坚守",
            )
        self.assertIsNone(self._stored_row())

        await self.service.set_plan(self.user_pk, "压制", "游击", "坚守")
        before = self._stored_row()
        with self.assertRaisesRegex(ValueError, "未知战术"):
            await self.service.set_plan(
                self.user_pk,
                "控制",
                "奇策",
                "坏数据",
            )
        self.assertEqual(self._stored_row(), before)

    async def test_in_db_write_obeys_caller_rollback(self):
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            plan = await self.service.set_plan_in_db(
                db,
                self.user_pk,
                "先手压制",
                "控制",
                "持久消耗",
            )
            self.assertEqual(
                await self.service.get_plan_in_db(db, self.user_pk),
                plan,
            )
            await db.rollback()

        self.assertIsNone(await self.service.get_plan(self.user_pk))

    async def test_concurrent_first_loads_create_exactly_one_row(self):
        def first_load():
            return asyncio.run(
                self.service.load_or_migrate(self.user_pk, "幸运赌局")
            )

        plans = await asyncio.gather(
            *(
                asyncio.to_thread(first_load)
                for _ in range(8)
            )
        )

        expected = TacticPlan(
            TacticFamily.GAMBIT,
            TacticFamily.GAMBIT,
            TacticFamily.GAMBIT,
        )
        self.assertEqual(plans, [expected] * 8)
        connection = sqlite3.connect(self.db_path)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM combat_loadouts WHERE user_pk = ?",
                (self.user_pk,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)

    async def test_custom_legacy_strategy_has_neutral_migration_fallback(self):
        plan = await self.service.load_or_migrate(
            self.user_pk,
            "先绕着对手唱三圈歌再进攻",
        )
        self.assertEqual(
            plan,
            TacticPlan(
                TacticFamily.SUSTAIN,
                TacticFamily.SUSTAIN,
                TacticFamily.SUSTAIN,
            ),
        )

    async def test_get_does_not_create_and_format_is_stable(self):
        self.assertIsNone(await self.service.get_plan(self.user_pk))
        self.assertIsNone(self._stored_row())
        text = self.service.format_plan(
            TacticPlan(
                TacticFamily.PRESSURE,
                TacticFamily.CONTROL,
                TacticFamily.SUSTAIN,
            )
        )
        self.assertEqual(text, "开局：压制｜中盘：控制｜终局：坚守")


if __name__ == "__main__":
    unittest.main()
