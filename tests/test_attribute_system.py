import os
import tempfile
import sqlite3
import unittest
from dataclasses import replace

from models.attributes import DAMAGE_TYPES, PrimaryAttributes
from models.user import UserIdentity
from services.attribute_service import (
    AttributeService,
    elemental_multiplier,
    skill_level_cap,
)
from services.build_service import CombatBuildService
from services.checkin_service import CheckinService
from services.combat_ai import STRATEGY_PROFILES
from services.combat_engine import SideviewCombatEngine
from services.db import connect_db, init_db
from services.equipment_service import EquipmentService
from services.skill_service import SkillService
from services.user_service import UserService


class AttributeSystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.attributes = AttributeService()
        self.equipment = EquipmentService(self.db_path)
        self.skills = SkillService(self.db_path)
        self.builds = CombatBuildService(
            self.equipment, self.skills, self.attributes
        )
        self.user = await self.users.get_or_create_user(
            UserIdentity("test", "group", "attribute-user", "属性测试")
        )

    async def asyncTearDown(self):
        os.remove(self.db_path)

    async def _snapshot(self, strategy="全力猛攻"):
        async with await connect_db(self.db_path) as db:
            snapshot = await self.builds.snapshot_in_db(
                db, self.user, strategy
            )
            await db.commit()
        return snapshot

    async def test_legacy_user_schema_gains_willpower_and_progress_tables(self):
        handle, legacy_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            connection = sqlite3.connect(legacy_path)
            connection.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '',
                    level INTEGER NOT NULL DEFAULT 1, exp INTEGER NOT NULL DEFAULT 0,
                    total_exp INTEGER NOT NULL DEFAULT 0,
                    stat_points INTEGER NOT NULL DEFAULT 0,
                    level_up_count INTEGER NOT NULL DEFAULT 0,
                    hp INTEGER NOT NULL DEFAULT 10, atk INTEGER NOT NULL DEFAULT 5,
                    defense INTEGER NOT NULL DEFAULT 5, speed INTEGER NOT NULL DEFAULT 5,
                    luck INTEGER NOT NULL DEFAULT 5, wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, group_id, user_id)
                );
                INSERT INTO users (
                    platform, group_id, user_id, nickname, hp, atk,
                    defense, speed, luck, created_at, updated_at
                ) VALUES ('old', 'g', 'u', '旧角色', 12, 8, 7, 6, 9, 'x', 'x');
                """
            )
            connection.commit()
            connection.close()
            await init_db(legacy_path)
            legacy_user = await UserService(legacy_path).get_user_by_pk(1)
            self.assertEqual(
                (
                    legacy_user.strength,
                    legacy_user.constitution,
                    legacy_user.dexterity,
                    legacy_user.perception,
                    legacy_user.magic,
                    legacy_user.willpower,
                ),
                (12, 7, 6, 8, 9, 5),
            )
            async with await connect_db(legacy_path) as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                tables = {row["name"] for row in await cursor.fetchall()}
                await cursor.close()
            self.assertIn("user_attribute_progress", tables)
            self.assertIn("attribute_growth_logs", tables)
            self.assertIn("advanced_attribute_logs", tables)
        finally:
            os.remove(legacy_path)
    async def test_legacy_columns_expose_six_primary_attributes(self):
        self.assertEqual(self.user.strength, self.user.hp)
        self.assertEqual(self.user.constitution, self.user.defense)
        self.assertEqual(self.user.dexterity, self.user.speed)
        self.assertEqual(self.user.perception, self.user.atk)
        self.assertEqual(self.user.magic, self.user.luck)
        self.assertEqual(self.user.willpower, 5)
        self.assertEqual(
            set(self.user.stats()),
            {
                "strength", "constitution", "dexterity",
                "perception", "magic", "willpower",
            },
        )

    async def test_snapshot_contains_resources_and_independent_action_speed(self):
        first = await self._snapshot()
        self.assertGreater(first.max_hp, 0)
        self.assertGreater(first.max_mp, 0)
        self.assertGreater(first.max_sp, 0)
        self.assertIsNotNone(first.derived)
        self.assertIsNotNone(first.attributes)

        original_speed = first.derived.action_speed
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET speed = speed + 50 WHERE id = ?",
                (self.user.id,),
            )
            await db.commit()
        self.user = await self.users.get_user_by_pk(self.user.id)
        second = await self._snapshot()
        self.assertEqual(second.derived.action_speed, original_speed)
        self.assertGreater(second.derived.evasion, first.derived.evasion)

    def test_skill_caps_use_governing_attribute_and_global_magic(self):
        base = PrimaryAttributes(10, 5, 5, 5, 5, 5)
        magical = replace(base, magic=25)
        self.assertEqual(skill_level_cap(base, ("strength",)), 42)
        self.assertGreater(
            skill_level_cap(magical, ("strength",)),
            skill_level_cap(base, ("strength",)),
        )
        self.assertGreater(
            skill_level_cap(base, ("strength",)),
            skill_level_cap(base, ("dexterity",)),
        )

    async def test_magic_increases_future_level_skill_points(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET luck = 50 WHERE id = ?", (self.user.id,)
            )
            await db.commit()
        self.user = await self.users.get_user_by_pk(self.user.id)
        async with await connect_db(self.db_path) as db:
            result = await self.users.add_exp_in_db(db, self.user, 100)
            await db.commit()
        self.assertEqual(result.level_ups[0].skill_points_gain, 3)
        self.assertEqual(result.user.skill_points, 3)

    async def test_magic_bonus_skill_points_freeze_and_restore_with_level(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET luck = 50 WHERE id = ?", (self.user.id,)
            )
            self.user = await self.users.get_user_by_pk_in_db(db, self.user.id)
            leveled = await self.users.add_exp_in_db(db, self.user, 100)
            downgraded = await self.users.deduct_exp_in_db(
                db, leveled.user, 1
            )
            restored = await self.users.add_exp_in_db(
                db, downgraded.user, 1
            )
            await db.commit()
        self.assertEqual(downgraded.level_downs[0].frozen_skill_points, 3)
        self.assertEqual(downgraded.user.skill_points, 0)
        self.assertEqual(restored.user.skill_points, 3)
        self.assertTrue(restored.level_ups[0].restored_from_freeze)
    async def test_will_and_potential_drive_attribute_growth(self):
        await self.skills.get_skills(self.user)
        async with await connect_db(self.db_path) as db:
            await self.attributes.ensure_progress_in_db(db, self.user.id)
            await db.execute(
                """
                UPDATE user_attribute_progress
                SET exp = 299, potential = 100
                WHERE user_pk = ? AND attribute_id = 'strength'
                """,
                (self.user.id,),
            )
            growth = await self.attributes.apply_battle_growth_in_db(
                db, self.user.id, {"longsword": 20}, None
            )
            await db.commit()
        strength = next(item for item in growth if item.attribute_id == "strength")
        self.assertEqual(strength.exp_gain, 21)
        self.assertEqual(strength.from_value, 10)
        self.assertEqual(strength.to_value, 11)
        self.assertEqual(strength.potential_after, 50)

    async def test_successful_checkin_restores_each_attribute_potential_once(self):
        async with await connect_db(self.db_path) as db:
            await self.attributes.ensure_progress_in_db(db, self.user.id)
            await db.execute(
                "UPDATE user_attribute_progress SET potential = 50 WHERE user_pk = ?",
                (self.user.id,),
            )
            await db.commit()
        checkins = CheckinService(self.db_path, self.users, self.attributes)
        identity = UserIdentity("test", "group", "attribute-user", "属性测试")
        first = await checkins.checkin(identity)
        second = await checkins.checkin(identity)
        self.assertEqual(first.attribute_potential_restore, 60)
        self.assertEqual(second.attribute_potential_restore, 0)
        async with await connect_db(self.db_path) as db:
            progress = await self.attributes.progress_in_db(db, self.user.id)
        self.assertTrue(all(item.potential == 60 for item in progress.values()))

    async def test_all_resistances_reduce_matching_mixed_damage(self):
        snapshot = await self._snapshot()
        attacker_derived = replace(
            snapshot.derived,
            elemental_damage={key: 12.0 for key in DAMAGE_TYPES},
        )
        open_defense = replace(
            snapshot.derived,
            resistances={key: 0.0 for key in DAMAGE_TYPES},
        )
        resistant_defense = replace(
            snapshot.derived,
            resistances={key: 0.5 for key in DAMAGE_TYPES},
        )
        attacker = replace(
            snapshot, user_pk=1, name="攻击者", derived=attacker_derived
        )
        open_target = replace(
            snapshot, user_pk=2, name="无耐性", derived=open_defense
        )
        resistant_target = replace(
            snapshot, user_pk=2, name="有耐性", derived=resistant_defense
        )
        engine = SideviewCombatEngine()
        profile = STRATEGY_PROFILES["全力猛攻"]
        open_result = engine.simulate(attacker, open_target, profile, profile, 77)
        resistant_result = engine.simulate(
            attacker, resistant_target, profile, profile, 77
        )
        open_hit = next(
            event
            for event in open_result.events
            if event.kind == "damage" and event.actor_pk == 1
        )
        resistant_hit = next(
            event
            for event in resistant_result.events
            if event.kind == "damage" and event.actor_pk == 1
        )
        self.assertEqual(set(open_hit.damage_breakdown) - {"physical"}, set(DAMAGE_TYPES))
        self.assertLess(resistant_hit.value, open_hit.value)
        for damage_type in DAMAGE_TYPES:
            self.assertLess(
                resistant_hit.damage_breakdown[damage_type],
                open_hit.damage_breakdown[damage_type],
            )
            self.assertEqual(elemental_multiplier(0.5), 0.5)


if __name__ == "__main__":
    unittest.main()
