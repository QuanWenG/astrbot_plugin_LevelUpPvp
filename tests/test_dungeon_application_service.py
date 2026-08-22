import asyncio
import os
import shutil
import types
import unittest
import uuid
from dataclasses import replace
from unittest import mock
from unittest.mock import patch

from models.combat import BattleEvent, FighterContinuationState, SimulationResult
from models.dungeon import DungeonRewardIntent
from models.user import UserIdentity
from services.attribute_service import AttributeService
from services.build_service import CombatBuildService
from services.db import connect_db, init_db
from services.dungeon_application_service import (
    DungeonAdventureApplicationService,
    DungeonRewardSettlementService,
)
from services.dungeon_catalog import DungeonCatalog
from services.equipment_service import EquipmentService, reward_quality_policy
from services.monster_build_service import MonsterBuildService
from services.monster_catalog import MonsterCatalog
from services.skill_service import SkillService
from services.spell_service import SpellService
from services.ability_catalog import SPELL_DEFINITIONS
from services.user_service import UserService


class RecordingWinningEngine:
    def __init__(self):
        self.environment_ids = []

    def simulate(
        self,
        attacker,
        defender,
        _attacker_profile,
        _defender_profile,
        random_seed,
        attacker_initial_state=None,
        _defender_initial_state=None,
        *,
        environment_id="calm",
    ):
        self.environment_ids.append(environment_id)
        state = attacker_initial_state or FighterContinuationState()
        final_state = replace(
            state,
            hp_ratio=max(0.25, state.hp_ratio - 0.08),
            mana_ratio=max(0.0, state.mana_ratio - 0.05),
            stamina_ratio=max(0.1, state.stamina_ratio - 0.06),
        )
        events = (
            BattleEvent(
                1,
                "damage",
                actor_pk=attacker.user_pk,
                target_pk=defender.user_pk,
                value=20,
                remaining_hp=0,
                skill_id="power_strike",
            ),
        )
        return SimulationResult(
            attacker=attacker,
            defender=defender,
            winner_pk=attacker.user_pk,
            loser_pk=defender.user_pk,
            duration_ticks=3,
            finish_reason="hp_depleted",
            attacker_remaining_hp=max(1, attacker.max_hp - 5),
            defender_remaining_hp=0,
            attacker_damage_dealt=20,
            defender_damage_dealt=5,
            events=events,
            random_seed=random_seed,
            attacker_remaining_stamina=80,
            attacker_remaining_mana=max(0, attacker.max_mp - 2),
            attacker_final_state=final_state,
            defender_final_state=FighterContinuationState(defeated=True),
            ruleset_id="sideview-v11",
            environment_id=environment_id,
        )


class DungeonApplicationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".test_tmp")
        )
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(root, f"dungeon-app-{uuid.uuid4().hex}")
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "levelup.db")
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.equipment = EquipmentService(self.db_path)
        self.skills = SkillService(self.db_path)
        self.attributes = AttributeService(self.db_path)
        self.spells = SpellService(
            self.db_path,
            self.skills,
            self.equipment,
            self.attributes,
        )
        self.builds = CombatBuildService(
            self.equipment,
            self.skills,
            self.attributes,
            self.spells,
        )
        self.monsters = MonsterCatalog()
        self.monster_builds = MonsterBuildService(self.monsters, self.attributes)
        self.dungeons = DungeonCatalog(monster_catalog=self.monsters)
        self.engine = RecordingWinningEngine()
        self.identity = UserIdentity("test", "group-a", "user-a", "Hero")
        self.now_ts = 1_786_406_400
        self.service = self._new_service()

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _new_service(self):
        return DungeonAdventureApplicationService(
            self.db_path,
            self.users,
            self.builds,
            self.monster_builds,
            self.equipment,
            self.skills,
            self.attributes,
            self.spells,
            combat_engine=self.engine,
            dungeon_catalog=self.dungeons,
        )

    async def _start(self, dungeon_id="verdant_wetland"):
        return await self.service.start_or_resume(
            self.identity,
            dungeon_id,
            2,
            "balanced",
            now_ts=self.now_ts,
        )

    async def _prepare_fight(self, result, route_index=None, risk_index=0):
        route = (
            result.view.routes[route_index]
            if route_index is not None
            else next(item for item in result.view.routes if item.requires_combat)
        )
        result = await self.service.choose_route(
            self.identity,
            result.view.adventure_id,
            route.option_id,
            now_ts=self.now_ts,
        )
        selected = next(
            item for item in result.view.routes if item.option_id == route.option_id
        )
        risk_id = selected.risk_choices[risk_index].risk_id
        return await self.service.choose_risk(
            self.identity,
            result.view.adventure_id,
            risk_id,
            now_ts=self.now_ts,
        )

    async def _count(self, table, where="", parameters=()):
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) AS count FROM {table} {where}", parameters
            )
            count = int((await cursor.fetchone())["count"])
            await cursor.close()
            return count

    async def test_context_includes_equipment_exploration_effects(self):
        user = await self.users.get_or_create_user(self.identity)
        equipment = types.SimpleNamespace(
            exploration_capabilities=("detect_invisible",),
            rare_equipment_find_bonus=0.15,
        )
        snapshot = types.SimpleNamespace(
            skill_ids=("basic_attack",),
            equipment=equipment,
            derived=types.SimpleNamespace(pve_stealth=0.37),
        )
        self.builds.snapshot_in_db = mock.AsyncMock(return_value=snapshot)
        self.skills.skills_in_db = mock.AsyncMock(return_value={})
        self.spells.spells_in_db = mock.AsyncMock(return_value={})

        async with await connect_db(self.db_path) as db:
            context = await self.service._context_for_user_in_db(
                db, user, "balanced"
            )

        self.assertIn("detect_invisible", context["capabilities"])
        self.assertEqual(context["rare_equipment_find_bonus"], 0.15)
        self.assertEqual(context["pve_stealth"], 0.37)

    async def test_restart_restores_exact_snapshot_and_cycle_is_not_rerolled(self):
        started = await self._start()
        prepared = await self._prepare_fight(started)
        fought = await self.service.fight(
            self.identity, prepared.view.adventure_id, now_ts=self.now_ts
        )
        restarted = self._new_service()
        restored = await restarted.view(
            self.identity, fought.view.adventure_id, now_ts=self.now_ts
        )
        resumed = await restarted.start_or_resume(
            self.identity,
            "verdant_wetland",
            5,
            "aggressive",
            now_ts=self.now_ts,
        )
        self.assertEqual(restored.view, fought.view)
        self.assertEqual(resumed.view.adventure_id, fought.view.adventure_id)
        self.assertEqual(resumed.view.difficulty, 2)
        self.assertEqual(resumed.view.strategy, "balanced")
        self.assertEqual(await self._count("dungeon_adventures"), 1)

    async def test_concurrent_route_choice_advances_only_once(self):
        started = await self._start()
        route = started.view.routes[0]
        results = await asyncio.gather(
            self.service.choose_route(
                self.identity,
                started.view.adventure_id,
                route.option_id,
                now_ts=self.now_ts,
            ),
            self.service.choose_route(
                self.identity,
                started.view.adventure_id,
                route.option_id,
                now_ts=self.now_ts,
            ),
            return_exceptions=True,
        )
        successes = [item for item in results if not isinstance(item, Exception)]
        failures = [item for item in results if isinstance(item, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        current = await self.service.view(
            self.identity, started.view.adventure_id, now_ts=self.now_ts
        )
        self.assertEqual(current.view.phase, "risk_choice")
        self.assertEqual(current.view.version, 1)

    async def test_concurrent_fight_simulates_and_rewards_only_once(self):
        started = await self._start()
        prepared = await self._prepare_fight(started)
        simulations_before = len(self.engine.environment_ids)
        results = await asyncio.gather(
            self.service.fight(
                self.identity, prepared.view.adventure_id, now_ts=self.now_ts
            ),
            self.service.fight(
                self.identity, prepared.view.adventure_id, now_ts=self.now_ts
            ),
            return_exceptions=True,
        )
        successes = [item for item in results if not isinstance(item, Exception)]
        failures = [item for item in results if isinstance(item, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(self.engine.environment_ids), simulations_before + 1)
        self.assertEqual(
            await self._count(
                "reward_ledger", "WHERE source = ?", ("dungeon_nefia",)
            ),
            len(successes[0].rewards),
        )
        current = await self.service.view(
            self.identity, prepared.view.adventure_id, now_ts=self.now_ts
        )
        self.assertEqual(current.view.completed_floors, 1)

    async def test_forged_intent_is_rejected_even_after_legitimate_settlement(self):
        started = await self._start()
        retreated = await self.service.retreat(
            self.identity, started.view.adventure_id, now_ts=self.now_ts
        )
        async with await connect_db(self.db_path) as db:
            adventure = await self.service.store.get_owned_in_db(
                db,
                owner_pk=(await self.users.get_or_create_user_in_db(db, self.identity))[0].id,
                adventure_id=started.view.adventure_id,
            )
        forged = replace(adventure.reward_intents[0], quantity=99_999)
        ledger_before = await self._count("reward_ledger")
        with self.assertRaisesRegex(ValueError, "不属于"):
            await self.service.settle(
                self.identity,
                started.view.adventure_id,
                (forged,),
                now_ts=self.now_ts,
            )
        self.assertEqual(await self._count("reward_ledger"), ledger_before)
        self.assertTrue(all(item.applied for item in retreated.rewards))

    async def test_failure_rolls_back_state_growth_ledger_exp_and_scrap(self):
        started = await self._start()
        user = await self.users.get_or_create_user(self.identity)
        before_exp = user.exp
        with patch(
            "services.dungeon_application_service.allocate_daily_growth_in_db",
            side_effect=RuntimeError("injected settlement failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                await self.service.retreat(
                    self.identity, started.view.adventure_id, now_ts=self.now_ts
                )
        current = await self.service.view(
            self.identity, started.view.adventure_id, now_ts=self.now_ts
        )
        user = await self.users.get_or_create_user(self.identity)
        self.assertEqual(current.view.phase, "route_choice")
        self.assertEqual(current.view.version, 0)
        self.assertEqual(user.exp, before_exp)
        self.assertEqual(await self._count("reward_ledger"), 0)
        self.assertEqual(await self._count("workshop_wallet"), 0)

    async def test_fight_passes_real_environment_and_settles_growth(self):
        started = await self._start()
        prepared = await self._prepare_fight(started)
        route = next(
            item for item in prepared.view.routes
            if item.option_id == prepared.view.selected_route_id
        )
        fought = await self.service.fight(
            self.identity, prepared.view.adventure_id, now_ts=self.now_ts
        )
        self.assertEqual(self.engine.environment_ids[-1], route.environment_id)
        self.assertEqual(fought.simulation.environment_id, route.environment_id)
        self.assertGreater(fought.skill_growth_count, 0)
        self.assertGreater(fought.attribute_growth_count, 0)
        self.assertTrue(fought.rewards)
        self.assertTrue(all(item.applied for item in fought.rewards))
        self.assertEqual(
            await self._count(
                "reward_ledger", "WHERE source = ?", ("dungeon_nefia",)
            ),
            len(fought.rewards),
        )

    async def test_full_daily_budget_turns_nefia_exp_into_idempotent_scrap(self):
        started = await self._start()
        prepared = await self._prepare_fight(started)
        user = await self.users.get_or_create_user(self.identity)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO reward_ledger (
                    reward_key, user_pk, battle_id, source, exp_gain,
                    currency_gain, reason, created_at_ts
                ) VALUES (?, ?, NULL, ?, ?, 0, ?, ?)
                """,
                (
                    "test:daily-budget-saturated",
                    user.id,
                    "test_growth",
                    999_999,
                    "{}",
                    self.now_ts,
                ),
            )
            await db.commit()

        fought = await self.service.fight(
            self.identity,
            prepared.view.adventure_id,
            now_ts=self.now_ts,
        )
        experience = next(
            reward
            for reward in fought.rewards
            if reward.reward_type == "experience"
        )
        self.assertEqual(experience.exp_gain, 0)
        self.assertGreater(experience.scrap_gain, 0)
        self.assertIn("成长额度已满", experience.description)

        async def scrap_balance():
            async with await connect_db(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT scrap_balance FROM workshop_wallet WHERE user_pk = ?",
                    (user.id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                return int(row["scrap_balance"])

        balance = await scrap_balance()
        self.assertEqual(balance, experience.scrap_gain)
        replay = await self.service.settle(
            self.identity,
            prepared.view.adventure_id,
            now_ts=self.now_ts,
        )
        replay_experience = next(
            reward
            for reward in replay.rewards
            if reward.reward_key == experience.reward_key
        )
        self.assertFalse(replay_experience.applied)
        self.assertEqual(replay_experience.scrap_gain, experience.scrap_gain)
        self.assertEqual(await scrap_balance(), balance)

    async def test_event_node_skips_combat_and_persists_narrative_rewards(self):
        started = await self._start()
        route = next(item for item in started.view.routes if not item.requires_combat)
        selected = await self.service.choose_route(
            self.identity,
            started.view.adventure_id,
            route.option_id,
            now_ts=self.now_ts,
        )
        route = next(
            item for item in selected.view.routes
            if item.option_id == route.option_id
        )
        prepared = await self.service.choose_risk(
            self.identity,
            selected.view.adventure_id,
            route.risk_choices[0].risk_id,
            now_ts=self.now_ts,
        )
        simulations_before = len(self.engine.environment_ids)
        resolved = await self.service.fight(
            self.identity,
            prepared.view.adventure_id,
            now_ts=self.now_ts,
        )

        self.assertIsNone(resolved.simulation)
        self.assertTrue(resolved.narrative)
        self.assertEqual(len(self.engine.environment_ids), simulations_before)
        self.assertEqual(resolved.view.completed_floors, 1)
        self.assertEqual(resolved.skill_growth_count, 0)
        self.assertEqual(resolved.spell_growth_count, 0)
        self.assertEqual(resolved.attribute_growth_count, 0)
        self.assertTrue(resolved.rewards)
        restored = await self._new_service().view(
            self.identity,
            resolved.view.adventure_id,
            now_ts=self.now_ts,
        )
        self.assertEqual(restored.view, resolved.view)

    async def test_route_view_exposes_final_risk_numbers_and_monster_name(self):
        started = await self._start()
        combat = next(item for item in started.view.routes if item.requires_combat)
        event = next(item for item in started.view.routes if not item.requires_combat)
        self.assertTrue(combat.monster_name)
        self.assertTrue(all(risk.monster_level > 0 for risk in combat.risk_choices))
        self.assertTrue(all(risk.monster_level == 0 for risk in event.risk_choices))
        self.assertTrue(
            all(
                risk.reward_multiplier > 0
                for route in started.view.routes
                for risk in route.risk_choices
            )
        )
        for route in started.view.routes:
            for risk in route.risk_choices:
                policy = reward_quality_policy(
                    max(0.0, risk.reward_multiplier - 1.0)
                    + risk.rare_find_quality_bonus
                )
                self.assertAlmostEqual(
                    risk.reward_quality_bonus,
                    policy.requested_bonus,
                )
                self.assertAlmostEqual(
                    risk.reward_effective_quality_bonus,
                    policy.effective_bonus,
                )
                self.assertAlmostEqual(
                    risk.reward_quality_progress,
                    policy.quality_progress,
                )
                self.assertEqual(
                    risk.reward_minimum_quality,
                    policy.minimum_quality,
                )
                self.assertEqual(
                    risk.reward_guaranteed_upgrades,
                    policy.guaranteed_upgrades,
                )
                self.assertAlmostEqual(
                    risk.reward_upgrade_chance,
                    policy.upgrade_chance,
                )

    async def test_clear_grants_boss_equipment_book_and_retry_is_idempotent(self):
        result = await self._start("verdant_wetland")
        while not result.view.terminal:
            result = await self._prepare_fight(result)
            result = await self.service.fight(
                self.identity, result.view.adventure_id, now_ts=self.now_ts
            )
        self.assertEqual(result.view.phase, "cleared")
        all_rewards = await self.service.settle(
            self.identity, result.view.adventure_id, now_ts=self.now_ts
        )
        reward_types = {item.reward_type for item in all_rewards.rewards}
        self.assertIn("equipment", reward_types)
        self.assertIn("spellbook", reward_types)
        self.assertTrue(all(not item.applied for item in all_rewards.rewards))
        ledger_count = await self._count("reward_ledger")
        equipment_count = await self._count("equipment_items")
        book_count = await self._count("spellbook_items")
        retried = await self.service.settle(
            self.identity, result.view.adventure_id, now_ts=self.now_ts
        )
        self.assertTrue(all(not item.applied for item in retried.rewards))
        self.assertEqual(await self._count("reward_ledger"), ledger_count)
        self.assertEqual(await self._count("equipment_items"), equipment_count)
        self.assertEqual(await self._count("spellbook_items"), book_count)

    async def test_full_spell_catalog_remains_reachable_outside_local_pool(self):
        intent = DungeonRewardIntent(
            "dungeon:proof:floor:1:spellbook",
            "spellbook",
            1,
            1,
            spell_pool=("fire_wall",),
        )
        seen = {
            DungeonRewardSettlementService._select_dungeon_spell(
                intent, seed, set(), player_level=80
            )
            for seed in range(20_000)
        }
        self.assertEqual(seen, set(SPELL_DEFINITIONS))
        self.assertGreater(len(seen - set(intent.spell_pool)), 70)

    async def test_level_one_boss_book_never_crosses_beginner_tier(self):
        intent = DungeonRewardIntent(
            "dungeon:beginner:floor:1:spellbook",
            "spellbook",
            1,
            1,
            spell_pool=("fire_wall", "storm_strike", "magic_arrow"),
        )
        seen = {
            DungeonRewardSettlementService._select_dungeon_spell(
                intent, seed, set(), player_level=1
            )
            for seed in range(5_000)
        }
        self.assertTrue(seen)
        self.assertTrue(
            all(SPELL_DEFINITIONS[spell_id].unlock_level <= 1 for spell_id in seen)
        )
        self.assertNotIn("storm_strike", seen)

    async def test_dungeon_books_prefer_new_spells_and_avoid_unread_stacks(self):
        eligible = {
            spell_id
            for spell_id, definition in SPELL_DEFINITIONS.items()
            if definition.unlock_level <= 20
        }
        known = set(sorted(eligible)[:3])
        held = set(sorted(eligible - known)[:4])
        intent = DungeonRewardIntent(
            "dungeon:novelty:floor:1:spellbook",
            "spellbook",
            1,
            1,
            spell_pool=tuple(sorted(eligible)),
        )

        seen = {
            DungeonRewardSettlementService._select_dungeon_spell(
                intent,
                seed,
                known,
                held,
                player_level=25,
            )
            for seed in range(2_000)
        }

        self.assertTrue(seen)
        self.assertTrue(seen <= eligible - held)
        self.assertTrue(seen & (eligible - known - held))


if __name__ == "__main__":
    unittest.main()
