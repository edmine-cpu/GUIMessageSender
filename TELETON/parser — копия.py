import re
import string
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional, TYPE_CHECKING, Callable, Dict, Any

from telethon import TelegramClient
from telethon.tl.types import (
    UserStatusOnline,
    UserStatusRecently,
    UserStatusLastWeek,
    UserStatusLastMonth,
    UserStatusOffline,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
)

from models import ParsedUser, MatchedPost
from database import Database
from ai_filter import AIProviderError

if TYPE_CHECKING:
    from ai_filter import AIFilter


# Русские буквы для aggressive-парсинга
RU_LETTERS = "абвгдежзиклмнопрстуфхцчшщэюя"


def _user_status_to_str(status) -> str:
    """Маппинг user.status в строку"""
    if isinstance(status, UserStatusOnline):
        return "online"
    if isinstance(status, UserStatusRecently):
        return "recently"
    if isinstance(status, UserStatusLastWeek):
        return "last_week"
    if isinstance(status, UserStatusLastMonth):
        return "last_month"
    if isinstance(status, UserStatusOffline):
        return "offline"
    return "unknown"


class GroupParser:
    def __init__(
        self,
        client: TelegramClient,
        db: Database,
        stop_requested: Optional[Callable[[], bool]] = None,
        progress_cb: Callable[[str], None] = print,
        progress_state_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.client = client
        self.db = db
        self.stop_requested = stop_requested
        self.progress_cb = progress_cb
        self.progress_state_cb = progress_state_cb
        self.last_content_stats: Dict[str, Any] = {}

    def _should_stop(self, group: str, processed_count: int, stage: str) -> bool:
        if self.stop_requested and self.stop_requested():
            self.progress_cb(
                f"[~] Остановка парсинга подтверждена: {stage} | {group} | уже собрано: {processed_count}"
            )
            return True
        return False

    async def parse_group(self, group: str, aggressive: bool = False, group_source_override: str = "") -> int:
        """
        Парсинг участников группы.
        aggressive=True — поиск по буквам алфавита для обхода лимита 200.
        Возвращает количество сохранённых пользователей.
        """
        users_map = {}

        group_source = (group_source_override or "").strip() or group

        if aggressive:
            # Поиск по каждой букве латинского и русского алфавита
            letters = list(string.ascii_lowercase) + list(RU_LETTERS)
            for letter in letters:
                if self._should_stop(group, len(users_map), f"aggressive:{letter}"):
                    break
                try:
                    async for user in self.client.iter_participants(group, search=letter):
                        if self._should_stop(group, len(users_map), f"aggressive:{letter}"):
                            break
                        if not user.bot and not user.deleted:
                            users_map[user.id] = user
                            if self.progress_state_cb and (len(users_map) % 200 == 0):
                                self.progress_state_cb({
                                    "kind": "regular",
                                    "group": group,
                                    "saved": len(users_map),
                                })
                except Exception as e:
                    print(f"  [-] Ошибка поиска по '{letter}': {e}")
        else:
            try:
                async for user in self.client.iter_participants(group):
                    if self._should_stop(group, len(users_map), "participants"):
                        break
                    if not user.bot and not user.deleted:
                        users_map[user.id] = user
                        if self.progress_state_cb and (len(users_map) % 200 == 0):
                            self.progress_state_cb({
                                "kind": "regular",
                                "group": group,
                                "saved": len(users_map),
                            })
            except Exception as e:
                print(f"  [-] Ошибка парсинга {group}: {e}")
                return 0

        # Преобразование в ParsedUser
        parsed_users = [
            ParsedUser(
                user_id=u.id,
                username=u.username,
                first_name=u.first_name,
                last_name=u.last_name,
                phone=u.phone,
                access_hash=int(getattr(u, "access_hash", 0) or 0),
                group_source=group_source,
                status=_user_status_to_str(u.status),
                is_bot=u.bot or False,
            )
            for u in users_map.values()
        ]

        self.db.save_parsed_users(parsed_users)
        print(f"[+] Спарсено {len(parsed_users)} пользователей из {group}")
        return len(parsed_users)

    async def parse_commenters(self, channel: str, limit_posts: int = 50, group_source_override: str = "") -> int:
        """
        Парсинг комментаторов канала.
        Итерация по последним постам, сбор уникальных пользователей из комментариев.
        """
        users_map = {}
        group_source = (group_source_override or "").strip() or channel

        try:
            async for msg in self.client.iter_messages(channel, limit=limit_posts):
                if self._should_stop(channel, len(users_map), f"commenters:post:{getattr(msg, 'id', '?')}"):
                    break
                try:
                    async for reply in self.client.iter_messages(channel, reply_to=msg.id):
                        if self._should_stop(channel, len(users_map), f"commenters:reply_to:{msg.id}"):
                            break
                        if reply.sender and hasattr(reply.sender, "id"):
                            sender = reply.sender
                            if not getattr(sender, "bot", False) and not getattr(sender, "deleted", False):
                                users_map[sender.id] = sender
                                if self.progress_state_cb and (len(users_map) % 100 == 0):
                                    self.progress_state_cb({
                                        "kind": "regular",
                                        "group": channel,
                                        "saved": len(users_map),
                                    })
                except Exception:
                    # Пост без комментариев или ошибка — пропускаем
                    continue

        except Exception as e:
            print(f"  [-] Ошибка парсинга комментариев {channel}: {e}")
            return 0

        parsed_users = [
            ParsedUser(
                user_id=u.id,
                username=u.username,
                first_name=getattr(u, "first_name", None),
                last_name=getattr(u, "last_name", None),
                phone=getattr(u, "phone", None),
                access_hash=int(getattr(u, "access_hash", 0) or 0),
                group_source=group_source,
                status=_user_status_to_str(getattr(u, "status", None)),
                is_bot=getattr(u, "bot", False) or False,
            )
            for u in users_map.values()
        ]

        self.db.save_parsed_users(parsed_users)
        print(f"[+] Спарсено {len(parsed_users)} комментаторов из {channel}")
        return len(parsed_users)

    def _check_keyword_match(
        self,
        text: str,
        include_keywords: Optional[List[str]] = None,
        exclude_keywords: Optional[List[str]] = None,
        use_exact_match: bool = False,
        use_regex: bool = False,
    ) -> tuple[bool, str]:
        """
        Проверить, совпадает ли текст с заданными условиями ключевых слов.
        
        Возвращает (matched, match_info)
        """
        if not include_keywords:
            include_keywords = []
        if not exclude_keywords:
            exclude_keywords = []
            
        text_lower = text.lower()
        
        # Проверка исключающих ключевых слов (если есть)
        for kw in exclude_keywords:
            kw_lower = kw.lower()
            if use_exact_match:
                pattern = rf'\b{re.escape(kw_lower)}\b'
                if re.search(pattern, text_lower):
                    return False, f"Исключено: {kw}"
            elif use_regex:
                try:
                    if re.search(kw, text, re.IGNORECASE):
                        return False, f"Исключено (regex): {kw}"
                except re.error:
                    pass
            else:
                if kw_lower in text_lower:
                    return False, f"Исключено: {kw}"
        
        # Проверка включающих ключевых слов
        matched_keywords = []
        for kw in include_keywords:
            kw_lower = kw.lower()
            if use_exact_match:
                pattern = rf'\b{re.escape(kw_lower)}\b'
                if re.search(pattern, text_lower):
                    matched_keywords.append(kw)
            elif use_regex:
                try:
                    if re.search(kw, text, re.IGNORECASE):
                        matched_keywords.append(kw)
                except re.error:
                    pass
            else:
                if kw_lower in text_lower:
                    matched_keywords.append(kw)
        
        if matched_keywords:
            match_type = "Точное" if use_exact_match else ("Regex" if use_regex else "Обычное")
            return True, f"{match_type}: {', '.join(matched_keywords)}"
        
        return False, ""

    async def parse_by_content(
        self,
        group: str,
        audience_name: str = "",
        mode: str = "keywords",
        keywords: Optional[List[str]] = None,
        exclude_keywords: Optional[List[str]] = None,
        use_exact_match: bool = False,
        use_regex: bool = False,
        ai_criteria: str = "",
        ai_filter: Optional['AIFilter'] = None,
        limit_messages: Optional[int] = 500,
        since_dt: Optional[datetime] = None,
        min_text_chars: int = 0,
        max_text_chars: int = 0,
        chat_index: int = 1,
        chats_total: int = 1,
    ) -> int:
        """
        Смарт-парсинг: чтение сообщений и фильтрация авторов по содержимому.
        mode="keywords" — поиск по ключевым словам.
        mode="ai" — анализ через OpenAI.
        Возвращает количество найденных совпадений.
        """
        found_count = 0
        audience_key = (audience_name or "").strip() or group
        # Для AI-батчинга: накапливаем посты
        ai_batch = []
        ai_batch_meta = []  # (msg, sender) для каждого поста в батче
        AI_BATCH_SIZE = 10
        scanned_count = 0
        skipped_short = 0
        skipped_long = 0
        skipped_excluded = 0
        msg_index = 0
        error_text = ""
        stopped_by_date = False
        min_text_chars = max(0, int(min_text_chars or 0))
        max_text_chars = max(0, int(max_text_chars or 0))

        if since_dt:
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            else:
                since_dt = since_dt.astimezone(timezone.utc)

        self.last_content_stats = {
            "group": group,
            "message_index": 0,
            "messages_total": int(limit_messages or 0),
            "scanned": 0,
            "skipped_short": 0,
            "skipped_long": 0,
            "skipped_excluded": 0,
            "found": 0,
            "error": "",
            "since_dt": since_dt.isoformat() if since_dt else "",
            "stopped_by_date": False,
        }

        if since_dt:
            limit_label = "без" if limit_messages is None else str(limit_messages)
            print(f"[~] Чтение сообщений из {group} (с {since_dt.isoformat()}, лимит: {limit_label})...")
        else:
            print(f"[~] Чтение сообщений из {group} (лимит: {limit_messages})...")
        if mode == "keywords":
            print("[i] Активные ключи: " + (", ".join(keywords or []) or "нет"))
        if exclude_keywords:
            print("[i] Исключающие слова: " + ", ".join(exclude_keywords))
        if min_text_chars or max_text_chars:
            print(f"[i] Фильтр длины текста: min={min_text_chars or '—'}, max={max_text_chars or '—'} символов")

        try:
            async for msg in self.client.iter_messages(group, limit=limit_messages):
                msg_index += 1
                if since_dt and getattr(msg, "date", None):
                    msg_dt = msg.date
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                    else:
                        msg_dt = msg_dt.astimezone(timezone.utc)
                    if msg_dt < since_dt:
                        stopped_by_date = True
                        print(
                            f"[i] Достигнута граница периода: "
                            f"{msg_dt.isoformat()} < {since_dt.isoformat()}"
                        )
                        break
                if self._should_stop(group, found_count, f"smart:{mode}:message:{getattr(msg, 'id', '?')}"):
                    break
                # Пропускаем пустые, без отправителя, ботов, удалённых
                if not msg.text or not msg.sender:
                    continue
                sender = msg.sender
                if getattr(sender, "bot", False) or getattr(sender, "deleted", False):
                    continue

                scanned_count += 1
                text_clean = re.sub(r"\s+", " ", msg.text or "").strip()
                text_len = len(text_clean)
                if min_text_chars and text_len < min_text_chars:
                    skipped_short += 1
                    continue
                if max_text_chars and text_len > max_text_chars:
                    skipped_long += 1
                    continue
                if exclude_keywords:
                    excluded, _excluded_str = self._check_keyword_match(
                        text_clean,
                        include_keywords=exclude_keywords,
                        exclude_keywords=None,
                        use_exact_match=use_exact_match,
                        use_regex=use_regex,
                    )
                    if excluded:
                        skipped_excluded += 1
                        continue

                username = getattr(sender, "username", None)
                sender_id = sender.id
                if self.progress_state_cb and (msg_index % 10 == 0):
                    self.progress_state_cb({
                        "kind": "smart",
                        "group": group,
                        "chat_index": chat_index,
                        "chats_total": chats_total,
                        "message_index": msg_index,
                        "messages_total": int(limit_messages or 0),
                        "found": found_count,
                        "saved": found_count,
                    })

                if mode == "keywords" and keywords:
                    matched, matched_str = self._check_keyword_match(
                        text_clean,
                        include_keywords=keywords,
                        exclude_keywords=None,
                        use_exact_match=use_exact_match,
                        use_regex=use_regex,
                    )
                    if matched:
                        print(f"[+] Совпадение: @{username or sender_id} — {matched_str}")

                        # Сохранить автора в parsed_users
                        parsed_user = ParsedUser(
                            user_id=sender_id,
                            username=username,
                            first_name=getattr(sender, "first_name", None),
                            last_name=getattr(sender, "last_name", None),
                            phone=getattr(sender, "phone", None),
                            access_hash=int(getattr(sender, "access_hash", 0) or 0),
                            group_source=audience_key,
                            status=_user_status_to_str(getattr(sender, "status", None)),
                            is_bot=False,
                        )
                        self.db.save_parsed_users([parsed_user])

                        # Сохранить пост в matched_posts
                        msg_date = ""
                        try:
                            if getattr(msg, "date", None):
                                msg_date = msg.date.isoformat()
                        except Exception:
                            msg_date = ""
                        message_link = ""
                        try:
                            g = (group or "").strip()
                            if g.startswith("@"):
                                username_group = g[1:]
                                if username_group:
                                    message_link = f"https://t.me/{username_group}/{msg.id}"
                            elif "t.me/" in g:
                                tail = g.split("t.me/", 1)[1].strip("/")
                                username_group = tail.split("/", 1)[0]
                                if username_group and not username_group.startswith("+"):
                                    message_link = f"https://t.me/{username_group}/{msg.id}"
                        except Exception:
                            message_link = ""
                        post = MatchedPost(
                            message_id=msg.id,
                            group_source=group,
                            origin_group=audience_key,
                            message_date=msg_date,
                            message_link=message_link or str(msg.id),
                            sender_id=sender_id,
                            sender_username=username,
                            sender_access_hash=int(getattr(sender, "access_hash", 0) or 0),
                            message_text=text_clean[:2000],
                            match_mode="keywords",
                            matched_keywords=matched_str,
                            ai_reason="",
                            matched_at=datetime.now().isoformat(),
                        )
                        self.db.save_matched_post(post)
                        found_count += 1
                        if self.progress_state_cb:
                            self.progress_state_cb({
                                "kind": "smart",
                                "group": group,
                                "chat_index": chat_index,
                                "chats_total": chats_total,
                                "message_index": msg_index,
                                "messages_total": int(limit_messages or 0),
                                "found": found_count,
                                "saved": found_count,
                            })

                elif mode == "ai" and ai_filter:
                    ai_batch.append({"id": msg.id, "text": text_clean[:1000]})
                    ai_batch_meta.append((msg, sender))

                    if len(ai_batch) >= AI_BATCH_SIZE:
                        if self._should_stop(group, found_count, f"smart:{mode}:before_ai_batch"):
                            break
                        found_count += await self._process_ai_batch(
                            ai_batch, ai_batch_meta, ai_filter, ai_criteria, origin_group=group, audience_key=audience_key
                        )
                        ai_batch = []
                        ai_batch_meta = []

            # Обработать остаток AI-батча
            if mode == "ai" and ai_filter and ai_batch and not self._should_stop(group, found_count, f"smart:{mode}:tail_batch"):
                found_count += await self._process_ai_batch(
                    ai_batch, ai_batch_meta, ai_filter, ai_criteria, origin_group=group, audience_key=audience_key
                )

        except FloodWaitError as e:
            error_text = f"FloodWait {e.seconds}s"
            print(f"  [!] FloodWait {e.seconds}s при парсинге {group} — ожидание...")
            await asyncio.sleep(e.seconds)
            # Итератор сломан — возвращаем то, что успели насобирать
        except AIProviderError as e:
            error_text = str(e)
            print(f"  [!] AI ошибка: {e}")
            print("  [!] Подсказка: проверь провайдера/ключ/прокси/квоту в Настройках")
        except Exception as e:
            error_text = str(e)
            print(f"  [-] Ошибка смарт-парсинга {group}: {e}")

        self.last_content_stats = {
            "group": group,
            "message_index": msg_index,
            "messages_total": int(limit_messages or 0),
            "scanned": scanned_count,
            "skipped_short": skipped_short,
            "skipped_long": skipped_long,
            "skipped_excluded": skipped_excluded,
            "found": found_count,
            "error": error_text,
            "since_dt": since_dt.isoformat() if since_dt else "",
            "stopped_by_date": stopped_by_date,
        }
        print(
            f"[i] Фильтр: прочитано={msg_index}, проверено={scanned_count}, "
            f"коротких={skipped_short}, длинных={skipped_long}, исключено={skipped_excluded}"
        )
        print(f"\n=== Найдено совпадений: {found_count} ===")
        return found_count

    async def _process_ai_batch(
        self,
        batch: List[dict],
        meta: list,
        ai_filter: 'AIFilter',
        criteria: str,
        origin_group: str,
        audience_key: str,
    ) -> int:
        """Обработать батч постов через AI фильтр"""
        found = 0
        provider = getattr(ai_filter, "provider", "") or "ai"
        print(f"[~] AI ({provider}): проверка {len(batch)} постов...")
        results = await asyncio.to_thread(ai_filter.check_posts_batch, batch, criteria)

        for result, (msg, sender) in zip(results, meta):
            if not result["match"]:
                continue

            username = getattr(sender, "username", None)
            sender_id = sender.id
            reason = result["reason"]
            print(f"[+] AI совпадение: @{username or sender_id} — {reason[:80]}")

            parsed_user = ParsedUser(
                user_id=sender_id,
                username=username,
                first_name=getattr(sender, "first_name", None),
                last_name=getattr(sender, "last_name", None),
                phone=getattr(sender, "phone", None),
                access_hash=int(getattr(sender, "access_hash", 0) or 0),
                group_source=audience_key,
                status=_user_status_to_str(getattr(sender, "status", None)),
                is_bot=False,
            )
            self.db.save_parsed_users([parsed_user])

            msg_date = ""
            try:
                if getattr(msg, "date", None):
                    msg_date = msg.date.isoformat()
            except Exception:
                msg_date = ""
            message_link = ""
            try:
                g = (origin_group or "").strip()
                if g.startswith("@"):
                    username_group = g[1:]
                    if username_group:
                        message_link = f"https://t.me/{username_group}/{msg.id}"
                elif "t.me/" in g:
                    tail = g.split("t.me/", 1)[1].strip("/")
                    username_group = tail.split("/", 1)[0]
                    if username_group and not username_group.startswith("+"):
                        message_link = f"https://t.me/{username_group}/{msg.id}"
            except Exception:
                message_link = ""

            post = MatchedPost(
                message_id=msg.id,
                group_source=origin_group,
                origin_group=audience_key,
                message_date=msg_date,
                message_link=message_link or str(msg.id),
                sender_id=sender_id,
                sender_username=username,
                sender_access_hash=int(getattr(sender, "access_hash", 0) or 0),
                message_text=msg.text[:2000],
                match_mode="ai",
                matched_keywords="",
                ai_reason=reason,
                matched_at=datetime.now().isoformat(),
            )
            self.db.save_matched_post(post)
            found += 1

        return found

# --- Вступление в группу ---

async def join_group(client: TelegramClient, group: str) -> str:
    """
    Вступление в группу или канал с поддержкой закрытых групп.

    Принимает username (@group), t.me-ссылку или invite-hash.
    Возвращает статус:
      joined       — успешно вступили
      join_request — отправлена заявка (закрытая группа)
      already      — уже участник
      expired      — ссылка истекла
      invalid      — неверная ссылка
      private      — группа недоступна
      flood_wait   — FloodWait, нужна пауза
      error        — прочая ошибка
    """
    # Извлечь invite hash из ссылки вида t.me/+HASH или t.me/joinchat/HASH
    invite_hash = None
    m = re.search(r"t\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)", group)
    if m:
        invite_hash = m.group(1)

    try:
        if invite_hash:
            result = await client(ImportChatInviteRequest(invite_hash))
            chats = getattr(result, "chats", [])
            if chats:
                print(f"  [+] Вступил по ссылке: {group}")
                return "joined"
            else:
                print(f"  [~] Заявка отправлена: {group}")
                return "join_request"
        else:
            await client(JoinChannelRequest(group))
            print(f"  [+] Вступил: {group}")
            return "joined"

    except UserAlreadyParticipantError:
        print(f"  [=] Уже участник: {group}")
        return "already"

    except InviteHashExpiredError:
        print(f"  [!] Ссылка истекла: {group}")
        return "expired"

    except InviteHashInvalidError:
        print(f"  [!] Неверная ссылка: {group}")
        return "invalid"

    except ChannelPrivateError:
        print(f"  [!] Группа недоступна: {group}")
        return "private"

    except FloodWaitError as e:
        print(f"  [!] FloodWait {e.seconds}s при вступлении в {group}")
        return "flood_wait"

    except Exception as e:
        err = str(e).lower()
        if "request to join" in err or "join_request" in err:
            print(f"  [~] Заявка отправлена: {group}")
            return "join_request"
        print(f"  [-] Ошибка вступления в {group}: {e}")
        return "error"


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


async def inspect_chat_access(client: TelegramClient, group: str) -> tuple[str, str, str]:
    """
    Read-only проверка доступа к чату для Dry Run.

    Ничего не отправляет и не вступает в чат. Используется там, где нужно
    понять, валиден ли target, не создавая побочных эффектов.
    """
    invite_hash = None
    m = re.search(r"t\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)", group)
    if m:
        invite_hash = m.group(1)

    try:
        if invite_hash:
            result = await client(CheckChatInviteRequest(invite_hash))
            result_name = type(result).__name__
            if result_name == "ChatInviteAlready":
                print(f"  [DRY] Уже есть доступ к invite-чату: {group}")
                return "ok", "dry_run_already", ""
            print(f"  [DRY] Invite-ссылка валидна: {group} (в live была бы попытка вступления)")
            return "ok", "dry_run_would_join", ""

        await client.get_entity(group)
        print(f"  [DRY] Чат резолвится без вступления: {group}")
        return "ok", "dry_run_resolved", ""

    except (InviteHashExpiredError, InviteHashInvalidError):
        print(f"  [DRY] Invite-ссылка недействительна: {group}")
        return "error", "invalid", _iso(datetime.now() + timedelta(days=7))
    except UsernameNotOccupiedError:
        print(f"  [DRY] Username не найден: {group}")
        return "error", "invalid", _iso(datetime.now() + timedelta(days=7))
    except ChannelPrivateError:
        print(f"  [DRY] Чат приватный / недоступен: {group}")
        return "error", "private", _iso(datetime.now() + timedelta(days=7))
    except FloodWaitError as e:
        print(f"  [DRY] FloodWait {e.seconds}s при проверке доступа к {group}")
        return "waiting", "flood_wait", _iso(datetime.now() + timedelta(minutes=30))
    except Exception as e:
        print(f"  [DRY] Не удалось проверить доступ к {group}: {e}")
        return "error", "error", _iso(datetime.now() + timedelta(hours=1))


async def ensure_chat_access(client: TelegramClient, group: str, dry_run: bool = False) -> tuple[str, str, str]:
    """
    Проверить доступ к чату для отправки: при необходимости попытаться вступить.

    Возвращает (decision, reason, retry_after_iso):
      decision:
        ok      — можно пробовать отправлять
        waiting — отложить до retry_after_iso (flood / заявка на вступление)
        error   — недоступно (приватный / битая ссылка / и т.п.)
    """
    if dry_run:
        return await inspect_chat_access(client, group)

    join_result = await join_group(client, group)

    if join_result in ("joined", "already"):
        return "ok", join_result, ""

    now = datetime.now()
    if join_result == "join_request":
        return "waiting", "join_request", _iso(now + timedelta(hours=6))
    if join_result == "flood_wait":
        return "waiting", "flood_wait", _iso(now + timedelta(minutes=30))
    if join_result in ("expired", "invalid", "private"):
        return "error", join_result, _iso(now + timedelta(days=7))

    return "error", join_result or "error", _iso(now + timedelta(hours=1))
