from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError

from .config import Settings
from .db import Database, now_ts


log = logging.getLogger(__name__)


class ForwardEngine:
    def __init__(self, client: TelegramClient, db: Database, settings: Settings) -> None:
        self.client = client
        self.db = db
        self.settings = settings
        self.stop_event = asyncio.Event()
        self.send_semaphore = asyncio.Semaphore(settings.max_parallel_sends)
        self.tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        recovered = self.db.recover_running_jobs()
        if recovered:
            log.warning("تعداد %s ارسال نیمه‌کاره دوباره به صف برگشت.", recovered)
        self.tasks = [
            asyncio.create_task(self.scan_sources_loop(), name="scan-sources"),
            asyncio.create_task(self.repeat_scheduler_loop(), name="repeat-scheduler"),
            asyncio.create_task(self.scheduled_starts_loop(), name="scheduled-starts"),
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
                date_text = self._message_date_text(message.date)
                self.db.remember_message(group_id, source_id, int(message.id), date_text)
                cycle_key = "once" if group["mode"] == "once" else "initial"
                enqueued = self.db.enqueue_message(group_id, source_id, int(message.id), cycle_key)
                if enqueued:
                    log.info("پیام %s از %s برای گروه %s وارد صف شد.", message.id, peer, group_id)

            if max_seen > last_id:
                self.db.set_last_message_id(group_id, source_id, max_seen)
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
        today = datetime.now().date()
        cycle_key = "once" if group["mode"] == "once" else "initial"

        collected = []
        async for message in self.client.iter_messages(peer, limit=day_message_limit):
            if not message or not message.id:
                continue
            if self._local_date(message.date) != today:
                break
            collected.append(message)

        if not collected:
            latest = await self.client.get_messages(peer, limit=1)
            if latest:
                message = latest[0]
                message_id = int(message.id)
                date_text = self._message_date_text(message.date)
                self.db.remember_message(group_id, source_id, message_id, date_text)
                self.db.set_last_message_id(group_id, source_id, message_id)
                log.info("گروه %s / مبدا %s امروز پستی نداشت؛ از پیام %s به بعد ادامه می‌شود.", group_id, peer, message_id)
            return

        max_seen = 0
        for message in reversed(collected):
            message_id = int(message.id)
            date_text = self._message_date_text(message.date)
            self.db.remember_message(group_id, source_id, message_id, date_text)
            self.db.enqueue_message(group_id, source_id, message_id, cycle_key)
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
        today = datetime.now().date().isoformat()
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

    async def worker_loop(self, worker_name: str) -> None:
        while not self.stop_event.is_set():
            job = self.db.claim_next_job(worker_name)
            if not job:
                await asyncio.sleep(1)
                continue
            async with self.send_semaphore:
                await self.send_job(job)
            await asyncio.sleep(self.settings.min_send_delay_seconds)

    async def send_job(self, job) -> None:
        group_id = int(job["group_id"])
        group = self.db.get_group(group_id)
        if not group or not group["enabled"]:
            self.db.finish_job(
                int(job["id"]),
                ok=False,
                error="گروه خاموش است؛ ارسال فعلا عقب افتاد",
                run_after=now_ts() + 60,
                max_attempts=999999,
            )
            return

        source = self.db.fetchone("SELECT * FROM entities WHERE id=?", (int(job["source_id"]),))
        destination = self.db.fetchone("SELECT * FROM entities WHERE id=?", (int(job["destination_id"]),))
        if not source or not destination:
            self.db.finish_job(int(job["id"]), ok=False, error="مبدا یا مقصد حذف شده است", max_attempts=1)
            return

        try:
            await self.client.forward_messages(
                entity=str(destination["peer"]),
                messages=int(job["telegram_message_id"]),
                from_peer=str(source["peer"]),
            )
            self.db.finish_job(int(job["id"]), ok=True, max_attempts=self.settings.max_job_attempts)
            log.info(
                "ارسال موفق: گروه %s، پیام %s، مقصد %s",
                group_id,
                job["telegram_message_id"],
                destination["peer"],
            )
        except FloodWaitError as exc:
            wait_seconds = int(getattr(exc, "seconds", 60)) + 5
            self.db.finish_job(
                int(job["id"]),
                ok=False,
                error=f"تلگرام محدودیت داد؛ {wait_seconds} ثانیه صبر",
                run_after=now_ts() + wait_seconds,
                max_attempts=999999,
            )
            log.warning("FloodWait برای گروه %s: %s ثانیه", group_id, wait_seconds)
        except RPCError as exc:
            self.db.finish_job(
                int(job["id"]),
                ok=False,
                error=f"خطای تلگرام در ارسال به {destination['peer']}: {exc.__class__.__name__}",
                run_after=now_ts() + 120,
                max_attempts=self.settings.max_job_attempts,
            )
            log.warning("خطای تلگرام در ارسال job %s: %s", job["id"], exc)
        except Exception as exc:
            self.db.finish_job(
                int(job["id"]),
                ok=False,
                error=f"خطای ارسال به {destination['peer']}: {exc}",
                run_after=now_ts() + 120,
                max_attempts=self.settings.max_job_attempts,
            )
            log.exception("خطای ارسال job %s", job["id"])

    def _message_date_text(self, value) -> str:
        if value is None:
            return datetime.now().isoformat(timespec="seconds")
        try:
            return value.astimezone().isoformat(timespec="seconds")
        except Exception:
            return value.isoformat(timespec="seconds")

    def _local_date(self, value):
        if value is None:
            return datetime.now().date()
        try:
            return value.astimezone().date()
        except Exception:
            return value.date()