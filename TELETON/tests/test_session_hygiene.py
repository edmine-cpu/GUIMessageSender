"""
Тесты функций управления сессиями аккаунта (account_manager).

Используют моки TelegramClient, потому что реальные вызовы требуют
живого подключения к Telegram. Проверяют:
  - list_sessions корректно возвращает auths / пустой список при ошибке
  - terminate_other_sessions пропускает current-сессию, убивает остальные
  - dry_run не вызывает ResetAuthorizationRequest
  - FloodWait обрабатывается корректно
  - FreshResetForbidden не валит процесс, а пропускает сессию
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.functions.account import (
    GetAuthorizationsRequest,
    ResetAuthorizationRequest,
)

from account_manager import list_sessions, terminate_other_sessions


def _make_auth(hash_val, current=False, device="Desktop", ip="1.2.3.4",
               country="RU", platform="Windows", system_version="10",
               app_name="Desktop", app_version="5.6.3", date_active="2026-04-24"):
    """Фейковый Authorization-объект с нужными полями."""
    return SimpleNamespace(
        hash=hash_val, current=current, device_model=device, ip=ip,
        country=country, platform=platform, system_version=system_version,
        app_name=app_name, app_version=app_version, date_active=date_active,
    )


class _FakeClient:
    """Клиент, который при вызове возвращает заранее подготовленные ответы
    или бросает заранее подготовленные исключения."""
    def __init__(self):
        self.calls = []  # история вызовов для проверки
        self.responses = {}  # тип_запроса → ответ или исключение

    async def __call__(self, request):
        self.calls.append(request)
        key = type(request).__name__
        # Если для этого типа запроса есть список ответов — берём по порядку
        if key in self.responses:
            responses = self.responses[key]
            if not responses:
                raise RuntimeError(f"No more responses queued for {key}")
            resp = responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        raise RuntimeError(f"Unexpected request type: {key}")


class TestListSessions:
    @pytest.mark.asyncio
    async def test_returns_authorizations(self):
        client = _FakeClient()
        client.responses["GetAuthorizationsRequest"] = [
            SimpleNamespace(authorizations=[
                _make_auth("hash1", current=True),
                _make_auth("hash2", current=False),
            ])
        ]

        auths = await list_sessions(client, progress_cb=lambda s: None)

        assert len(auths) == 2
        assert auths[0].current is True
        assert auths[1].hash == "hash2"

    @pytest.mark.asyncio
    async def test_returns_empty_on_flood_wait(self):
        client = _FakeClient()
        client.responses["GetAuthorizationsRequest"] = [
            FloodWaitError(request=None, capture=0)
        ]

        auths = await list_sessions(client, progress_cb=lambda s: None)
        assert auths == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_generic_error(self):
        client = _FakeClient()
        client.responses["GetAuthorizationsRequest"] = [
            RuntimeError("connection lost")
        ]

        auths = await list_sessions(client, progress_cb=lambda s: None)
        assert auths == []

    @pytest.mark.asyncio
    async def test_uses_progress_cb(self):
        client = _FakeClient()
        client.responses["GetAuthorizationsRequest"] = [
            SimpleNamespace(authorizations=[_make_auth("h1", current=True)])
        ]
        messages = []
        await list_sessions(client, progress_cb=messages.append)

        # Должно быть хотя бы одно сообщение с количеством сессий
        assert any("Активных сессий" in m for m in messages)


class TestTerminateOtherSessions:
    @pytest.mark.asyncio
    async def test_skips_current_session(self):
        """Сессия с current=True никогда не должна попадать в ResetAuthorizationRequest."""
        client = _FakeClient()
        client.responses["GetAuthorizationsRequest"] = [
            SimpleNamespace(authorizations=[
                _make_auth("h_current", current=True),
                _make_auth("h_other", current=False),
            ])
        ]
        client.responses["ResetAuthorizationRequest"] = [None]  # одна ожидается

        killed = await terminate_other_sessions(client, progress_cb=lambda s: None)

        assert killed == 1
        # Проверяем, что в истории вызовов Reset был только для h_other
        reset_calls = [c for c in client.calls
                       if isinstance(c, ResetAuthorizationRequest)]
        assert len(reset_calls) == 1
        assert reset_calls[0].hash == "h_other"

    @pytest.mark.asyncio
    async def test_returns_zero_when_only_current(self):
        client = _FakeClient()
        client.responses["GetAuthorizationsRequest"] = [
            SimpleNamespace(authorizations=[_make_auth("h1", current=True)])
        ]

        killed = await terminate_other_sessions(client, progress_cb=lambda s: None)
        assert killed == 0
        # Reset вообще не должен вызываться
        reset_calls = [c for c in client.calls
                       if isinstance(c, ResetAuthorizationRequest)]
        assert reset_calls == []

    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_reset(self, monkeypatch):
        # Обнуляем sleep, чтобы тесты шли быстро
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        client = _FakeClient()
        client.responses["GetAuthorizationsRequest"] = [
            SimpleNamespace(authorizations=[
                _make_auth("h1", current=True),
                _make_auth("h2", current=False),
                _make_auth("h3", current=False),
            ])
        ]

        killed = await terminate_other_sessions(
            client, progress_cb=lambda s: None, dry_run=True)

        assert killed == 0
        reset_calls = [c for c in client.calls
                       if isinstance(c, ResetAuthorizationRequest)]
        assert reset_calls == []

    @pytest.mark.asyncio
    async def test_fresh_reset_forbidden_skipped(self, monkeypatch):
        """Если одна сессия не убивается (<24ч), остальные всё равно убиваем."""
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        client = _FakeClient()
        client.responses["GetAuthorizationsRequest"] = [
            SimpleNamespace(authorizations=[
                _make_auth("h_current", current=True),
                _make_auth("h_fresh", current=False),
                _make_auth("h_old", current=False),
            ])
        ]
        # Первый Reset — кидает исключение, второй — проходит
        client.responses["ResetAuthorizationRequest"] = [
            RuntimeError("FreshResetAuthorisationForbidden"),
            None,
        ]

        killed = await terminate_other_sessions(client, progress_cb=lambda s: None)
        # h_fresh не убит, h_old убит → итого 1
        assert killed == 1

    @pytest.mark.asyncio
    async def test_flood_wait_breaks_loop(self, monkeypatch):
        """FloodWait прерывает зачистку, не продолжая убивать остальных."""
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        client = _FakeClient()
        client.responses["GetAuthorizationsRequest"] = [
            SimpleNamespace(authorizations=[
                _make_auth("h_current", current=True),
                _make_auth("h1", current=False),
                _make_auth("h2", current=False),
                _make_auth("h3", current=False),
            ])
        ]
        client.responses["ResetAuthorizationRequest"] = [
            None,  # h1 убит
            FloodWaitError(request=None, capture=30),  # h2 → flood
            # h3 уже не должен вызываться — break
        ]

        killed = await terminate_other_sessions(client, progress_cb=lambda s: None)
        assert killed == 1
        reset_calls = [c for c in client.calls
                       if isinstance(c, ResetAuthorizationRequest)]
        assert len(reset_calls) == 2  # не 3 — сработал break

    @pytest.mark.asyncio
    async def test_authorization_call_fails_gracefully(self):
        """Если GetAuthorizations не сработал — возвращаем 0, не падаем."""
        client = _FakeClient()
        client.responses["GetAuthorizationsRequest"] = [
            RuntimeError("connection refused")
        ]

        killed = await terminate_other_sessions(client, progress_cb=lambda s: None)
        assert killed == 0
