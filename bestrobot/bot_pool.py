"""استخر ربات‌های ارسال (Bot Pool).

ایده: اکانت شخصی دیگر خودش در گروه‌های مقصد فوروارد نمی‌کند (همان کاری که
باعث اسپم/محدودیت اکانت می‌شد). به‌جایش:

  ۱) اکانت هر پیامِ مبدا فقط «یک بار» در پی‌وی یکی از ربات‌ها فوروارد می‌شود
     (relay). آن ربات از این به بعد «صاحب» آن پیام است.
  ۲) خودِ ربات (با Bot API) پیام را از پی‌وی خودش در گروه مقصد فوروارد می‌کند.
     چون واقعاً فوروارد است، بالای پیام همان «Forwarded from» مبدا اصلی می‌آید.
  ۳) برای ارسال‌های تکراری (حالت repeat) دیگر اکانت هیچ کاری نمی‌کند؛ ربات
     صاحب، همان پیامی که در پی‌وی‌اش دارد را دوباره فوروارد می‌کند.

ربات‌ها فقط باید عضو عادی گروه‌های مقصد باشند (ادمین لازم نیست).

نکته‌ی فنی: شناسه‌ی پیام در پی‌وی، برای اکانت و برای ربات فرق دارد. برای همین
هر ربات با getUpdates پیام‌های رسیده از اکانت را می‌گیرد و شناسه‌ی سمتِ خودش را
ثبت می‌کند. برای اینکه پیام‌ها قاطی نشوند، هر بار فقط یک relay روی هر ربات
انجام می‌شود (قفل جدا برای هر ربات).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from .db import Database


log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


def load_bot_tokens(path: Path) -> list[str]:
    """هر خط یک توکن. خط خالی و خط‌های شروع‌شده با # نادیده گرفته می‌شوند.
    توکن تکراری فقط یک بار حساب می‌شود."""
    if not path.exists():
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        # اجازه‌ی فرمت «نام=توکن» یا «توکن  # توضیح»
        if "=" in line and ":" in line.split("=", 1)[1]:
            line = line.split("=", 1)[1].strip()
        line = line.split()[0]
        if ":" not in line or line in seen:
            continue
        seen.add(line)
        tokens.append(line)
    return tokens


class BotApiError(Exception):
    def __init__(self, code: int, description: str, retry_after: int | None = None) -> None:
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description or ""
        self.retry_after = retry_after

    @property
    def is_flood(self) -> bool:
        return self.code == 429

    @property
    def is_forbidden(self) -> bool:
        """ربات از گروه بیرون شده/بن شده/اجازه‌ی ارسال ندارد یا گروه پیدا نشد."""
        d = self.description.lower()
        if self.code == 403:
            return True
        return self.code == 400 and (
            "chat not found" in d
            or "not enough rights" in d
            or "have no rights" in d
            or "chat_write_forbidden" in d
            or "bot was kicked" in d
            or "need administrator rights" in d
            or "chat_restricted" in d
        )

    @property
    def is_message_gone(self) -> bool:
        """پیامی که ربات در پی‌وی داشت دیگر قابل فوروارد نیست."""
        d = self.description.lower()
        return self.code == 400 and (
            "message to forward not found" in d
            or "messages to forward not found" in d
            or "message_id_invalid" in d
            or "message can't be forwarded" in d
        )

    @property
    def is_unauthorized(self) -> bool:
        return self.code == 401


@dataclass
class PoolBot:
    index: int
    token: str
    username: str = ""
    bot_id: int = 0
    alive: bool = True
    relay_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    # تا این زمان (unix) ربات برای این مقصد در محدودیت 429 است
    flood_until: dict[int, float] = field(default_factory=dict)
    sent_count: int = 0
    error_count: int = 0

    @property
    def label(self) -> str:
        return f"@{self.username}" if self.username else f"bot#{self.index}"


class BotPool:
    def __init__(self, tokens: list[str], db: Database, relay_delay_seconds: float = 1.0) -> None:
        self.db = db
        self.bots: list[PoolBot] = [PoolBot(index=i + 1, token=t) for i, t in enumerate(tokens)]
        self.relay_delay_seconds = relay_delay_seconds
        self.session: aiohttp.ClientSession | None = None
        self.account_id: int = 0
        self._poll_tasks: list[asyncio.Task] = []
        self._rr = itertools.count()
        # فقط یک relay همزمان از اکانت، با فاصله — تا اکانت رگباری نفرستد
        self._relay_gate = asyncio.Lock()
        self._last_relay_at = 0.0
        self._stopping = False

    # ------------------------------------------------------------------ setup

    @property
    def enabled(self) -> bool:
        return any(b.alive for b in self.bots)

    def bot_by_index(self, index: int) -> PoolBot | None:
        if 1 <= index <= len(self.bots):
            return self.bots[index - 1]
        return None

    async def start(self, account_id: int, poll: bool = True) -> None:
        self.account_id = int(account_id)
        timeout = aiohttp.ClientTimeout(total=70)
        self.session = aiohttp.ClientSession(timeout=timeout)

        async def init_one(bot: PoolBot) -> None:
            try:
                me = await self._call(bot, "getMe")
                bot.username = me.get("username") or ""
                bot.bot_id = int(me.get("id") or 0)
                # اگر قبلاً webhook داشته، getUpdates کار نمی‌کند
                await self._call(bot, "deleteWebhook", {"drop_pending_updates": False})
            except BotApiError as exc:
                bot.alive = False
                log.error("ربات شماره %s کار نمی‌کند و کنار گذاشته شد: %s", bot.index, exc)
            except Exception as exc:  # noqa: BLE001
                bot.alive = False
                log.error("ربات شماره %s وصل نشد و کنار گذاشته شد: %s", bot.index, exc)

        await asyncio.gather(*(init_one(b) for b in self.bots))
        alive = [b for b in self.bots if b.alive]
        log.info("استخر ربات‌ها: %s ربات سالم از %s.", len(alive), len(self.bots))
        if not poll:
            return
        for bot in alive:
            self._poll_tasks.append(asyncio.create_task(self._poll_loop(bot), name=f"poll-{bot.label}"))

    async def stop(self) -> None:
        self._stopping = True
        for task in self._poll_tasks:
            task.cancel()
        await asyncio.gather(*self._poll_tasks, return_exceptions=True)
        if self.session:
            await self.session.close()

    def usernames(self) -> list[str]:
        return [b.username for b in self.bots if b.alive and b.username]

    # --------------------------------------------------------------- bot api

    async def _call(self, bot: PoolBot, method: str, payload: dict | None = None, timeout: float | None = None) -> dict | list:
        assert self.session is not None
        url = f"{API_BASE}/bot{bot.token}/{method}"
        kwargs = {"json": payload or {}}
        if timeout:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        async with self.session.post(url, **kwargs) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                raise BotApiError(resp.status, await resp.text())
        if not data.get("ok"):
            params = data.get("parameters") or {}
            raise BotApiError(
                int(data.get("error_code") or resp.status),
                str(data.get("description") or ""),
                params.get("retry_after"),
            )
        return data.get("result")

    async def _poll_loop(self, bot: PoolBot) -> None:
        """پیام‌هایی که اکانت در پی‌وی این ربات فوروارد می‌کند را می‌گیرد."""
        offset: int | None = None
        # بک‌لاگ قدیمی را دور می‌ریزیم تا با relayهای جدید قاطی نشود
        try:
            last = await self._call(bot, "getUpdates", {"offset": -1, "timeout": 0})
            if last:
                offset = int(last[-1]["update_id"]) + 1
        except Exception:  # noqa: BLE001
            pass
        while not self._stopping:
            try:
                payload = {"timeout": 50, "allowed_updates": ["message"]}
                if offset is not None:
                    payload["offset"] = offset
                updates = await self._call(bot, "getUpdates", payload, timeout=65)
                for upd in updates or []:
                    offset = int(upd["update_id"]) + 1
                    msg = upd.get("message")
                    if not msg:
                        continue
                    sender = (msg.get("from") or {}).get("id")
                    if self.account_id and sender != self.account_id:
                        continue
                    text = msg.get("text") or ""
                    if text.startswith("/start"):
                        continue
                    await bot.inbox.put(msg)
            except asyncio.CancelledError:
                raise
            except BotApiError as exc:
                if exc.is_unauthorized:
                    bot.alive = False
                    log.error("توکن %s باطل شده؛ ربات از استخر خارج شد.", bot.label)
                    return
                if exc.code == 409:
                    log.error(
                        "%s جای دیگری هم getUpdates/webhook دارد (409). این ربات نباید همزمان در برنامه‌ی دیگری استفاده شود.",
                        bot.label,
                    )
                await asyncio.sleep(exc.retry_after or 5)
            except Exception as exc:  # noqa: BLE001
                log.warning("خطا در دریافت پیام‌های %s: %s", bot.label, exc)
                await asyncio.sleep(5)

    # ----------------------------------------------------------------- relay

    def pick_owner(self) -> PoolBot | None:
        alive = [b for b in self.bots if b.alive]
        if not alive:
            return None
        return alive[next(self._rr) % len(alive)]

    async def relay(self, client, source_peer: str, message_ids: list[int], source_id: int, bot: PoolBot) -> dict[int, int]:
        """اکانت پیام‌ها را در پی‌وی «bot» فوروارد می‌کند و شناسه‌ی سمت ربات را
        برمی‌گرداند: {telegram_message_id مبدا: message_id در پی‌وی ربات}.
        FloodWait اکانت به بالا پرتاب می‌شود تا موتور مثل قبل مدیریتش کند."""
        if not message_ids:
            return {}
        message_ids = sorted(set(message_ids))
        async with bot.relay_lock:
            # باقی‌مانده‌ی relay قبلی (اگر دیر رسیده) نباید با این یکی قاطی شود
            while not bot.inbox.empty():
                bot.inbox.get_nowait()

            async with self._relay_gate:
                wait = self.relay_delay_seconds - (time.monotonic() - self._last_relay_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                result = await client.forward_messages(entity=bot.username, messages=message_ids, from_peer=source_peer)
                self._last_relay_at = time.monotonic()

            if not isinstance(result, list):
                result = [result]
            # پیام‌هایی که واقعاً فوروارد شدند (None یعنی آن یکی فوروارد نشد)
            delivered = [mid for mid, res in zip(message_ids, result) if res is not None]
            if not delivered:
                return {}

            received: list[dict] = []
            deadline = time.monotonic() + 30
            while len(received) < len(delivered):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    received.append(await asyncio.wait_for(bot.inbox.get(), timeout=remaining))
                except asyncio.TimeoutError:
                    break

            if len(received) != len(delivered):
                log.warning(
                    "relay به %s: %s پیام فرستاده شد ولی %s تا رسید؛ این دسته دوباره امتحان می‌شود.",
                    bot.label,
                    len(delivered),
                    len(received),
                )
                return {}

            received.sort(key=lambda m: int(m["message_id"]))
            mapping = {src: int(m["message_id"]) for src, m in zip(delivered, received)}
            for src, bot_mid in mapping.items():
                self.db.save_relay(source_id, src, bot.index, bot_mid)
            return mapping

    # ------------------------------------------------------------------ send

    async def forward_from_bot(self, bot: PoolBot, chat_id: int, bot_message_ids: list[int]) -> None:
        """ربات پیام‌های پی‌وی خودش را در گروه مقصد فوروارد می‌کند (حداکثر ۱۰۰ تا در هر درخواست)."""
        ids = sorted(set(bot_message_ids))
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            if len(chunk) == 1:
                await self._call(
                    bot,
                    "forwardMessage",
                    {"chat_id": chat_id, "from_chat_id": self.account_id, "message_id": chunk[0]},
                )
            else:
                await self._call(
                    bot,
                    "forwardMessages",
                    {"chat_id": chat_id, "from_chat_id": self.account_id, "message_ids": chunk},
                )
            bot.sent_count += len(chunk)

    def flood_remaining(self, bot: PoolBot, chat_id: int) -> float:
        until = bot.flood_until.get(chat_id, 0)
        return max(0.0, until - time.time())

    def set_flood(self, bot: PoolBot, chat_id: int, seconds: int) -> None:
        bot.flood_until[chat_id] = time.time() + max(1, seconds)

    async def send_start_to_all(self, client) -> tuple[int, int]:
        """اکانت یک بار به همه‌ی ربات‌ها /start می‌دهد (ربات تا کاربر شروعش نکند
        پیامی از او نمی‌گیرد)."""
        ok = fail = 0
        for bot in self.bots:
            if not bot.alive or not bot.username:
                continue
            try:
                await client.send_message(bot.username, "/start")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                fail += 1
                log.warning("ارسال /start به %s نشد: %s", bot.label, exc)
            await asyncio.sleep(1.5)
        return ok, fail

    async def is_member(self, bot: PoolBot, chat_id: int | str) -> bool | None:
        """خود ربات با Bot API چک می‌کند عضو این گروه هست و اجازه‌ی ارسال دارد یا نه.
        None یعنی گروه برای ربات قابل دیدن نیست (عضو نیست یا یوزرنیم اشتباه است)."""
        try:
            member = await self._call(bot, "getChatMember", {"chat_id": chat_id, "user_id": bot.bot_id})
        except BotApiError:
            return None
        status = member.get("status")
        if status in ("administrator", "creator", "member"):
            return True
        if status == "restricted":
            return bool(member.get("is_member")) and member.get("can_send_messages", True) is not False
        return False

    def status_lines(self) -> list[str]:
        lines = []
        for bot in self.bots:
            state = "✅" if bot.alive else "❌"
            lines.append(f"{state} {bot.index}. {bot.label} — ارسال: {bot.sent_count}، خطا: {bot.error_count}")
        return lines

