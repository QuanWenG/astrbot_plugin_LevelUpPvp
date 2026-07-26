import os
import shutil
import sqlite3
from pathlib import Path


DATABASE_FILENAME = "db_level_up_pvp.db"
LEGACY_BACKUP_SUFFIXES = (
    ".pre-primary-attributes-v2.bak",
    ".pre-elona-balance-v1.bak",
)


def prepare_persistent_database(
    persistent_dir: str | os.PathLike[str],
    legacy_db_path: str | os.PathLike[str],
) -> str:
    """Use AstrBot persistent storage and import a valid legacy DB once."""
    target_dir = Path(persistent_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / DATABASE_FILENAME
    legacy = Path(legacy_db_path)
    source = _best_legacy_source(legacy)

    if _is_valid_database(target):
        target_score = _database_progress_score(target)
        source_score = (
            _database_progress_score(source)
            if source is not None
            else (0, 0, 0)
        )
        if target_score != (0, 0, 0) or source_score == (0, 0, 0):
            return str(target)

    if source is not None:
        temporary = target.with_name(target.name + ".importing")
        if temporary.exists():
            temporary.unlink()
        shutil.copy2(source, temporary)
        os.replace(temporary, target)

    return str(target)


def _best_legacy_source(legacy: Path) -> Path | None:
    candidates = [
        *(Path(str(legacy) + suffix) for suffix in LEGACY_BACKUP_SUFFIXES),
        legacy,
    ]
    scored = [
        (_database_progress_score(candidate), -index, candidate)
        for index, candidate in enumerate(candidates)
        if _is_valid_database(candidate)
    ]
    if not scored:
        return None
    return max(scored, key=lambda item: (item[0], item[1]))[2]


def _is_valid_database(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    db = None
    try:
        db = sqlite3.connect(str(path))
        integrity = db.execute("PRAGMA quick_check").fetchone()
        users_table = db.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'users'
            """
        ).fetchone()
        return bool(integrity and integrity[0] == "ok" and users_table)
    except sqlite3.DatabaseError:
        return False
    finally:
        if db is not None:
            db.close()


def _database_progress_score(path: Path) -> tuple[int, int, int]:
    db = None
    try:
        db = sqlite3.connect(str(path))
        row = db.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(level), 0),
                   COALESCE(SUM(total_exp), 0)
            FROM users
            """
        ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])
    except (sqlite3.DatabaseError, TypeError, ValueError):
        return 0, 0, 0
    finally:
        if db is not None:
            db.close()
