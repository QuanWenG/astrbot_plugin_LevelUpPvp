import os
import shutil
import unittest
import uuid

from tests.test_command_handler import _install_dependency_stubs

_install_dependency_stubs()

from models.user import UserIdentity
from services.attribute_service import AttributeService
from services.build_service import CombatBuildService
from services.db import connect_db, init_db
from services.equipment_service import EquipmentService
from services.skill_service import SkillService
from services.spell_service import SpellService
from services.user_service import UserService
from handles.command_handler import LevelUpPvpCommandHandler


class AutoEquipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".test_tmp")
        )
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(root, f"autoequip-{uuid.uuid4().hex}")
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "test.db")
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.equipment = EquipmentService(self.db_path)
        self.skills = SkillService(self.db_path)
        self.attributes = AttributeService(self.db_path)
        self.spells = SpellService(
            self.db_path, self.skills, self.equipment, self.attributes
        )
        self.builds = CombatBuildService(
            self.equipment, self.skills, self.attributes, self.spells
        )
        self.user = await self.users.get_or_create_user(
            UserIdentity("test", "group-1", "user-1", "Hero"),
        )
        self.handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=self.users,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
            equipment_service=self.equipment,
            skill_service=self.skills,
            build_service=self.builds,
            attribute_service=self.attributes,
            spell_service=self.spells,
        )

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def _set_stats(self, **kwargs):
        defaults = dict(hp=5, defense=5, speed=5, atk=5, luck=5, willpower=5)
        defaults.update(kwargs)
        cols = ", ".join(f"{k} = ?" for k in defaults)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                f"UPDATE users SET {cols} WHERE id = ?",
                (*defaults.values(), self.user.id),
            )
            await db.commit()
        self.user = await self.users.get_user_by_pk(self.user.id)

    async def _grant(self, catalog_id):
        await self.equipment.grant_catalog_item([self.user.id], catalog_id)

    # ------------------------------------------------------------------ #
    #  _dominant_attribute                                               #
    # ------------------------------------------------------------------ #
    async def test_dominant_attribute_strength(self):
        await self._set_stats(hp=100)
        attrs = self.attributes.attributes_for_user(self.user)
        self.assertEqual(self.handler._dominant_attribute(attrs), "strength")

    async def test_dominant_attribute_dexterity(self):
        await self._set_stats(speed=100)
        attrs = self.attributes.attributes_for_user(self.user)
        self.assertEqual(self.handler._dominant_attribute(attrs), "dexterity")

    async def test_dominant_attribute_perception(self):
        await self._set_stats(atk=100)
        attrs = self.attributes.attributes_for_user(self.user)
        self.assertEqual(self.handler._dominant_attribute(attrs), "perception")

    # ------------------------------------------------------------------ #
    #  Full flow                                                         #
    # ------------------------------------------------------------------ #
    async def test_auto_equip_fills_slots(self):
        items = await self.equipment.list_items(self.user.id)
        self.assertGreater(len(items), 0)
        skills, _ = await self.skills.get_skills(self.user)
        attrs = self.attributes.attributes_for_user(self.user)
        dominant = self.handler._dominant_attribute(attrs)
        assignments = self.handler._select_optimal_loadout(
            items, self.user, skills, dominant
        )
        self.assertGreater(len(assignments), 0)
        results = await self.equipment.auto_equip(self.user.id, assignments)
        self.assertGreater(len(results), 0)
        slots, _ = await self.equipment.get_loadout(self.user.id)
        self.assertGreater(len(slots), 0)

    async def test_auto_equip_atomic_replacement(self):
        items = await self.equipment.list_items(self.user.id)
        first = items[0]
        await self.equipment.equip(self.user.id, first.id)
        old_slots, _ = await self.equipment.get_loadout(self.user.id)
        self.assertGreater(len(old_slots), 0)
        skills, _ = await self.skills.get_skills(self.user)
        attrs = self.attributes.attributes_for_user(self.user)
        dominant = self.handler._dominant_attribute(attrs)
        assignments = self.handler._select_optimal_loadout(
            items, self.user, skills, dominant
        )
        await self.equipment.auto_equip(self.user.id, assignments)
        new_slots, _ = await self.equipment.get_loadout(self.user.id)
        self.assertGreater(len(new_slots), 0)

    # ------------------------------------------------------------------ #
    #  Weapon type matching                                              #
    # ------------------------------------------------------------------ #
    async def test_weapon_type_matching_prefers_axe_for_strength(self):
        await self._set_stats(hp=200, speed=5)
        await self._grant(3015)  # 手斧 (axe, one_hand, strength)
        await self._grant(3036)  # 短弓 (bow, two_hand_ranged, dexterity)
        items = await self.equipment.list_items(self.user.id)
        weapons = [i for i in items if i.item_type == "weapon"]
        axe = [w for w in weapons if w.weapon_type == "axe"]
        bow = [w for w in weapons if w.weapon_type == "bow"]
        self.assertTrue(axe, "Axe not found in inventory")
        self.assertTrue(bow, "Bow not found in inventory")
        axe_score = self.handler._score_item(axe[0], self.user, "strength")
        bow_score = self.handler._score_item(bow[0], self.user, "strength")
        self.assertGreater(axe_score, bow_score)

    async def test_select_loadout_picks_matching_weapon(self):
        await self._set_stats(hp=200, speed=5)
        await self._grant(3015)  # 手斧 (axe)
        await self._grant(3036)  # 短弓 (bow)
        items = await self.equipment.list_items(self.user.id)
        skills, _ = await self.skills.get_skills(self.user)
        attrs = self.attributes.attributes_for_user(self.user)
        dominant = self.handler._dominant_attribute(attrs)
        self.assertEqual(dominant, "strength")
        assignments = self.handler._select_optimal_loadout(
            items, self.user, skills, dominant
        )
        assigned_ids = {eid for eid, _ in assignments}
        weapons = [i for i in items if i.item_type == "weapon"]
        axe_ids = {w.id for w in weapons if w.weapon_type == "axe"}
        self.assertTrue(assigned_ids & axe_ids, "Axe was not selected")

    # ------------------------------------------------------------------ #
    #  Ring selection                                                    #
    # ------------------------------------------------------------------ #
    async def test_ring_selection_max_two(self):
        await self._grant(3082)  # 装饰戒指
        await self._grant(3083)  # 戒指
        await self._grant(3084)  # 守护戒指
        items = await self.equipment.list_items(self.user.id)
        rings = [
            i for i in items if i.equip_slot in ("left_finger", "right_finger")
        ]
        self.assertGreaterEqual(len(rings), 3)
        skills, _ = await self.skills.get_skills(self.user)
        attrs = self.attributes.attributes_for_user(self.user)
        dominant = self.handler._dominant_attribute(attrs)
        assignments = self.handler._select_optimal_loadout(
            items, self.user, skills, dominant
        )
        ring_slot_count = sum(
            1 for _, slot in assignments
            if slot in ("left_finger", "right_finger")
        )
        self.assertLessEqual(ring_slot_count, 2)
        self.assertGreaterEqual(ring_slot_count, 1)

    # ------------------------------------------------------------------ #
    #  Service-level auto_equip                                          #
    # ------------------------------------------------------------------ #
    async def test_service_auto_equip_empty_raises(self):
        with self.assertRaises(ValueError):
            await self.equipment.auto_equip(self.user.id, [])

    async def test_service_auto_equip_replaces_loadout(self):
        items = await self.equipment.list_items(self.user.id)
        # Equip first item manually
        await self.equipment.equip(self.user.id, items[0].id)
        old_slots, _ = await self.equipment.get_loadout(self.user.id)
        # Auto-equip with a different set
        assignments = [(items[1].id, "")] if len(items) > 1 else [(items[0].id, "")]
        await self.equipment.auto_equip(self.user.id, assignments)
        new_slots, _ = await self.equipment.get_loadout(self.user.id)
        # Old item should be gone if we equipped a different one
        if len(items) > 1:
            self.assertNotIn(items[0].id, new_slots.values())


if __name__ == "__main__":
    unittest.main()
