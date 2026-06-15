import argparse
import asyncio
from datetime import datetime, timedelta
import random

from config import Config
from database import Database
from models import Task, SendLog
from sender import TelegramSender
from parser import GroupParser
from mentioner import Mentioner
from spintax import spin_text


def make_config(args) -> Config:
    """Создать конфиг"""
    return Config()


# --- Команда: parse ---

async def cmd_parse(args):
    """Парсинг участников группы"""
    cfg = make_config(args)
    db = Database(cfg.db_path)

    accounts = db.get_active_accounts()
    if args.account:
        accounts = [a for a in accounts if a.phone == args.account]

    if not accounts:
        print("[!] Нет доступных аккаунтов для парсинга")
        return

    acc = accounts[0]
    sender = TelegramSender(acc, cfg, db)

    if not await sender.connect():
        return

    try:
        parser = GroupParser(sender.client, db)

        if args.commenters:
            count = await parser.parse_commenters(args.group, limit_posts=args.limit_posts or 50)
        else:
            count = await parser.parse_group(args.group, aggressive=args.aggressive)

        print(f"\n=== Итого: {count} пользователей сохранено ===")
    finally:
        await sender.disconnect()

    db.close()


# --- Команда: smart-parse ---

async def cmd_smart_parse(args):
    """Смарт-парсинг: поиск постов по ключевым словам или через ИИ"""
    cfg = make_config(args)
    db = Database(cfg.db_path)

    accounts = db.get_active_accounts()
    if args.account:
        accounts = [a for a in accounts if a.phone == args.account]

    if not accounts:
        print("[!] Нет доступных аккаунтов для парсинга")
        return

    acc = accounts[0]
    sender = TelegramSender(acc, cfg, db)

    if not await sender.connect():
        return

    try:
        parser = GroupParser(sender.client, db)

        ai_filter_obj = None
        keywords = None

        if args.mode == "keywords":
            if not args.keywords:
                print("[!] Укажите --keywords для режима keywords")
                return
            keywords = [kw.strip() for kw in args.keywords.split(",") if kw.strip()]
        elif args.mode == "ai":
            if not args.criteria:
                print("[!] Укажите --criteria для режима ai")
                return
            from ai_filter import AIFilter
            provider = (getattr(args, "provider", "") or "openai").strip().lower()
            model = (getattr(args, "model", "") or "").strip()
            if provider == "groq":
                api_key = getattr(cfg, "groq_api_key", "") or ""
                proxy = (getattr(cfg, "groq_proxy", "") or getattr(cfg, "openai_proxy", "")) or ""
                model = model or "llama-3.3-70b-versatile"
            else:
                provider = "openai"
                api_key = getattr(cfg, "openai_api_key", "") or ""
                proxy = getattr(cfg, "openai_proxy", "") or ""
                model = model or getattr(cfg, "openai_model", "") or "gpt-4o-mini"

            if not api_key:
                print(f"[!] Не задан API key для провайдера {provider}. Укажите в .env или через GUI настройки")
                return

            try:
                ai_filter_obj = AIFilter(provider=provider, api_key=api_key, model=model, proxy=proxy, timeout_seconds=45.0)
            except Exception as e:
                print(f"[!] Не удалось настроить AI: {type(e).__name__}: {e}")
                return

        count = await parser.parse_by_content(
            group=args.group,
            mode=args.mode,
            keywords=keywords,
            ai_criteria=args.criteria or "",
            ai_filter=ai_filter_obj,
            limit_messages=args.limit,
        )

        print(f"\n=== Итого: {count} совпадений найдено ===")
    finally:
        await sender.disconnect()

    db.close()


# --- Команда: mention ---

async def cmd_mention(args):
    """Массовые упоминания пользователей в группе"""
    cfg = make_config(args)
    db = Database(cfg.db_path)

    accounts = db.get_active_accounts()
    if not accounts:
        print("[!] Нет активных аккаунтов")
        return

    # Получить уже упомянутых
    already_mentioned = db.get_already_mentioned_user_ids_from_log(args.target)

    # Получить пользователей для упоминания
    limit = args.limit or 0
    users = db.get_users_for_mention(args.source, exclude_ids=already_mentioned, limit=limit)

    if not users:
        print("[!] Нет пользователей для упоминания")
        return

    mentions_per_msg = args.mentions_per_message or 5
    mentioner = Mentioner(mentions_per_message=mentions_per_msg)

    # Разбить на батчи
    batches = []
    for i in range(0, len(users), mentions_per_msg):
        batches.append(users[i:i + mentions_per_msg])

    print(f"Пользователей: {len(users)}, батчей: {len(batches)}, аккаунтов: {len(accounts)}")

    mode = "DRY-RUN" if getattr(args,'dry_run',False) else "LIVE"
    print(f"Режим: {mode}\n")

    stats = {"sent": 0, "errors": 0, "skipped": 0, "dry_run": 0}
    batch_idx = 0
    acc_idx = 0

    while batch_idx < len(batches) and acc_idx < len(accounts):
        acc = accounts[acc_idx]
        sender = TelegramSender(acc, cfg, db)

        if not await sender.connect():
            acc_idx += 1
            continue

        try:
            while batch_idx < len(batches) and sender.can_send_more():
                batch = batches[batch_idx]
                text, entities = mentioner.build_mention_message(args.message, batch)

                if getattr(args, "dry_run", False):
                    preview = text.replace("\n", " ").strip()
                    if len(preview) > 120:
                        preview = preview[:120] + "…"
                    print(f"  [DRY] Упоминание -> {args.target} ({acc.phone}): {preview}")
                    status = "dry_run"
                else:
                    status = await sender.send_mention_message(args.target, text, entities)

                # Логирование
                user_ids = [u.user_id for u in batch]
                if not getattr(args, "dry_run", False):
                    db.log_mention(acc.phone, args.target, user_ids, status)

                if status in ("sent", "dry_run"):
                    if status == "sent":
                        stats["sent"] += 1
                    else:
                        stats["dry_run"] += 1
                    batch_idx += 1
                elif status == "flood_wait":
                    # Ротация на следующий аккаунт
                    print(f"  [~] Ротация с {acc.phone}")
                    break
                elif status in ("banned", "no_permission", "private"):
                    stats["skipped"] += 1
                    if status == "banned":
                        break  # Аккаунт деактивирован
                    elif status in ("no_permission", "private"):
                        # Группа недоступна — прекращаем полностью
                        print(f"[!] Группа {args.target} недоступна, прекращение")
                        batch_idx = len(batches)
                        break
                else:
                    stats["errors"] += 1
                    batch_idx += 1
        finally:
            await sender.disconnect()

        acc_idx += 1

    print("\n=== Итого ===")
    print(f"Отправлено: {stats['sent']}")
    print(f"Dry Run: {stats['dry_run']}")
    print(f"Ошибки: {stats['errors']}")
    print(f"Пропущено: {stats['skipped']}")
    print(f"Осталось батчей: {len(batches) - batch_idx}")

    db.close()


# --- Команда: broadcast ---

async def cmd_broadcast(args):
    """Рассылка из задач в БД"""
    cfg = make_config(args)
    db = Database(cfg.db_path)

    accounts = db.get_active_accounts()
    tasks = db.get_pending_tasks(task_type="broadcast")

    if not accounts or not tasks:
        print("[!] Нет аккаунтов или задач для рассылки")
        return

    mode = "DRY-RUN" if getattr(args,'dry_run',False) else "LIVE"
    print(f"Аккаунтов: {len(accounts)}, задач: {len(tasks)}, режим: {mode}\n")

    for acc in accounts:
        sender = TelegramSender(acc, cfg, db)

        if not await sender.connect():
            continue

        try:
            for task in tasks:
                if not sender.can_send_more():
                    break
                if getattr(task, "completed", False):
                    continue
                if getattr(task, "status", "pending") in ("waiting", "error", "done"):
                    continue

                from parser import ensure_chat_access
                decision, reason, retry_after = await ensure_chat_access(
                    sender.client, task.target_group, dry_run=getattr(args, "dry_run", False)
                )
                if decision != "ok":
                    if task.id and not getattr(args, "dry_run", False):
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
                    print(f"[!] {task.target_group}: нет доступа ({reason}) — пропуск")
                    continue

                candidates = [line.strip() for line in task.message_text.splitlines() if line.strip()]
                raw = random.choice(candidates) if candidates else task.message_text
                message = spin_text(raw)
                if getattr(args, "dry_run", False):
                    preview = message.replace("\n", " ").strip()
                    if len(preview) > 120:
                        preview = preview[:120] + "…"
                    print(f"  [DRY] {task.target_group} <- {acc.phone}: {preview}")
                    raw_status = "dry_run"
                    status = "dry_run"
                    error_detail = ""
                else:
                    raw_status = await sender.send_broadcast_message(task.target_group, message)
                    status = raw_status.split(":", 1)[0]
                    error_detail = raw_status if raw_status != status else ""

                    db.log_send(SendLog(
                        account_phone=acc.phone,
                        target_group=task.target_group,
                        message_text=message[:200],
                        status=status,
                        error_detail=error_detail[:200],
                        timestamp=datetime.now().isoformat(),
                    ))

                if status == "sent" and task.id and not getattr(args, "dry_run", False):
                    db.mark_task_completed(task.id)
                    task.completed = True
                    task.status = "done"

                if task.id and not getattr(args, "dry_run", False) and status in ("need_subscription", "no_permission", "private", "slow_mode", "error"):
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

                if status in ("flood_wait", "banned"):
                    break
        finally:
            await sender.disconnect()

    db.close()


# --- Команда: add-task ---

def cmd_add_task(args):
    """Добавить задачу в БД"""
    cfg = Config()
    db = Database(cfg.db_path)

    task = Task(
        target_group=args.target,
        message_text=args.message,
        task_type=args.type or "broadcast",
        source_group=args.source or "",
        mentions_per_message=args.mentions_per_message or 5,
    )

    db.add_task(task)
    print(f"[+] Задача добавлена: {args.type or 'broadcast'} -> {args.target}")

    db.close()


# --- Команда: stats ---

def cmd_stats(args):
    """Показать статистику отправок"""
    cfg = Config()
    db = Database(cfg.db_path)

    days = args.days or 7
    stats = db.get_stats(days)

    print(f"=== Статистика за {days} дней ===")
    print(f"Всего: {stats.get('total', 0)}")
    print(f"Отправлено: {stats.get('sent', 0)}")
    print(f"Ошибки: {stats.get('error', 0)}")
    print(f"Flood wait: {stats.get('flood_wait', 0)}")
    print(f"Бан: {stats.get('banned', 0)}")
    print(f"Нет доступа: {stats.get('no_permission', 0)}")

    db.close()


# --- CLI ---

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teleton",
        description="Telegram рассылка и упоминания через несколько аккаунтов",
    )
    subparsers = parser.add_subparsers(dest="command", help="Команды")

    # smart-parse
    p_smart = subparsers.add_parser("smart-parse", help="Смарт-парсинг по содержимому постов")
    p_smart.add_argument("--group", required=True, help="Username группы (@group)")
    p_smart.add_argument("--mode", required=True, choices=["keywords", "ai"],
                          help="Режим: keywords или ai")
    p_smart.add_argument("--keywords", help="Ключевые слова через запятую (для mode=keywords)")
    p_smart.add_argument("--criteria", help="Текст-описание критерия (для mode=ai)")
    p_smart.add_argument("--provider", choices=["openai", "groq"], default="openai", help="AI провайдер (для mode=ai)")
    p_smart.add_argument("--model", help="AI модель (для mode=ai)")
    p_smart.add_argument("--limit", type=int, default=500, help="Лимит сообщений (default=500)")
    p_smart.add_argument("--account", help="Телефон аккаунта")

    # parse
    p_parse = subparsers.add_parser("parse", help="Парсинг участников группы")
    p_parse.add_argument("--group", required=True, help="Username группы (@group)")
    p_parse.add_argument("--aggressive", action="store_true", help="Aggressive-парсинг по алфавиту")
    p_parse.add_argument("--commenters", action="store_true", help="Парсинг комментаторов канала")
    p_parse.add_argument("--limit-posts", type=int, default=50, help="Лимит постов для парсинга комментариев")
    p_parse.add_argument("--account", help="Телефон аккаунта для парсинга")

    # mention
    p_mention = subparsers.add_parser("mention", help="Массовые упоминания")
    p_mention.add_argument("--target", required=True, help="Целевая группа")
    p_mention.add_argument("--source", required=True, help="Источник пользователей (group_source)")
    p_mention.add_argument("--message", required=True, help="Шаблон сообщения (поддержка spintax)")
    p_mention.add_argument("--limit", type=int, default=0, help="Лимит пользователей")
    p_mention.add_argument("--mentions-per-message", type=int, default=5, help="Упоминаний в сообщении")
    p_mention.add_argument("--dry-run", action="store_true", help="Тестовый режим")

    # broadcast
    p_broadcast = subparsers.add_parser("broadcast", help="Рассылка из задач в БД")
    p_broadcast.add_argument("--dry-run", action="store_true", help="Тестовый режим")

    # add-task
    p_add_task = subparsers.add_parser("add-task", help="Добавить задачу")
    p_add_task.add_argument("--target", required=True, help="Целевая группа")
    p_add_task.add_argument("--message", required=True, help="Текст сообщения (spintax)")
    p_add_task.add_argument("--type", default="broadcast", choices=["broadcast", "mention"], help="Тип задачи")
    p_add_task.add_argument("--source", help="Группа-источник (для mention)")
    p_add_task.add_argument("--mentions-per-message", type=int, default=5, help="Упоминаний в сообщении")

    # stats
    p_stats = subparsers.add_parser("stats", help="Статистика отправок")
    p_stats.add_argument("--days", type=int, default=7, help="За сколько дней")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "add-task":
        cmd_add_task(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "parse":
        asyncio.run(cmd_parse(args))
    elif args.command == "smart-parse":
        asyncio.run(cmd_smart_parse(args))
    elif args.command == "mention":
        asyncio.run(cmd_mention(args))
    elif args.command == "broadcast":
        asyncio.run(cmd_broadcast(args))


if __name__ == "__main__":
    main()
