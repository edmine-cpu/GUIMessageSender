import asyncio
import os
import tempfile

import pytest

from autoreply import (
    AutoReplyListener,
    REPLY_MODE_EVERY_MESSAGE,
    REPLY_MODE_FOREVER,
    REPLY_MODE_SESSION,
)
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


class _FakeMe:
    id = 1000
    phone = "+100"


class _FakeSender:
    def __init__(self, peer_id=1, username="user", first_name="User", bot=False):
        self.id = peer_id
        self.username = username
        self.first_name = first_name
        self.last_name = ""
        self.bot = bot


class _FakeMessage:
    def __init__(self, text):
        self.message = text
        self.text = text


class _FakeEvent:
    def __init__(self, sender, text):
        self._sender = sender
        self.message = _FakeMessage(text)
        self.replies = []

    async def get_sender(self):
        return self._sender

    async def reply(self, text):
        self.replies.append(text)


class _FakeClient:
    def __init__(self):
        self.handlers = []
        self.removed = []

    async def get_me(self):
        return _FakeMe()

    def on(self, *args, **kwargs):
        def decorator(handler):
            self.handlers.append(handler)
            return handler
        return decorator

    def remove_event_handler(self, handler):
        self.removed.append(handler)


class _FakeAutoReplyDB:
    def __init__(self):
        self.replied = set()
        self.events = []

    def try_acquire_action_slot(self, *args, **kwargs):
        return True, "ok", 0.0

    def has_autoreplied_forever(self, account_phone, peer_id):
        return (account_phone, peer_id) in self.replied

    def mark_autoreplied_forever(self, account_phone, peer_id):
        self.replied.add((account_phone, peer_id))

    def log_autoreply_event(self, **kwargs):
        self.events.append(kwargs)


async def _start_fake_listener(listener, client):
    task = asyncio.create_task(listener.start())
    for _ in range(20):
        if client.handlers:
            return task
        await asyncio.sleep(0)
    raise AssertionError("handler was not registered")


@pytest.mark.asyncio
async def test_autoreply_session_mode_replies_once_until_stopped():
    client = _FakeClient()
    listener = AutoReplyListener(
        client,
        "hello",
        progress_cb=lambda msg: None,
        reply_mode=REPLY_MODE_SESSION,
    )
    task = await _start_fake_listener(listener, client)

    sender = _FakeSender(peer_id=10)
    first = _FakeEvent(sender, "hi")
    second = _FakeEvent(sender, "again")
    await client.handlers[0](first)
    await client.handlers[0](second)

    listener.stop()
    await asyncio.wait_for(task, timeout=1)

    assert first.replies == ["hello"]
    assert second.replies == []
    assert client.removed == client.handlers


@pytest.mark.asyncio
async def test_autoreply_every_message_mode_replies_to_repeated_messages():
    client = _FakeClient()
    listener = AutoReplyListener(
        client,
        "hello",
        progress_cb=lambda msg: None,
        reply_mode=REPLY_MODE_EVERY_MESSAGE,
    )
    task = await _start_fake_listener(listener, client)

    sender = _FakeSender(peer_id=10)
    first = _FakeEvent(sender, "hi")
    second = _FakeEvent(sender, "again")
    await client.handlers[0](first)
    await client.handlers[0](second)

    listener.stop()
    await asyncio.wait_for(task, timeout=1)

    assert first.replies == ["hello"]
    assert second.replies == ["hello"]


@pytest.mark.asyncio
async def test_autoreply_forever_mode_uses_persisted_registry():
    client = _FakeClient()
    db = _FakeAutoReplyDB()
    db.replied.add(("+100", 10))
    listener = AutoReplyListener(
        client,
        "hello",
        progress_cb=lambda msg: None,
        reply_mode=REPLY_MODE_FOREVER,
        db=db,
        account_phone="+100",
    )
    task = await _start_fake_listener(listener, client)

    event = _FakeEvent(_FakeSender(peer_id=10), "hi")
    await client.handlers[0](event)

    listener.stop()
    await asyncio.wait_for(task, timeout=1)

    assert event.replies == []
    assert db.events[-1]["status"] == "skip"
    assert db.events[-1]["reason"] == "already_replied_forever"
