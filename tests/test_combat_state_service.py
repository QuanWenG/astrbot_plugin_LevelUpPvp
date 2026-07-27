import os
import tempfile
import unittest

from models.ability import CombatStatus
from models.attributes import DerivedStats, PrimaryAttributes
from models.combat import FighterContinuationState, FighterSnapshot
from models.skill import SkillBuild
from services.combat_engine import SideviewCombatEngine
from services.combat_state_service import CombatStateService
from services.db import connect_db, init_db


def snapshot(user_pk: int = 1) -> FighterSnapshot:
    attributes = PrimaryAttributes(10, 20, 10, 10, 10, 30)
    derived = DerivedStats(
        max_hp=100,
        max_mp=80,
        max_sp=60,
        attack_power=10,
        accuracy=10,
        defense=10,
        evasion=10,
        critical_rate=0.05,
        critical_damage=1.5,
        action_speed=100,
        carry_capacity=20,
        healing_power=1.0,
    )
    skills = SkillBuild({}, {"healing": 12, "meditation": 15})
    return FighterSnapshot(
        user_pk=user_pk,
        name="测试者",
        level=20,
        hp=10,
        atk=10,
        defense=20,
        speed=10,
        luck=10,
        strategy="",
        attributes=attributes,
        derived=derived,
        skills=skills,
    )


class CombatStateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = SideviewCombatEngine()
        self.service = CombatStateService(":memory:", self.engine)
        self.snapshot = snapshot()

    def test_only_complete_thirty_second_turns_are_recovered(self):
        initial = FighterContinuationState(
            hp_ratio=0.5,
            mana_ratio=0.5,
            stamina_ratio=0.5,
            updated_at_ts=100,
        )
        at_29 = self.service.advance(self.snapshot, initial, 129)
        at_30 = self.service.advance(self.snapshot, initial, 130)
        at_59 = self.service.advance(self.snapshot, initial, 159)
        at_60 = self.service.advance(self.snapshot, initial, 160)

        self.assertEqual(at_29, initial)
        self.assertEqual(at_30.updated_at_ts, 130)
        self.assertEqual(at_59.updated_at_ts, 130)
        self.assertEqual(at_60.updated_at_ts, 160)
        self.assertGreater(at_30.hp_regen_buffer, 0)
        self.assertGreaterEqual(at_60.hp_ratio, at_30.hp_ratio)

    def test_fighter_owned_state_round_trips_but_pending_actions_do_not(self):
        initial = FighterContinuationState(
            hp_ratio=0.45,
            mana_ratio=-0.25,
            stamina_ratio=0.4,
            statuses=(
                CombatStatus(
                    "insight", 1, 8, beneficial=True
                ).to_dict(),
            ),
            skill_cooldowns={"spell_fire": 7},
            attack_cooldown=3,
            recovery_ticks=2,
            hitstun_ticks=1,
            counter_cooldown=4,
            lethal_survival_used=True,
        )
        fighter = self.engine._fighter_from_initial(
            self.snapshot, 200, initial
        )
        fighter.attack_pending = True
        fighter.pending_skill_id = "old_target_skill"
        restored = self.engine._fighter_from_initial(
            self.snapshot,
            800,
            self.engine._continuation_state(fighter),
        )

        self.assertEqual(restored.current_hp, 45)
        self.assertEqual(restored.mana, -20)
        self.assertEqual(restored.skill_cooldowns["spell_fire"], 7)
        self.assertIn("insight", restored.statuses)
        self.assertTrue(restored.lethal_survival_used)
        self.assertFalse(restored.attack_pending)
        self.assertIsNone(restored.pending_skill_id)
        self.assertEqual(restored.position, 800)

    def test_periodic_damage_defeat_immediately_restores_full_state(self):
        initial = FighterContinuationState(
            hp_ratio=0.01,
            statuses=(
                CombatStatus("poison", 2, 10, magnitude=5).to_dict(),
            ),
            updated_at_ts=100,
        )
        result = self.service.advance(self.snapshot, initial, 250)
        self.assertFalse(result.defeated)
        self.assertEqual(result.hp_ratio, 1)
        self.assertEqual(result.mana_ratio, 1)
        self.assertEqual(result.stamina_ratio, 1)
        self.assertEqual(result.statuses, ())

    def test_long_idle_time_uses_bulk_recovery(self):
        initial = FighterContinuationState(
            hp_ratio=0,
            mana_ratio=-1,
            stamina_ratio=0,
            updated_at_ts=100,
        )
        result = self.service.advance(
            self.snapshot, initial, 100 + 30 * 1_000_000
        )
        self.assertEqual(result.hp_ratio, 1)
        self.assertEqual(result.mana_ratio, 1)
        self.assertEqual(result.stamina_ratio, 1)


class CombatStatePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "state.db")
        await init_db(self.db_path)
        async with await connect_db(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO users (
                    platform, group_id, user_id, nickname,
                    created_at, updated_at
                ) VALUES ('test', 'group', 'user', '测试者', 'now', 'now')
                """
            )
            await db.commit()
        self.service = CombatStateService(self.db_path)
        self.snapshot = snapshot(1)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_defeat_is_immediately_restored_for_preview_and_battle(self):
        defeated = FighterContinuationState(
            hp_ratio=0,
            mana_ratio=0.2,
            stamina_ratio=0.1,
            defeated=True,
            updated_at_ts=100,
        )
        async with await connect_db(self.db_path) as db:
            await self.service.save_in_db(db, 1, defeated, 100)
            await db.commit()

        preview = await self.service.preview(self.snapshot, 130)
        self.assertFalse(preview.state.defeated)
        self.assertEqual(preview.current_hp, preview.max_hp)

        async with await connect_db(self.db_path) as db:
            battle_state = await self.service.load_in_db(
                db, self.snapshot, 130, consume_defeat=True
            )
        self.assertFalse(battle_state.defeated)
        self.assertEqual(battle_state.hp_ratio, 1)
        self.assertEqual(battle_state.mana_ratio, 1)
        self.assertEqual(battle_state.stamina_ratio, 1)


if __name__ == "__main__":
    unittest.main()
