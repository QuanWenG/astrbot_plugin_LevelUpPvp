from datetime import datetime
import types
import unittest
from unittest import mock

from tests.test_command_handler import FakeEvent, _user

from astrbot.api.message_components import At
from handles.command_handler import LevelUpPvpCommandHandler
from models.combat import BattleEvent, FighterSnapshot, SimulationResult
from models.dungeon import (
    DungeonAdventureView,
    DungeonApplicationResult,
    DungeonRewardReceipt,
    DungeonRiskView,
    DungeonRouteView,
)
from services.equipment_service import reward_quality_policy


class PlainHandler(LevelUpPvpCommandHandler):
    async def reply_text(self, event, text, title="LevelUpPvp"):
        return str(text)


class RecordingOperations:
    def __init__(self):
        self.events = []

    async def record_event(self, **kwargs):
        self.events.append(kwargs)
        return ()


class FakeNefiaService:
    def __init__(self):
        self.dungeon = types.SimpleNamespace(
            dungeon_id="verdant_wetland",
            name="新绿湿地",
            recommended_level=1,
        )
        self.dungeons = (self.dungeon,)
        self.current = None
        self.calls = []

    @staticmethod
    def _risk(
        risk_id,
        name,
        description,
        *,
        monster_level,
        monster_level_delta=0,
        reward_multiplier=1.0,
        hp_cost=0.0,
        mp_cost=0.0,
        mitigated=False,
    ):
        quality_bonus = max(0.0, reward_multiplier - 1.0)
        quality = reward_quality_policy(quality_bonus)
        return DungeonRiskView(
            risk_id=risk_id,
            name=name,
            description=description,
            monster_level=monster_level,
            monster_level_delta=monster_level_delta,
            reward_multiplier=reward_multiplier,
            entry_hp_cost_ratio=hp_cost,
            entry_mp_cost_ratio=mp_cost,
            capability_mitigated=mitigated,
            reward_quality_bonus=quality_bonus,
            reward_effective_quality_bonus=quality.effective_bonus,
            reward_quality_progress=quality.quality_progress,
            reward_minimum_quality=quality.minimum_quality,
            reward_guaranteed_upgrades=quality.guaranteed_upgrades,
            reward_upgrade_chance=quality.upgrade_chance,
        )

    @staticmethod
    def _routes():
        return (
            DungeonRouteView(
                "f1a",
                "苔痕岔路",
                "通往积水回廊。",
                "normal",
                2,
                "normal",
                "rain",
                "积水回廊",
                (),
                "森林",
                "药草采集点",
                True,
                (
                    FakeNefiaService._risk(
                        "careful_scout",
                        "谨慎侦察",
                        "敌人与战利品都略弱。",
                        monster_level=1,
                        monster_level_delta=-1,
                        reward_multiplier=0.88,
                    ),
                    FakeNefiaService._risk(
                        "break_the_seal",
                        "破坏封印",
                        "更强守卫换更多战利品。",
                        monster_level=4,
                        monster_level_delta=2,
                        reward_multiplier=1.30,
                        mp_cost=0.08,
                    ),
                ),
                "史莱姆",
                True,
            ),
            DungeonRouteView(
                "f1b",
                "古井暗门",
                "通往孢子迷雾。",
                "hidden_room",
                3,
                "normal",
                "fog",
                "孢子迷雾",
                ("藏书者",),
                "洞窟",
                "岩壁后的隐藏房",
                False,
                (
                    FakeNefiaService._risk(
                        "disarm_traps",
                        "拆除陷阱",
                        "稳妥通过。",
                        monster_level=0,
                        reward_multiplier=0.92,
                    ),
                    FakeNefiaService._risk(
                        "rush_the_traps",
                        "强闯机关",
                        "负伤换完整宝藏。",
                        monster_level=0,
                        reward_multiplier=1.32,
                        hp_cost=0.07,
                    ),
                ),
                "",
                False,
            ),
        )

    @classmethod
    def _view(cls, phase, *, selected_route=None, selected_risk=None, floor=1):
        return DungeonAdventureView(
            adventure_id="adventure-1",
            dungeon_id="verdant_wetland",
            cycle_key="2026-08-11",
            phase=phase,
            floor_number=floor,
            floor_count=3,
            completed_floors=max(0, floor - 1),
            difficulty=1,
            strategy="稳扎稳打",
            routes=cls._routes() if phase not in {"cleared", "defeated", "retreated"} else (),
            selected_route_id=selected_route,
            selected_risk_id=selected_risk,
            hp_ratio=0.92,
            mana_ratio=0.84,
            stamina_ratio=0.78,
            version=0,
        )

    def list_dungeons(self):
        return self.dungeons

    async def view_nefia(self, identity, adventure_id="", *, dungeon_id=""):
        self.calls.append(("view", dungeon_id))
        if self.current is None:
            raise KeyError("not started")
        return self.current

    async def start_nefia(self, identity, dungeon_id, difficulty=1, strategy=""):
        self.calls.append(("start", dungeon_id, difficulty, strategy))
        self.current = DungeonApplicationResult(self._view("route_choice"))
        return self.current

    async def choose_nefia_route(self, identity, adventure_id, option_id):
        self.calls.append(("route", option_id))
        self.current = DungeonApplicationResult(
            self._view("risk_choice", selected_route=option_id)
        )
        return self.current

    async def choose_nefia_risk(self, identity, adventure_id, risk_id):
        self.calls.append(("risk", risk_id))
        route_id = self.current.view.selected_route_id
        self.current = DungeonApplicationResult(
            self._view(
                "combat_ready",
                selected_route=route_id,
                selected_risk=risk_id,
            )
        )
        return self.current

    async def fight_nefia(self, identity, adventure_id, strategy=""):
        self.calls.append(("fight", adventure_id))
        selected_route_id = self.current.view.selected_route_id
        if selected_route_id == "f1b":
            rewards = (
                DungeonRewardReceipt(
                    "reward:salvage",
                    "salvage",
                    3,
                    True,
                    "获得工坊废料 3",
                    scrap_gain=3,
                ),
            )
            self.current = DungeonApplicationResult(
                self._view("route_choice", floor=2),
                rewards=rewards,
                narrative="你付出代价打开了岩壁后的隐藏房。",
            )
            return self.current
        attacker = FighterSnapshot(1, "测试用户", 2, 10, 8, 6, 6, 5, "稳扎稳打")
        defender = FighterSnapshot(-1, "史莱姆", 2, 7, 5, 4, 4, 2, "稳扎稳打")
        simulation = SimulationResult(
            attacker,
            defender,
            1,
            -1,
            12,
            "hp_depleted",
            80,
            0,
            30,
            10,
            (
                BattleEvent(
                    8,
                    "damage",
                    actor_pk=1,
                    target_pk=-1,
                    value=30,
                    remaining_hp=0,
                    skill_id="magic_arrow",
                    damage_type="magic",
                    damage_breakdown={"magic": 30},
                ),
            ),
            7,
            attacker_remaining_stamina=70,
            defender_remaining_stamina=30,
            attacker_remaining_mana=20,
            defender_remaining_mana=0,
            environment_id="rain",
        )
        rewards = (
            DungeonRewardReceipt(
                "reward:equipment",
                "equipment",
                1,
                True,
                "获得装备：旅人短剑",
                equipment_ids=(41,),
                equipment_names=("旅人短剑",),
            ),
            DungeonRewardReceipt(
                "reward:book",
                "spellbook",
                1,
                True,
                "发现魔法书：魔法箭",
                spell_ids=("magic_arrow",),
                spell_names=("魔法箭",),
            ),
        )
        self.current = DungeonApplicationResult(
            self._view("route_choice", floor=2),
            simulation=simulation,
            rewards=rewards,
            skill_growth_count=1,
            spell_growth_count=1,
        )
        return self.current

    async def retreat_nefia(self, identity, adventure_id):
        self.calls.append(("retreat", adventure_id))
        self.current = DungeonApplicationResult(self._view("retreated"))
        return self.current


class NefiaCommandHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dungeons = FakeNefiaService()
        self.operations = RecordingOperations()
        self.handler = PlainHandler(
            context=None,
            user_service=types.SimpleNamespace(
                get_or_create_user=self._get_user,
            ),
            checkin_service=None,
            stat_service=None,
            battle_service=None,
            dungeon_service=self.dungeons,
            operation_service=self.operations,
        )

    @staticmethod
    async def _get_user(identity):
        return _user()

    async def test_no_argument_starts_and_explains_one_command_choice(self):
        replies = [item async for item in self.handler.nefia(FakeEvent(), "")]
        self.assertEqual(len(replies), 1)
        self.assertIn("随机奈菲亚「新绿湿地」", replies[0])
        self.assertIn("/奈菲亚 1A", replies[0])
        self.assertIn("能力不足，仅基础收益", replies[0])
        self.assertIn("敌人Lv.1（-1级）", replies[0])
        self.assertIn("奖励 x1.30", replies[0])
        self.assertIn(
            "品质加值0.30→升1阶15%（最低普通，最高传说）",
            replies[0],
        )
        self.assertIn("MP -8%", replies[0])
        self.assertEqual(self.dungeons.calls[-1][0], "start")

    def test_default_theme_is_shared_across_levels_and_rotates(self):
        catalog = tuple(
            types.SimpleNamespace(
                dungeon_id=f"nefia-{level}",
                name=f"奈菲亚{level}",
                recommended_level=level,
            )
            for level in (1, 5, 10, 20)
        )
        self.dungeons.dungeons = catalog

        first = self.handler._default_nefia_dungeon(1, "shared-group", "2026-08-11")
        self.dungeons.dungeons = tuple(reversed(catalog))
        retry = self.handler._default_nefia_dungeon(
            100, "shared-group", "2026-08-11"
        )
        self.assertEqual(first.dungeon_id, retry.dungeon_id)

        themes = {
            self.handler._default_nefia_dungeon(
                10, "shared-group", f"2026-08-{day:02d}"
            ).dungeon_id
            for day in range(1, 29)
        }
        self.assertGreater(len(themes), 1)
        self.assertNotEqual(themes, {"nefia-10"})

    async def test_default_theme_uses_group_and_0400_activity_day(self):
        before_reset = FakeEvent(group_id="shared-group")
        before_reset.timestamp = int(
            datetime.fromisoformat("2026-08-12T03:59:00+08:00").timestamp()
        )
        with mock.patch.object(
            self.handler,
            "_default_nefia_dungeon",
            wraps=self.handler._default_nefia_dungeon,
        ) as choose_theme:
            _ = [item async for item in self.handler.nefia(before_reset, "")]

        choose_theme.assert_called_once()
        self.assertEqual(
            choose_theme.call_args.args[1:],
            ("shared-group", "2026-08-11"),
        )

    async def test_active_run_resumes_without_reselecting_daily_theme(self):
        event = FakeEvent(group_id="shared-group")
        _ = [item async for item in self.handler.nefia(event, "")]
        with mock.patch.object(
            self.handler,
            "_default_nefia_dungeon",
            wraps=self.handler._default_nefia_dungeon,
        ) as choose_theme:
            replies = [item async for item in self.handler.nefia(event, "")]

        choose_theme.assert_not_called()
        self.assertEqual(
            sum(call[0] == "start" for call in self.dungeons.calls),
            1,
        )
        self.assertIn("随机奈菲亚「新绿湿地」", replies[0])

    async def test_compact_choice_selects_route_risk_and_fights(self):
        _ = [item async for item in self.handler.nefia(FakeEvent(), "")]
        replies = [item async for item in self.handler.nefia(FakeEvent(), "1A")]

        self.assertEqual([call[0] for call in self.dungeons.calls[-3:]], ["route", "risk", "fight"])
        self.assertIn("本层战报", replies[0])
        self.assertIn("#41 旅人短剑", replies[0])
        self.assertIn("发现魔法书：魔法箭", replies[0])
        self.assertIn("/魔法书", replies[0])
        event_types = {item["event_type"] for item in self.operations.events}
        self.assertIn("risk_choice", event_types)
        self.assertIn("risk_choice_unique", event_types)
        self.assertIn("nefia_node", event_types)
        self.assertIn("battle_win", event_types)

    async def test_restart_phase_can_resume_with_risk_letter_only(self):
        self.dungeons.current = DungeonApplicationResult(
            self.dungeons._view("risk_choice", selected_route="f1b")
        )
        replies = [item async for item in self.handler.nefia(FakeEvent(), "B")]
        self.assertEqual([call[0] for call in self.dungeons.calls[-2:]], ["risk", "fight"])
        self.assertIn("本层事件", replies[0])
        self.assertIn("本层收获", replies[0])

    async def test_event_route_resolves_without_battle_report(self):
        _ = [item async for item in self.handler.nefia(FakeEvent(), "")]
        replies = [item async for item in self.handler.nefia(FakeEvent(), "2B")]
        self.assertIn("本层事件", replies[0])
        self.assertNotIn("本层战报", replies[0])
        event_types = {item["event_type"] for item in self.operations.events}
        self.assertIn("nefia_node", event_types)
        self.assertIn("nefia_discovery", event_types)

    async def test_retreat_does_not_silently_create_a_run(self):
        replies = [item async for item in self.handler.nefia(FakeEvent(), "撤退")]
        self.assertIn("今天尚未进入奈菲亚", replies[0])
        self.assertFalse(any(call[0] == "start" for call in self.dungeons.calls))

    def test_mention_form_parses_compact_nefia_choice(self):
        event = FakeEvent(
            message="<@bot-1> 奈菲亚 2B",
            messages=[At("bot-1", "机器人")],
        )
        self.assertEqual(
            self.handler.parse_mentioned_command(event),
            ("奈菲亚", "2B"),
        )

    def test_risk_text_discloses_quality_cap_and_personal_find_bonus(self):
        risk = DungeonRiskView(
            risk_id="extreme",
            name="极限契约",
            description="测试",
            monster_level=100,
            monster_level_delta=10,
            reward_multiplier=6.0,
            entry_hp_cost_ratio=0.2,
            entry_mp_cost_ratio=0.0,
            reward_quality_bonus=5.06,
            reward_effective_quality_bonus=4.0,
            reward_quality_progress=2.0,
            reward_minimum_quality="rare",
            reward_guaranteed_upgrades=2,
            reward_upgrade_chance=0.0,
            rare_find_quality_bonus=0.06,
        )

        text = self.handler._format_nefia_risk_effect(risk)

        self.assertIn("品质加值5.06（按4.00上限结算）", text)
        self.assertIn("含寻宝+0.06", text)
        self.assertIn("保底+2阶（最低精良，最高传说）", text)


if __name__ == "__main__":
    unittest.main()
