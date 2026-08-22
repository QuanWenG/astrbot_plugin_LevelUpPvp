import asyncio
import os
import shutil
import sqlite3
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

from models.user import UserIdentity
from services.db import init_db
from services.equipment_service import EquipmentService
from services.operation_service import OperationService
from services.operation_settlement_service import (
    EPIC_PITY_DRAWS,
    MYTHIC_PITY_DRAWS,
    OperationSettlementService,
)
from services.user_service import UserService


class ScriptedEquipmentService(EquipmentService):
    """Use production item persistence with a deterministic quality script."""

    def __init__(
        self,
        db_path,
        *,
        qualities=("common",),
        fail_after_insert_once=False,
    ):
        super().__init__(db_path, seed_source=lambda: 7331)
        self.qualities = tuple(qualities)
        self.generated_calls = 0
        self.fail_after_insert_once = bool(fail_after_insert_once)

    def generate_reward(self, *args, **kwargs):
        item = super().generate_reward(*args, **kwargs)
        index = min(self.generated_calls, len(self.qualities) - 1)
        self.generated_calls += 1
        return replace(item, quality=self.qualities[index])

    async def insert_item_in_db(self, db, item):
        equipment_id = await super().insert_item_in_db(db, item)
        if self.fail_after_insert_once:
            self.fail_after_insert_once = False
            raise RuntimeError("injected failure after equipment insert")
        return equipment_id


class OperationSettlementServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".test_tmp")
        )
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(root, f"operation-settlement-{uuid.uuid4().hex}")
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "settlement.db")
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.user = await self.users.get_or_create_user(
            UserIdentity("qq", "group-a", "settlement-user", "结算测试者")
        )
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "UPDATE users SET level = 25, exp = 7, total_exp = 17 WHERE id = ?",
                (self.user.id,),
            )
            connection.commit()
        finally:
            connection.close()
        self.operations = OperationService(self.db_path)
        self.now = datetime(2026, 8, 10, 12, 0, 0)

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def _claim_daily(self, *, now=None, group_id="group-a"):
        current = now or self.now
        tasks = self.operations.daily_tasks(group_id, current)
        for index, task in enumerate(tasks[:2]):
            await self.operations.advance_daily_task(
                user_pk=self.user.id,
                group_id=group_id,
                task_id=task.task_id,
                event_key=f"claim:{current.date()}:{group_id}:{index}",
                amount=task.target,
                now=current,
            )
        claim = await self.operations.claim_daily_reward(
            user_pk=self.user.id,
            group_id=group_id,
            now=current,
        )
        self.assertTrue(claim.granted)
        self.assertIsNotNone(claim.reward_intent)
        return claim.reward_intent

    async def _claim_weekly(self, *, now=None, group_id="group-a"):
        current = now or self.now
        tasks = self.operations.weekly_tasks(group_id, current)
        for index, task in enumerate(tasks[:5]):
            await self.operations.advance_weekly_task(
                user_pk=self.user.id,
                group_id=group_id,
                task_id=task.task_id,
                event_key=f"weekly:{current.date()}:{group_id}:{index}",
                amount=task.target,
                now=current,
            )
        claim = await self.operations.claim_weekly_reward(
            user_pk=self.user.id,
            group_id=group_id,
            now=current,
        )
        self.assertTrue(claim.granted)
        self.assertIsNotNone(claim.reward_intent)
        return claim.reward_intent

    def _scalar(self, sql, parameters=()):
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(sql, parameters).fetchone()[0]
        finally:
            connection.close()

    def _wallet(self):
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                """
                SELECT scrap_balance, season_tokens, lifetime_earned
                FROM workshop_wallet WHERE user_pk = ?
                """,
                (self.user.id,),
            ).fetchone()
        finally:
            connection.close()

    async def test_failed_settlement_rolls_back_every_reward_and_stable_claim_retries(self):
        intent = await self._claim_daily()
        equipment = ScriptedEquipmentService(
            self.db_path,
            fail_after_insert_once=True,
        )
        settlement = OperationSettlementService(
            self.db_path,
            self.users,
            equipment,
        )

        with self.assertRaisesRegex(RuntimeError, "injected failure"):
            await settlement.settle(user_pk=self.user.id, intent=intent)

        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM reward_ledger"),
            0,
        )
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM workshop_wallet"),
            0,
        )
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM equipment_items"),
            0,
        )
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM loot_pity"), 0)
        self.assertEqual(
            self._scalar("SELECT exp FROM users WHERE id = ?", (self.user.id,)),
            7,
        )

        retry_claim = await self.operations.claim_daily_reward(
            user_pk=self.user.id,
            group_id="group-a",
            now=self.now,
        )
        self.assertFalse(retry_claim.granted)
        self.assertTrue(retry_claim.already_claimed)
        self.assertEqual(retry_claim.reward_intent, intent)

        applied = await settlement.settle(
            user_pk=self.user.id,
            intent=retry_claim.reward_intent,
        )
        self.assertTrue(applied.applied)
        self.assertEqual(len(applied.equipment), 1)
        self.assertLessEqual(applied.equipment[0].item_level, 25)

    async def test_same_reward_key_concurrent_and_repeated_settlement_grants_once(self):
        intent = await self._claim_daily()
        equipment = ScriptedEquipmentService(self.db_path)
        settlement = OperationSettlementService(
            self.db_path,
            self.users,
            equipment,
        )

        def settle_on_independent_connection():
            # The project supports a synchronous sqlite fallback.  Separate
            # threads make this genuine lock contention between connections
            # instead of six coroutines that happen to run serially.
            return asyncio.run(
                settlement.settle(user_pk=self.user.id, intent=intent)
            )

        results = await asyncio.gather(
            *(asyncio.to_thread(settle_on_independent_connection) for _ in range(6))
        )
        repeated = await settlement.settle(user_pk=self.user.id, intent=intent)

        self.assertEqual(sum(result.applied for result in results), 1)
        self.assertFalse(repeated.applied)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM reward_ledger"), 1)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM equipment_items"),
            intent.loot_rolls,
        )
        self.assertEqual(
            self._wallet(),
            (intent.scrap, intent.season_tokens, intent.scrap),
        )
        self.assertEqual(
            self._scalar("SELECT total_draws FROM loot_pity"),
            intent.loot_rolls,
        )

    async def test_epic_tenth_draw_is_guaranteed_and_level_never_exceeds_player(self):
        intent = await self._claim_daily()
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                """
                INSERT INTO loot_pity (
                    user_pk, pool_id, epic_misses, legendary_misses,
                    total_draws, updated_at_ts
                ) VALUES (?, 'operation:daily_operation:v11', ?, 0, ?, 0)
                """,
                (self.user.id, EPIC_PITY_DRAWS - 1, EPIC_PITY_DRAWS - 1),
            )
            connection.commit()
        finally:
            connection.close()
        equipment = ScriptedEquipmentService(
            self.db_path,
            qualities=("common", "epic"),
        )
        result = await OperationSettlementService(
            self.db_path,
            self.users,
            equipment,
        ).settle(user_pk=self.user.id, intent=intent)

        self.assertEqual(result.equipment[0].quality, "epic")
        self.assertEqual(equipment.generated_calls, 2)
        self.assertLessEqual(result.equipment[0].item_level, 25)
        connection = sqlite3.connect(self.db_path)
        try:
            pity = connection.execute(
                "SELECT epic_misses, total_draws FROM loot_pity"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(pity, (0, EPIC_PITY_DRAWS))

    async def test_mythic_fortieth_draw_is_guaranteed(self):
        intent = await self._claim_daily()
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                """
                INSERT INTO loot_pity (
                    user_pk, pool_id, epic_misses, legendary_misses,
                    total_draws, updated_at_ts
                ) VALUES (?, 'operation:daily_operation:v11', 0, ?, ?, 0)
                """,
                (self.user.id, MYTHIC_PITY_DRAWS - 1, MYTHIC_PITY_DRAWS - 1),
            )
            connection.commit()
        finally:
            connection.close()
        equipment = ScriptedEquipmentService(
            self.db_path,
            qualities=("epic", "mythic"),
        )
        result = await OperationSettlementService(
            self.db_path,
            self.users,
            equipment,
        ).settle(user_pk=self.user.id, intent=intent)

        self.assertEqual(result.equipment[0].quality, "mythic")
        self.assertEqual(equipment.generated_calls, 2)
        connection = sqlite3.connect(self.db_path)
        try:
            pity = connection.execute(
                "SELECT epic_misses, legendary_misses, total_draws FROM loot_pity"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(pity, (0, 0, MYTHIC_PITY_DRAWS))

    async def test_weekly_claim_uses_the_same_canonical_atomic_boundary(self):
        intent = await self._claim_weekly()
        result = await OperationSettlementService(
            self.db_path,
            self.users,
            ScriptedEquipmentService(self.db_path),
        ).settle(user_pk=self.user.id, intent=intent)

        self.assertTrue(result.applied)
        self.assertEqual(len(result.equipment), 2)
        self.assertEqual(self._wallet(), (80, 30, 80))
        self.assertTrue(all(item.item_level <= 25 for item in result.equipment))

    async def test_forged_or_cross_user_intent_is_rejected_before_any_grant(self):
        intent = await self._claim_daily()
        settlement = OperationSettlementService(
            self.db_path,
            self.users,
            ScriptedEquipmentService(self.db_path),
        )

        with self.assertRaisesRegex(ValueError, "claimed reservation"):
            await settlement.settle(
                user_pk=self.user.id,
                intent=replace(intent, scrap=intent.scrap + 999_999),
            )
        other = await self.users.get_or_create_user(
            UserIdentity("qq", "group-a", "other-user", "另一人")
        )
        with self.assertRaisesRegex(ValueError, "claimed reservation"):
            await settlement.settle(user_pk=other.id, intent=intent)

        self.assertEqual(self._scalar("SELECT COUNT(*) FROM reward_ledger"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM workshop_wallet"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM equipment_items"), 0)

        legitimate = await settlement.settle(user_pk=self.user.id, intent=intent)
        self.assertTrue(legitimate.applied)


if __name__ == "__main__":
    unittest.main()
