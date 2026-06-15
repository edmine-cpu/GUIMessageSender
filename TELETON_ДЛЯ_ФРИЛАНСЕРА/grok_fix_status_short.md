Project: Teleton in C:\Users\Administrator\Desktop\TELETON_NEW_RUN
Fix only cyclic broadcast start/status UI.
Bug: user clicks Start or Start all. Logs show start attempt with targets/accounts/position, but UI still says stopped.
Also status line shows empty_text/no_permission/errors. User needs honest state.
Inspect only: BroadcastFrame._start_cycle, _cycle_loop, _cycle_refresh_cycle_buttons, _cycle_status_snapshot, _mass_start_everything, _cycle_runners/_cycle_stop_events.
Requirements:
1 If cycle worker is alive, UI must show running, not stopped.
2 If start failed or worker stopped immediately, UI/log must show exact reason: empty_text, no_permission, no targets, no accounts, no enabled campaigns.
3 Start all must not claim cycles started unless at least one runner is alive.
4 Stop must clear running state correctly.
5 Do NOT change Telegram sending logic, limits, speed, DB schema, parser, comments, TData, accounts, proxy.
6 Keep patch small. Add/update regression tests if possible.
Run: py -3.12 -m py_compile gui.py database.py models.py sender.py
Run: py -3.12 -m pytest tests\test_cycle_campaigns.py tests\test_cycle_start_regression.py -q
Return changed files, root cause, behavior changed, test results.
