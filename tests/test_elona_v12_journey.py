import os
import tempfile
import unittest

from models.chat_activity import ChatMessageContext
from models.user import UserIdentity
from services.attribute_service import AttributeService
from services.build_service import CombatBuildService
from services.chat_activity_service import (
    ChatActivityPolicy,
    ChatActivityService,
    ChatActivitySettlementService,
    EquipmentServiceDropAdapter,
    SpellServiceBookAdapter,
    chat_activity_day_window,
)
from services.combat_ai import STRATEGY_PROFILES
from services.combat_engine import SideviewCombatEngine
from services.combat_random import KeyedEntropy
from services.db import connect_db, init_db
from services.dungeon_application_service import DungeonAdventureApplicationService
from services.dungeon_catalog import DungeonCatalog
from services.equipment_service import EquipmentService
from services.monster_build_service import MonsterBuildService
from services.monster_catalog import MonsterCatalog
from services.skill_service import SkillService
from services.spell_service import SpellService
from services.user_service import UserService


class ElonaV12JourneyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
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
        self.monster_builds = MonsterBuildService(
            self.monsters,
            self.attributes,
        )
        self.dungeons = DungeonCatalog(monster_catalog=self.monsters)
        self.now_ts = 1_786_406_400

    async def asyncTearDown(self):
        os.remove(self.db_path)

    @staticmethod
    def _group_that_drops_magic_arrow(policy, day_key):
        beginner_pool = (
            "magic_arrow",
            "minor_heal",
            "armor_spell",
            "hero",
        )
        for index in range(1, 500):
            group_id = f"journey-group-{index}"
            entropy = KeyedEntropy(
                policy.ruleset_id,
                f"{group_id}|{day_key}|1|1",
            )
            selected = entropy.choice(
                beginner_pool,
                stream="spellbook-kind",
                actor=1,
                action_seq=1,
            )
            if selected == "magic_arrow":
                return group_id
        raise AssertionError("could not find deterministic beginner book seed")

    async def test_chat_book_to_spell_combat_and_nefia_restart_loop(self):
        policy = ChatActivityPolicy(exp_probability=1.0)
        day_key = chat_activity_day_window(self.now_ts)[0]
        group_id = self._group_that_drops_magic_arrow(policy, day_key)
        identity = UserIdentity("test", group_id, "journey-user", "旅人")
        user = await self.users.get_or_create_user(identity)
        self.assertEqual(user.id, 1)

        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO chat_activity_pity (
                    user_pk, equipment_misses, spellbook_misses
                ) VALUES (?, 0, 27)
                """,
                (user.id,),
            )
            await db.commit()

        chat = ChatActivityService(self.db_path, policy)
        context = ChatMessageContext(
            "qq:journey:message:1",
            group_id,
            user.id,
            "今天的风闻起来像一场冒险",
            self.now_ts,
        )
        decision = await chat.prepare_message(context)
        self.assertTrue(decision.intent.has_spellbook)
        self.assertEqual(decision.intent.spell_id, "magic_arrow")

        chat_settlement = ChatActivitySettlementService(
            self.db_path,
            self.users,
            equipment_port=EquipmentServiceDropAdapter(self.equipment),
            spellbook_port=SpellServiceBookAdapter(self.spells),
            policy=policy,
        )
        settled = await chat_settlement.settle(decision.intent)
        replay = await chat_settlement.settle(decision.intent)
        self.assertTrue(settled.applied)
        self.assertFalse(replay.applied)
        replayed_decision = await chat.prepare_message(context)
        self.assertTrue(replayed_decision.replayed)
        self.assertEqual(replayed_decision.intent, decision.intent)

        books = await self.spells.list_books(user.id)
        book = next(item for item in books if item.spell_id == "magic_arrow")
        async with await connect_db(self.db_path) as db:
            for skill_id in ("magic_training", "reading"):
                await db.execute(
                    """
                    INSERT OR REPLACE INTO user_skills (
                        user_pk, skill_id, level, exp, potential
                    ) VALUES (?, ?, 80, 0, 100)
                    """,
                    (user.id, skill_id),
                )
            await db.commit()
        read = await self.spells.read_book(
            user,
            book.id,
            random_seed=1,
            reading_bonus=1.0,
            now=self.now_ts,
        )
        self.assertTrue(read.success)
        self.assertEqual(read.outcome, "learned")
        await self.skills.set_active_slot(user, 1, "魔法箭")

        opponent = await self.users.get_or_create_user(
            UserIdentity("test", group_id, "journey-target", "木桩守卫")
        )
        async with await connect_db(self.db_path) as db:
            left = await self.builds.snapshot_in_db(db, user, "全力猛攻")
            right = await self.builds.snapshot_in_db(db, opponent, "全力猛攻")
            await db.commit()
        engine = SideviewCombatEngine()
        profile = STRATEGY_PROFILES["全力猛攻"]
        battle = engine.simulate(left, right, profile, profile, 20260812)
        self.assertTrue(
            any(
                event.actor_pk == user.id
                and event.kind in {"spell_cast_start", "spell_cast"}
                and event.skill_id == "magic_arrow"
                for event in battle.events
            )
        )

        dungeon = DungeonAdventureApplicationService(
            self.db_path,
            self.users,
            self.builds,
            self.monster_builds,
            self.equipment,
            self.skills,
            self.attributes,
            self.spells,
            dungeon_catalog=self.dungeons,
        )
        started = await dungeon.start_or_resume(
            identity,
            "verdant_wetland",
            now_ts=self.now_ts,
        )
        event_route = next(
            route for route in started.view.routes if not route.requires_combat
        )
        selected = await dungeon.choose_route(
            identity,
            started.view.adventure_id,
            event_route.option_id,
            now_ts=self.now_ts,
        )
        event_route = next(
            route
            for route in selected.view.routes
            if route.option_id == event_route.option_id
        )
        prepared = await dungeon.choose_risk(
            identity,
            selected.view.adventure_id,
            event_route.risk_choices[0].risk_id,
            now_ts=self.now_ts,
        )
        resolved = await dungeon.fight(
            identity,
            prepared.view.adventure_id,
            now_ts=self.now_ts,
        )
        self.assertIsNone(resolved.simulation)
        self.assertTrue(resolved.narrative)
        self.assertEqual(resolved.view.completed_floors, 1)
        self.assertTrue(all(item.applied for item in resolved.rewards))

        restarted = DungeonAdventureApplicationService(
            self.db_path,
            self.users,
            self.builds,
            self.monster_builds,
            self.equipment,
            self.skills,
            self.attributes,
            self.spells,
            dungeon_catalog=self.dungeons,
        )
        restored = await restarted.view(
            identity,
            resolved.view.adventure_id,
            now_ts=self.now_ts,
        )
        self.assertEqual(restored.view, resolved.view)
        duplicate_rewards = await restarted.settle(
            identity,
            resolved.view.adventure_id,
            now_ts=self.now_ts,
        )
        self.assertTrue(duplicate_rewards.rewards)
        self.assertTrue(all(not item.applied for item in duplicate_rewards.rewards))
        retreated = await restarted.retreat(
            identity,
            resolved.view.adventure_id,
            now_ts=self.now_ts,
        )
        self.assertEqual(retreated.view.phase, "retreated")


if __name__ == "__main__":
    unittest.main()
