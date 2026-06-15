"""
Тесты миграции БД: проверяем, что init_db корректно создаёт схему v3
с нуля, а также умеет мигрировать с v1 и v2 без потери данных.
"""
import os
import sqlite3
import tempfile

import pytest

from database import Database, SCHEMA_VERSION
from models import (
    Account,
    ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_NEEDS_REAUTH,
    ACCOUNT_STATUS_BANNED, ACCOUNT_STATUS_NETWORK_ISSUE,
)


@pytest.fixture
def tmp_db_path():
    """Временный путь под SQLite-файл, файл удаляется после теста."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # Database сам создаст
    yield path
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


class TestFreshInstall:
    """БД создаётся с нуля — должна сразу быть на SCHEMA_VERSION."""

    def test_fresh_db_is_on_latest_schema(self, tmp_db_path):
        db = Database(tmp_db_path)
        try:
            version = db.conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == SCHEMA_VERSION
        finally:
            db.close()

    def test_fresh_db_has_all_account_columns(self, tmp_db_path):
        db = Database(tmp_db_path)
        try:
            cols = {row[1] for row in
                    db.conn.execute("PRAGMA table_info(accounts)").fetchall()}
            expected = {
                "phone", "session_name", "proxy", "is_active",
                "sent_today", "last_reset_date",
                "api_id", "api_hash",
                "device_model", "system_version", "app_version", "lang_code",
                "status", "flood_until", "connect_fail_count", "last_status_change",
            }
            assert expected.issubset(cols), f"Missing: {expected - cols}"
        finally:
            db.close()

    def test_fresh_db_is_wal(self, tmp_db_path):
        db = Database(tmp_db_path)
        try:
            mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            db.close()

    def test_fresh_db_has_indexes(self, tmp_db_path):
        db = Database(tmp_db_path)
        try:
            indexes = {row[0] for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
            assert "idx_send_log_target_status" in indexes
            assert "idx_send_log_timestamp" in indexes
            assert "idx_parsed_users_group" in indexes
        finally:
            db.close()


class TestMigrationFromV1:
    """Имитируем старую БД v1 (до миграции) и проверяем восстановление."""

    def _create_v1_schema(self, path):
        """Ровно то, что было в v1: только базовые поля accounts."""
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE accounts (
                phone TEXT PRIMARY KEY,
                session_name TEXT NOT NULL,
                proxy TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                sent_today INTEGER DEFAULT 0,
                last_reset_date TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE TABLE parsed_users (user_id INTEGER, username TEXT, "
                     "first_name TEXT, last_name TEXT, phone TEXT, "
                     "group_source TEXT NOT NULL, status TEXT DEFAULT '', "
                     "is_bot INTEGER DEFAULT 0, "
                     "PRIMARY KEY (user_id, group_source))")
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, "
                     "target_group TEXT, message_text TEXT, task_type TEXT, "
                     "source_group TEXT, mentions_per_message INTEGER, "
                     "completed INTEGER DEFAULT 0)")
        conn.execute("CREATE TABLE matched_posts (id INTEGER PRIMARY KEY, "
                     "message_id INTEGER, group_source TEXT, sender_id INTEGER, "
                     "sender_username TEXT, message_text TEXT, match_mode TEXT, "
                     "matched_keywords TEXT, ai_reason TEXT, matched_at TEXT, "
                     "UNIQUE(message_id, group_source))")
        conn.execute("CREATE TABLE send_log (id INTEGER PRIMARY KEY, "
                     "account_phone TEXT, target_group TEXT, message_text TEXT, "
                     "status TEXT, error_detail TEXT, timestamp TEXT)")
        # Добавим данные, которые должны пережить миграцию
        conn.execute(
            "INSERT INTO accounts (phone, session_name, proxy, is_active, sent_today) "
            "VALUES (?, ?, ?, ?, ?)",
            ("+79001234567", "data/sessions/session_+79001234567", "", 1, 42),
        )
        # user_version = 1
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

    def test_migrates_v1_to_latest(self, tmp_db_path):
        self._create_v1_schema(tmp_db_path)

        db = Database(tmp_db_path)
        try:
            version = db.conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == SCHEMA_VERSION
        finally:
            db.close()

    def test_v1_data_preserved_after_migration(self, tmp_db_path):
        self._create_v1_schema(tmp_db_path)

        db = Database(tmp_db_path)
        try:
            accs = db.get_all_accounts()
            assert len(accs) == 1
            acc = accs[0]
            assert acc.phone == "+79001234567"
            assert acc.sent_today == 42
            # Новые поля должны иметь дефолты
            assert acc.api_id == 0
            assert acc.api_hash == ""
            assert acc.status == ACCOUNT_STATUS_ACTIVE
            assert acc.flood_until == ""
            assert acc.connect_fail_count == 0
        finally:
            db.close()


class TestMigrationIdempotency:
    """Повторный вызов init_db на уже мигрированной БД не должен падать."""

    def test_init_db_twice_no_error(self, tmp_db_path):
        db1 = Database(tmp_db_path)
        db1.close()
        # Второй init на той же БД
        db2 = Database(tmp_db_path)
        try:
            version = db2.conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == SCHEMA_VERSION
        finally:
            db2.close()

    def test_add_account_with_all_new_fields(self, tmp_db_path):
        """Account можно создать с полным набором полей и достать обратно."""
        db = Database(tmp_db_path)
        try:
            acc = Account(
                phone="+79009998877",
                session_name="data/sessions/session_+79009998877",
                proxy="socks5://u:p@h:1080",
                api_id=2040,
                api_hash="b18441a1ff607e10a989891a5462e627",
                device_model="Desktop",
                system_version="Windows 10",
                app_version="5.6.3 x64",
                lang_code="ru",
                status=ACCOUNT_STATUS_ACTIVE,
            )
            db.add_account(acc)

            restored = db.get_all_accounts()[0]
            assert restored.api_id == 2040
            assert restored.device_model == "Desktop"
            assert restored.app_version == "5.6.3 x64"
            assert restored.lang_code == "ru"
            assert restored.status == ACCOUNT_STATUS_ACTIVE
        finally:
            db.close()
