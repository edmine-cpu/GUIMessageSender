import os
import tempfile
import time
from datetime import date, datetime

import pytest

import database as database_module
from database import Database
from models import Account


@pytest.fixture
def tmp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    yield path
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def test_try_acquire_action_slot_min_interval(tmp_db_path):
    db = Database(tmp_db_path)
    try:
        db.add_account(Account(phone="+100", session_name="data/sessions/session_+100"))
        ok, reason, wait_s = db.try_acquire_action_slot("+100", "dm", min_interval_seconds=0.5, daily_actions_limit=10)
        assert ok is True
        ok2, reason2, wait_s2 = db.try_acquire_action_slot("+100", "dm", min_interval_seconds=0.5, daily_actions_limit=10)
        assert ok2 is False
        assert reason2 == "min_interval"
        assert wait_s2 > 0
        time.sleep(0.6)
        ok3, reason3, wait_s3 = db.try_acquire_action_slot("+100", "dm", min_interval_seconds=0.5, daily_actions_limit=10)
        assert ok3 is True
        assert reason3 == "ok"
        assert wait_s3 == 0.0
    finally:
        db.close()


def test_try_acquire_action_slot_daily_limit_pauses_until_next_midnight_and_health_reason(tmp_db_path, monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 18)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 18, 14, 30, 0, tzinfo=tz)

    monkeypatch.setattr(database_module, "date", FixedDate)
    monkeypatch.setattr(database_module, "datetime", FixedDateTime)

    daily_limit = 2
    phone = "+300"
    db = Database(tmp_db_path)
    try:
        db.add_account(Account(
            phone=phone,
            session_name=f"data/sessions/session_{phone}",
            actions_today=daily_limit,
            last_reset_date=FixedDate.today().isoformat(),
        ))

        ok, reason, wait_s = db.try_acquire_action_slot(
            phone,
            "dm",
            min_interval_seconds=0,
            daily_actions_limit=daily_limit,
        )

        assert ok is False
        assert reason == "daily_limit"
        assert wait_s == 0.0

        acc = next(x for x in db.get_all_accounts() if x.phone == phone)
        assert acc.paused_until == "2026-06-19T00:00:00"
        assert acc.pause_reason == f"daily_actions_limit:{daily_limit}"

        h = next(x for x in db.get_accounts_health() if x["phone"] == phone)
        assert h["health"] == "paused"
        assert "дневной лимит" in h["why"]
        assert "подождать" not in h["why"]
        assert "между действиями" not in h["why"]
    finally:
        db.close()


def test_daily_limit_auto_pause_clears_when_limit_disabled(tmp_db_path):
    phone = "+301"
    db = Database(tmp_db_path)
    try:
        db.add_account(Account(
            phone=phone,
            actions_today=9,
            last_reset_date=date.today().isoformat(),
            paused_until="2999-01-01T00:00:00",
            pause_reason="daily_actions_limit:5",
        ))

        changed = db.clear_daily_limit_auto_pauses([phone], daily_actions_limit=0)
        assert changed == 1

        acc = next(x for x in db.get_all_accounts() if x.phone == phone)
        assert acc.paused_until == ""
        assert acc.pause_reason == ""

        ok, reason, wait_s = db.try_acquire_action_slot(
            phone,
            "dm",
            min_interval_seconds=0,
            daily_actions_limit=0,
        )
        assert (ok, reason, wait_s) == (True, "ok", 0.0)
    finally:
        db.close()


def test_daily_limit_auto_pause_clears_when_limit_raised_above_actions_today(tmp_db_path):
    phone = "+302"
    db = Database(tmp_db_path)
    try:
        db.add_account(Account(
            phone=phone,
            actions_today=6,
            last_reset_date=date.today().isoformat(),
            paused_until="2999-01-01T00:00:00",
            pause_reason="daily_actions_limit:5",
        ))

        changed = db.clear_daily_limit_auto_pauses([phone], daily_actions_limit=10)
        assert changed == 1

        acc = next(x for x in db.get_all_accounts() if x.phone == phone)
        assert acc.paused_until == ""
        assert acc.pause_reason == ""

        ok, reason, wait_s = db.try_acquire_action_slot(
            phone,
            "dm",
            min_interval_seconds=0,
            daily_actions_limit=10,
        )
        assert (ok, reason, wait_s) == (True, "ok", 0.0)
    finally:
        db.close()


def test_try_acquire_action_slot_clears_stale_daily_limit_pause(tmp_db_path):
    phone = "+303"
    db = Database(tmp_db_path)
    try:
        db.add_account(Account(
            phone=phone,
            actions_today=4,
            last_reset_date=date.today().isoformat(),
            paused_until="2999-01-01T00:00:00",
            pause_reason="daily_actions_limit:3",
        ))

        ok, reason, wait_s = db.try_acquire_action_slot(
            phone,
            "dm",
            min_interval_seconds=0,
            daily_actions_limit=8,
        )
        assert (ok, reason, wait_s) == (True, "ok", 0.0)

        acc = next(x for x in db.get_all_accounts() if x.phone == phone)
        assert acc.paused_until == ""
        assert acc.pause_reason == ""
    finally:
        db.close()


def test_clear_daily_limit_auto_pauses_keeps_manual_pause(tmp_db_path):
    phone = "+304"
    db = Database(tmp_db_path)
    try:
        db.add_account(Account(
            phone=phone,
            actions_today=1,
            last_reset_date=date.today().isoformat(),
            paused_until="2999-01-01T00:00:00",
            pause_reason="manual",
        ))

        changed = db.clear_daily_limit_auto_pauses([phone], daily_actions_limit=0)
        assert changed == 0

        acc = next(x for x in db.get_all_accounts() if x.phone == phone)
        assert acc.paused_until == "2999-01-01T00:00:00"
        assert acc.pause_reason == "manual"
    finally:
        db.close()


def test_log_account_action_updates_counters(tmp_db_path):
    db = Database(tmp_db_path)
    try:
        db.add_account(Account(phone="+200", session_name="data/sessions/session_+200"))
        db.log_account_action("+200", "dm", "1", "sent", "")
        h = next(x for x in db.get_accounts_health() if x["phone"] == "+200")
        assert h["sent_today"] == 1
        assert h["actions_today"] == 1
        assert h["error_today"] == 0
        assert h["last_send_at"]

        db.log_account_action("+200", "dm", "1", "error", "oops")
        h2 = next(x for x in db.get_accounts_health() if x["phone"] == "+200")
        assert h2["actions_today"] == 2
        assert h2["error_today"] == 1
        assert "oops" in h2["last_error_text"] or h2["last_error_text"]
    finally:
        db.close()
