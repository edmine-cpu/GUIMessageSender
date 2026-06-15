import sqlite3

PROXY = "socks5://jAGome9inQjtByd:iGoQn4cIBkteq3R@89.32.126.116:43438"

new_phones = [
    "+12513654685",
    "+15313493507",
    "+17792952990"
]

conn = sqlite3.connect("data/teleton.db")
for phone in new_phones:
    conn.execute("UPDATE accounts SET proxy = ? WHERE phone = ?", (PROXY, phone))
conn.commit()

print("Assigned the existing socks5 proxy to 3 new accounts:")
for phone in new_phones:
    r = conn.execute("SELECT phone, custom_name, proxy FROM accounts WHERE phone=?", (phone,)).fetchone()
    print(dict(r))

print("\n=== All accounts now ===")
for r in conn.execute("SELECT phone, custom_name, proxy FROM accounts ORDER BY phone"):
    p = r[2] if r[2] else "(empty)"
    print(r[0], "|", (r[1] or "").ljust(6), "|", p[:50] + ("..." if len(r[2] or "") > 50 else "") if r[2] else "(empty)")

conn.close()
print("\nDone. 3 of the new TData accounts now use the same proxy as the existing one (+12232686033).")
