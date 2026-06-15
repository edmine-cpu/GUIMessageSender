"""
ads_ai.py — AI-генерация текста объявлений.

Задача: из короткого пользовательского описания ("продаю айфон 15, 90000,
Москва, торг") сгенерировать красивый пост для публикации в группах
объявлений. И опционально адаптировать базовый текст под конкретную
группу (более формальный тон, короче, акцент на срочности и т.д.).

Архитектура: абстрактный AIProvider + две имплементации (OpenAI и Groq),
выбираемые пользователем в настройках. В конструкторе провайдера можно
передать готовый клиент SDK, чтобы в тестах подставлять мок без сети.
"""

from abc import ABC, abstractmethod
from typing import Optional


SYSTEM_PROMPT_GENERATE = (
    "Ты помощник, который пишет короткие продающие объявления для "
    "размещения в группах объявлений в Telegram. "
    "Стиль: дружелюбный, конкретный, без воды. "
    "Обязательно сохрани все факты из описания пользователя: цена, "
    "адрес/город, контакты, условия, характеристики. "
    "Длина: 3–6 коротких абзацев или строк. Без хештегов, без звёздочек "
    "Markdown-жирного, без эмодзи-спама (1–2 уместных эмодзи максимум). "
    "Не выдумывай факты которых нет в описании. "
    "Не пиши вступлений типа 'вот ваше объявление:'. Сразу текст поста."
)

SYSTEM_PROMPT_ADAPT = (
    "Ты редактируешь объявление под требования конкретной группы. "
    "Сохрани ВСЕ фактические данные (цена, адрес, контакты, условия, "
    "характеристики) без изменений. Меняй только стиль/тон/длину по "
    "инструкции пользователя. Не выдумывай новые факты. "
    "Не пиши вступлений. Сразу отредактированный текст."
)


class AIProvider(ABC):
    """Абстрактный провайдер AI. Две имплементации: OpenAI и Groq."""

    @abstractmethod
    def complete(self, system: str, user: str,
                 temperature: float = 0.7,
                 max_tokens: int = 800) -> str:
        """Выполнить запрос, вернуть текст ответа."""
        ...


class OpenAIProvider(AIProvider):
    """Провайдер через OpenAI SDK."""

    def __init__(self, api_key: str,
                 model: str = "gpt-4o-mini",
                 proxy: str = "",
                 timeout_seconds: float = 45.0,
                 client=None):
        """
        Параметр client можно передать готовый (для тестов с моком).
        Иначе создаётся из api_key/proxy по паттерну ai_filter.py.
        """
        self.model = model
        if client is not None:
            self.client = client
            return

        from openai import OpenAI
        if proxy:
            try:
                import httpx
                http_client = httpx.Client(proxy=proxy, timeout=timeout_seconds)
                self.client = OpenAI(api_key=api_key, http_client=http_client)
            except ImportError as e:
                print(f"[!] OpenAI proxy недоступен: {e}")
                print("[!] Для SOCKS-прокси: pip install 'httpx[socks]'")
                self.client = OpenAI(api_key=api_key)
        else:
            try:
                import httpx
                http_client = httpx.Client(timeout=timeout_seconds)
                self.client = OpenAI(api_key=api_key, http_client=http_client)
            except Exception:
                self.client = OpenAI(api_key=api_key)

    def complete(self, system: str, user: str,
                 temperature: float = 0.7,
                 max_tokens: int = 800) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (response.choices[0].message.content or "").strip()


class GroqProvider(AIProvider):
    """Провайдер через Groq SDK (OpenAI-совместимый формат)."""

    def __init__(self, api_key: str,
                 model: str = "llama-3.3-70b-versatile",
                 proxy: str = "",
                 timeout_seconds: float = 45.0,
                 client=None):
        self.model = model
        if client is not None:
            self.client = client
            return

        from groq import Groq
        if proxy:
            try:
                import httpx
                http_client = httpx.Client(proxy=proxy, timeout=timeout_seconds)
                self.client = Groq(api_key=api_key, http_client=http_client)
            except ImportError as e:
                print(f"[!] Groq proxy недоступен: {e}")
                self.client = Groq(api_key=api_key)
        else:
            try:
                import httpx
                http_client = httpx.Client(timeout=timeout_seconds)
                self.client = Groq(api_key=api_key, http_client=http_client)
            except Exception:
                self.client = Groq(api_key=api_key)

    def complete(self, system: str, user: str,
                 temperature: float = 0.7,
                 max_tokens: int = 800) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (response.choices[0].message.content or "").strip()


class AdsAI:
    """
    Фасад над провайдером для задач объявлений.

    Использование:
        provider = OpenAIProvider(api_key="...", model="gpt-4o-mini")
        ai = AdsAI(provider)
        text = ai.generate_ad("продаю айфон 15, 90000, москва, торг")
        adapted = ai.adapt_ad(text, "короче, без эмодзи")
    """

    def __init__(self, provider: AIProvider):
        self.provider = provider

    def generate_ad(self, description: str,
                    tone: str = "",
                    length: str = "") -> str:
        """Сгенерировать объявление из описания."""
        if not description.strip():
            raise ValueError("Описание объявления не может быть пустым")

        extras = []
        if tone:
            extras.append(f"Тон: {tone}")
        if length:
            extras.append(f"Длина: {length}")
        extras_str = "\n".join(extras)

        user_msg = f"Описание:\n{description}"
        if extras_str:
            user_msg += f"\n\nДополнительно:\n{extras_str}"

        return self.provider.complete(SYSTEM_PROMPT_GENERATE, user_msg,
                                      temperature=0.7)

    def adapt_ad(self, base_text: str, adaptation_prompt: str) -> str:
        """Адаптировать готовый текст объявления под конкретную группу."""
        if not base_text.strip():
            raise ValueError("Базовый текст не может быть пустым")
        if not adaptation_prompt.strip():
            raise ValueError("Инструкция адаптации не может быть пустой")

        user_msg = (
            f"Исходное объявление:\n{base_text}\n\n"
            f"Инструкция: {adaptation_prompt}"
        )
        return self.provider.complete(SYSTEM_PROMPT_ADAPT, user_msg,
                                      temperature=0.4)


def make_provider(provider_name: str,
                  api_key: str,
                  model: Optional[str] = None,
                  proxy: str = "",
                  timeout_seconds: float = 45.0) -> AIProvider:
    """
    Фабрика: по имени провайдера создать объект.
    provider_name: "openai" или "groq".
    """
    name = provider_name.lower().strip()
    if name == "openai":
        return OpenAIProvider(api_key=api_key,
                              model=model or "gpt-4o-mini",
                              proxy=proxy,
                              timeout_seconds=timeout_seconds)
    if name == "groq":
        return GroqProvider(api_key=api_key,
                            model=model or "llama-3.3-70b-versatile",
                            proxy=proxy,
                            timeout_seconds=timeout_seconds)
    raise ValueError(f"Неизвестный AI-провайдер: {provider_name!r}. "
                     f"Доступны: 'openai', 'groq'")
