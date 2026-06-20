"""
Тесты для словаря подсказок при импорте TData (TDATA_ERROR_HINTS) и
функции _hint_for() из gui.py.

gui.py импортирует tkinter, поэтому полностью загрузить его в headless-CI
нельзя. Извлекаем нужные символы через AST-парсинг исходника.
"""
import ast
import os
import pytest


def _load_symbols_from_gui():
    """Извлекает TDATA_ERROR_HINTS и _hint_for из gui.py без import gui."""
    here = os.path.dirname(__file__)
    gui_path = os.path.join(here, "..", "gui.py")
    with open(gui_path, "r", encoding="utf-8") as f:
        src = f.read()

    tree = ast.parse(src)
    namespace = {}

    for node in tree.body:
        # Берём словарь-константу TDATA_ERROR_HINTS
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TDATA_ERROR_HINTS":
                    code = compile(ast.Module(body=[node], type_ignores=[]),
                                   filename="gui.py", mode="exec")
                    exec(code, namespace)
        # Берём функции, от которых зависит _hint_for
        if isinstance(node, ast.FunctionDef) and node.name in ("_safe_exception_text", "_hint_for"):
            code = compile(ast.Module(body=[node], type_ignores=[]),
                           filename="gui.py", mode="exec")
            exec(code, namespace)

    return namespace["TDATA_ERROR_HINTS"], namespace["_hint_for"]


@pytest.fixture(scope="module")
def hints_and_func():
    return _load_symbols_from_gui()


class TestTDATAErrorHints:
    def test_dict_not_empty(self, hints_and_func):
        hints, _ = hints_and_func
        assert len(hints) >= 10  # должно быть много подсказок

    def test_critical_telethon_errors_covered(self, hints_and_func):
        """Самые частые исключения Telethon должны иметь подсказку."""
        hints, _ = hints_and_func
        critical = [
            "AuthKeyUnregisteredError",
            "AuthKeyInvalidError",
            "FloodWaitError",
            "PhoneNumberBannedError",
            "UserDeactivatedBanError",
        ]
        for err in critical:
            assert err in hints, f"Нет подсказки для {err}"

    def test_system_errors_covered(self, hints_and_func):
        """Системные ошибки (сеть, диск) тоже должны иметь подсказки."""
        hints, _ = hints_and_func
        for err in ("TimeoutError", "ConnectionError", "OSError"):
            assert err in hints

    def test_opentele_errors_covered(self, hints_and_func):
        hints, _ = hints_and_func
        assert "OpenTeleException" in hints
        assert "TFileNotFound" in hints

    def test_all_hints_are_non_empty_strings(self, hints_and_func):
        hints, _ = hints_and_func
        for name, hint in hints.items():
            assert isinstance(hint, str), f"{name}: подсказка должна быть строкой"
            assert len(hint) > 20, f"{name}: подсказка слишком короткая"

    def test_all_hints_in_russian(self, hints_and_func):
        """Подсказки на русском (для пользователей)."""
        hints, _ = hints_and_func
        for name, hint in hints.items():
            # Хотя бы одна кириллическая буква в подсказке
            has_cyrillic = any('а' <= ch.lower() <= 'я' for ch in hint)
            assert has_cyrillic, f"{name}: подсказка не содержит русских букв"


class TestHintFor:
    def test_known_exception_returns_hint(self, hints_and_func):
        _, hint_for = hints_and_func

        class AuthKeyUnregisteredError(Exception):
            pass
        e = AuthKeyUnregisteredError()
        result = hint_for(e)
        assert "TData устарела" in result or "auth_key" in result

    def test_unknown_exception_returns_str(self, hints_and_func):
        """Для неизвестного исключения — возвращаем str(e) как fallback."""
        _, hint_for = hints_and_func

        class CustomUnknownError(Exception):
            pass
        e = CustomUnknownError("custom message")
        result = hint_for(e)
        assert result == "custom message"

    def test_unknown_exception_empty_message_returns_class_name(self, hints_and_func):
        """Если у исключения нет сообщения — fallback на имя класса."""
        _, hint_for = hints_and_func

        class CustomUnknownErrorNoMsg(Exception):
            pass
        e = CustomUnknownErrorNoMsg()
        result = hint_for(e)
        assert result == "CustomUnknownErrorNoMsg"

    def test_timeout_error(self, hints_and_func):
        """TimeoutError — стандартный класс, должен попадать в подсказку."""
        _, hint_for = hints_and_func
        e = TimeoutError("timed out")
        result = hint_for(e)
        # Подсказка про прокси/интернет
        assert "прокси" in result.lower() or "интернет" in result.lower()

    def test_os_error(self, hints_and_func):
        _, hint_for = hints_and_func
        e = OSError("permission denied")
        result = hint_for(e)
        assert "data/sessions" in result or "антивирус" in result.lower()

    def test_opentele_no_account_loaded_is_specific(self, hints_and_func):
        _, hint_for = hints_and_func

        class OpenTeleException(Exception):
            pass

        e = OpenTeleException("No account has been loaded")
        result = hint_for(e)
        assert "не смог загрузить ни один аккаунт" in result
        assert "вложенная tdata" in result

    def test_broken_exception_str_does_not_break_hint(self, hints_and_func):
        _, hint_for = hints_and_func

        class OpenTeleException(Exception):
            def __str__(self):
                raise RuntimeError("broken __str__")

        result = hint_for(OpenTeleException())
        assert "Папка TData повреждена" in result or "key_datas" in result
