import os

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from .handles.command_handler import LevelUpPvpCommandHandler
    from .services.battle_service import BattleService
    from .services.checkin_service import CheckinService
    from .services.db import init_db
    from .services.llm_service import LLMService
    from .services.stat_service import StatService
    from .services.user_service import UserService
except ImportError:
    from handles.command_handler import LevelUpPvpCommandHandler
    from services.battle_service import BattleService
    from services.checkin_service import CheckinService
    from services.db import init_db
    from services.llm_service import LLMService
    from services.stat_service import StatService
    from services.user_service import UserService


PLUGIN_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(PLUGIN_DIR, "data", "db_level_up_pvp.db")


@register("astrbot_plugin_LevelUpPvp", "QuanWenG", "升级就开打", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        user_service = UserService(DB_PATH)
        checkin_service = CheckinService(DB_PATH, user_service)
        stat_service = StatService(DB_PATH, user_service)
        llm_service = LLMService()
        battle_service = BattleService(DB_PATH, user_service, llm_service)
        self.command_handler = LevelUpPvpCommandHandler(
            context=context,
            user_service=user_service,
            checkin_service=checkin_service,
            stat_service=stat_service,
            battle_service=battle_service,
        )

    async def initialize(self):
        """初始化插件数据库。"""
        await init_db(DB_PATH)

    @filter.command("签到")
    async def sign(self, event: AstrMessageEvent):
        """每日签到获取随机经验。"""
        async for result in self.command_handler.sign(event):
            yield result

    @filter.command("面板")
    async def profile(self, event: AstrMessageEvent):
        """查看自己的等级、经验和属性。"""
        async for result in self.command_handler.profile(event):
            yield result

    @filter.command("加点")
    async def add_point(
        self,
        event: AstrMessageEvent,
        stat_name: str = "",
        amount: int = 1,
    ):
        """消耗自定义属性点，按属性随机范围提升属性。"""
        async for result in self.command_handler.add_point(event, stat_name, amount):
            yield result

    @filter.command("挑战")
    async def challenge(self, event: AstrMessageEvent):
        """At 一名用户发起概率战斗。"""
        async for result in self.command_handler.challenge(event):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def alias_challenge(self, event: AstrMessageEvent):
        """包含 At 和“艾斯比”的普通消息视为挑战。"""
        if not self.command_handler.is_alias_challenge_event(event):
            return
        async for result in self.command_handler.challenge(event):
            yield result
        event.stop_event()

    async def terminate(self):
        """插件卸载时无需额外清理。"""
