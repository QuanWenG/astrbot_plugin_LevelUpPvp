import json
import os
import random
import tempfile
import unittest
from dataclasses import replace

from models.combat import BattleState, FighterSnapshot, FighterState
from models.ability import CombatStatus, UserSpell
from models.skill import SkillBuild
from models.attributes import DerivedStats
from models.user import UserIdentity
from services.ability_catalog import (
    ACTIVE_ABILITY_DEFINITIONS,
    SPELL_DEFINITIONS,
    TECHNIQUE_DEFINITIONS,
)
from services.ability_runtime import AbilityRuntime
from services.attribute_service import AttributeService
from services.db import connect_db, init_db
from services.equipment_service import EquipmentService
from services.build_service import CombatBuildService
from services.combat_engine import SideviewCombatEngine
from services.combat_ai import STRATEGY_PROFILES
from services.battle_service import BattleService
from services.llm_service import LLMService
from services.skill_service import SkillService
from services.spell_service import SpellService
from services.spell_rules import SPELL_RULES, calculate_mana_cost
from services.user_service import UserService


def fighter(pk, name):
    snapshot = FighterSnapshot(pk, name, 20, 12, 12, 10, 10, 10, "稳扎稳打")
    return FighterState(snapshot, snapshot.max_hp, 250 if pk == 1 else 750, mana=100, stamina=100)


class AbilityCatalogRuntimeTests(unittest.TestCase):
    def test_catalog_contains_exactly_115_active_abilities(self):
        self.assertEqual(len(TECHNIQUE_DEFINITIONS), 30)
        self.assertEqual(len(SPELL_DEFINITIONS), 84)
        self.assertEqual(len(ACTIVE_ABILITY_DEFINITIONS), 115)
        self.assertIn("power_strike", ACTIVE_ABILITY_DEFINITIONS)

    def test_status_ticks_and_stance_mana_freeze_are_deterministic(self):
        runtime = AbilityRuntime()
        actor, target = fighter(1, "甲"), fighter(2, "乙")
        derived = DerivedStats(
            max_hp=actor.snapshot.max_hp, max_mp=80, max_sp=100,
            attack_power=20, accuracy=20, defense=10, evasion=10,
            critical_rate=0.1, critical_damage=1.5, action_speed=100,
            carry_capacity=50,
        )
        actor.snapshot = replace(actor.snapshot, derived=derived)
        actor.current_hp = actor.snapshot.max_hp
        actor.mana = actor.snapshot.max_mp
        state = BattleState(1, actor, target, [], 77)
        rage = ACTIVE_ABILITY_DEFINITIONS["barbarian_rage"]
        result = runtime.damage_result(actor, target, rage, random.Random(1))
        runtime.apply_secondary(state, actor, target, rage, result, random.Random(1))
        self.assertEqual(actor.frozen_mana_capacity, 20)
        self.assertEqual(actor.mana, 60)
        runtime.remove_status(state, actor, "barbarian_rage")
        self.assertEqual(actor.mana, 80)
        poison = next(
            effect for effect in SPELL_DEFINITIONS["thorn_entangle"].effects
            if effect.status_id == "poison"
        )
        runtime.apply_status(state, target, poison, actor.snapshot.user_pk, random.Random(1))
        before = target.current_hp
        state.tick = 5
        runtime.tick(state, random.Random(3))
        self.assertLess(target.current_hp, before)
    def test_every_active_ability_executes_through_shared_runtime(self):
        runtime = AbilityRuntime()
        for index, definition in enumerate(ACTIVE_ABILITY_DEFINITIONS.values()):
            with self.subTest(ability=definition.name):
                actor, target = fighter(1, "甲"), fighter(2, "乙")
                state = BattleState(1, actor, target, [], index + 1)
                rng = random.Random(index + 1)
                result = runtime.damage_result(actor, target, definition, rng)
                runtime.apply_secondary(state, actor, target, definition, result, rng)
                self.assertGreaterEqual(actor.current_hp, 0)
                self.assertGreaterEqual(target.current_hp, 0)


class DynamicSpellRuleTests(unittest.TestCase):
    def _spell_fighter(self, spell_id="magic_arrow", level=1, mana=100):
        definition = SPELL_DEFINITIONS[spell_id]
        skills = SkillBuild(
            {},
            {definition.unlock_skill_id: 80},
            (spell_id,),
            {spell_id: definition},
            {},
            {spell_id: UserSpell(spell_id, level, 0, 100)},
        )
        snapshot = FighterSnapshot(
            1, "法师", 50, 20, 30, 20, 20, 20, "稳扎稳打",
            skills=skills,
        )
        return FighterState(
            snapshot, snapshot.max_hp, 250, mana=mana, stamina=100
        )

    def test_all_84_spells_have_reading_and_mana_metadata(self):
        self.assertEqual(set(SPELL_RULES), set(SPELL_DEFINITIONS))
        self.assertEqual(len(SPELL_RULES), 84)
        for definition in SPELL_DEFINITIONS.values():
            self.assertGreater(definition.reading_difficulty, 0)
            self.assertIn(
                definition.reading_attribute,
                {"magic", "willpower", "perception"},
            )
            self.assertGreater(definition.base_mana_cost, 0)
            self.assertIn(definition.mana_cost_mode, {"scaled", "fixed"})
        self.assertEqual(SPELL_DEFINITIONS["gravity_barrier"].reading_difficulty, 1395)
        self.assertEqual(SPELL_DEFINITIONS["storm_strike"].base_mana_cost, 97)
        self.assertNotIn("identify", SPELL_DEFINITIONS)

    def test_level_scaled_fixed_and_void_embrace_costs(self):
        definition = SPELL_DEFINITIONS["magic_arrow"]
        for level, expected in ((1, 7), (50, 25), (100, 44)):
            fighter_state = self._spell_fighter(level=level)
            result = calculate_mana_cost(definition, fighter_state, False)
            self.assertEqual(result.final_cost, expected)
        fixed = calculate_mana_cost(
            SPELL_DEFINITIONS["limit_break"],
            self._spell_fighter("limit_break", 100),
            True,
        )
        self.assertEqual(fixed.final_cost, 1)
        normal = calculate_mana_cost(
            SPELL_DEFINITIONS["mana_storm"],
            self._spell_fighter("mana_storm", 50),
            False,
        )
        reduced = calculate_mana_cost(
            SPELL_DEFINITIONS["mana_storm"],
            self._spell_fighter("mana_storm", 50),
            True,
        )
        self.assertLess(reduced.final_cost, normal.final_cost)
        self.assertGreaterEqual(reduced.reduction_ratio, 0.25)
        own_cost = calculate_mana_cost(
            SPELL_DEFINITIONS["void_embrace"],
            self._spell_fighter("void_embrace", 50),
            True,
        )
        self.assertEqual(own_cost.reduction_ratio, 1.0)

    def test_negative_mana_backlash_uses_total_debt(self):
        engine = SideviewCombatEngine()
        actor = self._spell_fighter(mana=0)
        target = fighter(2, "目标")
        state = BattleState(1, actor, target, [], 9)
        engine._begin_attack(state, actor, "use_skill", "magic_arrow")
        first = next(e for e in state.events if e.kind == "mana_backlash")
        self.assertEqual(actor.mana, -7)
        self.assertEqual(first.value, 14)
        self.assertEqual(first.mana_before, 0)
        self.assertEqual(first.mana_after, -7)
        actor.attack_pending = False
        actor.pending_skill_id = None
        actor.pending_resource_details = {}
        actor.windup_ticks = 0
        state.tick = 2
        engine._begin_attack(state, actor, "use_skill", "magic_arrow")
        second = [e for e in state.events if e.kind == "mana_backlash"][-1]
        self.assertEqual(actor.mana, -14)
        self.assertEqual(second.value, 28)
        actor.mana += 5
        self.assertEqual(actor.mana, -9)

class SpellBookPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.skills = SkillService(self.db_path)
        self.spells = SpellService(self.db_path, self.skills)
        self.identity = UserIdentity("test", "group", "mage", "法师")
        self.user = await self.users.get_or_create_user(self.identity)
        await self.skills.get_skills(self.user)
        async with await connect_db(self.db_path) as db:
            for skill_id, level in (("magic_training", 80), ("reading", 80), ("silent_reading", 80)):
                await db.execute(
                    "INSERT OR REPLACE INTO user_skills (user_pk, skill_id, level, exp, potential) VALUES (?, ?, ?, 0, 100)",
                    (self.user.id, skill_id, level),
                )
            await db.commit()

    async def asyncTearDown(self):
        os.remove(self.db_path)

    async def test_valid_reads_always_consume_and_repeat_adds_potential(self):
        first = await self.spells.grant_book(self.user.id, "magic_arrow", 1, random_seed=7)
        learned = await self.spells.read_book(self.user, first.id, random_seed=1)
        self.assertTrue(learned.success)
        self.assertEqual(learned.spell.level, 1)
        self.assertEqual(await self.spells.list_books(self.user.id), [])

        repeat = await self.spells.grant_book(self.user.id, "magic_arrow", 1, random_seed=8)
        result = await self.spells.read_book(self.user, repeat.id, random_seed=1)
        self.assertTrue(result.success)
        self.assertGreater(result.spell.potential, 100)

        failed = await self.spells.grant_book(self.user.id, "teleport", 1, random_seed=9)
        result = await self.spells.read_book(self.user, failed.id, random_seed=2)
        self.assertFalse(result.success)
        self.assertNotIn(failed.id, [item.id for item in await self.spells.list_books(self.user.id)])

    async def test_equipment_stats_skills_and_reading_affixes_raise_ability(self):
        equipment = EquipmentService(self.db_path)
        slots, _ = await equipment.get_loadout(self.user.id)
        affixes = [
            {"type": "reading_power", "value": 100},
            {"type": "reading_success", "value": 0.10},
            {"type": "stat_flat", "stat": "magic", "value": 2},
            {"type": "skill_level", "skill_id": "reading", "value": 3},
        ]
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE user_skills SET level = 1 WHERE user_pk = ? "
                "AND skill_id IN ('magic_training', 'reading')",
                (self.user.id,),
            )
            await db.execute(
                "UPDATE equipment_items SET random_affixes_json = ? WHERE id = ?",
                (json.dumps(affixes), slots["head"]),
            )
            await db.commit()
        equipped_spells = SpellService(
            self.db_path, self.skills, equipment, AttributeService(self.db_path)
        )
        book = await equipped_spells.grant_book(
            self.user.id, "magic_arrow", 1, random_seed=13
        )
        result = await equipped_spells.read_book(
            self.user, book.id, random_seed=1
        )
        base_power = 80 + 1 * 8 + self.user.magic * 5 + 1 * 3
        self.assertEqual(result.reading_power, base_power + 134)
        expected = min(
            0.95,
            0.50 + (result.reading_power - 120) * 0.001 + 0.10,
        )
        self.assertAlmostEqual(result.chance, expected)
    async def test_high_difficulty_book_only_requires_permanent_school_level_one(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE user_skills SET level = 1, exp = 0 "
                "WHERE user_pk = ? AND skill_id IN ('magic_training', 'reading')",
                (self.user.id,),
            )
            await db.commit()
        first = await self.spells.grant_book(
            self.user.id, "mana_storm", 1, random_seed=11
        )
        failed = await self.spells.read_book(
            self.user, first.id, random_seed=1
        )
        self.assertFalse(failed.success)
        self.assertEqual(failed.reading_difficulty, 1400)
        expected_power = 80 + 1 * 8 + self.user.magic * 5 + 1 * 3
        self.assertEqual(failed.reading_power, expected_power)
        self.assertEqual(failed.chance, 0.05)
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT exp FROM user_skills "
                "WHERE user_pk = ? AND skill_id = 'reading'",
                (self.user.id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            self.assertEqual(int(row["exp"]), 0)
            cursor = await db.execute(
                "SELECT reading_difficulty, reading_power, reading_attribute, "
                "success_chance FROM spell_read_logs ORDER BY id DESC LIMIT 1"
            )
            log = await cursor.fetchone()
            await cursor.close()
        self.assertEqual(int(log["reading_difficulty"]), 1400)
        self.assertEqual(float(log["reading_power"]), expected_power)
        self.assertEqual(log["reading_attribute"], "magic")
        self.assertEqual(float(log["success_chance"]), 0.05)

        second = await self.spells.grant_book(
            self.user.id, "mana_storm", 1, random_seed=12
        )
        learned = await self.spells.read_book(
            self.user, second.id, random_seed=1, reading_bonus=2.0
        )
        self.assertTrue(learned.success)
        self.assertEqual(learned.chance, 0.95)
        self.assertEqual(learned.spell.spell_id, "mana_storm")
    async def test_learned_spell_enters_reproducible_v10_simulation(self):
        book = await self.spells.grant_book(self.user.id, "magic_arrow", 1)
        await self.spells.read_book(self.user, book.id, random_seed=1)
        await self.skills.set_active_slot(self.user, 1, "魔法箭")
        equipment = EquipmentService(self.db_path)
        builds = CombatBuildService(equipment, self.skills, spell_service=self.spells)
        opponent = await self.users.get_or_create_user(
            UserIdentity("test", "group", "target", "目标")
        )
        async with await connect_db(self.db_path) as db:
            left = await builds.snapshot_in_db(db, self.user, "全力猛攻")
            right = await builds.snapshot_in_db(db, opponent, "全力猛攻")
            await db.commit()
        engine = SideviewCombatEngine()
        profile = STRATEGY_PROFILES["全力猛攻"]
        first = engine.simulate(left, right, profile, profile, 20260722)
        second = engine.simulate(left, right, profile, profile, 20260722)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.engine_version, "sideview-v10")
        self.assertTrue(any(event.kind == "spell_cast" for event in first.events))
        profiles = tuple(STRATEGY_PROFILES.values())
        for attacker_index, attacker_profile in enumerate(profiles):
            for defender_index, defender_profile in enumerate(profiles):
                result = engine.simulate(
                    left, right, attacker_profile, defender_profile,
                    attacker_index * len(profiles) + defender_index,
                )
                self.assertEqual(result.engine_version, "sideview-v10")
    async def test_battle_settlement_persists_spell_growth_atomically(self):
        book = await self.spells.grant_book(self.user.id, "magic_arrow", 1)
        await self.spells.read_book(self.user, book.id, random_seed=1)
        await self.skills.set_active_slot(self.user, 1, "魔法箭")
        opponent_identity = UserIdentity("test", "group", "growth-target", "成长目标")
        service = BattleService(
            self.db_path, self.users, LLMService(),
            EquipmentService(self.db_path), self.skills,
            spell_service=self.spells,
        )
        result = await service.battle(self.identity, opponent_identity, "全力猛攻")
        self.assertTrue(any(item.spell_id == "magic_arrow" for item in result.spell_growths))
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM spell_growth_logs WHERE battle_id IS NOT NULL"
            )
            row = await cursor.fetchone()
            await cursor.close()
        self.assertGreater(int(row["count"]), 0)
    async def test_shared_slot_accepts_unlocked_technique_and_learned_spell(self):
        book = await self.spells.grant_book(self.user.id, "magic_arrow", 1)
        await self.spells.read_book(self.user, book.id, random_seed=1)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE user_skills SET level = 50 WHERE user_pk = ? AND skill_id = 'tactics'",
                (self.user.id,),
            )
            await db.commit()
        await self.skills.set_active_slot(self.user, 1, "勇士图腾")
        await self.skills.set_active_slot(self.user, 2, "魔法箭")
        _, slots = await self.skills.get_skills(self.user)
        self.assertEqual(slots[:2], ("warrior_totem", "magic_arrow"))


if __name__ == "__main__":
    unittest.main()
