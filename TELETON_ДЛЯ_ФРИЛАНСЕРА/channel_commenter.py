"""
channel_commenter.py — комментирование постов в каналах.

Два режима:
  - old_posts: комментирование уже существующих постов (с настройкой глубины)
  - new_posts: real-time listener на новые посты
"""

import asyncio
import random
from typing import Callable, List, Optional, Dict, Any

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    MsgIdInvalidError,
    ChannelPrivateError,
)

from channel_ai import generate_ai_comment
from database import Database


def _safe_text(s: str) -> str:
    s = "" if s is None else str(s)
    try:
        return s.encode("utf-8", "replace").decode("utf-8")
    except Exception:
        return repr(s)


def _reason_comments_unavailable(msg) -> str:
    try:
        replies = getattr(msg, "replies", None)
        if replies is None:
            return "комментарии недоступны (нет discussion/комментов)"
        comments = getattr(replies, "comments", None)
        if comments is False:
            return "комментарии отключены"
        return "комментарии недоступны"
    except Exception:
        return "комментарии недоступны"


def _log_post(
    db: Optional[Database],
    channel: str,
    post_id: int,
    comment_id: int,
    account_phone: str,
    status: str,
    error_text: str,
):
    if not db:
        return
    try:
        db.log_channel_comment(
            channel=channel,
            post_id=post_id,
            comment_id=comment_id,
            account_phone=account_phone,
            status=status,
            error_text=error_text,
        )
    except Exception:
        pass


# --- Комментирование старых постов ---

async def comment_old_posts(
    client: TelegramClient,
    channels: List[str],
    messages: List[str],
    limit_posts: int = 10,
    delay_min: float = 10.0,
    delay_max: float = 25.0,
    delay_between_channels: float = 5.0,
    progress_cb: Callable[[str], None] = print,
    ai_enabled: bool = False,
    ai_config: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    db: Optional[Database] = None,
    account_phone: str = "",
    stop_requested: Optional[Callable[[], bool]] = None,
) -> int:
    """
    Комментировать последние посты в списке каналов.

    channels        — список username или ссылок на каналы
    messages        — список текстов комментариев (выбирается случайно)
    limit_posts     — сколько последних постов комментировать в каждом канале
    delay_min/max   — задержка между комментариями (сек)
    delay_between_channels — задержка между каналами (сек)
    Возвращает общее количество отправленных комментариев.
    """
    total = 0
    ai_config = ai_config or {}
    stats = {
        "channels": 0,
        "posts": 0,
        "sent": 0,
        "dry_run": 0,
        "skip_no_comments": 0,
        "skip_duplicate": 0,
        "skip_no_messages": 0,
        "skip_ai_error": 0,
        "skip_limiter": 0,
        "no_permission": 0,
        "flood_wait": 0,
        "errors": 0,
    }

    def emit(text: str):
        try:
            progress_cb(_safe_text(text))
        except Exception:
            pass

    def should_stop() -> bool:
        try:
            return bool(stop_requested and stop_requested())
        except Exception:
            return False

    async def sleep_with_stop(seconds: float) -> bool:
        remaining = max(0.0, float(seconds or 0))
        while remaining > 0:
            if should_stop():
                return False
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step
        return not should_stop()

    for channel in channels:
        if should_stop():
            emit("  [=] Остановка старых постов запрошена")
            break
        emit(f"  [~] Канал: {channel}")
        stats["channels"] += 1
        sent_in_channel = 0

        try:
            entity = await client.get_entity(channel)
            messages_iter = await client.get_messages(entity, limit=limit_posts)

            for msg in messages_iter:
                if should_stop():
                    emit("  [=] Остановка старых постов запрошена")
                    break
                if not msg or not msg.id:
                    continue
                post_id = int(msg.id)
                stats["posts"] += 1

                if not getattr(msg, "replies", None) or not getattr(getattr(msg, "replies", None), "comments", False):
                    reason = _reason_comments_unavailable(msg)
                    emit(f"  [~] {channel} пост {post_id}: {reason}")
                    _log_post(db, channel, post_id, 0, account_phone, "skip_no_comments", reason)
                    stats["skip_no_comments"] += 1
                    continue

                if db and account_phone and db.has_successful_channel_comment(channel, post_id, account_phone):
                    reason = "этот пост уже был успешно прокомментирован этим аккаунтом"
                    emit(f"  [~] {channel} пост {post_id}: {reason}")
                    _log_post(db, channel, post_id, 0, account_phone, "skip_duplicate", reason)
                    stats["skip_duplicate"] += 1
                    continue

                post_text = (getattr(msg, "message", None) or getattr(msg, "text", None) or "").strip()
                text = ""
                if ai_enabled:
                    try:
                        text = await asyncio.to_thread(
                            generate_ai_comment,
                            provider_name=str(ai_config.get("provider", "openai")),
                            api_key=str(ai_config.get("api_key", "")),
                            model=str(ai_config.get("model", "")),
                            proxy=str(ai_config.get("proxy", "")),
                            post_text=post_text,
                            tone=str(ai_config.get("tone", "нейтральный")),
                            length=str(ai_config.get("length", "короткий")),
                            system_prompt_template=str(ai_config.get("system_prompt", "")),
                            user_prompt_template=str(ai_config.get("user_prompt", "")),
                        )
                    except Exception as e:
                        emit(f"  [!] AI не смог сгенерировать комментарий: {type(e).__name__}: {e}")
                        if messages:
                            text = random.choice(messages)
                            emit("  [~] Fallback: беру комментарий из списка")
                        else:
                            _log_post(db, channel, post_id, 0, account_phone, "skip_ai_error", f"{type(e).__name__}: {e}")
                            stats["skip_ai_error"] += 1
                            continue
                else:
                    if not messages:
                        emit(f"  [~] {channel} пост {post_id}: список комментариев пуст")
                        _log_post(db, channel, post_id, 0, account_phone, "skip_no_messages", "список комментариев пуст")
                        stats["skip_no_messages"] += 1
                        continue
                    text = random.choice(messages)

                try:
                    preview = (text or "").replace("\n", " ").strip()
                    if len(preview) > 120:
                        preview = preview[:120] + "…"
                    if dry_run:
                        emit(f"  [DRY] {channel} пост {post_id} ← {preview}")
                        _log_post(db, channel, post_id, 0, account_phone, "dry_run", "")
                        stats["dry_run"] += 1
                    else:
                        if db:
                            ok, reason, wait_s = db.try_acquire_action_slot(
                                account_phone, "comment", min_interval_seconds=2.0, daily_actions_limit=200
                            )
                            if not ok:
                                if reason == "min_interval" and wait_s > 0:
                                    await asyncio.sleep(min(wait_s, 5.0))
                                else:
                                    emit(f"  [~] {channel} пост {post_id}: лимитер блокирует ({reason})")
                                    _log_post(db, channel, post_id, 0, account_phone, "skip_limiter", reason)
                                    stats["skip_limiter"] += 1
                                    continue
                        sent = await asyncio.wait_for(
                            client.send_message(entity, text, comment_to=post_id),
                            timeout=30.0,
                        )
                        comment_id = int(getattr(sent, "id", 0) or 0)
                        sent_in_channel += 1
                        total += 1
                        stats["sent"] += 1
                        emit(f"  [+] {channel} пост {post_id}: comment_id={comment_id} (акк {account_phone})")
                        _log_post(db, channel, post_id, comment_id, account_phone, "sent", "")

                    delay = random.uniform(delay_min, delay_max)
                    if not await sleep_with_stop(delay):
                        emit("  [=] Остановка старых постов запрошена")
                        break

                except FloodWaitError as e:
                    emit(f"  [!] FloodWait {e.seconds}s — пауза...")
                    _log_post(db, channel, post_id, 0, account_phone, "flood_wait", f"{e.seconds}")
                    stats["flood_wait"] += 1
                    if not await sleep_with_stop(e.seconds):
                        emit("  [=] Остановка старых постов запрошена")
                        break

                except (ChatWriteForbiddenError, UserBannedInChannelError):
                    reason = "нет прав на комментарии (ChatWriteForbidden/UserBannedInChannel)"
                    emit(f"  [!] {channel}: {reason}")
                    _log_post(db, channel, post_id, 0, account_phone, "no_permission", reason)
                    stats["no_permission"] += 1
                    break

                except MsgIdInvalidError:
                    reason = "пост не принимает комментарии (MsgIdInvalidError)"
                    emit(f"  [-] {channel} пост {post_id}: {reason}")
                    _log_post(db, channel, post_id, 0, account_phone, "skip_no_comments", reason)
                    stats["skip_no_comments"] += 1
                    continue

                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    emit(f"  [-] {channel} пост {post_id}: ошибка комментария: {err}")
                    _log_post(db, channel, post_id, 0, account_phone, "error", err)
                    stats["errors"] += 1
                    continue

            emit(f"  [=] {channel}: отправлено {sent_in_channel} комментариев")

        except ChannelPrivateError:
            emit(f"  [!] Канал {channel} недоступен (ChannelPrivateError)")
            stats["errors"] += 1
        except Exception as e:
            emit(f"  [-] Ошибка получения канала {channel}: {type(e).__name__}: {e}")
            stats["errors"] += 1

        if not await sleep_with_stop(delay_between_channels):
            emit("  [=] Остановка старых постов запрошена")
            break

    if dry_run:
        emit("  [=] DRY-RUN завершён")
    else:
        emit(f"  [=] Итого комментариев: {total}")
    emit(
        "  [=] Сводка: "
        f"каналов={stats['channels']}, постов={stats['posts']}, "
        f"sent={stats['sent']}, dry={stats['dry_run']}, "
        f"no_comments={stats['skip_no_comments']}, "
        f"duplicates={stats['skip_duplicate']}, "
        f"no_permission={stats['no_permission']}, "
        f"ai_error={stats['skip_ai_error']}, errors={stats['errors']}"
    )
    if not dry_run and total == 0:
        emit("  [!] Комментариев отправлено 0 — смотри причины выше по каждому посту/каналу")
    return total


# --- Real-time listener на новые посты ---

class NewPostListener:
    """
    Слушает новые посты в указанных каналах и комментирует их.

    Использование:
        listener = NewPostListener(client, channels, messages, ...)
        await listener.start()   # блокирует до вызова stop()
    """

    def __init__(
        self,
        client: TelegramClient,
        channels: List[str],
        messages: List[str],
        delay_min: float = 5.0,
        delay_max: float = 15.0,
        progress_cb: Callable[[str], None] = print,
        ai_enabled: bool = False,
        ai_config: Optional[Dict[str, Any]] = None,
        dry_run: bool = False,
        db: Optional[Database] = None,
        account_phone: str = "",
    ):
        self.client = client
        self.channels = channels
        self.messages = messages
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.progress_cb = progress_cb
        self._stop_event = asyncio.Event()
        self._channel_ids: List[int] = []
        self.ai_enabled = ai_enabled
        self.ai_config = ai_config or {}
        self.dry_run = dry_run
        self.db = db
        self.account_phone = account_phone

    async def start(self):
        """Запустить listener. Блокирует до вызова stop()."""

        def emit(text: str):
            try:
                self.progress_cb(_safe_text(text))
            except Exception:
                pass

        # Резолвим channel ids для фильтрации событий
        for ch in self.channels:
            try:
                entity = await self.client.get_entity(ch)
                self._channel_ids.append(entity.id)
                emit(f"  [+] Слежу за каналом: {ch} (id:{entity.id})")
            except Exception as e:
                emit(f"  [-] Не удалось получить канал {ch}: {type(e).__name__}: {e}")

        if not self._channel_ids:
            emit("  [!] Нет доступных каналов для слежки")
            return

        @self.client.on(events.NewMessage(chats=self._channel_ids))
        async def handler(event):
            if not event.is_channel or event.message.out:
                return

            channel_id = event.chat_id
            msg_id = event.message.id
            post_text = (getattr(event.message, "message", None) or getattr(event.message, "text", None) or "").strip()
            channel_label = ""
            try:
                if getattr(event, "chat", None) and getattr(event.chat, "username", None):
                    channel_label = "@" + str(event.chat.username)
            except Exception:
                channel_label = ""
            if not channel_label:
                channel_label = str(channel_id)

            if not getattr(event.message, "replies", None) or not getattr(getattr(event.message, "replies", None), "comments", False):
                reason = _reason_comments_unavailable(event.message)
                emit(f"  [~] {channel_label} новый пост {msg_id}: {reason}")
                _log_post(self.db, channel_label, int(msg_id or 0), 0, self.account_phone, "skip_no_comments", reason)
                return

            if self.db and self.account_phone and self.db.has_successful_channel_comment(
                channel_label, int(msg_id or 0), self.account_phone
            ):
                reason = "этот новый пост уже был успешно прокомментирован этим аккаунтом"
                emit(f"  [~] {channel_label} новый пост {msg_id}: {reason}")
                _log_post(self.db, channel_label, int(msg_id or 0), 0, self.account_phone, "skip_duplicate", reason)
                return

            text = ""
            if self.ai_enabled:
                try:
                    text = await asyncio.to_thread(
                        generate_ai_comment,
                        provider_name=str(self.ai_config.get("provider", "openai")),
                        api_key=str(self.ai_config.get("api_key", "")),
                        model=str(self.ai_config.get("model", "")),
                        proxy=str(self.ai_config.get("proxy", "")),
                        post_text=post_text,
                        tone=str(self.ai_config.get("tone", "нейтральный")),
                        length=str(self.ai_config.get("length", "короткий")),
                        system_prompt_template=str(self.ai_config.get("system_prompt", "")),
                        user_prompt_template=str(self.ai_config.get("user_prompt", "")),
                    )
                except Exception as e:
                    emit(f"  [!] AI не смог сгенерировать комментарий: {type(e).__name__}: {e}")
                    if self.messages:
                        text = random.choice(self.messages)
                        emit("  [~] Fallback: беру комментарий из списка")
                    else:
                        _log_post(self.db, channel_label, int(msg_id or 0), 0, self.account_phone, "skip_ai_error", f"{type(e).__name__}: {e}")
                        return
            else:
                if not self.messages:
                    emit(f"  [~] {channel_label} новый пост {msg_id}: список комментариев пуст")
                    _log_post(self.db, channel_label, int(msg_id or 0), 0, self.account_phone, "skip_no_messages", "список комментариев пуст")
                    return
                text = random.choice(self.messages)

            try:
                await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))
                preview = (text or "").replace("\n", " ").strip()
                if len(preview) > 120:
                    preview = preview[:120] + "…"
                if self.dry_run:
                    emit(f"  [DRY] {channel_label} новый пост {msg_id} ← {preview}")
                    _log_post(self.db, channel_label, int(msg_id or 0), 0, self.account_phone, "dry_run", "")
                else:
                    if self.db:
                        ok, reason, wait_s = self.db.try_acquire_action_slot(
                            self.account_phone, "comment", min_interval_seconds=2.0, daily_actions_limit=200
                        )
                        if not ok:
                            if reason == "min_interval" and wait_s > 0:
                                await asyncio.sleep(min(wait_s, 5.0))
                            else:
                                emit(f"  [~] {channel_label} новый пост {msg_id}: лимитер блокирует ({reason})")
                                _log_post(self.db, channel_label, int(msg_id or 0), 0, self.account_phone, "skip_limiter", reason)
                                return
                    sent = await asyncio.wait_for(
                        self.client.send_message(event.chat, text, comment_to=msg_id),
                        timeout=30.0,
                    )
                    comment_id = int(getattr(sent, "id", 0) or 0)
                    emit(f"  [+] {channel_label} новый пост {msg_id}: comment_id={comment_id} (акк {self.account_phone})")
                    _log_post(self.db, channel_label, int(msg_id or 0), comment_id, self.account_phone, "sent", "")
            except FloodWaitError as e:
                emit(f"  [!] FloodWait {e.seconds}s")
                _log_post(self.db, channel_label, int(msg_id or 0), 0, self.account_phone, "flood_wait", f"{e.seconds}")
                await asyncio.sleep(e.seconds)
            except (ChatWriteForbiddenError, UserBannedInChannelError):
                reason = "нет прав на комментарии (ChatWriteForbidden/UserBannedInChannel)"
                emit(f"  [!] {channel_label}: {reason}")
                _log_post(self.db, channel_label, int(msg_id or 0), 0, self.account_phone, "no_permission", reason)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                emit(f"  [-] {channel_label} новый пост {msg_id}: ошибка комментария: {err}")
                _log_post(self.db, channel_label, int(msg_id or 0), 0, self.account_phone, "error", err)

        emit("  [i] Режим 'Новые посты' комментирует только посты, появившиеся ПОСЛЕ запуска")
        emit("  [~] Жду новых постов...")
        await self._stop_event.wait()

        self.client.remove_event_handler(handler)
        emit("  [=] Listener остановлен")

    def stop(self):
        """Сигнал остановки (потокобезопасен)."""
        self._stop_event.set()
