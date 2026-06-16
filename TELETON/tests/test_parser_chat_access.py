import pytest

from parser import ensure_chat_access, inspect_chat_access, join_group


class _JoinClient:
    def __init__(self):
        self.request = None

    async def __call__(self, request):
        self.request = request
        return object()


class _EntityClient:
    def __init__(self):
        self.target = None

    async def get_entity(self, target):
        self.target = target
        return object()


class _BoomJoinClient:
    async def __call__(self, request):
        raise RuntimeError("boom\nwith whitespace")


class _BoomEntityClient:
    async def get_entity(self, target):
        raise RuntimeError("resolve failed")


@pytest.mark.asyncio
async def test_join_group_normalizes_public_tme_url_to_username():
    client = _JoinClient()

    result = await join_group(client, "https://t.me/onlyadating")

    assert result == "joined"
    assert client.request.channel == "@onlyadating"


@pytest.mark.asyncio
async def test_inspect_chat_access_normalizes_public_tme_url_to_username():
    client = _EntityClient()

    decision, reason, retry_after = await inspect_chat_access(
        client, "t.me/onlyadating/123"
    )

    assert decision == "ok"
    assert reason == "dry_run_resolved"
    assert retry_after == ""
    assert client.target == "@onlyadating"


@pytest.mark.asyncio
async def test_join_group_preserves_generic_error_details():
    result = await join_group(_BoomJoinClient(), "https://t.me/onlyadating")

    assert result.startswith("error:RuntimeError: boom with whitespace")


@pytest.mark.asyncio
async def test_ensure_chat_access_reports_generic_error_details():
    decision, reason, retry_after = await ensure_chat_access(
        _BoomJoinClient(), "https://t.me/onlyadating"
    )

    assert decision == "error"
    assert reason.startswith("error:RuntimeError: boom with whitespace")
    assert retry_after


@pytest.mark.asyncio
async def test_inspect_chat_access_reports_generic_error_details():
    decision, reason, retry_after = await inspect_chat_access(
        _BoomEntityClient(), "https://t.me/onlyadating"
    )

    assert decision == "error"
    assert reason.startswith("error:RuntimeError: resolve failed")
    assert retry_after
