import asyncio
import os

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr

try:
    from .handles.command_handler import LevelUpPvpCommandHandler
    from .models.user import UserIdentity
    from .services.attribute_service import AttributeService
    from .services.battle_service import BattleService
    from .services.build_service import CombatBuildService
    from .services.challenge_queue import ChallengeQueueService
    from .services.checkin_service import CheckinService
    from .services.db import init_db
    from .services.equipment_service import EquipmentService
    from .services.external_activity_service import ExternalActivityService
    from .services.llm_service import LLMService
    from .services.skill_service import SkillService
    from .services.spell_service import SpellService
    from .services.stat_service import StatService
    from .services.storage import prepare_persistent_database
    from .services.user_service import UserService
except ImportError:
    from handles.command_handler import LevelUpPvpCommandHandler
    from models.user import UserIdentity
    from services.attribute_service import AttributeService
    from services.battle_service import BattleService
    from services.build_service import CombatBuildService
    from services.challenge_queue import ChallengeQueueService
    from services.checkin_service import CheckinService
    from services.db import init_db
    from services.equipment_service import EquipmentService
    from services.external_activity_service import ExternalActivityService
    from services.llm_service import LLMService
    from services.skill_service import SkillService
    from services.spell_service import SpellService
    from services.stat_service import StatService
    from services.storage import prepare_persistent_database
    from services.user_service import UserService


PLUGIN_DIR = os.path.dirname(__file__)
PLUGIN_NAME = "astrbot_plugin_LevelUpPvp"
LEGACY_DB_PATH = os.path.join(PLUGIN_DIR, "data", "db_level_up_pvp.db")


@register(PLUGIN_NAME, "QuanWenG", "群聊自动签到，升级就开打", "1.7.2")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.db_path = prepare_persistent_database(
            StarTools.get_data_dir(PLUGIN_NAME),
            LEGACY_DB_PATH,
        )
        self._db_init_task = asyncio.create_task(
            init_db(self.db_path),
            name=f"{PLUGIN_NAME}-database-init",
        )
        user_service = UserService(self.db_path)
        attribute_service = AttributeService(self.db_path)
        self.external_activity_service = ExternalActivityService(
            self.db_path,
            user_service,
            attribute_service,
        )
        checkin_service = CheckinService(
            self.db_path, user_service, attribute_service
        )
        stat_service = StatService(self.db_path, user_service)
        llm_service = LLMService()
        equipment_service = EquipmentService(self.db_path)
        skill_service = SkillService(self.db_path)
        spell_service = SpellService(
            self.db_path, skill_service, equipment_service, attribute_service
        )
        build_service = CombatBuildService(
            equipment_service, skill_service, attribute_service, spell_service
        )
        battle_service = BattleService(
            self.db_path, user_service, llm_service, equipment_service, skill_service,
            attribute_service, spell_service
        )
        self.challenge_queue = ChallengeQueueService(battle_service)
        self.command_handler = LevelUpPvpCommandHandler(
            context=context,
            user_service=user_service,
            checkin_service=checkin_service,
            stat_service=stat_service,
            battle_service=battle_service,
            challenge_queue=self.challenge_queue,
            equipment_service=equipment_service,
            skill_service=skill_service,
            build_service=build_service,
            attribute_service=attribute_service,
            spell_service=spell_service,
        )

    async def initialize(self):
        """初始化插件数据库。"""
        await self._ensure_database_ready()
        self.challenge_queue.start()

    async def _ensure_database_ready(self) -> None:
        """Block every event until schema creation and migrations finish."""
        await self._db_init_task

    async def grant_external_activity(
        self,
        *,
        platform: str,
        group_id: str,
        user_id: str,
        nickname: str,
        source: str,
        reward_key: str,
        valid_attempt: bool,
        correct: bool,
    ) -> dict:
        """Grant an idempotent reward requested by another AstrBot plugin.

        Args:
            platform: AstrBot platform adapter identifier.
            group_id: Group identifier, or an empty string in private chat.
            user_id: Stable platform user identifier.
            nickname: Current display name used when creating a new player.
            source: Stable caller plugin identifier.
            reward_key: Caller-scoped idempotency key.
            valid_attempt: Whether to grant the attempt component.
            correct: Whether to grant the correct-answer component.

        Returns:
            Concrete applied components and experience gains.
        """
        await self._ensure_database_ready()
        identity = UserIdentity(
            platform=str(platform),
            group_id=str(group_id or ""),
            user_id=str(user_id),
            nickname=str(nickname or user_id),
        )
        return await self.external_activity_service.grant(
            identity=identity,
            source=source,
            reward_key=reward_key,
            valid_attempt=valid_attempt,
            correct=correct,
        )

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=100,
    )
    async def auto_checkin(self, event: AstrMessageEvent):
        """自动登记群成员，并处理签到。"""
        await self._ensure_database_ready()
        await self.command_handler.ensure_sender_registered(event)
        if self.command_handler.is_explicit_checkin_event(event):
            async for result in self.command_handler.sign(event):
                yield result
            event.stop_event()
            return

        async for result in self.command_handler.auto_checkin(event):
            yield result

    @filter.command("签到")
    async def sign(self, event: AstrMessageEvent):
        """每日签到获取随机经验。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.sign(event):
            yield result

    @filter.command("面板")
    async def profile(self, event: AstrMessageEvent):
        """查看自己的等级、经验和属性。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.profile(event):
            yield result

    @filter.command("加点")
    async def add_point(
        self,
        event: AstrMessageEvent,
        stat_name: str = "",
        amount: int = 1,
    ):
        """消耗自定义属性点，按 1:1 固定提升主属性。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.add_point(event, stat_name, amount):
            yield result

    @filter.command("排行")
    async def ranking(self, event: AstrMessageEvent):
        """查看当前群等级排行榜，At 用户时查看该用户排名。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.ranking(event):
            yield result

    @filter.command("登记")
    async def register_nickname(self, event: AstrMessageEvent, nickname: str = ""):
        """使用平台用户名登记展示昵称，可选手动覆盖。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.register_nickname(event, nickname):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("修改登记")
    async def modify_registered_nickname(
        self,
        event: AstrMessageEvent,
        nickname: GreedyStr,
    ):
        """管理员修改指定用户的展示昵称。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.modify_registered_nickname(
            event,
            nickname,
        ):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("给予")
    async def grant_equipment(self, event: AstrMessageEvent, args: GreedyStr):
        """向单人、本群或全服发放装备表中的一件装备。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.grant_equipment(event, args):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重载装备表")
    async def reload_equipment_catalog(self, event: AstrMessageEvent):
        """校验并原子重载装备目录。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.reload_equipment_catalog(event):
            yield result

    @filter.command("背包")
    async def inventory(self, event: AstrMessageEvent, page: int = 1):
        await self._ensure_database_ready()
        async for result in self.command_handler.inventory(event, page): yield result

    @filter.command("装备")
    async def equipment(self, event: AstrMessageEvent):
        await self._ensure_database_ready()
        async for result in self.command_handler.equipment(event): yield result

    @filter.command("装备图鉴")
    async def equipment_catalog(
        self,
        event: AstrMessageEvent,
        args: GreedyStr = "",
    ):
        await self._ensure_database_ready()
        async for result in self.command_handler.equipment_catalog(event, args):
            yield result

    @filter.command("装备详情")
    async def equipment_detail(self, event: AstrMessageEvent, equipment_id: int):
        await self._ensure_database_ready()
        async for result in self.command_handler.equipment_detail(event, equipment_id): yield result

    @filter.command("穿戴")
    async def equip_item(self, event: AstrMessageEvent, equipment_id: int, slot: str = ""):
        await self._ensure_database_ready()
        async for result in self.command_handler.equip_item(event, equipment_id, slot): yield result

    @filter.command("卸下")
    async def unequip_item(self, event: AstrMessageEvent, slot: str):
        await self._ensure_database_ready()
        async for result in self.command_handler.unequip_item(event, slot): yield result

    @filter.command("技能")
    async def skills(self, event: AstrMessageEvent):
        await self._ensure_database_ready()
        async for result in self.command_handler.skills(event): yield result

    @filter.command("学习")
    async def learn_skill(self, event: AstrMessageEvent, name: GreedyStr):
        await self._ensure_database_ready()
        async for result in self.command_handler.learn_skill(event, name): yield result

    @filter.command("训练技能")
    async def train_skill(self, event: AstrMessageEvent, name: str, points: int = 1):
        await self._ensure_database_ready()
        async for result in self.command_handler.train_skill(event, name, points): yield result

    @filter.command("技能栏")
    async def skill_slot(self, event: AstrMessageEvent, slot: int, name: GreedyStr):
        await self._ensure_database_ready()
        async for result in self.command_handler.set_skill_slot(event, slot, name): yield result
    @filter.command("魔法书")
    async def spellbooks(self, event: AstrMessageEvent, page: int = 1):
        await self._ensure_database_ready()
        async for result in self.command_handler.spellbooks(event, page): yield result

    @filter.command("阅读")
    async def read_spellbook(self, event: AstrMessageEvent, book_id: int):
        await self._ensure_database_ready()
        async for result in self.command_handler.read_spellbook(event, book_id): yield result

    @filter.command("法术")
    async def spells(self, event: AstrMessageEvent):
        await self._ensure_database_ready()
        async for result in self.command_handler.spells(event): yield result

    @filter.command("战技")
    async def techniques(self, event: AstrMessageEvent):
        await self._ensure_database_ready()
        async for result in self.command_handler.techniques(event): yield result
    @filter.command("挑战")
    async def challenge(self, event: AstrMessageEvent):
        """At 一名用户发起概率战斗。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.challenge(event):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def mentioned_command(self, event: AstrMessageEvent):
        """普通消息里的 @机器人指令 或挑战唤起词。"""
        await self._ensure_database_ready()
        mentioned_command = self.command_handler.parse_mentioned_command(event)
        if mentioned_command:
            command, args = mentioned_command
            if command == "签到":
                async for result in self.command_handler.sign(event):
                    yield result
            elif command == "面板":
                async for result in self.command_handler.profile(event):
                    yield result
            elif command == "加点":
                parsed_args = self.command_handler.parse_add_point_args(args)
                if not parsed_args:
                    yield event.plain_result("用法：/加点 力量 2")
                else:
                    stat_name, amount = parsed_args
                    async for result in self.command_handler.add_point(
                        event,
                        stat_name,
                        amount,
                    ):
                        yield result
            elif command == "排行":
                async for result in self.command_handler.ranking(event):
                    yield result
            elif command == "登记":
                async for result in self.command_handler.register_nickname(event, args):
                    yield result
            elif command == "修改登记":
                async for result in self.command_handler.modify_registered_nickname(
                    event,
                    args,
                ):
                    yield result
            elif command == "给予":
                async for result in self.command_handler.grant_equipment(event, args):
                    yield result
            elif command == "重载装备表":
                async for result in self.command_handler.reload_equipment_catalog(event):
                    yield result
            elif command == "背包":
                page = int(args) if args.strip().isdigit() else 1
                async for result in self.command_handler.inventory(event, page): yield result
            elif command == "装备":
                async for result in self.command_handler.equipment(event): yield result
            elif command == "装备图鉴":
                async for result in self.command_handler.equipment_catalog(event, args): yield result
            elif command == "装备详情":
                if args.strip().isdigit():
                    async for result in self.command_handler.equipment_detail(event, int(args.strip())): yield result
                else: yield event.plain_result("用法：/装备详情 装备ID")
            elif command == "穿戴":
                parts = args.split()
                if parts and parts[0].isdigit():
                    async for result in self.command_handler.equip_item(event, int(parts[0]), parts[1] if len(parts) > 1 else ""): yield result
                else: yield event.plain_result("用法：/穿戴 装备ID [槽位]")
            elif command == "卸下":
                async for result in self.command_handler.unequip_item(event, args.strip()): yield result
            elif command == "技能":
                async for result in self.command_handler.skills(event): yield result
            elif command == "学习":
                async for result in self.command_handler.learn_skill(event, args.strip()): yield result
            elif command == "训练技能":
                parts = args.split()
                points = int(parts[-1]) if parts and parts[-1].isdigit() else 1
                name = " ".join(parts[:-1]) if parts and parts[-1].isdigit() else args.strip()
                async for result in self.command_handler.train_skill(event, name, points): yield result
            elif command == "技能栏":
                parts = args.split(maxsplit=1)
                if len(parts) == 2 and parts[0].isdigit():
                    async for result in self.command_handler.set_skill_slot(event, int(parts[0]), parts[1]): yield result
                else: yield event.plain_result("用法：/技能栏 位置 技能名|清空")
            elif command == "魔法书":
                page = int(args) if args.strip().isdigit() else 1
                async for result in self.command_handler.spellbooks(event, page): yield result
            elif command == "阅读":
                if args.strip().isdigit():
                    async for result in self.command_handler.read_spellbook(event, int(args.strip())): yield result
                else: yield event.plain_result("用法：/阅读 魔法书ID")
            elif command == "法术":
                async for result in self.command_handler.spells(event): yield result
            elif command == "战技":
                async for result in self.command_handler.techniques(event): yield result
            elif command == "挑战":
                async for result in self.command_handler.challenge(event):
                    yield result
            event.stop_event()
            return

        if not self.command_handler.is_alias_challenge_event(event):
            return
        async for result in self.command_handler.challenge(event):
            yield result
        event.stop_event()

    async def terminate(self):
        """插件卸载时无需额外清理。"""
        await self.challenge_queue.shutdown()
