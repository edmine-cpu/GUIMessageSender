import os
import tempfile
import time

import pytest

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

