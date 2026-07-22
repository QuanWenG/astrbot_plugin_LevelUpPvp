import os
import sqlite3

try:
    import aiosqlite
except ImportError:
    aiosqlite = None


class _AsyncSQLiteCursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()

    async def close(self) -> None:
        self._cursor.close()


class _AsyncSQLiteConnection:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._conn.close()

    async def execute(self, sql: str, parameters=()):
        return _AsyncSQLiteCursor(self._conn.execute(sql, parameters))

    async def executescript(self, sql_script: str) -> None:
        self._conn.executescript(sql_script)

    async def commit(self) -> None:
        self._conn.commit()

    async def rollback(self) -> None:
        self._conn.rollback()


class _AioSQLiteConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        await self._conn.close()

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


async def connect_db(db_path: str):
    if aiosqlite is None:
        db = _AsyncSQLiteConnection(db_path)
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = MEMORY")
        return db

    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = MEMORY")
    return _AioSQLiteConnectionWrapper(db)


async def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    async with await connect_db(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                group_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                level INTEGER NOT NULL DEFAULT 1,
                exp INTEGER NOT NULL DEFAULT 0,
                total_exp INTEGER NOT NULL DEFAULT 0,
                stat_points INTEGER NOT NULL DEFAULT 0,
                skill_points INTEGER NOT NULL DEFAULT 0,
                level_up_count INTEGER NOT NULL DEFAULT 0,
                hp INTEGER NOT NULL DEFAULT 10,
                atk INTEGER NOT NULL DEFAULT 5,
                defense INTEGER NOT NULL DEFAULT 5,
                speed INTEGER NOT NULL DEFAULT 5,
                luck INTEGER NOT NULL DEFAULT 5,
                willpower INTEGER NOT NULL DEFAULT 5,
                life_growth INTEGER NOT NULL DEFAULT 100,
                mana_growth INTEGER NOT NULL DEFAULT 100,
                advanced_speed INTEGER NOT NULL DEFAULT 100,
                advanced_luck INTEGER NOT NULL DEFAULT 100,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, group_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                checkin_date TEXT NOT NULL,
                streak_days INTEGER NOT NULL,
                exp_gain INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_pk, checkin_date),
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS battles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL DEFAULT '',
                attacker_pk INTEGER NOT NULL,
                defender_pk INTEGER NOT NULL,
                winner_pk INTEGER NOT NULL,
                loser_pk INTEGER NOT NULL,
                attacker_win_rate REAL NOT NULL,
                roll_value REAL NOT NULL,
                strategy TEXT NOT NULL DEFAULT '',
                winner_exp_gain INTEGER NOT NULL,
                loser_exp_loss INTEGER NOT NULL,
                analysis TEXT NOT NULL DEFAULT '',
                battle_log TEXT NOT NULL DEFAULT '[]',
                llm_raw_result TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'local',
                is_counterattack INTEGER NOT NULL DEFAULT 0,
                countered_battle_id INTEGER,
                battle_mode TEXT NOT NULL DEFAULT 'probability',
                engine_version TEXT NOT NULL DEFAULT 'legacy-v1',
                random_seed INTEGER,
                duration_ticks INTEGER NOT NULL DEFAULT 0,
                finish_reason TEXT NOT NULL DEFAULT '',
                simulation_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                created_at_ts INTEGER NOT NULL,
                FOREIGN KEY(attacker_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(defender_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(winner_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(loser_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(countered_battle_id) REFERENCES battles(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS level_up_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                from_level INTEGER NOT NULL,
                to_level INTEGER NOT NULL,
                auto_growth_json TEXT NOT NULL,
                stat_points_gain INTEGER NOT NULL,
                skill_points_gain INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS level_freezes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                frozen_level INTEGER NOT NULL,
                from_level INTEGER NOT NULL,
                to_level INTEGER NOT NULL,
                frozen_stats_json TEXT NOT NULL,
                frozen_stat_points INTEGER NOT NULL DEFAULT 0,
                frozen_skill_points INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'frozen',
                created_at TEXT NOT NULL,
                released_at TEXT,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS stat_point_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                stat_name TEXT NOT NULL,
                points_spent INTEGER NOT NULL,
                rolls_json TEXT NOT NULL,
                total_gain INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS nickname_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                group_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, group_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS equipment_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner_pk INTEGER NOT NULL,
                template_id TEXT NOT NULL, name TEXT NOT NULL, item_type TEXT NOT NULL,
                equip_slot TEXT NOT NULL, hand_mode TEXT NOT NULL DEFAULT 'none',
                weapon_type TEXT NOT NULL DEFAULT '', armor_type TEXT NOT NULL DEFAULT '',
                item_level INTEGER NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT 'common',
                star_type TEXT NOT NULL DEFAULT 'none', material TEXT NOT NULL DEFAULT 'iron',
                blessing_state TEXT NOT NULL DEFAULT 'normal', enhancement_level INTEGER NOT NULL DEFAULT 0,
                weight REAL NOT NULL DEFAULT 0, enchant_capacity INTEGER NOT NULL DEFAULT 0,
                used_capacity INTEGER NOT NULL DEFAULT 0, base_stats_json TEXT NOT NULL DEFAULT '{}',
                inherent_affixes_json TEXT NOT NULL DEFAULT '[]', random_affixes_json TEXT NOT NULL DEFAULT '[]',
                fusion_affixes_json TEXT NOT NULL DEFAULT '[]', bound INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, FOREIGN KEY(owner_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS equipment_loadout (
                user_pk INTEGER NOT NULL, slot TEXT NOT NULL, equipment_id INTEGER NOT NULL,
                PRIMARY KEY(user_pk, slot), FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(equipment_id) REFERENCES equipment_items(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_skills (
                user_pk INTEGER NOT NULL, skill_id TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 1,
                exp INTEGER NOT NULL DEFAULT 0, potential INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY(user_pk, skill_id), FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS active_skill_slots (
                user_pk INTEGER NOT NULL, slot INTEGER NOT NULL, skill_id TEXT NOT NULL,
                PRIMARY KEY(user_pk, slot), FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS skill_growth_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_pk INTEGER NOT NULL, battle_id INTEGER,
                skill_id TEXT NOT NULL, exp_gain INTEGER NOT NULL, from_level INTEGER NOT NULL,
                to_level INTEGER NOT NULL, potential_before INTEGER NOT NULL, potential_after INTEGER NOT NULL,
                created_at TEXT NOT NULL, FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS feature_grants (
                user_pk INTEGER NOT NULL, grant_key TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(user_pk, grant_key), FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS spellbook_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_pk INTEGER NOT NULL, spell_id TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'internal',
                random_seed INTEGER NOT NULL, bound INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(owner_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_spells (
                user_pk INTEGER NOT NULL, spell_id TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 1, exp INTEGER NOT NULL DEFAULT 0,
                potential INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY(user_pk, spell_id),
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS spell_read_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL, spell_id TEXT NOT NULL,
                book_item_id INTEGER NOT NULL, success INTEGER NOT NULL,
                success_chance REAL NOT NULL, random_seed INTEGER NOT NULL,
                potential_before INTEGER NOT NULL DEFAULT 0,
                potential_after INTEGER NOT NULL DEFAULT 0,
                reading_difficulty INTEGER NOT NULL DEFAULT 0,
                reading_power REAL NOT NULL DEFAULT 0,
                reading_attribute TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS spell_growth_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL, battle_id INTEGER,
                spell_id TEXT NOT NULL, exp_gain INTEGER NOT NULL,
                from_level INTEGER NOT NULL, to_level INTEGER NOT NULL,
                potential_before INTEGER NOT NULL,
                potential_after INTEGER NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS advanced_attribute_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL, attribute_id TEXT NOT NULL,
                amount INTEGER NOT NULL, value_before INTEGER NOT NULL,
                value_after INTEGER NOT NULL, source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_attribute_progress (
                user_pk INTEGER NOT NULL, attribute_id TEXT NOT NULL,
                exp INTEGER NOT NULL DEFAULT 0, potential INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY(user_pk, attribute_id),
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS attribute_growth_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_pk INTEGER NOT NULL,
                battle_id INTEGER, attribute_id TEXT NOT NULL, exp_gain INTEGER NOT NULL,
                from_value INTEGER NOT NULL, to_value INTEGER NOT NULL,
                potential_before INTEGER NOT NULL, potential_after INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_checkins_user_date
                ON checkins(user_pk, checkin_date);
            CREATE INDEX IF NOT EXISTS idx_battles_attacker
                ON battles(attacker_pk);
            CREATE INDEX IF NOT EXISTS idx_battles_defender
                ON battles(defender_pk);
            CREATE INDEX IF NOT EXISTS idx_battles_created_at_ts
                ON battles(created_at_ts);
            CREATE INDEX IF NOT EXISTS idx_level_up_logs_user
                ON level_up_logs(user_pk);
            CREATE INDEX IF NOT EXISTS idx_level_freezes_user_status
                ON level_freezes(user_pk, status, frozen_level);
            CREATE INDEX IF NOT EXISTS idx_stat_point_logs_user
                ON stat_point_logs(user_pk);
            CREATE INDEX IF NOT EXISTS idx_nickname_mappings_lookup
                ON nickname_mappings(platform, group_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_advanced_attribute_logs_user
                ON advanced_attribute_logs(user_pk, created_at);
            CREATE INDEX IF NOT EXISTS idx_attribute_growth_user
                ON attribute_growth_logs(user_pk, battle_id);
            CREATE INDEX IF NOT EXISTS idx_skill_growth_user
                ON skill_growth_logs(user_pk, battle_id);
            CREATE INDEX IF NOT EXISTS idx_spellbooks_owner
                ON spellbook_items(owner_pk, spell_id);
            CREATE INDEX IF NOT EXISTS idx_spell_growth_user
                ON spell_growth_logs(user_pk, battle_id);
            """
        )
        await _ensure_column(
            db,
            "battles",
            "is_counterattack",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(db, "battles", "countered_battle_id", "INTEGER")
        await _ensure_column(
            db, "battles", "battle_mode", "TEXT NOT NULL DEFAULT 'probability'"
        )
        await _ensure_column(
            db, "battles", "engine_version", "TEXT NOT NULL DEFAULT 'legacy-v1'"
        )
        await _ensure_column(db, "battles", "random_seed", "INTEGER")
        await _ensure_column(
            db, "battles", "duration_ticks", "INTEGER NOT NULL DEFAULT 0"
        )
        await _ensure_column(
            db, "battles", "finish_reason", "TEXT NOT NULL DEFAULT ''"
        )
        await _ensure_column(
            db, "battles", "simulation_json", "TEXT NOT NULL DEFAULT '{}'"
        )
        await _ensure_column(db, "spell_read_logs", "reading_difficulty", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "spell_read_logs", "reading_power", "REAL NOT NULL DEFAULT 0")
        await _ensure_column(db, "spell_read_logs", "reading_attribute", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "users", "life_growth", "INTEGER NOT NULL DEFAULT 100")
        await _ensure_column(db, "users", "mana_growth", "INTEGER NOT NULL DEFAULT 100")
        await _ensure_column(db, "users", "advanced_speed", "INTEGER NOT NULL DEFAULT 100")
        await _ensure_column(db, "users", "advanced_luck", "INTEGER NOT NULL DEFAULT 100")
        await _ensure_column(db, "users", "skill_points", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "users", "willpower", "INTEGER NOT NULL DEFAULT 5")
        await _ensure_column(db, "level_up_logs", "skill_points_gain", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "level_freezes", "frozen_skill_points", "INTEGER NOT NULL DEFAULT 0")
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_battles_countered
                ON battles(countered_battle_id)
            """
        )
        await db.commit()


async def _ensure_column(db, table_name: str, column_name: str, definition: str) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    rows = await cursor.fetchall()
    await cursor.close()
    if any(row["name"] == column_name for row in rows):
        return
    await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
