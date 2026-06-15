# Teleton P0: repair campaign start regression after "start all" button

Work in: `C:\Users\Administrator\Desktop\TELETON_NEW_RUN`

## Context

The app is a Python/customtkinter GUI for Telegram automation. Recent changes added a new button similar to "Запустить все". After that, the user reports:

- single cyclic campaign Start no longer reliably starts;
- "Запустить все" also does not behave reliably;
- UI can show a running state while nothing actually happens;
- the user needs the original single campaign flow restored, while keeping the new start-all button.

Important: do not change campaign volume, delays, target lists, account lists, anti-spam behavior, Telegram limits, or sending logic. This task is about fixing the local GUI/runtime regression and making start/stop state truthful.

## Observed concrete failures

From `data\logs\teleton_gui_crash.log`:

```text
AttributeError: 'BroadcastFrame' object has no attribute 'log'
```

This happened inside `BroadcastFrame._start_cycle` and also around `on_show`, at code paths using `self.log.append(...)`.

From `data\logs\teleton_gui.log`:

```text
name 'format_account' is not defined
```

This appears in the cyclic start/diagnostic path.

Also observed: multiple Teleton GUI processes may be open at the same time. Do not solve this by killing processes from code, but the code must not silently fail or leave the UI in a false "running" state after an exception.

## Required fixes

1. **Fix the single Start button regression.**
   - `_start_cycle` must not crash because of logging/diagnostics.
   - If config is invalid, it must show/log a clear reason and return cleanly.
   - If config is valid, it must create/start the cyclic worker the same way it did before the new button work.

2. **Fix invalid logging calls.**
   - In `BroadcastFrame`, do not call `self.log.append(...)` unless `self.log` is actually initialized and intended.
   - Find the existing logging mechanism used by this GUI/frame and use it consistently.
   - Prefer adding a tiny helper on `BroadcastFrame`, e.g. `_append_log(text)`, only if it matches local style.

3. **Fix `format_account` undefined.**
   - Either import/define the helper locally or remove/replace the diagnostic use.
   - The start path must not crash if account formatting fails.

4. **Keep "start all" button, but isolate it from single Start.**
   - The new button must not override/break the old single-campaign start behavior.
   - If only one campaign is selected, old Start should still work normally.

5. **Make UI state truthful.**
   - If start fails before the worker starts, UI must return to stopped state.
   - Stop button must not remain stuck because start crashed.
   - Logs/status should say why start did not proceed.

6. **Minimal diff.**
   - Prefer `gui.py` and targeted tests only.
   - Do not do broad refactors.
   - Do not change DB schema unless absolutely necessary for this regression.

## Validation required

Run these commands from `C:\Users\Administrator\Desktop\TELETON_NEW_RUN`:

```bat
py -3.12 -m py_compile gui.py
py -3.12 -m pytest tests\ -q
```

Add or update focused regression tests if practical. Static/AST tests are acceptable, for example:

- no direct `self.log.append(...)` in `BroadcastFrame` unless `self.log` is initialized;
- `_start_cycle` references the safe log helper;
- no undefined `format_account` usage in the cyclic start path.

## Report

At the end, report in Russian:

1. exact root cause found;
2. files changed;
3. what was changed;
4. test results;
5. remaining risks.

