import asyncio
import unittest

from services.challenge_queue import ChallengeQueueService


class FakeBattleService:
    def __init__(self):
        self.calls = []
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def battle(
        self,
        attacker,
        defender,
        strategy,
        *,
        context=None,
        event=None,
    ):
        self.calls.append((attacker, defender, strategy, context, event))
        if len(self.calls) == 1:
            self.first_started.set()
            await self.release_first.wait()
        if strategy == "fail":
            raise ValueError("simulated failure")
        return f"{attacker}->{defender}:{strategy}"


class ChallengeQueueServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.battle_service = FakeBattleService()
        self.queue = ChallengeQueueService(self.battle_service)

    async def asyncTearDown(self):
        await self.queue.shutdown()

    async def test_challenges_are_kept_and_processed_in_fifo_order(self):
        first = await self.queue.enqueue(
            "a", "b", "first", context="ctx-1", event="evt-1"
        )
        await self.battle_service.first_started.wait()
        second = await self.queue.enqueue(
            "c", "d", "second", context="ctx-2", event="evt-2"
        )
        third = await self.queue.enqueue(
            "e", "f", "third", context="ctx-3", event="evt-3"
        )

        self.assertEqual(first.position, 1)
        self.assertEqual(second.position, 2)
        self.assertEqual(third.position, 3)
        self.battle_service.release_first.set()

        results = await asyncio.gather(
            first.result(), second.result(), third.result()
        )

        self.assertEqual(results, ["a->b:first", "c->d:second", "e->f:third"])
        self.assertEqual(
            [(call[0], call[1]) for call in self.battle_service.calls],
            [("a", "b"), ("c", "d"), ("e", "f")],
        )
        self.assertEqual(
            self.battle_service.calls[1][3:], ("ctx-2", "evt-2")
        )

    async def test_failed_challenge_does_not_drop_the_next_one(self):
        failed = await self.queue.enqueue("a", "b", "fail")
        await self.battle_service.first_started.wait()
        following = await self.queue.enqueue("c", "d", "continue")
        self.battle_service.release_first.set()

        with self.assertRaisesRegex(ValueError, "simulated failure"):
            await failed.result()
        self.assertEqual(await following.result(), "c->d:continue")
        self.assertEqual(len(self.battle_service.calls), 2)


if __name__ == "__main__":
    unittest.main()
