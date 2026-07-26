import os
import tempfile
import unittest

from models.attributes import PrimaryAttributes
from services.balance_rules import (
    attack_mode_attribute,
    hit_chance,
    physical_defense_multiplier,
    primary_attribute_factor,
    resistance_multiplier,
    spell_power_multiplier,
)
from services.db import (
    ELONA_BALANCE_BACKUP_SUFFIX,
    ELONA_BALANCE_MIGRATION,
    connect_db,
    init_db,
)
from services.equipment_catalog import (
    EquipmentFactory,
    QUALITY_MULTIPLIERS,
    STARTER_BY_ID,
)
from services.equipment_service import EquipmentService
from services.material_catalog import MATERIAL_DEFINITIONS
from services.skill_service import SkillService
from services.user_service import UserService


class ElonaBalanceRuleTests(unittest.TestCase):
    def test_primary_curve_is_normalized_monotonic_and_diminishing(self):
        self.assertAlmostEqual(primary_attribute_factor(20), 1.0)
        values = [primary_attribute_factor(value) for value in (1, 20, 50, 100)]
        self.assertEqual(values, sorted(values))
        self.assertGreater(
            primary_attribute_factor(2) - primary_attribute_factor(1),
            primary_attribute_factor(100) - primary_attribute_factor(99),
        )

    def test_attack_modes_use_elona_responsibility_attributes(self):
        attrs = {
            "strength": 11,
            "dexterity": 22,
            "perception": 33,
        }
        self.assertEqual(
            attack_mode_attribute("sword_shield", "longsword", attrs), 11
        )
        self.assertEqual(
            attack_mode_attribute("dual_wield", "shortsword", attrs), 22
        )
        self.assertEqual(
            attack_mode_attribute("two_hand_ranged", "bow", attrs), 33
        )

    def test_defense_hit_and_resistance_are_natural_curves(self):
        self.assertGreater(
            physical_defense_multiplier(10, 20),
            physical_defense_multiplier(100, 20),
        )
        self.assertAlmostEqual(resistance_multiplier(50, 10), 0.5)
        self.assertEqual(hit_chance(1, 10_000, is_spell=False), 0.60)
        self.assertEqual(hit_chance(1, 10_000, is_spell=True), 0.55)
        self.assertLessEqual(hit_chance(10_000, 0, is_spell=False), 0.98)

    def test_spell_schools_use_distinct_primary_attributes(self):
        magic_build = {
            "magic": 50, "perception": 1, "willpower": 1,
        }
        perception_build = {
            "magic": 1, "perception": 50, "willpower": 1,
        }
        will_build = {
            "magic": 1, "perception": 1, "willpower": 50,
        }
        self.assertGreater(
            spell_power_multiplier(
                school_id="magic_training",
                school_level=1,
                attributes=magic_build,
            ),
            spell_power_multiplier(
                school_id="magic_training",
                school_level=1,
                attributes=will_build,
            ),
        )
        self.assertGreater(
            spell_power_multiplier(
                school_id="natural_knowledge",
                school_level=1,
                attributes=perception_build,
            ),
            spell_power_multiplier(
                school_id="natural_knowledge",
                school_level=1,
                attributes=magic_build,
            ),
        )
        self.assertGreater(
            spell_power_multiplier(
                school_id="restoration",
                school_level=1,
                attributes=will_build,
            ),
            spell_power_multiplier(
                school_id="restoration",
                school_level=1,
                attributes=perception_build,
            ),
        )

    def test_material_quality_and_resistance_affixes_use_v10_scale(self):
        self.assertEqual(
            QUALITY_MULTIPLIERS,
            {
                "common": 0.67,
                "excellent": 0.83,
                "rare": 1.0,
                "epic": 1.25,
                "mythic": 1.4,
                "legendary": 1.0,
            },
        )
        iron = MATERIAL_DEFINITIONS["iron"]
        self.assertEqual(
            (
                iron.attack_factor,
                iron.defense_factor,
                iron.accuracy_factor,
                iron.evasion_factor,
            ),
            (1.0, 1.0, 1.0, 1.0),
        )
        for material in MATERIAL_DEFINITIONS.values():
            self.assertTrue(0.65 <= material.attack_factor <= 1.45)
            self.assertTrue(0.65 <= material.defense_factor <= 1.45)
            self.assertTrue(0.75 <= material.accuracy_factor <= 1.30)
            self.assertTrue(0.75 <= material.evasion_factor <= 1.30)
        factory = EquipmentFactory()
        item = factory.generate(
            1, STARTER_BY_ID["training_longsword"], 10, "mythic", 9
        )
        for affix in item.random_affixes:
            if affix["type"].startswith("resistance_"):
                self.assertTrue(10 <= affix["value"] <= 50)


class ElonaBalanceMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "balance.db")
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.equipment = EquipmentService(self.db_path)
        self.skills = SkillService(self.db_path)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_reset_preserves_level_history_and_regrants_starters_once(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO users (
                    platform, group_id, user_id, nickname,
                    level, exp, total_exp, stat_points, skill_points,
                    hp, atk, defense, speed, luck, willpower,
                    life_growth, mana_growth, advanced_speed, advanced_luck,
                    wins, losses, created_at, updated_at
                ) VALUES (
                    'test', 'group', 'user', '旧角色',
                    10, 37, 999, 3, 4,
                    20, 21, 22, 23, 24, 25,
                    120, 130, 140, 150,
                    7, 8, datetime('now'), datetime('now')
                )
                """
            )
            cursor = await db.execute(
                "SELECT id FROM users WHERE user_id = 'user'"
            )
            user_pk = int((await cursor.fetchone())["id"])
            await cursor.close()
            user = await self.users.get_user_by_pk_in_db(db, user_pk)
            await self.equipment.ensure_starter_in_db(db, user_pk)
            await self.skills.ensure_initialized_in_db(db, user)
            await db.execute(
                """
                INSERT INTO user_spells
                    (user_pk, spell_id, level, exp, potential)
                VALUES (?, 'magic_arrow', 9, 12, 80)
                """,
                (user_pk,),
            )
            await db.execute(
                """
                INSERT INTO spellbook_items (
                    owner_pk, spell_id, quantity, source,
                    random_seed, bound, created_at
                ) VALUES (?, 'magic_arrow', 1, 'test', 1, 1, datetime('now'))
                """,
                (user_pk,),
            )
            await db.execute(
                """
                INSERT INTO checkins (
                    user_pk, checkin_date, streak_days,
                    exp_gain, created_at
                ) VALUES (?, '2026-07-20', 3, 20, datetime('now'))
                """,
                (user_pk,),
            )
            await db.execute(
                """
                INSERT INTO level_freezes (
                    user_pk, frozen_level, from_level, to_level,
                    frozen_stats_json, frozen_stat_points,
                    frozen_skill_points, status, created_at
                ) VALUES (?, 10, 10, 9, '{"strength": 4}', 3, 2,
                          'frozen', datetime('now'))
                """,
                (user_pk,),
            )
            await db.execute(
                "DELETE FROM schema_migrations WHERE migration_id = ?",
                (ELONA_BALANCE_MIGRATION,),
            )
            await db.commit()

        await init_db(self.db_path)
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT level, exp, total_exp, stat_points, skill_points,
                       hp, atk, defense, speed, luck, willpower,
                       life_growth, mana_growth, advanced_speed,
                       advanced_luck, wins, losses
                FROM users WHERE id = ?
                """,
                (user_pk,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            self.assertEqual((row["level"], row["exp"], row["total_exp"]), (10, 37, 999))
            self.assertEqual((row["stat_points"], row["skill_points"]), (9, 9))
            self.assertEqual(
                tuple(row[key] for key in ("hp", "atk", "defense", "speed", "luck", "willpower")),
                (1, 1, 1, 1, 1, 1),
            )
            self.assertEqual(
                tuple(row[key] for key in ("life_growth", "mana_growth", "advanced_speed", "advanced_luck")),
                (100, 100, 100, 100),
            )
            self.assertEqual((row["wins"], row["losses"]), (7, 8))
            for table in (
                "user_skills", "active_skill_slots", "user_spells",
                "spellbook_items", "equipment_items", "equipment_loadout",
            ):
                cursor = await db.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                )
                self.assertEqual(int((await cursor.fetchone())["count"]), 0)
                await cursor.close()
            cursor = await db.execute(
                "SELECT status, released_at FROM level_freezes"
            )
            freeze = await cursor.fetchone()
            await cursor.close()
            self.assertEqual(freeze["status"], "released")
            self.assertIsNotNone(freeze["released_at"])
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM checkins"
            )
            self.assertEqual(int((await cursor.fetchone())["count"]), 1)
            await cursor.close()

        self.assertTrue(
            os.path.exists(self.db_path + ELONA_BALANCE_BACKUP_SUFFIX)
        )
        user = await self.users.get_user_by_pk(user_pk)
        first_items = await self.equipment.list_items(user_pk)
        first_skills, slots = await self.skills.get_skills(user)
        self.assertTrue(first_items)
        self.assertTrue(first_skills)
        self.assertEqual(slots[0], "power_strike")
        await init_db(self.db_path)
        second_items = await self.equipment.list_items(user_pk)
        second_skills, _ = await self.skills.get_skills(user)
        self.assertEqual(len(second_items), len(first_items))
        self.assertEqual(set(second_skills), set(first_skills))


if __name__ == "__main__":
    unittest.main()
