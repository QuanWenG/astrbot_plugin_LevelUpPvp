import os
import shutil
import sqlite3
import unittest
import uuid

from services.db import init_db


V11_TABLES = {
    "combat_loadouts",
    "seasons",
    "season_users",
    "reward_ledger",
    "operation_progress",
    "loot_pity",
    "equipment_rework_state",
    "workshop_wallet",
}

V11_BATTLE_COLUMNS = {
    "loser_exp_gain",
    "ruleset_id",
    "environment_id",
    "attacker_rating_before",
    "attacker_rating_after",
    "defender_rating_before",
    "defender_rating_after",
    "rated",
    "reward_reason",
    "attacker_tactic_plan_json",
    "defender_tactic_plan_json",
}


class V11SchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".test_tmp")
        )
        os.makedirs(root, exist_ok=True)
        self.temp_dir = os.path.join(root, f"v11-schema-{uuid.uuid4().hex}")
        os.makedirs(self.temp_dir)
        self.db_path = os.path.join(self.temp_dir, "v11.db")

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_fresh_database_contains_v11_tables_columns_and_indexes(self):
        await init_db(self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            battle_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(battles)")
            }
            combat_state_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(combat_states)"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            season_rating_default = connection.execute(
                "SELECT dflt_value FROM pragma_table_info('season_users') "
                "WHERE name = 'rating'"
            ).fetchone()[0]
            workshop_token_default = connection.execute(
                "SELECT dflt_value FROM pragma_table_info('workshop_wallet') "
                "WHERE name = 'season_tokens'"
            ).fetchone()[0]
            equipment_lock_default = connection.execute(
                "SELECT dflt_value FROM pragma_table_info('equipment_items') "
                "WHERE name = 'is_locked'"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertTrue(V11_TABLES.issubset(tables))
        self.assertTrue(V11_BATTLE_COLUMNS.issubset(battle_columns))
        self.assertIn(
            "hard_control_immunity_ticks", combat_state_columns
        )
        self.assertEqual(season_rating_default, "1000")
        self.assertEqual(workshop_token_default, "0")
        self.assertEqual(equipment_lock_default, "0")
        self.assertTrue(
            {
                "idx_battles_ruleset_created",
                "idx_battles_rated_created",
                "idx_seasons_group_status",
                "idx_season_users_rating",
                "idx_reward_ledger_user_created",
                "idx_operation_progress_period",
            }.issubset(indexes)
        )

    async def test_repeated_init_preserves_rows_and_schema(self):
        await init_db(self.db_path)
        connection = sqlite3.connect(self.db_path)
        try:
            cursor = connection.execute(
                """
                INSERT INTO users (
                    platform, group_id, user_id, nickname, created_at, updated_at
                ) VALUES ('qq', 'group-1', 'user-1', '一号', 'now', 'now')
                """
            )
            user_pk = cursor.lastrowid
            connection.execute(
                """
                INSERT INTO combat_loadouts (
                    user_pk, opening_family, midgame_family, endgame_family,
                    active_slots_json, updated_at
                ) VALUES (?, 'assault', 'guard', 'control', '[1,2,3,4]', 'now')
                """,
                (user_pk,),
            )
            connection.commit()
        finally:
            connection.close()

        await init_db(self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT opening_family, midgame_family, endgame_family,
                       active_slots_json
                FROM combat_loadouts WHERE user_pk = ?
                """,
                (user_pk,),
            ).fetchone()
            table_count = connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'table' AND name IN (
                    'combat_loadouts', 'seasons', 'season_users',
                    'reward_ledger', 'operation_progress', 'loot_pity',
                    'equipment_rework_state', 'workshop_wallet'
                )
                """
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(row, ("assault", "guard", "control", "[1,2,3,4]"))
        self.assertEqual(table_count, len(V11_TABLES))

    async def test_season_and_reward_idempotency_keys_are_unique(self):
        await init_db(self.db_path)
        connection = sqlite3.connect(self.db_path)
        try:
            first_user = connection.execute(
                """
                INSERT INTO users (
                    platform, group_id, user_id, nickname, created_at, updated_at
                ) VALUES ('qq', 'group-1', 'user-1', '一号', 'now', 'now')
                """
            ).lastrowid
            second_user = connection.execute(
                """
                INSERT INTO users (
                    platform, group_id, user_id, nickname, created_at, updated_at
                ) VALUES ('qq', 'group-1', 'user-2', '二号', 'now', 'now')
                """
            ).lastrowid
            season_id = connection.execute(
                """
                INSERT INTO seasons (
                    group_id, season_key, ruleset_id, start_at_ts, end_at_ts
                ) VALUES ('group-1', '2026-S01', 'sideview-v11', 100, 200)
                """
            ).lastrowid
            connection.execute(
                "INSERT INTO season_users (season_id, user_pk) VALUES (?, ?)",
                (season_id, first_user),
            )
            connection.execute(
                """
                INSERT INTO reward_ledger (
                    reward_key, user_pk, source, exp_gain, reason, created_at_ts
                ) VALUES ('pvp:2026-S01:battle-1:user-1', ?, 'rated_pvp', 10,
                          'first_unique_opponent', 123)
                """,
                (first_user,),
            )
            connection.commit()

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO seasons (
                        group_id, season_key, ruleset_id,
                        start_at_ts, end_at_ts
                    ) VALUES ('group-1', '2026-S01', 'sideview-v11', 100, 200)
                    """
                )
            connection.rollback()

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO season_users (season_id, user_pk) VALUES (?, ?)",
                    (season_id, first_user),
                )
            connection.rollback()

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO reward_ledger (
                        reward_key, user_pk, source, created_at_ts
                    ) VALUES (
                        'pvp:2026-S01:battle-1:user-1', ?, 'rated_pvp', 124
                    )
                    """,
                    (second_user,),
                )
            connection.rollback()
        finally:
            connection.close()

    async def test_old_battle_rows_gain_v11_columns_without_rewrite(self):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE battles (
                    id INTEGER PRIMARY KEY,
                    attacker_pk INTEGER,
                    defender_pk INTEGER,
                    created_at_ts INTEGER
                );
                INSERT INTO battles (id, attacker_pk, defender_pk, created_at_ts)
                VALUES (7, 10, 20, 1234);
                """
            )
            connection.commit()
        finally:
            connection.close()

        await init_db(self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(battles)")
            }
            row = connection.execute(
                """
                SELECT id, ruleset_id, environment_id, rated,
                       loser_exp_gain, reward_reason, attacker_tactic_plan_json,
                       defender_tactic_plan_json
                FROM battles WHERE id = 7
                """
            ).fetchone()
        finally:
            connection.close()

        self.assertTrue(V11_BATTLE_COLUMNS.issubset(columns))
        self.assertEqual(
            row,
            (7, "legacy-v1", "", 0, 0, "", "{}", "{}"),
        )

    async def test_old_workshop_wallet_gains_season_tokens_without_rewrite(self):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE workshop_wallet (
                    user_pk INTEGER PRIMARY KEY,
                    scrap_balance INTEGER NOT NULL DEFAULT 0,
                    lifetime_earned INTEGER NOT NULL DEFAULT 0,
                    lifetime_spent INTEGER NOT NULL DEFAULT 0,
                    updated_at_ts INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO workshop_wallet (
                    user_pk, scrap_balance, lifetime_earned,
                    lifetime_spent, updated_at_ts
                ) VALUES (7, 123, 200, 77, 99);
                """
            )
            connection.commit()
        finally:
            connection.close()

        await init_db(self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(workshop_wallet)"
                )
            }
            row = connection.execute(
                """
                SELECT user_pk, scrap_balance, season_tokens,
                       lifetime_earned, lifetime_spent, updated_at_ts
                FROM workshop_wallet WHERE user_pk = 7
                """
            ).fetchone()
        finally:
            connection.close()

        self.assertIn("season_tokens", columns)
        self.assertEqual(row, (7, 123, 0, 200, 77, 99))

    async def test_old_equipment_rows_gain_lock_flag_without_rewrite(self):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE equipment_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_pk INTEGER NOT NULL,
                    template_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    equip_slot TEXT NOT NULL,
                    hand_mode TEXT NOT NULL DEFAULT 'none',
                    weapon_type TEXT NOT NULL DEFAULT '',
                    armor_type TEXT NOT NULL DEFAULT '',
                    item_level INTEGER NOT NULL DEFAULT 0,
                    quality TEXT NOT NULL DEFAULT 'common',
                    star_type TEXT NOT NULL DEFAULT 'none',
                    material TEXT NOT NULL DEFAULT 'iron',
                    blessing_state TEXT NOT NULL DEFAULT 'normal',
                    enhancement_level INTEGER NOT NULL DEFAULT 0,
                    weight REAL NOT NULL DEFAULT 0,
                    enchant_capacity INTEGER NOT NULL DEFAULT 0,
                    used_capacity INTEGER NOT NULL DEFAULT 0,
                    base_stats_json TEXT NOT NULL DEFAULT '{}',
                    inherent_affixes_json TEXT NOT NULL DEFAULT '[]',
                    random_affixes_json TEXT NOT NULL DEFAULT '[]',
                    fusion_affixes_json TEXT NOT NULL DEFAULT '[]',
                    bound INTEGER NOT NULL DEFAULT 1,
                    description TEXT NOT NULL DEFAULT '',
                    source_effects_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                INSERT INTO equipment_items (
                    owner_pk, template_id, name, item_type, equip_slot,
                    item_level, quality, created_at
                ) VALUES (
                    7, 'legacy-drop', '旧装备', 'armor', 'body',
                    12, 'excellent', 'now'
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        await init_db(self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(equipment_items)"
                )
            }
            row = connection.execute(
                "SELECT name, is_locked FROM equipment_items WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()

        self.assertIn("is_locked", columns)
        self.assertEqual(row, ("旧装备", 0))

    async def test_old_combat_state_gains_control_immunity_without_rewrite(self):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE combat_states (
                    user_pk INTEGER PRIMARY KEY,
                    hp_ratio REAL NOT NULL DEFAULT 1,
                    mana_ratio REAL NOT NULL DEFAULT 1,
                    stamina_ratio REAL NOT NULL DEFAULT 1,
                    hp_regen_buffer REAL NOT NULL DEFAULT 0,
                    mp_regen_buffer REAL NOT NULL DEFAULT 0,
                    sp_regen_buffer REAL NOT NULL DEFAULT 0,
                    recovery_turn_phase INTEGER NOT NULL DEFAULT 0,
                    statuses_json TEXT NOT NULL DEFAULT '[]',
                    skill_cooldowns_json TEXT NOT NULL DEFAULT '{}',
                    attack_cooldown INTEGER NOT NULL DEFAULT 0,
                    recovery_ticks INTEGER NOT NULL DEFAULT 0,
                    hitstun_ticks INTEGER NOT NULL DEFAULT 0,
                    counter_cooldown INTEGER NOT NULL DEFAULT 0,
                    stance_id TEXT,
                    frozen_mana_ratio REAL NOT NULL DEFAULT 0,
                    frozen_mana_capacity_ratio REAL NOT NULL DEFAULT 0,
                    lethal_survival_used INTEGER NOT NULL DEFAULT 0,
                    defeated INTEGER NOT NULL DEFAULT 0,
                    updated_at_ts INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1
                );
                INSERT INTO combat_states (
                    user_pk, hp_ratio, mana_ratio, stamina_ratio, version
                ) VALUES (7, 0.5, 0.4, 0.3, 9);
                """
            )
            connection.commit()
        finally:
            connection.close()

        await init_db(self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT hp_ratio, mana_ratio, stamina_ratio,
                       hard_control_immunity_ticks, version
                FROM combat_states WHERE user_pk = 7
                """
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(row, (0.5, 0.4, 0.3, 0, 9))


if __name__ == "__main__":
    unittest.main()
