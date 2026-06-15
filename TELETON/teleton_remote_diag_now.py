import glob
import os
import sqlite3


DB_PATH = r"C:\Users\Administrator\Desktop\TELETON_NEW_RUN\data\teleton.db"


def main():
    print("db_exists", os.path.exists(DB_PATH), "size", os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else None)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    for table in ("cycle_campaigns", "cycle_targets", "accounts"):
        try:
            row = con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            print("table", table, "count", row["c"])
        except Exception as exc:
            print("table", table, "err", repr(exc))

    print("cycle_campaigns")
    try:
        for row in con.execute("SELECT id,name,enabled,created_at FROM cycle_campaigns ORDER BY id DESC LIMIT 12"):
            print(dict(row))
    except Exception as exc:
        print("cycle_campaigns err", repr(exc))

    print("accounts")
    try:
        for row in con.execute("SELECT phone,enabled,health,proxy FROM accounts ORDER BY phone LIMIT 20"):
            print(dict(row))
    except Exception as exc:
        print("accounts err", repr(exc))

    con.close()

    print("recent_logs")
    logs = glob.glob(r"C:\Users\Administrator\Desktop\TELETON_NEW_RUN\data\logs\*")
    for path in sorted(logs, key=os.path.getmtime, reverse=True)[:8]:
        print(os.path.basename(path), int(os.path.getmtime(path)), os.path.getsize(path))


if __name__ == "__main__":
    main()
