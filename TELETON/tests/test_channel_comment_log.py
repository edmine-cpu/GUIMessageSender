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


def test_channel_comment_log_insert(tmp_db_path):
    db = Database(tmp_db_path)
    try:
        db.log_channel_comment(
            channel="@test",
            post_id=123,
            comment_id=456,
            account_phone="+100",
            status="sent",
            error_text="",
            created_at="2025-01-01T10:00:00",
        )
        row = db.conn.execute(
            "SELECT channel, post_id, comment_id, account_phone, status, error_text, created_at FROM channel_comment_log"
        ).fetchone()
        assert row is not None
        assert row["channel"] == "@test"
        assert row["post_id"] == 123
        assert row["comment_id"] == 456
        assert row["account_phone"] == "+100"
        assert row["status"] == "sent"
        assert row["created_at"] == "2025-01-01T10:00:00"
    finally:
        db.close()

