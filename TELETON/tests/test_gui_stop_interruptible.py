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
        "_cancel_task_bounded",
        "_run_loop",
        "STOP_CANCEL_GRACE_SECONDS",
        "STOP_LOOP_CLEANUP_GRACE_SECONDS",
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
        or (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in wanted for target in node.targets)
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


@pytest.mark.asyncio
async def test_await_interruptibly_does_not_hang_when_coroutine_ignores_cancel_on_stop():
    helpers = _load_stop_helpers()
    stop_event = threading.Event()
    release = asyncio.Event()
    ignored_cancel = {"value": False}

    async def ignores_cancel():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            ignored_cancel["value"] = True
            while not release.is_set():
                await asyncio.sleep(0.01)

    async def request_stop():
        await asyncio.sleep(0.02)
        stop_event.set()

    start = time.monotonic()
    stopper = asyncio.create_task(request_stop())
    try:
        with pytest.raises(helpers["OperationInterrupted"]):
            await helpers["_await_interruptibly"](
                ignores_cancel(),
                stop_event,
                op_name="test",
                label="stubborn",
                timeout=5,
                quantum=0.01,
            )
    finally:
        release.set()
        await stopper
        await asyncio.sleep(0.05)

    elapsed = time.monotonic() - start
    assert ignored_cancel["value"] is True
    assert elapsed < helpers["STOP_CANCEL_GRACE_SECONDS"] + 0.5


@pytest.mark.asyncio
async def test_await_interruptibly_does_not_hang_when_coroutine_ignores_cancel_on_timeout():
    helpers = _load_stop_helpers()
    release = asyncio.Event()

    async def ignores_cancel():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            while not release.is_set():
                await asyncio.sleep(0.01)

    start = time.monotonic()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await helpers["_await_interruptibly"](
                ignores_cancel(),
                threading.Event(),
                op_name="test",
                label="timeout",
                timeout=0.03,
                quantum=0.01,
            )
    finally:
        release.set()
        await asyncio.sleep(0.05)

    elapsed = time.monotonic() - start
    assert elapsed < helpers["STOP_CANCEL_GRACE_SECONDS"] + 0.5


def test_run_loop_cleanup_is_bounded_when_pending_task_ignores_cancel():
    helpers = _load_stop_helpers()

    async def main():
        async def ignores_cancel_forever():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                while True:
                    await asyncio.sleep(0.05)

        asyncio.create_task(ignores_cancel_forever())
        return "ok"

    loop = asyncio.new_event_loop()
    start = time.monotonic()
    result = helpers["_run_loop"](loop, main())
    elapsed = time.monotonic() - start

    assert result == "ok"
    assert loop.is_closed()
    assert elapsed < helpers["STOP_LOOP_CLEANUP_GRACE_SECONDS"] + 0.5


def test_mass_stop_does_not_finalize_running_state():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    mass_stop = methods.get("_mass_stop_everything", "")
    assert mass_stop
    assert "self._running = False" not in mass_stop
    assert 'self._active_op_name = ""' not in mass_stop
    assert "Остановка запрошена" in mass_stop


def test_mass_start_can_restart_current_cycle_after_stop_all():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    mass_start = methods.get("_mass_start_everything", "")
    assert mass_start
    assert "Включённых циклов нет" in mass_start
    assert "self._start_cycle()" in mass_start
    assert "текущий цикл" in mass_start


def test_mass_start_does_not_force_clear_regular_running_flag():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    mass_start = methods.get("_mass_start_everything", "")
    assert mass_start
    assert "db.get_pending_tasks(task_type=\"broadcast\")" in mass_start
    assert "self._running = False" not in mass_start
    assert "_regular_worker_alive()" in mass_start


def test_broadcastframe_workers_use_interruptible_wait_wrappers():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    expectations = {
        "_start_mention": "async def _mention_wait",
        "_check_and_clean": "async def _check_wait",
        "_start_broadcast": "async def _broadcast_wait",
    }
    for method_name, wrapper in expectations.items():
        body = methods.get(method_name, "")
        assert body, f"{method_name} must exist"
        assert wrapper in body
        assert "_await_interruptibly(" in body


def test_broadcastframe_workers_capture_per_run_stop_events():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    expectations = {
        "_start_mention": '"mention"',
        "_check_and_clean": '"check"',
        "_start_broadcast": '"broadcast"',
    }
    for method_name, key in expectations.items():
        body = methods.get(method_name, "")
        assert "stop_event = threading.Event()" in body
        assert f"_begin_regular_run({key}, stop_event)" in body
        assert "self._stop_event.clear()" not in body
        assert "_await_interruptibly(\n" in body
        assert "stop_event," in body


def test_regular_worker_done_messages_use_run_ids_and_stale_guard():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    on_queue = methods.get("on_queue_message", "")
    assert '("mention_done", {"run_id": run_id})' in methods["_start_mention"]
    assert '("check_done", {"run_id": run_id})' in methods["_check_and_clean"]
    assert '("broadcast_done", {"run_id": run_id})' in methods["_start_broadcast"]
    assert "_regular_run_ids.get(key) != run_id" in on_queue
    assert '_regular_run_ids.get("check") != run_id' in on_queue


def test_stop_watchdog_is_wired_to_current_stop():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    assert "STOP_UI_FORCE_MS" in _load_gui_source()
    assert "_schedule_regular_stop_watchdog" in methods
    assert "_force_regular_stop_ui" in methods
    assert "_schedule_regular_stop_watchdog()" in methods.get("_stop_current_process", "")
    assert "_set_all_regular_stop_events()" in methods.get("_mass_stop_everything", "")


def test_stop_helpers_do_not_use_unbounded_gather_for_cancellation():
    src = _load_gui_source()
    assert "asyncio.gather(task" not in src
    assert "asyncio.gather(*pending" not in src
    assert "STOP_CANCEL_GRACE_SECONDS" in src
    assert "STOP_LOOP_CLEANUP_GRACE_SECONDS" in src


def test_broadcastframe_workers_do_not_await_key_telegram_calls_directly():
    methods = _find_class_methods(_load_gui_source(), "BroadcastFrame")
    method_names = [
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
