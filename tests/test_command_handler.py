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
from models.user import CheckinResult, User, UserIdentity
from services.db import connect_db, init_db
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

    async def test_auto_checkin_replies_once_then_stays_silent(self):
        handler = self._handler(_result(), _result(already_checked=True))
        event = FakeEvent(messages=[object()])

        first = [item async for item in handler.auto_checkin(event)]
        second = [item async for item in handler.auto_checkin(event)]

        self.assertEqual(len(first), 1)
        self.assertIn("签到成功", first[0])
        self.assertEqual(second, [])

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
        self.assertIn("等级：Lv.1 经验：20/100", replies[0])


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
            source_effects=("识破隐形",),
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
        self.assertIn("资料效果（当前未结算）：识破隐形", reply)


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
