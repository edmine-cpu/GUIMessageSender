#!/usr/bin/env python3
"""
Import exactly 3 archived tdata zips from Desktop into Teleton DB.

- Each zip is one account (named by internal user id).
- Converts tdata -> Telethon .session using opentele.
- Sets the FIXED proxy AT INSERT TIME (so no later proxy change events).
- This proxy is used ONLY for these 3 accounts (as requested).

Zips (on Desktop):
  232154233_tdata.zip
  237823033_tdata.zip
  239595734_tdata.zip

Proxy (dedicated to these 3):
  socks5://HAQ8Ssb68Vqyq9j:Osuq4hfAE62FJxF@109.203.162.149:52691
"""

import asyncio
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime

from opentele.td import TDesktop
from opentele.api import API
from opentele.exception import NoPasswordProvided

# === CONFIG (edit only if paths change) ===
DESKTOP = r"C:\Users\Administrator\Desktop"
ZIPS = [
    os.path.join(DESKTOP, "232154233_tdata.zip"),
    os.path.join(DESKTOP, "237823033_tdata.zip"),
    os.path.join(DESKTOP, "239595734_tdata.zip"),
]

# The proxy from your screenshot — set AT CREATION for these 3 only
PROXY = "socks5://HAQ8Ssb68Vqyq9j:Osuq4hfAE62FJxF@109.203.162.149:52691"

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "teleton.db")
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sessions")

FPRINT = {
    "device_model": "Desktop",
    "system_version": "Windows 10",
    "app_version": "5.6.3 x64",
    "lang_code": "ru",
    "api_id": 2040,
    "api_hash": "b18441a1ff607e10a989891a5462e627",
}

os.makedirs(SESSIONS_DIR, exist_ok=True)


def _get_existing_phones():
    conn = sqlite3.connect(DB)
    try:
        return {row[0] for row in conn.execute("SELECT phone FROM accounts")}
    finally:
        conn.close()


async def _import_one(td_account, session_path: str):
    """Convert one TDesktop account to Telethon session and return its phone."""
    api = API.TelegramDesktop.Generate()
    api.api_id = FPRINT["api_id"]
    api.api_hash = FPRINT["api_hash"]

    client = await td_account.ToTelethon(session=session_path, proxy=None, api=api)
    await client.connect()
    me = await client.get_me()
    phone = str(me.phone)
    if not phone.startswith("+"):
        phone = "+" + phone
    try:
        await client.disconnect()
    except Exception:
        pass
    return phone


def _cleanup_session(session_path: str):
    for ext in ("", "-journal", "-wal", "-shm"):
        p = session_path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


def _insert_account(phone: str, session_path: str, proxy: str):
    """Insert a new account or update session/proxy fields without resetting health."""
    conn = sqlite3.connect(DB)
    try:
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
                session_path, proxy,
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
            """, (
                phone, session_path, proxy,
                FPRINT["api_id"], FPRINT["api_hash"],
                FPRINT["device_model"], FPRINT["system_version"], FPRINT["app_version"],
                FPRINT["lang_code"], 1, "", "active", ""
            ))
        conn.commit()
    finally:
        conn.close()


def main():
    print("=== Import 3 archived TData zips with proxy set AT CREATION ===")
    print("Proxy for these 3 ONLY:", repr(PROXY))
    print("DB:", DB)
    print()

    existing = _get_existing_phones()
    print("Already in Teleton:", sorted(existing))
    print()

    added = []
    skipped = []
    errors = []

    for zip_path in ZIPS:
        if not os.path.isfile(zip_path):
            print(f"[SKIP] Zip not found: {zip_path}")
            continue

        zip_name = os.path.basename(zip_path)
        print(f"\n--- Processing {zip_name} ---")

        try:
            with tempfile.TemporaryDirectory(prefix="tdata3_") as tmp:
                with zipfile.ZipFile(zip_path) as z:
                    z.extractall(tmp)

                # Find tdata roots (prefer the folder that *is* the tdata dir containing key_datas)
                tdirs = []
                for root, dirs, files in os.walk(tmp):
                    if "key_datas" in files or any(f.startswith("D877F783") for f in files):
                        tdirs.append(root)

                # Prefer the direct tdata/ child if present
                preferred = [p for p in tdirs if os.path.basename(p).lower() == "tdata"]
                if preferred:
                    tdirs = preferred + [p for p in tdirs if p not in preferred]

                print(f"  Found {len(tdirs)} tdata root(s) inside zip")

                for tdir in tdirs:
                    try:
                        td = TDesktop(tdir)
                        for acc in td.accounts:
                            uid = str(getattr(acc, "UserId", "u"))
                            session_path = os.path.join(SESSIONS_DIR, f"session_{uid}")

                            _cleanup_session(session_path)

                            try:
                                phone = asyncio.run(_import_one(acc, session_path))
                            except NoPasswordProvided:
                                print(f"  [SKIP 2FA] uid={uid} in {zip_name}")
                                skipped.append((zip_name, uid))
                                continue
                            except Exception as ex:
                                print(f"  [ERR convert] {zip_name} uid={uid}: {ex}")
                                import traceback
                                traceback.print_exc()
                                errors.append((zip_name, uid, str(ex)))
                                continue

                            if phone in existing:
                                print(f"  [ALREADY] {phone} (from {zip_name}) — skipping insert")
                                skipped.append((zip_name, phone))
                                continue

                            # === KEY: insert with proxy RIGHT NOW, at creation ===
                            _insert_account(phone, session_path, PROXY)
                            print(f"  [+] IMPORTED {phone}  (uid={uid})  proxy={PROXY}")
                            added.append(phone)
                            existing.add(phone)

                    except Exception as ex:
                        print(f"  [ERR TDesktop] {tdir}: {ex}")
                        import traceback
                        traceback.print_exc()
                        errors.append((zip_name, tdir, str(ex)))

        except Exception as ex:
            print(f"[ERR ZIP] {zip_name}: {ex}")
            import traceback
            traceback.print_exc()
            errors.append((zip_name, "zip", str(ex)))

    print("\n" + "=" * 60)
    print("DONE")
    print("Added with proxy at creation:", added)
    print("Skipped:", skipped)
    if errors:
        print("Errors:", errors)

    # Show final state for this proxy
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("\n=== All accounts that have THIS proxy now ===")
    for r in conn.execute(
        "SELECT phone, custom_name, proxy, is_active, status FROM accounts WHERE proxy = ? ORDER BY phone",
        (PROXY,)
    ):
        print(" ", dict(r))

    print("\n=== All accounts (for reference) ===")
    for r in conn.execute(
        "SELECT phone, custom_name, proxy, is_active FROM accounts ORDER BY phone"
    ):
        print(" ", dict(r))
    conn.close()


if __name__ == "__main__":
    main()
