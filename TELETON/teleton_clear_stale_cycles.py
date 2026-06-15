import sqlite3, os
base = r'C:\Users\Administrator\Desktop\TELETON_NEW_RUN'
db = os.path.join(base, 'data', 'teleton.db')
con = sqlite3.connect(db)
cur = con.cursor()
rows = cur.execute('select id,name,enabled from cycle_campaigns where enabled=1 order by id').fetchall()
print('enabled_before=', rows)
cur.execute('update cycle_campaigns set enabled=0, updated_at=datetime("now") where enabled=1')
con.commit()
rows2 = cur.execute('select id,name,enabled from cycle_campaigns order by id').fetchall()
print('after=', rows2)
con.close()
