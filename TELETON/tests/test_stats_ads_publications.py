import os
import tempfile
from datetime import datetime

import pytest

from database import Database


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    database = Database(path)
    database.conn.execute("""
        CREATE TABLE publications_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            account_phone TEXT NOT NULL,
            time TEXT NOT NULL,
            status TEXT NOT NULL,
            error_text TEXT DEFAULT '',
            message_id INTEGER
        )
    """)
    database.conn.commit()
    yield database
    database.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def _insert_ad_log(db: Database, phone: str, status: str):
    db.conn.execute("""
        INSERT INTO publications_log
            (ad_id, group_id, account_phone, time, status, error_text, message_id)
        VALUES
            (1, 1, ?, ?, ?, '', 123)
    """, (phone, datetime.now().isoformat(timespec="seconds"), status))
    db.conn.commit()


def test_get_stats_includes_ads_publications_when_send_log_is_empty(db):
    _insert_ad_log(db, "+100", "ok")
    _insert_ad_log(db, "+100", "slow_mode")
    _insert_ad_log(db, "+200", "forbidden")

    stats = db.get_stats(days=7)

    assert stats["total"] == 3
    assert stats["sent"] == 1
    assert stats["flood_wait"] == 1
    assert stats["no_permission"] == 1


def test_get_per_account_stats_includes_ads_publications(db):
    _insert_ad_log(db, "+100", "ok")
    _insert_ad_log(db, "+100", "ok")
    _insert_ad_log(db, "+200", "banned")

    rows = db.get_per_account_stats(days=7)

    assert {"phone": "+100", "status": "sent", "count": 2} in rows
    assert {"phone": "+200", "status": "banned", "count": 1} in rows
