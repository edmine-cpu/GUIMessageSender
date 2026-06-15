"""
ads_subscriptions.py — проверка и автовступление в обязательные подписки.

Перед публикацией в группу планировщик проверяет список required_subs:
каналы/группы в которых нужно состоять. Если аккаунт не состоит —
автоматически вступает с соблюдением hard limits + рандомного интервала
из SchedulerSettings.

Hard limits (вшиты в код):
  - HARD_MIN_JOIN_INTERVAL_SEC  = 300   (5 мин между вступлениями)
  - HARD_MAX_DAILY_JOINS        = 15    (вступлений в сутки)

Рандомный интервал: от settings.join_interval_min_seconds до
settings.join_interval_max_seconds (каждое вступление — новый random.uniform),
но не меньше HARD_MIN_JOIN_INTERVAL_SEC.

Счётчики хранятся в памяти (в экземпляре SubscriptionManager).
Планировщик создаёт один экземпляр на запуск — счётчики сбрасываются
при перезапуске приложения, что приемлемо (лучше немного занизить
чем переусердствовать).
"""

import random
from datetime import datetime, timedelta
from typing import Optional

from ads_database import AdsDB
from ads_models import RequiredSub, SchedulerSettings

# ─── Hard limits ────────────────────────────────────────────────────────────
HARD_MIN_JOIN_INTERVAL_SEC: int = 300   # 5 минут между вступлениями
HARD_MAX_DAILY_JOINS: int = 15          # максимум в сутки


def _now() -> datetime:
    return datetime.now()


class SubscriptionManager:
    """
    Управляет проверкой и автовступлением в каналы/группы.

    Использование:
        mgr = SubscriptionManager(db, account_phone)
        ok = await mgr.ensure_subscriptions(client, group_id)
        if not ok:
            # публикацию откладываем

    Опционально принимает settings для рандомного интервала между вступлениями.
    Если settings не переданы, используется hard-limit как фиксированный интервал
    (старое поведение — для обратной совместимости).
    """

    def __init__(self, db: AdsDB, account_phone: str,
                 settings: Optional[SchedulerSettings] = None):
        self.db = db
        self.account_phone = account_phone
        self.settings = settings

        # Счётчики в памяти — сбрасываются при перезапуске приложения
        self._joins_today: int = 0
        self._joins_today_date: str = _now().date().isoformat()
        # Следующее разрешённое время вступления (рандомное после каждого join)
        self._next_join_allowed_at: Optional[datetime] = None

    def _compute_next_join_delay_sec(self) -> float:
        """Случайная задержка до следующего вступления.
        Если settings заданы — uniform(min, max), иначе — HARD_MIN (legacy).
        min в любом случае не меньше HARD_MIN_JOIN_INTERVAL_SEC.
        """
        if self.settings is None:
            return float(HARD_MIN_JOIN_INTERVAL_SEC)

        lo = max(self.settings.join_interval_min_seconds,
                 HARD_MIN_JOIN_INTERVAL_SEC)
        hi = max(self.settings.join_interval_max_seconds, lo)
        if lo == hi:
            return float(lo)
        return random.uniform(lo, hi)

    # ─── Публичный API ───────────────────────────────────────────────────────

    async def ensure_subscriptions(self, client, group_id: int) -> bool:
        """
        Проверить и при необходимости выполнить все подписки для группы.

        Возвращает True если все подписки выполнены (публикацию можно продолжать).
        Возвращает False если не все выполнены (публикацию надо отложить —
        либо лимит исчерпан, либо вступление не удалось).
        """
        subs = self.db.get_required_subs_for_group(group_id)
        if not subs:
            return True  # нет требований — всегда ок

        all_ok = True
        for sub in subs:
            ok = await self._ensure_single(client, sub)
            if not ok:
                all_ok = False

        return all_ok

    async def check_membership(self, client, channel_link: str) -> bool:
        """
        Проверить состоит ли аккаунт в канале/группе.
        Возвращает True если состоит, False если нет или ошибка.
        """
        try:
            # Lazy import — защита от мока telethon в тестах
            from telethon.tl.functions.channels import GetParticipantRequest
            from telethon.errors import (
                UserNotParticipantError,
                ChannelPrivateError,
                ChatForbiddenError,
            )
            try:
                entity = await client.get_entity(channel_link)
                await client(GetParticipantRequest(entity, "me"))
                return True
            except UserNotParticipantError:
                return False
            except (ChannelPrivateError, ChatForbiddenError):
                # Группа приватная / недоступная — считаем "не состоим"
                return False
        except Exception:
            return False

    async def join_channel(self, client, channel_link: str) -> bool:
        """
        Вступить в канал/группу с соблюдением hard limits.

        Возвращает True если вступили успешно.
        Возвращает False если лимит исчерпан или ошибка вступления.
        """
        # Сброс счётчика если начался новый день
        today = _now().date().isoformat()
        if today != self._joins_today_date:
            self._joins_today = 0
            self._joins_today_date = today
            self._next_join_allowed_at = None  # новый день — можно сразу

        # Hard limit: максимум в сутки
        if self._joins_today >= HARD_MAX_DAILY_JOINS:
            print(f"  [!] Лимит вступлений в сутки исчерпан "
                  f"({HARD_MAX_DAILY_JOINS}), пропускаем {channel_link}")
            return False

        # Рандомный интервал между вступлениями
        if self._next_join_allowed_at is not None:
            now = _now()
            if now < self._next_join_allowed_at:
                wait = int((self._next_join_allowed_at - now).total_seconds())
                print(f"  [!] Слишком рано для вступления, подождём {wait}с "
                      f"перед {channel_link}")
                return False

        # Вступаем
        try:
            from telethon.tl.functions.channels import JoinChannelRequest
            from telethon.errors import (
                FloodWaitError,
                UserAlreadyParticipantError,
                ChannelPrivateError,
                InviteHashExpiredError,
            )
            try:
                entity = await client.get_entity(channel_link)
                await client(JoinChannelRequest(entity))
                self._joins_today += 1
                # Генерируем рандомную задержку до следующего join
                delay_sec = self._compute_next_join_delay_sec()
                self._next_join_allowed_at = _now() + timedelta(seconds=delay_sec)
                print(f"  [+] Вступил в {channel_link} • "
                      f"след. вступление через {delay_sec/60:.1f}м")
                return True

            except UserAlreadyParticipantError:
                # Уже состоим — считаем успехом, счётчики не трогаем
                return True

            except FloodWaitError as e:
                print(f"  [!] FloodWait {e.seconds}s при вступлении в {channel_link}")
                return False

            except (ChannelPrivateError, InviteHashExpiredError) as e:
                print(f"  [!] Не удалось вступить в {channel_link}: {e}")
                return False

        except Exception as e:
            print(f"  [-] Ошибка вступления в {channel_link}: {e}")
            return False

    def can_join_now(self) -> bool:
        """
        Можно ли вступить прямо сейчас (без учёта конкретного канала).
        Используется планировщиком для предварительной проверки.
        """
        today = _now().date().isoformat()
        if today != self._joins_today_date:
            return True  # новый день — счётчик сбросится

        if self._joins_today >= HARD_MAX_DAILY_JOINS:
            return False

        if self._next_join_allowed_at is not None:
            if _now() < self._next_join_allowed_at:
                return False

        return True

    def seconds_until_can_join(self) -> int:
        """Сколько секунд до следующего доступного вступления (0 если можно сейчас)."""
        if self._next_join_allowed_at is None:
            return 0
        remaining = (self._next_join_allowed_at - _now()).total_seconds()
        return max(0, int(remaining))

    # ─── Приватные методы ────────────────────────────────────────────────────

    async def _ensure_single(self, client, sub: RequiredSub) -> bool:
        """Проверить и при необходимости выполнить одну подписку."""
        # Проверяем через API
        is_member = await self.check_membership(client, sub.channel_link)

        # Обновляем статус в БД
        self.db.set_sub_joined(sub.group_id, sub.channel_link, is_member)

        if is_member:
            return True

        # Не состоим — пробуем вступить
        print(f"  [~] Не состоим в {sub.channel_link}, пробуем вступить...")
        joined = await self.join_channel(client, sub.channel_link)

        if joined:
            self.db.set_sub_joined(sub.group_id, sub.channel_link, True)
            return True

        return False
