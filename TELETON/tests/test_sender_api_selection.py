"""
Тесты выбора api_id/api_hash в TelegramSender._create_client.

Стратегия: мокаем TelegramClient, чтобы не делать реальные подключения,
и проверяем что конструктор вызван с правильными параметрами из Account.
"""
import os
import sys
from unittest.mock import patch, MagicMock
import tempfile

import pytest

from models import Account, ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_NEEDS_REAUTH
from database import Database


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    database = Database(path)
    yield database
    database.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


class TestCreateClientApiSelection:
    """_create_client должен выбирать api_id/api_hash по приоритету."""

    def test_uses_account_api_when_set(self, db):
        """Если у аккаунта api_id заполнен — используется он, не OWN_API_ID."""
        from sender import TelegramSender
        from config import Config

        acc = Account(
            phone="+79001234567",
            api_id=2040,
            api_hash="desktop_hash",
            device_model="Desktop",
            system_version="Windows 10",
            app_version="5.6.3 x64",
            lang_code="ru",
        )

        with patch("sender.TelegramClient") as MockClient:
            MockClient.return_value = MagicMock()
            sender = TelegramSender(acc, Config(), db)

            # Первый вызов — конструктор клиента
            args, kwargs = MockClient.call_args
            assert args[1] == 2040  # api_id
            assert args[2] == "desktop_hash"  # api_hash
            assert kwargs["device_model"] == "Desktop"
            assert kwargs["app_version"] == "5.6.3 x64"

    def test_falls_back_to_own_api_when_account_empty(self, db):
        """Если у аккаунта api_id=0 — используется OWN_API_ID из config."""
        from sender import TelegramSender
        from config import Config

        acc = Account(
            phone="+79001234567",
            api_id=0,          # пусто в БД
            api_hash="",
        )

        with patch("sender.TelegramClient") as MockClient, \
             patch("sender.OWN_API_ID", 12345678), \
             patch("sender.OWN_API_HASH", "own_hash"):
            MockClient.return_value = MagicMock()
            sender = TelegramSender(acc, Config(), db)

            args, _ = MockClient.call_args
            assert args[1] == 12345678
            assert args[2] == "own_hash"

    def test_raises_when_both_empty(self, db):
        """Нет api_id ни у аккаунта, ни в .env — явная ошибка с понятным сообщением."""
        from sender import TelegramSender
        from config import Config

        acc = Account(phone="+79001234567", api_id=0, api_hash="")

        with patch("sender.OWN_API_ID", 0), \
             patch("sender.OWN_API_HASH", ""):
            with pytest.raises(ValueError, match="api_id/api_hash"):
                TelegramSender(acc, Config(), db)

    def test_default_device_when_account_empty(self, db):
        """Пустые device-поля в аккаунте → дефолты PC 64bit/Windows 10."""
        from sender import TelegramSender
        from config import Config

        acc = Account(
            phone="+79001234567",
            api_id=123,
            api_hash="h",
            device_model="",
            system_version="",
        )

        with patch("sender.TelegramClient") as MockClient:
            MockClient.return_value = MagicMock()
            TelegramSender(acc, Config(), db)

            _, kwargs = MockClient.call_args
            assert kwargs["device_model"] == "PC 64bit"
            assert kwargs["system_version"] == "Windows 10"


class TestConnectAuthKeyErrors:
    @pytest.mark.asyncio
    async def test_auth_key_duplicated_marks_needs_reauth(self, db):
        from sender import TelegramSender
        from config import Config

        acc = Account(phone="+79001234567", api_id=123, api_hash="h")
        db.add_account(acc)

        with patch("sender.TelegramClient") as MockClient:
            MockClient.return_value = MagicMock()
            sender = TelegramSender(acc, Config(), db)

            async def fake_raw_connect():
                return "auth_key_duplicated"

            sender._raw_connect_with_retry = fake_raw_connect
            connected = await sender.connect()

        restored = db.get_all_accounts()[0]
        assert connected is False
        assert restored.status == ACCOUNT_STATUS_NEEDS_REAUTH
        assert "AuthKeyDuplicatedError" in restored.last_status_change

    @pytest.mark.asyncio
    async def test_database_locked_does_not_mark_needs_reauth_or_increment_failures(self, db):
        from sender import TelegramSender
        from config import Config

        acc = Account(phone="+79001234568", api_id=123, api_hash="h")
        db.add_account(acc)

        with patch("sender.TelegramClient") as MockClient:
            MockClient.return_value = MagicMock()
            sender = TelegramSender(acc, Config(), db)

            async def fake_raw_connect():
                return "database_locked"

            sender._raw_connect_with_retry = fake_raw_connect
            connected = await sender.connect()

        restored = next(a for a in db.get_all_accounts() if a.phone == acc.phone)
        assert connected is False
        assert restored.status == ACCOUNT_STATUS_ACTIVE
        assert restored.connect_fail_count == 0
        assert "database_locked" in restored.last_error_text
        assert sender.last_connect_error_code == "database_locked"
