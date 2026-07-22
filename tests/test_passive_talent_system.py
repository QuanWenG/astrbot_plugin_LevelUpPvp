import os
import tempfile
import unittest
from dataclasses import replace

from models.combat import BattleState, FighterState
from models.user import UserIdentity
from services.build_service import CombatBuildService
from services.combat_ai import STRATEGY_PROFILES
from services.combat_engine import SideviewCombatEngine
from services.db import connect_db, init_db
from services.equipment_service import EquipmentService
from services.passive_effects import resolve_passive_bonuses
from services.skill_catalog import SKILL_DEFINITIONS, skill_id_for
from services.skill_service import SkillService
from services.user_service import UserService


class PassiveTalentSystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.equipment = EquipmentService(self.db_path)
        self.skills = SkillService(self.db_path)
        self.builds = CombatBuildService(self.equipment, self.skills)
        self.user = await self.users.get_or_create_user(
            UserIdentity("test", "group", "talent", "天赋测试")
        )
        await self.skills.get_skills(self.user)

    async def asyncTearDown(self):
        os.remove(self.db_path)

    async def _set_skill(self, skill_id: str, level: int) -> None:
        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_skills (user_pk, skill_id, level, exp, potential)
                VALUES (?, ?, ?, 0, 100)
                ON CONFLICT(user_pk, skill_id)
                DO UPDATE SET level = excluded.level
                """,
                (self.user.id, skill_id, level),
            )
            await db.commit()

    async def _snapshot(self):
        self.user = await self.users.get_user_by_pk(self.user.id)
        async with await connect_db(self.db_path) as db:
            result = await self.builds.snapshot_in_db(
                db, self.user, "全力猛攻"
            )
            await db.commit()
        return result

    def test_catalog_contains_45_passives_one_active_and_legacy_aliases(self):
        passives = [item for item in SKILL_DEFINITIONS.values() if item.passive]
        actives = [item for item in SKILL_DEFINITIONS.values() if not item.passive]
        self.assertEqual(len(passives), 45)
        self.assertEqual([item.skill_id for item in actives], ["power_strike"])
        self.assertEqual(skill_id_for("长剑"), "longsword")
        self.assertEqual(skill_id_for("长剑专精"), "longsword")
        self.assertEqual(skill_id_for("盾牌"), "shield")
        self.assertEqual(skill_id_for("长枪"), "spear")

    async def test_weapon_tactics_shield_armor_and_mind_eye_resolve(self):
        snapshot = await self._snapshot()
        levels = {
            "longsword": 100,
            "tactics": 100,
            "shield": 100,
            "light_armor": 100,
            "dodge": 100,
            "mind_eye": 100,
            "weightlifting": 100,
        }
        bonuses = resolve_passive_bonuses(levels, snapshot.equipment)
        self.assertAlmostEqual(bonuses.attack_power, 20.0)
        self.assertAlmostEqual(bonuses.accuracy, 55.0)
        self.assertAlmostEqual(bonuses.physical_damage_bonus, 0.30)
        self.assertAlmostEqual(bonuses.block_rate, 0.10)
        self.assertAlmostEqual(bonuses.knockback_resistance, 0.20)
        self.assertAlmostEqual(bonuses.defense, 23.0)
        self.assertAlmostEqual(bonuses.evasion, 55.0)
        self.assertAlmostEqual(bonuses.physical_reduction, 0.05)
        self.assertAlmostEqual(bonuses.critical_rate, 0.10)
        self.assertAlmostEqual(bonuses.critical_damage, 0.20)
        self.assertAlmostEqual(bonuses.carry_capacity, 50.0)

    async def test_dual_wield_weight_and_solitary_weapon_style_conditions(self):
        items = await self.equipment.list_items(self.user.id)
        left = next(i for i in items if i.template_id == "training_dagger_left")
        right = next(i for i in items if i.template_id == "training_dagger_right")
        await self.equipment.equip(self.user.id, left.id, "main_hand")
        await self.equipment.equip(self.user.id, right.id, "off_hand")
        dual = await self._snapshot()
        light = resolve_passive_bonuses({"dual_wield": 100}, dual.equipment)
        heavy_equipment = replace(dual.equipment, weapon_weight=16.0)
        heavy = resolve_passive_bonuses({"dual_wield": 100}, heavy_equipment)
        self.assertEqual(light.style_multiplier, 1.0)
        self.assertAlmostEqual(heavy.style_multiplier, 0.90)

        await self.equipment.unequip(self.user.id, "off_hand")
        solitary = await self._snapshot()
        two_handed = resolve_passive_bonuses(
            {"two_handed": 100}, solitary.equipment
        )
        self.assertEqual(solitary.weapon_mode, "one_hand")
        self.assertEqual(two_handed.style_multiplier, 1.10)

    async def test_throwing_trains_tactics_not_marksmanship(self):
        items = await self.equipment.list_items(self.user.id)
        throwing = next(
            i for i in items if i.template_id == "training_throwing"
        )
        await self.equipment.equip(self.user.id, throwing.id)
        snapshot = await self._snapshot()
        opponent = replace(snapshot, user_pk=999, name="对手")
        profile = STRATEGY_PROFILES["全力猛攻"]
        result = SideviewCombatEngine().simulate(
            snapshot, opponent, profile, profile, 41
        )
        usage = self.skills.usage_from_simulation(result)[self.user.id]
        self.assertIn("throwing", usage)
        self.assertIn("tactics", usage)
        self.assertNotIn("marksmanship", usage)
    async def test_advanced_skill_requires_permanent_level_50_and_costs_point(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET skill_points = 2 WHERE id = ?",
                (self.user.id,),
            )
            await db.commit()
        with self.assertRaisesRegex(
            ValueError, "长剑专精 1/50.*长枪专精 0/50"
        ):
            await self.skills.learn(self.user, "军官武器")

        await self._set_skill("longsword", 50)
        await self._set_skill("spear", 50)
        learned = await self.skills.learn(self.user, "军官武器")
        self.assertEqual(learned.skill_id, "officer_weapon")
        refreshed = await self.users.get_user_by_pk(self.user.id)
        self.assertEqual(refreshed.skill_points, 1)

    async def test_advanced_skill_trains_with_supported_weapon(self):
        await self._set_skill("officer_weapon", 1)
        snapshot = await self._snapshot()
        opponent = replace(snapshot, user_pk=999, name="对手")
        profile = STRATEGY_PROFILES["全力猛攻"]
        result = SideviewCombatEngine().simulate(
            snapshot, opponent, profile, profile, 23
        )
        usage = self.skills.usage_from_simulation(result)
        self.assertIn("officer_weapon", usage[self.user.id])
        self.assertLessEqual(usage[self.user.id]["officer_weapon"], 20)

    async def test_future_magic_summon_reading_and_pve_hooks_serialize(self):
        for skill_id in (
            "natural_knowledge", "pact", "spiritualism", "reading",
            "magic_training", "barrier", "elemental_guidance",
            "shadow_magic", "ritual", "mana_limit", "blessing",
            "restoration", "necromancy", "mind_control",
            "silent_reading", "concealment",
        ):
            await self._set_skill(skill_id, 100)
        snapshot = await self._snapshot()
        derived = snapshot.derived
        self.assertAlmostEqual(derived.spell_multipliers["arcane"], 1.4)
        self.assertAlmostEqual(derived.spell_multipliers["fire"], 1.4)
        self.assertAlmostEqual(derived.spell_multipliers["nature"], 1.4)
        self.assertAlmostEqual(derived.spell_multipliers["hell"], 1.4)
        self.assertAlmostEqual(derived.spell_multipliers["mind"], 1.4)
        self.assertAlmostEqual(derived.healing_power, 1.7)
        self.assertAlmostEqual(derived.summon_power, 2.3)
        self.assertAlmostEqual(derived.blessing_power, 1.4)
        self.assertAlmostEqual(derived.reading_success, 0.5)
        self.assertAlmostEqual(derived.magic_potential_gain, 1.5)
        self.assertAlmostEqual(derived.mana_overcast_reduction, 0.5)
        self.assertAlmostEqual(derived.pve_stealth, 0.5)
        self.assertEqual(snapshot.to_dict()["derived"]["pve_stealth"], 0.5)

    async def test_healing_and_meditation_only_record_actual_recovery(self):
        await self._set_skill("healing", 100)
        await self._set_skill("meditation", 100)
        snapshot = await self._snapshot()
        opponent = replace(snapshot, user_pk=999, name="对手")
        profile = STRATEGY_PROFILES["全力猛攻"]
        result = SideviewCombatEngine().simulate(
            snapshot, opponent, profile, profile, 99
        )
        replay = SideviewCombatEngine().simulate(
            snapshot, opponent, profile, profile, 99
        )
        self.assertEqual(result.events, replay.events)
        self.assertEqual(result.winner_pk, replay.winner_pk)
        hp_events = [e for e in result.events if e.kind == "recover_hp"]
        self.assertTrue(hp_events)
        self.assertFalse(any(e.kind == "recover_mp" for e in result.events))
        usage = self.skills.usage_from_simulation(result)
        self.assertIn("healing", usage[self.user.id])

        fighter = FighterState(
            snapshot,
            current_hp=snapshot.max_hp,
            position=200,
            stamina=snapshot.max_sp,
            mana=0,
        )
        state = BattleState(1, fighter, fighter, [], 1)
        SideviewCombatEngine()._apply_passive_regen(state, fighter)
        self.assertEqual(fighter.mana, 1)
        self.assertEqual(state.events[-1].kind, "recover_mp")

    async def test_pve_stealth_does_not_change_pvp_resolution(self):
        snapshot = await self._snapshot()
        hidden = replace(
            snapshot,
            derived=replace(snapshot.derived, pve_stealth=0.5),
        )
        opponent = replace(snapshot, user_pk=999, name="对手")
        profile = STRATEGY_PROFILES["全力猛攻"]
        plain = SideviewCombatEngine().simulate(
            snapshot, opponent, profile, profile, 37
        )
        concealed = SideviewCombatEngine().simulate(
            hidden, opponent, profile, profile, 37
        )
        self.assertEqual(plain.winner_pk, concealed.winner_pk)
        self.assertEqual(plain.duration_ticks, concealed.duration_ticks)
        self.assertEqual(plain.events, concealed.events)


if __name__ == "__main__":
    unittest.main()
