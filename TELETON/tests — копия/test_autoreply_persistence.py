import os
import tempfile

import pytest

from database import Database


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


def test_autoreply_replied_forever_persists(tmp_db_path):
    db = Database(tmp_db_path)
    try:
        assert db.has_autoreplied_forever("+100", 1) is False
        db.mark_autoreplied_forever("+100", 1, replied_at="2025-01-01T10:00:00")
        assert db.has_autoreplied_forever("+100", 1) is True
    finally:
        db.close()

    db2 = Database(tmp_db_path)
    try:
        assert db2.has_autoreplied_forever("+100", 1) is True
        db2.log_autoreply_event(
            account_phone="+100",
            peer_id=1,
            peer_username="u",
            peer_name="User",
            incoming_text="hi",
            reply_text="hello",
            status="sent",
            reason="ok",
            created_at="2025-01-01T10:00:01",
        )
        row = db2.conn.execute("SELECT status, reason FROM autoreply_log").fetchone()
        assert row["status"] == "sent"
        assert row["reason"] == "ok"
    finally:
        db2.close()

