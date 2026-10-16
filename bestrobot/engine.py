from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatForwardsRestrictedError,
    ChatWriteForbiddenError,
    FloodWaitError,
    MessageIdInvalidError,
    RPCError,
)
from telethon.tl.types import MessageEntityStrike

from .bot_pool import BotApiError, BotPool, PoolBot
from .config import Settings, normalize_peer
from .db import Database, now_ts


log = logging.getLogger(__name__)

TEHRAN_TZ = ZoneInfo("Asia/Tehran")


class ForwardEngine:
    def __init__(self, client: TelegramClient, db: Database, settings: Settings, pool: BotPool | None = None) -> None:
        self.client = client
        self.db = db
        self.settings = settings
        # اگر استخر ربات فعال باشد، ارسال در گروه‌های مقصد با ربات‌هاست و
        # اکانت فقط پیام را یک بار در پی‌وی یک ربات فوروارد می‌کند.
        self.pool = pool if (pool is not None and pool.enabled) else None
        self._dest_chat_ids: dict[int, int] = {}
        # (bot_index, chat_id) -> تا این زمان این ربات در این مقصد استفاده نمی‌شود
        # (بیرون شده/عضو نیست/اجازه‌ی ارسال ندارد)
        self._bot_blocked: dict[tuple[int, int], int] = {}
        self._last_relay_purge = 0
        self.stop_event = asyncio.Event()
        self.send_semaphore = asyncio.Semaphore(settings.max_parallel_sends)
        self.tasks: list[asyncio.Task[None]] = []
        self._group_flood_until: dict[int, int] = {}

    async def start(self) -> None:
        recovered = self.db.recover_running_jobs()
        if recovered:
            log.warning("تعداد %s ارسال نیمه‌کاره دوباره به صف برگشت.", recovered)
        self.tasks = [
            asyncio.create_task(self.scan_sources_loop(), name="scan-sources"),
            asyncio.create_task(self.repeat_scheduler_loop(), name="repeat-scheduler"),
            asyncio.create_task(self.scheduled_starts_loop(), name="scheduled-starts"),
            asyncio.create_task(self.watchdog_loop(), name="watchdog"),
        ]
        for index in range(self.settings.worker_count):
            self.tasks.append(asyncio.create_task(self.worker_loop(f"worker-{index + 1}"), name=f"worker-{index + 1}"))
        log.info("موتور فوروارد روشن شد.")

    async def stop(self) -> None:
        self.stop_event.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        log.info("موتور فوروارد خاموش شد.")

    async def scan_sources_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.pool and now_ts() - self._last_relay_purge > 6 * 3600:
                    self._last_relay_purge = now_ts()
                    purged = self.db.purge_old_relays()
                    if purged:
                        log.info("%s رکورد قدیمی relay پاک شد.", purged)
                await self.scan_sources_once()
            except Exception:
                log.exception("خطا در اسکن مبداها")
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def scan_sources_once(self) -> None:
        groups = self.db.active_groups()
        for group in groups:
            group_id = int(group["id"])
            sources = self.db.group_sources(group_id)
            if not sources:
                continue
            for source in sources:
                await self.scan_one_source(group, source)

    async def scan_one_source(self, group, source) -> None:
        group_id = int(group["id"])
        source_id = int(source["id"])
        peer = str(source["peer"])
        today = datetime.now(TEHRAN_TZ).date()
        try:
            last_id = self.db.get_last_message_id(group_id, source_id)
            if last_id <= 0:
                await self.enqueue_todays_messages(group, source)
                return

            max_seen = last_id
            async for message in self.client.iter_messages(peer, min_id=last_id, reverse=True):
                if not message or not message.id:
                    continue
                max_seen = max(max_seen, int(message.id))
                if self._local_date(message.date) != today:
                    log.info(
                        "پیام %s از %s مال قبل از امروز است؛ فوروارد نمی‌شود و دیگر هم بررسی نمی‌شود.",
                        message.id,
                        peer,
                    )
                    continue
                if self._is_sticker(message):
                    log.info("پیام %s از %s استیکر است؛ فوروارد نمی‌شود.", message.id, peer)
                    continue
                date_text = self._message_date_text(message.date)
                has_strike = self._has_strike(message)
                self.db.remember_message(group_id, source_id, int(message.id), date_text, message.text, has_strike)
                if not self.db.should_forward(group_id, message.text, has_strike):
                    log.info(
                        "پیام %s از %s فیلتر شد (ایموجی لازم/کلمه ممنوعه/خط‌خوردگی)؛ فوروارد نمی‌شود.",
                        message.id,
                        peer,
                    )
                    continue
                cycle_key = "once" if group["mode"] == "once" else "initial"
                enqueued = self.db.enqueue_message(group_id, source_id, int(message.id), cycle_key)
                if enqueued:
                    log.info("پیام %s از %s برای گروه %s وارد صف شد.", message.id, peer, group_id)

            if max_seen > last_id:
                self.db.set_last_message_id(group_id, source_id, max_seen)
        except ValueError as exc:
            if str(exc).startswith("No user has"):
                self.db.disable_entity(source_id)
                self.db.mark_error(group_id, f"مبدا {peer} دیگر روی تلگرام وجود ندارد و خاموش شد")
                log.warning(
                    "مبدا %s دیگر پیدا نشد (یوزرنیم عوض شده یا حذف شده) و به‌صورت خودکار خاموش شد. "
                    "برای فعال‌سازی دوباره باید یوزرنیم درست را از پنل وارد کنی.",
                    peer,
                )
            else:
                self.db.mark_error(group_id, f"خطا در خواندن مبدا {peer}: {exc}")
                log.exception("خطا در خواندن مبدا %s", peer)
        except RPCError as exc:
            self.db.mark_error(group_id, f"خطای تلگرام هنگام خواندن مبدا {peer}: {exc.__class__.__name__}")
            log.warning("خطای تلگرام در خواندن مبدا %s: %s", peer, exc)
        except Exception as exc:
            self.db.mark_error(group_id, f"خطا در خواندن مبدا {peer}: {exc}")
            log.exception("خطا در خواندن مبدا %s", peer)

    async def enqueue_todays_messages(self, group, source, day_message_limit: int = 200) -> None:
        group_id = int(group["id"])
        source_id = int(source["id"])
        peer = str(source["peer"])
        today = datetime.now(TEHRAN_TZ).date()
        cycle_key = "once" if group["mode"] == "once" else "initial"

        collected = []
        highest_seen_id = 0
        async for message in self.client.iter_messages(peer, limit=day_message_limit):
            if not message or not message.id:
                continue
            if self._local_date(message.date) != today:
                break
            highest_seen_id = max(highest_seen_id, int(message.id))
            if self._is_sticker(message):
                log.info("پیام %s از %s استیکر است؛ فوروارد نمی‌شود.", message.id, peer)
                continue
            collected.append(message)

        if not collected:
            if highest_seen_id:
                self.db.set_last_message_id(group_id, source_id, highest_seen_id)
                log.info(
                    "گروه %s / مبدا %s امروز فقط استیکر داشت؛ از پیام %s به بعد ادامه می‌شود.",
                    group_id,
                    peer,
                    highest_seen_id,
                )
                return
            latest = await self.client.get_messages(peer, limit=1)
            if latest:
                message = latest[0]
                message_id = int(message.id)
                date_text = self._message_date_text(message.date)
                self.db.remember_message(group_id, source_id, message_id, date_text, message.text, self._has_strike(message))
                self.db.set_last_message_id(group_id, source_id, message_id)
                log.info("گروه %s / مبدا %s امروز پستی نداشت؛ از پیام %s به بعد ادامه می‌شود.", group_id, peer, message_id)
            return

        max_seen = highest_seen_id
        for message in reversed(collected):
            message_id = int(message.id)
            date_text = self._message_date_text(message.date)
            has_strike = self._has_strike(message)
            self.db.remember_message(group_id, source_id, message_id, date_text, message.text, has_strike)
            if self.db.should_forward(group_id, message.text, has_strike):
                self.db.enqueue_message(group_id, source_id, message_id, cycle_key)
            else:
                log.info("پیام %s از %s فیلتر شد (ایموجی لازم/کلمه ممنوعه/خط‌خوردگی)؛ فوروارد نمی‌شود.", message_id, peer)
            max_seen = max(max_seen, message_id)

        self.db.set_last_message_id(group_id, source_id, max_seen)
        log.info("گروه %s / مبدا %s: %s آگهی امروز وارد صف شد.", group_id, peer, len(collected))

    async def repeat_scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.enqueue_due_repeats()
            except Exception:
                log.exception("خطا در زمان‌بندی ارسال تکراری")
            await asyncio.sleep(self.settings.repeat_scan_seconds)

    async def enqueue_due_repeats(self) -> None:
        today = datetime.now(TEHRAN_TZ).date().isoformat()
        max_pending_per_group = 100
        for group in self.db.active_groups():
            if group["mode"] != "repeat":
                continue
            group_id = int(group["id"])
            health = self.db.fetchone("SELECT last_repeat_at FROM group_health WHERE group_id=?", (group_id,))
            last_repeat = int(health["last_repeat_at"] or 0) if health else 0
            interval = int(group["interval_seconds"])
            if not last_repeat:
                self.db.execute("UPDATE group_health SET last_repeat_at=? WHERE group_id=?", (now_ts(), group_id))
                continue
            if last_repeat and now_ts() - last_repeat < interval:
                continue
            pending = self.db.count_pending_jobs(group_id)
            if pending >= max_pending_per_group:
                self.db.execute("UPDATE group_health SET last_repeat_at=? WHERE group_id=?", (now_ts(), group_id))
                log.warning(
                    "گروه %s هنوز %s پیام در صف دارد؛ این دور تکرار رد شد تا صف خالی‌تر شود.",
                    group_id,
                    pending,
                )
                continue
            cycle_key = f"repeat:{today}:{now_ts() // max(1, interval)}"
            total = self.db.enqueue_repeat_due_messages(group_id, cycle_key, today)
            if total:
                log.info("برای گروه %s تعداد %s ارسال تکراری وارد صف شد.", group_id, total)

    async def scheduled_starts_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                for row in self.db.due_scheduled_starts():
                    self.db.complete_scheduled_start(int(row["id"]), int(row["group_id"]))
                    log.info("گروه %s طبق شروع فاصله‌ای روشن شد.", row["group_id"])
            except Exception:
                log.exception("خطا در روشن‌کردن زمان‌بندی‌شده گروه‌ها")
            await asyncio.sleep(5)

    async def watchdog_loop(self) -> None:
        """هر چند دقیقه چک می‌کند کلاینت تلگرام واقعاً پاسخگوست یا فقط ظاهراً وصل است.
        اگر پاسخ نگرفت، عمداً کل پروسه را می‌بندد تا PM2 آن را تمیز دوباره بالا بیاورد،
        چون تلاش برای ریکانکت دستی توی همین پروسه گاهی به‌خاطر هنگ‌کردن واقعی شبکه جواب نمی‌دهد.
        اگر همین الان یک یا چند گروه در محدودیت شناخته‌شده‌ی FloodWait هستند، کندی/عدم‌پاسخ
        سریع طبیعی است (نه هنگ واقعی)، پس در آن حالت به‌جای کشتن پروسه فقط صبر می‌کند."""
        while not self.stop_event.is_set():
            await asyncio.sleep(self.settings.watchdog_interval_seconds)
            if self.stop_event.is_set():
                return
            try:
                await asyncio.wait_for(self.client.get_me(), timeout=self.settings.watchdog_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now = now_ts()
                active_flood_groups = [gid for gid, until in self._group_flood_until.items() if until > now]
                if active_flood_groups:
                    log.warning(
                        "نگهبان: کلاینت به‌موقع پاسخ نداد (%s)، اما %s گروه در محدودیت شناخته‌شده‌ی FloodWait هستند؛ "
                        "این کندی طبیعی است، پروسه بسته نمی‌شود.",
                        exc,
                        len(active_flood_groups),
                    )
                    continue
                log.error(
                    "نگهبان: کلاینت تلگرام به موقع پاسخ نداد (%s) و هیچ FloodWait شناخته‌شده‌ای هم در جریان نیست. "
                    "برای ریست تمیز، پروسه الان بسته می‌شود تا PM2 دوباره بالا بیاوردش.",
                    exc,
                )
                os._exit(1)

    async def worker_loop(self, worker_name: str) -> None:
        while not self.stop_event.is_set():
            try:
                jobs = self.db.claim_next_batch(worker_name)
                if not jobs:
                    await asyncio.sleep(1)
                    continue
                async with self.send_semaphore:
                    await self.send_batch(list(jobs))
                await asyncio.sleep(self.settings.min_send_delay_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("خطای غیرمنتظره در %s؛ ورکر به‌جای مردن، بعد از یک مکث ادامه می‌دهد.", worker_name)
                await asyncio.sleep(2)

    async def send_batch(self, jobs: list) -> None:
        first = jobs[0]
        group_id = int(first["group_id"])
        job_ids = [int(j["id"]) for j in jobs]

        blocked_until = self._group_flood_until.get(group_id)
        if blocked_until and now_ts() < blocked_until:
            for jid in job_ids:
                self.db.finish_job(
                    jid,
                    ok=False,
                    error="گروه هنوز در محدودیت FloodWait قبلی است؛ ارسال عقب افتاد",
                    run_after=blocked_until,
                    max_attempts=999999,
                )
            return

        group = self.db.get_group(group_id)
        if not group or not group["enabled"]:
            for jid in job_ids:
                self.db.finish_job(
                    jid,
                    ok=False,
                    error="گروه خاموش است؛ ارسال فعلا عقب افتاد",
                    run_after=now_ts() + 3600,
                    max_attempts=999999,
                )
            return

        source = self.db.fetchone("SELECT * FROM entities WHERE id=?", (int(first["source_id"]),))
        destination = self.db.fetchone("SELECT * FROM entities WHERE id=?", (int(first["destination_id"]),))
        if not source or not destination:
            for jid in job_ids:
                self.db.finish_job(jid, ok=False, error="مبدا یا مقصد حذف شده است", max_attempts=1)
            return

        if self.pool is not None and self._dest_uses_pool(destination):
            await self.send_batch_via_bots(jobs, group_id, source, destination)
            return

        message_ids = [int(j["telegram_message_id"]) for j in jobs]
        try:
            await self.client.forward_messages(
                entity=str(destination["peer"]),
                messages=message_ids,
                from_peer=str(source["peer"]),
            )
            for jid in job_ids:
                self.db.finish_job(jid, ok=True, max_attempts=self.settings.max_job_attempts)
            log.info(
                "ارسال موفق (یکجا): گروه %s، %s پیام، مقصد %s",
                group_id,
                len(job_ids),
                destination["peer"],
            )
        except FloodWaitError as exc:
            wait_seconds = int(getattr(exc, "seconds", 60)) + 5
            self._group_flood_until[group_id] = now_ts() + wait_seconds
            for jid in job_ids:
                self.db.finish_job(
                    jid,
                    ok=False,
                    error=f"تلگرام محدودیت داد؛ {wait_seconds} ثانیه صبر",
                    run_after=now_ts() + wait_seconds,
                    max_attempts=999999,
                )
            log.warning("FloodWait برای گروه %s: %s ثانیه", group_id, wait_seconds)
        except (ChatWriteForbiddenError, ChannelPrivateError) as exc:
            for jid in job_ids:
                self.db.finish_job(
                    jid,
                    ok=False,
                    error=f"اکانت از مقصد {destination['peer']} بن شده یا بهش دسترسی نداره — دیگه تلاش نمی‌کنیم",
                    max_attempts=1,
                )
            log.warning("اکانت از مقصد %s بن شده/بی‌دسترسیه؛ %s job دیگه retry نمی‌شن.", destination["peer"], len(job_ids))
        except MessageIdInvalidError:
            if len(jobs) == 1:
                self.db.finish_job(
                    job_ids[0],
                    ok=False,
                    error="آیدیِ پیامِ مبدا نامعتبره (احتمالاً حذف شده) — دیگه تلاش نمی‌کنیم",
                    max_attempts=1,
                )
                log.warning("پیامِ مبدا برای job %s نامعتبره؛ دیگه retry نمی‌شه.", job_ids[0])
            else:
                # یکی از پیام‌های این دسته نامعتبره؛ دسته را دو نیم می‌کنیم و هرکدام
                # را جدا امتحان می‌کنیم تا فقط همان یک پیام خراب کنار برود، نه کل دسته.
                mid = len(jobs) // 2
                log.warning(
                    "یکی از پیام‌های دسته (گروه %s، %s پیام) نامعتبره؛ دسته دو نیم می‌شود.",
                    group_id,
                    len(jobs),
                )
                await self.send_batch(jobs[:mid])
                await self.send_batch(jobs[mid:])
        except RPCError as exc:
            if len(jobs) > 1:
                # این خطا ممکن است چون دسته خیلی بزرگ بوده رخ داده باشد (نه هر دلیل
                # دیگری)؛ به‌جای اینکه کل دسته را ۲ دقیقه معطل کنیم، دو نیمش می‌کنیم
                # و هر نیمه را جدا امتحان می‌کنیم. این‌طوری اندازه‌ی دسته خودش را با
                # سقف واقعی تلگرام (که مستند نیست) تطبیق می‌دهد، بدون این‌که لازم
                # باشد از قبل عدد درستش را حدس بزنیم.
                mid = len(jobs) // 2
                log.warning(
                    "خطای تلگرام در ارسال دسته (گروه %s، %s پیام): %s؛ دسته دو نیم می‌شود.",
                    group_id,
                    len(jobs),
                    exc,
                )
                await self.send_batch(jobs[:mid])
                await self.send_batch(jobs[mid:])
                return
            for jid in job_ids:
                self.db.finish_job(
                    jid,
                    ok=False,
                    error=f"خطای تلگرام در ارسال به {destination['peer']}: {exc.__class__.__name__}",
                    run_after=now_ts() + 120,
                    max_attempts=self.settings.max_job_attempts,
                )
            log.warning("خطای تلگرام در ارسال job %s: %s", job_ids[0], exc)
        except Exception as exc:
            if len(jobs) > 1:
                mid = len(jobs) // 2
                log.warning(
                    "خطای غیرمنتظره در ارسال دسته (گروه %s، %s پیام): %s؛ دسته دو نیم می‌شود.",
                    group_id,
                    len(jobs),
                    exc,
                )
                await self.send_batch(jobs[:mid])
                await self.send_batch(jobs[mid:])
                return
            for jid in job_ids:
                self.db.finish_job(
                    jid,
                    ok=False,
                    error=f"خطای ارسال به {destination['peer']}: {exc}",
                    run_after=now_ts() + 120,
                    max_attempts=self.settings.max_job_attempts,
                )
            log.exception("خطای ارسال job %s", job_ids[0])

    # ------------------------------------------------------------ bot pool

    def _dest_uses_pool(self, destination) -> bool:
        """یک مقصد فقط وقتی با ربات‌ها ارسال می‌شود که:
        ۱) در BOT_POOL_DESTINATIONS باشد (اگر این تنظیم خالی است، همه‌ی مقصدها مجازند)، و
        ۲) دست‌کم یک ربات با bots-join عضو آن ثبت شده باشد.
        وگرنه مثل قبل با خود اکانت — تا ربات‌هایی که عضو نیستند بی‌خود امتحان نشوند
        و اکانت پیام را بی‌جهت به پی‌وی‌شان منتقل نکند."""
        only = self.settings.bot_pool_destinations
        if only and normalize_peer(str(destination["peer"])) not in only:
            return False
        return bool(self.db.joined_bots(int(destination["id"])))

    async def _destination_chat_id(self, destination) -> int:
        """شناسه‌ی عددی مقصد (مثل -100...) که Bot API می‌فهمد."""
        dest_id = int(destination["id"])
        cached = self._dest_chat_ids.get(dest_id)
        if cached:
            return cached
        chat_id = int(await self.client.get_peer_id(str(destination["peer"])))
        self._dest_chat_ids[dest_id] = chat_id
        return chat_id

    def _bot_usable(self, bot: PoolBot | None, chat_id: int, members: set[str] | None = None) -> bool:
        if bot is None or not bot.alive:
            return False
        # اگر عضویت ربات‌ها در این مقصد ثبت شده (bots-join)، فقط همان‌ها
        if members and bot.username.lower() not in members:
            return False
        until = self._bot_blocked.get((bot.index, chat_id), 0)
        return until <= now_ts()

    def _pick_new_owner(self, chat_id: int, exclude: set[int], members: set[str] | None = None) -> PoolBot | None:
        """یک ربات سالم و عضو این مقصد که بلاک نیست؛ اول آن‌هایی که الان در محدودیت 429 نیستند."""
        assert self.pool is not None
        candidates = []
        for _ in range(len(self.pool.bots)):
            bot = self.pool.pick_owner()
            if bot is None:
                return None
            if bot.index in exclude or not self._bot_usable(bot, chat_id, members):
                continue
            if self.pool.flood_remaining(bot, chat_id) <= 0:
                return bot
            candidates.append(bot)
        if candidates:
            return min(candidates, key=lambda b: self.pool.flood_remaining(b, chat_id))
        return None

    def _defer(self, jobs: list, seconds: int, error: str) -> None:
        for job in jobs:
            self.db.finish_job(
                int(job["id"]),
                ok=False,
                error=error,
                run_after=now_ts() + max(1, int(seconds)),
                max_attempts=999999,
            )

    def _fail_permanently(self, jobs: list, error: str) -> None:
        for job in jobs:
            self.db.finish_job(int(job["id"]), ok=False, error=error, max_attempts=1)

    async def send_batch_via_bots(self, jobs: list, group_id: int, source, destination) -> None:
        assert self.pool is not None
        source_id = int(source["id"])
        source_peer = str(source["peer"])
        dest_peer = str(destination["peer"])

        try:
            chat_id = await self._destination_chat_id(destination)
        except Exception as exc:  # noqa: BLE001
            self._defer(jobs, 300, f"شناسه‌ی مقصد {dest_peer} پیدا نشد: {exc}")
            log.warning("شناسه‌ی عددی مقصد %s پیدا نشد: %s", dest_peer, exc)
            return

        message_ids = [int(j["telegram_message_id"]) for j in jobs]
        holders = self.db.get_relays(source_id, message_ids)
        members = self.db.joined_bots(int(destination["id"]))

        # همه‌ی پیام‌های این دسته (یک کانال → یک گروه) را «یک ربات» یک‌جا می‌فرستد،
        # پشت‌سرهم و هرکدام به‌صورت پیام جداگانه — نه اینکه هر پیام را ربات دیگری بفرستد.
        usable = sorted(
            (b for b in self.pool.bots if self._bot_usable(b, chat_id, members)),
            key=lambda b: b.index,
        )
        if not usable:
            self._defer(
                jobs,
                300,
                f"هیچ ربات سالمی برای مقصد {dest_peer} نیست (ربات‌ها عضو این گروه نیستند یا بیرون شده‌اند)",
            )
            log.warning("هیچ ربات قابل‌استفاده‌ای برای مقصد %s نیست.", dest_peer)
            return

        held: dict[int, dict[int, int]] = {}  # bot_index -> {پیام مبدا: شناسه در پی‌وی ربات}
        for mid, rows in holders.items():
            for bot_index, bot_mid in rows:
                held.setdefault(bot_index, {})[mid] = bot_mid

        # «ربات خانگی» هر کانال ثابت است (بر اساس شماره‌ی کانال)، تا پیام‌های یک کانال
        # همیشه در پی‌وی همان ربات جمع شوند و نوبت‌های بعدی بدون انتقال دوباره ارسال شوند.
        home = usable[source_id % len(usable)]
        free = [b for b in usable if self.pool.flood_remaining(b, chat_id) <= 0]
        if not free:
            wait = min(self.pool.flood_remaining(b, chat_id) for b in usable)
            self._defer(jobs, int(wait) + 1, f"همه‌ی ربات‌های مقصد {dest_peer} در محدودیت ارسال‌اند؛ کمی بعد")
            return
        home_wait = self.pool.flood_remaining(home, chat_id)
        if home in free:
            bot = home
        elif home_wait <= 120:
            # محدودیت کوتاه است؛ صبر می‌کنیم تا همان ربات خانگی بفرستد (بی‌جهت به ربات دیگر منتقل نمی‌کنیم)
            self._defer(jobs, int(home_wait) + 1, f"{home.label} چند ثانیه در محدودیت ارسال است؛ بعد همان می‌فرستد")
            return
        else:
            bot = max(free, key=lambda b: (len(held.get(b.index, {})), -b.index))

        mine = held.get(bot.index, {})
        items: list[tuple[object, int]] = []
        need_relay: list = []
        for job in jobs:
            mid = int(job["telegram_message_id"])
            if mid in mine:
                items.append((job, mine[mid]))
            else:
                need_relay.append(job)

        if need_relay:
            relay_ids = [int(j["telegram_message_id"]) for j in need_relay]
            mapping: dict[int, int] = {}
            try:
                for start in range(0, len(relay_ids), 100):
                    chunk = relay_ids[start : start + 100]
                    mapping.update(await self.pool.relay(self.client, source_peer, chunk, source_id, bot))
            except FloodWaitError as exc:
                wait_seconds = int(getattr(exc, "seconds", 60)) + 5
                self._group_flood_until[group_id] = now_ts() + wait_seconds
                log.warning("FloodWait اکانت هنگام انتقال پیام به ربات‌ها: %s ثانیه", wait_seconds)
            except ChatForwardsRestrictedError:
                self._fail_permanently(need_relay, f"مبدا {source_peer} اجازه‌ی فوروارد نمی‌دهد (محتوای محافظت‌شده)")
                need_relay = []
            except MessageIdInvalidError:
                if len(need_relay) == 1:
                    self._fail_permanently(need_relay, "آیدیِ پیامِ مبدا نامعتبره (احتمالاً حذف شده)")
                    need_relay = []
            except (ChannelPrivateError, ValueError) as exc:
                self._defer(need_relay, 600, f"اکانت به مبدا {source_peer} یا ربات {bot.label} دسترسی ندارد: {exc}")
                need_relay = []
            except RPCError as exc:
                log.warning("خطای تلگرام هنگام انتقال پیام به %s: %s", bot.label, exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("خطا هنگام انتقال پیام به %s: %s", bot.label, exc)

            unmapped = []
            for job in need_relay:
                mid = int(job["telegram_message_id"])
                if mid in mapping:
                    items.append((job, mapping[mid]))
                else:
                    unmapped.append(job)
            if unmapped:
                if items:
                    # بخشی از دسته منتقل نشد؛ برای اینکه همه با هم بروند، کل دسته
                    # کمی عقب می‌افتد (پیام‌های منتقل‌شده در پی‌وی ربات می‌مانند).
                    unmapped = unmapped + [job for job, _ in items]
                    items = []
                blocked_until = self._group_flood_until.get(group_id, 0)
                delay = max(60, blocked_until - now_ts())
                for job in unmapped:
                    self.db.finish_job(
                        int(job["id"]),
                        ok=False,
                        error="انتقال پیام به پی‌وی ربات انجام نشد؛ دوباره امتحان می‌شود",
                        run_after=now_ts() + delay,
                        max_attempts=self.settings.max_job_attempts * 3,
                    )

        # به ترتیب پیام‌های مبدا، همه با همان یک ربات
        items.sort(key=lambda it: int(it[0]["telegram_message_id"]))
        plan = {bot.index: items} if items else {}

        if plan:
            await asyncio.gather(
                *(
                    self._send_with_bot(self.pool.bot_by_index(bot_index), items, chat_id, source_id, dest_peer, group_id)
                    for bot_index, items in plan.items()
                )
            )

    async def _send_with_bot(self, bot: PoolBot, items: list, chat_id: int, source_id: int, dest_peer: str, group_id: int) -> None:
        assert self.pool is not None
        jobs = [job for job, _ in items]
        remaining = self.pool.flood_remaining(bot, chat_id)
        if remaining > 0:
            self._defer(jobs, int(remaining) + 1, f"{bot.label} در محدودیت ارسال است؛ ارسال کمی عقب افتاد")
            return
        try:
            await self.pool.forward_from_bot(bot, chat_id, [bot_mid for _, bot_mid in items])
            for job in jobs:
                self.db.finish_job(int(job["id"]), ok=True, max_attempts=self.settings.max_job_attempts)
            log.info("ارسال موفق با %s: گروه %s، %s پیام، مقصد %s", bot.label, group_id, len(jobs), dest_peer)
        except BotApiError as exc:
            bot.error_count += 1
            if exc.is_flood:
                wait_seconds = int(exc.retry_after or 30) + 1
                self.pool.set_flood(bot, chat_id, wait_seconds)
                self._defer(jobs, wait_seconds, f"{bot.label} محدودیت {wait_seconds} ثانیه گرفت؛ بعد از آن ادامه می‌دهد")
                log.warning("429 برای %s در مقصد %s: %s ثانیه", bot.label, dest_peer, wait_seconds)
            elif exc.is_unauthorized:
                bot.alive = False
                self._defer(jobs, 2, f"توکن {bot.label} باطل شده؛ ربات دیگری می‌فرستد")
                log.error("توکن %s باطل شده؛ از استخر خارج شد.", bot.label)
            elif exc.is_forbidden:
                self._bot_blocked[(bot.index, chat_id)] = now_ts() + 3600
                self._defer(jobs, 2, f"{bot.label} عضو مقصد {dest_peer} نیست یا اجازه‌ی ارسال ندارد؛ ربات دیگری می‌فرستد")
                log.warning("%s در مقصد %s اجازه‌ی ارسال ندارد (%s)؛ یک ساعت کنار گذاشته شد.", bot.label, dest_peer, exc.description)
            elif exc.is_message_gone:
                for job, _ in items:
                    self.db.delete_relay(source_id, int(job["telegram_message_id"]), bot.index)
                self._defer(jobs, 2, f"پیام در پی‌وی {bot.label} پیدا نشد؛ دوباره منتقل می‌شود")
            elif len(items) > 1:
                mid = len(items) // 2
                await self._send_with_bot(bot, items[:mid], chat_id, source_id, dest_peer, group_id)
                await self._send_with_bot(bot, items[mid:], chat_id, source_id, dest_peer, group_id)
            else:
                self.db.finish_job(
                    int(jobs[0]["id"]),
                    ok=False,
                    error=f"خطای ربات {bot.label} در ارسال به {dest_peer}: {exc}",
                    run_after=now_ts() + 120,
                    max_attempts=self.settings.max_job_attempts,
                )
                log.warning("خطای %s در ارسال به %s: %s", bot.label, dest_peer, exc)
        except Exception as exc:  # noqa: BLE001
            bot.error_count += 1
            self._defer(jobs, 30, f"خطای شبکه در ارسال با {bot.label}: {exc}")
            log.warning("خطای شبکه در ارسال با %s: %s", bot.label, exc)

    def _message_date_text(self, value) -> str:
        if value is None:
            return datetime.now(TEHRAN_TZ).isoformat(timespec="seconds")
        try:
            return value.astimezone(TEHRAN_TZ).isoformat(timespec="seconds")
        except Exception:
            return value.isoformat(timespec="seconds")

    def _local_date(self, value):
        if value is None:
            return datetime.now(TEHRAN_TZ).date()
        try:
            return value.astimezone(TEHRAN_TZ).date()
        except Exception:
            return value.date()

    def _is_sticker(self, message) -> bool:
        try:
            return message.sticker is not None
        except Exception:
            return False

    def _has_strike(self, message) -> bool:
        try:
            entities = message.entities or []
            return any(isinstance(e, MessageEntityStrike) for e in entities)
        except Exception:
            return False