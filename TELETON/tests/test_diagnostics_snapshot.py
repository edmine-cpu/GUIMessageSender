import os
import tempfile
from datetime import datetime, timedelta

import pytest

from ads_database import AdsDB
from ads_models import PublicationLog
from database import Database
from models import (
    Account,
    SendLog,
    Task,
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_NEEDS_REAUTH,
    ACCOUNT_STATUS_NETWORK_ISSUE,
)


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def _account(phone, status=ACCOUNT_STATUS_ACTIVE, is_active=True, flood_until=""):
    return Account(
        phone=phone,
        is_active=is_active,
        status=status,
        flood_until=flood_until,
        api_id=2040,
        api_hash="hash",
    )


def test_get_diagnostics_snapshot_counts_accounts_tasks_and_errors(db_path):
    db = Database(db_path)
    try:
        db.add_account(_account("+100"))
        db.add_account(_account("+101", flood_until=(datetime.now() + timedelta(minutes=20)).isoformat()))
        db.add_account(_account("+102", status=ACCOUNT_STATUS_NEEDS_REAUTH))
        db.add_account(_account("+103", status=ACCOUNT_STATUS_NETWORK_ISSUE))

        db.add_task(Task(target_group="@ready", message_text="hello", task_type="broadcast"))
        db.add_task(Task(target_group="@wait", message_text="hello", task_type="broadcast"))
        db.add_task(Task(target_group="@err", message_text="hello", task_type="broadcast"))
        db.add_task(Task(target_group="@done", message_text="hello", task_type="broadcast"))
        task_ids = {t.target_group: t.id for t in db.get_all_tasks()}
        waiting_id = task_ids["@wait"]
        error_id = task_ids["@err"]
        done_id = task_ids["@done"]
        db.mark_task_waiting(waiting_id, (datetime.now() + timedelta(minutes=5)).isoformat(), "flood_wait")
        db.mark_task_error(error_id, "empty_text")
        db.mark_task_completed(done_id)

        db.log_send(SendLog(
            account_phone="+100",
            target_group="@err",
            message_text="x",
            status="no_permission",
            error_detail="ChatWriteForbiddenError",
        ))
    finally:
        db.close()

    ads_db = AdsDB(db_path)
    try:
        ads_db.add_publication_log(PublicationLog(
            ad_id=1,
            group_id=2,
            account_phone="+101",
            time=datetime.now().isoformat(),
            status="flood_wait",
            error_text="FloodWait 60s",
        ))
    finally:
        ads_db.close()

    db = Database(db_path)
    try:
        snapshot = db.get_diagnostics_snapshot(days=1)
    finally:
        db.close()

    assert snapshot["accounts"]["enabled"] == 4
    assert snapshot["accounts"]["available"] == 1
    assert snapshot["accounts"]["flood_wait"] == 1
    assert snapshot["accounts"]["needs_reauth"] == 1
    assert snapshot["accounts"]["network_issue"] == 1
    assert snapshot["tasks"]["pending"] == 1
    assert snapshot["tasks"]["waiting"] == 1
    assert snapshot["tasks"]["error"] == 1
    assert snapshot["tasks"]["done"] == 1
    assert snapshot["errors"]["by_status"]["no_permission"] == 1
    assert snapshot["errors"]["by_status"]["flood_wait"] == 1
    assert any("нет прав" in item["reason"] for item in snapshot["errors"]["recent"])
