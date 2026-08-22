import os
import shutil
import types
import unittest
import uuid

from tests.test_command_handler import _install_dependency_stubs

_install_dependency_stubs()

from models.operation import stable_operation_seed
from models.user import UserIdentity
from services.attribute_service import AttributeService
from services.auto_equip_service import AutoEquipService
from services.auto_pilot_service import AutoPilotService
from services.daily_growth_budget import daily_growth_day_window
from services.db import connect_db, init_db
from services.effect_whitelist import EffectWhitelist
from services.equipment_service import EquipmentService
from services.skill_service import SkillService
from services.spell_service import SpellService
from services.stat_service import StatService
from services.build_service import CombatBuildService
from services.user_service import UserService


class EmptyDungeonService:
    def list_dungeons(self):
        return ()


class SeedDungeonService:
    def __init__(self, dungeons):
        self.dungeons = tuple(dungeons)
        self.started = []

    def list_dungeons(self):
        return self.dungeons

    async def view_nefia(self, identity, *, dungeon_id=""):
        raise KeyError(dungeon_id)

    async def start_nefia(self, identity, dungeon_id, difficulty=1, strategy=""):
        self.started.append((dungeon_id, difficulty, strategy))
        return None


class CountingOperationService:
    def __init__(self):
        self.daily_calls = 0
        self.weekly_calls = 0

    async def claim_daily_reward(self, **kwargs):
        self.daily_calls += 1
        return types.SimpleNamespace(eligible=False, reward_intent=None)

    async def claim_weekly_reward(self, **kwargs):
        self.weekly_calls += 1
        return types.SimpleNamespace(eligible=False, reward_intent=None)


class AutoPilotServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".test_tmp")
        )
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(root, f"auto-pilot-{uuid.uuid4().hex}")
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "test.db")
        await init_db(self.db_path)

        self.users = UserService(self.db_path)
        self.attributes = AttributeService(self.db_path)
        self.equipment = EquipmentService(self.db_path)
        self.skills = SkillService(self.db_path)
        self.spells = SpellService(
            self.db_path,
            self.skills,
            self.equipment,
            self.attributes,
        )
        self.stats = StatService(self.db_path, self.users)
        self.builds = CombatBuildService(
            self.equipment,
            self.skills,
            self.attributes,
            self.spells,
        )
        self.auto_equip = AutoEquipService(self.builds)
        self.service = AutoPilotService(
            db_path=self.db_path,
            effect_whitelist=EffectWhitelist(["group-1"]),
            user_service=self.users,
            stat_service=self.stats,
            attribute_service=self.attributes,
            skill_service=self.skills,
            spell_service=self.spells,
            equipment_service=self.equipment,
            auto_equip_service=self.auto_equip,
            dungeon_service=EmptyDungeonService(),
        )
        self.identity = UserIdentity("test", "group-1", "user-1", "托管用户")
        self.user = await self.users.get_or_create_user(self.identity)

    async def asyncTearDown(self):
        await self.service.shutdown()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_enable_disable_state_is_persistent(self):
        enabled = await self.service.enable(
            self.identity,
            origin_umo="test:GroupMessage:group-1",
        )
        self.assertTrue(enabled.enabled)
        self.assertEqual(enabled.origin_group_id, "group-1")

        reloaded = await self.service.get_state(self.user.id)
        self.assertIsNotNone(reloaded)
        self.assertTrue(reloaded.enabled)
        self.assertEqual(reloaded.origin_umo, "test:GroupMessage:group-1")

        self.assertTrue(await self.service.disable(self.user.id))
        disabled = await self.service.get_state(self.user.id)
        self.assertIsNotNone(disabled)
        self.assertFalse(disabled.enabled)

    async def test_pass_allocates_points_without_checking_in(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET stat_points = 2 WHERE id = ?",
                (self.user.id,),
            )
            await db.commit()

        await self.service.enable(self.identity, origin_umo="allowed")
        result = await self.service.run_pass(self.user.id, now_ts=1_700_000_000)

        self.assertTrue(result.enabled)
        self.assertIn("attributes", result.actions)
        updated = await self.users.get_user_by_pk(self.user.id)
        self.assertEqual(updated.stat_points, 0)
        self.assertGreater(updated.strength, self.user.strength)

        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM checkins WHERE user_pk = ?",
                (self.user.id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        self.assertEqual(int(row["count"]), 0)

    async def test_disallowed_origin_skips_effects(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET stat_points = 2 WHERE id = ?",
                (self.user.id,),
            )
            await db.commit()
        await self.service.enable(self.identity, origin_umo="allowed")
        self.service.effect_whitelist = EffectWhitelist(["different-group"])

        result = await self.service.run_pass(self.user.id, now_ts=1_700_000_000)

        self.assertEqual(result.actions, ())
        updated = await self.users.get_user_by_pk(self.user.id)
        self.assertEqual(updated.stat_points, 2)

    async def test_nefia_theme_uses_shared_operation_seed(self):
        dungeons = tuple(
            types.SimpleNamespace(dungeon_id=value)
            for value in ("alpha", "beta", "gamma")
        )
        fake = SeedDungeonService(dungeons)
        self.service.dungeon_service = fake

        await self.service._run_nefia_step(
            self.user,
            self.identity,
            now_ts=1_700_000_000,
        )

        day_key = daily_growth_day_window(1_700_000_000)[0]
        expected = max(
            dungeons,
            key=lambda dungeon: (
                stable_operation_seed(
                    "nefia-theme-v12",
                    self.identity.group_id,
                    day_key,
                    dungeon.dungeon_id,
                ),
                dungeon.dungeon_id,
            ),
        )
        self.assertEqual(fake.started[0][0], expected.dungeon_id)

    async def test_operations_are_checked_at_most_every_five_minutes(self):
        operations = CountingOperationService()
        self.service.operation_service = operations
        self.service.operation_settlement_service = object()
        await self.service.enable(self.identity, origin_umo="allowed")

        await self.service.run_pass(self.user.id, now_ts=1_700_000_000)
        await self.service.run_pass(self.user.id, now_ts=1_700_000_045)
        await self.service.run_pass(self.user.id, now_ts=1_700_000_300)

        self.assertEqual(operations.daily_calls, 2)
        self.assertEqual(operations.weekly_calls, 2)


if __name__ == "__main__":
    unittest.main()
