import sys
import types
import unittest


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
        message="",
        messages=None,
    ):
        self.group_id = group_id
        self.sender_id = sender_id
        self.self_id = self_id
        self.message = message
        self.messages = messages or []

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
        return "test"

    def get_session_id(self):
        return self.group_id

    def get_sender_name(self):
        return "测试用户"

    def plain_result(self, text):
        return text


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
