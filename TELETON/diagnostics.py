"""Human-readable diagnostics for GUI logs and panels."""

from __future__ import annotations

import re
from typing import Optional


def _clean(value: object, limit: int = 180) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _format_wait(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    try:
        total = max(0, int(float(seconds)))
    except Exception:
        return ""
    if total <= 0:
        return ""
    if total < 60:
        return f"{total} сек"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    rest = minutes % 60
    if rest:
        return f"{hours} ч {rest} мин"
    return f"{hours} ч"


def _extract_wait_seconds(detail: str) -> Optional[int]:
    text = detail or ""
    patterns = (
        r"FloodWait(?:Error)?\s*\(?\s*(\d+)\s*s?",
        r"SlowModeWait(?:Error)?\s*\(?\s*(\d+)\s*s?",
        r"(\d+)\s*seconds?",
        r"(\d+)\s*сек",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                return None
    return None


def human_reason(code: str, detail: str = "", wait_seconds: int | None = None) -> str:
    """Return a short plain-language Russian reason for a status/error code."""
    raw_code = _clean(code, 80)
    raw_detail = _clean(detail, 180)
    normalized = raw_code.lower().strip()
    detail_l = raw_detail.lower()
    wait = wait_seconds if wait_seconds is not None else _extract_wait_seconds(raw_detail)

    if ":" in normalized:
        prefix, rest = normalized.split(":", 1)
        if prefix in ("join", "error", "slow_mode", "flood_wait"):
            normalized = prefix
            if not raw_detail:
                raw_detail = rest

    if "proxy" in normalized or "proxy" in detail_l:
        base = "ошибка прокси или сети"
    elif normalized in ("flood_wait", "floodwait") or "floodwait" in detail_l:
        suffix = _format_wait(wait)
        base = f"Flood wait: аккаунт временно ограничен{f' на {suffix}' if suffix else ''}"
    elif normalized in ("slow_mode", "slowmode") or "slowmode" in detail_l or "slow mode" in detail_l:
        suffix = _format_wait(wait)
        base = f"Slow mode: в этом чате нужно подождать{f' {suffix}' if suffix else ''}"
    elif normalized in ("need_subscription", "not_member") or "needsubscription" in detail_l:
        base = "нужно вступить или подписаться перед отправкой"
    elif normalized in ("no_permission", "forbidden", "chat_banned") or "forbidden" in detail_l:
        base = "нет прав на отправку в этот чат"
    elif normalized in ("private", "unavailable", "invalid", "expired"):
        base = "нет доступа к чату или ссылка недоступна"
    elif normalized in ("needs_reauth", "auth", "auth_key_unregistered", "authkeyunregisterederror"):
        base = "нужна повторная авторизация аккаунта"
    elif normalized in ("banned", "peerflood", "userdeactivatedbanerror"):
        base = "аккаунт заблокирован или ограничен Telegram"
    elif normalized in ("network_issue", "connect_error", "timeout", "connectionerror"):
        base = "проблема сети или подключения к Telegram"
    elif normalized in ("session_locked", "database_locked", "in_app_session_busy"):
        base = "сессия или база занята другой операцией"
    elif normalized in ("daily_limit", "limit"):
        base = "достигнут дневной лимит действий"
    elif normalized in ("min_interval", "paused"):
        suffix = _format_wait(wait)
        base = f"нужно подождать между действиями{f' {suffix}' if suffix else ''}"
    elif normalized in ("inactive", "not_found"):
        base = "аккаунт выключен или не найден"
    elif normalized in ("empty_text", "empty text"):
        base = "пустой текст сообщения"
    elif normalized in ("db_error", "sqlite", "sqlite_error"):
        base = "ошибка базы данных"
    elif normalized in ("error", "unknown", ""):
        base = "ошибка выполнения действия"
    else:
        base = raw_code or "ошибка выполнения действия"

    technical = raw_detail
    if technical and technical.lower() not in base.lower():
        return f"{base} ({technical})"
    return base


def human_action_block_reason(reason: str, wait_seconds: float = 0) -> str:
    return human_reason(reason, wait_seconds=int(wait_seconds or 0) or None)


def human_exception(exc: BaseException) -> str:
    name = type(exc).__name__
    seconds = getattr(exc, "seconds", None)
    text = _clean(exc, 180)
    return human_reason(name, text, seconds)

