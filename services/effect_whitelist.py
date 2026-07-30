from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from functools import wraps
from typing import Any


class EffectWhitelist:
    """Decide whether LevelUpPVP is available for a message origin."""

    def __init__(self, entries: Iterable[object] | None = None) -> None:
        if isinstance(entries, (str, bytes)):
            entries = (entries,)
        self._entries = frozenset(
            normalized
            for entry in entries or ()
            if entry is not None and (normalized := str(entry).strip())
        )

    def allows(
        self,
        *,
        unified_msg_origin: object = "",
        group_id: object = "",
    ) -> bool:
        """Match an exact AstrBot UMO or group ID.

        An empty whitelist is intentionally strict and denies every origin.
        """
        candidates = {
            normalized
            for value in (unified_msg_origin, group_id)
            if (normalized := str(value or "").strip())
        }
        return bool(self._entries.intersection(candidates))

    def allows_event(self, event: Any) -> bool:
        return self.allows(
            unified_msg_origin=getattr(event, "unified_msg_origin", ""),
            group_id=event.get_group_id(),
        )


def effect_whitelist_only(
    handler: Callable[..., AsyncGenerator[Any, None]],
) -> Callable[..., AsyncGenerator[Any, None]]:
    """Protect an AstrBot async-generator handler with the plugin whitelist."""

    @wraps(handler)
    async def guarded(self, event, *args, **kwargs):
        if not self.effect_whitelist.allows_event(event):
            return
        async for result in handler(self, event, *args, **kwargs):
            yield result

    guarded.__effect_whitelist_guarded__ = True
    return guarded


def external_effect_whitelist_only(
    handler: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Protect a keyword-only external integration API with the whitelist."""

    @wraps(handler)
    async def guarded(self, *args, **kwargs):
        if not self.effect_whitelist.allows(
            unified_msg_origin=kwargs.get("unified_msg_origin", ""),
            group_id=kwargs.get("group_id", ""),
        ):
            return None
        return await handler(self, *args, **kwargs)

    guarded.__effect_whitelist_guarded__ = True
    return guarded


def should_stop_denied_llm(
    *,
    whitelist: EffectWhitelist,
    event: Any,
) -> bool:
    """Block every default LLM request outside the LevelUpPVP whitelist."""
    if whitelist.allows_event(event):
        return False
    if event.get_extra("provider_request") is not None:
        return False
    return True
