import sys
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
from models.user import CheckinResult, User


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
