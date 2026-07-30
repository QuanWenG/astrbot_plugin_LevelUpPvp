import ast
from pathlib import Path
import unittest

from services.effect_whitelist import (
    EffectWhitelist,
    effect_whitelist_only,
    external_effect_whitelist_only,
    should_stop_denied_llm,
)


class FakeEvent:
    def __init__(
        self,
        *,
        umo: str = "",
        group_id: str = "",
        provider_request=None,
        activated_handlers=None,
    ) -> None:
        self.unified_msg_origin = umo
        self._group_id = group_id
        self._extras = {
            "provider_request": provider_request,
            "activated_handlers": activated_handlers or [],
        }

    def get_group_id(self) -> str:
        return self._group_id

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


class GuardedHandler:
    def __init__(self, whitelist: EffectWhitelist) -> None:
        self.effect_whitelist = whitelist
        self.calls = 0

    @effect_whitelist_only
    async def handle(self, event, value):
        self.calls += 1
        yield value


class GuardedExternalApi:
    def __init__(self, whitelist: EffectWhitelist) -> None:
        self.effect_whitelist = whitelist
        self.calls = 0

    @external_effect_whitelist_only
    async def grant(
        self,
        *,
        group_id: str,
        unified_msg_origin: str = "",
    ):
        self.calls += 1
        return {"granted": True}


class EffectWhitelistTests(unittest.IsolatedAsyncioTestCase):
    def test_every_registered_message_entrypoint_is_guarded(self):
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        module = ast.parse(main_path.read_text(encoding="utf-8"))
        plugin = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "MyPlugin"
        )
        registered = []
        for method in plugin.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(item) for item in method.decorator_list]
            if any(
                item.startswith(("filter.command", "filter.event_message_type"))
                for item in decorators
            ):
                registered.append(method.name)
                self.assertIn(
                    "effect_whitelist_only",
                    decorators,
                    f"{method.name} is missing the effect whitelist guard",
                )
        self.assertTrue(registered)

        external_api = next(
            method
            for method in plugin.body
            if isinstance(method, ast.AsyncFunctionDef)
            and method.name == "grant_external_activity"
        )
        self.assertIn(
            "external_effect_whitelist_only",
            [ast.unparse(item) for item in external_api.decorator_list],
        )

        llm_guard = next(
            method
            for method in plugin.body
            if isinstance(method, ast.AsyncFunctionDef)
            and method.name == "stop_denied_levelup_llm"
        )
        self.assertIn(
            "filter.on_waiting_llm_request(priority=1000)",
            [ast.unparse(item) for item in llm_guard.decorator_list],
        )

    def test_empty_whitelist_denies_every_origin(self):
        whitelist = EffectWhitelist([])

        self.assertFalse(
            whitelist.allows(
                unified_msg_origin="aiocqhttp:GroupMessage:100",
                group_id="100",
            )
        )

    def test_matches_trimmed_group_id_or_exact_umo(self):
        whitelist = EffectWhitelist(
            [" 100 ", "", "aiocqhttp:FriendMessage:200"]
        )

        self.assertTrue(whitelist.allows(group_id="100"))
        self.assertTrue(
            whitelist.allows(
                unified_msg_origin="aiocqhttp:FriendMessage:200"
            )
        )
        self.assertFalse(whitelist.allows(group_id="200"))
        self.assertFalse(
            whitelist.allows(
                unified_msg_origin="aiocqhttp:GroupMessage:100"
            )
        )

    async def test_guard_silently_skips_denied_event(self):
        handler = GuardedHandler(EffectWhitelist(["allowed"]))

        results = [
            item
            async for item in handler.handle(
                FakeEvent(umo="denied", group_id="denied"),
                "result",
            )
        ]

        self.assertEqual(results, [])
        self.assertEqual(handler.calls, 0)

    async def test_guard_delegates_allowed_event(self):
        handler = GuardedHandler(EffectWhitelist(["group-1"]))

        results = [
            item
            async for item in handler.handle(
                FakeEvent(umo="platform:GroupMessage:group-1", group_id="group-1"),
                "result",
            )
        ]

        self.assertEqual(results, ["result"])
        self.assertEqual(handler.calls, 1)

    async def test_external_guard_returns_none_before_calling_denied_api(self):
        api = GuardedExternalApi(EffectWhitelist(["allowed"]))

        result = await api.grant(group_id="denied")

        self.assertIsNone(result)
        self.assertEqual(api.calls, 0)

    async def test_external_guard_accepts_umo_and_legacy_group_calls(self):
        api = GuardedExternalApi(
            EffectWhitelist(["group-1", "platform:FriendMessage:user-1"])
        )

        group_result = await api.grant(group_id="group-1")
        umo_result = await api.grant(
            group_id="",
            unified_msg_origin="platform:FriendMessage:user-1",
        )

        self.assertEqual(group_result, {"granted": True})
        self.assertEqual(umo_result, {"granted": True})
        self.assertEqual(api.calls, 2)

    def test_denied_origin_always_stops_default_llm(self):
        event = FakeEvent(
            umo="platform:GroupMessage:denied",
            group_id="denied",
        )

        self.assertTrue(
            should_stop_denied_llm(
                whitelist=EffectWhitelist(["allowed"]),
                event=event,
            )
        )

    def test_allowed_origin_and_provider_request_are_not_stopped(self):
        cases = (
            (
                EffectWhitelist(["group-1"]),
                FakeEvent(group_id="group-1"),
            ),
            (
                EffectWhitelist(["allowed"]),
                FakeEvent(
                    group_id="denied",
                    provider_request=object(),
                ),
            ),
        )

        for whitelist, event in cases:
            self.assertFalse(
                should_stop_denied_llm(
                    whitelist=whitelist,
                    event=event,
                )
            )


if __name__ == "__main__":
    unittest.main()
