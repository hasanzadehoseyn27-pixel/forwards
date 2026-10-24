"""پشتیبانی روبیکا — دقیقاً همان روش تلگرام:

  ۱) اکانت روبیکا (با rubpy) پیام‌های کانال‌های مبدا را می‌خواند و هر پیام را
     فقط یک بار در پی‌وی یکی از ربات‌ها فوروارد می‌کند (relay).
  ۲) ربات (Bot API v3 روبیکا) همان پیام را از پی‌وی خودش در گروه مقصد
     فوروارد می‌کند؛ پس بالای پیام همان «فورواردشده از» کانال اصلی می‌آید.
  ۳) در حالت تکرار، اکانت هیچ کاری نمی‌کند؛ ربات صاحب، دوباره فوروارد می‌کند.

فرق با تلگرام: در روبیکا ربات فقط وقتی می‌تواند در گروه پیام بفرستد/فوروارد
کند که «ادمین» گروه باشد (روی سرور تست شد: عضو عادی ← INVALID_ACCESS).

این فایل دو کلاس دارد که جای Telethon و BotPool را می‌گیرند تا موتور و پنل
بدون تغییرِ منطق، روی روبیکا هم کار کنند:
  • RubikaAccount — همان متدهایی از TelegramClient که موتور/پنل صدا می‌زنند
  • RubikaBotPool — همان رابط BotPool با Bot API روبیکا
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import aiohttp

from .bot_pool import BotApiError, BotPool, PoolBot, _tls_context
from .db import Database

log = logging.getLogger(__name__)

RUBIKA_BOT_API = "https://botapi.rubika.ir/v3"
# بزرگ‌تر از هر شناسه‌ی پیام روبیکا (حدود 1.95e18) و کوچک‌تر از سقف int64
_TOP_MESSAGE_ID = "9000000000000000000"
_GUID_RE = re.compile(r"^[cgub]0[A-Za-z0-9]{20,}$")
_FORWARD_CHUNK = 50


def is_guid(value: str) -> bool:
    return bool(_GUID_RE.match(value.strip()))


def load_rubika_tokens(path: Path) -> list[str]:
    """هر خط یک توکن ربات روبیکا (بدون «:»). خط خالی و # نادیده گرفته می‌شود.
    فرمت «نام=توکن» و «توکن  # توضیح» هم قبول است."""
    if not path.exists():
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            line = line.split("=", 1)[1].strip()
        if not line:
            continue
        line = line.split()[0]
        if len(line) < 20 or line in seen:
            continue
        seen.add(line)
        tokens.append(line)
    return tokens


# ======================================================================
#                              اکانت روبیکا
# ======================================================================


@dataclass
class RubikaMessage:
    """شبیه پیام Telethon، فقط فیلدهایی که موتور لازم دارد."""

    id: int
    date: datetime | None
    text: str
    sticker: object | None
    has_strike: bool
    raw: dict

    @property
    def entities(self) -> list:
        return []


def parse_message(raw: dict) -> RubikaMessage | None:
    try:
        mid = int(raw.get("message_id"))
    except (TypeError, ValueError):
        return None
    date = None
    try:
        ts = int(raw.get("time") or 0)
        if ts:
            date = datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError):
        date = None
    mtype = str(raw.get("type") or "")
    sticker = raw.get("sticker")
    if sticker is None and mtype == "Sticker":
        sticker = True
    # پیام‌های سیستمی (ساخت کانال، پین و ...) قابل فوروارد نیستند؛ مثل استیکر رد می‌شوند
    if sticker is None and (mtype == "Event" or raw.get("event_data")):
        sticker = "event"
    parts = ((raw.get("metadata") or {}).get("meta_data_parts")) or []
    has_strike = any(str(p.get("type") or "").lower() == "strike" for p in parts if isinstance(p, dict))
    return RubikaMessage(
        id=mid,
        date=date,
        text=str(raw.get("text") or ""),
        sticker=sticker,
        has_strike=has_strike,
        raw=raw,
    )


def _flood_error(seconds: int = 60):
    from telethon.errors import FloodWaitError

    return FloodWaitError(request=None, capture=seconds)


def _find(obj, keys: list[str]):
    """اولین مقدارِ یکی از کلیدها، در هر عمقی از dict/list."""
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]
        values = obj.values()
    elif isinstance(obj, list):
        values = obj
    else:
        return None
    for value in values:
        if isinstance(value, (dict, list)):
            found = _find(value, keys)
            if found not in (None, ""):
                return found
    return None


class RubikaAccount:
    """اکانت روبیکا با همان متدهایی که موتور و پنل از TelegramClient صدا می‌زنند."""

    def __init__(self, session_path: Path) -> None:
        from rubpy import Client

        self.session_path = session_path
        self.client = Client(name=str(session_path))
        self.guid: str = ""
        self._peer_cache: dict[str, str] = {}
        self._title_cache: dict[str, str] = {}

    # ------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        """بدون هیچ سؤالی (برای PM2): اگر سشن نباشد خطا می‌دهد."""
        from Crypto.PublicKey import RSA
        from Crypto.Signature import pkcs1_15
        from rubpy import exceptions
        from rubpy.crypto import Crypto

        c = self.client
        await c.connect()
        if not c.auth or not c.private_key:
            raise RuntimeError("سشن روبیکا آماده نیست. اول اجرا کن: python -m bestrobot login")
        c.decode_auth = Crypto.decode_auth(c.auth)
        c.import_key = pkcs1_15.new(RSA.import_key(c.private_key.encode()))
        try:
            me = await c.builder("getUserInfo", input={}, dict=True)
        except exceptions.NotRegistered as exc:
            raise RuntimeError("سشن روبیکا باطل شده. دوباره اجرا کن: python -m bestrobot login") from exc
        self.guid = str(_find(me, ["user_guid"]) or c.guid or "")
        c.guid = self.guid

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def _call(self, method: str, data: dict) -> dict:
        from rubpy import exceptions

        try:
            result = await self.client.builder(method, input=data, dict=True)
        except exceptions.TooRequests as exc:
            log.warning("روبیکا برای %s خطای TOO_REQUESTS داد: %s", method, getattr(exc, "dev_message", "") or exc)
            raise _flood_error(60) from exc
        return result or {}

    async def get_me(self):
        info = await self._call("getUserInfo", {})
        user = info.get("user") or {}
        guid = str(user.get("user_guid") or self.guid)
        self.guid = guid
        return SimpleNamespace(
            id=guid,
            username=user.get("username") or "",
            first_name=user.get("first_name") or "",
            last_name=user.get("last_name") or "",
        )

    # ---------------------------------------------------------------- peers

    async def get_peer_id(self, peer: str) -> str:
        """@username / username / https://rubika.ir/username /
        https://rubika.ir/joing/HASH / joinc/HASH / guid ← guid"""
        raw = str(peer).strip()
        if not raw:
            raise ValueError("آدرس خالی است")
        if is_guid(raw):
            return raw
        cached = self._peer_cache.get(raw)
        if cached:
            return cached

        guid = ""
        title = ""
        link = re.search(r"rubika\.ir/(joing|joinc)/([A-Za-z0-9]+)", raw, re.IGNORECASE)
        if link:
            kind, hash_link = link.group(1).lower(), link.group(2)
            if kind == "joing":
                res = await self._call("groupPreviewByJoinLink", {"hash_link": hash_link})
                guid = str(_find(res, ["group_guid"]) or "")
                title = str(_find(res, ["group_title"]) or "")
            else:
                res = await self._call("channelPreviewByJoinLink", {"hash_link": hash_link})
                guid = str(_find(res, ["channel_guid"]) or "")
                title = str(_find(res, ["channel_title"]) or "")
            if not guid:
                raise ValueError(f"لینک {raw} معتبر نیست یا منقضی شده")
        else:
            username = raw
            m = re.search(r"rubika\.ir/([A-Za-z0-9_]+)", raw, re.IGNORECASE)
            if m:
                username = m.group(1)
            username = username.lstrip("@").strip("/")
            res = await self._call("getObjectByUsername", {"username": username})
            if res.get("exist") is False:
                # همان متن خطای Telethon تا موتور مبدا را خودکار خاموش کند
                raise ValueError(f'No user has "{username}" as username')
            guid = str(_find(res, ["channel_guid", "group_guid", "bot_guid", "user_guid", "object_guid"]) or "")
            title = str(_find(res, ["channel_title", "group_title", "bot_title", "first_name"]) or "")
            if not guid:
                raise ValueError(f'No user has "{username}" as username')
        self._peer_cache[raw] = guid
        if title:
            self._title_cache[guid] = title
        return guid

    async def get_entity(self, peer: str):
        guid = await self.get_peer_id(peer)
        title = self._title_cache.get(guid, "")
        if not title:
            try:
                info = await self._info(guid)
                title = str(_find(info, ["channel_title", "group_title", "bot_title", "title", "first_name"]) or "")
            except Exception:  # noqa: BLE001
                title = ""
        return SimpleNamespace(id=guid, title=title or None, first_name="", last_name="")

    async def _info(self, guid: str) -> dict:
        if guid.startswith("c0"):
            return await self._call("getChannelInfo", {"channel_guid": guid})
        if guid.startswith("g0"):
            return await self._call("getGroupInfo", {"group_guid": guid})
        if guid.startswith("b0"):
            return await self._call("getBotInfo", {"bot_guid": guid})
        return await self._call("getUserInfo", {"user_guid": guid})

    # ------------------------------------------------------------- messages

    async def _page(self, guid: str, sort: str, anchor: str, limit: int) -> tuple[list[dict], dict]:
        data = {"object_guid": guid, "sort": sort, "limit": str(limit)}
        if sort == "FromMin":
            data["min_id"] = anchor
        else:
            data["max_id"] = anchor
        res = await self._call("getMessages", data)
        msgs = res.get("messages")
        if msgs is None:
            msgs = _find(res, ["messages"]) or []
        return [m for m in msgs if isinstance(m, dict)], res

    async def _top_anchor(self, guid: str) -> str:
        """max_id برای گرفتن جدیدترین پیام‌ها: آخرین پیام چت + ۱ (اگر سرور بدهد)."""
        try:
            info = await self._info(guid)
            last = _find(info, ["last_message_id"])
            if last:
                return str(int(last) + 1)
        except Exception:  # noqa: BLE001
            pass
        return _TOP_MESSAGE_ID

    async def _latest_desc(self, guid: str, limit: int) -> list[RubikaMessage]:
        """جدیدترین پیام‌ها، از جدید به قدیم."""
        out: list[RubikaMessage] = []
        anchor = await self._top_anchor(guid)
        seen: set[int] = set()
        for _ in range(20):
            batch, _res = await self._page(guid, "FromMax", anchor, min(50, max(2, limit - len(out) + 1)))
            msgs = [m for m in (parse_message(b) for b in batch) if m and m.id not in seen]
            if not msgs:
                break
            msgs.sort(key=lambda m: m.id, reverse=True)
            for m in msgs:
                seen.add(m.id)
                out.append(m)
                if len(out) >= limit:
                    return out
            # همان کمترین شناسه (نه یکی کمتر)؛ چه سرور max_id را شامل کند چه نه، چیزی جا نمی‌ماند
            anchor = str(msgs[-1].id)
        return out

    async def _newer_than(self, guid: str, min_id: int) -> list[RubikaMessage]:
        """پیام‌های جدیدتر از min_id، از قدیم به جدید (حداکثر ۵۰۰ تا)."""
        # از بالا (جدیدترین) به پایین ورق می‌زنیم تا به min_id برسیم — مستقل از
        # این‌که سرور sort=FromMin را چطور تفسیر کند.
        out: dict[int, RubikaMessage] = {}
        anchor = await self._top_anchor(guid)
        for _ in range(10):
            batch, _res = await self._page(guid, "FromMax", anchor, 50)
            msgs = [m for m in (parse_message(b) for b in batch) if m]
            if not msgs:
                break
            reached = False
            for m in msgs:
                if m.id > min_id:
                    out[m.id] = m
                else:
                    reached = True
            lowest = min(m.id for m in msgs)
            if reached or lowest <= min_id:
                break
            anchor = str(lowest)
        return [out[k] for k in sorted(out)]

    async def iter_messages(self, peer: str, limit: int | None = None, min_id: int | None = None, reverse: bool = False):
        guid = await self.get_peer_id(peer)
        if min_id is not None:
            msgs = await self._newer_than(guid, int(min_id))
            if not reverse:
                msgs = list(reversed(msgs))
            if limit:
                msgs = msgs[:limit]
        else:
            msgs = await self._latest_desc(guid, int(limit or 100))
            if reverse:
                msgs = list(reversed(msgs))
        for m in msgs:
            yield m

    async def get_messages(self, peer: str, limit: int = 1) -> list[RubikaMessage]:
        guid = await self.get_peer_id(peer)
        return await self._latest_desc(guid, int(limit))

    async def forward_messages(self, entity: str, messages, from_peer: str):
        """مثل Telethon: برای هر پیام یک مقدار (None یعنی فوروارد نشد)."""
        ids = messages if isinstance(messages, list) else [messages]
        to_guid = await self.get_peer_id(str(entity))
        from_guid = await self.get_peer_id(str(from_peer))
        results: list = []
        for start in range(0, len(ids), _FORWARD_CHUNK):
            chunk = [str(int(i)) for i in ids[start : start + _FORWARD_CHUNK]]
            await self._call(
                "forwardMessages",
                {
                    "from_object_guid": from_guid,
                    "to_object_guid": to_guid,
                    "message_ids": chunk,
                    "rnd": int(time.time() * 1000) % 10_000_000 + 1,
                },
            )
            results.extend([True] * len(chunk))
        return results if isinstance(messages, list) else results[0]

    async def send_message(self, peer: str, text: str):
        guid = await self.get_peer_id(str(peer))
        return await self._call(
            "sendMessage",
            {"object_guid": guid, "text": text, "rnd": int(time.time() * 1000) % 10_000_000 + 1},
        )


# ======================================================================
#                           استخر ربات‌های روبیکا
# ======================================================================


@dataclass
class RubikaPoolBot(PoolBot):
    bot_guid: str = ""
    # شناسه‌ی چتِ پی‌وی اکانت از دید ربات (با bot_guid فرق دارد؛ از آپدیت‌ها یاد گرفته می‌شود)
    pm_chat_id: str = ""
    offset_id: str | None = None
    seen_ids: set = field(default_factory=set)


class RubikaBotPool(BotPool):
    def __init__(
        self,
        tokens: list[str],
        db: Database,
        relay_delay_seconds: float = 1.0,
        state_path: Path | None = None,
        poll_seconds: float = 1.5,
    ) -> None:
        super().__init__([], db, relay_delay_seconds=relay_delay_seconds)
        self.bots = [RubikaPoolBot(index=i + 1, token=t) for i, t in enumerate(tokens)]
        self.account_guid = ""
        self.state_path = state_path
        self.poll_seconds = poll_seconds
        self._state: dict = self._load_state()

    # ---------------------------------------------------------------- state

    def _load_state(self) -> dict:
        if not self.state_path or not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception:  # noqa: BLE001
            log.exception("ذخیره‌ی وضعیت ربات‌های روبیکا نشد")

    def _remember_pm(self, bot: RubikaPoolBot, chat_id: str) -> None:
        if not chat_id or bot.pm_chat_id == chat_id:
            return
        bot.pm_chat_id = chat_id
        self._state.setdefault("pm", {})[bot.bot_guid or str(bot.index)] = chat_id
        self._save_state()

    # ---------------------------------------------------------------- setup

    async def start(self, account_id, poll: bool = True) -> None:  # type: ignore[override]
        self.account_guid = str(account_id or "")
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=40),
            connector=aiohttp.TCPConnector(ssl=_tls_context()),
        )
        pms = self._state.get("pm") or {}

        async def init_one(bot: RubikaPoolBot) -> None:
            try:
                me = await self._call(bot, "getMe")
                info = me.get("bot") if isinstance(me, dict) else None
                info = info or me or {}
                bot.username = str(info.get("username") or "")
                bot.bot_guid = str(info.get("bot_id") or info.get("bot_guid") or "")
                bot.pm_chat_id = str(pms.get(bot.bot_guid) or "")
            except BotApiError as exc:
                bot.alive = False
                log.error("ربات روبیکای شماره %s کار نمی‌کند و کنار گذاشته شد: %s", bot.index, exc)
            except Exception as exc:  # noqa: BLE001
                bot.alive = False
                log.error("ربات روبیکای شماره %s وصل نشد و کنار گذاشته شد: %s", bot.index, exc)

        await asyncio.gather(*(init_one(b) for b in self.bots))
        alive = [b for b in self.bots if b.alive]
        log.info("استخر ربات‌های روبیکا: %s ربات سالم از %s.", len(alive), len(self.bots))
        if not poll:
            return
        for bot in alive:
            self._poll_tasks.append(asyncio.create_task(self._poll_loop(bot), name=f"rb-poll-{bot.label}"))

    # -------------------------------------------------------------- bot api

    async def _call(self, bot: PoolBot, method: str, payload: dict | None = None, timeout: float | None = None):
        assert self.session is not None
        url = f"{RUBIKA_BOT_API}/{bot.token}/{method}"
        kwargs: dict = {"json": payload or {}}
        if timeout:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        async with self.session.post(url, **kwargs) as resp:
            if resp.status == 429:
                raise BotApiError(429, "TOO_REQUESTS", 30)
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                raise BotApiError(resp.status or 500, (await resp.text())[:300])
        status = str((data or {}).get("status") or "")
        if status == "OK":
            return data.get("data") or {}
        detail = str(data.get("dev_message") or data.get("status_det") or status)
        if status in ("TOO_REQUESTS", "TOO_REQUEST", "TooRequests"):
            raise BotApiError(429, detail, 30)
        if status == "INVALID_ACCESS":
            if method == "getMe":
                raise BotApiError(401, f"توکن نامعتبر ({detail})")
            # ربات ادمین گروه نیست / از گروه بیرون شده
            raise BotApiError(403, f"INVALID_ACCESS — ربات باید ادمین گروه باشد ({detail})")
        if status == "INVALID_INPUT":
            if method == "forwardMessage":
                raise BotApiError(400, f"message to forward not found (INVALID_INPUT: {detail})")
            raise BotApiError(400, f"INVALID_INPUT: {detail}")
        raise BotApiError(500, f"{status}: {detail}")

    async def _fetch_updates(self, bot: RubikaPoolBot) -> list[dict]:
        payload: dict = {"limit": 100}
        if bot.offset_id:
            payload["offset_id"] = bot.offset_id
        data = await self._call(bot, "getUpdates", payload, timeout=30)
        nxt = data.get("next_offset_id") if isinstance(data, dict) else None
        if nxt:
            bot.offset_id = str(nxt)
        updates = data.get("updates") if isinstance(data, dict) else None
        return [u for u in (updates or []) if isinstance(u, dict)]

    async def _drain_backlog(self, bot: RubikaPoolBot) -> None:
        """آپدیت‌های قدیمی دور ریخته می‌شوند (ولی شناسه‌ی پی‌وی اکانت یاد گرفته می‌شود)."""
        for _ in range(200):
            before = bot.offset_id
            updates = await self._fetch_updates(bot)
            for upd in updates:
                self._learn(bot, upd)
                mid = _find(upd, ["message_id"])
                if mid:
                    bot.seen_ids.add(str(mid))
            if not updates or bot.offset_id == before:
                return

    def _learn(self, bot: RubikaPoolBot, upd: dict) -> dict | None:
        """اگر آپدیت پیامِ اکانت در پی‌وی ربات است، خود پیام را برمی‌گرداند."""
        if upd.get("type") != "NewMessage":
            if upd.get("type") == "EventData":
                ev = upd.get("event_data") or {}
                log.info("%s در %s: %s (%s)", bot.label, upd.get("chat_id"), ev.get("type"), ev.get("join_type") or "")
            return None
        msg = upd.get("new_message") or {}
        chat_id = str(upd.get("chat_id") or "")
        if not chat_id.startswith("b0"):
            return None
        # نکته: در Bot API روبیکا sender_id اکانت از دید هر ربات فرق دارد و با
        # guid خود اکانت (u0...) یکی نیست؛ پس با فرستنده فیلتر نمی‌کنیم. فقط
        # پیام‌های «فورواردشده» relay حساب می‌شوند (کاری که فقط اکانت می‌کند).
        if not msg.get("forwarded_from"):
            return None
        self._remember_pm(bot, chat_id)
        return msg

    async def _poll_loop(self, bot: PoolBot) -> None:  # type: ignore[override]
        assert isinstance(bot, RubikaPoolBot)
        try:
            await self._drain_backlog(bot)
        except Exception as exc:  # noqa: BLE001
            log.warning("خالی‌کردن صف قدیمی %s نشد: %s", bot.label, exc)
        while not self._stopping:
            try:
                updates = await self._fetch_updates(bot)
                for upd in updates:
                    msg = self._learn(bot, upd)
                    if not msg:
                        continue
                    mid = str(msg.get("message_id") or "")
                    if not mid or mid in bot.seen_ids:
                        continue
                    bot.seen_ids.add(mid)
                    if len(bot.seen_ids) > 5000:
                        bot.seen_ids = set(list(bot.seen_ids)[-2000:])
                    if str(msg.get("text") or "").startswith("/start"):
                        continue
                    await bot.inbox.put(msg)
                await asyncio.sleep(self.poll_seconds)
            except asyncio.CancelledError:
                raise
            except BotApiError as exc:
                if exc.is_unauthorized:
                    bot.alive = False
                    log.error("توکن %s باطل شده؛ ربات از استخر خارج شد.", bot.label)
                    return
                await asyncio.sleep(exc.retry_after or 5)
            except Exception as exc:  # noqa: BLE001
                log.warning("خطا در دریافت پیام‌های %s: %s", bot.label, exc)
                await asyncio.sleep(5)

    # ---------------------------------------------------------------- relay

    async def relay(self, client, source_peer: str, message_ids: list[int], source_id: int, bot: PoolBot) -> dict[int, int]:
        assert isinstance(bot, RubikaPoolBot)
        if not message_ids or not (bot.username or bot.bot_guid):
            return {}
        message_ids = sorted(set(int(m) for m in message_ids))
        async with bot.relay_lock:
            while not bot.inbox.empty():
                bot.inbox.get_nowait()
            async with self._relay_gate:
                wait = self.relay_delay_seconds - (time.monotonic() - self._last_relay_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                # نکته: bot_id که Bot API می‌دهد از دید اکانت معتبر نیست (guidها برای هر
                # طرف فرق دارند)؛ اکانت ربات را با یوزرنیمش پیدا می‌کند.
                target = "@" + bot.username if bot.username else bot.bot_guid
                result = await client.forward_messages(entity=target, messages=message_ids, from_peer=source_peer)
                self._last_relay_at = time.monotonic()
            if not isinstance(result, list):
                result = [result]
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

            mapping = match_relayed(delivered, received)
            if len(mapping) != len(delivered):
                log.warning(
                    "relay روبیکا به %s: %s پیام فرستاده شد ولی %s تا شناسایی شد؛ بقیه دوباره امتحان می‌شوند.",
                    bot.label,
                    len(delivered),
                    len(mapping),
                )
            for src, bot_mid in mapping.items():
                self.db.save_relay(source_id, src, bot.index, bot_mid)
            return mapping

    # ----------------------------------------------------------------- send

    async def forward_from_bot(self, bot: PoolBot, chat_id, bot_message_ids: list[int]) -> None:  # type: ignore[override]
        assert isinstance(bot, RubikaPoolBot)
        if not bot.pm_chat_id:
            # هنوز نمی‌دانیم پی‌وی اکانت از دید این ربات چه شناسه‌ای دارد ← دوباره relay
            raise BotApiError(400, "message to forward not found (پی‌وی اکانت برای این ربات هنوز شناخته نشده)")
        for n, mid in enumerate(bot_message_ids):
            if n:
                await asyncio.sleep(0.4)
            await self._call(
                bot,
                "forwardMessage",
                {"from_chat_id": bot.pm_chat_id, "message_id": str(mid), "to_chat_id": str(chat_id)},
            )
            bot.sent_count += 1

    async def send_start_to_all(self, client) -> tuple[int, int]:
        ok = fail = 0
        for bot in self.bots:
            if not bot.alive or not bot.username:
                continue
            try:
                await client.send_message("@" + bot.username, "/start")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                fail += 1
                log.warning("ارسال /start به %s نشد: %s", bot.label, exc)
            await asyncio.sleep(1.5)
        return ok, fail

    async def is_member(self, bot: PoolBot, chat_id) -> bool | None:
        # Bot API روبیکا متدی برای چک عضویت ندارد
        return None

    async def resolve_chat_id(self, chat):
        return None

    def status_lines(self) -> list[str]:
        lines = []
        for bot in self.bots:
            state = "✅" if bot.alive else "❌"
            pm = "پی‌وی شناخته‌شده" if getattr(bot, "pm_chat_id", "") else "پی‌وی هنوز ناشناخته"
            lines.append(f"{state} {bot.index}. {bot.label} — {pm} — ارسال: {bot.sent_count}، خطا: {bot.error_count}")
        return lines


def match_relayed(delivered: list[int], received: list[dict]) -> dict[int, int]:
    """پیام‌های رسیده به پی‌وی ربات را به پیام‌های مبدا وصل می‌کند.
    اول دقیق، با forwarded_from.message_id (= شناسه‌ی پیام در کانال مبدا)؛
    پیام‌هایی که خودشان از جای دیگری فوروارد شده بودند (و forwarded_from به
    منبع اصلی اشاره می‌کند) بعد، به ترتیب رسیدن جفت می‌شوند."""
    wanted = set(delivered)
    mapping: dict[int, int] = {}
    leftovers: list[int] = []
    for msg in received:
        try:
            bot_mid = int(msg.get("message_id"))
        except (TypeError, ValueError):
            continue
        fwd = msg.get("forwarded_from") or {}
        try:
            src = int(fwd.get("message_id"))
        except (TypeError, ValueError):
            src = None
        if src is not None and src not in wanted:
            # Bot API گاهی همان شناسه را با چند رقم اضافه در انتها می‌دهد
            # (مثلاً 1950023074621770 ← 1950023074621770017)
            s = str(src)
            src = next((w for w in wanted if w not in mapping and s.startswith(str(w)) and len(s) - len(str(w)) <= 4), src)
        if src is not None and src in wanted and src not in mapping:
            mapping[src] = bot_mid
        else:
            leftovers.append(bot_mid)
    rest = [m for m in delivered if m not in mapping]
    if rest and len(rest) == len(leftovers):
        for src, bot_mid in zip(rest, sorted(leftovers)):
            mapping[src] = bot_mid
    return mapping
