import asyncio
import hashlib
import inspect
import re
import time
from collections import defaultdict
from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image as MessageImage, Node
from astrbot.core.utils.io import save_temp_img

if "." in (__package__ or ""):
    from ..services.battle_image_renderer import (
        RENDERER_REVISION,
        render_battle_report,
    )
    from ..services.battle_report import BattleReportBuilder
    from ..services.image_renderer import render_text_card
else:
    from services.battle_image_renderer import (
        RENDERER_REVISION,
        render_battle_report,
    )
    from services.battle_report import BattleReportBuilder
    from services.image_renderer import render_text_card

EXPECTED_RENDERER_REVISION = "astrbot-card-v3-light"
LONG_TEXT_LINE_THRESHOLD = 6
LONG_TEXT_CHAR_THRESHOLD = 240
if RENDERER_REVISION != EXPECTED_RENDERER_REVISION:
    raise RuntimeError(
        "LevelUpPvp battle renderer version mismatch: "
        f"expected {EXPECTED_RENDERER_REVISION}, got {RENDERER_REVISION}"
    )

try:
    from ..models.chat_activity import ChatMessageContext
    from ..models.attributes import PRIMARY_ATTRIBUTE_IDS
    from ..models.equipment import ACTIVE_SOURCE_EFFECTS, SLOT_LABELS
    from ..models.operation import stable_operation_seed
    from ..models.user import LevelDownEvent, LevelUpEvent, User, UserIdentity
    from ..services.attribute_service import (
        ADVANCED_ATTRIBUTE_LABELS,
        ATTRIBUTE_LABELS,
        DAMAGE_TYPE_LABELS,
        LEGACY_ATTRIBUTE_MAP,
        WEAPON_PRIMARY_WEIGHTS,
        attribute_exp_required,
        skill_level_cap,
    )
    from ..services.equipment_affixes import (
        effective_inherent_affixes,
        inherent_affix_level_ratio,
    )
    from ..services.equipment_catalog import QUALITY_LABELS, QUALITY_MULTIPLIERS
    from ..services.equipment_proc_service import EQUIPMENT_PROC_NAMES
    from ..services.material_catalog import actual_weight, material_for
    from ..services.progression_rules import progress_percent
    from ..services.skill_catalog import SKILL_DEFINITIONS, skill_exp_required, skill_id_for
    from ..services.ability_catalog import (
        ACTIVE_ABILITY_DEFINITIONS, SPELL_DEFINITIONS, TECHNIQUE_DEFINITIONS,
        ability_is_unlocked, spell_exp_required,
    )
    from ..services.auto_equip_service import AutoEquipService
    from ..services import config
    from ..services.chat_activity_service import format_chat_activity_settlement
    from ..services.daily_growth_budget import daily_growth_day_window
except ImportError:
    from models.chat_activity import ChatMessageContext
    from models.attributes import PRIMARY_ATTRIBUTE_IDS
    from models.equipment import ACTIVE_SOURCE_EFFECTS, SLOT_LABELS
    from models.operation import stable_operation_seed
    from models.user import LevelDownEvent, LevelUpEvent, User, UserIdentity
    from services.attribute_service import (
        ADVANCED_ATTRIBUTE_LABELS,
        ATTRIBUTE_LABELS,
        DAMAGE_TYPE_LABELS,
        LEGACY_ATTRIBUTE_MAP,
        WEAPON_PRIMARY_WEIGHTS,
        attribute_exp_required,
        skill_level_cap,
    )
    from services.equipment_affixes import (
        effective_inherent_affixes,
        inherent_affix_level_ratio,
    )
    from services.equipment_catalog import QUALITY_LABELS, QUALITY_MULTIPLIERS
    from services.equipment_proc_service import EQUIPMENT_PROC_NAMES
    from services.material_catalog import actual_weight, material_for
    from services.progression_rules import progress_percent
    from services.skill_catalog import SKILL_DEFINITIONS, skill_exp_required, skill_id_for
    from services.ability_catalog import (
        ACTIVE_ABILITY_DEFINITIONS, SPELL_DEFINITIONS, TECHNIQUE_DEFINITIONS,
        ability_is_unlocked, spell_exp_required,
    )
    from services.auto_equip_service import AutoEquipService
    from services import config
    from services.chat_activity_service import format_chat_activity_settlement
    from services.daily_growth_budget import daily_growth_day_window


"""很不文明哦，好孩子别学"""
CHALLENGE_WAKE_WORDS = [
    "艾斯比",
    "啥比"
]
MENTION_COMMAND_NAMES = ("重载装备表", "装备图鉴", "魔法书", "阅读", "研制", "法术", "战技", "修改登记", "装备详情", "训练技能", "技能栏", "签到", "面板", "加点", "排行", "登记", "挑战", "战术", "复盘", "今日", "周常", "赛季", "工坊", "背包", "装备", "一键穿戴", "一键托管", "关闭托管", "穿戴", "卸下", "技能", "学习", "给予", "奈菲亚", "副本", "副本详情")
MENTION_COMMAND_PATTERN = re.compile(
    rf"^/?({'|'.join(MENTION_COMMAND_NAMES)})(?:\s|$)"
)
CHALLENGE_COMMAND_PATTERN = re.compile(r"^/?挑战(?:\s|$)")
SLASH_CHALLENGE_COMMAND_PATTERN = re.compile(r"^/挑战(?:\s|$)")
MODIFY_REGISTER_COMMAND_PATTERN = re.compile(r"^/?修改登记(?:\s|$)")
SLASH_CHECKIN_COMMAND_PATTERN = re.compile(r"^/签到(?:\s|$)")
CHECKIN_COMMAND_PATTERN = re.compile(r"^/?签到(?:\s|$)")
MENTION_MARKUP_PATTERN = re.compile(
    r"(?:<@!?(?P<legacy_id>[^>\s]+)>|"
    r"<qqbot-at-user\b[^>]*\bid\s*=\s*[\"']?"
    r"(?P<qqbot_id>[^\"'\s/>]+)[\"']?[^>]*/?>)",
    re.IGNORECASE,
)
REGISTRATION_REQUIRED_MESSAGE = (
    "当前消息未携带平台用户名，暂时无法自动登记，请稍后再试。"
)
ADMIN_REQUIRED_MESSAGE = "只有 AstrBot 管理员可以使用该指令。"
class LevelUpPvpCommandHandler:
    def __init__(
        self,
        *,
        context,
        user_service,
        checkin_service,
        stat_service,
        battle_service,
        challenge_queue=None,
        equipment_service=None,
        skill_service=None,
        build_service=None,
        auto_equip_service=None,
        auto_pilot_service=None,
        attribute_service=None,
        spell_service=None,
        dungeon_service=None,
        operation_service=None,
        operation_settlement_service=None,
        workshop_service=None,
        replay_service=None,
        chat_activity_service=None,
        chat_activity_settlement_service=None,
    ):
        self.context = context
        self.user_service = user_service
        self.checkin_service = checkin_service
        self.stat_service = stat_service
        self.battle_service = battle_service
        self.challenge_queue = challenge_queue
        self.equipment_service = equipment_service
        self.skill_service = skill_service
        self.build_service = build_service
        self.attribute_service = attribute_service
        self.spell_service = spell_service
        self.auto_equip_service = auto_equip_service or (
            AutoEquipService(build_service) if build_service is not None else None
        )
        self.auto_pilot_service = auto_pilot_service
        self.dungeon_service = dungeon_service
        self.operation_service = operation_service
        self.operation_settlement_service = operation_settlement_service
        self.workshop_service = workshop_service
        self.replay_service = replay_service
        self.chat_activity_service = chat_activity_service
        self.chat_activity_settlement_service = chat_activity_settlement_service

    @staticmethod
    def _is_long_text(text: str) -> bool:
        normalized = str(text).strip()
        return (
            len(normalized.splitlines()) >= LONG_TEXT_LINE_THRESHOLD
            or len(normalized) >= LONG_TEXT_CHAR_THRESHOLD
        )

    def _image_result(self, event: AstrMessageEvent, file_url: str, title: str):
        if (event.get_platform_name() or "") == "aiocqhttp":
            node = Node(
                uin=event.get_self_id() or "0",
                name=title,
                content=[MessageImage(file=file_url)],
            )
            return event.chain_result([node])
        return event.image_result(file_url)

    async def reply_text(
        self,
        event: AstrMessageEvent,
        text: str,
        title: str = "LevelUpPvp",
    ):
        """Return short text directly and render long text as one image."""
        text = str(text)
        if not self._is_long_text(text):
            return event.plain_result(text)
        try:
            image = render_text_card(text, title=title)
            return self._image_result(event, save_temp_img(image), title)
        except Exception:
            logger.exception("LevelUpPvp text reply image render failed")
            return event.plain_result(text)

    async def sign(self, event: AstrMessageEvent) -> AsyncGenerator:
        try:
            result = await self.checkin_service.checkin(self._identity_from_event(event))
            if result.already_checked:
                yield await self.reply_text(
                    event, self._format_existing_checkin(result), "LevelUpPvp 签到"
                )
                return

            yield await self.reply_text(
                event, self._format_checkin_success(result), "LevelUpPvp 签到"
            )
        except Exception as exc:
            logger.exception("LevelUpPvp sign failed")
            yield await self.reply_text(event, f"签到失败：{exc}")

    async def auto_checkin(self, event: AstrMessageEvent) -> AsyncGenerator:
        """群内当天首条消息自动签到；失败时不影响原消息传播。"""
        if not self.is_auto_checkin_event(event):
            return
        try:
            result = await self.checkin_service.checkin(self._identity_from_event(event))
            if result.already_checked:
                return
            yield await self.reply_text(
                event, self._format_checkin_success(result), "LevelUpPvp 签到"
            )
        except Exception:
            logger.exception("LevelUpPvp automatic check-in failed")

    async def chat_activity(self, event: AstrMessageEvent) -> AsyncGenerator:
        """Settle organic-chat growth while keeping ordinary chatter quiet."""

        if (
            self.chat_activity_service is None
            or self.chat_activity_settlement_service is None
        ):
            return
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        if (
            not group_id
            or not sender_id
            or sender_id == str(event.get_self_id() or "")
        ):
            return
        try:
            user = await self.user_service.get_or_create_user(
                self._identity_from_event(event)
            )
            occurred_at_ts = self._chat_event_timestamp(event)
            context = ChatMessageContext(
                event_key=self._chat_event_key(event, occurred_at_ts),
                group_id=group_id,
                user_pk=user.id,
                content=event.get_message_str() or "",
                occurred_at_ts=occurred_at_ts,
                is_bot=False,
                is_command=self._is_chat_command(event),
                is_group_message=True,
            )
            decision = await self.chat_activity_service.prepare_message(context)
            if not decision.should_settle:
                return
            result = await self.chat_activity_settlement_service.settle(
                decision.intent
            )
            if not result.applied:
                return
            # Tiny EXP gains are intentionally silent.  Drops and level-ups are
            # the moments worth interrupting a real group conversation for.
            if (
                result.equipment is None
                and result.spellbook is None
                and not result.level_ups
            ):
                return
            username = " ".join(
                str(
                    getattr(user, "nickname", "")
                    or event.get_sender_name()
                    or event.get_sender_id()
                    or "未知用户"
                ).split()
            )
            text = format_chat_activity_settlement(result, username=username)
            if result.equipment is not None:
                item_id = getattr(result.equipment, "id", None)
                if item_id:
                    text += f"\n可用 /装备详情 {item_id} 查看，或 /一键穿戴 尝试换装。"
            if result.spellbook is not None:
                book_id = getattr(result.spellbook, "id", None)
                if book_id:
                    text += f"\n可用 /阅读 {book_id} 尝试研读。"
            if text:
                yield await self.reply_text(event, text, "LevelUpPvp 聊天奇遇")
        except Exception:
            # Ambient progression must never turn a normal conversation into
            # an error notification.  The durable intent remains retryable.
            logger.exception("LevelUpPvp chat activity failed")

    async def ambient_activity(self, event: AstrMessageEvent) -> AsyncGenerator:
        """Settle check-in and chat growth with at most one visible response."""

        checkin_replies = [item async for item in self.auto_checkin(event)]
        chat_replies = [item async for item in self.chat_activity(event)]
        # A player's first interaction may naturally be a command.  It still
        # consumes today's check-in, but the command's own reply should remain
        # the only visible response instead of producing a noisy extra card.
        visible_checkin = [] if self._is_chat_command(event) else checkin_replies
        for reply in chat_replies or visible_checkin:
            yield reply

    async def ensure_sender_registered(self, event: AstrMessageEvent) -> bool:
        """Silently register a new group member from the platform username."""
        identity = self._identity_from_event(event)
        nickname = " ".join((event.get_sender_name() or "").split())
        if (
            not identity.group_id
            or not identity.user_id
            or nickname in {"", identity.user_id, "未知用户"}
        ):
            return False
        try:
            if await self.user_service.has_registered_nickname(identity):
                return True
            await self.user_service.register_nickname(identity, nickname)
            return True
        except Exception:
            logger.exception("LevelUpPvp automatic nickname registration failed")
            return False

    def is_auto_checkin_event(self, event: AstrMessageEvent) -> bool:
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        if (
            not group_id
            or not sender_id
            or str(sender_id) == str(event.get_self_id())
        ):
            return False
        return not self.is_explicit_checkin_event(event)

    def is_explicit_checkin_event(self, event: AstrMessageEvent) -> bool:
        message = (event.get_message_str() or "").strip()
        if SLASH_CHECKIN_COMMAND_PATTERN.match(message):
            return True
        if not self._is_self_mentioned(event):
            return False
        text = self._text_without_mentions(event)
        return CHECKIN_COMMAND_PATTERN.match(text) is not None

    async def profile(self, event: AstrMessageEvent) -> AsyncGenerator:
        registration_error = await self._registration_error(event)
        if registration_error:
            yield await self.reply_text(event, registration_error)
            return
        try:
            identity = self._target_identity_from_event(event) or self._identity_from_event(
                event
            )
            user = await self.user_service.get_or_create_user(identity)
            build = None
            derived = None
            progress = None
            combat_state = None
            if self.equipment_service and self.skill_service and self.build_service:
                slots, items = await self.equipment_service.get_loadout(user.id)
                skills, _ = await self.skill_service.get_skills(user)
                build = self.build_service.resolve_equipment(
                    user, slots, items, skills
                )
                derived = self.build_service.resolve_derived(
                    user, build, skills
                )
                if self.attribute_service and self.attribute_service.db_path:
                    progress = await self.attribute_service.get_progress(user.id)
                if self.battle_service:
                    combat_state = (
                        await self.battle_service.combat_state_view(user)
                    )
            yield await self.reply_text(
                event,
                self._format_profile(
                    user, build, derived, progress, combat_state
                ),
                "LevelUpPvp 面板",
            )
        except Exception as exc:
            logger.exception("LevelUpPvp profile failed")
            yield await self.reply_text(event, f"查看面板失败：{exc}")

    async def add_point(
        self,
        event: AstrMessageEvent,
        stat_name: str = "",
        amount: int = 1,
    ) -> AsyncGenerator:
        registration_error = await self._registration_error(event)
        if registration_error:
            yield await self.reply_text(event, registration_error)
            return
        if not stat_name:
            yield await self.reply_text(event, "用法：/加点 力量 2")
            return
        try:
            result = await self.stat_service.allocate(
                self._identity_from_event(event),
                stat_name,
                amount,
            )
            label = config.STAT_LABELS[result.stat_name]
            yield await self.reply_text(
                event,
                "\n".join(
                    [
                        f"加点成功：{label} +{result.total_gain}",
                        f"消耗属性点：{result.points_spent}",
                        "换算规则：1 属性点 = 1 主属性",
                        f"剩余属性点：{result.user.stat_points}",
                        self._format_stats(result.user),
                    ]
                ),
                "LevelUpPvp 加点",
            )
        except Exception as exc:
            yield await self.reply_text(event, str(exc))

    async def inventory(self, event, page: int = 1):
        try:
            user = await self._own_user(event)
            items = await self.equipment_service.list_items(user.id)
            _, equipped = await self.equipment_service.get_loadout(user.id)
            equipped_ids = {item.id for item in equipped}
            page = max(1, int(page or 1)); size = 10; start = (page - 1) * size
            total_pages = max(1, (len(items) + size - 1) // size)
            lines = [
                f"{self._display_name(user)} 的背包 · 共{len(items)}件 · "
                f"第{page}/{total_pages}页"
            ]
            for item in items[start:start + size]:
                marks = []
                if item.id in equipped_ids:
                    marks.append("已装备")
                if bool(getattr(item, "is_locked", False)):
                    marks.append("已收藏")
                mark = f"[{'/'.join(marks)}]" if marks else ""
                material = material_for(item.material)
                resolved_weight = actual_weight(item.weight, item.material)
                lines.append(
                    f"No.{item.id} {QUALITY_LABELS.get(item.quality, item.quality)} {item.name} "
                    f"Lv.{item.item_level} {material.name} "
                    f"重量{item.weight:g}×{material.weight_multiplier:g}={resolved_weight:.3f}{mark}"
                )
            if len(lines) == 1: lines.append("这一页没有装备。")
            lines.append(
                "整理：/工坊 整理 支配（只选完全更弱）；"
                "背包很满可 /工坊 整理 优秀（普通+优秀，二次确认）"
            )
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 背包"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"查看背包失败：{exc}")

    async def equipment(self, event):
        try:
            registration_error = await self._registration_error(event)
            if registration_error:
                yield await self.reply_text(event, registration_error)
                return
            identity = self._target_identity_from_event(event) or self._identity_from_event(event)
            user = await self.user_service.get_or_create_user(identity)
            slots, items = await self.equipment_service.get_loadout(user.id)
            build = await self._combat_build(user, slots, items)
            by_id = {item.id: item for item in items}
            lines = [f"{self._display_name(user)} 的装备"]
            for slot, label in SLOT_LABELS.items():
                item = by_id.get(slots.get(slot)); lines.append(f"{label}：{item.name if item else '空'}")
            lines.append(f"战斗方式：{self._weapon_mode_label(build.weapon_mode)}")
            lines.append(f"护甲路线：{self._armor_style_label(build.armor_style)} 重量{build.total_weight:.2f}/{build.carry_capacity:.1f}{'（超负重）' if build.overloaded else ''}")
            lines.append(f"命中修正：物理 {build.physical_accuracy_multiplier:.0%} / 法术 {build.spell_accuracy_multiplier:.0%}")
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 装备"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"查看装备失败：{exc}")

    async def equipment_catalog(self, event, args: str = ""):
        try:
            parts = str(args or "").split()
            if len(parts) > 2:
                raise ValueError("用法：/装备图鉴 [页] [全部|武器|盾牌|防具|饰品|黑星]")
            page = 1
            category = "全部"
            if parts:
                if parts[0].isdigit():
                    page = int(parts[0])
                    if len(parts) == 2:
                        category = parts[1]
                else:
                    category = parts[0]
                    if len(parts) == 2:
                        if not parts[1].isdigit():
                            raise ValueError(
                                "用法：/装备图鉴 [页] [全部|武器|盾牌|防具|饰品|黑星]"
                            )
                        page = int(parts[1])
            predicates = {
                "全部": lambda entry: True,
                "武器": lambda entry: entry.template.item_type == "weapon",
                "盾牌": lambda entry: entry.template.item_type == "shield",
                "防具": lambda entry: entry.template.item_type == "armor",
                "饰品": lambda entry: entry.template.item_type == "accessory",
                "黑星": lambda entry: (
                    entry.mode == "fixed"
                    and entry.fixed.get("star_type") == "black_star"
                ),
            }
            if category not in predicates:
                raise ValueError(
                    "未知分类。可用：全部、武器、盾牌、防具、饰品、黑星"
                )
            entries = [
                entry
                for entry in self.equipment_service.catalog.snapshot.entries
                if predicates[category](entry)
            ]
            size = 20
            total_pages = max(1, (len(entries) + size - 1) // size)
            if page < 1 or page > total_pages:
                raise ValueError(
                    f"页码应在 1–{total_pages} 之间（{category}共{len(entries)}件）"
                )
            start = (page - 1) * size
            lines = [
                f"装备图鉴·{category} 第{page}/{total_pages}页 "
                f"（共{len(entries)}件）"
            ]
            for entry in entries[start:start + size]:
                template = entry.template
                star = (
                    "黑星"
                    if entry.mode == "fixed"
                    and entry.fixed.get("star_type") == "black_star"
                    else "普通"
                )
                lines.append(
                    f"{entry.catalog_id} {template.name} "
                    f"[{SLOT_LABELS[template.equip_slot]}] {star}"
                )
            yield await self.reply_text(
                event,
                "\n".join(lines),
                "LevelUpPvp 装备图鉴",
            )
        except Exception as exc:
            yield await self.reply_text(event, f"查看装备图鉴失败：{exc}")

    async def grant_equipment(self, event, args: str):
        if not await self._is_astrbot_admin(event):
            yield await self.reply_text(event, ADMIN_REQUIRED_MESSAGE)
            return
        try:
            args = " ".join(str(args or "").split())
            catalog_args = MENTION_MARKUP_PATTERN.sub(" ", args)
            catalog_args = re.sub(r"\[At:[^\]]+\]", " ", catalog_args)
            catalog_args = re.sub(r"@\S+", " ", catalog_args)
            id_matches = re.findall(r"(?<!\d)(\d+)(?!\d)", catalog_args)
            if len(id_matches) != 1:
                raise ValueError(
                    "用法：/给予 @用户 装备表ID；"
                    "/给予 本群 装备表ID；/给予 全服 装备表ID 确认"
                )
            catalog_id = int(id_matches[0])
            entry = self.equipment_service.catalog.get(catalog_id)
            platform = (
                event.get_platform_id()
                or event.get_platform_name()
                or "unknown"
            )
            group_id = event.get_group_id() or ""
            if "全服" in args:
                user_pks = await self.user_service.list_user_pks()
                if "确认" not in args.split():
                    yield await self.reply_text(
                        event,
                        f"装备表 ID {catalog_id}：{entry.template.name}\n"
                        f"预计接收人数：{len(user_pks)}\n"
                        f"未执行。请发送：/给予 全服 {catalog_id} 确认",
                    )
                    return
            elif "本群" in args:
                user_pks = await self.user_service.list_user_pks(
                    platform=platform,
                    group_id=group_id,
                )
            else:
                target = self._grant_target_identity(event)
                if target is None:
                    raise ValueError("单人发放用法：/给予 @用户 装备表ID")
                user = await self.user_service.get_or_create_user(target)
                user_pks = [user.id]
            result = await self.equipment_service.grant_catalog_item(
                user_pks,
                catalog_id,
            )
            yield await self.reply_text(
                event,
                f"发放完成：装备表 ID {result.catalog_id} "
                f"{result.equipment_name}\n"
                f"成功：{result.granted} 人\n"
                f"已有跳过：{result.skipped} 人",
            )
        except Exception as exc:
            yield await self.reply_text(event, f"发放失败：{exc}")

    async def reload_equipment_catalog(self, event):
        if not await self._is_astrbot_admin(event):
            yield await self.reply_text(event, ADMIN_REQUIRED_MESSAGE)
            return
        try:
            snapshot = self.equipment_service.catalog.reload()
            yield await self.reply_text(
                event,
                f"装备表重载成功：schema v{snapshot.schema_version}，"
                f"共 {len(snapshot.entries)} 件装备。",
            )
        except Exception as exc:
            yield await self.reply_text(
                event,
                f"装备表重载失败，已继续使用旧目录：{exc}",
            )

    async def equipment_detail(self, event, equipment_id: int):
        try:
            user = await self._own_user(event); item = await self.equipment_service.item_detail(user.id, int(equipment_id))
            material = material_for(item.material)
            resolved_weight = actual_weight(item.weight, item.material)
            effective_inherent = effective_inherent_affixes(
                item.inherent_affixes,
                user.level,
                item.item_level,
            )
            inherent_ratio = inherent_affix_level_ratio(
                user.level,
                item.item_level,
            )
            inherent_numeric = tuple(
                affix
                for affix in item.inherent_affixes
                if affix.get("type") != "trigger_ability"
            )
            effective_numeric = tuple(
                affix
                for affix in effective_inherent
                if affix.get("type") != "trigger_ability"
            )
            inherent_procs = tuple(
                affix
                for affix in item.inherent_affixes
                if affix.get("type") == "trigger_ability"
            )
            effective_procs = tuple(
                affix
                for affix in effective_inherent
                if affix.get("type") == "trigger_ability"
            )
            lines = [f"#{item.id} {item.name}"]
            if bool(getattr(item, "is_locked", False)):
                lines.append("保护：已收藏锁定（工坊不会分解）")
            if item.description:
                lines.append(f"介绍：{item.description}")
            lines.extend(
                [
                    f"品质：{QUALITY_LABELS.get(item.quality, item.quality)} / {item.star_type}",
                    f"等级：{item.item_level} 材质：{material.name} 状态：{item.blessing_state}",
                    f"重量：基础{item.weight:g} × {material.weight_multiplier:g} = {resolved_weight:.3f} 强化：+{item.enhancement_level}",
                    f"附魔容量：{item.used_capacity}/{item.enchant_capacity}",
                    f"基础：{item.base_stats or '无'}",
                    f"固有词条（原始）：{self._format_affixes(inherent_numeric)}",
                    self._effective_inherent_affix_line(
                        effective_numeric,
                        user.level,
                        item.item_level,
                        inherent_ratio,
                    ),
                    "触发能力（原始）："
                    + self._format_proc_affixes(inherent_procs),
                    "触发能力（当前有效）："
                    + self._format_proc_affixes(effective_procs),
                    f"随机词条：{self._format_affixes(item.random_affixes)}",
                    f"融合词条：{self._format_affixes(item.fusion_affixes)}",
                ]
            )
            source_effects = tuple(getattr(item, "source_effects", ()))
            if source_effects:
                active_effects = tuple(
                    effect
                    for effect in source_effects
                    if effect in ACTIVE_SOURCE_EFFECTS
                )
                reference_effects = tuple(
                    effect
                    for effect in source_effects
                    if effect not in ACTIVE_SOURCE_EFFECTS
                )
                if active_effects:
                    lines.append(
                        "已生效效果：" + "、".join(active_effects)
                    )
                if reference_effects:
                    lines.append(
                        "资料效果（当前未结算）："
                        + "、".join(reference_effects)
                    )
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 装备详情"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"查看装备详情失败：{exc}")

    async def equip_item(self, event, args, slot: str = ""):
        try:
            user = await self._own_user(event)
            if isinstance(args, int):
                assignments = (
                    (args, self._slot_id(slot) if slot else ""),
                )
            else:
                assignments = self._parse_equip_assignments(str(args or ""))
            results = await self.equipment_service.equip_many(
                user.id, assignments
            )
            lines = [
                f"{item.name}：{'、'.join(SLOT_LABELS[value] for value in slots)}"
                for item, slots in results
            ]
            yield await self.reply_text(
                event, "已穿戴：\n" + "\n".join(lines)
            )
        except Exception as exc:
            yield await self.reply_text(event, f"穿戴失败：{exc}")

    async def unequip_item(self, event, args: str):
        try:
            user = await self._own_user(event)
            values = str(args or "").split()
            if not values:
                raise ValueError("用法：/卸下 槽位 [槽位...]|全部")
            if values == ["全部"]:
                count = await self.equipment_service.unequip_all(user.id)
                yield await self.reply_text(
                    event, f"已卸下全部装备（{count}件）。"
                )
                return
            if "全部" in values:
                raise ValueError("“全部”不能与装备槽混用")
            normalized = tuple(self._slot_id(value) for value in values)
            if len(set(normalized)) != len(normalized):
                raise ValueError("卸下列表中有重复装备槽")
            count = await self.equipment_service.unequip_many(
                user.id, normalized
            )
            labels = "、".join(SLOT_LABELS[value] for value in normalized)
            yield await self.reply_text(
                event, f"已卸下{labels}对应的装备（{count}件）。"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"卸下失败：{exc}")

    async def auto_equip(self, event):
        try:
            user = await self._own_user(event)
            assignments = await self.auto_equip_service.select_for_user(
                user,
                respect_locked=True,
            )
            if not assignments:
                yield await self.reply_text(event, "背包里没有可选装备。")
                return
            results = await self.equipment_service.auto_equip(user.id, assignments)
            lines = [f"{self._display_name(user)} 已自动穿戴"]
            for item, slots in results:
                slot_labels = "、".join(SLOT_LABELS[s] for s in slots)
                quality = QUALITY_LABELS.get(item.quality, item.quality)
                lines.append(
                    f"{quality} {item.name} Lv.{item.item_level}：{slot_labels}"
                )
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 一键穿戴"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"一键穿戴失败：{exc}")

    async def auto_pilot(self, event, enabled: bool):
        """Toggle silent background automation for the current player."""
        if self.auto_pilot_service is None:
            yield await self.reply_text(event, "托管功能未启用。")
            return
        try:
            user = await self._own_user(event)
            if enabled:
                await self.auto_pilot_service.enable(
                    self._identity_from_event(event),
                    origin_umo=getattr(event, "unified_msg_origin", ""),
                )
                yield await self.reply_text(
                    event,
                    "一键托管已开启，后续将静默自动运营。",
                    "LevelUpPvp 托管",
                )
            else:
                await self.auto_pilot_service.disable(user.id)
                yield await self.reply_text(
                    event,
                    "一键托管已关闭。",
                    "LevelUpPvp 托管",
                )
        except Exception as exc:
            yield await self.reply_text(event, f"托管操作失败：{exc}")

    def _dominant_attribute(self, attributes) -> str:
        return self.auto_equip_service.dominant_attribute(attributes)

    def _score_item(self, item, user, dominant_attr: str) -> float:
        return self.auto_equip_service.score_item(item, user, dominant_attr)

    def _select_optimal_loadout(self, items, user, skills, dominant_attr: str):
        return self.auto_equip_service.select_optimal_loadout(
            items,
            user,
            skills,
            dominant_attr,
        )

    def _generate_hand_options(self, weapons, shields, user, dominant_attr):
        return self.auto_equip_service._generate_hand_options(
            weapons,
            shields,
            user,
            dominant_attr,
            {"main_hand", "off_hand"},
        )

    @staticmethod
    def _score_build(equipment, dominant_attr: str) -> float:
        return AutoEquipService._score_build(equipment, dominant_attr)

    async def skills(self, event):
        try:
            registration_error = await self._registration_error(event)
            if registration_error:
                yield await self.reply_text(event, registration_error)
                return
            identity = self._target_identity_from_event(event) or self._identity_from_event(event)
            user = await self.user_service.get_or_create_user(identity)
            skills, slots = await self.skill_service.get_skills(user)
            attributes = self.attribute_service.attributes_for_user(user)
            lines = [f"{self._display_name(user)} 的技能（技能点 {user.skill_points}）"]
            learned = sorted(
                skills.values(),
                key=lambda value: (
                    not SKILL_DEFINITIONS[value.skill_id].passive,
                    SKILL_DEFINITIONS[value.skill_id].name,
                ),
            )
            passive_lines = []
            active_lines = []
            for skill in learned:
                definition = SKILL_DEFINITIONS[skill.skill_id]
                level_cap = skill_level_cap(
                    attributes,
                    definition.governing_attributes,
                    skill.skill_id,
                )
                line = (
                    f"{definition.name} Lv.{skill.level}/{level_cap} "
                    f"EXP {progress_percent(skill.exp, skill_exp_required(skill.level)):.1f}% "
                    f"潜力{skill.potential}%"
                )
                (passive_lines if definition.passive else active_lines).append(line)
            if passive_lines:
                lines.append("【被动天赋】")
                lines.extend(passive_lines)
            if active_lines:
                lines.append("【主动技能】")
                lines.extend(active_lines)

            available = []
            locked = []
            active_available = []
            for skill_id, definition in SKILL_DEFINITIONS.items():
                if skill_id in skills:
                    continue
                missing = self.skill_service.missing_prerequisites(
                    definition, skills
                )
                if missing:
                    progress = "、".join(
                        f"{SKILL_DEFINITIONS[required_id].name} "
                        f"{skills.get(required_id).level if required_id in skills else 0}/{required_level}"
                        for required_id, required_level in missing
                    )
                    locked.append(f"{definition.name}（{progress}）")
                elif definition.passive:
                    available.append(definition.name)
                else:
                    active_available.append(definition.name)
            if available:
                lines.append("可学习天赋：" + "、".join(available))
            if active_available:
                lines.append("可学习主动技能：" + "、".join(active_available))
            if locked:
                lines.append("未解锁进阶：" + "；".join(locked))
            lines.append(
                "技能栏：" + " / ".join(
                    f"{i + 1}.{ACTIVE_ABILITY_DEFINITIONS[s].name if s in ACTIVE_ABILITY_DEFINITIONS else '空'}"
                    for i, s in enumerate(slots)
                )
            )
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 技能"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"查看技能失败：{exc}")
    async def spellbooks(self, event, page: int = 1):
        try:
            user = await self._own_user(event)
            library = await self.spell_service.get_book_library(user)
            page_size = 6
            total_pages = max(
                1, (len(library.entries) + page_size - 1) // page_size
            )
            page = min(total_pages, max(1, int(page)))
            visible = library.entries[
                (page - 1) * page_size:page * page_size
            ]
            held_count = sum(entry.quantity for entry in library.entries)
            lines = [
                f"{self._display_name(user)} 的魔法书（第{page}/{total_pages}页）",
                f"法术图鉴 {library.learned_count}/{library.total_spell_count}｜"
                f"持有 {held_count}本/{len(library.entries)}种｜"
                f"咒文残页 {library.research_pages}张",
            ]
            if not library.entries:
                lines.append(
                    "暂无魔法书。正常聊天中的奇遇与奈菲亚探索都有机会发现。"
                )
            for entry in visible:
                definition = SPELL_DEFINITIONS[entry.spell_id]
                attribute = ATTRIBUTE_LABELS.get(
                    entry.reading_attribute, entry.reading_attribute
                )
                school = SKILL_DEFINITIONS.get(entry.school_id)
                school_name = school.name if school else entry.school_id
                if entry.learned_spell is None:
                    learned = "未学"
                else:
                    learned = (
                        f"已学Lv.{entry.learned_spell.level}·"
                        f"潜力{entry.learned_spell.potential}%"
                    )
                references = "、".join(
                    f"#{item.id}" + (f"×{item.quantity}" if item.quantity > 1 else "")
                    for item in entry.items
                )
                lines.append(
                    f"《{definition.name}》×{entry.quantity} [{learned}]｜"
                    f"{school_name}·{attribute}·难度{entry.reading_difficulty}"
                )
                if entry.research_pages_per_book:
                    reading_state = (
                        f"潜力已满：每本转化+{entry.research_pages_per_book}残页"
                    )
                    next_step = f"/阅读 {entry.spell_name} 转化最老一本"
                else:
                    reading_state = (
                        f"成功率{entry.success_chance:.1%}｜"
                        f"研读进度{entry.study_progress:.0%}"
                    )
                    if entry.school_level < 1:
                        next_step = f"/学习 {school_name} 后再阅读"
                    elif entry.studied_today:
                        next_step = "已研读，等待下个04:00日界线"
                    else:
                        next_step = f"/阅读 {entry.spell_name}（默认最老#{entry.oldest_book_id}）"
                lines.append(
                    f"  可用 {references}｜{reading_state}｜下一步：{next_step}"
                )
            affordable = [
                option for option in library.craft_options if option.affordable
            ]
            if affordable:
                suggestions = "、".join(
                    f"{option.spell_name}({option.cost})"
                    for option in affordable[:3]
                )
                lines.append(
                    f"残页可定向研制未学书：{suggestions}；"
                    "使用 /研制 法术名。"
                )
            elif library.craft_options:
                target = library.craft_options[0]
                missing = max(0, target.cost - library.research_pages)
                lines.append(
                    f"残页目标：《{target.spell_name}》需{target.cost}张，"
                    f"还差{missing}张；成功读懂重复书会恢复潜力并抄录残页。"
                )
            if visible:
                lines.append(
                    "可用 /阅读 法术名 自动消耗最老一本，也可 /阅读 #编号；"
                    "读失败不会损失魔法书。"
                )
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 魔法书"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"查看魔法书失败：{exc}")

    async def read_spellbook(self, event, book_id):
        try:
            user = await self._own_user(event)
            book_reference = str(book_id or "").strip().lstrip("#")
            if not book_reference:
                raise ValueError("用法：/阅读 魔法书ID或法术名")
            result = await self.spell_service.read_book(user, book_reference)
            if result.outcome == "research_converted":
                definition = SPELL_DEFINITIONS[result.spell.spell_id]
                yield await self.reply_text(
                    event,
                    f"《{definition.name}》潜力已满，重复书已化为"
                    f"{result.research_pages_gain}张咒文残页。\n"
                    f"当前残页：{result.research_pages_balance}张；"
                    "用 /魔法书 查看可定向研制的未学法术。",
                )
                return
            detail = ""
            if result.success and result.spell:
                definition = SPELL_DEFINITIONS[result.spell.spell_id]
                if result.outcome == "learned":
                    detail = (
                        f"\n你永久学会了「{definition.name}」Lv.{result.spell.level}。"
                        f"\n让它进入战斗：/技能栏 1 {definition.name}"
                    )
                else:
                    detail = (
                        f"\n「{definition.name}」潜力恢复至"
                        f"{result.spell.potential}%（+{result.potential_gain}%）。"
                    )
                    if result.research_pages_gain:
                        detail += (
                            f"\n你同时抄录了{result.research_pages_gain}张咒文残页，"
                            f"当前共{result.research_pages_balance}张。"
                        )
            attribute = ATTRIBUTE_LABELS.get(result.reading_attribute, result.reading_attribute)
            if result.success:
                text = (
                    f"阅读成功（阅读能力{result.reading_power:.0f}，"
                    f"难度{result.reading_difficulty}，主属性：{attribute}，"
                    f"成功率{result.chance:.1%}）。已消耗{result.consumed}本。"
                    f"{detail}"
                )
            else:
                text = (
                    f"你暂时没能读懂这本书（阅读能力{result.reading_power:.0f}，"
                    f"难度{result.reading_difficulty}，主属性：{attribute}，"
                    f"本次成功率{result.chance:.1%}）。\n"
                    f"魔法书完好保留；研读进度已到{result.study_progress:.0%}，"
                    "下个04:00日界线后可继续研读，届时成功率会保留提升。"
                )
            yield await self.reply_text(
                event,
                text,
            )
        except Exception as exc:
            retry = (
                f"\n学完对应学派后继续：/阅读 {str(book_id).strip()}"
                if "/学习" in str(exc)
                else ""
            )
            yield await self.reply_text(event, f"阅读失败：{exc}{retry}")

    async def craft_spellbook(self, event, spell_name: str):
        try:
            user = await self._own_user(event)
            name = str(spell_name or "").strip()
            if not name:
                raise ValueError("用法：/研制 法术名")
            result = await self.spell_service.craft_book(user, name)
            yield await self.reply_text(
                event,
                f"定向研制完成：《{result.spell_name}》#{result.item.id}。\n"
                f"消耗{result.pages_spent}张咒文残页，"
                f"剩余{result.pages_balance}张。\n"
                f"下一步：/阅读 {result.item.id}",
            )
        except Exception as exc:
            yield await self.reply_text(event, f"研制失败：{exc}")

    async def spells(self, event):
        try:
            registration_error = await self._registration_error(event)
            if registration_error:
                yield await self.reply_text(event, registration_error); return
            identity = self._target_identity_from_event(event) or self._identity_from_event(event)
            user = await self.user_service.get_or_create_user(identity)
            spells = await self.spell_service.get_spells(user.id)
            lines = [f"{self._display_name(user)} 的法术"]
            if not spells:
                lines.append("尚未通过魔法书学会法术。")
            for spell in sorted(spells.values(), key=lambda value: SPELL_DEFINITIONS[value.spell_id].name):
                definition = SPELL_DEFINITIONS[spell.spell_id]
                lines.append(
                    f"{definition.name} Lv.{spell.level} "
                    f"EXP {progress_percent(spell.exp, spell_exp_required(spell.level)):.1f}% "
                    f"潜力{spell.potential}%"
                )
            if spells:
                lines.append("装入战斗栏：/技能栏 位置 法术名（例如 /技能栏 1 魔法箭）")
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 法术"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"查看法术失败：{exc}")

    async def techniques(self, event):
        try:
            registration_error = await self._registration_error(event)
            if registration_error:
                yield await self.reply_text(event, registration_error); return
            identity = self._target_identity_from_event(event) or self._identity_from_event(event)
            user = await self.user_service.get_or_create_user(identity)
            skills, _ = await self.skill_service.get_skills(user)
            unlocked, locked = [], []
            for definition in TECHNIQUE_DEFINITIONS.values():
                skill = skills.get(definition.unlock_skill_id)
                level = skill.level if skill else 0
                text = f"{definition.name}（{SKILL_DEFINITIONS[definition.unlock_skill_id].name} {level}/{definition.unlock_level}）"
                (unlocked if ability_is_unlocked(definition, skills, {}) else locked).append(text)
            lines = [f"{self._display_name(user)} 的战技", "【已解锁】" + ("、".join(unlocked) if unlocked else "无")]
            if locked: lines.append("【未解锁】" + "、".join(locked))
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 战技"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"查看战技失败：{exc}")
    async def learn_skill(self, event, name: str):
        try:
            user = await self._own_user(event)
            names = self._expand_skill_names(name)
            existing_skills, _ = await self.skill_service.get_skills(user)
            known_ids = set(existing_skills)
            pending = tuple(
                skill_name for skill_name in names
                if skill_id_for(skill_name) not in known_ids
            )
            if not pending:
                learned_names = "、".join(
                    SKILL_DEFINITIONS[skill_id_for(n)].name for n in names
                )
                raise ValueError(f"{learned_names}已经学会")
            skills = await self.skill_service.learn_many(user, pending)
            labels = "、".join(
                SKILL_DEFINITIONS[skill.skill_id].name for skill in skills
            )
            message = f"已学习技能：{labels}（均为 Lv.1）"
            skipped = tuple(
                skill_name for skill_name in names
                if skill_id_for(skill_name) in known_ids
            )
            if skipped:
                skipped_labels = "、".join(
                    SKILL_DEFINITIONS[skill_id_for(n)].name for n in skipped
                )
                message += f"\n已跳过已学会技能：{skipped_labels}"
            yield await self.reply_text(event, message)
        except Exception as exc:
            yield await self.reply_text(event, f"学习失败：{exc}")

    async def train_skill(
        self,
        event,
        args: str,
        points: int | None = None,
    ):
        try:
            user = await self._own_user(event)
            assignments = (
                ((args, int(points)),)
                if points is not None
                else self._parse_train_assignments(args)
            )
            skills = await self.skill_service.train_many(user, assignments)
            lines = [
                f"{SKILL_DEFINITIONS[skill.skill_id].name}："
                f"潜力提升至{skill.potential}%"
                for skill in skills
            ]
            yield await self.reply_text(
                event,
                "训练完成：\n" + "\n".join(lines),
            )
        except Exception as exc:
            yield await self.reply_text(event, f"训练失败：{exc}")

    async def set_skill_slot(
        self,
        event,
        args,
        name: str | None = None,
    ):
        try:
            user = await self._own_user(event)
            assignments = (
                ((int(args), name),)
                if name is not None
                else self._parse_skill_slot_assignments(str(args or ""))
            )
            await self.skill_service.set_active_slots(user, assignments)
            lines = [
                f"{slot}.{'空' if ability_name == '清空' else ability_name}"
                for slot, ability_name in assignments
            ]
            yield await self.reply_text(
                event, "技能栏已更新：" + " / ".join(lines)
            )
        except Exception as exc:
            yield await self.reply_text(event, f"技能栏设置失败：{exc}")

    def _expand_skill_names(self, args: str) -> tuple[str, ...]:
        tokens = str(args or "").split()
        if not tokens:
            raise ValueError("用法：/学习 技能名 [技能名...]|起始技能-结束技能")
        ordered = list(SKILL_DEFINITIONS)
        names = []
        for token in tokens:
            if "-" not in token:
                names.append(token)
                continue
            start_name, end_name = token.split("-", 1)
            start_id = self._skill_id(start_name)
            end_id = self._skill_id(end_name)
            start = ordered.index(start_id)
            end = ordered.index(end_id)
            if start > end:
                raise ValueError("技能范围必须按/技能中的顺序填写")
            names.extend(SKILL_DEFINITIONS[skill_id].name for skill_id in ordered[start:end + 1])
        if len(set(names)) != len(names):
            raise ValueError("学习列表中有重复技能")
        return tuple(names)

    @staticmethod
    def _skill_id(name: str) -> str:
        normalized = str(name or "").strip()
        for skill_id, definition in SKILL_DEFINITIONS.items():
            if normalized in {skill_id, definition.name}:
                return skill_id
        raise ValueError(f"未知技能：{normalized}")

    @staticmethod
    def _parse_train_assignments(
        args: str,
    ) -> tuple[tuple[str, int], ...]:
        tokens = str(args or "").split()
        if len(tokens) == 1:
            return ((tokens[0], 1),)
        if not tokens or len(tokens) % 2:
            raise ValueError("用法：/训练技能 技能名 点数 [技能名 点数...]")
        assignments = []
        for index in range(0, len(tokens), 2):
            if not tokens[index + 1].isdigit():
                raise ValueError("训练点数必须是正整数")
            points = int(tokens[index + 1])
            if points < 1:
                raise ValueError("训练点数必须是正整数")
            assignments.append((tokens[index], points))
        return tuple(assignments)

    def _parse_equip_assignments(
        self,
        args: str,
    ) -> tuple[tuple[int, str], ...]:
        tokens = str(args or "").split()
        if len(tokens) == 1 and tokens[0].isdigit():
            return ((int(tokens[0]), ""),)
        if not tokens or len(tokens) % 2:
            raise ValueError("用法：/穿戴 装备ID 槽位 [装备ID 槽位...]")
        assignments = []
        for index in range(0, len(tokens), 2):
            if not tokens[index].isdigit():
                raise ValueError("装备ID必须是正整数")
            assignments.append(
                (int(tokens[index]), self._slot_id(tokens[index + 1]))
            )
        if len({item_id for item_id, _ in assignments}) != len(assignments):
            raise ValueError("穿戴列表中有重复装备")
        return tuple(assignments)

    @staticmethod
    def _parse_skill_slot_assignments(
        args: str,
    ) -> tuple[tuple[int, str], ...]:
        tokens = str(args or "").split()
        if not tokens or len(tokens) % 2:
            raise ValueError("用法：/技能栏 位置 技能名 [位置 技能名...]")
        assignments = []
        for index in range(0, len(tokens), 2):
            if not tokens[index].isdigit():
                raise ValueError("技能栏位置必须是1到4")
            assignments.append((int(tokens[index]), tokens[index + 1]))
        return tuple(assignments)

    async def _own_user(self, event):
        error = await self._registration_error(event)
        if error: raise ValueError(error)
        return await self.user_service.get_or_create_user(self._identity_from_event(event))

    async def _combat_build(self, user, slots=None, items=None):
        if not self.equipment_service or not self.skill_service or not self.build_service:
            return None
        if slots is None: slots, items = await self.equipment_service.get_loadout(user.id)
        skills, _ = await self.skill_service.get_skills(user)
        return self.build_service.resolve_equipment(user, slots, items, skills)

    def _slot_id(self, value: str) -> str:
        aliases = {label: slot for slot, label in SLOT_LABELS.items()}; aliases.update({"手": "main_hand", "主手": "main_hand", "副手": "off_hand", "左指": "left_finger", "右指": "right_finger"})
        result = aliases.get((value or "").strip(), (value or "").strip())
        if result not in SLOT_LABELS: raise ValueError("未知装备槽")
        return result

    def _format_affixes(self, affixes) -> str:
        if not affixes:
            return "无"
        labels = {
            "skill_level": "技能等级", "block_rate": "格挡",
            "knockback_resistance": "击退抗性", "melee_followup": "追打",
            "ranged_followup": "追射", "element_resistance": "元素耐性",
            "status_immunity": "异常免疫", "spell_power": "法术增益",
            "armor_penetration": "护甲穿透", "life_steal": "生命汲取",
            "stamina_steal": "耐力汲取", "mana_steal": "魔力汲取",
            "execute_chance": "斩首概率",
            "status_resistance": "负面状态抗性",
            "status_resistance_paralysis": "麻痹抗性",
            "status_resistance_confusion": "混乱抗性",
            "max_hp": "最大生命",
            "accuracy": "命中", "evasion": "回避",
            "critical_rate": "暴击率", "critical_damage": "暴击伤害",
            "physical_reduction": "物理减伤",
            "magical_reduction": "魔法减伤", "action_speed": "行动速度",
        }
        rendered = []
        for item in affixes:
            kind = str(item.get("type", ""))
            if kind == "stat_flat":
                label = ATTRIBUTE_LABELS.get(
                    str(item.get("stat", "")), "主属性"
                )
            elif kind == "advanced_stat":
                label = ADVANCED_ATTRIBUTE_LABELS.get(
                    str(item.get("stat", "")), "高级属性"
                )
            elif kind.startswith("resistance_"):
                label = DAMAGE_TYPE_LABELS.get(
                    kind.removeprefix("resistance_"), kind
                ) + "耐性"
            elif kind.startswith("damage_"):
                label = DAMAGE_TYPE_LABELS.get(
                    kind.removeprefix("damage_"), kind
                ) + "伤害"
            else:
                label = labels.get(kind, kind)
            value = item.get("value", 0)
            if isinstance(value, float):
                value = f"{value:.4f}".rstrip("0").rstrip(".")
            rendered.append(f"{label}+{value}")
        return "、".join(rendered)

    def _format_proc_affixes(self, affixes) -> str:
        if not affixes:
            return "无"
        rendered = []
        for affix in affixes:
            ability_id = str(affix.get("ability_id", ""))
            name = EQUIPMENT_PROC_NAMES.get(ability_id, ability_id)
            chance = max(0.0, min(1.0, float(affix.get("value", 0))))
            rendered.append(f"{name} {chance:.1%}")
        return "、".join(rendered)

    def _effective_inherent_affix_line(
        self,
        affixes,
        character_level: int,
        item_level: int,
        ratio: float,
    ) -> str:
        if item_level > 0 and character_level < item_level:
            percent = f"{ratio * 100:.1f}".rstrip("0").rstrip(".")
            status = (
                f"角色Lv.{character_level}/需求Lv.{item_level}，"
                f"数值比例{percent}%"
            )
            return f"固有词条（当前有效，{status}）：{self._format_affixes(affixes)}"
        return f"固有词条（当前有效）：{self._format_affixes(affixes)}"

    def _armor_style_label(self, style: str) -> str:
        return {"light": "轻甲", "medium": "中甲", "heavy": "重甲"}.get(style, style)
    def _weapon_mode_label(self, mode: str) -> str:
        return {"unarmed": "空手", "one_hand": "单持", "sword_shield": "剑盾", "dual_wield": "双持", "two_hand_melee": "双手武器", "two_hand_heavy": "重武器", "two_hand_ranged": "远程武器"}.get(mode, mode)
    async def ranking(self, event: AstrMessageEvent) -> AsyncGenerator:
        registration_error = await self._registration_error(event)
        if registration_error:
            yield await self.reply_text(event, registration_error)
            return
        try:
            target_identity = self._target_identity_from_event(event)
            if target_identity:
                result = await self.user_service.get_user_rank(target_identity)
                if not result:
                    yield await self.reply_text(event, "该用户暂无排行数据。")
                    return
                rank, user = result
                yield await self.reply_text(
                    event,
                    "\n".join(
                        [
                            "排名 用户名 等级 当前经验/升级所需经验",
                            self._format_ranking_line(rank, user),
                        ]
                    ),
                    "LevelUpPvp 排行",
                )
                return

            identity = self._identity_from_event(event)
            ranked_users = await self.user_service.get_top_users(
                identity.platform,
                identity.group_id,
                10,
            )
            if not ranked_users:
                yield await self.reply_text(event, "当前群暂无排行数据。")
                return
            lines = ["等级排行 TOP10", "排名 用户名 等级 当前经验/升级所需经验"]
            lines.extend(
                self._format_ranking_line(rank, user) for rank, user in ranked_users
            )
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 排行"
            )
        except Exception as exc:
            logger.exception("LevelUpPvp ranking failed")
            yield await self.reply_text(event, f"查看排行失败：{exc}")

    async def register_nickname(
        self,
        event: AstrMessageEvent,
        nickname: str = "",
    ) -> AsyncGenerator:
        nickname = " ".join((nickname or event.get_sender_name() or "").split())
        if not nickname:
            yield await self.reply_text(
                event,
                "当前消息未携带平台用户名，无法自动登记。",
            )
            return
        try:
            user = await self.user_service.register_nickname(
                self._identity_from_event(event),
                nickname,
            )
            yield await self.reply_text(event, f"登记成功：{self._display_name(user)}")
        except Exception as exc:
            logger.exception("LevelUpPvp nickname registration failed")
            yield await self.reply_text(event, f"登记失败：{exc}")

    async def modify_registered_nickname(
        self,
        event: AstrMessageEvent,
        nickname: str = "",
    ) -> AsyncGenerator:
        if not await self._is_astrbot_admin(event):
            yield await self.reply_text(event, ADMIN_REQUIRED_MESSAGE)
            return
        registration_error = await self._registration_error(event)
        if registration_error:
            yield await self.reply_text(event, registration_error)
            return

        target_identity = self._target_identity_from_event(
            event
        ) or self._target_identity_from_text(event, nickname)
        nickname = self._extract_modified_nickname(event, target_identity, nickname)
        if not target_identity or not nickname:
            yield await self.reply_text(event, "用法：/修改登记 @用户 昵称")
            return
        try:
            user = await self.user_service.register_nickname(target_identity, nickname)
            yield await self.reply_text(
                event, f"修改登记成功：{self._display_name(user)}"
            )
        except Exception as exc:
            logger.exception("LevelUpPvp admin nickname registration failed")
            yield await self.reply_text(event, f"修改登记失败：{exc}")

    async def challenge(self, event: AstrMessageEvent) -> AsyncGenerator:
        registration_error = await self._registration_error(event)
        if registration_error:
            yield await self.reply_text(event, registration_error)
            return
        try:
            target_identity = self._target_identity_from_event(event)
            if not target_identity:
                # No @target: try to parse as a PvE dungeon challenge.
                if self.dungeon_service is not None:
                    async for result in self._try_dungeon_challenge(event):
                        yield result
                    return
                yield await self.reply_text(
                    event, "请 At 一个要挑战的用户。用法：/挑战 @用户 策略描述"
                )
                return
            if self._is_bot_target_id(event, target_identity.user_id):
                yield await self.reply_text(event, "不能挑战机器人。")
                return

            parsed_strategy = self._extract_strategy(event, target_identity, "")
            battle_args = (
                self._identity_from_event(event),
                target_identity,
                parsed_strategy,
            )
            if self.challenge_queue is None:
                result = await self.battle_service.battle(
                    *battle_args,
                    context=self.context,
                    event=event,
                )
            else:
                ticket = await self.challenge_queue.enqueue(
                    *battle_args,
                    context=self.context,
                    event=event,
                )
                result = await ticket.result()
            yield await self._battle_result(event, result)
        except Exception as exc:
            logger.exception("LevelUpPvp battle failed")
            yield await self.reply_text(event, f"挑战失败：{exc}")

    async def tactics(
        self,
        event: AstrMessageEvent,
        args: str = "",
    ) -> AsyncGenerator:
        """View or update the persistent opening/midgame/endgame tactic plan."""

        registration_error = await self._registration_error(event)
        if registration_error:
            yield await self.reply_text(event, registration_error)
            return
        try:
            identity = self._identity_from_event(event)
            raw = str(args or "").strip()
            if not raw:
                plan = await self.battle_service.get_tactic_plan(identity)
                summary = self.battle_service.tactic_loadout_service.format_plan(
                    plan
                )
                yield await self.reply_text(
                    event,
                    "当前三阶段战术：\n"
                    f"{summary}\n"
                    "六大战术：压制、反制、游击、控制、坚守、奇策。\n"
                    "设置：/战术 开局 中盘 终盘\n"
                    "例：/战术 游击 控制 奇策",
                    "LevelUpPvp 战术",
                )
                return
            tokens = [
                token
                for token in re.split(r"[\s,/|｜]+", raw)
                if token
            ]
            if len(tokens) != 3:
                raise ValueError(
                    "请依次填写开局、中盘、终盘三个战术，"
                    "例如：/战术 游击 控制 奇策"
                )
            plan = await self.battle_service.set_tactic_plan(
                identity,
                tokens[0],
                tokens[1],
                tokens[2],
            )
            summary = self.battle_service.tactic_loadout_service.format_plan(
                plan
            )
            yield await self.reply_text(
                event,
                "战术方案已保存。以后进攻未临时指定策略、以及被挑战时，"
                "都会使用它：\n"
                f"{summary}",
                "LevelUpPvp 战术",
            )
        except Exception as exc:
            logger.exception("LevelUpPvp tactic plan failed")
            yield await self.reply_text(event, f"战术设置失败：{exc}")

    async def operations(
        self,
        event: AstrMessageEvent,
        args: str = "",
    ) -> AsyncGenerator:
        """Show the group's rotating v11 content or settle completed bundles."""

        if self.operation_service is None:
            yield await self.reply_text(event, "今日运营功能未启用。")
            return
        registration_error = await self._registration_error(event)
        if registration_error:
            yield await self.reply_text(event, registration_error)
            return
        try:
            user = await self._own_user(event)
            group_id = str(event.get_group_id() or user.group_id or "global")
            action = str(args or "").strip()
            if action in {"领取", "领奖", "claim"}:
                lines = ["运营奖励结算："]
                for label, claim in (
                    (
                        "每日",
                        await self.operation_service.claim_daily_reward(
                            user_pk=user.id,
                            group_id=group_id,
                        ),
                    ),
                    (
                        "每周",
                        await self.operation_service.claim_weekly_reward(
                            user_pk=user.id,
                            group_id=group_id,
                        ),
                    ),
                ):
                    if not claim.eligible:
                        lines.append(
                            f"{label}：进度 {claim.completed_count}/"
                            f"{claim.required_count}，尚未完成"
                        )
                        continue
                    if (
                        claim.reward_intent is None
                        or self.operation_settlement_service is None
                    ):
                        lines.append(f"{label}：奖励已预留，等待结算服务")
                        continue
                    settled = await self.operation_settlement_service.settle(
                        user_pk=user.id,
                        intent=claim.reward_intent,
                    )
                    if label == "每日":
                        await self._record_daily_reward_progress(
                            user,
                            group_id,
                            claim.reward_intent.reward_key,
                        )
                    if not settled.applied:
                        lines.append(f"{label}：已经领取过，不会重复发放")
                        continue
                    reward_parts = []
                    if settled.scrap:
                        reward_parts.append(f"工坊碎片 +{settled.scrap}")
                    if settled.season_tokens:
                        reward_parts.append(f"赛季币 +{settled.season_tokens}")
                    if settled.experience:
                        reward_parts.append(f"经验 +{settled.experience}")
                    if settled.equipment:
                        reward_parts.append(
                            "装备 "
                            + "、".join(item.name for item in settled.equipment)
                        )
                    lines.append(f"{label}：" + "，".join(reward_parts))
                yield await self.reply_text(
                    event,
                    "\n".join(lines),
                    "LevelUpPvp 运营奖励",
                )
                return

            overview = await self.operation_service.overview(
                user_pk=user.id,
                group_id=group_id,
            )
            if action in {"周常", "周", "weekly"}:
                lines = self._format_weekly_operations(overview)
            elif action in {"赛季", "season"}:
                lines = self._format_season(overview)
            else:
                dungeon = None
                if self.dungeon_service is not None:
                    dungeon = self._default_nefia_dungeon(
                        user.level,
                        group_id,
                        overview.periods.daily.key,
                    )
                lines = self._format_daily_operations(overview, dungeon)
            yield await self.reply_text(
                event,
                "\n".join(lines),
                "LevelUpPvp 今日运营",
            )
        except Exception as exc:
            logger.exception("LevelUpPvp operations failed")
            yield await self.reply_text(event, f"查看今日运营失败：{exc}")

    async def replay(
        self,
        event: AstrMessageEvent,
        args: str = "",
    ) -> AsyncGenerator:
        """Explain the latest group battle, or one explicitly numbered battle."""

        if self.replay_service is None:
            yield await self.reply_text(event, "战斗复盘功能未启用。")
            return
        registration_error = await self._registration_error(event)
        if registration_error:
            yield await self.reply_text(event, registration_error)
            return
        try:
            raw = str(args or "").strip()
            if raw and (not raw.isdigit() or int(raw) <= 0):
                raise ValueError("用法：/复盘 [战斗ID]")
            identity = self._identity_from_event(event)
            battle_id = int(raw) if raw else None
            view = await self.replay_service.get_replay(
                identity,
                battle_id=battle_id,
            )
            if view is None:
                yield await self.reply_text(
                    event,
                    "没有找到可查看的战斗记录。先和群友打一场吧。",
                )
                return
            view_group_id = str(
                getattr(view, "group_id", identity.group_id) or ""
            )
            if view_group_id == identity.group_id:
                user = await self.user_service.get_or_create_user(identity)
                await self._record_replay_progress(user, view.battle_id)
            yield await self.reply_text(
                event,
                self.replay_service.format_replay(view),
                f"LevelUpPvp 复盘 #{view.battle_id}",
            )
        except Exception as exc:
            logger.exception("LevelUpPvp replay failed")
            yield await self.reply_text(event, f"查看复盘失败：{exc}")

    async def workshop(
        self,
        event: AstrMessageEvent,
        args: str = "",
    ) -> AsyncGenerator:
        """Salvage dead drops or preview/decide a directed affix rework."""

        if self.workshop_service is None:
            yield await self.reply_text(event, "工坊功能未启用。")
            return
        registration_error = await self._registration_error(event)
        if registration_error:
            yield await self.reply_text(event, registration_error)
            return
        try:
            user = await self._own_user(event)
            tokens = str(args or "").strip().split()
            if not tokens:
                wallet = await self.workshop_service.wallet(user.id)
                yield await self.reply_text(
                    event,
                    "装备工坊\n"
                    f"碎片：{wallet.scrap_balance}（累计获得 "
                    f"{wallet.lifetime_earned} / 消耗 {wallet.lifetime_spent}）\n"
                    f"赛季币：{wallet.season_tokens}\n"
                    "分解：/工坊 分解 装备ID\n"
                    "收藏：/工坊 收藏 装备ID；取消：/工坊 取消收藏 装备ID\n"
                    "普通整理：/工坊 整理 普通\n"
                    "快速整理：/工坊 整理 优秀（普通+优秀，需二次确认）\n"
                    "安全整理：/工坊 整理 支配（同槽同方向完全更弱才入选）\n"
                    "预览：/工坊 重铸 装备ID 力量|灵巧|射击|奥术|防御|奇运\n"
                    "刻印：/工坊 刻印 装备ID 方向（额外20赛季币，必出目标词条）\n"
                    "决定：/工坊 接受 装备ID  或  /工坊 放弃 装备ID\n"
                    "重铸先扣碎片再展示候选；放弃不会返还费用。",
                    "LevelUpPvp 工坊",
                )
                return
            action = tokens[0]
            if action == "分解" and len(tokens) == 2:
                result = await self.workshop_service.salvage(
                    user.id,
                    int(tokens[1]),
                )
                await self._record_workshop_progress(
                    user,
                    f"salvage:{result.equipment_id}",
                )
                text = (
                    f"已分解「{result.equipment_name}」"
                    f"（Lv.{result.item_level} {result.quality}）\n"
                    f"碎片 +{result.scrap_gained}，当前 {result.balance_after}"
                )
            elif action in {"收藏", "锁定"} and len(tokens) == 2:
                item = await self.equipment_service.set_item_locked(
                    user.id,
                    int(tokens[1]),
                    True,
                )
                text = (
                    f"已收藏锁定 #{item.id}「{item.name}」。\n"
                    "所有批量整理和单件分解都会跳过它。"
                )
            elif action in {"取消收藏", "解锁"} and len(tokens) == 2:
                item = await self.equipment_service.set_item_locked(
                    user.id,
                    int(tokens[1]),
                    False,
                )
                text = f"已取消收藏 #{item.id}「{item.name}」。"
            elif action == "整理" and len(tokens) == 2:
                result = await self.workshop_service.preview_bulk_salvage(
                    user.id,
                    tokens[1],
                )
                if result.policy_id == "dominated":
                    examples = []
                    for item in result.dominated_items[:8]:
                        quality = QUALITY_LABELS.get(item.quality, item.quality)
                        keeper_quality = QUALITY_LABELS.get(
                            item.keeper_quality,
                            item.keeper_quality,
                        )
                        directions = "、".join(item.direction_labels)
                        examples.append(
                            f"#{item.equipment_id} {quality}{item.equipment_name} "
                            f"Lv.{item.item_level} → 保留#{item.keeper_id} "
                            f"{keeper_quality}{item.keeper_name} Lv.{item.keeper_level}"
                            f"〔{item.slot_label}/{directions}〕"
                        )
                    if result.item_count > 8:
                        examples.append(f"……另有{result.item_count - 8}件")
                    text = (
                        f"安全整理预览：{result.item_count}件被完全支配装备\n"
                        + "\n".join(examples)
                        + f"\n预计碎片 +{result.scrap_total}\n"
                        "仅纳入普通/优秀/精良的普通星装备；已装备、收藏锁定、"
                        "重铸候选、史诗/神话（传说）、白星/黑星及特殊效果装备"
                        "均受保护。\n"
                        "优秀或精良也只会在本次清单与确认码完全一致时分解。\n"
                        f"确认请输入 /工坊 批量分解 支配 "
                        f"{result.confirmation_token}"
                    )
                elif result.policy_id == "excellent":
                    examples = "、".join(
                        f"#{item_id} {name} Lv.{level}"
                        for item_id, name, level in result.items[:8]
                    )
                    if result.item_count > 8:
                        examples += f"……另有{result.item_count - 8}件"
                    equipment_ids = "、".join(
                        f"#{item_id}"
                        for item_id, _name, _level in result.items
                    )
                    text = (
                        f"优秀及以下整理预览：{result.item_count}件普通/优秀装备\n"
                        f"{examples}\n"
                        f"将分解ID：{equipment_ids}\n"
                        f"预计碎片 +{result.scrap_total}\n"
                        "这是按品质全量清理，不比较装备数值或构筑；"
                        "请先用 /工坊 收藏 ID 保护想保留的装备。\n"
                        "仅纳入未穿戴、未收藏、无待定重铸、未强化、"
                        "普通星且祝福状态普通、无特殊资料效果或触发能力的"
                        "普通/优秀装备；精良及以上、白星/黑星均受保护。\n"
                        "确认时会重新核对完整装备快照；任何词条、状态或背包"
                        "变化都会使本确认码失效。\n"
                        f"确认请输入 /工坊 批量分解 优秀 "
                        f"{result.confirmation_token}"
                    )
                else:
                    examples = "、".join(
                        f"#{item_id} {name} Lv.{level}"
                        for item_id, name, level in result.items[:8]
                    )
                    if result.item_count > 8:
                        examples += f"……另有{result.item_count - 8}件"
                    text = (
                        f"整理预览：{result.item_count}件未穿戴普通装备\n"
                        f"{examples}\n"
                        f"预计碎片 +{result.scrap_total}\n"
                        "不会处理新手装、已穿戴装备、收藏锁定装备、"
                        "重铸候选、白星/黑星或优秀及以上装备。\n"
                        f"确认请输入 /工坊 批量分解 普通 "
                        f"{result.confirmation_token}"
                    )
            elif action == "批量分解" and len(tokens) == 3:
                result = await self.workshop_service.bulk_salvage(
                    user.id,
                    tokens[1],
                    tokens[2],
                )
                await self._record_workshop_progress(
                    user,
                    "bulk_salvage:"
                    f"{result.quality}:{result.equipment_ids[0]}:"
                    f"{result.equipment_ids[-1]}",
                )
                text = (
                    f"已批量分解 {result.item_count} 件"
                    f"{self._bulk_salvage_result_label(result.quality)}\n"
                    f"碎片 +{result.scrap_gained}，当前 {result.balance_after}"
                )
            elif action == "重铸" and len(tokens) == 3:
                result = await self.workshop_service.preview_rework(
                    user.id,
                    int(tokens[1]),
                    tokens[2],
                )
                wallet = await self.workshop_service.wallet(user.id)
                await self._record_workshop_progress(
                    user,
                    f"rework:{result.equipment_id}:{wallet.lifetime_spent}",
                )
                pity = "（本次触发第5次定向保底）" if result.pity_guaranteed else ""
                text = (
                    f"「{result.equipment_name}」{result.direction_label}重铸候选{pity}\n"
                    f"词条：{self._format_affixes(result.candidate_affixes)}\n"
                    f"方向匹配：{result.match_score}%\n"
                    f"消耗：{result.cost.quality_base}+{result.cost.level_surcharge}"
                    f"={result.cost.total} 碎片；余额 {result.balance_after}\n"
                    f"输入 /工坊 接受 {result.equipment_id} 或 "
                    f"/工坊 放弃 {result.equipment_id}"
                )
            elif action == "刻印" and len(tokens) == 3:
                result = await self.workshop_service.preview_season_rework(
                    user.id,
                    int(tokens[1]),
                    tokens[2],
                )
                wallet = await self.workshop_service.wallet(user.id)
                await self._record_workshop_progress(
                    user,
                    f"season_imprint:{result.equipment_id}:"
                    f"{wallet.lifetime_spent}",
                )
                text = (
                    f"「{result.equipment_name}」{result.direction_label}赛季刻印候选"
                    "（至少1条目标方向词条）\n"
                    f"词条：{self._format_affixes(result.candidate_affixes)}\n"
                    f"方向匹配：{result.match_score}%\n"
                    f"消耗：{result.cost.total} 碎片 + "
                    f"{result.cost.season_tokens} 赛季币；"
                    f"余额 {result.balance_after} 碎片 / "
                    f"{result.season_tokens_after} 赛季币\n"
                    f"输入 /工坊 接受 {result.equipment_id} 或 "
                    f"/工坊 放弃 {result.equipment_id}"
                )
            elif action in {"接受", "放弃"} and len(tokens) == 2:
                accept = action == "接受"
                result = await self.workshop_service.decide_rework(
                    user.id,
                    int(tokens[1]),
                    accept,
                )
                text = (
                    ("已采用新词条" if accept else "已放弃候选，原词条保持不变")
                    + f"：{self._format_affixes(result.item.random_affixes)}\n"
                    + f"碎片余额：{result.balance} / "
                    + f"赛季币：{result.season_tokens_balance}"
                )
            else:
                raise ValueError(
                    "用法：/工坊 分解 ID；/工坊 收藏|取消收藏 ID；"
                    "/工坊 整理 普通|优秀|支配；"
                    "/工坊 批量分解 普通|优秀|支配 确认码；"
                    "/工坊 重铸 ID 方向；"
                    "/工坊 刻印 ID 方向；"
                    "/工坊 接受 ID；/工坊 放弃 ID"
                )
            yield await self.reply_text(event, text, "LevelUpPvp 工坊")
        except Exception as exc:
            logger.exception("LevelUpPvp workshop failed")
            yield await self.reply_text(event, f"工坊操作失败：{exc}")

    async def _record_workshop_progress(self, user, event_key: str) -> None:
        if self.operation_service is None:
            return
        try:
            await self.operation_service.record_event(
                user_pk=user.id,
                group_id=user.group_id or "global",
                event_type="workshop_action",
                event_key=f"workshop:{event_key}",
            )
        except Exception:
            logger.exception("Workshop succeeded but operation progress failed")

    @staticmethod
    def _bulk_salvage_result_label(policy_id: str) -> str:
        return {
            "common": "普通装备",
            "excellent": "普通/优秀装备",
            "dominated": "被支配装备",
        }.get(str(policy_id), "装备")

    async def _record_replay_progress(self, user, battle_id: int) -> None:
        if self.operation_service is None:
            return
        try:
            await self.operation_service.record_event(
                user_pk=user.id,
                group_id=user.group_id or "global",
                event_type="battle_review",
                event_key=f"review:{int(battle_id)}",
            )
        except Exception:
            logger.exception("Replay succeeded but operation progress failed")

    async def _record_daily_reward_progress(
        self,
        user,
        group_id: str,
        reward_key: str,
    ) -> None:
        if self.operation_service is None:
            return
        try:
            await self.operation_service.record_event(
                user_pk=user.id,
                group_id=group_id,
                event_type="daily_reward",
                event_key=f"settled:{reward_key}",
            )
        except Exception:
            logger.exception(
                "Daily reward settled but weekly operation progress failed"
            )

    @staticmethod
    def _format_effects(effects) -> str:
        return "；".join(
            f"{effect.label}{effect.cap_text}" for effect in effects
        ) or "无额外修正"

    def _format_daily_operations(self, overview, dungeon=None) -> list[str]:
        progress_by_id = {
            state.task_id: state
            for state in getattr(overview, "daily_task_states", ())
        }
        theme_line = (
            f"今日全群共享奈菲亚：「{dungeon.name}」。"
            "敌人与奖励会按个人等级和所选难度缩放。"
            if dungeon is not None
            else "今日全群共享奈菲亚已经开放，敌人与奖励按个人等级缩放。"
        )
        lines = [
            f"今日冒险与委托 · {overview.periods.daily.key}",
            theme_line,
            "输入 /奈菲亚 开始探索；每层选择路线与风险，途中可撤退并"
            "保留已锁定收获。每日04:00更换共享主题。",
            f"每日委托 {overview.daily_completed}/2（3项任选2项）：",
        ]
        for task in overview.daily_tasks:
            state = progress_by_id.get(task.task_id)
            progress = state.progress if state is not None else 0
            completed = bool(state is not None and state.completed)
            lines.append(
                f"- {'✓' if completed else '·'} {task.name}："
                f"{task.description}（{progress}/{task.target}）"
            )
        lines.extend(
            (
                "已领取" if overview.daily_claimed else "完成后输入 /今日 领取",
                "查看周目标：/周常　查看赛季：/赛季",
            )
        )
        return lines

    @staticmethod
    def _format_weekly_operations(overview) -> list[str]:
        progress_by_id = {
            state.task_id: state
            for state in getattr(overview, "weekly_task_states", ())
        }
        lines = [
            f"本周目标 · {overview.periods.weekly.key}",
            f"完成 {overview.weekly_completed}/5（7项任选5项即拿满）：",
        ]
        for task in overview.weekly_tasks:
            state = progress_by_id.get(task.task_id)
            progress = state.progress if state is not None else 0
            completed = bool(state is not None and state.completed)
            lines.append(
                f"- {'✓' if completed else '·'} {task.name}："
                f"{task.description}（{progress}/{task.target}）"
            )
        simulation = overview.weekly_simulation
        best = "、".join(map(str, simulation.best_scores)) or "暂无"
        lines.extend(
            (
                f"周战斗评分：前{simulation.attempts_limit}场有效对战取最佳2场；"
                f"已记录 {simulation.attempts_used}/{simulation.attempts_limit}，"
                f"最佳：{best}",
                "已领取" if overview.weekly_claimed else "完成后输入 /今日 领取",
            )
        )
        return lines

    @staticmethod
    def _format_season(overview) -> list[str]:
        season = overview.season
        rating = "尚未定级" if season.rating is None else str(season.rating)
        return [
            f"赛季 {season.key}",
            f"第 {season.day_number}/{season.total_days} 天 · 状态 {season.status}",
            f"评级 {rating} · {season.games} 场 {season.wins}胜/{season.losses}负",
            "评级每28天轮换；每日同一对手首战、且等级差不超过10级时计入Elo。",
        ]

    async def _try_dungeon_challenge(self, event: AstrMessageEvent) -> AsyncGenerator:
        """Attempt to parse the challenge text as a dungeon name + strategy."""
        message = (event.get_message_str() or "").strip()
        message = CHALLENGE_COMMAND_PATTERN.sub("", message).strip()
        message = MENTION_MARKUP_PATTERN.sub(" ", message).strip()
        tokens = message.split()
        if not tokens:
            yield await self.reply_text(
                event,
                "用法：/挑战 @用户 策略  或  /挑战 副本名 策略",
            )
            return
        dungeon = self.dungeon_service.get_dungeon_by_name(tokens[0])
        if dungeon is None:
            yield await self.reply_text(
                event,
                f"未知副本：{tokens[0]}。可用 /副本 查看所有副本。",
            )
            return
        strategy = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        identity = self._identity_from_event(event)
        result = await self.dungeon_service.run_dungeon(
            identity, dungeon.dungeon_id, strategy
        )
        yield await self._dungeon_result(event, result)

    async def nefia(
        self,
        event: AstrMessageEvent,
        args: str = "",
    ) -> AsyncGenerator:
        """Run one compact, persistent random-Nefia decision at a time."""

        if self.dungeon_service is None:
            yield await self.reply_text(event, "奈菲亚功能未启用。")
            return
        try:
            identity = self._identity_from_event(event)
            user = await self.user_service.get_or_create_user(identity)
            tokens = str(args or "").strip().split()
            action = tokens[0] if tokens else ""
            difficulty = 1
            strategy = "稳扎稳打"
            cycle_key = daily_growth_day_window(
                self._chat_event_timestamp(event)
            )[0]

            current, dungeon = await self._find_current_nefia(identity)
            if action == "开始":
                remaining = tokens[1:]
                if remaining and remaining[0].isdigit():
                    difficulty = int(remaining.pop(0))
                if remaining:
                    strategy = " ".join(remaining)
                if current is None:
                    dungeon = self._default_nefia_dungeon(
                        user.level,
                        identity.group_id,
                        cycle_key,
                    )
                    current = await self.dungeon_service.start_nefia(
                        identity,
                        dungeon.dungeon_id,
                        difficulty,
                        strategy,
                    )
            elif current is None and action not in {"撤退", "战斗", "继续"}:
                dungeon = self._default_nefia_dungeon(
                    user.level,
                    identity.group_id,
                    cycle_key,
                )
                current = await self.dungeon_service.start_nefia(
                    identity,
                    dungeon.dungeon_id,
                    difficulty,
                    strategy,
                )

            if current is None or dungeon is None:
                yield await self.reply_text(
                    event,
                    "今天尚未进入奈菲亚。输入 /奈菲亚 开始 1 开启冒险。",
                )
                return

            if action == "撤退":
                retreat_view = current.view
                current = await self.dungeon_service.retreat_nefia(
                    identity, current.view.adventure_id
                )
                if retreat_view.selected_risk_id:
                    await self._record_nefia_risk_progress(
                        user,
                        retreat_view.adventure_id,
                        retreat_view.floor_number,
                        retreat_view.selected_risk_id,
                    )
            elif action in {"战斗", "继续"}:
                if current.view.phase != "combat_ready":
                    raise ValueError("当前还没有锁定路线与风险，请按页面选择 1A～2B")
                floor_number = current.view.floor_number
                route = self._selected_nefia_route(current.view)
                risk = self._selected_nefia_risk(current.view, route)
                current = await self.dungeon_service.fight_nefia(
                    identity, current.view.adventure_id
                )
                await self._record_nefia_risk_progress(
                    user,
                    current.view.adventure_id,
                    floor_number,
                    risk.risk_id,
                )
                await self._record_nefia_fight_progress(
                    user, current, route, risk, floor_number
                )
            elif action and action != "开始":
                floor_number = current.view.floor_number
                current, route, risk, _ = await self._advance_nefia_choice(
                    identity, current, action
                )
                await self._record_nefia_risk_progress(
                    user,
                    current.view.adventure_id,
                    floor_number,
                    risk.risk_id,
                )
                await self._record_nefia_fight_progress(
                    user, current, route, risk, floor_number
                )

            yield await self.reply_text(
                event,
                self._format_nefia_application(current, dungeon),
                "LevelUpPvp 随机奈菲亚",
            )
        except Exception as exc:
            logger.exception("LevelUpPvp Nefia command failed")
            yield await self.reply_text(event, f"奈菲亚行动失败：{exc}")

    async def _find_current_nefia(self, identity):
        terminal = None
        for dungeon in self.dungeon_service.list_dungeons():
            try:
                result = await self.dungeon_service.view_nefia(
                    identity, dungeon_id=dungeon.dungeon_id
                )
            except KeyError:
                continue
            if not result.view.terminal:
                return result, dungeon
            terminal = terminal or (result, dungeon)
        return terminal or (None, None)

    def _default_nefia_dungeon(
        self,
        level: int,
        group_id: str = "",
        cycle_key: str | None = None,
    ):
        dungeons = tuple(self.dungeon_service.list_dungeons())
        if not dungeons:
            raise ValueError("随机奈菲亚目录为空")
        activity_day = cycle_key or daily_growth_day_window()[0]
        group_key = str(group_id or "global")
        return max(
            dungeons,
            key=lambda dungeon: (
                stable_operation_seed(
                    "nefia-theme-v12",
                    group_key,
                    activity_day,
                    dungeon.dungeon_id,
                ),
                str(dungeon.dungeon_id),
            ),
        )

    async def _advance_nefia_choice(self, identity, result, token: str):
        view = result.view
        compact = re.fullmatch(r"([12])([AaBb])", token)
        risk_only = re.fullmatch(r"([AaBb])", token)
        if compact:
            route_index = int(compact.group(1)) - 1
            risk_index = 0 if compact.group(2).casefold() == "a" else 1
            if route_index >= len(view.routes):
                raise ValueError("当前层没有这条路线")
            route = view.routes[route_index]
        elif risk_only and view.phase == "risk_choice":
            route = self._selected_nefia_route(view)
            risk_index = 0 if risk_only.group(1).casefold() == "a" else 1
        else:
            raise ValueError("请选择 1A、1B、2A 或 2B；已选路线时也可只输入 A/B")

        if risk_index >= len(route.risk_choices):
            raise ValueError("当前路线没有这个风险选项")
        risk = route.risk_choices[risk_index]
        if view.phase == "route_choice":
            result = await self.dungeon_service.choose_nefia_route(
                identity, view.adventure_id, route.option_id
            )
            view = result.view
        if view.phase == "risk_choice":
            if view.selected_route_id != route.option_id:
                raise ValueError("本层路线已经锁定，请按当前页面选择 A 或 B")
            result = await self.dungeon_service.choose_nefia_risk(
                identity, view.adventure_id, risk.risk_id
            )
            view = result.view
            risk_chosen = True
        else:
            risk_chosen = False
        if view.phase != "combat_ready":
            raise ValueError("当前冒险不在可结算阶段")
        if (
            view.selected_route_id != route.option_id
            or view.selected_risk_id != risk.risk_id
        ):
            raise ValueError("本层路线或风险已经锁定为其他选项")
        result = await self.dungeon_service.fight_nefia(
            identity, view.adventure_id
        )
        return result, route, risk, risk_chosen

    @staticmethod
    def _selected_nefia_route(view):
        route = next(
            (
                item
                for item in view.routes
                if item.option_id == view.selected_route_id
            ),
            None,
        )
        if route is None:
            raise ValueError("奈菲亚已选路线状态损坏")
        return route

    @staticmethod
    def _selected_nefia_risk(view, route):
        risk = next(
            (
                item
                for item in route.risk_choices
                if item.risk_id == view.selected_risk_id
            ),
            None,
        )
        if risk is None:
            raise ValueError("奈菲亚已选风险状态损坏")
        return risk

    async def _record_nefia_risk_progress(
        self,
        user,
        adventure_id: str,
        floor_number: int,
        risk_id: str,
    ) -> None:
        if self.operation_service is None:
            return
        common = {
            "user_pk": user.id,
            "group_id": user.group_id or "global",
        }
        for event_type, event_key in (
            (
                "risk_choice",
                f"nefia:{adventure_id}:floor:{floor_number}:risk",
            ),
            ("risk_choice_unique", f"risk:{risk_id}"),
        ):
            try:
                await self.operation_service.record_event(
                    **common,
                    event_type=event_type,
                    event_key=event_key,
                )
            except Exception:
                logger.exception(
                    "Nefia risk succeeded but %s operation progress failed",
                    event_type,
                )

    async def _record_nefia_fight_progress(
        self, user, result, route, risk, floor_number: int
    ) -> None:
        if self.operation_service is None:
            return
        simulation = result.simulation
        prefix = f"nefia:{result.view.adventure_id}:floor:{max(1, int(floor_number))}"
        common = {
            "user_pk": user.id,
            "group_id": user.group_id or "global",
        }
        event_suffix = "fight" if simulation is not None else "event"
        events = [("nefia_node", f"{prefix}:{event_suffix}", 1)]
        if simulation is None:
            events.append(("nefia_discovery", f"{prefix}:discovery", 1))
        else:
            if route.node_kind in {"elite", "boss"}:
                events.append(("boss_attempt", f"{prefix}:boss-attempt", 1))
            if route.node_kind == "boss" and simulation.winner_pk == user.id:
                events.append(("nefia_boss_clear", f"{prefix}:boss-clear", 1))
            active_uses = sum(
                event.actor_pk == user.id
                and event.kind in {"skill_use", "spell_cast_start"}
                for event in simulation.events
            )
            if active_uses:
                events.append(("active_skill", f"{prefix}:active-skill", active_uses))
            for event_type, event_kind in (
                ("spell_cast", "spell_cast"),
                ("guard_action", "guard"),
                ("fortune_trigger", "fortune_swing"),
            ):
                count = sum(
                    event.actor_pk == user.id and event.kind == event_kind
                    for event in simulation.events
                )
                if count:
                    events.append((event_type, f"{prefix}:{event_type}", count))
            tactic_events = [
                event
                for event in simulation.events
                if event.actor_pk == user.id and event.kind == "strategy_trigger"
            ]
            if any(event.skill_id == "endgame" for event in tactic_events):
                events.append(("combat_endgame", f"{prefix}:endgame", 1))
            for family in {
                event.status_id for event in tactic_events if event.status_id
            }:
                events.append(("stance_unique", f"stance:{family}", 1))
            events.append(
                (
                    "environment_unique",
                    f"environment:{simulation.environment_id}",
                    1,
                )
            )
            if simulation.winner_pk == user.id:
                events.append(("battle_win", f"{prefix}:win", 1))
        for event_type, event_key, amount in events:
            try:
                await self.operation_service.record_event(
                    **common,
                    event_type=event_type,
                    event_key=event_key,
                    amount=amount,
                )
            except Exception:
                logger.exception(
                    "Nefia fight succeeded but %s operation progress failed",
                    event_type,
                )

    def _format_nefia_application(self, result, dungeon) -> str:
        view = result.view
        phase_labels = {
            "route_choice": "选择路线",
            "risk_choice": "选择风险",
            "combat_ready": "等待结算",
            "cleared": "通关",
            "defeated": "战败",
            "retreated": "已撤退",
        }
        lines = [
            f"随机奈菲亚「{dungeon.name}」· {view.floor_number}/{view.floor_count}层",
            f"难度{view.difficulty} · {phase_labels.get(view.phase, view.phase)} · "
            f"已通过{view.completed_floors}层",
        ]
        if not view.terminal:
            lines.append(
                f"冒险资源：HP {view.hp_ratio:.0%} / MP {view.mana_ratio:.0%} / "
                f"SP {view.stamina_ratio:.0%}"
            )
            equipment_pity = (
                "下个成功节点必出"
                if view.equipment_misses >= 2
                else f"{view.equipment_misses}/2"
            )
            spellbook_pity = (
                "下个成功节点必出"
                if view.spellbook_misses >= 3
                else f"{view.spellbook_misses}/3"
            )
            lines.append(
                f"发现保底：装备 {equipment_pity} / 魔法书 {spellbook_pity}"
            )
        if result.simulation is not None:
            lines.append("本层战报：")
            lines.extend(
                f"- {line}" for line in BattleReportBuilder().build(result.simulation)
            )
        elif result.narrative:
            lines.append(f"本层事件：{result.narrative}")
        if result.rewards:
            lines.append("本层收获：")
            for reward in result.rewards:
                if reward.reward_type == "equipment" and reward.equipment_ids:
                    labels = "、".join(
                        f"#{item_id} {name}"
                        for item_id, name in zip(
                            reward.equipment_ids, reward.equipment_names
                        )
                    )
                    lines.append(f"- 装备：{labels}")
                else:
                    lines.append(f"- {reward.description}")
            if any(reward.spell_ids for reward in result.rewards):
                lines.append("  用 /魔法书 查看编号，再用 /阅读 编号或法术名 研读。")
            if any(reward.equipment_ids for reward in result.rewards):
                lines.append("  可用 /一键穿戴 尝试换装，闲置装备可进工坊分解。")
        growth = []
        if result.skill_growth_count:
            growth.append(f"技能{result.skill_growth_count}项")
        if result.spell_growth_count:
            growth.append(f"法术{result.spell_growth_count}项")
        if result.attribute_growth_count:
            growth.append(f"属性{result.attribute_growth_count}项")
        if growth:
            lines.append("行动成长：" + "、".join(growth))

        if view.phase == "route_choice":
            lines.append("选择一组路线与风险，本层会自动结算：")
            rank_labels = {
                "normal": "普通战斗",
                "elite": "精英战斗",
                "boss": "首领战斗",
                "camp": "营地",
                "remains": "遗骸",
                "gathering": "采集",
                "hidden_room": "隐藏房",
                "treasure": "宝箱",
            }
            for route_index, route in enumerate(view.routes, 1):
                affixes = "、".join(route.affix_names) or "无词缀"
                access = (
                    "能力解法可用"
                    if route.discovery_accessible
                    else "能力不足，仅基础收益"
                )
                if route.requires_combat:
                    route_detail = (
                        f"{rank_labels.get(route.node_kind, route.node_kind)} "
                        f"{route.monster_name} Lv.{route.monster_level} / "
                        f"{route.terrain_name}·{route.environment_name} / {affixes}"
                    )
                else:
                    route_detail = (
                        f"{rank_labels.get(route.node_kind, route.node_kind)} / "
                        f"{route.terrain_name}·{route.environment_name} / 无需战斗"
                    )
                lines.append(
                    f"{route_index}. {route.name}〔{route_detail}〕"
                )
                if route.discovery_name:
                    lines.append(f"   发现：{route.discovery_name}（{access}）")
                for risk_index, risk in enumerate(route.risk_choices):
                    letter = "A" if risk_index == 0 else "B"
                    lines.append(
                        f"   {letter}. {risk.name}：{risk.description} "
                        f"〔{self._format_nefia_risk_effect(risk)}〕"
                    )
            lines.append(
                "输入 /奈菲亚 1A（或1B/2A/2B）；"
                "想见好就收可 /奈菲亚 撤退。"
            )
        elif view.phase == "risk_choice":
            route = self._selected_nefia_route(view)
            lines.append(f"路线已锁定为「{route.name}」，请选择风险：")
            for index, risk in enumerate(route.risk_choices):
                letter = "A" if index == 0 else "B"
                lines.append(
                    f"{letter}. {risk.name}：{risk.description} "
                    f"〔{self._format_nefia_risk_effect(risk)}〕"
                )
            action_name = "开战" if route.requires_combat else "处理事件"
            lines.append(f"输入 /奈菲亚 A 或 /奈菲亚 B，随后自动{action_name}。")
        elif view.phase == "combat_ready":
            route = self._selected_nefia_route(view)
            risk = self._selected_nefia_risk(view, route)
            action_name = "战斗" if route.requires_combat else "处理事件"
            lines.append(
                f"已锁定「{route.name} / {risk.name}」。"
                f"输入 /奈菲亚 继续 {action_name}。"
            )
        elif view.phase == "cleared":
            lines.append("你带着终点秘藏离开；明日04:00后会生成新的路线。")
        elif view.phase == "defeated":
            lines.append(
                "本次探索结束，但已锁定的收获不会丢失。"
                "明日04:00可再出发。"
            )
        else:
            lines.append(
                "你及时收手并带走已获得的战利品。"
                "明日04:00可再出发。"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_nefia_risk_effect(risk) -> str:
        effects = []
        if risk.monster_level > 0:
            delta = int(risk.monster_level_delta)
            suffix = f"（{delta:+d}级）" if delta else ""
            effects.append(f"敌人Lv.{risk.monster_level}{suffix}")
        if risk.entry_hp_cost_ratio > 0:
            effects.append(f"HP -{risk.entry_hp_cost_ratio:.0%}")
        if risk.entry_mp_cost_ratio > 0:
            effects.append(f"MP -{risk.entry_mp_cost_ratio:.0%}")
        effects.append(f"奖励 x{risk.reward_multiplier:.2f}")
        quality_bonus = max(
            0.0,
            float(getattr(risk, "reward_quality_bonus", 0.0)),
        )
        effective_quality_bonus = max(
            0.0,
            float(
                getattr(
                    risk,
                    "reward_effective_quality_bonus",
                    quality_bonus,
                )
            ),
        )
        bonus_text = f"{quality_bonus:.2f}"
        if effective_quality_bonus + 1e-9 < quality_bonus:
            bonus_text += f"（按{effective_quality_bonus:.2f}上限结算）"
        rare_find_bonus = max(
            0.0,
            float(getattr(risk, "rare_find_quality_bonus", 0.0)),
        )
        if rare_find_bonus > 0:
            bonus_text += f"，含寻宝+{rare_find_bonus:.2f}"
        guaranteed = max(
            0,
            int(getattr(risk, "reward_guaranteed_upgrades", 0)),
        )
        minimum_quality = QUALITY_LABELS.get(
            str(getattr(risk, "reward_minimum_quality", "common")),
            str(getattr(risk, "reward_minimum_quality", "common")),
        )
        maximum_quality = QUALITY_LABELS.get("mythic", "神话")
        upgrade_chance = max(
            0.0,
            min(1.0, float(getattr(risk, "reward_upgrade_chance", 0.0))),
        )
        if guaranteed:
            quality_effect = (
                f"品质加值{bonus_text}→保底+{guaranteed}阶"
                f"（最低{minimum_quality}，最高{maximum_quality}）"
            )
            if upgrade_chance > 0:
                quality_effect += f"，再升1阶{upgrade_chance:.0%}"
        elif upgrade_chance > 0:
            quality_effect = (
                f"品质加值{bonus_text}→升1阶{upgrade_chance:.0%}"
                f"（最低{minimum_quality}，最高{maximum_quality}）"
            )
        else:
            quality_effect = "装备品质按基础掉落"
        effects.append(quality_effect)
        if risk.capability_mitigated:
            effects.append("能力已减免代价")
        return " / ".join(effects)

    async def list_dungeons(self, event: AstrMessageEvent) -> AsyncGenerator:
        """列出所有可用副本。"""
        if self.dungeon_service is None:
            yield await self.reply_text(event, "副本功能未启用。")
            return
        try:
            dungeons = self.dungeon_service.list_dungeons()
            if not dungeons:
                yield await self.reply_text(event, "暂无可用副本。")
                return
            lines = [
                "每日随机探索：/奈菲亚",
                "旧版固定波次副本（兼容入口，每日合计1次）：",
            ]
            for dungeon in dungeons:
                lines.append(
                    f"「{dungeon.name}」 推荐等级 Lv.{dungeon.recommended_level}"
                    f"  波数 {len(dungeon.waves)}"
                )
            lines.append(
                "推荐输入 /奈菲亚 体验随机路线；旧玩法仍可用 "
                "/副本详情 副本名 与 /挑战 副本名，但04:00前仅结算1次。"
            )
            yield await self.reply_text(event, "\n".join(lines))
        except Exception as exc:
            yield await self.reply_text(event, f"副本列表失败：{exc}")

    async def dungeon_detail(self, event: AstrMessageEvent, name: str) -> AsyncGenerator:
        """查看指定副本的详情。"""
        if self.dungeon_service is None:
            yield await self.reply_text(event, "副本功能未启用。")
            return
        try:
            name = str(name or "").strip()
            dungeon = self.dungeon_service.get_dungeon_by_name(name)
            if dungeon is None:
                yield await self.reply_text(event, f"未知副本：{name}")
                return
            lines = [
                f"副本：「{dungeon.name}」",
                f"推荐等级：Lv.{dungeon.recommended_level}",
                f"经验折扣：{int(dungeon.exp_discount_rate * 100)}%",
                f"说明：{dungeon.description}" if dungeon.description else "",
                "波次（{0}）：".format(len(dungeon.waves)),
            ]
            for index, wave in enumerate(dungeon.waves, 1):
                lines.append(
                    f"  {index}. {wave.template_id} Lv.{wave.level} {wave.rank}"
                )
            cr = dungeon.clear_rewards
            pr = dungeon.partial_kill_rewards
            lines.append(
                f"通关奖励：{cr.equipment_count}件装备"
                f"（Lv.{cr.equipment_level_min}-{cr.equipment_level_max}）"
            )
            lines.append(
                f"部分击杀奖励：{int(pr.chance * 100)}%概率"
                f" {pr.equipment_count}件装备"
                f"（Lv.{pr.equipment_level_min}-{pr.equipment_level_max}）"
            )
            lines.append(
                "旧固定波次用法：/挑战 副本名 策略（每日合计1次）；"
                "每日随机玩法：/奈菲亚"
            )
            yield await self.reply_text(event, "\n".join(line for line in lines if line))
        except Exception as exc:
            yield await self.reply_text(event, f"副本详情失败：{exc}")

    async def _dungeon_result(self, event: AstrMessageEvent, result):
        """渲染副本战报。"""
        return await self.reply_text(
            event, self._format_dungeon_result(result), "LevelUpPvp 副本战报"
        )

    def _format_dungeon_result(self, result) -> str:
        dungeon = result.dungeon
        name = dungeon.name
        status = "通关" if result.cleared else "失败"
        lines = [
            f"副本：「{name}」  结果：{status}",
            f"击杀：{result.monsters_killed}/{result.total_monsters}",
        ]
        if result.exp_gain > 0:
            lines.append(f"经验：+{result.exp_gain}")
        if result.level_ups:
            lines.append(self._format_level_ups(result.level_ups))
        skill_levelups = [item for item in (result.skill_growths or []) if item.to_level > item.from_level]
        if skill_levelups:
            growth = " / ".join(
                f"{item.skill_name} Lv.{item.from_level}→{item.to_level}"
                for item in skill_levelups[:10]
            )
            lines.append("技能升级：" + growth)
        spell_levelups = [item for item in (result.spell_growths or []) if item.to_level > item.from_level]
        if spell_levelups:
            lines.append("法术升级：")
            lines.extend(
                f"- {item.spell_name} Lv.{item.from_level}→{item.to_level}"
                for item in spell_levelups[:10]
            )
        attr_levelups = [item for item in (result.attribute_growths or []) if item.to_value > item.from_value]
        if attr_levelups:
            growth = " / ".join(
                f"{ATTRIBUTE_LABELS[item.attribute_id]} {item.from_value}→{item.to_value}"
                for item in attr_levelups[:8]
            )
            lines.append("属性提升：" + growth)
        if result.rewards:
            reward_labels = " / ".join(
                f"{item.name}(Lv.{item.item_level})" for item in result.rewards
            )
            lines.append(f"装备奖励：{reward_labels}")
        if result.player_defeated:
            lines.append("战败，状态已恢复。")
        return "\n".join(lines)

    def is_alias_challenge_event(self, event: AstrMessageEvent) -> bool:
        message = (event.get_message_str() or "").strip()
        if (
            MENTION_COMMAND_PATTERN.match(message)
            or SLASH_CHALLENGE_COMMAND_PATTERN.match(message)
        ):
            return False
        has_wake_word = any(word and word in message for word in CHALLENGE_WAKE_WORDS)
        has_mention_command = "挑战" in message and (
            self._is_self_mentioned(event) or CHALLENGE_COMMAND_PATTERN.match(message)
        )
        if not has_wake_word and not has_mention_command:
            return False
        return self._target_identity_from_event(event) is not None

    def parse_mentioned_command(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        if not self._is_self_mentioned(event):
            return None
        message = (event.get_message_str() or "").strip()
        if MENTION_COMMAND_PATTERN.match(message):
            return None
        text = self._text_without_mentions(event)
        match = MENTION_COMMAND_PATTERN.match(text)
        if not match:
            return None
        command = match.group(1)
        args = text[match.end():].strip()
        return command, args

    def parse_add_point_args(self, args: str) -> tuple[str, int] | None:
        parts = args.split()
        if not parts:
            return None
        stat_name = parts[0]
        amount = 1
        if len(parts) >= 2:
            try:
                amount = int(parts[1])
            except ValueError:
                return None
        return stat_name, amount

    async def _registration_error(self, event: AstrMessageEvent) -> str:
        if await self.ensure_sender_registered(event):
            return ""
        return REGISTRATION_REQUIRED_MESSAGE

    async def _is_astrbot_admin(self, event: AstrMessageEvent) -> bool:
        check = getattr(event, "is_admin", None)
        if check is None:
            return False
        try:
            result = check() if callable(check) else check
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            logger.exception("LevelUpPvp admin permission check failed")
            return False

    def _identity_from_event(self, event: AstrMessageEvent) -> UserIdentity:
        return UserIdentity(
            platform=event.get_platform_id() or event.get_platform_name() or "unknown",
            group_id=event.get_group_id() or "",
            user_id=event.get_sender_id() or event.get_session_id(),
            nickname=event.get_sender_name() or event.get_sender_id() or "未知用户",
        )

    def _is_chat_command(self, event: AstrMessageEvent) -> bool:
        message = (event.get_message_str() or "").strip()
        if not message:
            return False
        if message.startswith(("/", "／", "!", "！", ".", "。")):
            return True
        if MENTION_COMMAND_PATTERN.match(message):
            return True
        return bool(
            self.parse_mentioned_command(event)
            or self.is_alias_challenge_event(event)
        )

    @staticmethod
    def _chat_event_timestamp(event: AstrMessageEvent) -> int:
        message_obj = getattr(event, "message_obj", None)
        candidates = (
            getattr(message_obj, "timestamp", None),
            getattr(message_obj, "time", None),
            getattr(event, "timestamp", None),
        )
        for candidate in candidates:
            try:
                value = int(float(candidate))
            except (TypeError, ValueError, OverflowError):
                continue
            if value > 10_000_000_000:
                value //= 1000
            if value >= 0:
                return value
        return int(time.time())

    @staticmethod
    def _chat_event_key(event: AstrMessageEvent, occurred_at_ts: int) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_id = ""
        getter = getattr(event, "get_message_id", None)
        if callable(getter):
            try:
                message_id = str(getter() or "").strip()
            except Exception:
                message_id = ""
        if not message_id:
            for name in ("message_id", "id", "messageId"):
                value = getattr(message_obj, name, None)
                if value not in (None, ""):
                    message_id = str(value).strip()
                    break
        if not message_id:
            raw = getattr(message_obj, "raw_message", None)
            if isinstance(raw, dict):
                for name in ("message_id", "id", "messageId"):
                    if raw.get(name) not in (None, ""):
                        message_id = str(raw[name]).strip()
                        break

        platform = str(
            event.get_platform_id()
            or event.get_platform_name()
            or "unknown"
        )
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        if message_id:
            return f"{platform}:group:{group_id}:message:{message_id}"[:240]

        # Adapters are expected to expose a message id.  This content/time
        # coordinate is a deterministic best-effort fallback for simple test
        # adapters and still prevents duplicate processing inside one delivery.
        payload = "\x1f".join(
            (
                platform,
                group_id,
                sender_id,
                str(int(occurred_at_ts)),
                event.get_message_str() or "",
            )
        ).encode("utf-8")
        digest = hashlib.blake2b(payload, digest_size=12).hexdigest()
        return f"{platform}:group:{group_id}:fallback:{digest}"[:240]

    def _is_bot_target_id(self, event: AstrMessageEvent, target_id: str) -> bool:
        """Account for QQ Official events that expose the target At as self_id."""
        if str(target_id) in {"qq_official", "unknown_selfid"}:
            return True
        self_id = str(event.get_self_id() or "")
        if str(target_id) != self_id:
            return False
        if (event.get_platform_name() or "") != "qq_official":
            return True

        message = (event.get_message_str() or "").strip()
        is_challenge = any(word in message for word in CHALLENGE_WAKE_WORDS)
        if not is_challenge:
            command_text = self._text_without_mentions(event)
            is_challenge = CHALLENGE_COMMAND_PATTERN.match(command_text) is not None
        return not is_challenge

    def _target_identity_from_event(self, event: AstrMessageEvent) -> UserIdentity | None:
        sender_id = event.get_sender_id()
        for comp in event.get_messages():
            if not isinstance(comp, At):
                continue
            target_id = str(comp.qq)
            if (
                target_id == "all"
                or target_id == sender_id
                or self._is_bot_target_id(event, target_id)
            ):
                continue
            return UserIdentity(
                platform=event.get_platform_id() or event.get_platform_name() or "unknown",
                group_id=event.get_group_id() or "",
                user_id=target_id,
                nickname=comp.name or target_id,
            )
        message = event.get_message_str() or ""
        ignored_ids = self._ignored_target_ids(event)
        for match in MENTION_MARKUP_PATTERN.finditer(message):
            target_id = (match.group("legacy_id") or match.group("qqbot_id")).strip()
            if target_id and target_id not in ignored_ids:
                return UserIdentity(
                    platform=event.get_platform_id() or event.get_platform_name() or "unknown",
                    group_id=event.get_group_id() or "",
                    user_id=target_id,
                    nickname=target_id,
                )
        return None

    def _grant_target_identity(
        self,
        event: AstrMessageEvent,
    ) -> UserIdentity | None:
        """Resolve an admin grant target, including QQ Official's aliased At ID."""
        sender_id = str(event.get_sender_id() or "")
        platform = event.get_platform_id() or event.get_platform_name() or "unknown"
        group_id = event.get_group_id() or ""
        text = event.get_message_str() or ""
        command_index = text.find("给予")
        for match in MENTION_MARKUP_PATTERN.finditer(text):
            target_id = (match.group("legacy_id") or match.group("qqbot_id")).strip()
            if (
                match.start() > command_index
                and target_id
                not in {"", "all", "qq_official", "unknown_selfid"}
            ):
                return UserIdentity(platform, group_id, target_id, target_id)
        for comp in event.get_messages():
            if not isinstance(comp, At):
                continue
            target_id = str(comp.qq)
            if target_id in {
                "",
                "all",
                "qq_official",
                "unknown_selfid",
                sender_id,
            }:
                continue
            if (
                target_id == str(event.get_self_id() or "")
                and (
                    (event.get_platform_name() or "") != "qq_official"
                    or bool(comp.name)
                    or re.match(r"^\s*/给予(?:\s|$)", text) is None
                )
            ):
                continue
            return UserIdentity(
                platform=platform,
                group_id=group_id,
                user_id=target_id,
                nickname=comp.name or target_id,
            )
        return None

    def _target_identity_from_text(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> UserIdentity | None:
        ignored_ids = self._ignored_target_ids(event)
        for match in MENTION_MARKUP_PATTERN.finditer(text or ""):
            target_id = (match.group("legacy_id") or match.group("qqbot_id")).strip()
            if target_id and target_id not in ignored_ids:
                return UserIdentity(
                    platform=event.get_platform_id() or event.get_platform_name() or "unknown",
                    group_id=event.get_group_id() or "",
                    user_id=target_id,
                    nickname=target_id,
                )
        return None

    def _is_self_mentioned(self, event: AstrMessageEvent) -> bool:
        ignored_ids = self._ignored_target_ids(event)
        for comp in event.get_messages():
            if isinstance(comp, At) and str(comp.qq) in ignored_ids:
                return True
        message = event.get_message_str() or ""
        return any(
            (match.group("legacy_id") or match.group("qqbot_id")).strip()
            in ignored_ids
            for match in MENTION_MARKUP_PATTERN.finditer(message)
        )

    def _ignored_target_ids(self, event: AstrMessageEvent) -> set[str]:
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()
        ignored_ids = {"all", "qq_official"}
        if sender_id:
            ignored_ids.add(sender_id)
        if self_id and self._is_bot_target_id(event, self_id):
            ignored_ids.add(self_id)

        has_self_component = any(
            isinstance(comp, At) and str(comp.qq) in ignored_ids
            for comp in event.get_messages()
        )
        if has_self_component:
            message = event.get_message_str() or ""
            match = MENTION_MARKUP_PATTERN.search(message)
            command_index = self._first_command_index(message)
            if match and command_index != -1 and match.start() < command_index:
                ignored_ids.add(
                    (match.group("legacy_id") or match.group("qqbot_id")).strip()
                )
        return ignored_ids

    def _first_command_index(self, message: str) -> int:
        indexes = [
            message.find(command)
            for command in MENTION_COMMAND_NAMES
            if message.find(command) != -1
        ]
        return min(indexes) if indexes else -1

    def _text_without_mentions(self, event: AstrMessageEvent) -> str:
        text = event.get_message_str() or ""
        text = MENTION_MARKUP_PATTERN.sub(" ", text)
        text = re.sub(r"@\S+", " ", text)
        return " ".join(text.split())

    def _extract_strategy(
        self,
        event: AstrMessageEvent,
        target_identity: UserIdentity,
        parsed_strategy: str,
    ) -> str:
        text = parsed_strategy.strip()
        if not text:
            message = event.get_message_str().strip()
            text = CHALLENGE_COMMAND_PATTERN.sub("", message).strip()
        text = MENTION_MARKUP_PATTERN.sub(" ", text)
        for token in [
            *CHALLENGE_WAKE_WORDS,
            f"<@{target_identity.user_id}>",
            f"<@!{target_identity.user_id}>",
            f"@{target_identity.user_id}",
            f"@{target_identity.nickname}",
            target_identity.user_id,
            target_identity.nickname,
        ]:
            if token:
                text = text.replace(token, " ")
        text = " ".join(text.split())
        text = CHALLENGE_COMMAND_PATTERN.sub("", text).strip()
        text = " ".join(text.split())
        return text

    def _extract_modified_nickname(
        self,
        event: AstrMessageEvent,
        target_identity: UserIdentity | None,
        parsed_nickname: str,
    ) -> str:
        text = parsed_nickname.strip()
        if not text:
            text = self._text_without_mentions(event)
            text = MODIFY_REGISTER_COMMAND_PATTERN.sub("", text).strip()
        if target_identity:
            for token in [
                f"<@{target_identity.user_id}>",
                f"<@!{target_identity.user_id}>",
                f"@{target_identity.user_id}",
            ]:
                if token:
                    text = text.replace(token, " ")
        text = re.sub(r"<@!?[^>\s]+>", " ", text)
        text = re.sub(r"@\S+", " ", text)
        text = MODIFY_REGISTER_COMMAND_PATTERN.sub("", text).strip()
        return " ".join(text.split())

    def _format_profile(
        self,
        user: User,
        build=None,
        derived=None,
        progress=None,
        combat_state=None,
    ) -> str:
        lines = [
            f"{self._display_name(user)} 的面板",
            self._format_level_progress(user),
            f"自定义属性点：{user.stat_points} / 技能点：{user.skill_points}",
            self._format_stats(user),
            self._format_advanced_stats(user, build),
        ]
        if build:
            lines.append(
                f"构筑：{self._weapon_mode_label(build.weapon_mode)} / "
                f"{self._armor_style_label(build.armor_style)} / "
                f"重量 {build.total_weight:.2f}/{build.carry_capacity:.1f}"
                f"{'（超负重）' if build.overloaded else ''}"
            )
            lines.append(
                f"命中修正：物理 {build.physical_accuracy_multiplier:.0%} / "
                f"法术 {build.spell_accuracy_multiplier:.0%}"
            )
        if derived:
            if combat_state:
                lines.append(
                    "当前状态："
                    f"HP {combat_state.current_hp}/{combat_state.max_hp}｜"
                    f"MP {combat_state.current_mp}/{combat_state.max_mp}｜"
                    f"SP {combat_state.current_stamina}/"
                    f"{combat_state.max_stamina}"
                )
                lines.append(
                    "自然恢复：每30秒 "
                    f"HP +{combat_state.rates.hp:.2f}｜"
                    f"MP +{combat_state.rates.mp:.2f}｜"
                    f"SP +{combat_state.rates.stamina:.2f}"
                    f"（下回合 {combat_state.next_recovery_seconds}秒）"
                )
                active_statuses = [
                    f"{item.get('status_id', 'unknown')} "
                    f"{int(item.get('remaining_ticks', 0))}回合"
                    for item in combat_state.state.statuses
                    if int(item.get("remaining_ticks", 0)) > 0
                ]
                if active_statuses:
                    lines.append("状态：" + " / ".join(active_statuses))
                cooldowns = [
                    f"{skill_id} {ticks}回合"
                    for skill_id, ticks
                    in combat_state.state.skill_cooldowns.items()
                    if ticks > 0
                ]
                if combat_state.state.attack_cooldown > 0:
                    cooldowns.append(
                        f"普通攻击 {combat_state.state.attack_cooldown}回合"
                    )
                if combat_state.state.counter_cooldown > 0:
                    cooldowns.append(
                        f"反击 {combat_state.state.counter_cooldown}回合"
                    )
                if combat_state.state.recovery_ticks > 0:
                    cooldowns.append(
                        f"后摇 {combat_state.state.recovery_ticks}回合"
                    )
                if combat_state.state.hitstun_ticks > 0:
                    cooldowns.append(
                        f"硬直 {combat_state.state.hitstun_ticks}回合"
                    )
                if combat_state.state.stance_id:
                    cooldowns.append(
                        f"架势 {combat_state.state.stance_id}"
                    )
                if cooldowns:
                    lines.append("延续状态：" + " / ".join(cooldowns))
            else:
                lines.append(
                    f"资源：HP {derived.max_hp} / MP {derived.max_mp} / "
                    f"SP {derived.max_sp}"
                )
            lines.append(
                f"战斗：攻击 {derived.attack_power:.1f} / 命中 {derived.accuracy:.1f} / "
                f"防御 {derived.defense:.1f} / 回避 {derived.evasion:.1f} / "
                f"暴击 {derived.critical_rate:.1%}×{derived.critical_damage:.2f} / "
                f"速度 {derived.action_speed:.0f}"
            )
        if progress:
            lines.append(
                "属性潜力：" + " / ".join(
                    f"{ATTRIBUTE_LABELS[key]} {progress[key].potential}%"
                    for key in ATTRIBUTE_LABELS
                )
            )
            lines.append(
                "属性经验：" + " / ".join(
                    f"{ATTRIBUTE_LABELS[key]} "
                    f"{progress_percent(progress[key].exp, attribute_exp_required(user.stats()[key])):.1f}%"
                    for key in ATTRIBUTE_LABELS
                )
            )
        freeze_summary = self._format_freeze_summary(user)
        if freeze_summary:
            lines.append(freeze_summary)
        lines.append(f"战绩：{user.wins} 胜 / {user.losses} 负")
        return "\n".join(lines)

    def _format_checkin_success(self, result) -> str:
        lines = [
            f"{self._display_name(result.user)} 签到成功！",
            f"获得经验：{result.exp_gain}",
            f"连续签到：{result.streak_days} 天",
            self._format_level_progress(result.user),
        ]
        if result.attribute_potential_restore:
            lines.append(
                f"六项属性潜力合计恢复：+{result.attribute_potential_restore}%"
            )
        if result.level_ups:
            lines.append(self._format_level_ups(result.level_ups))
        return "\n".join(lines)

    def _format_existing_checkin(self, result) -> str:
        return "\n".join(
            [
                "今天已经签到过了。",
                f"今日签到经验：{result.exp_gain}",
                f"连续签到：{result.streak_days} 天",
                self._format_level_progress(result.user),
            ]
        )

    def _format_level_progress(self, user: User) -> str:
        required = config.exp_required_for_next_level(user.level)
        return f"等级：Lv.{user.level} 经验：{user.exp}/{required}"

    def _format_stats(self, user: User) -> str:
        return (
            f"主属性：力量 {user.strength} / 体质 {user.constitution} / "
            f"灵巧 {user.dexterity} / 感知 {user.perception} / "
            f"魔力 {user.magic} / 意志 {user.willpower}"
        )

    def _format_advanced_stats(self, user: User, build=None) -> str:
        modifiers = build.advanced_stat_modifiers if build else {}
        def value(base, key):
            bonus = modifiers.get(key, 0.0)
            return f"{base + bonus:g}" + (f"（基础{base:g}+{bonus:g}）" if bonus else "")
        return (
            f"高级属性：生命成长 {value(user.life_growth, 'life_growth')} / "
            f"魔法成长 {value(user.mana_growth, 'mana_growth')} / "
            f"速度 {value(user.advanced_speed, 'speed')} / "
            f"幸运 {value(user.advanced_luck, 'luck')}"
        )
    def _format_ranking_line(self, rank: int, user: User) -> str:
        required = config.exp_required_for_next_level(user.level)
        return f"{rank} {self._display_name(user)} Lv.{user.level} {user.exp}/{required}"

    def _format_level_ups(self, level_ups: list[LevelUpEvent]) -> str:
        lines = ["等级变化："]
        for item in level_ups:
            label = "解冻恢复" if item.restored_from_freeze else "升级成长"
            growth = self._format_stat_changes(item.auto_growth, "+") or "无属性变化"
            parts = [growth]
            if item.stat_points_gain:
                parts.append(f"自定义属性点 +{item.stat_points_gain}")
            if item.skill_points_gain:
                parts.append(f"技能点 +{item.skill_points_gain}")
            lines.append(
                f"{label} Lv.{item.from_level} -> Lv.{item.to_level}："
                + "，".join(parts)
            )
        return "\n".join(lines)

    def _format_level_downs(self, level_downs: list[LevelDownEvent]) -> str:
        lines = ["降级冻结："]
        for item in level_downs:
            parts = []
            frozen_stats = self._format_stat_changes(item.frozen_stats, "-")
            if frozen_stats:
                parts.append(frozen_stats)
            if item.frozen_stat_points:
                parts.append(f"自定义属性点 -{item.frozen_stat_points}")
            if item.frozen_skill_points:
                parts.append(f"技能点 -{item.frozen_skill_points}")
            detail = "，".join(parts) or "无属性冻结"
            lines.append(f"Lv.{item.from_level} -> Lv.{item.to_level}：{detail}")
        return "\n".join(lines)

    def _format_freeze_summary(self, user: User) -> str:
        frozen_stats = self._format_stat_changes(user.frozen_stats, "-")
        parts = []
        if frozen_stats:
            parts.append(frozen_stats)
        if user.frozen_stat_points:
            parts.append(f"自定义属性点 -{user.frozen_stat_points}")
        if user.frozen_skill_points:
            parts.append(f"技能点 -{user.frozen_skill_points}")
        if not parts:
            return ""
        levels = "、".join(f"Lv.{level}" for level in user.frozen_levels) or "等级"
        return f"冻结：{levels} 待解冻，" + "，".join(parts)

    def _format_stat_changes(self, stats: dict[str, int], sign: str) -> str:
        changes = []
        for stat_name, label in config.STAT_LABELS.items():
            amount = int(stats.get(stat_name, 0) or 0)
            if amount > 0:
                changes.append(f"{label} {sign}{amount}")
        return " / ".join(changes)

    def _format_battle_result(self, result) -> str:
        winner_is_attacker = result.winner.id == result.attacker.id
        attacker_name = self._display_name(result.attacker)
        defender_name = self._display_name(result.defender)
        winner_name = self._display_name(result.winner)
        loser_name = self._display_name(result.loser)
        battle_mode = "排位" if getattr(result, "rated", False) else "切磋"
        loser_exp_gain = getattr(result, "loser_exp_gain", 0)
        lines = [
            f"{attacker_name} VS {defender_name}（{battle_mode}）",
            "策略："
            f"攻击方「{result.attacker_strategy}」"
            f"{'（随机）' if result.attacker_strategy_random else ''} / "
            f"防守方「{result.defender_strategy}」"
            f"{'（随机）' if result.defender_strategy_random else ''}",
            f"结算：{winner_name} +{result.winner_exp_gain} 经验，"
            f"{loser_name} +{loser_exp_gain} 参与经验",
        ]
        if getattr(result, "rated", False):
            lines.append(
                "评级："
                f"{attacker_name} {result.attacker_rating_before}→"
                f"{result.attacker_rating_after} / "
                f"{defender_name} {result.defender_rating_before}→"
                f"{result.defender_rating_after}"
            )
        elif getattr(result, "reward_reason", ""):
            lines.append(
                "本场为无奖励切磋：不计Elo、角色经验或技能/法术成长，"
                "但可以验证构筑并查看复盘。"
            )
            reward_reasons = {
                item
                for item in str(result.reward_reason).split(",")
                if item
            }
            if {
                "winner_account_not_qualified",
                "loser_account_not_qualified",
            } & reward_reasons:
                lines.append(
                    "排位解锁：双方都需达到 Lv.5，或各自累计签到 3 天。"
                )
            elif "rated_level_gap_exceeded" in reward_reasons:
                lines.append("排位条件：双方等级差不能超过 10 级。")
            elif "repeat_pair_today" in reward_reasons:
                lines.append("同一对手每天仅首场计入排位与成长奖励。")
        if result.is_counterattack:
            lines.insert(1, "反击：本次不消耗主动挑战次数")
        if result.analysis and result.simulation is None:
            lines.append(f"分析：{result.analysis}")
        if result.battle_log:
            lines.append("战报：")
            lines.extend(
                f"- {item}" for item in result.battle_log[: config.BATTLE_LOG_MAX_LINES]
            )
        if result.level_ups:
            lines.append(self._format_level_ups(result.level_ups))
        if getattr(result, "loser_level_ups", None):
            lines.append(self._format_level_ups(result.loser_level_ups))
        if result.level_downs:
            lines.append(self._format_level_downs(result.level_downs))
        skill_levelups = [item for item in (result.skill_growths or []) if item.to_level > item.from_level]
        if skill_levelups:
            growth = " / ".join(
                f"{attacker_name if item.user_pk == result.attacker.id else defender_name}·{item.skill_name} Lv.{item.from_level}→{item.to_level}"
                for item in skill_levelups[:10]
            )
            lines.append("技能升级：" + growth)
        spell_levelups = [item for item in (result.spell_growths or []) if item.to_level > item.from_level]
        if spell_levelups:
            lines.append("法术升级：")
            lines.extend(
                f"- {item.spell_name} Lv.{item.from_level}→{item.to_level}"
                for item in spell_levelups[:10]
            )
        attr_levelups = [item for item in (result.attribute_growths or []) if item.to_value > item.from_value]
        if attr_levelups:
            growth = " / ".join(
                f"{attacker_name if item.user_pk == result.attacker.id else defender_name}·"
                f"{ATTRIBUTE_LABELS[item.attribute_id]} {item.from_value}→{item.to_value}"
                for item in attr_levelups[:8]
            )
            lines.append("属性提升：" + growth)
        lines.append("结果：" + ("攻击方获胜" if winner_is_attacker else "防守方获胜"))
        return "\n".join(lines)

    async def _battle_result(self, event: AstrMessageEvent, result):
        """生成专用战报；失败时依次回退到通用图片和纯文字。"""
        try:
            report_image = render_battle_report(result)
            file_url = save_temp_img(report_image)
            return self._image_result(event, file_url, "LevelUpPvp 战报")
        except Exception:
            logger.exception("LevelUpPvp battle report image render failed")
            return await self.reply_text(
                event,
                self._format_battle_result(result),
                "LevelUpPvp 战报",
            )

    def _display_name(self, user: User) -> str:
        name = user.nickname or user.user_id
        if name == user.user_id and len(name) > 8:
            return f"{name[:3]}...{name[-2:]}"
        return name
