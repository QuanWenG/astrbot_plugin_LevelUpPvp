import os
import sqlite3

try:
    import aiosqlite
except ImportError:
    aiosqlite = None


PRIMARY_ATTRIBUTE_REBALANCE_MIGRATION = "2026-07-primary-attributes-v2"
PRIMARY_ATTRIBUTE_REBALANCE_BACKUP_SUFFIX = ".pre-primary-attributes-v2.bak"
ELONA_BALANCE_MIGRATION = "2026-07-elona-balance-v1"
ELONA_BALANCE_BACKUP_SUFFIX = ".pre-elona-balance-v1.bak"


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
                hp INTEGER NOT NULL DEFAULT 1,
                atk INTEGER NOT NULL DEFAULT 1,
                defense INTEGER NOT NULL DEFAULT 1,
                speed INTEGER NOT NULL DEFAULT 1,
                luck INTEGER NOT NULL DEFAULT 1,
                willpower INTEGER NOT NULL DEFAULT 1,
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

            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
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
        await _ensure_column(db, "users", "willpower", "INTEGER NOT NULL DEFAULT 1")
        await _ensure_column(db, "level_up_logs", "skill_points_gain", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "level_freezes", "frozen_skill_points", "INTEGER NOT NULL DEFAULT 0")
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_battles_countered
                ON battles(countered_battle_id)
            """
        )
        await db.commit()
        await _apply_primary_attribute_rebalance(db, db_path)
        await _apply_elona_balance_reset(db, db_path)


async def _ensure_column(db, table_name: str, column_name: str, definition: str) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    rows = await cursor.fetchall()
    await cursor.close()
    if any(row["name"] == column_name for row in rows):
        return
    await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _backup_database_once(db_path: str) -> str | None:
    return _backup_database_with_suffix(
        db_path, PRIMARY_ATTRIBUTE_REBALANCE_BACKUP_SUFFIX
    )


def _backup_database_with_suffix(
    db_path: str, suffix: str
) -> str | None:
    if db_path == ":memory:" or not os.path.isfile(db_path):
        return None
    backup_path = db_path + suffix
    if os.path.exists(backup_path):
        return backup_path
    temporary_path = backup_path + ".tmp"
    if os.path.exists(temporary_path):
        os.remove(temporary_path)
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(temporary_path)
    try:
        source.backup(destination)
        destination.close()
        source.close()
        os.replace(temporary_path, backup_path)
        return backup_path
    except Exception:
        destination.close()
        source.close()
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
        raise
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


async def _apply_primary_attribute_rebalance(db, db_path: str) -> None:
    cursor = await db.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (PRIMARY_ATTRIBUTE_REBALANCE_MIGRATION,),
    )
    already_applied = await cursor.fetchone()
    await cursor.close()
    if already_applied:
        return

    cursor = await db.execute("SELECT COUNT(*) AS count FROM users")
    user_count = int((await cursor.fetchone())["count"])
    await cursor.close()
    if user_count:
        _backup_database_once(db_path)

    await db.execute("BEGIN")
    try:
        await db.execute(
            """
            UPDATE users
            SET hp = 1, atk = 1, defense = 1,
                speed = 1, luck = 1, willpower = 1,
                stat_points = MAX(0, level - 1),
                life_growth = 100, mana_growth = 100,
                advanced_speed = 100, advanced_luck = 100
            """
        )
        await db.execute(
            """
            UPDATE user_attribute_progress
            SET exp = 0, potential = 100
            """
        )
        await db.execute(
            """
            UPDATE level_freezes
            SET frozen_stats_json = '{}', frozen_stat_points = 1
            WHERE status = 'frozen'
            """
        )
        await db.execute(
            """
            INSERT INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (PRIMARY_ATTRIBUTE_REBALANCE_MIGRATION,),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def _apply_elona_balance_reset(db, db_path: str) -> None:
    cursor = await db.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (ELONA_BALANCE_MIGRATION,),
    )
    already_applied = await cursor.fetchone()
    await cursor.close()
    if already_applied:
        return

    cursor = await db.execute("SELECT COUNT(*) AS count FROM users")
    user_count = int((await cursor.fetchone())["count"])
    await cursor.close()
    if user_count:
        _backup_database_with_suffix(
            db_path, ELONA_BALANCE_BACKUP_SUFFIX
        )

    await db.execute("BEGIN")
    try:
        await db.execute(
            """
            UPDATE users
            SET hp = 1, atk = 1, defense = 1,
                speed = 1, luck = 1, willpower = 1,
                stat_points = MAX(0, level - 1),
                skill_points = MAX(0, level - 1),
                life_growth = 100, mana_growth = 100,
                advanced_speed = 100, advanced_luck = 100
            """
        )
        await db.execute(
            """
            UPDATE user_attribute_progress
            SET exp = 0, potential = 100
            """
        )
        await db.execute(
            """
            UPDATE level_freezes
            SET status = 'released',
                released_at = COALESCE(released_at, datetime('now'))
            WHERE status = 'frozen'
            """
        )
        await db.execute("DELETE FROM active_skill_slots")
        await db.execute("DELETE FROM user_skills")
        await db.execute("DELETE FROM user_spells")
        await db.execute("DELETE FROM spellbook_items")
        await db.execute("DELETE FROM equipment_loadout")
        await db.execute("DELETE FROM equipment_items")
        await db.execute(
            """
            DELETE FROM feature_grants
            WHERE grant_key IN (
                'skills-v1',
                'starter-armory-v1',
                'starter-armory-v2-materials'
            )
            """
        )
        await db.execute(
            """
            INSERT INTO schema_migrations (migration_id, applied_at)
            VALUES (?, datetime('now'))
            """,
            (ELONA_BALANCE_MIGRATION,),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
