# Teleton P0 Regression Fix: cycle start is broken

You are working in:
`C:\Users\Administrator\Desktop\TELETON_NEW_RUN`

Important:
- Read `GROK_LAST_SESSION.md` first for context, but do not follow old broad refactor suggestions.
- This is a regression fix only.
- Do not increase sending volume, bypass Telegram limits, reduce delays, alter targets/text/accounts, or add anti-ban/bypass behavior.
- Keep changes minimal. Prefer `gui.py` only unless a direct bug is outside it.

Observed facts:
- Your previous run changed a lot of code (`gui.py`, `database.py`, `models.py`, `sender.py`). Tests passed, but user-level GUI behavior regressed.
- User reports:
  1. New button "запустить все" / "Включённые" does not work.
  2. Even normal single cyclic campaign start no longer works.
- Current Teleton process is alive, but app actions are not clearly logging to current daily logs.
- Existing tests can pass while the GUI start path is broken, so inspect the actual callbacks/state transitions.

Primary goal:
Restore reliable cyclic campaign start behavior:

1. Single selected campaign start must work again.
   - The existing "Старт" button in the cyclic tab must start the selected campaign when its configuration is valid.
   - It must not silently do nothing. If start is rejected, log and show a clear reason.
   - It must keep the UI responsive.

2. The new "start enabled campaigns" button must work as a safe helper.
   - Start only already-enabled/configured campaigns.
   - Do not enable disabled campaigns.
   - Do not duplicate campaigns that are already running.
   - Skip invalid campaigns with a clear log reason.
   - It must not break or block the old single start path.

3. Stop must remain usable.
   - Stop should request cooperative cancellation for active cyclic workers.
   - Do not leave the UI in a state where Start stays disabled forever after stopping.

4. Status must be clear enough for manual verification.
   - Log when the user clicks single Start.
   - Log when the user clicks start-enabled.
   - Log campaign id/name, target count, account count, and exact rejection reason if any.
   - Do not redesign the whole UI.

Specific things to inspect:
- `_start_cycle`, `_stop_cycle`, `_cycle_start_enabled_campaigns`, `_resume_enabled_cycles`, `_cycle_reject_start`, `_cycle_ui_busy`, button state updates, and any guard that returns early.
- Check whether `_cycle_running`, per-campaign runner maps, or disabled buttons incorrectly block starting.
- Check whether "all active"/selected account logic now returns zero usable accounts.
- Check whether target loading from template/base now returns zero after the previous edits.
- Check whether prior "Stop/disable all" logic sets DB flags that the start paths never restore.
- Check whether the new button calls a method that only resumes dead workers but never starts a selected valid campaign.

Validation required:
1. `py -3.12 -m py_compile gui.py`
2. `py -3.12 -m pytest tests\ -q`
3. Add or update a focused test if possible for the regression, without huge refactor. If GUI tests are impractical, add a small unit-level test around the corrected non-UI helper.

Deliverable:
- Make the minimal code changes.
- At the end, print a concise Russian report:
  1. Что было сломано.
  2. Что исправил.
  3. Какие файлы изменил.
  4. Какие тесты прошли.
  5. Что пользователю проверить вручную в GUI.
