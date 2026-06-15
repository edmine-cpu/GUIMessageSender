"""
Тесты для механизма расписания удаления сессий (партия 6):
  - миграция БД v3 (таблица pending_device_terminations)
  - CRUD-методы add/get_due/mark_done/mark_failed/cleanup_old
  - сериализация auth_hashes через JSON
  - чистка истории по возрасту
"""
import os
import json
import tempfile
from datetime import datetime, timedelta

import pytest

from ads_database import AdsDB


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    database = AdsDB(path)
    yield database
    database.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


class TestSchemaV3:
    def test_table_created(self, db):
        """Миграция v3 создаёт таблицу pending_device_terminations."""
        rows = db.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='pending_device_terminations'"
        ).fetchall()
        assert len(rows) == 1

    def test_table_has_correct_columns(self, db):
        cur = db.conn.execute("PRAGMA table_info(pending_device_terminations)")
        cols = {row["name"] for row in cur.fetchall()}
        expected = {"id", "account_phone", "auth_hashes", "scheduled_at",
                    "created_at", "status", "last_error"}
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_index_created(self, db):
        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_pending_device_status_scheduled'"
        ).fetchall()
        assert len(rows) == 1


class TestAddPendingDeviceTermination:
    def test_basic_add(self, db):
        scheduled = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
        task_id = db.add_pending_device_termination(
            "+79001234567", [12345, 67890], scheduled)
        assert task_id > 0

    def test_stores_auth_hashes_as_json(self, db):
        scheduled = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        task_id = db.add_pending_device_termination(
            "+79001234567", [111, 222, 333], scheduled)

        row = db.conn.execute(
            "SELECT auth_hashes FROM pending_device_terminations WHERE id=?",
            (task_id,)
        ).fetchone()
        parsed = json.loads(row["auth_hashes"])
        assert parsed == [111, 222, 333]

    def test_default_status_pending(self, db):
        scheduled = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        task_id = db.add_pending_device_termination("+79001234567", [123], scheduled)

        row = db.conn.execute(
            "SELECT status FROM pending_device_terminations WHERE id=?",
            (task_id,)
        ).fetchone()
        assert row["status"] == "pending"

    def test_empty_auth_hashes_list(self, db):
        """Пустой список тоже сохраняется (не наша забота валидировать)."""
        scheduled = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        task_id = db.add_pending_device_termination("+79001234567", [], scheduled)
        assert task_id > 0


class TestGetDueDeviceTerminations:
    def test_returns_overdue_tasks(self, db):
        """Просроченная задача — возвращается."""
        past = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        db.add_pending_device_termination("+79001234567", [111], past)

        now_iso = datetime.now().isoformat(timespec="seconds")
        due = db.get_due_device_terminations(now_iso)
        assert len(due) == 1
        assert due[0]["account_phone"] == "+79001234567"
        assert due[0]["auth_hashes"] == [111]

    def test_skips_future_tasks(self, db):
        """Задача в будущем — не возвращается."""
        future = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
        db.add_pending_device_termination("+79001234567", [111], future)

        now_iso = datetime.now().isoformat(timespec="seconds")
        due = db.get_due_device_terminations(now_iso)
        assert len(due) == 0

    def test_skips_done_tasks(self, db):
        past = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        task_id = db.add_pending_device_termination("+79001234567", [111], past)
        db.mark_device_termination_done(task_id)

        now_iso = datetime.now().isoformat(timespec="seconds")
        due = db.get_due_device_terminations(now_iso)
        assert len(due) == 0

    def test_skips_failed_tasks(self, db):
        past = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        task_id = db.add_pending_device_termination("+79001234567", [111], past)
        db.mark_device_termination_failed(task_id, "test error")

        now_iso = datetime.now().isoformat(timespec="seconds")
        due = db.get_due_device_terminations(now_iso)
        assert len(due) == 0

    def test_orders_by_scheduled_at_asc(self, db):
        """Самые старые просроченные задачи возвращаются первыми."""
        oldest = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        middle = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        newest = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")

        db.add_pending_device_termination("+1", [111], newest)
        db.add_pending_device_termination("+2", [222], oldest)
        db.add_pending_device_termination("+3", [333], middle)

        now_iso = datetime.now().isoformat(timespec="seconds")
        due = db.get_due_device_terminations(now_iso)

        assert len(due) == 3
        assert due[0]["account_phone"] == "+2"  # oldest first
        assert due[1]["account_phone"] == "+3"
        assert due[2]["account_phone"] == "+1"

    def test_handles_corrupted_json(self, db):
        """Если auth_hashes в БД повреждён — возвращаем пустой список (не падаем)."""
        past = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        # Прямая SQL-вставка с битым JSON
        db.conn.execute("""
            INSERT INTO pending_device_terminations
            (account_phone, auth_hashes, scheduled_at, created_at, status)
            VALUES ('+79001', 'NOT_VALID_JSON', ?, ?, 'pending')
        """, (past, datetime.now().isoformat(timespec="seconds")))
        db.conn.commit()

        due = db.get_due_device_terminations(
            datetime.now().isoformat(timespec="seconds"))
        assert len(due) == 1
        assert due[0]["auth_hashes"] == []  # битый JSON → []


class TestMarkDoneAndFailed:
    def test_mark_done_changes_status(self, db):
        past = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        task_id = db.add_pending_device_termination("+79001234567", [111], past)

        db.mark_device_termination_done(task_id)
        row = db.conn.execute(
            "SELECT status FROM pending_device_terminations WHERE id=?",
            (task_id,)
        ).fetchone()
        assert row["status"] == "done"

    def test_mark_failed_stores_error(self, db):
        past = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        task_id = db.add_pending_device_termination("+79001234567", [111], past)

        db.mark_device_termination_failed(task_id, "Connection refused")
        row = db.conn.execute(
            "SELECT status, last_error FROM pending_device_terminations WHERE id=?",
            (task_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert row["last_error"] == "Connection refused"

    def test_mark_failed_truncates_long_error(self, db):
        """Длинное сообщение об ошибке обрезается до 500 символов."""
        past = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        task_id = db.add_pending_device_termination("+79001234567", [111], past)

        long_err = "X" * 1000
        db.mark_device_termination_failed(task_id, long_err)
        row = db.conn.execute(
            "SELECT last_error FROM pending_device_terminations WHERE id=?",
            (task_id,)
        ).fetchone()
        assert len(row["last_error"]) == 500


class TestCleanupOldTerminations:
    def test_removes_old_done(self, db):
        # Создаём задачу через 35 дней назад, помечаем done вручную
        old_date = (datetime.now() - timedelta(days=35)).isoformat(timespec="seconds")
        db.conn.execute("""
            INSERT INTO pending_device_terminations
            (account_phone, auth_hashes, scheduled_at, created_at, status)
            VALUES (?, ?, ?, ?, 'done')
        """, ("+79001", json.dumps([111]), old_date, old_date))
        db.conn.commit()

        db.cleanup_old_device_terminations(days_old=30)

        rows = db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_device_terminations"
        ).fetchone()
        assert rows["cnt"] == 0

    def test_keeps_recent_done(self, db):
        recent = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
        db.conn.execute("""
            INSERT INTO pending_device_terminations
            (account_phone, auth_hashes, scheduled_at, created_at, status)
            VALUES (?, ?, ?, ?, 'done')
        """, ("+79001", json.dumps([111]), recent, recent))
        db.conn.commit()

        db.cleanup_old_device_terminations(days_old=30)
        rows = db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_device_terminations"
        ).fetchone()
        assert rows["cnt"] == 1

    def test_keeps_old_pending(self, db):
        """Чистка НЕ трогает pending-задачи, даже если они старые
        (например, после долгого простоя GUI)."""
        old_date = (datetime.now() - timedelta(days=100)).isoformat(timespec="seconds")
        db.conn.execute("""
            INSERT INTO pending_device_terminations
            (account_phone, auth_hashes, scheduled_at, created_at, status)
            VALUES (?, ?, ?, ?, 'pending')
        """, ("+79001", json.dumps([111]), old_date, old_date))
        db.conn.commit()

        db.cleanup_old_device_terminations(days_old=30)
        rows = db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_device_terminations "
            "WHERE status='pending'"
        ).fetchone()
        assert rows["cnt"] == 1


class TestSchedulerSettingsTdataFields:
    """Новые поля для импорта TData и устройств загружаются/сохраняются."""

    def test_default_values_present(self, db):
        s = db.load_scheduler_settings()
        assert s.tdata_connect_timeout_seconds == 60
        assert s.tdata_get_me_timeout_seconds == 60
        assert s.tdata_flood_max_wait_seconds == 300
        assert s.tdata_flood_jitter_min_seconds == 1
        assert s.tdata_flood_jitter_max_seconds == 3
        assert s.device_terminate_delay_min_seconds == 1
        assert s.device_terminate_delay_max_seconds == 3
        assert s.device_terminate_default_schedule_hours == 2

    def test_save_and_load_tdata_settings(self, db):
        s = db.load_scheduler_settings()
        s.tdata_connect_timeout_seconds = 120
        s.tdata_flood_max_wait_seconds = 600
        s.device_terminate_default_schedule_hours = 4
        db.save_scheduler_settings(s)

        s2 = db.load_scheduler_settings()
        assert s2.tdata_connect_timeout_seconds == 120
        assert s2.tdata_flood_max_wait_seconds == 600
        assert s2.device_terminate_default_schedule_hours == 4
