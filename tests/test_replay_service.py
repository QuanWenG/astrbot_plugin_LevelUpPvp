import json
import os
import shutil
import unittest
import uuid

from models.user import UserIdentity
from services.db import connect_db, init_db
from services.replay_service import (
    ReplayAccessDenied,
    ReplayService,
    format_replay,
)
from services.user_service import UserService


class ReplayServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(root, f"replay-{uuid.uuid4().hex}")
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "battle.db")
        await init_db(self.db_path)
        users = UserService(self.db_path)
        self.alice = await users.get_or_create_user(
            UserIdentity("test", "group-a", "alice", "爱丽丝")
        )
        self.bob = await users.get_or_create_user(
            UserIdentity("test", "group-a", "bob", "鲍勃")
        )
        self.service = ReplayService(self.db_path)

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def _insert_battle(
        self,
        *,
        simulation_json,
        strategy="{}",
        attacker_plan='{"opening":"pressure","midgame":"control","endgame":"gambit"}',
        defender_plan='{"opening":"counter","midgame":"sustain","endgame":"skirmish"}',
        created_at_ts=100,
        group_id="group-a",
        ruleset_id="sideview-v11",
        environment_id="rain",
        seed=4242,
        rated=1,
    ) -> int:
        async with await connect_db(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO battles (
                    group_id, attacker_pk, defender_pk, winner_pk, loser_pk,
                    attacker_win_rate, roll_value, strategy,
                    winner_exp_gain, loser_exp_gain, loser_exp_loss,
                    simulation_json, battle_mode, engine_version,
                    random_seed, duration_ticks, finish_reason,
                    ruleset_id, environment_id, rated, reward_reason,
                    attacker_rating_before, attacker_rating_after,
                    defender_rating_before, defender_rating_after,
                    attacker_tactic_plan_json, defender_tactic_plan_json,
                    created_at, created_at_ts
                ) VALUES (
                    ?, ?, ?, ?, ?, 0.5, 0.4, ?, 120, 60, 0,
                    ?, 'sideview', ?, ?, 80, 'knockout', ?, ?, ?,
                    'rated:first_pair', 1000, 1016, 1000, 984, ?, ?,
                    '2026-08-10T10:00:00', ?
                )
                """,
                (
                    group_id,
                    self.alice.id,
                    self.bob.id,
                    self.alice.id,
                    self.bob.id,
                    strategy,
                    simulation_json,
                    ruleset_id,
                    seed,
                    ruleset_id,
                    environment_id,
                    rated,
                    attacker_plan,
                    defender_plan,
                    created_at_ts,
                ),
            )
            await cursor.close()
            cursor = await db.execute("SELECT last_insert_rowid() AS id")
            battle_id = int((await cursor.fetchone())["id"])
            await cursor.close()
            await db.commit()
        return battle_id

    @staticmethod
    def _modern_simulation() -> str:
        return json.dumps(
            {
                "engine_version": "sideview-v11",
                "ruleset_id": "sideview-v11",
                "environment_id": "rain",
                "random_seed": 4242,
                "duration_ticks": 80,
                "finish_reason": "knockout",
                "winner_pk": 1,
                "loser_pk": 2,
                "attacker": {
                    "user_pk": 1,
                    "name": "爱丽丝",
                    "level": 25,
                    "max_hp": 500,
                    "max_mp": 120,
                    "max_sp": 100,
                },
                "defender": {
                    "user_pk": 2,
                    "name": "鲍勃",
                    "level": 24,
                    "max_hp": 560,
                    "max_mp": 80,
                    "max_sp": 110,
                },
                "attacker_remaining_hp": 180,
                "defender_remaining_hp": 0,
                "attacker_remaining_mana": 42,
                "defender_remaining_mana": 70,
                "attacker_remaining_stamina": 30,
                "defender_remaining_stamina": 12,
                "attacker_damage_dealt": 620,
                "defender_damage_dealt": 320,
                "attacker_final_statuses": [{"status_id": "bleed"}],
                "defender_final_statuses": [],
                "events": [
                    {
                        "tick": 1,
                        "kind": "strategy_trigger",
                        "actor_pk": 1,
                        "target_pk": 2,
                        "skill_id": "opening",
                        "status_id": "pressure",
                    },
                    {
                        "tick": 4,
                        "kind": "attack_windup",
                        "actor_pk": 1,
                        "target_pk": 2,
                        "skill_id": "power_strike",
                    },
                    {
                        "tick": 9,
                        "kind": "status_apply",
                        "actor_pk": 2,
                        "target_pk": 1,
                        "status_id": "stun",
                        "value": 3,
                    },
                    {
                        "tick": 17,
                        "kind": "fortune_swing",
                        "actor_pk": 1,
                        "target_pk": 2,
                        "status_id": "critical_downgrade",
                    },
                    {
                        "tick": 31,
                        "kind": "damage",
                        "actor_pk": 2,
                        "target_pk": 1,
                        "value": 72,
                    },
                    {
                        "tick": 64,
                        "kind": "damage",
                        "actor_pk": 1,
                        "target_pk": 2,
                        "value": 140,
                        "skill_id": "power_strike",
                    },
                    {
                        "tick": 65,
                        "kind": "strategy_trigger",
                        "actor_pk": 1,
                        "target_pk": 2,
                        "skill_id": "endgame",
                        "status_id": "gambit",
                    },
                    {
                        "tick": 80,
                        "kind": "knockout",
                        "actor_pk": 1,
                        "target_pk": 2,
                    },
                ],
            },
            ensure_ascii=False,
        )

    async def test_latest_same_group_replay_is_structured_and_reproducible(self):
        battle_id = await self._insert_battle(simulation_json=self._modern_simulation())

        view = await self.service.get_latest_replay("group-a")

        self.assertEqual(battle_id, view.battle_id)
        self.assertEqual("爱丽丝", view.winner.name)
        self.assertEqual("sideview-v11", view.ruleset_id)
        self.assertEqual(4242, view.random_seed)
        self.assertEqual("rain", view.environment_id)
        self.assertEqual(("pressure", "control", "gambit"), view.attacker_tactic_plan.as_tuple())
        self.assertEqual(180, view.attacker.remaining_hp)
        self.assertEqual(("bleed",), view.attacker.final_statuses)
        self.assertEqual(16.0, view.settlement.attacker_rating_delta)
        self.assertEqual(-16.0, view.settlement.defender_rating_delta)
        self.assertTrue(view.recipe.audit_complete)
        self.assertFalse(view.recipe.reproducible)
        self.assertIn('"random_seed":4242', view.recipe.command_info)
        categories = {moment.category for moment in view.turning_points}
        self.assertTrue(
            {"first_skill", "first_control", "fortune", "largest_hit", "endgame", "finish"}
            .issubset(categories)
        )
        largest = next(moment for moment in view.turning_points if moment.category == "largest_hit")
        self.assertEqual(140, largest.value)

        text = format_replay(view)
        self.assertIn(f"战斗复盘 #{battle_id}", text)
        self.assertIn("关键转折", text)
        self.assertIn("最大单击 140", text)
        self.assertIn("审计参数：ruleset=sideview-v11", text)

    async def test_specific_id_checks_group_but_participant_can_view(self):
        battle_id = await self._insert_battle(simulation_json=self._modern_simulation())

        with self.assertRaises(ReplayAccessDenied):
            await self.service.get_replay_by_id(
                UserIdentity("test", "other-group", "outsider", "路人"),
                battle_id,
            )

        participant = await self.service.get_replay_by_id(
            UserIdentity("test", "other-group", "alice", "爱丽丝"),
            battle_id,
        )
        self.assertEqual(battle_id, participant.battle_id)

    async def test_latest_uses_group_order_and_missing_returns_none(self):
        first = await self._insert_battle(
            simulation_json=self._modern_simulation(),
            created_at_ts=100,
        )
        second = await self._insert_battle(
            simulation_json=self._modern_simulation(),
            created_at_ts=200,
            seed=999,
        )

        self.assertNotEqual(first, second)
        view = await self.service.get_replay("group-a")
        self.assertEqual(second, view.battle_id)
        self.assertIsNone(await self.service.get_replay("empty-group"))

    async def test_damaged_legacy_json_degrades_without_crashing(self):
        battle_id = await self._insert_battle(
            simulation_json="{broken",
            strategy="全力猛攻",
            attacker_plan="[broken",
            defender_plan="{}",
            ruleset_id="legacy-v1",
            environment_id="",
            seed=None,
            rated=0,
        )

        view = await self.service.get_replay("group-a", battle_id)

        self.assertEqual(battle_id, view.battle_id)
        self.assertEqual("pressure", view.attacker_tactic_plan.opening)
        self.assertEqual("unknown", view.defender_tactic_plan.endgame)
        self.assertFalse(view.recipe.reproducible)
        self.assertTrue(view.compatibility_notes)
        self.assertIn("simulation_json 已损坏", "；".join(view.compatibility_notes))
        self.assertIn("旧记录不完整", format_replay(view))

    async def test_invalid_battle_id_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.service.get_replay("group-a", 0)


if __name__ == "__main__":
    unittest.main()
