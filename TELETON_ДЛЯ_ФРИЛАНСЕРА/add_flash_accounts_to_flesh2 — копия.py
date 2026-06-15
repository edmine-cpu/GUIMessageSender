#!/usr/bin/env python3
"""
Add NEW TG Desktop accounts (ready portable instances) to existing ФЛЕШ 2,
taking the accounts/TData from the flash folder on Desktop (the ТГ collection, excluding the 3 'в работе' we already did).

For each new TData found under the flash source:
- Create a subfolder in ФЛЕШ 2 named after the phone/folder identifier.
- Copy the base from the empty 'Telegram Desktop' (exe + modules).
- Inject the tdata using robocopy (robust to locks).
- Launch the instance so the window opens immediately.

This gives 'сразу с декстопом' ready Telegram windows for the accounts from the flash.
"""
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

DESKTOP = r"C:\Users\Administrator\Desktop"
EMPTY_TG = os.path.join(DESKTOP, "Telegram Desktop")
FLESH2 = os.path.join(DESKTOP, "ФЛЕШ 2")
FLASH_SOURCE = os.path.join(DESKTOP, "ТГ")  # the main flash dump folder containing the TData (including 'флеш' sub)

# The 3 we already added previously from 'в работе' — skip them
ALREADY_DONE_PHONES = {
    "+18023057895",
    "+905482809547",
    "+447446750531",
}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def find_all_tdata(base: str):
    """Find all valid TData roots under the flash source folder."""
    tdatas = []
    for root, dirs, files in os.walk(base):
        if "key_datas" in files or "key_datass" in files:
            tdatas.append(root)
            continue
        # Also classic hex auth dir (D877F... etc)
        for d in list(dirs):
            if re.fullmatch(r"[0-9A-Fa-f]{16}", d):
                # The parent of this hex dir is usually the tdata root
                tdatas.append(root)
                # Don't descend into it
                dirs.remove(d)
    # Dedup and prefer the 'tdata' subfolder when present
    cleaned = []
    seen = set()
    for p in sorted(set(tdatas)):
        if p in seen:
            continue
        # If this is a parent and has a 'tdata' child that is the real one, prefer the child
        tdata_child = os.path.join(p, "tdata")
        if os.path.isdir(tdata_child) and os.path.exists(os.path.join(tdata_child, "key_datas")):
            real = tdata_child
        else:
            real = p
        if real not in seen:
            seen.add(real)
            cleaned.append(real)
    return cleaned

def sanitize_name(raw: str) -> str:
    """Make a nice folder name from phone/folder like '+1 223 268 6033'."""
    name = re.sub(r"[^0-9+]", "", raw) or raw
    if not name.startswith("+") and name[0].isdigit():
        name = "+" + name
    return name or raw.replace(" ", "_")

def is_likely_phone_folder(path: str) -> bool:
    base = os.path.basename(path)
    # Matches +1 223... or +1223... or 8xxxxxxxxx etc.
    return bool(re.search(r"\+?\d[\d\s]{5,}", base)) or base.startswith("+")

def main():
    log("=== Adding NEW accounts from the flash folder (папка флеш / ТГ collection) to ФЛЕШ 2 ===")

    if not os.path.isdir(FLASH_SOURCE):
        log(f"ERROR: Flash source folder not found: {FLASH_SOURCE}")
        return

    # Discover TData
    log(f"Scanning for TData under flash source: {FLASH_SOURCE}")
    all_tdata = find_all_tdata(FLASH_SOURCE)
    log(f"Found {len(all_tdata)} TData locations total")

    # Filter to the 'new' ones (not the 3 we already did in 'в работе')
    new_tdatas = []
    for p in all_tdata:
        # Skip if path contains 'в работе' (we did those)
        if "в работе" in p:
            continue
        # Try to extract a phone-like identifier from the path
        # Common patterns: ...\+1 223 268 6033\tdata   or  .../ +phone / tdata
        parent = os.path.basename(os.path.dirname(p)) if os.path.basename(p).lower() == "tdata" else os.path.basename(p)
        if any(phone in parent for phone in ALREADY_DONE_PHONES):
            continue
        new_tdatas.append((p, parent))

    if not new_tdatas:
        log("No *new* TData found to add (all remaining were either already done or not valid).")
        log("If you meant the top-level +phone folders under ТГ, they should have been picked up.")
        return

    log(f"Will create {len(new_tdatas)} new instances in ФЛЕШ 2:")
    for p, ident in new_tdatas:
        log(f"  - {ident}  (from {p})")

    # Ensure ФЛЕШ 2 exists (we add to the existing one)
    os.makedirs(FLESH2, exist_ok=True)

    # Base files to copy for a working portable TG
    base_files = ["Telegram.exe", "Updater.exe"]
    base_dirs = ["modules"]

    added = []
    launched = []

    for tdata_path, ident in new_tdatas:
        if not os.path.isdir(tdata_path):
            log(f"SKIP (not a dir): {tdata_path}")
            continue

        # Derive a clean subfolder name inside ФЛЕШ 2
        nice = sanitize_name(ident)
        if nice in ("+", ""):
            nice = os.path.basename(os.path.dirname(tdata_path)) or "account_" + str(len(added))

        # Avoid overwriting the ones we already have with nice names
        if nice in ("БРИДЖ", "рома", "САВА"):
            nice = nice + "_" + sanitize_name(ident)

        inst_dir = os.path.join(FLESH2, nice)

        if os.path.exists(inst_dir):
            log(f"SKIP (already exists): {inst_dir}")
            continue

        log(f"Creating instance for {ident} -> {inst_dir}")

        try:
            os.makedirs(inst_dir, exist_ok=True)

            # Copy base runtime
            for f in base_files:
                src = os.path.join(EMPTY_TG, f)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(inst_dir, f))
            for d in base_dirs:
                srcd = os.path.join(EMPTY_TG, d)
                if os.path.isdir(srcd):
                    dstd = os.path.join(inst_dir, d)
                    if os.path.exists(dstd):
                        shutil.rmtree(dstd, ignore_errors=True)
                    shutil.copytree(srcd, dstd)

            # Inject tdata (robocopy is robust)
            tdata_dst = os.path.join(inst_dir, "tdata")
            if os.path.exists(tdata_dst):
                shutil.rmtree(tdata_dst, ignore_errors=True)
            os.makedirs(tdata_dst, exist_ok=True)

            robocmd = ["robocopy", tdata_path, tdata_dst, "/E", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"]
            subprocess.run(robocmd, check=False, capture_output=True)

            log(f"  Ready: {inst_dir}")

            # Launch immediately ("сразу")
            exe = os.path.join(inst_dir, "Telegram.exe")
            if os.path.isfile(exe):
                subprocess.Popen([exe], cwd=inst_dir)
                log(f"  Launched window for {nice}")
                launched.append(nice)
                time.sleep(1.5)  # stagger a bit
            else:
                log(f"  (no exe found to launch in {inst_dir})")

            added.append(nice)
        except Exception as ex:
            log(f"  ERROR creating {ident}: {ex}")

    log("")
    log("=== DONE ===")
    log(f"Added {len(added)} new instances to {FLESH2}: {added}")
    log(f"Launched windows for: {launched}")
    log("Check your Desktop → ФЛЕШ 2. New subfolders with full ready Telegram Desktop (exe + your TData from the flash folder).")
    log("If some TData copies were incomplete due to locks, close any running Telegram and re-run this script.")

if __name__ == "__main__":
    main()
