import re
from typing import Optional

from ads_ai import make_provider


DEFAULT_SYSTEM_PROMPT = (
    "Ты пишешь комментарии к постам в Telegram.\n"
    "Задача: написать короткий осмысленный комментарий к посту.\n"
    "Тон: {tone}.\n"
    "Длина: {length}.\n"
    "\n"
    "Правила:\n"
    "- Пиши на русском.\n"
    "- Не упоминай, что ты ИИ.\n"
    "- Не добавляй ссылки.\n"
    "- Не используй хэштеги.\n"
    "- Не используй кавычки вокруг всего ответа.\n"
    "- Верни только текст комментария.\n"
)

DEFAULT_USER_PROMPT = "Пост:\n{post_text}"


def _sanitize(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _length_hint(length: str) -> str:
    length = (length or "").strip().lower()
    if length in ("короткий", "short"):
        return "короткий (1 фраза)"
    if length in ("средний", "medium"):
        return "средний (2-3 фразы)"
    if length in ("длинный", "long"):
        return "длинный (4-6 фраз)"
    return length or "короткий (1 фраза)"


def generate_ai_comment(
    *,
    provider_name: str,
    api_key: str,
    model: str,
    proxy: str,
    post_text: str,
    tone: str,
    length: str,
    system_prompt_template: Optional[str] = None,
    user_prompt_template: Optional[str] = None,
    temperature: float = 0.8,
    max_tokens: int = 180,
) -> str:
    if not api_key:
        raise ValueError("AI API key не задан")
    post_text = (post_text or "").strip()
    if not post_text:
        raise ValueError("Пост пустой — нечего комментировать")

    system_template = (system_prompt_template or "").strip() or DEFAULT_SYSTEM_PROMPT
    user_template = (user_prompt_template or "").strip() or DEFAULT_USER_PROMPT

    system = system_template.format(tone=tone, length=_length_hint(length))
    user = user_template.format(post_text=post_text[:3500])

    provider = make_provider(provider_name, api_key=api_key, model=model, proxy=proxy)
    result = provider.complete(system=system, user=user, temperature=temperature, max_tokens=max_tokens)
    result = _sanitize(result)
    if not result:
        raise ValueError("AI вернул пустой ответ")
    return result

