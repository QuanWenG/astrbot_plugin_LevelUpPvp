import os
import random
import tempfile
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import patch

from models.attributes import DAMAGE_TYPES, PrimaryAttributes
from models.combat import FighterState
from models.user import UserIdentity
from services.ability_catalog import ACTIVE_ABILITY_DEFINITIONS
from services import config
from services.attribute_service import (
    AttributeService,
    attribute_exp_required,
    elemental_multiplier,
    skill_level_cap,
)
from services.build_service import CombatBuildService
from services.checkin_service import CheckinService
from services.combat_ai import STRATEGY_PROFILES
from services.combat_engine import SideviewCombatEngine
from services.db import (
    ELONA_PROGRESSION_BACKUP_SUFFIX,
    ELONA_PROGRESSION_MIGRATION,
    PRIMARY_ATTRIBUTE_REBALANCE_MIGRATION,
    PRIMARY_ATTRIBUTE_REBALANCE_BACKUP_SUFFIX,
    V11_PROGRESSION_BACKUP_SUFFIX,
    V11_PROGRESSION_MIGRATION,
    connect_db,
    init_db,
)
from services.progression_rules import (
    legacy_attribute_exp_required,
    legacy_level_exp_required,
    migrate_level_exp_preserving_progress,
    migrate_exp_preserving_progress,
    migrate_v10_skill_exp_preserving_progress,
    v10_skill_exp_required,
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

    async def test_missing_historical_marker_never_resets_modern_progress(self):
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
            self.assertEqual(migrated.stat_points, 8)
            self.assertEqual(
                tuple(migrated.stats().values()),
                (18, 13, 14, 12, 15, 16),
            )
            self.assertEqual(
                (
                    migrated.life_growth,
                    migrated.mana_growth,
                    migrated.advanced_speed,
                    migrated.advanced_luck,
                ),
                (125, 130, 140, 150),
            )
            self.assertEqual(migrated.skill_points, 6)
            self.assertEqual((migrated.wins, migrated.losses), (9, 4))
            self.assertEqual(len(await equipment.list_items(user.id)), len(items))
            self.assertFalse(os.path.exists(backup_path))

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
                all(item.exp == 88 and item.potential == 250 for item in progress.values())
            )
            self.assertEqual(frozen["frozen_stats_json"], '{"strength": 4}')
            self.assertEqual(frozen["frozen_stat_points"], 3)
            self.assertEqual(frozen["frozen_skill_points"], 2)

            await init_db(migration_path)
            self.assertEqual((await users.get_user_by_pk(user.id)).strength, 2)
        finally:
            if os.path.exists(migration_path):
                os.remove(migration_path)
            if os.path.exists(backup_path):
                os.remove(backup_path)

    async def test_progression_migration_preserves_fraction_and_all_assets(self):
        handle, migration_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        backup_path = migration_path + ELONA_PROGRESSION_BACKUP_SUFFIX
        v11_backup_path = migration_path + V11_PROGRESSION_BACKUP_SUFFIX
        try:
            await init_db(migration_path)
            users = UserService(migration_path)
            user = await users.get_or_create_user(
                UserIdentity("test", "group", "progression", "迁移玩家")
            )
            skills = SkillService(migration_path)
            await skills.get_skills(user)
            equipment = EquipmentService(migration_path)
            items_before = await equipment.list_items(user.id)
            async with await connect_db(migration_path) as db:
                await AttributeService().ensure_progress_in_db(db, user.id)
                await db.execute(
                    """
                    UPDATE user_attribute_progress
                    SET exp = 60, potential = 0
                    WHERE user_pk = ? AND attribute_id = 'strength'
                    """,
                    (user.id,),
                )
                await db.execute(
                    """
                    UPDATE user_skills
                    SET level = 10, exp = 100, potential = 450
                    WHERE user_pk = ? AND skill_id = 'longsword'
                    """,
                    (user.id,),
                )
                await db.execute(
                    """
                    INSERT INTO user_spells
                        (user_pk, spell_id, level, exp, potential)
                    VALUES (?, 'magic_arrow', 10, 100, 75)
                    """,
                    (user.id,),
                )
                await db.execute(
                    "DELETE FROM schema_migrations WHERE migration_id IN (?, ?)",
                    (
                        ELONA_PROGRESSION_MIGRATION,
                        V11_PROGRESSION_MIGRATION,
                    ),
                )
                await db.commit()

            await init_db(migration_path)
            self.assertTrue(os.path.exists(backup_path))
            async with await connect_db(migration_path) as db:
                progress = await AttributeService().progress_in_db(db, user.id)
                migrated_skills = await skills.skills_in_db(db, user.id)
                cursor = await db.execute(
                    """
                    SELECT level, exp, potential
                    FROM user_spells
                    WHERE user_pk = ? AND spell_id = 'magic_arrow'
                    """,
                    (user.id,),
                )
                spell = await cursor.fetchone()
                await cursor.close()
            expected_attribute = migrate_exp_preserving_progress(
                60,
                legacy_attribute_exp_required(1),
                attribute_exp_required(1),
            )
            self.assertEqual(progress["strength"].exp, expected_attribute)
            self.assertEqual(progress["strength"].potential, 1)
            self.assertEqual(migrated_skills["longsword"].level, 10)
            self.assertEqual(migrated_skills["longsword"].exp, 3400)
            # v11 reads legacy stored potential through the effective 200% cap.
            self.assertEqual(migrated_skills["longsword"].potential, 200)
            self.assertEqual(tuple(spell), (10, 11750, 75))
            self.assertEqual(
                [item.id for item in await equipment.list_items(user.id)],
                [item.id for item in items_before],
            )

            await init_db(migration_path)
            async with await connect_db(migration_path) as db:
                migrated_again = await skills.skills_in_db(db, user.id)
            self.assertEqual(migrated_again["longsword"].exp, 3400)
        finally:
            if os.path.exists(migration_path):
                os.remove(migration_path)
            if os.path.exists(backup_path):
                os.remove(backup_path)
            if os.path.exists(v11_backup_path):
                os.remove(v11_backup_path)

    async def test_v11_marker_supersedes_a_missing_historical_marker(self):
        await self.skills.get_skills(self.user)
        async with await connect_db(self.db_path) as db:
            await self.attributes.ensure_progress_in_db(db, self.user.id)
            await db.execute(
                """
                UPDATE user_skills
                SET level = 10, exp = 1234, potential = 175
                WHERE user_pk = ? AND skill_id = 'longsword'
                """,
                (self.user.id,),
            )
            await db.execute(
                """
                UPDATE user_attribute_progress
                SET exp = 2345, potential = 180
                WHERE user_pk = ? AND attribute_id = 'strength'
                """,
                (self.user.id,),
            )
            await db.execute(
                "DELETE FROM schema_migrations WHERE migration_id = ?",
                (ELONA_PROGRESSION_MIGRATION,),
            )
            await db.commit()

        await init_db(self.db_path)

        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT exp, potential FROM user_skills
                WHERE user_pk = ? AND skill_id = 'longsword'
                """,
                (self.user.id,),
            )
            skill = await cursor.fetchone()
            await cursor.close()
            cursor = await db.execute(
                """
                SELECT exp, potential FROM user_attribute_progress
                WHERE user_pk = ? AND attribute_id = 'strength'
                """,
                (self.user.id,),
            )
            attribute = await cursor.fetchone()
            await cursor.close()
            cursor = await db.execute(
                """
                SELECT migration_id FROM schema_migrations
                WHERE migration_id IN (?, ?)
                """,
                (
                    ELONA_PROGRESSION_MIGRATION,
                    V11_PROGRESSION_MIGRATION,
                ),
            )
            markers = {
                row["migration_id"] for row in await cursor.fetchall()
            }
            await cursor.close()

        self.assertEqual(tuple(skill), (1234, 175))
        self.assertEqual(tuple(attribute), (2345, 180))
        self.assertEqual(
            markers,
            {ELONA_PROGRESSION_MIGRATION, V11_PROGRESSION_MIGRATION},
        )
        self.assertFalse(
            os.path.exists(
                self.db_path + ELONA_PROGRESSION_BACKUP_SUFFIX
            )
        )

    async def test_v10_marker_selects_exactly_one_v11_conversion(self):
        backup_path = self.db_path + V11_PROGRESSION_BACKUP_SUFFIX
        self.addCleanup(
            lambda: os.path.exists(backup_path) and os.remove(backup_path)
        )
        await self.skills.get_skills(self.user)
        old_level_exp = legacy_level_exp_required(10) // 2
        old_skill_exp = v10_skill_exp_required(10) // 2
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET level = 10, exp = ? WHERE id = ?",
                (old_level_exp, self.user.id),
            )
            await db.execute(
                """
                UPDATE user_skills
                SET level = 10, exp = ?, potential = 175
                WHERE user_pk = ? AND skill_id = 'longsword'
                """,
                (old_skill_exp, self.user.id),
            )
            await db.execute(
                "DELETE FROM schema_migrations WHERE migration_id = ?",
                (V11_PROGRESSION_MIGRATION,),
            )
            await db.commit()

        await init_db(self.db_path)
        expected_user_exp = migrate_level_exp_preserving_progress(
            10,
            old_level_exp,
        )
        expected_skill_exp = migrate_v10_skill_exp_preserving_progress(
            10,
            old_skill_exp,
        )
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT exp FROM users WHERE id = ?",
                (self.user.id,),
            )
            user_exp = int((await cursor.fetchone())["exp"])
            await cursor.close()
            cursor = await db.execute(
                """
                SELECT exp FROM user_skills
                WHERE user_pk = ? AND skill_id = 'longsword'
                """,
                (self.user.id,),
            )
            skill_exp = int((await cursor.fetchone())["exp"])
            await cursor.close()

        self.assertEqual(user_exp, expected_user_exp)
        self.assertEqual(skill_exp, expected_skill_exp)

        await init_db(self.db_path)
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT exp FROM users WHERE id = ?",
                (self.user.id,),
            )
            repeated_user_exp = int((await cursor.fetchone())["exp"])
            await cursor.close()
            cursor = await db.execute(
                """
                SELECT exp FROM user_skills
                WHERE user_pk = ? AND skill_id = 'longsword'
                """,
                (self.user.id,),
            )
            repeated_skill_exp = int((await cursor.fetchone())["exp"])
            await cursor.close()
        self.assertEqual(repeated_user_exp, expected_user_exp)
        self.assertEqual(repeated_skill_exp, expected_skill_exp)

    async def test_progression_migration_aborts_before_writes_when_backup_fails(self):
        async with await connect_db(self.db_path) as db:
            await self.attributes.ensure_progress_in_db(db, self.user.id)
            await db.execute(
                """
                UPDATE user_attribute_progress
                SET exp = 123
                WHERE user_pk = ? AND attribute_id = 'strength'
                """,
                (self.user.id,),
            )
            await db.execute(
                "DELETE FROM schema_migrations WHERE migration_id IN (?, ?)",
                (
                    ELONA_PROGRESSION_MIGRATION,
                    V11_PROGRESSION_MIGRATION,
                ),
            )
            await db.commit()

        with patch(
            "services.db._backup_database_with_suffix",
            side_effect=RuntimeError("backup failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "backup failed"):
                await init_db(self.db_path)

        async with await connect_db(self.db_path) as db:
            progress = await self.attributes.progress_in_db(db, self.user.id)
            cursor = await db.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
                (ELONA_PROGRESSION_MIGRATION,),
            )
            migration = await cursor.fetchone()
            await cursor.close()
        self.assertEqual(progress["strength"].exp, 123)
        self.assertIsNone(migration)

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
        self.assertEqual(skill_level_cap(base, ("strength",)), 36)
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
            leveled = await self.users.add_exp_in_db(
                db,
                self.user,
                config.exp_required_for_next_level(self.user.level),
            )
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
                SET exp = ?, potential = 100
                WHERE user_pk = ? AND attribute_id = 'strength'
                """,
                (attribute_exp_required(1) - 2020, self.user.id),
            )
            growth = await self.attributes.apply_battle_growth_in_db(
                db, self.user.id, {"longsword": 20}, None
            )
            cursor = await db.execute(
                """
                SELECT rules_version
                FROM attribute_growth_logs
                WHERE user_pk = ?
                ORDER BY id DESC LIMIT 1
                """,
                (self.user.id,),
            )
            rules_version = (await cursor.fetchone())["rules_version"]
            await cursor.close()
            await db.commit()
        strength = next(item for item in growth if item.attribute_id == "strength")
        self.assertEqual(strength.exp_gain, 20)
        self.assertEqual(strength.from_value, 1)
        self.assertEqual(strength.to_value, 2)
        self.assertEqual(strength.potential_after, 96)
        self.assertEqual(rules_version, "elona-scaled-v2")

    async def test_one_percent_attribute_potential_still_accumulates(self):
        await self.skills.get_skills(self.user)
        async with await connect_db(self.db_path) as db:
            await self.attributes.ensure_progress_in_db(db, self.user.id)
            await db.execute(
                """
                UPDATE user_attribute_progress
                SET exp = 0, potential = 1
                WHERE user_pk = ? AND attribute_id = 'strength'
                """,
                (self.user.id,),
            )
            await self.attributes.apply_battle_growth_in_db(
                db, self.user.id, {"longsword": 1}, None
            )
            progress = await self.attributes.progress_in_db(db, self.user.id)
            await db.commit()
        self.assertEqual(progress["strength"].exp, 1)

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
            critical_rate=0.0,
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
        runtime = SideviewCombatEngine().ability_runtime
        definition = ACTIVE_ABILITY_DEFINITIONS["power_strike"]
        actor_state = FighterState(attacker, attacker.max_hp, 0)
        open_state = FighterState(open_target, open_target.max_hp, 0)
        resistant_state = FighterState(
            resistant_target,
            resistant_target.max_hp,
            0,
        )
        open_hit = runtime.damage_result(
            actor_state,
            open_state,
            definition,
            random.Random(77),
        )
        resistant_hit = runtime.damage_result(
            actor_state,
            resistant_state,
            definition,
            random.Random(77),
        )
        open_breakdown = open_hit[-1]
        resistant_breakdown = resistant_hit[-1]
        self.assertEqual(
            set(open_breakdown) - {"physical"},
            set(DAMAGE_TYPES),
        )
        self.assertLess(resistant_hit[0], open_hit[0])
        for damage_type in DAMAGE_TYPES:
            self.assertLess(
                resistant_breakdown[damage_type],
                open_breakdown[damage_type],
            )
            self.assertEqual(elemental_multiplier(50, 10), 0.75)


if __name__ == "__main__":
    unittest.main()
