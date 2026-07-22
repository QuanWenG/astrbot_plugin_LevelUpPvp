import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChallengeTicket:
    """A queued challenge and the future carrying its eventual result."""

    position: int
    _future: asyncio.Future

    async def result(self):
        # A cancelled message handler must not cancel a challenge that was already
        # accepted into the queue.
        return await asyncio.shield(self._future)


@dataclass(frozen=True)
class _ChallengeRequest:
    attacker_identity: Any
    defender_identity: Any
    strategy: str
    context: Any
    event: Any
    future: asyncio.Future


class ChallengeQueueService:
    """Runs LLM-backed battles one at a time in FIFO order."""

    def __init__(self, battle_service):
        self._battle_service = battle_service
        self._queue: asyncio.Queue[_ChallengeRequest] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._active = False
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("challenge queue is closed")
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(),
                name="level-up-pvp-challenge-queue",
            )

    async def enqueue(
        self,
        attacker_identity,
        defender_identity,
        strategy: str,
        *,
        context=None,
        event=None,
    ) -> ChallengeTicket:
        self.start()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        position = self._queue.qsize() + (1 if self._active else 0) + 1
        await self._queue.put(
            _ChallengeRequest(
                attacker_identity=attacker_identity,
                defender_identity=defender_identity,
                strategy=strategy,
                context=context,
                event=event,
                future=future,
            )
        )
        return ChallengeTicket(position=position, _future=future)

    async def shutdown(self) -> None:
        self._closed = True
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._fail_waiting(RuntimeError("plugin stopped before challenge execution"))

    async def _run(self) -> None:
        while True:
            request = await self._queue.get()
            self._active = True
            try:
                result = await self._battle_service.battle(
                    request.attacker_identity,
                    request.defender_identity,
                    request.strategy,
                    context=request.context,
                    event=request.event,
                )
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.set_exception(
                        RuntimeError("plugin stopped before challenge execution")
                    )
                raise
            except Exception as exc:
                if not request.future.done():
                    request.future.set_exception(exc)
            else:
                if not request.future.done():
                    request.future.set_result(result)
            finally:
                self._active = False
                self._queue.task_done()

    def _fail_waiting(self, exc: Exception) -> None:
        while True:
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not request.future.done():
                request.future.set_exception(exc)
            self._queue.task_done()

