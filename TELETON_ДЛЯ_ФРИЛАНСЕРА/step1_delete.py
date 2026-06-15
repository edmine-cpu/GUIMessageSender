import sqlite3
BAD = ['+17246484305', '+12368913226', '+447988550363']
conn = sqlite3.connect('data/teleton.db')
for p in BAD:
    conn.execute('DELETE FROM accounts WHERE phone=?', (p,))
    conn.execute('DELETE FROM cycle_campaign_accounts WHERE account_phone=?', (p,))
conn.commit()
print('Blocked phones deleted (if existed):', BAD)
print('Remaining accounts:')
for r in conn.execute('SELECT phone, custom_name, proxy, is_active FROM accounts ORDER BY phone'): print(' ', dict(r))
conn.close()
