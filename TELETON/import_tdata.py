import sqlite3
import zipfile
import tempfile
import os
import asyncio
from datetime import datetime

from opentele.td import TDesktop
from opentele.api import API
from opentele.exception import NoPasswordProvided

DB = "data/teleton.db"
SESSIONS_DIR = "data/sessions"
ZIP = r"C:\Users\Administrator\Desktop\tdata.zip"  # default; change for one-off imports from ФЛЕШ.zip etc.
PROXY = ""  # identical to БРИДЖ and АЛИСА

FPRINT = {
    "device_model": "Desktop",
    "system_version": "Windows 10",
    "app_version": "5.6.3 x64",
    "lang_code": "ru",
    "api_id": 2040,
    "api_hash": "b18441a1ff607e10a989891a5462e627",
}

# Make paths absolute relative to this script so it works from any cwd
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(SCRIPT_DIR, DB)
SESSIONS_DIR = os.path.join(SCRIPT_DIR, SESSIONS_DIR)
os.makedirs(SESSIONS_DIR, exist_ok=True)

print("=== Importing TData from your Desktop zip ===")
print("All new accounts will use proxy =", repr(PROXY), "(same as the working ones)")

async def _import_one(acc, spath):
    api = API.TelegramDesktop.Generate()
    api.api_id = FPRINT["api_id"]
    api.api_hash = FPRINT["api_hash"]
    cl = await acc.ToTelethon(session=spath, proxy=None, api=api)
    await cl.connect()
    me = await cl.get_me()
    phone = "+" + str(me.phone) if not str(me.phone).startswith("+") else str(me.phone)
    try:
        await cl.disconnect()
    except Exception:
        pass
    return phone

added = []
with tempfile.TemporaryDirectory(prefix="tdata_imp_") as tmp:
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(tmp)
    tdirs = []
    for root, dirs, files in os.walk(tmp):
        if "key_datas" in files or any(f.startswith("D877F783") for f in files):
            tdirs.append(root)
    print("Found", len(tdirs), "TData folders in the zip")
    for tdir in sorted(tdirs):
        try:
            td = TDesktop(tdir)
            for acc in td.accounts:
                uid = str(getattr(acc, "UserId", "u"))
                spath = os.path.join(SESSIONS_DIR, "session_" + uid)
                # remove any old colliding session file from previous attempts/deletes
                for ext in ("", "-journal", "-wal", "-shm"):
                    p = spath + ext
                    if os.path.exists(p):
                        try: os.remove(p)
                        except: pass
                try:
                    phone = asyncio.run(_import_one(acc, spath))
                    conn = sqlite3.connect(DB)
                    existing = conn.execute(
                        "SELECT 1 FROM accounts WHERE phone=?", (phone,)
                    ).fetchone()
                    if existing:
                        conn.execute("""
                            UPDATE accounts
                            SET session_name=?, proxy=?, api_id=?, api_hash=?,
                                device_model=?, system_version=?, app_version=?,
                                lang_code=?
                            WHERE phone=?
                        """, (
                            spath, PROXY,
                            FPRINT["api_id"], FPRINT["api_hash"],
                            FPRINT["device_model"], FPRINT["system_version"],
                            FPRINT["app_version"], FPRINT["lang_code"],
                            phone,
                        ))
                    else:
                        conn.execute("""
                            INSERT INTO accounts
                            (phone, session_name, proxy, api_id, api_hash, device_model, system_version,
                             app_version, lang_code, is_active, custom_name, status, last_error_text)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (phone, spath, PROXY,
                              FPRINT["api_id"], FPRINT["api_hash"],
                              FPRINT["device_model"], FPRINT["system_version"], FPRINT["app_version"],
                              FPRINT["lang_code"], 1, "", "active", ""))
                    conn.commit()
                    conn.close()
                    print("  + IMPORTED", phone, "proxy=", repr(PROXY))
                    added.append(phone)
                except NoPasswordProvided:
                    print(f"  SKIP (2FA password required): uid={uid} tdir={tdir}")
                    print("       To import this one, either disable 2FA temporarily in Telegram Desktop using this tdata,")
                    print("       or edit _import_one to pass password=... to ToTelethon() and re-run.")
                except Exception as ex:
                    print("  ERR (per acc)", tdir, uid, ":", ex)
        except Exception as ex:
            print("  ERR (tdir)", tdir, ":", ex)

print("Import finished. Phones added:", added)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
print("\n=== RESULT: all accounts in Teleton ===")
for r in conn.execute("SELECT phone, custom_name, proxy, device_model, is_active FROM accounts ORDER BY phone"):
    print(" ", dict(r))
conn.close()
