import types
import unittest
from unittest import mock

from tests.test_command_handler import At, FakeEvent, _user

from handles.command_handler import (
    MENTION_COMMAND_PATTERN,
    LevelUpPvpCommandHandler,
)
from models.operation import ClaimResult, OperationSettlementResult, RewardIntent


def _intent(reward_key: str, source: str = "daily_operation") -> RewardIntent:
    return RewardIntent(
        reward_key=reward_key,
        source=source,
        reason="测试运营奖励",
        scrap=15,
    )


def _claim(
    intent: RewardIntent | None,
    *,
    eligible: bool,
    already_claimed: bool = False,
    completed: int = 2,
    required: int = 2,
) -> ClaimResult:
    return ClaimResult(
        eligible=eligible,
        granted=eligible and not already_claimed,
        already_claimed=already_claimed,
        completed_count=completed,
        required_count=required,
        reward_intent=intent,
    )


class OperationRewardCommandHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _handler(
        *,
        daily_claim=None,
        weekly_claim=None,
        settlement_result=None,
        daily_error=None,
        weekly_error=None,
        settlement_error=None,
        overview_error=None,
    ):
        user = _user()
        user_service = types.SimpleNamespace(
            has_registered_nickname=mock.AsyncMock(return_value=True),
            register_nickname=mock.AsyncMock(),
            get_or_create_user=mock.AsyncMock(return_value=user),
        )
        daily = mock.AsyncMock(
            return_value=daily_claim
            or _claim(None, eligible=False, completed=0, required=2),
            side_effect=daily_error,
        )
        weekly = mock.AsyncMock(
            return_value=weekly_claim
            or _claim(None, eligible=False, completed=0, required=5),
            side_effect=weekly_error,
        )
        overview = mock.AsyncMock(
            return_value=types.SimpleNamespace(),
            side_effect=overview_error,
        )
        operation_service = types.SimpleNamespace(
            claim_daily_reward=daily,
            claim_weekly_reward=weekly,
            overview=overview,
            record_event=mock.AsyncMock(),
        )
        settle = mock.AsyncMock(
            return_value=settlement_result
            or OperationSettlementResult(reward_key="unused", applied=True),
            side_effect=settlement_error,
        )
        settlement_service = types.SimpleNamespace(settle=settle)
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
            operation_service=operation_service,
            operation_settlement_service=settlement_service,
        )
        return handler, operation_service, settlement_service

    async def test_applied_daily_settlement_projects_stable_daily_event(self):
        reward = _intent("daily:2026-08-10:user-1")
        handler, operations, settlement = self._handler(
            daily_claim=_claim(reward, eligible=True),
            settlement_result=OperationSettlementResult(
                reward_key=reward.reward_key,
                applied=True,
                scrap=15,
            ),
        )

        replies = [reply async for reply in handler.operations(FakeEvent(), "领取")]

        self.assertEqual(1, len(replies))
        self.assertIn("每日：工坊碎片 +15", replies[0])
        settlement.settle.assert_awaited_once_with(user_pk=1, intent=reward)
        operations.record_event.assert_awaited_once_with(
            user_pk=1,
            group_id="group-1",
            event_type="daily_reward",
            event_key=f"settled:{reward.reward_key}",
        )

    async def test_duplicate_daily_settlement_still_projects_same_daily_event(self):
        reward = _intent("daily:stable-key")
        handler, operations, _ = self._handler(
            daily_claim=_claim(
                reward,
                eligible=True,
                already_claimed=True,
            ),
            settlement_result=OperationSettlementResult(
                reward_key=reward.reward_key,
                applied=False,
            ),
        )

        replies = [reply async for reply in handler.operations(FakeEvent(), "claim")]

        self.assertIn("每日：已经领取过，不会重复发放", replies[0])
        operations.record_event.assert_awaited_once_with(
            user_pk=1,
            group_id="group-1",
            event_type="daily_reward",
            event_key="settled:daily:stable-key",
        )

    async def test_settlement_failure_does_not_project_daily_event(self):
        reward = _intent("daily:settlement-failure")
        handler, operations, _ = self._handler(
            daily_claim=_claim(reward, eligible=True),
            settlement_error=RuntimeError("奖励结算中断"),
        )

        replies = [reply async for reply in handler.operations(FakeEvent(), "领奖")]

        self.assertEqual(["查看今日运营失败：奖励结算中断"], replies)
        operations.record_event.assert_not_awaited()

    async def test_weekly_reward_never_projects_daily_reward_event(self):
        reward = _intent("weekly:2026-W33:user-1", source="weekly_operation")
        handler, operations, settlement = self._handler(
            weekly_claim=_claim(
                reward,
                eligible=True,
                completed=5,
                required=5,
            ),
            settlement_result=OperationSettlementResult(
                reward_key=reward.reward_key,
                applied=True,
                season_tokens=30,
            ),
        )

        replies = [reply async for reply in handler.operations(FakeEvent(), "领取")]

        self.assertIn("每周：赛季币 +30", replies[0])
        settlement.settle.assert_awaited_once_with(user_pk=1, intent=reward)
        operations.record_event.assert_not_awaited()

    async def test_reward_claim_failure_has_player_facing_error(self):
        handler, operations, settlement = self._handler(
            daily_error=RuntimeError("奖励状态读取失败")
        )

        replies = [reply async for reply in handler.operations(FakeEvent(), "领取")]

        self.assertEqual(["查看今日运营失败：奖励状态读取失败"], replies)
        operations.claim_weekly_reward.assert_not_awaited()
        settlement.settle.assert_not_awaited()
        operations.record_event.assert_not_awaited()

    async def test_task_overview_failure_has_player_facing_error(self):
        handler, operations, settlement = self._handler(
            overview_error=RuntimeError("任务生成失败")
        )

        replies = [reply async for reply in handler.operations(FakeEvent(), "今日")]

        self.assertEqual(["查看今日运营失败：任务生成失败"], replies)
        operations.overview.assert_awaited_once_with(
            user_pk=1,
            group_id="group-1",
        )
        settlement.settle.assert_not_awaited()
        operations.record_event.assert_not_awaited()


class ReplayMentionCommandPatternTests(unittest.TestCase):
    def test_mention_pattern_recognizes_replay_with_optional_slash_and_id(self):
        for text in ("复盘", "/复盘", "复盘 42", "/复盘 42"):
            with self.subTest(text=text):
                match = MENTION_COMMAND_PATTERN.match(text)
                self.assertIsNotNone(match)
                self.assertEqual("复盘", match.group(1))

    def test_mentioned_replay_command_parses_arguments(self):
        handler, _, _ = OperationRewardCommandHandlerTests._handler()
        event = FakeEvent(
            message="<@bot-1> 复盘 42",
            messages=[At("bot-1", "机器人")],
        )

        self.assertEqual(("复盘", "42"), handler.parse_mentioned_command(event))


if __name__ == "__main__":
    unittest.main()
