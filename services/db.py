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
                level_up_count INTEGER NOT NULL DEFAULT 0,
                hp INTEGER NOT NULL DEFAULT 10,
                atk INTEGER NOT NULL DEFAULT 5,
                defense INTEGER NOT NULL DEFAULT 5,
                speed INTEGER NOT NULL DEFAULT 5,
                luck INTEGER NOT NULL DEFAULT 5,
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
                created_at TEXT NOT NULL,
                created_at_ts INTEGER NOT NULL,
                FOREIGN KEY(attacker_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(defender_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(winner_pk) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(loser_pk) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS level_up_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_pk INTEGER NOT NULL,
                from_level INTEGER NOT NULL,
                to_level INTEGER NOT NULL,
                auto_growth_json TEXT NOT NULL,
                stat_points_gain INTEGER NOT NULL,
                created_at TEXT NOT NULL,
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
            CREATE INDEX IF NOT EXISTS idx_stat_point_logs_user
                ON stat_point_logs(user_pk);
            """
        )
        await db.commit()
