import sqlite3

PROXY = "socks5://jAGome9inQjtByd:iGoQn4cIBkteq3R@89.32.126.116:43438"

# 3 of the newly imported from tdata.zip (user asked for 2-3)
phones = [
    "+12513654685",
    "+15313493507",
    "+17792952990"
]

conn = sqlite3.connect("data/teleton.db")
conn.row_factory = sqlite3.Row

updated = 0
for phone in phones:
    cur = conn.execute("UPDATE accounts SET proxy = ? WHERE phone = ?", (PROXY, phone))
    updated += cur.rowcount

conn.commit()

print("Updated", updated, "accounts to use the existing socks5 proxy.")
print()

print("=== The 3 accounts that now have proxy ===")
for phone in phones:
    r = conn.execute("SELECT phone, custom_name, proxy FROM accounts WHERE phone = ?", (phone,)).fetchone()
    print(dict(r))

print()
print("=== Full list of all accounts + their proxies ===")
for r in conn.execute("SELECT phone, custom_name, proxy FROM accounts ORDER BY phone"):
    p = r["proxy"] if r["proxy"] else "(empty)"
    print(r["phone"].ljust(16), "|", (r["custom_name"] or "").ljust(8), "|", p)

conn.close()
print()
print("Done. These 3 new accounts now use the same proxy as the one that already had it (+12232686033).")
