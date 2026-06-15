"""
ads_models.py — dataclass-модели для планировщика объявлений.

Новый функциональный блок Teleton для публикации объявлений в группы
объявлений от одного аккаунта. Не пересекается с существующими моделями
(Account/Task/SendLog и т.д.) — отдельная подсистема.
"""

from dataclasses import dataclass
from typing import Optional


# --- Статусы ---

# Статус группы в системе
GROUP_STATUS_ACTIVE = "active"           # публикуем
GROUP_STATUS_PAUSED = "paused"           # на паузе вручную
GROUP_STATUS_BANNED = "banned"           # забанены насовсем
GROUP_STATUS_UNAVAILABLE = "unavailable" # группа удалена / приватная

# Статус членства в группе
JOIN_STATUS_UNKNOWN = "unknown"
JOIN_STATUS_MEMBER = "member"
JOIN_STATUS_NOT_MEMBER = "not_member"
JOIN_STATUS_BANNED = "banned"

# Статус публикации
PUB_STATUS_OK = "ok"
PUB_STATUS_FLOOD_WAIT = "flood_wait"
PUB_STATUS_SLOW_MODE = "slow_mode"
PUB_STATUS_FORBIDDEN = "forbidden"
PUB_STATUS_BANNED = "banned"
PUB_STATUS_NOT_MEMBER = "not_member"
PUB_STATUS_ERROR = "error"


@dataclass
class Ad:
    """Объявление — базовый текст + медиа, публикуется в N групп"""
    id: Optional[int] = None
    title: str = ""                    # внутреннее название, для твоего удобства
    text_base: str = ""                # базовый текст объявления
    media_path: str = ""               # путь к фото/видео, пусто если без медиа
    category: str = ""                 # категория/теги через запятую (для матчинга с группами)
    active: bool = True                # 1 — публикуется, 0 — на паузе
    account_phone: str = ""            # с какого аккаунта публикуем
    created_at: str = ""
    updated_at: str = ""


@dataclass
class GroupTarget:
    """Группа-назначение — куда публикуем"""
    id: Optional[int] = None
    link: str = ""                     # @username или t.me/... или invite-ссылка
    title: str = ""                    # отображаемое название
    category: str = ""                 # для матчинга с объявлениями
    interval_minutes: int = 60         # минимум между публикациями в эту группу (мин)
    interval_minutes_max: int = 0      # максимум (для рандома); 0 = использовать interval_minutes × 2
    hours_start: int = 0               # начало разрешённых часов (0-23)
    hours_end: int = 23                # конец разрешённых часов (0-23)
    notes: str = ""                    # свободный текст про правила
    status: str = GROUP_STATUS_ACTIVE
    join_status: str = JOIN_STATUS_UNKNOWN
    retry_after: str = ""              # ISO datetime — до этого времени не публикуем (запрет Telegram)
    next_allowed_at: str = ""          # ISO datetime — следующее разрешённое время публикации (рандомный интервал)
    last_error: str = ""
    created_at: str = ""


@dataclass
class Adaptation:
    """Адаптированный текст объявления под конкретную группу"""
    id: Optional[int] = None
    ad_id: int = 0
    group_id: int = 0
    text: str = ""
    adaptation_prompt: str = ""        # что просил у AI ("короче", "формальный тон", ...)
    created_at: str = ""


@dataclass
class PublicationLog:
    """Запись в журнале публикаций"""
    id: Optional[int] = None
    ad_id: int = 0
    group_id: int = 0
    account_phone: str = ""
    time: str = ""                     # ISO datetime
    status: str = ""                   # см. PUB_STATUS_*
    error_text: str = ""
    message_id: Optional[int] = None   # id сообщения в Telegram если успешно


@dataclass
class RequiredSub:
    """Обязательная подписка для публикации в группе"""
    id: Optional[int] = None
    group_id: int = 0
    channel_link: str = ""             # @channel или t.me/...
    is_joined: bool = False
    last_checked: str = ""


@dataclass
class SchedulerSettings:
    """
    Настройки планировщика, хранятся в key-value таблице scheduler_settings.
    Все значения в пределах HARD LIMITS (см. ads_scheduler.py).

    Поля *_min_seconds / *_max_seconds задают диапазон рандомной задержки:
    каждая реальная пауза = random.uniform(min, max).
    """
    # ─── Ads-планировщик: LEGACY (оставлено для обратной совместимости,
    #     удалится в следующей мажорной версии; уже не используется в логике) ───
    publication_interval_seconds: int = 300  # deprecated — используйте min/max
    join_interval_seconds: int = 900         # deprecated — используйте min/max

    # ─── Ads-планировщик: рандомные интервалы между публикациями ───
    publication_interval_min_seconds: int = 300   # 5 минут
    publication_interval_max_seconds: int = 600   # 10 минут
    daily_publication_limit: int = 30             # публикаций в сутки

    # ─── Обязательные подписки: рандомные интервалы вступления ───
    join_interval_min_seconds: int = 900          # 15 минут
    join_interval_max_seconds: int = 1800         # 30 минут
    daily_join_limit: int = 5                     # вступлений в сутки

    # ─── Broadcast (рассылка в группы) ───
    broadcast_delay_min_seconds: int = 30
    broadcast_delay_max_seconds: int = 90

    # ─── Mention (упоминания в группах) ───
    mention_delay_min_seconds: int = 45
    mention_delay_max_seconds: int = 120

    # ─── DM (личные сообщения) ───
    dm_delay_min_seconds: int = 60
    dm_delay_max_seconds: int = 180

    # ─── Join групп в "Проверить и очистить" ───
    group_check_join_delay_min_seconds: int = 15
    group_check_join_delay_max_seconds: int = 45

    # ─── Импорт TData ───
    tdata_connect_timeout_seconds: int = 60         # Шаг 5: client.connect()
    tdata_get_me_timeout_seconds: int = 60          # Шаг 6: client.get_me()
    tdata_flood_max_wait_seconds: int = 300         # FloodWait: до 5 мин ждём, дальше сдаёмся
    tdata_flood_jitter_min_seconds: int = 1         # +N сек после FloodWait перед ретраем (мин)
    tdata_flood_jitter_max_seconds: int = 3         # то же (макс)

    # ─── Управление устройствами (сессиями) ───
    device_terminate_delay_min_seconds: int = 1     # пауза между ResetAuthorizationRequest (мин)
    device_terminate_delay_max_seconds: int = 3     # то же (макс)
    device_terminate_default_schedule_hours: int = 2  # по умолчанию планируем через 2 часа

    # ─── AI ───
    ai_provider: str = "openai"              # "openai" или "groq"
    ai_model_openai: str = "gpt-4o-mini"
    ai_model_groq: str = "llama-3.3-70b-versatile"
