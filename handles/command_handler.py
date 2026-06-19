import re
from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At

try:
    from ..models.user import LevelUpEvent, User, UserIdentity
    from ..services import config
except ImportError:
    from models.user import LevelUpEvent, User, UserIdentity
    from services import config


CHALLENGE_WAKE_WORD = "艾斯比"
CHALLENGE_COMMAND_PATTERN = re.compile(r"^/?挑战(?:\s|$)")


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
                yield event.plain_result(
                    f"今天已经签到过了。\n当前连续签到：{result.streak_days} 天"
                )
                return

            lines = [
                f"{self._display_name(result.user)} 签到成功！",
                f"获得经验：{result.exp_gain}",
                f"连续签到：{result.streak_days} 天",
                self._format_level_progress(result.user),
            ]
            if result.level_ups:
                lines.append(self._format_level_ups(result.level_ups))
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            logger.exception("LevelUpPvp sign failed")
            yield event.plain_result(f"签到失败：{exc}")

    async def profile(self, event: AstrMessageEvent) -> AsyncGenerator:
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

    async def challenge(self, event: AstrMessageEvent) -> AsyncGenerator:
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
            yield event.plain_result(self._format_battle_result(result))
        except Exception as exc:
            logger.exception("LevelUpPvp battle failed")
            yield event.plain_result(f"挑战失败：{exc}")

    def is_alias_challenge_event(self, event: AstrMessageEvent) -> bool:
        message = (event.get_message_str() or "").strip()
        if CHALLENGE_WAKE_WORD not in message:
            return False
        if CHALLENGE_COMMAND_PATTERN.match(message):
            return False
        return self._target_identity_from_event(event) is not None

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
        for match in re.finditer(r"<@!?([^>\s]+)>", message):
            target_id = match.group(1).strip()
            if target_id and target_id not in {"all", sender_id, self_id, "qq_official"}:
                return UserIdentity(
                    platform=event.get_platform_id() or event.get_platform_name() or "unknown",
                    group_id=event.get_group_id() or "",
                    user_id=target_id,
                    nickname=target_id,
                )
        return None

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
            CHALLENGE_WAKE_WORD,
            target_identity.user_id,
            target_identity.nickname,
            f"@{target_identity.user_id}",
            f"@{target_identity.nickname}",
            f"<@{target_identity.user_id}>",
            f"<@!{target_identity.user_id}>",
        ]:
            if token:
                text = text.replace(token, " ")
        text = re.sub(r"<@!?[^>\s]+>", " ", text)
        text = " ".join(text.split())
        return text

    def _format_profile(self, user: User) -> str:
        return "\n".join(
            [
                f"{self._display_name(user)} 的面板",
                self._format_level_progress(user),
                f"自定义属性点：{user.stat_points}",
                self._format_stats(user),
                f"战绩：{user.wins} 胜 / {user.losses} 负",
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

    def _format_level_ups(self, level_ups: list[LevelUpEvent]) -> str:
        lines = ["升级成长："]
        for item in level_ups:
            growth = "，".join(
                f"{config.STAT_LABELS[name]} +{gain}"
                for name, gain in item.auto_growth.items()
            )
            lines.append(
                f"Lv.{item.from_level} -> Lv.{item.to_level}：{growth}，"
                f"自定义属性点 +{item.stat_points_gain}"
            )
        return "\n".join(lines)

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
        if result.analysis:
            lines.append(f"分析：{result.analysis}")
        if result.battle_log:
            lines.append("战报：")
            lines.extend(f"- {item}" for item in result.battle_log[:5])
        if result.level_ups:
            lines.append(self._format_level_ups(result.level_ups))
        lines.append("结果：" + ("攻击方获胜" if winner_is_attacker else "防守方获胜"))
        return "\n".join(lines)

    def _display_name(self, user: User) -> str:
        name = user.nickname or user.user_id
        if name == user.user_id and len(name) > 8:
            return f"{name[:3]}...{name[-2:]}"
        return name
