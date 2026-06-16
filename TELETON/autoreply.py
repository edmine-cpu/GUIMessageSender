"""
autoreply.py — автоответчик на входящие личные сообщения.

Запускается в фоновом потоке, слушает NewMessage-события,
отвечает по шаблону и логирует кому ответил.
"""

import asyncio
from typing import Callable, Optional

from telethon import TelegramClient, events
from telethon.tl.types import User

from database import Database


REPLY_MODE_SESSION = "session"
REPLY_MODE_FOREVER = "forever"
REPLY_MODE_EVERY_MESSAGE = "every_message"

REPLY_MODE_LABELS = {
    REPLY_MODE_SESSION: "1 раз до остановки",
    REPLY_MODE_FOREVER: "1 раз навсегда",
    REPLY_MODE_EVERY_MESSAGE: "Каждое сообщение",
}


def normalize_reply_mode(reply_mode: str) -> str:
    mode = (reply_mode or REPLY_MODE_SESSION).strip()
    if mode in REPLY_MODE_LABELS:
        return mode
    return REPLY_MODE_SESSION


def reply_mode_label(reply_mode: str) -> str:
    return REPLY_MODE_LABELS.get(normalize_reply_mode(reply_mode), REPLY_MODE_LABELS[REPLY_MODE_SESSION])


class AutoReplyListener:
    """
    Слушатель входящих личных сообщений с автоответом.

    Использование:
        listener = AutoReplyListener(client, template, progress_cb)
        await listener.start()   # блокирует до вызова stop()
        await listener.stop()
    """

    def __init__(
        self,
        client: TelegramClient,
        template: str,
        progress_cb: Callable[[str], None] = print,
        reply_mode: str = "session",
        db: Optional[Database] = None,
        account_phone: str = "",
        include_keywords: str = "",
        exclude_keywords: str = "",
    ):
        """
        client      — подключённый TelegramClient
        template    — шаблон ответного сообщения
        progress_cb — коллбэк для логирования в GUI
        reply_mode  — "session", "forever" или "every_message"
        """
        self.client = client
        self.template = template
        self.progress_cb = progress_cb
        self.reply_mode = normalize_reply_mode(reply_mode)
        self.db = db
        self.account_phone = (account_phone or "").strip()
        self.include_keywords = include_keywords or ""
        self.exclude_keywords = exclude_keywords or ""
        self._replied: set = set()   # user_id кому уже ответили
        self._stop_event = asyncio.Event()

    async def start(self):
        """Запустить listener. Блокирует до вызова stop()."""
        me = await self.client.get_me()
        self.progress_cb(f"  [+] Автоответчик запущен: {me.phone}")
        self.progress_cb(f"  [~] Шаблон: {self.template[:80]}")
        if self.reply_mode == REPLY_MODE_FOREVER:
            self.progress_cb("  [i] Режим: отвечать один раз НАВСЕГДА (с сохранением в БД)")
        elif self.reply_mode == REPLY_MODE_EVERY_MESSAGE:
            self.progress_cb("  [i] Режим: отвечать на КАЖДОЕ входящее личное сообщение")
        else:
            self.progress_cb("  [i] Режим: отвечать один раз ДО ОСТАНОВКИ")

        @self.client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def handler(event):
            sender: Optional[User] = await event.get_sender()
            if sender is None:
                return
            if getattr(sender, "bot", False):
                self._log_event(sender, event, status="skip", reason="bot", reply_text="")
                return
            if sender.id == me.id:
                self._log_event(sender, event, status="skip", reason="self", reply_text="")
                return

            incoming_text = (getattr(event.message, "message", None) or getattr(event.message, "text", None) or "").strip()
            if not incoming_text:
                self.progress_cb(f"  [~] Пропуск {sender.id}: пустое сообщение")
                self._log_event(sender, event, status="skip", reason="empty_text", reply_text="")
                return

            if self._is_filtered_out(incoming_text):
                name = getattr(sender, "username", None) or getattr(sender, "first_name", str(sender.id))
                self.progress_cb(f"  [~] Пропуск @{name} (id:{sender.id}): не прошёл фильтры")
                self._log_event(sender, event, status="skip", reason="filtered", reply_text="")
                return

            if self.reply_mode == REPLY_MODE_FOREVER:
                if self.db and self.account_phone and self.db.has_autoreplied_forever(self.account_phone, sender.id):
                    name = getattr(sender, "username", None) or getattr(sender, "first_name", str(sender.id))
                    self.progress_cb(f"  [~] Пропуск @{name} (id:{sender.id}): уже отвечали (навсегда)")
                    self._log_event(sender, event, status="skip", reason="already_replied_forever", reply_text="")
                    return
            elif self.reply_mode == REPLY_MODE_SESSION:
                if sender.id in self._replied:
                    name = getattr(sender, "username", None) or getattr(sender, "first_name", str(sender.id))
                    self.progress_cb(f"  [~] Пропуск @{name} (id:{sender.id}): уже отвечали до остановки")
                    self._log_event(sender, event, status="skip", reason="already_replied_session", reply_text="")
                    return

            try:
                if self.db and self.account_phone:
                    ok, reason, wait_s = self.db.try_acquire_action_slot(
                        self.account_phone, "autoreply", min_interval_seconds=2.0, daily_actions_limit=200
                    )
                    if not ok:
                        if reason == "min_interval" and wait_s > 0:
                            await asyncio.sleep(min(wait_s, 5.0))
                        else:
                            name = getattr(sender, "username", None) or getattr(sender, "first_name", str(sender.id))
                            self.progress_cb(f"  [~] Пропуск @{name} (id:{sender.id}): лимитер блокирует ({reason})")
                            self._log_event(sender, event, status="skip", reason=f"limiter:{reason}", reply_text="")
                            return
                await event.reply(self.template)
                self._replied.add(sender.id)
                if self.reply_mode == REPLY_MODE_FOREVER and self.db and self.account_phone:
                    self.db.mark_autoreplied_forever(self.account_phone, sender.id)
                name = getattr(sender, "username", None) or getattr(sender, "first_name", str(sender.id))
                self.progress_cb(f"  [+] Ответил @{name} (id:{sender.id})")
                self._log_event(sender, event, status="sent", reason="ok", reply_text=self.template)
            except Exception as e:
                self.progress_cb(f"  [-] Ошибка ответа {sender.id}: {e}")
                self._log_event(sender, event, status="error", reason=f"{type(e).__name__}: {e}", reply_text=self.template)

        # Ждём сигнала остановки
        await self._stop_event.wait()

        # Снять обработчик
        self.client.remove_event_handler(handler)
        self.progress_cb("  [=] Автоответчик остановлен")

    def stop(self):
        """Сигнал остановки (можно вызвать из другого потока через loop)."""
        self._stop_event.set()

    def _split_keywords(self, s: str) -> list[str]:
        items = []
        for part in (s or "").split(","):
            p = part.strip().lower()
            if p:
                items.append(p)
        return items

    def _is_filtered_out(self, text: str) -> bool:
        text_l = (text or "").lower()
        inc = self._split_keywords(self.include_keywords)
        exc = self._split_keywords(self.exclude_keywords)
        if inc and not any(k in text_l for k in inc):
            return True
        if exc and any(k in text_l for k in exc):
            return True
        return False

    def _log_event(self, sender: User, event, status: str, reason: str, reply_text: str):
        if not self.db or not self.account_phone:
            return
        try:
            incoming_text = (getattr(event.message, "message", None) or getattr(event.message, "text", None) or "").strip()
            peer_username = str(getattr(sender, "username", "") or "")
            peer_name = str(getattr(sender, "first_name", "") or "")
            if getattr(sender, "last_name", None):
                peer_name = (peer_name + " " + str(sender.last_name)).strip()
            self.db.log_autoreply_event(
                account_phone=self.account_phone,
                peer_id=int(getattr(sender, "id", 0) or 0),
                peer_username=peer_username,
                peer_name=peer_name,
                incoming_text=incoming_text,
                reply_text=reply_text or "",
                status=status,
                reason=reason or "",
            )
        except Exception:
            pass
