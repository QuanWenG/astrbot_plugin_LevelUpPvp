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
from services.db import (
    PRIMARY_ATTRIBUTE_REBALANCE_MIGRATION,
    PRIMARY_ATTRIBUTE_REBALANCE_BACKUP_SUFFIX,
    connect_db,
    init_db,
)
from services.equipment_service import EquipmentService
from services.skill_service import SkillService
from services.stat_service import StatService
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
                (1, 1, 1, 1, 1, 1),
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
            backup_path = legacy_path + PRIMARY_ATTRIBUTE_REBALANCE_BACKUP_SUFFIX
            if os.path.exists(backup_path):
                os.remove(backup_path)
    async def test_legacy_columns_expose_six_primary_attributes(self):
        self.assertEqual(self.user.strength, self.user.hp)
        self.assertEqual(self.user.constitution, self.user.defense)
        self.assertEqual(self.user.dexterity, self.user.speed)
        self.assertEqual(self.user.perception, self.user.atk)
        self.assertEqual(self.user.magic, self.user.luck)
        self.assertEqual(self.user.willpower, 1)
        self.assertEqual(
            set(self.user.stats()),
            {
                "strength", "constitution", "dexterity",
                "perception", "magic", "willpower",
            },
        )

    async def test_stat_allocation_is_fixed_one_to_one(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET stat_points = 3 WHERE id = ?",
                (self.user.id,),
            )
            await db.commit()

        result = await StatService(self.db_path, self.users).allocate(
            UserIdentity("test", "group", "attribute-user", "属性测试"),
            "力量",
            3,
        )

        self.assertEqual(result.points_spent, 3)
        self.assertEqual(result.rolls, [1, 1, 1])
        self.assertEqual(result.total_gain, 3)
        self.assertEqual(result.user.strength, 4)
        self.assertEqual(result.user.stat_points, 0)

    async def test_primary_attribute_rebalance_migrates_once(self):
        handle, migration_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        backup_path = migration_path + PRIMARY_ATTRIBUTE_REBALANCE_BACKUP_SUFFIX
        try:
            await init_db(migration_path)
            users = UserService(migration_path)
            identity = UserIdentity("test", "group", "legacy", "旧玩家")
            user = await users.get_or_create_user(identity)
            skills = SkillService(migration_path)
            await skills.get_skills(user)
            equipment = EquipmentService(migration_path)
            items = await equipment.list_items(user.id)

            async with await connect_db(migration_path) as db:
                await AttributeService().ensure_progress_in_db(db, user.id)
                await db.execute(
                    """
                    UPDATE users
                    SET level = 5, exp = 77, total_exp = 999,
                        stat_points = 8, skill_points = 6,
                        hp = 18, atk = 12, defense = 13,
                        speed = 14, luck = 15, willpower = 16,
                        life_growth = 125, mana_growth = 130,
                        advanced_speed = 140, advanced_luck = 150,
                        wins = 9, losses = 4
                    WHERE id = ?
                    """,
                    (user.id,),
                )
                await db.execute(
                    """
                    UPDATE user_attribute_progress
                    SET exp = 88, potential = 250
                    WHERE user_pk = ?
                    """,
                    (user.id,),
                )
                await db.execute(
                    """
                    INSERT INTO level_freezes (
                        user_pk, frozen_level, from_level, to_level,
                        frozen_stats_json, frozen_stat_points,
                        frozen_skill_points, status, created_at
                    ) VALUES (?, 6, 6, 5, '{"strength": 4}', 3, 2, 'frozen', 'x')
                    """,
                    (user.id,),
                )
                await db.execute(
                    "DELETE FROM schema_migrations WHERE migration_id = ?",
                    (PRIMARY_ATTRIBUTE_REBALANCE_MIGRATION,),
                )
                await db.commit()

            await init_db(migration_path)
            migrated = await users.get_user_by_pk(user.id)
            self.assertEqual(migrated.level, 5)
            self.assertEqual(migrated.exp, 77)
            self.assertEqual(migrated.total_exp, 999)
            self.assertEqual(migrated.stat_points, 4)
            self.assertEqual(tuple(migrated.stats().values()), (1, 1, 1, 1, 1, 1))
            self.assertEqual(
                (
                    migrated.life_growth,
                    migrated.mana_growth,
                    migrated.advanced_speed,
                    migrated.advanced_luck,
                ),
                (100, 100, 100, 100),
            )
            self.assertEqual(migrated.skill_points, 6)
            self.assertEqual((migrated.wins, migrated.losses), (9, 4))
            self.assertEqual(len(await equipment.list_items(user.id)), len(items))
            self.assertTrue(os.path.exists(backup_path))
            backup = sqlite3.connect(backup_path)
            try:
                backed_up = backup.execute(
                    "SELECT hp, atk, defense, speed, luck, willpower FROM users WHERE id = ?",
                    (user.id,),
                ).fetchone()
            finally:
                backup.close()
            self.assertEqual(backed_up, (18, 12, 13, 14, 15, 16))

            async with await connect_db(migration_path) as db:
                progress = await AttributeService().progress_in_db(db, user.id)
                cursor = await db.execute(
                    """
                    SELECT frozen_stats_json, frozen_stat_points,
                           frozen_skill_points
                    FROM level_freezes
                    WHERE user_pk = ? AND status = 'frozen'
                    """,
                    (user.id,),
                )
                frozen = await cursor.fetchone()
                await cursor.close()
                await db.execute(
                    "UPDATE users SET hp = 2 WHERE id = ?",
                    (user.id,),
                )
                await db.commit()
            self.assertTrue(
                all(item.exp == 0 and item.potential == 100 for item in progress.values())
            )
            self.assertEqual(frozen["frozen_stats_json"], "{}")
            self.assertEqual(frozen["frozen_stat_points"], 1)
            self.assertEqual(frozen["frozen_skill_points"], 2)

            await init_db(migration_path)
            self.assertEqual((await users.get_user_by_pk(user.id)).strength, 2)
        finally:
            if os.path.exists(migration_path):
                os.remove(migration_path)
            if os.path.exists(backup_path):
                os.remove(backup_path)

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
        self.assertEqual(skill_level_cap(base, ("strength",)), 29)
        self.assertEqual(
            skill_level_cap(base, ("constitution",), "healing"), 5
        )
        self.assertEqual(
            skill_level_cap(base, ("willpower",), "meditation"), 5
        )
        self.assertGreater(
            skill_level_cap(magical, ("strength",)),
            skill_level_cap(base, ("strength",)),
        )
        self.assertGreater(
            skill_level_cap(base, ("strength",)),
            skill_level_cap(base, ("dexterity",)),
        )

    async def test_future_level_skill_points_are_fixed(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET luck = 50 WHERE id = ?", (self.user.id,)
            )
            await db.commit()
        self.user = await self.users.get_user_by_pk(self.user.id)
        async with await connect_db(self.db_path) as db:
            result = await self.users.add_exp_in_db(db, self.user, 100)
            await db.commit()
        self.assertEqual(result.level_ups[0].skill_points_gain, 1)
        self.assertEqual(result.user.skill_points, 1)
        self.assertEqual(result.level_ups[0].stat_points_gain, 1)
        self.assertEqual(result.level_ups[0].auto_growth, {})

    async def test_fixed_skill_points_freeze_and_restore_with_level(self):
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
        self.assertEqual(downgraded.level_downs[0].frozen_skill_points, 1)
        self.assertEqual(downgraded.user.skill_points, 0)
        self.assertEqual(restored.user.skill_points, 1)
        self.assertTrue(restored.level_ups[0].restored_from_freeze)
    async def test_will_and_potential_drive_attribute_growth(self):
        await self.skills.get_skills(self.user)
        async with await connect_db(self.db_path) as db:
            await self.attributes.ensure_progress_in_db(db, self.user.id)
            await db.execute(
                """
                UPDATE user_attribute_progress
                SET exp = 119, potential = 100
                WHERE user_pk = ? AND attribute_id = 'strength'
                """,
                (self.user.id,),
            )
            growth = await self.attributes.apply_battle_growth_in_db(
                db, self.user.id, {"longsword": 20}, None
            )
            await db.commit()
        strength = next(item for item in growth if item.attribute_id == "strength")
        self.assertEqual(strength.exp_gain, 20)
        self.assertEqual(strength.from_value, 1)
        self.assertEqual(strength.to_value, 2)
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
            resistances={key: 50.0 for key in DAMAGE_TYPES},
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
            self.assertEqual(elemental_multiplier(50, 10), 0.5)


if __name__ == "__main__":
    unittest.main()
