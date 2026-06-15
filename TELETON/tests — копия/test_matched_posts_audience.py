import os
import tempfile

import pytest

from database import Database
from models import MatchedPost


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


def test_matched_posts_group_by_audience_key(tmp_db_path):
    db = Database(tmp_db_path)
    try:
        db.save_matched_post(MatchedPost(
            message_id=1,
            group_source="@chat1",
            origin_group="models",
            message_date="2025-01-01T10:00:00",
            message_link="https://t.me/chat1/1",
            sender_id=101,
            sender_username="u1",
            message_text="hello",
            match_mode="keywords",
            matched_keywords="model",
            ai_reason="",
            matched_at="2025-01-01T10:01:00",
        ))
        db.save_matched_post(MatchedPost(
            message_id=1,
            group_source="@chat2",
            origin_group="models",
            message_date="2025-01-01T11:00:00",
            message_link="https://t.me/chat2/1",
            sender_id=202,
            sender_username="u2",
            message_text="world",
            match_mode="keywords",
            matched_keywords="agency",
            ai_reason="",
            matched_at="2025-01-01T11:01:00",
        ))

        posts = db.get_matched_posts("models")
        assert len(posts) == 2
        assert {p.group_source for p in posts} == {"@chat1", "@chat2"}

        auds = db.get_all_audiences()
        matched = [a for a in auds if a["audience_type"] == "matched"]
        assert any(a["group_source"] == "models" for a in matched)

        members = db.get_audience_members("models", "matched")
        assert {m["user_id"] for m in members} == {101, 202}
    finally:
        db.close()

