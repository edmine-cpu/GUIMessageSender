"""Тесты spin_text / spin_unique / apply_mask."""
import pytest
from spintax import spin_text, spin_unique, apply_mask, reload_mask


class TestSpinText:
    def test_no_braces_returns_original(self):
        assert spin_text("hello world") == "hello world"

    def test_simple_choice(self):
        result = spin_text("{hello|hi}")
        assert result in ("hello", "hi")

    def test_multiple_groups(self):
        for _ in range(20):
            result = spin_text("{привет|здравствуй} {друг|коллега}")
            assert result.split()[0] in ("привет", "здравствуй")
            assert result.split()[1] in ("друг", "коллега")

    def test_nested_spintax(self):
        """Вложенные скобки раскрываются от внутренних к внешним."""
        for _ in range(20):
            result = spin_text("{a|{b|c}}")
            assert result in ("a", "b", "c")

    def test_empty_option(self):
        """Пустой вариант → может вернуться пустая строка."""
        for _ in range(50):
            result = spin_text("hello{ world|}")
            assert result in ("hello world", "hello")

    def test_three_deep_nesting(self):
        for _ in range(30):
            result = spin_text("{a|{b|{c|d}}}")
            assert result in ("a", "b", "c", "d")


class TestSpinUnique:
    def test_returns_requested_count(self):
        variants = spin_unique("{a|b|c|d}", count=3)
        assert len(variants) == 3

    def test_variants_are_unique_when_possible(self):
        # 4 вариантов достаточно для 3 уникальных
        variants = spin_unique("{a|b|c|d}", count=3)
        assert len(set(variants)) == 3

    def test_fallback_when_not_enough_variants(self):
        """Запрошено больше, чем возможно — не падает, просто возвращает с повторами."""
        variants = spin_unique("{a|b}", count=5)
        assert len(variants) == 5
        # Уникальных всего 2
        assert len(set(variants)) <= 2


class TestApplyMask:
    def test_no_mask_file_returns_original(self, tmp_path):
        reload_mask()
        mask_path = str(tmp_path / "nonexistent.txt")
        assert apply_mask("Hello", mask_path) == "Hello"

    def test_empty_mask_returns_original(self, tmp_path):
        reload_mask()
        mask_path = tmp_path / "empty.txt"
        mask_path.write_text("{}", encoding="utf-8")
        assert apply_mask("Hello", str(mask_path)) == "Hello"

    def test_mask_substitutes_chars(self, tmp_path):
        reload_mask()
        mask_path = tmp_path / "mask.txt"
        # H всегда будет заменён на X, остальное без изменений
        mask_path.write_text('{"H": ["X"]}', encoding="utf-8")
        assert apply_mask("Hello", str(mask_path)) == "Xello"
        reload_mask()

    def test_chars_without_variants_unchanged(self, tmp_path):
        reload_mask()
        mask_path = tmp_path / "mask.txt"
        mask_path.write_text('{"Z": ["X"]}', encoding="utf-8")
        # Z нет в тексте, ничего не меняется
        assert apply_mask("Hello", str(mask_path)) == "Hello"
        reload_mask()
