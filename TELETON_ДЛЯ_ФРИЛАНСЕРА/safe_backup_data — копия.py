"""Safe backup for TELETON data.

Run from the bot folder before closing a frozen GUI. It creates a SQLite
backup of data/teleton.db and copies the rest of the data folder.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def _desktop_dir() -> Path:
    desktop = Path.home() / "Desktop"
    return desktop if desktop.exists() else Path.cwd()


def _copytree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def backup_sqlite(src_db: Path, dst_db: Path) -> bool:
    """Use SQLite online backup API; safer than copying a live DB file."""
    if not src_db.exists():
        return False
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    src = None
    dst = None
    try:
        src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(str(dst_db), timeout=30)
        with dst:
            src.backup(dst)
        return True
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()


def main() -> int:
    root = Path(__file__).resolve().parent
    data_dir = root / "data"
    src_db = data_dir / "teleton.db"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = _desktop_dir() / "TELETON_DATA_BACKUPS" / stamp
    backup_root.mkdir(parents=True, exist_ok=True)

    print(f"[~] Bot folder: {root}")
    print(f"[~] Backup folder: {backup_root}")

    if not data_dir.exists():
        print("[!] data folder not found")
        return 1

    db_ok = False
    if src_db.exists():
        try:
            db_ok = backup_sqlite(src_db, backup_root / "teleton.db")
            print(f"[+] SQLite backup created: {backup_root / 'teleton.db'}")
        except Exception as exc:
            print(f"[!] SQLite backup failed: {type(exc).__name__}: {exc}")
            print("[~] Falling back to raw DB copy")
            for name in ("teleton.db", "teleton.db-wal", "teleton.db-shm"):
                _copytree(data_dir / name, backup_root / "raw_db_copy" / name)
    else:
        print("[!] data/teleton.db not found")

    raw_data = backup_root / "data_files"
    for item in data_dir.iterdir():
        if item.name in {"teleton.db", "teleton.db-wal", "teleton.db-shm"}:
            continue
        _copytree(item, raw_data / item.name)

    if src_db.exists():
        print(f"[i] Source DB size: {src_db.stat().st_size:,} bytes")
    if db_ok:
        print(f"[i] Backup DB size: {(backup_root / 'teleton.db').stat().st_size:,} bytes")
    print("[+] Backup complete. Now it is safer to restart the GUI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
