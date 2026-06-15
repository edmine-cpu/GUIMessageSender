import sqlite3
conn = sqlite3.connect("data/teleton.db")
conn.row_factory = sqlite3.Row
print("=== Полный актуальный список аккаунтов и прокси ===")
print("телефон            | имя      | прокси")
print("-" * 70)
for r in conn.execute("SELECT phone, custom_name, proxy FROM accounts ORDER BY phone"):
    p = r["proxy"] if r["proxy"] else "(пустой)"
    print(r["phone"].ljust(18), "|", (r["custom_name"] or "").ljust(8), "|", p)
print()
print("Всего аккаунтов:", conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
conn.close()
