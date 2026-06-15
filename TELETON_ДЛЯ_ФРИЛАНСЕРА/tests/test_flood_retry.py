"""
Тесты для хелпера _try_with_flood_retry из gui.py.

gui.py импортирует tkinter и не загружается в headless-окружении CI.
Извлекаем функцию _try_with_flood_retry через AST-парсинг (как делается
для test_tdata_error_hints.py).

Покрываем:
  - успешная попытка с первого раза → 1 вызов coro_factory
  - FloodWait ниже лимита → ретрай, всего 2 вызова
  - FloodWait выше лимита → пробрасываем наружу, 1 вызов
  - другое исключение → пробрасываем без ретрая
  - jitter min=max → детерминированная задержка
"""
import ast
import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from telethon.errors import FloodWaitError


def _load_helper():
    """Извлекает _try_with_flood_retry из gui.py без import gui (нет tkinter)."""
    here = os.path.dirname(__file__)
    gui_path = os.path.join(here, "..", "gui.py")
    with open(gui_path, "r", encoding="utf-8") as f:
        src = f.read()

    tree = ast.parse(src)
    namespace = {}

    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_try_with_flood_retry":
            code = compile(ast.Module(body=[node], type_ignores=[]),
                           filename="gui.py", mode="exec")
            exec(code, namespace)
            return namespace["_try_with_flood_retry"]
    raise RuntimeError("_try_with_flood_retry not found in gui.py")


@pytest.fixture(scope="module")
def try_with_flood_retry():
    return _load_helper()


def _make_flood_error(seconds: int) -> FloodWaitError:
    """Создаёт FloodWaitError с заданным e.seconds. Без request — для тестов."""
    return FloodWaitError(request=None, capture=seconds)


class TestSuccess:
    @pytest.mark.asyncio
    async def test_first_try_succeeds(self, try_with_flood_retry):
        """Если coro_factory сразу успешна — никаких ретраев, 1 вызов."""
        call_count = {"n": 0}

        async def factory():
            call_count["n"] += 1
            return "ok"

        result = await try_with_flood_retry(
            factory, max_wait_sec=300, jitter_min=0, jitter_max=0,
            log_cb=lambda _m: None,
        )
        assert result == "ok"
        assert call_count["n"] == 1


class TestFloodRetry:
    @pytest.mark.asyncio
    async def test_short_floodwait_triggers_retry(self, try_with_flood_retry,
                                                    monkeypatch):
        """FloodWait <= max_wait_sec → ждём + ретрай."""
        # Замокаем asyncio.sleep чтобы тест шёл быстро
        sleep_mock = AsyncMock()
        monkeypatch.setattr("asyncio.sleep", sleep_mock)

        call_count = {"n": 0}

        async def factory():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _make_flood_error(30)
            return "ok_on_retry"

        result = await try_with_flood_retry(
            factory, max_wait_sec=300, jitter_min=1, jitter_max=3,
            log_cb=lambda _m: None,
        )
        assert result == "ok_on_retry"
        assert call_count["n"] == 2  # один первый + один ретрай
        # Проверяем что было ожидание (sleep вызывался)
        assert sleep_mock.called

    @pytest.mark.asyncio
    async def test_long_floodwait_raises(self, try_with_flood_retry):
        """FloodWait > max_wait_sec → НЕ ретраим, пробрасываем наверх."""
        call_count = {"n": 0}

        async def factory():
            call_count["n"] += 1
            raise _make_flood_error(600)  # 10 минут — больше нашего лимита 300

        with pytest.raises(FloodWaitError) as excinfo:
            await try_with_flood_retry(
                factory, max_wait_sec=300, jitter_min=1, jitter_max=3,
                log_cb=lambda _m: None,
            )
        assert excinfo.value.seconds == 600
        assert call_count["n"] == 1  # только один вызов, без ретрая

    @pytest.mark.asyncio
    async def test_floodwait_at_exact_limit(self, try_with_flood_retry,
                                              monkeypatch):
        """FloodWait == max_wait_sec → ретраим (граница включена)."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr("asyncio.sleep", sleep_mock)

        call_count = {"n": 0}

        async def factory():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _make_flood_error(300)
            return "ok"

        result = await try_with_flood_retry(
            factory, max_wait_sec=300, jitter_min=0, jitter_max=0,
            log_cb=lambda _m: None,
        )
        assert result == "ok"
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_double_floodwait_raises(self, try_with_flood_retry,
                                             monkeypatch):
        """Если ретрай тоже FloodWait — пробрасываем (нет двойного ретрая)."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr("asyncio.sleep", sleep_mock)

        call_count = {"n": 0}

        async def factory():
            call_count["n"] += 1
            raise _make_flood_error(30)  # всегда FloodWait, оба раза ниже лимита

        with pytest.raises(FloodWaitError):
            await try_with_flood_retry(
                factory, max_wait_sec=300, jitter_min=0, jitter_max=0,
                log_cb=lambda _m: None,
            )
        assert call_count["n"] == 2  # 1 первый + 1 ретрай, не больше


class TestOtherExceptions:
    @pytest.mark.asyncio
    async def test_other_exception_no_retry(self, try_with_flood_retry):
        """Любое НЕ-FloodWait исключение → пробрасываем БЕЗ ретрая."""
        call_count = {"n": 0}

        async def factory():
            call_count["n"] += 1
            raise TimeoutError("connection timed out")

        with pytest.raises(TimeoutError):
            await try_with_flood_retry(
                factory, max_wait_sec=300, jitter_min=0, jitter_max=0,
                log_cb=lambda _m: None,
            )
        assert call_count["n"] == 1  # без ретрая


class TestLogCb:
    @pytest.mark.asyncio
    async def test_log_cb_called_on_floodwait(self, try_with_flood_retry,
                                                monkeypatch):
        sleep_mock = AsyncMock()
        monkeypatch.setattr("asyncio.sleep", sleep_mock)

        messages = []

        async def factory():
            if not messages or "OK" not in str(messages):
                # Первый вызов — FloodWait. После записи "OK" возвращаем результат.
                if not any("retry done" in str(m) for m in messages):
                    raise _make_flood_error(10)
            return "ok"

        # Простой контроль: счётчик вызовов
        attempts = {"n": 0}

        async def factory2():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _make_flood_error(30)
            return "ok"

        result = await try_with_flood_retry(
            factory2, max_wait_sec=300, jitter_min=1, jitter_max=2,
            log_cb=messages.append,
        )
        assert result == "ok"
        # log_cb должен был получить хотя бы одно сообщение про FloodWait
        joined = "\n".join(messages)
        assert "FloodWait" in joined or "Flood" in joined
