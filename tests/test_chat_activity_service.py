import asyncio
import os
import sqlite3
import tempfile
import types
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from models.chat_activity import ChatMessageContext
from models.user import UserIdentity
from services.chat_activity_service import (
    ChatActivityPolicy,
    ChatActivityService,
    ChatActivitySettlementService,
    EquipmentServiceDropAdapter,
    SpellServiceBookAdapter,
    chat_activity_day_window,
    format_chat_activity_settlement,
)
from services.db import connect_db, init_db
from services.equipment_service import EquipmentService
from services.progression_rules import level_daily_exp_budget
from services.spell_service import SpellService
from services.user_service import UserService


class RecordingEquipmentPort:
    def __init__(self, key="chat-test-equipment"):
        self.key = key
        self.calls = 0

    async def grant_in_db(self, db, *, user_pk, player_level, seed):
        self.calls += 1
        await db.execute(
            "INSERT INTO feature_grants (user_pk, grant_key, created_at) VALUES (?, ?, 'now')",
            (user_pk, self.key),
        )
        return {
            "kind": "equipment",
            "level": player_level,
            "seed": seed,
        }


class RecordingSpellbookPort:
    def __init__(self, key="chat-test-spellbook", fail=False):
        self.key = key
        self.fail = fail
        self.calls = 0

    async def grant_in_db(self, db, *, user_pk, spell_id, seed):
        self.calls += 1
        await db.execute(
            "INSERT INTO feature_grants (user_pk, grant_key, created_at) VALUES (?, ?, 'now')",
            (user_pk, self.key),
        )
        if self.fail:
            raise RuntimeError("injected spellbook failure")
        return {"kind": "spellbook", "spell_id": spell_id, "seed": seed}


class ChatActivityServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.user = await self.users.get_or_create_user(
            UserIdentity("qq", "group-chat", "chat-user", "闲聊者")
        )
        self.base_ts = int(
            datetime(
                2026, 8, 11, 12, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")
            ).timestamp()
        )

    async def asyncTearDown(self):
        os.remove(self.db_path)

    def message(
        self,
        index,
        *,
        content="今天我们聊聊副本里的机关",
        seconds=0,
        **overrides,
    ):
        values = dict(
            event_key=f"qq:message:{index}",
            group_id="group-chat",
            user_pk=self.user.id,
            content=content,
            occurred_at_ts=self.base_ts + seconds,
        )
        values.update(overrides)
        return ChatMessageContext(**values)

    async def test_commands_bots_short_text_and_duplicate_spam_are_silent(self):
        service = ChatActivityService(self.db_path)
        command = await service.prepare_message(
            self.message(1, content="/签到", is_command=True)
        )
        bot = await service.prepare_message(
            self.message(2, content="机器人自动回复", is_bot=True, seconds=1)
        )
        short = await service.prepare_message(
            self.message(3, content="哈", seconds=2)
        )
        first = await service.prepare_message(self.message(4, seconds=10))
        too_fast = await service.prepare_message(
            self.message(5, content="这个想法挺有意思", seconds=12)
        )
        duplicate = await service.prepare_message(self.message(6, seconds=120))

        self.assertEqual(command.reason, "command_message")
        self.assertEqual(bot.reason, "bot_message")
        self.assertEqual(short.reason, "too_short")
        self.assertTrue(first.accepted)
        self.assertEqual(too_fast.reason, "too_fast")
        self.assertEqual(duplicate.reason, "duplicate_content")
        self.assertFalse(command.should_settle)
        self.assertFalse(duplicate.should_settle)

    async def test_redelivery_does_not_advance_sequence_or_reroll(self):
        service = ChatActivityService(self.db_path)
        context = self.message(1)
        original = await service.prepare_message(context)
        replay = await service.prepare_message(context)

        self.assertTrue(original.accepted)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.valid_message_index, 1)
        self.assertEqual(replay.intent, original.intent)
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT valid_messages, reward_rolls FROM chat_activity_daily"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, (1, 1))

    async def test_named_randomness_depends_on_coordinate_not_event_text(self):
        service = ChatActivityService(self.db_path)
        first = await service.prepare_message(
            self.message(1, content="今天讨论战斗策略")
        )
        async with await connect_db(self.db_path) as db:
            await db.execute("DELETE FROM chat_activity_events")
            await db.execute("DELETE FROM chat_activity_daily")
            await db.execute("DELETE FROM chat_activity_pity")
            await db.commit()
        second = await service.prepare_message(
            self.message(2, content="副本里面发现机关")
        )

        self.assertEqual(first.valid_message_index, second.valid_message_index)
        self.assertEqual(
            first.equipment_probability,
            second.equipment_probability,
        )
        self.assertEqual(first.spellbook_probability, second.spellbook_probability)
        self.assertEqual(first.intent, second.intent)

    async def test_cooldown_does_not_consume_pity_or_reward_roll(self):
        policy = ChatActivityPolicy(minimum_valid_interval_seconds=1)
        service = ChatActivityService(self.db_path, policy)
        await service.prepare_message(self.message(1))
        within = await service.prepare_message(
            self.message(2, content="我们继续研究新的路线", seconds=10)
        )

        self.assertTrue(within.accepted)
        self.assertEqual(within.reason, "accepted_reward_cooldown")
        connection = sqlite3.connect(self.db_path)
        try:
            daily = connection.execute(
                "SELECT valid_messages, reward_rolls FROM chat_activity_daily"
            ).fetchone()
            pity = connection.execute(
                "SELECT equipment_misses, spellbook_misses FROM chat_activity_pity"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(daily, (2, 1))
        self.assertEqual(pity, (1, 1))

    async def test_burst_and_daily_limits_do_not_turn_chat_into_a_grind(self):
        burst_policy = ChatActivityPolicy(
            minimum_valid_interval_seconds=1,
            burst_message_limit=3,
            reward_cooldown_seconds=1,
        )
        service = ChatActivityService(self.db_path, burst_policy)
        for index in range(3):
            result = await service.prepare_message(
                self.message(
                    index,
                    content=f"第{index}种不同的冒险路线",
                    seconds=index * 10,
                )
            )
            self.assertTrue(result.accepted)
        suppressed = await service.prepare_message(
            self.message(9, content="第四种完全不同的冒险路线", seconds=30)
        )
        self.assertEqual(suppressed.reason, "burst_suppressed")

    async def test_first_drop_pity_is_fast_and_later_pity_is_bounded(self):
        self.assertEqual(
            ChatActivityService._equipment_probability(0, first_drop=True),
            0.04,
        )
        self.assertEqual(
            ChatActivityService._equipment_probability(17, first_drop=True),
            1.0,
        )
        self.assertEqual(
            ChatActivityService._equipment_probability(49, first_drop=False),
            1.0,
        )
        self.assertEqual(
            ChatActivityService._spellbook_probability(27, first_drop=True),
            1.0,
        )
        self.assertEqual(
            ChatActivityService._spellbook_probability(64, first_drop=False),
            1.0,
        )

    async def test_forced_pity_reserves_story_equipment_and_spellbook(self):
        policy = ChatActivityPolicy(exp_probability=1.0)
        service = ChatActivityService(self.db_path, policy)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO chat_activity_pity (
                    user_pk, equipment_misses, spellbook_misses
                ) VALUES (?, 17, 27)
                """,
                (self.user.id,),
            )
            await db.commit()

        decision = await service.prepare_message(self.message(1))

        self.assertTrue(decision.should_settle)
        self.assertEqual(decision.equipment_probability, 1.0)
        self.assertEqual(decision.spellbook_probability, 1.0)
        self.assertTrue(decision.intent.has_equipment)
        self.assertTrue(decision.intent.has_spellbook)
        self.assertGreater(decision.intent.experience, 0)
        self.assertIn("奇遇", decision.intent.story_text)
        pending = await service.pending_intents(user_pk=self.user.id)
        self.assertEqual(pending, (decision.intent,))

    async def test_spellbook_drop_prefers_new_spell_and_avoids_unread_stacks(self):
        service = ChatActivityService(self.db_path)
        async with await connect_db(self.db_path) as db:
            for spell_id in ("magic_arrow", "minor_heal"):
                await db.execute(
                    """
                    INSERT INTO user_spells (
                        user_pk, spell_id, level, exp, potential
                    ) VALUES (?, ?, 1, 0, 100)
                    """,
                    (self.user.id, spell_id),
                )
            for random_seed, spell_id in enumerate(
                ("magic_arrow", "minor_heal", "armor_spell"),
                1,
            ):
                await db.execute(
                    """
                    INSERT INTO spellbook_items (
                        owner_pk, spell_id, quantity, source,
                        random_seed, bound, created_at
                    ) VALUES (?, ?, 1, 'test', ?, 1, '2026-08-11T00:00:00')
                    """,
                    (self.user.id, spell_id, random_seed),
                )
            await db.execute(
                """
                INSERT INTO chat_activity_pity (
                    user_pk, equipment_misses, spellbook_misses
                ) VALUES (?, 0, 27)
                """,
                (self.user.id,),
            )
            await db.commit()

        decision = await service.prepare_message(self.message(1))

        self.assertTrue(decision.intent.has_spellbook)
        self.assertEqual(decision.intent.spell_id, "hero")

    async def test_atomic_settlement_retry_never_double_grants(self):
        policy = ChatActivityPolicy(exp_probability=1.0)
        preparation = ChatActivityService(self.db_path, policy)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "INSERT INTO chat_activity_pity (user_pk, equipment_misses, spellbook_misses) VALUES (?, 17, 27)",
                (self.user.id,),
            )
            await db.commit()
        intent = (await preparation.prepare_message(self.message(1))).intent
        equipment = RecordingEquipmentPort()
        failing_book = RecordingSpellbookPort(fail=True)
        failing_settlement = ChatActivitySettlementService(
            self.db_path,
            self.users,
            equipment_port=equipment,
            spellbook_port=failing_book,
            policy=policy,
        )

        with self.assertRaisesRegex(RuntimeError, "injected"):
            await failing_settlement.settle(intent)
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM reward_ledger").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM feature_grants").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT exp FROM users WHERE id = ?", (self.user.id,)).fetchone()[0],
                0,
            )
        finally:
            connection.close()

        good_equipment = RecordingEquipmentPort()
        good_book = RecordingSpellbookPort()
        settlement = ChatActivitySettlementService(
            self.db_path,
            self.users,
            equipment_port=good_equipment,
            spellbook_port=good_book,
            policy=policy,
        )
        outcomes = await asyncio.gather(
            settlement.settle(intent),
            settlement.settle(intent),
        )
        applied = next(item for item in outcomes if item.applied)
        duplicate = next(item for item in outcomes if not item.applied)

        self.assertTrue(applied.applied)
        self.assertTrue(applied.should_announce)
        announcement = format_chat_activity_settlement(applied, username="用户名")
        self.assertTrue(announcement.startswith("用户名：\n"))
        self.assertIn("冒险见识", announcement)
        self.assertIn("魔法书", announcement)
        self.assertIn("恢复魔法潜力", announcement)
        self.assertFalse(duplicate.applied)
        self.assertFalse(duplicate.should_announce)
        self.assertEqual(format_chat_activity_settlement(duplicate), "")
        self.assertEqual(good_equipment.calls, 1)
        self.assertEqual(good_book.calls, 1)
        self.assertEqual(await preparation.pending_intents(user_pk=self.user.id), ())

    async def test_recovery_pages_past_failures_and_more_than_one_batch(self):
        service = ChatActivityService(self.db_path)
        intents = []
        async with await connect_db(self.db_path) as db:
            for index in range(205):
                intent = types.SimpleNamespace()
                reward_key = f"chat:2026-08-11:{index:04d}"
                payload = (
                    '{"day_key":"2026-08-11","equipment_seed":null,'
                    '"experience":1,"group_id":"group-chat",'
                    f'"reward_key":"{reward_key}",'
                    '"ruleset_id":"chat-serendipity-v12",'
                    '"spell_id":null,"spell_name":null,'
                    '"spellbook_seed":null,"story_id":"recovery",'
                    '"story_text":"recovery","user_pk":1,'
                    f'"valid_message_index":{index + 1}' + '}'
                )
                await db.execute(
                    """
                    INSERT INTO chat_activity_events (
                        event_key, user_pk, group_id, occurred_at_ts,
                        accepted, decision_reason, day_key,
                        reward_key, intent_json, settled
                    ) VALUES (?, ?, 'group-chat', ?, 1, 'reserved',
                              '2026-08-11', ?, ?, 0)
                    """,
                    (f"event:{index:04d}", self.user.id, index, reward_key, payload),
                )
            await db.commit()

        class Settlement:
            def __init__(inner_self):
                inner_self.keys = []

            async def settle(inner_self, intent):
                inner_self.keys.append(intent.reward_key)
                if intent.reward_key.endswith("0050"):
                    raise RuntimeError("poisoned reservation")

        settlement = Settlement()
        errors = []
        recovered, failed = await service.recover_pending_intents(
            settlement,
            batch_size=100,
            on_error=lambda intent, exc: errors.append((intent, exc)),
        )

        self.assertEqual(len(settlement.keys), 205)
        self.assertEqual(len(set(settlement.keys)), 205)
        self.assertEqual((recovered, failed), (204, 1))
        self.assertEqual(len(errors), 1)

    async def test_recovery_repairs_event_already_present_in_reward_ledger(self):
        policy = ChatActivityPolicy(exp_probability=1.0)
        preparation = ChatActivityService(self.db_path, policy)
        intent = (await preparation.prepare_message(self.message(1))).intent
        settlement = ChatActivitySettlementService(
            self.db_path,
            self.users,
            equipment_port=RecordingEquipmentPort(),
            spellbook_port=RecordingSpellbookPort(),
            policy=policy,
        )
        first = await settlement.settle(intent)
        self.assertTrue(first.applied)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE chat_activity_events SET settled = 0, actual_exp = 0 "
                "WHERE reward_key = ?",
                (intent.reward_key,),
            )
            await db.commit()

        recovered, failed = await preparation.recover_pending_intents(
            settlement,
            batch_size=1,
        )

        self.assertEqual((recovered, failed), (1, 0))
        self.assertEqual(await preparation.pending_intents(), ())

    async def test_experience_uses_remaining_shared_daily_budget(self):
        policy = ChatActivityPolicy(exp_probability=1.0)
        service = ChatActivityService(self.db_path, policy)
        day_key, _, _ = chat_activity_day_window(self.base_ts)
        budget = level_daily_exp_budget(1)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO checkins (
                    user_pk, checkin_date, streak_days, exp_gain, created_at
                ) VALUES (?, ?, 1, ?, 'now')
                """,
                (self.user.id, day_key, budget - 1),
            )
            await db.commit()

        intent = (await service.prepare_message(self.message(1))).intent
        self.assertIsNotNone(intent)
        self.assertEqual(intent.experience, 1)
        settlement = ChatActivitySettlementService(
            self.db_path,
            self.users,
            equipment_port=RecordingEquipmentPort(),
            spellbook_port=RecordingSpellbookPort(),
            policy=policy,
        )
        result = await settlement.settle(intent)
        self.assertEqual(result.experience, 1)
        connection = sqlite3.connect(self.db_path)
        try:
            ledger_exp = connection.execute(
                "SELECT exp_gain FROM reward_ledger WHERE reward_key = ?",
                (intent.reward_key,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(ledger_exp, 1)

    async def test_production_adapters_grant_useful_equipment_and_real_book(self):
        policy = ChatActivityPolicy(exp_probability=1.0)
        service = ChatActivityService(self.db_path, policy)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "INSERT INTO chat_activity_pity (user_pk, equipment_misses, spellbook_misses) VALUES (?, 17, 27)",
                (self.user.id,),
            )
            await db.commit()
        intent = (await service.prepare_message(self.message(1))).intent
        settlement = ChatActivitySettlementService(
            self.db_path,
            self.users,
            equipment_port=EquipmentServiceDropAdapter(
                EquipmentService(self.db_path)
            ),
            spellbook_port=SpellServiceBookAdapter(SpellService(self.db_path)),
            policy=policy,
        )

        result = await settlement.settle(intent)

        self.assertIn(
            result.equipment.quality,
            {"excellent", "rare", "epic", "mythic"},
        )
        self.assertEqual(result.spellbook.spell_id, intent.spell_id)
        self.assertEqual(result.spellbook.source, "chat_serendipity")
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM equipment_items WHERE owner_pk = ?",
                    (self.user.id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM spellbook_items WHERE owner_pk = ?",
                    (self.user.id,),
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    async def test_excellent_quality_is_accepted_without_rerolling(self):
        class Catalog:
            def __init__(self):
                self.calls = 0

            def generate_reward(self, *args, **kwargs):
                self.calls += 1
                return types.SimpleNamespace(
                    quality="excellent",
                    name="优秀短剑",
                    item_level=1,
                )

            async def insert_item_in_db(self, db, item):
                return 77

        catalog = Catalog()
        result = await EquipmentServiceDropAdapter(catalog).grant_in_db(
            None,
            user_pk=self.user.id,
            player_level=1,
            seed=9,
        )
        self.assertEqual(catalog.calls, 1)
        self.assertEqual(result.quality, "excellent")
        self.assertEqual(result.id, 77)

    async def test_schema_has_durable_reservation_and_pity_tables(self):
        connection = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(
            {
                "chat_activity_daily",
                "chat_activity_events",
                "chat_activity_pity",
            }.issubset(tables)
        )
        self.assertIn("idx_chat_activity_reward_key", indexes)

    async def test_active_day_resets_at_four_in_hong_kong(self):
        zone = ZoneInfo("Asia/Hong_Kong")
        before = int(datetime(2026, 8, 11, 3, 59, tzinfo=zone).timestamp())
        after = int(datetime(2026, 8, 11, 4, 0, tzinfo=zone).timestamp())
        self.assertEqual(chat_activity_day_window(before)[0], "2026-08-10")
        self.assertEqual(chat_activity_day_window(after)[0], "2026-08-11")


if __name__ == "__main__":
    unittest.main()
