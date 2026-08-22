import os
import shutil
import sqlite3
import unittest
import uuid
from datetime import datetime, timedelta

from models.operation import stable_operation_seed
from services.db import init_db
from services.operation_service import (
    BOSS_AFFIXES,
    CURRENTLY_RECORDABLE_EVENT_TYPES,
    DAILY_TASK_CATALOG,
    ENVIRONMENTS,
    RISK_EVENTS,
    WEEKLY_SIMULATION_SCORE_CAP,
    OperationService,
)


class OperationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".test_tmp")
        )
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(root, f"operations-{uuid.uuid4().hex}")
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "operations.db")
        await init_db(self.db_path)
        connection = sqlite3.connect(self.db_path)
        try:
            self.user_pk = connection.execute(
                """
                INSERT INTO users (
                    platform, group_id, user_id, nickname, created_at, updated_at
                ) VALUES ('qq', 'group-a', 'user-a', '冒险者', 'now', 'now')
                """
            ).lastrowid
            connection.commit()
        finally:
            connection.close()
        self.service = OperationService(self.db_path)
        self.monday = datetime(2026, 8, 10, 12, 0, 0)

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_catalogs_are_varied_and_all_effects_publish_hard_caps(self):
        self.assertGreaterEqual(len(ENVIRONMENTS), 6)
        self.assertGreaterEqual(len(RISK_EVENTS), 8)
        self.assertGreaterEqual(len(BOSS_AFFIXES), 4)
        self.assertGreaterEqual(len(DAILY_TASK_CATALOG), 3)
        self.assertTrue(all(len(event.choices) == 2 for event in RISK_EVENTS))

        effects = [
            effect
            for environment in ENVIRONMENTS
            for effect in environment.effects
        ]
        effects += [
            effect
            for event in RISK_EVENTS
            for choice in event.choices
            for effect in choice.effects
        ]
        effects += [
            effect for affix in BOSS_AFFIXES for effect in affix.effects
        ]
        self.assertTrue(effects)
        for effect in effects:
            self.assertLessEqual(abs(effect.magnitude), effect.cap)
            self.assertIn("上限", effect.cap_text)

    def test_daily_nefia_is_shared_deterministic_and_has_exactly_three_nodes(self):
        first = self.service.daily_nefia("group-a", self.monday)
        retry = OperationService(self.db_path).daily_nefia(
            "group-a", self.monday + timedelta(hours=3)
        )

        self.assertEqual(first, retry)
        self.assertEqual(len(first.nodes), 3)
        self.assertEqual(len(first.risk_event.choices), 2)
        self.assertEqual(len(first.boss.affixes), 2)
        self.assertEqual(
            first.group_seed,
            stable_operation_seed("group-a", "2026-08-10", "sideview-v11"),
        )
        self.assertNotEqual(
            first.group_seed,
            self.service.daily_nefia("group-b", self.monday).group_seed,
        )
        self.assertNotEqual(
            first.group_seed,
            self.service.daily_nefia(
                "group-a", self.monday + timedelta(days=1)
            ).group_seed,
        )

    def test_daily_reset_occurs_at_five_instead_of_midnight(self):
        before = datetime(2026, 8, 10, 4, 59, 59)
        boundary = datetime(2026, 8, 10, 5, 0, 0)

        self.assertEqual(self.service.periods(before).daily.key, "2026-08-09")
        self.assertEqual(self.service.periods(boundary).daily.key, "2026-08-10")
        self.assertEqual(
            self.service.daily_nefia("group-a", before),
            self.service.daily_nefia("group-a", datetime(2026, 8, 9, 23, 0)),
        )

    def test_player_drop_seed_is_fixed_per_user_and_does_not_accept_retry_count(self):
        nefia = self.service.daily_nefia("group-a", self.monday)

        self.assertEqual(
            nefia.drop_seed_for(self.user_pk),
            self.service.player_drop_seed("group-a", self.user_pk, self.monday),
        )
        self.assertEqual(
            nefia.drop_seed_for(self.user_pk),
            nefia.drop_seed_for(self.user_pk),
        )
        self.assertNotEqual(
            nefia.drop_seed_for(self.user_pk),
            nefia.drop_seed_for(self.user_pk + 1),
        )

    async def test_daily_three_choose_any_two_and_claim_is_idempotent(self):
        tasks = self.service.daily_tasks("group-a", self.monday)
        self.assertEqual(len(tasks), 3)
        early = await self.service.claim_daily_reward(
            user_pk=self.user_pk,
            group_id="group-a",
            now=self.monday,
        )
        self.assertFalse(early.eligible)
        self.assertFalse(early.granted)

        first = await self.service.advance_daily_task(
            user_pk=self.user_pk,
            group_id="group-a",
            task_id=tasks[0].task_id,
            event_key="battle:100:first-task",
            amount=tasks[0].target,
            now=self.monday,
        )
        duplicate = await self.service.advance_daily_task(
            user_pk=self.user_pk,
            group_id="group-a",
            task_id=tasks[0].task_id,
            event_key="battle:100:first-task",
            amount=tasks[0].target,
            now=self.monday,
        )
        self.assertTrue(first.applied)
        self.assertTrue(first.record.completed)
        self.assertFalse(duplicate.applied)
        self.assertEqual(duplicate.record.progress, tasks[0].target)

        await self.service.advance_daily_task(
            user_pk=self.user_pk,
            group_id="group-a",
            task_id=tasks[1].task_id,
            event_key="battle:101:second-task",
            amount=tasks[1].target,
            now=self.monday,
        )
        granted = await self.service.claim_daily_reward(
            user_pk=self.user_pk,
            group_id="group-a",
            now=self.monday,
        )
        repeated = await self.service.claim_daily_reward(
            user_pk=self.user_pk,
            group_id="group-a",
            now=self.monday,
        )

        self.assertTrue(granted.eligible)
        self.assertTrue(granted.granted)
        self.assertIsNotNone(granted.reward_intent)
        self.assertEqual(granted.completed_count, 2)
        self.assertFalse(repeated.granted)
        self.assertTrue(repeated.already_claimed)
        self.assertEqual(
            repeated.reward_intent.reward_key,
            granted.reward_intent.reward_key,
        )
        upgraded_retry = await OperationService(
            self.db_path,
            ruleset_id="sideview-v12",
        ).claim_daily_reward(
            user_pk=self.user_pk,
            group_id="group-a",
            now=self.monday,
        )
        self.assertTrue(upgraded_retry.eligible)
        self.assertTrue(upgraded_retry.already_claimed)
        self.assertEqual(upgraded_retry.reward_intent, granted.reward_intent)

        connection = sqlite3.connect(self.db_path)
        try:
            # The operation service emits intent only; no actual wallet/item row
            # has been changed by claiming.
            wallet_rows = connection.execute(
                "SELECT COUNT(*) FROM workshop_wallet WHERE user_pk = ?",
                (self.user_pk,),
            ).fetchone()[0]
            reward_rows = connection.execute(
                "SELECT COUNT(*) FROM reward_ledger WHERE user_pk = ?",
                (self.user_pk,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(wallet_rows, 0)
        self.assertEqual(reward_rows, 0)

    async def test_progress_is_isolated_by_group_and_resets_to_new_period_key(self):
        task = self.service.daily_tasks("group-a", self.monday)[0]
        current = await self.service.advance_daily_task(
            user_pk=self.user_pk,
            group_id="group-a",
            task_id=task.task_id,
            event_key="evt-1",
            amount=task.target,
            now=self.monday,
        )
        other_group_task = self.service.daily_tasks("group-b", self.monday)[0]
        other_group = await self.service.advance_daily_task(
            user_pk=self.user_pk,
            group_id="group-b",
            task_id=other_group_task.task_id,
            event_key="evt-1",
            amount=1,
            now=self.monday,
        )
        tomorrow_task = self.service.daily_tasks(
            "group-a", self.monday + timedelta(days=1)
        )[0]
        tomorrow = await self.service.advance_daily_task(
            user_pk=self.user_pk,
            group_id="group-a",
            task_id=tomorrow_task.task_id,
            event_key="evt-1",
            amount=1,
            now=self.monday + timedelta(days=1),
        )

        self.assertEqual(current.record.period_key, "2026-08-10")
        self.assertEqual(other_group.record.group_id, "group-b")
        self.assertEqual(tomorrow.record.period_key, "2026-08-11")
        self.assertTrue(other_group.applied)
        self.assertTrue(tomorrow.applied)

    async def test_record_event_advances_all_matching_random_tasks_idempotently(self):
        selected_group = None
        event_type = None
        for index in range(100):
            candidate = f"event-group-{index}"
            daily_types = {
                task.event_type for task in self.service.daily_tasks(candidate, self.monday)
            }
            weekly_types = {
                task.event_type for task in self.service.weekly_tasks(candidate, self.monday)
            }
            overlap = daily_types & weekly_types
            if overlap:
                selected_group = candidate
                event_type = sorted(overlap)[0]
                break
        self.assertIsNotNone(selected_group)

        expected = tuple(
            (period_kind, task)
            for period_kind, tasks in (
                ("daily", self.service.daily_tasks(selected_group, self.monday)),
                ("weekly", self.service.weekly_tasks(selected_group, self.monday)),
            )
            for task in tasks
            if task.event_type == event_type
        )
        first = await self.service.record_event(
            user_pk=self.user_pk,
            group_id=selected_group,
            event_type=event_type,
            event_key="battle:7001",
            now=self.monday,
        )
        retry = await self.service.record_event(
            user_pk=self.user_pk,
            group_id=selected_group,
            event_type=event_type,
            event_key="battle:7001",
            now=self.monday,
        )

        self.assertEqual(len(first), len(expected))
        self.assertEqual(
            {(update.record.period_kind, update.record.operation_key) for update in first},
            {
                (kind, self.service._task_key(kind, task.task_id))
                for kind, task in expected
            },
        )
        self.assertTrue(all(update.applied for update in first))
        self.assertTrue(all(not update.applied for update in retry))
        self.assertTrue(all(update.record.progress == 1 for update in retry))
        with self.assertRaises(ValueError):
            await self.service.record_event(
                user_pk=self.user_pk,
                group_id=selected_group,
                event_type="typo_event",
                event_key="bad:1",
                now=self.monday,
            )

    def test_daily_task_sampling_always_has_two_live_event_paths(self):
        start = datetime(2026, 8, 1, 12, 0, 0)
        for group_index in range(40):
            for day_offset in range(45):
                tasks = self.service.daily_tasks(
                    f"sample-group-{group_index}",
                    start + timedelta(days=day_offset),
                )
                live_count = sum(
                    task.event_type in CURRENTLY_RECORDABLE_EVENT_TYPES
                    for task in tasks
                )
                self.assertEqual(len(tasks), 3)
                self.assertGreaterEqual(live_count, 2)

    def test_weekly_task_sampling_always_keeps_five_of_seven_reachable(self):
        start = datetime(2026, 8, 1, 12, 0, 0)
        for group_index in range(40):
            for day_offset in range(70):
                tasks = self.service.weekly_tasks(
                    f"weekly-sample-group-{group_index}",
                    start + timedelta(days=day_offset),
                )
                live_count = sum(
                    task.event_type in CURRENTLY_RECORDABLE_EVENT_TYPES
                    for task in tasks
                )
                self.assertEqual(len(tasks), 7)
                self.assertGreaterEqual(live_count, 5)

    async def test_weekly_five_of_seven_is_full_reward_threshold(self):
        tasks = self.service.weekly_tasks("group-a", self.monday)
        self.assertEqual(len(tasks), 7)
        self.assertEqual(
            tasks,
            self.service.weekly_tasks("group-a", self.monday + timedelta(days=5)),
        )
        for index, task in enumerate(tasks[:4]):
            await self.service.advance_weekly_task(
                user_pk=self.user_pk,
                group_id="group-a",
                task_id=task.task_id,
                event_key=f"weekly-event-{index}",
                amount=task.target,
                now=self.monday,
            )
        not_yet = await self.service.claim_weekly_reward(
            user_pk=self.user_pk, group_id="group-a", now=self.monday
        )
        self.assertFalse(not_yet.eligible)

        fifth = tasks[4]
        await self.service.advance_weekly_task(
            user_pk=self.user_pk,
            group_id="group-a",
            task_id=fifth.task_id,
            event_key="weekly-event-4",
            amount=fifth.target,
            now=self.monday,
        )
        full = await self.service.claim_weekly_reward(
            user_pk=self.user_pk, group_id="group-a", now=self.monday
        )
        self.assertTrue(full.granted)
        self.assertEqual(full.completed_count, 5)
        self.assertGreater(full.reward_intent.season_tokens, 0)

    async def test_weekly_simulation_uses_best_two_of_at_most_four(self):
        scores = (120, 500, 300, 450)
        for index, score in enumerate(scores):
            result = await self.service.record_weekly_simulation(
                user_pk=self.user_pk,
                group_id="group-a",
                submission_key=f"build-{index}",
                score=score,
                now=self.monday,
            )
            self.assertTrue(result.accepted)
        self.assertEqual(result.attempts_used, 4)
        self.assertEqual(result.best_scores, (500, 450))
        self.assertEqual(result.scored_total, 950)

        duplicate = await self.service.record_weekly_simulation(
            user_pk=self.user_pk,
            group_id="group-a",
            submission_key="build-1",
            score=999,
            now=self.monday,
        )
        rejected = await self.service.record_weekly_simulation(
            user_pk=self.user_pk,
            group_id="group-a",
            submission_key="build-5",
            score=999,
            now=self.monday,
        )
        self.assertTrue(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.submitted_score, 500)
        self.assertFalse(rejected.accepted)
        self.assertFalse(rejected.duplicate)
        self.assertEqual(rejected.attempts_used, 4)
        self.assertEqual(rejected.best_scores, (500, 450))

        with self.assertRaises(ValueError):
            await self.service.record_weekly_simulation(
                user_pk=self.user_pk,
                group_id="group-a",
                submission_key="impossible",
                score=WEEKLY_SIMULATION_SCORE_CAP + 1,
                now=self.monday,
            )

    async def test_overview_uses_persisted_28_day_season(self):
        overview = await self.service.overview(
            user_pk=self.user_pk,
            group_id="group-a",
            now=self.monday,
        )
        repeated = await self.service.overview(
            user_pk=self.user_pk,
            group_id="group-a",
            now=self.monday + timedelta(days=1),
        )

        self.assertEqual(overview.season.key, "2026-07-20-v11")
        self.assertEqual(overview.season.total_days, 28)
        self.assertEqual(overview.season.day_number, 22)
        self.assertEqual(overview.season.season_id, repeated.season.season_id)
        self.assertEqual(len(overview.daily_tasks), 3)
        self.assertEqual(len(overview.weekly_tasks), 7)

        connection = sqlite3.connect(self.db_path)
        try:
            season_rows = connection.execute(
                """
                SELECT COUNT(*) FROM seasons
                WHERE group_id = 'group-a' AND season_key = '2026-07-20-v11'
                """
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(season_rows, 1)


if __name__ == "__main__":
    unittest.main()
