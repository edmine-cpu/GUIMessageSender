from types import SimpleNamespace

import pytest
from telethon.errors import ChatForwardsRestrictedError
from telethon.tl.types import (
    KeyboardButtonRow,
    KeyboardButtonUrl,
    MessageEntityCustomEmoji,
    MessageEntityTextUrl,
    ReplyInlineMarkup,
)

import sender as sender_module
from models import Account
from sender import TelegramSender


class FakeSavedMessage:
    def __init__(self, raw_text=None, message=None, text=None, entities=None, media=None, reply_markup=None):
        self.id = None
        self.raw_text = raw_text
        self.message = message
        self.text = text
        self.entities = entities
        self.media = media
        self.reply_markup = reply_markup


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


@pytest.mark.asyncio
async def test_get_saved_message_templates_preserves_rich_payloads():
    entities = [SimpleNamespace(kind="text_url")]
    reply_markup = SimpleNamespace(buttons=["tap"])
    rich = FakeSavedMessage(raw_text="Tap here", text="**Tap here**", entities=entities, reply_markup=reply_markup)
    media_only = FakeSavedMessage(raw_text="", media=SimpleNamespace(kind="premium_sticker"))
    sender = make_sender([
        FakeSavedMessage(raw_text="."),
        rich,
        media_only,
    ])

    templates = await sender.get_saved_message_templates(limit=30)

    assert len(templates) == 2
    assert templates[0].message is rich
    assert templates[0].text == "Tap here"
    assert templates[0].entities == entities
    assert templates[0].reply_markup is reply_markup
    assert templates[0].is_rich
    assert templates[1].message is media_only
    assert templates[1].text == ""
    assert templates[1].media is media_only.media
    assert templates[1].is_usable


def test_saved_message_template_accepts_real_telegram_rich_types():
    entities = [
        MessageEntityTextUrl(offset=0, length=4, url="https://t.me/example"),
        MessageEntityCustomEmoji(offset=5, length=2, document_id=123456789),
    ]
    markup = ReplyInlineMarkup(rows=[
        KeyboardButtonRow(buttons=[
            KeyboardButtonUrl(text="Tap", url="https://t.me/example"),
        ]),
    ])
    original = FakeSavedMessage(raw_text="Tap 😀", entities=entities, reply_markup=markup)

    template = TelegramSender._build_saved_message_template(original)

    assert template is not None
    assert template.is_rich
    assert template.entities == entities
    assert template.reply_markup is markup


class FakeSendClient:
    def __init__(self, fail_forward=None):
        self.calls = []
        self.fail_forward = fail_forward

    async def send_message(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(id=123)

    async def forward_messages(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.fail_forward:
            raise self.fail_forward
        return SimpleNamespace(id=456)


class FakeDB:
    def __init__(self):
        self.actions = []

    def try_acquire_action_slot(self, *args, **kwargs):
        return True, "ok", 0.0

    def log_account_action(self, *args):
        self.actions.append(args)


@pytest.mark.asyncio
async def test_send_saved_message_uses_original_message_object(monkeypatch):
    fake_client = FakeSendClient()
    fake_db = FakeDB()
    sender = TelegramSender.__new__(TelegramSender)
    sender.account = Account(phone="+15550000001", session_name="session_test")
    sender.db = fake_db
    sender.client = fake_client
    sender.sent_count = 0
    events = []
    monkeypatch.setattr(sender_module, "log_event", lambda **kwargs: events.append(kwargs))
    original = FakeSavedMessage(
        raw_text="Tap here",
        entities=[SimpleNamespace(kind="custom_emoji")],
        reply_markup=SimpleNamespace(buttons=["tap"]),
    )
    template = TelegramSender._build_saved_message_template(original)

    result = await sender.send_saved_message("@target", template)

    assert result == "sent:123"
    assert fake_client.calls == [(("@target", original), {})]
    assert fake_db.actions == [("+15550000001", "group", "@target", "sent", "123")]
    assert events and events[0]["status"] == "sent"


@pytest.mark.asyncio
async def test_send_saved_message_forwards_rich_message_with_hidden_author(monkeypatch):
    fake_client = FakeSendClient()
    fake_db = FakeDB()
    sender = TelegramSender.__new__(TelegramSender)
    sender.account = Account(phone="+15550000001", session_name="session_test")
    sender.db = fake_db
    sender.client = fake_client
    sender.sent_count = 0
    events = []
    monkeypatch.setattr(sender_module, "log_event", lambda **kwargs: events.append(kwargs))
    original = FakeSavedMessage(
        raw_text="Tap here",
        entities=[SimpleNamespace(kind="custom_emoji")],
        reply_markup=SimpleNamespace(buttons=["tap"]),
    )
    original.id = 777
    template = TelegramSender._build_saved_message_template(original)

    result = await sender.send_saved_message("@target", template)

    assert result == "sent:456"
    assert fake_client.calls == [(("@target", original), {"drop_author": True})]
    assert fake_db.actions == [("+15550000001", "group", "@target", "sent", "456")]
    assert events and events[0]["status"] == "sent"


@pytest.mark.asyncio
async def test_send_saved_message_falls_back_to_copy_when_forward_restricted(monkeypatch):
    fake_client = FakeSendClient(fail_forward=ChatForwardsRestrictedError(request=None))
    fake_db = FakeDB()
    sender = TelegramSender.__new__(TelegramSender)
    sender.account = Account(phone="+15550000001", session_name="session_test")
    sender.db = fake_db
    sender.client = fake_client
    sender.sent_count = 0
    events = []
    monkeypatch.setattr(sender_module, "log_event", lambda **kwargs: events.append(kwargs))
    original = FakeSavedMessage(
        raw_text="Tap here",
        entities=[SimpleNamespace(kind="custom_emoji")],
        reply_markup=SimpleNamespace(buttons=["tap"]),
    )
    original.id = 777
    template = TelegramSender._build_saved_message_template(original)

    result = await sender.send_saved_message("@target", template)

    assert result == "sent:123"
    assert fake_client.calls == [
        (("@target", original), {"drop_author": True}),
        (("@target", original), {}),
    ]
    assert fake_db.actions == [("+15550000001", "group", "@target", "sent", "123")]
    assert events and events[0]["status"] == "sent"
