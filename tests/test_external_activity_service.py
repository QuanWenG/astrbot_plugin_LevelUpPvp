import os
import tempfile
import unittest

from models.user import UserIdentity
from services.attribute_service import AttributeService
from services.db import connect_db, init_db
from services.external_activity_service import ExternalActivityService
from services.user_service import UserService


class ExternalActivityServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.attributes = AttributeService(self.db_path)
        self.identity = UserIdentity("test", "group", "player", "玩家")
        self.service = ExternalActivityService(
            self.db_path,
            self.users,
            self.attributes,
            randint=lambda _lower, _upper: 10,
        )

    async def asyncTearDown(self):
        os.remove(self.db_path)

    async def test_wrong_then_correct_applies_separate_idempotent_components(self):
        attempt = await self.service.grant(
            identity=self.identity,
            source="guess",
            reward_key="round-1",
            valid_attempt=True,
            correct=False,
        )
        self.assertEqual(attempt["applied_components"], ["attempt"])
        self.assertEqual(attempt["nickname"], "玩家")
        self.assertEqual(attempt["level_exp"], 0)
        self.assertEqual(
            attempt["attribute_exp"],
            {"perception": 10, "magic": 10},
        )

        correct = await self.service.grant(
            identity=self.identity,
            source="guess",
            reward_key="round-1",
            valid_attempt=True,
            correct=True,
        )
        self.assertEqual(correct["applied_components"], ["correct"])
        self.assertEqual(correct["level_exp"], 20)
        self.assertEqual(
            correct["attribute_exp"],
            {"perception": 10, "magic": 10},
        )

        duplicate = await self.service.grant(
            identity=self.identity,
            source="guess",
            reward_key="round-1",
            valid_attempt=True,
            correct=True,
        )
        self.assertEqual(duplicate["applied_components"], [])
        self.assertEqual(duplicate["nickname"], "玩家")
        user = await self.users.get_or_create_user(self.identity)
        self.assertEqual(user.exp, 20)
        progress = await self.attributes.get_progress(user.id)
        self.assertEqual(progress["perception"].exp, 2020)
        self.assertEqual(progress["magic"].exp, 2020)

    async def test_first_correct_combines_two_attribute_components(self):
        result = await self.service.grant(
            identity=self.identity,
            source="guess",
            reward_key="round-2",
            valid_attempt=True,
            correct=True,
        )
        self.assertEqual(result["applied_components"], ["attempt", "correct"])
        self.assertEqual(
            result["attribute_exp"],
            {"perception": 20, "magic": 20},
        )
        self.assertEqual(result["level_exp"], 20)

        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT source, exp_gain FROM reward_ledger
                WHERE source = 'external_activity:guess'
                """
            )
            audit = await cursor.fetchone()
            await cursor.close()
        self.assertIsNotNone(audit)
        self.assertEqual(audit["source"], "external_activity:guess")
        self.assertEqual(int(audit["exp_gain"]), result["level_exp"])

    async def test_potential_is_a_percentage_multiplier(self):
        user = await self.users.get_or_create_user(self.identity)
        async with await connect_db(self.db_path) as db:
            await self.attributes.ensure_progress_in_db(db, user.id)
            await db.execute(
                """
                UPDATE user_attribute_progress
                SET potential = 400
                WHERE user_pk = ? AND attribute_id IN ('perception', 'magic')
                """,
                (user.id,),
            )
            await db.commit()

        result = await self.service.grant(
            identity=self.identity,
            source="guess",
            reward_key="round-3",
            valid_attempt=True,
            correct=False,
        )
        self.assertEqual(
            result["attribute_exp"],
            {"perception": 40, "magic": 40},
        )

    async def test_level_exp_roll_count_is_perception_plus_magic(self):
        user = await self.users.get_or_create_user(self.identity)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET atk = 3, luck = 4 WHERE id = ?",
                (user.id,),
            )
            await db.commit()

        rolls = []

        def fixed_roll(lower, upper):
            rolls.append((lower, upper))
            return 10

        service = ExternalActivityService(
            self.db_path,
            self.users,
            self.attributes,
            randint=fixed_roll,
        )
        result = await service.grant(
            identity=self.identity,
            source="guess",
            reward_key="round-level-exp",
            valid_attempt=True,
            correct=True,
        )

        self.assertEqual(result["level_exp"], 70)
        self.assertEqual(rolls, [(10, 20)] * 7)

    async def test_transaction_rolls_back_on_invalid_component_insert(self):
        with self.assertRaises(ValueError):
            await self.service.grant(
                identity=self.identity,
                source="",
                reward_key="round-4",
                valid_attempt=True,
                correct=False,
            )
