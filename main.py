import asyncio
import os
import sys

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr


PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# AstrBot may load a plugin's main.py as a top-level module instead of as a
# package. In that mode relative imports are unavailable, so make this plugin
# directory the fallback import root for its local packages.
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

if __package__:
    from .handles.command_handler import LevelUpPvpCommandHandler
    from .models.user import UserIdentity
    from .services.attribute_service import AttributeService
    from .services.auto_equip_service import AutoEquipService
    from .services.auto_pilot_service import AutoPilotService
    from .services.battle_service import BattleService
    from .services.build_service import CombatBuildService
    from .services.challenge_queue import ChallengeQueueService
    from .services.checkin_service import CheckinService
    from .services.chat_activity_service import (
        ChatActivityService,
        ChatActivitySettlementService,
        EquipmentServiceDropAdapter,
        SpellServiceBookAdapter,
    )
    from .services.db import init_db
    from .services.dungeon_catalog import DungeonCatalog
    from .services.dungeon_service import DungeonService
    from .services.equipment_service import EquipmentService
    from .services.effect_whitelist import (
        EffectWhitelist,
        effect_whitelist_only,
        external_effect_whitelist_only,
        should_stop_denied_llm,
    )
    from .services.external_activity_service import ExternalActivityService
    from .services.llm_service import LLMService
    from .services.monster_catalog import MonsterCatalog
    from .services.monster_build_service import MonsterBuildService
    from .services.operation_service import OperationService
    from .services.operation_settlement_service import OperationSettlementService
    from .services.replay_service import ReplayService
    from .services.workshop_service import WorkshopService
    from .services.skill_service import SkillService
    from .services.spell_service import SpellService
    from .services.stat_service import StatService
    from .services.storage import prepare_persistent_database
    from .services.user_service import UserService
else:
    from handles.command_handler import LevelUpPvpCommandHandler
    from models.user import UserIdentity
    from services.attribute_service import AttributeService
    from services.auto_equip_service import AutoEquipService
    from services.auto_pilot_service import AutoPilotService
    from services.battle_service import BattleService
    from services.build_service import CombatBuildService
    from services.challenge_queue import ChallengeQueueService
    from services.checkin_service import CheckinService
    from services.chat_activity_service import (
        ChatActivityService,
        ChatActivitySettlementService,
        EquipmentServiceDropAdapter,
        SpellServiceBookAdapter,
    )
    from services.db import init_db
    from services.dungeon_catalog import DungeonCatalog
    from services.dungeon_service import DungeonService
    from services.equipment_service import EquipmentService
    from services.effect_whitelist import (
        EffectWhitelist,
        effect_whitelist_only,
        external_effect_whitelist_only,
        should_stop_denied_llm,
    )
    from services.external_activity_service import ExternalActivityService
    from services.llm_service import LLMService
    from services.monster_catalog import MonsterCatalog
    from services.monster_build_service import MonsterBuildService
    from services.operation_service import OperationService
    from services.operation_settlement_service import OperationSettlementService
    from services.replay_service import ReplayService
    from services.workshop_service import WorkshopService
    from services.skill_service import SkillService
    from services.spell_service import SpellService
    from services.stat_service import StatService
    from services.storage import prepare_persistent_database
    from services.user_service import UserService
PLUGIN_NAME = "astrbot_plugin_LevelUpPvp"
LEGACY_DB_PATH = os.path.join(PLUGIN_DIR, "data", "db_level_up_pvp.db")


@register(PLUGIN_NAME, "QuanWenG", "聊天遇奇物，奈菲亚里验证构筑", "2.0.0")
class MyPlugin(Star):
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ):
        super().__init__(context, config)
        self.config = config or {}
        self.effect_whitelist = EffectWhitelist(
            self.config.get("effect_whitelist", [])
        )
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
        chat_activity_service = ChatActivityService(self.db_path)
        chat_activity_settlement_service = ChatActivitySettlementService(
            self.db_path,
            user_service,
            equipment_port=EquipmentServiceDropAdapter(equipment_service),
            spellbook_port=SpellServiceBookAdapter(spell_service),
        )
        self.chat_activity_service = chat_activity_service
        self.chat_activity_settlement_service = chat_activity_settlement_service
        build_service = CombatBuildService(
            equipment_service, skill_service, attribute_service, spell_service
        )
        auto_equip_service = AutoEquipService(build_service)
        operation_service = OperationService(self.db_path)
        battle_service = BattleService(
            self.db_path, user_service, llm_service, equipment_service, skill_service,
            attribute_service, spell_service, operation_service
        )
        self.challenge_queue = ChallengeQueueService(battle_service)
        monster_catalog = MonsterCatalog()
        monster_build_service = MonsterBuildService(monster_catalog, attribute_service)
        dungeon_catalog = DungeonCatalog(monster_catalog=monster_catalog)
        dungeon_service = DungeonService(
            self.db_path,
            user_service,
            build_service,
            monster_build_service,
            equipment_service,
            skill_service,
            attribute_service,
            spell_service,
            battle_service.combat_engine,
            battle_service.combat_state_service,
            dungeon_catalog,
        )
        operation_settlement_service = OperationSettlementService(
            self.db_path,
            user_service,
            equipment_service,
        )
        self.auto_pilot_service = AutoPilotService(
            db_path=self.db_path,
            effect_whitelist=self.effect_whitelist,
            user_service=user_service,
            stat_service=stat_service,
            attribute_service=attribute_service,
            skill_service=skill_service,
            spell_service=spell_service,
            equipment_service=equipment_service,
            auto_equip_service=auto_equip_service,
            dungeon_service=dungeon_service,
            operation_service=operation_service,
            operation_settlement_service=operation_settlement_service,
        )
        workshop_service = WorkshopService(self.db_path, equipment_service)
        replay_service = ReplayService(self.db_path)
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
            auto_equip_service=auto_equip_service,
            auto_pilot_service=self.auto_pilot_service,
            attribute_service=attribute_service,
            spell_service=spell_service,
            dungeon_service=dungeon_service,
            operation_service=operation_service,
            operation_settlement_service=operation_settlement_service,
            workshop_service=workshop_service,
            replay_service=replay_service,
            chat_activity_service=chat_activity_service,
            chat_activity_settlement_service=chat_activity_settlement_service,
        )

    async def initialize(self):
        """初始化插件数据库。"""
        await self._ensure_database_ready()
        await self.chat_activity_service.recover_pending_intents(
            self.chat_activity_settlement_service,
            batch_size=100,
            on_error=lambda _intent, _exc: logger.exception(
                "LevelUpPvp chat reward recovery failed"
            ),
        )
        self.challenge_queue.start()
        self.auto_pilot_service.start()

    async def _ensure_database_ready(self) -> None:
        """Block every event until schema creation and migrations finish."""
        await self._db_init_task

    @external_effect_whitelist_only
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
        unified_msg_origin: str = "",
    ) -> dict | None:
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
            unified_msg_origin: Optional full AstrBot message origin. Older
                callers may omit it and match by group ID only.

        Returns:
            Concrete applied components and experience gains, or ``None`` when
            the origin is outside the effect whitelist.
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

    @filter.on_waiting_llm_request(priority=1000)
    async def stop_denied_levelup_llm(self, event: AstrMessageEvent) -> None:
        """Prevent denied LevelUpPVP commands from reaching default LLM."""
        if should_stop_denied_llm(
            whitelist=self.effect_whitelist,
            event=event,
        ):
            event.stop_event()

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=100,
    )
    @effect_whitelist_only
    async def auto_checkin(self, event: AstrMessageEvent):
        """自动登记群成员，并处理签到。"""
        await self._ensure_database_ready()
        await self.command_handler.ensure_sender_registered(event)
        if self.command_handler.is_explicit_checkin_event(event):
            async for result in self.command_handler.sign(event):
                yield result
            event.stop_event()
            return

        async for result in self.command_handler.ambient_activity(event):
            yield result

    @filter.command("签到")
    @effect_whitelist_only
    async def sign(self, event: AstrMessageEvent):
        """每日签到获取随机经验。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.sign(event):
            yield result

    @filter.command("面板")
    @effect_whitelist_only
    async def profile(self, event: AstrMessageEvent):
        """查看自己的等级、经验和属性。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.profile(event):
            yield result

    @filter.command("加点")
    @effect_whitelist_only
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
    @effect_whitelist_only
    async def ranking(self, event: AstrMessageEvent):
        """查看当前群等级排行榜，At 用户时查看该用户排名。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.ranking(event):
            yield result

    @filter.command("登记")
    @effect_whitelist_only
    async def register_nickname(self, event: AstrMessageEvent, nickname: str = ""):
        """使用平台用户名登记展示昵称，可选手动覆盖。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.register_nickname(event, nickname):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("修改登记")
    @effect_whitelist_only
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
    @effect_whitelist_only
    async def grant_equipment(self, event: AstrMessageEvent, args: GreedyStr):
        """向单人、本群或全服发放装备表中的一件装备。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.grant_equipment(event, args):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重载装备表")
    @effect_whitelist_only
    async def reload_equipment_catalog(self, event: AstrMessageEvent):
        """校验并原子重载装备目录。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.reload_equipment_catalog(event):
            yield result

    @filter.command("背包")
    @effect_whitelist_only
    async def inventory(self, event: AstrMessageEvent, page: int = 1):
        await self._ensure_database_ready()
        async for result in self.command_handler.inventory(event, page): yield result

    @filter.command("装备")
    @effect_whitelist_only
    async def equipment(self, event: AstrMessageEvent):
        await self._ensure_database_ready()
        async for result in self.command_handler.equipment(event): yield result

    @filter.command("装备图鉴")
    @effect_whitelist_only
    async def equipment_catalog(
        self,
        event: AstrMessageEvent,
        args: GreedyStr = "",
    ):
        await self._ensure_database_ready()
        async for result in self.command_handler.equipment_catalog(event, args):
            yield result

    @filter.command("装备详情")
    @effect_whitelist_only
    async def equipment_detail(self, event: AstrMessageEvent, equipment_id: int):
        await self._ensure_database_ready()
        async for result in self.command_handler.equipment_detail(event, equipment_id): yield result

    @filter.command("穿戴")
    @effect_whitelist_only
    async def equip_item(self, event: AstrMessageEvent, args: GreedyStr):
        await self._ensure_database_ready()
        async for result in self.command_handler.equip_item(event, args): yield result

    @filter.command("一键穿戴")
    @effect_whitelist_only
    async def auto_equip(self, event: AstrMessageEvent):
        """自动从背包挑选最优装备穿戴。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.auto_equip(event):
            yield result

    @filter.command("一键托管")
    @effect_whitelist_only
    async def auto_pilot(self, event: AstrMessageEvent):
        """开启静默的一键托管。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.auto_pilot(event, True):
            yield result

    @filter.command("关闭托管")
    @effect_whitelist_only
    async def stop_auto_pilot(self, event: AstrMessageEvent):
        """关闭静默的一键托管。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.auto_pilot(event, False):
            yield result

    @filter.command("卸下")
    @effect_whitelist_only
    async def unequip_item(self, event: AstrMessageEvent, args: GreedyStr):
        await self._ensure_database_ready()
        async for result in self.command_handler.unequip_item(event, args): yield result

    @filter.command("技能")
    @effect_whitelist_only
    async def skills(self, event: AstrMessageEvent):
        await self._ensure_database_ready()
        async for result in self.command_handler.skills(event): yield result

    @filter.command("学习")
    @effect_whitelist_only
    async def learn_skill(self, event: AstrMessageEvent, name: GreedyStr):
        await self._ensure_database_ready()
        async for result in self.command_handler.learn_skill(event, name): yield result

    @filter.command("训练技能")
    @effect_whitelist_only
    async def train_skill(self, event: AstrMessageEvent, args: GreedyStr):
        await self._ensure_database_ready()
        async for result in self.command_handler.train_skill(event, args): yield result

    @filter.command("技能栏")
    @effect_whitelist_only
    async def skill_slot(self, event: AstrMessageEvent, args: GreedyStr):
        await self._ensure_database_ready()
        async for result in self.command_handler.set_skill_slot(event, args): yield result
    @filter.command("魔法书")
    @effect_whitelist_only
    async def spellbooks(self, event: AstrMessageEvent, page: int = 1):
        await self._ensure_database_ready()
        async for result in self.command_handler.spellbooks(event, page): yield result

    @filter.command("阅读")
    @effect_whitelist_only
    async def read_spellbook(self, event: AstrMessageEvent, args: GreedyStr):
        await self._ensure_database_ready()
        async for result in self.command_handler.read_spellbook(event, args): yield result

    @filter.command("研制")
    @effect_whitelist_only
    async def craft_spellbook(self, event: AstrMessageEvent, args: GreedyStr):
        await self._ensure_database_ready()
        async for result in self.command_handler.craft_spellbook(event, args): yield result

    @filter.command("法术")
    @effect_whitelist_only
    async def spells(self, event: AstrMessageEvent):
        await self._ensure_database_ready()
        async for result in self.command_handler.spells(event): yield result

    @filter.command("战技")
    @effect_whitelist_only
    async def techniques(self, event: AstrMessageEvent):
        await self._ensure_database_ready()
        async for result in self.command_handler.techniques(event): yield result
    @filter.command("挑战")
    @effect_whitelist_only
    async def challenge(self, event: AstrMessageEvent):
        """At 一名用户发起事件模拟战斗。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.challenge(event):
            yield result

    @filter.command("战术")
    @effect_whitelist_only
    async def tactics(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """查看或设置开局、中盘、终盘三阶段战术。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.tactics(event, args):
            yield result

    @filter.command("复盘")
    @effect_whitelist_only
    async def replay(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """查看本群最近一场或指定编号的战斗复盘。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.replay(event, args):
            yield result

    @filter.command("今日")
    @effect_whitelist_only
    async def operations(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """查看今日异变、委托，或领取已完成奖励。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.operations(event, args):
            yield result

    @filter.command("周常")
    @effect_whitelist_only
    async def weekly_operations(self, event: AstrMessageEvent):
        """查看七选五周目标。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.operations(event, "周常"):
            yield result

    @filter.command("赛季")
    @effect_whitelist_only
    async def season(self, event: AstrMessageEvent):
        """查看28日PvP赛季进度。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.operations(event, "赛季"):
            yield result

    @filter.command("工坊")
    @effect_whitelist_only
    async def workshop(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """分解闲置装备，或进行有保底的定向词条重铸。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.workshop(event, args):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    @effect_whitelist_only
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
            elif command == "一键穿戴":
                async for result in self.command_handler.auto_equip(event): yield result
            elif command == "一键托管":
                async for result in self.command_handler.auto_pilot(event, True): yield result
            elif command == "关闭托管":
                async for result in self.command_handler.auto_pilot(event, False): yield result
            elif command == "穿戴":
                async for result in self.command_handler.equip_item(event, args): yield result
            elif command == "卸下":
                async for result in self.command_handler.unequip_item(event, args.strip()): yield result
            elif command == "技能":
                async for result in self.command_handler.skills(event): yield result
            elif command == "学习":
                async for result in self.command_handler.learn_skill(event, args.strip()): yield result
            elif command == "训练技能":
                async for result in self.command_handler.train_skill(event, args): yield result
            elif command == "技能栏":
                async for result in self.command_handler.set_skill_slot(event, args): yield result
            elif command == "魔法书":
                page = int(args) if args.strip().isdigit() else 1
                async for result in self.command_handler.spellbooks(event, page): yield result
            elif command == "阅读":
                if args.strip():
                    async for result in self.command_handler.read_spellbook(event, args.strip()): yield result
                else: yield event.plain_result("用法：/阅读 魔法书ID或法术名")
            elif command == "研制":
                async for result in self.command_handler.craft_spellbook(event, args.strip()): yield result
            elif command == "法术":
                async for result in self.command_handler.spells(event): yield result
            elif command == "战技":
                async for result in self.command_handler.techniques(event): yield result
            elif command == "挑战":
                async for result in self.command_handler.challenge(event):
                    yield result
            elif command == "战术":
                async for result in self.command_handler.tactics(event, args):
                    yield result
            elif command == "复盘":
                async for result in self.command_handler.replay(event, args):
                    yield result
            elif command == "今日":
                async for result in self.command_handler.operations(event, args):
                    yield result
            elif command == "周常":
                async for result in self.command_handler.operations(event, "周常"):
                    yield result
            elif command == "赛季":
                async for result in self.command_handler.operations(event, "赛季"):
                    yield result
            elif command == "工坊":
                async for result in self.command_handler.workshop(event, args):
                    yield result
            elif command == "奈菲亚":
                async for result in self.command_handler.nefia(event, args):
                    yield result
            elif command == "副本":
                async for result in self.command_handler.list_dungeons(event):
                    yield result
            elif command == "副本详情":
                async for result in self.command_handler.dungeon_detail(event, args):
                    yield result
            event.stop_event()
            return

        if not self.command_handler.is_alias_challenge_event(event):
            return
        async for result in self.command_handler.challenge(event):
            yield result
        event.stop_event()

    @filter.command("奈菲亚")
    @effect_whitelist_only
    async def nefia(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """探索每日生成、可撤退且会持久保存的随机奈菲亚。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.nefia(event, args):
            yield result

    @filter.command("副本")
    @effect_whitelist_only
    async def list_dungeons(self, event: AstrMessageEvent):
        """查看所有可用副本。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.list_dungeons(event):
            yield result

    @filter.command("副本详情")
    @effect_whitelist_only
    async def dungeon_detail(self, event: AstrMessageEvent, name: GreedyStr = ""):
        """查看指定副本的波次与奖励详情。"""
        await self._ensure_database_ready()
        async for result in self.command_handler.dungeon_detail(event, name):
            yield result

    async def terminate(self):
        """Stop background workers when the plugin is unloaded."""
        await self.auto_pilot_service.shutdown()
        await self.challenge_queue.shutdown()
