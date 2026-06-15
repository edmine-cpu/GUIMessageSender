import sqlite3
conn = sqlite3.connect('data/teleton.db')
conn.row_factory = sqlite3.Row
print('=== ACCOUNTS ===')
for r in conn.execute('SELECT phone, custom_name, proxy, is_active FROM accounts ORDER BY phone'):
    print(dict(r))
print()
print('=== DISTINCT PROXY ON ACTIVE ===')
for r in conn.execute('SELECT DISTINCT proxy FROM accounts WHERE is_active=1'):
    print(repr(r[0]))
print()
print('=== CYCLE ASSIGNMENTS ===')
for r in conn.execute('SELECT campaign_name, account_phone FROM cycle_campaign_accounts'):
    print(dict(r))
conn.close()
