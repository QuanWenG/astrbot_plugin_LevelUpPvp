import types
import unittest
from unittest import mock

from tests.test_command_handler import FakeEvent, _user

from handles.command_handler import LevelUpPvpCommandHandler
from services.replay_service import ReplayAccessDenied


class ReplayCommandHandlerTests(unittest.IsolatedAsyncioTestCase):
    def _handler(self, *, replay_result=None, replay_error=None):
        user = _user()
        user_service = types.SimpleNamespace(
            has_registered_nickname=mock.AsyncMock(return_value=True),
            get_or_create_user=mock.AsyncMock(return_value=user),
            register_nickname=mock.AsyncMock(),
        )
        if replay_error is None:
            get_replay = mock.AsyncMock(return_value=replay_result)
        else:
            get_replay = mock.AsyncMock(side_effect=replay_error)
        replay_service = types.SimpleNamespace(
            get_replay=get_replay,
            format_replay=mock.Mock(return_value="结构化复盘正文"),
        )
        operation_service = types.SimpleNamespace(
            record_event=mock.AsyncMock(),
        )
        handler = LevelUpPvpCommandHandler(
            context=None,
            user_service=user_service,
            checkin_service=None,
            stat_service=None,
            battle_service=None,
            replay_service=replay_service,
            operation_service=operation_service,
        )
        return handler, user_service, replay_service, operation_service

    async def test_empty_argument_reads_latest_group_battle(self):
        view = types.SimpleNamespace(battle_id=37)
        handler, _, replay_service, operation_service = self._handler(
            replay_result=view
        )

        replies = [reply async for reply in handler.replay(FakeEvent(), "")]

        self.assertEqual(["结构化复盘正文"], replies)
        replay_service.get_replay.assert_awaited_once()
        call = replay_service.get_replay.await_args
        identity = call.args[0]
        self.assertEqual("test", identity.platform)
        self.assertEqual("group-1", identity.group_id)
        self.assertEqual("user-1", identity.user_id)
        self.assertIsNone(call.kwargs["battle_id"])
        operation_service.record_event.assert_awaited_once_with(
            user_pk=1,
            group_id="group-1",
            event_type="battle_review",
            event_key="review:37",
        )

    async def test_numeric_argument_reads_exact_battle_id(self):
        view = types.SimpleNamespace(battle_id=2048)
        handler, _, replay_service, _ = self._handler(replay_result=view)

        replies = [reply async for reply in handler.replay(FakeEvent(), " 2048 ")]

        self.assertEqual(["结构化复盘正文"], replies)
        self.assertEqual(2048, replay_service.get_replay.await_args.kwargs["battle_id"])
        replay_service.format_replay.assert_called_once_with(view)

    async def test_repeated_review_uses_same_idempotency_key(self):
        view = types.SimpleNamespace(battle_id=88)
        handler, _, _, operation_service = self._handler(replay_result=view)
        event = FakeEvent()

        first = [reply async for reply in handler.replay(event, "88")]
        second = [reply async for reply in handler.replay(event, "88")]

        self.assertEqual(["结构化复盘正文"], first)
        self.assertEqual(["结构化复盘正文"], second)
        self.assertEqual(2, operation_service.record_event.await_count)
        self.assertEqual(
            ["review:88", "review:88"],
            [
                item.kwargs["event_key"]
                for item in operation_service.record_event.await_args_list
            ],
        )
        self.assertTrue(
            all(
                item.kwargs["event_type"] == "battle_review"
                for item in operation_service.record_event.await_args_list
            )
        )

    async def test_invalid_id_returns_usage_without_query_or_progress(self):
        handler, user_service, replay_service, operation_service = self._handler()

        for bad_value in ("abc", "0", "-7", "1 2"):
            with self.subTest(value=bad_value):
                replies = [
                    reply async for reply in handler.replay(FakeEvent(), bad_value)
                ]
                self.assertEqual(
                    ["查看复盘失败：用法：/复盘 [战斗ID]"],
                    replies,
                )

        replay_service.get_replay.assert_not_awaited()
        user_service.get_or_create_user.assert_not_awaited()
        operation_service.record_event.assert_not_awaited()

    async def test_no_record_returns_friendly_message_without_progress(self):
        handler, user_service, replay_service, operation_service = self._handler(
            replay_result=None
        )

        replies = [reply async for reply in handler.replay(FakeEvent(), "")]

        self.assertEqual(
            ["没有找到可查看的战斗记录。先和群友打一场吧。"],
            replies,
        )
        replay_service.get_replay.assert_awaited_once()
        user_service.get_or_create_user.assert_not_awaited()
        operation_service.record_event.assert_not_awaited()

    async def test_cross_group_error_is_explained_without_progress(self):
        handler, user_service, replay_service, operation_service = self._handler(
            replay_error=ReplayAccessDenied("只能查看自己参战或本群发生的战斗")
        )

        replies = [reply async for reply in handler.replay(FakeEvent(), "99")]

        self.assertEqual(
            ["查看复盘失败：只能查看自己参战或本群发生的战斗"],
            replies,
        )
        replay_service.get_replay.assert_awaited_once()
        user_service.get_or_create_user.assert_not_awaited()
        operation_service.record_event.assert_not_awaited()

    async def test_participant_cross_group_replay_does_not_credit_current_group(self):
        view = types.SimpleNamespace(battle_id=101, group_id="old-group")
        handler, user_service, replay_service, operation_service = self._handler(
            replay_result=view
        )

        replies = [reply async for reply in handler.replay(FakeEvent(), "101")]

        self.assertEqual(["结构化复盘正文"], replies)
        replay_service.get_replay.assert_awaited_once()
        user_service.get_or_create_user.assert_not_awaited()
        operation_service.record_event.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
