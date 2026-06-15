from types import SimpleNamespace

import pytest

from ads_models import GroupTarget, PUB_STATUS_ERROR, PUB_STATUS_OK
from ads_publisher import publish_to_group


class _SendMessageClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def send_message(self, target, text):
        self.calls.append((target, text))
        return self.result


@pytest.mark.asyncio
async def test_publish_to_group_returns_ok_with_message_id():
    client = _SendMessageClient(SimpleNamespace(id=123))
    group = GroupTarget(link="@desk")

    result = await publish_to_group(client, group, "hello")

    assert result.status == PUB_STATUS_OK
    assert result.message_id == 123
    assert client.calls == [("@desk", "hello")]


@pytest.mark.asyncio
@pytest.mark.parametrize("send_result", [None, SimpleNamespace()])
async def test_publish_to_group_handles_missing_message_id(send_result):
    client = _SendMessageClient(send_result)
    group = GroupTarget(link="@desk")

    result = await publish_to_group(client, group, "hello")

    assert result.status == PUB_STATUS_ERROR
    assert result.message_id is None
    assert "no message id" in result.error_text
    assert result.retry_after
