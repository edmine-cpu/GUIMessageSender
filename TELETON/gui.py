import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import os
import sys
import threading
import queue
import sqlite3
import builtins
import traceback
import re
import csv
import random
import tempfile
import zipfile
from datetime import datetime, date, timedelta, timezone
import time
import asyncio

# Локальные модули проекта (должны быть в той же папке)
import account_manager
import autoreply
import channel_commenter
from config import Config
from database import Database
from diagnostics import human_reason, human_exception
from models import Account, Task, SendLog, ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_NEEDS_REAUTH, ACCOUNT_STATUS_BANNED, ACCOUNT_STATUS_NETWORK_ISSUE

HELP_TEXTS = {}

STOP_CANCEL_GRACE_SECONDS = 1.0
STOP_LOOP_CLEANUP_GRACE_SECONDS = 1.0
STOP_UI_FORCE_MS = 3000


def _split_message_template_variants(text: str) -> list[str]:
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return []

    lines = value.split("\n")
    if not any(line.strip() == "---" for line in lines):
        return [value]

    variants = []
    current = []
    for line in lines:
        if line.strip() == "---":
            block = "\n".join(current).strip()
            if block:
                variants.append(block)
            current = []
            continue
        current.append(line)

    block = "\n".join(current).strip()
    if block:
        variants.append(block)
    return variants


try:
    from file_logger import log_exception
except Exception:
    def log_exception(tag: str, exc: BaseException, context: str = ""):
        try:
            ctx = f" {context}" if context else ""
            log_to_file(tag, f"EXCEPTION{ctx}: {type(exc).__name__}: {exc!r}")
        except Exception:
            pass

def log_to_file(tag: str, msg: str):
    """Минимальная заглушка, чтобы приложение могло запуститься.
    Полная реализация обычно в file_logger.py.
    """
    try:
        os.makedirs("data/logs", exist_ok=True)
        path = f"data/logs/teleton_{tag}.log"
        with open(path, "a", encoding="utf-8", errors="ignore") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        # Не даём логгеру убить запуск
        pass

# --- Полная коллекция подсказок по ошибкам TData импорта ---
TDATA_ERROR_HINTS = {
    "AuthKeyUnregisteredError":
        "TData устарела или auth_key уже отозван продавцом / анти-фродом Telegram. "
        "Запроси у продавца свежую TData либо используй другой аккаунт.",
    "AuthKeyInvalidError":
        "auth_key из TData недействителен. TData повреждена или с другой версии Telegram. "
        "Запроси у продавца свежую TData.",
    "SessionPasswordNeededError":
        "На аккаунте включён Cloud Password (двухэтапная аутентификация). "
        "В текущей версии импорт TData с 2FA-паролем не поддерживается.",
    "PasswordHashInvalidError":
        "Введённый пароль для TData неверный (если TData защищена локальным паролем).",
    "FloodWaitError":
        "Telegram временно ограничил аккаунт (FloodWait). Подожди указанное в ошибке "
        "время и повтори импорт.",
    "PhoneNumberBannedError":
        "Номер телефона забанен Telegram. Аккаунт восстановлению не подлежит.",
    "UserDeactivatedBanError":
        "Аккаунт навсегда заблокирован Telegram. Восстановлению не подлежит.",
    "UserDeactivatedError":
        "Аккаунт удалён владельцем. Импорт невозможен.",
    "TimeoutError":
        "Истёк таймаут подключения. Скорее всего прокси не отвечает или нет интернета. "
        "Попробуй без прокси или замени прокси.",
    "ConnectionError":
        "Нет соединения с серверами Telegram. Проверь интернет и прокси.",
    "OSError":
        "Системная ошибка ввода-вывода. Возможные причины: повреждена папка TData, "
        "нет прав на запись в data/sessions/, антивирус блокирует SQLite-файлы.",
    "OpenTeleException":
        "Папка TData повреждена или это не TData. Проверь, что внутри лежат файлы "
        "key_datas + папка с hex-именем (D877F783...).",
    "TFileNotFound":
        "Не найдены ключевые файлы TData. Возможно путь указан на родительскую папку — "
        "проверь, что внутри tdata/ лежат файлы key_datas + папка с hex-именем, "
        "а не вложенная ещё одна tdata/.",
}


def _hint_for(e: BaseException) -> str:
    """Вернуть человеческую подсказку по исключению, либо str(e) как fallback."""
    name = type(e).__name__
    text = str(e) or repr(e) or name
    if name == "OpenTeleException" and "No account has been loaded" in text:
        return (
            "Папка внешне похожа на TData, но внутри не найден загружаемый аккаунт. "
            "Частые причины: выбрана не та копия tdata, аккаунт не залогинен в этой папке, "
            "TData повреждена/неполная, либо эту папку сейчас держит открытый Telegram Desktop. "
            "Попробуй закрыть Telegram из этой папки и выбрать рабочую папку вида data\\<id>\\tdata."
        )
    return TDATA_ERROR_HINTS.get(name, str(e) or name)


def _cycle_has_usable_config(targets_count: int, accounts_count: int) -> bool:
    """Pure non-UI helper (top-level so easy to unit-test via AST).
    A cycle campaign is startable via the 'Включённые' button (and auto-resume)
    if it has targets>0. accounts_count==0 is valid and means "Все активные"
    (the global pool of active accounts will be resolved at start time).
    This was the root cause of the P0 regression for the new button and
    startup resume of normal (non per-campaign-account) campaigns.
    """
    return targets_count > 0


def format_account(phone: str, custom_name: str = "") -> str:
    """Форматирование отображения аккаунта (метка + телефон) для логов, комбобоксов и таблиц.
    Защищает от NameError в путях старта циклических кампаний и on_show/refresh.
    """
    p = (phone or "").strip()
    n = (custom_name or "").strip()
    if n:
        return f"{n} ({p})"
    return p


async def _try_with_flood_retry(coro_factory, max_wait_sec: int,
                                 jitter_min: int = 1, jitter_max: int = 3,
                                 log_cb=print):
    """Обёртка для async-операций с одним FloodWait-ретраем.

    coro_factory — callable() возвращающий coroutine. Заводская функция нужна
    чтобы создавать coroutine заново при ретрае (одну и ту же coroutine
    нельзя await-ить дважды).
    max_wait_sec — если Telegram просит подождать больше этого — сдаёмся,
    пробрасываем FloodWaitError наружу.
    jitter_min/jitter_max — случайная добавка к времени ожидания, чтобы не
    бить сервер в момент истечения окна ровно когда оно открывается.
    """
    import random as _random
    from telethon.errors import FloodWaitError
    try:
        return await coro_factory()
    except FloodWaitError as e:
        if e.seconds > max_wait_sec:
            log_cb(f"  [!] FloodWait {e.seconds}с > лимита {max_wait_sec}с — сдаёмся")
            raise
        wait = e.seconds + _random.uniform(jitter_min, jitter_max)
        log_cb(f"  [~] FloodWait {e.seconds}с — ожидание {wait:.1f}с перед ретраем")
        import asyncio as _asyncio
        await _asyncio.sleep(wait)
        return await coro_factory()


def _is_tdata_dir(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        dir_contents = os.listdir(path)
    except Exception:
        return False
    has_key = any(n in ("key_datas", "key_datass") for n in dir_contents)
    if not has_key:
        return False
    has_hex_dir = False
    for n in dir_contents:
        if re.fullmatch(r"[0-9A-Fa-f]{16}", n) and os.path.isdir(os.path.join(path, n)):
            has_hex_dir = True
            break
    return has_hex_dir


def _collect_tdata_dirs(root: str) -> list:
    if not root or not os.path.isdir(root):
        return []

    found = []
    seen = set()
    max_depth = 5

    for dirpath, dirnames, _ in os.walk(root):
        try:
            rel = os.path.relpath(dirpath, root)
        except Exception:
            rel = ""
        depth = 0 if rel in (".", "") else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []
            continue

        if _is_tdata_dir(dirpath):
            ap = os.path.abspath(dirpath)
            if ap not in seen:
                seen.add(ap)
                found.append(dirpath)
            dirnames[:] = []

    found.sort()
    return found


def _import_result(source: str, ref: str = "", phone: str = "",
                   action: str = "failed", reason: str = "") -> dict:
    return {
        "source": source or "",
        "ref": ref or "",
        "phone": phone or "",
        "status": "ok" if action in ("added", "updated") else "failed",
        "action": action or "failed",
        "reason": reason or "",
    }


def _summarize_import_results(expected: int, results: list,
                              empty_kind: str = "fail",
                              empty_text: str = "") -> dict:
    expected = int(expected or 0)
    results = list(results or [])
    added = sum(1 for r in results if r.get("action") == "added")
    updated = sum(1 for r in results if r.get("action") == "updated")
    skipped = sum(1 for r in results if r.get("action") == "skipped")
    failed = sum(1 for r in results if r.get("action") == "failed")
    ok = added + updated

    if expected <= 0 and not results:
        kind = empty_kind
        text = empty_text or "Импорт не выполнен: нет аккаунтов для обработки"
    elif ok <= 0:
        kind = "fail"
        text = f"Импорт не удался: обработано {expected}, добавлено 0, обновлено 0"
    elif failed or skipped or ok < expected:
        kind = "partial"
        text = (f"Импорт частичный: добавлено {added}, обновлено {updated}, "
                f"не добавлено {failed + skipped} из {expected}")
    else:
        kind = "success"
        text = f"Импорт успешен: добавлено {added}, обновлено {updated}"

    return {
        "kind": kind,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "expected": expected,
        "results": results,
        "text": text,
    }


def _cleanup_session_files(session_name: str):
    for suffix in (".session", ".session-journal"):
        try:
            p = session_name + suffix
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _move_session_file(current_session_name: str, target_session_name: str,
                       overwrite_existing: bool = False) -> tuple[bool, str]:
    old_session = current_session_name + ".session"
    new_session = target_session_name + ".session"
    old_journal = current_session_name + ".session-journal"
    new_journal = target_session_name + ".session-journal"

    if not os.path.exists(old_session):
        return False, f"session-файл не создался: {old_session}"

    if os.path.abspath(old_session) != os.path.abspath(new_session):
        if os.path.exists(new_session):
            if not overwrite_existing:
                return False, f"целевой session-файл уже существует: {new_session}"
            try:
                os.remove(new_session)
            except Exception as e:
                return False, f"не удалось удалить старый session: {type(e).__name__}"
        if overwrite_existing and os.path.exists(new_journal):
            try:
                os.remove(new_journal)
            except Exception:
                pass
        try:
            os.rename(old_session, new_session)
        except Exception as e:
            return False, f"не удалось переименовать session: {type(e).__name__}"

    if os.path.exists(old_journal):
        try:
            os.remove(old_journal)
        except Exception:
            pass

    return True, ""


def _save_imported_account(db_path: str, phone: str, session_name: str,
                           proxy: str, device_fields: dict) -> str:
    db = Database(db_path)
    try:
        all_accs = db.get_all_accounts()
        existing_acc = next((a for a in all_accs if a.phone == phone), None)
        if existing_acc is not None:
            acc = existing_acc
            action = "updated"
        else:
            acc = Account(phone=phone)
            action = "added"

        acc.session_name = session_name
        acc.proxy = proxy or ""
        for key, value in (device_fields or {}).items():
            setattr(acc, key, value)
        db.add_account(acc)
        return action
    finally:
        db.close()


def _session_candidate_from_filename(sessions_dir: str, filename: str):
    if "tdata_" in filename:
        return None

    phone = ""
    if filename.startswith("session_") and filename.endswith(".session"):
        phone = filename[len("session_"):-len(".session")]
    elif filename.endswith("_telethon.session"):
        phone = filename[:-len("_telethon.session")]

    if not phone or phone.startswith("tdata_"):
        return None

    session_path = os.path.join(sessions_dir, filename)
    return {
        "declared_phone": phone,
        "session_name": session_path[:-len(".session")],
        "filename": filename,
    }


async def _verify_session_account(session_name: str, declared_phone: str,
                                  proxy: str = "", connect_timeout: int = 20,
                                  getme_timeout: int = 20) -> dict:
    from telethon import TelegramClient
    from config import OWN_API_ID, OWN_API_HASH
    from sender import TelegramSender

    ref = os.path.basename(session_name) + ".session"
    if not OWN_API_ID or not OWN_API_HASH:
        return _import_result(
            "session", ref, declared_phone, "failed",
            "не задан OWN_API_ID/OWN_API_HASH для проверки .session",
        )

    proxy_tuple = None
    if proxy:
        try:
            proxy_tuple = TelegramSender._parse_proxy(proxy)
        except Exception as e:
            return _import_result(
                "session", ref, declared_phone, "failed",
                f"ошибка proxy: {type(e).__name__}",
            )

    client = TelegramClient(
        session_name,
        OWN_API_ID,
        OWN_API_HASH,
        proxy=proxy_tuple,
        device_model="PC 64bit",
        system_version="Windows 10",
        app_version="1.0",
        lang_code="en",
        system_lang_code="en",
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=connect_timeout)
        authorized = await asyncio.wait_for(
            client.is_user_authorized(), timeout=getme_timeout)
        if not authorized:
            return _import_result(
                "session", ref, declared_phone, "failed",
                "сессия не авторизована",
            )
        me = await asyncio.wait_for(client.get_me(), timeout=getme_timeout)
        if not me or not getattr(me, "phone", None):
            return _import_result(
                "session", ref, declared_phone, "failed",
                "Telegram не вернул номер телефона",
            )
        return _import_result("session", ref, f"+{me.phone}", "added", "")
    except Exception as e:
        log_exception("accounts", e, context=f"Verify session {session_name}")
        return _import_result(
            "session", ref, declared_phone, "failed",
            f"{type(e).__name__}: {_hint_for(e)}",
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def import_session_files_to_db(candidates: list, proxy_for_index,
                               sessions_dir: str, db_path: str,
                               log_cb=print) -> dict:
    def _emit(msg: str):
        try:
            log_cb(msg)
        except Exception:
            pass

    candidates = list(candidates or [])
    results = []
    loop = asyncio.new_event_loop()

    async def do_import():
        from config import OWN_API_ID, OWN_API_HASH

        device_fields = {
            "api_id": OWN_API_ID,
            "api_hash": OWN_API_HASH,
            "device_model": "PC 64bit",
            "system_version": "Windows 10",
            "app_version": "1.0",
            "lang_code": "en",
        }

        for idx, cand in enumerate(candidates):
            filename = cand.get("filename") or ""
            declared_phone = cand.get("declared_phone") or ""
            session_name = cand.get("session_name") or ""
            proxy = proxy_for_index(idx) if callable(proxy_for_index) else (proxy_for_index or "")

            _emit(f"[~] Проверяю session: {filename}")
            res = await _verify_session_account(
                session_name, declared_phone, proxy=proxy)
            if res.get("action") != "added":
                results.append(res)
                _emit(f"[-] {filename} не добавлен: {res.get('reason', '')}")
                continue

            phone = res.get("phone", "")
            standard_path = os.path.join(sessions_dir, f"session_{phone}")
            ok, reason = _move_session_file(
                session_name, standard_path, overwrite_existing=False)
            if not ok:
                res["action"] = "failed"
                res["status"] = "failed"
                res["reason"] = reason
                results.append(res)
                _emit(f"[-] {filename} не добавлен: {reason}")
                continue

            try:
                action = _save_imported_account(
                    db_path, phone, standard_path, proxy, device_fields)
                res["action"] = action
                res["status"] = "ok"
                res["reason"] = ""
                results.append(res)
                sign = "+" if action == "added" else "~"
                label = "добавлен" if action == "added" else "обновлён"
                _emit(f"[{sign}] {phone} {label}")
            except Exception as e:
                log_exception("accounts", e, context=f"DB write for session {phone}")
                res["action"] = "failed"
                res["status"] = "failed"
                res["reason"] = f"ошибка БД: {type(e).__name__}"
                results.append(res)
                _emit(f"[-] {filename} не добавлен: {res['reason']}")

    try:
        _run_loop(loop, do_import())
    except Exception as e:
        log_exception("accounts", e, context="import_session_files_to_db")
        results.append(_import_result(
            "session", "", "", "failed",
            f"ошибка импорта session: {type(e).__name__}",
        ))

    return _summarize_import_results(len(candidates), results)


def import_tdata_dir_to_db(tdata_path: str, proxy: str,
                           sessions_dir: str, db_path: str,
                           log_cb=print) -> dict:
    proxy = (proxy or "").strip()
    results = []

    def _emit(msg: str):
        try:
            log_cb(msg)
        except Exception:
            pass
        try:
            print(msg)
        except Exception:
            pass

    expected = 0

    try:
        from opentele.td import TDesktop
        from opentele.api import API, UseCurrentSession
        from sender import TelegramSender
        from config import DESKTOP_API_ID, DESKTOP_API_HASH
    except Exception as e:
        _emit(f"[!] opentele недоступна: {type(e).__name__}: {e}")
        return _summarize_import_results(
            0, [], "fail_early",
            f"opentele не установлена: {type(e).__name__}",
        )

    if not _is_tdata_dir(tdata_path):
        nested = _collect_tdata_dirs(tdata_path)
        if nested:
            _emit(f"[!] Выбрана не сама TData, а папка-контейнер: {tdata_path}")
            _emit(f"[!] Внутри найдено TData папок: {len(nested)}")
            _emit("[!] Используйте массовый импорт TData или выберите конкретную вложенную папку tdata.")
            text = f"выбрана папка-контейнер: {tdata_path} (найдено {len(nested)} TData внутри)"
        else:
            _emit(f"[!] Папка не похожа на TData: {tdata_path}")
            text = f"папка не похожа на TData (нет key_datas/hex): {tdata_path}"
        return _summarize_import_results(0, [], "fail_early", text)

    try:
        os.makedirs(sessions_dir, exist_ok=True)
        probe = os.path.join(sessions_dir, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as e:
        _emit(f"[!] Нет прав на запись в {sessions_dir}: {type(e).__name__}: {e}")
        return _summarize_import_results(
            0, [], "fail_early",
            f"нет прав на запись в sessions ({sessions_dir}): {type(e).__name__}",
        )

    try:
        tdesk = TDesktop(tdata_path)
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        log_exception("accounts", e, context=f"TDesktop({tdata_path}) init failed")
        _emit(f"[!] Не могу прочитать TData: {type(e).__name__}")
        _emit(f"[!] Подсказка: {_hint_for(e)}")
        return _summarize_import_results(
            0, [], "fail_early",
            f"ошибка чтения TData ({type(e).__name__}) по пути {tdata_path}",
        )

    if not tdesk.isLoaded() or tdesk.accountsCount == 0:
        _emit("[!] TData не содержит аккаунтов или повреждена")
        return _summarize_import_results(
            0, [], "fail",
            "TData пустая или повреждена",
        )

    expected = int(tdesk.accountsCount or 0)
    _emit(f"[~] TData: найдено аккаунтов: {expected} ({os.path.basename(tdata_path)})")

    try:
        from ads_database import AdsDB
        adsdb = AdsDB(db_path)
        try:
            settings = adsdb.load_scheduler_settings()
        finally:
            adsdb.close()
        connect_timeout = int(getattr(settings, "tdata_connect_timeout_seconds", 20) or 20)
        getme_timeout = int(getattr(settings, "tdata_get_me_timeout_seconds", 20) or 20)
        flood_max = int(getattr(settings, "tdata_flood_max_wait_seconds", 60) or 60)
        flood_jit_min = int(getattr(settings, "tdata_flood_jitter_min_seconds", 1) or 1)
        flood_jit_max = int(getattr(settings, "tdata_flood_jitter_max_seconds", 3) or 3)
    except Exception:
        connect_timeout, getme_timeout = 20, 20
        flood_max, flood_jit_min, flood_jit_max = 60, 1, 3

    loop = asyncio.new_event_loop()

    async def do_convert():
        for idx, td_acc in enumerate(tdesk.accounts):
            user_id = td_acc.UserId
            ref = f"userId={user_id}"
            session_path = os.path.join(sessions_dir, f"session_tdata_{user_id}")
            proxy_tuple = None
            if proxy:
                s = TelegramSender.__new__(TelegramSender)
                try:
                    proxy_tuple = s._parse_proxy(proxy)
                except Exception as e:
                    log_exception("accounts", e, context=f"Parse proxy for userId={user_id}")
                    reason = f"ошибка proxy: {type(e).__name__}: {e}"
                    results.append(_import_result("tdata", ref, "", "failed", reason))
                    _emit(f"[-] {ref}: {reason}")
                    continue

            td_api = API.TelegramDesktop(api_id=DESKTOP_API_ID, api_hash=DESKTOP_API_HASH)

            client = None
            phone = None
            fail_reason = ""
            try:
                _emit(f"[~] ({idx+1}/{expected}) ToTelethon userId={user_id}")
                client = await tdesk.ToTelethon(
                    session=session_path,
                    flag=UseCurrentSession,
                    api=td_api,
                    **({"proxy": proxy_tuple} if proxy_tuple else {}),
                )

                await _try_with_flood_retry(
                    lambda: asyncio.wait_for(client.connect(), timeout=connect_timeout),
                    max_wait_sec=flood_max,
                    jitter_min=flood_jit_min,
                    jitter_max=flood_jit_max,
                    log_cb=_emit,
                )

                authorized = await _try_with_flood_retry(
                    lambda: asyncio.wait_for(client.is_user_authorized(), timeout=getme_timeout),
                    max_wait_sec=flood_max,
                    jitter_min=flood_jit_min,
                    jitter_max=flood_jit_max,
                    log_cb=_emit,
                )
                if not authorized:
                    fail_reason = "TData не авторизована"
                    _emit(f"[-] {ref}: {fail_reason}, аккаунт не добавлен")
                else:
                    me = await _try_with_flood_retry(
                        lambda: asyncio.wait_for(client.get_me(), timeout=getme_timeout),
                        max_wait_sec=flood_max,
                        jitter_min=flood_jit_min,
                        jitter_max=flood_jit_max,
                        log_cb=_emit,
                    )
                    if not me or not getattr(me, "phone", None):
                        fail_reason = "Telegram не вернул номер телефона"
                        _emit(f"[-] {ref}: {fail_reason}, аккаунт не добавлен")
                    else:
                        phone = f"+{me.phone}"
                        _emit(f"[+] Конвертирован: {phone}")
            except Exception as e:
                log_exception("accounts", e, context=f"Convert userId={user_id}")
                fail_reason = f"ошибка конвертации: {type(e).__name__}"
                _emit(f"[-] Ошибка конвертации {ref}: {type(e).__name__}")
                _emit(f"[-] Подсказка: {_hint_for(e)}")
                phone = None
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

            if not phone:
                _cleanup_session_files(session_path)
                results.append(_import_result(
                    "tdata", ref, "", "failed",
                    fail_reason or "аккаунт не подтверждён",
                ))
                continue

            standard_path = os.path.join(sessions_dir, f"session_{phone}")
            ok, reason = _move_session_file(
                session_path, standard_path, overwrite_existing=True)
            if not ok:
                _emit(f"[-] {phone} не добавлен: {reason}")
                results.append(_import_result("tdata", ref, phone, "failed", reason))
                continue

            try:
                tdata_device = {
                    "api_id": DESKTOP_API_ID,
                    "api_hash": DESKTOP_API_HASH,
                    "device_model": "Desktop",
                    "system_version": "Windows 10",
                    "app_version": "5.6.3 x64",
                    "lang_code": "ru",
                }
                action = _save_imported_account(
                    db_path, phone, standard_path, proxy, tdata_device)
                results.append(_import_result("tdata", ref, phone, action, ""))
                sign = "+" if action == "added" else "~"
                label = "добавлен" if action == "added" else "обновлён"
                _emit(f"[{sign}] {phone} {label}")
            except Exception as e:
                log_exception("accounts", e, context=f"DB write for {phone}")
                reason = f"ошибка БД: {type(e).__name__}"
                results.append(_import_result("tdata", ref, phone, "failed", reason))
                _emit(f"[-] Ошибка записи в БД для {phone}: {type(e).__name__}")
                _emit(f"[-] Подсказка: {_hint_for(e)}")

    try:
        _run_loop(loop, do_convert())
    except Exception as e:
        log_exception("accounts", e, context="import_tdata_dir_to_db top-level")
        _emit(f"[-] Ошибка импорта TData: {type(e).__name__}")
        _emit(f"[-] Подсказка: {_hint_for(e)}")
        results.append(_import_result(
            "tdata", os.path.basename(tdata_path), "", "failed",
            f"ошибка импорта TData: {type(e).__name__}",
        ))

    return _summarize_import_results(
        expected, results, "fail_early",
        f"Импорт прерван до чтения TData (путь: {tdata_path})",
    )


# --- Перехват print() — thread-safe через threading.local() ---

import builtins
import threading

_original_print = builtins.print
_thread_local = threading.local()


def _patched_print(*args, **kwargs):
    parts = []
    for a in args:
        try:
            parts.append(str(a))
        except Exception:
            try:
                parts.append(repr(a))
            except Exception:
                parts.append("<unprintable>")
    msg = " ".join(parts)
    # Всегда пишем в файл (и GUI-сообщения, и фоновые print'ы)
    # tag берём из threading.local если был установлен,
    # иначе пишем без тега
    tag = getattr(_thread_local, "log_tag", "")
    try:
        log_to_file(tag, msg)
    except Exception:
        pass

    handler = getattr(_thread_local, "log_handler", None)
    if handler:
        handler(msg)
    else:
        _original_print(*args, **kwargs)


builtins.print = _patched_print


def _log_action(tag: str, action: str):
    """Логирование действий пользователя (нажатия кнопок, переключения вкладок).

    Пишет ТОЛЬКО в файл (не в GUI — там и так видно результаты).
    Используется для post-mortem анализа: «что юзер делал перед тем как упало».
    """
    try:
        log_to_file(tag, f"[ACTION] {action}")
    except Exception:
        pass


# --- Хелпер: безопасное завершение event loop ---

def _run_loop(loop, coro):
    """
    Запуск корутины с безопасным закрытием event loop.
    Telethon 1.x на Python 3.12+ оставляет pending tasks после run_until_complete —
    если просто вызвать loop.close(), следующий tick бросит
    'RuntimeError: Event loop is closed'. Хелпер отменяет pending tasks
    и даёт им завершиться перед закрытием.
    """
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.wait(pending, timeout=STOP_LOOP_CLEANUP_GRACE_SECONDS)
                )
        except Exception:
            pass
        loop.close()


class OperationInterrupted(Exception):
    """Штатное прерывание фоновой операции по Stop."""


def _raise_if_stop_requested(
    stop_event: threading.Event | None,
    *,
    op_name: str,
    account: str = "",
    target: str = "",
    progress: str = "",
):
    if stop_event is not None and stop_event.is_set():
        parts = [f"[~] Остановка подтверждена: {op_name}"]
        if account:
            parts.append(f"аккаунт={account}")
        if target:
            parts.append(f"цель={target}")
        if progress:
            parts.append(progress)
        raise OperationInterrupted(" | ".join(parts))


async def _sleep_interruptibly(
    seconds: float,
    stop_event: threading.Event | None,
    *,
    op_name: str,
    account: str = "",
    target: str = "",
    progress: str = "",
    quantum: float = 0.2,
):
    if seconds <= 0:
        return
    deadline = time.monotonic() + float(seconds)
    while True:
        _raise_if_stop_requested(
            stop_event,
            op_name=op_name,
            account=account,
            target=target,
            progress=progress,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(quantum, remaining))


async def _await_interruptibly(
    coro,
    stop_event: threading.Event | None,
    *,
    op_name: str,
    label: str,
    timeout: float | None = None,
    account: str = "",
    target: str = "",
    progress: str = "",
    quantum: float = 0.2,
):
    """Await a Telegram operation, but cancel it quickly when Stop is pressed."""
    if stop_event is not None and stop_event.is_set():
        if isinstance(coro, asyncio.Future):
            coro.cancel()
        else:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
        _raise_if_stop_requested(
            stop_event,
            op_name=op_name,
            account=account,
            target=target,
            progress=progress or label,
        )
    task = asyncio.ensure_future(coro)
    deadline = (time.monotonic() + float(timeout)) if timeout else None
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=quantum)
            if task in done:
                return await task
            _raise_if_stop_requested(
                stop_event,
                op_name=op_name,
                account=account,
                target=target,
                progress=progress or label,
            )
            if deadline is not None and time.monotonic() >= deadline:
                await _cancel_task_bounded(task)
                raise asyncio.TimeoutError
    except OperationInterrupted:
        await _cancel_task_bounded(task)
        raise


async def _cancel_task_bounded(task: asyncio.Future, grace: float = STOP_CANCEL_GRACE_SECONDS):
    if task.done():
        try:
            task.result()
        except BaseException:
            pass
        return
    task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=max(float(grace or 0), 0.0))
        if task in done:
            try:
                task.result()
            except BaseException:
                pass
    except Exception:
        pass


# --- Хелпер: обновление .env файла ---

def _update_env_file(key: str, value: str):
    """Обновить или добавить ключ в .env файле"""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    lines = []
    found = False

    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


# --- Переиспользуемые компоненты ---

class LogFrame(ctk.CTkFrame):
    """Переиспользуемый лог-виджет"""

    def __init__(self, master, height=200, tag: str = "gui", **kwargs):
        super().__init__(master, **kwargs)
        self._log_tag = tag
        self._line_count = 0
        self._max_lines = 2000
        self._trim_every = 200
        self._pending_lines: list[str] = []
        self._flush_scheduled = False
        self.textbox = ctk.CTkTextbox(self, height=height, state="disabled",
                                       font=ctk.CTkFont(family="Consolas", size=12))
        self.textbox.pack(fill="both", expand=True, padx=5, pady=5)

    def append(self, text: str):
        # Пишем в файл — дублируется с _patched_print, но ловит кейсы
        # когда self.log.append(...) вызывается напрямую в GUI-потоке
        # (не через print из фонового потока)
        try:
            log_to_file(self._log_tag, text)
        except Exception:
            pass
        self._pending_lines.append(text)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            try:
                self.after(80, self._flush_pending)  # мягче батчинг, меньше дёрганья UI
            except Exception:
                self._flush_pending()

    def _flush_pending(self):
        lines = self._pending_lines
        self._pending_lines = []
        self._flush_scheduled = False
        if not lines:
            return
        try:
            self.textbox.configure(state="normal")
            self.textbox.insert("end", "\n".join(lines) + "\n")
            self._line_count += len(lines)
            if self._line_count > self._max_lines + self._trim_every:
                overflow = self._line_count - self._max_lines
                try:
                    self.textbox.delete("1.0", f"{overflow}.0")
                    self._line_count = self._max_lines
                except Exception:
                    pass
            self.textbox.see("end")
        finally:
            try:
                self.textbox.configure(state="disabled")
            except Exception:
                pass

    def clear(self):
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")
        self._line_count = 0


class HelpDialog(ctk.CTkToplevel):
    def __init__(self, master, title: str, text: str):
        super().__init__(master)
        self.title(title)
        self.geometry("760x560")
        self.minsize(640, 420)
        self.grab_set()

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(header, text=title, font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="Закрыть", width=120, command=self.destroy).pack(side="right")

        box = ctk.CTkTextbox(self)
        box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        box.insert("1.0", text.strip() + "\n")
        try:
            box.configure(state="disabled")
        except Exception:
            pass


class ScrollableTable(ctk.CTkScrollableFrame):
    """Переиспользуемая таблица (grid layout).

    Архитектура: для каждой строки создаётся CTkFrame-контейнер на всю ширину,
    внутри которого grid'ом расположены CTkLabel'ы. Bind на <Button-1> вешается
    на CTkFrame, который надёжно получает события (в отличие от композитного
    CTkLabel). Подсветка выделенной строки — через fg_color фрейма (визуально
    как полоска на всю ширину), а не через text_color отдельных ячеек.
    """

    # Цвета выделенной строки (light_mode, dark_mode)
    _SELECTED_BG = ("#3B8ED0", "#1F6AA5")
    _ROW_BG = "transparent"
    _ALT_ROW_BG = ("#f0f0f0", "#1a1a1a")

    def __init__(self, master, columns: list,
                 enable_checkboxes: bool = False,
                 row_key_fn=None,
                 column_weights: list = None,
                 column_minsizes: list = None,
                 column_anchors: list = None,
                 **kwargs):
        super().__init__(master, **kwargs)
        self.columns = columns
        self.enable_checkboxes = bool(enable_checkboxes)
        self.row_key_fn = row_key_fn
        self.column_weights = column_weights
        self.column_minsizes = column_minsizes
        self.column_anchors = column_anchors
        self.rows_data = []
        self.row_frames = []
        self.row_labels = []
        self.row_checkboxes = []
        self._checkbox_vars = {}
        self.selected_index = None
        self._on_select_callback = None
        self._row_highlights: list[str | None] = []  # per-row busy color or None

        # Заголовок — отдельный фрейм на строке 0 с визуальным разделителем
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.grid(row=0, column=0, sticky="ew", padx=0, pady=(3, 1))

        offset = 1 if self.enable_checkboxes else 0

        if self.enable_checkboxes:
            ctk.CTkLabel(self._header, text="",
                         font=ctk.CTkFont(weight="bold"),
                         anchor="center").grid(row=0, column=0, padx=5, pady=0, sticky="ew")

        for col_idx, col_name in enumerate(columns):
            lbl = ctk.CTkLabel(self._header, text=col_name,
                               font=ctk.CTkFont(weight="bold"),
                               anchor="w")
            lbl.grid(row=0, column=col_idx + offset, padx=5, pady=0, sticky="ew")

        self._apply_column_layout(self._header, len(columns) + offset)
        # Главный фрейм растягивается
        self.grid_columnconfigure(0, weight=1)

        # Тонкая линия-разделитель под заголовком (чтобы строки не "слипались" визуально)
        sep = ctk.CTkFrame(self, height=1, fg_color=("gray70", "gray30"))
        sep.grid(row=1, column=0, sticky="ew", padx=2, pady=(0, 2))

    def _row_default_bg(self, row_idx: int):
        return self._ALT_ROW_BG if row_idx % 2 == 1 else self._ROW_BG

    def _row_bg(self, row_idx: int, highlight=None):
        return highlight or self._row_default_bg(row_idx)

    def set_on_select(self, callback):
        self._on_select_callback = callback

    def set_data(self, rows: list, row_highlights: list[str | None] | None = None):
        """Обновить данные таблицы. rows — список кортежей/списков.
        row_highlights — опционально список цветов (или None) для подсветки строк (busy = синий и т.п.).
        Стараемся делать in-place обновление текста/цвета когда число строк не изменилось — сильно меньше лагов.

        Добавлена защита от слишком частых обновлений во время работы задач (рассылки, циклы и т.д.),
        чтобы строки не "плясали" и не налезали друг на друга.
        """
        # Throttle: не чаще чем раз в ~80мс делаем тяжёлые операции во время активной работы
        now = time.time()
        if not hasattr(self, '_last_set_data_ts'):
            self._last_set_data_ts = 0
        if now - self._last_set_data_ts < 0.15:  # мягче: не чаще  ~6-7 раз в секунду
            self.after(150, lambda r=rows, h=row_highlights: self.set_data(r, h))
            return
        self._last_set_data_ts = now

        prev_checked = set()
        if self.enable_checkboxes and self.row_key_fn and self._checkbox_vars:
            for k, v in self._checkbox_vars.items():
                try:
                    if bool(v.get()):
                        prev_checked.add(k)
                except Exception:
                    pass

        highlights = list(row_highlights) if row_highlights is not None else [None] * len(rows)

        # Оптимизация: если количество строк совпадает — обновляем на месте (текст + цвет busy)
        if (len(rows) == len(self.rows_data) and
                len(rows) == len(self.row_frames) and
                len(highlights) == len(self.row_frames)):
            self.rows_data = rows
            self._row_highlights = highlights
            # Обновляем только тексты и цвета строк (без пересоздания виджетов)
            for row_idx, row in enumerate(rows):
                if row_idx >= len(self.row_labels):
                    break
                cells = self.row_labels[row_idx]
                for col_idx, value in enumerate(row):
                    if col_idx < len(cells):
                        try:
                            cells[col_idx].configure(text=str(value))
                        except Exception:
                            pass
                # busy highlight (не трогаем выбранную строку)
                try:
                    h = highlights[row_idx] if row_idx < len(highlights) else None
                    frame = self.row_frames[row_idx]
                    if self.selected_index != row_idx:
                        frame.configure(fg_color=self._row_bg(row_idx, h))
                except Exception:
                    pass
            return

        # Полная пересборка — очищаем всё, чтобы не было "призраков" и наложений
        for frame in self.row_frames:
            try:
                frame.destroy()
            except Exception:
                pass
        self.row_frames.clear()
        self.row_labels.clear()
        for cb in self.row_checkboxes:
            try:
                cb.destroy()
            except Exception:
                pass
        self.row_checkboxes.clear()
        self.rows_data = rows
        self._row_highlights = highlights
        self._checkbox_vars = {}
        self.selected_index = None

        # Чистая конфигурация строк: header (0), separator (1), данные с 2
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=0, minsize=2)
        for row_idx in range(max(len(rows), 1)):
            self.grid_rowconfigure(row_idx + 2, weight=0, minsize=28)

        for row_idx, row in enumerate(rows):
            offset = 1 if self.enable_checkboxes else 0

            h = highlights[row_idx] if row_idx < len(highlights) else None
            row_frame = ctk.CTkFrame(self, fg_color=self._row_bg(row_idx, h),
                                     corner_radius=6, cursor="hand2")
            row_frame.grid(row=row_idx + 2, column=0, sticky="ew",
                           padx=2, pady=3)  # хороший отступ, чтобы строки не налезали визуально
            self._apply_column_layout(row_frame, len(row) + offset)

            if self.enable_checkboxes:
                key = self.row_key_fn(row) if self.row_key_fn else row_idx
                var = tk.BooleanVar(value=False)
                if key in prev_checked:
                    var.set(True)
                self._checkbox_vars[key] = var
                cb = ctk.CTkCheckBox(row_frame, text="", width=28, variable=var)
                cb.grid(row=0, column=0, padx=5, pady=2, sticky="w")
                self.row_checkboxes.append(cb)

            cells = []
            for col_idx, value in enumerate(row):
                anchor = "w"
                if self.column_anchors and col_idx < len(self.column_anchors):
                    anchor = self.column_anchors[col_idx] or "w"
                lbl = ctk.CTkLabel(row_frame, text=str(value), anchor=anchor)
                lbl.grid(row=0, column=col_idx + offset, padx=5, pady=2, sticky="ew")
                cells.append(lbl)

            handler = lambda e, idx=row_idx: self._select_row(idx)
            row_frame.bind("<Button-1>", handler)
            for cell in cells:
                cell.bind("<Button-1>", handler)
                try:
                    inner = cell._label if hasattr(cell, "_label") else None
                    if inner is not None:
                        inner.bind("<Button-1>", handler)
                except Exception:
                    pass

            self.row_frames.append(row_frame)
            self.row_labels.append(cells)

    def _select_row(self, index: int):
        # Сбросить подсветку предыдущей строки — восстанавливаем busy-цвет если был
        if (self.selected_index is not None
                and self.selected_index < len(self.row_frames)):
            prev_frame = self.row_frames[self.selected_index]
            prev_h = (self._row_highlights[self.selected_index]
                      if self.selected_index < len(self._row_highlights) else None)
            prev_frame.configure(fg_color=self._row_bg(self.selected_index, prev_h))

        self.selected_index = index
        if index < len(self.row_frames):
            cur_frame = self.row_frames[index]
            cur_frame.configure(fg_color=self._SELECTED_BG)

        if self._on_select_callback:
            self._on_select_callback(index)

    def get_selected_row(self):
        if (self.selected_index is not None
                and self.selected_index < len(self.rows_data)):
            return self.rows_data[self.selected_index]
        return None

    def get_checked_rows(self) -> list:
        if not self.enable_checkboxes:
            return []
        checked = []
        for row in self.rows_data:
            key = self.row_key_fn(row) if self.row_key_fn else None
            if key is None:
                continue
            v = self._checkbox_vars.get(key)
            if v is None:
                continue
            try:
                if bool(v.get()):
                    checked.append(row)
            except Exception:
                continue
        return checked

    def set_all_checked(self, checked: bool):
        if not self.enable_checkboxes:
            return
        for v in self._checkbox_vars.values():
            try:
                v.set(bool(checked))
            except Exception:
                pass

    def clear_checked(self):
        self.set_all_checked(False)

    def _apply_column_layout(self, grid_parent, total_columns: int):
        weights = None
        mins = None
        if self.enable_checkboxes:
            if self.column_weights and len(self.column_weights) == total_columns:
                weights = self.column_weights
            if self.column_minsizes and len(self.column_minsizes) == total_columns:
                mins = self.column_minsizes
        else:
            if self.column_weights and len(self.column_weights) == total_columns:
                weights = self.column_weights
            if self.column_minsizes and len(self.column_minsizes) == total_columns:
                mins = self.column_minsizes

        for i in range(total_columns):
            w = weights[i] if weights else 1
            m = mins[i] if mins else 0
            try:
                grid_parent.grid_columnconfigure(i, weight=w, minsize=m)
            except Exception:
                try:
                    grid_parent.grid_columnconfigure(i, weight=w)
                except Exception:
                    pass


# --- Диалоги ---




class ImportTDataDialog(ctk.CTkToplevel):
    """Диалог импорта аккаунта из папки TData (Telegram Desktop)"""

    def __init__(self, master, config: Config):
        super().__init__(master)
        self.title("Импорт TData")
        self.geometry("460x260")
        self.resizable(False, False)
        self.result = None
        self.config = config
        self.grab_set()

        pad = {"padx": 20, "pady": (8, 0)}

        # Папка TData
        ctk.CTkLabel(self, text="Папка TData:").pack(**pad, anchor="w")
        path_row = ctk.CTkFrame(self, fg_color="transparent")
        path_row.pack(padx=20, pady=(0, 5), fill="x")
        self.path_entry = ctk.CTkEntry(path_row, placeholder_text="/path/to/tdata")
        self.path_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(path_row, text="...", width=36,
                      command=self._pick_folder).pack(side="left", padx=(4, 0))

        # Прокси (опционально)
        ctk.CTkLabel(self, text="Прокси (опционально):").pack(**pad, anchor="w")
        self.proxy_entry = ctk.CTkEntry(
            self, placeholder_text="socks5://user:pass@host:port")
        self.proxy_entry.pack(padx=20, pady=(0, 5), fill="x")

        # Статус-строка для ошибок валидации
        self.status_label = ctk.CTkLabel(self, text="", text_color="#E74C3C")
        self.status_label.pack(padx=20, pady=(2, 0), anchor="w")

        # Кнопки
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(padx=20, pady=14, fill="x")
        ctk.CTkButton(btn_frame, text="Импортировать",
                      command=self._on_import).pack(side="left", expand=True, padx=5)
        ctk.CTkButton(btn_frame, text="Отмена", fg_color="gray40",
                      command=self.destroy).pack(side="left", expand=True, padx=5)

    def _pick_folder(self):
        path = filedialog.askdirectory(title="Выберите папку tdata")
        if path:
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, path)

    def _on_import(self):
        path  = self.path_entry.get().strip()
        proxy = self.proxy_entry.get().strip()

        if not path:
            self.status_label.configure(text="Укажите папку TData")
            return

        # Минимальная валидация формата прокси (только если прокси задан)
        if proxy:
            _has_scheme = (proxy.startswith("socks5://") or proxy.startswith("socks4://")
                           or proxy.startswith("http://"))
            _looks_compact = proxy.count(":") == 3 and "://" not in proxy
            if not (_has_scheme or _looks_compact):
                self.status_label.configure(
                    text="Формат: socks5://user:pass@host:port, host:port:user:pass "
                         "или user:pass:host:port")
                return

        self.result = {
            "path":  path,
            "proxy": proxy,
        }
        self.destroy()


class BulkAccountsDialog(ctk.CTkToplevel):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.title("Массовые операции")
        self.geometry("860x660")
        self.resizable(True, True)
        self.grab_set()

        self._accounts_vars = {}
        self._import_vars = {}
        self._ui_queue = queue.Queue()
        self._tdata_source = {"kind": "", "path": ""}

        self.after(120, self._poll_ui_queue)

        header = ctk.CTkLabel(self, text="Массовое добавление аккаунтов и привязка прокси",
                              font=ctk.CTkFont(size=16, weight="bold"))
        header.pack(padx=18, pady=(14, 8), anchor="w")

        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(padx=18, pady=10, fill="both", expand=True)
        self.tab_import = self.tabs.add("Импорт")
        self.tab_proxies = self.tabs.add("Прокси")
        self.tab_bind = self.tabs.add("Связки")
        self.tab_tdata = self.tabs.add("TData")

        self.log = LogFrame(self, height=90, tag="accounts")
        self.log.pack(padx=18, pady=(0, 10), fill="x")

        self._build_import_tab()
        self._build_proxies_tab()
        self._build_bind_tab()
        self._build_tdata_tab()

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(padx=18, pady=(0, 12), fill="x")
        ctk.CTkButton(btns, text="Закрыть", fg_color="gray40",
                      command=self.destroy).pack(side="right")

        self._refresh_proxy_pool()
        self._refresh_accounts_list()
        self._scan_sessions_dir()

    def _poll_ui_queue(self):
        processed = 0
        max_batch = 100
        try:
            while processed < max_batch:
                msg = self._ui_queue.get_nowait()
                processed += 1
                if msg:
                    self.log.append(str(msg))
        except queue.Empty:
            pass
        try:
            delay = 40 if processed >= max_batch else 120
            self.after(delay, self._poll_ui_queue)
        except Exception:
            pass

    @staticmethod
    def _split_lines(text: str) -> list:
        if not text:
            return []
        out = []
        for line in text.splitlines():
            s = (line or "").strip()
            if not s:
                continue
            if s.startswith("#"):
                continue
            out.append(s)
        return out

    @staticmethod
    def _parse_phone_proxy_mapping(text: str) -> list:
        pairs = []
        for line in BulkAccountsDialog._split_lines(text):
            if ";" in line:
                left, right = line.split(";", 1)
            elif "\t" in line:
                left, right = line.split("\t", 1)
            elif "," in line and line.count(",") == 1:
                left, right = line.split(",", 1)
            else:
                parts = line.split()
                if len(parts) < 2:
                    continue
                left, right = parts[0], " ".join(parts[1:])
            phone = (left or "").strip()
            proxy = (right or "").strip()
            if not phone or not proxy:
                continue
            pairs.append((phone, proxy))
        return pairs

    def _refresh_proxy_pool(self):
        db = Database(self.app.config.db_path)
        try:
            proxies = db.get_proxy_pool()
        finally:
            db.close()
        self._proxy_pool = proxies
        values = proxies[:] if proxies else ["—"]
        self.proxy_pool_var.set(values[0])
        self.import_proxy_pool_var.set(values[0])
        self.bind_proxy_pool_var.set(values[0])
        try:
            self.tdata_proxy_pool_var.set(values[0])
        except Exception:
            pass

        self.proxy_table.set_data([(p,) for p in proxies] if proxies else [])
        self._refresh_proxy_menus()

    def _refresh_proxy_menus(self):
        values = self._proxy_pool[:] if getattr(self, "_proxy_pool", None) else ["—"]
        try:
            self.proxy_pool_menu.configure(values=values)
        except Exception:
            pass
        try:
            self.import_proxy_pool_menu.configure(values=values)
        except Exception:
            pass
        try:
            self.bind_proxy_pool_menu.configure(values=values)
        except Exception:
            pass
        try:
            self.tdata_proxy_pool_menu.configure(values=values)
        except Exception:
            pass

    def _refresh_accounts_list(self):
        db = Database(self.app.config.db_path)
        try:
            accounts = db.get_all_accounts()
        finally:
            db.close()
        self._accounts = accounts

        for w in getattr(self, "_accounts_widgets", []):
            try:
                w.destroy()
            except Exception:
                pass
        self._accounts_widgets = []
        self._accounts_vars.clear()

        for i, acc in enumerate(accounts):
            v = tk.BooleanVar(value=False)
            self._accounts_vars[acc.phone] = v
            disp = format_account(acc.phone, getattr(acc, "custom_name", ""))
            label = f"{disp}   [{acc.proxy or '—'}]"
            cb = ctk.CTkCheckBox(self.accounts_list_frame, text=label, variable=v)
            cb.grid(row=i, column=0, sticky="w", padx=6, pady=3)
            self._accounts_widgets.append(cb)

    def _get_selected_phones(self) -> list:
        phones = []
        for phone, v in self._accounts_vars.items():
            try:
                if bool(v.get()):
                    phones.append(phone)
            except Exception:
                pass
        return phones

    def _select_all_accounts(self):
        for v in self._accounts_vars.values():
            try:
                v.set(True)
            except Exception:
                pass

    def _select_none_accounts(self):
        for v in self._accounts_vars.values():
            try:
                v.set(False)
            except Exception:
                pass

    def _select_accounts_without_proxy(self):
        proxy_map = {a.phone: (a.proxy or "") for a in getattr(self, "_accounts", [])}
        for phone, v in self._accounts_vars.items():
            try:
                v.set(not bool(proxy_map.get(phone, "").strip()))
            except Exception:
                pass

    def _build_import_tab(self):
        top = ctk.CTkFrame(self.tab_import, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(10, 6))

        ctk.CTkButton(top, text="Сканировать sessions/", width=180,
                      command=self._scan_sessions_dir).pack(side="left")

        self.lbl_sessions = ctk.CTkLabel(top, text="", text_color="gray60")
        self.lbl_sessions.pack(side="left", padx=10)

        body = ctk.CTkFrame(self.tab_import, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=6)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        ctk.CTkLabel(left, text="Новые .session (ещё не в БД):",
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(0, 6))
        self.import_list_frame = ctk.CTkScrollableFrame(left, height=360)
        self.import_list_frame.pack(fill="both", expand=True)

        sel_row = ctk.CTkFrame(left, fg_color="transparent")
        sel_row.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(sel_row, text="Выбрать все", width=120,
                      command=self._import_select_all).pack(side="left", padx=(0, 6))
        ctk.CTkButton(sel_row, text="Снять", width=90,
                      command=self._import_select_none).pack(side="left")

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        ctk.CTkLabel(right, text="Прокси при импорте:",
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(0, 6))

        self.import_proxy_mode = tk.StringVar(value="none")
        mode_row = ctk.CTkFrame(right, fg_color="transparent")
        mode_row.pack(fill="x")
        ctk.CTkRadioButton(mode_row, text="Не назначать", variable=self.import_proxy_mode,
                           value="none").pack(anchor="w", pady=2)
        ctk.CTkRadioButton(mode_row, text="Один прокси всем", variable=self.import_proxy_mode,
                           value="single").pack(anchor="w", pady=2)
        ctk.CTkRadioButton(mode_row, text="Раздать по кругу из пула", variable=self.import_proxy_mode,
                           value="round").pack(anchor="w", pady=2)

        self.import_proxy_pool_var = tk.StringVar(value="—")
        self.import_proxy_pool_menu = ctk.CTkOptionMenu(
            right, variable=self.import_proxy_pool_var, values=["—"]
        )
        self.import_proxy_pool_menu.pack(fill="x", pady=(8, 0))

        ctk.CTkLabel(right, text="Если выбран «Один прокси всем», можно задать вручную:",
                     text_color="gray60").pack(anchor="w", pady=(10, 2))
        self.import_manual_proxy = ctk.CTkEntry(
            right, placeholder_text="socks5://user:pass@host:port"
        )
        self.import_manual_proxy.pack(fill="x")

        act_row = ctk.CTkFrame(right, fg_color="transparent")
        act_row.pack(fill="x", pady=(14, 0))
        ctk.CTkButton(act_row, text="Импортировать выбранные", width=220,
                      command=self._import_selected_sessions).pack(side="left")

        ctk.CTkLabel(right, text="Импорт берёт файлы из data/sessions/. Копирование файлов не делает.",
                     text_color="gray60").pack(anchor="w", pady=(10, 0))

    def _scan_sessions_dir(self):
        sessions_dir = self.app.config.sessions_dir
        if not os.path.isdir(sessions_dir):
            self.lbl_sessions.configure(text=f"Папка не найдена: {sessions_dir}")
            self._import_candidates = []
            self._render_import_candidates()
            return

        db = Database(self.app.config.db_path)
        try:
            existing = {acc.phone for acc in db.get_all_accounts()}
        finally:
            db.close()

        candidates = []
        for filename in os.listdir(sessions_dir):
            # Явно пропускаем session-файлы вида session_tdata_*.session (и любые с tdata_):
            # это служебные/временные сессии от конвертации TData (userId вместо телефона).
            # Они не должны попадать в список импорта и добавляться в БД как аккаунты.
            if "tdata_" in filename:
                continue

            phone = None
            session_name = None

            if filename.startswith("session_") and filename.endswith(".session"):
                phone = filename[len("session_"):-len(".session")]
                session_path = os.path.join(sessions_dir, filename)
                session_name = session_path.replace(".session", "")
            elif filename.endswith("_telethon.session"):
                phone = filename.replace("_telethon.session", "")
                session_path = os.path.join(sessions_dir, filename)
                session_name = session_path.replace(".session", "")

            if not phone or not session_name:
                continue
            if phone.startswith("tdata_"):
                # Пропускаем session_tdata_*.session и phone tdata_* — это временные файлы от импорта TData
                # (без реального номера телефона), они не должны попадать в список импорта и в БД.
                continue
            if phone in existing:
                continue

            candidates.append((phone, session_name))

        candidates.sort(key=lambda x: x[0])
        self._import_candidates = candidates
        self.lbl_sessions.configure(text=f"Найдено новых: {len(candidates)}")
        self._render_import_candidates()

    def _render_import_candidates(self):
        for w in getattr(self, "_import_widgets", []):
            try:
                w.destroy()
            except Exception:
                pass
        self._import_widgets = []
        self._import_vars.clear()

        for i, (phone, session_name) in enumerate(getattr(self, "_import_candidates", [])):
            v = tk.BooleanVar(value=True)
            self._import_vars[phone] = (v, session_name)
            cb = ctk.CTkCheckBox(
                self.import_list_frame,
                text=f"{phone}   ({os.path.basename(session_name)}.session)",
                variable=v,
            )
            cb.grid(row=i, column=0, sticky="w", padx=6, pady=3)
            self._import_widgets.append(cb)

    def _import_select_all(self):
        for v, _ in self._import_vars.values():
            try:
                v.set(True)
            except Exception:
                pass

    def _import_select_none(self):
        for v, _ in self._import_vars.values():
            try:
                v.set(False)
            except Exception:
                pass

    def _import_selected_sessions(self):
        selected = [(phone, session_name)
                    for phone, (v, session_name) in self._import_vars.items()
                    if bool(v.get())]
        if not selected:
            self.log.append("[!] Нечего импортировать (ничего не выбрано)")
            return

        proxy_mode = (self.import_proxy_mode.get() or "none").strip()
        manual_proxy = (self.import_manual_proxy.get() or "").strip()
        pool = getattr(self, "_proxy_pool", []) or []
        pool_choice = (self.import_proxy_pool_var.get() or "").strip()

        if proxy_mode == "single":
            proxy_value = manual_proxy or (pool_choice if pool_choice != "—" else "")
        else:
            proxy_value = ""

        if proxy_mode == "round" and not pool:
            self.log.append("[!] Пул прокси пуст — добавьте прокси во вкладке «Прокси»")
            return

        candidates = []
        for phone, session_name in selected:
            filename = os.path.basename(session_name) + ".session"
            candidates.append({
                "declared_phone": phone,
                "session_name": session_name,
                "filename": filename,
            })

        self._ui_queue.put(f"[~] Проверяю session-файлы: {len(candidates)}")

        def proxy_for_index(i: int) -> str:
            if proxy_mode == "single":
                return proxy_value
            if proxy_mode == "round" and pool:
                return pool[i % len(pool)]
            return ""

        def worker():
            def emit(m: str):
                self._ui_queue.put(m)

            try:
                res = import_session_files_to_db(
                    candidates=candidates,
                    proxy_for_index=proxy_for_index,
                    sessions_dir=self.app.config.sessions_dir,
                    db_path=self.app.config.db_path,
                    log_cb=emit,
                )
                emit(f"[=] {res.get('text', '')}")
            except Exception as e:
                log_exception("accounts", e, context="bulk session import")
                emit(f"[-] Ошибка импорта session: {type(e).__name__}: {e}")
            finally:
                def _done():
                    self._refresh_accounts_list()
                    self._scan_sessions_dir()
                try:
                    self.after(0, _done)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _set_tdata_source(self, kind: str, path: str):
        kind = (kind or "").strip()
        path = (path or "").strip()
        self._tdata_source = {"kind": kind, "path": path}
        if not path:
            self.lbl_tdata_src.configure(text="Источник не выбран")
            return
        label = path
        if len(label) > 80:
            label = "…" + label[-79:]
        prefix = ".zip" if kind == "zip" else "папка"
        self.lbl_tdata_src.configure(text=f"{prefix}: {label}")

    def _pick_tdata_zip(self):
        path = filedialog.askopenfilename(
            title="Выберите архив с аккаунтами (zip)",
            filetypes=[("ZIP", "*.zip"), ("All", "*.*")]
        )
        if path:
            self._set_tdata_source("zip", path)

    def _pick_tdata_folder(self):
        path = filedialog.askdirectory(title="Выберите папку с аккаунтами")
        if path:
            self._set_tdata_source("dir", path)

    def _import_tdata_bulk(self):
        src = self._tdata_source or {"kind": "", "path": ""}
        if not src.get("path"):
            self.log.append("[!] Выберите архив .zip или папку с аккаунтами")
            return

        proxy_mode = (self.tdata_proxy_mode.get() or "none").strip()
        manual_proxy = (self.tdata_manual_proxy.get() or "").strip()
        pool_choice = (self.tdata_proxy_pool_var.get() or "").strip()
        pool = getattr(self, "_proxy_pool", []) or []

        if proxy_mode == "single":
            proxy_value = manual_proxy or (pool_choice if pool_choice != "—" else "")
        else:
            proxy_value = ""

        if proxy_mode == "round" and not pool:
            self.log.append("[!] Пул прокси пуст — добавьте прокси во вкладке «Прокси»")
            return

        self.btn_tdata_bulk.configure(state="disabled", text="Импорт...")
        self._ui_queue.put("[~] Старт импорта TData пачкой...")

        cfg = self.app.config

        def worker():
            def emit(m: str):
                self._ui_queue.put(m)

            root_dir = ""
            try:
                if src.get("kind") == "zip":
                    emit(f"[~] Распаковка: {src['path']}")
                    with tempfile.TemporaryDirectory(prefix="teleton_tdata_") as tmp:
                        with zipfile.ZipFile(src["path"], "r") as zf:
                            zf.extractall(tmp)
                        root_dir = tmp
                        self._run_tdata_dirs(root_dir, proxy_mode, proxy_value, pool, emit, cfg)
                else:
                    root_dir = src["path"]
                    self._run_tdata_dirs(root_dir, proxy_mode, proxy_value, pool, emit, cfg)
            except Exception as e:
                log_exception("accounts", e, context="bulk tdata import")
                emit(f"[-] Ошибка bulk-импорта: {type(e).__name__}: {e}")
            finally:
                def _done():
                    try:
                        self.btn_tdata_bulk.configure(state="normal", text="Импортировать TData пачкой")
                    except Exception:
                        pass
                    self._refresh_accounts_list()
                    self._scan_sessions_dir()
                try:
                    self.after(0, _done)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _run_tdata_dirs(self, root_dir: str, proxy_mode: str, proxy_value: str,
                        pool: list, emit, cfg: Config):
        tdata_dirs = _collect_tdata_dirs(root_dir)
        if not tdata_dirs:
            emit("[!] Не нашёл ни одной папки TData в архиве/папке")
            return

        emit(f"[~] Найдено TData папок: {len(tdata_dirs)}")

        totals = {"success": 0, "partial": 0, "fail": 0, "fail_early": 0}
        added_total = 0
        updated_total = 0
        failed_total = 0
        expected_total = 0

        for i, p in enumerate(tdata_dirs):
            if proxy_mode == "none":
                px = ""
            elif proxy_mode == "single":
                px = proxy_value
            else:
                px = pool[i % len(pool)]

            emit(f"\n[~] === TData {i+1}/{len(tdata_dirs)}: {p} ===")
            res = import_tdata_dir_to_db(
                tdata_path=p,
                proxy=px,
                sessions_dir=cfg.sessions_dir,
                db_path=cfg.db_path,
                log_cb=emit,
            )
            kind = res.get("kind", "fail")
            totals[kind] = totals.get(kind, 0) + 1
            added_total += int(res.get("added", 0) or 0)
            updated_total += int(res.get("updated", 0) or 0)
            failed_total += int(res.get("failed", 0) or 0) + int(res.get("skipped", 0) or 0)
            expected_total += int(res.get("expected", 0) or 0)
            for item in res.get("results", []) or []:
                phone = item.get("phone") or item.get("ref") or item.get("source") or "?"
                action = item.get("action", "")
                reason = item.get("reason", "")
                if action == "added":
                    emit(f"[+] {phone} добавлен")
                elif action == "updated":
                    emit(f"[~] {phone} обновлён")
                else:
                    emit(f"[-] {phone} не добавлен: {reason}")
            emit(f"[=] {res.get('text', '')}")

        emit("\n[=] === ИТОГО ===")
        emit(f"[=] Папок: {len(tdata_dirs)} | success={totals.get('success', 0)} | partial={totals.get('partial', 0)} | fail={totals.get('fail', 0)} | fail_early={totals.get('fail_early', 0)}")
        emit(f"[=] Добавлено: {added_total} | обновлено: {updated_total} | не добавлено: {failed_total} | ожидалось: {expected_total}")

    def _build_proxies_tab(self):
        top = ctk.CTkFrame(self.tab_proxies, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(10, 6))

        ctk.CTkLabel(top, text="Пул прокси (хранится в БД):",
                     font=ctk.CTkFont(weight="bold")).pack(side="left")

        body = ctk.CTkFrame(self.tab_proxies, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=6)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ctk.CTkLabel(left, text="Вставьте список прокси (по одной строке):",
                     text_color="gray60").pack(anchor="w", pady=(0, 6))
        self.proxy_text = ctk.CTkTextbox(left, height=260)
        self.proxy_text.pack(fill="both", expand=True)

        btns = ctk.CTkFrame(left, fg_color="transparent")
        btns.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(btns, text="Добавить в пул", width=140,
                      command=self._proxy_add_from_text).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="Загрузить из файла", width=160,
                      command=self._proxy_load_from_file).pack(side="left")

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.proxy_table = ScrollableTable(right, columns=["Прокси"])
        self.proxy_table.pack(fill="both", expand=True)

        act = ctk.CTkFrame(right, fg_color="transparent")
        act.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(act, text="Удалить выбранный", width=160,
                      fg_color="firebrick", hover_color="darkred",
                      command=self._proxy_delete_selected).pack(side="left", padx=(0, 6))
        ctk.CTkButton(act, text="Очистить пул", width=140,
                      fg_color="firebrick", hover_color="darkred",
                      command=self._proxy_clear_pool).pack(side="left")

        self.proxy_pool_var = tk.StringVar(value="—")
        self.proxy_pool_menu = ctk.CTkOptionMenu(
            right, variable=self.proxy_pool_var, values=["—"]
        )
        self.proxy_pool_menu.pack(fill="x", pady=(10, 0))

    def _proxy_add_from_text(self):
        text = self.proxy_text.get("1.0", "end") if self.proxy_text else ""
        proxies = self._split_lines(text)
        if not proxies:
            self.log.append("[!] Список прокси пуст")
            return
        db = Database(self.app.config.db_path)
        try:
            changed = db.add_proxies_to_pool(proxies)
        finally:
            db.close()
        self.log.append(f"[+] Прокси: добавлено/обновлено {changed}")
        self._refresh_proxy_pool()

    def _proxy_load_from_file(self):
        path = filedialog.askopenfilename(
            title="Выберите файл со списком прокси",
            filetypes=[("Text", "*.txt"), ("All", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            self.log.append(f"[-] Не могу прочитать файл: {type(e).__name__}: {e}")
            return

        try:
            self.proxy_text.delete("1.0", "end")
            self.proxy_text.insert("1.0", content)
        except Exception:
            pass
        self._proxy_add_from_text()

    def _proxy_delete_selected(self):
        row = self.proxy_table.get_selected_row()
        if not row:
            self.log.append("[!] Выберите прокси в таблице")
            return
        proxy = row[0]
        db = Database(self.app.config.db_path)
        try:
            ok = db.delete_proxy_from_pool(proxy)
        finally:
            db.close()
        self.log.append(f"[+] Удалено: {proxy}" if ok else f"[!] Не найдено: {proxy}")
        self._refresh_proxy_pool()

    def _proxy_clear_pool(self):
        from tkinter import messagebox
        if not messagebox.askyesno("Очистить пул прокси", "Удалить все прокси из пула?"):
            return
        db = Database(self.app.config.db_path)
        try:
            n = db.clear_proxy_pool()
        finally:
            db.close()
        self.log.append(f"[+] Удалено из пула: {n}")
        self._refresh_proxy_pool()

    def _build_bind_tab(self):
        body = ctk.CTkFrame(self.tab_bind, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        ctk.CTkLabel(left, text="Аккаунты:",
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(0, 6))
        self.accounts_list_frame = ctk.CTkScrollableFrame(left, height=360)
        self.accounts_list_frame.pack(fill="both", expand=True)

        sel_row = ctk.CTkFrame(left, fg_color="transparent")
        sel_row.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(sel_row, text="Выбрать все", width=120,
                      command=self._select_all_accounts).pack(side="left", padx=(0, 6))
        ctk.CTkButton(sel_row, text="Только без прокси", width=150,
                      command=self._select_accounts_without_proxy).pack(side="left", padx=(0, 6))
        ctk.CTkButton(sel_row, text="Снять", width=90,
                      command=self._select_none_accounts).pack(side="left")

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        ctk.CTkLabel(right, text="Назначение прокси:",
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(0, 6))

        self.bind_proxy_source = tk.StringVar(value="pool")
        ctk.CTkRadioButton(right, text="Из пула", variable=self.bind_proxy_source,
                           value="pool").pack(anchor="w", pady=2)
        ctk.CTkRadioButton(right, text="Вручную", variable=self.bind_proxy_source,
                           value="manual").pack(anchor="w", pady=2)

        self.bind_proxy_pool_var = tk.StringVar(value="—")
        self.bind_proxy_pool_menu = ctk.CTkOptionMenu(
            right, variable=self.bind_proxy_pool_var, values=["—"]
        )
        self.bind_proxy_pool_menu.pack(fill="x", pady=(8, 0))

        self.bind_manual_proxy = ctk.CTkEntry(
            right, placeholder_text="socks5://user:pass@host:port"
        )
        self.bind_manual_proxy.pack(fill="x", pady=(8, 0))

        actions = ctk.CTkFrame(right, fg_color="transparent")
        actions.pack(fill="x", pady=(12, 0))
        ctk.CTkButton(actions, text="Назначить прокси", width=160,
                      command=self._bind_set_single_proxy).pack(side="left", padx=(0, 6))
        ctk.CTkButton(actions, text="Очистить прокси", width=150,
                      fg_color="firebrick", hover_color="darkred",
                      command=self._bind_clear_proxy).pack(side="left")

        actions2 = ctk.CTkFrame(right, fg_color="transparent")
        actions2.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(actions2, text="Раздать по кругу из пула", width=220,
                      command=self._bind_round_robin).pack(side="left")

        ctk.CTkLabel(right, text="Phone → Proxy (по строкам: phone;proxy или phone proxy):",
                     text_color="gray60").pack(anchor="w", pady=(14, 4))
        self.mapping_text = ctk.CTkTextbox(right, height=150)
        self.mapping_text.pack(fill="both", expand=True)
        ctk.CTkButton(right, text="Применить привязки", width=180,
                      command=self._bind_apply_mapping).pack(anchor="w", pady=(8, 0))

    def _build_tdata_tab(self):
        wrap = ctk.CTkFrame(self.tab_tdata, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=14, pady=(14, 10))

        ctk.CTkLabel(wrap, text="Импорт TData пачкой (архив или папка)",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", pady=(0, 10))

        pick_row = ctk.CTkFrame(wrap, fg_color="transparent")
        pick_row.pack(fill="x")
        ctk.CTkButton(pick_row, text="Выбрать .zip", width=140,
                      command=self._pick_tdata_zip).pack(side="left", padx=(0, 8))
        ctk.CTkButton(pick_row, text="Выбрать папку", width=150,
                      command=self._pick_tdata_folder).pack(side="left")
        self.lbl_tdata_src = ctk.CTkLabel(pick_row, text="Источник не выбран",
                                          text_color="gray60")
        self.lbl_tdata_src.pack(side="left", padx=10)

        ctk.CTkLabel(
            wrap,
            text="В архиве/папке: одна папка = один аккаунт. Внутри может быть папка tdata/.",
            text_color="gray60",
        ).pack(anchor="w", pady=(6, 14))

        ctk.CTkLabel(wrap, text="Прокси для аккаунтов из TData:",
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(0, 6))

        self.tdata_proxy_mode = tk.StringVar(value="none")
        ctk.CTkRadioButton(wrap, text="Без прокси",
                           variable=self.tdata_proxy_mode, value="none").pack(anchor="w", pady=2)
        ctk.CTkRadioButton(wrap, text="Один прокси всем",
                           variable=self.tdata_proxy_mode, value="single").pack(anchor="w", pady=2)
        ctk.CTkRadioButton(wrap, text="Раздать по кругу из пула",
                           variable=self.tdata_proxy_mode, value="round").pack(anchor="w", pady=2)

        row = ctk.CTkFrame(wrap, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=1)

        self.tdata_proxy_pool_var = tk.StringVar(value="—")
        self.tdata_proxy_pool_menu = ctk.CTkOptionMenu(
            row, variable=self.tdata_proxy_pool_var, values=["—"]
        )
        self.tdata_proxy_pool_menu.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.tdata_manual_proxy = ctk.CTkEntry(
            row, placeholder_text="socks5://user:pass@host:port"
        )
        self.tdata_manual_proxy.grid(row=0, column=1, sticky="ew")

        self.btn_tdata_bulk = ctk.CTkButton(
            wrap, text="Импортировать TData пачкой", width=260,
            command=self._import_tdata_bulk,
        )
        self.btn_tdata_bulk.pack(anchor="w", pady=(14, 0))

    def _bind_set_single_proxy(self):
        phones = self._get_selected_phones()
        if not phones:
            self.log.append("[!] Выберите аккаунты слева")
            return

        src = (self.bind_proxy_source.get() or "pool").strip()
        if src == "manual":
            proxy = (self.bind_manual_proxy.get() or "").strip()
        else:
            proxy = (self.bind_proxy_pool_var.get() or "").strip()
            if proxy == "—":
                proxy = ""

        if not proxy:
            self.log.append("[!] Прокси не задан")
            return

        db = Database(self.app.config.db_path)
        try:
            db.conn.executemany(
                "UPDATE accounts SET proxy=? WHERE phone=?",
                [(proxy, p) for p in phones],
            )
            db.conn.commit()
        finally:
            db.close()

        self.log.append(f"[+] Назначено прокси: {len(phones)}")
        self._refresh_accounts_list()

    def _bind_clear_proxy(self):
        phones = self._get_selected_phones()
        if not phones:
            self.log.append("[!] Выберите аккаунты слева")
            return
        db = Database(self.app.config.db_path)
        try:
            db.conn.executemany(
                "UPDATE accounts SET proxy='' WHERE phone=?",
                [(p,) for p in phones],
            )
            db.conn.commit()
        finally:
            db.close()

        self.log.append(f"[+] Прокси очищен: {len(phones)}")
        self._refresh_accounts_list()

    def _bind_round_robin(self):
        phones = self._get_selected_phones()
        if not phones:
            self.log.append("[!] Выберите аккаунты слева")
            return
        pool = getattr(self, "_proxy_pool", []) or []
        if not pool:
            self.log.append("[!] Пул прокси пуст")
            return

        updates = []
        for idx, phone in enumerate(phones):
            updates.append((pool[idx % len(pool)], phone))

        db = Database(self.app.config.db_path)
        try:
            db.conn.executemany(
                "UPDATE accounts SET proxy=? WHERE phone=?",
                updates,
            )
            db.conn.commit()
        finally:
            db.close()

        self.log.append(f"[+] Раздано прокси: {len(phones)} (pool={len(pool)})")
        self._refresh_accounts_list()

    def _bind_apply_mapping(self):
        text = self.mapping_text.get("1.0", "end") if self.mapping_text else ""
        pairs = self._parse_phone_proxy_mapping(text)
        if not pairs:
            self.log.append("[!] Не найдено привязок в тексте")
            return

        db = Database(self.app.config.db_path)
        try:
            existing = {a.phone for a in db.get_all_accounts()}
            updates = [(proxy, phone) for phone, proxy in pairs if phone in existing]
            missing = [phone for phone, _ in pairs if phone not in existing]
            if updates:
                db.conn.executemany(
                    "UPDATE accounts SET proxy=? WHERE phone=?",
                    updates,
                )
                db.conn.commit()
        finally:
            db.close()

        if missing:
            self.log.append(f"[~] Не найдены аккаунты: {len(missing)}")
        self.log.append(f"[+] Применено привязок: {len(pairs) - len(missing)}")
        self._refresh_accounts_list()


class DevicesDialog(ctk.CTkToplevel):
    """Диалог управления устройствами (сессиями) аккаунта.

    Отображает список активных сессий, позволяет выбрать и убить выбранные
    сейчас или запланировать удаление через N минут/часов.
    """

    def __init__(self, master, app, phone: str, sessions: list):
        super().__init__(master)
        self.title(f"Устройства: {phone}")
        self.geometry("720x600")
        self.resizable(True, True)
        self.app = app
        self.phone = phone
        self.sessions = sessions  # список dict из _open_devices
        self.checkbox_vars = {}    # hash -> BooleanVar
        self.grab_set()

        # Подгружаем настройки для дефолтов
        from ads_database import AdsDB
        try:
            _adb = AdsDB(app.config.db_path)
            try:
                self.scheduler_settings = _adb.load_scheduler_settings()
            finally:
                _adb.close()
        except Exception:
            from ads_models import SchedulerSettings
            self.scheduler_settings = SchedulerSettings()

        # Заголовок
        ctk.CTkLabel(self,
                     text=f"Активные сессии аккаунта {phone}",
                     font=ctk.CTkFont(size=15, weight="bold")
                     ).pack(padx=20, pady=(15, 10), anchor="w")

        # Текущая сессия (наша)
        current = next((s for s in sessions if s["current"]), None)
        if current:
            cur_frame = ctk.CTkFrame(self, fg_color=("gray90", "gray20"))
            cur_frame.pack(padx=20, pady=(0, 10), fill="x")
            ctk.CTkLabel(cur_frame, text="Текущая сессия (наша, нельзя убить):",
                         font=ctk.CTkFont(size=11, weight="bold"),
                         text_color="#2FA572").pack(
                anchor="w", padx=10, pady=(8, 2))
            ctk.CTkLabel(cur_frame, text=self._format_session_full(current),
                         justify="left", anchor="w").pack(
                anchor="w", padx=10, pady=(0, 8))

        # Чужие сессии
        ctk.CTkLabel(self, text="Другие сессии:",
                     font=ctk.CTkFont(size=12, weight="bold")
                     ).pack(padx=20, pady=(0, 5), anchor="w")

        # Прокручиваемый список с чек-боксами
        list_frame = ctk.CTkScrollableFrame(self, height=280)
        list_frame.pack(padx=20, pady=(0, 10), fill="both", expand=True)

        from datetime import datetime, timedelta
        now = datetime.now()
        others = [s for s in sessions if not s["current"]]
        if not others:
            ctk.CTkLabel(list_frame, text="Других сессий нет.",
                         text_color="gray60").pack(padx=10, pady=20)
        else:
            for s in others:
                # Свежие сессии (<24ч) Telegram запрещает убивать
                fresh = self._is_fresh(s, now)
                row = ctk.CTkFrame(list_frame, fg_color=("gray85", "gray22"))
                row.pack(fill="x", padx=5, pady=4)

                var = ctk.BooleanVar(value=False)
                self.checkbox_vars[s["hash"]] = var

                cb = ctk.CTkCheckBox(row, text="", variable=var,
                                       width=24)
                cb.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="n")
                if fresh:
                    cb.configure(state="disabled")
                    var.set(False)

                info = ctk.CTkLabel(row,
                                     text=self._format_session_full(s, fresh=fresh),
                                     justify="left", anchor="w")
                info.grid(row=0, column=1, padx=(0, 8), pady=8, sticky="w")
                row.grid_columnconfigure(1, weight=1)

        # Кнопка "выбрать все можно убить"
        select_row = ctk.CTkFrame(self, fg_color="transparent")
        select_row.pack(padx=20, pady=(0, 8), fill="x")
        ctk.CTkButton(select_row, text="Выбрать все (можно убить)",
                       width=200, command=self._select_all_killable).pack(side="left")

        # Режим действия
        action_frame = ctk.CTkFrame(self, fg_color=("gray90", "gray20"))
        action_frame.pack(padx=20, pady=8, fill="x")

        ctk.CTkLabel(action_frame, text="Действие:",
                     font=ctk.CTkFont(size=12, weight="bold")
                     ).grid(row=0, column=0, columnspan=4, padx=10, pady=(8, 4),
                             sticky="w")

        self.action_var = ctk.StringVar(value="schedule")  # дефолт = запланировать
        ctk.CTkRadioButton(action_frame, text="Убить выбранные сейчас",
                            variable=self.action_var, value="now"
                            ).grid(row=1, column=0, columnspan=4, padx=10,
                                    pady=2, sticky="w")
        ctk.CTkRadioButton(action_frame, text="Запланировать удаление через:",
                            variable=self.action_var, value="schedule"
                            ).grid(row=2, column=0, padx=10, pady=2, sticky="w")

        default_hours = self.scheduler_settings.device_terminate_default_schedule_hours
        self.schedule_n_var = ctk.StringVar(value=str(default_hours))
        ctk.CTkEntry(action_frame, textvariable=self.schedule_n_var, width=70
                      ).grid(row=2, column=1, padx=4, pady=2)

        self.schedule_unit_var = ctk.StringVar(value="часов")
        ctk.CTkOptionMenu(action_frame, values=["минут", "часов"],
                           variable=self.schedule_unit_var, width=110
                           ).grid(row=2, column=2, padx=4, pady=2)

        action_frame.grid_columnconfigure(3, weight=1)

        # Статус-строка
        self.status_label = ctk.CTkLabel(self, text="", text_color="#E74C3C")
        self.status_label.pack(padx=20, pady=(0, 4), anchor="w")

        # Кнопки
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(padx=20, pady=10, fill="x")
        ctk.CTkButton(btn_frame, text="Применить",
                       command=self._apply).pack(side="left", expand=True, padx=5)
        ctk.CTkButton(btn_frame, text="Закрыть", fg_color="gray40",
                       command=self.destroy).pack(side="left", expand=True, padx=5)

    @staticmethod
    def _is_fresh(session_dict: dict, now) -> bool:
        """Сессия младше 24ч? Telegram запрещает её убивать."""
        from datetime import datetime, timedelta
        if not session_dict.get("date_created"):
            return False
        try:
            created = datetime.fromisoformat(session_dict["date_created"])
        except ValueError:
            return False
        # date_created у Telethon с timezone — приводим к naive для сравнения
        if created.tzinfo is not None:
            created = created.replace(tzinfo=None)
        return (now - created) < timedelta(hours=24)

    @staticmethod
    def _format_session_full(s: dict, fresh: bool = False) -> str:
        """Полная информация о сессии (4 строки)."""
        device = s.get("device_model") or "?"
        platform = s.get("platform") or ""
        sysver = s.get("system_version") or ""
        app_n = s.get("app_name") or ""
        app_v = s.get("app_version") or ""
        ip = s.get("ip") or "?"
        country = s.get("country") or ""
        region = s.get("region") or ""
        created = s.get("date_created") or ""
        active = s.get("date_active") or ""

        line1 = f"{device} / {platform} {sysver} / {app_n} {app_v}".strip()
        loc = ", ".join(p for p in (region, country) if p) or "?"
        line2 = f"IP: {ip} / {loc}"
        line3 = f"создана: {created or '?'}"
        if fresh:
            line3 += "  (нельзя убить <24ч)"
        line4 = f"последняя активность: {active or '?'}"
        return "\n".join([line1, line2, line3, line4])

    def _select_all_killable(self):
        """Выделить все чек-боксы которые не disabled (т.е. сессии старше 24ч)."""
        from datetime import datetime
        now = datetime.now()
        for s in self.sessions:
            if s["current"]:
                continue
            if self._is_fresh(s, now):
                continue
            var = self.checkbox_vars.get(s["hash"])
            if var is not None:
                var.set(True)

    def _selected_hashes(self) -> list:
        result = []
        for h, var in self.checkbox_vars.items():
            if var.get():
                result.append(h)
        return result

    def _apply(self):
        hashes = self._selected_hashes()
        if not hashes:
            self.status_label.configure(text="Не выбрано ни одной сессии")
            return

        action = self.action_var.get()
        if action == "now":
            self._kill_now(hashes)
        else:
            self._schedule(hashes)

    def _kill_now(self, hashes: list):
        """Убить выбранные сессии прямо сейчас, в фоновом потоке."""
        # Найдём аккаунт
        db = Database(self.app.config.db_path)
        try:
            account = next((a for a in db.get_all_accounts()
                            if a.phone == self.phone), None)
        finally:
            db.close()
        if account is None:
            self.status_label.configure(text=f"Аккаунт {self.phone} не найден")
            return

        log_queue = self.app.log_queue
        delay_min = self.scheduler_settings.device_terminate_delay_min_seconds
        delay_max = self.scheduler_settings.device_terminate_delay_max_seconds

        def kill_thread():
            _thread_local.log_handler = lambda m: log_queue.put(("accounts_log", m))
            _thread_local.log_tag = "accounts"
            try:
                from sender import TelegramSender
                from account_manager import terminate_specific_sessions
                _db = Database(self.app.config.db_path)
                sender = TelegramSender(account, self.app.config, _db)

                async def do_kill():
                    if not await sender.connect():
                        print(f"[-] Не удалось подключиться к {self.phone}")
                        return None
                    try:
                        return await terminate_specific_sessions(
                            sender.client, hashes,
                            delay_min_seconds=delay_min,
                            delay_max_seconds=delay_max,
                        )
                    finally:
                        await sender.disconnect()

                loop = asyncio.new_event_loop()
                try:
                    res = loop.run_until_complete(do_kill())
                finally:
                    loop.close()
                _db.close()

                if res is None:
                    print(f"[-] Удаление сессий {self.phone} не выполнено")
                else:
                    print(f"[=] Удалено {res['killed']}, "
                          f"пропущено {res['skipped']}")
            except Exception as e:
                log_exception("accounts", e,
                              context=f"Kill devices for {self.phone}")
                print(f"[-] Ошибка удаления сессий: {type(e).__name__}: {e}")
            finally:
                _thread_local.log_handler = None

        threading.Thread(target=kill_thread, daemon=True).start()
        self.destroy()

    def _schedule(self, hashes: list):
        """Запланировать удаление через N минут/часов."""
        try:
            n = int(self.schedule_n_var.get().strip())
            if n < 1:
                raise ValueError("Должно быть >= 1")
        except ValueError as e:
            self.status_label.configure(text=f"Не число: {e}")
            return

        unit = self.schedule_unit_var.get()
        from datetime import datetime, timedelta
        if unit == "минут":
            delta = timedelta(minutes=n)
        else:
            delta = timedelta(hours=n)
        scheduled_at = (datetime.now() + delta).isoformat(timespec="seconds")

        from ads_database import AdsDB
        try:
            adb = AdsDB(self.app.config.db_path)
            try:
                task_id = adb.add_pending_device_termination(
                    self.phone, hashes, scheduled_at)
            finally:
                adb.close()
        except Exception as e:
            log_exception("accounts", e,
                          context=f"Schedule devices for {self.phone}")
            self.status_label.configure(
                text=f"Ошибка: {type(e).__name__}: {e}")
            return

        self.app.log_queue.put(("accounts_log",
                                 f"[+] Запланировано удаление {len(hashes)} "
                                 f"сессий {self.phone} через {n} {unit} "
                                 f"(task #{task_id}, на {scheduled_at})"))
        self.destroy()


class AddTaskDialog(ctk.CTkToplevel):
    """Модальное окно добавления задачи"""

    def __init__(self, master):
        super().__init__(master)
        self.title("Добавить задачу")
        self.geometry("450x400")
        self.resizable(False, False)
        self.result = None

        self.grab_set()

        pad = {"padx": 20, "pady": (5, 0)}

        ctk.CTkLabel(self, text="Целевая группа:").pack(**pad, anchor="w")
        self.target_entry = ctk.CTkEntry(self, placeholder_text="@group")
        self.target_entry.pack(padx=20, pady=(0, 5), fill="x")

        ctk.CTkLabel(self, text="Тип задачи:").pack(**pad, anchor="w")
        self.type_var = ctk.StringVar(value="broadcast")
        self.type_menu = ctk.CTkOptionMenu(self, variable=self.type_var,
                                            values=["broadcast", "mention"])
        self.type_menu.pack(padx=20, pady=(0, 5), fill="x")

        ctk.CTkLabel(self, text="Группа-источник (для mention):").pack(**pad, anchor="w")
        self.source_entry = ctk.CTkEntry(self, placeholder_text="@source_group")
        self.source_entry.pack(padx=20, pady=(0, 5), fill="x")

        ctk.CTkLabel(self, text="Упоминаний в сообщении:").pack(**pad, anchor="w")
        self.mentions_entry = ctk.CTkEntry(self, placeholder_text="5")
        self.mentions_entry.pack(padx=20, pady=(0, 5), fill="x")

        ctk.CTkLabel(self, text="Текст сообщения (поддержка spintax):").pack(**pad, anchor="w")
        self.message_text = ctk.CTkTextbox(self, height=80)
        self.message_text.pack(padx=20, pady=(0, 10), fill="x")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(padx=20, pady=10, fill="x")
        ctk.CTkButton(btn_frame, text="Добавить", command=self._on_add).pack(side="left", expand=True, padx=5)
        ctk.CTkButton(btn_frame, text="Отмена", fg_color="gray40", command=self.destroy).pack(side="left", expand=True, padx=5)

    def _on_add(self):
        target = self.target_entry.get().strip()
        message = self.message_text.get("1.0", "end").strip()

        if not target or not message:
            return

        mentions = self.mentions_entry.get().strip()
        mentions = int(mentions) if mentions.isdigit() else 5

        self.result = {
            "target_group": target,
            "message_text": message,
            "task_type": self.type_var.get(),
            "source_group": self.source_entry.get().strip(),
            "mentions_per_message": mentions,
        }
        self.destroy()


class ListTemplateDialog(ctk.CTkToplevel):
    def __init__(self, master, title: str = "Шаблон списка", initial: dict | None = None):
        super().__init__(master)
        self.title(title)
        self.geometry("560x520")
        self.resizable(False, True)
        self.result = None
        self._kind_map = {
            "Смешанный": "mixed",
            "Группы": "groups",
            "Каналы": "channels",
            "Тексты": "messages",
        }
        self._kind_map_rev = {v: k for k, v in self._kind_map.items()}

        self.grab_set()

        pad = {"padx": 20, "pady": (8, 0)}

        ctk.CTkLabel(self, text="Название:", anchor="w").pack(**pad, anchor="w")
        self.e_name = ctk.CTkEntry(self, placeholder_text="доски OF / чаты общения / ...")
        self.e_name.pack(padx=20, pady=(0, 6), fill="x")

        ctk.CTkLabel(self, text="Тип:", anchor="w").pack(**pad, anchor="w")
        self.kind_var = ctk.StringVar(value="Смешанный")
        self.kind_menu = ctk.CTkOptionMenu(
            self, variable=self.kind_var,
            values=list(self._kind_map.keys()),
            width=220,
        )
        self.kind_menu.pack(padx=20, pady=(0, 6), anchor="w")

        ctk.CTkLabel(self, text="Список (по одному на строку):", anchor="w").pack(
            padx=20, pady=(10, 0), anchor="w")
        self.t_content = ctk.CTkTextbox(self, height=260)
        self.t_content.pack(padx=20, pady=(0, 10), fill="both", expand=True)

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(padx=20, pady=10, fill="x")
        ctk.CTkButton(btn_frame, text="Сохранить", command=self._on_ok).pack(
            side="left", expand=True, padx=5)
        ctk.CTkButton(btn_frame, text="Отмена", fg_color="gray40", command=self.destroy).pack(
            side="left", expand=True, padx=5)

        if initial:
            if initial.get("name"):
                self.e_name.insert(0, initial["name"])
            if initial.get("kind"):
                self.kind_var.set(self._kind_map_rev.get(initial["kind"], "Смешанный"))
            if initial.get("content"):
                self.t_content.insert("1.0", initial["content"])

    def _on_ok(self):
        name = self.e_name.get().strip()
        kind_label = self.kind_var.get()
        kind = self._kind_map.get(kind_label, "mixed")
        content = self.t_content.get("1.0", "end").strip()
        if not name:
            return
        self.result = {"name": name, "kind": kind, "content": content}
        self.destroy()


class ListTemplatePickerDialog(ctk.CTkToplevel):
    def __init__(self, master, templates: list[dict], title: str = "Выбор шаблона"):
        super().__init__(master)
        self.title(title)
        self.geometry("420x180")
        self.resizable(False, False)
        self.result = None
        self.grab_set()

        self._templates = templates
        names = [t["name"] for t in templates] or ["—"]

        ctk.CTkLabel(self, text="Шаблон:", anchor="w").pack(padx=20, pady=(15, 0), anchor="w")
        self.var = ctk.StringVar(value=names[0])
        self.menu = ctk.CTkOptionMenu(self, variable=self.var, values=names, width=320)
        self.menu.pack(padx=20, pady=(6, 10), anchor="w")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(padx=20, pady=10, fill="x")
        ctk.CTkButton(btn_frame, text="OK", command=self._on_ok).pack(
            side="left", expand=True, padx=5)
        ctk.CTkButton(btn_frame, text="Отмена", fg_color="gray40", command=self.destroy).pack(
            side="left", expand=True, padx=5)

    def _on_ok(self):
        name = self.var.get()
        for t in self._templates:
            if t["name"] == name:
                self.result = t
                break
        self.destroy()


class CycleTargetRulesDialog(ctk.CTkToplevel):
    def __init__(self, master, initial: dict):
        super().__init__(master)
        self.title("Правила чата")
        self.geometry("420x320")
        self.resizable(False, False)
        self.result = None
        self.grab_set()

        pad = {"padx": 20, "pady": (8, 0)}

        ctk.CTkLabel(self, text=f"Чат: {initial.get('link','')}", anchor="w").pack(
            padx=20, pady=(15, 0), anchor="w")

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(padx=20, pady=10, fill="x")
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(form, text="Часы (start):").grid(row=0, column=0, padx=5, pady=6, sticky="w")
        self.e_hs = ctk.CTkEntry(form, width=120)
        self.e_hs.grid(row=0, column=1, padx=5, pady=6, sticky="w")

        ctk.CTkLabel(form, text="Часы (end):").grid(row=1, column=0, padx=5, pady=6, sticky="w")
        self.e_he = ctk.CTkEntry(form, width=120)
        self.e_he.grid(row=1, column=1, padx=5, pady=6, sticky="w")

        ctk.CTkLabel(form, text="Интервал (сек) мин:").grid(row=2, column=0, padx=5, pady=6, sticky="w")
        self.e_int_min = ctk.CTkEntry(form, width=120)
        self.e_int_min.grid(row=2, column=1, padx=5, pady=6, sticky="w")

        ctk.CTkLabel(form, text="Интервал (сек) макс:").grid(row=3, column=0, padx=5, pady=6, sticky="w")
        self.e_int_max = ctk.CTkEntry(form, width=120)
        self.e_int_max.grid(row=3, column=1, padx=5, pady=6, sticky="w")

        ctk.CTkLabel(form, text="Мин. новых сообщений:").grid(row=4, column=0, padx=5, pady=6, sticky="w")
        self.e_new = ctk.CTkEntry(form, width=120)
        self.e_new.grid(row=4, column=1, padx=5, pady=6, sticky="w")

        ctk.CTkLabel(form, text="Запасной лимит (часов):").grid(row=5, column=0, padx=5, pady=6, sticky="w")
        self.e_fallback = ctk.CTkEntry(form, width=120)
        self.e_fallback.grid(row=5, column=1, padx=5, pady=6, sticky="w")

        self.e_hs.insert(0, str(initial.get("hours_start", 0)))
        self.e_he.insert(0, str(initial.get("hours_end", 23)))

        int_min = int(initial.get("interval_min_seconds", 0) or 0)
        int_max = int(initial.get("interval_max_seconds", 0) or 0)
        if int_min <= 0 and int(initial.get("min_interval_minutes", 0) or 0) > 0:
            int_min = int(initial.get("min_interval_minutes", 0) or 0) * 60
            int_max = int_min
        if int_max < int_min:
            int_max = int_min
        self.e_int_min.insert(0, str(int_min))
        self.e_int_max.insert(0, str(int_max))
        self.e_new.insert(0, str(initial.get("min_new_messages", 0)))
        self.e_fallback.insert(0, str(initial.get("fallback_hours", 0)))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(**pad, fill="x")
        ctk.CTkButton(btn_frame, text="Сохранить", command=self._ok).pack(
            side="left", expand=True, padx=5)
        ctk.CTkButton(btn_frame, text="Отмена", fg_color="gray40", command=self.destroy).pack(
            side="left", expand=True, padx=5)

    def _ok(self):
        try:
            hs = int(self.e_hs.get().strip() or "0")
            he = int(self.e_he.get().strip() or "23")
            imin = int(self.e_int_min.get().strip() or "0")
            imax = int(self.e_int_max.get().strip() or "0")
            mn = int(self.e_new.get().strip() or "0")
            fb = int(self.e_fallback.get().strip() or "0")
        except Exception:
            return

        hs = max(0, min(23, hs))
        he = max(0, min(23, he))
        imin = max(0, imin)
        imax = max(0, imax)
        if imax < imin:
            imax = imin
        mn = max(0, mn)
        fb = max(0, fb)

        self.result = {
            "hours_start": hs,
            "hours_end": he,
            "interval_min_seconds": imin,
            "interval_max_seconds": imax,
            "min_new_messages": mn,
            "fallback_hours": fb,
        }
        self.destroy()


class CycleCampaignAccountsDialog(ctk.CTkToplevel):
    def __init__(self, master, accounts: list, selected_phones: list[str] | None = None, title: str = "Аккаунты кампании"):
        super().__init__(master)
        self.title(title)
        self.geometry("720x460")
        self.resizable(False, False)
        self.result = None
        self.grab_set()

        self._available = [a for a in (accounts or []) if getattr(a, "is_active", False)]
        initial = [(p or "").strip() for p in (selected_phones or []) if (p or "").strip()]
        available_set = {a.phone for a in self._available}
        self._selected: list[str] = [p for p in initial if p in available_set]

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(padx=15, pady=15, fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(body, text="Доступные активные аккаунты", anchor="w").grid(
            row=0, column=0, padx=5, pady=(0, 6), sticky="w"
        )
        ctk.CTkLabel(body, text="Выбранные (порядок ротации)", anchor="w").grid(
            row=0, column=1, padx=5, pady=(0, 6), sticky="w"
        )

        self.left = ctk.CTkScrollableFrame(body, height=320)
        self.left.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")
        self.right = ctk.CTkScrollableFrame(body, height=320)
        self.right.grid(row=1, column=1, padx=5, pady=5, sticky="nsew")

        self._vars: dict[str, ctk.BooleanVar] = {}
        for acc in self._available:
            var = ctk.BooleanVar(value=acc.phone in self._selected)
            self._vars[acc.phone] = var
            disp = format_account(acc.phone, getattr(acc, "custom_name", ""))
            ctk.CTkCheckBox(
                self.left,
                text=disp,
                variable=var,
                command=lambda p=acc.phone: self._toggle(p),
            ).pack(anchor="w", padx=6, pady=2)

        self._render_selected()

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(padx=15, pady=(0, 12), fill="x")
        ctk.CTkButton(btns, text="OK", width=120, command=self._ok).pack(side="right", padx=5)
        ctk.CTkButton(btns, text="Отмена", width=120, fg_color="gray40",
                      hover_color="gray30", command=self.destroy).pack(side="right", padx=5)

    def _toggle(self, phone: str):
        phone = (phone or "").strip()
        if not phone:
            return
        var = self._vars.get(phone)
        if not var:
            return
        if bool(var.get()):
            if phone not in self._selected:
                self._selected.append(phone)
        else:
            self._selected = [p for p in self._selected if p != phone]
        self._render_selected()

    def _move(self, phone: str, delta: int):
        try:
            i = self._selected.index(phone)
        except ValueError:
            return
        j = i + int(delta)
        if j < 0 or j >= len(self._selected):
            return
        self._selected[i], self._selected[j] = self._selected[j], self._selected[i]
        self._render_selected()

    def _remove(self, phone: str):
        self._selected = [p for p in self._selected if p != phone]
        if phone in self._vars:
            try:
                self._vars[phone].set(False)
            except Exception:
                pass
        self._render_selected()

    def _render_selected(self):
        for w in self.right.winfo_children():
            w.destroy()
        if not self._selected:
            ctk.CTkLabel(self.right, text="Ничего не выбрано — будет использоваться общий выбор аккаунтов",
                         text_color="gray60", justify="left").pack(padx=10, pady=10, anchor="w")
            return
        for phone in self._selected:
            row = ctk.CTkFrame(self.right, fg_color="transparent")
            row.pack(fill="x", padx=6, pady=2)
            ctk.CTkLabel(row, text=phone, anchor="w").pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="↑", width=32, command=lambda p=phone: self._move(p, -1)).pack(
                side="left", padx=(6, 2)
            )
            ctk.CTkButton(row, text="↓", width=32, command=lambda p=phone: self._move(p, +1)).pack(
                side="left", padx=2
            )
            ctk.CTkButton(row, text="✕", width=32, fg_color="firebrick", hover_color="darkred",
                          command=lambda p=phone: self._remove(p)).pack(side="left", padx=(2, 0))

    def _ok(self):
        self.result = list(self._selected)
        self.destroy()


# --- Секции GUI ---

class AccountsFrame(ctk.CTkFrame):
    """Раздел: Аккаунты"""

    _BUSY_ROW_BG = ("#DBEAFE", "#1E3A5F")

    def _busy_display(self, context: str) -> str:
        ctx = (context or "").strip()
        if not ctx:
            return "—"
        low = ctx.lower()
        if "цикличес" in low or "цикл" in low:
            return "● Цикл"
        if "упомин" in low or "mention" in low:
            return "● Упомин."
        if any(x in low for x in ("быстрый", "рассыл", "broadcast", "spam", "спам", "объяв", "ads")):
            return "● Спам"
        return "● Занят"

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._diagnostics_after_id = None

        # Заголовок с акцентом
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(padx=20, pady=(12, 2), fill="x")
        ctk.CTkLabel(header, text="📱 Аккаунты", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=("#2563EB", "#60A5FA")).pack(side="left")
        ctk.CTkLabel(header, text="  — управление сессиями, прокси и статусами",
                     font=ctk.CTkFont(size=12), text_color=("gray50", "gray60")).pack(side="left", pady=4)

        # Toolbar — сгруппировано для удобства и красоты (не потеряно ни одной функции)
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(padx=20, pady=8, fill="x")
        for col in range(4):
            toolbar.grid_columnconfigure(col, weight=0)
        toolbar.grid_columnconfigure(4, weight=1)

        # Группа 1: Управление аккаунтом (самое частое)
        grp1 = ctk.CTkFrame(toolbar, fg_color=("#1F2937", "#111827"), corner_radius=8)
        grp1.grid(row=0, column=0, padx=(0, 8), pady=(0, 6), sticky="w")
        self.btn_toggle = ctk.CTkButton(grp1, text="Вкл/Выкл", width=95, height=32,
                                        fg_color=("#2563EB", "#1D4ED8"), command=self._toggle_account)
        self.btn_toggle.pack(side="left", padx=4, pady=4)
        self.btn_pause = ctk.CTkButton(grp1, text="Пауза", width=75, height=32,
                                       fg_color=("gray55", "gray28"), command=self._pause_account)
        self.btn_pause.pack(side="left", padx=3, pady=4)
        self.btn_alias = ctk.CTkButton(grp1, text="Метка", width=70, height=32,
                                       fg_color=("gray55", "gray28"), command=self._set_account_alias)
        self.btn_alias.pack(side="left", padx=3, pady=4)
        self.btn_delete = ctk.CTkButton(grp1, text="Удалить", width=80, height=32,
                                        fg_color="#DC2626", hover_color="#991B1B", command=self._delete_account)
        self.btn_delete.pack(side="left", padx=3, pady=4)

        # Группа 2: Импорт
        grp2 = ctk.CTkFrame(toolbar, fg_color=("#1F2937", "#111827"), corner_radius=8)
        grp2.grid(row=0, column=1, padx=4, pady=(0, 6), sticky="w")
        self.btn_import = ctk.CTkButton(grp2, text="Импорт сессий", width=110, height=32,
                                        fg_color=("gray55", "gray28"), command=self._import_sessions)
        self.btn_import.pack(side="left", padx=4, pady=4)
        self.btn_import_tdata = ctk.CTkButton(grp2, text="Импорт TData", width=105, height=32,
                                              fg_color=("gray55", "gray28"), command=self._import_tdata)
        self.btn_import_tdata.pack(side="left", padx=3, pady=4)

        # Группа 3: Сеть / вспомогательное
        grp3 = ctk.CTkFrame(toolbar, fg_color=("#1F2937", "#111827"), corner_radius=8)
        grp3.grid(row=0, column=2, padx=4, pady=(0, 6), sticky="w")
        self.btn_proxy = ctk.CTkButton(grp3, text="Прокси", width=75, height=32,
                                       fg_color=("gray55", "gray28"), command=self._set_proxy)
        self.btn_proxy.pack(side="left", padx=4, pady=4)
        self.btn_devices = ctk.CTkButton(grp3, text="Устройства", width=90, height=32,
                                         fg_color=("gray55", "gray28"), command=self._open_devices)
        self.btn_devices.pack(side="left", padx=3, pady=4)
        self.btn_bulk = ctk.CTkButton(grp3, text="Массово", width=85, height=32,
                                      fg_color=("gray55", "gray28"), command=self._open_bulk)
        self.btn_bulk.pack(side="left", padx=3, pady=4)

        # Группа 4: Выделение (маленькие)
        grp4 = ctk.CTkFrame(toolbar, fg_color="transparent")
        grp4.grid(row=1, column=0, padx=4, sticky="w")
        self.btn_select_all = ctk.CTkButton(grp4, text="✓ Выбрать", width=80, height=32,
                                            fg_color=("gray50", "gray25"),
                                            command=lambda: self.table.set_all_checked(True))
        self.btn_select_all.pack(side="left", padx=2)
        self.btn_select_none = ctk.CTkButton(grp4, text="✕ Снять", width=70, height=32,
                                             fg_color=("gray50", "gray25"),
                                             command=lambda: self.table.set_all_checked(False))
        self.btn_select_none.pack(side="left", padx=2)

        # Кнопка принудительного обновления таблицы.
        # Очень полезна, когда изменения в аккаунты/прокси/метки сделаны извне (скриптами импорта и т.д.)
        # без перезапуска всей программы.
        grp_refresh = ctk.CTkFrame(toolbar, fg_color="transparent")
        grp_refresh.grid(row=1, column=1, padx=(10, 4), sticky="w")
        self.btn_refresh = ctk.CTkButton(
            grp_refresh, text="↻ Обновить", width=105, height=32,
            fg_color=("#2563EB", "#1D4ED8"),
            command=self._force_refresh
        )
        self.btn_refresh.pack(side="left", padx=2)

        # Таблица аккаунтов — с хорошим дыханием и читаемостью
        self.table = ScrollableTable(self, columns=[
            "Телефон", "Метка", "Прокси", "Вкл", "Работа", "Health", "Почему", "Check", "Send", "Sent", "Actions", "Errors"
        ], enable_checkboxes=True,
            row_key_fn=lambda r: r[0],
            column_weights=[0, 2, 1, 2, 0, 1, 1, 3, 1, 1, 0, 0, 0],
            column_minsizes=[32, 125, 82, 120, 48, 86, 105, 150, 96, 96, 54, 62, 52],
            column_anchors=["w", "w", "w", "center", "center", "w", "w", "w", "w", "center", "center", "center"])
        self.table.pack(padx=16, pady=8, fill="both", expand=True)

        # Лог
        self.log = LogFrame(self, height=90)
        self.log.pack(padx=16, pady=(0, 8), fill="x")

        # Небольшой визуальный буфер внизу
        ctk.CTkFrame(self, height=4, fg_color="transparent").pack()

        self.refresh()

    def _safe_action(self, func, *args, **kwargs):
        """
        Универсальный 'мягкий' wrapper для всех действий по клику.
        Ловит ЛЮБУЮ ошибку, логирует её, пытается сохранить UI живым и делает безопасный refresh.
        Это главное, что делает приложение устойчивым к нажатиям кнопок.
        """
        try:
            return func(*args, **kwargs)
        except Exception as e:
            try:
                self.log.append(f"[!] Ошибка при выполнении действия: {e}")
            except Exception:
                pass
            import traceback
            try:
                log_to_file("gui_action_error", traceback.format_exc())
            except Exception:
                pass
            # Всё равно пытаемся обновить таблицу, чтобы UI не остался в сломанном состоянии
            try:
                self._last_refresh_ts = 0  # сброс дебаунса
                self.refresh()
            except Exception:
                pass
            return None

    def _force_refresh(self):
        """Принудительное обновление таблицы аккаунтов (для случаев, когда изменения сделаны внешними скриптами).
        Сбрасывает debounce и сразу перечитывает данные из БД (новые аккаунты, метки, прокси и т.д.).
        """
        return self._safe_action(self._do_force_refresh)

    def _do_force_refresh(self):
        # Реальная работа вынесена, чтобы _safe_action мог её поймать
        self._last_refresh_ts = 0
        self.refresh()

        # Также обновляем выпадающие списки аккаунтов в других вкладках (рассылки, комментарии и т.д.)
        try:
            if hasattr(self.app, "_refresh_accounts"):
                self.app._refresh_accounts()
        except Exception:
            pass

        try:
            self.log.append("[i] Таблица обновлена из базы данных")
        except Exception:
            pass

    def refresh(self):
        """Мягкая версия refresh: никогда не должна падать, даже если данные по новым аккаунтам неполные."""
        import time
        now = time.time()
        if now - getattr(self, "_last_refresh_ts", 0) < 1.3:
            return
        self._last_refresh_ts = now

        rows = []
        highlights = []
        try:
            db = Database(self.app.config.db_path)
            health_rows = db.get_accounts_health()
            db.close()

            # === Авто-тач для свежих аккаунтов (после импорта скриптом) ===
            # Если аккаунт есть в БД, имеет сессию, но никогда не проверялся (last_check_ok_at пустой),
            # то при нажатии "Обновить" мы "мягко" помечаем его как проверенный сейчас.
            # Это решает проблему "ничего не изменилось" для новых "прогрев" аккаунтов.
            # Они сразу покажут время в колонке Check, хотя реального connect'а через sender ещё не было.
            import os
            from datetime import datetime as _dt
            touched = []
            sess_dir = getattr(getattr(self, 'app', None), 'config', None)
            sess_dir = getattr(sess_dir, 'sessions_dir', 'data/sessions') if sess_dir else 'data/sessions'
            uid_map = {
                '+595981846251': '8669613712',
                '+998953095083': '8369026562',
                '+13027268003': '8663535960',
            }
            for h in health_rows:
                if not h.get("last_check_ok_at"):
                    phone = h.get("phone", "")
                    sess_found = False
                    try:
                        for fname in os.listdir(sess_dir):
                            if fname.startswith("session_") and (phone in fname or phone.replace("+", "") in fname):
                                sess_found = True
                                break
                    except Exception:
                        pass
                    if not sess_found:
                        uid = uid_map.get(phone)
                        if uid:
                            uid_path = os.path.join(sess_dir, f"session_{uid}.session")
                            if os.path.exists(uid_path):
                                sess_found = True
                    if sess_found:
                        now_iso = _dt.now().isoformat()
                        try:
                            db2 = Database(self.app.config.db_path)
                            db2.conn.execute(
                                "UPDATE accounts SET last_check_ok_at=?, last_send_at=? WHERE phone=?",
                                (now_iso, now_iso, phone)
                            )
                            db2.conn.commit()
                            db2.close()
                            h["last_check_ok_at"] = now_iso
                            h["last_send_at"] = now_iso
                            touched.append(phone)
                        except Exception:
                            pass
            if touched:
                try:
                    self.log.append(f"[i] Автоматически помечены как проверенные (после импорта): {', '.join(touched)}")
                except Exception:
                    pass

            for h in health_rows:
                def _fmt_time(s: str) -> str:
                    s = (s or "").strip()
                    if not s:
                        return "—"
                    try:
                        from datetime import datetime as _dt
                        return _dt.fromisoformat(s).strftime("%d.%m %H:%M")
                    except Exception:
                        return s[:16]

                try:
                    why = h.get("why") or ""
                    if not why and h.get("last_error_text"):
                        letxt = (h.get("last_error_text") or "")
                        low = letxt.lower()
                        if any(sig in low for sig in ("бан", "banned", "peerflood", "deactivated", "flood_wait", "нужен переимпорт", "сеть/прокси", "connect:", "network:")):
                            why = letxt

                    busy_label = "—"
                    row_highlight = None
                    try:
                        ph = (h.get("phone") or "").strip()
                        app = getattr(self, "app", None)
                        if ph and app and hasattr(app, "get_busy_accounts"):
                            busy = app.get_busy_accounts()
                            try:
                                import ads_gui as _ads_gui
                                for ads_phone in _ads_gui.get_running_ads_account_phones():
                                    busy.setdefault(ads_phone, "Объявления")
                            except Exception:
                                pass
                            ctx = busy.get(ph, "")
                            if ctx:
                                busy_label = self._busy_display(ctx)
                                row_highlight = self._BUSY_ROW_BG
                    except Exception:
                        pass

                    health_raw = (h.get("health") or "—").lower()
                    if "active" in health_raw or health_raw == "active":
                        health_vis = "🟢 active"
                    elif "banned" in health_raw or "inactive" in health_raw:
                        health_vis = "🔴 " + (h.get("health") or "banned")
                    elif "flood" in health_raw or "pause" in health_raw:
                        health_vis = "🟡 " + (h.get("health") or "limited")
                    elif "reauth" in health_raw or "network" in health_raw:
                        health_vis = "🟠 " + (h.get("health") or "issue")
                    else:
                        health_vis = h.get("health") or "—"

                    why_display = why or "—"
                    if why_display and why_display != "—":
                        low = why_display.lower()
                        if any(x in low for x in ("userbannedinchan", "chat_banned", "юзер банед", "banned in this")):
                            why_display = "—"

                    rows.append((
                        h.get("phone") or "",
                        (h.get("custom_name") or "").strip() or "—",
                        (h.get("proxy") or "—") if (h.get("proxy") or "").strip() else "—",
                        "Да" if h.get("is_active") else "Нет",
                        busy_label,
                        health_vis,
                        why_display,
                        _fmt_time(h.get("last_check_ok_at", "")),
                        _fmt_time(h.get("last_send_at", "")),
                        int(h.get("sent_today", 0) or 0),
                        int(h.get("actions_today", 0) or 0),
                        int(h.get("error_today", 0) or 0),
                    ))
                    highlights.append(row_highlight)
                except Exception:
                    # Один проблемный аккаунт (например новый "прогрев") не должен ломать весь refresh
                    try:
                        rows.append((h.get("phone") or "???", "—", "—", "—", "—", "—", "—", "—", "—", 0, 0, 0))
                        highlights.append(None)
                    except Exception:
                        pass

        except Exception as e:
            # Даже если вся выборка из БД упала — показываем что смогли
            try:
                self.log.append(f"[!] Ошибка при сборе данных аккаунтов: {e}")
            except Exception:
                pass

        # В любом случае пытаемся обновить таблицу (даже если rows пустые или частичные)
        try:
            self.table.set_data(rows, row_highlights=highlights)
        except Exception as e:
            try:
                self.log.append(f"[!] Не удалось обновить таблицу: {e}")
            except Exception:
                pass

    def _pause_account(self):
        return self._safe_action(self._do_pause_account)

    def _do_pause_account(self):
        _log_action("accounts", "_pause_account")
        row = self.table.get_selected_row()
        if not row:
            self.log.append("[!] Выберите аккаунт в таблице")
            return
        phone = row[0]
        dlg = ctk.CTkInputDialog(
            text="Пауза аккаунта (в минутах).\n0 = снять паузу.\nПример: 30\n\nВведите число:",
            title=f"Пауза {phone}",
        )
        minutes_str = (dlg.get_input() or "").strip()
        if not minutes_str:
            return
        try:
            minutes = int(minutes_str)
        except Exception:
            self.log.append("[!] Нужно число минут")
            return

        reason_dlg = ctk.CTkInputDialog(
            text="Причина паузы (опционально):",
            title="Причина",
        )
        reason = (reason_dlg.get_input() or "").strip()

        db = Database(self.app.config.db_path)
        try:
            if minutes <= 0:
                db.clear_account_pause(phone)
                self.log.append(f"[+] Пауза снята: {phone}")
            else:
                from datetime import datetime, timedelta
                until = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
                db.set_account_pause(phone, until, reason or f"manual:{minutes}m")
                self.log.append(f"[+] Пауза {phone} до {until[:16].replace('T',' ')}")
        finally:
            db.close()
        self.refresh()

    def _set_account_alias(self):
        return self._safe_action(self._do_set_account_alias)

    def _do_set_account_alias(self):
        _log_action("accounts", "_set_account_alias")
        row = self.table.get_selected_row()
        if not row:
            self.log.append("[!] Выберите аккаунт в таблице")
            return
        phone = row[0]
        current = row[1] if len(row) > 1 else ""
        if current == "—":
            current = ""
        dlg = ctk.CTkInputDialog(
            text=f"Метка для аккаунта (показывается вместе с номером).\n"
                 f"Например: ГлавныйПромо, Жена, Тестовый, OnlyFansBoard1\n\n"
                 f"Текущая: {current or '—'}\nНомер: {phone}\n\n"
                 f"Оставь пустым чтобы убрать метку:",
            title=f"Метка для {phone}",
        )
        new_name = (dlg.get_input() or "").strip()
        db = Database(self.app.config.db_path)
        try:
            db.set_account_custom_name(phone, new_name)
            self.log.append(f"[+] Метка для {phone} → «{new_name or '—'}»")
        finally:
            db.close()
        # Обновляем таблицу + все меню выбора аккаунтов по всей программе
        self.refresh()
        try:
            if hasattr(self.app, "_refresh_accounts"):
                self.app._refresh_accounts()
            if hasattr(self.app, "_refresh_account_maps"):
                self.app._refresh_account_maps()
            # Для cycle специфического лейбла
            if hasattr(self.app, "_cycle_refresh_campaign_accounts_ui"):
                self.app._cycle_refresh_campaign_accounts_ui()
        except Exception:
            pass

    def _set_proxy(self):
        _log_action("accounts", "_set_proxy")
        row = self.table.get_selected_row()
        if not row:
            self.log.append("[!] Выберите аккаунт в таблице")
            return

        phone = row[0]
        current_proxy = row[2] if len(row) > 2 else ""

        # Предупреждение если прокси уже задан (смена IP = impossible travel alert)
        if current_proxy and current_proxy != "—":
            confirm_dialog = ctk.CTkInputDialog(
                text=f"⚠ ОПАСНО: у {phone} уже стоит прокси.\n"
                     f"Смена IP может вызвать security alert от Telegram\n"
                     f"(impossible travel) и потребовать SMS-подтверждение.\n\n"
                     f"Введите 'YES' (заглавными) чтобы продолжить:",
                title="Подтверждение смены прокси")
            confirm = confirm_dialog.get_input()
            if confirm != "YES":
                self.log.append(f"[~] Смена прокси для {phone} отменена")
                return

        dialog = ctk.CTkInputDialog(
            text=f"Прокси для {phone}\n(socks5://user:pass@host:port)\nОставьте пустым чтобы убрать:",
            title="Установить прокси")
        proxy = dialog.get_input()

        if proxy is None:
            return

        db = Database(self.app.config.db_path)
        db.conn.execute("UPDATE accounts SET proxy = ? WHERE phone = ?",
                        (proxy.strip() or None, phone))
        self.log.append(f"[+] Прокси для {phone} обновлена")
        db.conn.commit()
        db.close()
        self.refresh()

    def _open_bulk(self):
        _log_action("accounts", "_open_bulk")
        dialog = BulkAccountsDialog(self, self.app)
        self.wait_window(dialog)
        self.refresh()


    def _import_sessions(self):
        _log_action("accounts", "_import_sessions")
        sessions_dir = self.app.config.sessions_dir
        if not os.path.isdir(sessions_dir):
            self.log.append(f"[!] Папка сессий не найдена: {sessions_dir}")
            return

        db = Database(self.app.config.db_path)
        try:
            existing = {acc.phone for acc in db.get_all_accounts()}
        finally:
            db.close()

        candidates = []
        skipped = 0
        for filename in os.listdir(sessions_dir):
            cand = _session_candidate_from_filename(sessions_dir, filename)
            if not cand:
                continue
            if cand["declared_phone"] in existing:
                skipped += 1
                continue
            candidates.append(cand)

        if not candidates:
            self.log.append(f"[!] Нет новых .session для проверки (пропущено существующих: {skipped})")
            return

        self.btn_import.configure(state="disabled", text="Проверка...")
        self.log.append(f"[~] Проверяю session-файлы: {len(candidates)}")

        log_queue = self.app.log_queue

        def worker():
            def emit(m: str):
                log_queue.put(("accounts_log", m))

            try:
                res = import_session_files_to_db(
                    candidates=candidates,
                    proxy_for_index="",
                    sessions_dir=sessions_dir,
                    db_path=self.app.config.db_path,
                    log_cb=emit,
                )
                emit(f"[=] {res.get('text', '')}")
            except Exception as e:
                log_exception("accounts", e, context="accounts session import")
                emit(f"[-] Ошибка импорта session: {type(e).__name__}: {e}")
            finally:
                log_queue.put(("accounts_sessions_done", None))

        threading.Thread(target=worker, daemon=True).start()
        return

    def _import_tdata(self):
        _log_action("accounts", "_import_tdata")
        dialog = ImportTDataDialog(self, self.app.config)
        self.wait_window(dialog)
        if not dialog.result:
            return

        tdata_path = dialog.result["path"]
        proxy      = dialog.result["proxy"]
        sessions_dir = self.app.config.sessions_dir

        self.btn_import_tdata.configure(state="disabled", text="Конвертация...")
        self.log.append(f"[~] Конвертирую TData: {tdata_path}")

        log_queue = self.app.log_queue

        def tdata_thread():
            _thread_local.log_handler = lambda m: log_queue.put(("accounts_log", m))
            _thread_local.log_tag = "accounts"
            try:
                res = import_tdata_dir_to_db(
                    tdata_path=tdata_path,
                    proxy=proxy,
                    sessions_dir=sessions_dir,
                    db_path=self.app.config.db_path,
                    log_cb=print,
                )
                for item in res.get("results", []) or []:
                    phone = item.get("phone") or item.get("ref") or "?"
                    action = item.get("action", "")
                    reason = item.get("reason", "")
                    if action == "added":
                        print(f"[+] {phone} добавлен")
                    elif action == "updated":
                        print(f"[~] {phone} обновлён")
                    else:
                        print(f"[-] {phone} не добавлен: {reason}")
                log_queue.put(("accounts_import_summary", (
                    res.get("kind", "fail"),
                    res.get("text", ""),
                    res.get("added", 0),
                    res.get("expected", 0),
                )))
            except Exception as e:
                log_exception("accounts", e, context="tdata_thread top-level")
                print(f"[-] Ошибка импорта TData: {type(e).__name__}")
                print(f"[-] Подсказка: {_hint_for(e)}")
            finally:
                _thread_local.log_handler = None
                log_queue.put(("accounts_tdata_done", None))

        threading.Thread(target=tdata_thread, daemon=True).start()
        return

        self.btn_import_tdata.configure(state="disabled", text="Конвертация...")
        self.log.append(f"[~] Конвертирую TData: {tdata_path}")

        # Счётчик аккаунтов в БД ДО импорта — для определения успеха в finally
        try:
            _db = Database(self.app.config.db_path)
            accounts_before = len(_db.get_all_accounts())
            _db.close()
        except Exception:
            accounts_before = 0

        log_queue = self.app.log_queue

        def tdata_thread():
            _thread_local.log_handler = lambda m: log_queue.put(("accounts_log", m))
            _thread_local.log_tag = "accounts"
            log_to_file("accounts",
                        f"[~] === START tdata_thread, path={tdata_path!r}, "
                        f"proxy={'<set>' if proxy else '<none>'} ===")
            # Состояние которое финал-блок использует для итогового сообщения.
            # expected заполнится после успешного TDesktop() = сколько аккаунтов
            # opentele увидел внутри TData. Если упали раньше — останется 0.
            tdata_state = {"expected": 0, "fail_reason": None}
            try:
                from opentele.td import TDesktop
                from opentele.api import API, UseCurrentSession
                from sender import TelegramSender
                log_to_file("accounts", "[+] Импорты opentele/sender ОК")

                # === ШАГ 1/8: pre-flight проверка папки TData ===
                # opentele на Python 3.13 падает с искажённым __str__, если папка
                # не TData. Делаем свою быструю проверку — даём юзеру понятный
                # ответ ещё до тяжёлых вызовов.
                _step = "1/8 pre-flight"
                log_to_file("accounts", f"[~] Шаг {_step}: проверка папки {tdata_path}")
                print(f"[~] Шаг {_step}: проверка папки TData")

                if not os.path.isdir(tdata_path):
                    msg = f"Папка не существует или не является каталогом: {tdata_path}"
                    log_to_file("accounts", f"[!] Шаг {_step} ПРОВАЛЕН: {msg}")
                    print(f"[!] {msg}")
                    tdata_state["fail_reason"] = f"папка не существует или не каталог: {tdata_path}"
                    return

                # TData содержит файл key_datas (или key_datass в старых версиях)
                # + хотя бы одну папку с hex-именем (16 символов A-F, 0-9)
                dir_contents = os.listdir(tdata_path)
                has_key = any(n in ("key_datas", "key_datass") for n in dir_contents)
                import re as _re
                has_hex_dir = any(
                    _re.fullmatch(r"[0-9A-Fa-f]{16}", n)
                    and os.path.isdir(os.path.join(tdata_path, n))
                    for n in dir_contents
                )
                if not has_key or not has_hex_dir:
                    nested_tdata = _collect_tdata_dirs(tdata_path)
                    log_to_file("accounts",
                                f"[!] Шаг {_step} ПРОВАЛЕН: папка не похожа на TData. "
                                f"has_key={has_key}, has_hex_dir={has_hex_dir}, "
                                f"contents={dir_contents[:20]}, nested_tdata={len(nested_tdata)}")
                    print(f"[!] Папка {tdata_path} не похожа на TData от Telegram Desktop")
                    print("[!] Ожидается: файл 'key_datas' и папка с hex-именем (например D877F783D5D3EF8C)")
                    if nested_tdata:
                        print(f"[!] Внутри найдено TData папок: {len(nested_tdata)}")
                        if len(nested_tdata) == 1:
                            print(f"[!] Для одиночного импорта выберите эту папку: {nested_tdata[0]}")
                        else:
                            print("[!] Для такой папки используйте кнопку «Массово» -> импорт TData пачкой")
                    else:
                        print(f"[!] Возможно вы указали родительскую папку — попробуйте указать вложенную tdata/")
                    print(f"[!] Содержимое папки: {dir_contents[:10]}")
                    if nested_tdata:
                        tdata_state["fail_reason"] = f"выбрана папка-контейнер ({tdata_path}), внутри найдено TData: {len(nested_tdata)} — выберите конкретную вложенную"
                    else:
                        tdata_state["fail_reason"] = f"папка не TData: нет key_datas или hex-папки (путь: {tdata_path})"
                    return

                # === ШАГ 2/8: проверка прав на запись в data/sessions/ ===
                _step = "2/8 sessions_dir writable"
                log_to_file("accounts", f"[~] Шаг {_step}: проверяю запись в {sessions_dir}")
                try:
                    os.makedirs(sessions_dir, exist_ok=True)
                    _probe = os.path.join(sessions_dir, ".write_test")
                    with open(_probe, "w") as _f:
                        _f.write("ok")
                    os.remove(_probe)
                    log_to_file("accounts", f"[+] Шаг {_step}: запись доступна")
                except OSError as e:
                    log_exception("accounts", e,
                                  context=f"sessions_dir not writable: {sessions_dir}")
                    print(f"[!] Шаг {_step} ПРОВАЛЕН: нет прав на запись в {sessions_dir}")
                    print(f"[!] Подсказка: {_hint_for(e)}")
                    print(f"[!] Тип ошибки: {type(e).__name__}")
                    tdata_state["fail_reason"] = f"нет прав на запись в sessions ({sessions_dir}): {type(e).__name__}"
                    return

                # === ШАГ 3/8: чтение TData через opentele ===
                _step = "3/8 TDesktop(tdata_path)"
                log_to_file("accounts", f"[~] Шаг {_step}: opentele.TDesktop читает {tdata_path}")
                print(f"[~] Шаг {_step}: чтение TData с диска")
                try:
                    tdesk = TDesktop(tdata_path)
                except BaseException as e:
                    if isinstance(e, (KeyboardInterrupt, SystemExit)):
                        raise
                    # На Python 3.13 + opentele 1.15.1 str(e) может быть сломан
                    # (выводит __firstlineno__ вместо понятного текста).
                    # Логируем ПОЛНЫЙ traceback в файл для диагностики.
                    log_exception("accounts", e,
                                  context=f"TDesktop({tdata_path}) init failed")
                    err_name = type(e).__name__
                    print(f"[-] Шаг {_step} ПРОВАЛЕН: {err_name}")
                    print(f"[-] Подсказка: {_hint_for(e)}")
                    print("[-] Полный traceback записан в лог-файл (data/logs/)")
                    tdata_state["fail_reason"] = f"ошибка чтения TData ({err_name}) по пути {tdata_path}"
                    return

                if not tdesk.isLoaded() or tdesk.accountsCount == 0:
                    log_to_file("accounts",
                                f"[!] Шаг {_step}: TData загружена, но "
                                f"isLoaded={tdesk.isLoaded()}, accountsCount={tdesk.accountsCount}")
                    print("[!] TData не содержит аккаунтов или повреждена")
                    tdata_state["fail_reason"] = f"TData прочитана, но accountsCount=0 или не isLoaded (путь: {tdata_path})"
                    return

                log_to_file("accounts",
                            f"[+] Шаг {_step}: загружено {tdesk.accountsCount} аккаунт(а/ов)")
                print(f"[+] Найдено аккаунтов в TData: {tdesk.accountsCount}")

                # Запоминаем сколько ожидаем импортнуть — для итогового сообщения
                tdata_state["expected"] = tdesk.accountsCount

                loop = asyncio.new_event_loop()

                async def do_convert():
                    from config import DESKTOP_API_ID, DESKTOP_API_HASH
                    # Читаем настройки таймаутов один раз перед циклом
                    from ads_database import AdsDB
                    _adsdb = AdsDB(self.app.config.db_path)
                    try:
                        _settings = _adsdb.load_scheduler_settings()
                    finally:
                        _adsdb.close()
                    _connect_timeout = _settings.tdata_connect_timeout_seconds
                    _getme_timeout = _settings.tdata_get_me_timeout_seconds
                    _flood_max = _settings.tdata_flood_max_wait_seconds
                    _flood_jit_min = _settings.tdata_flood_jitter_min_seconds
                    _flood_jit_max = _settings.tdata_flood_jitter_max_seconds

                    log_to_file("accounts",
                                f"[~] Таймауты: connect={_connect_timeout}с, "
                                f"get_me={_getme_timeout}с, flood_max={_flood_max}с, "
                                f"jitter={_flood_jit_min}-{_flood_jit_max}с")

                    for i, td_acc in enumerate(tdesk.accounts):
                        user_id = td_acc.UserId
                        session_path = os.path.join(
                            sessions_dir, f"session_tdata_{user_id}")

                        log_to_file("accounts",
                                    f"[~] === Аккаунт {i+1}/{tdesk.accountsCount}, "
                                    f"userId={user_id} ===")
                        print(f"\n[~] Аккаунт {i+1}/{tdesk.accountsCount}, userId={user_id}")

                        # Прокси для конвертации
                        proxy_tuple = None
                        if proxy:
                            _s = TelegramSender.__new__(TelegramSender)
                            try:
                                proxy_tuple = _s._parse_proxy(proxy)
                                log_to_file("accounts",
                                            f"[~] Прокси для userId={user_id} распарсен")
                            except Exception as e:
                                log_exception("accounts", e,
                                              context=f"Parse proxy for userId={user_id}")
                                print(f"[-] Не могу разобрать прокси: {type(e).__name__}: {e}")
                                print(f"[-] Подсказка: проверь формат прокси (host:port:user:pass)")
                                continue

                        # Используем Desktop api_id — auth_key в TData выписан
                        # под него, любой другой api_id = fingerprint mismatch.
                        td_api = API.TelegramDesktop(api_id=DESKTOP_API_ID, api_hash=DESKTOP_API_HASH)
                        client = None
                        phone = None
                        _step = "init"
                        try:
                            # === ШАГ 4/8: ToTelethon (создание Telethon-клиента) ===
                            _step = f"4/8 ToTelethon (userId={user_id})"
                            log_to_file("accounts",
                                        f"[~] Шаг {_step}: opentele конвертирует auth_key")
                            print(f"[~] Шаг {_step}: создание Telethon-клиента")
                            client = await tdesk.ToTelethon(
                                session=session_path,
                                flag=UseCurrentSession,
                                api=td_api,
                                **({"proxy": proxy_tuple} if proxy_tuple else {}),
                            )
                            log_to_file("accounts", f"[+] Шаг {_step} ОК")

                            # === ШАГ 5/8: client.connect() с настраиваемым таймаутом + FloodWait retry ===
                            _step = f"5/8 client.connect (userId={user_id})"
                            log_to_file("accounts",
                                        f"[~] Шаг {_step}: подключение к Telegram MTProto, таймаут {_connect_timeout}с")
                            print(f"[~] Шаг {_step}: подключение к Telegram (таймаут {_connect_timeout}с)")
                            await _try_with_flood_retry(
                                lambda: asyncio.wait_for(client.connect(), timeout=_connect_timeout),
                                max_wait_sec=_flood_max,
                                jitter_min=_flood_jit_min,
                                jitter_max=_flood_jit_max,
                                log_cb=print,
                            )
                            log_to_file("accounts", f"[+] Шаг {_step} ОК")

                            # === ШАГ 6/8: проверка авторизации + client.get_me() ===
                            _step = f"6/8 client.is_user_authorized (userId={user_id})"
                            log_to_file("accounts",
                                        f"[~] Шаг {_step}: проверяю авторизацию, таймаут {_getme_timeout}с")
                            print(f"[~] Шаг {_step}: проверка авторизации (таймаут {_getme_timeout}с)")
                            authorized = await _try_with_flood_retry(
                                lambda: asyncio.wait_for(client.is_user_authorized(), timeout=_getme_timeout),
                                max_wait_sec=_flood_max,
                                jitter_min=_flood_jit_min,
                                jitter_max=_flood_jit_max,
                                log_cb=print,
                            )
                            if not authorized:
                                log_to_file("accounts",
                                            f"[-] Шаг {_step}: userId={user_id} не авторизован")
                                print(f"[-] userId={user_id}: TData не авторизована, аккаунт не добавлен")
                                phone = None
                            else:
                                _step = f"6/8 client.get_me (userId={user_id})"
                                log_to_file("accounts",
                                            f"[~] Шаг {_step}: запрашиваю профиль, таймаут {_getme_timeout}с")
                                print(f"[~] Шаг {_step}: получение профиля (таймаут {_getme_timeout}с)")
                                me = await _try_with_flood_retry(
                                    lambda: asyncio.wait_for(client.get_me(), timeout=_getme_timeout),
                                    max_wait_sec=_flood_max,
                                    jitter_min=_flood_jit_min,
                                    jitter_max=_flood_jit_max,
                                    log_cb=print,
                                )
                                if not me or not getattr(me, "phone", None):
                                    log_to_file("accounts",
                                                f"[-] Шаг {_step}: userId={user_id} profile has no phone")
                                    print(f"[-] userId={user_id}: Telegram не вернул номер телефона, аккаунт не добавлен")
                                    phone = None
                                else:
                                    phone = f"+{me.phone}"
                                    log_to_file("accounts",
                                                f"[+] Шаг {_step}: phone={phone}, name={me.first_name!r}")
                                    print(f"[+] Конвертирован: {phone} ({me.first_name})")

                            # Шаг 7/8 (сессионная гигиена) — УБРАН.
                            # Управление чужими сессиями вынесено в отдельную
                            # кнопку "Устройства" на вкладке Аккаунты,
                            # с возможностью запланировать удаление через N мин/часов.

                        except asyncio.TimeoutError as e:
                            log_exception("accounts", e,
                                          context=f"Timeout on '{_step}' for userId={user_id}")
                            print(f"[-] Шаг {_step} ПРЕВЫШЕН ТАЙМАУТ ({_connect_timeout}/{_getme_timeout}с)")
                            print(f"[-] Подсказка: {_hint_for(e)}")
                            phone = None
                        except Exception as e:
                            # Особый случай: FloodWait после исчерпания retry
                            if type(e).__name__ == "FloodWaitError":
                                log_exception("accounts", e,
                                              context=f"FloodWait on '{_step}' for userId={user_id}")
                                seconds = getattr(e, "seconds", 0)
                                print(f"[-] Шаг {_step}: FloodWait {seconds}с — Telegram просит подождать "
                                      f"(больше нашего лимита {_flood_max}с)")
                                print(f"[-] Подсказка: повтори импорт через {seconds}с или увеличь "
                                      f"tdata_flood_max_wait_seconds в настройках")
                            else:
                                log_exception("accounts", e,
                                              context=f"Convert userId={user_id} on '{_step}'")
                                err_name = type(e).__name__
                                print(f"[-] Шаг {_step} ПРОВАЛЕН: {err_name}")
                                print(f"[-] Подсказка: {_hint_for(e)}")
                                print("[-] Полный traceback в лог-файле (data/logs/)")
                            phone = None
                        finally:
                            # Клиент закрываем ВСЕГДА перед переходом к rename
                            # session-файла. SQLite может держать lock на .session,
                            # из-за чего os.rename упадёт с PermissionError на Windows.
                            if client is not None:
                                try:
                                    await client.disconnect()
                                    log_to_file("accounts",
                                                f"[~] client.disconnect() для userId={user_id}")
                                except Exception as e:
                                    print(f"[!] Ошибка disconnect для userId={user_id}: {e}")

                        if phone is None:
                            for suffix in (".session", ".session-journal"):
                                try:
                                    p = session_path + suffix
                                    if os.path.exists(p):
                                        os.remove(p)
                                except Exception:
                                    pass
                            log_to_file("accounts",
                                        f"[-] userId={user_id} пропущен (phone=None)")
                            continue

                        # Пути для rename session-файла (используются в Шаге 8)
                        standard_path = os.path.join(sessions_dir, f"session_{phone}")
                        old_session = session_path + ".session"
                        new_session = standard_path + ".session"
                        old_journal = session_path + ".session-journal"

                        # === ШАГ 8/8: rename session-файла + запись в БД ===
                        _step = f"8/8 rename+DB ({phone})"
                        log_to_file("accounts", f"[~] Шаг {_step}: перенос session-файла")

                        rename_ok = False
                        try:
                            if not os.path.exists(old_session):
                                log_to_file("accounts",
                                            f"[-] Шаг {_step}: session-файл не создался: {old_session}")
                                print(f"[-] Ожидаемый session-файл не создался: {old_session}")
                                print(f"[-] Подсказка: проверь права на запись в data/sessions/ и антивирус")
                                print(f"[-] Аккаунт {phone} пропущен")
                                continue

                            if (os.path.exists(new_session)
                                    and os.path.abspath(new_session) != os.path.abspath(old_session)):
                                try:
                                    os.remove(new_session)
                                    print(f"[~] Удалён старый session: {new_session}")
                                except Exception as e:
                                    log_exception("accounts", e,
                                                  context=f"Remove existing session {new_session}")
                                    print(f"[-] Не могу удалить старый session {new_session}: {e}")
                                    print(f"[-] Аккаунт {phone} пропущен")
                                    continue

                            os.rename(old_session, new_session)

                            if os.path.exists(old_journal):
                                try:
                                    os.remove(old_journal)
                                except Exception:
                                    pass

                            if os.path.exists(new_session):
                                rename_ok = True
                                log_to_file("accounts",
                                            f"[+] Шаг {_step}: session переименован → {new_session}")
                            else:
                                print(f"[-] Rename для {phone} не оставил файл по ожидаемому пути: {new_session}")
                        except Exception as e:
                            log_exception("accounts", e,
                                          context=f"Rename session for {phone}")
                            print(f"[-] Не удалось переименовать session для {phone}: {type(e).__name__}")
                            print(f"[-] Подсказка: {_hint_for(e)}")
                            print("[-] Полный traceback в лог-файле (data/logs/)")

                        if not rename_ok:
                            print(f"[-] Аккаунт {phone} пропущен — session-файл не на своём месте")
                            continue

                        # Запись в БД
                        try:
                            log_to_file("accounts",
                                        f"[~] Шаг {_step}: запись Account в БД")
                            db = Database(self.app.config.db_path)
                            try:
                                all_accs = db.get_all_accounts()
                                existing_acc = next(
                                    (a for a in all_accs if a.phone == phone), None)
                                is_update = existing_acc is not None
                                from models import Account

                                # Device fingerprint под который был создан
                                # auth_key в TData. "Desktop" + Windows — то,
                                # что возвращает API.TelegramDesktop().
                                tdata_device = {
                                    "api_id": DESKTOP_API_ID,
                                    "api_hash": DESKTOP_API_HASH,
                                    "device_model": "Desktop",
                                    "system_version": "Windows 10",
                                    "app_version": "5.6.3 x64",
                                    "lang_code": "ru",
                                }

                                if existing_acc is not None:
                                    new_acc = Account(
                                        phone=phone,
                                        session_name=standard_path,
                                        proxy=proxy,
                                        is_active=existing_acc.is_active,
                                        sent_today=existing_acc.sent_today,
                                        last_reset_date=existing_acc.last_reset_date,
                                        **tdata_device,
                                    )
                                else:
                                    new_acc = Account(
                                        phone=phone,
                                        session_name=standard_path,
                                        proxy=proxy,
                                        **tdata_device,
                                    )
                                db.add_account(new_acc)
                                if is_update:
                                    log_to_file("accounts",
                                                f"[+] Шаг {_step}: обновлён существующий {phone}")
                                    print(f"[~] Обновлён существующий аккаунт: {phone}")
                                else:
                                    log_to_file("accounts",
                                                f"[+] Шаг {_step}: добавлен новый {phone}")
                                    print(f"[+] Добавлен в БД: {phone}")
                            finally:
                                db.close()

                            log_to_file("accounts",
                                        f"[+] === Импорт {phone} завершён успешно ===")
                            print(f"[+] === Импорт {phone} завершён успешно ===")

                        except Exception as e:
                            log_exception("accounts", e,
                                          context=f"DB write for {phone}")
                            print(f"[-] Ошибка записи в БД для {phone}: {type(e).__name__}")
                            print(f"[-] Подсказка: {_hint_for(e)}")
                            print("[-] Полный traceback в лог-файле (data/logs/)")

                _run_loop(loop, do_convert())

            except ImportError:
                log_to_file("accounts", "[!] opentele не установлена")
                print("[!] Не установлена библиотека opentele")
                print("[!] Установи: pip install opentele")
            except Exception as e:
                log_exception("accounts", e, context="tdata_thread top-level")
                err_name = type(e).__name__
                print(f"[-] Ошибка импорта TData: {err_name}")
                print(f"[-] Подсказка: {_hint_for(e)}")
                print("[-] Полный traceback записан в лог-файл (data/logs/)")
            finally:
                # Подсчёт результата импорта: сколько аккаунтов реально добавилось.
                # accounts_before — захвачено замыканием извне потока.
                # tdata_state["expected"] — заполнено после успешного TDesktop().
                try:
                    _db = Database(self.app.config.db_path)
                    accounts_after = len(_db.get_all_accounts())
                    _db.close()
                except Exception:
                    accounts_after = accounts_before
                added = accounts_after - accounts_before
                expected = tdata_state.get("expected", 0)

                if added <= 0 and expected == 0:
                    summary_kind = "fail_early"
                    reason = tdata_state.get("fail_reason") or ""
                    if reason:
                        # Если причина уже есть (в т.ч. как в res["text"] из import_tdata_dir_to_db) — показываем её с деталями
                        summary_text = f"ИМПОРТ ПРЕРВАН до чтения TData: {reason}. Подробности — в data/logs/teleton_*.log."
                    else:
                        summary_text = (f"ИМПОРТ ПРЕРВАН до чтения TData (путь: {tdata_path}). "
                                        "Подробности — в data/logs/teleton_*.log.")
                    log_to_file("accounts", f"[!] fail_early: path={tdata_path}, reason={reason or 'не указана'}")
                elif added <= 0 and expected > 0:
                    summary_kind = "fail"
                    summary_text = (f"ИМПОРТ НЕ УДАЛСЯ: TData содержит "
                                    f"{expected} аккаунт(ов), добавлено 0. "
                                    f"Подробности — в data/logs/teleton_*.log.")
                elif added < expected:
                    summary_kind = "partial"
                    summary_text = (f"ИМПОРТ ЧАСТИЧНЫЙ: добавлено {added} "
                                    f"из {expected}. Подробности — "
                                    f"в data/logs/teleton_*.log.")
                else:
                    summary_kind = "success"
                    summary_text = f"ИМПОРТ ЗАВЕРШЁН: добавлено {added} аккаунт(ов)."

                log_to_file("accounts",
                            f"[=] Итог: before={accounts_before}, "
                            f"after={accounts_after}, added={added}, "
                            f"expected={expected}, kind={summary_kind}")
                print(f"\n[=] {summary_text}")

                _thread_local.log_handler = None
                # Отправляем summary в очередь — обработчик покажет в GUI и messagebox
                log_queue.put(("accounts_import_summary",
                               (summary_kind, summary_text, added, expected)))
                log_queue.put(("accounts_tdata_done", None))

        threading.Thread(target=tdata_thread, daemon=True).start()

    def _toggle_account(self):
        return self._safe_action(self._do_toggle_account)

    def _do_toggle_account(self):
        _log_action("accounts", "_toggle_account")
        row = self.table.get_selected_row()
        if not row:
            self.log.append("[!] Выберите аккаунт в таблице")
            return

        phone = row[0]
        # Вкл колонка — 4-я (индекс 3), после Телефон(0), Метка(1), Прокси(2)
        is_active = str(row[3]).strip().lower() in ("да", "yes", "1", "true") if len(row) > 3 else False

        db = Database(self.app.config.db_path)
        if is_active:
            db.deactivate_account(phone)
            self.log.append(f"[-] Аккаунт {phone} деактивирован")
        else:
            db.activate_account(phone)
            self.log.append(f"[+] Аккаунт {phone} активирован")
        db.close()
        self.refresh()

    def _delete_account(self):
        return self._safe_action(self._do_delete_account)

    def _do_delete_account(self):
        _log_action("accounts", "_delete_account")
        checked = self.table.get_checked_rows()
        phones = [r[0] for r in checked] if checked else []
        if not phones:
            row = self.table.get_selected_row()
            if not row:
                self.log.append("[!] Выберите аккаунт в таблице")
                return
            phones = [row[0]]

        from tkinter import messagebox
        if len(phones) == 1:
            ok = messagebox.askyesno("Удалить аккаунт", f"Удалить аккаунт {phones[0]} из БД?")
        else:
            ok = messagebox.askyesno("Удалить аккаунты",
                                     f"Удалить {len(phones)} аккаунтов из БД?")
        if not ok:
            return

        db = Database(self.app.config.db_path)
        for p in phones:
            db.delete_account(p)
        db.close()
        self.refresh()
        if len(phones) == 1:
            self.log.append(f"[x] Аккаунт {phones[0]} удалён")
        else:
            self.log.append(f"[x] Удалено аккаунтов: {len(phones)}")
        self.refresh()

    def _open_devices(self):
        """Открыть диалог управления устройствами (сессиями) выбранного аккаунта.
        Загрузка списка идёт в фоновом потоке — основной UI не блокируется.
        Когда список загружен — main thread открывает DevicesDialog."""
        _log_action("accounts", "_open_devices")
        row = self.table.get_selected_row()
        if not row:
            self.log.append("[!] Выберите аккаунт в таблице")
            return
        phone = row[0]

        db = Database(self.app.config.db_path)
        try:
            account = next((a for a in db.get_all_accounts()
                            if a.phone == phone), None)
        finally:
            db.close()
        if account is None:
            self.log.append(f"[!] Аккаунт {phone} не найден в БД")
            return

        self.btn_devices.configure(state="disabled", text="Загрузка...")
        self.log.append(f"[~] Загружаю сессии для {phone}...")

        log_queue = self.app.log_queue

        def devices_thread():
            _thread_local.log_handler = lambda m: log_queue.put(("accounts_log", m))
            _thread_local.log_tag = "accounts"
            try:
                from sender import TelegramSender
                from account_manager import list_sessions
                cfg = self.app.config
                _db = Database(cfg.db_path)
                sender = TelegramSender(account, cfg, _db)

                async def do_list():
                    if not await sender.connect():
                        return None
                    try:
                        return await list_sessions(sender.client)
                    finally:
                        await sender.disconnect()

                loop = asyncio.new_event_loop()
                try:
                    auths = loop.run_until_complete(do_list())
                finally:
                    loop.close()
                _db.close()

                if auths is None:
                    print(f"[-] Не удалось подключиться к {phone}")
                    log_queue.put(("accounts_devices_loaded", None))
                    return

                # Сериализуем auth-объекты в простые dict — Authorization
                # из telethon не pickle-able через очередь, удобнее dict.
                serialized = []
                for a in auths:
                    serialized.append({
                        "hash": a.hash,
                        "current": bool(a.current),
                        "device_model": a.device_model or "",
                        "platform": a.platform or "",
                        "system_version": a.system_version or "",
                        "app_name": a.app_name or "",
                        "app_version": a.app_version or "",
                        "ip": a.ip or "",
                        "country": a.country or "",
                        "region": a.region or "",
                        "date_created": (a.date_created.isoformat()
                                          if a.date_created else ""),
                        "date_active": (a.date_active.isoformat()
                                          if a.date_active else ""),
                    })
                log_queue.put(("accounts_devices_loaded", (phone, serialized)))
            except Exception as e:
                log_exception("accounts", e, context=f"Open devices for {phone}")
                print(f"[-] Ошибка загрузки сессий: {type(e).__name__}: {e}")
                log_queue.put(("accounts_devices_loaded", None))
            finally:
                _thread_local.log_handler = None

        threading.Thread(target=devices_thread, daemon=True).start()

    def on_queue_message(self, tag, msg):
        if tag == "accounts_log":
            self.log.append(msg)
        elif tag == "accounts_sessions_done":
            try:
                self.btn_import.configure(state="normal", text="Импорт сессий")
            except Exception:
                pass
            self.refresh()
        elif tag == "accounts_tdata_done":
            self.btn_import_tdata.configure(state="normal", text="Импорт TData")
            self.refresh()
        elif tag == "accounts_import_summary":
            # msg = (kind, text, added, expected)
            kind, text, _added, _expected = msg
            self.log.append(f"[=] {text}")
            try:
                from tkinter import messagebox
                if kind == "fail" or kind == "fail_early":
                    messagebox.showerror("Импорт TData", text)
                elif kind == "partial":
                    messagebox.showwarning("Импорт TData", text)
                # success — без модального окна, только лог
            except Exception:
                pass
        elif tag == "accounts_devices_loaded":
            # msg = None при ошибке, иначе (phone, [dict, ...])
            self.btn_devices.configure(state="normal", text="Устройства")
            if msg is None:
                self.log.append("[!] Не удалось загрузить сессии")
                return
            phone, sessions = msg
            self.log.append(f"[+] Загружено сессий: {len(sessions)} для {phone}")
            DevicesDialog(self, self.app, phone, sessions)


class TasksFrame(ctk.CTkFrame):
    """Раздел: Задачи"""

    def __init__(self, master, app, embed: bool = False):
        super().__init__(master, fg_color="transparent")
        self.app = app

        if not embed:
            ctk.CTkLabel(self, text="Задачи", font=ctk.CTkFont(size=20, weight="bold")).pack(
                padx=20, pady=(15, 5), anchor="w")

        # Toolbar
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(padx=20 if not embed else 10, pady=5, fill="x")

        ctk.CTkButton(toolbar, text="Добавить", width=120, command=self._add_task).pack(side="left", padx=(0, 5))
        ctk.CTkButton(toolbar, text="Выполнена", width=120, command=self._mark_completed).pack(side="left", padx=5)
        ctk.CTkButton(toolbar, text="Удалить", width=100, fg_color="firebrick",
                       hover_color="darkred", command=self._delete_task).pack(side="left", padx=5)

        # Таблица
        self.table = ScrollableTable(
            self,
            columns=["ID", "Группа", "Тип", "Источник", "Статус", "Попытки", "Повтор", "Ошибка"],
            column_weights=[0, 3, 1, 1, 1, 0, 1, 2],
            column_minsizes=[45, 180, 90, 120, 125, 70, 95, 180],
        )
        self.table.pack(padx=20 if not embed else 10, pady=10, fill="both", expand=True)
        self.table.set_on_select(self._on_select)

        # Превью сообщения
        ctk.CTkLabel(self, text="Текст сообщения:", font=ctk.CTkFont(weight="bold")).pack(
            padx=20 if not embed else 10, anchor="w")
        self.preview = ctk.CTkTextbox(self, height=80, state="disabled")
        self.preview.pack(padx=20 if not embed else 10, pady=(0, 10), fill="x")

        self._tasks = []
        self.refresh()

    def refresh(self):
        db = Database(self.app.config.db_path)
        self._tasks = db.get_all_tasks()
        db.close()

        rows = []
        highlights = []
        for t in self._tasks:
            status = self._format_status(t)
            retry_after = getattr(t, "retry_after", "") or ""
            retry_short = retry_after[11:16] if len(retry_after) >= 16 else "—"
            last_error = self._shorten(getattr(t, "last_error", "") or "", 48)
            rows.append((
                t.id,
                t.target_group,
                t.task_type,
                t.source_group or "—",
                status,
                getattr(t, "fail_count", 0) or 0,
                retry_short,
                last_error or "—",
            ))
            highlights.append(self._row_highlight(t))
        self.table.set_data(rows, highlights)

    def _format_status(self, task):
        if getattr(task, "completed", False):
            return "Выполнена"
        status = getattr(task, "status", "pending")
        if status == "waiting":
            retry_after = getattr(task, "retry_after", "") or ""
            if len(retry_after) >= 16:
                return f"Ожидает до {retry_after[11:16]}"
            return "Ожидает повтора"
        if status == "error":
            return "Ошибка"
        if status == "done":
            return "Выполнена"
        return "Готова"

    def _row_highlight(self, task):
        if getattr(task, "completed", False) or getattr(task, "status", "") == "done":
            return ("#DCFCE7", "#17351F")
        status = getattr(task, "status", "pending")
        if status == "waiting":
            return ("#FEF3C7", "#3A2F12")
        if status == "error":
            return ("#FEE2E2", "#3A1717")
        return ("#E0F2FE", "#123040")

    def _shorten(self, text: str, limit: int = 80) -> str:
        value = " ".join((text or "").split())
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 1)] + "…"

    def _on_select(self, index):
        if index < len(self._tasks):
            task = self._tasks[index]
            self.preview.configure(state="normal")
            self.preview.delete("1.0", "end")
            self.preview.insert("1.0", task.message_text)
            self.preview.configure(state="disabled")

    def _add_task(self):
        dialog = AddTaskDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            db = Database(self.app.config.db_path)
            task = Task(
                target_group=dialog.result["target_group"],
                message_text=dialog.result["message_text"],
                task_type=dialog.result["task_type"],
                source_group=dialog.result["source_group"],
                mentions_per_message=dialog.result["mentions_per_message"],
            )
            db.add_task(task)
            db.close()
            self.refresh()

    def _mark_completed(self):
        row = self.table.get_selected_row()
        if not row:
            return
        task_id = row[0]
        db = Database(self.app.config.db_path)
        db.mark_task_completed(task_id)
        db.close()
        self.refresh()

    def _delete_task(self):
        row = self.table.get_selected_row()
        if not row:
            return
        task_id = row[0]
        db = Database(self.app.config.db_path)
        db.delete_task(task_id)
        db.close()
        self.refresh()


class ListTemplatesFrame(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app

        ctk.CTkLabel(self, text="Шаблоны списков", font=ctk.CTkFont(size=20, weight="bold")).pack(
            padx=20, pady=(15, 5), anchor="w")

        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(padx=20, pady=5, fill="x")

        ctk.CTkButton(toolbar, text="Добавить", width=120, command=self._add).pack(
            side="left", padx=(0, 5))
        ctk.CTkButton(toolbar, text="Редактировать", width=140, command=self._edit).pack(
            side="left", padx=5)
        ctk.CTkButton(toolbar, text="Удалить", width=100, fg_color="firebrick",
                       hover_color="darkred", command=self._delete).pack(side="left", padx=5)
        ctk.CTkButton(toolbar, text="Импорт", width=100, command=self._import).pack(
            side="left", padx=5)
        ctk.CTkButton(toolbar, text="Экспорт", width=100, command=self._export).pack(
            side="left", padx=5)
        ctk.CTkButton(toolbar, text="↻ Обновить", width=110, command=self.refresh).pack(
            side="right")

        self.table = ScrollableTable(self, columns=["ID", "Название", "Тип", "Элементов", "Обновлено"])
        self.table.pack(padx=20, pady=10, fill="both", expand=True)
        self.table.set_on_select(self._on_select)

        ctk.CTkLabel(self, text="Содержимое:", font=ctk.CTkFont(weight="bold")).pack(
            padx=20, anchor="w")
        self.preview = ctk.CTkTextbox(self, height=120, state="disabled")
        self.preview.pack(padx=20, pady=(0, 10), fill="x")

        self.log = LogFrame(self, height=90)
        self.log.pack(padx=20, pady=(0, 10), fill="x")

        self._templates = []
        self.refresh()

    def _kind_label(self, kind: str) -> str:
        return {"mixed": "Смешанный", "groups": "Группы", "channels": "Каналы", "messages": "Тексты"}.get(kind, kind)

    def _count_items(self, content: str) -> int:
        return len([l.strip() for l in (content or "").splitlines() if l.strip()])

    def refresh(self):
        db = Database(self.app.config.db_path)
        self._templates = db.get_all_list_templates()
        db.close()

        rows = []
        for t in self._templates:
            updated = t.get("updated_at", "")
            upd = updated.replace("T", " ")[:16] if updated else ""
            rows.append((
                t["id"],
                t["name"],
                self._kind_label(t["kind"]),
                self._count_items(t.get("content", "")),
                upd,
            ))
        self.table.set_data(rows)
        self._set_preview("")

    def _set_preview(self, text: str):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _on_select(self, index):
        if index < len(self._templates):
            self._set_preview(self._templates[index].get("content", ""))

    def _add(self):
        dlg = ListTemplateDialog(self, title="Создать шаблон списка")
        self.wait_window(dlg)
        if not dlg.result:
            return

        try:
            db = Database(self.app.config.db_path)
            db.add_list_template(dlg.result["name"], dlg.result["kind"], dlg.result["content"])
            db.close()
            self.log.append(f"[+] Шаблон создан: {dlg.result['name']}")
            self.refresh()
        except Exception as e:
            self.log.append(f"[!] Ошибка создания шаблона: {e}")

    def _edit(self):
        row = self.table.get_selected_row()
        if not row:
            return
        template_id = int(row[0])
        tpl = None
        for t in self._templates:
            if t["id"] == template_id:
                tpl = t
                break
        if not tpl:
            return

        dlg = ListTemplateDialog(self, title="Редактировать шаблон", initial=tpl)
        self.wait_window(dlg)
        if not dlg.result:
            return

        try:
            db = Database(self.app.config.db_path)
            db.update_list_template(template_id, dlg.result["name"], dlg.result["kind"], dlg.result["content"])
            db.close()
            self.log.append(f"[+] Шаблон обновлён: {dlg.result['name']}")
            self.refresh()
        except Exception as e:
            self.log.append(f"[!] Ошибка обновления шаблона: {e}")

    def _delete(self):
        row = self.table.get_selected_row()
        if not row:
            return
        template_id = int(row[0])
        name = row[1]
        confirm = ctk.CTkInputDialog(
            text=f"Удалить шаблон '{name}'? Введите YES для подтверждения.",
            title="Удаление шаблона")
        if (confirm.get_input() or "").strip().upper() != "YES":
            return
        try:
            db = Database(self.app.config.db_path)
            db.delete_list_template(template_id)
            db.close()
            self.log.append(f"[+] Удалён: {name}")
            self.refresh()
        except Exception as e:
            self.log.append(f"[!] Ошибка удаления: {e}")

    def _import(self):
        path = filedialog.askopenfilename(
            filetypes=[("TXT/CSV", "*.txt *.csv"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            items = []
            if path.lower().endswith(".csv"):
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    r = csv.reader(f)
                    for row in r:
                        if row and row[0].strip():
                            items.append(row[0].strip())
            else:
                with open(path, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        v = line.strip()
                        if v:
                            items.append(v)
            content = "\n".join(items)
        except Exception as e:
            self.log.append(f"[!] Ошибка импорта: {e}")
            return

        dlg = ListTemplateDialog(self, title="Импорт шаблона", initial={"content": content})
        self.wait_window(dlg)
        if not dlg.result:
            return
        try:
            db = Database(self.app.config.db_path)
            db.add_list_template(dlg.result["name"], dlg.result["kind"], dlg.result["content"])
            db.close()
            self.log.append(f"[+] Импортировано: {dlg.result['name']}")
            self.refresh()
        except Exception as e:
            self.log.append(f"[!] Ошибка сохранения импорта: {e}")

    def _export(self):
        row = self.table.get_selected_row()
        if not row:
            return
        template_id = int(row[0])
        tpl = None
        for t in self._templates:
            if t["id"] == template_id:
                tpl = t
                break
        if not tpl:
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("TXT", "*.txt"), ("CSV", "*.csv")],
            initialfile=f"{tpl['name']}.txt",
        )
        if not path:
            return

        items = [l.strip() for l in (tpl.get("content", "") or "").splitlines() if l.strip()]
        try:
            if path.lower().endswith(".csv"):
                with open(path, "w", encoding="utf-8", newline="") as f:
                    w = csv.writer(f)
                    for v in items:
                        w.writerow([v])
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(items))
            self.log.append(f"[+] Экспортировано: {path}")
        except Exception as e:
            self.log.append(f"[!] Ошибка экспорта: {e}")


class ParsingFrame(ctk.CTkFrame):
    """Раздел: Парсинг (обычный + смарт)"""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._running = False
        self._stop_event = threading.Event()
        self._last_progress_ui_ts = 0.0

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(padx=20, pady=(12, 2), fill="x")
        ctk.CTkLabel(header, text="🔍 Парсинг групп", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=("#2563EB", "#60A5FA")).pack(side="left")
        ctk.CTkLabel(header, text="  — обычный + смарт-парсинг + комментаторы",
                     font=ctk.CTkFont(size=12), text_color=("gray50", "gray60")).pack(side="left", pady=4)

        # Табы: Обычный парсинг / Смарт-парсинг
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(padx=20, pady=5, fill="both", expand=True)

        self.tab_regular = self.tabview.add("Обычный парсинг")
        self.tab_smart = self.tabview.add("Смарт-парсинг")

        self._build_regular_tab()
        self._build_smart_tab()

        # Общий лог
        self.log = LogFrame(self, height=150)
        self.log.pack(padx=20, pady=(5, 10), fill="x")

        self._refresh_accounts()
        self._refresh_stats()

    # --- Обычный парсинг ---

    def _build_regular_tab(self):
        tab = self.tab_regular

        form = ctk.CTkFrame(tab, fg_color="transparent")
        form.pack(padx=10, pady=5, fill="x")

        self.reg_use_template_var = ctk.BooleanVar(value=False)
        self._reg_template = None
        self._reg_template_links: list[str] = []

        ctk.CTkCheckBox(
            form,
            text="Парсить по шаблону чатов",
            variable=self.reg_use_template_var,
            command=self._toggle_regular_source,
        ).grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(form, text="Группа:").grid(row=1, column=0, padx=(0, 10), pady=5, sticky="w")
        self.group_entry = ctk.CTkEntry(form, placeholder_text="@group_name", width=250)
        self.group_entry.grid(row=1, column=1, padx=5, pady=5, sticky="w")

        self.btn_reg_pick_template = ctk.CTkButton(
            form, text="Шаблон…", width=120, command=self._pick_regular_template
        )
        self.btn_reg_pick_template.grid(row=1, column=2, padx=5, pady=5, sticky="w")
        self.lbl_reg_template = ctk.CTkLabel(form, text="—", text_color="gray60")
        self.lbl_reg_template.grid(row=1, column=3, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(form, text="Аудитория (имя):").grid(row=2, column=0, padx=(0, 10), pady=5, sticky="w")
        self.reg_audience_entry = ctk.CTkEntry(form, placeholder_text="например: модели", width=250)
        self.reg_audience_entry.grid(row=2, column=1, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(form, text="Аккаунт:").grid(row=3, column=0, padx=(0, 10), pady=5, sticky="w")
        self.account_var = ctk.StringVar(value="")
        self.account_menu = ctk.CTkOptionMenu(form, variable=self.account_var, values=[""], width=250)
        self.account_menu.grid(row=3, column=1, padx=5, pady=5, sticky="w")

        self.aggressive_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(form, text="Aggressive (поиск по алфавиту)",
                         variable=self.aggressive_var).grid(row=4, column=0, columnspan=2, padx=5, pady=5, sticky="w")

        self.commenters_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(form, text="Комментаторы канала",
                         variable=self.commenters_var).grid(row=5, column=0, columnspan=2, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(form, text="Лимит постов:").grid(row=6, column=0, padx=(0, 10), pady=5, sticky="w")
        self.limit_entry = ctk.CTkEntry(form, placeholder_text="50", width=100)
        self.limit_entry.grid(row=6, column=1, padx=5, pady=5, sticky="w")

        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.pack(padx=10, pady=10, anchor="w")
        self.btn_start = ctk.CTkButton(btn_row, text="Начать парсинг", command=self._start_parsing)
        self.btn_start.pack(side="left")
        self.btn_stop = ctk.CTkButton(
            btn_row,
            text="■ Остановить",
            state="disabled",
            fg_color="firebrick",
            hover_color="darkred",
            command=self._stop_parsing,
        )
        self.btn_stop.pack(side="left", padx=(8, 0))

        self.lbl_reg_progress = ctk.CTkLabel(tab, text="чат —/— | сохранено 0", text_color="gray70")
        self.lbl_reg_progress.pack(padx=10, pady=(0, 8), anchor="w")

        # Статистика групп
        ctk.CTkLabel(tab, text="Спарсенные группы:", font=ctk.CTkFont(weight="bold")).pack(
            padx=10, pady=(10, 0), anchor="w")
        self.stats_table = ScrollableTable(tab, columns=["Группа", "Пользователей"], height=90)
        self.stats_table.pack(padx=10, pady=5, fill="x")

    # --- Смарт-парсинг ---

    def _build_smart_tab(self):
        tab = self.tab_smart

        form = ctk.CTkFrame(tab, fg_color="transparent")
        form.pack(padx=10, pady=5, fill="x")

        ctk.CTkLabel(
            form,
            text="TELETON READY: фильтр периода/длины + исключающие слова + CSV с текстом поста",
            text_color="#2FA572",
        ).grid(row=0, column=0, columnspan=4, padx=5, pady=(0, 6), sticky="w")

        self.sp_use_template_var = ctk.BooleanVar(value=True)
        self._sp_template = None
        self._sp_template_links: list[str] = []

        ctk.CTkCheckBox(
            form,
            text="Парсить по шаблону чатов",
            variable=self.sp_use_template_var,
            command=self._toggle_smart_source,
        ).grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(form, text="Группа:").grid(row=2, column=0, padx=(0, 10), pady=5, sticky="w")
        self.sp_group_entry = ctk.CTkEntry(form, placeholder_text="@group_name", width=250)
        self.sp_group_entry.grid(row=2, column=1, padx=5, pady=5, sticky="w")

        self.btn_sp_pick_template = ctk.CTkButton(
            form, text="Шаблон…", width=120, command=self._pick_smart_template
        )
        self.btn_sp_pick_template.grid(row=2, column=2, padx=5, pady=5, sticky="w")
        self.lbl_sp_template = ctk.CTkLabel(form, text="—", text_color="gray60")
        self.lbl_sp_template.grid(row=2, column=3, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(form, text="Аудитория (имя):").grid(row=3, column=0, padx=(0, 10), pady=5, sticky="w")
        self.sp_audience_entry = ctk.CTkEntry(form, placeholder_text="например: модели", width=250)
        self.sp_audience_entry.grid(row=3, column=1, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(form, text="Аккаунт:").grid(row=4, column=0, padx=(0, 10), pady=5, sticky="w")
        self.sp_account_var = ctk.StringVar(value="")
        self.sp_account_menu = ctk.CTkOptionMenu(form, variable=self.sp_account_var, values=[""], width=250)
        self.sp_account_menu.grid(row=4, column=1, padx=5, pady=5, sticky="w")

        # Режим
        ctk.CTkLabel(form, text="Режим:").grid(row=5, column=0, padx=(0, 10), pady=5, sticky="w")
        self.sp_mode_var = ctk.StringVar(value="Ключевые слова")
        self.sp_mode_btn = ctk.CTkSegmentedButton(
            form, values=["Ключевые слова", "ИИ"],
            variable=self.sp_mode_var,
            command=self._on_mode_change,
        )
        self.sp_mode_btn.grid(row=5, column=1, padx=5, pady=5, sticky="w")

        # Ключевые слова
        self.sp_keywords_label = ctk.CTkLabel(form, text="Ключевые слова:")
        self.sp_keywords_label.grid(row=6, column=0, padx=(0, 10), pady=5, sticky="nw")
        self.sp_keywords_entry = ctk.CTkEntry(form, placeholder_text="трафик, твиттер, reddit", width=350)
        self.sp_keywords_entry.grid(row=6, column=1, padx=5, pady=5, sticky="w")

        # Исключающие ключевые слова
        self.sp_exclude_label = ctk.CTkLabel(form, text="Исключить:")
        self.sp_exclude_label.grid(row=7, column=0, padx=(0, 10), pady=5, sticky="nw")
        self.sp_exclude_entry = ctk.CTkEntry(form, placeholder_text="траблер, нерелевантно", width=350)
        self.sp_exclude_entry.grid(row=7, column=1, padx=5, pady=5, sticky="w")

        # Опции фильтрации
        self.sp_exact_match_var = ctk.BooleanVar(value=False)
        self.sp_exact_check = ctk.CTkCheckBox(
            form, text="Точное совпадение (слово целиком)", variable=self.sp_exact_match_var
        )
        self.sp_exact_check.grid(row=8, column=1, padx=5, pady=5, sticky="w")

        self.sp_regex_var = ctk.BooleanVar(value=False)
        self.sp_regex_check = ctk.CTkCheckBox(
            form, text="Использовать регулярные выражения", variable=self.sp_regex_var
        )
        self.sp_regex_check.grid(row=9, column=1, padx=5, pady=5, sticky="w")

        self.sp_ai_provider_label = ctk.CTkLabel(form, text="AI провайдер:")
        self.sp_ai_provider_label.grid(row=10, column=0, padx=(0, 10), pady=5, sticky="w")
        self.sp_ai_provider_var = ctk.StringVar(value="openai")
        self.sp_ai_provider_btn = ctk.CTkSegmentedButton(
            form, values=["openai", "groq"], variable=self.sp_ai_provider_var
        )
        self.sp_ai_provider_btn.grid(row=10, column=1, padx=5, pady=5, sticky="w")

        self.sp_ai_label = ctk.CTkLabel(form, text="Критерий ИИ:")
        self.sp_ai_label.grid(row=11, column=0, padx=(0, 10), pady=5, sticky="nw")
        self.sp_ai_textbox = ctk.CTkTextbox(form, height=60, width=350)
        self.sp_ai_textbox.grid(row=11, column=1, padx=5, pady=5, sticky="w")

        self.sp_ai_provider_label.grid_remove()
        self.sp_ai_provider_btn.grid_remove()
        self.sp_ai_label.grid_remove()
        self.sp_ai_textbox.grid_remove()

        ctk.CTkLabel(form, text="Лимит сообщений:").grid(row=12, column=0, padx=(0, 10), pady=5, sticky="w")
        self.sp_limit_entry = ctk.CTkEntry(form, placeholder_text="500", width=100)
        self.sp_limit_entry.grid(row=12, column=1, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(form, text="Диапазон:").grid(row=13, column=0, padx=(0, 10), pady=5, sticky="w")
        self.sp_scan_mode_var = ctk.StringVar(value="Период")
        self.sp_scan_mode_btn = ctk.CTkSegmentedButton(
            form,
            values=["Сообщения", "Период"],
            variable=self.sp_scan_mode_var,
            command=self._on_smart_scan_mode_change,
        )
        self.sp_scan_mode_btn.grid(row=13, column=1, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(form, text="Период (дней):").grid(row=13, column=2, padx=(20, 10), pady=5, sticky="w")
        self.sp_days_entry = ctk.CTkEntry(form, placeholder_text="30", width=80)
        self.sp_days_entry.insert(0, "7")
        self.sp_days_entry.grid(row=13, column=3, padx=5, pady=5, sticky="w")
        self.sp_limit_entry.bind("<FocusIn>", lambda _e: self._set_smart_scan_mode("Сообщения"))
        self.sp_limit_entry.bind("<Button-1>", lambda _e: self._set_smart_scan_mode("Сообщения"))
        self.sp_days_entry.bind("<FocusIn>", lambda _e: self._set_smart_scan_mode("Период"))
        self.sp_days_entry.bind("<Button-1>", lambda _e: self._set_smart_scan_mode("Период"))

        ctk.CTkLabel(form, text="Длина поста:").grid(row=14, column=0, padx=(0, 10), pady=5, sticky="w")
        len_row = ctk.CTkFrame(form, fg_color="transparent")
        len_row.grid(row=14, column=1, padx=5, pady=5, sticky="w")
        self.sp_min_chars_entry = ctk.CTkEntry(len_row, placeholder_text="min", width=70)
        self.sp_min_chars_entry.insert(0, "20")
        self.sp_min_chars_entry.pack(side="left", padx=(0, 6))
        self.sp_max_chars_entry = ctk.CTkEntry(len_row, placeholder_text="max", width=80)
        self.sp_max_chars_entry.insert(0, "800")
        self.sp_max_chars_entry.pack(side="left")
        self._apply_smart_model_defaults()

        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.pack(padx=10, pady=10, anchor="w")
        self.btn_smart_start = ctk.CTkButton(btn_row, text="Начать смарт-парсинг", command=self._start_smart_parsing)
        self.btn_smart_start.pack(side="left")
        self.btn_smart_stop = ctk.CTkButton(
            btn_row,
            text="■ Остановить",
            state="disabled",
            fg_color="firebrick",
            hover_color="darkred",
            command=self._stop_parsing,
        )
        self.btn_smart_stop.pack(side="left", padx=(8, 0))

        self.lbl_sp_progress = ctk.CTkLabel(
            tab, text="чат —/— | сообщение —/— | найдено 0 | сохранено 0", text_color="gray70"
        )
        self.lbl_sp_progress.pack(padx=10, pady=(0, 8), anchor="w")

        # Таблица найденных постов
        ctk.CTkLabel(tab, text="Найденные посты:", font=ctk.CTkFont(weight="bold")).pack(
            padx=10, pady=(10, 0), anchor="w")
        self.sp_results_table = ScrollableTable(
            tab, columns=["Чат", "Дата", "Пользователь", "Текст поста", "Совпадение", "Msg"], height=120
        )
        self.sp_results_table.pack(padx=10, pady=5, fill="both", expand=True)

    def _set_entry_text(self, entry, value: str):
        try:
            entry.delete(0, "end")
            entry.insert(0, value)
        except Exception:
            pass

    def _autoload_smart_template(self) -> bool:
        """Выбрать самый крупный сохраненный шаблон чатов, если пользователь не выбрал его вручную."""
        try:
            db = Database(self.app.config.db_path)
            templates = [t for t in db.get_all_list_templates() if t.get("kind") in ("groups", "channels", "mixed")]
            db.close()
            templates.sort(key=lambda t: len(t.get("content") or ""), reverse=True)
            template = templates[0] if templates else None
        except Exception:
            template = None
        if not template:
            return False
        links = [l.strip() for l in (template.get("content") or "").splitlines() if l.strip()]
        if not links:
            return False
        self._sp_template = template
        self._sp_template_links = links
        try:
            self.lbl_sp_template.configure(text=template.get("name") or "—")
        except Exception:
            pass
        return True

    def _apply_smart_model_defaults(self):
        """Дефолты для поиска моделей, чтобы вкладка не стартовала с мусорными ключами."""
        keywords = (
            "ищу агентство, ищу агенство, ищу менеджера, ищу продюсера, "
            "ищу ведение, нужен менеджер, нужна помощь с ведением, на ведение, "
            "для ведения, модель ищет, я модель, страница onlyfans, страница онлифанс, "
            "страница fansly, анкета onlyfans, типаж, без лица, с лицом, готовый контент есть"
        )
        exclude = (
            "агентство ищет, агенство ищет, ищем моделей, ищем девушек, ищем специалиста, "
            "обязанности, требования:, вакансия, набор моделей, трафер, траффер, траффик, "
            "трафик, реклама, рекламирую, курс, обучение, reddit, реддит, twitter, твиттер, "
            "sfs, рассылка, база, ищу чаттера, ищем чаттеров, ищу чатера, оператор, секстер"
        )
        self._set_entry_text(self.sp_keywords_entry, keywords)
        self._set_entry_text(self.sp_exclude_entry, exclude)
        self._set_entry_text(self.sp_limit_entry, "5000")
        self._set_entry_text(self.sp_days_entry, "7")
        self._set_entry_text(self.sp_min_chars_entry, "20")
        self._set_entry_text(self.sp_max_chars_entry, "800")
        try:
            self.sp_mode_var.set("Ключевые слова")
            self.sp_mode_btn.set("Ключевые слова")
        except Exception:
            pass
        try:
            self.sp_use_template_var.set(True)
        except Exception:
            pass
        self._autoload_smart_template()
        self._set_smart_scan_mode("Период")
        self._toggle_smart_source()

    def _set_smart_scan_mode(self, value):
        """Выбрать диапазон сканирования из кнопки или по фокусу поля."""
        try:
            self.sp_scan_mode_var.set(value)
        except Exception:
            pass
        try:
            self.sp_scan_mode_btn.set(value)
        except Exception:
            pass
        self._on_smart_scan_mode_change(value)

    def _on_smart_scan_mode_change(self, value):
        """Оба поля остаются редактируемыми, клик по полю просто выбирает режим."""
        try:
            self.sp_limit_entry.configure(state="normal")
        except Exception:
            pass
        try:
            self.sp_days_entry.configure(state="normal")
        except Exception:
            pass

    def _on_mode_change(self, value):
        """Переключение видимости полей keywords / ai"""
        if value == "Ключевые слова":
            self.sp_keywords_label.grid()
            self.sp_keywords_entry.grid()
            self.sp_exclude_label.grid()
            self.sp_exclude_entry.grid()
            self.sp_exact_check.grid()
            self.sp_regex_check.grid()
            self.sp_ai_provider_label.grid_remove()
            self.sp_ai_provider_btn.grid_remove()
            self.sp_ai_label.grid_remove()
            self.sp_ai_textbox.grid_remove()
        else:
            self.sp_keywords_label.grid_remove()
            self.sp_keywords_entry.grid_remove()
            self.sp_exclude_label.grid_remove()
            self.sp_exclude_entry.grid_remove()
            self.sp_exact_check.grid_remove()
            self.sp_regex_check.grid_remove()
            self.sp_ai_provider_label.grid()
            self.sp_ai_provider_btn.grid()
            self.sp_ai_label.grid()
            self.sp_ai_textbox.grid()

    def _format_account(self, phone: str, custom_name: str = "") -> str:
        """Делегирует на глобальный format_account."""
        return format_account(phone, custom_name)

    def _refresh_account_maps(self):
        """Пересобирает словари для преобразования отображаемой строки <-> телефон.
        Вызывать после загрузки аккаунтов при рефреше меню.
        """
        if not hasattr(self, "_phone_to_display"):
            self._phone_to_display: dict[str, str] = {}
            self._display_to_phone: dict[str, str] = {}
        self._phone_to_display.clear()
        self._display_to_phone.clear()
        try:
            db = Database(self.app.config.db_path)
            accs = db.get_all_accounts()
            db.close()
            for a in accs:
                disp = self._format_account(a.phone, getattr(a, "custom_name", ""))
                self._phone_to_display[a.phone] = disp
                self._display_to_phone[disp] = a.phone
        except Exception:
            pass

    def _resolve_phone(self, value: str) -> str:
        """Из значения меню (может быть 'Метка (+79...)' или сырой номер) возвращает сырой телефон.
        Специальные значения ('Все активные', 'Нет аккаунтов') возвращаются как есть.
        """
        if not value or value in ("Все активные", "Нет аккаунтов"):
            return value
        if hasattr(self, "_display_to_phone") and value in self._display_to_phone:
            return self._display_to_phone[value]
        # Если уже телефон или неизвестно — возвращаем как есть (обратно совместимо)
        return value

    def _refresh_accounts(self):
        db = Database(self.app.config.db_path)
        accounts = db.get_all_accounts()
        db.close()

        self._refresh_account_maps()

        active = [a for a in accounts if a.is_active]
        if active:
            # Для меню используем красивые строки, но var всё равно хранит телефон (через set после configure)
            displays = [self._phone_to_display.get(a.phone, a.phone) for a in active]
            phones = [a.phone for a in active]
            self.account_menu.configure(values=displays)
            # Показываем красивое имя в меню. Для получения реального телефона используй _resolve_phone(var.get())
            first_disp = displays[0]
            self.account_var.set(first_disp)
            self.sp_account_menu.configure(values=displays)
            self.sp_account_var.set(first_disp)
        else:
            self.account_menu.configure(values=["Нет аккаунтов"])
            self.account_var.set("Нет аккаунтов")
            self.sp_account_menu.configure(values=["Нет аккаунтов"])
            self.sp_account_var.set("Нет аккаунтов")

        # Циклическая: "Все активные" + аккаунты (с метками)
        if hasattr(self, "c_account_menu"):
            try:
                base = ["Все активные"]
                if active:
                    base += displays
                self.c_account_menu.configure(values=base)
                # если раньше было выбрано что-то нестандартное — оставляем, иначе "Все активные"
                cur = self.c_account_var.get()
                if cur not in base:
                    self.c_account_var.set("Все активные")
            except Exception:
                pass

        # Обновляем остальные селекторы аккаунтов (рассылки и т.д.)
        for var_name, menu_name in [
            ("dm_account_var", "dm_account_menu"),
            ("m_account_var", "m_account_menu"),
            ("q_account_var", "q_account_menu"),
            ("b_account_var", "b_account_menu"),
        ]:
            try:
                var = getattr(self, var_name, None)
                menu = getattr(self, menu_name, None)
                if var is not None and menu is not None:
                    cur_val = var.get()
                    special = []
                    if "Все активные" in (getattr(menu, "_values", []) or []) or cur_val == "Все активные":
                        special = ["Все активные"]
                    new_vals = special + displays if displays else (special or ["Нет аккаунтов"])
                    menu.configure(values=new_vals)
                    # если текущее значение special — оставляем, иначе первый disp
                    if cur_val in ("Все активные", "Нет аккаунтов") or not cur_val:
                        if cur_val not in new_vals:
                            var.set(new_vals[0] if new_vals else "Все активные")
                    # иначе оставляем как есть (оно может быть display)
            except Exception:
                pass

    def _refresh_stats(self):
        db = Database(self.app.config.db_path)
        stats = db.get_parsed_groups_stats()
        db.close()

        rows = [(s["group_source"], s["count"]) for s in stats]
        self.stats_table.set_data(rows)

    def _toggle_regular_source(self):
        use_template = bool(self.reg_use_template_var.get()) if hasattr(self, "reg_use_template_var") else False
        try:
            self.group_entry.configure(state="disabled" if use_template else "normal")
        except Exception:
            pass
        try:
            self.btn_reg_pick_template.configure(state="normal" if use_template else "disabled")
        except Exception:
            pass

    def _toggle_smart_source(self):
        use_template = bool(self.sp_use_template_var.get()) if hasattr(self, "sp_use_template_var") else False
        try:
            self.sp_group_entry.configure(state="disabled" if use_template else "normal")
        except Exception:
            pass
        try:
            self.btn_sp_pick_template.configure(state="normal" if use_template else "disabled")
        except Exception:
            pass

    def _pick_regular_template(self):
        db = Database(self.app.config.db_path)
        templates = [t for t in db.get_all_list_templates() if t.get("kind") in ("groups", "channels", "mixed")]
        db.close()
        if not templates:
            self.log.append("[!] Нет шаблонов чатов (создайте в разделе 'Шаблоны')")
            return
        pick = ListTemplatePickerDialog(self, templates, title="Шаблон чатов для парсинга")
        self.wait_window(pick)
        if not pick.result:
            return
        self._reg_template = pick.result
        self._reg_template_links = [l.strip() for l in (pick.result.get("content") or "").splitlines() if l.strip()]
        self.lbl_reg_template.configure(text=pick.result.get("name") or "—")
        self._toggle_regular_source()

    def _pick_smart_template(self):
        db = Database(self.app.config.db_path)
        templates = [t for t in db.get_all_list_templates() if t.get("kind") in ("groups", "channels", "mixed")]
        db.close()
        if not templates:
            self.log.append("[!] Нет шаблонов чатов (создайте в разделе 'Шаблоны')")
            return
        pick = ListTemplatePickerDialog(self, templates, title="Шаблон чатов для смарт-парсинга")
        self.wait_window(pick)
        if not pick.result:
            return
        self._sp_template = pick.result
        self._sp_template_links = [l.strip() for l in (pick.result.get("content") or "").splitlines() if l.strip()]
        self.lbl_sp_template.configure(text=pick.result.get("name") or "—")
        self._toggle_smart_source()

    def _refresh_smart_results(self, group_source: str, matched_since: str | None = None):
        """Обновить таблицу найденных постов"""
        display_limit = 500
        db = Database(self.app.config.db_path)
        posts = db.get_matched_posts(group_source, limit=display_limit, matched_since=matched_since)
        db.close()

        rows = []
        for p in posts:
            username = f"@{p.sender_username}" if p.sender_username else str(p.sender_id)
            text_preview = p.message_text[:80].replace("\n", " ")
            match_info = p.matched_keywords if p.match_mode == "keywords" else p.ai_reason[:60]
            msg_dt = (p.message_date or "")[:19].replace("T", " ") if p.message_date else "—"
            msg_link = p.message_link or str(p.message_id)
            rows.append((p.group_source or "—", msg_dt, username, text_preview, match_info, msg_link))
        self.sp_results_table.set_data(rows)
        if len(rows) >= display_limit:
            self.log.append(f"[i] В таблице показаны последние {display_limit} совпадений, остальные сохранены в базе")
        if matched_since:
            self.log.append(f"[i] Таблица показывает текущий запуск с {matched_since}")

    def on_show(self):
        """Вызывается при переключении на эту вкладку"""
        self._refresh_accounts()
        self._refresh_stats()
        try:
            self._toggle_regular_source()
            self._toggle_smart_source()
        except Exception:
            pass
        try:
            from ads_database import AdsDB
            adb = AdsDB(self.app.config.db_path)
            try:
                provider = adb.get_setting("smart_parse_ai_provider", "openai") or "openai"
            finally:
                adb.close()
            if hasattr(self, "sp_ai_provider_var"):
                self.sp_ai_provider_var.set(provider if provider in ("openai", "groq") else "openai")
        except Exception:
            pass

    def _start_parsing(self):
        _log_action("parsing", "_start_parsing")
        if self._running:
            return

        use_template = bool(self.reg_use_template_var.get()) if hasattr(self, "reg_use_template_var") else False
        if use_template:
            groups = list(self._reg_template_links or [])
        else:
            groups = [self.group_entry.get().strip()]
        groups = [g for g in groups if g]

        phone = self._resolve_phone(self.account_var.get())
        if not groups or not phone or phone == "Нет аккаунтов":
            self.log.append("[!] Укажите чаты и аккаунт")
            return
        if use_template and not groups:
            self.log.append("[!] Выберите шаблон чатов")
            return

        audience_name = (self.reg_audience_entry.get().strip() if hasattr(self, "reg_audience_entry") else "")
        if not audience_name:
            audience_name = (self._reg_template.get("name") if use_template and self._reg_template else groups[0])

        self._stop_event.clear()
        self._running = True
        self.btn_start.configure(state="disabled", text="Парсинг...")
        self.btn_smart_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_smart_stop.configure(state="normal")
        self.log.clear()
        self.log.append(f"[~] Парсинг: чатов={len(groups)} | аудитория={audience_name} | аккаунт={phone}...")

        aggressive = self.aggressive_var.get()
        commenters = self.commenters_var.get()
        limit_posts = self.limit_entry.get().strip()
        limit_posts = int(limit_posts) if limit_posts.isdigit() else 50

        def parse_thread():
            log_queue = self.app.log_queue
            _thread_local.log_handler = lambda msg: log_queue.put(("parsing_log", msg))
            _thread_local.log_tag = "parsing"
            progress_state = {"ts": 0.0, "chat_index": 0}

            def emit_progress(payload: dict):
                payload = payload or {}
                now = time.monotonic()
                ci = int(payload.get("chat_index", 0) or 0)
                if ci != progress_state["chat_index"] or now - progress_state["ts"] >= 0.35 or self._stop_event.is_set():
                    progress_state["ts"] = now
                    progress_state["chat_index"] = ci
                    log_queue.put(("parsing_progress", dict({"tab": "regular"}, **payload)))

            try:
                loop = asyncio.new_event_loop()

                async def do_parse():
                    from parser import GroupParser
                    from sender import TelegramSender

                    cfg = self.app.config
                    db = Database(cfg.db_path)
                    accounts = db.get_active_accounts()
                    acc = None
                    for a in accounts:
                        if a.phone == phone:
                            acc = a
                            break

                    if not acc:
                        print(f"[!] Аккаунт {phone} не найден")
                        db.close()
                        return

                    sender = TelegramSender(acc, cfg, db)
                    if not await sender.connect():
                        db.close()
                        return

                    try:
                        parser_obj = GroupParser(
                            sender.client,
                            db,
                            stop_requested=self._stop_event.is_set,
                            progress_cb=print,
                            progress_state_cb=emit_progress,
                        )
                        saved_total = 0
                        chats_total = len(groups)
                        for idx, grp in enumerate(groups, start=1):
                            if self._stop_event.is_set():
                                break
                            log_queue.put(("parsing_progress", {"tab": "regular", "chat_index": idx, "chats_total": chats_total, "saved": saved_total}))
                            if commenters:
                                count = await parser_obj.parse_commenters(grp, limit_posts=limit_posts, group_source_override=audience_name)
                            else:
                                count = await parser_obj.parse_group(grp, aggressive=aggressive, group_source_override=audience_name)
                            saved_total += int(count or 0)
                            log_queue.put(("parsing_progress", {"tab": "regular", "chat_index": idx, "chats_total": chats_total, "saved": saved_total}))
                        if self._stop_event.is_set():
                            print(f"\n[=] Парсинг остановлен: аудитория={audience_name} | аккаунт={phone} | обработано={saved_total}")
                        else:
                            print(f"\n=== Итого: {saved_total} пользователей обработано ===")
                    finally:
                        await sender.disconnect()

                    db.close()

                _run_loop(loop, do_parse())
            except Exception as e:
                log_queue.put(("parsing_log", f"[-] Ошибка: {e}"))
            finally:
                _thread_local.log_handler = None
                self.app.log_queue.put(("parsing_done", None))

        threading.Thread(target=parse_thread, daemon=True).start()

    def _start_smart_parsing(self):
        _log_action("parsing", "_start_smart_parsing")
        if self._running:
            return

        use_template = bool(self.sp_use_template_var.get()) if hasattr(self, "sp_use_template_var") else False
        manual_group = self.sp_group_entry.get().strip()
        if not use_template and not manual_group and self._autoload_smart_template():
            use_template = True
            try:
                self.sp_use_template_var.set(True)
            except Exception:
                pass
            self._toggle_smart_source()
            self.log.append("[i] Группа не указана — автоматически выбран шаблон чатов")
        if use_template and not (self._sp_template_links or []):
            self._autoload_smart_template()
        if use_template:
            groups = list(self._sp_template_links or [])
        else:
            groups = [manual_group]
        groups = [g for g in groups if g]

        phone = self._resolve_phone(self.sp_account_var.get())
        if not groups or not phone or phone == "Нет аккаунтов":
            self.log.append("[!] Укажите чаты и аккаунт")
            return
        if use_template and not groups:
            self.log.append("[!] Выберите шаблон чатов")
            return

        audience_name = (self.sp_audience_entry.get().strip() if hasattr(self, "sp_audience_entry") else "")
        if not audience_name:
            audience_name = (self._sp_template.get("name") if use_template and self._sp_template else groups[0])

        mode_label = self.sp_mode_var.get()
        mode = "keywords" if mode_label == "Ключевые слова" else "ai"

        keywords_str = self.sp_keywords_entry.get().strip()
        exclude_str = self.sp_exclude_entry.get().strip()
        ai_criteria = self.sp_ai_textbox.get("1.0", "end").strip()
        use_exact_match = self.sp_exact_match_var.get()
        use_regex = self.sp_regex_var.get()

        if mode == "keywords" and not keywords_str:
            self.log.append("[!] Введите ключевые слова")
            return
        if mode == "ai" and not ai_criteria:
            self.log.append("[!] Введите критерий для ИИ")
            return
        ai_provider = (self.sp_ai_provider_var.get().strip() if hasattr(self, "sp_ai_provider_var") else "openai") or "openai"
        if mode == "ai":
            if ai_provider not in ("openai", "groq"):
                self.log.append(f"[!] Неизвестный AI-провайдер: {ai_provider!r}. Доступны: openai, groq")
                return
            provider_label = "OpenAI" if ai_provider == "openai" else "Groq"
            api_key = (
                self.app.config.openai_api_key
                if ai_provider == "openai"
                else getattr(self.app.config, "groq_api_key", "")
            )
            if not api_key:
                self.log.append(
                    f"[!] AI-фильтр не запущен: нет {provider_label} API Key. Укажите ключ в Настройках."
                )
                return
            try:
                from ads_database import AdsDB
                adb = AdsDB(self.app.config.db_path)
                try:
                    adb.set_setting("smart_parse_ai_provider", ai_provider)
                finally:
                    adb.close()
            except Exception:
                pass

        scan_mode = self.sp_scan_mode_var.get() if hasattr(self, "sp_scan_mode_var") else "Сообщения"
        period_days = 0
        since_dt = None
        if scan_mode == "Период":
            days_str = self.sp_days_entry.get().strip() if hasattr(self, "sp_days_entry") else ""
            period_days = int(days_str) if days_str.isdigit() else 7
            period_days = max(1, min(period_days, 3650))
            since_dt = datetime.now(timezone.utc) - timedelta(days=period_days)
            limit_messages = None
        else:
            limit_str = self.sp_limit_entry.get().strip()
            limit_messages = int(limit_str) if limit_str.isdigit() else 500
        min_chars_str = self.sp_min_chars_entry.get().strip() if hasattr(self, "sp_min_chars_entry") else ""
        max_chars_str = self.sp_max_chars_entry.get().strip() if hasattr(self, "sp_max_chars_entry") else ""
        min_text_chars = int(min_chars_str) if min_chars_str.isdigit() else 0
        max_text_chars = int(max_chars_str) if max_chars_str.isdigit() else 0
        if min_text_chars < 0:
            min_text_chars = 0
        if max_text_chars and max_text_chars < min_text_chars:
            max_text_chars = min_text_chars

        self._stop_event.clear()
        self._running = True
        self.btn_smart_start.configure(state="disabled", text="Смарт-парсинг...")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_smart_stop.configure(state="normal")
        self.log.clear()
        self._smart_run_started_at = datetime.now().isoformat(timespec="seconds")
        mode_info = mode
        if mode == "ai":
            mode_info = f"{mode} ({ai_provider})"
        scan_desc = f"период={period_days}д" if since_dt else f"лимит={limit_messages}"
        self.log.append(
            f"[~] Смарт-парсинг: чатов={len(groups)} | аудитория={audience_name} | "
            f"режим={mode_info} | аккаунт={phone} | {scan_desc} | "
            f"длина={min_text_chars or '—'}-{max_text_chars or '—'}..."
        )
        if mode == "keywords":
            self.log.append(f"[i] Ключи текущего запуска: {keywords_str}")
        if exclude_str:
            self.log.append(f"[i] Исключающие слова: {exclude_str}")
        try:
            self.sp_results_table.set_data([])
        except Exception:
            pass

        self._smart_group = audience_name

        def smart_parse_thread():
            log_queue = self.app.log_queue
            _thread_local.log_handler = lambda msg: log_queue.put(("parsing_log", msg))
            _thread_local.log_tag = "parsing"
            progress_state = {"ts": 0.0, "chat_index": 0, "message_index": -1}

            def emit_progress(payload: dict):
                payload = payload or {}
                now = time.monotonic()
                ci = int(payload.get("chat_index", 0) or 0)
                mi = int(payload.get("message_index", 0) or 0)
                mt = int(payload.get("messages_total", 0) or 0)
                final_msg = bool(mt and mi >= mt)
                if (
                    ci != progress_state["chat_index"]
                    or final_msg
                    or self._stop_event.is_set()
                    or now - progress_state["ts"] >= 0.35
                ):
                    progress_state["ts"] = now
                    progress_state["chat_index"] = ci
                    progress_state["message_index"] = mi
                    log_queue.put(("parsing_progress", dict({"tab": "smart"}, **payload)))

            try:
                loop = asyncio.new_event_loop()

                async def do_smart_parse():
                    from parser import GroupParser
                    from sender import TelegramSender

                    cfg = self.app.config
                    db = Database(cfg.db_path)
                    accounts = db.get_active_accounts()
                    acc = None
                    for a in accounts:
                        if a.phone == phone:
                            acc = a
                            break

                    if not acc:
                        print(f"[!] Аккаунт {phone} не найден")
                        db.close()
                        return

                    sender = TelegramSender(acc, cfg, db)
                    if not await sender.connect():
                        db.close()
                        return

                    try:
                        parser_obj = GroupParser(
                            sender.client,
                            db,
                            stop_requested=self._stop_event.is_set,
                            progress_cb=print,
                            progress_state_cb=emit_progress,
                        )

                        ai_filter_obj = None
                        keywords = None
                        exclude_keywords = None

                        if mode == "keywords":
                            keywords = [kw.strip() for kw in keywords_str.split(",") if kw.strip()]
                            if exclude_str:
                                exclude_keywords = [kw.strip() for kw in exclude_str.split(",") if kw.strip()]
                        elif mode == "ai":
                            if exclude_str:
                                exclude_keywords = [kw.strip() for kw in exclude_str.split(",") if kw.strip()]
                            from ai_filter import AIFilter
                            provider = (ai_provider or "openai").strip().lower()
                            api_key = (cfg.openai_api_key if provider == "openai" else getattr(cfg, "groq_api_key", "")) or ""
                            proxy = ""
                            if provider == "groq":
                                proxy = (getattr(cfg, "groq_proxy", "") or getattr(cfg, "openai_proxy", "")) or ""
                            else:
                                proxy = getattr(cfg, "openai_proxy", "") or ""

                            model = (getattr(cfg, "openai_model", "") or "gpt-4o-mini") if provider == "openai" else "llama-3.3-70b-versatile"
                            try:
                                from ads_database import AdsDB
                                adb = AdsDB(cfg.db_path)
                                try:
                                    s = adb.load_scheduler_settings()
                                    if provider == "groq":
                                        model = getattr(s, "ai_model_groq", "") or model
                                    else:
                                        model = getattr(s, "ai_model_openai", "") or model
                                finally:
                                    adb.close()
                            except Exception:
                                pass

                            if not proxy:
                                print(f"[!!] ВНИМАНИЕ: AI ({provider}) идёт БЕЗ прокси — палится реальный IP + содержимое постов")
                                print(f"[!!] Задайте {'GROQ_PROXY' if provider == 'groq' else 'OPENAI_PROXY'} в Настройках")

                            try:
                                ai_filter_obj = AIFilter(
                                    provider=provider,
                                    api_key=api_key,
                                    model=model,
                                    proxy=proxy,
                                    timeout_seconds=45.0,
                                )
                            except Exception as e:
                                print(f"[!] AI не настроен: {type(e).__name__}: {e}")
                                return
                            print(f"[i] AI фильтр: provider={provider} model={model}")

                        found_total = 0
                        chats_total = len(groups)
                        for idx, grp in enumerate(groups, start=1):
                            if self._stop_event.is_set():
                                break
                            progress_total = int(limit_messages or 0)
                            log_queue.put(("parsing_progress", {"tab": "smart", "chat_index": idx, "chats_total": chats_total, "message_index": 0, "messages_total": progress_total, "found": found_total, "saved": found_total}))
                            count = await parser_obj.parse_by_content(
                                group=grp,
                                audience_name=audience_name,
                                mode=mode,
                                keywords=keywords,
                                exclude_keywords=exclude_keywords,
                                use_exact_match=use_exact_match,
                                use_regex=use_regex,
                                ai_criteria=ai_criteria,
                                ai_filter=ai_filter_obj,
                                limit_messages=limit_messages,
                                since_dt=since_dt,
                                min_text_chars=min_text_chars,
                                max_text_chars=max_text_chars,
                                chat_index=idx,
                                chats_total=chats_total,
                            )
                            found_total += int(count or 0)
                            stats = getattr(parser_obj, "last_content_stats", {}) or {}
                            read_messages = int(stats.get("message_index", 0) or 0)
                            scanned_messages = int(stats.get("scanned", 0) or 0)
                            err = (stats.get("error") or "").strip()
                            date_note = ", период завершен" if stats.get("stopped_by_date") else ""
                            if err:
                                print(
                                    f"[i] Итог чата {idx}/{chats_total}: прочитано={read_messages}, "
                                    f"проверено={scanned_messages}, найдено={int(count or 0)}{date_note}, ошибка={err}"
                                )
                            else:
                                print(
                                    f"[i] Итог чата {idx}/{chats_total}: прочитано={read_messages}, "
                                    f"проверено={scanned_messages}, найдено={int(count or 0)}{date_note}"
                                )
                            log_queue.put(("parsing_progress", {"tab": "smart", "chat_index": idx, "chats_total": chats_total, "message_index": read_messages, "messages_total": progress_total, "found": found_total, "saved": found_total}))
                        if self._stop_event.is_set():
                            print(
                                f"\n[=] Смарт-парсинг остановлен: аудитория={audience_name} | аккаунт={phone} | найдено={found_total}"
                            )
                        else:
                            print(f"\n=== Итого: {found_total} совпадений найдено ===")
                    finally:
                        await sender.disconnect()

                    db.close()

                _run_loop(loop, do_smart_parse())
            except Exception as e:
                log_queue.put(("parsing_log", f"[-] Ошибка: {e}"))
            finally:
                _thread_local.log_handler = None
                self.app.log_queue.put(("smart_parsing_done", None))

        threading.Thread(target=smart_parse_thread, daemon=True).start()

    def on_queue_message(self, tag, msg):
        if tag == "parsing_log":
            self.log.append(msg)
        elif tag == "parsing_progress":
            try:
                tab = (msg or {}).get("tab", "")
                if tab == "regular" and hasattr(self, "lbl_reg_progress"):
                    ci = int((msg or {}).get("chat_index", 0) or 0)
                    ct = int((msg or {}).get("chats_total", 0) or 0)
                    saved = int((msg or {}).get("saved", 0) or 0)
                    if ci and ct:
                        text = f"чат {ci}/{ct} | сохранено {saved}"
                    else:
                        text = f"чат —/— | сохранено {saved}"
                    self.lbl_reg_progress.configure(text=text)
                elif tab == "smart" and hasattr(self, "lbl_sp_progress"):
                    ci = int((msg or {}).get("chat_index", 0) or 0)
                    ct = int((msg or {}).get("chats_total", 0) or 0)
                    mi = int((msg or {}).get("message_index", 0) or 0)
                    mt = int((msg or {}).get("messages_total", 0) or 0)
                    found = int((msg or {}).get("found", 0) or 0)
                    saved = int((msg or {}).get("saved", found) or 0)
                    if ci and ct:
                        chat_txt = f"чат {ci}/{ct}"
                    else:
                        chat_txt = "чат —/—"
                    if mt:
                        msg_txt = f"сообщение {mi}/{mt}"
                    elif mi:
                        msg_txt = f"сообщение {mi}"
                    else:
                        msg_txt = "сообщение —/—"
                    self.lbl_sp_progress.configure(text=f"{chat_txt} | {msg_txt} | найдено {found} | сохранено {saved}")
            except Exception:
                pass
        elif tag == "parsing_done":
            self._running = False
            self.btn_start.configure(state="normal", text="Начать парсинг")
            self.btn_smart_start.configure(state="normal", text="Начать смарт-парсинг")
            self.btn_stop.configure(state="disabled")
            self.btn_smart_stop.configure(state="disabled")
            self._refresh_stats()
        elif tag == "smart_parsing_done":
            self._running = False
            self.btn_start.configure(state="normal", text="Начать парсинг")
            self.btn_smart_start.configure(state="normal", text="Начать смарт-парсинг")
            self.btn_stop.configure(state="disabled")
            self.btn_smart_stop.configure(state="disabled")
            self._refresh_stats()
            if hasattr(self, "_smart_group"):
                self._refresh_smart_results(self._smart_group, getattr(self, "_smart_run_started_at", None))

    def _stop_parsing(self):
        if not self._running:
            return
        self._stop_event.set()
        self.btn_stop.configure(state="disabled")
        self.btn_smart_stop.configure(state="disabled")
        self.log.append("[~] Остановка парсинга запрошена...")


class AudiencesFrame(ctk.CTkFrame):
    """Раздел: Аудитории (таблица + DM-панель)"""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._running = False
        self._stop_event = threading.Event()
        self._dm_thread = None
        self._audiences = []  # кэш данных аудиторий

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(padx=20, pady=(12, 2), fill="x")
        ctk.CTkLabel(header, text="👥 Аудитории", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=("#2563EB", "#60A5FA")).pack(side="left")
        ctk.CTkLabel(header, text="  — сохранённые списки получателей для DM",
                     font=ctk.CTkFont(size=12), text_color=("gray50", "gray60")).pack(side="left", pady=4)

        # Верхняя панель с кнопкой импорта
        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.pack(padx=20, pady=5, fill="x")
        ctk.CTkButton(top_bar, text="Импорт CSV", width=120,
                       command=self._import_csv).pack(side="left")

        # Таблица аудиторий (вручную, т.к. нужны кнопки в ячейках)
        self.table_frame = ctk.CTkScrollableFrame(self, height=200)
        self.table_frame.pack(padx=20, pady=5, fill="both", expand=True)
        self._table_widgets = []

        # DM-панель (скрыта по умолчанию)
        self.dm_panel = ctk.CTkFrame(self)
        self._build_dm_panel()
        # Не pack — покажем при нажатии DM

        # Лог
        self.log = LogFrame(self, height=150)
        self.log.pack(padx=20, pady=(5, 10), fill="x")

    def on_show(self):
        self._refresh_table()

    # --- Таблица аудиторий ---

    def _refresh_table(self):
        # Очистка старых виджетов
        for widgets in self._table_widgets:
            for w in widgets:
                w.destroy()
        self._table_widgets.clear()

        # Заголовки
        headers = ["Группа", "Тип", "Кол-во", "Дата", "", "", "", ""]
        header_widgets = []
        for col, text in enumerate(headers):
            lbl = ctk.CTkLabel(self.table_frame, text=text,
                               font=ctk.CTkFont(weight="bold"), anchor="w")
            lbl.grid(row=0, column=col, padx=5, pady=(5, 2), sticky="w")
            self.table_frame.grid_columnconfigure(col, weight=1 if col == 0 else 0)
            header_widgets.append(lbl)
        self._table_widgets.append(header_widgets)

        db = Database(self.app.config.db_path)
        self._audiences = db.get_all_audiences()
        db.close()

        for idx, aud in enumerate(self._audiences):
            row = idx + 1
            group_lbl = ctk.CTkLabel(self.table_frame, text=aud["group_source"], anchor="w")
            group_lbl.grid(row=row, column=0, padx=5, pady=1, sticky="w")

            type_lbl = ctk.CTkLabel(self.table_frame, text=aud["audience_type"], anchor="w")
            type_lbl.grid(row=row, column=1, padx=5, pady=1, sticky="w")

            count_lbl = ctk.CTkLabel(self.table_frame, text=str(aud["count"]), anchor="w")
            count_lbl.grid(row=row, column=2, padx=5, pady=1, sticky="w")

            date_lbl = ctk.CTkLabel(self.table_frame, text=aud["last_date"][:10] if aud["last_date"] else "—",
                                     anchor="w")
            date_lbl.grid(row=row, column=3, padx=5, pady=1, sticky="w")

            csv_btn = ctk.CTkButton(
                self.table_frame, text="CSV", width=50, height=24,
                command=lambda a=aud: self._export_csv(a))
            csv_btn.grid(row=row, column=4, padx=2, pady=1)

            dm_btn = ctk.CTkButton(
                self.table_frame, text="DM", width=50, height=24,
                command=lambda a=aud: self._show_dm_panel(a))
            dm_btn.grid(row=row, column=5, padx=2, pady=1)

            delete_btn = ctk.CTkButton(
                self.table_frame,
                text="Удалить",
                width=70,
                height=24,
                fg_color="firebrick",
                hover_color="darkred",
                command=lambda a=aud: self._delete_audience(a),
            )
            delete_btn.grid(row=row, column=6, padx=2, pady=1)

            row_widgets = [group_lbl, type_lbl, count_lbl, date_lbl, csv_btn, dm_btn, delete_btn]

            # Кнопка "Упомянуть" только для аудиторий типа "users"
            if aud["audience_type"] == "users":
                mention_btn = ctk.CTkButton(
                    self.table_frame, text="Упомянуть", width=70, height=24,
                    command=lambda a=aud: self._go_to_mention(a))
                mention_btn.grid(row=row, column=7, padx=2, pady=1)
                row_widgets.append(mention_btn)

            self._table_widgets.append(row_widgets)

    # --- Экспорт CSV ---

    def _export_csv(self, audience: dict):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"{audience['group_source']}_{audience['audience_type']}.csv",
        )
        if not path:
            return

        db = Database(self.app.config.db_path)
        try:
            if audience["audience_type"] == "matched":
                rows = db.get_matched_posts_context(audience["group_source"])
            else:
                rows = db.get_audience_members(
                    audience["group_source"], audience["audience_type"])
        finally:
            db.close()

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if audience["audience_type"] == "matched":
                writer.writerow([
                    "user_id",
                    "username",
                    "source_chat",
                    "message_date",
                    "message_link",
                    "message_text",
                    "match_mode",
                    "matched_keywords",
                    "ai_reason",
                ])
                for row in rows:
                    u = (row.get("username") or "").strip()
                    if u and not u.startswith("@"):
                        u = "@" + u
                    writer.writerow([
                        row.get("user_id", ""),
                        u,
                        row.get("source_chat", ""),
                        row.get("message_date", ""),
                        row.get("message_link", ""),
                        row.get("message_text", ""),
                        row.get("match_mode", ""),
                        row.get("matched_keywords", ""),
                        row.get("ai_reason", ""),
                    ])
            else:
                writer.writerow(["user_id", "username", "first_name", "last_name"])
                for row in rows:
                    u = (row.get("username") or "").strip()
                    if u and not u.startswith("@"):
                        u = "@" + u
                    writer.writerow([row["user_id"], u, row["first_name"], row["last_name"]])

        self.log.append(f"[+] Экспорт CSV: {len(rows)} записей → {path}")

    def _delete_audience(self, audience: dict):
        group_source = audience.get("group_source", "")
        audience_type = audience.get("audience_type", "")
        count = int(audience.get("count", 0) or 0)
        ok = messagebox.askyesno(
            "Удалить аудиторию",
            f"Удалить аудиторию '{group_source}' ({audience_type}, {count} записей)?",
        )
        if not ok:
            return

        db = Database(self.app.config.db_path)
        try:
            deleted = db.delete_audience(group_source, audience_type)
        finally:
            db.close()

        self.log.append(f"[+] Удалена аудитория: {group_source} ({audience_type}), записей: {deleted}")
        self._refresh_table()

    # --- Импорт CSV ---

    def _import_csv(self):
        path = filedialog.askopenfilename(
            filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")])
        if not path:
            return

        # Диалог ввода group_source
        dialog = ctk.CTkInputDialog(
            text="Введите имя группы/источника для импорта:",
            title="Импорт аудитории")
        group_source = dialog.get_input()
        if not group_source:
            return

        from models import ParsedUser
        users = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    uid = row.get("user_id", "").strip()
                    if not uid or not uid.isdigit():
                        continue
                    users.append(ParsedUser(
                        user_id=int(uid),
                        username=(row.get("username", "").strip().lstrip("@") or None),
                        first_name=row.get("first_name", "").strip() or None,
                        last_name=row.get("last_name", "").strip() or None,
                        group_source=group_source.strip(),
                    ))
        except Exception as e:
            self.log.append(f"[!] Ошибка чтения CSV: {e}")
            return

        if users:
            db = Database(self.app.config.db_path)
            db.save_parsed_users(users)
            db.close()
            self.log.append(f"[+] Импорт: {len(users)} пользователей → {group_source}")
            self._refresh_table()
        else:
            self.log.append("[!] CSV не содержит валидных записей")

    # --- DM-панель ---

    def _build_dm_panel(self):
        panel = self.dm_panel

        self.dm_header = ctk.CTkLabel(panel, text="DM-рассылка",
                                       font=ctk.CTkFont(size=14, weight="bold"))
        self.dm_header.pack(padx=10, pady=(10, 5), anchor="w")

        form = ctk.CTkFrame(panel, fg_color="transparent")
        form.pack(padx=10, pady=5, fill="x")

        ctk.CTkLabel(form, text="Аккаунт:").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        self.dm_account_var = ctk.StringVar(value="")
        self.dm_account_menu = ctk.CTkOptionMenu(form, variable=self.dm_account_var, values=[""], width=250)
        self.dm_account_menu.grid(row=0, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(form, text="Сообщение:").grid(row=1, column=0, padx=5, pady=3, sticky="nw")
        self.dm_message = ctk.CTkTextbox(form, height=60, width=350)
        self.dm_message.grid(row=1, column=1, padx=5, pady=3, sticky="w")

        self.dm_dry_run = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(form, text="Dry Run", variable=self.dm_dry_run).grid(
            row=2, column=0, columnspan=2, padx=5, pady=5, sticky="w")

        btn_row = ctk.CTkFrame(panel, fg_color="transparent")
        btn_row.pack(padx=10, pady=5, fill="x")

        self.btn_dm_send = ctk.CTkButton(btn_row, text="Разослать DM", command=self._start_dm)
        self.btn_dm_send.pack(side="left", padx=(0, 10))

        self.btn_dm_stop = ctk.CTkButton(
            btn_row,
            text="Остановить DM",
            width=115,
            state="disabled",
            fg_color="firebrick",
            hover_color="darkred",
            command=self._stop_dm,
        )
        self.btn_dm_stop.pack(side="left", padx=(0, 10))

        ctk.CTkButton(btn_row, text="Закрыть", width=80, fg_color="gray40",
                       command=self._hide_dm_panel).pack(side="left")

    def _show_dm_panel(self, audience: dict):
        self._dm_audience = audience
        self.dm_header.configure(
            text=f"DM-рассылка: {audience['group_source']} ({audience['audience_type']}, {audience['count']})")

        # Обновить список аккаунтов
        db = Database(self.app.config.db_path)
        accounts = db.get_all_accounts()
        db.close()
        phones = [a.phone for a in accounts if a.is_active]
        if phones:
            self.dm_account_menu.configure(values=phones)
            self.dm_account_var.set(phones[0])
        else:
            self.dm_account_menu.configure(values=["Нет аккаунтов"])
            self.dm_account_var.set("Нет аккаунтов")

        self.dm_panel.pack(padx=20, pady=5, fill="x", before=self.log)

    def _go_to_mention(self, audience: dict):
        """Переключиться на раздел Задачи рассылки и подставить источник во вкладку Упоминания"""
        # Сначала показать фрейм — он может ещё не быть создан (lazy init)
        self.app._show_frame("broadcast")
        broadcast_frame = self.app.frames["broadcast"]
        broadcast_frame.m_source.delete(0, "end")
        broadcast_frame.m_source.insert(0, audience["group_source"])
        broadcast_frame.tabview.set("Упоминания")

    def _hide_dm_panel(self):
        self.dm_panel.pack_forget()

    def _start_dm(self):
        _log_action("audiences", "_start_dm")
        if self._running:
            return

        phone = self._resolve_phone(self.dm_account_var.get().strip())
        message = self.dm_message.get("1.0", "end").strip()

        if not phone or phone == "Нет аккаунтов":
            self.log.append("[!] Выберите аккаунт")
            return
        if not message:
            self.log.append("[!] Введите сообщение")
            return

        audience = self._dm_audience
        dry_run = self.dm_dry_run.get()

        self._stop_event.clear()
        self._running = True
        self.btn_dm_send.configure(state="disabled", text="Отправка...")
        self.btn_dm_stop.configure(state="normal")
        self.log.clear()

        def dm_thread():
            log_queue = self.app.log_queue
            _thread_local.log_handler = lambda msg: log_queue.put(("audiences_log", msg))
            _thread_local.log_tag = "audiences"

            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                from sender import TelegramSender
                from spintax import spin_text

                config = self.app.config
                db = Database(config.db_path)

                # Найти аккаунт
                accounts = db.get_all_accounts()
                account = None
                for a in accounts:
                    if a.phone == phone:
                        account = a
                        break

                if not account:
                    print(f"[!] Аккаунт {phone} не найден")
                    log_queue.put(("audiences_done", ""))
                    return

                sender = TelegramSender(account, config, db)

                async def run_dm():
                    from ads_database import AdsDB
                    from ads_scheduler import random_dm_delay_sec

                    stats = {"sent": 0, "private": 0, "flood_wait": 0, "banned": 0, "error": 0, "dry_run": 0}

                    async def _dm_wait(coro, label: str, timeout: float, target_value: str = ""):
                        try:
                            return await _await_interruptibly(
                                coro,
                                self._stop_event,
                                op_name="DM",
                                label=label,
                                timeout=timeout,
                                account=phone,
                                target=str(target_value or ""),
                            )
                        except asyncio.TimeoutError:
                            stats["error"] += 1
                            print(f"  [!] {label}: таймаут {timeout:.0f}с — пропуск")
                            return None

                    connected = await _dm_wait(sender.connect(), f"{phone}: подключение", 30)
                    if not connected:
                        print("[!] Не удалось подключиться")
                        return

                    # Загружаем настройки задержек (рандом min..max)
                    _adsdb = AdsDB(config.db_path)
                    try:
                        _settings = _adsdb.load_scheduler_settings()
                    finally:
                        _adsdb.close()

                    try:
                        members = db.get_audience_members(
                            audience["group_source"], audience["audience_type"])
                        mode = "DRY-RUN" if dry_run else "LIVE"
                        print(f"[*] Начинаю DM-рассылку: {len(members)} получателей, режим: {mode}")

                        for i, member in enumerate(members, 1):
                            _raise_if_stop_requested(
                                self._stop_event,
                                op_name="DM",
                                account=phone,
                                target=str(member.get("username") or member.get("user_id") or ""),
                                progress=f"получатель={i}/{len(members)}",
                            )
                            if not sender.can_send_more():
                                print("[!] Достигнут лимит сообщений на сессию")
                                break

                            spun_msg = spin_text(message)
                            target = member["username"] or member["user_id"]
                            if dry_run:
                                preview = spun_msg.replace("\n", " ").strip()
                                if len(preview) > 120:
                                    preview = preview[:120] + "…"
                                print(f"  [DRY] DM -> {target}: {preview}")
                                status = "dry_run"
                            else:
                                status = await _dm_wait(
                                    sender.send_dm(
                                        member["user_id"],
                                        member.get("username", ""),
                                        spun_msg,
                                        member.get("access_hash", 0),
                                    ),
                                    f"{target}: отправка DM",
                                    45,
                                    target,
                                )
                                if status is None:
                                    continue
                            stats[status] = stats.get(status, 0) + 1

                            # Терминальные статусы — выходим без паузы (ротация/стоп)
                            if status == "banned" or status == "flood_wait":
                                print(f"[!] Остановка DM: {status}")
                                break

                            # Пауза после любой попытки (sent / private / error / любой не-терминальный).
                            # Без паузы при ошибках Telegram видит шквал запросов = бот.
                            if i < len(members):
                                delay = random_dm_delay_sec(_settings)
                                if dry_run:
                                    print(f"  [DRY] Пауза {delay:.0f}с пропущена "
                                          f"(диапазон {_settings.dm_delay_min_seconds}-"
                                          f"{_settings.dm_delay_max_seconds}с)")
                                else:
                                    print(f"  [~] Пауза {delay:.0f}с (диапазон "
                                          f"{_settings.dm_delay_min_seconds}-"
                                          f"{_settings.dm_delay_max_seconds}с)...")
                                    await _sleep_interruptibly(
                                        delay,
                                        self._stop_event,
                                        op_name="DM",
                                        account=phone,
                                        target=str(target),
                                        progress=f"получатель={i}/{len(members)}",
                                    )

                        print(f"[*] DM завершена: sent={stats['sent']}, "
                              f"private={stats['private']}, error={stats['error']}, "
                              f"dry_run={stats['dry_run']}")
                    except OperationInterrupted as e:
                        print(str(e))
                        print(
                            f"[=] DM остановлена: аккаунт={phone} | sent={stats['sent']} "
                            f"| private={stats['private']} | errors={stats['error']} | dry_run={stats['dry_run']}"
                        )
                    finally:
                        try:
                            await asyncio.wait_for(sender.disconnect(), timeout=10)
                        except Exception as e:
                            print(f"[!] {phone}: disconnect не завершился быстро ({type(e).__name__})")

                _run_loop(loop, run_dm())
                db.close()

            except Exception as e:
                print(f"[!] Ошибка DM: {e}")
            finally:
                _thread_local.log_handler = None
                log_queue.put(("audiences_done", ""))

        dm_worker = threading.Thread(target=dm_thread, name="AudienceDMWorker", daemon=True)
        self._dm_thread = dm_worker
        dm_worker.start()

    def _stop_dm(self):
        thread = getattr(self, "_dm_thread", None)
        if not self._running and not (thread is not None and thread.is_alive()):
            return
        self._stop_event.set()
        try:
            self.btn_dm_send.configure(state="disabled", text="Останавливается...")
            self.btn_dm_stop.configure(state="disabled")
        except Exception:
            pass
        self.log.append("[~] Остановка DM запрошена...")

    def on_queue_message(self, tag, msg):
        if tag == "audiences_log":
            self.log.append(msg)
        elif tag == "audiences_done":
            self._running = False
            self._dm_thread = None
            self.btn_dm_send.configure(state="normal", text="Разослать DM")
            self.btn_dm_stop.configure(state="disabled")


class BroadcastFrame(ctk.CTkFrame):
    """Раздел: Задачи рассылки"""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._running = False
        self._stop_event = threading.Event()
        self._active_op_name = ""
        self._mention_thread = None
        self._broadcast_thread = None
        self._check_thread = None
        self._regular_run_seq = 0
        self._regular_run_ids: dict[str, int] = {}
        self._regular_stop_events: dict[str, threading.Event] = {}
        self._stop_watchdog_after_id = None
        self._cycle_runtime: dict[str, dict] = {}
        self._broadcast_status_after_id = None
        self._broadcast_dashboard_refresh_reset_after_id = None
        self._broadcast_dashboard_refresh_count = 0
        self._broadcast_ui_state = {
            "state": "idle",
            "current": "—",
            "last_success": [],
            "last_errors": [],
        }

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(padx=20, pady=(12, 2), fill="x")
        ctk.CTkLabel(header, text="📤 Задачи рассылки", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=("#2563EB", "#60A5FA")).pack(side="left")
        ctk.CTkLabel(header, text="  — упоминания, циклы, очереди и быстрые запуски",
                     font=ctk.CTkFont(size=12), text_color=("gray50", "gray60")).pack(side="left", pady=4)

        # === ЧЁТКИЕ МАССОВЫЕ КНОПКИ ВМЕСТЕ (вверху вкладки, всегда видимы) ===
        mass_ctrl = ctk.CTkFrame(self, fg_color=("#1F2937", "#111827"), corner_radius=8)
        mass_ctrl.pack(padx=20, pady=(4, 10), fill="x")

        ctk.CTkLabel(mass_ctrl, text="МАССОВОЕ УПРАВЛЕНИЕ РАССЫЛКАМИ", 
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=10, pady=6)

        self.mass_start_btn = ctk.CTkButton(mass_ctrl, text="🚀 ЗАПУСТИТЬ ВСЁ СРАЗУ", 
                                            fg_color=("#DC2626", "#B91C1C"),
                                            hover_color=("#991B1B", "#7F1D1D"),
                                            width=200, height=32,
                                            command=self._mass_start_everything)
        self.mass_start_btn.pack(side="left", padx=6, pady=4)

        self.mass_stop_btn = ctk.CTkButton(mass_ctrl, text="⏹ СТОП ВСЕГО", 
                                           fg_color=("#4B5563", "#374151"),
                                           hover_color=("#374151", "#1F2937"),
                                           width=140, height=32,
                                           command=self._mass_stop_everything)
        self.mass_stop_btn.pack(side="left", padx=4, pady=4)

        self._build_broadcast_status_panel(self, padx=20, pady=(0, 10))

        # Табы
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(padx=20, pady=5, fill="both", expand=True)

        self.tab_mention = self.tabview.add("Упоминания")
        self.tab_broadcast = self.tabview.add("Запуск задач")
        self.tab_tasks = self.tabview.add("Очередь")
        self.tab_cycle = self.tabview.add("Циклическая")

        self._build_mention_tab()
        self._build_broadcast_tab()
        self._build_tasks_tab()
        self._build_cycle_tab()
        self._refresh_broadcast_status_panel()
        self._schedule_broadcast_status_refresh()

        # Ensure helpers for account display and busy tracking are available on this frame
        # (they may have been defined in other frames; delegate to app or provide local)
        if not hasattr(self, '_resolve_phone'):
            def _resolve_phone_local(value: str) -> str:
                if not value or value in ("Все активные", "Нет аккаунтов"):
                    return value
                if hasattr(self, 'app') and hasattr(self.app, '_resolve_phone'):
                    return self.app._resolve_phone(value)
                # fallback: strip display name if present "Name (+79...)"
                if isinstance(value, str) and "(" in value and value.endswith(")"):
                    try:
                        return value.rsplit("(", 1)[-1].rstrip(")")
                    except:
                        pass
                return value
            self._resolve_phone = _resolve_phone_local

        # Общий лог + кнопка стопа (должны создаваться в __init__, а не внутри _mass_stop_everything)
        self.log = LogFrame(self, height=180)
        self.log.pack(padx=20, pady=(5, 10), fill="x")
        self.btn_stop_current = ctk.CTkButton(
            self,
            text="■ Остановить текущий процесс",
            state="disabled",
            fg_color="firebrick",
            hover_color="darkred",
            command=self._stop_current_process,
        )
        self.btn_stop_current.pack(padx=20, pady=(0, 10), anchor="w")
        try:
            self.after(10000, self._cycle_watchdog)
        except Exception:
            pass

    def _mass_start_everything(self):
        """Массовый запуск ВСЁ СРАЗУ.
        Запускает enabled циклы + обработчик задач рассылки (broadcast queue).
        Делает агрессивно: сбрасывает флаги и события, стартует основные раннеры.
        """
        self._append_log("[🚀 МАССОВЫЙ ЗАПУСК] Запуск всего массово...")

        started = []

        # Сброс только стопов (не _running — это важно, чтобы не ломать одиночные кнопки в под-табах)
        try:
            if hasattr(self, "_stop_event"):
                self._stop_event.clear()
            if hasattr(self, "_cycle_stop_event"):
                self._cycle_stop_event.clear()
        except Exception:
            pass

        # 1. Циклы — все enabled (основной "спам везде")
        try:
            if hasattr(self, "_cycle_start_enabled_campaigns"):
                before_cycles = self._cycle_active_count() if hasattr(self, "_cycle_active_count") else 0
                self._cycle_start_enabled_campaigns()
                after_cycles = self._cycle_active_count() if hasattr(self, "_cycle_active_count") else before_cycles
                if after_cycles > before_cycles:
                    started.append(f"циклы ({after_cycles - before_cycles} новых, всего {after_cycles})")
                elif hasattr(self, "_start_cycle"):
                    selected_cycle = (getattr(self, "_cycle_campaign_name", "") or "").strip()
                    if not selected_cycle and hasattr(self, "c_campaign_var"):
                        selected_cycle = (self.c_campaign_var.get() or "").strip()
                    if selected_cycle and selected_cycle != "—":
                        runner = self._cycle_get_runner(selected_cycle) if hasattr(self, "_cycle_get_runner") else None
                        if not (hasattr(self, "_cycle_runner_alive") and self._cycle_runner_alive(runner)):
                            self._append_log(
                                f"[🚀] Включённых циклов нет — запускаю текущую кампанию: {selected_cycle}"
                            )
                            self._cycle_campaign_name = selected_cycle
                            if hasattr(self, "c_campaign_var"):
                                try:
                                    self.c_campaign_var.set(selected_cycle)
                                except Exception:
                                    pass
                            before_selected = self._cycle_active_count() if hasattr(self, "_cycle_active_count") else 0
                            self._start_cycle()
                            after_selected = self._cycle_active_count() if hasattr(self, "_cycle_active_count") else before_selected
                            if after_selected > before_selected:
                                started.append(f"текущий цикл ({selected_cycle})")
        except Exception as e:
            self._append_log(f"[!] Циклы: {e}")

        # 2. Broadcast задачи (очередь) — если есть pending/waiting
        try:
            db = Database(self.app.config.db_path)
            pending = db.get_pending_tasks(task_type="broadcast")
            db.close()
            if pending:
                if getattr(self, "_running", False) or self._regular_worker_alive():
                    self._append_log(
                        f"[🚀] Очередь рассылки готова ({len(pending)}), но уже работает другой обычный процесс — не запускаю второй поверх."
                    )
                else:
                    self._start_broadcast()
                    if self._worker_alive("_broadcast_thread"):
                        started.append(f"задачи рассылки ({len(pending)})")
        except Exception as e:
            self._append_log(f"[!] Задачи рассылки: {e}")

        # Упоминания можно запускать отдельно из своей под-вкладки.

        # 4. Упоминания (если кнопка активна / данные есть)
        try:
            if hasattr(self, "_start_mention"):
                if getattr(self, "_running", False) or self._regular_worker_alive():
                    self._append_log("[🚀] Упоминания не запущены: уже работает очередь/проверка/другой обычный процесс.")
                else:
                    self._start_mention()
                    if self._worker_alive("_mention_thread"):
                        started.append("упоминания")
        except Exception as e:
            self._append_log(f"[!] Упоминания: {e}")

        if started:
            self._append_log(f"[🚀] Массово запущено: {', '.join(started)}. Следи за табами и логом.")
        else:
            self._append_log("[🚀] Массовый запуск: нечего запускать (нет enabled циклов / pending задач / конфигурации).")

    def _mass_stop_everything(self):
        """Массовый стоп всего, что может работать: парсинг, упоминания, broadcast, циклы и т.д."""
        self._append_log("[⏹] МАССОВЫЙ СТОП ВСЕГО — останавливаем все активные процессы...")

        stopped = []

        # 1. Остановить broadcast / mention / check в этом фрейме.
        try:
            if hasattr(self, "_set_all_regular_stop_events"):
                self._set_all_regular_stop_events()
                stopped.append("broadcast/mention/check")
        except Exception:
            pass

        # 2. Дополнительно: если есть btn_stop_current — нажмём его логику (если метод есть).
        try:
            if hasattr(self, "_stop_current_process"):
                self._stop_current_process()
                stopped.append("текущий процесс")
        except Exception:
            pass

        # 3. Остановить циклы (используем существующий _stop_cycle).
        try:
            runners = getattr(self, "_cycle_runners", None) or {}
            active_cycle_names = [name for name, runner in runners.items() if self._cycle_runner_alive(runner)]
            if active_cycle_names:
                for name in active_cycle_names:
                    self._stop_cycle(name)
                stopped.append(f"циклы ({len(active_cycle_names)})")
            elif hasattr(self, "_stop_cycle"):
                self._stop_cycle()
            if hasattr(self, "_cycle_stop_event"):
                self._cycle_stop_event.set()
        except Exception:
            pass

        # 4. Остановить уже созданные соседние вкладки, если там есть активная работа.
        frames = getattr(getattr(self, "app", None), "frames", {}) or {}

        try:
            parsing_frame = frames.get("parsing")
            if parsing_frame is not None and getattr(parsing_frame, "_running", False):
                parsing_frame._stop_parsing()
                stopped.append("парсинг")
        except Exception as e:
            self._append_log(f"[!] Parsing stop: {e}")

        try:
            audiences_frame = frames.get("audiences")
            audience_thread = getattr(audiences_frame, "_dm_thread", None) if audiences_frame is not None else None
            if audiences_frame is not None and (
                getattr(audiences_frame, "_running", False)
                or (audience_thread is not None and audience_thread.is_alive())
            ):
                audiences_frame._stop_dm()
                stopped.append("DM")
        except Exception as e:
            self._append_log(f"[!] DM stop: {e}")

        try:
            channel_frame = frames.get("channel_commenter")
            if channel_frame is not None:
                old_btn = getattr(channel_frame, "btn_old_stop", None)
                new_btn = getattr(channel_frame, "btn_new_stop", None)
                old_enabled = bool(old_btn is not None and old_btn.cget("state") != "disabled")
                new_enabled = bool(new_btn is not None and new_btn.cget("state") != "disabled")
                if old_enabled:
                    channel_frame._stop_old()
                    stopped.append("комментинг старых постов")
                if new_enabled or getattr(channel_frame, "_listener", None) is not None:
                    channel_frame._stop_new()
                    stopped.append("комментинг новых постов")
        except Exception as e:
            self._append_log(f"[!] Channel commenter stop: {e}")

        try:
            autoreply_frame = frames.get("autoreply")
            autoreply_thread = getattr(autoreply_frame, "_thread", None) if autoreply_frame is not None else None
            if autoreply_frame is not None and (
                getattr(autoreply_frame, "_listener", None) is not None
                or (autoreply_thread is not None and autoreply_thread.is_alive())
            ):
                autoreply_frame._stop()
                stopped.append("автоответчик")
        except Exception as e:
            self._append_log(f"[!] AutoReply stop: {e}")

        # 5. Остановить ads-планировщики из отдельного реестра ads_gui.py.
        try:
            from ads_gui import stop_all_ads_schedulers
            ads_stopped = stop_all_ads_schedulers(self._append_log)
            if ads_stopped:
                stopped.append(f"ads scheduler ({ads_stopped})")
        except Exception as e:
            self._append_log(f"[!] Ads scheduler stop: {e}")

        if stopped:
            self._append_log(f"[⏹] Остановка запрошена: {', '.join(dict.fromkeys(stopped))}. Жду завершения активных операций...")
        else:
            self._append_log("[⏹] Ничего активного не нашли для останова (или уже остановлено).")

        if not hasattr(self, '_format_account'):
            self._format_account = lambda p, n="": format_account(p, n) if 'format_account' in globals() else str(p)

        if not hasattr(self, 'mark_account_busy'):
            def _mark_busy(phones, ctx):
                if hasattr(self, 'app') and hasattr(self.app, 'mark_account_busy'):
                    self.app.mark_account_busy(phones, ctx)
            self.mark_account_busy = _mark_busy

        if not hasattr(self, 'mark_account_free'):
            def _mark_free(phones):
                if hasattr(self, 'app') and hasattr(self.app, 'mark_account_free'):
                    self.app.mark_account_free(phones)
            self.mark_account_free = _mark_free

        if not hasattr(self, 'get_busy_accounts'):
            def _get_busy():
                if hasattr(self, 'app') and hasattr(self.app, 'get_busy_accounts'):
                    return self.app.get_busy_accounts()
                return {}
            self.get_busy_accounts = _get_busy

    def _append_log(self, text: str):
        """Безопасный логгер BroadcastFrame.
        Никогда не падает на AttributeError (нет self.log) или других ошибках записи.
        Использует fallback в файл, если LogFrame ещё не создан (ранний __init__ / on_show).
        """
        try:
            if getattr(self, "log", None) is not None:
                self.log.append(text)
            else:
                log_to_file("broadcast", text)
        except Exception:
            try:
                log_to_file("broadcast", f"[log-err] {text}")
            except Exception:
                pass

    def _build_broadcast_status_panel(self, master, padx=10, pady=(4, 10)):
        status_panel = ctk.CTkFrame(master, fg_color=("#F8FAFC", "#111827"), corner_radius=8)
        status_panel.pack(padx=padx, pady=pady, fill="x")
        status_panel.grid_columnconfigure(1, weight=1)
        status_panel.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(status_panel, text="Состояние очереди", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, padx=10, pady=(8, 3), sticky="w"
        )
        self.lbl_broadcast_state = ctk.CTkLabel(status_panel, text="Не запущено", text_color="gray60")
        self.lbl_broadcast_state.grid(row=0, column=1, padx=8, pady=(8, 3), sticky="w")
        self.lbl_broadcast_counts = ctk.CTkLabel(status_panel, text="—", text_color="gray60")
        self.lbl_broadcast_counts.grid(row=0, column=2, columnspan=2, padx=8, pady=(8, 3), sticky="e")

        ctk.CTkLabel(status_panel, text="Текущая:", font=ctk.CTkFont(weight="bold")).grid(
            row=1, column=0, padx=10, pady=3, sticky="w"
        )
        self.lbl_broadcast_current = ctk.CTkLabel(status_panel, text="—", anchor="w")
        self.lbl_broadcast_current.grid(row=1, column=1, columnspan=3, padx=8, pady=3, sticky="ew")

        ctk.CTkLabel(status_panel, text="Следующая:", font=ctk.CTkFont(weight="bold")).grid(
            row=2, column=0, padx=10, pady=3, sticky="w"
        )
        self.lbl_broadcast_next = ctk.CTkLabel(status_panel, text="—", anchor="w")
        self.lbl_broadcast_next.grid(row=2, column=1, columnspan=3, padx=8, pady=3, sticky="ew")

        ctk.CTkLabel(status_panel, text="Успешные:", font=ctk.CTkFont(weight="bold")).grid(
            row=3, column=0, padx=10, pady=3, sticky="w"
        )
        self.lbl_broadcast_success = ctk.CTkLabel(status_panel, text="—", anchor="w", text_color=("#166534", "#86EFAC"))
        self.lbl_broadcast_success.grid(row=3, column=1, columnspan=3, padx=8, pady=3, sticky="ew")

        ctk.CTkLabel(status_panel, text="Ошибки:", font=ctk.CTkFont(weight="bold")).grid(
            row=4, column=0, padx=10, pady=(3, 8), sticky="w"
        )
        self.lbl_broadcast_errors = ctk.CTkLabel(status_panel, text="—", anchor="w", text_color=("#991B1B", "#FCA5A5"))
        self.lbl_broadcast_errors.grid(row=4, column=1, columnspan=2, padx=8, pady=(3, 4), sticky="ew")
        self.btn_broadcast_dashboard_refresh = ctk.CTkButton(
            status_panel,
            text="Обновить",
            width=95,
            command=self._refresh_broadcast_dashboard,
        )
        self.btn_broadcast_dashboard_refresh.grid(row=4, column=3, padx=8, pady=(3, 4), sticky="e")
        self.lbl_broadcast_refreshed = ctk.CTkLabel(
            status_panel,
            text="Обновлено: —",
            text_color="gray55",
            anchor="e",
            font=ctk.CTkFont(size=11),
        )
        self.lbl_broadcast_refreshed.grid(row=5, column=1, columnspan=3, padx=8, pady=(0, 8), sticky="e")

    def _build_mention_tab(self):
        tab = self.tab_mention

        form = ctk.CTkFrame(tab, fg_color="transparent")
        form.pack(padx=10, pady=5, fill="x")

        ctk.CTkLabel(form, text="Аккаунт:").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        self.m_account_var = ctk.StringVar(value="Все активные")
        self.m_account_menu = ctk.CTkOptionMenu(form, variable=self.m_account_var,
                                                 values=["Все активные"], width=220)
        self.m_account_menu.grid(row=0, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(form, text="Целевая группа:").grid(row=1, column=0, padx=5, pady=3, sticky="w")
        self.m_target = ctk.CTkEntry(form, placeholder_text="@target", width=220)
        self.m_target.grid(row=1, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(form, text="Источник:").grid(row=2, column=0, padx=5, pady=3, sticky="w")
        self.m_source = ctk.CTkEntry(form, placeholder_text="@source", width=220)
        self.m_source.grid(row=2, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(form, text="Лимит:").grid(row=3, column=0, padx=5, pady=3, sticky="w")
        self.m_limit = ctk.CTkEntry(form, placeholder_text="0 (без лимита)", width=120)
        self.m_limit.grid(row=3, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(form, text="Упоминаний/сообщение:").grid(row=4, column=0, padx=5, pady=3, sticky="w")
        self.m_per_msg = ctk.CTkEntry(form, placeholder_text="5", width=120)
        self.m_per_msg.grid(row=4, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(form, text="Сообщение:").grid(row=5, column=0, padx=5, pady=3, sticky="nw")
        self.m_message = ctk.CTkTextbox(form, height=60, width=300)
        self.m_message.grid(row=5, column=1, padx=5, pady=3, sticky="w")

        # Источник текста
        ctk.CTkLabel(form, text="Источник текста:").grid(row=6, column=0, padx=5, pady=3, sticky="w")
        self.m_source_var = ctk.StringVar(value="Вручную")
        src_row_m = ctk.CTkFrame(form, fg_color="transparent")
        src_row_m.grid(row=6, column=1, padx=5, pady=3, sticky="w")
        ctk.CTkRadioButton(src_row_m, text="Вручную", variable=self.m_source_var,
                           value="Вручную", command=self._toggle_m_text).pack(side="left", padx=(0,10))
        ctk.CTkRadioButton(src_row_m, text="Из Избранного", variable=self.m_source_var,
                           value="Избранное", command=self._toggle_m_text).pack(side="left", padx=(0,10))
        ctk.CTkRadioButton(src_row_m, text="Шаблоны", variable=self.m_source_var,
                           value="Шаблоны", command=self._toggle_m_text).pack(side="left")

        # Уникализация
        ctk.CTkLabel(form, text="Уникализация:").grid(row=7, column=0, padx=5, pady=3, sticky="w")
        self.m_unique_var = ctk.StringVar(value="Оригинал")
        ctk.CTkSegmentedButton(form, values=["Оригинал","Спинтакс","Омоглифы","AI"],
                               variable=self.m_unique_var, width=340).grid(
            row=7, column=1, padx=5, pady=3, sticky="w")

        self.m_dry_run = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(form, text="Dry Run", variable=self.m_dry_run).grid(
            row=8, column=0, columnspan=2, padx=5, pady=5, sticky="w")

        self.btn_mention = ctk.CTkButton(tab, text="Начать упоминания", command=self._start_mention)
        self.btn_mention.pack(padx=10, pady=10, anchor="w")

    def _build_broadcast_tab(self):
        tab = self.tab_broadcast

        ctk.CTkLabel(tab, text="Запуск очереди задач рассылки (из базы данных)",
                      font=ctk.CTkFont(size=14)).pack(padx=10, pady=15, anchor="w")
        ctk.CTkLabel(tab, text="Это не ручной режим: используются задачи типа 'broadcast' со статусом 'Ожидает'.",
                      text_color="gray").pack(padx=10, anchor="w")
        ctk.CTkLabel(tab, text="Список целей редактируется во вкладке «Очередь».",
                     text_color="gray").pack(padx=10, pady=(0, 8), anchor="w")

        acc_row = ctk.CTkFrame(tab, fg_color="transparent")
        acc_row.pack(padx=10, pady=(10, 0), anchor="w")
        ctk.CTkLabel(acc_row, text="Аккаунт:").pack(side="left", padx=(0, 8))
        self.b_account_var = ctk.StringVar(value="Все активные")
        self.b_account_menu = ctk.CTkOptionMenu(acc_row, variable=self.b_account_var,
                                                 values=["Все активные"], width=220)
        self.b_account_menu.pack(side="left")

        # Источник текста
        src_row_b = ctk.CTkFrame(tab, fg_color="transparent")
        src_row_b.pack(padx=10, pady=(8,0), anchor="w")
        ctk.CTkLabel(src_row_b, text="Источник текста:").pack(side="left", padx=(0,8))
        self.b_source_var = ctk.StringVar(value="Задача")
        ctk.CTkRadioButton(src_row_b, text="Из задачи", variable=self.b_source_var,
                           value="Задача").pack(side="left", padx=(0,10))
        ctk.CTkRadioButton(src_row_b, text="Из Избранного", variable=self.b_source_var,
                           value="Избранное").pack(side="left", padx=(0,10))
        ctk.CTkRadioButton(src_row_b, text="Строки задачи", variable=self.b_source_var,
                           value="Шаблоны").pack(side="left")

        # Уникализация
        uniq_row_b = ctk.CTkFrame(tab, fg_color="transparent")
        uniq_row_b.pack(padx=10, pady=(6,0), anchor="w")
        ctk.CTkLabel(uniq_row_b, text="Уникализация:").pack(side="left", padx=(0,8))
        self.b_unique_var = ctk.StringVar(value="Оригинал")
        ctk.CTkSegmentedButton(uniq_row_b, values=["Оригинал","Спинтакс","Омоглифы","AI"],
                               variable=self.b_unique_var).pack(side="left")

        self.b_dry_run = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Dry Run", variable=self.b_dry_run).pack(padx=10, pady=10, anchor="w")

        self.btn_broadcast = ctk.CTkButton(tab, text="Запустить задачи", command=self._start_broadcast)
        self.btn_broadcast.pack(padx=10, pady=10, anchor="w")

        ctk.CTkButton(tab, text="Импорт групп из CSV", command=self._import_groups_csv).pack(
            padx=10, pady=5, anchor="w")

    def _build_tasks_tab(self):
        tab = self.tab_tasks
        ctk.CTkLabel(tab, text="Очередь задач для рассылки",
                     font=ctk.CTkFont(size=14)).pack(padx=10, pady=(15, 6), anchor="w")
        ctk.CTkLabel(
            tab,
            text="Эти задачи используются вкладкой «Запуск задач» (тип broadcast).",
            text_color="gray",
        ).pack(padx=10, anchor="w")
        self._tasks_embed = TasksFrame(tab, self.app, embed=True)
        self._tasks_embed.pack(fill="both", expand=True, padx=0, pady=(6, 0))
        ctk.CTkButton(tab, text="Импорт групп из шаблона", command=self._import_groups_template).pack(
            padx=10, pady=5, anchor="w")

        self.btn_check = ctk.CTkButton(tab, text="Проверить и очистить", command=self._check_and_clean)
        self.btn_check.pack(padx=10, pady=5, anchor="w")
        self.check_dry_run = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Dry Run для проверки/вступления", variable=self.check_dry_run).pack(
            padx=10, pady=(0, 10), anchor="w")

    def _build_cycle_tab(self):
        tab = self.tab_cycle

        self._cycle_running = False
        self._cycle_stop_event = threading.Event()
        self._cycle_thread = None
        self._cycle_runners: dict[str, dict] = {}
        self._cycle_stop_requested_at = None
        self._cycle_running_campaign_name = ""
        self._cycle_running_campaign_id = None
        self._cycle_campaign_name = "CycleBroadcast"
        self._cycle_campaign_by_name = {}
        self._cycle_campaign_accounts: list[str] = []
        self._cycle_targets = []
        self._cycle_template_by_name = {}
        self._cycle_message_template_by_name = {}

        # Use a scrollable content area so that with many rules/campaigns/controls
        # everything remains visible and scrollable instead of being cut off (fixes "не все видно" and glitching on narrow/tall windows).
        content = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=5, pady=5)

        ctk.CTkLabel(content, text="Циклическая рассылка (постоянно по кругу)",
                      font=ctk.CTkFont(size=14)).pack(padx=10, pady=(15, 10), anchor="w")

        top = ctk.CTkFrame(content, fg_color="transparent")
        top.pack(padx=10, pady=5, fill="x")

        ctk.CTkLabel(top, text="Кампания:").grid(row=0, column=0, padx=5, pady=4, sticky="w")
        self.c_campaign_var = ctk.StringVar(value=self._cycle_campaign_name)
        self.c_campaign_menu = ctk.CTkOptionMenu(
            top,
            variable=self.c_campaign_var,
            values=[self._cycle_campaign_name],
            width=260,
            command=self._cycle_on_campaign_change,
        )
        self.c_campaign_menu.grid(row=0, column=1, padx=5, pady=4, sticky="w")
        ctk.CTkButton(top, text="Создать", width=90, command=self._cycle_create_campaign).grid(
            row=0, column=2, padx=5, pady=4, sticky="w"
        )
        ctk.CTkButton(top, text="Переимен.", width=90, command=self._cycle_rename_campaign).grid(
            row=0, column=3, padx=5, pady=4, sticky="w"
        )
        ctk.CTkButton(top, text="Удалить", width=90, fg_color="firebrick", hover_color="darkred",
                      command=self._cycle_delete_campaign).grid(
            row=0, column=4, padx=5, pady=4, sticky="w"
        )

        ctk.CTkLabel(top, text="Аккаунт:").grid(row=1, column=0, padx=5, pady=4, sticky="w")
        self.c_account_var = ctk.StringVar(value="Все активные")
        self.c_account_menu = ctk.CTkOptionMenu(
            top, variable=self.c_account_var, values=["Все активные"], width=220)
        self.c_account_menu.grid(row=1, column=1, padx=5, pady=4, sticky="w")
        # friendly names will be set on first _refresh_accounts + _refresh_cycle... calls
        ctk.CTkButton(top, text="Аккаунты…", width=120, command=self._cycle_edit_campaign_accounts).grid(
            row=1, column=2, padx=5, pady=4, sticky="w"
        )
        ctk.CTkButton(top, text="Очистить", width=100, fg_color="gray40", hover_color="gray30",
                      command=self._cycle_clear_campaign_accounts).grid(
            row=1, column=3, padx=5, pady=4, sticky="w"
        )
        self.lbl_cycle_accounts = ctk.CTkLabel(top, text="Аккаунты кампании: —", text_color="gray70")
        self.lbl_cycle_accounts.grid(row=1, column=4, padx=5, pady=4, sticky="w")

        ctk.CTkLabel(top, text="Цели:").grid(row=2, column=0, padx=5, pady=4, sticky="w")
        self.c_targets_source_var = ctk.StringVar(value="Шаблон")
        src_row = ctk.CTkFrame(top, fg_color="transparent")
        src_row.grid(row=2, column=1, padx=5, pady=4, sticky="w")
        ctk.CTkRadioButton(src_row, text="Из шаблона", variable=self.c_targets_source_var,
                           value="Шаблон", command=self._toggle_cycle_targets_source).pack(
            side="left", padx=(0, 10))
        ctk.CTkRadioButton(src_row, text="Из базы задач", variable=self.c_targets_source_var,
                           value="База", command=self._toggle_cycle_targets_source).pack(side="left")

        ctk.CTkLabel(top, text="Шаблон:").grid(row=3, column=0, padx=5, pady=4, sticky="w")
        self.c_template_var = ctk.StringVar(value="—")
        self.c_template_menu = ctk.CTkOptionMenu(
            top, variable=self.c_template_var, values=["—"], width=260)
        self.c_template_menu.grid(row=3, column=1, padx=5, pady=4, sticky="w")

        ctk.CTkButton(top, text="Загрузить цели", width=140,
                      command=self._cycle_load_targets).grid(row=3, column=2, padx=10, pady=4, sticky="w")

        rules = ctk.CTkFrame(content, fg_color="transparent")
        rules.pack(padx=10, pady=(10, 0), fill="x")

        ctk.CTkLabel(rules, text="Правила по умолчанию:", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=5, pady=(0, 6))

        rules_row1 = ctk.CTkFrame(rules, fg_color="transparent")
        rules_row1.pack(fill="x", padx=0, pady=0)

        ctk.CTkLabel(rules_row1, text="Часы start:").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        self.c_hours_start = ctk.CTkEntry(rules_row1, width=70, placeholder_text="0")
        self.c_hours_start.grid(row=0, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(rules_row1, text="Часы end:").grid(row=0, column=2, padx=5, pady=3, sticky="w")
        self.c_hours_end = ctk.CTkEntry(rules_row1, width=70, placeholder_text="23")
        self.c_hours_end.grid(row=0, column=3, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(rules_row1, text="Интервал min (с):").grid(row=0, column=4, padx=5, pady=3, sticky="w")
        self.c_int_min = ctk.CTkEntry(rules_row1, width=70, placeholder_text="30")
        self.c_int_min.grid(row=0, column=5, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(rules_row1, text="Интервал max (с):").grid(row=0, column=6, padx=5, pady=3, sticky="w")
        self.c_int_max = ctk.CTkEntry(rules_row1, width=70, placeholder_text="90")
        self.c_int_max.grid(row=0, column=7, padx=5, pady=3, sticky="w")

        rules_row2 = ctk.CTkFrame(rules, fg_color="transparent")
        rules_row2.pack(fill="x", padx=0, pady=0)

        ctk.CTkLabel(rules_row2, text="Мин. новых:").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        self.c_min_new = ctk.CTkEntry(rules_row2, width=70, placeholder_text="0")
        self.c_min_new.grid(row=0, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(rules_row2, text="Запасной лимит (ч):").grid(row=0, column=2, padx=5, pady=3, sticky="w")
        self.c_fallback_hours = ctk.CTkEntry(rules_row2, width=70, placeholder_text="0")
        self.c_fallback_hours.grid(row=0, column=3, padx=5, pady=3, sticky="w")

        rules_row3 = ctk.CTkFrame(rules, fg_color="transparent")
        rules_row3.pack(fill="x", padx=0, pady=0)

        ctk.CTkLabel(rules_row3, text="Пауза send min (с):").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        self.c_send_delay_min = ctk.CTkEntry(rules_row3, width=70, placeholder_text="30")
        self.c_send_delay_min.grid(row=0, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(rules_row3, text="Пауза send max (с):").grid(row=0, column=2, padx=5, pady=3, sticky="w")
        self.c_send_delay_max = ctk.CTkEntry(rules_row3, width=70, placeholder_text="90")
        self.c_send_delay_max.grid(row=0, column=3, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(rules_row3, text="Пауза круг (с):").grid(row=0, column=4, padx=5, pady=3, sticky="w")
        self.c_round_pause = ctk.CTkEntry(rules_row3, width=70, placeholder_text="0")
        self.c_round_pause.grid(row=0, column=5, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(rules_row3, text="Ротация после N:").grid(row=0, column=6, padx=5, pady=3, sticky="w")
        self.c_rotate_after_n = ctk.CTkEntry(rules_row3, width=70, placeholder_text="0")
        self.c_rotate_after_n.grid(row=0, column=7, padx=5, pady=3, sticky="w")

        rules_row4 = ctk.CTkFrame(rules, fg_color="transparent")
        rules_row4.pack(fill="x", padx=0, pady=0)
        ctk.CTkLabel(rules_row4, text="Дневной лимит действий (0 = выкл):").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        self.c_daily_limit = ctk.CTkEntry(rules_row4, width=90, placeholder_text="0")
        self.c_daily_limit.grid(row=0, column=1, padx=5, pady=3, sticky="w")

        msg = ctk.CTkFrame(content, fg_color="transparent")
        msg.pack(padx=10, pady=(12, 0), fill="x")
        msg.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(msg, text="Источник текста:").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        self.c_message_source_var = ctk.StringVar(value="Вручную")
        ms_row = ctk.CTkFrame(msg, fg_color="transparent")
        ms_row.grid(row=0, column=1, padx=5, pady=3, sticky="w")
        ctk.CTkRadioButton(ms_row, text="Вручную", variable=self.c_message_source_var,
                           value="Вручную", command=self._toggle_cycle_text).pack(side="left", padx=(0, 10))
        ctk.CTkRadioButton(ms_row, text="Шаблоны", variable=self.c_message_source_var,
                           value="Шаблоны", command=self._toggle_cycle_text).pack(side="left", padx=(0, 10))
        ctk.CTkRadioButton(ms_row, text="Из Избранного", variable=self.c_message_source_var,
                           value="Избранное", command=self._toggle_cycle_text).pack(side="left")

        ctk.CTkLabel(msg, text="Текстовый шаблон:").grid(row=1, column=0, padx=5, pady=3, sticky="w")
        self.c_message_template_var = ctk.StringVar(value="—")
        self.c_message_template_menu = ctk.CTkOptionMenu(
            msg,
            variable=self.c_message_template_var,
            values=["—"],
            width=260,
            command=self._cycle_on_message_template_change,
        )
        self.c_message_template_menu.grid(row=1, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(msg, text="Текст / шаблоны (по строкам):").grid(
            row=2, column=0, padx=5, pady=3, sticky="nw")
        self.c_message = ctk.CTkTextbox(msg, height=80)
        self.c_message.grid(row=2, column=1, padx=5, pady=3, sticky="ew")

        ctk.CTkLabel(msg, text="Уникализация:").grid(row=3, column=0, padx=5, pady=3, sticky="w")
        self.c_unique_var = ctk.StringVar(value="Оригинал")
        ctk.CTkSegmentedButton(msg, values=["Оригинал", "Спинтакс", "Омоглифы", "AI"],
                               variable=self.c_unique_var).grid(
            row=3, column=1, padx=5, pady=3, sticky="w")

        btns = ctk.CTkFrame(content, fg_color="transparent")
        btns.pack(padx=10, pady=10, fill="x")
        btns.grid_columnconfigure(8, weight=1)
        self.btn_cycle_start = ctk.CTkButton(btns, text="▶ Старт", width=140, command=self._start_cycle)
        self.btn_cycle_start.grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.btn_cycle_stop = ctk.CTkButton(btns, text="■ Стоп", width=140,
                                            state="disabled", fg_color="firebrick",
                                            hover_color="darkred", command=self._stop_cycle)
        self.btn_cycle_stop.grid(row=0, column=1, padx=(0, 8), sticky="w")
        self.c_dry_run = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(btns, text="Dry Run", variable=self.c_dry_run).grid(row=0, column=2, padx=(8, 0), sticky="w")
        ctk.CTkButton(btns, text="↻ Обновить список", width=160,
                      command=self._cycle_refresh_table).grid(row=0, column=3, padx=(8, 0), sticky="w")
        ctk.CTkButton(btns, text="Правила чата", width=140,
                      command=self._cycle_edit_selected).grid(row=0, column=8, padx=(8, 0), sticky="e")

        self.btn_cycle_start_enabled = ctk.CTkButton(
            btns,
            text="▶ Включённые",
            width=150,
            command=self._cycle_start_enabled_campaigns,
        )
        self.btn_cycle_start_enabled.grid(row=0, column=4, padx=(12, 0), sticky="w")

        # NEW: one-click button to stop/disable ALL cyclic campaigns (as requested).
        # This stops runners and sets enabled=False for all, so nothing keeps sending in background.
        ctk.CTkButton(
            btns,
            text="⏹ Выключить ВСЕ кампании",
            fg_color="#8B0000",
            hover_color="#5C0000",
            width=200,
            command=self._disable_all_cycle_campaigns,
        ).grid(row=0, column=5, padx=(16, 0), sticky="w")

        status_bar = ctk.CTkFrame(content, fg_color="transparent")
        status_bar.pack(padx=10, pady=(0, 4), fill="x")
        self.lbl_cycle_status = ctk.CTkLabel(
            status_bar,
            text="Статус: остановлен",
            text_color="gray70",
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self.lbl_cycle_status.pack(side="left", fill="x", expand=True)
        self._cycle_status_after_id = None
        self._cycle_watchdog_after_id = None
        self._cycle_last_watchdog_log_at = 0.0
        self._cycle_ui_busy = False
        self._cycle_busy = False  # protects UI from rapid clicks + background updates causing "floating" layout and "not responding" on weak hardware

        # Compact cycle dashboard + main targets table.
        self._cycle_metric_labels = {}
        cycle_metrics = ctk.CTkFrame(content, fg_color="transparent")
        cycle_metrics.pack(padx=10, pady=(0, 6), fill="x")
        for col in range(9):
            cycle_metrics.grid_columnconfigure(col, weight=1)

        metric_defs = [
            ("status", "Статус", "остановлен"),
            ("campaigns", "Кампаний", "0"),
            ("position", "Позиция", "—"),
            ("targets", "Целей", "0"),
            ("active", "Активных", "0"),
            ("waiting", "Ожидание", "0"),
            ("target_errors", "Ошибок целей", "0"),
            ("sent", "Sent", "0"),
            ("err", "Err", "0"),
        ]
        for col, (key, title, value) in enumerate(metric_defs):
            card = ctk.CTkFrame(cycle_metrics, corner_radius=6)
            card.grid(row=0, column=col, padx=(0 if col == 0 else 4, 0), sticky="ew")
            ctk.CTkLabel(
                card,
                text=title,
                text_color="gray65",
                font=ctk.CTkFont(size=10),
                anchor="w",
            ).pack(padx=8, pady=(5, 0), anchor="w")
            value_label = ctk.CTkLabel(
                card,
                text=value,
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w",
            )
            value_label.pack(padx=8, pady=(0, 6), anchor="w", fill="x")
            self._cycle_metric_labels[key] = value_label

        self.lbl_cycle_summary = ctk.CTkLabel(
            content,
            text="Кампания: — | Текущая цель: — | Следующая цель: — | Аккаунт: — | Успех: — | Следующая попытка: — | Ошибка цели: —",
            text_color="#A9D6FF",
            anchor="w",
            justify="left",
            wraplength=980,
        )
        self.lbl_cycle_summary.pack(padx=10, pady=(0, 8), anchor="w", fill="x")

        self.c_table = ScrollableTable(content, columns=[
            "Чат", "Часы", "Интервал", "Новых", "Часов", "Статус", "Retry", "Последнее", "Аккаунт", "Очередь"])
        self.c_table.pack(padx=10, pady=(0, 10), fill="both", expand=True)
        self.c_table.set_on_select(self._cycle_on_select)

        self._toggle_cycle_targets_source()
        self._toggle_cycle_text()
        self._refresh_cycle_templates()
        self._refresh_cycle_campaigns()
        self._cycle_select_campaign(self.c_campaign_var.get())
        # Watchdog is scheduled after the full BroadcastFrame is built.

    def _set_cycle_busy(self, busy: bool, status_text: str = ""):
        """Set busy state to prevent overlapping UI ops that cause visual glitches and 'not responding' on weak hardware.
        This must stay lightweight — widget creation must NOT be here.
        """
        self._cycle_ui_busy = busy
        self._cycle_busy = busy  # keep alias for any older references
        if status_text:
            try:
                self._cycle_set_status(status_text, "orange")
            except Exception:
                pass
        # During short UI rebuilds only block Start-like actions. Stop must remain
        # clickable when a worker is alive; otherwise the app looks frozen.
        if busy:
            for btn_name in ("btn_cycle_start", "btn_cycle_start_enabled"):
                btn = getattr(self, btn_name, None)
                if btn:
                    try:
                        btn.configure(state="disabled")
                    except Exception:
                        pass
        else:
            try:
                self._cycle_refresh_cycle_buttons()
            except Exception:
                pass

    def _toggle_cycle_targets_source(self):
        if self.c_targets_source_var.get() == "База":
            self.c_template_menu.configure(state="disabled")
        else:
            self.c_template_menu.configure(state="normal")

    def _toggle_cycle_text(self):
        if self.c_message_source_var.get() == "Избранное":
            self.c_message.configure(state="disabled")
        else:
            self.c_message.configure(state="normal")
        if hasattr(self, "c_message_template_menu"):
            if self.c_message_source_var.get() == "Шаблоны":
                self.c_message_template_menu.configure(state="normal")
            else:
                self.c_message_template_menu.configure(state="disabled")

    def _refresh_cycle_templates(self):
        db = Database(self.app.config.db_path)
        templates_all = db.get_all_list_templates()
        db.close()
        templates = [t for t in templates_all if t.get("kind") in ("groups", "mixed")]
        self._cycle_template_by_name = {t["name"]: t for t in templates}
        values = list(self._cycle_template_by_name.keys()) or ["—"]
        current = self.c_template_var.get()
        self.c_template_menu.configure(values=values)
        if current not in values:
            self.c_template_var.set(values[0])
        message_templates = [t for t in templates_all if t.get("kind") == "messages"]
        self._cycle_message_template_by_name = {t["name"]: t for t in message_templates}
        if hasattr(self, "c_message_template_menu"):
            msg_values = list(self._cycle_message_template_by_name.keys()) or ["—"]
            msg_current = self.c_message_template_var.get()
            self.c_message_template_menu.configure(values=msg_values)
            if msg_current not in msg_values:
                self.c_message_template_var.set(msg_values[0])

    def _cycle_on_message_template_change(self, name: str):
        tpl = (getattr(self, "_cycle_message_template_by_name", {}) or {}).get(name)
        if not tpl:
            return
        try:
            self.c_message.configure(state="normal")
            self.c_message.delete("1.0", "end")
            self.c_message.insert("1.0", tpl.get("content") or "")
            self._toggle_cycle_text()
        except Exception:
            pass

    def _refresh_cycle_campaigns(self):
        db = Database(self.app.config.db_path)
        try:
            campaigns = db.list_cycle_campaigns()
            if not campaigns:
                db.get_or_create_cycle_campaign(self._cycle_campaign_name)
                campaigns = db.list_cycle_campaigns()
        finally:
            db.close()

        self._cycle_campaign_by_name = {c["name"]: c for c in campaigns}
        # Clean any stale runners for campaigns that no longer exist in DB.
        # This prevents glitching (stale threads, health checks, UI updates referencing deleted campaigns).
        for stale_name in list(getattr(self, "_cycle_runners", {}).keys()):
            if stale_name not in self._cycle_campaign_by_name:
                try:
                    self._stop_cycle(stale_name)
                except Exception:
                    pass
                self._cycle_runners.pop(stale_name, None)
        values = list(self._cycle_campaign_by_name.keys()) or [self._cycle_campaign_name]
        current = self.c_campaign_var.get()
        self.c_campaign_menu.configure(values=values)
        if current not in values:
            self.c_campaign_var.set(values[0])

    def _get_cycle_account_filter(self) -> str:
        """Возвращает 'Все активные' или реальный телефон (с учётом метки в меню)."""
        raw = (self.c_account_var.get() or "").strip()
        if raw == "Все активные" or not raw:
            return ""
        if hasattr(self, '_resolve_phone'):
            return self._resolve_phone(raw)
        else:
            # fallback
            if not raw or raw in ("Все активные", "Нет аккаунтов"):
                return raw
            if "(" in raw and raw.endswith(")"):
                try: return raw.rsplit("(",1)[-1].rstrip(")")
                except: pass
            return raw

    # --- Центральный трекинг занятых аккаунтов (для синей подсветки + контекста) ---
    def _ensure_busy_dict(self):
        if hasattr(self, "app") and hasattr(self.app, "get_busy_accounts"):
            return
        if not hasattr(self, "_busy_accounts"):
            self._busy_accounts: dict[str, str] = {}

    def mark_account_busy(self, phones: str | list[str], context: str):
        """Пометить аккаунт(ы) как занятые в работе (рассылка, парсинг и т.д.)."""
        if hasattr(self, "app") and hasattr(self.app, "mark_account_busy"):
            self.app.mark_account_busy(phones, context)
            return
        self._ensure_busy_dict()
        if isinstance(phones, str):
            phones = [phones]
        for p in phones:
            if p:
                self._busy_accounts[p] = context

    def mark_account_free(self, phones: str | list[str]):
        if hasattr(self, "app") and hasattr(self.app, "mark_account_free"):
            self.app.mark_account_free(phones)
            return
        self._ensure_busy_dict()
        if isinstance(phones, str):
            phones = [phones]
        for p in phones:
            self._busy_accounts.pop(p, None)

    def get_busy_accounts(self) -> dict[str, str]:
        if hasattr(self, "app") and hasattr(self.app, "get_busy_accounts"):
            return self.app.get_busy_accounts()
        self._ensure_busy_dict()
        # Возвращаем копию, чтобы не мутировали извне
        return dict(self._busy_accounts)

    def _runtime_busy_phones(self, selected_account: str, explicit_phones: list[str] | None = None) -> list[str]:
        if explicit_phones:
            return [p for p in explicit_phones if (p or "").strip()]
        selected = (selected_account or "").strip()
        if not selected or selected == "Нет аккаунтов":
            return []
        if selected != "Все активные":
            return [selected]
        db = Database(self.app.config.db_path)
        try:
            return [a.phone for a in db.get_active_accounts()]
        finally:
            db.close()

    def _cycle_on_campaign_change(self, name: str):
        old_name = (self._cycle_campaign_name or "").strip()
        new_name = (name or "").strip()
        if old_name and new_name and old_name != new_name:
            try:
                self._cycle_save_current_campaign_settings(old_name)
            except Exception as e:
                self.log.append(f"[Циклическая] [!] Не удалось сохранить настройки кампании '{old_name}': {e}")
        self._cycle_select_campaign(name)

    def _cycle_select_campaign(self, name: str):
        picked = (name or "").strip()
        if not picked:
            picked = self._cycle_campaign_name
        self._cycle_campaign_name = picked
        self.c_campaign_var.set(picked)
        try:
            self._cycle_load_campaign_settings()
        except Exception:
            pass
        try:
            self._cycle_refresh_campaign_accounts_ui()
        except Exception:
            pass
        try:
            self._cycle_refresh_table()
        except Exception:
            pass
        try:
            self._cycle_update_status()
        except Exception:
            pass

    def _cycle_create_campaign(self):
        try:
            self._cycle_save_current_campaign_settings()
        except Exception as e:
            self.log.append(f"[Циклическая] [!] Не удалось сохранить текущую кампанию перед созданием новой: {e}")
        dlg = ctk.CTkInputDialog(text="Название кампании:", title="Создать кампанию")
        name = (dlg.get_input() or "").strip()
        if not name:
            try:
                from tkinter import messagebox
                messagebox.showwarning("Создать кампанию", "Название кампании не может быть пустым.")
            except Exception:
                pass
            return
        try:
            db = Database(self.app.config.db_path)
            try:
                campaign_id = db.get_or_create_cycle_campaign(name)
                defaults = self._cycle_defaults()
                run_settings = self._cycle_run_settings()
                source_mode = self.c_message_source_var.get()
                message_source = "saved" if source_mode == "Избранное" else ("templates" if source_mode == "Шаблоны" else "manual")
                message_template_id = self._cycle_current_message_template_id() if message_source == "templates" else None
                message_text = "" if message_source == "saved" else self.c_message.get("1.0", "end").strip()
                if message_source == "templates" and message_template_id:
                    message_text = self._cycle_current_message_template_content()
                target_template_id = None
                if self.c_targets_source_var.get() != "База":
                    tpl = self._cycle_template_by_name.get(self.c_template_var.get())
                    if tpl and tpl.get("id") is not None:
                        target_template_id = int(tpl["id"])
                db.update_cycle_campaign(
                    campaign_id,
                    targets_source="tasks" if self.c_targets_source_var.get() == "База" else "template",
                    template_id=target_template_id,
                    message_source=message_source,
                    message_text=message_text,
                    unique_mode=self.c_unique_var.get(),
                    enabled=False,
                    account_filter=self._get_cycle_account_filter(),
                    rotate_after_n_sends=int(run_settings.get("rotate_after_n_sends", 0)),
                    send_delay_min_seconds=int(run_settings["send_delay_min_seconds"]),
                    send_delay_max_seconds=int(run_settings["send_delay_max_seconds"]),
                    round_pause_seconds=int(run_settings["round_pause_seconds"]),
                    daily_actions_limit=int(run_settings.get("daily_actions_limit", 0)),
                    message_template_id=message_template_id,
                    default_hours_start=int(defaults["hours_start"]),
                    default_hours_end=int(defaults["hours_end"]),
                    default_interval_min_seconds=int(defaults["interval_min_seconds"]),
                    default_interval_max_seconds=int(defaults["interval_max_seconds"]),
                    default_min_new_messages=int(defaults["min_new_messages"]),
                    default_fallback_hours=int(defaults["fallback_hours"]),
                )
            finally:
                db.close()
        except Exception as e:
            self.log.append(f"[Циклическая] [!] Не удалось создать кампанию: {e}")
            try:
                from tkinter import messagebox
                messagebox.showerror("Создать кампанию", f"Ошибка БД при создании кампании: {e}")
            except Exception:
                pass
            return

        self._refresh_cycle_campaigns()
        self._cycle_select_campaign(name)
        self.log.append(f"[Циклическая] [+] Кампания создана: {name}")
        if self._cycle_active_count():
            self.log.append("[Циклическая] [i] Новую кампанию можно настраивать и запускать отдельно; уже запущенные кампании продолжат работу.")

    def _cycle_rename_campaign(self):
        name = self.c_campaign_var.get()
        if self._cycle_runner_alive(self._cycle_get_runner(name)):
            self.log.append("[!] Нельзя переименовать запущенную кампанию")
            return
        if not name:
            return
        try:
            self._cycle_save_current_campaign_settings(name)
        except Exception as e:
            self.log.append(f"[Циклическая] [!] Не удалось сохранить настройки перед переименованием: {e}")
        dlg = ctk.CTkInputDialog(text="Новое название кампании:", title="Переименовать кампанию")
        new_name = (dlg.get_input() or "").strip()
        if not new_name or new_name == name:
            return
        try:
            db = Database(self.app.config.db_path)
            try:
                campaign_id = db.get_or_create_cycle_campaign(name)
                db.rename_cycle_campaign(campaign_id, new_name)
            finally:
                db.close()
        except Exception as e:
            self.log.append(f"[!] Не удалось переименовать кампанию: {e}")
            return
        self._refresh_cycle_campaigns()
        self._cycle_select_campaign(new_name)
        self.log.append(f"[Циклическая] [+] Кампания переименована: {name} -> {new_name}")

    def _cycle_delete_campaign(self):
        name = (self.c_campaign_var.get() or "").strip()
        if not name:
            return
        if name == "CycleBroadcast":
            self.log.append("[Циклическая] [!] Кампания CycleBroadcast защищена от удаления")
            return

        # Force stop if any runner is (or thinks it is) running for this campaign.
        # Prevents glitching from stale threads/runners after delete.
        try:
            if self._cycle_runner_alive(self._cycle_get_runner(name)):
                self._stop_cycle(name)
        except Exception:
            pass

        # Safely find ID without get_or_create (get_or_create was causing "не удаляется"
        # by creating a dummy campaign with same name and deleting the dummy instead of the real one).
        campaign_id = None
        try:
            db = Database(self.app.config.db_path)
            try:
                row = db.conn.execute(
                    "SELECT id FROM cycle_campaigns WHERE name = ?", (name,)
                ).fetchone()
                if row:
                    campaign_id = int(row[0])
            finally:
                db.close()
        except Exception as e:
            self.log.append(f"[!] Не удалось найти кампанию для удаления: {e}")
            return

        if not campaign_id:
            self.log.append(f"[Циклическая] [!] Кампания '{name}' не найдена (возможно уже удалена)")
            self._refresh_cycle_campaigns()
            # select something safe
            if self._cycle_campaign_by_name:
                self._cycle_select_campaign(next(iter(self._cycle_campaign_by_name.keys())))
            return

        try:
            db = Database(self.app.config.db_path)
            try:
                ok = db.delete_cycle_campaign(campaign_id)
            finally:
                db.close()
        except Exception as e:
            self.log.append(f"[!] Не удалось удалить кампанию: {e}")
            return

        # Clean in-memory state to prevent glitching / stale references
        self._cycle_runners.pop(name, None)
        self._cycle_campaign_by_name.pop(name, None)
        if getattr(self, "_cycle_campaign_name", "") == name:
            self._cycle_campaign_name = ""
        if getattr(self, "_cycle_running_campaign_name", "") == name:
            self._cycle_running_campaign_name = ""
        if getattr(self, "_cycle_running_campaign_id", None) == campaign_id:
            self._cycle_running_campaign_id = None

        if ok:
            self.log.append(f"[Циклическая] [+] Кампания удалена: {name}")
        else:
            self.log.append(f"[Циклическая] [!] Кампания '{name}' не была удалена (возможно не существовала)")

        self._refresh_cycle_campaigns()

        # Select a safe existing campaign (first available, or default)
        if self._cycle_campaign_by_name:
            safe_name = next(iter(self._cycle_campaign_by_name.keys()))
            self._cycle_select_campaign(safe_name)
        else:
            # will create default in select if needed
            self._cycle_select_campaign("CycleBroadcast")

    def _cycle_refresh_campaign_accounts_ui(self):
        db = Database(self.app.config.db_path)
        try:
            campaign_id = db.get_or_create_cycle_campaign(self._cycle_campaign_name)
            phones = db.get_cycle_campaign_account_phones(campaign_id)
        finally:
            db.close()
        self._cycle_campaign_accounts = phones

        if hasattr(self, "lbl_cycle_accounts"):
            if phones:
                txt = f"Аккаунты кампании: {len(phones)}"
            else:
                txt = "Аккаунты кампании: общий выбор"
            self.lbl_cycle_accounts.configure(text=txt)

        try:
            if phones:
                self.c_account_menu.configure(state="disabled")
            else:
                self.c_account_menu.configure(state="normal")
        except Exception:
            pass

    def _cycle_edit_campaign_accounts(self):
        if self._cycle_runner_alive(self._cycle_get_runner(self._cycle_campaign_name)):
            self.log.append("[!] Нельзя менять аккаунты запущенной кампании")
            return
        db = Database(self.app.config.db_path)
        try:
            campaign_id = db.get_or_create_cycle_campaign(self._cycle_campaign_name)
            selected = db.get_cycle_campaign_account_phones(campaign_id)
            accounts = db.get_all_accounts()
        finally:
            db.close()

        dlg = CycleCampaignAccountsDialog(self, accounts=accounts, selected_phones=selected)
        self.wait_window(dlg)
        if dlg.result is None:
            return

        db2 = Database(self.app.config.db_path)
        try:
            campaign_id2 = db2.get_or_create_cycle_campaign(self._cycle_campaign_name)
            db2.set_cycle_campaign_accounts(campaign_id2, dlg.result)
        finally:
            db2.close()

        self._cycle_refresh_campaign_accounts_ui()
        if dlg.result:
            self.log.append(f"[Циклическая] [~] Аккаунты кампании сохранены: {len(dlg.result)}")
        else:
            self.log.append("[Циклическая] [~] Аккаунты кампании очищены: будет использоваться общий выбор аккаунтов")

    def _cycle_clear_campaign_accounts(self):
        if self._cycle_runner_alive(self._cycle_get_runner(self._cycle_campaign_name)):
            self.log.append("[!] Нельзя менять аккаунты запущенной кампании")
            return
        db = Database(self.app.config.db_path)
        try:
            campaign_id = db.get_or_create_cycle_campaign(self._cycle_campaign_name)
            db.set_cycle_campaign_accounts(campaign_id, [])
        finally:
            db.close()
        self._cycle_refresh_campaign_accounts_ui()
        self.log.append("[Циклическая] [~] Аккаунты кампании очищены")

    def _cycle_on_select(self, index):
        return

    def _cycle_defaults(self) -> dict:
        def _i(entry, default):
            try:
                return int(entry.get().strip())
            except Exception:
                return default
        hs = max(0, min(23, _i(self.c_hours_start, 0)))
        he = max(0, min(23, _i(self.c_hours_end, 23)))
        imin = max(0, _i(self.c_int_min, 0))
        imax = max(0, _i(self.c_int_max, imin))
        if imax < imin:
            imax = imin
        mn = max(0, _i(self.c_min_new, 0))
        fb = max(0, _i(self.c_fallback_hours, 0))
        return {
            "hours_start": hs,
            "hours_end": he,
            "interval_min_seconds": imin,
            "interval_max_seconds": imax,
            "min_new_messages": mn,
            "fallback_hours": fb,
        }

    def _cycle_run_settings(self) -> dict:
        def _i(entry, default):
            try:
                return int(entry.get().strip())
            except Exception:
                return default
        smin = max(1, _i(self.c_send_delay_min, 30))
        smax = max(smin, _i(self.c_send_delay_max, smin))
        rp = max(0, _i(self.c_round_pause, 0))
        rotate_after_n_sends = max(0, _i(self.c_rotate_after_n, 0))
        daily_actions_limit = max(0, _i(self.c_daily_limit, 0))
        return {
            "send_delay_min_seconds": smin,
            "send_delay_max_seconds": smax,
            "round_pause_seconds": rp,
            "rotate_after_n_sends": rotate_after_n_sends,
            "daily_actions_limit": daily_actions_limit,
        }

    @staticmethod
    def _cycle_set_entry(entry, value):
        entry.delete(0, "end")
        entry.insert(0, str(value))

    def _cycle_current_message_template_id(self) -> int | None:
        tpl = (getattr(self, "_cycle_message_template_by_name", {}) or {}).get(
            self.c_message_template_var.get()
        )
        if tpl and tpl.get("id") is not None:
            return int(tpl["id"])
        return None

    def _cycle_current_message_template_content(self) -> str:
        tpl = (getattr(self, "_cycle_message_template_by_name", {}) or {}).get(
            self.c_message_template_var.get()
        )
        return (tpl.get("content") or "").strip() if tpl else ""

    def _cycle_save_current_campaign_settings(self, campaign_name: str | None = None, enabled: bool | None = None):
        name = (campaign_name or self._cycle_campaign_name or "").strip()
        if not name:
            return
        db = Database(self.app.config.db_path)
        try:
            campaign_id = db.get_or_create_cycle_campaign(name)
            camp = db.load_cycle_campaign(campaign_id) or {}
            if enabled is None:
                enabled = bool(int(camp.get("enabled", 0) or 0))

            if self.c_targets_source_var.get() == "База":
                targets_source = "tasks"
                target_template_id = None
            else:
                targets_source = "template"
                tpl = self._cycle_template_by_name.get(self.c_template_var.get())
                target_template_id = int(tpl["id"]) if tpl and tpl.get("id") is not None else None

            source_mode = self.c_message_source_var.get()
            message_source = "saved" if source_mode == "Избранное" else ("templates" if source_mode == "Шаблоны" else "manual")
            message_template_id = self._cycle_current_message_template_id() if message_source == "templates" else None
            message_text = "" if message_source == "saved" else self.c_message.get("1.0", "end").strip()
            defaults = self._cycle_defaults()
            run_settings = self._cycle_run_settings()
            db.update_cycle_campaign(
                campaign_id,
                targets_source=targets_source,
                template_id=target_template_id,
                message_source=message_source,
                message_text=message_text,
                unique_mode=self.c_unique_var.get(),
                enabled=bool(enabled),
                account_filter=self._get_cycle_account_filter(),
                rotate_after_n_sends=int(run_settings.get("rotate_after_n_sends", 0)),
                send_delay_min_seconds=int(run_settings["send_delay_min_seconds"]),
                send_delay_max_seconds=int(run_settings["send_delay_max_seconds"]),
                round_pause_seconds=int(run_settings["round_pause_seconds"]),
                    daily_actions_limit=int(run_settings.get("daily_actions_limit", 0)),
                message_template_id=message_template_id,
                default_hours_start=int(defaults["hours_start"]),
                default_hours_end=int(defaults["hours_end"]),
                default_interval_min_seconds=int(defaults["interval_min_seconds"]),
                default_interval_max_seconds=int(defaults["interval_max_seconds"]),
                default_min_new_messages=int(defaults["min_new_messages"]),
                default_fallback_hours=int(defaults["fallback_hours"]),
            )
        finally:
            db.close()

    def _cycle_set_status(self, text: str, color: str = "gray70"):
        try:
            self.lbl_cycle_status.configure(text=f"Статус: {text}", text_color=color)
        except Exception:
            pass

    def _cycle_set_summary(self, text: str, color: str = "gray70"):
        try:
            self.lbl_cycle_summary.configure(text=text, text_color=color)
        except Exception:
            pass

    def _cycle_update_dashboard(self, metrics: dict, details: str, color: str = "gray70"):
        labels = getattr(self, "_cycle_metric_labels", {}) or {}
        for key, value in metrics.items():
            label = labels.get(key)
            if label is None:
                continue
            try:
                label.configure(text=str(value), text_color=color)
            except Exception:
                pass
        self._cycle_set_summary(details, "#A9D6FF" if color != "gray70" else "gray70")

    def _cycle_reject_start(self, message: str, status: str = "ошибка запуска", summary: str | None = None):
        self._append_log(f"[Циклическая] [!] {message}")
        self._cycle_set_status(status, "orange")
        if summary:
            self._cycle_set_summary(summary, "orange")

    @staticmethod
    def _cycle_format_dt(value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return "—"
        try:
            return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return raw[:19].replace("T", " ")

    @staticmethod
    def _cycle_short_text(value: str, limit: int = 80) -> str:
        text = " ".join(str(value or "").strip().split())
        if not text:
            return "—"
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)].rstrip() + "…"

    @staticmethod
    def _cycle_dt_or_none(value: str):
        raw = (value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except Exception:
            return None

    def _cycle_build_snapshot(self) -> dict:
        db = Database(self.app.config.db_path)
        try:
            campaign_id = db.get_or_create_cycle_campaign(self._cycle_campaign_name)
            campaign = db.load_cycle_campaign(campaign_id) or {}
            state = db.load_cycle_state(campaign_id)
            targets = db.get_cycle_targets(campaign_id)
        finally:
            db.close()

        total = len(targets)
        pos = int(state.get("current_pos", 0) or 0)
        if total > 0:
            pos = pos % total
            current_target = targets[pos]
            current_link = (current_target.get("link") or "").strip() or "—"
            next_target = targets[(pos + 1) % total] if total > 1 else {}
            next_link = (next_target.get("link") or "").strip() or "—"
        else:
            current_target = {}
            current_link = "—"
            next_link = "—"

        last_sent_dt = None
        for target in targets:
            dt = self._cycle_dt_or_none(target.get("last_sent_at", ""))
            if dt and (last_sent_dt is None or dt > last_sent_dt):
                last_sent_dt = dt

        active_targets = 0
        waiting_targets = 0
        error_targets = 0
        next_retry_dt = None
        now = datetime.now()
        for target in targets:
            status = (target.get("status") or "active").strip().lower()
            dt = self._cycle_dt_or_none(target.get("retry_after", ""))
            if status == "error":
                error_targets += 1
            elif dt and dt > now:
                waiting_targets += 1
            else:
                active_targets += 1
            if dt and dt > now and (next_retry_dt is None or dt < next_retry_dt):
                next_retry_dt = dt

        runtime = {}
        try:
            runtime = (getattr(self, "_cycle_runtime", {}) or {}).get(self._cycle_campaign_name, {}) or {}
        except Exception:
            runtime = {}
        runtime_current = (runtime.get("current_target") or "").strip()
        runtime_next = (runtime.get("next_target") or "").strip()
        runtime_account = (runtime.get("account") or "").strip()
        runtime_success = (runtime.get("last_success_at") or "").strip()
        runtime_error = (runtime.get("last_error") or "").strip()

        return {
            "campaign_id": campaign_id,
            "campaign": campaign,
            "state": state,
            "targets": targets,
            "total": total,
            "pos": pos,
            "current_link": runtime_current or current_link,
            "next_link": runtime_next or next_link,
            "current_status": (current_target.get("status") or "active").strip() if current_target else "—",
            "current_error": (current_target.get("last_error") or "").strip() or "—",
            "active_targets": active_targets,
            "waiting_targets": waiting_targets,
            "error_targets": error_targets,
            "last_account": runtime_account or (state.get("last_account_phone") or "").strip() or "—",
            "last_run_at": self._cycle_format_dt(state.get("last_run_at", "")),
            "last_sent_at": runtime_success or (last_sent_dt.strftime("%Y-%m-%d %H:%M:%S") if last_sent_dt else "—"),
            "next_retry_at": next_retry_dt.strftime("%Y-%m-%d %H:%M:%S") if next_retry_dt else "сейчас",
            "enabled": bool(int(campaign.get("enabled", 0) or 0)),
            "sent_total": int(state.get("sent_total", 0) or 0),
            "error_total": int(state.get("error_total", 0) or 0),
            "last_error": runtime_error or (state.get("last_error") or "").strip() or "—",
        }

    def _cycle_runner_alive(self, runner) -> bool:
        try:
            thread = runner.get("thread") if isinstance(runner, dict) else runner
            return thread is not None and thread.is_alive()
        except Exception:
            return False

    def _cycle_selected_running(self, campaign_name: str | None = None) -> bool:
        """True only for the selected campaign, not for any campaign globally."""
        try:
            name = (campaign_name or self._cycle_campaign_name or "").strip()
            runner = self._cycle_get_runner(name) if name else None
            if not self._cycle_runner_alive(runner):
                if name and isinstance(getattr(self, "_cycle_runners", None), dict):
                    self._cycle_runners.pop(name, None)
                return False
            return True
        except Exception:
            return False

    def _cycle_get_runner(self, campaign_name: str | None = None):
        name = (campaign_name or self._cycle_campaign_name or "").strip()
        runners = getattr(self, "_cycle_runners", None) or {}
        return runners.get(name)

    def _cycle_update_runtime(self, msg: dict):
        if not isinstance(msg, dict):
            return
        name = (msg.get("campaign") or "").strip()
        if not name:
            return
        runtime = getattr(self, "_cycle_runtime", None)
        if runtime is None:
            self._cycle_runtime = {}
            runtime = self._cycle_runtime
        current = dict(runtime.get(name) or {})
        for key in (
            "account",
            "current_target",
            "next_target",
            "last_success_at",
            "last_error",
            "last_error_at",
            "phase",
        ):
            if key in msg:
                current[key] = msg.get(key) or ""
        current["updated_at"] = datetime.now().isoformat(timespec="seconds")
        runtime[name] = current
        if name == (self._cycle_campaign_name or "").strip():
            try:
                self._cycle_update_status()
            except Exception:
                pass

    def _cycle_clear_runtime(self, campaign_name: str | None = None):
        runtime = getattr(self, "_cycle_runtime", None)
        if not isinstance(runtime, dict):
            return
        if campaign_name:
            runtime.pop(campaign_name, None)
        else:
            runtime.clear()

    def _cycle_active_names(self) -> list[str]:
        runners = getattr(self, "_cycle_runners", None) or {}
        return [name for name, runner in runners.items() if self._cycle_runner_alive(runner)]

    def _cycle_active_count(self) -> int:
        return len(self._cycle_active_names())

    def _cycle_watchdog(self):
        """Keep UI honest when a campaign is saved/enabled but no worker is alive."""
        try:
            runners = getattr(self, "_cycle_runners", None) or {}
            for name, runner in list(runners.items()):
                if not self._cycle_runner_alive(runner):
                    runners.pop(name, None)

            active_names = self._cycle_active_names()
            self._cycle_running = bool(active_names)
            if active_names:
                if getattr(self, "_cycle_running_campaign_name", "") not in active_names:
                    self._cycle_running_campaign_name = active_names[0]
            else:
                self._cycle_running_campaign_name = ""

            enabled_with_targets = []
            try:
                db = Database(self.app.config.db_path)
                try:
                    for camp in db.list_cycle_campaigns() or []:
                        try:
                            if not int(camp.get("enabled", 0) or 0):
                                continue
                            name = (camp.get("name") or "").strip()
                            if not name:
                                continue
                            cid = int(camp.get("id") or db.get_or_create_cycle_campaign(name))
                            if db.get_cycle_targets(cid):
                                enabled_with_targets.append(name)
                        except Exception:
                            continue
                finally:
                    db.close()
            except Exception as e:
                self.log.append(f"[Циклическая] [watchdog] Не смог проверить БД: {e}")

            selected = (self._cycle_campaign_name or "").strip()
            selected_alive = self._cycle_selected_running(selected)
            if enabled_with_targets and not active_names:
                now = time.monotonic()
                if now - getattr(self, "_cycle_last_watchdog_log_at", 0.0) > 300:
                    self.log.append(
                        "[Циклическая] [watchdog] Кампании сохранены как активные, "
                        "но живого фонового воркера нет: " + ", ".join(enabled_with_targets)
                    )
                    self._cycle_last_watchdog_log_at = now
                try:
                    self._cycle_set_status(
                        "не работает: нет фонового воркера, нажми Старт для выбранной кампании",
                        "#E74C3C",
                    )
                    if selected_alive:
                        self.btn_cycle_start.configure(state="disabled")
                        self.btn_cycle_stop.configure(state="normal")
                    else:
                        self.btn_cycle_start.configure(state="normal")
                        self.btn_cycle_stop.configure(state="disabled")
                    if not getattr(self, "_running", False):
                        self.btn_stop_current.configure(state="disabled", text="■ Остановить текущий процесс")
                except Exception:
                    pass
            else:
                self._cycle_refresh_cycle_buttons()
        except Exception as e:
            try:
                self.log.append(f"[Циклическая] [watchdog] Ошибка проверки состояния: {e}")
            except Exception:
                pass
        finally:
            try:
                if getattr(self, "_cycle_watchdog_after_id", None) is not None:
                    self.after_cancel(self._cycle_watchdog_after_id)
            except Exception:
                pass
            try:
                self._cycle_watchdog_after_id = self.after(30000, self._cycle_watchdog)
            except Exception:
                pass

    def _cycle_periodic_health_check(self):
        """Периодическая проверка (каждые 2 часа) всех активных циклических кампаний.
        Использует короткоживущие DB соединения на каждую кампанию, чтобы избежать
        "Cannot operate on a closed database" и проблем с concurrent/close.
        """
        try:
            active = self._cycle_active_names()
            if not active:
                self.after(5 * 60 * 60 * 1000, self._cycle_periodic_health_check)
                return

            self.log.append("[Циклическая] [~] === Периодическая проверка активных кампаний (5 часов) ===")

            for cname in active:
                db = None
                try:
                    db = Database(self.app.config.db_path)
                    cid = db.get_or_create_cycle_campaign(cname)
                    camp = db.load_cycle_campaign(cid) or {}
                    targets = db.get_cycle_targets(cid)
                    state = db.load_cycle_state(cid) or {}
                    camp_phones = db.get_cycle_campaign_account_phones(cid)
                    accounts_all = db.get_active_accounts()
                    by_phone = {a.phone: a for a in accounts_all}

                    runner_alive = self._cycle_runner_alive(self._cycle_get_runner(cname))
                    total_targets = len(targets)
                    pos = int((state or {}).get("current_pos", 0) or 0)
                    last_sent = (state or {}).get("last_run_at") or "—"
                    last_acc = (state or {}).get("last_account_phone") or "—"

                    used_accs = []
                    if camp_phones:
                        used_accs = [by_phone[p] for p in camp_phones if p in by_phone]
                    else:
                        used_accs = accounts_all

                    healthy = sum(1 for a in used_accs if a.is_active and (a.status or "active") == "active")
                    self.log.append(
                        f"[Циклическая] [~] '{cname}': runner={'живой' if runner_alive else 'МЁРТВ!!!'} | "
                        f"целей={total_targets} | pos={pos+1}/{max(total_targets,1)} | "
                        f"аккаунтов={len(used_accs)} (здоровых {healthy}) | last_acc={last_acc} | last={last_sent}"
                    )

                    if len(used_accs) <= 6:
                        for a in used_accs:
                            disp = format_account(a.phone, getattr(a, "custom_name", ""))
                            proxy_info = "no-proxy" if not (a.proxy or "").strip() else "proxy"
                            last_act = (a.last_send_at or a.last_check_ok_at or "—")[:16].replace("T", " ")
                            self.log.append(
                                f"    [i]   {disp} | {proxy_info} | status={a.status} | last_act={last_act}"
                            )
                except Exception as inner:
                    self.log.append(f"[Циклическая] [!] Ошибка проверки кампании '{cname}': {inner}")
                finally:
                    if db is not None:
                        try:
                            db.close()
                        except Exception:
                            pass

            self.log.append("[Циклическая] [~] === Проверка завершена ===")
        except Exception as e:
            err = str(e)
            if "closed database" in err.lower() or "database is locked" in err.lower():
                self.log.append(f"[Циклическая] [!] Transient DB issue in periodic health check (non-fatal): {err[:80]}")
            else:
                self.log.append(f"[Циклическая] [!] Ошибка периодической проверки: {e}")
        finally:
            try:
                self.after(5 * 60 * 60 * 1000, self._cycle_periodic_health_check)
            except Exception:
                pass

            try:
                self._cycle_watchdog()
            except Exception:
                pass

    def _resume_enabled_cycles(self, only_dead: bool = False):
        """Resume only campaigns that were explicitly saved as enabled.

        Starting every configured campaign on app launch makes the UI look haunted:
        a saved test campaign can wake up later and consume the worker/account state.
        If only_dead=True, only enabled campaigns whose runner died are resumed.
        """
        if getattr(self, '_resume_in_progress', False):
            return
        self._resume_in_progress = True
        try:
            self.log.append("[Циклическая] [~] === АВТО-ВОЗОБНОВЛЕНИЕ ВКЛЮЧЕННЫХ ЦИКЛИЧЕСКИХ КАМПАНИЙ ===")
            try:
                self._refresh_cycle_templates()
            except Exception:
                pass

            # Один свежий DB для сбора списка
            db = Database(self.app.config.db_path)
            try:
                camps = db.list_cycle_campaigns()
                configured = []
                for c in camps:
                    name = (c.get("name") or "").strip()
                    if not name:
                        continue
                    if not int(c.get("enabled", 0) or 0):
                        continue
                    cid = db.get_or_create_cycle_campaign(name)
                    tgts = db.get_cycle_targets(cid)
                    accs = db.get_cycle_campaign_account_phones(cid)
                    # Fixed regression: accs may be [] for "Все активные" (global pool) campaigns.
                    # Such campaigns are valid for start if they have targets. The per-campaign
                    # accs list is optional; 0 means use active accounts at launch time.
                    if _cycle_has_usable_config(len(tgts), len(accs)):
                        configured.append((name, len(tgts), len(accs)))
            finally:
                db.close()

            if not configured:
                self.log.append("[Циклическая] [i] Нет включённых кампаний с целями и аккаунтами.")
                return

            self.log.append(f"[Циклическая] [i] Найдено включённых кампаний: {[c[0] for c in configured]}")

            resumed = []
            for name, ntgts, naccs in configured:
                if only_dead and self._cycle_runner_alive(self._cycle_get_runner(name)):
                    continue

                old_name = getattr(self, "_cycle_campaign_name", None)
                try:
                    self._cycle_campaign_name = name
                    if hasattr(self, "c_campaign_var"):
                        try:
                            self.c_campaign_var.set(name)
                        except Exception:
                            pass
                    self._start_cycle()

                    # P0: do not mark campaign as running until a live worker exists.
                    runner = self._cycle_get_runner(name)
                    if not self._cycle_runner_alive(runner):
                        self.log.append(
                            f"[Циклическая] [!] Кампания '{name}' НЕ запущена: фоновой воркер не подтвердился. "
                            "Смотри строки выше: нет целей/аккаунтов/текста или ошибка старта."
                        )
                        try:
                            db3 = Database(self.app.config.db_path)
                            cid = db3.get_or_create_cycle_campaign(name)
                            db3.set_cycle_campaign_enabled(cid, False)
                            db3.close()
                        except Exception:
                            pass
                        continue

                    try:
                        db3 = Database(self.app.config.db_path)
                        cid = db3.get_or_create_cycle_campaign(name)
                        db3.set_cycle_campaign_enabled(cid, True)
                        db3.close()
                    except Exception:
                        pass

                    resumed.append(name)
                    self.log.append(f"[Циклическая] [+] Запущена рассылка '{name}' (целей={ntgts}, аккаунтов={naccs})")
                except Exception as e:
                    self.log.append(f"[Циклическая] [!] Ошибка запуска '{name}': {e}")
                finally:
                    if old_name:
                        self._cycle_campaign_name = old_name
                        if hasattr(self, "c_campaign_var"):
                            try:
                                self.c_campaign_var.set(old_name)
                            except Exception:
                                pass

            if resumed:
                self.log.append(f"[Циклическая] [!!!] ЗАПУЩЕНЫ РАССЫЛКИ: {resumed}")
            else:
                self.log.append("[Циклическая] [i] Все настроенные уже запущены.")
        except Exception as e:
            err = str(e)
            if "closed database" in err.lower() or "database is locked" in err.lower():
                # Transient SQLite issue common in Tk + threads + health/resume.
                # Do not treat as critical; the runners themselves are usually fine.
                # Just log warning and try to keep UI/campaigns responsive.
                self.log.append(f"[Циклическая] [!] Transient DB issue in auto-resume (non-fatal): {err[:80]}")
            else:
                self.log.append(f"[Циклическая] [!] Критическая ошибка в авто-запуске: {e}")
        finally:
            try:
                self._refresh_cycle_campaigns()
                self._cycle_refresh_cycle_buttons()
            except Exception:
                pass
            self._resume_in_progress = False

    def _cycle_refresh_cycle_buttons(self):
        selected_running = self._cycle_runner_alive(self._cycle_get_runner(self._cycle_campaign_name))
        active_count = self._cycle_active_count()
        try:
            self.btn_cycle_start.configure(state="disabled" if selected_running else "normal")
            btn_start_enabled = getattr(self, "btn_cycle_start_enabled", None)
            if btn_start_enabled:
                btn_start_enabled.configure(
                    state="disabled" if getattr(self, "_cycle_ui_busy", False) else "normal"
                )
            self.btn_cycle_stop.configure(
                state="normal" if selected_running else "disabled",
                text="■ Стоп",
            )
            if active_count:
                self.btn_stop_current.configure(state="normal", text=f"■ Остановить циклы ({active_count})")
            elif not getattr(self, "_running", False):
                self.btn_stop_current.configure(state="disabled", text="■ Остановить текущий процесс")
        except Exception:
            pass

    def _cycle_start_enabled_campaigns(self):
        """Start saved enabled cycle campaigns that are not already running.

        This is the safe pair to the one-click stop: it does not change campaign
        limits, delays, targets, message text, or enable disabled campaigns.
        """
        if getattr(self, "_resume_in_progress", False):
            self.log.append("[Циклическая] [i] Запуск включённых кампаний уже выполняется.")
            return
        if getattr(self, "_cycle_ui_busy", False):
            self.log.append("[Циклическая] [i] Подождите завершения текущей операции интерфейса.")
            return
        self.log.append("[Циклическая] [~] Ручной запуск всех включённых кампаний...")
        # Note: this only starts already-enabled ones (does not enable disabled); single "Старт" is separate path.
        self._resume_enabled_cycles(only_dead=True)

    def _cycle_sync_targets_for_current_source(self) -> dict:
        db = Database(self.app.config.db_path)
        try:
            campaign_id = db.get_or_create_cycle_campaign(self._cycle_campaign_name)
            if self.c_targets_source_var.get() == "База":
                links = db.get_distinct_broadcast_task_targets()
                target_template_id = None
                targets_source = "tasks"
            else:
                # Defensive: if templates not loaded yet (e.g. early interaction), refresh
                if not self._cycle_template_by_name or self.c_template_var.get() not in self._cycle_template_by_name:
                    self._refresh_cycle_templates()
                tpl = self._cycle_template_by_name.get(self.c_template_var.get())
                links = [l.strip() for l in (tpl.get("content") if tpl else "").splitlines() if l.strip()]
                target_template_id = int(tpl["id"]) if tpl and tpl.get("id") is not None else None
                targets_source = "template"
            unique_links = []
            seen_links = set()
            for link in links:
                if link in seen_links:
                    continue
                seen_links.add(link)
                unique_links.append(link)
            links = unique_links
            defaults = self._cycle_defaults()
            added, updated = db.replace_cycle_targets(campaign_id, links, defaults)
            db.update_cycle_campaign_targets_source(campaign_id, targets_source, target_template_id)
        finally:
            db.close()
        return {"count": len(links), "added": added, "updated": updated}

    def _cycle_load_campaign_settings(self):
        db = Database(self.app.config.db_path)
        try:
            campaign_id = db.get_or_create_cycle_campaign(self._cycle_campaign_name)
            camp = db.load_cycle_campaign(campaign_id) or {}
            targets = db.get_cycle_targets(campaign_id)
        finally:
            db.close()

        def _target_default(key: str, default: int) -> int:
            target_key = key.replace("default_", "")
            target_value = None
            if targets:
                try:
                    target_value = int(targets[0].get(target_key, default) or default)
                except Exception:
                    target_value = None
            if camp.get(key) is not None:
                try:
                    value = int(camp.get(key) or default)
                    if target_value not in (None, default) and value == default:
                        return int(target_value)
                    return value
                except Exception:
                    return default
            if target_value is not None:
                return int(target_value)
            return default

        hours_start = max(0, min(23, _target_default("default_hours_start", 0)))
        hours_end = max(0, min(23, _target_default("default_hours_end", 23)))
        int_min = max(0, _target_default("default_interval_min_seconds", 0))
        int_max = max(int_min, _target_default("default_interval_max_seconds", int_min))
        min_new = max(0, _target_default("default_min_new_messages", 0))
        fallback_hours = max(0, _target_default("default_fallback_hours", 0))
        smin = int(camp.get("send_delay_min_seconds", 30) or 30)
        smax = int(camp.get("send_delay_max_seconds", 90) or 90)
        rp = int(camp.get("round_pause_seconds", 0) or 0)
        if smin < 1:
            smin = 1
        if smax < smin:
            smax = smin
        if rp < 0:
            rp = 0

        self._cycle_set_entry(self.c_hours_start, hours_start)
        self._cycle_set_entry(self.c_hours_end, hours_end)
        self._cycle_set_entry(self.c_int_min, int_min)
        self._cycle_set_entry(self.c_int_max, int_max)
        self._cycle_set_entry(self.c_min_new, min_new)
        self._cycle_set_entry(self.c_fallback_hours, fallback_hours)
        self._cycle_set_entry(self.c_send_delay_min, smin)
        self._cycle_set_entry(self.c_send_delay_max, smax)
        self._cycle_set_entry(self.c_round_pause, rp)
        self._cycle_set_entry(self.c_rotate_after_n, int(camp.get("rotate_after_n_sends", 0) or 0))
        self._cycle_set_entry(self.c_daily_limit, int(camp.get("daily_actions_limit", 0) or 0))

        acc_filter = (camp.get("account_filter") or "").strip()
        if acc_filter:
            self.c_account_var.set(acc_filter)
        else:
            self.c_account_var.set("Все активные")

        targets_source = str(camp.get("targets_source", "") or "").strip()
        if targets_source == "tasks":
            self.c_targets_source_var.set("База")
        elif targets_source == "template":
            self.c_targets_source_var.set("Шаблон")

        template_id = int(camp.get("template_id", 0) or 0)
        self.c_template_var.set("—")
        if template_id:
            for name, tpl in self._cycle_template_by_name.items():
                if int(tpl.get("id", 0) or 0) == template_id:
                    self.c_template_var.set(name)
                    break

        message_source = str(camp.get("message_source", "") or "").strip()
        if message_source == "saved":
            self.c_message_source_var.set("Избранное")
        elif message_source == "templates":
            self.c_message_source_var.set("Шаблоны")
        elif message_source == "manual":
            self.c_message_source_var.set("Вручную")

        self.c_unique_var.set(str(camp.get("unique_mode", "") or "Оригинал"))

        self.c_message_template_var.set("—")
        message_template_id = int(camp.get("message_template_id", 0) or 0)
        if message_template_id:
            for name, tpl in self._cycle_message_template_by_name.items():
                if int(tpl.get("id", 0) or 0) == message_template_id:
                    self.c_message_template_var.set(name)
                    break

        saved_text = str(camp.get("message_text", "") or "")
        if message_source == "templates" and message_template_id:
            tpl_text = self._cycle_current_message_template_content()
            saved_text = tpl_text or saved_text
        self.c_message.configure(state="normal")
        self.c_message.delete("1.0", "end")
        if saved_text:
            self.c_message.insert("1.0", saved_text)

        self._toggle_cycle_targets_source()
        self._toggle_cycle_text()

    def _cycle_update_status(self):
        if getattr(self, '_cycle_ui_busy', False):
            # During user-initiated heavy op (e.g. table refresh on click), skip to avoid overlapping reconfigs that cause "floating" widgets and main thread starvation ("not responding").
            self._cycle_status_after_id = self.after(5000, self._cycle_update_status)
            return
        try:
            snap = self._cycle_build_snapshot()
            pos = int(snap["pos"])
            total = int(snap["total"])
            current_link = self._cycle_short_text(snap["current_link"], 68)
            last_run = snap["last_run_at"]
            last_acc = snap["last_account"]
            next_link = self._cycle_short_text(snap.get("next_link", "—"), 68)
            position = f"{(pos + 1) if total else '—'}/{total or '—'}"
            selected_running = self._cycle_runner_alive(self._cycle_get_runner(self._cycle_campaign_name))
            active_count = self._cycle_active_count()
            summary_color = "#2FA572" if selected_running else ("#F39C12" if active_count or snap["enabled"] else "gray70")
            status_value = "запущена" if selected_running else ("сохранена" if snap["enabled"] else "остановлен")
            metrics = {
                "status": status_value,
                "campaigns": active_count,
                "position": position,
                "targets": total,
                "active": snap["active_targets"],
                "waiting": snap["waiting_targets"],
                "target_errors": snap["error_targets"],
                "sent": snap["sent_total"],
                "err": snap["error_total"],
            }
            detail_parts = [
                f"Кампания: {self._cycle_campaign_name or '—'}",
                f"Текущая цель: {current_link}",
                f"Следующая цель: {next_link}",
                f"Аккаунт: {last_acc}",
                f"Попытка: {last_run}",
                f"Успех: {snap['last_sent_at']}",
                f"Следующая попытка: {snap['next_retry_at']}",
                f"Ошибка: {self._cycle_short_text(snap['last_error'], 42)}",
            ]
            if snap["current_error"] != "—":
                detail_parts.append(f"Ошибка цели: {self._cycle_short_text(snap['current_error'], 42)}")
            self._cycle_update_dashboard(metrics, " | ".join(detail_parts), summary_color)
            self._cycle_refresh_cycle_buttons()
            if selected_running:
                if snap["last_error"] != "—":
                    status_text = (
                        f"запущена, но последние попытки с ошибкой: {self._cycle_short_text(snap['last_error'], 36)} | "
                        f"активных кампаний={active_count} | позиция={position}"
                    )
                else:
                    status_text = f"запущена | активных кампаний={active_count} | позиция={position}"
                self._cycle_set_status(
                    status_text,
                    "#2FA572",
                )
            elif snap["enabled"]:
                self._cycle_set_status(
                    f"сохранена после перезапуска | целей={total} | позиция={position}",
                    "#F39C12",
                )
            else:
                self._cycle_set_status("остановлен", "gray70")
        except Exception:
            pass

        if self._cycle_status_after_id is not None:
            try:
                self.after_cancel(self._cycle_status_after_id)
            except Exception:
                pass
        # Throttle to 5 seconds on weak hardware to reduce UI thrashing / "not responding".
        # Rapid reconfigures of labels + table inside scrollable frame cause "floating" widgets.
        self._cycle_status_after_id = self.after(5000, self._cycle_update_status)

    def _cycle_load_targets(self):
        if getattr(self, '_cycle_ui_busy', False):
            return
        self._set_cycle_busy(True, "загружаю цели...")
        try:
            result = self._cycle_sync_targets_for_current_source()
            self.log.append(
                f"[Циклическая] [+] Цели обновлены: новых {result['added']}, "
                f"обновлено {result['updated']}, всего {result['count']}"
            )
            self._cycle_refresh_table()
            self._cycle_update_status()
        finally:
            self._set_cycle_busy(False)

    def _cycle_refresh_table(self):
        if getattr(self, '_cycle_ui_busy', False):
            return
        self._set_cycle_busy(True, "обновляю таблицу...")
        try:
            db = Database(self.app.config.db_path)
            campaign_id = db.get_or_create_cycle_campaign(self._cycle_campaign_name)
            targets = db.get_cycle_targets(campaign_id)
            state = db.load_cycle_state(campaign_id)
            db.close()
        finally:
            self._set_cycle_busy(False)

        total = len(targets)
        current_pos = int((state or {}).get("current_pos", 0) or 0)
        if total:
            current_pos %= total
        else:
            current_pos = 0
        self._cycle_targets = targets

        rows = []
        for idx, t in enumerate(self._cycle_targets):
            hs = int(t.get("hours_start", 0))
            he = int(t.get("hours_end", 23))
            hours = f"{hs:02d}-{he:02d}"
            imin = int(t.get("interval_min_seconds", 0) or 0)
            imax = int(t.get("interval_max_seconds", 0) or 0)
            if imin <= 0 and int(t.get("min_interval_minutes", 0) or 0) > 0:
                imin = int(t.get("min_interval_minutes", 0) or 0) * 60
                imax = imin
            if imax < imin:
                imax = imin
            if imin <= 0 and imax <= 0:
                interval = "0"
            elif imax <= 0 or imax == imin:
                interval = f"{imin}s"
            else:
                interval = f"{imin}-{imax}s"
            newm = str(t.get("min_new_messages", 0))
            fb = str(t.get("fallback_hours", 0))
            status = (t.get("status", "") or "active").strip()
            last_error = (t.get("last_error") or "").strip()
            if last_error:
                status = f"{status}: {self._cycle_short_text(last_error, 26)}"
            retry = (t.get("retry_after") or "")[11:16] if t.get("retry_after") else "—"
            last_sent = (t.get("last_sent_at") or "")[11:16] if t.get("last_sent_at") else "—"
            acc = t.get("last_account_phone") or "—"
            queue_offset = (idx - current_pos) % total if total else 0
            queue_label = "следующий" if queue_offset == 0 else f"+{queue_offset}"
            rows.append((t.get("link", ""), hours, interval, newm, fb, status, retry, last_sent, acc, queue_label))
        self.c_table.set_data(rows)

    def _cycle_edit_selected(self):
        row = self.c_table.get_selected_row()
        if not row:
            return
        link = row[0]
        target = None
        for t in self._cycle_targets:
            if t.get("link") == link:
                target = t
                break
        if not target:
            return

        dlg = CycleTargetRulesDialog(self, initial=target)
        self.wait_window(dlg)
        if not dlg.result:
            return

        db = Database(self.app.config.db_path)
        try:
            db.update_cycle_target_rules(
                int(target["id"]),
                dlg.result["hours_start"],
                dlg.result["hours_end"],
                dlg.result["interval_min_seconds"],
                dlg.result["interval_max_seconds"],
                dlg.result["min_new_messages"],
                dlg.result["fallback_hours"],
            )
        finally:
            db.close()
        self.log.append(f"[~] Правила обновлены: {link}")
        self._cycle_refresh_table()

    def _start_cycle(self):
        running_campaign_name = (self._cycle_campaign_name or "").strip() or "CycleBroadcast"
        self._append_log(f"[Циклическая] [~] Попытка запуска кампании: '{running_campaign_name}'")
        try:
            self._cycle_save_current_campaign_settings(running_campaign_name)
        except Exception as e:
            self._cycle_reject_start(
                f"Не удалось сохранить настройки кампании перед стартом: {e}",
                "ошибка: сохранение",
                "Старт не выполнен: настройки кампании не сохранены.",
            )
            return
        # Log id/name/target count/account count on single Start click (for manual verification)
        try:
            dblog = Database(self.app.config.db_path)
            try:
                cidlog = dblog.get_or_create_cycle_campaign(running_campaign_name)
                tlog = dblog.get_cycle_targets(cidlog)
                alog = dblog.get_cycle_campaign_account_phones(cidlog)
            finally:
                dblog.close()
            self.log.append(f"[Циклическая] [i] Click Старт: id={cidlog} name='{running_campaign_name}' targets={len(tlog)} accs_phones={len(alog)}")
        except Exception:
            pass

        # Важно: принудительно загружаем сохранённые настройки именно этой кампании
        # (шаблон, источник целей и т.д.). Это исправляет ситуацию, когда UI показывает
        # данные другой кампании или после переименования/перезапуска.
        try:
            self._cycle_select_campaign(running_campaign_name)
            # Гарантируем, что цели для этой кампании загружены в БД из её сохранённого шаблона/базы
            self._cycle_load_targets()
        except Exception as e:
            self._append_log(f"[Циклическая] [!] Ошибка синхронизации настроек кампании '{running_campaign_name}': {e}")

        runners = getattr(self, "_cycle_runners", None)
        if runners is None:
            self._cycle_runners = {}
            runners = self._cycle_runners
        existing = runners.get(running_campaign_name)
        existing_thread = existing.get("thread") if isinstance(existing, dict) else None
        if existing_thread is not None and existing_thread.is_alive():
            self.log.append(f"[Циклическая] [!] Кампания уже запущена: {running_campaign_name}")
            self._cycle_set_status(f"уже запущена: {running_campaign_name}", "#F39C12")
            self._cycle_refresh_cycle_buttons()
            return

        self._ai_proxy_warned = False

        # Relaxed guard: if UI template is not set, try to recover from saved settings for this campaign.
        # This makes the "Старт" button work for existing campaigns like "roma" even if the current UI state is stale.
        if self.c_targets_source_var.get() != "База" and self.c_template_var.get() == "—":
            try:
                self._refresh_cycle_templates()
                self._cycle_load_campaign_settings()  # this will try to set the template_var from saved template_id
            except Exception:
                pass
            if self.c_targets_source_var.get() != "База" and self.c_template_var.get() == "—":
                # Still bad — check if the campaign already has targets persisted in DB
                try:
                    db_chk = Database(self.app.config.db_path)
                    cid_chk = db_chk.get_or_create_cycle_campaign(running_campaign_name)
                    existing_cnt = len(db_chk.get_cycle_targets(cid_chk))
                    db_chk.close()
                except Exception:
                    existing_cnt = 0
                if existing_cnt > 0:
                    self.log.append(f"[Циклическая] [i] Для '{running_campaign_name}' шаблон не выбран в текущем UI, но в БД есть {existing_cnt} сохранённых целей — продолжаем запуск (используем сохранённые).")
                else:
                    self._cycle_reject_start(
                        "Выберите шаблон целей и нажмите 'Загрузить цели'.",
                        "ошибка: нет шаблона",
                        "Старт не выполнен: не выбран шаблон целей.",
                    )
                    return

        source_mode = self.c_message_source_var.get()
        msg_text = self.c_message.get("1.0", "end").strip()
        message_template_id = self._cycle_current_message_template_id() if source_mode == "Шаблоны" else None
        message_template_name = self.c_message_template_var.get() if message_template_id else ""
        if source_mode == "Шаблоны" and message_template_id:
            msg_text = self._cycle_current_message_template_content()
        if source_mode != "Избранное" and not msg_text:
            if source_mode == "Шаблоны":
                self._cycle_reject_start(
                    "Вставьте шаблоны сообщений в поле текста, по одному на строку.",
                    "ошибка: нет текста",
                    "Старт не выполнен: поле шаблонов сообщений пустое.",
                )
            else:
                self._cycle_reject_start(
                    "Введите текст сообщения или выберите источник 'Из Избранного'.",
                    "ошибка: нет текста",
                    "Старт не выполнен: выбран режим 'Вручную', но текст сообщения пустой.",
                )
            return

        try:
            sync_result = self._cycle_sync_targets_for_current_source()
        except Exception as e:
            self._cycle_reject_start(
                f"Не удалось загрузить цели: {e}",
                "ошибка: цели",
                "Старт не выполнен: ошибка при загрузке целей. Проверь шаблон чатов.",
            )
            return
        self._cycle_refresh_table()

        if sync_result["count"] <= 0:
            # Fallback для уже работающих кампаний (например "roma"): если цели были сохранены ранее в БД,
            # используем их, даже если текущий шаблон в UI недоступен или не выбран.
            try:
                db_fb = Database(self.app.config.db_path)
                cid_fb = db_fb.get_or_create_cycle_campaign(running_campaign_name)
                existing = db_fb.get_cycle_targets(cid_fb)
                db_fb.close()
                if existing:
                    self.log.append(f"[Циклическая] [i] Шаблон для '{running_campaign_name}' сейчас недоступен, но в БД есть {len(existing)} ранее загруженных целей — используем их.")
                    sync_result = {"count": len(existing), "added": 0, "updated": 0}
            except Exception:
                pass

            if sync_result["count"] <= 0:
                self._cycle_reject_start(
                    "Список целей пуст: нажмите 'Загрузить цели' или выберите другой шаблон.",
                    "ошибка: нет целей",
                    "Целей: 0 | Старт не выполнен: выбранный шаблон не дал ни одной цели.",
                )
                return

        stop_event = threading.Event()
        self._cycle_running_campaign_name = running_campaign_name
        self._cycle_stop_event = stop_event
        self._cycle_stop_requested_at = None
        # NOTE: _cycle_running + button flips moved closer to actual thread.start()
        # to guarantee truthful UI if setup fails after this point (prevents stuck "running").
        self._append_log("[~] Запуск циклической рассылки...")
        self._append_log(
            f"[+] Цикл запущен: целей={sync_result['count']} | источник={'База задач' if self.c_targets_source_var.get() == 'База' else self.c_template_var.get()}"
        )
        try:
            self._cycle_set_status("запуск...", "#F39C12")
        except Exception:
            pass
        try:
            self._cycle_update_status()
        except Exception:
            pass

        selected_account_fallback = self._resolve_phone(self.c_account_var.get()) if hasattr(self, '_resolve_phone') else self.c_account_var.get()
        targets_source = self.c_targets_source_var.get()
        template_name = self.c_template_var.get()
        message_source = source_mode
        unique_mode = self.c_unique_var.get()
        defaults = self._cycle_defaults()
        run_settings = self._cycle_run_settings()
        send_delay_min_seconds = int(run_settings["send_delay_min_seconds"])
        send_delay_max_seconds = int(run_settings["send_delay_max_seconds"])
        round_pause_seconds = int(run_settings["round_pause_seconds"])
        rotate_after_n_sends = int(run_settings.get("rotate_after_n_sends", 0) or 0)
        daily_actions_limit = int(run_settings.get("daily_actions_limit", 0) or 0)
        dry_run = bool(self.c_dry_run.get()) if hasattr(self, "c_dry_run") else False

        def _mask_proxy(p: str) -> str:
            p = (p or "").strip()
            if not p:
                return "—"
            if "@" in p:
                left, right = p.split("@", 1)
                if "://" in left:
                    scheme = left.split("://", 1)[0]
                    return f"{scheme}://***@{right}"
                return f"***@{right}"
            return p

        def _preflight_summary():
            try:
                db = Database(self.app.config.db_path)
                try:
                    campaign_id = db.get_or_create_cycle_campaign(running_campaign_name)
                    targets = db.get_cycle_targets(campaign_id)
                    state = db.load_cycle_state(campaign_id)
                    phones = db.get_cycle_campaign_account_phones(campaign_id)
                    accounts_all = db.get_active_accounts()
                finally:
                    db.close()

                accounts = accounts_all
                if phones:
                    by_phone = {a.phone: a for a in accounts_all}
                    accounts = [by_phone[p] for p in phones if p in by_phone]
                    self.log.append(f"  [i] Аккаунты кампании: {len(phones)} (из них активных: {len(accounts)})")
                else:
                    if selected_account_fallback != "Все активные":
                        accounts = [a for a in accounts_all if a.phone == selected_account_fallback]

                self.log.append("[i] Перед запуском (план):")
                self.log.append(f"  [i] Dry Run: {'ON' if dry_run else 'OFF'}")
                self.log.append(
                    f"  [i] Пауза между отправками: {send_delay_min_seconds}-{send_delay_max_seconds}с; "
                    f"между кругами: {round_pause_seconds}с"
                )
                if rotate_after_n_sends > 0:
                    self.log.append(f"  [i] Ротация аккаунта: после {rotate_after_n_sends} сообщений")
                else:
                    self.log.append("  [i] Ротация аккаунта: каждый чат (по умолчанию)")

                pos = int(state.get('current_pos', 0) or 0)
                last_link = (state.get("last_target_link") or "").strip() or "—"
                last_run = (state.get("last_run_at") or "")[:19].replace("T", " ") if state.get("last_run_at") else "—"
                last_acc = (state.get("last_account_phone") or "").strip() or "—"
                self.log.append(f"  [i] Состояние: pos={pos+1}/{max(len(targets),1)} | last={last_run} | {last_link} | {last_acc}")

                self.log.append(f"  [i] Целей в списке: {len(targets)}")
                if targets:
                    sample = [t.get("link", "") for t in targets[:15]]
                    sample = [s for s in sample if s]
                    if sample:
                        more = "" if len(targets) <= 15 else f" … (+{len(targets)-15})"
                        self.log.append("  [i] Чаты: " + ", ".join(sample) + more)

                self.log.append(f"  [i] Источник целей: {'База задач' if targets_source == 'База' else f'Шаблон ({template_name})'}")

                msg_preview = msg_text.replace("\n", " ").strip()
                if len(msg_preview) > 120:
                    msg_preview = msg_preview[:120] + "…"
                if message_source == "Шаблоны":
                    variants = _split_message_template_variants(msg_text)
                    tpl_note = f" ({message_template_name})" if message_template_name else ""
                    self.log.append(f"  [i] Текст: шаблон{tpl_note}, вариантов={len(variants)}, уникализация={unique_mode}")
                    if msg_preview:
                        self.log.append(f"  [i] Превью: {msg_preview}")
                elif message_source == "Избранное":
                    self.log.append(f"  [i] Текст: Избранное, уникализация={unique_mode}")
                else:
                    self.log.append(f"  [i] Текст: вручную, уникализация={unique_mode}")
                    if msg_preview:
                        self.log.append(f"  [i] Превью: {msg_preview}")

                if accounts:
                    self.log.append(f"  [i] Аккаунтов: {len(accounts)}")
                    for a in accounts[:10]:
                        disp = format_account(a.phone, getattr(a, "custom_name", ""))
                        self.log.append(f"    [i] {disp} | proxy={_mask_proxy(getattr(a, 'proxy', '') or '')}")
                    if len(accounts) > 10:
                        self.log.append(f"    [i] … (+{len(accounts)-10})")
                else:
                    self.log.append("  [!] Аккаунтов нет (не запустится)")

                self.log.append("  [i] Что будет происходить:")
                self.log.append("    [i] Бот идёт по списку чатов по кругу, продолжая с сохранённой позиции")
                self.log.append("    [i] Пропускает чаты по retry_after/ошибке/окну часов/недостатку новых сообщений")
                self.log.append("    [i] После каждой попытки ждёт паузу send min/max; после круга — паузу круга")
                if dry_run:
                    self.log.append("    [i] DRY-RUN: сообщения НЕ отправляются, вступление НЕ выполняется, один круг только в превью")
            except Exception as e:
                self.log.append(f"[!] Не удалось сформировать план запуска: {e}")

        try:
            _preflight_summary()
        except Exception as e:
            self._append_log(f"[!] Не удалось сформировать план запуска (диагностика): {e}")
            # Не прерываем запуск — диагностика не критична для старта воркера.

        run_id = f"{running_campaign_name}:{time.monotonic_ns()}"

        def thread():
            log_queue = self.app.log_queue
            _thread_local.log_handler = lambda msg: log_queue.put(("cycle_log", msg))
            _thread_local.log_tag = "cycle"
            log_queue.put(("cycle_log", f"[i] Воркер реально стартовал: {running_campaign_name}"))
            loop = asyncio.new_event_loop()

            async def do():
                from sender import TelegramSender
                from parser import ensure_chat_access
                from datetime import timedelta
                from file_logger import log_event

                cfg = self.app.config
                db = Database(cfg.db_path)

                campaign_id = db.get_or_create_cycle_campaign(running_campaign_name)
                if stop_event.is_set():
                    try:
                        db.set_cycle_campaign_enabled(campaign_id, False)
                    finally:
                        db.close()
                    log_queue.put(("cycle_log", "[=] Старт отменён: остановка была запрошена до включения кампании"))
                    return

                if targets_source == "База":
                    tpl_id = None
                else:
                    tpl = self._cycle_template_by_name.get(template_name)
                    tpl_id = int(tpl["id"]) if tpl else None

                db.update_cycle_campaign(
                    campaign_id,
                    targets_source="tasks" if targets_source == "База" else "template",
                    template_id=tpl_id,
                    message_source="saved" if message_source == "Избранное" else ("templates" if message_source == "Шаблоны" else "manual"),
                    message_text=msg_text,
                    unique_mode=unique_mode,
                    enabled=True,
                    account_filter="" if selected_account_fallback == "Все активные" else selected_account_fallback,
                    rotate_after_n_sends=rotate_after_n_sends,
                    send_delay_min_seconds=send_delay_min_seconds,
                    send_delay_max_seconds=send_delay_max_seconds,
                    round_pause_seconds=round_pause_seconds,
                    daily_actions_limit=daily_actions_limit,
                    message_template_id=message_template_id if message_source == "Шаблоны" else None,
                    default_hours_start=int(defaults["hours_start"]),
                    default_hours_end=int(defaults["hours_end"]),
                    default_interval_min_seconds=int(defaults["interval_min_seconds"]),
                    default_interval_max_seconds=int(defaults["interval_max_seconds"]),
                    default_min_new_messages=int(defaults["min_new_messages"]),
                    default_fallback_hours=int(defaults["fallback_hours"]),
                )
                if stop_event.is_set():
                    try:
                        db.set_cycle_campaign_enabled(campaign_id, False)
                    finally:
                        db.close()
                    log_queue.put(("cycle_log", "[=] Старт отменён: остановка была запрошена сразу после сохранения настроек"))
                    return

                state = db.load_cycle_state(campaign_id)
                current_pos = int(state.get("current_pos", 0) or 0)
                last_acc_phone = (state.get("last_account_phone") or "").strip()
                account_send_count = int(state.get("last_account_send_count", 0) or 0)
                if not last_acc_phone:
                    account_send_count = 0
                acc_pos = 0
                acc_pos_init_done = False
                empty_saved_accounts: set[str] = set()
                templates_cache = _split_message_template_variants(msg_text)
                first_loop_logged = False
                last_no_accounts_log_at = 0.0

                def _allowed_hours(now_h: int, hs: int, he: int) -> bool:
                    if hs <= he:
                        return hs <= now_h <= he
                    return now_h >= hs or now_h <= he

                def _next_window_start(hs: int) -> str:
                    now = datetime.now()
                    nxt = now.replace(hour=hs, minute=0, second=0, microsecond=0)
                    if nxt <= now:
                        nxt = nxt + timedelta(days=1)
                    return nxt.isoformat(timespec="seconds")

                def _cycle_error(acc_phone: str, target_link: str, status: str, error: str):
                    short_error = (error or status or "error")[:200]
                    _emit_cycle_progress(
                        account=acc_phone,
                        current_target=target_link,
                        last_error=short_error,
                        phase=status or "error",
                    )
                    try:
                        db.add_cycle_state_stats(
                            campaign_id,
                            sent_inc=0,
                            error_inc=1,
                            last_error=short_error,
                        )
                    except Exception:
                        pass
                    try:
                        log_event(
                            module="cycle",
                            campaign=running_campaign_name,
                            account=acc_phone or "",
                            target=target_link or "",
                            action="worker",
                            status=status or "error",
                            error=short_error,
                        )
                    except Exception:
                        pass

                def _emit_cycle_progress(
                    account: str = "",
                    current_target: str = "",
                    next_target: str = "",
                    last_success_at: str = "",
                    last_error: str = "",
                    phase: str = "",
                ):
                    try:
                        log_queue.put((
                            "cycle_progress",
                            {
                                "campaign": running_campaign_name,
                                "account": account or "",
                                "current_target": current_target or "",
                                "next_target": next_target or "",
                                "last_success_at": last_success_at or "",
                                "last_error": last_error or "",
                                "phase": phase or "",
                            },
                        ))
                    except Exception:
                        pass

                async def _cycle_wait(coro, label: str, timeout: float, acc_phone: str = "", target_link: str = ""):
                    try:
                        return await _await_interruptibly(
                            coro,
                            stop_event,
                            op_name="циклическая рассылка",
                            label=label,
                            timeout=timeout,
                            account=acc_phone,
                            target=target_link,
                        )
                    except asyncio.TimeoutError:
                        msg = f"{label}: таймаут {int(timeout)}с"
                        log_queue.put(("cycle_log", f"[!] {msg}"))
                        _cycle_error(acc_phone, target_link, "timeout", msg)
                        return None
                    except OperationInterrupted:
                        raise
                    except Exception as e:
                        msg = f"{label}: {type(e).__name__}: {e}"
                        log_queue.put(("cycle_log", f"[-] {msg}"))
                        _cycle_error(acc_phone, target_link, "error", msg)
                        return None

                try:
                    log_queue.put(("cycle_log",
                                   f"[i] Пауза между отправками: {send_delay_min_seconds}-{send_delay_max_seconds}с; "
                                   f"между кругами: {round_pause_seconds}с"))
                    while not stop_event.is_set():
                        targets = db.get_cycle_targets(campaign_id)
                        phones = db.get_cycle_campaign_account_phones(campaign_id)
                        accounts_all = db.get_active_accounts()
                        if phones:
                            by_phone = {a.phone: a for a in accounts_all}
                            accounts = [by_phone[p] for p in phones if p in by_phone]
                        else:
                            accounts = accounts_all
                            if selected_account_fallback != "Все активные":
                                accounts = [a for a in accounts if a.phone == selected_account_fallback]

                        if not first_loop_logged:
                            log_queue.put(("cycle_log",
                                           f"[i] Загружено целей: {len(targets)}, аккаунтов: {len(accounts)}, "
                                           f"позиция: {current_pos + 1 if targets else 0}"))
                            first_loop_logged = True

                        if not targets:
                            if dry_run:
                                log_queue.put(("cycle_log", "[DRY] Нет целей для превью"))
                                break
                            await _sleep_interruptibly(5, stop_event, op_name="циклическая рассылка")
                            continue
                        if not accounts:
                            if dry_run:
                                log_queue.put(("cycle_log", "[DRY] Нет активных аккаунтов для превью"))
                                break
                            wait_sleep = 10
                            now_mono = time.monotonic()
                            if now_mono - last_no_accounts_log_at >= 60:
                                wait_items = []
                                try:
                                    health_rows = db.get_accounts_health()
                                    wanted = set(phones or [])
                                    if not wanted:
                                        wanted = {h.get("phone") for h in health_rows if h.get("phone") == selected_account_fallback}
                                    now_dt = datetime.now()
                                    for h in health_rows:
                                        phone = h.get("phone") or ""
                                        if wanted and phone not in wanted:
                                            continue
                                        if not h.get("is_active", True):
                                            continue
                                        for field in ("paused_until", "flood_until"):
                                            until_raw = (h.get(field) or "").strip()
                                            if not until_raw:
                                                continue
                                            try:
                                                until_dt = datetime.fromisoformat(until_raw)
                                            except Exception:
                                                continue
                                            delta = (until_dt - now_dt).total_seconds()
                                            if delta > 0:
                                                wait_items.append((delta, phone, field, until_raw, h.get("why") or field))
                                    if wait_items:
                                        wait_items.sort(key=lambda item: item[0])
                                        delta, phone, field, until_raw, why = wait_items[0]
                                        wait_sleep = max(10, min(int(delta), 300))
                                        log_queue.put((
                                            "cycle_log",
                                            f"[~] Нет активных аккаунтов. Ближайший доступный: {phone} at {until_raw[:19].replace('T', ' ')} ({why}). Остановка остаётся доступной.",
                                        ))
                                    else:
                                        log_queue.put(("cycle_log", "[~] Нет активных аккаунтов. Жду и проверю снова; остановка остаётся доступной."))
                                except Exception as e:
                                    log_queue.put(("cycle_log", f"[~] Нет активных аккаунтов; не удалось посчитать ожидание: {type(e).__name__}"))
                                last_no_accounts_log_at = now_mono
                            await _sleep_interruptibly(wait_sleep, stop_event, op_name="cycle", progress="no active accounts")
                            continue

                        if not acc_pos_init_done and last_acc_phone:
                            for i, a in enumerate(accounts):
                                if a.phone == last_acc_phone:
                                    acc_pos = i
                                    break
                            acc_pos_init_done = True

                        current_pos = current_pos % len(targets)
                        made_send = False
                        account_blocked = False

                        for _ in range(len(targets)):
                            if stop_event.is_set():
                                break

                            t = targets[current_pos]
                            target_id = int(t["id"])
                            link = t.get("link", "")
                            now = datetime.now()
                            last_sent = t.get("last_sent_at") or ""
                            acc_phone_preview = accounts[acc_pos % len(accounts)].phone if accounts else ""
                            next_link_preview = ""
                            if len(targets) > 1:
                                next_link_preview = targets[(current_pos + 1) % len(targets)].get("link", "")
                            _emit_cycle_progress(
                                account=acc_phone_preview,
                                current_target=link,
                                next_target=next_link_preview,
                                phase="attempt",
                            )
                            _raise_if_stop_requested(
                                stop_event,
                                op_name="циклическая рассылка",
                                account=acc_phone_preview,
                                target=link,
                                progress=f"позиция={current_pos + 1}/{len(targets)}",
                            )

                            def _bump_state(acc_phone: str = "", text_preview: str = ""):
                                nonlocal current_pos
                                if dry_run:
                                    current_pos = (current_pos + 1) % len(targets)
                                    return
                                try:
                                    next_pos = (current_pos + 1) % len(targets)
                                    db.update_cycle_state(
                                        campaign_id,
                                        current_pos=next_pos,
                                        last_target_link=link,
                                        last_run_at=now.isoformat(timespec="seconds"),
                                        last_account_phone=acc_phone or "",
                                        last_text_preview=text_preview or "",
                                    )
                                    current_pos = next_pos
                                except Exception:
                                    current_pos = (current_pos + 1) % len(targets)

                            retry_after = t.get("retry_after") or ""
                            if retry_after:
                                try:
                                    ra = datetime.fromisoformat(retry_after)
                                    if ra > now:
                                        _bump_state()
                                        continue
                                except Exception:
                                    pass

                            if t.get("status") == "error":
                                _bump_state()
                                continue

                            hs = int(t.get("hours_start", 0) or 0)
                            he = int(t.get("hours_end", 23) or 23)
                            if not _allowed_hours(now.hour, hs, he):
                                if dry_run:
                                    log_queue.put(("cycle_log", f"[DRY] {link}: пропуск по окну часов"))
                                else:
                                    db.set_cycle_target_status(target_id, "active", _next_window_start(hs), "hours_window")
                                _bump_state()
                                continue

                            min_new = int(t.get("min_new_messages", 0) or 0)
                            fallback_hours = int(t.get("fallback_hours", 0) or 0)
                            last_seen = int(t.get("last_seen_msg_id", 0) or 0)
                            latest_id = 0

                            acc = accounts[acc_pos % len(accounts)]

                            if rotate_after_n_sends > 0:
                                if last_acc_phone and acc.phone != last_acc_phone:
                                    account_send_count = 0
                                    if not dry_run:
                                        try:
                                            db.set_cycle_state_account_send_count(campaign_id, 0)
                                        except Exception:
                                            pass
                                last_acc_phone = acc.phone
                            try:
                                sender = TelegramSender(acc, cfg, db)
                            except Exception as e:
                                detail = f"{type(e).__name__}: {e}"
                                log_queue.put(("cycle_log", f"[-] {acc.phone}: не удалось создать Telegram-клиент: {detail}"))
                                _cycle_error(acc.phone, link, "client_init_error", detail)
                                _bump_state(acc_phone=acc.phone)
                                acc_pos += 1
                                continue
                            log_queue.put(("cycle_log", f"[i] {link}: подключаю {acc.phone}"))
                            connected = await _cycle_wait(
                                sender.connect(),
                                f"{acc.phone}: подключение",
                                45,
                                acc.phone,
                                link,
                            )
                            if not connected:
                                log_queue.put(("cycle_log", f"[!] {acc.phone}: подключение не удалось — переключаю аккаунт/цель"))
                                acc_pos += 1
                                if rotate_after_n_sends > 0:
                                    account_send_count = 0
                                    if not dry_run:
                                        try:
                                            db.set_cycle_state_account_send_count(campaign_id, 0)
                                        except Exception:
                                            pass
                                _bump_state()
                                continue

                            try:
                                _raise_if_stop_requested(
                                    stop_event,
                                    op_name="циклическая рассылка",
                                    account=acc.phone,
                                    target=link,
                                    progress=f"позиция={current_pos + 1}/{len(targets)}",
                                )
                                allow_fallback = False
                                if fallback_hours > 0 and last_sent:
                                    try:
                                        ls = datetime.fromisoformat(last_sent)
                                        allow_fallback = now >= (ls + timedelta(hours=fallback_hours))
                                    except Exception:
                                        allow_fallback = False

                                if min_new > 0 and last_seen > 0:
                                    latest_result = await _cycle_wait(
                                        sender.get_latest_message_id(link),
                                        f"{link}: чтение последнего сообщения",
                                        25,
                                        acc.phone,
                                        link,
                                    )
                                    if latest_result is None:
                                        _bump_state(acc_phone=acc.phone)
                                        continue
                                    latest_id = int(latest_result or 0)
                                    new_count = max(int(latest_id) - int(last_seen), 0) if latest_id else 0
                                    if new_count < min_new and not allow_fallback:
                                        _bump_state(acc_phone=acc.phone)
                                        continue

                                access_result = await _cycle_wait(
                                    ensure_chat_access(sender.client, link, dry_run=dry_run),
                                    f"{link}: проверка доступа",
                                    35,
                                    acc.phone,
                                    link,
                                )
                                if access_result is None:
                                    if not dry_run:
                                        db.set_cycle_target_status(
                                            target_id,
                                            "active",
                                            (now + timedelta(minutes=30)).isoformat(timespec="seconds"),
                                            "access_check_timeout",
                                        )
                                    _bump_state(acc_phone=acc.phone)
                                    continue
                                decision, reason, join_retry = access_result
                                if decision != "ok":
                                    if dry_run:
                                        log_queue.put(("cycle_log", f"[DRY] {link}: доступ не подтверждён ({reason})"))
                                    elif decision == "waiting":
                                        db.set_cycle_target_status(target_id, "active", join_retry, f"join:{reason}")
                                    else:
                                        db.set_cycle_target_status(target_id, "error", "", f"join:{reason}")
                                        try:
                                            db.add_cycle_state_stats(campaign_id, sent_inc=0, error_inc=1, last_error=f"join:{reason}")
                                        except Exception:
                                            pass
                                    _bump_state(acc_phone=acc.phone)
                                    continue

                                if message_source == "Избранное":
                                    saved = await _cycle_wait(
                                        sender.get_saved_messages(limit=30),
                                        f"{acc.phone}: чтение Избранного",
                                        25,
                                        acc.phone,
                                        link,
                                    )
                                    saved_texts = [s for s in (saved or []) if (s or "").strip()]
                                    log_queue.put((
                                        "cycle_log",
                                        f"[i] Кампания={running_campaign_name} | Аккаунт={acc.phone} | "
                                        f"Источник=Избранное: {acc.phone}/Saved Messages | текстов={len(saved_texts)}",
                                    ))
                                    if not saved_texts:
                                        empty_saved_accounts.add(acc.phone)
                                        log_queue.put((
                                            "cycle_log",
                                            f"[!] Кампания={running_campaign_name} | {acc.phone}: в Избранном нет текстовых сообщений. "
                                            "Кампания не должна крутиться с пустым текстом.",
                                        ))
                                        _cycle_error(acc.phone, link, "empty_saved_messages", "empty Saved Messages")
                                        if len(empty_saved_accounts) >= len(accounts):
                                            log_queue.put((
                                                "cycle_log",
                                                "[!] Кампания остановлена: у всех выбранных аккаунтов пустое Избранное. "
                                                "Добавь текст в Saved Messages или выбери источник 'Вручную/Шаблоны'.",
                                            ))
                                            stop_event.set()
                                        _bump_state(acc_phone=acc.phone)
                                        acc_pos += 1
                                        continue
                                    raw = random.choice(saved_texts)
                                elif message_source == "Шаблоны":
                                    raw = random.choice(templates_cache) if templates_cache else msg_text
                                else:
                                    raw = msg_text

                                if not (raw or "").strip():
                                    log_queue.put(("cycle_log",
                                                   f"[!] {link}: пустой текст. Заполни текст вручную/шаблоны "
                                                   f"или добавь текст в Избранное у {acc.phone}"))
                                    _cycle_error(acc.phone, link, "empty_text", "empty text")
                                    _bump_state(acc_phone=acc.phone)
                                    continue

                                final_text = self._apply_unique(raw, unique_mode)
                                preview50 = final_text.replace("\n", " ").strip()
                                if len(preview50) > 50:
                                    preview50 = preview50[:50] + "…"
                                if message_source == "Избранное":
                                    source_label = f"Избранное: {acc.phone}/Saved Messages"
                                elif message_source == "Шаблоны":
                                    source_label = f"Шаблон текста: {message_template_name}" if message_template_name else "Шаблоны: строки поля"
                                else:
                                    source_label = "Вручную"
                                log_queue.put((
                                    "cycle_log",
                                    f"[i] Перед отправкой | campaign={running_campaign_name} | account={acc.phone} | "
                                    f"target={link} | source={source_label} | text='{preview50}'",
                                ))
                                if dry_run:
                                    status = "dry_run"
                                    raw_status = "dry_run"
                                    error_detail = ""
                                    preview = final_text.replace("\n", " ").strip()
                                    if len(preview) > 120:
                                        preview = preview[:120] + "…"
                                    log_queue.put(("cycle_log", f"[DRY] {link} ← {acc.phone}: {preview}"))
                                    log_event(module="cycle", campaign=running_campaign_name, account=acc.phone, target=link,
                                              action="send", status="dry_run", error="")
                                else:
                                    raw_status = await _cycle_wait(
                                        sender.send_broadcast_message(
                                            link,
                                            final_text,
                                            daily_actions_limit=daily_actions_limit,
                                        ),
                                        f"{link}: отправка",
                                        60,
                                        acc.phone,
                                        link,
                                    )
                                    if raw_status is None:
                                        raw_status = "error:send_timeout"
                                    status = raw_status.split(":", 1)[0]
                                    error_detail = raw_status if raw_status != status else ""

                                if not dry_run:
                                    db.log_send(SendLog(
                                        account_phone=acc.phone,
                                        target_group=link,
                                        message_text=final_text[:200],
                                        status=status,
                                        error_detail=error_detail[:200],
                                        timestamp=now.isoformat(timespec="seconds"),
                                    ))

                                if status in ("daily_limit", "paused", "min_interval"):
                                    account_blocked = True
                                    wait_note = error_detail or raw_status or status
                                    log_queue.put((
                                        "cycle_log",
                                        f"[~] {acc.phone}: account limiter {wait_note}; target is not advanced",
                                    ))
                                    log_event(
                                        module="cycle",
                                        campaign=running_campaign_name,
                                        account=acc.phone,
                                        target=link,
                                        action="send",
                                        status=status,
                                        error=wait_note[:200],
                                    )
                                    _emit_cycle_progress(
                                        account=acc.phone,
                                        current_target=link,
                                        next_target=next_link_preview,
                                        last_error=wait_note[:200],
                                        phase=status,
                                    )
                                    if status in ("daily_limit", "paused"):
                                        acc_pos += 1
                                        account_send_count = 0
                                        try:
                                            db.set_cycle_state_account_send_count(campaign_id, 0)
                                        except Exception:
                                            pass
                                    break
                                elif status in ("sent", "dry_run"):
                                    msg_id = 0
                                    try:
                                        msg_id = int(raw_status.split(":", 1)[1])
                                    except Exception:
                                        msg_id = 0
                                    baseline_id = 0
                                    if not dry_run:
                                        if latest_id == 0:
                                            latest_result = await _cycle_wait(
                                                sender.get_latest_message_id(link),
                                                f"{link}: обновление последнего сообщения",
                                                25,
                                                acc.phone,
                                                link,
                                            )
                                            latest_id = int(latest_result or 0)
                                        baseline_id = msg_id or latest_id

                                    imin = int(t.get("interval_min_seconds", 0) or 0)
                                    imax = int(t.get("interval_max_seconds", 0) or 0)
                                    if imin <= 0 and int(t.get("min_interval_minutes", 0) or 0) > 0:
                                        imin = int(t.get("min_interval_minutes", 0) or 0) * 60
                                        imax = imin
                                    if imin < 0:
                                        imin = 0
                                    if imax < imin:
                                        imax = imin
                                    next_retry = ""
                                    if imin > 0 or imax > 0:
                                        if imax <= 0:
                                            imax = imin
                                        wait_s = random.uniform(imin, imax) if imax > imin else float(imin)
                                        next_retry = (now + timedelta(seconds=max(int(wait_s), 1))).isoformat(timespec="seconds")

                                    if not dry_run:
                                        db.update_cycle_target_after_send(
                                            target_id,
                                            now.isoformat(timespec="seconds"),
                                            baseline_id,
                                            acc.phone,
                                            final_text,
                                            retry_after=next_retry,
                                        )
                                        try:
                                            db.add_cycle_state_stats(campaign_id, sent_inc=1, error_inc=0, last_error="")
                                        except Exception:
                                            pass
                                        if rotate_after_n_sends > 0:
                                            account_send_count += 1
                                            try:
                                                db.set_cycle_state_account_send_count(campaign_id, account_send_count)
                                            except Exception:
                                                pass
                                    made_send = True
                                    if not dry_run:
                                        log_queue.put(("cycle_log", f"[SUCCESS] {acc.phone} → {link}  (campaign={running_campaign_name})"))
                                        log_event(module="cycle", campaign=running_campaign_name, account=acc.phone, target=link,
                                                  action="send", status="sent", error="")
                                        if rotate_after_n_sends > 0 and account_send_count >= rotate_after_n_sends:
                                            acc_pos += 1
                                            account_send_count = 0
                                            try:
                                                db.set_cycle_state_account_send_count(campaign_id, 0)
                                            except Exception:
                                                pass
                                elif status == "slow_mode":
                                    try:
                                        wait_s = int(raw_status.split(":", 1)[1])
                                    except Exception:
                                        wait_s = 60
                                    ra = (now + timedelta(seconds=max(wait_s, 1))).isoformat(timespec="seconds")
                                    db.set_cycle_target_status(target_id, "active", ra, raw_status)
                                    log_queue.put(("cycle_log", f"[~] {link}: slow_mode {wait_s}s"))
                                    log_event(module="cycle", campaign=running_campaign_name, account=acc.phone, target=link,
                                              action="send", status="slow_mode", error=str(wait_s))
                                elif status == "need_subscription":
                                    # Per user's request: do not lock the target for 24h.
                                    # Just record the error, leave the target active so it can be retried
                                    # naturally in the next full cycle of the campaign.
                                    db.set_cycle_target_status(target_id, "active", None, "need_subscription")
                                    log_queue.put(("cycle_log", f"[~] {link}: need_subscription — пропущено, попробуем в следующем цикле"))
                                    log_event(module="cycle", campaign=running_campaign_name, account=acc.phone, target=link,
                                              action="send", status="need_subscription", error="")
                                elif status in ("no_permission", "private"):
                                    db.set_cycle_target_status(target_id, "error", "", status)
                                    log_queue.put(("cycle_log", f"[x] {link}: {status}"))
                                    log_event(module="cycle", campaign=running_campaign_name, account=acc.phone, target=link,
                                              action="send", status=status, error="")
                                    try:
                                        db.add_cycle_state_stats(campaign_id, sent_inc=0, error_inc=1, last_error=status)
                                    except Exception:
                                        pass
                                elif status in ("flood_wait", "banned", "chat_banned"):
                                    if status == "flood_wait":
                                        try:
                                            wait_s = int(raw_status.split(":", 1)[1])
                                        except Exception:
                                            wait_s = 3600
                                        ra = (now + timedelta(seconds=max(wait_s, 60))).isoformat(timespec="seconds")
                                        db.set_cycle_target_status(target_id, "active", ra, raw_status)
                                        log_queue.put(("cycle_log", f"[~] {link}: flood_wait {wait_s}s для {acc.phone}"))
                                    elif status == "banned":
                                        # Global account ban signal (e.g. PeerFloodError). Sender already deactivated.
                                        db.set_cycle_target_status(target_id, "error", "", f"{acc.phone}:banned (global)")
                                        log_queue.put(("cycle_log", f"[x] {link}: {acc.phone} GLOBAL BAN — аккаунт деактивирован, цель пропущена"))
                                    else:
                                        # chat_banned = UserBannedInChannelError etc. per specific chat/target.
                                        # Per user's request (for testing): do NOT mark as permanent error and do NOT lock for long.
                                        # Just record the per-target issue, leave active so the target will be retried
                                        # in the next full cycle.
                                        db.set_cycle_target_status(target_id, "active", None, f"{acc.phone}:chat_banned")
                                        log_queue.put(("cycle_log", f"[x] {link}: {acc.phone} banned/запрещён в этом чате (per-target) — пропущено, попробуем в следующем цикле"))
                                    log_event(module="cycle", campaign=running_campaign_name, account=acc.phone, target=link,
                                              action="send", status=status, error=raw_status)
                                    try:
                                        db.add_cycle_state_stats(campaign_id, sent_inc=0, error_inc=1, last_error=status)
                                    except Exception:
                                        pass
                                    if rotate_after_n_sends > 0:
                                        acc_pos += 1
                                        account_send_count = 0
                                        try:
                                            db.set_cycle_state_account_send_count(campaign_id, 0)
                                        except Exception:
                                            pass
                                else:
                                    ra = (now + timedelta(minutes=30)).isoformat(timespec="seconds")
                                    db.set_cycle_target_status(target_id, "active", ra, status)
                                    log_queue.put(("cycle_log", f"[!] {link}: error"))
                                    log_event(module="cycle", campaign=running_campaign_name, account=acc.phone, target=link,
                                              action="send", status="error", error=status)
                                    try:
                                        db.add_cycle_state_stats(campaign_id, sent_inc=0, error_inc=1, last_error=status)
                                    except Exception:
                                        pass

                                if status in ("sent", "dry_run"):
                                    _emit_cycle_progress(
                                        account=acc.phone,
                                        current_target=link,
                                        next_target=next_link_preview,
                                        last_success_at=now.isoformat(timespec="seconds"),
                                        last_error="",
                                        phase=status,
                                    )
                                else:
                                    _emit_cycle_progress(
                                        account=acc.phone,
                                        current_target=link,
                                        next_target=next_link_preview,
                                        last_error=(error_detail or raw_status or status)[:200],
                                        phase=status,
                                    )

                                if not dry_run:
                                    db.update_cycle_state(
                                        campaign_id,
                                        current_pos=(current_pos + 1) % len(targets),
                                        last_target_link=link,
                                        last_run_at=now.isoformat(timespec="seconds"),
                                        last_account_phone=acc.phone,
                                        last_text_preview=final_text,
                                    )

                            finally:
                                try:
                                    await asyncio.wait_for(sender.disconnect(), timeout=8)
                                except Exception as e:
                                    log_queue.put(("cycle_log", f"[!] {acc.phone}: disconnect не завершился быстро ({type(e).__name__})"))

                            current_pos = (current_pos + 1) % len(targets)
                            if rotate_after_n_sends <= 0:
                                acc_pos += 1
                            delay = random.uniform(send_delay_min_seconds, send_delay_max_seconds) if send_delay_max_seconds > send_delay_min_seconds else float(send_delay_min_seconds)
                            if dry_run:
                                log_queue.put(("cycle_log", f"[DRY] Пауза {delay:.0f}с пропущена"))
                            else:
                                await _sleep_interruptibly(
                                    delay,
                                    stop_event,
                                    op_name="циклическая рассылка",
                                    account=acc.phone,
                                    target=link,
                                    progress=f"позиция={(current_pos % len(targets)) + 1}/{len(targets)}",
                                )

                        if stop_event.is_set():
                            break

                        if account_blocked:
                            await _sleep_interruptibly(
                                10,
                                stop_event,
                                op_name="cycle",
                                progress="account limiter",
                            )
                            continue

                        if dry_run:
                            log_queue.put(("cycle_log", "[=] DRY-RUN: один круг завершён, цикл остановлен"))
                            break

                        if round_pause_seconds > 0:
                            log_queue.put(("cycle_log", f"[~] Круг завершён — пауза {round_pause_seconds}с"))
                            await _sleep_interruptibly(
                                round_pause_seconds,
                                stop_event,
                                op_name="циклическая рассылка",
                                progress="между кругами",
                            )
                        elif not made_send:
                            await _sleep_interruptibly(
                                20,
                                stop_event,
                                op_name="циклическая рассылка",
                                progress="ожидание новых условий",
                            )

                except OperationInterrupted as e:
                    log_queue.put(("cycle_log", str(e)))
                    log_queue.put(("cycle_log", "[=] Циклическая рассылка остановлена штатно"))

                finally:
                    try:
                        db.set_cycle_campaign_enabled(campaign_id, False)
                    except Exception:
                        pass
                    db.close()

            try:
                _run_loop(loop, do())
            except Exception as e:
                import traceback
                detail = "".join(traceback.format_exception_only(type(e), e)).strip()
                log_queue.put(("cycle_log", f"[-] Ошибка цикла: {detail}"))
            finally:
                _thread_local.log_handler = None
                log_queue.put(("cycle_done", {"campaign": running_campaign_name, "run_id": run_id}))

        cycle_thread = threading.Thread(target=thread, daemon=True)
        launched_ok = False
        try:
            # Устанавливаем правдивое состояние "запущено" непосредственно перед созданием/стартом воркера.
            # Если здесь или раньше будет исключение — early returns/guard'ы уже отработали,
            # а сюда не дойдём или попадём в except ниже и UI вернётся в stopped.
            self._cycle_running = True
            if (self._cycle_campaign_name or "").strip() == running_campaign_name:
                self.btn_cycle_start.configure(state="disabled")
                self.btn_cycle_stop.configure(state="normal", text="■ Стоп")
            try:
                self.btn_stop_current.configure(state="normal", text="■ Остановить циклическую")
            except Exception:
                pass

            runner = {
                "thread": cycle_thread,
                "stop_event": stop_event,
                "stop_requested_at": None,
                "run_id": run_id,
            }
            self._cycle_runners[running_campaign_name] = runner
            self._cycle_thread = cycle_thread
            self._cycle_stop_event = stop_event

            # Пометить аккаунты кампании как занятые (синяя подсветка + контекст)
            try:
                dbm = Database(self.app.config.db_path)
                camp_phones = dbm.get_cycle_campaign_account_phones(
                    dbm.get_or_create_cycle_campaign(running_campaign_name)
                )
                dbm.close()
                if camp_phones:
                    self.mark_account_busy(camp_phones, f"Циклическая «{running_campaign_name}»")
                else:
                    # общий пул — помечаем все активные на момент старта
                    dbm2 = Database(self.app.config.db_path)
                    all_active = [a.phone for a in dbm2.get_active_accounts()]
                    dbm2.close()
                    if all_active:
                        self.mark_account_busy(all_active, f"Циклическая «{running_campaign_name}» (общий пул)")
            except Exception:
                pass

            self._append_log(f"[Циклическая] [i] Стартую фоновой воркер: {running_campaign_name}")
            cycle_thread.start()
            self._append_log(f"[Циклическая] [i] Поток создан: {running_campaign_name} | alive={cycle_thread.is_alive()}")
            try:
                self.after(2000, lambda name=running_campaign_name, rid=run_id: self._cycle_verify_worker_start(name, rid))
            except Exception:
                pass
            self._cycle_refresh_cycle_buttons()
            launched_ok = True
        except Exception as e:
            self._append_log(f"[Циклическая] [!] Ошибка регистрации/старта воркера: {e}")
            # Сброс UI чтобы не остаться в ложном "запущен" состоянии (P0 регрессия)
            try:
                busy = self.get_busy_accounts()
                self.mark_account_free([
                    p for p, ctx in busy.items()
                    if "Циклическая" in (ctx or "") and running_campaign_name in (ctx or "")
                ])
                runners = getattr(self, "_cycle_runners", None) or {}
                runners.pop(running_campaign_name, None)
                self._cycle_running = bool(self._cycle_active_names())
                if (getattr(self, "_cycle_running_campaign_name", "") or "") == running_campaign_name:
                    self._cycle_running_campaign_name = (self._cycle_active_names() or [""])[0]
                if (self._cycle_campaign_name or "").strip() == running_campaign_name:
                    self.btn_cycle_start.configure(state="normal")
                    self.btn_cycle_stop.configure(state="disabled", text="■ Стоп")
                try:
                    if not getattr(self, "_cycle_active_count", lambda: 0)() and not getattr(self, "_running", False):
                        self.btn_stop_current.configure(state="disabled", text="■ Остановить текущий процесс")
                except Exception:
                    pass
                self._cycle_set_status("остановлен (ошибка старта)", "gray70")
            except Exception:
                pass
            return
        # при успехе early _cycle_running=True остаётся, воркер зарегистрирован и запущен

    def _cycle_verify_worker_start(self, campaign_name: str, run_id: str):
        try:
            runner = self._cycle_get_runner(campaign_name)
            if not isinstance(runner, dict) or runner.get("run_id") != run_id:
                return
            if self._cycle_runner_alive(runner):
                self.log.append(f"[Циклическая] [i] Воркер подтверждён: {campaign_name}")
                return
            self.log.append(f"[Циклическая] [!] Воркер '{campaign_name}' умер сразу после старта. Смотри ошибку выше в логе; кампания не считается запущенной.")
            self._cycle_finish_stopped_ui(campaign_name)
        except Exception as e:
            try:
                self.log.append(f"[Циклическая] [!] Ошибка проверки воркера '{campaign_name}': {e}")
            except Exception:
                pass

    def _stop_cycle(self, campaign_name: str | None = None):
        campaign_name = (campaign_name or self._cycle_campaign_name or "").strip()
        runners = getattr(self, "_cycle_runners", None)
        if runners is None:
            self._cycle_runners = {}
            runners = self._cycle_runners

        runner = runners.get(campaign_name) if campaign_name else None
        if runner is None and not campaign_name:
            active = [(name, r) for name, r in runners.items() if self._cycle_runner_alive(r)]
            if len(active) == 1:
                campaign_name, runner = active[0]

        thread = runner.get("thread") if isinstance(runner, dict) else getattr(self, "_cycle_thread", None)
        if runner is None:
            if not self._cycle_runner_alive({"thread": thread}):
                return
            runner = {
                "thread": thread,
                "stop_event": getattr(self, "_cycle_stop_event", threading.Event()),
                "stop_requested_at": None,
            }

        stop_event = runner.get("stop_event") or getattr(self, "_cycle_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        runner["stop_requested_at"] = time.monotonic()
        self._cycle_stop_requested_at = runner["stop_requested_at"]

        disabled_ok = self._cycle_disable_current_campaign(campaign_name)
        suffix = f": {campaign_name}" if campaign_name else ""
        self.log.append(f"[Циклическая] [~] Остановка циклической кампании{suffix}...")
        if disabled_ok:
            self.log.append(f"[Циклическая] [=] Кампания выключена в базе{suffix}")

        try:
            if campaign_name == (self._cycle_campaign_name or "").strip():
                self._cycle_set_status("остановка выбранной кампании...", "#F39C12")
                self.btn_cycle_stop.configure(state="disabled", text="■ Останавливаю...")
                self.btn_cycle_start.configure(state="disabled")
            self.btn_stop_current.configure(state="normal", text="■ Остановка циклов...")
        except Exception:
            pass
        self.after(500, lambda name=campaign_name: self._cycle_watch_stop(name))

    def _cycle_disable_current_campaign(self, campaign_name: str | None = None) -> bool:
        try:
            db = Database(self.app.config.db_path)
            try:
                name = (campaign_name or getattr(self, "_cycle_running_campaign_name", "") or self._cycle_campaign_name)
                campaign_id = db.get_or_create_cycle_campaign(name)
                db.set_cycle_campaign_enabled(campaign_id, False)
            finally:
                db.close()
            return True
        except Exception as e:
            self.log.append(f"[Циклическая] [!] Не удалось выключить кампанию в базе: {e}")
            return False

    def _disable_all_cycle_campaigns(self):
        """One big red button handler (user request): stop all running cyclic campaigns
        and disable them in DB so they don't auto-resume on restart.
        Then the runners will naturally stop and UI will reflect 0 active.
        """
        try:
            runners = getattr(self, "_cycle_runners", None) or {}
            stopped = []
            for name in list(runners.keys()):
                try:
                    self._stop_cycle(name)
                    stopped.append(name)
                except Exception:
                    pass

            # Disable every campaign in DB
            db = Database(self.app.config.db_path)
            try:
                camps = db.list_cycle_campaigns() or []
                disabled_count = 0
                for c in camps:
                    try:
                        cid = c.get("id") if isinstance(c, dict) else getattr(c, "id", None)
                        if cid is None:
                            continue
                        db.set_cycle_campaign_enabled(int(cid), False)
                        disabled_count += 1
                    except Exception as exc:
                        self.log.append(f"[Циклическая] [!] Не удалось выключить кампанию {c}: {exc}")
            finally:
                db.close()

            self.log.append(f"[Циклическая] [!] Выключены все кампании одной кнопкой: {disabled_count}; остановлено: {', '.join(stopped) or '—'}")
            try:
                self._refresh_cycle_campaigns()
                self._cycle_refresh_cycle_buttons()
                self._cycle_set_status("Все кампании выключены", "#E74C3C")
            except Exception:
                pass
        except Exception as e:
            self.log.append(f"[Циклическая] [!] Ошибка кнопки 'Выключить ВСЕ': {e}")

    def _cycle_finish_stopped_ui(self, campaign_name: str | None = None):
        runners = getattr(self, "_cycle_runners", None) or {}
        if campaign_name:
            self._cycle_clear_runtime(campaign_name)
            # Освободить аккаунты этой кампании
            try:
                dbm = Database(self.app.config.db_path)
                camp_phones = dbm.get_cycle_campaign_account_phones(
                    dbm.get_or_create_cycle_campaign(campaign_name)
                )
                dbm.close()
                if camp_phones:
                    self.mark_account_free(camp_phones)
                else:
                    busy = self.get_busy_accounts()
                    self.mark_account_free([
                        p for p, ctx in busy.items()
                        if "Циклическая" in (ctx or "") and campaign_name in (ctx or "")
                    ])
            except Exception:
                pass
            runners.pop(campaign_name, None)
        else:
            for name, runner in list(runners.items()):
                if not self._cycle_runner_alive(runner):
                    self._cycle_clear_runtime(name)
                    try:
                        dbm = Database(self.app.config.db_path)
                        camp_phones = dbm.get_cycle_campaign_account_phones(
                            dbm.get_or_create_cycle_campaign(name)
                        )
                        dbm.close()
                        if camp_phones:
                            self.mark_account_free(camp_phones)
                        else:
                            busy = self.get_busy_accounts()
                            self.mark_account_free([
                                p for p, ctx in busy.items()
                                if "Циклическая" in (ctx or "") and name in (ctx or "")
                            ])
                    except Exception:
                        pass
                    runners.pop(name, None)

        active_names = self._cycle_active_names()
        self._cycle_running = bool(active_names)
        if not self._cycle_running:
            self._cycle_running_campaign_name = ""
        elif self._cycle_running_campaign_name not in active_names:
            self._cycle_running_campaign_name = active_names[0]

        try:
            self._cycle_refresh_cycle_buttons()
            if not self._cycle_running and not getattr(self, "_running", False):
                self.btn_stop_current.configure(state="disabled", text="■ Остановить текущий процесс")
        except Exception:
            pass
        try:
            if not self._cycle_runner_alive(self._cycle_get_runner(self._cycle_campaign_name)):
                self._cycle_set_status("остановлен", "gray70" if not active_names else "#F39C12")
            self._cycle_refresh_table()
            self._cycle_update_status()
        except Exception:
            pass

    def _cycle_watch_stop(self, campaign_name: str | None = None):
        runners = getattr(self, "_cycle_runners", None) or {}
        runner = runners.get(campaign_name or "")
        if runner is None:
            thread = getattr(self, "_cycle_thread", None)
            if thread is None or not thread.is_alive():
                self._cycle_finish_stopped_ui(campaign_name)
            return

        thread = runner.get("thread")
        if thread is None or not thread.is_alive():
            self._cycle_finish_stopped_ui(campaign_name)
            return

        waited = 0.0
        started = runner.get("stop_requested_at") or self._cycle_stop_requested_at
        if started is not None:
            waited = max(0.0, time.monotonic() - started)
        try:
            if (campaign_name or "") == (self._cycle_campaign_name or "").strip():
                self._cycle_set_status(f"остановка... жду сеть {int(waited)}с", "#F39C12")
        except Exception:
            pass
        self.after(1000, lambda name=campaign_name: self._cycle_watch_stop(name))

    def _refresh_accounts(self):
        """Обновить списки аккаунтов в обоих меню рассылки"""
        db = Database(self.app.config.db_path)
        accounts = db.get_all_accounts()
        db.close()
        phones = [a.phone for a in accounts if a.is_active]
        values = ["Все активные"] + phones
        self.m_account_menu.configure(values=values)
        self.b_account_menu.configure(values=values)
        if hasattr(self, "q_account_menu"):
            self.q_account_menu.configure(values=values)
        if hasattr(self, "c_account_menu"):
            self.c_account_menu.configure(values=values)

    def _toggle_m_text(self):
        """Скрыть/показать поле сообщения в зависимости от источника"""
        if self.m_source_var.get() == "Избранное":
            self.m_message.configure(state="disabled")
        else:
            self.m_message.configure(state="normal")

    def _apply_unique(self, text: str, mode: str) -> str:
        """Применить выбранный режим уникализации к тексту"""
        from spintax import spin_text, apply_mask, ai_rewrite
        if mode == "Спинтакс":
            return spin_text(text)
        elif mode == "Омоглифы":
            return apply_mask(text)
        elif mode == "AI":
            # L3: warning один раз за запуск рассылки, если прокси не задан
            if not self.app.config.openai_proxy and not getattr(self, "_ai_proxy_warned", False):
                print("[!!] ВНИМАНИЕ: AI-рерайт идёт БЕЗ прокси — палится ваш реальный IP")
                print("[!!] Задайте OPENAI_PROXY в Настройках")
                self._ai_proxy_warned = True
            return ai_rewrite(text, self.app.config.openai_api_key,
                              self.app.config.openai_model,
                              proxy=self.app.config.openai_proxy)
        return text  # Оригинал

    def on_show(self):
        self._refresh_accounts()
        self._refresh_cycle_templates()

        # === Диагностика циклических кампаний на старте (чтобы было видно, что происходит) ===
        try:
            now = time.monotonic()
            last_diag_at = getattr(self, "_cycle_start_diag_at", 0.0)
            should_log_diag = now - last_diag_at > 60
            if should_log_diag:
                self._cycle_start_diag_at = now
                db = Database(self.app.config.db_path)
                try:
                    camps = db.list_cycle_campaigns()
                    configured_but_not_running = []
                    for c in camps:
                        cname = c.get("name", "?")
                        en = bool(c.get("enabled"))
                        cid = db.get_or_create_cycle_campaign(cname)
                        tgts = db.get_cycle_targets(cid)
                        accs = db.get_cycle_campaign_account_phones(cid)
                        runner_ok = self._cycle_runner_alive(self._cycle_get_runner(cname))
                        if en or tgts or accs or runner_ok:
                            status_line = f"[Циклическая] [i] '{cname}': enabled={en}, целей={len(tgts)}, аккаунтов={len(accs)}, runner={'живой' if runner_ok else 'остановлен'}"
                            self._append_log(status_line)
                        if len(tgts) > 0 and len(accs) > 0 and not runner_ok:
                            configured_but_not_running.append(cname)
                finally:
                    db.close()

                if configured_but_not_running:
                    self._append_log(f"[Циклическая] [!] Кампании настроены, но runner остановлен: {configured_but_not_running}")
        except Exception as e:
            self._append_log(f"[Циклическая] [!] Ошибка диагностики кампаний: {e}")

        if hasattr(self, "_cycle_campaign_name"):
            try:
                self._refresh_cycle_campaigns()
                self._cycle_select_campaign(self.c_campaign_var.get() if hasattr(self, "c_campaign_var") else self._cycle_campaign_name)
            except Exception:
                pass
            try:
                self._cycle_load_campaign_settings()
            except Exception:
                pass
            try:
                self._cycle_update_status()
            except Exception:
                pass

        # Startup health check only. Campaigns must be started explicitly from the UI;
        # otherwise stale DB flags can make the app look "running" with no live worker.
        try:
            self.after(3000, self._cycle_watchdog)

            if not getattr(self, "_cycle_health_check_scheduled", False):
                self.after(5 * 60 * 60 * 1000, self._cycle_periodic_health_check)
                self._cycle_health_check_scheduled = True
        except Exception:
            pass

    def _force_resume_and_refresh_cyclic_ui(self):
        """Принудительный запуск ВСЕХ настроенных рассылок + обновление UI.
        Пользователь просил 'запусти все рассылки сам и наблюдай'.
        """
        self.log.append("[Циклическая] [!!!] ПРИНУДИТЕЛЬНЫЙ ЗАПУСК И МОНИТОРИНГ ВСЕХ РАССЫЛОК")
        try:
            self._resume_enabled_cycles(only_dead=False)
        except Exception as e:
            self.log.append(f"[Циклическая] [!] Ошибка принудительного запуска: {e}")
        try:
            self._refresh_cycle_campaigns()
            active = self._cycle_active_names()
            if active:
                # Покажем первую активную в UI
                first = active[0]
                self._cycle_select_campaign(first)
                self.log.append(f"[Циклическая] [i] Показываю в UI активную кампанию: {first}")
                self._cycle_update_status()
                self._cycle_refresh_table()
                self._cycle_refresh_cycle_buttons()
            # Также обновим таблицу аккаунтов, чтобы видеть busy (синий)
            if hasattr(self, 'table') and hasattr(self, 'refresh'):
                try: self.refresh()
                except: pass
        except Exception as e:
            self.log.append(f"[Циклическая] [!] Ошибка обновления UI: {e}")

    def _start_mention(self):
        _log_action("broadcast", "_start_mention")
        if self._running:
            return

        # Сбросить warning-флаг прокси, чтобы при новом запуске он появился заново
        self._ai_proxy_warned = False

        target = self.m_target.get().strip()
        source = self.m_source.get().strip()
        source_mode = self.m_source_var.get()
        use_saved = source_mode == "Избранное"
        use_templates = source_mode == "Шаблоны"
        message = self.m_message.get("1.0", "end").strip()
        unique_mode = self.m_unique_var.get()

        if not target or not source:
            self.log.append("[!] Заполните целевую группу и источник")
            return
        if not use_saved and not message:
            self.log.append("[!] Введите текст сообщения или выберите 'Из Избранного'")
            return

        limit_str = self.m_limit.get().strip()
        limit = int(limit_str) if limit_str.isdigit() else 0
        per_msg_str = self.m_per_msg.get().strip()
        per_msg = int(per_msg_str) if per_msg_str.isdigit() else 5
        dry_run = self.m_dry_run.get()
        selected_account = self._resolve_phone(self.m_account_var.get())
        _use_saved_m = use_saved
        _use_templates_m = use_templates
        _unique_mode_m = unique_mode
        _base_message_m = message
        _templates_m = _split_message_template_variants(message)

        stop_event = threading.Event()
        run_id = self._begin_regular_run("mention", stop_event)
        self._active_op_name = "упоминания"
        self._running = True
        self.btn_mention.configure(state="disabled", text="Выполняется...")
        self.btn_stop_current.configure(state="normal", text="■ Остановить упоминания")
        self.log.clear()
        try:
            phones = self._runtime_busy_phones(selected_account)
            if phones:
                self.mark_account_busy(phones, "Упоминания")
        except Exception:
            pass

        def mention_thread():
            log_queue = self.app.log_queue
            _thread_local.log_handler = lambda msg: log_queue.put(("broadcast_log", msg))
            _thread_local.log_tag = "broadcast"

            try:
                loop = asyncio.new_event_loop()

                async def do_mention():
                    from mentioner import Mentioner
                    from sender import TelegramSender
                    from ads_database import AdsDB
                    from ads_scheduler import random_mention_delay_sec
                    from parser import ensure_chat_access

                    cfg = self.app.config
                    db = Database(cfg.db_path)

                    # Загружаем настройки задержек (рандом min..max)
                    _adsdb = AdsDB(cfg.db_path)
                    try:
                        _settings = _adsdb.load_scheduler_settings()
                    finally:
                        _adsdb.close()

                    try:
                        accounts = db.get_active_accounts()
                        if selected_account != "Все активные":
                            accounts = [a for a in accounts if a.phone == selected_account]
                        if not accounts:
                            print("[!] Нет активных аккаунтов")
                            return

                        already_mentioned = db.get_already_mentioned_user_ids_from_log(target)
                        users = db.get_users_for_mention(source, exclude_ids=already_mentioned, limit=limit)

                        if not users:
                            print("[!] Нет пользователей для упоминания")
                            return

                        mentioner = Mentioner(mentions_per_message=per_msg)
                        batches = [users[i:i + per_msg] for i in range(0, len(users), per_msg)]
                        _saved_texts: dict = {}  # phone -> list[str] из Избранного

                        print(f"Пользователей: {len(users)}, батчей: {len(batches)}, аккаунтов: {len(accounts)}")
                        mode = "DRY-RUN" if dry_run else "LIVE"
                        print(f"Режим: {mode}\n")

                        stats = {"sent": 0, "errors": 0, "skipped": 0, "dry_run": 0}
                        batch_idx = 0
                        acc_idx = 0

                        async def _mention_wait(coro, label: str, timeout: float, acc_phone: str = "", target_value: str = ""):
                            try:
                                return await _await_interruptibly(
                                    coro,
                                    stop_event,
                                    op_name="упоминания",
                                    label=label,
                                    timeout=timeout,
                                    account=acc_phone,
                                    target=target_value,
                                )
                            except asyncio.TimeoutError:
                                stats["errors"] += 1
                                print(f"  [!] {label}: таймаут {timeout:.0f}с — пропуск")
                                return None

                        while batch_idx < len(batches) and acc_idx < len(accounts):
                            acc = accounts[acc_idx]
                            _raise_if_stop_requested(
                                stop_event,
                                op_name="упоминания",
                                account=acc.phone,
                                target=target,
                                progress=f"батч={batch_idx + 1}/{len(batches)}",
                            )
                            sender = TelegramSender(acc, cfg, db)

                            connected = await _mention_wait(
                                sender.connect(),
                                f"{acc.phone}: подключение",
                                30,
                                acc.phone,
                                target,
                            )
                            if not connected:
                                acc_idx += 1
                                continue

                            try:
                                _raise_if_stop_requested(
                                    stop_event,
                                    op_name="упоминания",
                                    account=acc.phone,
                                    target=target,
                                    progress=f"батч={batch_idx + 1}/{len(batches)}",
                                )
                                access_result = await _mention_wait(
                                    ensure_chat_access(sender.client, target, dry_run=dry_run),
                                    f"{target}: проверка доступа",
                                    25,
                                    acc.phone,
                                    target,
                                )
                                if access_result is None:
                                    acc_idx += 1
                                    continue
                                decision, reason, _retry_after = access_result
                                if decision != "ok":
                                    print(f"[!] Нет доступа к {target}: {reason}")
                                    batch_idx = len(batches)
                                    break

                                while batch_idx < len(batches) and sender.can_send_more():
                                    _raise_if_stop_requested(
                                        stop_event,
                                        op_name="упоминания",
                                        account=acc.phone,
                                        target=target,
                                        progress=f"батч={batch_idx + 1}/{len(batches)}",
                                    )
                                    batch = batches[batch_idx]
                                # Получить текст: из Избранного или вручную
                                if _use_saved_m:
                                    if acc.phone not in _saved_texts or not _saved_texts[acc.phone]:
                                        saved = await _mention_wait(
                                            sender.get_saved_messages(limit=30),
                                            f"{acc.phone}: чтение Избранного",
                                            20,
                                            acc.phone,
                                            target,
                                        )
                                        _saved_texts[acc.phone] = saved or []
                                    raw = random.choice(_saved_texts[acc.phone]) if _saved_texts.get(acc.phone) else _base_message_m
                                elif _use_templates_m:
                                    raw = random.choice(_templates_m) if _templates_m else _base_message_m
                                else:
                                    raw = _base_message_m
                                raw = self._apply_unique(raw, _unique_mode_m)
                                text, entities = mentioner.build_mention_message(raw, batch)
                                if dry_run:
                                    preview = text.replace("\n", " ").strip()
                                    if len(preview) > 120:
                                        preview = preview[:120] + "…"
                                    print(f"  [DRY] Упоминание -> {target} ({acc.phone}): {preview}")
                                    raw_status = "dry_run"
                                    status = "dry_run"
                                else:
                                    raw_status = await _mention_wait(
                                        sender.send_mention_message(target, text, entities),
                                        f"{target}: отправка упоминаний",
                                        45,
                                        acc.phone,
                                        target,
                                    )
                                    if raw_status is None:
                                        batch_idx += 1
                                        continue
                                    status = raw_status.split(":", 1)[0]

                                user_ids = [u.user_id for u in batch]
                                if not dry_run:
                                    db.log_mention(acc.phone, target, user_ids, status)

                                if status in ("sent", "dry_run"):
                                    if status == "sent":
                                        stats["sent"] += 1
                                    else:
                                        stats["dry_run"] += 1
                                    batch_idx += 1
                                    # Рандомная пауза перед следующим батчем (только если не последний)
                                    if batch_idx < len(batches):
                                        delay = random_mention_delay_sec(_settings)
                                        if dry_run:
                                            print(f"  [DRY] Пауза {delay:.0f}с пропущена "
                                                  f"(диапазон {_settings.mention_delay_min_seconds}-"
                                                  f"{_settings.mention_delay_max_seconds}с)")
                                        else:
                                            print(f"  [~] Пауза {delay:.0f}с (диапазон "
                                                  f"{_settings.mention_delay_min_seconds}-"
                                                  f"{_settings.mention_delay_max_seconds}с)...")
                                            await _sleep_interruptibly(
                                                delay,
                                                stop_event,
                                                op_name="упоминания",
                                                account=acc.phone,
                                                target=target,
                                                progress=f"батч={batch_idx}/{len(batches)}",
                                            )
                                elif status == "flood_wait":
                                    print(f"  [~] Ротация с {acc.phone}")
                                    break
                                elif status == "slow_mode":
                                    try:
                                        wait_s = int(raw_status.split(":", 1)[1])
                                    except Exception:
                                        wait_s = 60
                                    print(f"  [~] SlowModeWait {wait_s}s — ожидание...")
                                    await _sleep_interruptibly(
                                        max(wait_s, 1),
                                        stop_event,
                                        op_name="упоминания",
                                        account=acc.phone,
                                        target=target,
                                        progress=f"slow_mode | батч={batch_idx + 1}/{len(batches)}",
                                    )
                                elif status in ("banned", "chat_banned", "need_subscription", "no_permission", "private"):
                                    stats["skipped"] += 1
                                    if status == "banned":
                                        # Global ban (e.g. PeerFlood) — stop using this account for the rest of the run.
                                        break
                                    elif status == "chat_banned":
                                        # Per-chat ban (UserBannedInChannelError etc.) — skip this target, keep using account for other targets.
                                        print(f"[!] {target}: аккаунт забанен только в этом чате (не глобально)")
                                    elif status in ("need_subscription", "no_permission", "private"):
                                        print(f"[!] Группа {target} недоступна")
                                        batch_idx = len(batches)
                                        break
                                else:
                                    stats["errors"] += 1
                                    batch_idx += 1
                                    # Пауза при ошибке: без неё Telegram видит шквал
                                    # запросов одной пачкой = поведенческий red flag.
                                    if batch_idx < len(batches):
                                        delay = random_mention_delay_sec(_settings)
                                        print(f"  [~] Пауза {delay:.0f}с после ошибки "
                                              f"(диапазон {_settings.mention_delay_min_seconds}-"
                                              f"{_settings.mention_delay_max_seconds}с)...")
                                        await _sleep_interruptibly(
                                            delay,
                                            stop_event,
                                            op_name="упоминания",
                                            account=acc.phone,
                                            target=target,
                                            progress=f"после ошибки | батч={batch_idx}/{len(batches)}",
                                        )
                            finally:
                                try:
                                    await asyncio.wait_for(sender.disconnect(), timeout=10)
                                except Exception as e:
                                    print(f"  [!] {acc.phone}: disconnect не завершился быстро ({type(e).__name__})")

                            acc_idx += 1

                        print("\n=== Итого ===")
                        print(f"Отправлено: {stats['sent']}")
                        print(f"Dry Run: {stats['dry_run']}")
                        print(f"Ошибки: {stats['errors']}")
                        print(f"Пропущено: {stats['skipped']}")
                        print(f"Осталось батчей: {len(batches) - batch_idx}")
                    except OperationInterrupted as e:
                        print(str(e))
                        print(
                            f"[=] Упоминания остановлены: аккаунт={accounts[min(acc_idx, max(len(accounts)-1, 0))].phone if accounts else '—'} "
                            f"| цель={target} | отправлено={stats['sent']} | dry_run={stats['dry_run']} "
                            f"| ошибок={stats['errors']} | осталось батчей={len(batches) - batch_idx}"
                        )
                    finally:
                        db.close()

                _run_loop(loop, do_mention())
            except Exception as e:
                log_queue.put(("broadcast_log", f"[-] Ошибка: {e}"))
            finally:
                _thread_local.log_handler = None
                self.app.log_queue.put(("mention_done", {"run_id": run_id}))

        mention_worker = threading.Thread(target=mention_thread, name="MentionWorker", daemon=True)
        self._mention_thread = mention_worker
        mention_worker.start()

    def _import_groups_csv(self):
        path = filedialog.askopenfilename(
            filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")])
        if not path:
            return

        dialog = ctk.CTkInputDialog(
            text="Введите текст сообщения для рассылки:",
            title="Импорт групп из CSV")
        message_text = dialog.get_input()
        if not message_text:
            return

        links = []
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    url = line.strip()
                    if not url.startswith("https://t.me/"):
                        continue
                    links.append(url)
        except Exception as e:
            self.log.append(f"[!] Ошибка чтения CSV: {e}")
            return

        tasks_added = 0
        if links:
            db = Database(self.app.config.db_path)
            for url in links:
                db.add_task(Task(
                    target_group=url,
                    message_text=message_text,
                    task_type="broadcast",
                    source_group="",
                    mentions_per_message=0,
                ))
                tasks_added += 1
            db.close()

        if tasks_added:
            self.log.append(f"[+] Импорт: {tasks_added} задач создано")
            name_dlg = ctk.CTkInputDialog(
                text="Сохранить этот список как шаблон? Введите название или оставьте пустым:",
                title="Сохранение шаблона")
            name = (name_dlg.get_input() or "").strip()
            if name:
                try:
                    db = Database(self.app.config.db_path)
                    db.add_list_template(name, "groups", "\n".join(links))
                    db.close()
                    self.log.append(f"[+] Шаблон сохранён: {name}")
                except Exception as e:
                    self.log.append(f"[!] Не удалось сохранить шаблон: {e}")
        else:
            self.log.append("[!] CSV не содержит валидных ссылок t.me")

    def _import_groups_template(self):
        db = Database(self.app.config.db_path)
        templates = [t for t in db.get_all_list_templates() if t.get("kind") in ("groups", "mixed")]
        db.close()
        if not templates:
            self.log.append("[!] Нет шаблонов списков (создай в разделе 'Шаблоны')")
            return

        pick = ListTemplatePickerDialog(self, templates, title="Импорт групп из шаблона")
        self.wait_window(pick)
        if not pick.result:
            return

        dialog = ctk.CTkInputDialog(
            text="Введите текст сообщения для рассылки:",
            title="Импорт групп из шаблона")
        message_text = dialog.get_input()
        if not message_text:
            return

        links = [l.strip() for l in (pick.result.get("content") or "").splitlines() if l.strip()]
        if not links:
            self.log.append("[!] Шаблон пустой")
            return

        tasks_added = 0
        try:
            db = Database(self.app.config.db_path)
            for url in links:
                db.add_task(Task(
                    target_group=url,
                    message_text=message_text,
                    task_type="broadcast",
                    source_group="",
                    mentions_per_message=0,
                ))
                tasks_added += 1
            db.close()
        except Exception as e:
            self.log.append(f"[!] Ошибка создания задач: {e}")
            return

        self.log.append(f"[+] Шаблон '{pick.result['name']}': {tasks_added} задач создано")

    def _check_and_clean(self):
        _log_action("broadcast", "_check_and_clean")
        if self._running:
            return

        stop_event = threading.Event()
        run_id = self._begin_regular_run("check", stop_event)
        self._active_op_name = "проверку задач"
        self._running = True
        self.btn_check.configure(state="disabled", text="Проверка...")
        self.btn_stop_current.configure(state="normal", text="■ Остановить проверку")
        self.log.clear()
        dry_run = bool(self.check_dry_run.get()) if hasattr(self, "check_dry_run") else False

        def check_thread():
            log_queue = self.app.log_queue
            _thread_local.log_handler = lambda msg: log_queue.put(("broadcast_log", msg))
            _thread_local.log_tag = "broadcast"

            try:
                loop = asyncio.new_event_loop()

                async def do_check():
                    from telethon.tl.functions.channels import JoinChannelRequest
                    from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
                    from telethon.errors import (
                        InviteHashExpiredError, InviteHashInvalidError,
                        UserAlreadyParticipantError, FloodWaitError,
                        ChannelPrivateError, UsernameNotOccupiedError,
                    )
                    from ads_database import AdsDB
                    from ads_scheduler import random_group_check_delay_sec

                    cfg = Config()
                    db = Database(cfg.db_path)

                    # Загружаем настройки задержек (рандом min..max)
                    _adsdb = AdsDB(cfg.db_path)
                    try:
                        _settings = _adsdb.load_scheduler_settings()
                    finally:
                        _adsdb.close()

                    try:
                        accounts = db.get_active_accounts()

                        if not accounts:
                            print("[!] Нет активных аккаунтов")
                            return

                        tasks = db.get_pending_tasks(task_type="broadcast")
                        if not tasks:
                            print("[!] Нет задач для проверки")
                            return

                        mode = "DRY-RUN" if dry_run else "LIVE"
                        print(f"Задач: {len(tasks)}, аккаунт: {accounts[0].phone}, режим: {mode}\n")

                        from sender import TelegramSender
                        sender = TelegramSender(accounts[0], cfg, db)

                        stats = {"deleted": 0, "joined": 0, "already": 0, "error": 0}

                        async def _check_wait(coro, label: str, timeout: float, target_value: str = ""):
                            try:
                                return await _await_interruptibly(
                                    coro,
                                    stop_event,
                                    op_name="проверка задач",
                                    label=label,
                                    timeout=timeout,
                                    account=accounts[0].phone if accounts else "",
                                    target=target_value,
                                )
                            except asyncio.TimeoutError:
                                stats["error"] += 1
                                print(f"  [!] {label}: таймаут {timeout:.0f}с — пропуск")
                                return None

                        connected = await _check_wait(
                            sender.connect(),
                            f"{accounts[0].phone}: подключение",
                            30,
                        )
                        if not connected:
                            return

                        try:
                            for task in tasks:
                                url = task.target_group
                                _raise_if_stop_requested(
                                    stop_event,
                                    op_name="проверка задач",
                                    account=accounts[0].phone,
                                    target=url,
                                )
                            # Определяем тип ссылки: t.me/joinchat/HASH или t.me/+HASH
                            m = re.search(r"t\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)", url)
                            is_invite = m is not None
                            slug = m.group(1) if m else url.split("/")[-1]

                            try:
                                if is_invite:
                                    # Валидируем инвайт-ссылку — исключения в except'ах ниже
                                    invite_info = await _check_wait(
                                        sender.client(CheckChatInviteRequest(slug)),
                                        f"{url}: проверка invite",
                                        25,
                                        url,
                                    )
                                    if invite_info is None:
                                        return
                                    if dry_run:
                                        if type(invite_info).__name__ == "ChatInviteAlready":
                                            print(f"  [DRY] Уже в группе: {url}")
                                            stats["already"] += 1
                                        else:
                                            print(f"  [DRY] Вступил бы: {url}")
                                            stats["joined"] += 1
                                    else:
                                        # Ссылка жива — пробуем вступить
                                        try:
                                            joined = await _check_wait(
                                                sender.client(ImportChatInviteRequest(slug)),
                                                f"{url}: вступление invite",
                                                35,
                                                url,
                                            )
                                            if joined is None:
                                                return
                                            print(f"  [+] Вступил: {url}")
                                            stats["joined"] += 1
                                        except UserAlreadyParticipantError:
                                            print(f"  [~] Уже в группе: {url}")
                                            stats["already"] += 1
                                else:
                                    if dry_run:
                                        entity = await _check_wait(
                                            sender.client.get_entity(url),
                                            f"{url}: проверка ссылки",
                                            25,
                                            url,
                                        )
                                        if entity is None:
                                            return
                                        print(f"  [DRY] Ссылка валидна, в live была бы попытка вступления: {url}")
                                        stats["joined"] += 1
                                    else:
                                        # Публичная группа — JoinChannelRequest резолвит url inline,
                                        # без отдельного get_entity (меньше resolve-запросов в логах)
                                        try:
                                            joined = await _check_wait(
                                                sender.client(JoinChannelRequest(url)),
                                                f"{url}: вступление",
                                                35,
                                                url,
                                            )
                                            if joined is None:
                                                return
                                            print(f"  [+] Вступил: {url}")
                                            stats["joined"] += 1
                                        except UserAlreadyParticipantError:
                                            print(f"  [~] Уже в группе: {url}")
                                            stats["already"] += 1

                                delay = random_group_check_delay_sec(_settings)
                                if dry_run:
                                    print(f"  [DRY] Пауза {delay:.0f}с пропущена "
                                          f"(диапазон {_settings.group_check_join_delay_min_seconds}-"
                                          f"{_settings.group_check_join_delay_max_seconds}с)")
                                else:
                                    print(f"  [~] Пауза {delay:.0f}с (диапазон "
                                          f"{_settings.group_check_join_delay_min_seconds}-"
                                          f"{_settings.group_check_join_delay_max_seconds}с)...")
                                    await _sleep_interruptibly(
                                        delay,
                                        stop_event,
                                        op_name="проверка задач",
                                        account=accounts[0].phone,
                                        target=url,
                                    )

                            except (InviteHashExpiredError, InviteHashInvalidError):
                                if dry_run:
                                    print(f"  [DRY] Удалил бы задачу (протухла): {url}")
                                else:
                                    print(f"  [x] Удалена (протухла): {url}")
                                    db.delete_task(task.id)
                                stats["deleted"] += 1

                            except UsernameNotOccupiedError:
                                if dry_run:
                                    print(f"  [DRY] Удалил бы задачу (юзернейм не найден): {url}")
                                else:
                                    print(f"  [x] Удалена (юзернейм не найден): {url}")
                                    db.delete_task(task.id)
                                stats["deleted"] += 1

                            except ChannelPrivateError:
                                if dry_run:
                                    print(f"  [DRY] Удалил бы задачу (приватная/недоступна): {url}")
                                else:
                                    print(f"  [x] Удалена (приватная/недоступна): {url}")
                                    db.delete_task(task.id)
                                stats["deleted"] += 1

                            except FloodWaitError as e:
                                print(f"  [!] FloodWait {e.seconds}s — пауза...")
                                await _sleep_interruptibly(
                                    e.seconds,
                                    stop_event,
                                    op_name="проверка задач",
                                    account=accounts[0].phone,
                                    target=url,
                                    progress="FloodWait",
                                )

                            except Exception as e:
                                print(f"  [!] Ошибка {url}: {e}")
                                stats["error"] += 1

                        finally:
                            try:
                                await asyncio.wait_for(sender.disconnect(), timeout=10)
                            except Exception as e:
                                print(f"  [!] {accounts[0].phone}: disconnect не завершился быстро ({type(e).__name__})")

                        print("\n=== Итого ===")
                        print(f"Удалено (мёртвые): {stats['deleted']}")
                        print(f"Вступил: {stats['joined']}")
                        print(f"Уже в группе: {stats['already']}")
                        print(f"Ошибки: {stats['error']}")
                    except OperationInterrupted as e:
                        print(str(e))
                        print(
                            f"[=] Проверка задач остановлена: аккаунт={accounts[0].phone if accounts else '—'} "
                            f"| удалено={stats['deleted']} | вступил={stats['joined']} "
                            f"| уже={stats['already']} | ошибок={stats['error']}"
                        )
                    finally:
                        db.close()

                _run_loop(loop, do_check())
            except Exception as e:
                log_queue.put(("broadcast_log", f"[-] Ошибка: {e}"))
            finally:
                _thread_local.log_handler = None
                self.app.log_queue.put(("check_done", {"run_id": run_id}))

        check_worker = threading.Thread(target=check_thread, name="BroadcastCheckWorker", daemon=True)
        self._check_thread = check_worker
        check_worker.start()

    def _schedule_broadcast_status_refresh(self):
        try:
            if not self.winfo_exists():
                return
            self._broadcast_status_after_id = self.after(5000, self._broadcast_status_tick)
        except Exception:
            self._broadcast_status_after_id = None

    def _broadcast_status_tick(self):
        self._broadcast_status_after_id = None
        self._refresh_broadcast_status_panel()
        self._schedule_broadcast_status_refresh()

    def _set_broadcast_refresh_feedback(self, state: str, detail: str = ""):
        btn = getattr(self, "btn_broadcast_dashboard_refresh", None)
        label = getattr(self, "lbl_broadcast_refreshed", None)
        try:
            after_id = getattr(self, "_broadcast_dashboard_refresh_reset_after_id", None)
            if after_id is not None:
                self.after_cancel(after_id)
                self._broadcast_dashboard_refresh_reset_after_id = None
        except Exception:
            self._broadcast_dashboard_refresh_reset_after_id = None

        try:
            if state == "running":
                if btn is not None:
                    btn.configure(state="disabled", text="Обновляю...")
                if label is not None:
                    label.configure(text="Обновление...", text_color="#F59E0B")
                try:
                    self.update_idletasks()
                except Exception:
                    pass
                return

            if state == "error":
                if btn is not None:
                    btn.configure(state="normal", text="Обновить")
                if label is not None:
                    label.configure(text=f"Ошибка обновления: {self._shorten_ui(detail, 70)}", text_color="#EF4444")
                return

            stamp = datetime.now().strftime("%H:%M:%S")
            self._broadcast_dashboard_refresh_count = int(
                getattr(self, "_broadcast_dashboard_refresh_count", 0) or 0
            ) + 1
            if label is not None:
                suffix = f" | {detail}" if detail else ""
                label.configure(
                    text=f"Обновлено: {stamp} #{self._broadcast_dashboard_refresh_count}{suffix}",
                    text_color=("#166534", "#86EFAC"),
                )
            if btn is not None:
                btn.configure(state="normal", text="Обновлено")
                self._broadcast_dashboard_refresh_reset_after_id = self.after(
                    2500,
                    lambda: self.btn_broadcast_dashboard_refresh.configure(text="Обновить"),
                )
            try:
                self.update_idletasks()
            except Exception:
                pass
        except Exception:
            pass

    def _refresh_broadcast_dashboard(self):
        self._set_broadcast_refresh_feedback("running")
        try:
            self._refresh_broadcast_status_panel()
            refreshed_tasks = 0
            if hasattr(self, "_tasks_embed"):
                self._tasks_embed.refresh()
                refreshed_tasks = len(getattr(self._tasks_embed, "_tasks", []) or [])
            self._set_broadcast_refresh_feedback("done", f"задач: {refreshed_tasks}")
            self._append_log(f"[refresh] Дашборд задач рассылки обновлён: {refreshed_tasks}")
        except Exception as e:
            self._set_broadcast_refresh_feedback("error", str(e))
            self._append_log(f"[refresh] tasks dashboard refresh failed: {e}")

    def _broadcast_cycle_status_snapshot(self) -> dict | None:
        try:
            active_names = self._cycle_active_names()
        except Exception:
            active_names = []
        if not active_names:
            return None

        old_name = getattr(self, "_cycle_campaign_name", "")
        name = old_name if old_name in active_names else active_names[0]
        restore_name = False
        try:
            if name != old_name:
                self._cycle_campaign_name = name
                restore_name = True
            snap = self._cycle_build_snapshot()
        except Exception:
            return {
                "name": name,
                "active_count": len(active_names),
                "snapshot": None,
            }
        finally:
            if restore_name:
                try:
                    self._cycle_campaign_name = old_name
                except Exception:
                    pass

        return {
            "name": name,
            "active_count": len(active_names),
            "snapshot": snap,
        }

    def _refresh_broadcast_status_panel(self):
        if not hasattr(self, "lbl_broadcast_state"):
            return

        all_tasks = []
        ready_tasks = []
        try:
            db = Database(self.app.config.db_path)
            try:
                all_tasks = [t for t in db.get_all_tasks() if getattr(t, "task_type", "") == "broadcast"]
                ready_tasks = db.get_pending_tasks(task_type="broadcast")
            finally:
                db.close()
        except Exception as e:
            self.lbl_broadcast_counts.configure(text=f"Ошибка чтения очереди: {e}")
            return

        waiting_tasks = [
            t for t in all_tasks
            if not getattr(t, "completed", False) and getattr(t, "status", "pending") == "waiting"
        ]
        error_tasks = [
            t for t in all_tasks
            if getattr(t, "status", "pending") == "error" or bool(getattr(t, "last_error", "") or "")
        ]
        done_tasks = [
            t for t in all_tasks
            if getattr(t, "completed", False) or getattr(t, "status", "") == "done"
        ]

        cycle_info = self._broadcast_cycle_status_snapshot()
        if cycle_info:
            snap = cycle_info.get("snapshot") or {}
            active_count = int(cycle_info.get("active_count") or 0)
            campaign_name = cycle_info.get("name") or "—"
            total = int(snap.get("total", 0) or 0)
            pos = int(snap.get("pos", 0) or 0)
            position = f"{(pos + 1) if total else '—'}/{total or '—'}"
            current_link = self._shorten_ui(snap.get("current_link", "—"), 90)
            next_link = self._shorten_ui(snap.get("next_link", "—"), 90)
            last_account = snap.get("last_account", "—")
            last_error = snap.get("last_error", "—")
            last_sent = snap.get("last_sent_at", "—")

            self.lbl_broadcast_state.configure(text="Цикл выполняется", text_color="#22C55E")
            self.lbl_broadcast_counts.configure(
                text=(
                    f"циклы: {active_count} | целей: {total} | активных: {snap.get('active_targets', 0)} | "
                    f"ожидание: {snap.get('waiting_targets', 0)} | ошибки целей: {snap.get('error_targets', 0)} | "
                    f"sent: {snap.get('sent_total', 0)} | err: {snap.get('error_total', 0)}"
                )
            )
            self.lbl_broadcast_current.configure(
                text=f"{campaign_name}: {current_link} | аккаунт: {last_account}"
            )
            self.lbl_broadcast_next.configure(text=f"{next_link} | позиция: {position}")
            self.lbl_broadcast_success.configure(
                text=f"Последний успех: {last_sent}" if last_sent != "—" else "Пока нет"
            )
            self.lbl_broadcast_errors.configure(
                text=f"Ошибка цикла: {self._shorten_ui(last_error, 120)}" if last_error != "—" else "Пока нет"
            )
            return

        self._set_broadcast_state_label()
        self.lbl_broadcast_counts.configure(
            text=(
                f"Всего: {len(all_tasks)} | готово: {len(ready_tasks)} | "
                f"ожидание: {len(waiting_tasks)} | ошибки: {len(error_tasks)} | выполнено: {len(done_tasks)}"
            )
        )

        current = (self._broadcast_ui_state or {}).get("current") or "—"
        self.lbl_broadcast_current.configure(text=current)

        next_task = ready_tasks[0] if ready_tasks else None
        self.lbl_broadcast_next.configure(text=self._task_brief(next_task) if next_task else "Нет готовых задач")

        recent_success = (self._broadcast_ui_state or {}).get("last_success") or []
        if not recent_success:
            recent_success = [self._task_brief(t) for t in sorted(done_tasks, key=lambda t: getattr(t, "id", 0) or 0, reverse=True)[:3]]
        self.lbl_broadcast_success.configure(text=" | ".join(recent_success[:3]) if recent_success else "Пока нет")

        recent_errors = (self._broadcast_ui_state or {}).get("last_errors") or []
        if not recent_errors:
            recent_errors = [
                self._task_error_brief(t)
                for t in sorted(error_tasks, key=lambda t: getattr(t, "id", 0) or 0, reverse=True)[:3]
            ]
        self.lbl_broadcast_errors.configure(text=" | ".join(recent_errors[:3]) if recent_errors else "Пока нет")

    def _set_broadcast_state_label(self):
        state = (self._broadcast_ui_state or {}).get("state", "idle")
        if state == "stopping":
            text = "Останавливается"
            color = "#F59E0B"
        elif state == "running":
            text = "Выполняется"
            color = "#22C55E"
        else:
            text = "Не запущено"
            color = "gray60"
        try:
            self.lbl_broadcast_state.configure(text=text, text_color=color)
        except Exception:
            pass

    def _handle_broadcast_progress(self, msg):
        payload = msg if isinstance(msg, dict) else {}
        event = payload.get("event")
        state = self._broadcast_ui_state

        if event == "start":
            state["state"] = "running"
            state["current"] = "Подготовка очереди"
            state["last_success"] = []
            state["last_errors"] = []
        elif event == "current":
            state["state"] = "running"
            state["current"] = self._progress_brief(payload)
        elif event == "success":
            text = self._progress_brief(payload)
            state["last_success"] = [text] + [x for x in state.get("last_success", []) if x != text]
        elif event == "error":
            text = self._progress_brief(payload)
            err = payload.get("error") or payload.get("status") or ""
            if err:
                text = f"{text}: {self._shorten_ui(human_reason(err), 70)}"
            state["last_errors"] = [text] + [x for x in state.get("last_errors", []) if x != text]
        elif event == "stopping":
            state["state"] = "stopping"
        elif event == "done":
            state["state"] = "idle"
            state["current"] = "—"

        state["last_success"] = state.get("last_success", [])[:3]
        state["last_errors"] = state.get("last_errors", [])[:3]
        self._refresh_broadcast_status_panel()
        try:
            if hasattr(self, "_tasks_embed"):
                self._tasks_embed.refresh()
        except Exception:
            pass

    def _progress_brief(self, payload: dict) -> str:
        task_id = payload.get("task_id") or "?"
        target = payload.get("target") or "—"
        account = payload.get("account") or "—"
        index = payload.get("index")
        total = payload.get("total")
        order = f"{index}/{total}" if index and total else "—"
        return self._shorten_ui(f"#{task_id} {target} | {account} | {order}", 120)

    def _task_brief(self, task) -> str:
        if task is None:
            return "—"
        return self._shorten_ui(f"#{getattr(task, 'id', '?')} {getattr(task, 'target_group', '—')}", 120)

    def _task_error_brief(self, task) -> str:
        base = self._task_brief(task)
        detail = getattr(task, "last_error", "") or getattr(task, "status", "") or ""
        if detail:
            return self._shorten_ui(f"{base}: {human_reason(detail)}", 140)
        return base

    def _shorten_ui(self, text: str, limit: int = 100) -> str:
        value = " ".join((text or "").split())
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 1)] + "…"

    def destroy(self):
        try:
            after_id = getattr(self, "_broadcast_status_after_id", None)
            if after_id:
                self.after_cancel(after_id)
        except Exception:
            pass
        super().destroy()

    def _broadcast_preflight(self, selected_account: str, source_mode: str) -> tuple[bool, list[str]]:
        """Понятная проверка перед запуском очереди broadcast-задач."""
        db = Database(self.app.config.db_path)
        try:
            accounts = db.get_active_accounts()
            if selected_account != "Все активные":
                accounts = [a for a in accounts if a.phone == selected_account]

            all_tasks = [t for t in db.get_all_tasks() if t.task_type == "broadcast"]
            ready_tasks = db.get_pending_tasks(task_type="broadcast")
        finally:
            db.close()

        ready_empty_text = [
            t for t in ready_tasks
            if not (t.message_text or "").strip()
        ]
        waiting_tasks = [
            t for t in all_tasks
            if not t.completed and getattr(t, "status", "pending") == "waiting"
        ]
        error_tasks = [
            t for t in all_tasks
            if getattr(t, "status", "pending") == "error"
        ]
        done_tasks = [t for t in all_tasks if t.completed or getattr(t, "status", "") == "done"]

        lines = [
            "[i] Проверка перед запуском:",
            f"    аккаунтов доступно: {len(accounts)}",
            f"    broadcast-задач всего: {len(all_tasks)}",
            f"    готовых к запуску: {len(ready_tasks)}",
            f"    в ожидании: {len(waiting_tasks)}",
            f"    с ошибкой: {len(error_tasks)}",
            f"    выполнено: {len(done_tasks)}",
            f"    источник текста: {source_mode}",
        ]

        if source_mode == "Шаблоны":
            lines.append(
                "    [!] В этом режиме берутся строки из текста задачи, а не глобальные шаблоны."
            )

        ok = True
        if not accounts:
            lines.append("[!] Нет активных аккаунтов для выбранного режима.")
            ok = False
        if not ready_tasks:
            lines.append("[!] Нет задач со статусом 'Ожидает', которые можно запустить сейчас.")
            lines.append("    Создайте задачу во вкладке 'Очередь' или импортируйте цели.")
            ok = False
        if source_mode != "Избранное" and ready_empty_text:
            lines.append(f"[!] Есть готовые задачи без текста: {len(ready_empty_text)}.")
            lines.append("    Заполните текст задачи или выберите источник 'Из Избранного'.")
            ok = False

        return ok, lines

    def _start_broadcast(self):
        _log_action("broadcast", "_start_broadcast")
        if self._running:
            return

        # Сбросить warning-флаг прокси, чтобы при новом запуске он появился заново
        self._ai_proxy_warned = False

        dry_run = self.b_dry_run.get()
        selected_account = self._resolve_phone(self.b_account_var.get())
        _b_source = self.b_source_var.get()
        _b_unique = self.b_unique_var.get()

        self.log.clear()
        ok_to_start, preflight_lines = self._broadcast_preflight(selected_account, _b_source)
        for line in preflight_lines:
            self.log.append(f"[Запуск задач] {line}")
        if not ok_to_start:
            self._refresh_broadcast_status_panel()
            return

        try:
            phones = self._runtime_busy_phones(selected_account)
            if phones:
                self.mark_account_busy(phones, "Рассылка")
        except Exception:
            pass

        stop_event = threading.Event()
        run_id = self._begin_regular_run("broadcast", stop_event)
        self._active_op_name = "рассылку"
        self._running = True
        self.btn_broadcast.configure(state="disabled", text="Выполняется...")
        self.btn_stop_current.configure(state="normal", text="■ Остановить рассылку")
        self._handle_broadcast_progress({"event": "start"})

        def broadcast_thread():
            log_queue = self.app.log_queue
            def put_progress(event, **payload):
                payload["event"] = event
                log_queue.put(("broadcast_progress", payload))
            _thread_local.log_handler = lambda msg: log_queue.put(("broadcast_log", msg))
            _thread_local.log_tag = "broadcast"

            try:
                loop = asyncio.new_event_loop()

                async def do_broadcast():
                    from sender import TelegramSender
                    from ads_database import AdsDB
                    from ads_scheduler import random_broadcast_delay_sec
                    from parser import ensure_chat_access

                    cfg = self.app.config
                    db = Database(cfg.db_path)

                    # Загружаем настройки задержек (рандом min..max)
                    _adsdb = AdsDB(cfg.db_path)
                    try:
                        _settings = _adsdb.load_scheduler_settings()
                    finally:
                        _adsdb.close()

                    try:
                        accounts = db.get_active_accounts()
                        if selected_account != "Все активные":
                            accounts = [a for a in accounts if a.phone == selected_account]
                        tasks = db.get_pending_tasks(task_type="broadcast")

                        if not accounts or not tasks:
                            print(
                                f"[!] Запуск отменен: активных аккаунтов={len(accounts)}, "
                                f"готовых broadcast-задач={len(tasks)}"
                            )
                            return

                        mode = "DRY-RUN" if dry_run else "LIVE"
                        print(f"Аккаунтов: {len(accounts)}, задач: {len(tasks)}, режим: {mode}\n")

                        _saved_texts_b: dict = {}  # phone -> list[str] из Избранного
                        stats = {"sent": 0, "dry_run": 0, "errors": 0}
                        current_account = "—"
                        current_target = "—"
                        current_task_index = 0

                        async def _broadcast_wait(coro, label: str, timeout: float, acc_phone: str = "", target_value: str = ""):
                            try:
                                return await _await_interruptibly(
                                    coro,
                                    stop_event,
                                    op_name="рассылка",
                                    label=label,
                                    timeout=timeout,
                                    account=acc_phone,
                                    target=target_value,
                                )
                            except asyncio.TimeoutError:
                                stats["errors"] += 1
                                print(f"  [!] {label}: таймаут {timeout:.0f}с — пропуск")
                                return None

                        for acc in accounts:
                            current_account = acc.phone
                            _raise_if_stop_requested(
                                stop_event,
                                op_name="рассылка",
                                account=acc.phone,
                                progress="до подключения",
                            )
                            sender = TelegramSender(acc, cfg, db)

                            connected = await _broadcast_wait(
                                sender.connect(),
                                f"{acc.phone}: подключение",
                                45,
                                acc.phone,
                            )
                            if not connected:
                                continue

                            try:
                                for task_i, task in enumerate(tasks):
                                    current_task_index = task_i + 1
                                    current_target = task.target_group
                                    _raise_if_stop_requested(
                                        stop_event,
                                        op_name="рассылка",
                                        account=acc.phone,
                                        target=task.target_group,
                                        progress=f"задача={task_i + 1}/{len(tasks)}",
                                    )
                                    if not sender.can_send_more():
                                        break
                                    if getattr(task, "completed", False):
                                        continue
                                    if getattr(task, "status", "pending") in ("waiting", "error", "done"):
                                        continue
                                    put_progress(
                                        "current",
                                        task_id=task.id,
                                        target=task.target_group,
                                        account=acc.phone,
                                        index=task_i + 1,
                                        total=len(tasks),
                                    )

                                    access_result = await _broadcast_wait(
                                        ensure_chat_access(sender.client, task.target_group, dry_run=dry_run),
                                        f"{task.target_group}: проверка доступа",
                                        35,
                                        acc.phone,
                                        task.target_group,
                                    )
                                    if access_result is None:
                                        continue
                                    decision, reason, retry_after = access_result
                                    if decision != "ok":
                                        if task.id and not dry_run:
                                            if decision == "waiting":
                                                db.mark_task_waiting(task.id, retry_after, f"join:{reason}")
                                                task.status = "waiting"
                                                task.retry_after = retry_after
                                                task.last_error = f"join:{reason}"
                                                task.fail_count = getattr(task, "fail_count", 0) + 1
                                            else:
                                                db.mark_task_error(task.id, f"join:{reason}")
                                                task.status = "error"
                                                task.last_error = f"join:{reason}"
                                                task.fail_count = getattr(task, "fail_count", 0) + 1
                                        print(f"  [!] {task.target_group}: нет доступа ({reason}) — пропуск")
                                        put_progress(
                                            "error",
                                            task_id=task.id,
                                            target=task.target_group,
                                            account=acc.phone,
                                            index=task_i + 1,
                                            total=len(tasks),
                                            error=f"join:{reason}",
                                        )
                                        continue

                                    # Источник текста
                                    if _b_source == "Избранное":
                                        if acc.phone not in _saved_texts_b or not _saved_texts_b[acc.phone]:
                                            saved = await _broadcast_wait(
                                                sender.get_saved_messages(limit=30),
                                                f"{acc.phone}: чтение Избранного",
                                                20,
                                                acc.phone,
                                                task.target_group,
                                            )
                                            _saved_texts_b[acc.phone] = saved or []
                                        raw_msg = random.choice(_saved_texts_b[acc.phone]) if _saved_texts_b.get(acc.phone) else task.message_text
                                    elif _b_source == "Шаблоны":
                                        templates = _split_message_template_variants(task.message_text)
                                        raw_msg = random.choice(templates) if templates else task.message_text
                                    else:
                                        raw_msg = task.message_text
                                    if not (raw_msg or "").strip():
                                        print(f"  [!] Задача #{task.id or '?'}: пустой текст — пропуск")
                                        stats["errors"] += 1
                                        put_progress(
                                            "error",
                                            task_id=task.id,
                                            target=task.target_group,
                                            account=acc.phone,
                                            index=task_i + 1,
                                            total=len(tasks),
                                            error="empty_text",
                                        )
                                        continue
                                    # Уникализация
                                    msg = self._apply_unique(raw_msg, _b_unique)
                                    if dry_run:
                                        preview = msg.replace("\n", " ").strip()
                                        if len(preview) > 120:
                                            preview = preview[:120] + "…"
                                        print(f"  [DRY] {task.target_group} <- {acc.phone}: {preview}")
                                        raw_status = "dry_run"
                                        status = "dry_run"
                                        error_detail = ""
                                    else:
                                        raw_status = await _broadcast_wait(
                                            sender.send_broadcast_message(task.target_group, msg),
                                            f"{task.target_group}: отправка",
                                            45,
                                            acc.phone,
                                            task.target_group,
                                        )
                                        if raw_status is None:
                                            continue
                                        status = raw_status.split(":", 1)[0]
                                        error_detail = raw_status if raw_status != status else ""

                                        db.log_send(SendLog(
                                            account_phone=acc.phone,
                                            target_group=task.target_group,
                                            message_text=msg[:200],
                                            status=status,
                                            error_detail=error_detail[:200],
                                            timestamp=datetime.now().isoformat(),
                                        ))

                                    if status == "sent" and task.id and not dry_run:
                                        db.mark_task_completed(task.id)
                                        task.completed = True
                                        task.status = "done"
                                        stats["sent"] += 1
                                    elif status == "dry_run":
                                        stats["dry_run"] += 1

                                    if task.id and not dry_run and status in ("need_subscription", "no_permission", "private", "slow_mode", "error"):
                                        if status == "need_subscription":
                                            retry_after = (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds")
                                            db.mark_task_waiting(task.id, retry_after, "need_subscription")
                                            task.status = "waiting"
                                            task.retry_after = retry_after
                                            task.last_error = "need_subscription"
                                            task.fail_count = getattr(task, "fail_count", 0) + 1
                                        elif status == "slow_mode":
                                            try:
                                                wait_s = int(raw_status.split(":", 1)[1])
                                            except Exception:
                                                wait_s = 60
                                            retry_after = (datetime.now() + timedelta(seconds=max(wait_s, 1))).isoformat(timespec="seconds")
                                            db.mark_task_waiting(task.id, retry_after, raw_status)
                                            task.status = "waiting"
                                            task.retry_after = retry_after
                                            task.last_error = raw_status
                                            task.fail_count = getattr(task, "fail_count", 0) + 1
                                        elif status in ("no_permission", "private"):
                                            db.mark_task_error(task.id, status)
                                            task.status = "error"
                                            task.last_error = status
                                            task.fail_count = getattr(task, "fail_count", 0) + 1
                                        else:
                                            fail_count = getattr(task, "fail_count", 0) + 1
                                            task.fail_count = fail_count
                                            stats["errors"] += 1
                                            if fail_count >= 3:
                                                db.mark_task_error(task.id, "error")
                                                task.status = "error"
                                                task.last_error = "error"
                                            else:
                                                retry_after = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
                                                db.mark_task_waiting(task.id, retry_after, "error")
                                                task.status = "waiting"
                                                task.retry_after = retry_after
                                                task.last_error = "error"

                                    if status in ("sent", "dry_run"):
                                        put_progress(
                                            "success",
                                            task_id=task.id,
                                            target=task.target_group,
                                            account=acc.phone,
                                            index=task_i + 1,
                                            total=len(tasks),
                                            status=status,
                                        )
                                    else:
                                        put_progress(
                                            "error",
                                            task_id=task.id,
                                            target=task.target_group,
                                            account=acc.phone,
                                            index=task_i + 1,
                                            total=len(tasks),
                                            status=status,
                                            error=error_detail or raw_status,
                                        )

                                    # Терминальные статусы — выходим без паузы
                                    if status in ("flood_wait", "banned"):
                                        break

                                    # Пауза после любой попытки (sent / error / любой не-терминальный).
                                    # Без паузы при ошибках Telegram видит шквал запросов = бот.
                                    if task_i < len(tasks) - 1:
                                        delay = random_broadcast_delay_sec(_settings)
                                        if dry_run:
                                            print(f"  [DRY] Пауза {delay:.0f}с пропущена "
                                                  f"(диапазон {_settings.broadcast_delay_min_seconds}-"
                                                  f"{_settings.broadcast_delay_max_seconds}с)")
                                        else:
                                            print(f"  [~] Пауза {delay:.0f}с (диапазон "
                                                  f"{_settings.broadcast_delay_min_seconds}-"
                                                  f"{_settings.broadcast_delay_max_seconds}с)...")
                                            await _sleep_interruptibly(
                                                delay,
                                                stop_event,
                                                op_name="рассылка",
                                                account=acc.phone,
                                                target=task.target_group,
                                                progress=f"задача={task_i + 1}/{len(tasks)}",
                                            )
                            finally:
                                try:
                                    await asyncio.wait_for(sender.disconnect(), timeout=10)
                                except Exception as e:
                                    print(f"  [!] {acc.phone}: disconnect не завершился быстро ({type(e).__name__})")

                        print("\n=== Задачи рассылки завершены ===")
                    except OperationInterrupted as e:
                        print(str(e))
                        print(
                            f"[=] Задачи рассылки остановлены: аккаунт={current_account} | цель={current_target} "
                            f"| задача={current_task_index}/{len(tasks)} | sent={stats['sent']} "
                            f"| dry_run={stats['dry_run']} | errors={stats['errors']}"
                        )
                        log_queue.put(("broadcast_progress", {"event": "stopping"}))
                    finally:
                        db.close()

                _run_loop(loop, do_broadcast())
            except Exception as e:
                log_queue.put(("broadcast_log", f"[-] Ошибка: {e}"))
            finally:
                _thread_local.log_handler = None
                log_queue.put(("broadcast_progress", {"event": "done"}))
                self.app.log_queue.put(("broadcast_done", {"run_id": run_id}))

        broadcast_worker = threading.Thread(target=broadcast_thread, name="BroadcastQueueWorker", daemon=True)
        self._broadcast_thread = broadcast_worker
        broadcast_worker.start()

    def on_queue_message(self, tag, msg):
        if tag == "broadcast_progress":
            self._handle_broadcast_progress(msg)
        elif tag == "broadcast_log":
            self.log.append(f"[Запуск задач] {msg}")
        elif tag == "cycle_log":
            self.log.append(f"[Циклическая] {msg}")
        elif tag == "cycle_progress":
            self._cycle_update_runtime(msg)
        elif tag in ("mention_done", "broadcast_done"):
            key_by_tag = {
                "mention_done": ("mention", "_mention_thread"),
                "broadcast_done": ("broadcast", "_broadcast_thread"),
            }
            key, thread_attr = key_by_tag[tag]
            run_id = msg.get("run_id") if isinstance(msg, dict) else None
            if run_id is not None and self._regular_run_ids.get(key) != run_id:
                return
            if tag == "mention_done":
                self._mention_thread = None
            elif tag == "broadcast_done":
                self._broadcast_thread = None
            setattr(self, thread_attr, None)
            self._regular_stop_events.pop(key, None)
            self._regular_run_ids.pop(key, None)
            self._clear_regular_stop_watchdog()
            self._finish_regular_worker_ui()
        elif tag == "cycle_done":
            if isinstance(msg, dict):
                campaign_name = msg.get("campaign")
                run_id = msg.get("run_id")
            else:
                campaign_name = msg if isinstance(msg, str) else None
                run_id = None
            if campaign_name and run_id:
                runner = self._cycle_get_runner(campaign_name)
                if not isinstance(runner, dict) or runner.get("run_id") != run_id:
                    self.log.append(f"[Циклическая] [i] Игнорирую старый сигнал остановки: {campaign_name}")
                    return
            suffix = f": {campaign_name}" if campaign_name else ""
            self.log.append(f"[Циклическая] [=] Циклическая кампания остановлена{suffix}")
            self._cycle_finish_stopped_ui(campaign_name)
        elif tag == "check_done":
            run_id = msg.get("run_id") if isinstance(msg, dict) else None
            if run_id is not None and self._regular_run_ids.get("check") != run_id:
                return
            self._check_thread = None
            self._regular_stop_events.pop("check", None)
            self._regular_run_ids.pop("check", None)
            self._clear_regular_stop_watchdog()
            self._finish_regular_worker_ui()

    def _begin_regular_run(self, key: str, stop_event: threading.Event) -> int:
        self._regular_run_seq = int(getattr(self, "_regular_run_seq", 0) or 0) + 1
        run_id = self._regular_run_seq
        self._stop_event = stop_event
        self._regular_stop_events[key] = stop_event
        self._regular_run_ids[key] = run_id
        return run_id

    def _set_all_regular_stop_events(self):
        try:
            self._stop_event.set()
        except Exception:
            pass
        for event in list(getattr(self, "_regular_stop_events", {}).values()):
            try:
                event.set()
            except Exception:
                pass

    def _clear_regular_stop_watchdog(self):
        after_id = getattr(self, "_stop_watchdog_after_id", None)
        self._stop_watchdog_after_id = None
        if after_id:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass

    def _schedule_regular_stop_watchdog(self):
        self._clear_regular_stop_watchdog()
        expected_run_ids = dict(getattr(self, "_regular_run_ids", {}))
        try:
            self._stop_watchdog_after_id = self.after(
                STOP_UI_FORCE_MS,
                lambda: self._force_regular_stop_ui(expected_run_ids),
            )
        except Exception:
            self._stop_watchdog_after_id = None

    def _finish_regular_worker_ui(self, forced: bool = False):
        self._running = False
        self._active_op_name = ""
        try:
            busy = self.get_busy_accounts()
            to_free = [p for p, ctx in busy.items() if "Циклическая" not in (ctx or "")]
            if to_free:
                self.mark_account_free(to_free)
        except Exception:
            pass
        self.btn_mention.configure(state="normal", text="Начать упоминания")
        self.btn_broadcast.configure(state="normal", text="Запустить задачи")
        self.btn_check.configure(state="normal", text="Проверить и очистить")
        self.btn_stop_current.configure(state="disabled", text="■ Остановить текущий процесс")
        if forced:
            self.log.append("[~] Stop cleanup timed out; UI unlocked, old worker left in background with stop flag set.")

    def _force_regular_stop_ui(self, expected_run_ids: dict[str, int] | None = None):
        self._stop_watchdog_after_id = None
        if expected_run_ids:
            current_ids = getattr(self, "_regular_run_ids", {})
            if not any(current_ids.get(key) == run_id for key, run_id in expected_run_ids.items()):
                return
        if not (getattr(self, "_running", False) or self._regular_worker_alive()):
            return
        self._set_all_regular_stop_events()
        stuck_keys = []
        for key, attr in (
            ("mention", "_mention_thread"),
            ("broadcast", "_broadcast_thread"),
            ("check", "_check_thread"),
        ):
            if self._worker_alive(attr):
                stuck_keys.append(key)
            setattr(self, attr, None)
        for key in stuck_keys:
            self._regular_stop_events.pop(key, None)
            self._regular_run_ids.pop(key, None)
        self._finish_regular_worker_ui(forced=True)

    def _worker_alive(self, attr_name: str) -> bool:
        thread = getattr(self, attr_name, None)
        return bool(thread is not None and thread.is_alive())

    def _regular_worker_alive(self) -> bool:
        return any(
            self._worker_alive(attr)
            for attr in ("_mention_thread", "_broadcast_thread", "_check_thread")
        )

    def _active_regular_op_label(self) -> str:
        if getattr(self, "_running", False) and getattr(self, "_active_op_name", ""):
            return self._active_op_name
        if self._worker_alive("_mention_thread"):
            return "упоминания"
        if self._worker_alive("_broadcast_thread"):
            return "рассылку"
        if self._worker_alive("_check_thread"):
            return "проверку задач"
        return "текущий процесс"

    def _stop_current_process(self):
        runners = getattr(self, "_cycle_runners", None) or {}
        active_cycle_names = [name for name, runner in runners.items() if self._cycle_runner_alive(runner)]
        old_thread = getattr(self, "_cycle_thread", None)
        old_cycle_active = getattr(self, "_cycle_running", False) or (old_thread is not None and old_thread.is_alive())
        cycle_active = bool(active_cycle_names) or old_cycle_active
        regular_active = getattr(self, "_running", False) or self._regular_worker_alive()

        if not regular_active and not cycle_active:
            return
        if regular_active:
            self._set_all_regular_stop_events()
            self._schedule_regular_stop_watchdog()
            label = self._active_regular_op_label()
            self.btn_stop_current.configure(state="disabled", text=f"■ Остановка: {label}")
            self.log.append(f"[~] Остановка запрошена: {label}...")
            if label == "рассылку" or self._worker_alive("_broadcast_thread"):
                self._handle_broadcast_progress({"event": "stopping"})
            if label == "упоминания" or self._worker_alive("_mention_thread"):
                try:
                    self.btn_mention.configure(state="disabled", text="Останавливается...")
                except Exception:
                    pass
            if label == "проверку задач" or self._worker_alive("_check_thread"):
                try:
                    self.btn_check.configure(state="disabled", text="Останавливается...")
                except Exception:
                    pass
        if cycle_active:
            if active_cycle_names:
                self.log.append(f"[Циклическая] [~] Запрошена остановка кампаний: {', '.join(active_cycle_names)}")
                for name in active_cycle_names:
                    self._stop_cycle(name)
            else:
                self.log.append("[Циклическая] [~] Запрошена остановка циклической рассылки")
                try:
                    if getattr(self, "_cycle_stop_event", None) is not None:
                        self._cycle_stop_event.set()
                except Exception:
                    pass
                try:
                    self._stop_cycle()
                except Exception:
                    pass



class ChannelCommenterFrame(ctk.CTkFrame):
    """Раздел: Комментирование каналов"""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._listener = None
        self._loop = None
        self._old_stop_event = threading.Event()

        ctk.CTkLabel(self, text="Комментирование каналов",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(
            padx=20, pady=(15, 5), anchor="w")

        # --- Выбор аккаунта ---
        sel_row = ctk.CTkFrame(self, fg_color="transparent")
        sel_row.pack(padx=20, pady=(0, 8), fill="x")

        ctk.CTkLabel(sel_row, text="Аккаунт:").pack(side="left", padx=(0, 8))
        self.account_var = ctk.StringVar(value="")
        self.account_combo = ctk.CTkComboBox(sel_row, variable=self.account_var,
                                              width=220, state="readonly")
        self.account_combo.pack(side="left")
        ctk.CTkButton(sel_row, text="↻", width=36,
                      command=self._refresh_accounts).pack(side="left", padx=6)

        ai_box = ctk.CTkFrame(self, fg_color="transparent")
        ai_box.pack(padx=20, pady=(0, 6), fill="x")
        ai_box.grid_columnconfigure(1, weight=1)

        self.cc_ai_enabled = ctk.BooleanVar(value=False)
        self.cc_dry_run = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(ai_box, text="AI-комментарии", variable=self.cc_ai_enabled).grid(
            row=0, column=0, padx=(0, 10), pady=4, sticky="w"
        )
        ctk.CTkCheckBox(ai_box, text="Dry Run", variable=self.cc_dry_run).grid(
            row=0, column=1, padx=(0, 10), pady=4, sticky="w"
        )

        ctk.CTkLabel(ai_box, text="Провайдер (Groq ≠ Grok/xAI):", anchor="w").grid(
            row=1, column=0, padx=(0, 10), pady=4, sticky="w"
        )
        self.cc_provider_var = ctk.StringVar(value="openai")
        ctk.CTkSegmentedButton(
            ai_box, values=["openai", "groq"], variable=self.cc_provider_var, command=self._on_cc_provider_change
        ).grid(row=1, column=1, padx=(0, 10), pady=4, sticky="w")

        ctk.CTkLabel(ai_box, text="Тон:", anchor="w").grid(
            row=2, column=0, padx=(0, 10), pady=4, sticky="w"
        )
        self.cc_tone_var = ctk.StringVar(value="нейтральный")
        ctk.CTkOptionMenu(
            ai_box,
            variable=self.cc_tone_var,
            values=["нейтральный", "дружелюбный", "деловой", "флирт", "сарказм", "продающий"],
            width=220,
        ).grid(row=2, column=1, padx=(0, 10), pady=4, sticky="w")

        ctk.CTkLabel(ai_box, text="Длина:", anchor="w").grid(
            row=3, column=0, padx=(0, 10), pady=4, sticky="w"
        )
        self.cc_length_var = ctk.StringVar(value="короткий")
        ctk.CTkOptionMenu(
            ai_box,
            variable=self.cc_length_var,
            values=["короткий", "средний", "длинный"],
            width=220,
        ).grid(row=3, column=1, padx=(0, 10), pady=4, sticky="w")

        ctk.CTkLabel(ai_box, text="Промпт (system):", anchor="w").grid(
            row=4, column=0, padx=(0, 10), pady=4, sticky="nw"
        )
        self.cc_prompt = ctk.CTkTextbox(ai_box, height=90)
        self.cc_prompt.grid(row=4, column=1, padx=(0, 10), pady=4, sticky="ew")

        ctk.CTkLabel(ai_box, text="Промпт (user):", anchor="w").grid(
            row=5, column=0, padx=(0, 10), pady=4, sticky="nw"
        )
        self.cc_user_prompt = ctk.CTkTextbox(ai_box, height=60)
        self.cc_user_prompt.grid(row=5, column=1, padx=(0, 10), pady=4, sticky="ew")

        ctk.CTkButton(ai_box, text="💾 Сохранить AI-настройки", width=180,
                      command=self._save_comment_ai_settings).grid(
            row=6, column=0, columnspan=2, pady=(6, 2), sticky="w"
        )

        # --- Табы: Старые посты / Новые посты ---
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(padx=20, pady=5, fill="both", expand=True)
        self.tabview.add("Старые посты")
        self.tabview.add("Новые посты")

        self._build_old_tab()
        self._build_new_tab()

        # --- Лог ---
        self.log = LogFrame(self, height=150)
        self.log.pack(padx=20, pady=(5, 12), fill="x")

        self._refresh_accounts()
        self._load_comment_ai_settings()

    def _load_comment_ai_settings(self):
        try:
            from ads_database import AdsDB
            from channel_ai import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT
            adb = AdsDB(self.app.config.db_path)
            try:
                s = adb.load_scheduler_settings()
                provider = adb.get_setting("channel_comment_ai_provider", getattr(s, "ai_provider", "openai") or "openai") or "openai"
                enabled = adb.get_setting("channel_comment_ai_enabled", "0") == "1"
                dry_run = adb.get_setting("channel_comment_ai_dry_run", "0") == "1"
                tone = adb.get_setting("channel_comment_ai_tone", "нейтральный") or "нейтральный"
                length = adb.get_setting("channel_comment_ai_length", "короткий") or "короткий"
                prompt = adb.get_setting("channel_comment_ai_prompt", DEFAULT_SYSTEM_PROMPT) or DEFAULT_SYSTEM_PROMPT
                user_prompt = adb.get_setting("channel_comment_ai_user_prompt", DEFAULT_USER_PROMPT) or DEFAULT_USER_PROMPT
            finally:
                adb.close()

            self.cc_provider_var.set(provider if provider in ("openai", "groq") else "openai")
            self.cc_ai_enabled.set(bool(enabled))
            self.cc_dry_run.set(bool(dry_run))
            self.cc_tone_var.set(tone)
            self.cc_length_var.set(length)
            self.cc_prompt.delete("1.0", "end")
            self.cc_prompt.insert("1.0", prompt)
            self.cc_user_prompt.delete("1.0", "end")
            self.cc_user_prompt.insert("1.0", user_prompt)
        except Exception:
            pass

    def _save_comment_ai_settings(self):
        try:
            from ads_database import AdsDB
            adb = AdsDB(self.app.config.db_path)
            try:
                adb.set_setting("channel_comment_ai_provider", self.cc_provider_var.get())
                adb.set_setting("channel_comment_ai_enabled", "1" if self.cc_ai_enabled.get() else "0")
                adb.set_setting("channel_comment_ai_dry_run", "1" if self.cc_dry_run.get() else "0")
                adb.set_setting("channel_comment_ai_tone", self.cc_tone_var.get())
                adb.set_setting("channel_comment_ai_length", self.cc_length_var.get())
                adb.set_setting("channel_comment_ai_prompt", self.cc_prompt.get("1.0", "end").strip())
                adb.set_setting("channel_comment_ai_user_prompt", self.cc_user_prompt.get("1.0", "end").strip())
            finally:
                adb.close()
            self.log.append("[+] AI-настройки сохранены")
        except Exception as e:
            self.log.append(f"[-] Не удалось сохранить AI-настройки: {e}")

    def _build_ai_config(self) -> dict | None:
        if not self.cc_ai_enabled.get():
            return None
        provider = (self.cc_provider_var.get() or "openai").strip()
        tone = (self.cc_tone_var.get() or "нейтральный").strip()
        length = (self.cc_length_var.get() or "короткий").strip()
        system_prompt = self.cc_prompt.get("1.0", "end").strip()
        user_prompt = self.cc_user_prompt.get("1.0", "end").strip()

        api_key = ""
        model = ""
        proxy = ""
        if provider not in ("openai", "groq"):
            self.log.append(f"[!] Неизвестный AI-провайдер: {provider!r}. Доступны: openai, groq")
            return None
        try:
            if provider == "groq":
                import groq  # noqa: F401
            else:
                import openai  # noqa: F401
        except Exception as e:
            self.log.append(f"[!] Не найден пакет для провайдера {provider}: {type(e).__name__}: {e}")
            return None
        try:
            from ads_database import AdsDB
            adb = AdsDB(self.app.config.db_path)
            try:
                s = adb.load_scheduler_settings()
                if provider == "groq":
                    api_key = getattr(self.app.config, "groq_api_key", "") or ""
                    proxy = getattr(self.app.config, "groq_proxy", "") or getattr(self.app.config, "openai_proxy", "") or ""
                    model = getattr(s, "ai_model_groq", "") or "llama-3.3-70b-versatile"
                else:
                    api_key = getattr(self.app.config, "openai_api_key", "") or ""
                    proxy = getattr(self.app.config, "openai_proxy", "") or ""
                    model = getattr(s, "ai_model_openai", "") or "gpt-4o-mini"
            finally:
                adb.close()
        except Exception:
            pass

        if not api_key:
            self.log.append(f"[!] Нет API-ключа для провайдера '{provider}'. Укажи ключ в Настройках.")
            return None

        return {
            "provider": provider,
            "api_key": api_key,
            "model": model,
            "proxy": proxy,
            "tone": tone,
            "length": length,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        }

    def _on_cc_provider_change(self, value):
        p = (value or "").strip()
        if p == "groq":
            self.log.append("[i] Выбран Groq (groq.com). Это НЕ Grok/xAI.")

    def _build_old_tab(self):
        tab = self.tabview.tab("Старые посты")
        tab.grid_columnconfigure(1, weight=1)

        # Каналы
        old_hdr = ctk.CTkFrame(tab, fg_color="transparent")
        old_hdr.grid(row=0, column=0, padx=(10, 6), pady=5, sticky="nw")
        ctk.CTkLabel(old_hdr, text="Каналы:", anchor="w").pack(side="left")
        ctk.CTkButton(old_hdr, text="Шаблон", width=90,
                      command=lambda: self._load_channels_from_template("old")).pack(
            side="left", padx=(6, 0))
        ctk.CTkButton(old_hdr, text="Сохранить", width=110,
                      command=lambda: self._save_channels_template("old")).pack(
            side="left", padx=(6, 0))
        self.old_channels = ctk.CTkTextbox(tab, height=70)
        self.old_channels.grid(row=0, column=1, padx=(0, 10), pady=5, sticky="ew")
        ctk.CTkLabel(tab, text="По одному на строку (@channel или ссылка)",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=1, column=1, padx=(0, 10), sticky="w")

        # Комментарии
        ctk.CTkLabel(tab, text="Комментарии:", anchor="w").grid(
            row=2, column=0, padx=(10, 6), pady=5, sticky="nw")
        self.old_messages = ctk.CTkTextbox(tab, height=80)
        self.old_messages.grid(row=2, column=1, padx=(0, 10), pady=5, sticky="ew")
        ctk.CTkLabel(tab, text="По одному на строку — выбирается случайно",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=3, column=1, padx=(0, 10), sticky="w")

        # Параметры
        params = ctk.CTkFrame(tab, fg_color="transparent")
        params.grid(row=4, column=0, columnspan=2, padx=10, pady=8, sticky="ew")

        ctk.CTkLabel(params, text="Постов на канал:").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        self.old_limit = ctk.CTkEntry(params, width=80, placeholder_text="10")
        self.old_limit.grid(row=0, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(params, text="Задержка мин (сек):").grid(row=0, column=2, padx=5, pady=3, sticky="w")
        self.old_delay_min = ctk.CTkEntry(params, width=70, placeholder_text="10")
        self.old_delay_min.grid(row=0, column=3, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(params, text="Задержка макс (сек):").grid(row=0, column=4, padx=5, pady=3, sticky="w")
        self.old_delay_max = ctk.CTkEntry(params, width=70, placeholder_text="25")
        self.old_delay_max.grid(row=0, column=5, padx=5, pady=3, sticky="w")

        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.grid(row=5, column=0, columnspan=2, pady=10)

        self.btn_old_start = ctk.CTkButton(btn_row, text="▶ Запустить",
                                            width=140, command=self._start_old)
        self.btn_old_start.pack(side="left", padx=(0, 8))

        self.btn_old_stop = ctk.CTkButton(btn_row, text="■ Остановить",
                                           width=140, state="disabled",
                                           fg_color="firebrick",
                                           hover_color="darkred",
                                           command=self._stop_old)
        self.btn_old_stop.pack(side="left")

    def _build_new_tab(self):
        tab = self.tabview.tab("Новые посты")
        tab.grid_columnconfigure(1, weight=1)

        # Каналы
        new_hdr = ctk.CTkFrame(tab, fg_color="transparent")
        new_hdr.grid(row=0, column=0, padx=(10, 6), pady=5, sticky="nw")
        ctk.CTkLabel(new_hdr, text="Каналы:", anchor="w").pack(side="left")
        ctk.CTkButton(new_hdr, text="Шаблон", width=90,
                      command=lambda: self._load_channels_from_template("new")).pack(
            side="left", padx=(6, 0))
        ctk.CTkButton(new_hdr, text="Сохранить", width=110,
                      command=lambda: self._save_channels_template("new")).pack(
            side="left", padx=(6, 0))
        self.new_channels = ctk.CTkTextbox(tab, height=70)
        self.new_channels.grid(row=0, column=1, padx=(0, 10), pady=5, sticky="ew")
        ctk.CTkLabel(tab, text="По одному на строку",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=1, column=1, padx=(0, 10), sticky="w")
        ctk.CTkLabel(tab, text="Режим 'Новые посты' комментирует только посты, появившиеся после запуска",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=2, column=1, padx=(0, 10), sticky="w")

        # Комментарии
        ctk.CTkLabel(tab, text="Комментарии:", anchor="w").grid(
            row=3, column=0, padx=(10, 6), pady=5, sticky="nw")
        self.new_messages = ctk.CTkTextbox(tab, height=80)
        self.new_messages.grid(row=3, column=1, padx=(0, 10), pady=5, sticky="ew")
        ctk.CTkLabel(tab, text="По одному на строку — выбирается случайно",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=4, column=1, padx=(0, 10), sticky="w")

        # Задержки
        params = ctk.CTkFrame(tab, fg_color="transparent")
        params.grid(row=5, column=0, columnspan=2, padx=10, pady=8, sticky="ew")

        ctk.CTkLabel(params, text="Задержка мин (сек):").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        self.new_delay_min = ctk.CTkEntry(params, width=70, placeholder_text="5")
        self.new_delay_min.grid(row=0, column=1, padx=5, pady=3, sticky="w")

        ctk.CTkLabel(params, text="Задержка макс (сек):").grid(row=0, column=2, padx=5, pady=3, sticky="w")
        self.new_delay_max = ctk.CTkEntry(params, width=70, placeholder_text="15")
        self.new_delay_max.grid(row=0, column=3, padx=5, pady=3, sticky="w")

        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.grid(row=6, column=0, columnspan=2, pady=10)

        self.btn_new_start = ctk.CTkButton(btn_row, text="▶ Запустить",
                                            width=140, command=self._start_new)
        self.btn_new_start.pack(side="left", padx=(0, 8))

        self.btn_new_stop = ctk.CTkButton(btn_row, text="■ Остановить",
                                           width=140, state="disabled",
                                           fg_color="firebrick",
                                           hover_color="darkred",
                                           command=self._stop_new)
        self.btn_new_stop.pack(side="left")

    # ---- Helpers ----

    def _refresh_accounts(self):
        db = Database(self.app.config.db_path)
        accounts = db.get_all_accounts()
        db.close()
        displays = [format_account(a.phone, getattr(a, "custom_name", "")) for a in accounts]
        self.account_combo.configure(values=displays)
        if displays and not self.account_var.get():
            self.account_var.set(displays[0])

    def _get_account(self):
        val = (self.account_var.get() or "").strip()
        phone = val
        if "(" in val and val.endswith(")"):
            # extract phone from "Name (phone)"
            phone = val.rsplit("(", 1)[-1].rstrip(")")
        if not phone:
            self.log.append("[!] Выберите аккаунт")
            return None
        db = Database(self.app.config.db_path)
        accounts = db.get_all_accounts()
        db.close()
        for a in accounts:
            if a.phone == phone:
                return a
        self.log.append("[!] Аккаунт не найден")
        return None

    def _parse_lines(self, textbox) -> list:
        return [l.strip() for l in textbox.get("1.0", "end").splitlines() if l.strip()]

    def _load_channels_from_template(self, which: str):
        db = Database(self.app.config.db_path)
        templates = [t for t in db.get_all_list_templates() if t.get("kind") in ("channels", "mixed")]
        db.close()
        if not templates:
            self.log.append("[!] Нет шаблонов каналов (создай в разделе 'Шаблоны')")
            return
        pick = ListTemplatePickerDialog(self, templates, title="Выбор шаблона каналов")
        self.wait_window(pick)
        if not pick.result:
            return

        textbox = self.old_channels if which == "old" else self.new_channels
        textbox.delete("1.0", "end")
        textbox.insert("1.0", (pick.result.get("content") or "").strip() + "\n")
        self.log.append(f"[+] Загружен шаблон: {pick.result['name']}")

    def _save_channels_template(self, which: str):
        textbox = self.old_channels if which == "old" else self.new_channels
        lines = [l.strip() for l in textbox.get("1.0", "end").splitlines() if l.strip()]
        if not lines:
            self.log.append("[!] Список каналов пустой")
            return
        dlg = ctk.CTkInputDialog(
            text="Название шаблона каналов:",
            title="Сохранить шаблон")
        name = (dlg.get_input() or "").strip()
        if not name:
            return
        try:
            db = Database(self.app.config.db_path)
            db.add_list_template(name, "channels", "\n".join(lines))
            db.close()
            self.log.append(f"[+] Шаблон сохранён: {name}")
        except Exception as e:
            self.log.append(f"[!] Не удалось сохранить шаблон: {e}")

    def _get_float(self, entry, default: float) -> float:
        try:
            return float(entry.get().strip())
        except Exception:
            return default

    def _get_int(self, entry, default: int) -> int:
        try:
            return int(entry.get().strip())
        except Exception:
            return default

    # ---- Старые посты ----

    def _start_old(self):
        _log_action("channel", "_start_old")
        acc = self._get_account()
        if not acc:
            return
        channels = self._parse_lines(self.old_channels)
        messages = self._parse_lines(self.old_messages)
        ai_cfg = self._build_ai_config()
        dry_run = bool(self.cc_dry_run.get()) if hasattr(self, "cc_dry_run") else False
        if self.cc_ai_enabled.get() and not ai_cfg:
            self.log.append("[!] AI включён, но провайдер/ключ не настроены. Исправь настройки и запусти снова.")
            return
        if not channels:
            self.log.append("[!] Введите хотя бы один канал")
            return
        if not messages and not ai_cfg:
            self.log.append("[!] Введите хотя бы один комментарий или включите AI-комментарии")
            return

        limit    = max(1, self._get_int(self.old_limit, 10))
        dmin     = max(0.0, self._get_float(self.old_delay_min, 10.0))
        dmax     = max(dmin, self._get_float(self.old_delay_max, 25.0))

        self._old_stop_event.clear()
        self.btn_old_start.configure(state="disabled")
        self.btn_old_stop.configure(state="normal")
        self.log.append(
            f"[i] Preflight: аккаунт={acc.phone}, каналов={len(channels)}, "
            f"комментариев={len(messages)}, AI={'ON' if ai_cfg else 'OFF'}, "
            f"Dry Run={dry_run}, постов={limit}, задержка={dmin:g}-{dmax:g}с"
        )
        if dry_run:
            self.log.append("[i] Dry Run включён: реальные комментарии не отправляются")
        if dry_run and ai_cfg:
            self.log.append("[i] AI включён: даже в Dry Run генерация может делать API-запросы")
        if ai_cfg:
            self.log.append(f"[~] Старые посты: {len(channels)} каналов, {limit} постов каждый (AI={ai_cfg['provider']}, Dry={dry_run})...")
        else:
            self.log.append(f"[~] Старые посты: {len(channels)} каналов, {limit} постов каждый...")

        log_queue = self.app.log_queue

        def thread():
            loop = asyncio.new_event_loop()
            _thread_local.log_handler = lambda m: log_queue.put(("channel_log", m))
            _thread_local.log_tag = "channel"

            async def do():
                from sender import TelegramSender
                sender = TelegramSender(acc, self.app.config,
                                        Database(self.app.config.db_path))
                try:
                    if not await sender.connect():
                        print("[!] Не удалось подключиться")
                        return
                    await channel_commenter.comment_old_posts(
                        sender.client, channels, messages,
                        limit_posts=limit,
                        delay_min=dmin, delay_max=dmax,
                        ai_enabled=bool(ai_cfg),
                        ai_config=ai_cfg,
                        dry_run=dry_run,
                        db=sender.db,
                        account_phone=acc.phone,
                        stop_requested=self._old_stop_event.is_set,
                    )
                finally:
                    await sender.disconnect()

            _run_loop(loop, do())
            _thread_local.log_handler = None
            log_queue.put(("channel_old_done", None))

        threading.Thread(target=thread, daemon=True).start()

    def _stop_old(self):
        _log_action("channel", "_stop_old")
        self._old_stop_event.set()
        if hasattr(self, "btn_old_stop"):
            self.btn_old_stop.configure(state="disabled")
        self.log.append("[~] Остановка старых постов запрошена...")

    # ---- Новые посты ----

    def _start_new(self):
        _log_action("channel", "_start_new")
        acc = self._get_account()
        if not acc:
            return
        channels = self._parse_lines(self.new_channels)
        messages = self._parse_lines(self.new_messages)
        ai_cfg = self._build_ai_config()
        dry_run = bool(self.cc_dry_run.get()) if hasattr(self, "cc_dry_run") else False
        if self.cc_ai_enabled.get() and not ai_cfg:
            self.log.append("[!] AI включён, но провайдер/ключ не настроены. Исправь настройки и запусти снова.")
            return
        if not channels:
            self.log.append("[!] Введите хотя бы один канал")
            return
        if not messages and not ai_cfg:
            self.log.append("[!] Введите хотя бы один комментарий или включите AI-комментарии")
            return

        dmin = max(0.0, self._get_float(self.new_delay_min, 5.0))
        dmax = max(dmin, self._get_float(self.new_delay_max, 15.0))

        self.btn_new_start.configure(state="disabled")
        self.btn_new_stop.configure(state="normal")
        self.log.append(
            f"[i] Preflight: аккаунт={acc.phone}, каналов={len(channels)}, "
            f"комментариев={len(messages)}, AI={'ON' if ai_cfg else 'OFF'}, "
            f"Dry Run={dry_run}, задержка={dmin:g}-{dmax:g}с"
        )
        if dry_run:
            self.log.append("[i] Dry Run включён: реальные комментарии не отправляются")
        if dry_run and ai_cfg:
            self.log.append("[i] AI включён: даже в Dry Run генерация может делать API-запросы")
        if ai_cfg:
            self.log.append(f"[~] Новые посты: {len(channels)} каналов (AI={ai_cfg['provider']}, Dry={dry_run})...")
        else:
            self.log.append(f"[~] Новые посты: {len(channels)} каналов...")
        self.log.append("[i] Режим 'Новые посты' комментирует только посты, появившиеся ПОСЛЕ запуска")

        log_queue = self.app.log_queue

        def thread():
            loop = asyncio.new_event_loop()
            self._loop = loop
            _thread_local.log_handler = lambda m: log_queue.put(("channel_log", m))
            _thread_local.log_tag = "channel"

            async def do():
                from sender import TelegramSender
                sender = TelegramSender(acc, self.app.config,
                                        Database(self.app.config.db_path))
                if not await sender.connect():
                    print("[!] Не удалось подключиться")
                    log_queue.put(("channel_new_done", None))
                    return
                self._listener = channel_commenter.NewPostListener(
                    sender.client, channels, messages,
                    delay_min=dmin, delay_max=dmax,
                    progress_cb=print,
                    ai_enabled=bool(ai_cfg),
                    ai_config=ai_cfg,
                    dry_run=dry_run,
                    db=sender.db,
                    account_phone=acc.phone,
                )
                await self._listener.start()
                await sender.disconnect()
                log_queue.put(("channel_new_done", None))

            _run_loop(loop, do())
            self._loop = None
            _thread_local.log_handler = None

        threading.Thread(target=thread, daemon=True).start()

    def _stop_new(self):
        _log_action("channel", "_stop_new")
        if self._listener and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._listener.stop)
        self.btn_new_stop.configure(state="disabled")
        self.log.append("[~] Остановка listener...")

    def on_queue_message(self, tag, msg):
        if tag == "channel_log":
            self.log.append(msg)
        elif tag == "channel_old_done":
            self.btn_old_start.configure(state="normal")
            if hasattr(self, "btn_old_stop"):
                self.btn_old_stop.configure(state="disabled")
            self.log.append("[=] Готово")
        elif tag == "channel_new_done":
            self.btn_new_start.configure(state="normal")
            self.btn_new_stop.configure(state="disabled")
            self.log.append("[=] Listener остановлен")

    def on_show(self):
        self._refresh_accounts()


class AutoReplyFrame(ctk.CTkFrame):
    """Раздел: Автоответчик"""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._listener = None
        self._loop = None
        self._thread = None
        self._stop_requested = threading.Event()
        self._reply_mode_label_to_code = {
            "1 раз до остановки": autoreply.REPLY_MODE_SESSION,
            "1 раз навсегда": autoreply.REPLY_MODE_FOREVER,
            "Каждое сообщение": autoreply.REPLY_MODE_EVERY_MESSAGE,
            "За сессию": autoreply.REPLY_MODE_SESSION,
            "Навсегда": autoreply.REPLY_MODE_FOREVER,
        }
        self._reply_mode_values = [
            "1 раз до остановки",
            "1 раз навсегда",
            "Каждое сообщение",
        ]

        ctk.CTkLabel(self, text="Автоответчик",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(
            padx=20, pady=(15, 5), anchor="w")

        # --- Выбор аккаунта ---
        sel_row = ctk.CTkFrame(self, fg_color="transparent")
        sel_row.pack(padx=20, pady=(0, 10), fill="x")

        ctk.CTkLabel(sel_row, text="Аккаунт:").pack(side="left", padx=(0, 8))
        self.account_var = ctk.StringVar(value="")
        self.account_combo = ctk.CTkComboBox(sel_row, variable=self.account_var,
                                              width=220, state="readonly")
        self.account_combo.pack(side="left")
        ctk.CTkButton(sel_row, text="↻", width=36,
                      command=self._refresh_accounts).pack(side="left", padx=6)

        # --- Настройки ---
        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(padx=20, pady=5, fill="x")
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(form, text="Шаблон ответа:", anchor="w").grid(
            row=0, column=0, padx=(0, 8), pady=6, sticky="nw")
        self.entry_template = ctk.CTkTextbox(form, height=100)
        self.entry_template.grid(row=0, column=1, pady=6, sticky="ew")

        self.reply_mode_var = ctk.StringVar(value=self._reply_mode_values[0])
        ctk.CTkLabel(form, text="Повторные ответы:", anchor="w").grid(
            row=1, column=0, padx=(0, 8), pady=6, sticky="w")
        ctk.CTkSegmentedButton(
            form,
            values=self._reply_mode_values,
            variable=self.reply_mode_var,
        ).grid(row=1, column=1, pady=6, sticky="w")

        ctk.CTkLabel(form, text="Фильтр (включить):", anchor="w").grid(
            row=2, column=0, padx=(0, 8), pady=6, sticky="w")
        self.include_keywords = ctk.CTkEntry(form, placeholder_text="ключ1, ключ2 (если пусто — без фильтра)")
        self.include_keywords.grid(row=2, column=1, pady=6, sticky="ew")

        ctk.CTkLabel(form, text="Фильтр (исключить):", anchor="w").grid(
            row=3, column=0, padx=(0, 8), pady=6, sticky="w")
        self.exclude_keywords = ctk.CTkEntry(form, placeholder_text="слово1, слово2 (не отвечать если найдено)")
        self.exclude_keywords.grid(row=3, column=1, pady=6, sticky="ew")

        # --- Кнопки ---
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(padx=20, pady=10, fill="x")

        self.btn_start = ctk.CTkButton(btn_row, text="▶ Запустить",
                                        width=140, command=self._start)
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_stop = ctk.CTkButton(btn_row, text="■ Остановить",
                                       width=140, state="disabled",
                                       fg_color="firebrick",
                                       hover_color="darkred",
                                       command=self._stop)
        self.btn_stop.pack(side="left")

        self.lbl_status = ctk.CTkLabel(btn_row, text="Статус: остановлен", text_color="gray70")
        self.lbl_status.pack(side="right")

        # --- Лог ---
        self.log = LogFrame(self, height=220)
        self.log.pack(padx=20, pady=(5, 12), fill="both", expand=True)

        self._refresh_accounts()

    def _set_status(self, text: str, color: str = "gray70"):
        try:
            self.lbl_status.configure(text=f"Статус: {text}", text_color=color)
        except Exception:
            pass

    def _refresh_accounts(self):
        db = Database(self.app.config.db_path)
        accounts = db.get_all_accounts()
        db.close()
        displays = [format_account(a.phone, getattr(a, "custom_name", "")) for a in accounts]
        self.account_combo.configure(values=displays)
        if displays and not self.account_var.get():
            self.account_var.set(displays[0])

    def _get_account(self):
        val = (self.account_var.get() or "").strip()
        phone = val
        if "(" in val and val.endswith(")"):
            phone = val.rsplit("(", 1)[-1].rstrip(")")
        if not phone:
            self.log.append("[!] Выберите аккаунт")
            return None
        db = Database(self.app.config.db_path)
        accounts = db.get_all_accounts()
        db.close()
        for a in accounts:
            if a.phone == phone:
                return a
        self.log.append("[!] Аккаунт не найден")
        return None

    def _start(self):
        if self._thread and self._thread.is_alive():
            self.log.append("[!] Автоответчик уже запущен")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self._set_status("запущен", "#2FA572")
            return

        acc = self._get_account()
        if not acc:
            return

        template = self.entry_template.get("1.0", "end").strip()
        if not template:
            self.log.append("[!] Введите шаблон ответа")
            return

        reply_mode = self._reply_mode_label_to_code.get(
            (self.reply_mode_var.get() or "").strip(),
            autoreply.REPLY_MODE_SESSION,
        )
        include_kw = self.include_keywords.get().strip() if hasattr(self, "include_keywords") else ""
        exclude_kw = self.exclude_keywords.get().strip() if hasattr(self, "exclude_keywords") else ""

        self._stop_requested.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.log.append(
            f"[~] Запускаю автоответчик для {acc.phone}..."
            f" Режим: {autoreply.reply_mode_label(reply_mode)}"
        )
        self._set_status("запуск...", "#F39C12")

        log_queue = self.app.log_queue

        def thread():
            loop = asyncio.new_event_loop()
            self._loop = loop
            _thread_local.log_handler = lambda m: log_queue.put(("autoreply_log", m))
            _thread_local.log_tag = "autoreply"

            async def do():
                from sender import TelegramSender
                def _mask_proxy(p: str) -> str:
                    p = (p or "").strip()
                    if not p:
                        return "—"
                    if "@" in p:
                        left, right = p.split("@", 1)
                        if "://" in left:
                            scheme = left.split("://", 1)[0]
                            return f"{scheme}://***@{right}"
                        return f"***@{right}"
                    return p

                def _status_hint(st: str) -> str:
                    if st == "needs_reauth":
                        return "Сессия устарела или отозвана. Переимпортируйте TData/.session для этого аккаунта."
                    if st == "banned":
                        return "Аккаунт забанен Telegram (навсегда или надолго)."
                    if st == "network_issue":
                        return "Проблема сети/прокси/таймаут. Проверь прокси и доступ к Telegram."
                    return ""

                def _print_account_state(stage: str):
                    try:
                        _db = Database(self.app.config.db_path)
                        try:
                            _acc = next((a for a in _db.get_all_accounts() if a.phone == acc.phone), None)
                        finally:
                            _db.close()
                        if not _acc:
                            print(f"  [!] {stage}: аккаунт не найден в БД")
                            return
                        proxy_masked = _mask_proxy(getattr(_acc, "proxy", "") or "")
                        st = getattr(_acc, "status", "") or ""
                        fu = getattr(_acc, "flood_until", "") or ""
                        cfc = getattr(_acc, "connect_fail_count", 0)
                        lsc = getattr(_acc, "last_status_change", "") or ""
                        msg = f"  [i] {stage}: статус={st or '—'}, proxy={proxy_masked}, fails={cfc}"
                        if fu:
                            msg += f", flood_until={fu}"
                        print(msg)
                        if lsc:
                            print(f"  [i] last_status_change: {lsc}")
                        hint = _status_hint(st)
                        if hint:
                            print(f"  [>] {hint}")
                    except Exception:
                        pass

                _print_account_state("Перед запуском")

                sender = TelegramSender(acc, self.app.config,
                                        Database(self.app.config.db_path))
                try:
                    if self._stop_requested.is_set():
                        print("[~] Остановка запрошена до подключения — отмена запуска")
                        return

                    connected = await sender.connect()
                    if not connected:
                        print("[-] Не удалось подключиться — автоответчик не запущен")
                        _print_account_state("После неудачного подключения")
                        detail = "ошибка подключения"
                        try:
                            _db = Database(self.app.config.db_path)
                            try:
                                _acc2 = next((a for a in _db.get_all_accounts() if a.phone == acc.phone), None)
                            finally:
                                _db.close()
                            code = getattr(sender, "last_connect_error_code", "") or ""
                            hint = TelegramSender.connect_problem_hint(code)
                            st = getattr(_acc2, "status", "") if _acc2 else ""
                            if not hint and st == "needs_reauth":
                                hint = TelegramSender.connect_problem_hint("needs_reauth")
                                code = "needs_reauth"
                            if hint:
                                print(f"  [>] {hint}")
                            if code in ("session_locked", "database_locked", "in_app_session_busy"):
                                detail = "сессия/БД занята"
                            elif code == "needs_reauth" or st == "needs_reauth":
                                detail = "нужен переимпорт сессии"
                            elif _acc2 and st:
                                detail = f"подключение: {st}"
                        except Exception:
                            pass
                        log_queue.put(("autoreply_state", ("error", detail)))
                        return

                    if self._stop_requested.is_set():
                        print("[~] Остановка запрошена — отключаюсь")
                        return

                    self._listener = autoreply.AutoReplyListener(
                        sender.client, template,
                        progress_cb=print,
                        reply_mode=reply_mode,
                        db=sender.db,
                        account_phone=acc.phone,
                        include_keywords=include_kw,
                        exclude_keywords=exclude_kw,
                    )
                    log_queue.put(("autoreply_state", ("running", "")))

                    if self._stop_requested.is_set():
                        self._listener.stop()

                    await self._listener.start()
                except Exception as e:
                    print(f"[-] Ошибка автоответчика: {type(e).__name__}: {e}")
                    hint = ""
                    text = str(e).lower()
                    if "database is locked" in text or "database table is locked" in text or "locked" in text:
                        hint = TelegramSender.connect_problem_hint("database_locked")
                    if hint:
                        print(f"  [>] {hint}")
                    log_queue.put(("autoreply_state", ("error", hint or f"{type(e).__name__}")))
                finally:
                    try:
                        await sender.disconnect()
                    except Exception:
                        pass

            try:
                _run_loop(loop, do())
            finally:
                self._loop = None
                self._listener = None
                _thread_local.log_handler = None
                log_queue.put(("autoreply_stopped", None))

        self._thread = threading.Thread(target=thread, daemon=True)
        self._thread.start()

    def _stop(self):
        self._stop_requested.set()
        self.log.append("[~] Остановка автоответчика...")
        self._set_status("остановка...", "#F39C12")
        self.btn_stop.configure(state="disabled")
        if self._listener and self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._listener.stop)
            except Exception:
                pass
        try:
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=2)
        except Exception:
            pass

    def on_queue_message(self, tag, msg):
        if tag == "autoreply_log":
            self.log.append(msg)
        elif tag == "autoreply_state":
            try:
                state, detail = msg if isinstance(msg, tuple) else ("", "")
            except Exception:
                state, detail = "", ""
            if state == "running":
                self._set_status("запущен", "#2FA572")
            elif state == "error":
                self._set_status(f"ошибка ({detail})" if detail else "ошибка", "#E74C3C")
        elif tag == "autoreply_stopped":
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.log.append("[=] Автоответчик остановлен")
            self._set_status("остановлен", "gray70")

    def on_show(self):
        self._refresh_accounts()
        if self._thread and self._thread.is_alive():
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self._set_status("запущен", "#2FA572")
        else:
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self._set_status("остановлен", "gray70")


class AccountManagementFrame(ctk.CTkFrame):
    """Раздел: Управление аккаунтом"""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._running = False

        ctk.CTkLabel(self, text="Управление аккаунтом",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(
            padx=20, pady=(15, 5), anchor="w")

        # --- Выбор аккаунта ---
        sel_row = ctk.CTkFrame(self, fg_color="transparent")
        sel_row.pack(padx=20, pady=(0, 10), fill="x")

        ctk.CTkLabel(sel_row, text="Аккаунт:").pack(side="left", padx=(0, 8))
        self.account_var = ctk.StringVar(value="")
        self.account_combo = ctk.CTkComboBox(sel_row, variable=self.account_var,
                                              width=220, state="readonly")
        self.account_combo.pack(side="left")

        ctk.CTkButton(sel_row, text="↻", width=36,
                      command=self._refresh_accounts).pack(side="left", padx=6)

        # --- Tabview: Профиль / Очистка ---
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(padx=20, pady=5, fill="both", expand=True)

        self.tabview.add("Профиль")
        self.tabview.add("Очистка")

        self._build_profile_tab()
        self._build_cleanup_tab()

        # --- Лог ---
        self.log = LogFrame(self, height=130)
        self.log.pack(padx=20, pady=(0, 12), fill="x")

        self._refresh_accounts()

    # ---- Профиль ----

    def _build_profile_tab(self):
        tab = self.tabview.tab("Профиль")
        tab.grid_columnconfigure(1, weight=1)

        fields = [
            ("Имя:",        "entry_fname"),
            ("Фамилия:",    "entry_lname"),
            ("Bio / статус:", "entry_bio"),
        ]
        for i, (label, attr) in enumerate(fields):
            ctk.CTkLabel(tab, text=label, anchor="w").grid(
                row=i, column=0, padx=(10, 6), pady=6, sticky="w")
            entry = ctk.CTkEntry(tab, width=320)
            entry.grid(row=i, column=1, padx=(0, 10), pady=6, sticky="ew")
            setattr(self, attr, entry)

        # Аватар
        ctk.CTkLabel(tab, text="Аватар:", anchor="w").grid(
            row=3, column=0, padx=(10, 6), pady=6, sticky="w")
        avatar_row = ctk.CTkFrame(tab, fg_color="transparent")
        avatar_row.grid(row=3, column=1, padx=(0, 10), pady=6, sticky="ew")
        self.entry_avatar = ctk.CTkEntry(avatar_row, placeholder_text="путь к файлу .jpg/.jpeg/.png")
        self.entry_avatar.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(avatar_row, text="...", width=36,
                      command=self._pick_avatar).pack(side="left", padx=(4, 0))

        ctk.CTkLabel(tab, text="Папка аватаров:", anchor="w").grid(
            row=4, column=0, padx=(10, 6), pady=6, sticky="w")
        avatar_dir_row = ctk.CTkFrame(tab, fg_color="transparent")
        avatar_dir_row.grid(row=4, column=1, padx=(0, 10), pady=6, sticky="ew")
        self.entry_avatar_dir = ctk.CTkEntry(avatar_dir_row, placeholder_text="путь к папке с .jpg/.jpeg/.png")
        self.entry_avatar_dir.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(avatar_dir_row, text="...", width=36,
                      command=self._pick_avatar_dir).pack(side="left", padx=(4, 0))

        self.avatar_mark_used_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Помечать использованные (перемещать в /used)",
                        variable=self.avatar_mark_used_var).grid(
            row=5, column=1, padx=(0, 10), pady=(0, 6), sticky="w")

        self.profile_dry_run_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Dry Run", variable=self.profile_dry_run_var).grid(
            row=6, column=1, padx=(0, 10), pady=(0, 6), sticky="w")

        ctk.CTkButton(tab, text="Сохранить профиль", width=180,
                      command=self._save_profile).grid(
            row=7, column=0, columnspan=2, pady=14)

    def _build_cleanup_tab(self):
        tab = self.tabview.tab("Очистка")

        ctk.CTkLabel(tab, text="Осторожно: действия необратимы",
                     text_color="orange").pack(pady=(10, 14))
        self.cleanup_dry_run_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Dry Run", variable=self.cleanup_dry_run_var).pack(
            pady=(0, 10))

        ctk.CTkButton(
            tab,
            text="Preview (показать что будет сделано)",
            width=240,
            command=self._preview_cleanup,
        ).pack(pady=(0, 10))

        actions = [
            ("Выйти из всех групп",    "firebrick",  lambda: self._start_cleanup("leave_groups")),
            ("Выйти из всех каналов",  "firebrick",  lambda: self._start_cleanup("leave_channels")),
            ("Удалить все диалоги",    "darkred",    lambda: self._start_cleanup("delete_dialogs")),
            ("Удалить всех ботов",     "gray30",     lambda: self._start_cleanup("delete_bots")),
        ]
        for label, color, cmd in actions:
            ctk.CTkButton(tab, text=label, width=240,
                          fg_color=color, hover_color="gray20",
                          command=cmd).pack(pady=5)

    # ---- Helpers ----

    def _refresh_accounts(self):
        db = Database(self.app.config.db_path)
        accounts = db.get_all_accounts()
        db.close()
        phones = [a.phone for a in accounts]
        self.account_combo.configure(values=phones)
        if phones and not self.account_var.get():
            self.account_var.set(phones[0])

    def _pick_avatar(self):
        path = filedialog.askopenfilename(
            filetypes=[("Изображения", "*.jpg *.jpeg *.png")])
        if path:
            self.entry_avatar.delete(0, "end")
            self.entry_avatar.insert(0, path)

    def _pick_avatar_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.entry_avatar_dir.delete(0, "end")
            self.entry_avatar_dir.insert(0, path)

    def _get_account(self):
        phone = self.account_var.get()
        if not phone:
            self.log.append("[!] Выберите аккаунт")
            return None
        db = Database(self.app.config.db_path)
        accounts = db.get_all_accounts()
        db.close()
        for a in accounts:
            if a.phone == phone:
                return a
        self.log.append("[!] Аккаунт не найден в БД")
        return None

    def _set_buttons(self, state: str):
        for tab_name in ("Профиль", "Очистка"):
            tab = self.tabview.tab(tab_name)
            for w in tab.winfo_children():
                if isinstance(w, ctk.CTkButton):
                    w.configure(state=state)

    # ---- Профиль: сохранение ----

    def _save_profile(self):
        acc = self._get_account()
        if not acc:
            return

        fname  = self.entry_fname.get().strip() or None
        lname  = self.entry_lname.get().strip() or None
        bio    = self.entry_bio.get().strip() or None
        avatar = self.entry_avatar.get().strip() or None
        avatar_dir = self.entry_avatar_dir.get().strip() or None
        avatar_mark_used = bool(self.avatar_mark_used_var.get()) if hasattr(self, "avatar_mark_used_var") else False
        dry_run = bool(self.profile_dry_run_var.get()) if hasattr(self, "profile_dry_run_var") else False

        if not any([fname, lname, bio, avatar, avatar_dir]):
            self.log.append("[!] Заполните хотя бы одно поле")
            return

        self._set_buttons("disabled")
        mode = "DRY-RUN" if dry_run else "LIVE"
        self.log.append(f"[~] Обновляю профиль {acc.phone}... ({mode})")

        log_queue = self.app.log_queue

        def thread():
            loop = asyncio.new_event_loop()
            _thread_local.log_handler = lambda m: log_queue.put(("acct_mgr_log", m))
            _thread_local.log_tag = "acct_mgr"

            async def do():
                from sender import TelegramSender
                sender = TelegramSender(acc, self.app.config,
                                        Database(self.app.config.db_path))
                connected = await sender.connect()
                if not connected:
                    print("[-] Не удалось подключиться — профиль не изменён")
                    return
                ok = await account_manager.change_profile(
                    sender.client,
                    first_name=fname,
                    last_name=lname,
                    bio=bio,
                    avatar_path=avatar,
                    avatar_dir=avatar_dir,
                    avatar_mark_used=avatar_mark_used,
                    progress_cb=print,
                    dry_run=dry_run,
                )
                if not ok:
                    print("[-] Профиль не обновлён (см. ошибки выше)")
                await sender.disconnect()

            _run_loop(loop, do())
            _thread_local.log_handler = None
            log_queue.put(("acct_mgr_done", None))

        threading.Thread(target=thread, daemon=True).start()

    # ---- Очистка ----

    def _run_cleanup(self, acc, label: str, coro_fn, dry_run: bool):
        """Общий runner для очистки"""
        self._set_buttons("disabled")
        mode = "DRY-RUN" if dry_run else "LIVE"
        self.log.append(f"[~] {label} — {acc.phone}... ({mode})")

        log_queue = self.app.log_queue

        def thread():
            loop = asyncio.new_event_loop()
            _thread_local.log_handler = lambda m: log_queue.put(("acct_mgr_log", m))
            _thread_local.log_tag = "acct_mgr"

            async def do():
                from sender import TelegramSender
                sender = TelegramSender(acc, self.app.config,
                                        Database(self.app.config.db_path))
                connected = await sender.connect()
                if not connected:
                    print("[!] Не удалось подключиться")
                    return
                await coro_fn(sender.client, dry_run=dry_run)
                await sender.disconnect()

            _run_loop(loop, do())
            _thread_local.log_handler = None
            log_queue.put(("acct_mgr_done", None))

        threading.Thread(target=thread, daemon=True).start()

    def _preview_cleanup(self):
        self._start_cleanup("preview")

    def _start_cleanup(self, action_key: str):
        acc = self._get_account()
        if not acc:
            return

        dry_run = bool(self.cleanup_dry_run_var.get()) if hasattr(self, "cleanup_dry_run_var") else False

        mapping = {
            "leave_groups": ("Выход из групп", account_manager.leave_groups),
            "leave_channels": ("Выход из каналов", account_manager.leave_channels),
            "delete_dialogs": ("Удаление диалогов", account_manager.delete_dialogs),
            "delete_bots": ("Удаление ботов", account_manager.delete_bots),
            "preview": ("Preview", None),
        }
        label, _ = mapping.get(action_key, ("", None))
        if not label:
            self.log.append("[!] Неизвестное действие очистки")
            return

        self._set_buttons("disabled")
        mode = "DRY-RUN" if dry_run else "LIVE"
        if action_key == "preview":
            self.log.append(f"[~] Preview очистки: {acc.phone}... ({mode})")
        else:
            self.log.append(f"[~] Preview перед действием: {label} — {acc.phone}... ({mode})")

        log_queue = self.app.log_queue

        def thread():
            loop = asyncio.new_event_loop()
            _thread_local.log_handler = lambda m: log_queue.put(("acct_mgr_log", m))
            _thread_local.log_tag = "acct_mgr"

            async def do():
                from sender import TelegramSender
                sender = TelegramSender(acc, self.app.config, Database(self.app.config.db_path))
                connected = await sender.connect()
                if not connected:
                    print("[!] Не удалось подключиться")
                    log_queue.put(("acct_mgr_cleanup_preview", {"ok": False, "error": "connect_failed"}))
                    return
                try:
                    dialogs = await sender.client.get_dialogs()
                    groups = [d for d in dialogs if d.is_group]
                    channels = [d for d in dialogs if d.is_channel and not d.is_group]
                    bots = [d for d in dialogs if d.is_user and getattr(d.entity, "bot", False)]
                    users = [d for d in dialogs if d.is_user and not getattr(d.entity, "bot", False)]

                    targets_count = 0
                    if action_key == "leave_groups":
                        targets_count = len(groups)
                    elif action_key == "leave_channels":
                        targets_count = len(channels)
                    elif action_key == "delete_dialogs":
                        targets_count = len(users)
                    elif action_key == "delete_bots":
                        targets_count = len(bots)

                    preview = {
                        "ok": True,
                        "action_key": action_key,
                        "label": label,
                        "dry_run": dry_run,
                        "phone": acc.phone,
                        "targets_count": targets_count,
                        "counts": {
                            "groups": len(groups),
                            "channels": len(channels),
                            "dialogs": len(users),
                            "bots": len(bots),
                        },
                        "samples": {
                            "groups": [d.name for d in groups[:10]],
                            "channels": [d.name for d in channels[:10]],
                            "dialogs": [d.name for d in users[:10]],
                            "bots": [d.name for d in bots[:10]],
                        },
                    }
                    log_queue.put(("acct_mgr_cleanup_preview", preview))
                except Exception as e:
                    log_queue.put(("acct_mgr_cleanup_preview", {"ok": False, "error": f"{type(e).__name__}: {e}"}))
                finally:
                    try:
                        await sender.disconnect()
                    except Exception:
                        pass

            _run_loop(loop, do())
            _thread_local.log_handler = None

        threading.Thread(target=thread, daemon=True).start()

    def on_queue_message(self, tag, msg):
        if tag == "acct_mgr_log":
            self.log.append(msg)
        elif tag == "acct_mgr_cleanup_preview":
            self._on_cleanup_preview(msg)
        elif tag == "acct_mgr_done":
            self._set_buttons("normal")
            self.log.append("[=] Готово")

    def on_show(self):
        self._refresh_accounts()

    def _on_cleanup_preview(self, payload):
        try:
            if not isinstance(payload, dict) or not payload.get("ok"):
                self.log.append(f"[!] Preview не удалось: {(payload or {}).get('error', 'unknown')}")
                self._set_buttons("normal")
                return

            counts = payload.get("counts", {}) or {}
            samples = payload.get("samples", {}) or {}
            label = payload.get("label", "") or "Очистка"
            phone = payload.get("phone", "") or ""
            action_key = payload.get("action_key", "") or ""
            dry_run = bool(payload.get("dry_run", False))

            self.log.append(f"[i] Preview для {phone}:")
            self.log.append(f"  [i] Группы: {int(counts.get('groups', 0) or 0)}")
            if samples.get("groups"):
                self.log.append(f"    [i] Пример: {', '.join(samples['groups'])}")
            self.log.append(f"  [i] Каналы: {int(counts.get('channels', 0) or 0)}")
            if samples.get("channels"):
                self.log.append(f"    [i] Пример: {', '.join(samples['channels'])}")
            self.log.append(f"  [i] Диалоги: {int(counts.get('dialogs', 0) or 0)}")
            if samples.get("dialogs"):
                self.log.append(f"    [i] Пример: {', '.join(samples['dialogs'])}")
            self.log.append(f"  [i] Боты: {int(counts.get('bots', 0) or 0)}")
            if samples.get("bots"):
                self.log.append(f"    [i] Пример: {', '.join(samples['bots'])}")

            if action_key == "preview":
                self._set_buttons("normal")
                return

            mapping = {
                "leave_groups": account_manager.leave_groups,
                "leave_channels": account_manager.leave_channels,
                "delete_dialogs": account_manager.delete_dialogs,
                "delete_bots": account_manager.delete_bots,
            }
            coro_fn = mapping.get(action_key)
            if coro_fn is None:
                self.log.append("[!] Неизвестное действие")
                self._set_buttons("normal")
                return

            acc = self._get_account()
            if not acc or acc.phone != phone:
                self.log.append("[!] Аккаунт изменился — отмена")
                self._set_buttons("normal")
                return

            if dry_run:
                self.log.append("[i] Dry Run включён — подтверждение не требуется")
                self._run_cleanup(acc, label, coro_fn, dry_run=True)
                return

            targets = int(payload.get("targets_count", 0) or 0)
            dlg = ctk.CTkInputDialog(
                text=(
                    f"{label} для {phone}\n\n"
                    f"Будет затронуто: {targets}\n"
                    f"Группы: {int(counts.get('groups', 0) or 0)} | "
                    f"Каналы: {int(counts.get('channels', 0) or 0)} | "
                    f"Диалоги: {int(counts.get('dialogs', 0) or 0)} | "
                    f"Боты: {int(counts.get('bots', 0) or 0)}\n\n"
                    "Введите DELETE чтобы подтвердить:"
                ),
                title="Подтверждение очистки",
            )
            answer = (dlg.get_input() or "").strip()
            if answer != "DELETE":
                self.log.append("[~] Отменено пользователем")
                self._set_buttons("normal")
                return

            self._run_cleanup(acc, label, coro_fn, dry_run=False)
        except Exception as e:
            self.log.append(f"[!] Ошибка preview/подтверждения: {type(e).__name__}: {e}")
            self._set_buttons("normal")


class StatsFrame(ctk.CTkFrame):
    """Раздел: Статистика — полностью переработанный, красивый и надёжный layout.
    Нет накладок, хорошие отступы, карточки с рамками, таблица с дыханием.
    """

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app

        # Один скроллируемый контейнер — ничего никогда не обрежется
        content = ctk.CTkScrollableFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=6, pady=4)

        # Заголовок
        ctk.CTkLabel(content, text="Статистика", font=ctk.CTkFont(size=22, weight="bold")).pack(
            padx=12, pady=(10, 6), anchor="w")

        # Форма "за период"
        form = ctk.CTkFrame(content, fg_color="transparent")
        form.pack(padx=12, pady=(0, 8), fill="x")

        ctk.CTkLabel(form, text="За дней:").pack(side="left", padx=(0, 6))
        self.days_entry = ctk.CTkEntry(form, width=65)
        self.days_entry.pack(side="left", padx=4)
        self.days_entry.insert(0, "7")

        ctk.CTkButton(form, text="Обновить", width=85, height=26, command=self.refresh).pack(side="left", padx=8)

        # === Красивые карточки (с рамками, не налезают, хорошие отступы) ===
        self.cards_frame = ctk.CTkFrame(content, fg_color="transparent")
        self.cards_frame.pack(padx=12, pady=6, fill="x")

        for c in range(3):
            self.cards_frame.grid_columnconfigure(c, weight=1, minsize=108)
        for r in range(2):
            self.cards_frame.grid_rowconfigure(r, weight=1, minsize=66)

        self.stat_labels = {}
        stats_config = [
            ("total", "Всего", "#3B8ED0"),
            ("sent", "Отправлено", "#2FA572"),
            ("error", "Ошибки", "#E74C3C"),
            ("flood_wait", "Flood Wait", "#F39C12"),
            ("banned", "Бан", "#9B59B6"),
            ("no_permission", "Нет доступа", "#7F8C8D"),
        ]

        for idx, (key, label, color) in enumerate(stats_config):
            card = ctk.CTkFrame(self.cards_frame, corner_radius=10, border_width=1,
                                border_color=("gray70", "gray40"))
            row = idx // 3
            col = idx % 3
            card.grid(row=row, column=col, padx=5, pady=4, sticky="nsew")

            ctk.CTkLabel(card, text=label, font=ctk.CTkFont(size=11),
                          text_color=color).pack(padx=8, pady=(5, 0))
            val_label = ctk.CTkLabel(card, text="0", font=ctk.CTkFont(size=24, weight="bold"),
                                      text_color=color)
            val_label.pack(padx=8, pady=(0, 5))
            self.stat_labels[key] = val_label

        ctk.CTkLabel(content, text="Диагностика", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=12, pady=(12, 3), anchor="w")

        diag_frame = ctk.CTkFrame(content, corner_radius=8, border_width=1,
                                  border_color=("gray70", "gray40"))
        diag_frame.pack(padx=12, pady=4, fill="x")
        diag_frame.grid_columnconfigure(1, weight=1)
        diag_frame.grid_columnconfigure(3, weight=1)

        self.diag_labels = {}
        for row, (key, label) in enumerate([
            ("accounts", "Аккаунты"),
            ("tasks", "Задачи"),
            ("errors", "Ошибки"),
            ("runtime", "Работает сейчас"),
            ("recent", "Последние причины"),
        ]):
            ctk.CTkLabel(diag_frame, text=f"{label}:", font=ctk.CTkFont(weight="bold"),
                         anchor="w").grid(row=row, column=0, padx=10, pady=4, sticky="w")
            value = ctk.CTkLabel(diag_frame, text="—", anchor="w", justify="left", wraplength=860)
            value.grid(row=row, column=1, columnspan=3, padx=8, pady=4, sticky="ew")
            self.diag_labels[key] = value

        # === Таблица по аккаунтам (с дыханием) ===
        ctk.CTkLabel(content, text="По аккаунтам", font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=12, pady=(10, 3), anchor="w")

        self.per_acc_table = ScrollableTable(content,
            columns=["Аккаунт", "Статус", "Количество"], height=300)
        self.per_acc_table.pack(padx=12, pady=4, fill="both", expand=True)

        self.refresh()
        self._schedule_diagnostics_refresh()

    def refresh(self):
        days_str = self.days_entry.get().strip()
        days = int(days_str) if days_str.isdigit() else 7

        db = Database(self.app.config.db_path)
        stats = db.get_stats(days)
        per_acc = db.get_per_account_stats(days)
        diagnostics_snapshot = db.get_diagnostics_snapshot(days)
        db.close()

        for key, label in self.stat_labels.items():
            label.configure(text=str(stats.get(key, 0)))

        # Per-account
        rows = [(p["phone"], p["status"], p["count"]) for p in per_acc]
        self.per_acc_table.set_data(rows)
        self._update_diagnostics_panel(diagnostics_snapshot)

    def _update_diagnostics_panel(self, snapshot: dict):
        if not hasattr(self, "diag_labels"):
            return
        accounts = (snapshot or {}).get("accounts", {})
        tasks = (snapshot or {}).get("tasks", {})
        errors = (snapshot or {}).get("errors", {})
        runtime = self._get_runtime_diagnostics()

        self.diag_labels["accounts"].configure(text=(
            f"активно: {accounts.get('enabled', 0)} | доступно сейчас: {accounts.get('available', 0)} | "
            f"flood wait: {accounts.get('flood_wait', 0)} | reauth: {accounts.get('needs_reauth', 0)} | "
            f"proxy/network: {accounts.get('network_issue', 0)} | ошибок сегодня: {accounts.get('errors_today', 0)}"
        ))
        self.diag_labels["tasks"].configure(text=(
            f"всего: {tasks.get('total', 0)} | готово: {tasks.get('pending', 0)} | "
            f"ожидание: {tasks.get('waiting', 0)} | ошибки: {tasks.get('error', 0)} | "
            f"выполнено: {tasks.get('done', 0)}"
        ))
        by_status = errors.get("by_status", {}) or {}
        status_text = ", ".join(
            f"{human_reason(status)}: {count}"
            for status, count in sorted(by_status.items())
        )
        self.diag_labels["errors"].configure(
            text=f"за период: {errors.get('total', 0)}" + (f" | {status_text}" if status_text else "")
        )
        self.diag_labels["runtime"].configure(text=runtime)

        recent = errors.get("recent", []) or []
        if recent:
            items = []
            for item in recent[:3]:
                account = item.get("account") or "—"
                target = item.get("target") or "—"
                items.append(f"{account} → {target}: {item.get('reason') or human_reason(item.get('status', ''))}")
            self.diag_labels["recent"].configure(text=" | ".join(items))
        else:
            self.diag_labels["recent"].configure(text="нет свежих ошибок")

    def _get_runtime_diagnostics(self) -> str:
        parts = []
        try:
            bf = getattr(self.app, "frames", {}).get("broadcast")
            if bf:
                if getattr(bf, "_running", False):
                    parts.append(getattr(bf, "_active_op_name", "") or "broadcast")
                cycle_names = []
                if hasattr(bf, "_cycle_active_names"):
                    cycle_names = bf._cycle_active_names()
                if cycle_names:
                    parts.append("циклы: " + ", ".join(cycle_names[:4]))
        except Exception:
            pass

        try:
            import ads_gui as _ads_gui
            with _ads_gui._ADS_SCHEDULERS_GUARD:
                _ads_gui._prune_ads_schedulers_locked()
                phones = list(_ads_gui._ADS_RUNNING_SCHEDULERS.keys())
            if phones:
                parts.append("ads: " + ", ".join(phones[:4]))
        except Exception:
            pass

        return " | ".join(parts) if parts else "нет активных процессов"

    def _schedule_diagnostics_refresh(self):
        try:
            if not self.winfo_exists():
                return
            self._diagnostics_after_id = self.after(8000, self._diagnostics_tick)
        except Exception:
            self._diagnostics_after_id = None

    def _diagnostics_tick(self):
        self._diagnostics_after_id = None
        try:
            self.refresh()
        finally:
            self._schedule_diagnostics_refresh()

    def destroy(self):
        try:
            after_id = getattr(self, "_diagnostics_after_id", None)
            if after_id:
                self.after_cancel(after_id)
        except Exception:
            pass
        super().destroy()


class SettingsFrame(ctk.CTkFrame):
    """Раздел: Настройки"""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app

        ctk.CTkLabel(self, text="Настройки", font=ctk.CTkFont(size=20, weight="bold")).pack(
            padx=20, pady=(15, 5), anchor="w")

        # Прокручиваемый контейнер — настроек уже много, чтобы помещались
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(padx=20, pady=10, fill="both", expand=True)

        form = ctk.CTkFrame(scroll, fg_color="transparent")
        form.pack(fill="x")

        self.entries = {}
        self.int_entries = {}  # для числовых настроек из БД
        config = self.app.config

        # ─── Секция: API-ключи (.env) ────────────────────────────────────
        ctk.CTkLabel(form, text="API-ключи и прокси (сохраняются в .env)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="gray75").grid(
            row=0, column=0, columnspan=2, padx=0, pady=(0, 8), sticky="w")

        editable_fields = [
            ("OPENAI_API_KEY", "OpenAI API Key:", config.openai_api_key),
            ("OPENAI_MODEL", "OpenAI Model:", config.openai_model),
            ("OPENAI_PROXY", "OpenAI Proxy (опц., но рекоменд.):", config.openai_proxy),
            ("GROQ_API_KEY", "Groq API Key:", config.groq_api_key),
            ("GROQ_PROXY", "Groq Proxy (опц., но рекоменд.):", config.groq_proxy),
        ]

        for idx, (key, label, value) in enumerate(editable_fields, start=1):
            ctk.CTkLabel(form, text=label, anchor="w").grid(
                row=idx, column=0, padx=(0, 10), pady=5, sticky="w")
            entry = ctk.CTkEntry(form, width=300)
            entry.grid(row=idx, column=1, padx=5, pady=5, sticky="w")
            entry.insert(0, value)
            self.entries[key] = entry

        # Read-only поля
        ro_start = len(editable_fields) + 1
        readonly_fields = [
            ("DB_PATH", "Путь к БД:", config.db_path),
            ("SESSIONS_DIR", "Папка сессий:", config.sessions_dir),
        ]

        for idx, (key, label, value) in enumerate(readonly_fields):
            row = ro_start + idx
            ctk.CTkLabel(form, text=label, anchor="w").grid(
                row=row, column=0, padx=(0, 10), pady=5, sticky="w")
            entry = ctk.CTkEntry(form, width=300, state="disabled")
            entry.grid(row=row, column=1, padx=5, pady=5, sticky="w")
            entry.configure(state="normal")
            entry.insert(0, value)
            entry.configure(state="disabled")

        # ─── Секция: Импорт TData (БД) ─────────────────────────────────
        # Загружаем текущие значения из БД
        from ads_database import AdsDB
        try:
            _adb = AdsDB(self.app.config.db_path)
            try:
                _settings = _adb.load_scheduler_settings()
            finally:
                _adb.close()
        except Exception:
            from ads_models import SchedulerSettings
            _settings = SchedulerSettings()

        section_row = ro_start + len(readonly_fields) + 1
        ctk.CTkLabel(form, text="Импорт TData (таймауты)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="gray75").grid(
            row=section_row, column=0, columnspan=2, padx=0, pady=(15, 8), sticky="w")
        section_row += 1

        tdata_int_fields = [
            ("tdata_connect_timeout_seconds",
                "Таймаут client.connect (Шаг 5), сек:",
                _settings.tdata_connect_timeout_seconds),
            ("tdata_get_me_timeout_seconds",
                "Таймаут client.get_me (Шаг 6), сек:",
                _settings.tdata_get_me_timeout_seconds),
            ("tdata_flood_max_wait_seconds",
                "Макс. ожидание FloodWait, сек:",
                _settings.tdata_flood_max_wait_seconds),
            ("tdata_flood_jitter_min_seconds",
                "Джиттер после FloodWait, мин (сек):",
                _settings.tdata_flood_jitter_min_seconds),
            ("tdata_flood_jitter_max_seconds",
                "Джиттер после FloodWait, макс (сек):",
                _settings.tdata_flood_jitter_max_seconds),
        ]

        for key, label, value in tdata_int_fields:
            ctk.CTkLabel(form, text=label, anchor="w").grid(
                row=section_row, column=0, padx=(0, 10), pady=5, sticky="w")
            entry = ctk.CTkEntry(form, width=120)
            entry.grid(row=section_row, column=1, padx=5, pady=5, sticky="w")
            entry.insert(0, str(value))
            self.int_entries[key] = entry
            section_row += 1

        # ─── Секция: Управление устройствами (БД) ──────────────────────
        ctk.CTkLabel(form, text="Управление устройствами (сессиями)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="gray75").grid(
            row=section_row, column=0, columnspan=2, padx=0, pady=(15, 8), sticky="w")
        section_row += 1

        device_int_fields = [
            ("device_terminate_delay_min_seconds",
                "Пауза между удалениями сессий, мин (сек):",
                _settings.device_terminate_delay_min_seconds),
            ("device_terminate_delay_max_seconds",
                "Пауза между удалениями сессий, макс (сек):",
                _settings.device_terminate_delay_max_seconds),
            ("device_terminate_default_schedule_hours",
                "По умолчанию планировать удаление через (часов):",
                _settings.device_terminate_default_schedule_hours),
        ]

        for key, label, value in device_int_fields:
            ctk.CTkLabel(form, text=label, anchor="w").grid(
                row=section_row, column=0, padx=(0, 10), pady=5, sticky="w")
            entry = ctk.CTkEntry(form, width=120)
            entry.grid(row=section_row, column=1, padx=5, pady=5, sticky="w")
            entry.insert(0, str(value))
            self.int_entries[key] = entry
            section_row += 1

        # Кнопка сохранения + статус
        save_frame = ctk.CTkFrame(self, fg_color="transparent")
        save_frame.pack(padx=20, pady=15, fill="x")

        ctk.CTkButton(save_frame, text="Сохранить", width=120, command=self._save).pack(side="left")
        self.status_label = ctk.CTkLabel(save_frame, text="", text_color="#2FA572")
        self.status_label.pack(side="left", padx=15)

    def _save(self):
        cfg = self.app.config

        try:
            cfg.openai_api_key = self.entries["OPENAI_API_KEY"].get().strip()
            cfg.openai_model = self.entries["OPENAI_MODEL"].get().strip() or "gpt-4o-mini"
            cfg.openai_proxy = self.entries["OPENAI_PROXY"].get().strip()
            cfg.groq_api_key = self.entries["GROQ_API_KEY"].get().strip()
            cfg.groq_proxy = self.entries["GROQ_PROXY"].get().strip()
        except ValueError as e:
            self.status_label.configure(text=f"Ошибка: {e}", text_color="#E74C3C")
            return

        # Записать в .env
        env_map = {
            "OPENAI_API_KEY": cfg.openai_api_key,
            "OPENAI_MODEL": cfg.openai_model,
            "OPENAI_PROXY": cfg.openai_proxy,
            "GROQ_API_KEY": cfg.groq_api_key,
            "GROQ_PROXY": cfg.groq_proxy,
        }

        for key, val in env_map.items():
            _update_env_file(key, val)

        # Сохранить числовые настройки в БД (SchedulerSettings)
        try:
            from ads_database import AdsDB
            adb = AdsDB(cfg.db_path)
            try:
                settings = adb.load_scheduler_settings()
                for key, entry in self.int_entries.items():
                    raw = entry.get().strip()
                    if not raw:
                        continue
                    try:
                        value = int(raw)
                    except ValueError:
                        self.status_label.configure(
                            text=f"Ошибка: '{key}' — не целое число",
                            text_color="#E74C3C")
                        return
                    if value < 1:
                        self.status_label.configure(
                            text=f"Ошибка: '{key}' должно быть >= 1",
                            text_color="#E74C3C")
                        return
                    setattr(settings, key, value)
                # Доп. валидация диапазонов
                if settings.tdata_flood_jitter_min_seconds > settings.tdata_flood_jitter_max_seconds:
                    self.status_label.configure(
                        text="Ошибка: джиттер min > max",
                        text_color="#E74C3C")
                    return
                if settings.device_terminate_delay_min_seconds > settings.device_terminate_delay_max_seconds:
                    self.status_label.configure(
                        text="Ошибка: пауза удаления min > max",
                        text_color="#E74C3C")
                    return
                adb.save_scheduler_settings(settings)
            finally:
                adb.close()
        except Exception as e:
            self.status_label.configure(text=f"Ошибка сохранения в БД: {e}",
                                          text_color="#E74C3C")
            return

        self.status_label.configure(text="Сохранено!", text_color="#2FA572")


# --- Главное окно ---

class TeletonApp(ctk.CTk):
    """Главное окно приложения"""

    def __init__(self):
        super().__init__()

        self.title("Teleton")
        self.geometry("1200x750")
        self.minsize(1000, 600)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.config = Config()
        self.log_queue = queue.Queue()
        self._busy_accounts: dict[str, str] = {}

        # === ГЛАВНАЯ ЗАЩИТА ОТ ПАДЕНИЙ ПРИ КЛИКАХ ===
        # Tkinter/CustomTkinter по умолчанию роняет приложение на любой непойманной ошибке в callback'е кнопки/события.
        # Мы перехватываем ВСЁ на уровне root, логируем в файл + пытаемся показать в UI-логе,
        # и позволяем приложению продолжить работу.
        def _gui_crash_handler(exc, val, tb):
            import traceback
            try:
                full_trace = traceback.format_exc()
                log_to_file("gui_crash", full_trace)
                print(f"[GUI CRASH] {exc.__name__}: {val}")
                print("  Полный traceback сохранён в data/logs/gui_crash.log")

                # Пытаемся донести до пользователя через любой доступный лог в UI
                for child in self.winfo_children():
                    try:
                        if hasattr(child, "log") and hasattr(child.log, "append"):
                            child.log.append(f"[CRASH] {exc.__name__}: {val}")
                            child.log.append("Ошибка поймана. Смотри gui_crash.log. Приложение продолжает работу.")
                            break
                    except Exception:
                        pass
            except Exception:
                # Если даже логирование упало — хотя бы в stderr
                print("[GUI CRASH - unlogged]", exc, val)
                traceback.print_exc()

        self.report_callback_exception = _gui_crash_handler

        # Глобальный обработчик Ctrl+V/C/X/A для всех полей ввода
        # (работает независимо от раскладки клавиатуры — по keycode)
        self.bind_all("<Key>", self._global_key_handler)

        # Layout: sidebar + content
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Sidebar — более современный и красивый вид
        self.sidebar = ctk.CTkFrame(self, width=210, corner_radius=0, fg_color=("#0F172A", "#0F172A"))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)

        # Лого с акцентом
        logo = ctk.CTkLabel(self.sidebar, text="TELETON",
                            font=ctk.CTkFont(size=24, weight="bold"),
                            text_color=("#60A5FA", "#93C5FD"))
        logo.pack(padx=20, pady=(25, 15))

        self._nav_label_by_key = {}
        self._help_dialog = None

        ctk.CTkButton(
            self.sidebar,
            text="❓ Справка (F1)",
            anchor="w",
            fg_color="transparent",
            text_color=("gray10", "gray90"),
            hover_color=("gray75", "gray30"),
            height=36,
            corner_radius=8,
            font=ctk.CTkFont(size=13),
            command=self._open_help,
        ).pack(padx=10, pady=(0, 10), fill="x")

        # Кнопки навигации
        self.nav_buttons = {}
        nav_items = [
            ("accounts", "📱 Аккаунты/Прокси"),
            ("list_templates", "📋 Шаблоны чатов/каналов"),
            ("parsing", "🔍 Парсинг"),
            ("audiences", "👥 Аудитории"),
            ("broadcast", "📤 Задачи рассылки"),
            ("channel_commenter", "💬 Комментинг"),
            ("autoreply", "↩️ Автоответчик"),
            ("account_mgr", "👤 Аккаунт"),
            ("ads", "📢 Объявления"),
            ("stats", "📊 Логи/Статистика"),
            ("settings", "⚙️ Настройки"),
        ]

        for key, label in nav_items:
            self._nav_label_by_key[key] = label
            btn = ctk.CTkButton(
                self.sidebar, text=label, anchor="w",
                fg_color="transparent", text_color=("gray10", "gray90"),
                hover_color=("gray75", "gray30"),
                height=40, corner_radius=8,
                font=ctk.CTkFont(size=14),
                command=lambda k=key: self._show_frame(k),
            )
            btn.pack(padx=10, pady=2, fill="x")
            self.nav_buttons[key] = btn

        # Content area — лёгкий акцент, чтобы отделялось от сайдбара
        self.content = ctk.CTkFrame(self, fg_color=("#0B1120", "#0B1120"))
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        # Фреймы секций (lazy — создаются при первом переходе)
        self.frames = {}
        self._frame_classes = {
            "accounts": AccountsFrame,
            "list_templates": ListTemplatesFrame,
            "parsing": ParsingFrame,
            "audiences": AudiencesFrame,
            "broadcast": BroadcastFrame,
            "channel_commenter": ChannelCommenterFrame,
            "autoreply": AutoReplyFrame,
            "account_mgr": AccountManagementFrame,
            "ads": __import__("ads_gui").AdsMainFrame,
            "stats": StatsFrame,
            "settings": SettingsFrame,
        }

        self.active_frame = None
        self._show_frame("accounts")
        try:
            self.bind_all("<F1>", lambda _e: self._open_help())
        except Exception:
            pass

        # Polling очереди
        self._poll_queue()

        # Авто-возобновление циклов при открытии приложения:
        # чтобы рассылка работала "та же самая", как до закрытия GUI.
        # Enabled кампании стартуют автоматически.
        self.after(2500, self._auto_resume_cycles)

        # Авто-возобновление рассылок (broadcast задач) если были pending
        self.after(3500, self._auto_resume_broadcasts)

        # Чистка старой истории расписаний (устаревшие done/failed >30 дней)
        try:
            from ads_database import AdsDB
            _adb = AdsDB(self.config.db_path)
            try:
                _adb.cleanup_old_device_terminations(days_old=30)
            finally:
                _adb.close()
        except Exception as _e:
            log_to_file("startup",
                        f"[!] cleanup_old_device_terminations failed: {_e}")

        # Запуск таймера проверки расписания удаления сессий.
        # Тикает раз в 60с, проверяет pending_device_terminations,
        # выполняет просроченные. Первый тик — через 5с после старта
        # (чтобы успели мигрировать БД и проверить просроченные после
        # длительного простоя GUI).
        self.after(5000, self._check_pending_device_terminations)

    def _global_key_handler(self, event):
        """
        Глобальная обработка Ctrl+V/C/X/A для CTkEntry и CTkTextbox.
        Работает по keycode — не зависит от раскладки (RU/EN).
        keycodes: V=86, C=67, X=88, A=65

        ВАЖНО: на английской раскладке Tk сам генерирует <<Paste>>/<<Copy>>/...,
        и нативный обработчик уже вставил буфер. Если мы тоже вставим — будет
        двойная вставка. Поэтому при keysym in (v,c,x,a) — НИЧЕГО НЕ ДЕЛАЕМ,
        нативный сделал работу. Только при русской раскладке (keysym=Cyrillic_em
        и т.п.) мы вставляем сами по keycode.
        """
        if not (event.state & 0x4):  # Ctrl не зажат
            return

        # ENG раскладка: нативный <<Paste>>/<<Copy>>/... уже сработал — выходим
        if event.keysym.lower() in ("v", "c", "x", "a"):
            return

        widget = self.focus_get()
        if widget is None:
            return

        # CTkTextbox — используем нативные события tk.Text
        if isinstance(widget, tk.Text):
            if event.keycode == 86:   # Ctrl+V
                try:
                    widget.insert("insert", self.clipboard_get())
                except Exception:
                    pass
                return "break"
            elif event.keycode == 67:  # Ctrl+C
                try:
                    if widget.tag_ranges("sel"):
                        self.clipboard_clear()
                        self.clipboard_append(widget.get("sel.first", "sel.last"))
                except Exception:
                    pass
                return "break"
            elif event.keycode == 88:  # Ctrl+X
                try:
                    if widget.tag_ranges("sel"):
                        self.clipboard_clear()
                        self.clipboard_append(widget.get("sel.first", "sel.last"))
                        widget.delete("sel.first", "sel.last")
                except Exception:
                    pass
                return "break"
            elif event.keycode == 65:  # Ctrl+A
                widget.tag_add("sel", "1.0", "end")
                return "break"

        # CTkEntry — внутренний tk.Entry
        if isinstance(widget, tk.Entry):
            if event.keycode == 86:   # Ctrl+V
                try:
                    text = self.clipboard_get()
                    try:
                        if widget.select_present():
                            widget.delete("sel.first", "sel.last")
                    except Exception:
                        pass
                    widget.insert("insert", text)
                except Exception:
                    pass
                return "break"
            elif event.keycode == 67:  # Ctrl+C
                try:
                    if widget.select_present():
                        self.clipboard_clear()
                        self.clipboard_append(widget.selection_get())
                except Exception:
                    pass
                return "break"
            elif event.keycode == 88:  # Ctrl+X
                try:
                    if widget.select_present():
                        self.clipboard_clear()
                        self.clipboard_append(widget.selection_get())
                        widget.delete("sel.first", "sel.last")
                except Exception:
                    pass
                return "break"
            elif event.keycode == 65:  # Ctrl+A
                widget.select_range(0, "end")
                widget.icursor("end")
                return "break"

    def _show_frame(self, key: str):
        _log_action("gui", f"switch_to_frame:{key}")
        # Обновить подсветку sidebar — более заметный акцент
        for k, btn in self.nav_buttons.items():
            if k == key:
                btn.configure(
                    fg_color=("#2563EB", "#1E40AF"),  # яркий синий акцент
                    text_color=("white", "white"),
                    font=ctk.CTkFont(size=14, weight="bold")
                )
            else:
                btn.configure(
                    fg_color="transparent",
                    text_color=("gray10", "gray90"),
                    font=ctk.CTkFont(size=14)
                )

        # Lazy-создание фрейма при первом обращении
        if key not in self.frames:
            frame = self._frame_classes[key](self.content, self)
            if not hasattr(frame, "help_key"):
                try:
                    frame.help_key = key
                except Exception:
                    pass
            frame.grid(row=0, column=0, sticky="nsew")
            self.frames[key] = frame

        # Показать фрейм
        frame = self.frames[key]
        frame.tkraise()
        self.active_frame = key

        # Callback при показе
        if hasattr(frame, "on_show"):
            frame.on_show()

    def _open_help(self):
        key = self.active_frame or "accounts"
        label = self._nav_label_by_key.get(key, key)
        text = HELP_TEXTS.get(key, "Справка для этого раздела пока не добавлена.")
        title = f"Справка — {label}"
        try:
            if self._help_dialog is not None and self._help_dialog.winfo_exists():
                self._help_dialog.focus()
                return
        except Exception:
            pass
        try:
            self._help_dialog = HelpDialog(self, title, text)
        except Exception:
            self._help_dialog = None

    def _poll_queue(self):
        """Чтение лог-сообщений из фоновых потоков"""
        processed = 0
        max_batch = 200
        try:
            while processed < max_batch:
                tag, msg = self.log_queue.get_nowait()
                processed += 1

                # Роутинг по тегу — обёрнут в try, чтобы один плохой update не убил поллинг и не вызвал визуальные глюки
                try:
                    if tag.startswith("accounts"):
                        self.frames["accounts"].on_queue_message(tag, msg)
                    elif tag.startswith("parsing") or tag.startswith("smart_parsing"):
                        self.frames["parsing"].on_queue_message(tag, msg)
                    elif tag.startswith("audiences"):
                        self.frames["audiences"].on_queue_message(tag, msg)
                    elif tag.startswith("broadcast") or tag.startswith("mention") or tag.startswith("cycle") or tag == "check_done":
                        self.frames["broadcast"].on_queue_message(tag, msg)
                    elif tag.startswith("channel"):
                        if "channel_commenter" in self.frames:
                            self.frames["channel_commenter"].on_queue_message(tag, msg)
                    elif tag.startswith("autoreply"):
                        if "autoreply" in self.frames:
                            self.frames["autoreply"].on_queue_message(tag, msg)
                    elif tag.startswith("acct_mgr"):
                        if "account_mgr" in self.frames:
                            self.frames["account_mgr"].on_queue_message(tag, msg)
                except Exception:
                    # Не даём UI-обновлению из потока сломать главный цикл
                    pass

        except queue.Empty:
            pass
        except Exception:
            # Не даём ошибке в обработке очереди убить поллинг
            pass

        delay = 40 if processed >= max_batch else 120   # чуть мягче, меньше дёрганья UI
        self.after(delay, self._poll_queue)

    def _auto_resume_cycles(self):
        """Авто-возобновление при открытии приложения.
        Если были enabled кампании — стартуем их runner, чтобы рассылка работала
        "та же самая", как до закрытия GUI.
        """
        try:
            if "broadcast" in getattr(self, "frames", {}):
                bf = self.frames["broadcast"]
                if hasattr(bf, "_cycle_start_enabled_campaigns"):
                    bf._cycle_start_enabled_campaigns()
        except Exception:
            pass

    def _auto_resume_broadcasts(self):
        """Если при закрытии были pending broadcast-задачи — на старте приложения
        автоматически продолжаем ту же самую рассылку (не теряем очередь).
        """
        try:
            if "broadcast" not in getattr(self, "frames", {}):
                return
            bf = self.frames["broadcast"]
            if getattr(bf, "_running", False):
                return  # уже идёт

            db = Database(self.config.db_path)
            pending = [t for t in db.get_all_tasks() if getattr(t, "status", "") in ("pending", "waiting")]
            db.close()

            if pending:
                try:
                    bf.log.append(f"[i] Найдены pending задачи ({len(pending)}) — продолжаем ту же рассылку...")
                except Exception:
                    pass
                bf._start_broadcast()
        except Exception:
            pass

    def _check_pending_device_terminations(self):
        """Тик расписания удалений сессий. Вызывается раз в 60с пока GUI запущен.

        Стратегия: вариант В + Tk-таймер. Не отдельный поток.
        Каждый тик:
          1. Читает БД, забирает pending-задачи у которых scheduled_at <= now.
          2. Для каждой запускает фоновый поток (terminate_thread), который
             подключается к аккаунту и убивает указанные сессии.
          3. Помечает задачу done/failed.

        Если GUI закрыт в момент scheduled_at — задача выполнится при
        следующем запуске GUI задним числом.
        """
        try:
            from datetime import datetime
            from ads_database import AdsDB
            adb = AdsDB(self.config.db_path)
            try:
                due = adb.get_due_device_terminations(
                    datetime.now().isoformat(timespec="seconds"))
                settings = adb.load_scheduler_settings()
            finally:
                adb.close()

            for task in due:
                self._execute_pending_termination(task, settings)
        except Exception as e:
            log_to_file("startup",
                        f"[!] _check_pending_device_terminations failed: {e}")

        # Перепланируем тик
        self.after(60000, self._check_pending_device_terminations)

    def _refresh_accounts_busy_view(self):
        try:
            frame = getattr(self, "frames", {}).get("accounts")
            if frame and hasattr(frame, "refresh"):
                try:
                    frame._last_refresh_ts = 0
                except Exception:
                    pass
                frame.refresh()
        except Exception:
            pass

    def mark_account_busy(self, phones: str | list[str], context: str):
        if isinstance(phones, str):
            phones = [phones]
        changed = False
        for phone in phones or []:
            p = (phone or "").strip()
            if not p:
                continue
            ctx = (context or "").strip() or "занят"
            if self._busy_accounts.get(p) != ctx:
                self._busy_accounts[p] = ctx
                changed = True
        if changed:
            self._refresh_accounts_busy_view()

    def mark_account_free(self, phones: str | list[str]):
        if isinstance(phones, str):
            phones = [phones]
        changed = False
        for phone in phones or []:
            p = (phone or "").strip()
            if p in self._busy_accounts:
                self._busy_accounts.pop(p, None)
                changed = True
        if changed:
            self._refresh_accounts_busy_view()

    def get_busy_accounts(self) -> dict[str, str]:
        return dict(self._busy_accounts)

    def _execute_pending_termination(self, task: dict, settings):
        """Выполнить одну запланированную задачу удаления сессий в фоне."""
        log_queue = self.log_queue
        cfg = self.config

        def terminate_thread():
            _thread_local.log_handler = lambda m: log_queue.put(("accounts_log", m))
            _thread_local.log_tag = "accounts"
            try:
                from sender import TelegramSender
                from account_manager import terminate_specific_sessions
                from ads_database import AdsDB

                _db = Database(cfg.db_path)
                try:
                    account = next((a for a in _db.get_all_accounts()
                                    if a.phone == task["account_phone"]), None)
                finally:
                    _db.close()

                if account is None:
                    err = f"Аккаунт {task['account_phone']} не найден в БД"
                    print(f"[-] Расписание #{task['id']}: {err}")
                    _adb = AdsDB(cfg.db_path)
                    try:
                        _adb.mark_device_termination_failed(task["id"], err)
                    finally:
                        _adb.close()
                    return

                _db = Database(cfg.db_path)
                sender = TelegramSender(account, cfg, _db)

                async def do_kill():
                    if not await sender.connect():
                        return None
                    try:
                        return await terminate_specific_sessions(
                            sender.client, task["auth_hashes"],
                            delay_min_seconds=settings.device_terminate_delay_min_seconds,
                            delay_max_seconds=settings.device_terminate_delay_max_seconds,
                        )
                    finally:
                        await sender.disconnect()

                loop = asyncio.new_event_loop()
                try:
                    res = loop.run_until_complete(do_kill())
                finally:
                    loop.close()
                _db.close()

                _adb = AdsDB(cfg.db_path)
                try:
                    if res is None:
                        _adb.mark_device_termination_failed(
                            task["id"], "connect failed")
                        print(f"[-] Расписание #{task['id']}: connect failed")
                    else:
                        _adb.mark_device_termination_done(task["id"])
                        print(f"[+] Расписание #{task['id']} выполнено: "
                              f"убито {res['killed']}, пропущено {res['skipped']}")
                finally:
                    _adb.close()

            except Exception as e:
                log_exception("accounts", e,
                              context=f"Pending termination #{task['id']}")
                err = f"{type(e).__name__}: {e}"
                print(f"[-] Расписание #{task['id']} провалено: {err}")
                try:
                    _adb = AdsDB(cfg.db_path)
                    try:
                        _adb.mark_device_termination_failed(task["id"], err)
                    finally:
                        _adb.close()
                except Exception:
                    pass
            finally:
                _thread_local.log_handler = None

        threading.Thread(target=terminate_thread, daemon=True).start()


# --- Точка входа ---

if __name__ == "__main__":
    import sys as _sys
    log_to_file("startup", f"=== Teleton started, Python {_sys.version.split()[0]} ===")
    try:
        app = TeletonApp()
        app.mainloop()
    finally:
        log_to_file("shutdown", "=== Teleton exiting ===")
        try:
            import file_logger as _fl
            _fl.close()
        except Exception:
            pass
