import asyncio
import threading
import sys
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
from file_logger import log_event


class TelegramSender:
    def __init__(self, account: Account, config: Config, db: Database):
        self.account = account
        self.config = config
        self.db = db
        self.client = self._create_client()
        self.sent_count = account.sent_today
        self._session_lock = None
        self._session_lock_acquired = False

    _locks_guard = threading.Lock()
    _account_locks: dict[str, threading.Lock] = {}
    _CONNECT_TIMEOUT_SECONDS = 30.0
    _SEND_TIMEOUT_SECONDS = 30.0

    @classmethod
    def _get_account_lock(cls, phone: str) -> threading.Lock:
        key = (phone or "").strip()
        with cls._locks_guard:
            lock = cls._account_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._account_locks[key] = lock
            return lock

    def _try_acquire_session_lock(self) -> bool:
        if self._session_lock_acquired:
            return True
        lock = self._get_account_lock(self.account.phone)
        ok = lock.acquire(blocking=False)
        self._session_lock = lock
        self._session_lock_acquired = bool(ok)
        if not ok:
            print(f"[!] Аккаунт занят другим процессом внутри приложения: {self.account.phone}")
        return ok

    def _release_session_lock(self):
        if self._session_lock and self._session_lock_acquired:
            try:
                self._session_lock.release()
            except Exception:
                pass
        self._session_lock = None
        self._session_lock_acquired = False

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
                if "locked" in str(e).lower() and attempt < 3:
                    print(f"  [!] Сессия заблокирована, попытка {attempt}/3 — жду 3с...")
                    try:
                        await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
                    except Exception:
                        pass
                    self.client = self._create_client()
                    await asyncio.sleep(3.0)
                else:
                    print(f"  [-] Ошибка сессии {self.account.phone}: {e}")
                    return "error"
            except (ConnectionError, OSError, TimeoutError,
                    asyncio.TimeoutError) as e:
                print(f"  [-] Сетевая ошибка {self.account.phone}: {e}")
                return "network"
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
            return False
        # 1. Низкоуровневое подключение
        connect_result = await self._raw_connect_with_retry()
        if connect_result == "network":
            self.db.on_connect_network_issue(self.account.phone, "connect failed")
            self._release_session_lock()
            return False
        if connect_result != "ok":
            self.db.on_connect_error(self.account.phone, "low-level connect failed")
            self._release_session_lock()
            return False

        # 2. Проверка авторизации — здесь могут вылететь auth-специфичные ошибки
        try:
            authorized = await self.client.is_user_authorized()
        except (AuthKeyUnregisteredError, AuthKeyInvalidError,
                SessionExpiredError, SessionRevokedError) as e:
            print(f"[!] Сессия недействительна {self.account.phone}: {type(e).__name__}")
            self.db.set_account_status(
                self.account.phone, ACCOUNT_STATUS_NEEDS_REAUTH, type(e).__name__)
            self._release_session_lock()
            return False
        except (UserDeactivatedBanError, PhoneNumberBannedError,
                UserDeactivatedError) as e:
            print(f"[!] Аккаунт забанен {self.account.phone}: {type(e).__name__}")
            self.db.set_account_status(
                self.account.phone, ACCOUNT_STATUS_BANNED, type(e).__name__)
            self._release_session_lock()
            return False
        except Exception as e:
            print(f"[-] Неожиданная ошибка проверки авторизации "
                  f"{self.account.phone}: {type(e).__name__}: {e}")
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

    async def send_mention_message(self, group: str, text: str, entities: list,
                                   _retry: int = 0) -> str:
        """
        Отправка сообщения с упоминаниями.
        Возвращает статус: sent / flood_wait / banned / chat_banned / no_permission / private / error / slow_mode
        """
        ok, reason, wait_s = self.db.try_acquire_action_slot(
            self.account.phone, "group", min_interval_seconds=2.0, daily_actions_limit=200
        )
        if not ok:
            if reason == "min_interval" and wait_s > 0 and _retry < 3:
                await asyncio.sleep(min(wait_s, 5.0))
                return await self.send_mention_message(group, text, entities, _retry + 1)
            self.db.log_account_action(self.account.phone, "group", group, "skip", reason)
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="skip", error=reason)
            return "flood_wait" if reason == "flood_wait" else "error"

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
            if _retry < 3:
                print(f"  [!] FloodWait {e.seconds}s — ожидание...")
                await asyncio.sleep(e.seconds)
                return await self.send_mention_message(group, text, entities, _retry + 1)
            else:
                print(f"  [!] FloodWait {e.seconds}s — превышен лимит retry")
                # Ставим аккаунт на паузу до истечения flood
                flood_until = (datetime.now() + timedelta(seconds=e.seconds)).isoformat()
                self.db.set_account_flood_until(self.account.phone, flood_until)
                self.db.log_account_action(self.account.phone, "group", group, "flood_wait", f"FloodWait {e.seconds}s")
                log_event(module="sender", campaign="", account=self.account.phone, target=group,
                          action="send_group", status="flood_wait", error=f"FloodWait {e.seconds}s")
                return "flood_wait"

        except SlowModeWaitError as e:
            wait = max(getattr(e, "seconds", 0) or 0, 1)
            print(f"  [!] SlowModeWait {wait}s в {group}")
            self.db.log_account_action(self.account.phone, "group", group, "slow_mode", str(wait))
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="slow_mode", error=str(wait))
            return f"slow_mode:{wait}"

        except PeerFloodError:
            print(f"  [!] PeerFloodError — деактивация {self.account.phone}")
            self.db.deactivate_account(self.account.phone)
            self.db.log_account_action(self.account.phone, "group", group, "banned", "PeerFloodError")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="banned", error="PeerFloodError")
            return "banned"

        except UserBannedInChannelError:
            print(f"  [!] Аккаунт забанен в канале {group} (per-target, не глобальный бан)")
            self.db.log_account_action(self.account.phone, "group", group, "chat_banned", "UserBannedInChannelError")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="chat_banned", error="UserBannedInChannelError")
            return "chat_banned"

        except ChatGuestSendForbiddenError as e:
            print(f"  [!] Для отправки в {group} нужна подписка/вступление: {e}")
            self.db.log_account_action(self.account.phone, "group", group, "need_subscription", str(e)[:200])
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="need_subscription", error=str(e)[:200])
            return "need_subscription"

        except (ChatWriteForbiddenError, ChatAdminRequiredError):
            print(f"  [!] Нет прав на запись в {group}")
            self.db.log_account_action(self.account.phone, "group", group, "no_permission", "")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="no_permission", error="")
            return "no_permission"

        except ChannelPrivateError:
            print(f"  [!] Группа {group} приватная / недоступна")
            self.db.log_account_action(self.account.phone, "group", group, "private", "")
            log_event(module="sender", campaign="", account=self.account.phone, target=group,
                      action="send_group", status="private", error="")
            return "private"

        except Exception as e:
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
            _log("error", f"limiter:{reason}")
            self.db.log_account_action(self.account.phone, "dm", str(target), "skip", reason)
            log_event(module="sender", campaign="", account=self.account.phone, target=str(target),
                      action="send_dm", status="skip", error=reason)
            return "flood_wait" if reason == "flood_wait" else "error"

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
            print(f"  [!] Приватность запрещает DM → {target}")
            _log("private")
            self.db.log_account_action(self.account.phone, "dm", str(target), "private", "")
            log_event(module="sender", campaign="", account=self.account.phone, target=str(target),
                      action="send_dm", status="private", error="")
            return "private"

        except FloodWaitError as e:
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
            print(f"  [!] PeerFloodError — деактивация {self.account.phone}")
            self.db.deactivate_account(self.account.phone)
            _log("banned", "PeerFloodError")
            self.db.log_account_action(self.account.phone, "dm", str(target), "banned", "PeerFloodError")
            log_event(module="sender", campaign="", account=self.account.phone, target=str(target),
                      action="send_dm", status="banned", error="PeerFloodError")
            return "banned"

        except Exception as e:
            msg = str(e)
            if "Could not find the input entity for" in msg:
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

    async def send_broadcast_message(self, group: str, message: str) -> str:
        """Отправка обычного текстового сообщения (broadcast)."""
        return await self.send_mention_message(group, message, entities=[])

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
        try:
            messages = await self.client.get_messages("me", limit=1)
            if messages and messages[0].text:
                print(f"  [+] Текст из Избранного ({self.account.phone}): {messages[0].text[:60]}...")
                return messages[0].text
            print(f"  [!] Избранное пусто у {self.account.phone}")
            return ""
        except Exception as e:
            print(f"  [-] Ошибка получения Избранного ({self.account.phone}): {e}")
            return ""

    async def get_saved_messages(self, limit: int = 30) -> list:
        try:
            messages = await self.client.get_messages("me", limit=limit)
            texts = []
            for m in messages or []:
                t = getattr(m, "text", None)
                if not t:
                    continue
                t = t.strip()
                if t:
                    texts.append(t)
            if texts:
                print(f"  [+] Избранное: {len(texts)} текстов ({self.account.phone})")
            else:
                print(f"  [!] Избранное пусто у {self.account.phone}")
            return texts
        except Exception as e:
            print(f"  [-] Ошибка получения Избранного ({self.account.phone}): {e}")
            return []
