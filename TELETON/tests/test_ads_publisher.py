from types import SimpleNamespace

import pytest

from ads_models import GroupTarget, PUB_STATUS_ERROR, PUB_STATUS_OK
from ads_publisher import normalize_button_url, publish_to_group


class _SendMessageClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def send_message(self, target, text):
        self.calls.append((target, text))
        return self.result


class _SendMessageWithButtonsClient:
    def __init__(self, result, bot=True):
        self.result = result
        self.bot = bot
        self.calls = []

    async def get_me(self):
        return SimpleNamespace(bot=self.bot)

    async def send_message(self, target, text, **kwargs):
        self.calls.append((target, text, kwargs))
        return self.result


class _SendFileWithButtonsClient:
    def __init__(self, result, bot=True):
        self.result = result
        self.bot = bot
        self.calls = []

    async def get_me(self):
        return SimpleNamespace(bot=self.bot)

    async def send_file(self, target, media_path, **kwargs):
        self.calls.append((target, media_path, kwargs))
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
async def test_publish_to_group_passes_url_button_to_send_message():
    client = _SendMessageWithButtonsClient(SimpleNamespace(id=123))
    group = GroupTarget(link="@desk")

    result = await publish_to_group(
        client,
        group,
        "hello",
        button_text="Написать",
        button_url="@contact",
    )

    assert result.status == PUB_STATUS_OK
    assert len(client.calls) == 1
    target, text, kwargs = client.calls[0]
    assert target == "@desk"
    assert text == "hello"
    assert kwargs["buttons"] is not None


@pytest.mark.asyncio
async def test_publish_to_group_falls_back_to_link_text_for_user_account():
    client = _SendMessageWithButtonsClient(SimpleNamespace(id=123), bot=False)
    group = GroupTarget(link="@desk")

    result = await publish_to_group(
        client,
        group,
        "hello",
        button_text="Написать",
        button_url="@contact",
    )

    assert result.status == PUB_STATUS_OK
    assert len(client.calls) == 1
    target, text, kwargs = client.calls[0]
    assert target == "@desk"
    assert text == "hello\n\nНаписать: https://t.me/contact"
    assert kwargs == {}


@pytest.mark.asyncio
async def test_publish_to_group_passes_url_button_to_send_file(tmp_path):
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"fake image")
    client = _SendFileWithButtonsClient(SimpleNamespace(id=123))
    group = GroupTarget(link="@desk")

    result = await publish_to_group(
        client,
        group,
        "hello",
        media_path=str(media),
        button_text="Открыть чат",
        button_url="t.me/contact",
    )

    assert result.status == PUB_STATUS_OK
    assert len(client.calls) == 1
    target, media_path, kwargs = client.calls[0]
    assert target == "@desk"
    assert media_path == str(media)
    assert kwargs["caption"] == "hello"
    assert kwargs["buttons"] is not None


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


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("@contact", "https://t.me/contact"),
        ("t.me/contact", "https://t.me/contact"),
        ("telegram.me/contact", "https://t.me/contact"),
        ("https://example.com/x", "https://example.com/x"),
        ("http://example.com/x", "http://example.com/x"),
        ("tg://resolve?domain=contact", "tg://resolve?domain=contact"),
    ],
)
def test_normalize_button_url(raw_url, expected):
    assert normalize_button_url(raw_url) == expected
