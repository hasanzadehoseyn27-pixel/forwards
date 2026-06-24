from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
from contextlib import AsyncExitStack

from telethon import TelegramClient

from .config import PROJECT_DIR, Settings
from .db import Database
from .engine import ForwardEngine
from .lock import AppLock
from .logging_utils import setup_logging
from .panel import AdminPanel


log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(prog="bestrobot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="ساخت دیتابیس و فایل .env اولیه")
    sub.add_parser("login", help="ورود و ذخیره سشن اکانت تلگرام برای فوروارد")
    run_parser = sub.add_parser("run", help="اجرای همزمان پنل و موتور فوروارد")
    run_parser.add_argument("--force-lock", action="store_true", help="نادیده گرفتن lock قدیمی")
    panel_parser = sub.add_parser("panel", help="اجرای فقط پنل مدیریت")
    panel_parser.add_argument("--force-lock", action="store_true")
    engine_parser = sub.add_parser("engine", help="اجرای فقط موتور فوروارد")
    engine_parser.add_argument("--force-lock", action="store_true")
    sub.add_parser("status", help="نمایش وضعیت خلاصه دیتابیس")

    args = parser.parse_args()
    if args.command == "init":
        command_init()
        return

    settings = Settings.load()
    setup_logging(settings)
    db = Database(settings.db_path)
    db.init_schema()

    if args.command == "login":
        asyncio.run(command_login(settings))
    elif args.command == "run":
        asyncio.run(command_run(settings, db, args.force_lock))
    elif args.command == "panel":
        asyncio.run(command_panel(settings, db, args.force_lock))
    elif args.command == "engine":
        asyncio.run(command_engine(settings, db, args.force_lock))
    elif args.command == "status":
        command_status(db)


def command_init() -> None:
    env_path = PROJECT_DIR / ".env"
    example_path = PROJECT_DIR / ".env.example"
    if not env_path.exists():
        shutil.copyfile(example_path, env_path)
        print(f"فایل .env ساخته شد: {env_path}")
        print("مقادیر API_ID، API_HASH، BOT_TOKEN و ADMIN_IDS را داخل آن پر کن.")
    else:
        print(f"فایل .env از قبل وجود دارد: {env_path}")
    settings = Settings.load()
    db = Database(settings.db_path)
    db.init_schema()
    db.close()
    print(f"دیتابیس آماده است: {settings.db_path}")


async def command_login(settings: Settings) -> None:
    print("ورود اکانت تلگرام شروع شد. شماره، کد و در صورت نیاز رمز دو مرحله‌ای را وارد کن.")
    client = TelegramClient(
        str(settings.user_session),
        settings.api_id,
        settings.api_hash,
        proxy=settings.telethon_proxy(),
        connection_retries=20,
        timeout=20,
    )
    await client.start()
    me = await client.get_me()
    await client.disconnect()
    print(f"سشن ذخیره شد: {settings.user_session}")
    print(f"اکانت فعال: {getattr(me, 'first_name', '')} ({me.id})")


async def command_run(settings: Settings, db: Database, force_lock: bool) -> None:
    async with managed_locks(settings, ["panel", "engine"], force_lock):
        bot = await start_bot(settings)
        user = await start_user(settings)
        panel = AdminPanel(bot, db, settings)
        panel.register()
        engine = ForwardEngine(user, db, settings)
        await engine.start()
        print("BestRobot روشن شد. برای توقف Ctrl+C بزن.")
        try:
            await wait_forever()
        finally:
            await engine.stop()
            await bot.disconnect()
            await user.disconnect()
            db.close()


async def command_panel(settings: Settings, db: Database, force_lock: bool) -> None:
    async with managed_locks(settings, ["panel"], force_lock):
        bot = await start_bot(settings)
        panel = AdminPanel(bot, db, settings)
        panel.register()
        print("پنل مدیریت روشن شد. برای توقف Ctrl+C بزن.")
        try:
            await wait_forever()
        finally:
            await bot.disconnect()
            db.close()


async def command_engine(settings: Settings, db: Database, force_lock: bool) -> None:
    async with managed_locks(settings, ["engine"], force_lock):
        user = await start_user(settings)
        engine = ForwardEngine(user, db, settings)
        await engine.start()
        print("موتور فوروارد روشن شد. برای توقف Ctrl+C بزن.")
        try:
            await wait_forever()
        finally:
            await engine.stop()
            await user.disconnect()
            db.close()


async def start_bot(settings: Settings) -> TelegramClient:
    bot = TelegramClient(
        str(settings.bot_session),
        settings.api_id,
        settings.api_hash,
        proxy=settings.telethon_proxy(),
        connection_retries=20,
        timeout=20,
    )
    await bot.start(bot_token=settings.bot_token)
    me = await bot.get_me()
    log.info("پنل بات وصل شد: %s", getattr(me, "username", me.id))
    return bot


async def start_user(settings: Settings) -> TelegramClient:
    user = TelegramClient(
        str(settings.user_session),
        settings.api_id,
        settings.api_hash,
        proxy=settings.telethon_proxy(),
        connection_retries=20,
        timeout=20,
    )
    await user.connect()
    if not await user.is_user_authorized():
        await user.disconnect()
        raise RuntimeError("سشن اکانت تلگرام آماده نیست. اول اجرا کن: python -m bestrobot login")
    me = await user.get_me()
    log.info("اکانت فوروارد وصل شد: %s", getattr(me, "username", me.id))
    return user


async def wait_forever() -> None:
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


class managed_locks:
    def __init__(self, settings: Settings, names: list[str], force: bool) -> None:
        self.settings = settings
        self.names = names
        self.force = force
        self.stack = AsyncExitStack()
        self.locks: list[AppLock] = []

    async def __aenter__(self):
        for name in self.names:
            lock = AppLock(self.settings.data_dir / f"{self.settings.instance_name}-{name}.lock", self.settings.lock_stale_seconds)
            lock.acquire(force=self.force)
            lock.start_heartbeat()
            self.locks.append(lock)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for lock in reversed(self.locks):
            await lock.close()


def command_status(db: Database) -> None:
    groups = db.list_groups()
    print(f"تعداد گروه‌ها: {len(groups)}")
    for group in groups:
        status = db.group_status(int(group["id"]))
        jobs = status["jobs"]
        state = "روشن" if group["enabled"] else "خاموش"
        print(
            f"{group['id']}. {group['name']} | {state} | {group['mode']} | "
            f"pending={jobs.get('pending', 0)} running={jobs.get('running', 0)} "
            f"done={jobs.get('done', 0)} failed={jobs.get('failed', 0)}"
        )
