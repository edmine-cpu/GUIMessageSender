import json
import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class AIProviderError(Exception):
    message: str
    retryable: bool = False
    provider: str = ""

    def __str__(self) -> str:
        return self.message


def _maybe_extract_json(text: str) -> Optional[dict]:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    snippet = raw[start:end + 1]
    try:
        return json.loads(snippet)
    except Exception:
        return None


def _friendly_ai_error(provider: str, e: Exception) -> AIProviderError:
    msg = str(e) or ""
    msg_l = msg.lower()
    name = type(e).__name__
    name_l = name.lower()

    if any(x in msg_l for x in ("insufficient_quota", "billing", "quota", "exceeded your current quota")):
        return AIProviderError(
            message=f"{provider}: квота/биллинг недоступны (quota/billing). Проверь подписку/лимиты у провайдера.",
            retryable=False,
            provider=provider,
        )
    if any(x in msg_l for x in ("invalid_api_key", "incorrect api key", "api key", "unauthorized", "401")) and any(
        x in msg_l for x in ("invalid", "incorrect", "unauthorized", "401", "key")
    ):
        return AIProviderError(
            message=f"{provider}: неверный API key или нет доступа (401). Проверь ключ в Настройках.",
            retryable=False,
            provider=provider,
        )
    if any(x in msg_l for x in ("forbidden", "permission", "403")):
        return AIProviderError(
            message=f"{provider}: доступ запрещён (403). Проверь права ключа/организацию/проект.",
            retryable=False,
            provider=provider,
        )
    if "429" in msg_l or "rate" in name_l or "ratelimit" in name_l or "rate limit" in msg_l:
        return AIProviderError(
            message=f"{provider}: rate limit (429). Попробуй позже или снизь лимит сообщений/частоту.",
            retryable=True,
            provider=provider,
        )
    if "timeout" in name_l or "timed out" in msg_l:
        return AIProviderError(
            message=f"{provider}: таймаут запроса. Проверь прокси/интернет или попробуй позже.",
            retryable=True,
            provider=provider,
        )
    if any(x in name_l for x in ("apiconnection", "connection", "connecterror")) or any(
        x in msg_l for x in ("connection", "proxy", "dns", "ssl", "network")
    ):
        return AIProviderError(
            message=f"{provider}: ошибка соединения/прокси. Проверь прокси и доступ к API.",
            retryable=True,
            provider=provider,
        )
    if any(x in msg_l for x in ("invalid request", "bad request", "400")):
        return AIProviderError(
            message=f"{provider}: некорректный запрос (400). Упрости критерий или уменьши размер текста.",
            retryable=False,
            provider=provider,
        )
    if not msg:
        msg = name
    return AIProviderError(
        message=f"{provider}: ошибка API: {name}: {msg}",
        retryable=False,
        provider=provider,
    )


class AIFilter:
    """Фильтрация текста постов через AI (OpenAI/Groq)."""

    def __init__(
        self,
        *,
        provider: str = "openai",
        api_key: str,
        model: str,
        proxy: str = "",
        timeout_seconds: float = 45.0,
    ):
        self.provider = (provider or "openai").strip().lower()
        self.model = (model or "").strip()
        self.proxy = (proxy or "").strip()
        self.timeout_seconds = float(timeout_seconds or 45.0)

        if not api_key:
            raise ValueError(f"{self.provider}: API key не задан")
        if not self.model:
            raise ValueError(f"{self.provider}: model не задан")

        from ads_ai import make_provider
        self._provider_obj = make_provider(
            self.provider,
            api_key=api_key,
            model=self.model,
            proxy=self.proxy,
            timeout_seconds=self.timeout_seconds,
        )

    def check_post(self, post_text: str, criteria: str) -> dict:
        """
        Проверяет пост на соответствие критерию.
        criteria — описание на естественном языке, например:
        "ищет трафик с твиттера или софт для автоматизации реддита"

        Возвращает: {"match": True/False, "reason": "почему подходит"}
        """
        try:
            criteria = (criteria or "").strip()
            if not criteria:
                raise ValueError("AI критерий пустой")
            post_text = (post_text or "").strip()
            if not post_text:
                return {"match": False, "reason": ""}

            system = (
                'Ты фильтр постов. Определи, подходит ли пост под критерий.\n'
                'Верни строго JSON: {"match": true/false, "reason": "кратко почему"}'
            )
            user = f"Критерий: {criteria}\n\nПост: {post_text[:3500]}"
            text = self._provider_obj.complete(system=system, user=user, temperature=0.0, max_tokens=220)
            result = _maybe_extract_json(text) or {}
            return {
                "match": bool(result.get("match", False)),
                "reason": str(result.get("reason", "")),
            }
        except Exception as e:
            raise _friendly_ai_error(self.provider, e)

    def check_posts_batch(self, posts: List[dict], criteria: str) -> List[dict]:
        """
        Батч-проверка нескольких постов в одном запросе.
        posts — список {"id": msg_id, "text": msg_text}

        Возвращает список {"id": msg_id, "match": bool, "reason": str}
        """
        if not posts:
            return []

        posts_text = "\n\n".join(f"[Пост #{p['id']}]: {p['text']}" for p in posts)

        try:
            criteria = (criteria or "").strip()
            if not criteria:
                raise ValueError("AI критерий пустой")

            system = (
                "Ты фильтр постов. Для каждого поста определи, подходит ли он под критерий.\n"
                'Верни строго JSON: {"results": [{"id": <id>, "match": true/false, "reason": "кратко почему"}, ...]}'
            )
            user = f"Критерий: {criteria}\n\n{posts_text[:12000]}"
            text = self._provider_obj.complete(system=system, user=user, temperature=0.0, max_tokens=900)
            data = _maybe_extract_json(text) or {}
            results = data.get("results", []) or []

            # Маппинг по id
            result_map = {}
            for r in results:
                result_map[r.get("id")] = {
                    "match": bool(r.get("match", False)),
                    "reason": str(r.get("reason", "")),
                }

            output = []
            for p in posts:
                r = result_map.get(p["id"], {"match": False, "reason": ""})
                output.append({"id": p["id"], "match": r["match"], "reason": r["reason"]})
            return output

        except Exception as e:
            raise _friendly_ai_error(self.provider, e)
