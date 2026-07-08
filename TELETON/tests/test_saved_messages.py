from types import SimpleNamespace

import pytest

from sender import TelegramSender


class FakeSavedMessage:
    def __init__(self, raw_text=None, message=None, text=None):
        self.raw_text = raw_text
        self.message = message
        self.text = text


class FakeSavedClient:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def get_messages(self, chat, limit=30):
        self.calls.append((chat, limit))
        return self.messages[:limit]


def make_sender(messages):
    sender = object.__new__(TelegramSender)
    sender.client = FakeSavedClient(messages)
    sender.account = SimpleNamespace(phone="+100")
    return sender


@pytest.mark.asyncio
async def test_get_saved_messages_skips_dot_and_uses_raw_text_before_markdown_text():
    sender = make_sender([
        FakeSavedMessage(raw_text=".", text="."),
        FakeSavedMessage(raw_text="Real ad\nLine", text="**Real ad**\nLine"),
        FakeSavedMessage(raw_text="", message="Fallback text", text="**Fallback text**"),
    ])

    texts = await sender.get_saved_messages(limit=30)

    assert texts == ["Real ad\nLine", "Fallback text"]
    assert sender.client.calls == [("me", 30)]


@pytest.mark.asyncio
async def test_get_saved_message_returns_first_real_template_after_placeholder():
    sender = make_sender([
        FakeSavedMessage(raw_text="."),
        FakeSavedMessage(raw_text="Main template"),
    ])

    assert await sender.get_saved_message() == "Main template"


def test_saved_message_template_filter_rejects_punctuation_only_text():
    assert not TelegramSender._is_saved_message_template_text(".")
    assert not TelegramSender._is_saved_message_template_text("...")
    assert not TelegramSender._is_saved_message_template_text("!")
    assert not TelegramSender._is_saved_message_template_text("a")
    assert TelegramSender._is_saved_message_template_text("OK")
    assert TelegramSender._is_saved_message_template_text("https://t.me/example")
    assert TelegramSender._is_saved_message_template_text("Price 90000")
