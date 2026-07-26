import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.storage import DATABASE_FILENAME, prepare_persistent_database


def _write_user_database(path: Path, level: int, total_exp: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    try:
        db.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                level INTEGER NOT NULL,
                total_exp INTEGER NOT NULL
            )
            """
        )
        db.execute(
            "INSERT INTO users (level, total_exp) VALUES (?, ?)",
            (level, total_exp),
        )
        db.commit()
    finally:
        db.close()


def _read_one(path: str, sql: str):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql).fetchone()
    finally:
        db.close()


class PersistentStorageTests(unittest.TestCase):
    def test_imports_legacy_database_outside_plugin_directory(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            legacy = root_path / "plugin" / "data" / DATABASE_FILENAME
            persistent = root_path / "astrbot-data" / "plugin_data" / "plugin"
            _write_user_database(legacy, 17, 900)

            selected = prepare_persistent_database(persistent, legacy)

            self.assertEqual(selected, str(persistent / DATABASE_FILENAME))
            self.assertEqual(
                _read_one(selected, "SELECT level, total_exp FROM users"),
                (17, 900),
            )

    def test_prefers_progress_backup_and_never_overwrites_persistent_db(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            legacy = root_path / "plugin" / "data" / DATABASE_FILENAME
            persistent = root_path / "persistent"
            _write_user_database(legacy, 1, 0)
            _write_user_database(
                Path(str(legacy) + ".pre-primary-attributes-v2.bak"),
                25,
                5000,
            )

            selected = prepare_persistent_database(persistent, legacy)
            self.assertEqual(
                _read_one(selected, "SELECT level FROM users")[0],
                25,
            )

            replacement = root_path / "replacement.db"
            _write_user_database(replacement, 99, 99999)
            selected_again = prepare_persistent_database(persistent, replacement)
            self.assertEqual(
                _read_one(selected_again, "SELECT level FROM users")[0],
                25,
            )

    def test_ignores_zero_byte_legacy_database(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            legacy = root_path / "plugin" / "data" / DATABASE_FILENAME
            legacy.parent.mkdir(parents=True)
            legacy.touch()

            selected = prepare_persistent_database(
                root_path / "persistent", legacy
            )

            self.assertFalse(Path(selected).exists())

    def test_replaces_schema_only_target_with_populated_legacy_db(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            legacy = root_path / "plugin" / "data" / DATABASE_FILENAME
            persistent = root_path / "persistent"
            persistent.mkdir()
            empty_target = persistent / DATABASE_FILENAME
            db = sqlite3.connect(str(empty_target))
            try:
                db.execute(
                    """
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY,
                        level INTEGER NOT NULL,
                        total_exp INTEGER NOT NULL
                    )
                    """
                )
                db.commit()
            finally:
                db.close()
            _write_user_database(legacy, 12, 700)

            selected = prepare_persistent_database(persistent, legacy)

            self.assertEqual(
                _read_one(selected, "SELECT level FROM users")[0],
                12,
            )
