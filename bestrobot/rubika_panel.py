"""پنل مدیریت داخل خود روبیکا — همان پنل تلگرام (panel.py)، بدون هیچ تغییری در
منطقش. این فایل فقط «جای Telethon» را برای ربات پنل روبیکا می‌گیرد:

  • پیام‌ها با getUpdates از ربات پنل (مثلاً keyvankhodroBot) گرفته می‌شوند.
  • event.respond / event.edit ← sendMessage روبیکا با کیبورد (chat_keypad).
  • دکمه‌های شیشه‌ای تلگرام (inline) ← دکمه‌های همان کیبورد پایین چت؛
    وقتی رویشان زده شود، مثل CallbackQuery به پنل داده می‌شوند.

چرا کیبورد پایین و نه دکمه‌ی شیشه‌ای؟ در Bot API روبیکا کلیک دکمه‌ی شیشه‌ای
فقط از راه webhook (آدرس عمومی) می‌رسد، نه getUpdates؛ کیبورد پایین چت با
همان getUpdates کار می‌کند و سرور به آدرس عمومی نیاز ندارد.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field

import aiohttp

from .bot_pool import BotApiError, _tls_context

log = logging.getLogger(__name__)

RUBIKA_BOT_API = "https://botapi.rubika.ir/v3"
MAX_TEXT = 3900


def user_key(guid: str) -> int:
    """panel_sessions.user_id عددی است؛ guid روبیکا به یک عدد ثابت تبدیل می‌شود."""
    return int(hashlib.sha1(guid.encode("utf-8")).hexdigest()[:13], 16)


@dataclass
class ChatState:
    inline_rows: list = field(default_factory=list)  # [[(label, data_bytes)]]
    reply_rows: list = field(default_factory=list)  # [[label]]
    by_id: dict = field(default_factory=dict)  # button_id -> data
    by_text: dict = field(default_factory=dict)  # label -> data
    turn: int = -1  # شماره‌ی آپدیتی که inline_rows در آن ساخته شد


def _unwrap(button):
    """Button.text() در Telethon یک شیء custom.Button برمی‌گرداند که دکمه‌ی واقعی در .button است."""
    if type(button).__name__ == "Button":
        inner = getattr(button, "button", None)
        if inner is not None:
            return inner
    return button


def _rows(buttons) -> list[list]:
    if not buttons:
        return []
    if not isinstance(buttons, (list, tuple)):
        return [[_unwrap(buttons)]]
    if buttons and not isinstance(buttons[0], (list, tuple)):
        return [[_unwrap(b) for b in buttons]]
    return [[_unwrap(b) for b in r] for r in buttons]


def _attr(button, name: str):
    """data/url در Telethon قدیمی روی خود دکمه است، در نسخه‌های جدید روی button.type."""
    value = getattr(button, name, None)
    if value is None:
        value = getattr(getattr(button, "type", None), name, None)
    return value


def _kind(button) -> str:
    if isinstance(button, str):
        return "text"
    if _attr(button, "url"):
        return "url"
    if _attr(button, "data") is not None:
        return "inline"
    return "text"


class RubikaEvent:
    """شبیه event تلگرام (NewMessage / CallbackQuery) برای panel.py"""

    def __init__(self, bot: "RubikaPanelBot", chat_id: str, sender_guid: str, text: str, data: bytes | None = None):
        self._bot = bot
        self.chat_id = chat_id
        self.sender_guid = sender_guid
        self.sender_id = user_key(sender_guid)
        self.raw_text = text
        self.text = text
        self.data = data

    async def respond(self, text: str, buttons=None, **_kwargs):
        await self._bot.send(self.chat_id, text, buttons)

    async def reply(self, text: str, buttons=None, **_kwargs):
        await self._bot.send(self.chat_id, text, buttons)

    async def edit(self, text: str, buttons=None, **_kwargs):
        # در کیبورد پایین چت «ویرایش پیام» معنی ندارد؛ پیام تازه می‌فرستیم
        await self._bot.send(self.chat_id, text, buttons)

    async def answer(self, *_args, **_kwargs):
        return None


class RubikaPanelBot:
    def __init__(self, token: str, poll_seconds: float = 1.0) -> None:
        self.token = token
        self.poll_seconds = poll_seconds
        self.session: aiohttp.ClientSession | None = None
        self.username = ""
        self.bot_guid = ""
        self.panel = None
        self._task: asyncio.Task | None = None
        self._offset: str | None = None
        self._stopping = False
        self._chats: dict[str, ChatState] = {}
        self._seen: set[str] = set()
        self._turn = 0
        self._ids = 0

    # ---------------------------------------------------------------- api

    async def _call(self, method: str, payload: dict | None = None, timeout: float = 30):
        assert self.session is not None
        url = f"{RUBIKA_BOT_API}/{self.token}/{method}"
        async with self.session.post(url, json=payload or {}, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                raise BotApiError(resp.status or 500, (await resp.text())[:300])
        status = str((data or {}).get("status") or "")
        if status != "OK":
            raise BotApiError(401 if (method == "getMe" and status == "INVALID_ACCESS") else 400, status)
        return data.get("data") or {}

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_tls_context()))
        me = await self._call("getMe")
        info = me.get("bot") or me
        self.username = str(info.get("username") or "")
        self.bot_guid = str(info.get("bot_id") or "")
        await self._drain()
        log.info("پنل روبیکا وصل شد: @%s", self.username)

    def attach_panel(self, panel) -> None:
        self.panel = panel
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="rubika-panel")

    async def disconnect(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.session:
            await self.session.close()

    async def get_me(self):
        from types import SimpleNamespace

        return SimpleNamespace(id=self.bot_guid, username=self.username)

    # ------------------------------------------------------------- updates

    async def _fetch(self) -> list[dict]:
        payload: dict = {"limit": 100}
        if self._offset:
            payload["offset_id"] = self._offset
        data = await self._call("getUpdates", payload)
        if data.get("next_offset_id"):
            self._offset = str(data["next_offset_id"])
        return [u for u in (data.get("updates") or []) if isinstance(u, dict)]

    async def _drain(self) -> None:
        """پیام‌های قدیمی (قبل از روشن‌شدن) جواب داده نمی‌شوند."""
        for _ in range(200):
            before = self._offset
            updates = await self._fetch()
            for upd in updates:
                mid = (upd.get("new_message") or {}).get("message_id")
                if mid:
                    self._seen.add(str(mid))
            if not updates or self._offset == before:
                return

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                for upd in await self._fetch():
                    await self._handle(upd)
                await asyncio.sleep(self.poll_seconds)
            except asyncio.CancelledError:
                raise
            except BotApiError as exc:
                log.warning("خطای Bot API پنل روبیکا: %s", exc)
                await asyncio.sleep(5)
            except Exception:  # noqa: BLE001
                log.exception("خطا در پنل روبیکا")
                await asyncio.sleep(3)

    async def _handle(self, upd: dict) -> None:
        if upd.get("type") != "NewMessage":
            return
        msg = upd.get("new_message") or {}
        mid = str(msg.get("message_id") or "")
        if not mid or mid in self._seen:
            return
        self._seen.add(mid)
        if len(self._seen) > 5000:
            self._seen = set(list(self._seen)[-2000:])
        if str(msg.get("sender_type") or "User") != "User":
            return
        chat_id = str(upd.get("chat_id") or "")
        sender = str(msg.get("sender_id") or "")
        text = str(msg.get("text") or "").strip()
        if not chat_id or not sender or self.panel is None:
            return
        self._turn += 1
        state = self._chats.setdefault(chat_id, ChatState())
        button_id = str(((msg.get("aux_data") or {}).get("button_id")) or "")
        data = state.by_id.get(button_id) if button_id else None
        if data is None and text in state.by_text:
            data = state.by_text[text]

        if data is not None:
            if data.startswith(b"url:"):
                await self.send(chat_id, "🔗 " + data[4:].decode("utf-8"))
                return
            await self.panel.on_callback(RubikaEvent(self, chat_id, sender, text, data=data))
            return
        event = RubikaEvent(self, chat_id, sender, text)
        if text == "/start":
            await self.panel.on_start(event)
        elif text == "/cancel":
            await self.panel.on_cancel(event)
        else:
            await self.panel.on_text(event)

    # ---------------------------------------------------------------- send

    def _next_id(self) -> str:
        self._ids += 1
        return str(self._ids)

    def _keypad(self, state: ChatState) -> dict | None:
        rows = []
        state.by_id = {}
        state.by_text = {}
        used: set[str] = set()
        for row in state.inline_rows:
            buttons = []
            for label, data in row:
                text = label
                n = 2
                while text in used:
                    text = f"{label} ({n})"
                    n += 1
                used.add(text)
                bid = self._next_id()
                state.by_id[bid] = data
                state.by_text[text] = data
                buttons.append({"id": bid, "type": "Simple", "button_text": text})
            if buttons:
                rows.append({"buttons": buttons})
        for row in state.reply_rows:
            buttons = []
            for label in row:
                if label in used:
                    continue
                used.add(label)
                buttons.append({"id": self._next_id(), "type": "Simple", "button_text": label})
            if buttons:
                rows.append({"buttons": buttons})
        if not rows:
            return None
        return {"rows": rows, "resize_keyboard": True, "on_time_keyboard": False}

    async def send(self, chat_id: str, text: str, buttons=None) -> None:
        state = self._chats.setdefault(chat_id, ChatState())
        rows = _rows(buttons)
        if rows:
            kinds = {_kind(b) for row in rows for b in row}
            if kinds <= {"text"}:
                state.reply_rows = [[b if isinstance(b, str) else str(getattr(b, "text", "")) for b in row] for row in rows]
                # دکمه‌های لیستی که در همین نوبت فرستاده شده‌اند بمانند؛ قدیمی‌ها پاک شوند
                if state.turn != self._turn:
                    state.inline_rows = []
            else:
                inline_rows = []
                for row in rows:
                    out = []
                    for b in row:
                        k = _kind(b)
                        label = str(getattr(b, "text", b))
                        if k == "url":
                            out.append((label, b"url:" + str(_attr(b, "url")).encode("utf-8")))
                        elif k == "inline":
                            raw = _attr(b, "data")
                            data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
                            out.append((label, data))
                        else:
                            out.append((label, label.encode("utf-8")))
                    inline_rows.append(out)
                state.inline_rows = inline_rows
                state.turn = self._turn
        payload: dict = {"chat_id": chat_id, "text": (text or "-")[:MAX_TEXT]}
        keypad = self._keypad(state)
        if keypad:
            payload["chat_keypad_type"] = "New"
            payload["chat_keypad"] = keypad
        try:
            await self._call("sendMessage", payload)
        except BotApiError as exc:
            log.warning("ارسال پیام پنل روبیکا نشد (%s)؛ بدون کیبورد دوباره امتحان می‌شود.", exc)
            await self._call("sendMessage", {"chat_id": chat_id, "text": (text or "-")[:MAX_TEXT]})
