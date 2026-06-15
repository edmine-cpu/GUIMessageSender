"""
Тесты для _parse_links_input из ads_gui — парсер списка ссылок групп.

Покрывают:
- одна ссылка: разные форматы (https, t.me, @user)
- несколько ссылок через перенос
- через запятую/точку с запятой
- слитные через одиночный пробел (как заказчик вставил из буфера)
- мусор и пустые строки выкидываются
- невалидный формат выкидывается
"""
import ast
import os
import pytest


def _load_parser():
    """Извлекает _parse_links_input из ads_gui без import (нет tkinter)."""
    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "ads_gui.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_parse_links_input":
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                          filename="ads_gui.py", mode="exec"), ns)
            return ns["_parse_links_input"]
    raise RuntimeError("_parse_links_input not found")


@pytest.fixture(scope="module")
def parse():
    return _load_parser()


class TestSingleLink:
    def test_https_form(self, parse):
        assert parse("https://t.me/group1") == ["https://t.me/group1"]

    def test_http_form(self, parse):
        assert parse("http://t.me/group1") == ["http://t.me/group1"]

    def test_no_scheme(self, parse):
        assert parse("t.me/group1") == ["t.me/group1"]

    def test_at_username(self, parse):
        assert parse("@groupname") == ["@groupname"]

    def test_short_at_rejected(self, parse):
        # @ab — слишком короткий username
        assert parse("@ab") == []

    def test_with_trailing_slash(self, parse):
        # Конечный / удаляется
        assert parse("https://t.me/group1/") == ["https://t.me/group1"]

    def test_with_message_id(self, parse):
        # Ссылки на конкретное сообщение в канале/чате
        assert parse("https://t.me/group/123") == ["https://t.me/group/123"]

    def test_private_link(self, parse):
        # Приватные группы — t.me/c/12345/1
        result = parse("https://t.me/c/12345/1")
        assert result == ["https://t.me/c/12345/1"]


class TestMultipleLinks:
    def test_newline_separated(self, parse):
        raw = "https://t.me/group1\nhttps://t.me/group2\nhttps://t.me/group3"
        assert parse(raw) == [
            "https://t.me/group1",
            "https://t.me/group2",
            "https://t.me/group3",
        ]

    def test_comma_separated(self, parse):
        raw = "@group1, @group2, @group3"
        assert parse(raw) == ["@group1", "@group2", "@group3"]

    def test_semicolon_separated(self, parse):
        raw = "https://t.me/g1; https://t.me/g2"
        assert parse(raw) == ["https://t.me/g1", "https://t.me/g2"]

    def test_mixed_separators(self, parse):
        raw = "https://t.me/g1\n@group2,https://t.me/g3"
        assert parse(raw) == ["https://t.me/g1", "@group2", "https://t.me/g3"]

    def test_with_empty_lines(self, parse):
        raw = "https://t.me/g1\n\n\nhttps://t.me/g2\n"
        assert parse(raw) == ["https://t.me/g1", "https://t.me/g2"]

    def test_real_user_paste(self, parse):
        """Реальный сценарий — вставка из буфера большого списка."""
        raw = """https://t.me/CardoCrewDeskModels
https://t.me/ONLYTRAFCH
https://t.me/DeskSpark
https://t.me/OnlyBoardTG
https://t.me/onlyadating"""
        result = parse(raw)
        assert len(result) == 5
        assert "https://t.me/CardoCrewDeskModels" in result
        assert "https://t.me/onlyadating" in result


class TestInvalidInput:
    def test_empty_string(self, parse):
        assert parse("") == []

    def test_whitespace_only(self, parse):
        assert parse("   \n\n  \t") == []

    def test_invalid_format(self, parse):
        assert parse("just text") == []

    def test_random_url(self, parse):
        # Не t.me — отбрасываем
        assert parse("https://google.com") == []

    def test_facebook_url(self, parse):
        assert parse("https://facebook.com/page") == []

    def test_garbage_with_valid(self, parse):
        """Мусор отфильтровывается, валидные остаются."""
        raw = "https://t.me/g1\nrandom text\n@group2\nhttps://google.com\n@group3"
        result = parse(raw)
        assert "https://t.me/g1" in result
        assert "@group2" in result
        assert "@group3" in result
        assert "random text" not in result
        assert "https://google.com" not in result


class TestEdgeCases:
    def test_single_with_extra_whitespace(self, parse):
        assert parse("   https://t.me/group1   ") == ["https://t.me/group1"]

    def test_uppercase_https(self, parse):
        # Регистрозависимость HTTPS
        assert parse("HTTPS://t.me/group1") == ["HTTPS://t.me/group1"]

    def test_glued_links_single_space(self, parse):
        """Если в одной строке две ссылки через одиночный пробел."""
        raw = "https://t.me/g1 https://t.me/g2"
        result = parse(raw)
        assert "https://t.me/g1" in result
        assert "https://t.me/g2" in result
