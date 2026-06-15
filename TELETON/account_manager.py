"""
account_manager.py — управление профилем аккаунта и очистка.

Все функции принимают уже подключённый TelegramClient.
progress_cb(str) — коллбэк для логирования в GUI.
"""

import asyncio
import random
import os
import shutil
from typing import Callable, Optional

from telethon import TelegramClient
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.account import (
    UpdateProfileRequest,
    GetAuthorizationsRequest,
    ResetAuthorizationRequest,
)
from telethon.tl.functions.photos import UploadProfilePhotoRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteHistoryRequest
from telethon.errors import FloodWaitError, RPCError
try:
    from telethon.errors.rpcerrorlist import (
        FirstNameInvalidError,
        AboutTooLongError,
        PhotoCropSizeSmallError,
        PhotoExtInvalidError,
        ImageProcessFailedError,
        FrozenMethodInvalidError,
    )
except Exception:
    FirstNameInvalidError = type("FirstNameInvalidError", (Exception,), {})
    AboutTooLongError = type("AboutTooLongError", (Exception,), {})
    PhotoCropSizeSmallError = type("PhotoCropSizeSmallError", (Exception,), {})
    PhotoExtInvalidError = type("PhotoExtInvalidError", (Exception,), {})
    ImageProcessFailedError = type("ImageProcessFailedError", (Exception,), {})
    FrozenMethodInvalidError = type("FrozenMethodInvalidError", (Exception,), {})


# --- Профиль ---

async def change_profile(
    client: TelegramClient,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    bio: Optional[str] = None,
    avatar_path: Optional[str] = None,
    avatar_dir: Optional[str] = None,
    avatar_mark_used: bool = False,
    progress_cb: Callable[[str], None] = print,
    dry_run: bool = False,
) -> bool:
    """
    Изменить профиль аккаунта.
    Передавай только те поля, которые нужно изменить (None — не трогать).
    avatar_path — путь к файлу изображения (.jpg / .jpeg / .png).
    avatar_dir — папка с изображениями (.jpg / .jpeg / .png). Если задана,
                 берётся случайное фото из папки (с перебором при ошибках).
    avatar_mark_used — если True и задан avatar_dir: после успешной установки
                       перемещает файл в подпапку "used".
    Возвращает True при успехе.
    """
    try:
        if dry_run:
            progress_cb("  [DRY] Профиль НЕ изменяется, только превью действий")
            if first_name is not None:
                progress_cb(f"  [DRY] Имя -> {first_name}")
            if last_name is not None:
                progress_cb(f"  [DRY] Фамилия -> {last_name}")
            if bio is not None:
                progress_cb(f"  [DRY] Bio -> {bio[:120]}")
            if avatar_path:
                progress_cb(f"  [DRY] Аватар -> файл {avatar_path}")
            if avatar_dir:
                try:
                    exts = (".jpg", ".jpeg", ".png")
                    candidates = [
                        os.path.join(avatar_dir, name)
                        for name in os.listdir(avatar_dir)
                        if name.lower().endswith(exts)
                    ]
                    progress_cb(f"  [DRY] Аватар -> случайный файл из папки {avatar_dir} ({len(candidates)} шт.)")
                except Exception as e:
                    progress_cb(f"  [DRY] Не удалось прочитать папку аватаров: {type(e).__name__}: {e}")
            if avatar_mark_used and avatar_dir:
                progress_cb("  [DRY] Пометка used НЕ выполняется")
            return True

        async def _get_full_about() -> str:
            try:
                me = await client.get_me()
                if not me:
                    return ""
                full = await client(GetFullUserRequest(me))
                about = getattr(getattr(full, "full_user", None), "about", "") or ""
                return str(about)
            except Exception:
                return ""

        def _fmt_err(e: Exception) -> str:
            try:
                s = str(e)
            except Exception:
                s = ""
            if s:
                return f"{type(e).__name__}: {s}"
            return f"{type(e).__name__}"

        def _human_hint(e: Exception, stage: str) -> str:
            tn = type(e).__name__
            if isinstance(e, FloodWaitError):
                return f"Подожди {getattr(e, 'seconds', 0)}с и попробуй снова."
            if isinstance(e, FrozenMethodInvalidError) or tn == "FrozenMethodInvalidError":
                return ("Аккаунт заморожен/ограничен (frozen). Telegram запрещает менять профиль. "
                        "Открой официальный Telegram и пройди разморозку/подтверждения, "
                        "проверь ограничения у @SpamBot.")
            if isinstance(e, FirstNameInvalidError) or tn == "FirstNameInvalidError":
                return "Имя некорректно: попробуй без спецсимволов/эмодзи, не пустое."
            if isinstance(e, AboutTooLongError) or tn == "AboutTooLongError":
                return "Bio слишком длинное: сократи текст."
            if tn in ("PhotoCropSizeSmallError", "PhotoExtInvalidError", "ImageProcessFailedError"):
                return "Картинка не подходит: попробуй другой файл (обычный .jpg/.png), без CMYK/битых данных."
            if isinstance(e, RPCError):
                if stage == "profile":
                    return "Telegram отклонил изменение имени/bio. Проверь ограничения аккаунта и корректность текста."
                if stage == "avatar":
                    return "Telegram отклонил изменение аватара. Проверь ограничения аккаунта и файл изображения."
            return ""

        before_me = None
        before_about = ""
        try:
            before_me = await client.get_me()
            before_about = await _get_full_about()
        except Exception:
            before_me = None
            before_about = ""

        # Обновить имя / фамилию / bio
        kwargs = {}
        if first_name is not None:
            kwargs["first_name"] = first_name
        if last_name is not None:
            kwargs["last_name"] = last_name
        if bio is not None:
            kwargs["about"] = bio
        want_profile_update = bool(kwargs)
        want_avatar_update = bool(avatar_path or avatar_dir)
        profile_ok = False
        avatar_ok = False
        avatar_blocked = False

        if want_profile_update:
            try:
                await client(UpdateProfileRequest(**kwargs))
                after_me = None
                after_about = ""
                try:
                    after_me = await client.get_me()
                    after_about = await _get_full_about()
                except Exception:
                    after_me = None
                    after_about = ""

                progress_cb("  [+] Имя/Bio обновлены")
                profile_ok = True
                if before_me and after_me:
                    bfn = getattr(before_me, "first_name", "") or ""
                    bln = getattr(before_me, "last_name", "") or ""
                    afn = getattr(after_me, "first_name", "") or ""
                    aln = getattr(after_me, "last_name", "") or ""
                    if (bfn, bln) != (afn, aln):
                        progress_cb(f"  [=] Имя: '{bfn} {bln}'.strip() -> '{afn} {aln}'.strip()")
                    else:
                        progress_cb(f"  [=] Имя: '{afn} {aln}'.strip() (без изменений)")
                if bio is not None:
                    if before_about != after_about and after_about:
                        progress_cb("  [=] Bio: обновлено")
                    elif after_about:
                        progress_cb("  [=] Bio: без изменений")
                    else:
                        progress_cb("  [=] Bio: обновлено (проверка недоступна)")
            except FloodWaitError as e:
                progress_cb(f"  [-] Telegram не дал изменить имя/bio: {_fmt_err(e)}")
                hint = _human_hint(e, "profile")
                if hint:
                    progress_cb(f"  [>] {hint}")
                return False
            except (FrozenMethodInvalidError, FirstNameInvalidError, AboutTooLongError, RPCError) as e:
                progress_cb(f"  [-] Telegram не дал изменить имя/bio: {_fmt_err(e)}")
                hint = _human_hint(e, "profile")
                if hint:
                    progress_cb(f"  [>] {hint}")

        # Загрузить аватар
        candidates = []
        if avatar_path:
            if os.path.isfile(avatar_path):
                candidates = [avatar_path]
            else:
                progress_cb(f"  [-] Файл аватара не найден: {avatar_path}")
                return False
        elif avatar_dir:
            try:
                exts = (".jpg", ".jpeg", ".png")
                for name in os.listdir(avatar_dir):
                    if name.lower().endswith(exts):
                        candidates.append(os.path.join(avatar_dir, name))
            except Exception as e:
                progress_cb(f"  [-] Ошибка чтения папки аватаров: {_fmt_err(e)}")
                candidates = []

            random.shuffle(candidates)
            if candidates:
                progress_cb(f"  [~] Аватаров найдено: {len(candidates)}")
            else:
                progress_cb("  [-] В папке нет подходящих изображений (.jpg/.jpeg/.png)")
                return False

        used_path = ""
        if candidates:
            uploaded = False
            for p in candidates:
                try:
                    file = await client.upload_file(p)
                    await client(UploadProfilePhotoRequest(file=file))
                    progress_cb(f"  [+] Аватар загружен: {p}")
                    uploaded = True
                    avatar_ok = True
                    used_path = p
                    break
                except FloodWaitError as e:
                    progress_cb(f"  [!] FloodWait {e.seconds}s при загрузке аватара — попробуй позже")
                    return False
                except FrozenMethodInvalidError as e:
                    avatar_blocked = True
                    progress_cb(
                        "  [-] Telegram запретил менять аватар: аккаунт заморожен/ограничен"
                    )
                    progress_cb(
                        f"  [-] Детали: {_fmt_err(e)} (UploadProfilePhotoRequest)"
                    )
                    hint = _human_hint(e, "avatar")
                    if hint:
                        progress_cb(f"  [>] {hint}")
                    break
                except (PhotoCropSizeSmallError, PhotoExtInvalidError, ImageProcessFailedError) as e:
                    progress_cb(f"  [!] Нельзя поставить аватар '{p}': {_fmt_err(e)}")
                    hint = _human_hint(e, "avatar")
                    if hint:
                        progress_cb(f"  [>] {hint}")
                except Exception as e:
                    progress_cb(f"  [!] Не удалось поставить аватар '{p}': {_fmt_err(e)}")
                    hint = _human_hint(e, "avatar")
                    if hint:
                        progress_cb(f"  [>] {hint}")

            if not uploaded:
                progress_cb("  [-] Не удалось поставить ни один аватар из выбранных")
                if not profile_ok:
                    return False

        if avatar_dir and avatar_mark_used and used_path:
            try:
                used_dir = os.path.join(avatar_dir, "used")
                os.makedirs(used_dir, exist_ok=True)
                dst = os.path.join(used_dir, os.path.basename(used_path))
                if os.path.abspath(used_path) != os.path.abspath(dst):
                    shutil.move(used_path, dst)
                    progress_cb(f"  [~] Перемещено в used: {dst}")
            except Exception as e:
                progress_cb(f"  [!] Не удалось пометить аватар как использованный: {_fmt_err(e)}")

        if profile_ok and want_avatar_update and not avatar_ok:
            if avatar_blocked:
                progress_cb("  [!] Имя/Bio могли обновиться, но аватар изменить нельзя (frozen)")
            else:
                progress_cb("  [!] Имя/Bio обновлены, но аватар не обновился")

        return bool(profile_ok or avatar_ok)

    except FloodWaitError as e:
        progress_cb(f"  [!] FloodWait {e.seconds}s — попробуй позже")
        return False
    except (FirstNameInvalidError, AboutTooLongError, RPCError) as e:
        hint = ""
        if isinstance(e, FirstNameInvalidError):
            hint = " (имя некорректно — попробуй без спецсимволов, не пустое)"
        elif isinstance(e, AboutTooLongError):
            hint = " (bio слишком длинное — сократи текст)"
        progress_cb(f"  [-] Telegram не дал изменить профиль: {_fmt_err(e)}{hint}")
        return False
    except Exception as e:
        progress_cb(f"  [-] Ошибка изменения профиля: {_fmt_err(e)}")
        return False


# --- Очистка ---

async def leave_groups(
    client: TelegramClient,
    progress_cb: Callable[[str], None] = print,
    dry_run: bool = False,
) -> int:
    """
    Выйти из всех групп (мегагрупп и обычных чатов).
    Возвращает количество покинутых групп.
    """
    count = 0
    try:
        dialogs = await client.get_dialogs()
        targets = [d for d in dialogs if d.is_group]
        progress_cb(f"  [~] Найдено групп: {len(targets)}")

        if dry_run:
            for d in targets:
                progress_cb(f"  [DRY] Вышел бы из группы: {d.name}")
            progress_cb(f"  [=] DRY-RUN: действий не выполнено, групп в превью: {len(targets)}")
            return 0

        for d in targets:
            try:
                await client(LeaveChannelRequest(d.entity))
                progress_cb(f"  [+] Покинул группу: {d.name}")
                count += 1
                delay = random.uniform(15.0, 45.0)
                progress_cb(f"  [~] Пауза {delay:.0f}с...")
                await asyncio.sleep(delay)
            except FloodWaitError as e:
                progress_cb(f"  [!] FloodWait {e.seconds}s — пауза...")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                progress_cb(f"  [-] Ошибка выхода из {d.name}: {e}")

    except Exception as e:
        progress_cb(f"  [-] Ошибка получения диалогов: {e}")

    progress_cb(f"  [=] Покинуто групп: {count}")
    return count


async def leave_channels(
    client: TelegramClient,
    progress_cb: Callable[[str], None] = print,
    dry_run: bool = False,
) -> int:
    """
    Выйти из всех каналов.
    Возвращает количество покинутых каналов.
    """
    count = 0
    try:
        dialogs = await client.get_dialogs()
        targets = [d for d in dialogs if d.is_channel and not d.is_group]
        progress_cb(f"  [~] Найдено каналов: {len(targets)}")

        if dry_run:
            for d in targets:
                progress_cb(f"  [DRY] Вышел бы из канала: {d.name}")
            progress_cb(f"  [=] DRY-RUN: действий не выполнено, каналов в превью: {len(targets)}")
            return 0

        for d in targets:
            try:
                await client(LeaveChannelRequest(d.entity))
                progress_cb(f"  [+] Покинул канал: {d.name}")
                count += 1
                delay = random.uniform(15.0, 45.0)
                progress_cb(f"  [~] Пауза {delay:.0f}с...")
                await asyncio.sleep(delay)
            except FloodWaitError as e:
                progress_cb(f"  [!] FloodWait {e.seconds}s — пауза...")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                progress_cb(f"  [-] Ошибка выхода из {d.name}: {e}")

    except Exception as e:
        progress_cb(f"  [-] Ошибка получения диалогов: {e}")

    progress_cb(f"  [=] Покинуто каналов: {count}")
    return count


async def delete_dialogs(
    client: TelegramClient,
    progress_cb: Callable[[str], None] = print,
    dry_run: bool = False,
) -> int:
    """
    Удалить все личные диалоги (переписки с людьми).
    Возвращает количество удалённых диалогов.
    """
    count = 0
    try:
        dialogs = await client.get_dialogs()
        targets = [d for d in dialogs if d.is_user and not getattr(d.entity, "bot", False)]
        progress_cb(f"  [~] Найдено диалогов: {len(targets)}")

        if dry_run:
            for d in targets:
                progress_cb(f"  [DRY] Удалил бы диалог: {d.name}")
            progress_cb(f"  [=] DRY-RUN: действий не выполнено, диалогов в превью: {len(targets)}")
            return 0

        for d in targets:
            try:
                await client(DeleteHistoryRequest(
                    peer=d.entity,
                    max_id=0,
                    just_clear=False,
                    revoke=False,
                ))
                progress_cb(f"  [+] Удалён диалог: {d.name}")
                count += 1
                delay = random.uniform(8.0, 20.0)
                progress_cb(f"  [~] Пауза {delay:.0f}с...")
                await asyncio.sleep(delay)
            except FloodWaitError as e:
                progress_cb(f"  [!] FloodWait {e.seconds}s — пауза...")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                progress_cb(f"  [-] Ошибка удаления диалога {d.name}: {e}")

    except Exception as e:
        progress_cb(f"  [-] Ошибка получения диалогов: {e}")

    progress_cb(f"  [=] Удалено диалогов: {count}")
    return count


async def delete_bots(
    client: TelegramClient,
    progress_cb: Callable[[str], None] = print,
    dry_run: bool = False,
) -> int:
    """
    Удалить все диалоги с ботами.
    Возвращает количество удалённых диалогов с ботами.
    """
    count = 0
    try:
        dialogs = await client.get_dialogs()
        targets = [d for d in dialogs if d.is_user and getattr(d.entity, "bot", False)]
        progress_cb(f"  [~] Найдено ботов: {len(targets)}")

        if dry_run:
            for d in targets:
                progress_cb(f"  [DRY] Удалил бы бота: {d.name}")
            progress_cb(f"  [=] DRY-RUN: действий не выполнено, ботов в превью: {len(targets)}")
            return 0

        for d in targets:
            try:
                await client(DeleteHistoryRequest(
                    peer=d.entity,
                    max_id=0,
                    just_clear=False,
                    revoke=False,
                ))
                progress_cb(f"  [+] Удалён бот: {d.name}")
                count += 1
                delay = random.uniform(8.0, 20.0)
                progress_cb(f"  [~] Пауза {delay:.0f}с...")
                await asyncio.sleep(delay)
            except FloodWaitError as e:
                progress_cb(f"  [!] FloodWait {e.seconds}s — пауза...")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                progress_cb(f"  [-] Ошибка удаления бота {d.name}: {e}")

    except Exception as e:
        progress_cb(f"  [-] Ошибка получения диалогов: {e}")

    progress_cb(f"  [=] Удалено ботов: {count}")
    return count


# --- Управление сессиями (для tdata-аккаунтов) ---

async def list_sessions(
    client: TelegramClient,
    progress_cb: Callable[[str], None] = print,
) -> list:
    """
    Получить список всех активных сессий аккаунта.
    Возвращает список Authorization-объектов Telethon (или пустой список при ошибке).
    """
    try:
        res = await client(GetAuthorizationsRequest())
        auths = res.authorizations
        progress_cb(f"  [~] Активных сессий на аккаунте: {len(auths)}")
        for a in auths:
            marker = "← ЭТА" if a.current else "     "
            progress_cb(
                f"  {marker} hash={a.hash} | {a.device_model} | "
                f"{a.platform} {a.system_version} | "
                f"app={a.app_name} {a.app_version} | "
                f"ip={a.ip} ({a.country}) | "
                f"active={a.date_active}"
            )
        return auths
    except FloodWaitError as e:
        progress_cb(f"  [!] FloodWait {e.seconds}s при получении сессий")
        return []
    except Exception as e:
        progress_cb(f"  [-] Не удалось получить сессии: {type(e).__name__}: {e}")
        return []


async def terminate_other_sessions(
    client: TelegramClient,
    progress_cb: Callable[[str], None] = print,
    dry_run: bool = False,
) -> int:
    """
    Завершить ВСЕ сессии аккаунта кроме текущей.

    dry_run=True — только показать список, не убивать.

    Возвращает количество реально убитых сессий.

    Примечание: Telegram запрещает ResetAuthorizationRequest для сессий
    младше 24 часов (вернёт FreshResetAuthorisationForbiddenError).
    Это нормально — конкретная сессия просто пропускается.
    """
    try:
        res = await client(GetAuthorizationsRequest())
        others = [a for a in res.authorizations if not a.current]
        progress_cb(f"  [~] Чужих сессий найдено: {len(others)}")

        if not others:
            return 0

        if dry_run:
            progress_cb(f"  [~] dry_run=True — сессии НЕ убиваем, только показали")
            return 0

        killed = 0
        for a in others:
            try:
                await client(ResetAuthorizationRequest(hash=a.hash))
                progress_cb(
                    f"  [+] Убита: {a.device_model} / {a.ip} ({a.country})"
                )
                killed += 1
                delay = random.uniform(1.0, 2.5)
                await asyncio.sleep(delay)
            except FloodWaitError as e:
                progress_cb(f"  [!] FloodWait {e.seconds}s — прерываю зачистку")
                await asyncio.sleep(e.seconds)
                break
            except Exception as e:
                # FreshResetAuthorisationForbiddenError, HashInvalid и т.п.
                progress_cb(
                    f"  [-] Не убил hash={a.hash} "
                    f"({a.device_model}): {type(e).__name__}"
                )

        progress_cb(f"  [=] Убито чужих сессий: {killed}/{len(others)}")
        return killed

    except Exception as e:
        progress_cb(f"  [-] Ошибка зачистки сессий: {type(e).__name__}: {e}")
        return 0


async def terminate_specific_sessions(
    client: TelegramClient,
    auth_hashes: list,
    progress_cb: Callable[[str], None] = print,
    delay_min_seconds: float = 1.0,
    delay_max_seconds: float = 3.0,
) -> dict:
    """
    Завершить КОНКРЕТНЫЕ сессии по списку их hash'ей.

    auth_hashes — список int (Authorization.hash) из Telethon.
    delay_min_seconds / delay_max_seconds — диапазон рандомной паузы между
    последовательными ResetAuthorizationRequest.

    Возвращает dict: {"killed": int, "skipped": int, "errors": list[str]}.

    Telegram запрещает ResetAuthorizationRequest для сессий <24ч —
    такие сессии попадут в skipped с пометкой FreshResetForbidden.
    """
    result = {"killed": 0, "skipped": 0, "errors": []}
    if not auth_hashes:
        progress_cb("  [~] Пустой список hash'ей — нечего удалять")
        return result

    progress_cb(f"  [~] Запрошено удалить сессий: {len(auth_hashes)}")
    for h in auth_hashes:
        try:
            await client(ResetAuthorizationRequest(hash=h))
            progress_cb(f"  [+] Убита сессия hash={h}")
            result["killed"] += 1
            if delay_max_seconds > 0:
                lo = max(0.0, float(delay_min_seconds))
                hi = max(lo, float(delay_max_seconds))
                await asyncio.sleep(random.uniform(lo, hi))
        except FloodWaitError as e:
            progress_cb(f"  [!] FloodWait {e.seconds}s — прерываю зачистку")
            result["errors"].append(f"FloodWait {e.seconds}s")
            await asyncio.sleep(e.seconds)
            break
        except Exception as e:
            err_name = type(e).__name__
            progress_cb(f"  [-] Не убил hash={h}: {err_name}")
            result["skipped"] += 1
            result["errors"].append(f"hash={h}: {err_name}")

    progress_cb(f"  [=] Итог: убито {result['killed']}, "
                f"пропущено {result['skipped']}")
    return result
