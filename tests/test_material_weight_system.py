import os
import random
import tempfile
import unittest
from dataclasses import replace

from models.attributes import AdvancedAttributes, PrimaryAttributes
from models.combat import BattleState, FighterState
from models.equipment import EQUIPMENT_SLOTS, EquipmentTemplate
from models.user import UserIdentity
from services.ability_catalog import status
from services.attribute_service import AttributeService
from services.build_service import CombatBuildService
from services.combat_ai import STRATEGY_PROFILES
from services.combat_engine import SideviewCombatEngine
from services.db import connect_db, init_db
from services.equipment_catalog import EquipmentFactory, STARTER_BY_ID
from services.equipment_service import EquipmentService
from services.material_catalog import (
    MATERIAL_DEFINITIONS,
    actual_weight,
    armor_style_for_weight,
    validate_base_weight,
    weight_accuracy_multipliers,
)
from services.skill_service import SkillService
from services.stat_service import StatService
from services.user_service import UserService


class MaterialCatalogTests(unittest.TestCase):
    def test_all_materials_and_reference_weights_are_exact(self):
        self.assertEqual(len(MATERIAL_DEFINITIONS), 35)
        expected = {
            "paper": 0.10, "cloth": 0.20, "silk": 0.40,
            "mica": 0.40, "spirit_cloth": 0.40, "nightweave": 0.45,
            "zylon": 0.50, "griffin_scale": 0.70, "ether": 0.80,
            "organic": 1.00, "leather": 1.00, "bone": 1.20,
            "obsidian": 1.60, "glass": 1.80, "scale": 1.80,
            "coral": 1.80, "bronze": 2.00, "crystal": 2.00,
            "titanium": 2.00, "chain": 2.00, "dragon_scale": 2.20,
            "silver": 2.30, "mithril": 2.40, "pearl": 2.40,
            "emerald": 2.40, "ruby": 2.50, "wood": 2.50,
            "platinum": 2.60, "steel": 2.70, "iron": 2.80,
            "gold": 3.00, "lead": 3.00, "chrome": 3.20,
            "diamond": 3.30, "adamantine": 3.60,
        }
        self.assertEqual(
            {key: value.weight_multiplier for key, value in MATERIAL_DEFINITIONS.items()},
            expected,
        )
        self.assertAlmostEqual(actual_weight(7.5, "adamantine"), 27.0)
        self.assertAlmostEqual(actual_weight(1.3, "nightweave"), 0.585)
        self.assertAlmostEqual(actual_weight(1.8, "spirit_cloth"), 0.72)

    def test_unsupported_material_features_are_not_serialized_as_effects(self):
        self.assertEqual(MATERIAL_DEFINITIONS["obsidian"].effects, ())
        self.assertEqual(MATERIAL_DEFINITIONS["organic"].effects, ())
        self.assertTrue(all(
            effect.target != "ether_disease"
            for effect in MATERIAL_DEFINITIONS["ether"].effects
        ))

    def test_shared_armor_boundaries_and_heavy_accuracy_penalties(self):
        self.assertEqual(armor_style_for_weight(14.999), "light")
        self.assertEqual(armor_style_for_weight(15), "medium")
        self.assertEqual(armor_style_for_weight(35), "medium")
        self.assertEqual(armor_style_for_weight(35.001), "heavy")
        self.assertEqual(weight_accuracy_multipliers("light", False), (1.0, 1.0))
        self.assertEqual(weight_accuracy_multipliers("medium", False), (0.95, 0.90))
        self.assertEqual(weight_accuracy_multipliers("heavy", False), (0.85, 0.75))
        physical, spell = weight_accuracy_multipliers("heavy", True)
        self.assertAlmostEqual(physical, 0.85 * 0.85)
        self.assertAlmostEqual(spell, 0.75 * 0.85)

    def test_weight_ranges_and_explicit_black_star_exception(self):
        valid = EquipmentTemplate(
            "plate", "板甲", "armor", "body", material="adamantine", weight=7.5
        )
        validate_base_weight(valid)
        invalid = replace(valid, weight=7.501)
        with self.assertRaises(ValueError):
            validate_base_weight(invalid)
        exempt = replace(invalid, weight_range_exception=True)
        EquipmentFactory().generate(1, exempt, 10, "epic", 7)


class MaterialBuildPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.equipment = EquipmentService(self.db_path)
        self.skills = SkillService(self.db_path)
        self.attributes = AttributeService(self.db_path)
        self.builds = CombatBuildService(
            self.equipment, self.skills, self.attributes
        )
        self.user = await self.users.get_or_create_user(
            UserIdentity("test", "material", "one", "材质测试")
        )

    async def asyncTearDown(self):
        os.remove(self.db_path)

    async def test_speed_and_luck_are_not_regular_point_aliases(self):
        stats = StatService(self.db_path, self.users)
        identity = UserIdentity("test", "material", "one", "材质测试")
        for name in ("速度", "speed", "幸运", "luck"):
            with self.assertRaisesRegex(ValueError, "高级属性"):
                await stats.allocate(identity, name, 1)
    async def test_starter_v2_has_eleven_slots_and_exact_default_weight(self):
        first = await self.equipment.list_items(self.user.id)
        second = await self.equipment.list_items(self.user.id)
        slots, equipped = await self.equipment.get_loadout(self.user.id)
        skills, _ = await self.skills.get_skills(self.user)
        build = self.builds.resolve_equipment(self.user, slots, equipped, skills)
        self.assertEqual(len(EQUIPMENT_SLOTS), 11)
        self.assertEqual(set(slots), set(EQUIPMENT_SLOTS))
        self.assertEqual(len(first), len(second))
        self.assertEqual(len(first), len(STARTER_BY_ID))
        self.assertAlmostEqual(build.total_weight, 11.90, places=2)
        self.assertEqual(build.armor_style, "light")
        self.assertIn("training_cape", {item.template_id for item in first})
        self.assertIn("training_gloves", {item.template_id for item in first})

    async def test_v2_migration_repairs_training_weights_without_overwriting_slots(self):
        items = await self.equipment.list_items(self.user.id)
        by_template = {item.template_id: item for item in items}
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "DELETE FROM feature_grants WHERE user_pk = ? AND grant_key = ?",
                (self.user.id, "starter-armory-v2-materials"),
            )
            await db.execute(
                "DELETE FROM equipment_items WHERE owner_pk = ? "
                "AND template_id IN ('training_cape', 'training_gloves')",
                (self.user.id,),
            )
            await db.execute(
                "INSERT OR REPLACE INTO equipment_loadout "
                "(user_pk, slot, equipment_id) VALUES (?, 'back', ?)",
                (self.user.id, by_template["training_clothes"].id),
            )
            await db.execute(
                "UPDATE equipment_items SET weight = 99 "
                "WHERE owner_pk = ? AND template_id = 'training_longsword'",
                (self.user.id,),
            )
            await db.commit()
        migrated = await self.equipment.list_items(self.user.id)
        slots, _ = await self.equipment.get_loadout(self.user.id)
        migrated_by_template = {item.template_id: item for item in migrated}
        self.assertEqual(migrated_by_template["training_longsword"].weight, 2.0)
        self.assertEqual(slots["back"], by_template["training_clothes"].id)
        self.assertEqual(slots["wrist"], migrated_by_template["training_gloves"].id)
        self.assertEqual(
            len([item for item in migrated if item.template_id == "training_cape"]),
            1,
        )
    async def test_legacy_rows_gain_advanced_defaults_and_atomic_log(self):
        self.assertEqual(
            (
                self.user.life_growth, self.user.mana_growth,
                self.user.advanced_speed, self.user.advanced_luck,
            ),
            (100, 100, 100, 100),
        )
        changed = await self.attributes.increase_advanced_attribute(
            self.user.id, "life_growth", 3, "test_item"
        )
        self.assertEqual(changed.life_growth, 103)
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT amount, value_before, value_after, source "
                "FROM advanced_attribute_logs WHERE user_pk = ?",
                (self.user.id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        self.assertEqual(tuple(row), (3, 100, 103, "test_item"))

    async def test_material_effects_stack_without_quality_or_level_scaling(self):
        items = await self.equipment.list_items(self.user.id)
        cap = next(item for item in items if item.template_id == "training_cap")
        cape = next(item for item in items if item.template_id == "training_cape")
        material_items = [
            replace(cap, material="ruby", item_level=100, quality="mythic"),
            replace(cape, material="ruby", item_level=100, quality="mythic"),
            replace(cap, id=-10, material="mithril", item_level=100, quality="mythic"),
            replace(cape, id=-11, material="dragon_scale", item_level=100, quality="mythic"),
        ]
        skills, _ = await self.skills.get_skills(self.user)
        slots = {"head": cap.id, "back": cape.id}
        build = self.builds.resolve_equipment(self.user, slots, material_items, skills)
        self.assertEqual(build.advanced_stat_modifiers["life_growth"], 6)
        self.assertEqual(build.skill_modifiers["magic_training"], 2)
        self.assertAlmostEqual(build.combat_effects["resistance_fire"], 25)
        self.assertAlmostEqual(build.combat_effects["resistance_cold"], 25)

    async def test_growth_changes_resources_and_luck_is_bounded_fortune(self):
        skills, _ = await self.skills.get_skills(self.user)
        slots, equipped = await self.equipment.get_loadout(self.user.id)
        build = self.builds.resolve_equipment(self.user, slots, equipped, skills)
        primary = PrimaryAttributes(10, 10, 10, 10, 10, 10)
        base = self.attributes.derive(
            level=10, attributes=primary, equipment=build,
            advanced=AdvancedAttributes(100, 100, 100, 100),
            effective_skills={},
        )
        grown = self.attributes.derive(
            level=10, attributes=primary, equipment=replace(build, action_speed=120),
            advanced=AdvancedAttributes(125, 140, 120, 999),
            effective_skills={},
        )
        self.assertEqual(grown.max_hp, round(base.max_hp * 1.25))
        self.assertEqual(grown.max_mp, round(base.max_mp * 1.40))
        self.assertEqual(grown.action_speed, 120)

        async with await connect_db(self.db_path) as db:
            left = await self.builds.snapshot_in_db(db, self.user, "全力猛攻")
            opponent, _ = await self.users.get_or_create_user_in_db(
                db, UserIdentity("test", "material", "two", "对手")
            )
            right = await self.builds.snapshot_in_db(db, opponent, "全力猛攻")
            await db.commit()
        lucky_left = replace(
            left,
            advanced_attributes=replace(left.advanced_attributes, luck=999),
        )
        engine = SideviewCombatEngine()
        normal_state = engine._fighter_from_initial(
            left,
            engine.ATTACKER_START,
            None,
        )
        lucky_state = engine._fighter_from_initial(
            lucky_left,
            engine.ATTACKER_START,
            None,
        )
        target_state = engine._fighter_from_initial(
            right,
            engine.DEFENDER_START,
            None,
        )
        self.assertGreater(
            lucky_state.fortune_charges,
            normal_state.fortune_charges,
        )
        self.assertLessEqual(
            lucky_state.fortune_charges,
            engine.ruleset.fortune.charge_cap,
        )
        normal_critical = engine._critical_chance(
            normal_state,
            target_state,
        )
        lucky_critical = engine._critical_chance(
            lucky_state,
            target_state,
        )
        self.assertGreater(lucky_critical, normal_critical)
        self.assertAlmostEqual(
            lucky_critical - normal_critical,
            engine.ruleset.fortune.critical_bonus_cap,
        )
        self.assertLessEqual(
            lucky_critical,
            engine.ruleset.damage.critical_chance_cap,
        )

    async def test_feather_float_recomputes_runtime_weight_and_penalties(self):
        async with await connect_db(self.db_path) as db:
            snapshot = await self.builds.snapshot_in_db(db, self.user, "稳扎稳打")
            await db.commit()
        heavy_equipment = replace(
            snapshot.equipment,
            total_weight=40.0,
            carry_capacity=35.0,
            armor_style="heavy",
            overloaded=True,
            movement_multiplier=0.75 * 0.875,
            physical_accuracy_multiplier=0.85 * 0.85,
            spell_accuracy_multiplier=0.75 * 0.85,
        )
        heavy = replace(snapshot, equipment=heavy_equipment)
        actor = FighterState(heavy, heavy.max_hp, 200, stamina=heavy.max_sp, mana=heavy.max_mp)
        target = FighterState(snapshot, snapshot.max_hp, 800, stamina=snapshot.max_sp, mana=snapshot.max_mp)
        state = BattleState(1, actor, target, [], 99)
        effect = status(
            "floating", 40, magnitude=0.25, target="self", beneficial=True,
            params={"weight_reduction": 0.25},
        )
        engine = SideviewCombatEngine()
        self.assertTrue(engine.ability_runtime.apply_status(state, actor, effect, actor.snapshot.user_pk, random.Random(1)))
        self.assertEqual(actor.runtime_weight, 30.0)
        self.assertEqual(actor.runtime_armor_style, "medium")
        self.assertFalse(actor.runtime_overloaded)
        self.assertEqual(engine._accuracy_multiplier(actor, False), 0.95)
        self.assertEqual(engine._accuracy_multiplier(actor, True), 0.90)
        engine.ability_runtime.remove_status(state, actor, "floating")
        self.assertEqual(engine._accuracy_multiplier(actor, False), 0.85 * 0.85)
        self.assertEqual(engine._accuracy_multiplier(actor, True), 0.75 * 0.85)
    async def test_snapshot_serializes_v10_advanced_and_accuracy_data(self):
        async with await connect_db(self.db_path) as db:
            snapshot = await self.builds.snapshot_in_db(
                db, self.user, "稳扎稳打"
            )
            await db.commit()
        payload = snapshot.to_dict()
        self.assertEqual(payload["advanced_attributes"]["life_growth"], 100)
        self.assertIn("physical_accuracy_multiplier", payload["equipment"])
        result = SideviewCombatEngine().simulate(
            snapshot, snapshot,
            STRATEGY_PROFILES["稳扎稳打"], STRATEGY_PROFILES["稳扎稳打"], 8,
        )
        self.assertEqual(result.engine_version, SideviewCombatEngine.ENGINE_VERSION)


if __name__ == "__main__":
    unittest.main()
