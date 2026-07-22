import os
import tempfile
import unittest
from dataclasses import replace

from models.user import UserIdentity
from services.battle_service import BattleService
from services.build_service import CombatBuildService
from services.combat_ai import STRATEGY_PROFILES
from services.combat_engine import SideviewCombatEngine
from services.db import connect_db, init_db
from services.equipment_catalog import EquipmentFactory, STARTER_BY_ID
from services.equipment_service import EquipmentService
from services.skill_service import SkillService
from services.user_service import UserService


class EquipmentSkillSystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.equipment = EquipmentService(self.db_path)
        self.skills = SkillService(self.db_path)
        self.builds = CombatBuildService(self.equipment, self.skills)
        self.user = await self.users.get_or_create_user(
            UserIdentity("test", "group", "one", "One")
        )

    async def asyncTearDown(self):
        os.remove(self.db_path)

    async def test_starter_grants_are_idempotent_and_default_to_sword_shield(self):
        first = await self.equipment.list_items(self.user.id)
        second = await self.equipment.list_items(self.user.id)
        slots, equipped = await self.equipment.get_loadout(self.user.id)
        skills, _ = await self.skills.get_skills(self.user)
        build = self.builds.resolve_equipment(
            self.user, slots, equipped, skills
        )

        self.assertEqual(len(first), len(STARTER_BY_ID))
        self.assertEqual([item.id for item in first], [item.id for item in second])
        self.assertEqual(build.weapon_mode, "sword_shield")
        self.assertEqual(build.armor_style, "light")
        self.assertLessEqual(build.total_weight, 15)

    async def test_all_starter_weapon_modes_resolve_with_expected_ranges(self):
        items = await self.equipment.list_items(self.user.id)
        by_template = {item.template_id: item for item in items}
        cases = (
            ("training_greataxe", "two_hand_heavy", 110),
            ("training_spear", "two_hand_melee", 150),
            ("training_bow", "two_hand_ranged", 350),
            ("training_firearm", "two_hand_ranged", 450),
            ("training_throwing", "two_hand_ranged", 250),
        )
        for template_id, expected_mode, expected_range in cases:
            await self.equipment.equip(self.user.id, by_template[template_id].id)
            async with await connect_db(self.db_path) as db:
                snapshot = await self.builds.snapshot_in_db(
                    db, self.user, "稳扎稳打"
                )
                await db.commit()
            self.assertEqual(snapshot.weapon_mode, expected_mode)
            self.assertEqual(snapshot.equipment.attack_range, expected_range)
    async def test_two_handed_item_occupies_both_hands_and_conflicts_are_atomic(self):
        items = await self.equipment.list_items(self.user.id)
        axe = next(item for item in items if item.template_id == "training_greataxe")
        dagger = next(item for item in items if item.template_id == "training_dagger_left")

        await self.equipment.equip(self.user.id, axe.id)
        slots, _ = await self.equipment.get_loadout(self.user.id)
        self.assertEqual(slots["main_hand"], axe.id)
        self.assertEqual(slots["off_hand"], axe.id)

        await self.equipment.equip(self.user.id, dagger.id, "main_hand")
        slots, _ = await self.equipment.get_loadout(self.user.id)
        self.assertEqual(slots["main_hand"], dagger.id)
        self.assertNotIn("off_hand", slots)

    async def test_character_level_skill_point_freezes_and_restores(self):
        await self.skills.get_skills(self.user)
        async with await connect_db(self.db_path) as db:
            current = await self.users.get_user_by_pk_in_db(db, self.user.id)
            gained = await self.users.add_exp_in_db(db, current, 100)
            self.assertEqual(gained.user.level, 2)
            self.assertEqual(gained.user.skill_points, 1)
            self.assertEqual(gained.level_ups[0].skill_points_gain, 1)
            lost = await self.users.deduct_exp_in_db(db, gained.user, 1)
            self.assertEqual(lost.user.level, 1)
            self.assertEqual(lost.user.skill_points, 0)
            self.assertEqual(lost.level_downs[0].frozen_skill_points, 1)
            restored = await self.users.add_exp_in_db(db, lost.user, 1)
            await db.commit()
        self.assertEqual(restored.user.level, 2)
        self.assertEqual(restored.user.skill_points, 1)
        self.assertTrue(restored.level_ups[0].restored_from_freeze)
    async def test_capacity_and_black_star_invariants_are_enforced(self):
        factory = EquipmentFactory()
        template = STARTER_BY_ID["training_longsword"]
        epic = factory.generate(self.user.id, template, 20, "epic", 5)
        with self.assertRaises(ValueError):
            replace(epic, enchant_capacity=0)
        with self.assertRaises(ValueError):
            factory.generate(self.user.id, template, 20, "legendary", 5)
        black = replace(
            epic,
            quality="legendary",
            star_type="black_star",
            enchant_capacity=3,
            used_capacity=3,
            random_affixes=(),
            fusion_affixes=(
                {"type": "stat_flat", "value": 1},
                {"type": "stat_flat", "value": 1},
                {"type": "stat_flat", "value": 1},
            ),
        )
        self.assertEqual(black.fusion_slot_limit, 3)
    async def test_skill_initialization_retroactive_points_and_potential_tiers(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET level = 5 WHERE id = ?", (self.user.id,)
            )
            await db.commit()
        self.user = await self.users.get_user_by_pk(self.user.id)
        learned, slots = await self.skills.get_skills(self.user)
        self.assertEqual(self.user.skill_points, 4)
        self.assertIn("longsword", learned)
        self.assertEqual(slots[0], "power_strike")

        trained = await self.skills.train_potential(self.user, "长剑", 2)
        self.assertEqual(trained.potential, 140)
        with self.assertRaises(ValueError):
            await self.skills.learn(self.user, "长剑")

    async def test_skill_initialization_does_not_duplicate_new_level_points(self):
        other = await self.users.get_or_create_user(
            UserIdentity("test", "group", "new", "New")
        )
        async with await connect_db(self.db_path) as db:
            leveled = await self.users.add_exp_in_db(db, other, 100)
            await db.commit()
        self.assertEqual(leveled.user.level, 2)
        self.assertEqual(leveled.user.skill_points, 1)
        await self.skills.get_skills(leveled.user)
        refreshed = await self.users.get_user_by_pk(leveled.user.id)
        self.assertEqual(refreshed.skill_points, 1)
    async def test_factory_is_seeded_and_marks_white_star(self):
        factory = EquipmentFactory()
        template = STARTER_BY_ID["training_longsword"]
        first = factory.generate(self.user.id, template, 50, "epic", 42)
        replay = factory.generate(self.user.id, template, 50, "epic", 42)
        self.assertEqual(first.to_dict(), replay.to_dict())
        self.assertEqual(first.star_type, "white_star")
        self.assertLessEqual(first.used_capacity, first.enchant_capacity)

    async def test_weight_level_penalty_and_skill_levelup_rules(self):
        items = await self.equipment.list_items(self.user.id)
        sword = next(item for item in items if item.template_id == "training_longsword")
        high_level = replace(sword, item_level=100, weight=31.0, base_stats={"weapon_power": 100})
        skills, _ = await self.skills.get_skills(self.user)
        build = self.builds.resolve_equipment(
            self.user, {"main_hand": high_level.id}, [high_level], skills
        )
        self.assertEqual(build.armor_style, "heavy")
        self.assertTrue(build.overloaded)
        self.assertEqual(build.weapon_power, 50)

        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE user_skills SET exp = 64, potential = 200 WHERE user_pk = ? AND skill_id = 'longsword'",
                (self.user.id,),
            )
            growth = await self.skills.apply_growth_in_db(
                db, self.user.id, {"longsword": 1}, None
            )
            await db.commit()
        self.assertEqual(growth[0].to_level, 2)
        self.assertEqual(growth[0].potential_after, 100)

    async def test_dual_wield_produces_a_second_damage_segment(self):
        items = await self.equipment.list_items(self.user.id)
        left = next(item for item in items if item.template_id == "training_dagger_left")
        right = next(item for item in items if item.template_id == "training_dagger_right")
        await self.equipment.equip(self.user.id, left.id, "main_hand")
        await self.equipment.equip(self.user.id, right.id, "off_hand")
        async with await connect_db(self.db_path) as db:
            snapshot = await self.builds.snapshot_in_db(db, self.user, "全力猛攻")
            await db.commit()
        defender = replace(snapshot, user_pk=999, name="Two")
        result = SideviewCombatEngine().simulate(
            snapshot, defender, STRATEGY_PROFILES["全力猛攻"],
            STRATEGY_PROFILES["全力猛攻"], 31,
        )
        self.assertEqual(snapshot.weapon_mode, "dual_wield")
        self.assertTrue(any(event.kind == "followup_trigger" for event in result.events))
        self.assertTrue(any(event.kind == "followup" for event in result.events))
    async def test_battle_settlement_persists_both_sides_skill_growth(self):
        service = BattleService(
            self.db_path, self.users, _NoopLLM(), self.equipment, self.skills
        )
        result = await service.battle(
            UserIdentity("test", "group", "one", "One"),
            UserIdentity("test", "group", "two", "Two"),
            "全力猛攻",
        )
        self.assertTrue(result.skill_growths)
        self.assertTrue(result.attribute_growths)
        self.assertIsNotNone(result.simulation.attacker.equipment)
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) AS count FROM skill_growth_logs")
            row = await cursor.fetchone()
            await cursor.close()
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM attribute_growth_logs"
            )
            attribute_row = await cursor.fetchone()
            await cursor.close()
        self.assertGreater(row["count"], 0)
        self.assertGreater(attribute_row["count"], 0)
    async def test_combat_snapshot_uses_v5_resources_skill_and_growth(self):
        async with await connect_db(self.db_path) as db:
            snapshot = await self.builds.snapshot_in_db(
                db, self.user, "全力猛攻"
            )
            await db.commit()
        result = SideviewCombatEngine().simulate(
            snapshot,
            snapshot,
            STRATEGY_PROFILES["全力猛攻"],
            STRATEGY_PROFILES["全力猛攻"],
            17,
        )
        self.assertEqual(result.engine_version, "sideview-v9")
        self.assertTrue(any(event.stamina is not None for event in result.events))
        self.assertTrue(any(event.kind == "skill_use" for event in result.events))
        usage = self.skills.usage_from_simulation(result)
        self.assertIn("longsword", usage[self.user.id])

class _NoopLLM:
    async def describe_simulation_result(self, *args, **kwargs):
        return None

if __name__ == "__main__":
    unittest.main()
