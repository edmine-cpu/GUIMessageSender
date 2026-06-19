import pytest
from telethon.errors import FloodWaitError

import sender as sender_module
from models import Account
from sender import TelegramSender


class _FakeClient:
    def __init__(self, seconds: int):
        self.seconds = seconds
        self.calls = 0

    async def send_message(self, *args, **kwargs):
        self.calls += 1
        raise FloodWaitError(request=None, capture=self.seconds)


class _FakeDB:
    def __init__(self):
        self.flood_until = ""
        self.actions = []

    def try_acquire_action_slot(self, *args, **kwargs):
        return True, "ok", 0.0

    def set_account_flood_until(self, phone, flood_until):
        self.flood_until = flood_until

    def log_account_action(self, *args):
        self.actions.append(args)


@pytest.mark.asyncio
async def test_cycle_mode_floodwait_returns_immediately_without_sleep(monkeypatch):
    db = _FakeDB()
    fake_client = _FakeClient(42)
    sender = TelegramSender.__new__(TelegramSender)
    sender.account = Account(phone="+15550000001", session_name="session_test")
    sender.db = db
    sender.client = fake_client
    sender.sent_count = 0

    async def fail_sleep(seconds):
        raise AssertionError(f"sender must not sleep in cycle FloodWait mode: {seconds}")

    events = []
    monkeypatch.setattr(sender_module.asyncio, "sleep", fail_sleep)
    monkeypatch.setattr(sender_module, "log_event", lambda **kwargs: events.append(kwargs))

    result = await sender.send_mention_message(
        "@board",
        "hello",
        entities=[],
        min_interval_seconds=0,
        daily_actions_limit=0,
        sleep_on_flood_wait=False,
    )

    assert result == "flood_wait:42"
    assert fake_client.calls == 1
    assert db.flood_until
    assert db.actions == [
        ("+15550000001", "group", "@board", "flood_wait", "FloodWait 42s")
    ]
    assert events and events[0]["status"] == "flood_wait"
