import sqlite3
conn = sqlite3.connect("data/teleton.db")
conn.row_factory = sqlite3.Row
print("=== CYCLE_CAMPAIGNS (basic) ===")
for r in conn.execute("SELECT * FROM cycle_campaigns ORDER BY name"):
    keys = [k for k in r.keys() if not k.startswith("_")]
    print({k: r[k] for k in keys})
print()
print("=== CYCLE_CAMPAIGN_ACCOUNTS ===")
for r in conn.execute("SELECT * FROM cycle_campaign_accounts"):
    print(dict(r))
print()
print("=== TARGET COUNTS (via join to campaigns) ===")
for r in conn.execute("""
    SELECT c.name, COUNT(ct.id) as total_targets
    FROM cycle_campaigns c
    LEFT JOIN cycle_targets ct ON ct.campaign_id = c.id
    GROUP BY c.id, c.name
    ORDER BY c.name
"""):
    print(dict(r))
conn.close()
