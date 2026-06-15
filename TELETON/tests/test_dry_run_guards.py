import asyncio
from types import SimpleNamespace

from account_manager import change_profile, leave_groups
from parser import ensure_chat_access


class _NoSideEffectsClient:
    def __init__(self):
        self.calls = []

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        name = type(request).__name__
        if name == "CheckChatInviteRequest":
            return SimpleNamespace()
        raise AssertionError(f"Unexpected request: {name}")

    async def get_entity(self, group):
        self.calls.append(("get_entity", group))
        return SimpleNamespace(id=123, title="Test")

    async def get_dialogs(self):
        self.calls.append("get_dialogs")
        return [
            SimpleNamespace(is_group=True, name="Group One", entity=SimpleNamespace(id=1)),
            SimpleNamespace(is_group=False, name="User Chat", entity=SimpleNamespace(id=2)),
        ]

    async def get_me(self):
        raise AssertionError("get_me should not be called in dry run")

    async def upload_file(self, path):
        raise AssertionError("upload_file should not be called in dry run")


def test_ensure_chat_access_dry_run_uses_read_only_invite_check():
    client = _NoSideEffectsClient()

    decision, reason, retry_after = asyncio.run(
        ensure_chat_access(client, "https://t.me/+abcdef", dry_run=True)
    )

    assert decision == "ok"
    assert reason == "dry_run_would_join"
    assert retry_after == ""
    assert client.calls == ["CheckChatInviteRequest"]


def test_change_profile_dry_run_does_not_touch_telegram():
    client = _NoSideEffectsClient()
    logs = []

    ok = asyncio.run(
        change_profile(
            client,
            first_name="Test",
            bio="Hello",
            avatar_path="/tmp/avatar.jpg",
            progress_cb=logs.append,
            dry_run=True,
        )
    )

    assert ok is True
    assert any("DRY" in msg for msg in logs)
    assert client.calls == []


def test_leave_groups_dry_run_only_lists_targets():
    client = _NoSideEffectsClient()
    logs = []

    count = asyncio.run(leave_groups(client, progress_cb=logs.append, dry_run=True))

    assert count == 0
    assert any("Вышел бы из группы: Group One" in msg for msg in logs)
    assert "get_dialogs" in client.calls
    assert all(call != "LeaveChannelRequest" for call in client.calls if isinstance(call, str))
