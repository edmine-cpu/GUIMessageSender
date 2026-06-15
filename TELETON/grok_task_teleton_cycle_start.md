You are auditing a Windows Python desktop app named Teleton in:

C:\Users\Administrator\Desktop\TELETON_NEW_RUN

The user reports:
- Cyclic broadcast does not actually start after pressing Start.
- "Start all" was added earlier and appears to have broken even single campaign start.
- Stop/full stop previously left stale enabled campaigns in SQLite.
- UI often claims a campaign is running while no worker is alive and no progress happens.
- The app freezes or feels unresponsive around broadcast controls.

Important constraints:
- Do NOT start real Telegram sending.
- Do NOT optimize, increase, or run mass messaging.
- You may inspect code, logs, DB state, and run tests.
- You may patch code only for reliability/state correctness/UI diagnostics.
- Verification must be via unit tests, dry-run paths, direct DB state checks, and logs.
- Keep the patch small and focused. Do not redesign the whole app unless absolutely necessary.

Known context:
- Previous bug: _disable_all_cycle_campaigns called db.set_cycle_campaign_enabled(name, False), but database.py expects campaign_id:int. That was patched to use id.
- Recent tests previously passed: py -3.12 -m pytest tests\ -q
- DB path: data\teleton.db
- Logs path: data\logs
- The app uses customtkinter, Telethon, SQLite.

Your task:
1. Inspect cycle broadcast start/stop code in gui.py and related database.py methods.
2. Determine why pressing Start can result in:
   - UI says running but no worker alive;
   - Start button disabled forever;
   - campaign enabled=1 but no actual loop;
   - no visible error to the user.
3. Check whether "Start all" / "Запустить все" shares state incorrectly with single campaign start.
4. Check whether campaign/account selection is stored correctly for one campaign and multiple campaigns.
5. Check if DB writes can leave stale state when worker thread fails at startup.
6. Patch P0 reliability issues only:
   - If worker startup fails, immediately revert enabled/status and show/log exact error.
   - If a stale enabled campaign is detected but no live worker exists, auto-mark it stopped or expose a clear UI/log warning.
   - Ensure Start can be pressed again after failed startup.
   - Ensure Stop reliably cancels the active worker and clears enabled state.
   - Ensure "Start all" does not break normal single start.
   - Add or update tests for these behaviors if possible.
7. Do not change Telegram send logic except adding dry-run guards or better error handling.
8. Do not remove user data.

After work, report:
- files changed;
- exact root cause(s);
- exact code changes;
- tests run and output;
- remaining risks;
- how the user should safely verify in GUI without real sending.
