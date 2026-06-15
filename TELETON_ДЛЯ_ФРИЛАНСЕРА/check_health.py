import sqlite3
conn = sqlite3.connect("data/teleton.db")
conn.row_factory = sqlite3.Row
print("=== DETAILED ACCOUNT HEALTH (all, focus on new ones) ===")
for r in conn.execute("SELECT phone, custom_name, proxy, is_active, status, last_error_text, error_today, actions_today, last_send_at FROM accounts ORDER BY custom_name, phone"):
    print(dict(r))
conn.close()
