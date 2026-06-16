import os
import tempfile

import pytest

from database import Database
from models import MatchedPost, ParsedUser


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


def test_matched_posts_context_keeps_message_metadata(tmp_db_path):
    db = Database(tmp_db_path)
    try:
        db.save_matched_post(MatchedPost(
            message_id=11,
            group_source="@source_chat",
            origin_group="models",
            message_date="2025-01-02T10:00:00",
            message_link="https://t.me/source_chat/11",
            sender_id=303,
            sender_username="lead",
            message_text="full post text",
            match_mode="ai",
            matched_keywords="",
            ai_reason="fits criteria",
            matched_at="2025-01-02T10:01:00",
        ))

        rows = db.get_matched_posts_context("models")

        assert rows == [{
            "user_id": 303,
            "username": "lead",
            "source_chat": "@source_chat",
            "message_date": "2025-01-02T10:00:00",
            "message_link": "https://t.me/source_chat/11",
            "message_text": "full post text",
            "match_mode": "ai",
            "matched_keywords": "",
            "ai_reason": "fits criteria",
            "matched_at": "2025-01-02T10:01:00",
        }]
    finally:
        db.close()


def test_delete_users_audience_only_removes_selected_group(tmp_db_path):
    db = Database(tmp_db_path)
    try:
        db.save_parsed_users([
            ParsedUser(user_id=1, username="one", group_source="models"),
            ParsedUser(user_id=2, username="two", group_source="other"),
        ])

        deleted = db.delete_audience("models", "users")

        assert deleted == 1
        assert db.get_audience_members("models", "users") == []
        remaining = db.get_audience_members("other", "users")
        assert [u["user_id"] for u in remaining] == [2]
    finally:
        db.close()


def test_delete_matched_audience_only_removes_selected_origin_group(tmp_db_path):
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
            message_id=2,
            group_source="@chat2",
            origin_group="models",
            message_date="2025-01-01T11:00:00",
            message_link="https://t.me/chat2/2",
            sender_id=202,
            sender_username="u2",
            message_text="world",
            match_mode="keywords",
            matched_keywords="agency",
            ai_reason="",
            matched_at="2025-01-01T11:01:00",
        ))
        db.save_matched_post(MatchedPost(
            message_id=3,
            group_source="@chat3",
            origin_group="other",
            message_date="2025-01-01T12:00:00",
            message_link="https://t.me/chat3/3",
            sender_id=303,
            sender_username="u3",
            message_text="keep",
            match_mode="keywords",
            matched_keywords="keep",
            ai_reason="",
            matched_at="2025-01-01T12:01:00",
        ))

        deleted = db.delete_audience("models", "matched")

        assert deleted == 2
        assert db.get_matched_posts("models") == []
        assert [p.sender_id for p in db.get_matched_posts("other")] == [303]
    finally:
        db.close()
