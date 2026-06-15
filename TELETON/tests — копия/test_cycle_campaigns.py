import os
import tempfile

import pytest

from database import Database


@pytest.fixture
def tmp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    yield path
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def test_cycle_campaign_crud(tmp_db_path):
    db = Database(tmp_db_path)
    try:
        a_id = db.get_or_create_cycle_campaign("ThemeA")
        b_id = db.get_or_create_cycle_campaign("ThemeB")
        assert a_id != b_id

        names = [c["name"] for c in db.list_cycle_campaigns()]
        assert "ThemeA" in names
        assert "ThemeB" in names

        db.rename_cycle_campaign(a_id, "ThemeA-Renamed")
        names2 = [c["name"] for c in db.list_cycle_campaigns()]
        assert "ThemeA" not in names2
        assert "ThemeA-Renamed" in names2

        ok = db.delete_cycle_campaign(b_id)
        assert ok is True
        names3 = [c["name"] for c in db.list_cycle_campaigns()]
        assert "ThemeB" not in names3
    finally:
        db.close()


# --- Focused regression test for the cycle start enabled helper (non-UI) ---
# Uses the project's AST-extract pattern because gui.py pulls tkinter at import time.
import ast
import os
import pytest


def _load_cycle_has_usable_config():
    """Extract the pure helper added to fix the 'Включённые' regression (tgts>0 even if accs=0)."""
    here = os.path.dirname(__file__)
    gui_path = os.path.join(here, "..", "gui.py")
    with open(gui_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_cycle_has_usable_config":
            code = compile(ast.Module(body=[node], type_ignores=[]), "gui.py", "exec")
            exec(code, ns)
            return ns["_cycle_has_usable_config"]
    raise RuntimeError("_cycle_has_usable_config not found in gui.py")


@pytest.fixture(scope="module")
def cycle_has_usable_config():
    return _load_cycle_has_usable_config()


def test_cycle_has_usable_config_for_regression(cycle_has_usable_config):
    """Regression guard: targets>0 is sufficient (accs=0 means 'Все активные' pool, must not block start)."""
    assert cycle_has_usable_config(5, 3) is True
    assert cycle_has_usable_config(1, 0) is True   # the key case that was broken (empty per-campaign accs list)
    assert cycle_has_usable_config(0, 0) is False
    assert cycle_has_usable_config(0, 10) is False
    assert cycle_has_usable_config(12, 0) is True

