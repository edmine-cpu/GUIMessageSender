import ast
import os
import sys
import types

import pytest

from database import Database
from models import Account, ACCOUNT_STATUS_BANNED


def _load_import_symbols():
    here = os.path.dirname(__file__)
    gui_path = os.path.join(here, "..", "gui.py")
    with open(gui_path, "r", encoding="utf-8") as f:
        src = f.read()

    names = {
        "TDATA_ERROR_HINTS",
        "_safe_exception_text",
        "_hint_for",
        "_try_with_flood_retry",
        "_is_tdata_dir",
        "_collect_tdata_dirs",
        "_tdata_layout_diagnostics",
        "_format_tdata_layout_diagnostics",
        "_format_tdata_read_error",
        "_import_result",
        "_summarize_import_results",
        "_cleanup_session_files",
        "_move_session_file",
        "_save_imported_account",
        "_session_candidate_from_filename",
        "_verify_session_account",
        "import_session_files_to_db",
        "import_tdata_dir_to_db",
        "_run_loop",
    }
    tree = ast.parse(src)
    namespace = {
        "os": os,
        "re": __import__("re"),
        "asyncio": __import__("asyncio"),
        "Database": Database,
        "Account": Account,
        "log_exception": lambda *args, **kwargs: None,
        "print": lambda *args, **kwargs: None,
    }

    for node in tree.body:
        node_name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node_name = node.name
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    node_name = target.id
                    break
        if node_name in names:
            code = compile(ast.Module(body=[node], type_ignores=[]),
                           filename="gui.py", mode="exec")
            exec(code, namespace)

    return namespace


@pytest.fixture(scope="module")
def symbols():
    return _load_import_symbols()


def _make_tdata_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "key_datas").write_text("key", encoding="utf-8")
    (path / "D877F783D5D3EF8C").mkdir()
    return path


class _FakeTdAccount:
    def __init__(self, user_id):
        self.UserId = user_id


class _FakeMe:
    def __init__(self, phone):
        self.phone = phone
        self.first_name = "Test"


class _FakeTDataClient:
    def __init__(self, session, spec):
        self.session = session
        self.spec = spec
        if spec.get("create_session", True):
            with open(session + ".session", "w", encoding="utf-8") as f:
                f.write("fake")

    async def connect(self):
        if self.spec.get("connect_error"):
            raise self.spec["connect_error"]

    async def is_user_authorized(self):
        return self.spec.get("authorized", True)

    async def get_me(self):
        phone = self.spec.get("phone")
        if phone is None:
            return None
        return _FakeMe(phone)

    async def disconnect(self):
        pass


def _install_fake_opentele(monkeypatch, specs):
    class FakeTDesktop:
        def __init__(self, path):
            self.path = path
            self.accounts = [_FakeTdAccount(s["user_id"]) for s in specs]
            self.accountsCount = len(self.accounts)

        def isLoaded(self):
            return True

        async def ToTelethon(self, session, flag, api, **kwargs):
            user_id = int(os.path.basename(session).replace("session_tdata_", ""))
            spec = next(s for s in specs if s["user_id"] == user_id)
            return _FakeTDataClient(session, spec)

    class FakeAPI:
        @staticmethod
        def TelegramDesktop(api_id, api_hash):
            return {"api_id": api_id, "api_hash": api_hash}

    monkeypatch.setitem(sys.modules, "opentele", types.ModuleType("opentele"))
    td_mod = types.ModuleType("opentele.td")
    td_mod.TDesktop = FakeTDesktop
    api_mod = types.ModuleType("opentele.api")
    api_mod.API = FakeAPI
    api_mod.UseCurrentSession = object()
    monkeypatch.setitem(sys.modules, "opentele.td", td_mod)
    monkeypatch.setitem(sys.modules, "opentele.api", api_mod)


def _install_failing_opentele(monkeypatch, exc):
    class FakeTDesktop:
        def __init__(self, path):
            raise exc

    class FakeAPI:
        @staticmethod
        def TelegramDesktop(api_id, api_hash):
            return {"api_id": api_id, "api_hash": api_hash}

    monkeypatch.setitem(sys.modules, "opentele", types.ModuleType("opentele"))
    td_mod = types.ModuleType("opentele.td")
    td_mod.TDesktop = FakeTDesktop
    api_mod = types.ModuleType("opentele.api")
    api_mod.API = FakeAPI
    api_mod.UseCurrentSession = object()
    monkeypatch.setitem(sys.modules, "opentele.td", td_mod)
    monkeypatch.setitem(sys.modules, "opentele.api", api_mod)


def test_tdata_dir_detection(symbols, tmp_path):
    is_tdata = symbols["_is_tdata_dir"]
    collect = symbols["_collect_tdata_dirs"]

    root = tmp_path / "root"
    valid = _make_tdata_dir(root / "acc1" / "tdata")
    invalid = root / "bad"
    invalid.mkdir(parents=True)
    (invalid / "key_datas").write_text("key", encoding="utf-8")

    assert is_tdata(str(valid))
    assert not is_tdata(str(invalid))
    assert collect(str(root)) == [str(valid)]


def test_tdata_invalid_and_container_fail_early(symbols, monkeypatch, tmp_path):
    import_tdata = symbols["import_tdata_dir_to_db"]
    db_path = tmp_path / "db.sqlite"
    sessions = tmp_path / "sessions"
    container = tmp_path / "container"
    nested = _make_tdata_dir(container / "one" / "tdata")
    _install_fake_opentele(monkeypatch, [])

    invalid = import_tdata(str(tmp_path / "missing"), "", str(sessions), str(db_path),
                           log_cb=lambda msg: None)
    assert invalid["kind"] == "fail_early"
    assert invalid["added"] == 0

    res = import_tdata(str(container), "", str(sessions), str(db_path),
                       log_cb=lambda msg: None)
    assert res["kind"] == "fail_early"
    assert str(nested) in res["text"] or "TData" in res["text"]


def test_tdata_opentele_no_account_loaded_has_actionable_summary(symbols, monkeypatch, tmp_path):
    import_tdata = symbols["import_tdata_dir_to_db"]
    tdata = _make_tdata_dir(tmp_path / "acc" / "tdata")
    (tdata / "Telegram.exe").write_text("fake", encoding="utf-8")
    nested = _make_tdata_dir(tdata / "tdata")
    db_path = tmp_path / "db.sqlite"
    sessions = tmp_path / "sessions"

    class OpenTeleException(Exception):
        pass

    _install_failing_opentele(monkeypatch, OpenTeleException("No account has been loaded"))

    res = import_tdata(str(tdata), "", str(sessions), str(db_path),
                       log_cb=lambda msg: None)

    assert res["kind"] == "fail_early"
    assert "No account has been loaded" in res["text"]
    assert "не смог загрузить ни один аккаунт" in res["text"]
    assert "Telegram.exe" in res["text"]
    assert str(nested) in res["text"]


def test_tdata_success_adds_real_phone_and_desktop_fields(symbols, monkeypatch, tmp_path):
    import_tdata = symbols["import_tdata_dir_to_db"]
    tdata = _make_tdata_dir(tmp_path / "acc" / "tdata")
    db_path = tmp_path / "db.sqlite"
    sessions = tmp_path / "sessions"
    _install_fake_opentele(monkeypatch, [{"user_id": 10, "phone": "79990000001"}])

    res = import_tdata(str(tdata), "socks5://u:p@127.0.0.1:1080",
                       str(sessions), str(db_path), log_cb=lambda msg: None)

    assert res["kind"] == "success"
    assert res["added"] == 1
    assert res["updated"] == 0
    assert res["results"][0]["phone"] == "+79990000001"

    db = Database(str(db_path))
    try:
        acc = db.get_all_accounts()[0]
    finally:
        db.close()
    assert acc.phone == "+79990000001"
    assert acc.session_name == os.path.join(str(sessions), "session_+79990000001")
    assert acc.api_id == 2040
    assert acc.device_model == "Desktop"
    assert os.path.exists(acc.session_name + ".session")


def test_tdata_reimport_updates_existing_without_fail(symbols, monkeypatch, tmp_path):
    import_tdata = symbols["import_tdata_dir_to_db"]
    tdata = _make_tdata_dir(tmp_path / "acc" / "tdata")
    db_path = tmp_path / "db.sqlite"
    sessions = tmp_path / "sessions"
    db = Database(str(db_path))
    try:
        db.add_account(Account(
            phone="+79990000002",
            session_name="old",
            is_active=False,
            sent_today=7,
            status=ACCOUNT_STATUS_BANNED,
            last_status_change="2026-01-01T00:00:00 | banned | test",
            paused_until="2099-01-01T00:00:00",
            custom_name="keep-me",
        ))
    finally:
        db.close()
    _install_fake_opentele(monkeypatch, [{"user_id": 20, "phone": "79990000002"}])

    res = import_tdata(str(tdata), "", str(sessions), str(db_path),
                       log_cb=lambda msg: None)

    assert res["kind"] == "success"
    assert res["added"] == 0
    assert res["updated"] == 1
    db = Database(str(db_path))
    try:
        acc = db.get_all_accounts()[0]
    finally:
        db.close()
    assert acc.is_active is False
    assert acc.status == ACCOUNT_STATUS_BANNED
    assert acc.last_status_change == "2026-01-01T00:00:00 | banned | test"
    assert acc.sent_today == 7
    assert acc.paused_until == "2099-01-01T00:00:00"
    assert acc.custom_name == "keep-me"
    assert acc.session_name == os.path.join(str(sessions), "session_+79990000002")


def test_tdata_partial_cleans_failed_temp_session(symbols, monkeypatch, tmp_path):
    import_tdata = symbols["import_tdata_dir_to_db"]
    tdata = _make_tdata_dir(tmp_path / "acc" / "tdata")
    db_path = tmp_path / "db.sqlite"
    sessions = tmp_path / "sessions"
    _install_fake_opentele(monkeypatch, [
        {"user_id": 30, "phone": "79990000003"},
        {"user_id": 31, "phone": "79990000004", "authorized": False},
    ])

    res = import_tdata(str(tdata), "", str(sessions), str(db_path),
                       log_cb=lambda msg: None)

    assert res["kind"] == "partial"
    assert res["added"] == 1
    assert res["failed"] == 1
    assert not os.path.exists(os.path.join(str(sessions), "session_tdata_31.session"))


class _FakeSessionClient:
    outcomes = {}

    def __init__(self, session_name, api_id, api_hash, **kwargs):
        self.session_name = session_name
        self.outcome = self.outcomes[os.path.basename(session_name)]

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return self.outcome.get("authorized", True)

    async def get_me(self):
        return _FakeMe(self.outcome["phone"])

    async def disconnect(self):
        pass


def _install_fake_telethon(monkeypatch, outcomes):
    _FakeSessionClient.outcomes = outcomes
    telethon_mod = types.ModuleType("telethon")
    telethon_mod.TelegramClient = _FakeSessionClient
    monkeypatch.setitem(sys.modules, "telethon", telethon_mod)

    import config
    monkeypatch.setattr(config, "OWN_API_ID", 12345)
    monkeypatch.setattr(config, "OWN_API_HASH", "own_hash")


def test_session_import_requires_authorization(symbols, monkeypatch, tmp_path):
    import_sessions = symbols["import_session_files_to_db"]
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    bad = sessions / "session_+100.session"
    bad.write_text("fake", encoding="utf-8")
    _install_fake_telethon(monkeypatch, {"session_+100": {"authorized": False, "phone": "100"}})

    res = import_sessions(
        [{"declared_phone": "+100", "session_name": str(bad)[:-len(".session")],
          "filename": bad.name}],
        proxy_for_index="",
        sessions_dir=str(sessions),
        db_path=str(tmp_path / "db.sqlite"),
        log_cb=lambda msg: None,
    )

    assert res["kind"] == "fail"
    assert res["added"] == 0
    db = Database(str(tmp_path / "db.sqlite"))
    try:
        assert db.get_all_accounts() == []
    finally:
        db.close()


def test_session_import_uses_real_phone_from_get_me(symbols, monkeypatch, tmp_path):
    import_sessions = symbols["import_session_files_to_db"]
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    src = sessions / "session_+111.session"
    src.write_text("fake", encoding="utf-8")
    _install_fake_telethon(monkeypatch, {"session_+111": {"phone": "222"}})

    res = import_sessions(
        [{"declared_phone": "+111", "session_name": str(src)[:-len(".session")],
          "filename": src.name}],
        proxy_for_index="",
        sessions_dir=str(sessions),
        db_path=str(tmp_path / "db.sqlite"),
        log_cb=lambda msg: None,
    )

    assert res["kind"] == "success"
    assert res["added"] == 1
    db = Database(str(tmp_path / "db.sqlite"))
    try:
        acc = db.get_all_accounts()[0]
    finally:
        db.close()
    assert acc.phone == "+222"
    assert acc.session_name == os.path.join(str(sessions), "session_+222")
    assert os.path.exists(acc.session_name + ".session")
    assert not src.exists()
