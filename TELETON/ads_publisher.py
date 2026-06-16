"""
ads_publisher.py — публикация объявления в одну группу через Telethon.

Принимает уже подключённый TelegramClient, GroupTarget, текст и путь
к медиафайлу. Возвращает PublicationResult с PUB_STATUS_* и данными
для обновления groups_targets.retry_after.

Обрабатывает все типы ошибок Telegram:
  - SlowModeWaitError   → retry_after = now + seconds
  - FloodWaitError      → retry_after = now + seconds (личный флуд)
  - ChatWriteForbiddenError / ChatGuestSendForbiddenError →
      пытаемся распарсить "until <datetime>" из текста ошибки,
      если не получилось — retry_after = now + 24h
  - UserBannedInChannelError → status banned, retry_after далеко
  - ChannelPrivateError / ChatForbiddenError → unavailable
  - ChatAdminRequiredError / ChatRestrictedError → forbidden
  - Exception           → error, retry_after = now + 1h

Не использует Database напрямую — возвращает PublicationResult,
вызывающий код сам пишет в лог и обновляет group.
"""

import os
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ads_models import (
    GroupTarget, PublicationLog,
    PUB_STATUS_OK, PUB_STATUS_FLOOD_WAIT, PUB_STATUS_SLOW_MODE,
    PUB_STATUS_FORBIDDEN, PUB_STATUS_BANNED, PUB_STATUS_ERROR,
    GROUP_STATUS_BANNED, GROUP_STATUS_UNAVAILABLE,
)


@dataclass
class PublicationResult:
    """Результат одной попытки публикации."""
    status: str                        # PUB_STATUS_*
    message_id: Optional[int] = None  # Telegram message id если успешно
    error_text: str = ""
    retry_after: str = ""             # ISO datetime когда снова можно
    new_group_status: str = ""        # если нужно обновить groups_targets.status


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now()


def _retry_after_seconds(seconds: int) -> str:
    return _iso(_now() + timedelta(seconds=seconds))


def _retry_after_hours(hours: float) -> str:
    return _iso(_now() + timedelta(hours=hours))


def _parse_until_datetime(error_text: str) -> Optional[str]:
    """
    Пытается извлечь datetime из строки ошибки Telegram.

    Поддерживает форматы:
      'until 23.04.2026, 21:53'    (английский)
      'до 23.04.2026, 21:53'       (русский Telegram Desktop)
      'until 2026-04-23 21:53'     (ISO-подобный)
      dd.mm.yyyy, HH:MM            (просто паттерн без предлога)
    Возвращает ISO-строку или None.
    """
    # Формат dd.mm.yyyy, HH:MM — с любым предлогом или без
    # Ищем сам паттерн даты без привязки к конкретному слову
    m = re.search(r'(\d{2})\.(\d{2})\.(\d{4}),?\s+(\d{2}):(\d{2})',
                  error_text)
    if m:
        try:
            dt = datetime(
                int(m.group(3)), int(m.group(2)), int(m.group(1)),
                int(m.group(4)), int(m.group(5))
            )
            # Дата должна быть в будущем (если в прошлом — ограничение уже снято)
            if dt > datetime.now():
                return _iso(dt)
        except ValueError:
            pass

    # Формат yyyy-mm-dd HH:MM — с "until" или без
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})',
                  error_text)
    if m:
        try:
            dt = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5))
            )
            if dt > datetime.now():
                return _iso(dt)
        except ValueError:
            pass

    return None


def _message_id_or_none(msg) -> Optional[int]:
    """Telethon should return a Message, but defensive handling keeps scheduler alive."""
    try:
        message_id = getattr(msg, "id", None)
        if message_id is None:
            return None
        return int(message_id)
    except Exception:
        return None


def normalize_button_url(raw_url: str) -> str:
    """Normalize user-entered ad button target into a Telegram URL button URL."""
    url = (raw_url or "").strip()
    if not url:
        return ""
    if any(ch.isspace() for ch in url):
        raise ValueError("button URL must not contain spaces")
    lowered = url.lower()
    if url.startswith("@") and len(url) > 1:
        return f"https://t.me/{url[1:]}"
    if lowered.startswith("t.me/"):
        return f"https://{url}"
    if lowered.startswith("telegram.me/"):
        return f"https://t.me/{url[len('telegram.me/'):]}"
    if lowered.startswith(("http://", "https://", "tg://")):
        return url
    raise ValueError("button URL must be @username, t.me/..., http(s)://..., or tg://...")


def _build_url_button(button_text: str, button_url: str):
    text = (button_text or "").strip()
    url = (button_url or "").strip()
    if not text and not url:
        return None
    if not text or not url:
        raise ValueError("button text and URL must be filled together")

    from telethon import Button
    return Button.url(text, normalize_button_url(url))


async def _client_supports_buttons(client) -> bool:
    """Telegram only allows custom buttons from bot sessions."""
    try:
        get_me = getattr(client, "get_me", None)
        if get_me is None:
            return False
        me = await asyncio.wait_for(get_me(), timeout=10.0)
        return bool(getattr(me, "bot", False))
    except Exception:
        return False


def _append_button_link(text: str, button_text: str, button_url: str) -> str:
    url = normalize_button_url(button_url)
    label = (button_text or "").strip()
    if not label or not url:
        return text
    return f"{text}\n\n{label}: {url}"


async def publish_to_group(
    client,
    group: GroupTarget,
    text: str,
    media_path: str = "",
    account_phone: str = "",
    ad_id: int = 0,
    button_text: str = "",
    button_url: str = "",
) -> PublicationResult:
    """
    Опубликовать текст (+ медиа) в группу.

    Параметры:
        client        — подключённый TelegramClient
        group         — GroupTarget (нужен group.link для адресации)
        text          — текст публикации
        media_path    — путь к файлу (пусто = без медиа)
        account_phone — для лога
        ad_id         — для лога
        button_text   — текст inline-кнопки (опционально)
        button_url    — куда ведёт inline-кнопка (опционально)

    Возвращает PublicationResult.
    """
    # Lazy import — защита от мока telethon в тестах (test_mentioner.py)
    from telethon.errors import (
        SlowModeWaitError,
        FloodWaitError,
        ChatWriteForbiddenError,
        ChatGuestSendForbiddenError,
        UserBannedInChannelError,
        ChannelPrivateError,
        ChatForbiddenError,
        ChatAdminRequiredError,
        ChatRestrictedError,
        UsernameNotOccupiedError,
        ChannelInvalidError,
    )

    target = group.link

    try:
        try:
            button = _build_url_button(button_text, button_url)
        except ValueError as e:
            return PublicationResult(
                status=PUB_STATUS_ERROR,
                error_text=str(e),
                retry_after=_retry_after_hours(1),
            )
        use_inline_button = bool(button and await _client_supports_buttons(client))
        outbound_text = (
            text if use_inline_button or button is None
            else _append_button_link(text, button_text, button_url)
        )

        if media_path and os.path.exists(media_path):
            if use_inline_button:
                msg = await asyncio.wait_for(
                    client.send_file(target, media_path, caption=outbound_text, buttons=button),
                    timeout=30.0,
                )
            else:
                msg = await asyncio.wait_for(
                    client.send_file(target, media_path, caption=outbound_text),
                    timeout=30.0,
                )
        else:
            if use_inline_button:
                msg = await asyncio.wait_for(
                    client.send_message(target, outbound_text, buttons=button),
                    timeout=30.0,
                )
            else:
                msg = await asyncio.wait_for(
                    client.send_message(target, outbound_text),
                    timeout=30.0,
                )

        message_id = _message_id_or_none(msg)
        if message_id is None:
            return PublicationResult(
                status=PUB_STATUS_ERROR,
                error_text="Telegram returned no message id after send",
                retry_after=_retry_after_hours(1),
            )

        return PublicationResult(
            status=PUB_STATUS_OK,
            message_id=message_id,
        )

    except SlowModeWaitError as e:
        wait = max(e.seconds, 1)
        return PublicationResult(
            status=PUB_STATUS_SLOW_MODE,
            error_text=f"SlowModeWait {wait}s",
            retry_after=_retry_after_seconds(wait),
        )

    except FloodWaitError as e:
        wait = max(e.seconds, 1)
        return PublicationResult(
            status=PUB_STATUS_FLOOD_WAIT,
            error_text=f"FloodWait {wait}s",
            retry_after=_retry_after_seconds(wait),
        )

    except ChatGuestSendForbiddenError as e:
        err_str = f"NeedSubscription: {str(e)}"
        retry_after = _parse_until_datetime(err_str)
        if not retry_after:
            retry_after = _retry_after_hours(24)
        return PublicationResult(
            status=PUB_STATUS_FORBIDDEN,
            error_text=err_str[:300],
            retry_after=retry_after,
        )

    except ChatWriteForbiddenError as e:
        err_str = str(e)
        retry_after = _parse_until_datetime(err_str)
        if not retry_after:
            retry_after = _retry_after_hours(24)
        return PublicationResult(
            status=PUB_STATUS_FORBIDDEN,
            error_text=err_str[:300],
            retry_after=retry_after,
        )

    except (ChatAdminRequiredError, ChatRestrictedError) as e:
        return PublicationResult(
            status=PUB_STATUS_FORBIDDEN,
            error_text=str(e)[:300],
            retry_after=_retry_after_hours(24 * 7),
        )

    except UserBannedInChannelError as e:
        return PublicationResult(
            status=PUB_STATUS_BANNED,
            error_text=str(e)[:300],
            retry_after=_retry_after_hours(24 * 30),
            new_group_status=GROUP_STATUS_BANNED,
        )

    except (ChannelPrivateError, ChatForbiddenError,
            UsernameNotOccupiedError, ChannelInvalidError) as e:
        return PublicationResult(
            status=PUB_STATUS_ERROR,
            error_text=str(e)[:300],
            retry_after=_retry_after_hours(24 * 7),
            new_group_status=GROUP_STATUS_UNAVAILABLE,
        )

    except Exception as e:
        return PublicationResult(
            status=PUB_STATUS_ERROR,
            error_text=str(e)[:300],
            retry_after=_retry_after_hours(1),
        )


def build_publication_log(
    result: PublicationResult,
    ad_id: int,
    group_id: int,
    account_phone: str,
) -> PublicationLog:
    """Собрать PublicationLog из результата публикации."""
    return PublicationLog(
        ad_id=ad_id,
        group_id=group_id,
        account_phone=account_phone,
        time=_iso(_now()),
        status=result.status,
        error_text=result.error_text,
        message_id=result.message_id,
    )
