import json
import os
import random
import re
from typing import List, Optional


def spin_text(text: str) -> str:
    """Рекурсивный парсинг spintax: {вариант1|вариант2|вариант3}"""
    def _replace(match):
        options = match.group(1).split("|")
        return random.choice(options)

    # Рекурсивно обрабатываем вложенные конструкции (от внутренних к внешним)
    pattern = re.compile(r"\{([^{}]+)\}")
    previous = None
    result = text
    while result != previous:
        previous = result
        result = pattern.sub(_replace, result)
    return result


def spin_unique(text: str, count: int) -> List[str]:
    """Генерация count уникальных вариантов spintax (макс 100 попыток на вариант)"""
    results = []
    seen = set()

    for _ in range(count):
        for _attempt in range(100):
            variant = spin_text(text)
            if variant not in seen:
                seen.add(variant)
                results.append(variant)
                break
        else:
            # Если за 100 попыток не нашли уникальный — берём последний
            results.append(variant)

    return results


# --- Омоглифы (mask.txt) ---

_mask_cache: Optional[dict] = None


def _load_mask(mask_path: str) -> dict:
    """Загрузить mask.txt с кешированием. Формат: {"А": ["А", "Α"], ...}"""
    global _mask_cache
    if _mask_cache is not None:
        return _mask_cache
    if not os.path.exists(mask_path):
        _mask_cache = {}
        return _mask_cache
    try:
        with open(mask_path, "r", encoding="utf-8") as f:
            _mask_cache = json.load(f)
    except Exception:
        _mask_cache = {}
    return _mask_cache


def apply_mask(text: str, mask_path: str = "data/mask.txt") -> str:
    """
    Случайная замена символов на омоглифы из mask.txt.
    Каждый символ заменяется случайным вариантом из списка (включая оригинал).
    Если mask.txt не найден или пустой — возвращает текст без изменений.
    """
    mask = _load_mask(mask_path)
    if not mask:
        return text

    result = []
    for char in text:
        variants = mask.get(char)
        if variants:
            result.append(random.choice(variants))
        else:
            result.append(char)
    return "".join(result)


def reload_mask():
    """Сбросить кеш маски (вызвать если mask.txt изменился)"""
    global _mask_cache
    _mask_cache = None


# --- AI-рерайт ---

def ai_rewrite(text: str, api_key: str, model: str = "gpt-4o-mini",
               proxy: str = "") -> str:
    """
    Уникализация текста через OpenAI.
    Промпт: перефразировать сообщение, сохранить смысл и эмодзи, вернуть только текст.
    При любой ошибке возвращает оригинальный текст без исключений.

    proxy — если задан, все запросы к OpenAI идут через прокси (защита OpSec).
    """
    if not api_key or not text.strip():
        return text

    try:
        from openai import OpenAI
        if proxy:
            try:
                import httpx
                http_client = httpx.Client(proxy=proxy)
                client = OpenAI(api_key=api_key, http_client=http_client)
            except ImportError as e:
                print(f"  [!] OpenAI proxy недоступен ({e}), прямое подключение")
                client = OpenAI(api_key=api_key)
        else:
            client = OpenAI(api_key=api_key)

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — помощник по уникализации текста. "
                        "Перефразируй сообщение пользователя своими словами. "
                        "Сохрани общий смысл, тональность и все эмодзи. "
                        "Верни ТОЛЬКО готовый текст, без пояснений и кавычек."
                    ),
                },
                {"role": "user", "content": text},
            ],
            max_tokens=1024,
            temperature=0.9,
        )
        result = response.choices[0].message.content
        return result.strip() if result else text

    except Exception as e:
        print(f"  [!] AI-рерайт недоступен: {e}")
        return text
