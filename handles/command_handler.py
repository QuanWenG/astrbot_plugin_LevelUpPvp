import asyncio
import inspect
import re
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
    from ..services.image_renderer import render_text_card
else:
    from services.battle_image_renderer import (
        RENDERER_REVISION,
        render_battle_report,
    )
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
    from ..models.equipment import SLOT_LABELS
    from ..models.user import LevelDownEvent, LevelUpEvent, User, UserIdentity
    from ..services.attribute_service import (
        ADVANCED_ATTRIBUTE_LABELS,
        ATTRIBUTE_LABELS,
        DAMAGE_TYPE_LABELS,
        attribute_exp_required,
        skill_level_cap,
    )
    from ..services.equipment_affixes import (
        effective_inherent_affixes,
        inherent_affix_level_ratio,
    )
    from ..services.equipment_catalog import QUALITY_LABELS
    from ..services.equipment_proc_service import EQUIPMENT_PROC_NAMES
    from ..services.material_catalog import actual_weight, material_for
    from ..services.skill_catalog import SKILL_DEFINITIONS
    from ..services.ability_catalog import (
        ACTIVE_ABILITY_DEFINITIONS, SPELL_DEFINITIONS, TECHNIQUE_DEFINITIONS,
        ability_is_unlocked, spell_exp_required,
    )
    from ..services import config
except ImportError:
    from models.equipment import SLOT_LABELS
    from models.user import LevelDownEvent, LevelUpEvent, User, UserIdentity
    from services.attribute_service import (
        ADVANCED_ATTRIBUTE_LABELS,
        ATTRIBUTE_LABELS,
        DAMAGE_TYPE_LABELS,
        attribute_exp_required,
        skill_level_cap,
    )
    from services.equipment_affixes import (
        effective_inherent_affixes,
        inherent_affix_level_ratio,
    )
    from services.equipment_catalog import QUALITY_LABELS
    from services.equipment_proc_service import EQUIPMENT_PROC_NAMES
    from services.material_catalog import actual_weight, material_for
    from services.skill_catalog import SKILL_DEFINITIONS
    from services.ability_catalog import (
        ACTIVE_ABILITY_DEFINITIONS, SPELL_DEFINITIONS, TECHNIQUE_DEFINITIONS,
        ability_is_unlocked, spell_exp_required,
    )
    from services import config


"""很不文明哦，好孩子别学"""
CHALLENGE_WAKE_WORDS = [
    "艾斯比",
    "啥比"
]
MENTION_COMMAND_NAMES = ("重载装备表", "装备图鉴", "魔法书", "阅读", "法术", "战技", "修改登记", "装备详情", "训练技能", "技能栏", "签到", "面板", "加点", "排行", "登记", "挑战", "背包", "装备", "穿戴", "卸下", "技能", "学习", "给予")
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
        attribute_service=None,
        spell_service=None,
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
            lines = [f"{self._display_name(user)} 的背包 第{page}页"]
            for item in items[start:start + size]:
                mark = "[已装备]" if item.id in equipped_ids else ""
                material = material_for(item.material)
                resolved_weight = actual_weight(item.weight, item.material)
                lines.append(
                    f"No.{item.id} {QUALITY_LABELS.get(item.quality, item.quality)} {item.name} "
                    f"Lv.{item.item_level} {material.name} "
                    f"重量{item.weight:g}×{material.weight_multiplier:g}={resolved_weight:.3f}{mark}"
                )
            if len(lines) == 1: lines.append("这一页没有装备。")
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
                lines.append(
                    "资料效果（当前未结算）："
                    + "、".join(source_effects)
                )
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 装备详情"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"查看装备详情失败：{exc}")

    async def equip_item(self, event, equipment_id: int, slot: str = ""):
        try:
            user = await self._own_user(event); normalized = self._slot_id(slot) if slot else ""
            item, slots = await self.equipment_service.equip(user.id, int(equipment_id), normalized)
            yield await self.reply_text(
                event, f"已穿戴 {item.name}：{'、'.join(SLOT_LABELS[s] for s in slots)}"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"穿戴失败：{exc}")

    async def unequip_item(self, event, slot: str):
        try:
            user = await self._own_user(event); normalized = self._slot_id(slot) if slot else ""
            await self.equipment_service.unequip(user.id, normalized)
            yield await self.reply_text(event, f"已卸下{SLOT_LABELS[normalized]}装备。")
        except Exception as exc:
            yield await self.reply_text(event, f"卸下失败：{exc}")

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
                    attributes, definition.governing_attributes
                )
                line = (
                    f"{definition.name} Lv.{skill.level}/{level_cap} "
                    f"EXP {skill.exp}/{50 + skill.level * 15} "
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
            books = await self.spell_service.list_books(user.id)
            page = max(1, int(page)); page_size = 10
            visible = books[(page - 1) * page_size:page * page_size]
            lines = [f"{self._display_name(user)} 的魔法书（第{page}页）"]
            if not visible:
                lines.append("暂无魔法书。当前版本只提供内部原子发放接口。")
            for book in visible:
                definition = SPELL_DEFINITIONS[book.spell_id]
                attribute = ATTRIBUTE_LABELS.get(definition.reading_attribute, definition.reading_attribute)
                lines.append(
                    f"#{book.id} {definition.name} ×{book.quantity}"
                    f"（难度{definition.reading_difficulty}，主属性：{attribute}）"
                )
            yield await self.reply_text(
                event, "\n".join(lines), "LevelUpPvp 魔法书"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"查看魔法书失败：{exc}")

    async def read_spellbook(self, event, book_id: int):
        try:
            user = await self._own_user(event)
            result = await self.spell_service.read_book(user, int(book_id))
            outcome = "阅读成功" if result.success else "阅读失败"
            detail = ""
            if result.spell:
                definition = SPELL_DEFINITIONS[result.spell.spell_id]
                detail = f"，{definition.name} Lv.{result.spell.level} 潜力{result.spell.potential}%"
            attribute = ATTRIBUTE_LABELS.get(result.reading_attribute, result.reading_attribute)
            yield await self.reply_text(
                event,
                f"{outcome}（阅读能力{result.reading_power:.0f}，"
                f"难度{result.reading_difficulty}，主属性：{attribute}，"
                f"成功率{result.chance:.1%}，已消耗1本）{detail}"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"阅读失败：{exc}")

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
                lines.append(f"{definition.name} Lv.{spell.level} EXP {spell.exp}/{spell_exp_required(spell.level)} 潜力{spell.potential}%")
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
            user = await self._own_user(event); skill = await self.skill_service.learn(user, name)
            yield await self.reply_text(
                event, f"已学习技能：{SKILL_DEFINITIONS[skill.skill_id].name} Lv.1"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"学习失败：{exc}")

    async def train_skill(self, event, name: str, points: int):
        try:
            user = await self._own_user(event); skill = await self.skill_service.train_potential(user, name, int(points))
            yield await self.reply_text(
                event,
                f"训练完成：{SKILL_DEFINITIONS[skill.skill_id].name} 潜力提升至{skill.potential}%",
            )
        except Exception as exc:
            yield await self.reply_text(event, f"训练失败：{exc}")

    async def set_skill_slot(self, event, slot: int, name: str):
        try:
            user = await self._own_user(event); await self.skill_service.set_active_slot(user, int(slot), name)
            yield await self.reply_text(
                event, f"技能栏{slot}已{'清空' if name == '清空' else '设置为' + name}。"
            )
        except Exception as exc:
            yield await self.reply_text(event, f"技能栏设置失败：{exc}")

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
                    f"{ATTRIBUTE_LABELS[key]} {progress[key].exp}/"
                    f"{attribute_exp_required(user.stats()[key])}"
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
        lines = [
            f"{attacker_name} VS {defender_name}",
            "策略："
            f"攻击方「{result.attacker_strategy}」"
            f"{'（随机）' if result.attacker_strategy_random else ''} / "
            f"防守方「{result.defender_strategy}」"
            f"{'（随机）' if result.defender_strategy_random else ''}",
            f"结算：{winner_name} +{result.winner_exp_gain} 经验，"
            f"{loser_name} -{result.loser_exp_loss} 经验",
        ]
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
