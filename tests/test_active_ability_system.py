import json
import os
import random
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime

from models.combat import BattleState, FighterSnapshot, FighterState
from models.ability import ActionEffect, CombatStatus, UserSpell
from models.skill import SkillBuild
from models.attributes import DerivedStats
from models.user import UserIdentity
from services.ability_catalog import (
    ACTIVE_ABILITY_DEFINITIONS,
    SPELL_DEFINITIONS,
    TECHNIQUE_DEFINITIONS,
    spell_exp_required,
)
from services.ability_runtime import AbilityRuntime
from services.attribute_service import AttributeService
from services.db import connect_db, init_db
from services.equipment_service import EquipmentService
from services.build_service import CombatBuildService
from services.combat_engine import SideviewCombatEngine
from services.combat_ai import STRATEGY_PROFILES
from services.combat_ai import _ability_score
from services.battle_service import BattleService
from services.llm_service import LLMService
from services.skill_service import SkillService
from services.spell_service import (
    SpellService,
    select_spellbook_drop,
    spellbook_craft_cost,
)
from services.spell_rules import SPELL_RULES, calculate_mana_cost
from services.user_service import UserService


def fighter(pk, name):
    snapshot = FighterSnapshot(pk, name, 20, 12, 12, 10, 10, 10, "稳扎稳打")
    return FighterState(snapshot, snapshot.max_hp, 250 if pk == 1 else 750, mana=100, stamina=100)


class AbilityCatalogRuntimeTests(unittest.TestCase):
    def test_catalog_contains_player_and_monster_active_abilities(self):
        self.assertEqual(len(TECHNIQUE_DEFINITIONS), 32)
        self.assertEqual(len(SPELL_DEFINITIONS), 84)
        self.assertEqual(len(ACTIVE_ABILITY_DEFINITIONS), 119)
        self.assertIn("power_strike", ACTIVE_ABILITY_DEFINITIONS)
        self.assertIn("monster_split", ACTIVE_ABILITY_DEFINITIONS)
        self.assertIn("monster_corrosive_splash", ACTIVE_ABILITY_DEFINITIONS)

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

    def test_hard_control_cap_and_immunity_use_exclusive_tick_boundary(self):
        runtime = AbilityRuntime()
        actor, target = fighter(1, "甲"), fighter(2, "乙")
        state = BattleState(10, actor, target, [], 77)
        stun = ActionEffect(
            "apply_status",
            duration_ticks=20,
            chance=1.0,
            status_id="stun",
        )

        self.assertTrue(
            runtime.apply_status(
                state,
                target,
                stun,
                actor.snapshot.user_pk,
                random.Random(1),
            )
        )
        self.assertEqual(target.statuses["stun"].remaining_ticks, 4)
        self.assertEqual(target.hard_control_immunity_until, 15)

        for tick in range(11, 15):
            state.tick = tick
            runtime.tick(state, random.Random(tick))
        self.assertNotIn("stun", target.statuses)

        state.tick = 14
        self.assertFalse(
            runtime.apply_status(
                state,
                target,
                stun,
                actor.snapshot.user_pk,
                random.Random(2),
            )
        )
        state.tick = 15
        self.assertTrue(
            runtime.apply_status(
                state,
                target,
                stun,
                actor.snapshot.user_pk,
                random.Random(3),
            )
        )
        self.assertEqual(target.statuses["stun"].remaining_ticks, 3)

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

    def _spell_pair(self, spell_id, *, spell_level=20):
        definition = SPELL_DEFINITIONS[spell_id]
        derived = DerivedStats(
            max_hp=300,
            max_mp=240,
            max_sp=100,
            attack_power=35,
            accuracy=50,
            defense=20,
            evasion=20,
            critical_rate=0.05,
            critical_damage=1.5,
            action_speed=100,
            carry_capacity=50,
            resistances={},
        )
        skills = SkillBuild(
            {},
            {definition.unlock_skill_id: 80},
            (spell_id,),
            {spell_id: definition},
            {},
            {spell_id: UserSpell(spell_id, spell_level, 0, 100)},
        )
        actor, target = fighter(1, "召唤者"), fighter(2, "目标")
        actor.snapshot = replace(
            actor.snapshot,
            level=50,
            skills=skills,
            derived=derived,
        )
        target.snapshot = replace(
            target.snapshot,
            level=50,
            derived=derived,
        )
        actor.current_derived = derived
        target.current_derived = derived
        actor.current_hp = actor.max_hp
        target.current_hp = target.max_hp
        actor.mana = actor.max_mp
        actor.position = 250
        target.position = 500
        return actor, target, definition

    def test_elm_blessing_summons_a_guardian_that_attacks(self):
        runtime = AbilityRuntime()
        actor, target, definition = self._spell_pair("elm_blessing")
        state = BattleState(1, actor, target, [], 7)
        runtime.apply_secondary(
            state,
            actor,
            target,
            definition,
            (0, False, False, 0, definition.ability_id, {}),
            random.Random(1),
        )
        self.assertEqual(len(state.entities), 1)
        before = target.current_hp
        state.tick = 6
        runtime.tick(state, random.Random(2))
        self.assertLess(target.current_hp, before)
        self.assertTrue(
            any(event.kind == "summon_strike" for event in state.events)
        )
        self.assertIn("elm_guardian_aura", actor.statuses)
        sage = SPELL_DEFINITIONS["sage_blessing"]
        runtime.apply_secondary(
            state,
            actor,
            target,
            sage,
            (0, False, False, 0, sage.ability_id, {}),
            random.Random(3),
        )
        state.tick = 7
        runtime.tick(state, random.Random(4))
        self.assertIn("sage_blessing", actor.statuses)
        self.assertNotIn("elm_blessing", actor.statuses)

    def test_life_steal_and_zone_pulses_have_visible_events(self):
        runtime = AbilityRuntime()
        actor, target, definition = self._spell_pair("hell_breath")
        actor.current_hp = 100
        state = BattleState(1, actor, target, [], 8)
        damage_result = runtime.damage_result(
            actor, target, definition, random.Random(3)
        )
        runtime.apply_secondary(
            state, actor, target, definition, damage_result, random.Random(4)
        )
        self.assertTrue(
            any(event.kind == "life_steal" for event in state.events)
        )

        actor, target, definition = self._spell_pair("fire_wall")
        state = BattleState(1, actor, target, [], 9)
        runtime.apply_secondary(
            state,
            actor,
            target,
            definition,
            (0, False, False, 0, definition.ability_id, {}),
            random.Random(5),
        )
        state.tick = 1
        runtime.tick(state, random.Random(6))
        early_status_events = [
            event for event in state.events
            if event.kind in {"status_apply", "status_resist"}
            and event.target_pk == target.snapshot.user_pk
        ]
        state.tick = 5
        runtime.tick(state, random.Random(7))
        pulse_status_events = [
            event for event in state.events
            if event.kind in {"status_apply", "status_resist"}
            and event.target_pk == target.snapshot.user_pk
        ]
        self.assertEqual(early_status_events, [])
        self.assertGreater(len(pulse_status_events), 0)

    def test_ai_can_cleanse_at_full_health_and_values_mobility(self):
        own, opponent, _ = self._spell_pair("minor_heal")
        own.statuses["poison"] = CombatStatus(
            "poison", opponent.snapshot.user_pk, 10, beneficial=False
        )
        heal_score = _ability_score(
            SPELL_DEFINITIONS["minor_heal"],
            own,
            opponent,
            250,
            0,
            random.Random(1),
        )
        self.assertIsNotNone(heal_score)
        opponent.attack_pending = True
        mobility_score = _ability_score(
            SPELL_DEFINITIONS["blink"],
            own,
            opponent,
            100,
            0,
            random.Random(1),
        )
        self.assertIsNotNone(mobility_score)
        self.assertGreater(mobility_score, 50)


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

    def test_negative_mana_backlash_is_hp_ratio_bounded_and_debt_scaled(self):
        engine = SideviewCombatEngine()
        actor = self._spell_fighter(mana=0)
        actor.snapshot = replace(
            actor.snapshot,
            derived=DerivedStats(
                max_hp=actor.max_hp,
                max_mp=100,
                max_sp=100,
                attack_power=20,
                accuracy=20,
                defense=10,
                evasion=10,
                critical_rate=0.1,
                critical_damage=1.5,
                action_speed=100,
                carry_capacity=50,
            ),
        )
        actor.current_derived = actor.snapshot.derived
        target = fighter(2, "目标")
        state = BattleState(1, actor, target, [], 9)
        engine._begin_attack(state, actor, "use_skill", "magic_arrow")
        first = next(e for e in state.events if e.kind == "mana_backlash")
        self.assertEqual(actor.mana, -7)
        self.assertGreater(first.value, 0)
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
        self.assertGreater(second.value, first.value)
        self.assertLessEqual(
            second.value,
            actor.max_hp
            * engine.ruleset.resource.overcast_backlash_hp_ratio_cap
            + 1,
        )
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

    async def test_existing_read_log_table_gains_activity_day_key(self):
        handle, legacy_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """
                CREATE TABLE spell_read_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_pk INTEGER NOT NULL,
                    spell_id TEXT NOT NULL,
                    book_item_id INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    success_chance REAL NOT NULL,
                    random_seed INTEGER NOT NULL,
                    potential_before INTEGER NOT NULL DEFAULT 0,
                    potential_after INTEGER NOT NULL DEFAULT 0,
                    reading_difficulty INTEGER NOT NULL DEFAULT 0,
                    reading_power REAL NOT NULL DEFAULT 0,
                    reading_attribute TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        try:
            await init_db(legacy_path)
            connection = sqlite3.connect(legacy_path)
            try:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(spell_read_logs)"
                    )
                }
            finally:
                connection.close()
            self.assertIn("activity_day_key", columns)
        finally:
            os.remove(legacy_path)

    async def test_success_consumes_failure_retains_and_repeat_adds_potential(self):
        first = await self.spells.grant_book(self.user.id, "magic_arrow", 1, random_seed=7)
        learned = await self.spells.read_book(self.user, first.id, random_seed=1)
        self.assertTrue(learned.success)
        self.assertEqual(learned.spell.level, 1)
        self.assertEqual(await self.spells.list_books(self.user.id), [])

        repeat = await self.spells.grant_book(self.user.id, "magic_arrow", 1, random_seed=8)
        result = await self.spells.read_book(self.user, repeat.id, random_seed=1)
        self.assertTrue(result.success)
        self.assertGreater(result.spell.potential, 100)
        self.assertEqual(result.research_pages_gain, 1)
        self.assertEqual(result.research_pages_balance, 1)
        self.assertEqual(await self.spells.get_research_balance(self.user.id), 1)

        failed = await self.spells.grant_book(self.user.id, "teleport", 1, random_seed=9)
        result = await self.spells.read_book(self.user, failed.id, random_seed=2)
        self.assertFalse(result.success)
        self.assertEqual(result.consumed, 0)
        self.assertTrue(result.book_retained)
        self.assertEqual(result.outcome, "study_progress")
        self.assertIn(failed.id, [item.id for item in await self.spells.list_books(self.user.id)])

    async def test_library_aggregates_books_without_losing_actionable_ids_or_seeds(self):
        first = await self.spells.grant_book(
            self.user.id, "magic_arrow", 2, random_seed=101
        )
        second = await self.spells.grant_book(
            self.user.id, "magic_arrow", 1, random_seed=202
        )
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "INSERT INTO user_spells "
                "(user_pk, spell_id, level, exp, potential) "
                "VALUES (?, 'magic_arrow', 3, 7, 180)",
                (self.user.id,),
            )
            await db.execute(
                """
                INSERT INTO spell_read_logs (
                    user_pk, spell_id, book_item_id, success,
                    success_chance, random_seed, activity_day_key, created_at
                ) VALUES (?, 'magic_arrow', ?, 0, 0.5, 303, '2026-08-12', 'test')
                """,
                (self.user.id, first.id),
            )
            await db.commit()

        library = await self.spells.get_book_library(
            self.user, now=datetime(2026, 8, 12, 12, 0)
        )

        self.assertEqual(library.learned_count, 1)
        self.assertEqual(library.total_spell_count, 84)
        self.assertEqual(len(library.entries), 1)
        entry = library.entries[0]
        self.assertEqual(entry.quantity, 3)
        self.assertEqual(entry.oldest_book_id, first.id)
        self.assertEqual([item.id for item in entry.items], [first.id, second.id])
        self.assertEqual(
            [item.random_seed for item in entry.items], [101, 202]
        )
        self.assertEqual(entry.learned_spell.potential, 180)
        self.assertEqual(entry.study_progress, 0.10)
        self.assertTrue(entry.studied_today)

    async def test_reading_by_spell_name_consumes_the_oldest_copy(self):
        oldest = await self.spells.grant_book(
            self.user.id, "magic_arrow", random_seed=311
        )
        newer = await self.spells.grant_book(
            self.user.id, "magic_arrow", random_seed=312
        )

        result = await self.spells.read_book(
            self.user, "魔法箭", random_seed=1
        )

        self.assertTrue(result.success)
        books = await self.spells.list_books(self.user.id)
        self.assertEqual([book.id for book in books], [newer.id])
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT book_item_id FROM spell_read_logs ORDER BY id DESC LIMIT 1"
            )
            log = await cursor.fetchone()
            await cursor.close()
        self.assertEqual(int(log["book_item_id"]), oldest.id)

    async def test_max_potential_duplicate_becomes_audited_idempotent_pages(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "INSERT INTO user_spells "
                "(user_pk, spell_id, level, exp, potential) "
                "VALUES (?, 'magic_arrow', 8, 0, 400)",
                (self.user.id,),
            )
            await db.commit()
        book = await self.spells.grant_book(
            self.user.id, "magic_arrow", 1, random_seed=777
        )

        result = await self.spells.read_book(self.user, "magic_arrow")

        self.assertTrue(result.success)
        self.assertEqual(result.outcome, "research_converted")
        self.assertEqual(result.research_pages_gain, 3)
        self.assertEqual(result.research_pages_balance, 3)
        self.assertEqual(await self.spells.list_books(self.user.id), [])
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT delta, balance_after, reason, source_book_id, "
                "source_seed, operation_key FROM spell_research_logs"
            )
            log = await cursor.fetchone()
            await cursor.close()
            applied, balance = await self.spells._change_research_pages_in_db(
                db,
                user_pk=self.user.id,
                spell_id="magic_arrow",
                delta=3,
                reason="max_potential_duplicate_book",
                operation_key=str(log["operation_key"]),
                source_book_id=book.id,
                source_seed=777,
            )
            await db.commit()
        self.assertEqual(int(log["delta"]), 3)
        self.assertEqual(int(log["balance_after"]), 3)
        self.assertEqual(log["reason"], "max_potential_duplicate_book")
        self.assertEqual(int(log["source_book_id"]), book.id)
        self.assertEqual(int(log["source_seed"]), 777)
        self.assertFalse(applied)
        self.assertEqual(balance, 3)
        self.assertEqual(await self.spells.get_research_balance(self.user.id), 3)

    async def test_pages_can_target_craft_an_unlearned_book_atomically(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "INSERT INTO user_spells "
                "(user_pk, spell_id, level, exp, potential) "
                "VALUES (?, 'magic_arrow', 8, 0, 400)",
                (self.user.id,),
            )
            await db.execute(
                "INSERT OR REPLACE INTO user_skills "
                "(user_pk, skill_id, level, exp, potential) "
                "VALUES (?, 'barrier', 1, 0, 100)",
                (self.user.id,),
            )
            await db.commit()
        stack = await self.spells.grant_book(
            self.user.id, "magic_arrow", 4, random_seed=888
        )
        for _ in range(4):
            await self.spells.read_book(self.user, stack.id)
        self.assertEqual(await self.spells.get_research_balance(self.user.id), 12)

        crafted = await self.spells.craft_book(
            self.user, "护甲术", random_seed=999
        )

        self.assertEqual(crafted.pages_spent, spellbook_craft_cost(130))
        self.assertEqual(crafted.pages_balance, 0)
        self.assertEqual(crafted.item.spell_id, "armor_spell")
        self.assertEqual(crafted.item.random_seed, 999)
        self.assertEqual(crafted.item.source, "spell_research")
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                "SELECT delta, balance_after, result_book_id, source_seed "
                "FROM spell_research_logs "
                "WHERE reason = 'targeted_spellbook_craft'"
            )
            log = await cursor.fetchone()
            await cursor.close()
        self.assertEqual(int(log["delta"]), -12)
        self.assertEqual(int(log["balance_after"]), 0)
        self.assertEqual(int(log["result_book_id"]), crafted.item.id)
        self.assertEqual(int(log["source_seed"]), 999)
        with self.assertRaisesRegex(ValueError, "背包里已有"):
            await self.spells.craft_book(self.user, "护甲术")

    async def test_spell_level_up_preserves_the_400_point_book_potential_domain(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "INSERT INTO user_spells (user_pk, spell_id, level, exp, potential) "
                "VALUES (?, 'magic_arrow', 1, ?, 250)",
                (self.user.id, spell_exp_required(1) - 1),
            )
            growths = await self.spells.apply_growth_in_db(
                db,
                self.user.id,
                {"magic_arrow": 1},
                None,
            )
            await db.commit()
        spells = await self.spells.get_spells(self.user.id)
        self.assertEqual(spells["magic_arrow"].level, 2)
        self.assertEqual(spells["magic_arrow"].potential, 240)
        self.assertEqual(growths[0].potential_after, 240)

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
            self.user,
            first.id,
            random_seed=1,
            now=datetime(2026, 8, 12, 6, 0),
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
            self.assertGreater(int(row["exp"]), 0)
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

        with self.assertRaisesRegex(ValueError, "下个04:00日界线"):
            await self.spells.read_book(
                self.user,
                first.id,
                random_seed=1,
                reading_bonus=2.0,
                now=datetime(2026, 8, 13, 3, 59),
            )
        learned = await self.spells.read_book(
            self.user,
            first.id,
            random_seed=1,
            reading_bonus=2.0,
            now=datetime(2026, 8, 13, 4, 0),
        )
        self.assertTrue(learned.success)
        self.assertEqual(learned.chance, 0.95)
        self.assertEqual(learned.spell.spell_id, "mana_storm")
        self.assertFalse(learned.book_retained)

    async def test_failed_study_accumulates_progress_until_same_book_is_learned(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE user_skills SET level = 1, exp = 0 "
                "WHERE user_pk = ? AND skill_id IN ('magic_training', 'reading')",
                (self.user.id,),
            )
            await db.commit()
        book = await self.spells.grant_book(
            self.user.id, "mana_storm", random_seed=99
        )
        first = await self.spells.read_book(
            self.user,
            book.id,
            random_seed=2,
            now=datetime(2026, 8, 12, 6, 0),
        )
        duplicate = await self.spells.grant_book(
            self.user.id, "mana_storm", random_seed=100
        )
        with self.assertRaisesRegex(ValueError, "研读进度为10%"):
            await self.spells.read_book(
                self.user,
                book.id,
                random_seed=2,
                now=datetime(2026, 8, 12, 20, 0),
            )
        with self.assertRaisesRegex(ValueError, "下个04:00日界线"):
            await self.spells.read_book(
                self.user,
                duplicate.id,
                random_seed=2,
                now=datetime(2026, 8, 12, 20, 0),
            )
        second = await self.spells.read_book(
            self.user,
            book.id,
            random_seed=2,
            now=datetime(2026, 8, 13, 6, 0),
        )
        self.assertFalse(first.success)
        self.assertFalse(second.success)
        self.assertGreater(second.chance, first.chance)
        self.assertGreater(second.study_progress, first.study_progress)
        learned = await self.spells.read_book(
            self.user,
            book.id,
            random_seed=1,
            now=datetime(2026, 8, 14, 6, 0),
        )
        self.assertTrue(learned.success)
        self.assertNotIn(
            book.id,
            [item.id for item in await self.spells.list_books(self.user.id)],
        )

    async def test_failed_study_progress_is_shared_across_same_spell_books(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE user_skills SET level = 1, exp = 0 "
                "WHERE user_pk = ? AND skill_id IN ('magic_training', 'reading')",
                (self.user.id,),
            )
            await db.commit()
        first_book = await self.spells.grant_book(
            self.user.id, "mana_storm", random_seed=201
        )
        second_book = await self.spells.grant_book(
            self.user.id, "mana_storm", random_seed=202
        )

        first = await self.spells.read_book(
            self.user,
            first_book.id,
            random_seed=2,
            now=datetime(2026, 8, 12, 6, 0),
        )
        with self.assertRaisesRegex(ValueError, "研读进度为10%"):
            await self.spells.read_book(
                self.user,
                second_book.id,
                random_seed=2,
                now=datetime(2026, 8, 12, 20, 0),
            )
        second = await self.spells.read_book(
            self.user,
            second_book.id,
            random_seed=2,
            now=datetime(2026, 8, 13, 6, 0),
        )

        self.assertFalse(first.success)
        self.assertFalse(second.success)
        self.assertGreater(second.chance, first.chance)
        self.assertEqual(second.study_progress, 0.20)

    async def test_legacy_failed_log_without_day_key_still_adds_spell_progress(self):
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE user_skills SET level = 1, exp = 0 "
                "WHERE user_pk = ? AND skill_id IN ('magic_training', 'reading')",
                (self.user.id,),
            )
            await db.execute(
                """
                INSERT INTO spell_read_logs (
                    user_pk, spell_id, book_item_id, success,
                    success_chance, random_seed, activity_day_key, created_at
                ) VALUES (?, 'mana_storm', 999999, 0, 0.05, 7, '', 'legacy')
                """,
                (self.user.id,),
            )
            await db.commit()
        book = await self.spells.grant_book(
            self.user.id, "mana_storm", random_seed=203
        )

        result = await self.spells.read_book(
            self.user,
            book.id,
            random_seed=2,
            now=datetime(2026, 8, 12, 6, 0),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.study_progress, 0.20)

    async def test_reward_grant_is_caller_owned_and_idempotent(self):
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            first = await self.spells.grant_book_reward_in_db(
                db,
                user_pk=self.user.id,
                spell_id="magic_arrow",
                reward_key="chat:message:42",
                source="ambient_chat",
            )
            duplicate = await self.spells.grant_book_reward_in_db(
                db,
                user_pk=self.user.id,
                spell_id="teleport",
                reward_key="chat:message:42",
                source="ambient_chat",
            )
            self.assertTrue(first.applied)
            self.assertFalse(duplicate.applied)
            await db.rollback()
        self.assertEqual(await self.spells.list_books(self.user.id), [])

        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            retried = await self.spells.grant_random_book_reward_in_db(
                db,
                user_pk=self.user.id,
                reward_key="chat:message:42",
                source="ambient_chat",
                random_seed=77,
                player_level=10,
            )
            await db.commit()
        self.assertTrue(retried.applied)
        async with await connect_db(self.db_path) as db:
            await db.execute("BEGIN")
            repeated = await self.spells.grant_random_book_reward_in_db(
                db,
                user_pk=self.user.id,
                reward_key="chat:message:42",
                source="ambient_chat",
                random_seed=77,
                player_level=10,
            )
            await db.commit()
        self.assertFalse(repeated.applied)
        self.assertEqual(len(await self.spells.list_books(self.user.id)), 1)

    def test_chat_drop_roll_is_reproducible_and_level_shaped(self):
        first = select_spellbook_drop(random_seed=7, player_level=10)
        second = select_spellbook_drop(random_seed=7, player_level=10)
        self.assertEqual(first, second)
        self.assertEqual(SPELL_DEFINITIONS[first.spell_id].unlock_level, 1)
        self.assertIn(first.rarity, {"common", "uncommon", "rare", "legendary"})
    async def test_learned_spell_enters_reproducible_v11_simulation(self):
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
        self.assertEqual(first.engine_version, SideviewCombatEngine.ENGINE_VERSION)
        self.assertTrue(any(event.kind == "spell_cast" for event in first.events))
        profiles = tuple(STRATEGY_PROFILES.values())
        for attacker_index, attacker_profile in enumerate(profiles):
            for defender_index, defender_profile in enumerate(profiles):
                result = engine.simulate(
                    left, right, attacker_profile, defender_profile,
                    attacker_index * len(profiles) + defender_index,
                )
                self.assertEqual(
                    result.engine_version,
                    SideviewCombatEngine.ENGINE_VERSION,
                )
    async def test_battle_settlement_persists_spell_growth_atomically(self):
        book = await self.spells.grant_book(self.user.id, "magic_arrow", 1)
        await self.spells.read_book(self.user, book.id, random_seed=1)
        await self.skills.set_active_slot(self.user, 1, "魔法箭")
        opponent_identity = UserIdentity("test", "group", "growth-target", "成长目标")
        opponent = await self.users.get_or_create_user(opponent_identity)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET level = 5 WHERE id IN (?, ?)",
                (self.user.id, opponent.id),
            )
            await db.commit()
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
