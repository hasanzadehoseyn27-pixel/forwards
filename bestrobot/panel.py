from __future__ import annotations

import logging
import re
from datetime import datetime

from telethon import Button, TelegramClient, events

from .config import Settings
from .db import Database


log = logging.getLogger(__name__)


HOME = "🏠 خانه"
BACK = "↩️ بازگشت"
SOURCES = "📥 مبداها"
DESTINATIONS = "📤 مقصدها"
GROUPS = "🧩 گروه‌های فوروارد"
STAGGER = "⏱ شروع با فاصله"
CUSTOMERS = "👥 مشتری‌ها و اشتراک"
STATUS = "📊 وضعیت کلی"
ADD_SOURCE = "➕ افزودن مبدا"
ADD_DESTINATION = "➕ افزودن مقصد"
ADD_GROUP = "➕ ساخت گروه"
ADD_CUSTOMER = "➕ افزودن مشتری"
ADD_SUBSCRIPTION = "➕ افزودن اشتراک"
TOGGLE_GROUP = "🔌 روشن/خاموش"
RESET_GROUP = "🔄 ریست گروه"
MODE_ONCE = "📨 حالت یک‌بار"
MODE_REPEAT = "🔁 حالت تکراری"
SET_INTERVAL = "⏳ تنظیم فاصله"
INTERVAL_SECONDS = "ثانیه‌ای"
INTERVAL_MINUTES = "دقیقه‌ای"
INTERVAL_HOURS = "ساعتی"
ADD_GROUP_SOURCE = "📥 افزودن مبدا به گروه"
ADD_GROUP_DESTINATION = "📤 افزودن مقصد به گروه"


class AdminPanel:
    def __init__(self, bot: TelegramClient, db: Database, settings: Settings) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings

    def register(self) -> None:
        self.bot.add_event_handler(self.on_start, events.NewMessage(pattern=r"^/start$"))
        self.bot.add_event_handler(self.on_cancel, events.NewMessage(pattern=r"^/cancel$"))
        self.bot.add_event_handler(self.on_text, events.NewMessage)
        self.bot.add_event_handler(self.on_callback, events.CallbackQuery)

    async def on_start(self, event) -> None:
        if not await self.ensure_admin(event):
            return
        self.db.pop_panel_action(event.sender_id)
        await self.show_home(event)

    async def on_cancel(self, event) -> None:
        if not await self.ensure_admin(event):
            return
        self.db.pop_panel_action(event.sender_id)
        await event.respond("عملیات لغو شد.", buttons=self.main_buttons())

    async def on_text(self, event) -> None:
        if event.raw_text.startswith("/start") or event.raw_text.startswith("/cancel"):
            return
        if not await self.ensure_admin(event):
            return

        text = event.raw_text.strip()
        if not text:
            return

        handled = await self.handle_navigation(event, text)
        if handled:
            return

        action = self.db.pop_panel_action(event.sender_id)
        if not action:
            await self.show_home(event)
            return

        name, payload = action
        try:
            if name == "add_entity":
                await self.handle_add_entity(event, payload["kind"], text)
            elif name == "add_group":
                group_id = self.db.add_group(text)
                await event.respond(f"گروه «{text}» ساخته شد. شناسه: {group_id}", buttons=self.group_buttons(group_id))
            elif name == "set_interval":
                group_id = int(payload["group_id"])
                seconds = parse_duration(text)
                self.db.update_group(group_id, interval_seconds=seconds)
                self.db.set_panel_action(event.sender_id, "group_menu", {"group_id": group_id})
                await event.respond(f"فاصله ارسال روی {format_duration(seconds)} تنظیم شد.", buttons=self.group_buttons(group_id))
            elif name == "set_interval_unit":
                group_id = int(payload["group_id"])
                unit = parse_interval_unit(text)
                self.db.set_panel_action(event.sender_id, "set_interval_value", {"group_id": group_id, "unit": unit})
                await event.respond("عدد دلخواه را بفرست.", buttons=self.cancel_buttons())
            elif name == "set_interval_value":
                group_id = int(payload["group_id"])
                unit = payload["unit"]
                seconds = interval_to_seconds(text, unit)
                self.db.update_group(group_id, interval_seconds=seconds)
                self.db.set_panel_action(event.sender_id, "group_menu", {"group_id": group_id})
                await event.respond(f"فاصله تکرار روی {format_duration(seconds)} تنظیم شد.\n\n{self.group_text(group_id)}", buttons=self.group_buttons(group_id))
            elif name == "stagger":
                await self.handle_stagger(event, text)
            elif name == "add_customer":
                title, note = split_title_peer(text)
                customer_id = self.db.add_customer(title, note)
                await event.respond(f"مشتری ثبت شد. شناسه: {customer_id}", buttons=self.customer_buttons())
            elif name == "add_subscription":
                await self.handle_subscription(event, text)
            elif name == "pick_entity":
                await self.handle_pick_entity(event, payload, text)
            else:
                await event.respond("عملیات نامشخص بود. دوباره از منو انتخاب کن.", buttons=self.main_buttons())
        except Exception as exc:
            log.exception("خطای پنل در action=%s", name)
            if name in {"set_interval", "set_interval_unit", "set_interval_value", "add_entity", "pick_entity"}:
                self.db.set_panel_action(event.sender_id, name, payload)
                buttons = self.interval_unit_buttons() if name == "set_interval_unit" else self.cancel_buttons()
            else:
                buttons = self.main_buttons()
            await event.respond(f"خطا: {exc}", buttons=buttons)

    async def on_callback(self, event) -> None:
        if not await self.ensure_admin(event):
            return
        data = event.data.decode("utf-8")
        await event.answer()
        if data == "noop":
            return
        if data.startswith("open_group:"):
            group_id = int(data.split(":", 1)[1])
            self.db.set_panel_action(event.sender_id, "group_menu", {"group_id": group_id})
            await event.respond(self.group_text(group_id), buttons=self.group_buttons(group_id))
            return
        await event.respond(self.main_text(), buttons=self.main_buttons())

    async def handle_navigation(self, event, text: str) -> bool:
        if text == HOME:
            self.db.pop_panel_action(event.sender_id)
            await self.show_home(event)
            return True
        if text == BACK:
            self.db.pop_panel_action(event.sender_id)
            await self.show_home(event)
            return True
        if text == SOURCES:
            self.db.pop_panel_action(event.sender_id)
            await event.respond(self.entities_text("source"), buttons=self.entities_buttons("source"))
            return True
        if text == DESTINATIONS:
            self.db.pop_panel_action(event.sender_id)
            await event.respond(self.entities_text("destination"), buttons=self.entities_buttons("destination"))
            return True
        if text == ADD_SOURCE:
            self.db.set_panel_action(event.sender_id, "add_entity", {"kind": "source"})
            await event.respond(
                "یوزرنیم مبدا را با @ بفرست.\nنمونه‌ها:\nکانال اصلی | @mainchannel\n@mainchannel",
                buttons=self.cancel_buttons(),
            )
            return True
        if text == ADD_DESTINATION:
            self.db.set_panel_action(event.sender_id, "add_entity", {"kind": "destination"})
            await event.respond(
                "یوزرنیم مقصد را با @ بفرست.\nنمونه‌ها:\nگروه تست | @testgroup\n@testgroup",
                buttons=self.cancel_buttons(),
            )
            return True
        if text == GROUPS:
            self.db.pop_panel_action(event.sender_id)
            await event.respond(self.groups_text(), buttons=self.group_list_buttons())
            await event.respond("برای ساخت گروه جدید یا بازگشت از دکمه‌های پایین استفاده کن.", buttons=self.groups_buttons())
            return True
        if text == ADD_GROUP:
            self.db.set_panel_action(event.sender_id, "add_group")
            await event.respond("نام گروه فوروارد را بفرست.", buttons=self.cancel_buttons())
            return True
        if text.startswith("🧩 گروه "):
            group_id = parse_first_int(text)
            self.db.set_panel_action(event.sender_id, "group_menu", {"group_id": group_id})
            await event.respond(self.group_text(group_id), buttons=self.group_buttons(group_id))
            return True
        if text == STAGGER:
            self.db.set_panel_action(event.sender_id, "stagger")
            await event.respond(
                "شناسه گروه‌ها و فاصله شروع را بفرست.\nنمونه: 1,2,3 | 2m\nگروه‌ها خاموش می‌شوند و با فاصله روشن می‌شوند.",
                buttons=self.cancel_buttons(),
            )
            return True
        if text == CUSTOMERS:
            self.db.pop_panel_action(event.sender_id)
            await event.respond(self.customers_text(), buttons=self.customer_buttons())
            return True
        if text == ADD_CUSTOMER:
            self.db.set_panel_action(event.sender_id, "add_customer")
            await event.respond("نام مشتری را بفرست.\nفرمت اختیاری: نام | توضیح", buttons=self.cancel_buttons())
            return True
        if text == ADD_SUBSCRIPTION:
            self.db.set_panel_action(event.sender_id, "add_subscription")
            await event.respond("اشتراک را بفرست.\nفرمت: customer_id | days | توضیح", buttons=self.cancel_buttons())
            return True
        if text == STATUS:
            self.db.pop_panel_action(event.sender_id)
            await event.respond(self.all_status_text(), buttons=self.main_buttons())
            return True

        action = self.db.fetchone("SELECT action, payload FROM panel_sessions WHERE user_id=?", (event.sender_id,))
        current_group_id = None
        if action and action["action"] == "group_menu":
            import json

            current_group_id = int(json.loads(action["payload"] or "{}").get("group_id", 0) or 0)

        if current_group_id:
            return await self.handle_group_command(event, current_group_id, text)

        return False

    async def handle_group_command(self, event, group_id: int, text: str) -> bool:
        if text == TOGGLE_GROUP:
            enabled = self.db.toggle_group(group_id)
            state = "روشن" if enabled else "خاموش"
            self.db.set_panel_action(event.sender_id, "group_menu", {"group_id": group_id})
            await event.respond(f"گروه {state} شد.\n\n{self.group_text(group_id)}", buttons=self.group_buttons(group_id))
            return True
        if text == RESET_GROUP:
            self.db.reset_group(group_id)
            self.db.set_panel_action(event.sender_id, "group_menu", {"group_id": group_id})
            await event.respond("گروه ریست شد. از پیام‌های جدید به بعد ادامه می‌دهد.", buttons=self.group_buttons(group_id))
            return True
        if text == MODE_ONCE:
            self.db.update_group(group_id, mode="once")
            self.db.set_panel_action(event.sender_id, "group_menu", {"group_id": group_id})
            await event.respond(self.group_text(group_id), buttons=self.group_buttons(group_id))
            return True
        if text == MODE_REPEAT:
            self.db.update_group(group_id, mode="repeat")
            self.db.set_panel_action(event.sender_id, "set_interval_unit", {"group_id": group_id})
            await event.respond("حالت تکراری فعال شد. واحد فاصله را انتخاب کن.", buttons=self.interval_unit_buttons())
            return True
        if text == SET_INTERVAL:
            self.db.set_panel_action(event.sender_id, "set_interval_unit", {"group_id": group_id})
            await event.respond("واحد فاصله را انتخاب کن.", buttons=self.interval_unit_buttons())
            return True
        if text == ADD_GROUP_SOURCE:
            self.db.set_panel_action(event.sender_id, "pick_entity", {"group_id": group_id, "kind": "source"})
            await event.respond("یک مبدا را انتخاب کن یا @username جدید بفرست.", buttons=self.pick_entity_buttons(group_id, "source"))
            return True
        if text == ADD_GROUP_DESTINATION:
            self.db.set_panel_action(event.sender_id, "pick_entity", {"group_id": group_id, "kind": "destination"})
            await event.respond("یک مقصد را انتخاب کن یا @username جدید بفرست.", buttons=self.pick_entity_buttons(group_id, "destination"))
            return True
        return False

    async def show_home(self, event) -> None:
        await event.respond(self.main_text(), buttons=self.main_buttons())

    async def ensure_admin(self, event) -> bool:
        sender_id = int(event.sender_id or 0)
        if not self.settings.admin_ids:
            await event.respond("ADMIN_IDS در فایل .env تنظیم نشده است.")
            return False
        if sender_id not in self.settings.admin_ids:
            await event.respond("شما دسترسی مدیریت ندارید.")
            return False
        return True

    async def handle_add_entity(self, event, kind: str, text: str) -> None:
        title, peer = split_title_peer(text)
        peer = normalize_peer(peer)
        entity_id = self.db.add_entity(kind, title, peer)
        label = "مبدا" if kind == "source" else "مقصد"
        await event.respond(f"{label} ثبت شد.\nشناسه: {entity_id}\nآدرس: {peer}", buttons=self.entities_buttons(kind))

    async def handle_pick_entity(self, event, payload, text: str) -> None:
        group_id = int(payload["group_id"])
        kind = payload["kind"]
        entity_id = None
        if text.startswith("📌 "):
            entity_id = parse_first_int(text)
        else:
            title, peer = split_title_peer(text)
            entity_id = self.db.add_entity(kind, title, normalize_peer(peer))

        self.db.link_entity(group_id, int(entity_id), kind)
        self.db.set_panel_action(event.sender_id, "group_menu", {"group_id": group_id})
        await event.respond("اضافه شد.\n\n" + self.group_text(group_id), buttons=self.group_buttons(group_id))

    async def handle_stagger(self, event, text: str) -> None:
        if "|" not in text:
            raise ValueError("فرمت درست: 1,2,3 | 2m")
        ids_part, gap_part = text.split("|", 1)
        group_ids = [int(part.strip()) for part in ids_part.split(",") if part.strip()]
        gap_seconds = parse_duration(gap_part.strip())
        if not group_ids:
            raise ValueError("هیچ شناسه گروهی پیدا نشد.")
        self.db.schedule_staggered_start(group_ids, gap_seconds)
        await event.respond(
            f"{len(group_ids)} گروه زمان‌بندی شد. فاصله شروع: {format_duration(gap_seconds)}",
            buttons=self.main_buttons(),
        )

    async def handle_subscription(self, event, text: str) -> None:
        parts = [part.strip() for part in text.split("|")]
        if len(parts) < 2:
            raise ValueError("فرمت درست: customer_id | days | توضیح")
        customer_id = int(parts[0])
        days = int(parts[1])
        note = parts[2] if len(parts) > 2 else ""
        sub_id = self.db.add_subscription(customer_id, days, note)
        await event.respond(f"اشتراک ثبت شد. شناسه: {sub_id}", buttons=self.customer_buttons())

    def main_text(self) -> str:
        return "پنل مدیریت BestRobot\nاز دکمه‌های پایین چت برای مدیریت مبدا، مقصد، گروه‌ها و وضعیت استفاده کن."

    def main_buttons(self):
        return reply_keyboard(
            [
                [SOURCES, DESTINATIONS],
                [GROUPS, STAGGER],
                [CUSTOMERS, STATUS],
            ]
        )

    def cancel_buttons(self):
        return reply_keyboard([[HOME, BACK]])

    def entities_text(self, kind: str) -> str:
        label = "مبداها" if kind == "source" else "مقصدها"
        rows = self.db.list_entities(kind)
        if not rows:
            return f"{label}\nهنوز چیزی ثبت نشده."
        lines = [label]
        for row in rows:
            state = "فعال" if row["enabled"] else "خاموش"
            lines.append(f"{row['id']}. {row['peer']} ({state})")
        return "\n".join(lines)

    def entities_buttons(self, kind: str):
        add_label = ADD_SOURCE if kind == "source" else ADD_DESTINATION
        return reply_keyboard([[add_label], [HOME, BACK]])

    def groups_text(self) -> str:
        groups = self.db.list_groups()
        if not groups:
            return "هنوز گروه فوروارد ساخته نشده."
        lines = ["گروه‌های فوروارد"]
        for group in groups:
            state = "روشن" if group["enabled"] else "خاموش"
            mode = "یک‌بار" if group["mode"] == "once" else "تکراری"
            lines.append(f"{group['id']}. {group['name']} - {state} - {mode} - {format_duration(group['interval_seconds'])}")
        return "\n".join(lines)

    def groups_buttons(self):
        rows = [[ADD_GROUP], [HOME, BACK]]
        return reply_keyboard(rows)

    def group_list_buttons(self):
        rows = []
        for group in self.db.list_groups():
            rows.append([Button.inline(f"ویرایش گروه {group['id']} - {group['name']}", f"open_group:{group['id']}".encode())])
        if not rows:
            rows.append([Button.inline("هنوز گروهی نیست", b"noop")])
        return rows

    def group_text(self, group_id: int) -> str:
        status = self.db.group_status(group_id)
        group = status["group"]
        if not group:
            return "گروه پیدا نشد."
        health = status["health"]
        state = "روشن" if group["enabled"] else "خاموش"
        mode = "ارسال یک‌بار" if group["mode"] == "once" else "ارسال تکراری"
        sources = "\n".join(f"- {row['peer']}" for row in status["sources"]) or "-"
        destinations = "\n".join(f"- {row['peer']}" for row in status["destinations"]) or "-"
        jobs = status["jobs"]
        last_ok = format_ts(health.get("last_success_at"))
        last_err = format_ts(health.get("last_error_at"))
        return (
            f"گروه {group['id']} - {group['name']}\n"
            f"وضعیت: {state}\n"
            f"حالت: {mode}\n"
            f"فاصله: {format_duration(group['interval_seconds'])}\n"
            f"\nمبداها:\n{sources}\n"
            f"\nمقصدها:\n{destinations}\n"
            f"\nصف: pending={jobs.get('pending', 0)} running={jobs.get('running', 0)} done={jobs.get('done', 0)} failed={jobs.get('failed', 0)}\n"
            f"آخرین موفق: {last_ok}\n"
            f"آخرین خطا: {last_err}\n"
            f"خطا: {health.get('last_error') or '-'}\n"
            f"یادداشت: {health.get('running_note') or '-'}"
        )

    def group_buttons(self, group_id: int):
        return reply_keyboard(
            [
                [TOGGLE_GROUP, RESET_GROUP],
                [MODE_ONCE, MODE_REPEAT],
                [SET_INTERVAL],
                [ADD_GROUP_SOURCE],
                [ADD_GROUP_DESTINATION],
                [GROUPS, HOME],
            ]
        )

    def interval_unit_buttons(self):
        return reply_keyboard([[INTERVAL_SECONDS, INTERVAL_MINUTES, INTERVAL_HOURS], [HOME, BACK]])

    def pick_entity_buttons(self, group_id: int, kind: str):
        rows = []
        for row in self.db.list_entities(kind):
            rows.append([f"📌 {row['id']} - {row['peer']}"])
        rows.append([GROUPS, HOME])
        return reply_keyboard(rows)

    def customers_text(self) -> str:
        customers = self.db.list_customers()
        subs = self.db.list_subscriptions()
        lines = ["مشتری‌ها"]
        if customers:
            lines.extend(f"{row['id']}. {row['name']} - {row['note'] or '-'}" for row in customers)
        else:
            lines.append("هنوز مشتری ثبت نشده.")
        lines.append("\nاشتراک‌ها")
        if subs:
            for row in subs[:20]:
                lines.append(f"{row['id']}. {row['customer_name']} تا {format_ts(row['ends_at'])} - {row['note'] or '-'}")
        else:
            lines.append("هنوز اشتراکی ثبت نشده.")
        return "\n".join(lines)

    def customer_buttons(self):
        return reply_keyboard([[ADD_CUSTOMER, ADD_SUBSCRIPTION], [HOME, BACK]])

    def all_status_text(self) -> str:
        groups = self.db.list_groups()
        if not groups:
            return "گروهی برای نمایش وضعیت وجود ندارد."
        lines = ["وضعیت کلی"]
        for group in groups:
            status = self.db.group_status(int(group["id"]))
            jobs = status["jobs"]
            health = status["health"]
            state = "روشن" if group["enabled"] else "خاموش"
            lines.append(
                f"{group['id']}. {group['name']} ({state}) | "
                f"pending={jobs.get('pending', 0)} done={jobs.get('done', 0)} failed={jobs.get('failed', 0)} | "
                f"آخرین موفق: {format_ts(health.get('last_success_at'))}"
            )
        return "\n".join(lines)


def reply_keyboard(rows: list[list[str]]):
    return [[Button.text(label, resize=True) for label in row] for row in rows]


def split_title_peer(text: str) -> tuple[str, str]:
    if "|" in text:
        title, peer = text.split("|", 1)
        return title.strip(), peer.strip()

    peer = extract_peer(text)
    if peer:
        title = text.replace(peer, "").strip(" -|،,:")
        return title or peer, peer
    return text.strip(), text.strip()


def extract_peer(text: str) -> str | None:
    link = re.search(r"(?:https?://)?t\.me/([+A-Za-z0-9_][A-Za-z0-9_+\-]*)", text, re.IGNORECASE)
    if link:
        value = link.group(1).strip()
        return value if value.startswith("+") else "@" + value.lstrip("@")

    username = re.search(r"@([A-Za-z0-9_]{4,})", text)
    if username:
        return "@" + username.group(1)
    return None


def normalize_peer(peer: str) -> str:
    peer = peer.strip()
    found = extract_peer(peer)
    if found:
        return found
    return peer


def parse_first_int(text: str) -> int:
    match = re.search(r"\d+", text)
    if not match:
        raise ValueError("شناسه عددی پیدا نشد.")
    return int(match.group(0))


def parse_duration(text: str) -> int:
    value = text.strip().lower()
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", value)
    if not match:
        raise ValueError("زمان نامعتبر است. نمونه: 30m یا 1800")
    number = int(match.group(1))
    unit = match.group(2) or "s"
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = number * factors[unit]
    if seconds < 1:
        raise ValueError("فاصله کمتر از ۱ ثانیه قابل قبول نیست.")
    return seconds


def parse_interval_unit(text: str) -> str:
    value = text.strip()
    mapping = {
        INTERVAL_SECONDS: "s",
        INTERVAL_MINUTES: "m",
        INTERVAL_HOURS: "h",
        "ثانیه": "s",
        "دقیقه": "m",
        "ساعت": "h",
    }
    if value not in mapping:
        raise ValueError("یکی از گزینه‌های ثانیه‌ای، دقیقه‌ای یا ساعتی را انتخاب کن.")
    return mapping[value]


def interval_to_seconds(text: str, unit: str) -> int:
    value = text.strip()
    if re.fullmatch(r"\d+\s*[smhd]", value.lower()):
        return parse_duration(value)
    if not re.fullmatch(r"\d+", value):
        raise ValueError("فقط عدد بفرست. مثلا 30")
    number = int(value)
    if number <= 0:
        raise ValueError("عدد باید بزرگ‌تر از صفر باشد.")
    factors = {"s": 1, "m": 60, "h": 3600}
    seconds = number * factors[unit]
    if seconds < 1:
        raise ValueError("فاصله کمتر از ۱ ثانیه قابل قبول نیست.")
    return seconds


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds % 86400 == 0:
        return f"{seconds // 86400} روز"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ساعت"
    if seconds % 60 == 0:
        return f"{seconds // 60} دقیقه"
    return f"{seconds} ثانیه"


def format_ts(value) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
