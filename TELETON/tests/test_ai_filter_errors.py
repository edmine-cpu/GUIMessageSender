import pytest

from ai_filter import _friendly_ai_error


class RateLimitError(Exception):
    pass


@pytest.mark.parametrize(
    ("exc", "expected_text", "retryable"),
    [
        (Exception("insufficient_quota: billing hard limit reached"), "квота/биллинг", False),
        (Exception("invalid_api_key: incorrect API key provided"), "неверный API key", False),
        (RateLimitError("too many requests"), "rate limit (429)", True),
        (Exception("401 unauthorized"), "неверный API key", False),
    ],
)
def test_friendly_ai_error_messages_are_actionable(exc, expected_text, retryable):
    err = _friendly_ai_error("openai", exc)

    assert expected_text in str(err)
    assert err.retryable is retryable
    assert err.provider == "openai"
