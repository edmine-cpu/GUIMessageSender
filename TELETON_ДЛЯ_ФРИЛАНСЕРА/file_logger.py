"""
file_logger.py — запись всех GUI-сообщений в файл для последующего анализа.

Логи пишутся в `data/logs/teleton_YYYY-MM-DD.log`. Каждая строка:
    2026-04-21 14:35:12 [accounts] [+] Аккаунт +79001234567 добавлен

- Ротация по дням (новый файл каждый день).
- Буферизация отключена — пишем сразу, чтобы не потерять при краше.
- Thread-safe через threading.Lock.
- Если запись в файл упала — молча игнорируем, чтобы не ломать GUI.

Использование:
    from file_logger import log_to_file
    log_to_file("accounts", "[+] Аккаунт добавлен")
"""

import os
import threading
from datetime import datetime


_LOG_DIR = "data/logs"
_lock = threading.Lock()
_current_file = None
_current_date = None
_file_handle = None
_event_handle = None


def _get_log_path() -> str:
    """Путь к лог-файлу на сегодня."""
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(_LOG_DIR, f"teleton_{today}.log")

def _get_events_path() -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(_LOG_DIR, f"teleton_events_{today}.log")


def _open_file_if_needed():
    """Открыть файл на сегодня. При смене даты перезакрыть на новый."""
    global _current_file, _current_date, _file_handle, _event_handle
    today = datetime.now().strftime("%Y-%m-%d")
    if _current_date != today:
        # Смена даты — закрываем старый
        if _file_handle is not None:
            try:
                _file_handle.close()
            except Exception:
                pass
            _file_handle = None
        if _event_handle is not None:
            try:
                _event_handle.close()
            except Exception:
                pass
            _event_handle = None
        os.makedirs(_LOG_DIR, exist_ok=True)
        _current_date = today
        _current_file = _get_log_path()
    if _file_handle is None:
        try:
            # line_buffering=True — каждая строка сразу на диск
            _file_handle = open(_current_file, "a", encoding="utf-8",
                                buffering=1)
        except Exception:
            _file_handle = None
    if _event_handle is None:
        try:
            _event_handle = open(_get_events_path(), "a", encoding="utf-8", buffering=1)
        except Exception:
            _event_handle = None


def log_to_file(tag: str, message: str):
    """Записать сообщение в лог-файл.

    tag     — секция GUI (accounts, parsing, broadcast, ...). Пусто допустимо.
    message — текст сообщения (уже со всеми [+]/[-]/[!!] префиксами).

    При ошибке записи не бросает исключение — лог не должен ломать приложение.
    """
    with _lock:
        try:
            _open_file_if_needed()
            if _file_handle is None:
                return
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tag_str = f"[{tag}]" if tag else ""
            line = f"{ts} {tag_str} {message}\n".lstrip()
            _file_handle.write(line)
        except Exception:
            pass  # лог не должен ломать GUI


def _safe_field(value: str, limit: int = 500) -> str:
    s = "" if value is None else str(value)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    s = s.replace("|", "/")
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def log_event(
    *,
    module: str,
    campaign: str = "",
    account: str = "",
    target: str = "",
    action: str = "",
    status: str = "",
    error: str = "",
):
    with _lock:
        try:
            _open_file_if_needed()
            if _event_handle is None:
                return
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = (
                f"{ts} | "
                f"{_safe_field(module, 80)} | "
                f"{_safe_field(campaign, 120)} | "
                f"{_safe_field(account, 40)} | "
                f"{_safe_field(target, 200)} | "
                f"{_safe_field(action, 80)} | "
                f"{_safe_field(status, 40)} | "
                f"{_safe_field(error, 500)}\n"
            )
            _event_handle.write(line)
        except Exception:
            pass


def log_exception(tag: str, exc: BaseException, context: str = ""):
    """Записать исключение с полным traceback.

    Используется когда в GUI сообщение об ошибке может быть искажено
    (например Python 3.13 + opentele ломает __str__ у exception).
    """
    import traceback
    with _lock:
        try:
            _open_file_if_needed()
            if _file_handle is None:
                return
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tag_str = f"[{tag}]" if tag else ""
            _file_handle.write(
                f"{ts} {tag_str} === EXCEPTION ===\n".lstrip())
            if context:
                _file_handle.write(f"{ts} {tag_str} Context: {context}\n".lstrip())
            _file_handle.write(
                f"{ts} {tag_str} Type: {type(exc).__name__}\n".lstrip())
            # str() может быть сломанным — пишем repr тоже
            try:
                _file_handle.write(
                    f"{ts} {tag_str} Str: {exc}\n".lstrip())
            except Exception:
                pass
            try:
                _file_handle.write(
                    f"{ts} {tag_str} Repr: {exc!r}\n".lstrip())
            except Exception:
                pass
            tb_str = traceback.format_exception(
                type(exc), exc, exc.__traceback__)
            for line in "".join(tb_str).splitlines():
                _file_handle.write(
                    f"{ts} {tag_str} {line}\n".lstrip())
            _file_handle.write(
                f"{ts} {tag_str} === /EXCEPTION ===\n".lstrip())
        except Exception:
            pass


def current_log_path() -> str:
    """Текущий путь к лог-файлу (для отображения пользователю)."""
    with _lock:
        _open_file_if_needed()
        return _current_file or _get_log_path()


def close():
    """Закрыть файл (вызывается при выходе). Не обязательно — ОС сама закроет."""
    global _file_handle, _event_handle
    with _lock:
        if _file_handle is not None:
            try:
                _file_handle.close()
            except Exception:
                pass
            _file_handle = None
        if _event_handle is not None:
            try:
                _event_handle.close()
            except Exception:
                pass
            _event_handle = None
