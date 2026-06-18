import ast
import asyncio
import os
import threading
import time

import pytest


def _load_gui_source() -> str:
    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "gui.py")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _find_class_methods(src: str, class_name: str) -> dict[str, str]:
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            methods = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[item.name] = ast.get_source_segment(src, item) or ""
            return methods
    return {}


def _load_stop_helpers():
    src = _load_gui_source()
    tree = ast.parse(src)
    wanted = {
        "OperationInterrupted",
        "_raise_if_stop_requested",
        "_await_interruptibly",
    }
    nodes = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "OperationInterrupted"
        )
        or (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in wanted
        )
    ]
    namespace = {
        "asyncio": asyncio,
        "threading": threading,
        "time": time,
    }
    code = compile(ast.Module(body=nodes, type_ignores=[]), filename="gui.py", mode="exec")
    exec(code, namespace)
    return namespace


@pytest.mark.asyncio
async def test_await_interruptibly_cancels_pending_coroutine_on_stop():
    helpers = _load_stop_helpers()
    stop_event = threading.Event()
    cancelled = {"value": False}

    async def never_returns():
        try:
            await asyncio.sleep(60)
        finally:
            cancelled["value"] = True

    async def request_stop():
        await asyncio.sleep(0.03)
        stop_event.set()

    stopper = asyncio.create_task(request_stop())
    try:
        with pytest.raises(helpers["OperationInterrupted"]):
            await helpers["_await_interruptibly"](
                never_returns(),
                stop_event,
                op_name="test",
                label="hang",
                timeout=5,
                quantum=0.01,
            )
    finally:
        await stopper

    assert cancelled["value"] is True


def test_mass_stop_does_not_finalize_running_state():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    mass_stop = methods.get("_mass_stop_everything", "")
    assert mass_stop
    assert "self._running = False" not in mass_stop
    assert 'self._active_op_name = ""' not in mass_stop
    assert "Остановка запрошена" in mass_stop


def test_broadcastframe_workers_use_interruptible_wait_wrappers():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    expectations = {
        "_start_quick_broadcast": "async def _quick_wait",
        "_start_mention": "async def _mention_wait",
        "_check_and_clean": "async def _check_wait",
        "_start_broadcast": "async def _broadcast_wait",
    }
    for method_name, wrapper in expectations.items():
        body = methods.get(method_name, "")
        assert body, f"{method_name} must exist"
        assert wrapper in body
        assert "_await_interruptibly(" in body


def test_broadcastframe_workers_do_not_await_key_telegram_calls_directly():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    method_names = [
        "_start_quick_broadcast",
        "_start_mention",
        "_check_and_clean",
        "_start_broadcast",
    ]
    forbidden = [
        "await sender.connect()",
        "await ensure_chat_access(",
        "await sender.get_saved_messages(",
        "await sender.send_broadcast_message(",
        "await sender.send_mention_message(",
        "await sender.send_dm(",
        "await sender.client(",
        "await sender.client.get_entity(",
        "await asyncio.sleep(delay)",
    ]
    for method_name in method_names:
        body = methods.get(method_name, "")
        assert body, f"{method_name} must exist"
        for token in forbidden:
            assert token not in body, f"{method_name} still contains direct {token}"


def test_stop_current_process_uses_worker_liveness_not_only_running_flag():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    stop_current = methods.get("_stop_current_process", "")
    assert "_regular_worker_alive()" in stop_current
    assert 'self._worker_alive("_quick_thread")' in stop_current
    assert 'self._worker_alive("_broadcast_thread")' in stop_current
    assert 'self._worker_alive("_mention_thread")' in stop_current
    assert 'self._worker_alive("_check_thread")' in stop_current


def test_audiences_dm_has_stop_event_and_interruptible_sleep():
    methods = _find_class_methods(_load_gui_source(), "AudiencesFrame")
    start_dm = methods.get("_start_dm", "")
    stop_dm = methods.get("_stop_dm", "")
    assert start_dm and stop_dm
    assert "self._stop_event.clear()" in start_dm
    assert "async def _dm_wait" in start_dm
    assert "_await_interruptibly(" in start_dm
    assert "_sleep_interruptibly(" in start_dm
    assert "await asyncio.sleep(delay)" not in start_dm
    assert "self._stop_event.set()" in stop_dm
