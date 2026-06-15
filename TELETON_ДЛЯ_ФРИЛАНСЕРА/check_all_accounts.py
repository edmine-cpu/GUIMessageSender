import sqlite3
conn = sqlite3.connect("data/teleton.db")
conn.row_factory = sqlite3.Row
print("=== ВСЕ АККАУНТЫ + ПРОКСИ + СТАТУС ===")
for r in conn.execute("SELECT phone, custom_name, proxy, is_active, status, last_error_text, health FROM accounts ORDER BY phone"):
    p = r["proxy"] if r["proxy"] else "(empty)"
    print(f'{r["phone"]} | { (r["custom_name"] or "-").ljust(8) } | proxy: {p[:50]} | active:{r["is_active"]} | status:{r["status"]} | err:{(r["last_error_text"] or "-")[:40]}')
print()
print("=== КАМПАНИИ И ПРИВЯЗКИ ===")
for r in conn.execute("""
    SELECT c.name as camp, c.enabled, ca.account_phone, a.custom_name as acc_name, a.proxy
    FROM cycle_campaigns c
    LEFT JOIN cycle_campaign_accounts ca ON ca.campaign_id = c.id
    LEFT JOIN accounts a ON a.phone = ca.account_phone
    ORDER BY c.name
"""):
    prox = r["proxy"] if r["proxy"] else "(empty)"
    print(f'{r["camp"].ljust(10)} | enabled:{r["enabled"]} | {r["account_phone"] or "-"} ({r["acc_name"] or "-"}) | proxy:{prox[:40]}')
print()
print("=== ЦЕЛЕЙ ПО КАМПАНИЯМ ===")
for r in conn.execute("""
    SELECT c.name, COUNT(ct.id) as total
    FROM cycle_campaigns c
    LEFT JOIN cycle_targets ct ON ct.campaign_id = c.id
    GROUP BY c.id, c.name
    ORDER BY c.name
"""):
    print(dict(r))
conn.close()
