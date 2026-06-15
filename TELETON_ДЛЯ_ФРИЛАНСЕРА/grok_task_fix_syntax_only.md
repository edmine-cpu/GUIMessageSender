Work in C:\Users\Administrator\Desktop\TELETON_NEW_RUN.

Your previous changes left gui.py uncompilable:

py -3.12 -m py_compile gui.py database.py
fails with:
gui.py line 8258: SyntaxError: expected 'except' or 'finally' block

Narrow task:
- Fix only the syntax/indentation around the cyclic worker do()/thread() try/except/finally block near lines 8200-8280.
- Preserve the intended behavior:
  - OperationInterrupted is caught inside do()
  - db.set_cycle_campaign_enabled(campaign_id, False) runs in do() finally
  - outer thread catches unexpected exceptions, logs cycle error, clears thread local handler, emits cycle_done
- Do not modify send logic.
- Do not start real Telegram sending.

Verify:
py -3.12 -m py_compile gui.py database.py

If compile passes, stop and report the exact lines changed.
