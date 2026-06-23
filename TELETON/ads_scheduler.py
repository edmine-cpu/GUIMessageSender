"""
ads_scheduler.py — планировщик публикации объявлений.

Запускается в отдельном потоке. Каждые N секунд (tick_interval) вызывает
tick() который:
  1. Находит активные объявления
  2. Для каждого — находит группы где можно публиковать прямо сейчас
  3. Проверяет обязательные подписки
  4. Публикует через ads_publisher
  5. Логирует результат в publications_log
  6. Обновляет groups_targets.retry_after

Hard limits (вшиты в код):
  HARD_MIN_PUBLICATION_INTERVAL_SEC  = 30   (между любыми публикациями)
  HARD_MAX_DAILY_PUBLICATIONS        = 50   (в сутки с аккаунта)
  HARD_MIN_GROUP_INTERVAL_SEC        = 1800 (30 мин между публикациями в одну группу)

Настраиваемые параметры берутся из scheduler_settings (хранятся в БД),
но не могут быть меньше hard limits.
"""

import asyncio
import random
import threading
from datetime import datetime, timedelta
from typing import Optional, Callable

from ads_database import AdsDB
from ads_models import (
    Ad, GroupTarget, SchedulerSettings,
    PUB_STATUS_OK,
    GROUP_STATUS_ACTIVE,
    GROUP_STATUS_UNAVAILABLE,
)
from ads_publisher import publish_to_group, build_publication_log
from ads_subscriptions import SubscriptionManager
from diagnostics import human_exception, human_reason
from file_logger import log_to_file

# ─── Hard limits ────────────────────────────────────────────────────────────
HARD_MIN_PUBLICATION_INTERVAL_SEC: int = 30    # между любыми публикациями
HARD_MAX_DAILY_PUBLICATIONS: int = 50          # в сутки с аккаунта
HARD_MIN_GROUP_INTERVAL_SEC: int = 1800        # 30 минут между публ. в одну группу

# Рекомендуемые defaults (используются если БД пустая)
DEFAULT_PUBLICATION_INTERVAL_SEC: int = 300    # 5 минут
DEFAULT_DAILY_PUBLICATIONS: int = 30
DEFAULT_JOIN_INTERVAL_SEC: int = 900           # 15 минут
DEFAULT_DAILY_JOINS: int = 5


def _now() -> datetime:
    return datetime.now()


def _clamp(value: int, minimum: int) -> int:
    """Вернуть value, но не меньше minimum (hard limit)."""
    return max(value, minimum)


def _random_interval_sec(min_sec: int, max_sec: int, hard_min: int) -> float:
    """Случайный интервал в секундах из диапазона [min, max], но не меньше hard_min.

    Безопасно обрабатывает крайние случаи:
      - min > max  → используем min
      - min == max → возвращаем min (без рандома)
      - оба < hard_min → возвращаем hard_min
    """
    lo = _clamp(min_sec, hard_min)
    hi = _clamp(max_sec, lo)  # hi не может быть меньше lo
    if lo == hi:
        return float(lo)
    return random.uniform(lo, hi)


def _random_group_interval_sec(group: GroupTarget) -> float:
    """Случайный интервал для конкретной группы.

    Базовый минимум — group.interval_minutes × 60.
    Максимум — group.interval_minutes_max × 60, либо (если 0) interval_minutes × 2.
    Hard limit HARD_MIN_GROUP_INTERVAL_SEC применяется снизу.
    """
    min_sec = group.interval_minutes * 60
    if group.interval_minutes_max and group.interval_minutes_max > 0:
        max_sec = group.interval_minutes_max * 60
    else:
        # Legacy: если max не задан, используем 2 × min (разумный дефолт)
        max_sec = min_sec * 2
    return _random_interval_sec(min_sec, max_sec, HARD_MIN_GROUP_INTERVAL_SEC)


# ─── Публичные helpers для рассылочных подсистем (gui.py) ──────────────────
# Используются в gui.py для расчёта рандомных пауз между отправками в:
#   - broadcast (рассылка в группы)
#   - mention (упоминания)
#   - DM (личные сообщения)
#   - group check (join групп в "Проверить и очистить")
# Hard min = 1 секунда (физически быстрее нельзя между send'ами).

def random_broadcast_delay_sec(settings: SchedulerSettings) -> float:
    """Случайная задержка между broadcast-сообщениями."""
    return _random_interval_sec(
        settings.broadcast_delay_min_seconds,
        settings.broadcast_delay_max_seconds,
        hard_min=1,
    )


def random_mention_delay_sec(settings: SchedulerSettings) -> float:
    """Случайная задержка между mention-сообщениями (упоминания)."""
    return _random_interval_sec(
        settings.mention_delay_min_seconds,
        settings.mention_delay_max_seconds,
        hard_min=1,
    )


def random_dm_delay_sec(settings: SchedulerSettings) -> float:
    """Случайная задержка между DM."""
    return _random_interval_sec(
        settings.dm_delay_min_seconds,
        settings.dm_delay_max_seconds,
        hard_min=1,
    )


def random_group_check_delay_sec(settings: SchedulerSettings) -> float:
    """Случайная задержка между join'ами в "Проверить и очистить"."""
    return _random_interval_sec(
        settings.group_check_join_delay_min_seconds,
        settings.group_check_join_delay_max_seconds,
        hard_min=1,
    )


def _can_publish_to_group(group: GroupTarget) -> bool:
    """
    Проверить все условия для публикации в конкретную группу:
    - группа активна
    - нет активного retry_after (запрет Telegram)
    - нет активного next_allowed_at (наш рандомный интервал)
    - текущий час в разрешённых часах группы

    Интервал между публикациями в эту группу теперь хранится в БД как
    next_allowed_at и обновляется после каждой публикации случайным значением
    из диапазона [interval_minutes, interval_minutes_max].
    """
    if group.status != GROUP_STATUS_ACTIVE:
        return False

    now = _now()

    # retry_after — временный запрет от Telegram (flood, forbidden, slow_mode)
    if group.retry_after:
        try:
            retry_dt = datetime.fromisoformat(group.retry_after)
            if now < retry_dt:
                return False
        except ValueError:
            pass

    # next_allowed_at — наш рандомный интервал между публикациями в эту группу
    if group.next_allowed_at:
        try:
            next_dt = datetime.fromisoformat(group.next_allowed_at)
            if now < next_dt:
                return False
        except ValueError:
            pass

    # Проверка разрешённых часов. Диапазон может переходить через полночь:
    # 22-06 значит "с 22:00 до 06:59".
    current_hour = now.hour
    if group.hours_start <= group.hours_end:
        allowed = group.hours_start <= current_hour <= group.hours_end
    else:
        allowed = current_hour >= group.hours_start or current_hour <= group.hours_end
    if not allowed:
        return False

    return True


def _format_wait_until(dt: datetime, now: Optional[datetime] = None) -> str:
    now = now or _now()
    seconds = max(0, int((dt - now).total_seconds()))
    minutes = seconds // 60
    if minutes >= 60:
        wait = f"{minutes // 60}ч {minutes % 60}м"
    elif minutes > 0:
        wait = f"{minutes}м"
    else:
        wait = f"{seconds}с"
    return f"{dt.strftime('%Y-%m-%d %H:%M')} (ещё {wait})"


def _retry_after_label(group: GroupTarget, retry_dt: datetime,
                       now: datetime) -> str:
    error = (group.last_error or "").strip()
    error_l = error.lower()
    seconds = max(0, int((retry_dt - now).total_seconds()))

    if "slowmode" in error_l or "slow_mode" in error_l:
        label = "slow mode"
    elif error_l.startswith("join:") or group.join_status == "not_member":
        label = "нет доступа/не вступили"
    elif "banned" in error_l or seconds >= 20 * 24 * 3600:
        label = "бан/долгая блокировка"
    elif "forbidden" in error_l or seconds >= 6 * 24 * 3600:
        label = "запрет отправки"
    elif error:
        label = "временная ошибка"
    else:
        label = "отложено Telegram"

    if error:
        return f"{label} ({error[:80]})"
    return label


def _group_block_reason(group: GroupTarget,
                        now: Optional[datetime] = None) -> str:
    """Return a short human-readable reason why a group is not publishable."""
    now = now or _now()
    if group.status != GROUP_STATUS_ACTIVE:
        return f"статус {group.status}"

    if group.retry_after:
        try:
            retry_dt = datetime.fromisoformat(group.retry_after)
            if now < retry_dt:
                label = _retry_after_label(group, retry_dt, now)
                return f"{label} до {_format_wait_until(retry_dt, now)}"
        except ValueError:
            pass

    if group.next_allowed_at:
        try:
            next_dt = datetime.fromisoformat(group.next_allowed_at)
            if now < next_dt:
                return f"интервал группы до {_format_wait_until(next_dt, now)}"
        except ValueError:
            pass

    current_hour = now.hour
    if group.hours_start <= group.hours_end:
        allowed = group.hours_start <= current_hour <= group.hours_end
    else:
        allowed = current_hour >= group.hours_start or current_hour <= group.hours_end
    if not allowed:
        return f"вне часов {group.hours_start:02d}:00-{group.hours_end:02d}:59"

    return ""


def _blocked_groups_summary(groups: list[GroupTarget],
                            now: Optional[datetime] = None,
                            limit: int = 3) -> str:
    now = now or _now()
    samples: list[str] = []
    counts: dict[str, int] = {}
    for group in groups:
        reason = _group_block_reason(group, now)
        if not reason:
            continue
        key = reason.split(" до ", 1)[0]
        counts[key] = counts.get(key, 0) + 1
        if len(samples) < limit:
            samples.append(f"{group.link}: {reason}")

    parts = []
    if counts:
        parts.append(", ".join(f"{name}: {count}" for name, count in counts.items()))
    if samples:
        parts.append("примеры: " + "; ".join(samples))
    return " | ".join(parts)


def _blocked_groups_signature(groups: list[GroupTarget],
                              now: Optional[datetime] = None) -> str:
    now = now or _now()
    counts: dict[str, int] = {}
    for group in groups:
        reason = _group_block_reason(group, now)
        if not reason:
            continue
        key = reason.split(" до ", 1)[0]
        counts[key] = counts.get(key, 0) + 1
    return ";".join(f"{name}:{counts[name]}" for name in sorted(counts))


def _can_publish_globally(pub_count_today: int,
                           next_pub_allowed_at: Optional[datetime],
                           settings: SchedulerSettings) -> bool:
    """
    Проверить глобальные лимиты публикаций:
    - дневной лимит
    - глобальный next_pub_allowed_at (рандомный интервал между публикациями)
    """
    # Дневной лимит
    daily_limit = _clamp(settings.daily_publication_limit,
                          1)  # минимум 1
    daily_limit = min(daily_limit, HARD_MAX_DAILY_PUBLICATIONS)
    if pub_count_today >= daily_limit:
        return False

    # Глобальный интервал — проверяем next_allowed_at (устанавливается после публикации)
    if next_pub_allowed_at is not None and _now() < next_pub_allowed_at:
        return False

    return True


class AdsScheduler:
    """
    Планировщик публикации объявлений.

    Использование:
        scheduler = AdsScheduler(db_path, account_phone, client_factory)
        scheduler.start()
        ...
        scheduler.stop()

    client_factory — callable() -> TelegramClient (НЕподключённый).
                     Планировщик сам вызывает connect() и is_user_authorized(),
                     переиспользует клиент между тиками, дисконнектит при остановке.
    log_cb — необязательный коллбэк для логирования в GUI: log_cb(str).
    """

    def __init__(self,
                 db_path: str,
                 account_phone: str,
                 client_factory: Callable,
                 log_cb: Optional[Callable[[str], None]] = None,
                 tick_interval: int = 60):
        self.db_path = db_path
        self.account_phone = account_phone
        self.client_factory = client_factory
        self.log_cb = log_cb or (lambda msg: print(msg))
        self.tick_interval = tick_interval

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Следующее разрешённое время публикации (для глобального рандомного
        # интервала). Устанавливается после каждой успешной публикации как
        # now + uniform(publication_interval_min, publication_interval_max).
        # None означает "можно публиковать прямо сейчас".
        self._next_pub_allowed_at: Optional[datetime] = None

        # Переиспользуемый клиент. Создаётся лениво при первой публикации,
        # переиспользуется между тиками, дисконнектится при stop().
        self._client = None
        self._session_lock = None
        self._CONNECT_TIMEOUT_SECONDS = 30.0
        self._AUTH_TIMEOUT_SECONDS = 20.0
        self._DISCONNECT_TIMEOUT_SECONDS = 10.0
        self._ACCOUNT_HEALTH_RECHECK_SECONDS = 15 * 60
        self._last_account_health_check_at: Optional[datetime] = None
        self._IDLE_GUI_LOG_INTERVAL_SECONDS = 300
        self._last_idle_gui_log_at: Optional[datetime] = None
        self._last_idle_gui_log_signature = ""

    # ─── Управление ──────────────────────────────────────────────────────────

    def start(self):
        """Запустить планировщик в фоновом потоке."""
        if self.is_alive:
            return
        if self._running and self._thread and not self._thread.is_alive():
            self._running = False
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"AdsScheduler:{self.account_phone}")
        self._thread.start()
        self.log_cb("[+] Планировщик запущен")

    def stop(self):
        """Остановить планировщик."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        stopped = not (self._thread and self._thread.is_alive())
        if stopped:
            self.log_cb("[~] Планировщик остановлен")
        else:
            self.log_cb("[!] Планировщик не остановился за timeout")
        return stopped

    @property
    def is_running(self) -> bool:
        return bool(self._running and self._thread and self._thread.is_alive())

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _log_idle(self, message: str, signature: Optional[str] = None):
        log_to_file("ads", message)
        signature = signature or message
        now = _now()
        if (
            signature != self._last_idle_gui_log_signature
            or self._last_idle_gui_log_at is None
            or (now - self._last_idle_gui_log_at).total_seconds() >= self._IDLE_GUI_LOG_INTERVAL_SECONDS
        ):
            self.log_cb(message)
            self._last_idle_gui_log_signature = signature
            self._last_idle_gui_log_at = now

    def _account_health_check_due(self) -> bool:
        if self._last_account_health_check_at is None:
            return True
        return (
            _now() - self._last_account_health_check_at
        ).total_seconds() >= self._ACCOUNT_HEALTH_RECHECK_SECONDS

    def _account_can_attempt_connect(self) -> tuple[bool, str]:
        try:
            from database import Database
            db = Database(self.db_path)
            try:
                rows = db.get_accounts_health()
            finally:
                db.close()
        except Exception as e:
            log_to_file("ads", f"[-] account health read failed for {self.account_phone}: {e}")
            return True, ""

        health = next(
            (h for h in rows if h.get("phone") == self.account_phone),
            None,
        )
        if not health:
            return False, "not_found"

        state = (health.get("health") or "active").strip().lower()
        if state in ("banned", "needs_reauth", "inactive", "paused", "flood_wait"):
            why = (health.get("why") or health.get("last_error_text") or state).strip()
            return False, f"{state}: {why}" if why else state
        return True, ""

    @staticmethod
    def _classify_connect_exception(e: Exception) -> tuple[str, str]:
        name = type(e).__name__
        if name in {
            "UserDeactivatedBanError",
            "PhoneNumberBannedError",
            "UserDeactivatedError",
        }:
            return "banned", name
        if name in {
            "AuthKeyDuplicatedError",
            "AuthKeyUnregisteredError",
            "AuthKeyInvalidError",
            "SessionExpiredError",
            "SessionRevokedError",
        }:
            return "needs_reauth", name
        if isinstance(e, (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError)):
            return "network", name
        return "error", name

    def _record_account_connect_success(self):
        try:
            from database import Database
            db = Database(self.db_path)
            try:
                db.on_connect_success(self.account_phone)
            finally:
                db.close()
            self._last_account_health_check_at = _now()
        except Exception as e:
            log_to_file("ads", f"[-] account connect success write failed for {self.account_phone}: {e}")

    def _record_account_connect_problem(self, kind: str, reason: str):
        try:
            from database import Database
            from models import ACCOUNT_STATUS_BANNED, ACCOUNT_STATUS_NEEDS_REAUTH

            db = Database(self.db_path)
            try:
                if kind == "banned":
                    db.set_account_status(self.account_phone, ACCOUNT_STATUS_BANNED, reason)
                    db.log_account_action(
                        self.account_phone, "ads_connect", "", "banned", reason)
                elif kind == "needs_reauth":
                    db.set_account_status(self.account_phone, ACCOUNT_STATUS_NEEDS_REAUTH, reason)
                    db.record_connect_problem(self.account_phone, reason)
                elif kind == "network":
                    db.on_connect_network_issue(self.account_phone, reason)
                else:
                    db.on_connect_error(self.account_phone, reason)
            finally:
                db.close()
        except Exception as e:
            log_to_file("ads", f"[-] account connect problem write failed for {self.account_phone}: {e}")

    async def _check_client_authorized(self, client) -> bool:
        try:
            authorized = await asyncio.wait_for(
                client.is_user_authorized(), timeout=self._AUTH_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            kind, reason = self._classify_connect_exception(e)
            self._record_account_connect_problem(kind, reason)
            self.log_cb(f"[!] Account {self.account_phone} auth check failed: {reason}")
            return False

        if not authorized:
            reason = "is_user_authorized=False"
            self._record_account_connect_problem("needs_reauth", reason)
            self.log_cb(f"[!] Account {self.account_phone} is not authorized; marked needs_reauth")
            return False

        self._record_account_connect_success()
        return True

    # ─── Внутренний цикл ─────────────────────────────────────────────────────

    def _run(self):
        """Точка входа фонового потока."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._loop_async())
        finally:
            self._running = False
            # Дренируем pending tasks (Python 3.12+ требует)
            pending = asyncio.all_tasks(self._loop)
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()
            self._loop = None

    async def _loop_async(self):
        """Async цикл — вызывает tick() каждые tick_interval секунд."""
        try:
            while self._running:
                try:
                    await self._tick()
                except Exception as e:
                    self.log_cb(f"[-] Ошибка в планировщике: {e}")
                # Ждём следующего тика, но проверяем _running каждую секунду
                for _ in range(self.tick_interval):
                    if not self._running:
                        return
                    await asyncio.sleep(1)
        finally:
            # Дисконнектим клиент при остановке планировщика
            await self._disconnect_client()

    async def _ensure_connected_client(self, force_auth_check: bool = False):
        """Получить подключённый и авторизованный клиент.
        Создаёт новый через фабрику если клиента нет или он отключён.
        Возвращает клиент или None при ошибке.
        """
        # Если клиент существует и подключён — используем его
        if self._client is not None:
            try:
                if self._client.is_connected():
                    if force_auth_check:
                        if await self._check_client_authorized(self._client):
                            return self._client
                        await self._disconnect_client()
                        return None
                    return self._client
            except Exception:
                pass
            # Клиент есть, но отключён — дисконнектим и пересоздаём
            await self._disconnect_client()

        # Создаём нового. Ads bypasses TelegramSender.connect(), поэтому берём
        # общий per-account lock здесь, до создания/подключения raw client.
        client = None
        try:
            if self._session_lock is None:
                try:
                    from sender import TelegramSender
                    self._session_lock = TelegramSender.try_acquire_account_session(self.account_phone)
                except Exception as e:
                    self.log_cb(f"[!] Не удалось получить lock аккаунта {self.account_phone}: {e}")
                    return None
                if self._session_lock is None:
                    self.log_cb(f"[!] Аккаунт занят другим процессом: {self.account_phone}")
                    return None
            client = self.client_factory()
            await asyncio.wait_for(client.connect(), timeout=self._CONNECT_TIMEOUT_SECONDS)
            if not await self._check_client_authorized(client):
                self.log_cb(f"[!] Аккаунт {self.account_phone} не авторизован")
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=self._DISCONNECT_TIMEOUT_SECONDS)
                except Exception:
                    pass
                self._release_session_lock()
                return None
            self._client = client
            return client
        except Exception as e:
            kind, reason = self._classify_connect_exception(e)
            self._record_account_connect_problem(kind, reason)
            self.log_cb(f"[-] {self.account_phone}: {human_exception(e)}")
            self.log_cb(f"[-] Не удалось подключить клиент {self.account_phone}: {e}")
            if client is not None:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=self._DISCONNECT_TIMEOUT_SECONDS)
                except Exception:
                    pass
            self._release_session_lock()
            return None

    def _release_session_lock(self):
        if self._session_lock is None:
            return
        try:
            from sender import TelegramSender
            TelegramSender.release_account_session(self._session_lock)
        except Exception:
            pass
        self._session_lock = None

    async def _disconnect_client(self):
        """Отключить клиент если он подключён и освободить account lock."""
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.disconnect(), timeout=self._DISCONNECT_TIMEOUT_SECONDS)
            except Exception:
                pass
            self._client = None
        self._release_session_lock()

    async def _tick(self):
        """Один тик планировщика: найти и выполнить следующую публикацию."""
        db = AdsDB(self.db_path)
        try:
            settings = db.load_scheduler_settings()
            ads = db.get_active_ads()
            # Считаем относящиеся к этому аккаунту
            my_ads = [a for a in ads
                      if a.account_phone == self.account_phone]
            pub_count_today = db.count_publications_today(self.account_phone)
            all_groups_count = len(db.get_all_groups())

            if not ads:
                self._log_idle(
                    f"[~] tick {self.account_phone}: нет активных объявлений "
                    f"(всего ads=0)",
                    signature="no-active-ads",
                )
                return

            if not my_ads:
                self._log_idle(
                    f"[~] tick {self.account_phone}: нет объявлений для этого "
                    f"аккаунта (есть {len(ads)} ads, но account_phone не совпадает)",
                    signature=f"no-account-ads:{len(ads)}",
                )
                return

            can_connect, block_reason = self._account_can_attempt_connect()
            if not can_connect:
                self._log_idle(
                    f"[!] tick {self.account_phone}: account blocked - {block_reason}",
                    signature=f"account-blocked:{block_reason}",
                )
                return

            client = await self._ensure_connected_client(
                force_auth_check=self._account_health_check_due())
            if client is None:
                return

            # Глобальная проверка лимитов
            if not _can_publish_globally(pub_count_today,
                                          self._next_pub_allowed_at, settings):
                # Уточняем причину
                if pub_count_today >= settings.daily_publication_limit:
                    reason = (f"исчерпан дневной лимит "
                              f"({pub_count_today}/{settings.daily_publication_limit})")
                else:
                    secs_left = max(0, int((self._next_pub_allowed_at -
                                             datetime.now()).total_seconds()))
                    reason = f"общий интервал ещё {secs_left}с"
                self._log_idle(
                    f"[~] tick {self.account_phone}: глобально нельзя — {reason}",
                    signature=("global-daily-limit" if pub_count_today >= settings.daily_publication_limit
                               else "global-interval"),
                )
                return

            # Ищем следующую пару (объявление, группа) для публикации.
            ready_groups = 0
            total_groups = 0
            blocked_groups: list[GroupTarget] = []
            for ad in my_ads:
                groups = db.get_groups_for_ad(ad.id)
                total_groups += len(groups)
                for group in groups:
                    if _can_publish_to_group(group):
                        ready_groups += 1
                        # Нашли пару — публикуем
                        log_to_file("ads",
                                    f"[+] tick {self.account_phone}: публикую "
                                    f"'{ad.title}' → {group.link}")
                        await self._publish_one(db, ad, group, settings)
                        return  # Один тик = одна публикация
                    blocked_groups.append(group)

            # Если дошли сюда — ни одна группа не готова
            summary = _blocked_groups_summary(blocked_groups)
            signature = _blocked_groups_signature(blocked_groups)
            details = f" | {summary}" if summary else ""
            self._log_idle(
                f"[~] tick {self.account_phone}: ни одна группа не готова "
                f"(ads={len(my_ads)}, linked_groups={total_groups}/{all_groups_count}, "
                f"ready=0, posted_today={pub_count_today}/{settings.daily_publication_limit})"
                f"{details}",
                signature=f"no-ready:{len(my_ads)}:{total_groups}:{all_groups_count}:{signature}",
            )

        finally:
            db.close()

    async def _publish_one(self, db: AdsDB, ad: Ad, group: GroupTarget,
                            settings: SchedulerSettings):
        """Выполнить одну публикацию."""
        # Подбираем текст: адаптация под группу или базовый
        adaptation = db.get_adaptation(ad.id, group.id)
        text = adaptation.text if adaptation else ad.text_base

        if not text:
            self.log_cb(f"[!] Объявление #{ad.id}: {human_reason('empty_text')}")
            self.log_cb(f"[!] Пустой текст объявления #{ad.id}, пропускаем")
            return

        self.log_cb(f"[~] Публикую объявление '{ad.title}' → {group.link}...")

        try:
            client = await self._ensure_connected_client()
            if client is None:
                # Не смогли получить подключённый авторизованный клиент
                # — пропускаем публикацию, следующий тик попробует снова
                return

            # Проверяем обязательные подписки
            sub_mgr = SubscriptionManager(db, self.account_phone, settings)
            subs_ok = await sub_mgr.ensure_subscriptions(client, group.id)
            if not subs_ok:
                self.log_cb(f"[!] {group.link}: {human_reason('need_subscription')}")
                self.log_cb(
                    f"[!] Не выполнены подписки для {group.link}, откладываем")
                return

            from parser import ensure_chat_access

            decision, reason, retry_after = await ensure_chat_access(client, group.link)
            if decision != "ok":
                self.log_cb(f"[!] {group.link}: {human_reason(reason)}")
                db.set_group_join_status(group.id, "not_member")
                if retry_after:
                    db.set_group_retry_after(group.id, retry_after, f"join:{reason}")
                if reason in ("invalid", "expired", "private"):
                    db.set_group_status(group.id, GROUP_STATUS_UNAVAILABLE, f"join:{reason}")
                self.log_cb(
                    f"[!] Нет доступа к {group.link}: {reason} — отложено")
                return

            db.set_group_join_status(group.id, "member")

            # Публикуем
            result = await publish_to_group(
                client=client,
                group=group,
                text=text,
                media_path=ad.media_path or "",
                account_phone=self.account_phone,
                ad_id=ad.id,
                button_text=ad.button_text,
                button_url=ad.button_url,
            )

            # Логируем результат
            log = build_publication_log(
                result, ad_id=ad.id, group_id=group.id,
                account_phone=self.account_phone)
            db.add_publication_log(log)

            # Обновляем retry_after группы если нужно
            if result.retry_after:
                db.set_group_retry_after(
                    group.id, result.retry_after, result.error_text)

            if result.new_group_status:
                db.set_group_status(group.id, result.new_group_status,
                                    result.error_text)

            if result.status == PUB_STATUS_OK:
                # Устанавливаем рандомные "следующие разрешённые времена":
                # - глобальный (в памяти) для этого экземпляра планировщика
                # - per-group (в БД) для этой конкретной группы
                now = _now()

                global_delay = _random_interval_sec(
                    settings.publication_interval_min_seconds,
                    settings.publication_interval_max_seconds,
                    HARD_MIN_PUBLICATION_INTERVAL_SEC,
                )
                self._next_pub_allowed_at = now + timedelta(seconds=global_delay)

                group_delay = _random_group_interval_sec(group)
                group_next_allowed = (now + timedelta(seconds=group_delay)).isoformat(timespec="seconds")
                db.set_group_next_allowed_at(group.id, group_next_allowed)

                self.log_cb(
                    f"[+] Опубликовано: '{ad.title}' → {group.link} "
                    f"(msg_id={result.message_id}) • "
                    f"след. в эту группу через {group_delay/60:.1f}м, "
                    f"глобально через {global_delay/60:.1f}м")
            else:
                self.log_cb(f"[!] {group.link}: {human_reason(result.status, result.error_text)}")
                self.log_cb(
                    f"[!] Ошибка публикации в {group.link}: "
                    f"{result.status} — {result.error_text[:100]}")

        except Exception as e:
            self.log_cb(f"[-] Исключение при публикации в {group.link}: {e}")
            # При неожиданной ошибке — сбрасываем клиента, чтобы следующий
            # тик пересоздал соединение (возможно, сеть упала)
            await self._disconnect_client()


def clamp_settings(settings: SchedulerSettings) -> SchedulerSettings:
    """
    Применить hard limits к настройкам — значения не могут быть
    ниже hard limits. Возвращает обновлённый объект.

    Для пар min/max: min не может быть ниже hard limit, max не может быть
    ниже min (иначе рандом развалится).
    """
    # Legacy-поле (для обратной совместимости)
    settings.publication_interval_seconds = _clamp(
        settings.publication_interval_seconds,
        HARD_MIN_PUBLICATION_INTERVAL_SEC)

    # Publication min/max
    settings.publication_interval_min_seconds = _clamp(
        settings.publication_interval_min_seconds,
        HARD_MIN_PUBLICATION_INTERVAL_SEC)
    settings.publication_interval_max_seconds = _clamp(
        settings.publication_interval_max_seconds,
        settings.publication_interval_min_seconds)

    # Join min/max (hard limit см. ads_subscriptions.py)
    from ads_subscriptions import HARD_MIN_JOIN_INTERVAL_SEC
    settings.join_interval_min_seconds = _clamp(
        settings.join_interval_min_seconds,
        HARD_MIN_JOIN_INTERVAL_SEC)
    settings.join_interval_max_seconds = _clamp(
        settings.join_interval_max_seconds,
        settings.join_interval_min_seconds)

    # Broadcast / mention / DM / group_check — min >= 1, max >= min
    for min_attr, max_attr in (
        ("broadcast_delay_min_seconds", "broadcast_delay_max_seconds"),
        ("mention_delay_min_seconds", "mention_delay_max_seconds"),
        ("dm_delay_min_seconds", "dm_delay_max_seconds"),
        ("group_check_join_delay_min_seconds", "group_check_join_delay_max_seconds"),
    ):
        min_val = _clamp(getattr(settings, min_attr), 1)
        max_val = _clamp(getattr(settings, max_attr), min_val)
        setattr(settings, min_attr, min_val)
        setattr(settings, max_attr, max_val)

    # Дневные лимиты
    settings.daily_publication_limit = min(
        settings.daily_publication_limit,
        HARD_MAX_DAILY_PUBLICATIONS)

    return settings
