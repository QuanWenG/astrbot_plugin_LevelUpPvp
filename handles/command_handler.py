import asyncio
import inspect
import re
from collections.abc import AsyncGenerator

from PIL import Image, ImageDraw

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Node, Plain
from astrbot.core.utils.io import save_temp_img
from astrbot.core.utils.t2i.local_strategy import FontManager

try:
    from ..models.user import LevelDownEvent, LevelUpEvent, User, UserIdentity
    from ..services import config
except ImportError:
    from models.user import LevelDownEvent, LevelUpEvent, User, UserIdentity
    from services import config


"""很不文明哦，好孩子别学"""
CHALLENGE_WAKE_WORDS = [
    "艾斯比",
    "啥比"
]
MENTION_COMMAND_NAMES = ("修改登记", "签到", "面板", "加点", "排行", "登记", "挑战")
MENTION_COMMAND_PATTERN = re.compile(
    rf"^/?({'|'.join(MENTION_COMMAND_NAMES)})(?:\s|$)"
)
CHALLENGE_COMMAND_PATTERN = re.compile(r"^/?挑战(?:\s|$)")
SLASH_CHALLENGE_COMMAND_PATTERN = re.compile(r"^/挑战(?:\s|$)")
MODIFY_REGISTER_COMMAND_PATTERN = re.compile(r"^/?修改登记(?:\s|$)")
SLASH_CHECKIN_COMMAND_PATTERN = re.compile(r"^/签到(?:\s|$)")
CHECKIN_COMMAND_PATTERN = re.compile(r"^/?签到(?:\s|$)")
REGISTRATION_REQUIRED_MESSAGE = "请先使用 /登记 昵称 完成昵称登记后再使用本插件指令。"
ADMIN_REQUIRED_MESSAGE = "只有 AstrBot 管理员可以使用该指令。"
BATTLE_REPORT_WIDTH = 1280
BATTLE_REPORT_MIN_HEIGHT = 720
BATTLE_REPORT_HEADER_HEIGHT = 112
BATTLE_REPORT_HORIZONTAL_PADDING = 96
BATTLE_REPORT_CONTENT_TOP = 82
BATTLE_REPORT_CONTENT_BOTTOM = 56
BATTLE_REPORT_TITLE_FONT_SIZE = 56
BATTLE_REPORT_CONTENT_FONT_SIZE = 30
BATTLE_REPORT_LINE_HEIGHT = 54


class LevelUpPvpCommandHandler:
    def __init__(
        self,
        *,
        context,
        user_service,
        checkin_service,
        stat_service,
        battle_service,
    ):
        self.context = context
        self.user_service = user_service
        self.checkin_service = checkin_service
        self.stat_service = stat_service
        self.battle_service = battle_service

    async def sign(self, event: AstrMessageEvent) -> AsyncGenerator:
        try:
            result = await self.checkin_service.checkin(self._identity_from_event(event))
            if result.already_checked:
                yield event.plain_result(self._format_existing_checkin(result))
                return

            yield event.plain_result(self._format_checkin_success(result))
        except Exception as exc:
            logger.exception("LevelUpPvp sign failed")
            yield event.plain_result(f"签到失败：{exc}")

    async def auto_checkin(self, event: AstrMessageEvent) -> AsyncGenerator:
        """群内当天首条消息自动签到；失败时不影响原消息传播。"""
        if not self.is_auto_checkin_event(event):
            return
        try:
            result = await self.checkin_service.checkin(self._identity_from_event(event))
            if result.already_checked:
                return
            yield event.plain_result(self._format_checkin_success(result))
        except Exception:
            logger.exception("LevelUpPvp automatic check-in failed")

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
            yield event.plain_result(registration_error)
            return
        try:
            identity = self._target_identity_from_event(event) or self._identity_from_event(
                event
            )
            user = await self.user_service.get_or_create_user(identity)
            yield event.plain_result(self._format_profile(user))
        except Exception as exc:
            logger.exception("LevelUpPvp profile failed")
            yield event.plain_result(f"查看面板失败：{exc}")

    async def add_point(
        self,
        event: AstrMessageEvent,
        stat_name: str = "",
        amount: int = 1,
    ) -> AsyncGenerator:
        registration_error = await self._registration_error(event)
        if registration_error:
            yield event.plain_result(registration_error)
            return
        if not stat_name:
            yield event.plain_result("用法：/加点 攻击 2")
            return
        try:
            result = await self.stat_service.allocate(
                self._identity_from_event(event),
                stat_name,
                amount,
            )
            label = config.STAT_LABELS[result.stat_name]
            rolls = " + ".join(str(item) for item in result.rolls)
            yield event.plain_result(
                "\n".join(
                    [
                        f"加点成功：{label} +{result.total_gain}",
                        f"消耗属性点：{result.points_spent}",
                        f"随机结果：{rolls}",
                        f"剩余属性点：{result.user.stat_points}",
                        self._format_stats(result.user),
                    ]
                )
            )
        except Exception as exc:
            yield event.plain_result(str(exc))

    async def ranking(self, event: AstrMessageEvent) -> AsyncGenerator:
        registration_error = await self._registration_error(event)
        if registration_error:
            yield event.plain_result(registration_error)
            return
        try:
            target_identity = self._target_identity_from_event(event)
            if target_identity:
                result = await self.user_service.get_user_rank(target_identity)
                if not result:
                    yield event.plain_result("该用户暂无排行数据。")
                    return
                rank, user = result
                yield event.plain_result(
                    "\n".join(
                        [
                            "排名 用户名 等级 当前经验/升级所需经验",
                            self._format_ranking_line(rank, user),
                        ]
                    )
                )
                return

            identity = self._identity_from_event(event)
            ranked_users = await self.user_service.get_top_users(
                identity.platform,
                identity.group_id,
                10,
            )
            if not ranked_users:
                yield event.plain_result("当前群暂无排行数据。")
                return
            lines = ["等级排行 TOP10", "排名 用户名 等级 当前经验/升级所需经验"]
            lines.extend(
                self._format_ranking_line(rank, user) for rank, user in ranked_users
            )
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            logger.exception("LevelUpPvp ranking failed")
            yield event.plain_result(f"查看排行失败：{exc}")

    async def register_nickname(
        self,
        event: AstrMessageEvent,
        nickname: str = "",
    ) -> AsyncGenerator:
        if not nickname:
            yield event.plain_result("用法：/登记 昵称")
            return
        try:
            user = await self.user_service.register_nickname(
                self._identity_from_event(event),
                nickname,
            )
            yield event.plain_result(f"登记成功：{self._display_name(user)}")
        except Exception as exc:
            logger.exception("LevelUpPvp nickname registration failed")
            yield event.plain_result(f"登记失败：{exc}")

    async def modify_registered_nickname(
        self,
        event: AstrMessageEvent,
        nickname: str = "",
    ) -> AsyncGenerator:
        if not await self._is_astrbot_admin(event):
            yield event.plain_result(ADMIN_REQUIRED_MESSAGE)
            return
        registration_error = await self._registration_error(event)
        if registration_error:
            yield event.plain_result(registration_error)
            return

        target_identity = self._target_identity_from_event(
            event
        ) or self._target_identity_from_text(event, nickname)
        nickname = self._extract_modified_nickname(event, target_identity, nickname)
        if not target_identity or not nickname:
            yield event.plain_result("用法：/修改登记 @用户 昵称")
            return
        try:
            user = await self.user_service.register_nickname(target_identity, nickname)
            yield event.plain_result(f"修改登记成功：{self._display_name(user)}")
        except Exception as exc:
            logger.exception("LevelUpPvp admin nickname registration failed")
            yield event.plain_result(f"修改登记失败：{exc}")

    async def challenge(self, event: AstrMessageEvent) -> AsyncGenerator:
        registration_error = await self._registration_error(event)
        if registration_error:
            yield event.plain_result(registration_error)
            return
        try:
            target_identity = self._target_identity_from_event(event)
            if not target_identity:
                yield event.plain_result("请 At 一个要挑战的用户。用法：/挑战 @用户 策略描述")
                return
            if target_identity.user_id == event.get_self_id():
                yield event.plain_result("不能挑战机器人。")
                return

            parsed_strategy = self._extract_strategy(event, target_identity, "")
            result = await self.battle_service.battle(
                self._identity_from_event(event),
                target_identity,
                parsed_strategy,
                context=self.context,
                event=event,
            )
            yield await self._battle_result(event, self._format_battle_result(result))
        except Exception as exc:
            logger.exception("LevelUpPvp battle failed")
            yield event.plain_result(f"挑战失败：{exc}")

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
        identity = self._identity_from_event(event)
        if await self.user_service.has_registered_nickname(identity):
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

    def _target_identity_from_event(self, event: AstrMessageEvent) -> UserIdentity | None:
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()
        for comp in event.get_messages():
            if not isinstance(comp, At):
                continue
            target_id = str(comp.qq)
            if target_id == "all" or target_id == sender_id or target_id == self_id:
                continue
            return UserIdentity(
                platform=event.get_platform_id() or event.get_platform_name() or "unknown",
                group_id=event.get_group_id() or "",
                user_id=target_id,
                nickname=comp.name or target_id,
            )
        message = event.get_message_str() or ""
        ignored_ids = self._ignored_target_ids(event)
        for match in re.finditer(r"<@!?([^>\s]+)>", message):
            target_id = match.group(1).strip()
            if target_id and target_id not in ignored_ids:
                return UserIdentity(
                    platform=event.get_platform_id() or event.get_platform_name() or "unknown",
                    group_id=event.get_group_id() or "",
                    user_id=target_id,
                    nickname=target_id,
                )
        return None

    def _target_identity_from_text(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> UserIdentity | None:
        ignored_ids = self._ignored_target_ids(event)
        for match in re.finditer(r"<@!?([^>\s]+)>", text or ""):
            target_id = match.group(1).strip()
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
            match.group(1).strip() in ignored_ids
            for match in re.finditer(r"<@!?([^>\s]+)>", message)
        )

    def _ignored_target_ids(self, event: AstrMessageEvent) -> set[str]:
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()
        ignored_ids = {"all", "qq_official"}
        if sender_id:
            ignored_ids.add(sender_id)
        if self_id:
            ignored_ids.add(self_id)

        has_self_component = any(
            isinstance(comp, At) and str(comp.qq) in ignored_ids
            for comp in event.get_messages()
        )
        if has_self_component:
            message = event.get_message_str() or ""
            match = re.search(r"<@!?([^>\s]+)>", message)
            command_index = self._first_command_index(message)
            if match and command_index != -1 and match.start() < command_index:
                ignored_ids.add(match.group(1).strip())
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
        text = re.sub(r"<@!?[^>\s]+>", " ", text)
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
        text = re.sub(r"<@!?[^>\s]+>", " ", text)
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

    def _format_profile(self, user: User) -> str:
        lines = [
            f"{self._display_name(user)} 的面板",
            self._format_level_progress(user),
            f"自定义属性点：{user.stat_points}",
            self._format_stats(user),
        ]
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
            f"属性：生命 {user.hp} / 攻击 {user.atk} / 防御 {user.defense} / "
            f"速度 {user.speed} / 幸运 {user.luck}"
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
        rate = result.attacker_win_rate * 100
        roll = result.roll_value * 100
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
            f"攻击方胜率：{rate:.1f}% / 随机值：{roll:.1f}%",
            f"结算：{winner_name} +{result.winner_exp_gain} 经验，"
            f"{loser_name} -{result.loser_exp_loss} 经验",
        ]
        if result.is_counterattack:
            lines.insert(1, "反击：本次不消耗主动挑战次数")
        if result.analysis:
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
        lines.append("结果：" + ("攻击方获胜" if winner_is_attacker else "防守方获胜"))
        return "\n".join(lines)

    async def _battle_result(self, event: AstrMessageEvent, text: str):
        """按平台选择战报载体，避免长战报刷屏。"""
        platform_name = event.get_platform_name() or ""
        if platform_name in {"qq_official", "qq_official_webhook"}:
            try:
                image = await asyncio.to_thread(self._render_battle_report_image, text)
                return event.image_result(image)
            except BaseException:
                logger.exception("LevelUpPvp battle report image rendering failed")
                return event.plain_result(text)
        if platform_name == "aiocqhttp":
            node = Node(
                uin=event.get_self_id() or "0",
                name="LevelUpPvp 战报",
                content=[Plain(text)],
            )
            return event.chain_result([node])
        return event.plain_result(text)

    def _render_battle_report_image(self, text: str) -> str:
        title_font = FontManager.get_font(BATTLE_REPORT_TITLE_FONT_SIZE)
        content_font = FontManager.get_font(BATTLE_REPORT_CONTENT_FONT_SIZE)
        measure_image = Image.new("RGB", (1, 1))
        measure_draw = ImageDraw.Draw(measure_image)
        max_text_width = (
            BATTLE_REPORT_WIDTH - BATTLE_REPORT_HORIZONTAL_PADDING * 2
        )
        wrapped_lines = self._wrap_battle_report_text(
            text,
            measure_draw,
            content_font,
            max_text_width,
        )
        content_height = (
            BATTLE_REPORT_CONTENT_TOP
            + len(wrapped_lines) * BATTLE_REPORT_LINE_HEIGHT
            + BATTLE_REPORT_CONTENT_BOTTOM
        )
        image_height = max(
            BATTLE_REPORT_MIN_HEIGHT,
            BATTLE_REPORT_HEADER_HEIGHT + content_height,
        )
        image = Image.new(
            "RGB",
            (BATTLE_REPORT_WIDTH, image_height),
            color="#ffffff",
        )
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (0, 0, BATTLE_REPORT_WIDTH, BATTLE_REPORT_HEADER_HEIGHT),
            fill="#3276dc",
        )
        title = "# LevelUpPvp 战报"
        title_box = draw.textbbox((0, 0), title, font=title_font)
        title_height = title_box[3] - title_box[1]
        title_y = (BATTLE_REPORT_HEADER_HEIGHT - title_height) // 2 - title_box[1]
        draw.text((18, title_y), title, fill="#ffffff", font=title_font)

        content_y = BATTLE_REPORT_HEADER_HEIGHT + BATTLE_REPORT_CONTENT_TOP
        for line in wrapped_lines:
            draw.text(
                (BATTLE_REPORT_HORIZONTAL_PADDING, content_y),
                line,
                fill="#1f2937",
                font=content_font,
            )
            content_y += BATTLE_REPORT_LINE_HEIGHT
        return save_temp_img(image)

    def _wrap_battle_report_text(
        self,
        text: str,
        draw: ImageDraw.ImageDraw,
        font,
        max_width: int,
    ) -> list[str]:
        wrapped: list[str] = []
        for source_line in text.splitlines():
            if not source_line:
                wrapped.append("")
                continue
            remaining = source_line
            while remaining:
                low, high = 1, len(remaining)
                while low <= high:
                    middle = (low + high) // 2
                    width = draw.textlength(remaining[:middle], font=font)
                    if width <= max_width:
                        low = middle + 1
                    else:
                        high = middle - 1
                split_at = max(1, high)
                wrapped.append(remaining[:split_at])
                remaining = remaining[split_at:]
        return wrapped or [""]

    def _display_name(self, user: User) -> str:
        name = user.nickname or user.user_id
        if name == user.user_id and len(name) > 8:
            return f"{name[:3]}...{name[-2:]}"
        return name
