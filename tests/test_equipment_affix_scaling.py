import os
import tempfile
import unittest
from dataclasses import replace

from models.user import UserIdentity
from services.build_service import CombatBuildService
from services.db import (
    CLASSIC_BLACK_STAR_EFFECTS_MIGRATION,
    CLASSIC_BLACK_STAR_LEVEL_MIGRATION,
    CLASSIC_BLACK_STAR_TEMPLATE_IDS,
    connect_db,
    init_db,
)
from services.equipment_affixes import (
    effective_inherent_affix_value,
    inherent_affix_level_ratio,
    skill_level_affix_cap,
)
from services.equipment_catalog import EquipmentFactory, STARTER_BY_ID
from services.equipment_service import EquipmentService
from services.skill_service import SkillService
from services.user_service import UserService


class EquipmentAffixRuleTests(unittest.TestCase):
    def test_skill_level_cap_boundaries(self):
        cases = {
            0: 1,
            1: 2,
            20: 2,
            21: 4,
            40: 4,
            41: 7,
            60: 7,
            61: 9,
            80: 9,
            81: 11,
            100: 11,
        }
        self.assertEqual(
            {level: skill_level_affix_cap(level) for level in cases},
            cases,
        )

    def test_inherent_values_are_capped_by_character_and_original_value(self):
        skill_five = {"type": "skill_level", "value": 5}
        skill_two = {"type": "skill_level", "value": 2}
        numeric = {"type": "block_rate", "value": 0.12}

        self.assertEqual(
            effective_inherent_affix_value(skill_five, 20, 40),
            2,
        )
        self.assertEqual(
            effective_inherent_affix_value(skill_five, 30, 40),
            4,
        )
        self.assertEqual(
            effective_inherent_affix_value(skill_two, 30, 40),
            2,
        )
        self.assertEqual(
            effective_inherent_affix_value(skill_five, 40, 40),
            5,
        )
        self.assertAlmostEqual(
            effective_inherent_affix_value(numeric, 20, 40),
            0.06,
        )
        self.assertAlmostEqual(inherent_affix_level_ratio(80, 40), 1.0)
        self.assertEqual(
            effective_inherent_affix_value(numeric, 1, 0),
            0.12,
        )

    def test_generated_skill_affixes_respect_item_level_cap(self):
        factory = EquipmentFactory()
        template = STARTER_BY_ID["training_longsword"]
        for level in (0, 20, 21, 40, 41, 60, 61, 80, 81, 100):
            found = []
            for seed in range(500):
                item = factory.generate(1, template, level, "mythic", seed)
                found.extend(
                    int(affix["value"])
                    for affix in item.random_affixes
                    if affix["type"] == "skill_level"
                )
            self.assertTrue(found, f"Lv.{level} 未生成技能等级词条")
            self.assertTrue(
                all(1 <= value <= skill_level_affix_cap(level) for value in found)
            )


class EquipmentAffixBuildTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.equipment = EquipmentService(self.db_path)
        self.skills = SkillService(self.db_path)
        self.builds = CombatBuildService(self.equipment, self.skills)
        self.user = await self.users.get_or_create_user(
            UserIdentity("test", "group", "affix", "Affix")
        )

    async def asyncTearDown(self):
        os.remove(self.db_path)

    async def test_build_scales_only_inherent_affixes(self):
        items = await self.equipment.list_items(self.user.id)
        sword = next(
            item for item in items
            if item.template_id == "training_longsword"
        )
        scaled = replace(
            sword,
            item_level=40,
            inherent_affixes=(
                {"type": "skill_level", "skill_id": "longsword", "value": 5},
                {"type": "stat_flat", "stat": "strength", "value": 8},
                {"type": "block_rate", "value": 0.2},
            ),
            random_affixes=(
                {"type": "skill_level", "skill_id": "longsword", "value": 3},
                {"type": "stat_flat", "stat": "strength", "value": 2},
            ),
        )
        control = replace(
            scaled,
            inherent_affixes=(),
            random_affixes=(),
        )
        skills, _ = await self.skills.get_skills(self.user)

        low = self.builds.resolve_equipment(
            self.user,
            {"main_hand": scaled.id},
            [scaled],
            skills,
        )
        low_control = self.builds.resolve_equipment(
            self.user,
            {"main_hand": control.id},
            [control],
            skills,
        )
        self.assertEqual(
            low.skill_modifiers["longsword"]
            - low_control.skill_modifiers.get("longsword", 0),
            5,
        )
        self.assertAlmostEqual(
            low.stat_modifiers["strength"]
            - low_control.stat_modifiers.get("strength", 0),
            2,
        )
        self.assertAlmostEqual(low.block_rate - low_control.block_rate, 0.005)

        full_user = replace(self.user, level=40)
        full = self.builds.resolve_equipment(
            full_user,
            {"main_hand": scaled.id},
            [scaled],
            skills,
        )
        full_control = self.builds.resolve_equipment(
            full_user,
            {"main_hand": control.id},
            [control],
            skills,
        )
        self.assertEqual(
            full.skill_modifiers["longsword"]
            - full_control.skill_modifiers.get("longsword", 0),
            8,
        )
        self.assertAlmostEqual(
            full.stat_modifiers["strength"]
            - full_control.stat_modifiers.get("strength", 0),
            10,
        )
        self.assertAlmostEqual(full.block_rate - full_control.block_rate, 0.2)

    async def test_black_star_level_migration_updates_inventory_in_place(self):
        for catalog_id in range(4001, 4021):
            await self.equipment.grant_catalog_item([self.user.id], catalog_id)
        items = await self.equipment.list_items(self.user.id)
        dagger = next(
            item for item in items
            if item.template_id == "black_star_ether_dagger"
        )
        await self.equipment.equip(self.user.id, dagger.id, "main_hand")

        async with await connect_db(self.db_path) as db:
            placeholders = ", ".join("?" for _ in CLASSIC_BLACK_STAR_TEMPLATE_IDS)
            await db.execute(
                f"""
                UPDATE equipment_items SET item_level = 100
                WHERE template_id IN ({placeholders})
                """,
                CLASSIC_BLACK_STAR_TEMPLATE_IDS,
            )
            await db.execute(
                "DELETE FROM schema_migrations WHERE migration_id = ?",
                (CLASSIC_BLACK_STAR_LEVEL_MIGRATION,),
            )
            await db.commit()

        await init_db(self.db_path)
        await init_db(self.db_path)
        migrated = await self.equipment.list_items(self.user.id)
        migrated_black_stars = [
            item for item in migrated
            if item.template_id in CLASSIC_BLACK_STAR_TEMPLATE_IDS
        ]
        slots, _ = await self.equipment.get_loadout(self.user.id)

        self.assertEqual(len(migrated_black_stars), 20)
        self.assertEqual({item.item_level for item in migrated_black_stars}, {40})
        self.assertEqual(slots["main_hand"], dagger.id)
        self.assertEqual(
            next(
                item.item_level for item in migrated
                if item.template_id == "training_longsword"
            ),
            0,
        )
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations "
                "WHERE migration_id = ?",
                (CLASSIC_BLACK_STAR_LEVEL_MIGRATION,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        self.assertEqual(row["count"], 1)

    async def test_black_star_effect_migration_preserves_instance_progress(self):
        await self.equipment.grant_catalog_item([self.user.id], 4001)
        item = next(
            item
            for item in await self.equipment.list_items(self.user.id)
            if item.template_id == "black_star_ether_dagger"
        )
        await self.equipment.equip(self.user.id, item.id, "main_hand")
        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                UPDATE equipment_items
                SET description = '旧介绍',
                    base_stats_json = '{"weapon_power": 1}',
                    inherent_affixes_json = '[]',
                    source_effects_json = '[]',
                    material = 'gold',
                    enhancement_level = 7
                WHERE id = ?
                """,
                (item.id,),
            )
            await db.execute(
                "DELETE FROM schema_migrations WHERE migration_id = ?",
                (CLASSIC_BLACK_STAR_EFFECTS_MIGRATION,),
            )
            await db.commit()

        await init_db(self.db_path)
        await init_db(self.db_path)
        migrated = await self.equipment.item_detail(self.user.id, item.id)
        slots, _ = await self.equipment.get_loadout(self.user.id)

        self.assertEqual(migrated.description, "被风缠绕的短剑。")
        self.assertEqual(migrated.source_effects, ("加速以太病的发展",))
        self.assertGreater(len(migrated.inherent_affixes), 0)
        self.assertEqual(migrated.material, "gold")
        self.assertEqual(migrated.enhancement_level, 7)
        self.assertEqual(slots["main_hand"], item.id)
