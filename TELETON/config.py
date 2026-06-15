import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

# ─── API credentials ────────────────────────────────────────────────────────
# OWN_* — твои личные api_id/api_hash с my.telegram.org.
# Используются для phone-login сессий (когда сделаем) и как fallback для
# аккаунтов, у которых в БД не прописан собственный api_id.
OWN_API_ID = int(os.getenv("OWN_API_ID", "0"))
OWN_API_HASH = os.getenv("OWN_API_HASH", "")

# DESKTOP_* — api_id/api_hash Telegram Desktop.
# Используются ТОЛЬКО для импорта TData через opentele.UseCurrentSession —
# auth_key в TData выписан именно под этот api_id, поэтому все последующие
# запросы должны идти с ним же. Не менять.
DESKTOP_API_ID = 2040
DESKTOP_API_HASH = "b18441a1ff607e10a989891a5462e627"


@dataclass
class Config:
    db_path: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "data/teleton.db")
    )
    sessions_dir: str = field(
        default_factory=lambda: os.getenv("SESSIONS_DIR", "data/sessions")
    )
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    openai_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    )
    openai_proxy: str = field(
        default_factory=lambda: os.getenv("OPENAI_PROXY", "")
    )
    groq_api_key: str = field(
        default_factory=lambda: os.getenv("GROQ_API_KEY", "")
    )
    groq_proxy: str = field(
        default_factory=lambda: os.getenv("GROQ_PROXY", "")
    )

    def __post_init__(self):
        os.makedirs(self.sessions_dir, exist_ok=True)
