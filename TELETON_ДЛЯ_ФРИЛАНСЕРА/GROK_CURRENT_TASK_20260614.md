# Teleton P0 Stability Task

You are working in `C:\Users\Administrator\Desktop\TELETON_NEW_RUN`.

Important workflow:
- Read `GROK_LAST_SESSION.md` first. It contains the previous Grok/Teleton context.
- Work only on stability, launchability, UI responsiveness, and correctness of configured cyclic campaign control.
- Do not increase sending volume, bypass Telegram limits, reduce delays, or add any anti-ban/bypass behavior.
- Keep changes minimal and testable.

Current user problem:
1. Teleton GUI currently fails to start from CLI:
   `NameError: name 'builtins' is not defined` at `gui.py`, around `_original_print = builtins.print`.
   Fix startup safely.
2. User wants a one-click button that starts all already-enabled/configured cyclic campaigns.
   This must be the safe pair to the existing "stop/disable all" button:
   - start only campaigns that are already enabled in DB;
   - do not enable disabled campaigns;
   - do not change delays, targets, limits, message source, or account selection;
   - do not duplicate already running campaigns;
   - skip campaigns without targets/accounts/text and show/log a clear reason.
3. The cyclic UI is confusing and status text overlaps/gets hidden. Improve only the P0-level clarity:
   - make the status line readable;
   - show whether a campaign is running or stopped;
   - show active runner count if available;
   - do not redesign the whole app.
4. Stop must remain clickable while any cyclic worker is running or UI is busy.
5. Remove/ignore temporary helper files from previous manual attempts if they are not needed:
   `patch_start_enabled_button.py`, `restart_teleton_once.ps1`, `start_teleton_once.bat`.

Required validation:
- Run `py -3.12 -m py_compile gui.py`.
- Run `py -3.12 -m pytest tests\ -q`.
- If tests fail, fix only failures caused by your changes.

Deliverable:
- Make the code changes yourself.
- At the end, print a concise report in Russian:
  1. Что изменил.
  2. Какие файлы изменил.
  3. Какие тесты прошли.
  4. Какие риски остались.

