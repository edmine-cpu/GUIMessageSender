import asyncio
import threading
import sys
import re
from dataclasses import dataclass
from urllib.parse import urlparse
from datetime import datetime, timedelta

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    SlowModeWaitError,
    PeerFloodError,
    UserBannedInChannelError,
    ChatWriteForbiddenError,
    ChatGuestSendForbiddenError,
    ChatAdminRequiredError,
    ChannelPrivateError,
    UserPrivacyRestrictedError,
    # Auth-специфичные — для классификации в connect()
    AuthKeyUnregisteredError,
    AuthKeyInvalidError,
    AuthKeyDuplicatedError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    PhoneNumberBannedError,
)

from models import (
    Account, SendLog,
    ACCOUNT_STATUS_NEEDS_REAUTH, ACCOUNT_STATUS_BANNED,
)
from database import Database
from config import Config, OWN_API_ID, OWN_API_HASH
from diagnostics import human_action_block_reason, human_exception, human_reason
from file_logger import log_event


@dataclass(frozen=True)
class SavedMessageTemplate:
    message: object
    text: str

    @property
    def media(self):
        return getattr(self.message, "media", None)

    @property
    def entities(self) -> list:
        return list(getattr(self.message, "entities", None) or [])

    @property
    def reply_markup(self):
        return getattr(self.message, "reply_markup", None)

    @property
    def is_rich(self) -> bool:
        return bool(self.media or self.entities or self.reply_markup)

    @property
    def is_usable(self) -> bool:
        return bool((self.text or "").strip() or self.is_rich)

    def strip(self) -> str:
        return (self.text or "").strip()


class TelegramSender:
    def __init__(self, account: Account, config: Config, db: Database):
        self.account = account
        self.config = config
        self.db = db
        self.client = self._create_client()
        self.sent_count = account.sent_today
        self._session_lock = None
        self._session_lock_acquired = False
        self.last_connect_error_code = ""
        self.last_connect_error_message = ""

    _locks_guard = threading.Lock()
    _account_locks: dict[str, threading.Lock] = {}
    _CONNECT_TIMEOUT_SECONDS = 30.0
    _SEND_TIMEOUT_SECONDS = 30.0
    _SAVED_MESSAGE_MIN_ALNUM = 2

    @classmethod
    def _get_account_lock(cls, phone: str) -> threading.Lock:
        key = (phone or "").strip()
        with cls._locks_guard:
            lock = cls._account_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._account_locks[key] = lock
            return lock

    @classmethod
    def try_acquire_account_session(cls, phone: str):
        """Try to reserve a Telegram session for any in-app subsystem."""
        lock = cls._get_account_lock(phone)
        if lock.acquire(blocking=False):
            return lock
        return None

    @classmethod
    def release_account_session(cls, lock):
        if lock is None:
            return
        try:
            lock.release()
        except Exception:
            pass

    def _try_acquire_session_lock(self) -> bool:
        if self._session_lock_acquired:
            return True
        lock = self.try_acquire_account_session(self.account.phone)
        self._session_lock = lock
        self._session_lock_acquired = lock is not None
        if lock is None:
            print(f"[!] Аккаунт занят другим процессом внутри приложения: {self.account.phone}")
        return self._session_lock_acquired

    def _release_session_lock(self):
        if self._session_lock and self._session_lock_acquired:
            self.release_account_session(self._session_lock)
        self._session_lock = None
        self._session_lock_acquired = False

    def _remember_connect_problem(self, code: str, message: str):
        self.last_connect_error_code = (code or "").strip()
        self.last_connect_error_message = (message or "").strip()

    def _record_connect_problem_without_status_change(self, code: str, message: str):
        self._remember_connect_problem(code, message)
        try:
            self.db.record_connect_problem(self.account.phone, f"{code}: {message}")
        except Exception:
            pass

    @staticmethod
    def _locked_connect_code(error_text: str) -> str:
        text = (error_text or "").lower()
        if "database is locked" in text or "database table is locked" in text:
            return "database_locked"
        if "locked" in text:
            return "session_locked"
        return ""

    @staticmethod
    def connect_problem_hint(code: str) -> str:
        code = (code or "").strip()
        if code in ("session_locked", "database_locked", "in_app_session_busy"):
            return (
                "Файл сессии или БД занят другим процессом/операцией. "
                "Остановите параллельные операции или закройте Telegram Desktop и запустите снова."
            )
        if code == "needs_reauth":
            return (
                "Сессия устарела или отозвана. "
                "Переимпортируйте TData/.session для этого аккаунта."
            )
        return ""

    def _create_client(self) -> TelegramClient:
        """Создание клиента с учётом api_id/device из БД.

        Приоритет api_id/api_hash:
          1. account.api_id/api_hash если заданы (TData → Desktop, phone-login → OWN)
          2. OWN_API_ID / OWN_API_HASH из .env (fallback)

        Device fingerprint обязан совпадать с тем, под которым был выписан
        auth_key — иначе Telegram видит mismatch. Пустые поля — это старые
        аккаунты до миграции v2, для них даём минимальные дефолты.
        """
        proxy = None
        if self.account.proxy:
            proxy = self._parse_proxy(self.account.proxy)

        api_id = self.account.api_id or OWN_API_ID
        api_hash = self.account.api_hash or OWN_API_HASH

        if not api_id or not api_hash:
            raise ValueError(
                f"Для аккаунта {self.account.phone} не задан api_id/api_hash "
                f"ни в БД, ни в .env (OWN_API_ID / OWN_API_HASH). "
                f"Переимпортируй аккаунт или заполни .env."
            )

        return TelegramClient(
            self.account.session_name,
            api_id,
            api_hash,
            proxy=proxy,
            device_model=self.account.device_model or "PC 64bit",
            system_version=self.account.system_version or "Windows 10",
            app_version=self.account.app_version or "1.0",
            lang_code=self.account.lang_code or "en",
            system_lang_code=self.account.lang_code or "en",
        )

    @staticmethod
    def _normalize_proxy_url(proxy_str: str) -> str:
        """
        Привести прокси к каноническому виду: scheme://user:pass@host:port

        Поддерживаемые входные форматы:
            socks5://user:pass@host:port           (канонический)
            socks5://user:pass:host:port           (схема + auth впереди)
            socks5://host:port:user:pass           (схема + auth сзади)
            user:pass:host:port                    (без схемы, auth впереди)
            host:port:user:pass                    (без схемы, auth сзади)
        Без схемы — по умолчанию socks5://.
        """
        proxy_str = proxy_str.strip()

        if "://" in proxy_str and "@" in proxy_str:
            return proxy_str

        if "://" in proxy_str:
            scheme, rest = proxy_str.split("://", 1)
        else:
            scheme, rest = "socks5", proxy_str

        parts = rest.split(":")
        if len(parts) != 4:
            raise ValueError(
                f"Прокси должен иметь 4 сегмента (host:port:user:pass или "
                f"user:pass:host:port), получено {len(parts)}: {proxy_str}"
            )

        a, b, c, d = parts

        def _is_port(s: str) -> bool:
            return s.isdigit() and 1 <= int(s) <= 65535

        if _is_port(b):
            host, port, user, pwd = a, b, c, d
        elif _is_port(d):
            user, pwd, host, port = a, b, c, d
        else:
            raise ValueError(f"Не могу определить порт в строке прокси: {proxy_str}")

        return f"{scheme}://{user}:{pwd}@{host}:{port}"

    @staticmethod
    def _parse_proxy(proxy_str: str) -> tuple:
        """Парсинг прокси из строки в Telethon-tuple через нормализацию к URL."""
        normalized = TelegramSender._normalize_proxy_url(proxy_str)
        parsed = urlparse(normalized)
        proxy_type = 2  # socks5 по умолчанию
        if parsed.scheme == "socks4":
            proxy_type = 1
        elif parsed.scheme == "http":
            proxy_type = 3

        return (
            proxy_type,
            parsed.hostname,
            parsed.port,
            True,
            parsed.username,
            parsed.password,
        )

    async def _raw_connect_with_retry(self) -> str:
        """Подключение с retry при locked-session, БЕЗ проверки авторизации.
        Возвращает:
          'ok'       — соединение установлено
          'network'  — сетевая / прокси / таймаут
          'error'    — всё остальное (sqlite-lock, неизвестные исключения)
        """
        import sqlite3
        for attempt in range(1, 4):
            try:
                await asyncio.wait_for(self.client.connect(), timeout=self._CONNECT_TIMEOUT_SECONDS)
                return "ok"
            except sqlite3.OperationalError as e:
                locked_code = self._locked_connect_code(str(e))
                if locked_code and attempt < 3:
                    print(f"  [!] Сессия заблокирована, попытка {attempt}/3 — жду 3с...")
                    try:
                        await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
                    except Exception:
                        pass
                    self.client = self._create_client()
                    await asyncio.sleep(3.0)
                elif locked_code:
                    print(f"  [-] Сессия/БД занята {self.account.phone}: {e}")
                    return locked_code
                else:
                    print(f"  [-] Ошибка сессии {self.account.phone}: {e}")
                    return "error"
            except (ConnectionError, OSError, TimeoutError,
                    asyncio.TimeoutError) as e:
                print(f"  [-] Сетевая ошибка {self.account.phone}: {e}")
                return "network"
            except AuthKeyDuplicatedError as e:
                print(f"  [!] Сессия недействительна {self.account.phone}: {type(e).__name__}: {e}")
                return "auth_key_duplicated"
            except Exception as e:
                print(f"  [-] Ошибка подключения {self.account.phone}: {e}")
                return "error"
        return "error"

    async def connect(self) -> bool:
        """Подключение к Telegram + проверка авторизации с классификацией ошибок.
        При неудаче записывает соответствующий статус в БД.
        Возвращает True/False для обратной совместимости с существующим кодом.
        """
        if not self._try_acquire_session_lock():
            message = "Аккаунт уже используется другой операцией внутри приложения"
            self._record_connect_problem_without_status_change("in_app_session_busy", message)
            return False
        # 1. Низкоуровневое подключение
        try:
            connect_result = await self._raw_connect_with_retry()
        except asyncio.CancelledError:
            self._release_session_lock()
            raise
        if connect_result in ("session_locked", "database_locked"):
            message = self.connect_problem_hint(connect_result)
            self._record_connect_problem_without_status_change(connect_result, message)
            self._release_session_lock()
            return False
        if connect_result == "network":
            self._remember_connect_problem("network", "connect failed")
            self.db.on_connect_network_issue(self.account.phone, "connect failed")
            self._release_session_lock()
            return False
        if connect_result == "auth_key_duplicated":
            self._remember_connect_problem("needs_reauth", "AuthKeyDuplicatedError")
            self.db.set_account_status(
                self.account.phone, ACCOUNT_STATUS_NEEDS_REAUTH,
                "AuthKeyDuplicatedError")
            self._release_session_lock()
            return False
        if connect_result != "ok":
            self._remember_connect_problem("connect_error", "low-level connect failed")
            self.db.on_connect_error(self.account.phone, "low-level connect failed")
            self._release_session_lock()
            return False

        # 2. Проверка авторизации — здесь могут вылететь auth-специфичные ошибки
        try:
            authorized = await self.client.is_user_authorized()
        except asyncio.CancelledError:
            self._release_session_lock()
            raise
        except (AuthKeyUnregisteredError, AuthKeyInvalidError,
                SessionExpiredError, SessionRevokedError) as e:
            print(f"[!] Сессия недействительна {self.account.phone}: {type(e).__name__}")
            self._remember_connect_problem("needs_reauth", type(e).__name__)
            self.db.set_account_status(
                self.account.phone, ACCOUNT_STATUS_NEEDS_REAUTH, type(e).__name__)
            self._release_session_lock()
            return False
        except (UserDeactivatedBanError, PhoneNumberBannedError,
                UserDeactivatedError) as e:
            print(f"[!] Аккаунт забанен {self.account.phone}: {type(e).__name__}")
            self._remember_connect_problem("banned", type(e).__name__)
            self.db.set_account_status(
                self.account.phone, ACCOUNT_STATUS_BANNED, type(e).__name__)
            self._release_session_lock()
            return False
        except Exception as e:
            print(f"[-] Неожиданная ошибка проверки авторизации "
                  f"{self.account.phone}: {type(e).__name__}: {e}")
            self._remember_connect_problem("connect_error", type(e).__name__)
            self.db.on_connect_error(self.account.phone, type(e).__name__)
            self._release_session_lock()
            return False

        if not authorized:
            # auth_key есть локально, но сервер ответил "логин не активен".
            # Почти всегда это означает terminate сессии с другого устройства:
            # - владелец tdata (для купленных) убил сессию
            # - "Завершить все другие сеансы" с телефона/веба
            # - анти-фрод Telegram при резкой смене IP/региона
            # - смена пароля 2FA инвалидирует все сессии кроме той, где меняли
            # Аккаунт НЕ забанен, но auth_key мёртв → нужен переимпорт tdata.
            print(f"[!] Сессия {self.account.phone} revoked "
                  f"(is_user_authorized=False). "
                  f"auth_key валиден локально, но сервер не видит активного логина. "
                  f"Вероятные причины: terminate с другого устройства, "
                  f"смена 2FA-пароля, анти-фрод при смене IP. "
                  f"Требуется переимпорт tdata.")
            self._remember_connect_problem("needs_reauth", "is_user_authorized=False")
            self.db.set_account_status(
                self.account.phone, ACCOUNT_STATUS_NEEDS_REAUTH,
                "is_user_authorized=False (session revoked)")
            self._release_session_lock()
            return False

        # 3. Успех — обнуляем счётчик fail'ов, network_issue → active
        self.db.on_connect_success(self.account.phone)
        print(f"[+] Подключён: {self.account.phone}")
        return True

    async def disconnect(self):
        """Отключение от Telegram."""
        try:
            await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
        finally:
            self._release_session_lock()

    def can_send_more(self) -> bool:
        """Быстрая проверка health без захвата слота."""
        try:
            health = next(
                (h for h in self.db.get_accounts_health() if h.get("phone") == self.account.phone),
                None,
            )
            if not health:
                return True
            return health.get("health") in ("active", "network_issue")
        except Exception:
            return True

    async def send_mention_message(
        self,
        group: str,
        text: str,
        entities: list,
        _retry: int = 0,
        min_interval_seconds: float = 2.0,
        daily_actions_limit: int = 200,
        sleep_on_flood_wait: bool = True,
    ) -> str:
        """
        Отправка сообщения с упоминаниями.
        Возвращает статус: sent / flood_wait / banned / chat_banned / no_permission / private / error / slow_mode
        """
        ok, reason, wait_s = self.db.try_acquire_action_slot(
            self.account.phone,
            "group",
            min_interval_seconds=min_interval_seconds,
            daily_actions_limit=daily_actions_limit,
        )
        if not ok:
            if reason == "min_interval" and wait_s > 0 and _retry < 3:
                await asyncio.sleep(min(wait_s, 5.0))
                return await self.send_mention_message(
                    group,
                    text,
                    entities,
                    _retry + 1,
                    min_interval_seconds=min_interval_seconds,
                    daily_actions_limit=daily_actions_limit,
                    sleep_on_flood_wait=sleep_on_flood_wait,
                )
            print(f"  [!] {self.account.phone}: действие пропущено — {human_action_block_reason(reason, wait_s)}")
            self.db.log_account_action(self.account.phone, "group", group, "skip", reason)
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="skip", error=reason)
            if reason in ("flood_wait", "daily_limit", "paused", "min_interval", "inactive", "needs_reauth", "banned"):
                return f"{reason}:{int(wait_s)}" if wait_s else reason
            return "error"

        try:
            msg = await asyncio.wait_for(
                self.client.send_message(group, text, formatting_entities=entities),
                timeout=self._SEND_TIMEOUT_SECONDS,
            )
            self.sent_count += 1
            print(f"  [+] Отправлено в {group} ({self.account.phone})")
            msg_id = int(getattr(msg, "id", 0) or 0)
            self.db.log_account_action(self.account.phone, "group", group, "sent", str(msg_id) if msg_id else "")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="sent", error=str(msg_id) if msg_id else "")
            return f"sent:{msg_id}" if msg_id else "sent"

        except FloodWaitError as e:
            print(f"  [!] {human_exception(e)}")
            wait_seconds = max(int(getattr(e, "seconds", 0) or 0), 1)
            if not sleep_on_flood_wait:
                flood_until = (datetime.now() + timedelta(seconds=wait_seconds)).isoformat()
                self.db.set_account_flood_until(self.account.phone, flood_until)
                self.db.log_account_action(self.account.phone, "group", group, "flood_wait", f"FloodWait {wait_seconds}s")
                log_event(module="sender", campaign="", account=self.account.phone, target=group,
                          action="send_group", status="flood_wait", error=f"FloodWait {wait_seconds}s")
                return f"flood_wait:{wait_seconds}"
            if _retry < 3:
                print(f"  [!] FloodWait {e.seconds}s — ожидание...")
                await asyncio.sleep(wait_seconds)
                return await self.send_mention_message(
                    group,
                    text,
                    entities,
                    _retry + 1,
                    min_interval_seconds=min_interval_seconds,
                    daily_actions_limit=daily_actions_limit,
                    sleep_on_flood_wait=sleep_on_flood_wait,
                )
            else:
                print(f"  [!] FloodWait {e.seconds}s — превышен лимит retry")
                # Ставим аккаунт на паузу до истечения flood
                flood_until = (datetime.now() + timedelta(seconds=wait_seconds)).isoformat()
                self.db.set_account_flood_until(self.account.phone, flood_until)
                self.db.log_account_action(self.account.phone, "group", group, "flood_wait", f"FloodWait {wait_seconds}s")
                log_event(module="sender", campaign="", account=self.account.phone, target=group,
                          action="send_group", status="flood_wait", error=f"FloodWait {wait_seconds}s")
                return f"flood_wait:{wait_seconds}"

        except SlowModeWaitError as e:
            wait = max(getattr(e, "seconds", 0) or 0, 1)
            print(f"  [!] {group}: {human_reason('slow_mode', wait_seconds=wait)}")
            print(f"  [!] SlowModeWait {wait}s в {group}")
            self.db.log_account_action(self.account.phone, "group", group, "slow_mode", str(wait))
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="slow_mode", error=str(wait))
            return f"slow_mode:{wait}"

        except PeerFloodError:
            print(f"  [!] {self.account.phone}: {human_reason('banned', 'PeerFloodError')}")
            print(f"  [!] PeerFloodError — деактивация {self.account.phone}")
            self.db.deactivate_account(self.account.phone)
            self.db.log_account_action(self.account.phone, "group", group, "banned", "PeerFloodError")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="banned", error="PeerFloodError")
            return "banned"

        except UserBannedInChannelError:
            print(f"  [!] {group}: {human_reason('chat_banned', 'UserBannedInChannelError')}")
            print(f"  [!] Аккаунт забанен в канале {group} (per-target, не глобальный бан)")
            self.db.log_account_action(self.account.phone, "group", group, "chat_banned", "UserBannedInChannelError")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="chat_banned", error="UserBannedInChannelError")
            return "chat_banned"

        except ChatGuestSendForbiddenError as e:
            print(f"  [!] {group}: {human_reason('need_subscription', str(e))}")
            print(f"  [!] Для отправки в {group} нужна подписка/вступление: {e}")
            self.db.log_account_action(self.account.phone, "group", group, "need_subscription", str(e)[:200])
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="need_subscription", error=str(e)[:200])
            return "need_subscription"

        except (ChatWriteForbiddenError, ChatAdminRequiredError):
            print(f"  [!] {group}: {human_reason('no_permission')}")
            print(f"  [!] Нет прав на запись в {group}")
            self.db.log_account_action(self.account.phone, "group", group, "no_permission", "")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="no_permission", error="")
            return "no_permission"

        except ChannelPrivateError:
            print(f"  [!] {group}: {human_reason('private')}")
            print(f"  [!] Группа {group} приватная / недоступна")
            self.db.log_account_action(self.account.phone, "group", group, "private", "")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="private", error="")
            return "private"

        except Exception as e:
            print(f"  [-] {group}: {human_exception(e)}")
            print(f"  [-] Ошибка в {group}: {e}")
            self.db.log_account_action(self.account.phone, "group", group, "error", str(e)[:200])
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="error", error=str(e)[:200])
            return "error"

    async def send_dm(self, user_id: int, username: str, message: str,
                      access_hash: int = 0,
                      _retry: int = 0) -> str:
        """
        Отправка личного сообщения пользователю.
        Возвращает статус: sent / private / flood_wait / banned / chat_banned / error
        """
        raw_username = (username or "").strip()
        norm_username = raw_username.lstrip("@").strip()
        access_hash = int(access_hash or 0)
        target = (f"@{norm_username}" if norm_username else str(int(user_id)))

        def _log(status: str, error_detail: str = ""):
            self.db.log_send(SendLog(
                account_phone=self.account.phone,
                target_group=str(target),
                message_text=message[:200],
                status=status,
                error_detail=error_detail,
                timestamp=datetime.now().isoformat(),
            ))

        ok, reason, wait_s = self.db.try_acquire_action_slot(
            self.account.phone, "dm", min_interval_seconds=2.0, daily_actions_limit=200
        )
        if not ok:
            if reason == "min_interval" and wait_s > 0 and _retry < 3:
                await asyncio.sleep(min(wait_s, 5.0))
                return await self.send_dm(user_id, username, message, access_hash, _retry + 1)
            print(f"  [!] {self.account.phone}: DM пропущен — {human_action_block_reason(reason, wait_s)}")
            _log("error", f"limiter:{reason}")
            self.db.log_account_action(self.account.phone, "dm", str(target), "skip", reason)
            log_event(module="sender", campaign="", account=self.account.phone, target=str(target),
                      action="send_dm", status="skip", error=reason)
            if reason in ("flood_wait", "daily_limit", "paused", "min_interval", "inactive", "needs_reauth", "banned"):
                return f"{reason}:{int(wait_s)}" if wait_s else reason
            return "error"

        try:
            if norm_username:
                await asyncio.wait_for(
                    self.client.send_message(norm_username, message),
                    timeout=self._SEND_TIMEOUT_SECONDS,
                )
            elif access_hash:
                from telethon.tl.types import InputPeerUser
                peer = InputPeerUser(int(user_id), access_hash)
                await asyncio.wait_for(
                    self.client.send_message(peer, message),
                    timeout=self._SEND_TIMEOUT_SECONDS,
                )
            else:
                await asyncio.wait_for(
                    self.client.send_message(int(user_id), message),
                    timeout=self._SEND_TIMEOUT_SECONDS,
                )
            self.sent_count += 1
            _log("sent")
            self.db.log_account_action(self.account.phone, "dm", str(target), "sent", "")
            log_event(module="sender", campaign="", account=self.account.phone, target=str(target),
                      action="send_dm", status="sent", error="")
            print(f"  [+] DM отправлено → {target} ({self.account.phone})")
            return "sent"

        except UserPrivacyRestrictedError:
            print(f"  [!] {target}: {human_reason('private', 'UserPrivacyRestrictedError')}")
            print(f"  [!] Приватность запрещает DM → {target}")
            _log("private")
            self.db.log_account_action(self.account.phone, "dm", str(target), "private", "")
            log_event(module="sender", campaign="", account=self.account.phone, target=str(target),
                      action="send_dm", status="private", error="")
            return "private"

        except FloodWaitError as e:
            print(f"  [!] {human_exception(e)}")
            if _retry < 3:
                print(f"  [!] FloodWait {e.seconds}s — ожидание...")
                await asyncio.sleep(e.seconds)
                return await self.send_dm(user_id, username, message, access_hash, _retry + 1)
            else:
                print(f"  [!] FloodWait {e.seconds}s — превышен лимит retry")
                flood_until = (datetime.now() + timedelta(seconds=e.seconds)).isoformat()
                self.db.set_account_flood_until(self.account.phone, flood_until)
                _log("flood_wait", f"FloodWait {e.seconds}s")
                self.db.log_account_action(self.account.phone, "dm", str(target), "flood_wait", f"FloodWait {e.seconds}s")
                log_event(module="sender", campaign="", account=self.account.phone, target=str(target),
                          action="send_dm", status="flood_wait", error=f"FloodWait {e.seconds}s")
                return "flood_wait"

        except PeerFloodError:
            print(f"  [!] {self.account.phone}: {human_reason('banned', 'PeerFloodError')}")
            print(f"  [!] PeerFloodError — деактивация {self.account.phone}")
            self.db.deactivate_account(self.account.phone)
            _log("banned", "PeerFloodError")
            self.db.log_account_action(self.account.phone, "dm", str(target), "banned", "PeerFloodError")
            log_event(module="sender", campaign="", account=self.account.phone, target=str(target),
                      action="send_dm", status="banned", error="PeerFloodError")
            return "banned"

        except Exception as e:
            msg = str(e)
            print(f"  [-] DM → {target}: {human_exception(e)}")
            if "Could not find the input entity for" in msg:
                print(f"  [-] {target}: {human_reason('error', 'не найден получатель; нужен username или access_hash')}")
                hint = "no_input_entity: нет access_hash/username для резолва. Переспарси аудиторию этим аккаунтом или экспортируй/импортируй с username."
                print(f"  [-] Ошибка DM → {target}: {msg}")
                _log("error", hint[:200])
                self.db.log_account_action(self.account.phone, "dm", str(target), "error", hint[:200])
                log_event(module="sender", campaign="", account=self.account.phone, target=str(target),
                          action="send_dm", status="error", error=hint[:200])
                return "error"
            print(f"  [-] Ошибка DM → {target}: {e}")
            _log("error", str(e)[:200])
            self.db.log_account_action(self.account.phone, "dm", str(target), "error", str(e)[:200])
            log_event(module="sender", campaign="", account=self.account.phone, target=str(target),
                      action="send_dm", status="error", error=str(e)[:200])
            return "error"

    async def send_broadcast_message(
        self,
        group: str,
        message: str,
        min_interval_seconds: float = 2.0,
        daily_actions_limit: int = 200,
        sleep_on_flood_wait: bool = True,
    ) -> str:
        """Отправка обычного текстового сообщения (broadcast)."""
        return await self.send_mention_message(
            group,
            message,
            entities=[],
            min_interval_seconds=min_interval_seconds,
            daily_actions_limit=daily_actions_limit,
            sleep_on_flood_wait=sleep_on_flood_wait,
        )

    async def send_saved_message(
        self,
        group: str,
        saved_message,
        _retry: int = 0,
        min_interval_seconds: float = 2.0,
        daily_actions_limit: int = 200,
        sleep_on_flood_wait: bool = True,
    ) -> str:
        """Send a Saved Messages template as a Telegram message copy."""
        if isinstance(saved_message, SavedMessageTemplate):
            source_message = saved_message.message
        else:
            source_message = saved_message

        if isinstance(source_message, str):
            return await self.send_broadcast_message(
                group,
                source_message,
                min_interval_seconds=min_interval_seconds,
                daily_actions_limit=daily_actions_limit,
                sleep_on_flood_wait=sleep_on_flood_wait,
            )

        ok, reason, wait_s = self.db.try_acquire_action_slot(
            self.account.phone,
            "group",
            min_interval_seconds=min_interval_seconds,
            daily_actions_limit=daily_actions_limit,
        )
        if not ok:
            if reason == "min_interval" and wait_s > 0 and _retry < 3:
                await asyncio.sleep(min(wait_s, 5.0))
                return await self.send_saved_message(
                    group,
                    saved_message,
                    _retry + 1,
                    min_interval_seconds=min_interval_seconds,
                    daily_actions_limit=daily_actions_limit,
                    sleep_on_flood_wait=sleep_on_flood_wait,
                )
            print(f"  [!] {self.account.phone}: action skipped - {human_action_block_reason(reason, wait_s)}")
            self.db.log_account_action(self.account.phone, "group", group, "skip", reason)
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="skip", error=reason)
            if reason in ("flood_wait", "daily_limit", "paused", "min_interval", "inactive", "needs_reauth", "banned"):
                return f"{reason}:{int(wait_s)}" if wait_s else reason
            return "error"

        try:
            msg = await asyncio.wait_for(
                self.client.send_message(group, source_message),
                timeout=self._SEND_TIMEOUT_SECONDS,
            )
            self.sent_count += 1
            print(f"  [+] Sent Saved Messages copy to {group} ({self.account.phone})")
            msg_id = int(getattr(msg, "id", 0) or 0)
            self.db.log_account_action(self.account.phone, "group", group, "sent", str(msg_id) if msg_id else "")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="sent", error=str(msg_id) if msg_id else "")
            return f"sent:{msg_id}" if msg_id else "sent"

        except FloodWaitError as e:
            print(f"  [!] {human_exception(e)}")
            wait_seconds = max(int(getattr(e, "seconds", 0) or 0), 1)
            if not sleep_on_flood_wait:
                flood_until = (datetime.now() + timedelta(seconds=wait_seconds)).isoformat()
                self.db.set_account_flood_until(self.account.phone, flood_until)
                self.db.log_account_action(self.account.phone, "group", group, "flood_wait", f"FloodWait {wait_seconds}s")
                log_event(module="sender", campaign="", account=self.account.phone, target=group,
                          action="send_group", status="flood_wait", error=f"FloodWait {wait_seconds}s")
                return f"flood_wait:{wait_seconds}"
            if _retry < 3:
                print(f"  [!] FloodWait {e.seconds}s - waiting...")
                await asyncio.sleep(wait_seconds)
                return await self.send_saved_message(
                    group,
                    saved_message,
                    _retry + 1,
                    min_interval_seconds=min_interval_seconds,
                    daily_actions_limit=daily_actions_limit,
                    sleep_on_flood_wait=sleep_on_flood_wait,
                )
            flood_until = (datetime.now() + timedelta(seconds=wait_seconds)).isoformat()
            self.db.set_account_flood_until(self.account.phone, flood_until)
            self.db.log_account_action(self.account.phone, "group", group, "flood_wait", f"FloodWait {wait_seconds}s")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="flood_wait", error=f"FloodWait {wait_seconds}s")
            return f"flood_wait:{wait_seconds}"

        except SlowModeWaitError as e:
            wait = max(getattr(e, "seconds", 0) or 0, 1)
            print(f"  [!] {group}: {human_reason('slow_mode', wait_seconds=wait)}")
            self.db.log_account_action(self.account.phone, "group", group, "slow_mode", str(wait))
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="slow_mode", error=str(wait))
            return f"slow_mode:{wait}"

        except PeerFloodError:
            print(f"  [!] {self.account.phone}: {human_reason('banned', 'PeerFloodError')}")
            self.db.deactivate_account(self.account.phone)
            self.db.log_account_action(self.account.phone, "group", group, "banned", "PeerFloodError")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="banned", error="PeerFloodError")
            return "banned"

        except UserBannedInChannelError:
            print(f"  [!] {group}: {human_reason('chat_banned', 'UserBannedInChannelError')}")
            self.db.log_account_action(self.account.phone, "group", group, "chat_banned", "UserBannedInChannelError")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="chat_banned", error="UserBannedInChannelError")
            return "chat_banned"

        except ChatGuestSendForbiddenError as e:
            print(f"  [!] {group}: {human_reason('need_subscription', str(e))}")
            self.db.log_account_action(self.account.phone, "group", group, "need_subscription", str(e)[:200])
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="need_subscription", error=str(e)[:200])
            return "need_subscription"

        except (ChatWriteForbiddenError, ChatAdminRequiredError):
            print(f"  [!] {group}: {human_reason('no_permission')}")
            self.db.log_account_action(self.account.phone, "group", group, "no_permission", "")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="no_permission", error="")
            return "no_permission"

        except ChannelPrivateError:
            print(f"  [!] {group}: {human_reason('private')}")
            self.db.log_account_action(self.account.phone, "group", group, "private", "")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="private", error="")
            return "private"

        except Exception as e:
            print(f"  [-] {group}: {human_exception(e)}")
            self.db.log_account_action(self.account.phone, "group", group, "error", str(e)[:200])
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="error", error=str(e)[:200])
            return "error"

    async def get_latest_message_id(self, chat: str) -> int:
        try:
            msgs = await self.client.get_messages(chat, limit=1)
            if not msgs:
                return 0
            msg = msgs[0]
            return int(getattr(msg, "id", 0) or 0)
        except Exception:
            return 0

    async def get_saved_message(self) -> str:
        """Получить последнее сообщение из Избранного (Saved Messages)."""
        messages = await self.get_saved_messages(limit=30)
        if messages:
            return messages[0]
        return ""

    @classmethod
    def _extract_saved_message_text(cls, message) -> str:
        for attr in ("raw_text", "message", "text"):
            value = getattr(message, attr, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    value = None
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
        return ""

    @classmethod
    def _is_saved_message_template_text(cls, text: str) -> bool:
        normalized = re.sub(r"\s+", " ", text or "").strip()
        if not normalized:
            return False
        alnum_count = sum(1 for ch in normalized if ch.isalnum())
        return alnum_count >= cls._SAVED_MESSAGE_MIN_ALNUM

    @classmethod
    def _has_saved_message_rich_payload(cls, message) -> bool:
        return bool(
            getattr(message, "media", None)
            or getattr(message, "entities", None)
            or getattr(message, "reply_markup", None)
        )

    @classmethod
    def _build_saved_message_template(cls, message) -> SavedMessageTemplate | None:
        text = cls._extract_saved_message_text(message)
        if text and cls._is_saved_message_template_text(text):
            return SavedMessageTemplate(message=message, text=text)
        if cls._has_saved_message_rich_payload(message):
            return SavedMessageTemplate(message=message, text=text)
        return None

    async def get_saved_message_templates(self, limit: int = 30) -> list[SavedMessageTemplate]:
        try:
            messages = await self.client.get_messages("me", limit=limit)
            templates = []
            skipped = 0
            for m in messages or []:
                template = self._build_saved_message_template(m)
                if template is None or not template.is_usable:
                    skipped += 1
                    continue
                templates.append(template)
            if templates:
                note = f", skipped short/service: {skipped}" if skipped else ""
                rich_count = sum(1 for item in templates if item.is_rich)
                print(
                    f"  [+] Saved Messages: {len(templates)} templates "
                    f"({rich_count} rich) ({self.account.phone}){note}"
                )
            else:
                print(f"  [!] Saved Messages is empty for {self.account.phone}")
            return templates
        except Exception as e:
            print(f"  [-] Failed to read Saved Messages ({self.account.phone}): {e}")
            return []

    async def get_saved_messages(self, limit: int = 30) -> list:
        try:
            templates = await self.get_saved_message_templates(limit=limit)
            return [item.text for item in templates if (item.text or "").strip()]
        except Exception as e:
            print(f"  [-] Ошибка получения Избранного ({self.account.phone}): {e}")
            return []
