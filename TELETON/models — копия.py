from dataclasses import dataclass
from typing import Optional


# ─── Статусы аккаунта ──────────────────────────────────────────────────────
# Используются в accounts.status. В паре с is_active (master-флаг от юзера)
# определяют, можно ли брать аккаунт в ротацию.
ACCOUNT_STATUS_ACTIVE = "active"               # всё ок, берём в ротацию
ACCOUNT_STATUS_NEEDS_REAUTH = "needs_reauth"   # auth_key мёртв, нужен переимпорт
ACCOUNT_STATUS_BANNED = "banned"               # забанен Telegram навсегда
ACCOUNT_STATUS_NETWORK_ISSUE = "network_issue" # сетевые проблемы, cooldown


@dataclass
class Account:
    phone: str
    session_name: str = ""
    proxy: str = ""
    is_active: bool = True
    sent_today: int = 0
    last_reset_date: str = ""
    # API credentials для этой сессии. Ставятся при импорте (TData → Desktop,
    # phone-login → OWN_*). Пустые значения означают fallback на OWN_API_ID.
    api_id: int = 0
    api_hash: str = ""
    # Device fingerprint — должен совпадать с тем, под которым выписан
    # auth_key. Для TData — "Desktop"/Windows, для новых — что-то стабильное.
    device_model: str = ""
    system_version: str = ""
    app_version: str = ""
    lang_code: str = "en"
    # Статус-трекинг (см. ACCOUNT_STATUS_*)
    status: str = ACCOUNT_STATUS_ACTIVE
    flood_until: str = ""              # ISO datetime — до этого не брать
    connect_fail_count: int = 0        # подряд неудачных connect'ов
    last_status_change: str = ""       # "ISO | статус | причина"
    paused_until: str = ""             # ISO datetime — до этого не брать (manual/global limiter)
    pause_reason: str = ""             # текст причины паузы
    last_check_ok_at: str = ""         # ISO datetime — последняя успешная проверка (connect ok)
    last_send_at: str = ""             # ISO datetime — последняя успешная отправка/действие
    last_action_at: str = ""           # ISO datetime — последняя попытка действия (для глоб. лимитера)
    actions_today: int = 0             # счётчик любых действий сегодня
    error_today: int = 0               # счётчик ошибок сегодня
    last_error_at: str = ""            # ISO datetime — последняя ошибка
    last_error_text: str = ""          # краткая причина последней ошибки
    # Кастомная метка/имя аккаунта (для удобства различения). Показываем всегда вместе с номером.
    custom_name: str = ""

    def __post_init__(self):
        if not self.session_name:
            self.session_name = f"data/sessions/session_{self.phone}"


@dataclass
class ParsedUser:
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    access_hash: int = 0
    group_source: str = ""
    status: str = ""
    is_bot: bool = False


@dataclass
class Task:
    id: Optional[int] = None
    target_group: str = ""
    message_text: str = ""
    task_type: str = "broadcast"  # broadcast / mention
    source_group: str = ""
    mentions_per_message: int = 2
    completed: bool = False
    status: str = "pending"       # pending / waiting / error
    retry_after: str = ""         # ISO datetime — до этого времени не брать в работу
    last_error: str = ""
    fail_count: int = 0


@dataclass
class SendLog:
    id: Optional[int] = None
    account_phone: str = ""
    target_group: str = ""
    message_text: str = ""
    status: str = ""  # sent / error / flood_wait / banned / no_permission / private
    error_detail: str = ""
    timestamp: str = ""


@dataclass
class MatchedPost:
    id: Optional[int] = None
    message_id: int = 0
    group_source: str = ""
    origin_group: str = ""
    message_date: str = ""
    message_link: str = ""
    sender_id: int = 0
    sender_username: Optional[str] = None
    sender_access_hash: int = 0
    message_text: str = ""
    match_mode: str = ""
    matched_keywords: str = ""
    ai_reason: str = ""
    matched_at: str = ""
