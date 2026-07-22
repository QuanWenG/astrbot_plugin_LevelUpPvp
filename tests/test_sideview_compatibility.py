import os
import shutil
import sqlite3
import unittest
import uuid

from services.battle_service import BattleService
from services.combat_ai import profile_for_strategy
from services.db import connect_db, init_db


class CustomStrategyAIProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_profile_is_converted_to_ai_behavior(self):
        class FakeLLM:
            async def analyze_custom_strategy(self, *args, **kwargs):
                return {
                    "primary_stats": ["speed", "defense", "luck"],
                    "counters": ["全力猛攻"],
                }

        service = BattleService(":memory:", None, FakeLLM())
        profile_data = await service._build_custom_strategy_profile(
            "疾风架势",
            context=object(),
            event=object(),
        )
        ai_profile = profile_for_strategy("疾风架势", profile_data)

        self.assertEqual(profile_data["primary_stats"], ("speed", "defense", "luck"))
        self.assertEqual(profile_data["counters"], ("全力猛攻",))
        self.assertEqual(ai_profile.preferred_range, 125)
        self.assertGreater(ai_profile.guard_tendency, 0.2)

    async def test_keyword_fallback_produces_non_default_profile(self):
        service = BattleService(":memory:", None, None)
        profile_data = service._fallback_custom_strategy_profile("高速防守反击")
        ai_profile = profile_for_strategy("高速防守反击", profile_data)

        self.assertIn("speed", profile_data["primary_stats"])
        self.assertIn("defense", profile_data["primary_stats"])
        self.assertEqual(ai_profile.preferred_range, 125)
        self.assertGreater(ai_profile.guard_tendency, 0.2)


class LegacyDatabaseMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(root, f"migration-{uuid.uuid4().hex}")
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "legacy.db")

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_old_battle_rows_survive_compatible_column_migration(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE battles (
                id INTEGER PRIMARY KEY,
                attacker_pk INTEGER,
                defender_pk INTEGER,
                created_at_ts INTEGER
            );
            INSERT INTO battles (id, attacker_pk, defender_pk, created_at_ts)
            VALUES (1, 10, 20, 1234);
            """
        )
        connection.commit()
        connection.close()

        await init_db(self.db_path)

        async with await connect_db(self.db_path) as db:
            cursor = await db.execute("PRAGMA table_info(battles)")
            columns = {row["name"] for row in await cursor.fetchall()}
            await cursor.close()
            cursor = await db.execute(
                "SELECT id, battle_mode, engine_version, simulation_json FROM battles WHERE id = 1"
            )
            row = await cursor.fetchone()
            await cursor.close()

        self.assertTrue(
            {
                "battle_mode",
                "engine_version",
                "random_seed",
                "duration_ticks",
                "finish_reason",
                "simulation_json",
            }.issubset(columns)
        )
        self.assertEqual(row["id"], 1)
        self.assertEqual(row["battle_mode"], "probability")
        self.assertEqual(row["engine_version"], "legacy-v1")


if __name__ == "__main__":
    unittest.main()
