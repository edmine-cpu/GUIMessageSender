from diagnostics import human_action_block_reason, human_exception, human_reason


class DummyFloodWait(Exception):
    def __init__(self, seconds):
        super().__init__(f"FloodWait {seconds}s")
        self.seconds = seconds


def test_human_reason_flood_wait_with_seconds():
    text = human_reason("flood_wait", wait_seconds=125)
    assert "Flood wait" in text
    assert "2 мин" in text


def test_human_reason_access_and_permissions():
    assert "нет прав" in human_reason("no_permission")
    assert "нет доступа" in human_reason("private")
    assert "вступить" in human_reason("need_subscription")


def test_human_reason_reauth_proxy_empty_text():
    assert "повторная авторизация" in human_reason("needs_reauth")
    assert "прокси" in human_reason("proxy")
    assert "пустой текст" in human_reason("empty_text")


def test_human_action_block_reason():
    assert "дневной лимит" in human_action_block_reason("daily_limit")
    assert "подождать" in human_action_block_reason("min_interval", 30)


def test_human_exception_uses_exception_name_and_seconds():
    text = human_exception(DummyFloodWait(61))
    assert "Flood wait" in text
    assert "1 мин" in text


def test_unknown_fallback_keeps_detail():
    text = human_reason("weird_code", "raw technical detail")
    assert "weird_code" in text
    assert "raw technical detail" in text

