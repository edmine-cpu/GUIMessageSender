import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(r"C:\Users\Administrator\Desktop\TELETON_NEW_RUN")
DB = ROOT / "data" / "teleton.db"


def out(label, value):
    print(f"\n=== {label} ===")
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        print(value)


def rows(con, table, query=None):
    try:
        con.row_factory = sqlite3.Row
        if query is None:
            query = f"select * from {table} limit 20"
        return [dict(r) for r in con.execute(query)]
    except Exception as exc:
        return {"error": repr(exc)}


def main():
    out("time_utc", datetime.now(timezone.utc).isoformat())
    out("root_exists", ROOT.exists())
    out("db_exists", DB.exists())

    if DB.exists():
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
        out("tables_interest", [t for t in tables if any(x in t.lower() for x in ("account", "cycle", "campaign", "target", "log"))])
        for table in ("accounts", "cycle_campaigns", "cycle_campaign_accounts", "cycle_state"):
            if table in tables:
                out(table, rows(con, table))
        if "cycle_targets" in tables:
            out("cycle_targets_count_by_campaign", rows(con, "cycle_targets", "select campaign_id, count(*) as cnt, sum(case when status='active' then 1 else 0 end) as active_cnt, sum(case when last_error is not null and last_error<>'' then 1 else 0 end) as with_error from cycle_targets group by campaign_id order by campaign_id"))
            out("cycle_targets_sample", rows(con, "cycle_targets", "select * from cycle_targets order by campaign_id, pos limit 12"))
            out("cycle_targets_recent", rows(con, "cycle_targets", "select * from cycle_targets where last_sent_at is not null or last_error is not null order by coalesce(last_sent_at, updated_at, '') desc limit 12"))
        con.close()

    logs_dir = ROOT / "data" / "logs"
    if logs_dir.exists():
        log_files = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
        out("recent_log_files", [{"name": p.name, "size": p.stat().st_size, "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat()} for p in log_files])
        for p in log_files[:3]:
            try:
                data = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception as exc:
                out(f"log_tail_{p.name}", f"read error: {exc!r}")
                continue
            out(f"log_tail_{p.name}", "\n".join(data[-80:]))
    else:
        out("logs_dir", "missing")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"DIAG_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
