from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
from contextlib import AsyncExitStack

from telethon import TelegramClient

from .bot_pool import BotPool, load_bot_tokens
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
    sub.add_parser("bots-status", help="بررسی ربات‌های استخر ارسال (bot_tokens.txt)")
    sub.add_parser("bots-start", help="اکانت یک بار به همه‌ی ربات‌های استخر /start می‌دهد")
    join_parser = sub.add_parser("bots-join", help="اکانت ربات‌های استخر را عضو گروه‌های مقصد می‌کند")
    join_parser.add_argument("--delay", type=float, default=4.0, help="فاصله بین هر دعوت (ثانیه)")

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
    elif args.command == "bots-status":
        asyncio.run(command_bots_status(settings, db))
    elif args.command == "bots-start":
        asyncio.run(command_bots_start(settings, db))
    elif args.command == "bots-join":
        asyncio.run(command_bots_join(settings, db, args.delay))


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
        panel = AdminPanel(bot, db, settings, forward_client=user)
        panel.register()
        pool = await start_pool(settings, db, user)
        engine = ForwardEngine(user, db, settings, pool=pool)
        await engine.start()
        print("BestRobot روشن شد. برای توقف Ctrl+C بزن.")
        try:
            await wait_forever()
        finally:
            await _shutdown_gracefully(engine, bot, user, db, pool)


async def _shutdown_gracefully(
    engine: ForwardEngine, bot: TelegramClient, user: TelegramClient, db: Database, pool: BotPool | None = None
) -> None:
    """خاموش‌کردن تمیز، ولی با تایم‌اوت روی هر مرحله: قبلا اگر یکی از این مرحله‌ها
    (مخصوصا disconnect شبکه‌ای) هنگ می‌کرد، کل پروسه نه واقعا می‌مرد نه کار می‌کرد؛
    فقط 'آنلاین' می‌ماند بدون هیچ فعالیتی، و PM2 هم متوجه نمی‌شد که باید دوباره
    بالایش بیاورد. الان اگر خاموش‌شدن تمیز بیش از حد طول بکشد، پروسه را عمدا
    می‌بندیم تا PM2 واقعا تشخیص بدهد و از نو راه‌اندازی‌اش کند."""

    try:
        await asyncio.wait_for(_full_shutdown(engine, bot, user, db, pool), timeout=45)
    except asyncio.TimeoutError:
        log.error("خاموش‌شدن تمیز کلا بیش از ۴۵ ثانیه طول کشید؛ پروسه برای اطمینان بسته می‌شود تا PM2 دوباره بالایش بیاورد.")
        os._exit(1)


async def _full_shutdown(
    engine: ForwardEngine, bot: TelegramClient | None, user: TelegramClient, db: Database, pool: BotPool | None = None
) -> None:
    try:
        await engine.stop()
    except Exception:
        log.exception("خطا هنگام توقف موتور فوروارد.")
    if pool is not None:
        try:
            await pool.stop()
        except Exception:
            log.exception("خطا هنگام توقف استخر ربات‌ها.")
    if bot is not None:
        try:
            await bot.disconnect()
        except Exception:
            log.exception("خطا هنگام قطع اتصال بات پنل.")
    try:
        await user.disconnect()
    except Exception:
        log.exception("خطا هنگام قطع اتصال اکانت فوروارد.")
    db.close()


async def command_panel(settings: Settings, db: Database, force_lock: bool) -> None:
    async with managed_locks(settings, ["panel"], force_lock):
        bot = await start_bot(settings)
        forward_client = await try_start_user(settings)
        panel = AdminPanel(bot, db, settings, forward_client=forward_client)
        panel.register()
        print("پنل مدیریت روشن شد. برای توقف Ctrl+C بزن.")
        try:
            await wait_forever()
        finally:
            await bot.disconnect()
            if forward_client:
                await forward_client.disconnect()
            db.close()


async def command_engine(settings: Settings, db: Database, force_lock: bool) -> None:
    async with managed_locks(settings, ["engine"], force_lock):
        user = await start_user(settings)
        pool = await start_pool(settings, db, user)
        engine = ForwardEngine(user, db, settings, pool=pool)
        await engine.start()
        print("موتور فوروارد روشن شد. برای توقف Ctrl+C بزن.")
        try:
            await wait_forever()
        finally:
            try:
                await asyncio.wait_for(_full_shutdown(engine, None, user, db, pool), timeout=45)
            except asyncio.TimeoutError:
                log.error("خاموش‌شدن تمیز کلا بیش از ۴۵ ثانیه طول کشید؛ پروسه برای اطمینان بسته می‌شود تا PM2 دوباره بالایش بیاورد.")
                os._exit(1)


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


async def try_start_user(settings: Settings) -> TelegramClient | None:
    try:
        return await start_user(settings)
    except Exception as exc:
        log.warning("اتصال اکانت فوروارد برای پنل برقرار نشد؛ گرفتن اسم واقعی کانال‌ها غیرفعال می‌ماند: %s", exc)
        return None


async def start_pool(settings: Settings, db: Database, user: TelegramClient, poll: bool = True) -> BotPool | None:
    """اگر bot_tokens.txt توکن داشته باشد، ارسال در مقصدها با ربات‌ها انجام می‌شود؛
    وگرنه همان روش قبلی (فوروارد مستقیم با اکانت) می‌ماند."""
    tokens = load_bot_tokens(settings.bot_tokens_file)
    if not tokens:
        log.info("فایل %s توکنی ندارد؛ ارسال مثل قبل با خود اکانت انجام می‌شود.", settings.bot_tokens_file)
        return None
    pool = BotPool(tokens, db, relay_delay_seconds=settings.relay_delay_seconds)
    me = await user.get_me()
    await pool.start(int(me.id), poll=poll)
    if not pool.enabled:
        log.error("هیچ‌کدام از ربات‌های %s کار نکرد؛ ارسال مثل قبل با خود اکانت انجام می‌شود.", settings.bot_tokens_file)
        await pool.stop()
        return None
    log.info("ارسال با استخر ربات‌ها: %s ربات فعال.", len(pool.usernames()))
    return pool


async def command_bots_status(settings: Settings, db: Database) -> None:
    tokens = load_bot_tokens(settings.bot_tokens_file)
    if not tokens:
        print(f"توکنی در {settings.bot_tokens_file} نیست.")
        return
    user = await start_user(settings)
    try:
        pool = await start_pool(settings, db, user, poll=False)
        if pool is None:
            return
        try:
            alive = [b for b in pool.bots if b.alive]
            print(f"ربات‌های سالم: {len(alive)} از {len(pool.bots)}")
            destinations = db.all_destinations()
            for bot in pool.bots:
                if not bot.alive:
                    print(f"❌ {bot.index}. (توکن کار نمی‌کند)")
                    continue
                joined = sum(1 for d in destinations if db.get_membership(bot.username, int(d["id"])) == "joined")
                print(f"✅ {bot.index}. @{bot.username} — عضو {joined} از {len(destinations)} مقصد")
        finally:
            await pool.stop()
    finally:
        await user.disconnect()


async def command_bots_start(settings: Settings, db: Database) -> None:
    user = await start_user(settings)
    try:
        pool = await start_pool(settings, db, user, poll=False)
        if pool is None:
            return
        try:
            ok, fail = await pool.send_start_to_all(user)
            print(f"/start فرستاده شد: {ok} موفق، {fail} ناموفق.")
        finally:
            await pool.stop()
    finally:
        await user.disconnect()


async def command_bots_join(settings: Settings, db: Database, delay: float) -> None:
    """ربات‌ها را (به‌عنوان عضو عادی) به همه‌ی گروه‌های مقصد اضافه می‌کند.
    قابل ادامه است: عضویت‌های انجام‌شده ثبت می‌شوند و اجرای دوباره از همان‌جا ادامه می‌دهد."""
    from telethon import functions, types
    from telethon.errors import (
        BotGroupsBlockedError,
        ChatAdminRequiredError,
        FloodWaitError,
        PeerFloodError,
        UserAlreadyParticipantError,
        UserNotParticipantError,
    )

    user = await start_user(settings)
    try:
        pool = await start_pool(settings, db, user, poll=False)
        if pool is None:
            return
        try:
            destinations = db.all_destinations()
            usernames = pool.usernames()
            print(f"{len(usernames)} ربات و {len(destinations)} گروه مقصد.")
            for dest in destinations:
                dest_id = int(dest["id"])
                peer = str(dest["peer"])
                try:
                    entity = await user.get_entity(peer)
                except Exception as exc:  # noqa: BLE001
                    print(f"⚠️ مقصد {peer} پیدا نشد: {exc}")
                    continue
                if isinstance(entity, types.Channel) and not entity.megagroup:
                    print(f"⚠️ {peer} کانال است نه گروه؛ ربات فقط به‌عنوان ادمین می‌تواند عضو کانال شود — رد شد.")
                    continue
                added = already = failed = 0
                for username in usernames:
                    if db.get_membership(username, dest_id) == "joined":
                        already += 1
                        continue
                    try:
                        bot_entity = await user.get_input_entity(username)
                        if isinstance(entity, types.Channel):
                            try:
                                await user(functions.channels.GetParticipantRequest(entity, bot_entity))
                                db.set_membership(username, dest_id, "joined", "از قبل عضو بود")
                                already += 1
                                continue
                            except UserNotParticipantError:
                                pass
                            result = await user(functions.channels.InviteToChannelRequest(entity, [bot_entity]))
                        else:
                            result = await user(functions.messages.AddChatUserRequest(entity.id, bot_entity, fwd_limit=0))
                        missing = getattr(result, "missing_invitees", None) or []
                        if missing:
                            db.set_membership(username, dest_id, "failed", "اضافه نشد (محدودیت حریم خصوصی/تنظیمات ربات)")
                            failed += 1
                        else:
                            db.set_membership(username, dest_id, "joined")
                            added += 1
                    except UserAlreadyParticipantError:
                        db.set_membership(username, dest_id, "joined", "از قبل عضو بود")
                        already += 1
                    except BotGroupsBlockedError:
                        db.set_membership(username, dest_id, "failed", "در BotFather گزینه‌ی Allow Groups خاموش است")
                        failed += 1
                        print(f"   ❌ @{username}: در BotFather گزینه‌ی Allow Groups را روشن کن.")
                    except ChatAdminRequiredError:
                        print(f"❌ اکانت در {peer} اجازه‌ی اضافه‌کردن عضو ندارد؛ این گروه رد شد.")
                        break
                    except FloodWaitError as exc:
                        if exc.seconds > 900:
                            print(f"⏳ تلگرام {exc.seconds} ثانیه محدودیت داد. بعداً دوباره همین دستور را بزن؛ از همین‌جا ادامه می‌دهد.")
                            return
                        print(f"⏳ {exc.seconds} ثانیه صبر...")
                        await asyncio.sleep(exc.seconds + 5)
                    except PeerFloodError:
                        print("⛔ تلگرام اجازه‌ی دعوت بیشتر نمی‌دهد (PeerFlood). چند ساعت بعد دوباره همین دستور را بزن؛ از همین‌جا ادامه می‌دهد.")
                        return
                    except Exception as exc:  # noqa: BLE001
                        db.set_membership(username, dest_id, "failed", str(exc))
                        failed += 1
                        print(f"   ❌ @{username}: {exc}")
                    await asyncio.sleep(delay)
                print(f"✅ {peer}: {added} اضافه شد، {already} از قبل عضو، {failed} ناموفق.")
        finally:
            await pool.stop()
    finally:
        await user.disconnect()


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