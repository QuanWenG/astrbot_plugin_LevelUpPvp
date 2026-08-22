import os
import sys
import tempfile
import types
import unittest
from unittest import mock


def _install_dependency_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.logger = types.SimpleNamespace(exception=lambda *args, **kwargs: None)

    astrbot_event = types.ModuleType("astrbot.api.event")
    astrbot_event.AstrMessageEvent = object

    components = types.ModuleType("astrbot.api.message_components")

    class At:
        def __init__(self, qq, name=""):
            self.qq = qq
            self.name = name

    class Node:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Image:
        def __init__(self, file=""):
            self.file = file

    components.At = At
    components.Node = Node
    components.Plain = Plain
    components.Image = Image

    io_module = types.ModuleType("astrbot.core.utils.io")
    io_module.save_temp_img = lambda image: "temp.png"
    font_module = types.ModuleType("astrbot.core.utils.t2i.local_strategy")
    font_module.FontManager = types.SimpleNamespace(get_font=lambda size: None)

    pil = types.ModuleType("PIL")
    pil.Image = types.SimpleNamespace()
    pil.ImageDraw = types.SimpleNamespace(ImageDraw=object)

    modules = {
        "astrbot": astrbot,
        "astrbot.api": astrbot_api,
        "astrbot.api.event": astrbot_event,
        "astrbot.api.message_components": components,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
        "astrbot.core.utils.io": io_module,
        "astrbot.core.utils.t2i": types.ModuleType("astrbot.core.utils.t2i"),
        "astrbot.core.utils.t2i.local_strategy": font_module,
        "PIL": pil,
    }
    for name, module in modules.items():
        sys.modules.setdefault(name, module)


_install_dependency_stubs()

from astrbot.api.message_components import At
from handles.command_handler import LevelUpPvpCommandHandler
from models.ability import (
    SpellBookCollectionEntry,
    SpellBookCraftResult,
    SpellBookItem,
    SpellBookLibrary,
    SpellReadResult,
    SpellResearchCraftOption,
    UserSpell,
)
from models.chat_activity import (
    CHAT_ACTIVITY_RULESET_ID,
    ChatActivityDecision,
    ChatActivitySettlementResult,
    ChatRewardIntent,
)
from models.user import CheckinResult, User, UserIdentity
from models.workshop import (
    BulkSalvagePreview,
    BulkSalvageResult,
    DominatedSalvageItem,
)
from services.db import connect_db, init_db
from services.progression_rules import level_exp_required
from services.skill_service import SkillService
from services.user_service import UserService


class FakeEvent:
    def __init__(
        self,
        *,
        group_id="group-1",
        sender_id="user-1",
        self_id="bot-1",
        platform="test",
        message="",
        messages=None,
        sender_name="测试用户",
    ):
        self.group_id = group_id
        self.sender_id = sender_id
        self.self_id = self_id
        self.platform = platform
        self.message = message
        self.messages = messages or []
        self.sender_name = sender_name

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return self.sender_id

    def get_self_id(self):
        return self.self_id

    def get_message_str(self):
        return self.message

    def get_messages(self):
        return self.messages

    def get_platform_id(self):
        return "test"

    def get_platform_name(self):
        return self.platform

    def get_session_id(self):
        return self.group_id

    def get_sender_name(self):
        return self.sender_name

    def plain_result(self, text):
        return text

    def image_result(self, url):
        return ("image", url)

    def chain_result(self, chain):
        return chain


class FakeCheckinService:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def checkin(self, identity):
        result = self.results[self.calls]
        self.calls += 1
        return result


def _user(total_exp=20):
    return User(
        id=1,
        platform="test",
        group_id="group-1",
        user_id="user-1",
        nickname="测试用户",
        level=1,
        exp=total_exp,
        total_exp=total_exp,
        stat_points=0,
        level_up_count=0,
        hp=10,
        atk=5,
        defense=5,
        speed=5,
        luck=5,
        wins=0,
        losses=0,
        created_at="2026-07-03T00:00:00",
        updated_at="2026-07-03T00:00:00",
    )


def _result(*, already_checked=False):
    return CheckinResult(
        user=_user(),
        exp_gain=20,
        streak_days=1,
        level_ups=[],
        already_checked=already_checked,
    )


class AutoCheckinHandlerTests(unittest.IsolatedAsyncioTestCase):
    def _handler(self, *results):
        return LevelUpPvpCommandHandler(
            context=None,
            user_service=types.SimpleNamespace(
                has_registered_nickname=lambda identity: (_ for _ in ()).throw(
                    AssertionError("签到不应检查登记状态")
                )
            ),
            checkin_service=FakeCheckinService(*results),
            stat_service=None,
            battle_service=None,
        )

    def test_any_group_message_type_is_a_candidate(self):
        handler = self._handler()
        event = FakeEvent(messages=[object()])
        self.assertTrue(handler.is_auto_checkin_event(event))

    def test_private_self_and_missing_sender_events_are_ignored(self):
        handler = self._handler()
        self.assertFalse(
            handler.is_auto_checkin_event(FakeEvent(group_id="", messages=[object()]))
        )
        self.assertFalse(
            handler.is_auto_checkin_event(
                FakeEvent(sender_id="bot-1", messages=[object()])
            )
        )
        self.assertFalse(
            handler.is_auto_checkin_event(
                FakeEvent(sender_id=1, self_id="1", messages=[object()])
            )
        )
        self.assertFalse(
            handler.is_auto_checkin_event(FakeEvent(sender_id="", messages=[object()]))
        )

    def test_explicit_checkin_is_left_to_command_handler(self):
        handler = self._handler()
        self.assertFalse(
            handler.is_auto_checkin_event(FakeEvent(message="/签到"))
        )
        self.assertFalse(
            handler.is_auto_checkin_event(
                FakeEvent(
                    message="<@bot-1> 签到",
                    messages=[At("bot-1", "机器人")],
                )
            )
        )

    def test_commands_are_auto_checkin_candidates_but_explicit_sign_is_not(self):
        handler = self._handler()
        self.assertTrue(handler.is_auto_checkin_event(FakeEvent(message="/面板")))
        self.assertTrue(
            handler.is_auto_checkin_event(
                FakeEvent(
                    message="<@bot-1> 面板",
                    messages=[At("bot-1", "机器人")],
                )
            )
        )
        self.assertTrue(
            handler.is_auto_checkin_event(
                FakeEvent(
                    message="艾斯比 <@user-2>",
                    messages=[At("user-2", "对手")],
                )
            )
        )
        self.assertTrue(
            handler.is_auto_checkin_event(FakeEvent(message="今天去哪里探险？"))
        )

    async def test_first_command_settles_checkin_without_extra_reply(self):
        handler = self._handler(_result())

        replies = [
            item
            async for item in handler.ambient_activity(
                FakeEvent(message="/面板")
            )
        ]

        self.assertEqual(handler.checkin_service.calls, 1)
        self.assertEqual(replies, [])

    async def test_auto_checkin_replies_once_then_stays_silent(self):
        handler = self._handler(_result(), _result(already_checked=True))
        event = FakeEvent(messages=[object()])

        first = [item async for item in handler.auto_checkin(event)]
        second = [item async for item in handler.auto_checkin(event)]

        self.assertEqual(len(first), 1)
        self.assertIn("签到成功", first[0])
        self.assertEqual(second, [])

    async def test_ambient_activity_prefers_discovery_over_checkin_reply(self):
        handler = self._handler(_result())
        handler.chat_activity = mock.Mock(
            return_value=self._async_items("发现了一本魔法书")
        )

        replies = [
            item async for item in handler.ambient_activity(FakeEvent())
        ]

        self.assertEqual(replies, ["发现了一本魔法书"])

    @staticmethod
    async def _async_items(*items):
        for item in items:
            yield item

    async def test_explicit_checkin_does_not_require_registration(self):
        handler = self._handler(_result())
        replies = [item async for item in handler.sign(FakeEvent(message="/签到"))]

        self.assertEqual(len(replies), 1)
        self.assertIn("签到成功", replies[0])

    async def test_repeated_explicit_checkin_returns_today_summary(self):
        handler = self._handler(_result(already_checked=True))
        replies = [item async for item in handler.sign(FakeEvent(message="/签到"))]

        self.assertEqual(len(replies), 1)
        self.assertIn("今天已经签到过了", replies[0])
        self.assertIn("今日签到经验：20", replies[0])
        self.assertIn("连续签到：1 天", replies[0])
        self.assertIn(
            f"等级：Lv.1 经验：20/{level_exp_required(1)}",
            replies[0],
        )


class ChatActivityHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _intent():
        return ChatRewardIntent(
            reward_key="chat:2026-08-11:user-1:1",
            ruleset_id=CHAT_ACTIVITY_RULESET_ID,
            group_id="group-1",
            day_key="2026-08-11",
            user_pk=1,
            valid_message_index=1,
            experience=3,
            equipment_seed=7,
            spell_id="magic_arrow",
            spell_name="魔法箭",
            spellbook_seed=8,
            story_id="test",
            story_text="一阵风把两个包裹吹到了你脚边。",
        )

    def _handler(self, settlement):
        intent = self._intent()

        class Activity:
            context = None

            async def prepare_message(inner_self, context):
                inner_self.context = context
                return ChatActivityDecision(
                    context.event_key,
                    True,
                    "reward_reserved",
                    intent.day_key,
                    1,
                    1,
                    intent,
                )

        class Settlement:
            calls = 0

            async def settle(inner_self, value):
                inner_self.calls += 1
                self.assertEqual(value, intent)
                return settlement

        activity = Activity()
        settlement_service = Settlement()
        user_service = types.SimpleNamespace(
            get_or_create_user=mock.AsyncMock(return_value=_user())
        )
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
            chat_activity_service=activity,
            chat_activity_settlement_service=settlement_service,
        )
        return handler, activity, settlement_service

    async def test_drop_is_announced_with_actionable_ids(self):
        result = ChatActivitySettlementResult(
            reward_key=self._intent().reward_key,
            applied=True,
            experience=3,
            equipment=types.SimpleNamespace(
                id=21,
                name="旅人短剑",
                quality="excellent",
                item_level=4,
            ),
            spellbook=types.SimpleNamespace(id=22),
            spell_name="魔法箭",
            story_text="一阵风把两个包裹吹到了你脚边。",
        )
        handler, activity, settlement = self._handler(result)
        event = FakeEvent(message="今天去哪里冒险？")

        replies = [item async for item in handler.chat_activity(event)]

        self.assertEqual(len(replies), 1)
        self.assertIn("旅人短剑", replies[0])
        self.assertIn("魔法箭", replies[0])
        self.assertIn("/装备详情 21", replies[0])
        self.assertIn("/阅读 22", replies[0])
        self.assertFalse(activity.context.is_command)
        self.assertEqual(settlement.calls, 1)

    async def test_xp_only_growth_stays_silent(self):
        result = ChatActivitySettlementResult(
            reward_key=self._intent().reward_key,
            applied=True,
            experience=3,
            story_text="你从闲谈里悟到一点心得。",
        )
        handler, _, settlement = self._handler(result)

        replies = [
            item
            async for item in handler.chat_activity(
                FakeEvent(message="普通而自然的一句话")
            )
        ]

        self.assertEqual(replies, [])
        self.assertEqual(settlement.calls, 1)

    async def test_commands_are_marked_for_domain_rejection(self):
        result = ChatActivitySettlementResult(
            reward_key=self._intent().reward_key,
            applied=False,
        )
        handler, activity, _ = self._handler(result)

        _ = [
            item
            async for item in handler.chat_activity(
                FakeEvent(message="/面板")
            )
        ]

        self.assertTrue(activity.context.is_command)


class BatchCommandParsingTests(unittest.TestCase):
    def setUp(self):
        self.handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=None,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )

    def test_skill_range_uses_skill_display_order_and_is_inclusive(self):
        self.assertEqual(
            self.handler._expand_skill_names("斧头专精-战术"),
            ("斧头专精", "格斗技巧", "镰刀专精", "战术"),
        )
        with self.assertRaisesRegex(ValueError, "顺序"):
            self.handler._expand_skill_names("战术-斧头专精")

    def test_batch_pair_parsers(self):
        self.assertEqual(
            self.handler._parse_train_assignments(
                "斧头专精 2 格斗技巧 1"
            ),
            (("斧头专精", 2), ("格斗技巧", 1)),
        )
        self.assertEqual(
            self.handler._parse_train_assignments("斧头专精"),
            (("斧头专精", 1),),
        )
        self.assertEqual(
            self.handler._parse_skill_slot_assignments(
                "1 强击 2 清空"
            ),
            ((1, "强击"), (2, "清空")),
        )
        self.assertEqual(
            self.handler._parse_equip_assignments("2 头 3 颈"),
            ((2, "head"), (3, "neck")),
        )


class LongTextReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=None,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )

    async def test_five_lines_and_239_characters_stay_plain(self):
        event = FakeEvent()

        five_lines = "\n".join(["短"] * 5)
        result = await self.handler.reply_text(event, five_lines)
        self.assertEqual(five_lines, result)

        text_239 = "文" * 239
        result = await self.handler.reply_text(event, text_239)
        self.assertEqual(text_239, result)

    async def test_six_lines_or_240_characters_render_as_image(self):
        event = FakeEvent()
        rendered = object()
        with mock.patch(
            "handles.command_handler.render_text_card",
            return_value=rendered,
        ) as render, mock.patch(
            "handles.command_handler.save_temp_img",
            return_value="reply.png",
        ):
            six_lines = "\n".join(["内容"] * 6)
            result = await self.handler.reply_text(event, six_lines, "测试")
            self.assertEqual(("image", "reply.png"), result)
            render.assert_called_once_with(six_lines, title="测试")

        with mock.patch(
            "handles.command_handler.render_text_card",
            return_value=rendered,
        ), mock.patch(
            "handles.command_handler.save_temp_img",
            return_value="reply.png",
        ):
            result = await self.handler.reply_text(event, "文" * 240)
            self.assertEqual(("image", "reply.png"), result)

    async def test_aiocqhttp_uses_one_forward_image_node(self):
        event = FakeEvent(platform="aiocqhttp")
        with mock.patch(
            "handles.command_handler.render_text_card",
            return_value=object(),
        ), mock.patch(
            "handles.command_handler.save_temp_img",
            return_value="reply.png",
        ):
            result = await self.handler.reply_text(
                event, "\n".join(["内容"] * 6), "LevelUpPvp 面板"
            )

        self.assertEqual(1, len(result))
        self.assertEqual("LevelUpPvp 面板", result[0].name)
        self.assertEqual("reply.png", result[0].content[0].file)

    async def test_render_failure_falls_back_to_exact_original_text(self):
        event = FakeEvent()
        original = "第一行\n" + "很长的正文" * 60
        with mock.patch(
            "handles.command_handler.render_text_card",
            side_effect=RuntimeError("render failed"),
        ):
            result = await self.handler.reply_text(event, original)
        self.assertEqual(original, result)


class NicknameRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_argument_uses_event_sender_name(self):
        registered_user = _user()
        registered_user.nickname = "荃翁龟"
        user_service = types.SimpleNamespace(
            register_nickname=mock.AsyncMock(return_value=registered_user),
        )
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )
        event = FakeEvent(
            platform="qq_official",
            sender_id="BC729CFA021694764C24EF8C285DD78B",
            sender_name="荃翁龟",
        )

        replies = [reply async for reply in handler.register_nickname(event)]

        self.assertEqual(["登记成功：荃翁龟"], replies)
        identity, nickname = user_service.register_nickname.await_args.args
        self.assertEqual("荃翁龟", identity.nickname)
        self.assertEqual("荃翁龟", nickname)

    async def test_explicit_nickname_remains_an_override(self):
        registered_user = _user()
        registered_user.nickname = "自定义昵称"
        user_service = types.SimpleNamespace(
            register_nickname=mock.AsyncMock(return_value=registered_user),
        )
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )

        replies = [
            reply
            async for reply in handler.register_nickname(
                FakeEvent(sender_name="事件用户名"),
                "自定义昵称",
            )
        ]

        self.assertEqual(["登记成功：自定义昵称"], replies)
        self.assertEqual(
            "自定义昵称",
            user_service.register_nickname.await_args.args[1],
        )

    async def test_missing_event_name_keeps_manual_fallback(self):
        user_service = types.SimpleNamespace(
            register_nickname=mock.AsyncMock(),
        )
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )

        replies = [
            reply
            async for reply in handler.register_nickname(
                FakeEvent(sender_name=""),
            )
        ]

        self.assertEqual(
            ["当前消息未携带平台用户名，无法自动登记。"],
            replies,
        )
        user_service.register_nickname.assert_not_awaited()


class AutomaticNicknameRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_group_message_registers_platform_username(self):
        user_service = types.SimpleNamespace(
            has_registered_nickname=mock.AsyncMock(return_value=False),
            register_nickname=mock.AsyncMock(return_value=_user()),
        )
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )
        event = FakeEvent(
            platform="qq_official",
            sender_id="BC729CFA021694764C24EF8C285DD78B",
            sender_name="荃翁龟",
            message="",
            messages=[object()],
        )

        self.assertTrue(await handler.ensure_sender_registered(event))
        identity, nickname = user_service.register_nickname.await_args.args
        self.assertEqual("BC729CFA021694764C24EF8C285DD78B", identity.user_id)
        self.assertEqual("荃翁龟", identity.nickname)
        self.assertEqual("荃翁龟", nickname)

    async def test_existing_registration_is_not_overwritten(self):
        user_service = types.SimpleNamespace(
            has_registered_nickname=mock.AsyncMock(return_value=True),
            register_nickname=mock.AsyncMock(),
        )
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )

        self.assertTrue(
            await handler.ensure_sender_registered(
                FakeEvent(sender_name="平台新名字"),
            )
        )
        user_service.register_nickname.assert_not_awaited()

    async def test_restricted_command_registration_gate_is_automatic(self):
        user_service = types.SimpleNamespace(
            has_registered_nickname=mock.AsyncMock(return_value=False),
            register_nickname=mock.AsyncMock(return_value=_user()),
        )
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )

        error = await handler._registration_error(
            FakeEvent(sender_name="荃翁龟"),
        )

        self.assertEqual("", error)
        user_service.register_nickname.assert_awaited_once()

    async def test_missing_platform_username_does_not_create_mapping(self):
        user_service = types.SimpleNamespace(
            has_registered_nickname=mock.AsyncMock(),
            register_nickname=mock.AsyncMock(),
        )
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )

        self.assertFalse(
            await handler.ensure_sender_registered(
                FakeEvent(sender_name=""),
            )
        )
        user_service.has_registered_nickname.assert_not_awaited()
        user_service.register_nickname.assert_not_awaited()


class AstrBotMentionCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=None,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
        )

    def test_qqofficial_markup_triggers_alias_challenge(self):
        event = FakeEvent(
            platform="qq_official",
            message='<qqbot-at-user id="target-openid" /> 艾斯比 防守反击',
            messages=[At("qq_official", "机器人")],
            self_id="qq_official",
        )

        self.assertTrue(self.handler.is_alias_challenge_event(event))
        target = self.handler._target_identity_from_event(event)
        self.assertIsNotNone(target)
        self.assertEqual("target-openid", target.user_id)
        self.assertEqual(
            "防守反击",
            self.handler._extract_strategy(event, target, ""),
        )

    def test_qqofficial_target_at_misreported_as_self_id_still_triggers(self):
        target_id = "B2DDC6FFD2F562C68CC02CAD749EF622"
        event = FakeEvent(
            platform="qq_official",
            sender_id="BC729CFA021694764C24EF8C285DD78B",
            self_id=target_id,
            message="艾斯比",
            messages=[At(target_id, "")],
        )

        self.assertTrue(self.handler.is_alias_challenge_event(event))
        target = self.handler._target_identity_from_event(event)
        self.assertIsNotNone(target)
        self.assertEqual(target_id, target.user_id)
        self.assertFalse(self.handler._is_bot_target_id(event, target.user_id))

    def test_non_qqofficial_self_at_remains_a_bot_target(self):
        event = FakeEvent(
            platform="aiocqhttp",
            self_id="bot-1",
            message="艾斯比",
            messages=[At("bot-1", "机器人")],
        )

        self.assertFalse(self.handler.is_alias_challenge_event(event))
        self.assertTrue(self.handler._is_bot_target_id(event, "bot-1"))

    def test_qqofficial_markup_supports_bot_mentioned_command(self):
        event = FakeEvent(
            platform="qq_official",
            message=(
                '<qqbot-at-user id="qq_official" /> 挑战 '
                '<qqbot-at-user id="target-openid" /> 游走消耗'
            ),
            messages=[At("qq_official", "机器人")],
            self_id="qq_official",
        )

        self.assertEqual(
            ("挑战", "游走消耗"),
            self.handler.parse_mentioned_command(event),
        )
        target = self.handler._target_identity_from_event(event)
        self.assertIsNotNone(target)
        self.assertEqual("target-openid", target.user_id)

    def test_qqofficial_markup_supports_bot_mentioned_grant_command(self):
        event = FakeEvent(
            platform="qq_official",
            message=(
                '<qqbot-at-user id="qq_official" /> 给予 '
                '<qqbot-at-user id="target-openid" /> 1001'
            ),
            messages=[At("qq_official", "机器人")],
            self_id="qq_official",
        )

        self.assertEqual(
            ("给予", "1001"),
            self.handler.parse_mentioned_command(event),
        )
        target = self.handler._grant_target_identity(event)
        self.assertEqual("target-openid", target.user_id)


class EquipmentGrantCommandTests(unittest.IsolatedAsyncioTestCase):
    def _handler(self, *, user_service=None, equipment_service=None):
        return LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service or types.SimpleNamespace(),
            checkin_service=None,
            stat_service=None,
            battle_service=None,
            equipment_service=equipment_service or types.SimpleNamespace(),
        )

    async def test_non_admin_is_rejected_by_handler_recheck(self):
        equipment_service = types.SimpleNamespace(
            grant_catalog_item=mock.AsyncMock()
        )
        handler = self._handler(equipment_service=equipment_service)
        event = FakeEvent()
        event.is_admin = lambda: False

        replies = [
            reply async for reply in handler.grant_equipment(event, "本群 1001")
        ]

        self.assertEqual(["只有 AstrBot 管理员可以使用该指令。"], replies)
        equipment_service.grant_catalog_item.assert_not_awaited()

    async def test_inventory_shows_total_pages_and_safe_cleanup_entry(self):
        items = [
            types.SimpleNamespace(
                id=index,
                name=f"测试装备{index}",
                quality="common",
                item_level=1,
                material="iron",
                weight=1.0,
                is_locked=False,
            )
            for index in range(1, 13)
        ]
        equipment_service = types.SimpleNamespace(
            list_items=mock.AsyncMock(return_value=items),
            get_loadout=mock.AsyncMock(return_value=({}, [])),
        )
        handler = self._handler(equipment_service=equipment_service)
        handler._own_user = mock.AsyncMock(return_value=_user())
        handler.reply_text = mock.AsyncMock(
            side_effect=lambda event, text, *args: text
        )

        reply = [
            value async for value in handler.inventory(FakeEvent(), page=2)
        ][0]

        self.assertIn("共12件 · 第2/2页", reply)
        self.assertIn("No.11", reply)
        self.assertIn("/工坊 整理 支配", reply)

    async def test_equipment_catalog_is_paginated_and_filterable(self):
        from services.equipment_catalog import DEFAULT_EQUIPMENT_CATALOG

        equipment_service = types.SimpleNamespace(
            catalog=DEFAULT_EQUIPMENT_CATALOG
        )
        handler = self._handler(equipment_service=equipment_service)
        handler.reply_text = mock.AsyncMock(
            side_effect=lambda event, text, *args: text
        )

        first_page = [
            reply
            async for reply in handler.equipment_catalog(FakeEvent(), "1 武器")
        ][0]
        black_stars = [
            reply
            async for reply in handler.equipment_catalog(FakeEvent(), "黑星")
        ][0]

        self.assertIn("装备图鉴·武器 第1/", first_page)
        self.assertIn("3001 短剑 [主手] 普通", first_page)
        self.assertIn("装备图鉴·黑星 第1/2页 （共21件）", black_stars)
        self.assertIn("2001 珍贵的龟龟项链 [颈] 黑星", black_stars)

    async def test_equipment_catalog_rejects_invalid_page_and_category(self):
        from services.equipment_catalog import DEFAULT_EQUIPMENT_CATALOG

        handler = self._handler(
            equipment_service=types.SimpleNamespace(
                catalog=DEFAULT_EQUIPMENT_CATALOG
            )
        )
        handler.reply_text = mock.AsyncMock(
            side_effect=lambda event, text, *args: text
        )

        invalid_page = [
            reply
            async for reply in handler.equipment_catalog(FakeEvent(), "999 武器")
        ][0]
        invalid_category = [
            reply
            async for reply in handler.equipment_catalog(FakeEvent(), "坐骑")
        ][0]

        self.assertIn("页码应在", invalid_page)
        self.assertIn("未知分类", invalid_category)

    async def test_full_server_without_confirmation_only_previews(self):
        entry = types.SimpleNamespace(
            catalog_id=1001,
            template=types.SimpleNamespace(name="训练长剑"),
        )
        user_service = types.SimpleNamespace(
            list_user_pks=mock.AsyncMock(return_value=[1, 2, 3])
        )
        equipment_service = types.SimpleNamespace(
            catalog=types.SimpleNamespace(get=lambda catalog_id: entry),
            grant_catalog_item=mock.AsyncMock(),
        )
        handler = self._handler(
            user_service=user_service,
            equipment_service=equipment_service,
        )
        event = FakeEvent()
        event.is_admin = lambda: True

        replies = [
            reply async for reply in handler.grant_equipment(event, "全服 1001")
        ]

        self.assertIn("预计接收人数：3", replies[0])
        self.assertIn("/给予 全服 1001 确认", replies[0])
        equipment_service.grant_catalog_item.assert_not_awaited()

    async def test_qqofficial_aliased_at_is_used_as_single_target(self):
        target_id = "B2DDC6FFD2F562C68CC02CAD749EF622"
        user = types.SimpleNamespace(id=42)
        entry = types.SimpleNamespace(
            catalog_id=1001,
            template=types.SimpleNamespace(name="训练长剑"),
        )
        grant_result = types.SimpleNamespace(
            catalog_id=1001,
            equipment_name="训练长剑",
            granted=1,
            skipped=0,
        )
        user_service = types.SimpleNamespace(
            get_or_create_user=mock.AsyncMock(return_value=user)
        )
        equipment_service = types.SimpleNamespace(
            catalog=types.SimpleNamespace(get=lambda catalog_id: entry),
            grant_catalog_item=mock.AsyncMock(return_value=grant_result),
        )
        handler = self._handler(
            user_service=user_service,
            equipment_service=equipment_service,
        )
        event = FakeEvent(
            platform="qq_official",
            sender_id="sender-openid",
            self_id=target_id,
            message="/给予 1001",
            messages=[At(target_id, "")],
        )
        event.is_admin = lambda: True

        replies = [
            reply async for reply in handler.grant_equipment(event, "1001")
        ]

        identity = user_service.get_or_create_user.await_args.args[0]
        self.assertEqual(identity.user_id, target_id)
        equipment_service.grant_catalog_item.assert_awaited_once_with([42], 1001)
        self.assertIn("成功：1 人", replies[0])

    async def test_admin_can_explicitly_at_self_for_single_grant(self):
        sender_id = "BC729CFA021694764C24EF8C285DD78B"
        user = types.SimpleNamespace(id=42)
        entry = types.SimpleNamespace(
            catalog_id=2001,
            template=types.SimpleNamespace(name="珍贵的龟龟项链"),
        )
        result = types.SimpleNamespace(
            catalog_id=2001,
            equipment_name="珍贵的龟龟项链",
            granted=1,
            skipped=0,
        )
        user_service = types.SimpleNamespace(
            get_or_create_user=mock.AsyncMock(return_value=user)
        )
        equipment_service = types.SimpleNamespace(
            catalog=types.SimpleNamespace(get=lambda catalog_id: entry),
            grant_catalog_item=mock.AsyncMock(return_value=result),
        )
        handler = self._handler(
            user_service=user_service,
            equipment_service=equipment_service,
        )
        event = FakeEvent(
            platform="qq_official",
            sender_id=sender_id,
            self_id="qq_official",
            message=f"/给予 <@{sender_id}> 2001",
            messages=[At(sender_id, "")],
        )
        event.is_admin = lambda: True

        replies = [
            reply
            async for reply in handler.grant_equipment(
                event,
                f"<@{sender_id}> 2001",
            )
        ]

        identity = user_service.get_or_create_user.await_args.args[0]
        self.assertEqual(identity.user_id, sender_id)
        equipment_service.grant_catalog_item.assert_awaited_once_with([42], 2001)
        self.assertIn("成功：1 人", replies[0])

    async def test_full_server_confirmation_grants_to_every_role(self):
        entry = types.SimpleNamespace(
            catalog_id=1001,
            template=types.SimpleNamespace(name="训练长剑"),
        )
        result = types.SimpleNamespace(
            catalog_id=1001,
            equipment_name="训练长剑",
            granted=2,
            skipped=1,
        )
        user_service = types.SimpleNamespace(
            list_user_pks=mock.AsyncMock(return_value=[1, 2, 3])
        )
        equipment_service = types.SimpleNamespace(
            catalog=types.SimpleNamespace(get=lambda catalog_id: entry),
            grant_catalog_item=mock.AsyncMock(return_value=result),
        )
        handler = self._handler(
            user_service=user_service,
            equipment_service=equipment_service,
        )
        event = FakeEvent()
        event.is_admin = lambda: True

        replies = [
            reply
            async for reply in handler.grant_equipment(
                event,
                "全服 1001 确认",
            )
        ]

        equipment_service.grant_catalog_item.assert_awaited_once_with(
            [1, 2, 3],
            1001,
        )
        self.assertIn("成功：2 人", replies[0])
        self.assertIn("已有跳过：1 人", replies[0])

    async def test_equipment_detail_displays_persisted_description(self):
        item = types.SimpleNamespace(
            id=88,
            name="珍贵的龟龟项链",
            description="某个笨蛋丢失了大家最宝贵的东西",
            quality="legendary",
            star_type="black_star",
            item_level=1,
            material="emerald",
            blessing_state="normal",
            weight=0.1,
            enhancement_level=0,
            used_capacity=0,
            enchant_capacity=0,
            base_stats={},
            inherent_affixes=(
                {"type": "advanced_stat", "stat": "life_growth", "value": 10},
                {"type": "advanced_stat", "stat": "mana_growth", "value": 10},
                {"type": "advanced_stat", "stat": "speed", "value": 5},
                {"type": "advanced_stat", "stat": "luck", "value": 5},
            ),
            random_affixes=(),
            fusion_affixes=(),
        )
        equipment_service = types.SimpleNamespace(
            item_detail=mock.AsyncMock(return_value=item)
        )
        handler = self._handler(equipment_service=equipment_service)
        handler._own_user = mock.AsyncMock(
            return_value=types.SimpleNamespace(id=1, level=1)
        )
        handler.reply_text = mock.AsyncMock(
            side_effect=lambda event, text, *args: text
        )

        replies = [
            reply async for reply in handler.equipment_detail(FakeEvent(), 88)
        ]

        self.assertIn(
            "介绍：某个笨蛋丢失了大家最宝贵的东西",
            replies[0],
        )
        self.assertIn(
            "固有词条（原始）：生命成长+10、魔法成长+10、速度+5、幸运+5",
            replies[0],
        )
        self.assertIn(
            "固有词条（当前有效）：生命成长+10、魔法成长+10、速度+5、幸运+5",
            replies[0],
        )

    async def test_equipment_detail_displays_scaled_inherent_affixes(self):
        item = types.SimpleNamespace(
            id=89,
            name="测试黑星",
            description="",
            quality="legendary",
            star_type="black_star",
            item_level=40,
            material="iron",
            blessing_state="normal",
            weight=1.0,
            enhancement_level=0,
            used_capacity=0,
            enchant_capacity=0,
            base_stats={},
            inherent_affixes=(
                {
                    "type": "skill_level",
                    "skill_id": "longsword",
                    "value": 5,
                },
                {"type": "stat_flat", "stat": "strength", "value": 8},
                {"type": "block_rate", "value": 0.2},
                {
                    "type": "trigger_ability",
                    "ability_id": "time_stop",
                    "target": "enemy",
                    "value": 0.1,
                    "source_power": 200,
                },
            ),
            random_affixes=(),
            fusion_affixes=(),
            source_effects=("识破隐形", "防止物品被盗"),
        )
        equipment_service = types.SimpleNamespace(
            item_detail=mock.AsyncMock(return_value=item)
        )
        handler = self._handler(equipment_service=equipment_service)
        handler._own_user = mock.AsyncMock(
            return_value=types.SimpleNamespace(id=1, level=20)
        )
        handler.reply_text = mock.AsyncMock(
            side_effect=lambda event, text, *args: text
        )

        reply = [
            value
            async for value in handler.equipment_detail(FakeEvent(), 89)
        ][0]

        self.assertIn(
            "固有词条（原始）：技能等级+5、力量+8、格挡+0.2",
            reply,
        )
        self.assertIn(
            "固有词条（当前有效，角色Lv.20/需求Lv.40，数值比例50%）"
            "：技能等级+2、力量+4、格挡+0.1",
            reply,
        )
        self.assertIn("触发能力（原始）：时间停止 10.0%", reply)
        self.assertIn("触发能力（当前有效）：时间停止 5.0%", reply)
        self.assertIn("已生效效果：识破隐形", reply)
        self.assertIn("资料效果（当前未结算）：防止物品被盗", reply)


class WorkshopCommandUiTests(unittest.IsolatedAsyncioTestCase):
    def _handler(self, workshop_service, equipment_service=None):
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=types.SimpleNamespace(),
            checkin_service=None,
            stat_service=None,
            battle_service=None,
            equipment_service=equipment_service or types.SimpleNamespace(),
            workshop_service=workshop_service,
        )
        handler._registration_error = mock.AsyncMock(return_value="")
        handler._own_user = mock.AsyncMock(
            return_value=types.SimpleNamespace(
                id=1,
                group_id="group-1",
            )
        )
        handler.reply_text = mock.AsyncMock(
            side_effect=lambda event, text, *args: text
        )
        return handler

    async def test_dominated_preview_explains_keeper_and_exact_confirmation(self):
        reason = DominatedSalvageItem(
            equipment_id=11,
            equipment_name="旧短剑",
            quality="excellent",
            item_level=20,
            slot_label="主手",
            direction_labels=("灵巧",),
            keeper_id=19,
            keeper_name="迅捷短剑",
            keeper_quality="rare",
            keeper_level=25,
        )
        preview = BulkSalvagePreview(
            user_pk=1,
            quality="dominated",
            quality_label="同槽同方向被完全支配的装备",
            items=((11, "旧短剑", 20),),
            scrap_total=13,
            confirmation_token="a1b2c3d4e5",
            policy_id="dominated",
            dominated_items=(reason,),
        )
        workshop = types.SimpleNamespace(
            preview_bulk_salvage=mock.AsyncMock(return_value=preview)
        )
        handler = self._handler(workshop)

        reply = [
            item async for item in handler.workshop(FakeEvent(), "整理 支配")
        ][0]

        self.assertIn("安全整理预览：1件被完全支配装备", reply)
        self.assertIn("#11 优秀旧短剑 Lv.20 → 保留#19 精良迅捷短剑", reply)
        self.assertIn("〔主手/灵巧〕", reply)
        self.assertIn("已装备、收藏锁定", reply)
        self.assertIn("史诗/神话（传说）、白星/黑星", reply)
        self.assertIn("/工坊 批量分解 支配 a1b2c3d4e5", reply)

    async def test_excellent_preview_lists_every_id_and_safety_boundary(self):
        preview = BulkSalvagePreview(
            user_pk=1,
            quality="excellent",
            quality_label="未穿戴普通与优秀装备",
            items=(
                (11, "旧短剑", 20),
                (14, "旧斗篷", 22),
            ),
            scrap_total=27,
            confirmation_token="f1e2d3c4b5",
            policy_id="excellent",
        )
        workshop = types.SimpleNamespace(
            preview_bulk_salvage=mock.AsyncMock(return_value=preview)
        )
        handler = self._handler(workshop)

        reply = [
            item async for item in handler.workshop(FakeEvent(), "整理 优秀")
        ][0]

        workshop.preview_bulk_salvage.assert_awaited_once_with(1, "优秀")
        self.assertIn("优秀及以下整理预览：2件普通/优秀装备", reply)
        self.assertIn("将分解ID：#11、#14", reply)
        self.assertIn("预计碎片 +27", reply)
        self.assertIn("不比较装备数值或构筑", reply)
        self.assertIn("/工坊 收藏 ID", reply)
        self.assertIn("精良及以上、白星/黑星均受保护", reply)
        self.assertIn("重新核对完整装备快照", reply)
        self.assertIn("/工坊 批量分解 优秀 f1e2d3c4b5", reply)

    async def test_excellent_confirmation_uses_distinct_result_label(self):
        result = BulkSalvageResult(
            user_pk=1,
            quality="excellent",
            item_count=2,
            equipment_ids=(11, 14),
            scrap_gained=27,
            balance_after=35,
        )
        workshop = types.SimpleNamespace(
            bulk_salvage=mock.AsyncMock(return_value=result)
        )
        handler = self._handler(workshop)

        reply = [
            item
            async for item in handler.workshop(
                FakeEvent(),
                "批量分解 优秀 f1e2d3c4b5",
            )
        ][0]

        workshop.bulk_salvage.assert_awaited_once_with(
            1,
            "优秀",
            "f1e2d3c4b5",
        )
        self.assertIn("已批量分解 2 件普通/优秀装备", reply)
        self.assertIn("碎片 +27，当前 35", reply)

    async def test_collection_command_persists_protection(self):
        item = types.SimpleNamespace(id=77, name="纪念指环", is_locked=True)
        equipment = types.SimpleNamespace(
            set_item_locked=mock.AsyncMock(return_value=item)
        )
        handler = self._handler(types.SimpleNamespace(), equipment)

        reply = [
            value async for value in handler.workshop(FakeEvent(), "收藏 77")
        ][0]

        equipment.set_item_locked.assert_awaited_once_with(1, 77, True)
        self.assertIn("已收藏锁定 #77", reply)
        self.assertIn("单件分解都会跳过", reply)


class QQOfficialChallengeDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_misreported_self_id_reaches_battle_service(self):
        target_id = "B2DDC6FFD2F562C68CC02CAD749EF622"
        battle_result = object()
        user_service = types.SimpleNamespace(
            has_registered_nickname=mock.AsyncMock(return_value=True),
        )
        battle_service = types.SimpleNamespace(
            battle=mock.AsyncMock(return_value=battle_result),
        )
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=battle_service,
        )
        handler._battle_result = mock.AsyncMock(return_value="战斗已触发")
        event = FakeEvent(
            platform="qq_official",
            sender_id="BC729CFA021694764C24EF8C285DD78B",
            self_id=target_id,
            message="艾斯比",
            messages=[At(target_id, "")],
        )

        replies = [reply async for reply in handler.challenge(event)]

        self.assertEqual(["战斗已触发"], replies)
        battle_service.battle.assert_awaited_once()
        self.assertEqual(target_id, battle_service.battle.await_args.args[1].user_id)

class LearnSkillRangeTests(unittest.IsolatedAsyncioTestCase):
    """Range learning should skip already-learned skills instead of failing."""

    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        await init_db(self.db_path)
        self.users = UserService(self.db_path)
        self.skills = SkillService(self.db_path)
        self.identity = UserIdentity(
            platform="test", group_id="group-1",
            user_id="user-1", nickname="测试用户",
        )
        self.user = await self.users.get_or_create_user(self.identity)
        await self.users.register_nickname(self.identity, "测试用户")
        async with await connect_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET skill_points = 20 WHERE id = ?",
                (self.user.id,),
            )
            await db.commit()
        self.handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=self.users,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
            skill_service=self.skills,
        )

    async def asyncTearDown(self):
        os.remove(self.db_path)

    async def test_range_learning_skips_already_learned_skills(self):
        replies = [
            reply
            async for reply in self.handler.learn_skill(
                FakeEvent(), "斧头专精-法杖专精"
            )
        ]
        self.assertEqual(len(replies), 1)
        self.assertIn("已学习技能", replies[0])
        self.assertIn("斧头专精", replies[0])
        self.assertIn("法杖专精", replies[0])
        self.assertIn("已跳过已学会技能", replies[0])
        self.assertIn("战术", replies[0])
        self.assertIn("举重", replies[0])
        skills, _ = await self.skills.get_skills(self.user)
        self.assertIn("axe", skills)
        self.assertIn("staff", skills)
        self.assertEqual(skills["axe"].level, 1)

    async def test_single_already_learned_skill_still_errors(self):
        replies = [
            reply
            async for reply in self.handler.learn_skill(
                FakeEvent(), "战术"
            )
        ]
        self.assertEqual(len(replies), 1)
        self.assertIn("学习失败", replies[0])
        self.assertIn("战术", replies[0])
        self.assertIn("已经学会", replies[0])

    async def test_all_learned_range_reports_error(self):
        replies1 = [
            reply
            async for reply in self.handler.learn_skill(
                FakeEvent(), "斧头专精-镰刀专精"
            )
        ]
        self.assertIn("已学习技能", replies1[0])
        # Now every skill in that range is learned; re-learning should fail.
        replies2 = [
            reply
            async for reply in self.handler.learn_skill(
                FakeEvent(), "斧头专精-战术"
            )
        ]
        self.assertEqual(len(replies2), 1)
        self.assertIn("学习失败", replies2[0])
        self.assertIn("已经学会", replies2[0])


class SpellBookCommandHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _plain_reply(event, text, title="LevelUpPvp"):
        return event.plain_result(text)

    def _handler(self, spell_service):
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=types.SimpleNamespace(
                get_or_create_user=mock.AsyncMock(return_value=_user()),
                has_registered_nickname=mock.AsyncMock(return_value=True),
                register_nickname=mock.AsyncMock(),
            ),
            checkin_service=None,
            stat_service=None,
            battle_service=None,
            spell_service=spell_service,
        )
        handler.reply_text = self._plain_reply
        return handler

    async def test_spellbook_page_is_grouped_and_shows_collection_actions(self):
        item1 = SpellBookItem(11, 1, "magic_arrow", 2, "chat", 101, True)
        item2 = SpellBookItem(19, 1, "magic_arrow", 1, "dungeon", 202, True)
        entry = SpellBookCollectionEntry(
            spell_id="magic_arrow",
            spell_name="魔法箭",
            school_id="magic_training",
            items=(item1, item2),
            quantity=3,
            learned_spell=UserSpell("magic_arrow", 4, 12, 180),
            success_chance=0.735,
            reading_power=355,
            reading_difficulty=120,
            reading_attribute="magic",
            study_progress=0.20,
            studied_today=False,
            school_level=8,
        )
        library = SpellBookLibrary(
            entries=(entry,),
            learned_count=1,
            total_spell_count=84,
            research_pages=15,
            craft_options=(
                SpellResearchCraftOption(
                    "armor_spell", "护甲术", "barrier", 12, True
                ),
            ),
        )
        service = types.SimpleNamespace(
            get_book_library=mock.AsyncMock(return_value=library)
        )
        handler = self._handler(service)

        replies = [
            reply async for reply in handler.spellbooks(FakeEvent(), page=99)
        ]

        self.assertEqual(len(replies), 1)
        text = replies[0]
        self.assertIn("第1/1页", text)
        self.assertIn("法术图鉴 1/84", text)
        self.assertIn("持有 3本/1种", text)
        self.assertIn("咒文残页 15张", text)
        self.assertIn("《魔法箭》×3 [已学Lv.4·潜力180%]", text)
        self.assertIn("#11×2、#19", text)
        self.assertIn("成功率73.5%", text)
        self.assertIn("研读进度20%", text)
        self.assertIn("默认最老#11", text)
        self.assertIn("护甲术(12)", text)
        self.assertIn("/研制 法术名", text)

    async def test_read_by_name_reports_max_potential_conversion(self):
        result = SpellReadResult(
            spell=UserSpell("magic_arrow", 8, 0, 400),
            success=True,
            chance=1.0,
            random_seed=777,
            consumed=1,
            outcome="research_converted",
            research_pages_gain=3,
            research_pages_balance=18,
        )
        service = types.SimpleNamespace(
            read_book=mock.AsyncMock(return_value=result)
        )
        handler = self._handler(service)

        replies = [
            reply
            async for reply in handler.read_spellbook(FakeEvent(), "魔法箭")
        ]

        service.read_book.assert_awaited_once()
        self.assertEqual(service.read_book.await_args.args[1], "魔法箭")
        self.assertIn("重复书已化为3张咒文残页", replies[0])
        self.assertIn("当前残页：18张", replies[0])

    async def test_repeat_read_reports_potential_and_annotated_page(self):
        result = SpellReadResult(
            spell=UserSpell("magic_arrow", 3, 0, 118),
            success=True,
            chance=0.8,
            random_seed=778,
            consumed=1,
            potential_gain=18,
            reading_power=300,
            reading_difficulty=120,
            reading_attribute="magic",
            outcome="potential_restored",
            research_pages_gain=1,
            research_pages_balance=7,
        )
        service = types.SimpleNamespace(
            read_book=mock.AsyncMock(return_value=result)
        )
        handler = self._handler(service)

        reply = [
            value
            async for value in handler.read_spellbook(FakeEvent(), "魔法箭")
        ][0]

        self.assertIn("潜力恢复至118%（+18%）", reply)
        self.assertIn("同时抄录了1张咒文残页", reply)
        self.assertIn("当前共7张", reply)

    async def test_targeted_craft_returns_actionable_book_id(self):
        item = SpellBookItem(
            23, 1, "armor_spell", 1, "spell_research", 909, True
        )
        service = types.SimpleNamespace(
            craft_book=mock.AsyncMock(
                return_value=SpellBookCraftResult(item, "护甲术", 12, 4)
            )
        )
        handler = self._handler(service)

        replies = [
            reply
            async for reply in handler.craft_spellbook(FakeEvent(), "护甲术")
        ]

        self.assertIn("《护甲术》#23", replies[0])
        self.assertIn("消耗12张", replies[0])
        self.assertIn("/阅读 23", replies[0])
