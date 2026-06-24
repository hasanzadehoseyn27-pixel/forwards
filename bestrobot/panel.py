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
REFRESH_SOURCE_TITLES = "🔄 به‌روزرسانی نام مبداها"
REFRESH_DESTINATION_TITLES = "🔄 به‌روزرسانی نام مقصدها"
ADD_GROUP = "➕ ساخت گروه"
ADD_CUSTOMER = "➕ افزودن مشتری"
ADD_SUBSCRIPTION = "➕ افزودن اشتراک"
TOGGLE_GROUP = "🔌 روشن/خاموش"
RESET_GROUP = "🔄 ریست گروه"
DELETE_GROUP = "🗑 حذف گروه"
CONFIRM_DELETE_GROUP = "✅ بله، گروه حذف شود"
CANCEL_DELETE_GROUP = "❌ نه، منصرف شدم"
MODE_ONCE = "📨 حالت یک‌بار"
MODE_REPEAT = "🔁 حالت دائمی"
INTERVAL_SECONDS = "ثانیه‌ای"
INTERVAL_MINUTES = "دقیقه‌ای"
INTERVAL_HOURS = "ساعتی"
ADD_GROUP_SOURCE = "📥 افزودن مبدا به گروه"
ADD_GROUP_DESTINATION = "📤 افزودن مقصد به گروه"


class AdminPanel:
    def __init__(self, bot: TelegramClient, db: Database, settings: Settings, forward_client: TelegramClient | None = None) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self.forward_client = forward_client

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
                self.db.set_panel_action(event.sender_id, "pick_entity_multi", {"group_id": group_id, "kind": "source", "page": 0, "wizard": "source"})
                await event.respond(f"گروه «{text}» ساخته شد و به‌صورت پیش‌فرض روشن است. شناسه: {group_id}")
                await event.respond(self.entity_picker_text(group_id, "source"), buttons=self.entity_picker_keyboard(group_id, "source", 0, "source"))
                await event.respond(
                    "اگر مبدای جدیدی داری که در این لیست نیست، یوزرنیمش را با @ بفرست تا اضافه و مستقیم به این گروه وصل شود.",
                    buttons=self.cancel_buttons(),
                )
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
            elif name == "stagger_names":
                await self.handle_stagger_names(event, text)
            elif name == "stagger_gap":
                await self.handle_stagger_gap(event, payload, text)
            elif name == "add_customer":
                title, note = split_title_peer(text)
                customer_id = self.db.add_customer(title, note)
                await event.respond(f"مشتری ثبت شد. شناسه: {customer_id}", buttons=self.customer_buttons())
            elif name == "add_subscription":
                await self.handle_subscription(event, text)
            elif name == "pick_entity_multi":
                await self.handle_pick_entity_multi(event, payload, text)
            elif name == "confirm_delete_group":
                await self.handle_confirm_delete_group(event, payload, text)
            else:
                await event.respond("عملیات نامشخص بود. دوباره از منو انتخاب کن.", buttons=self.main_buttons())
        except Exception as exc:
            log.exception("خطای پنل در action=%s", name)
            if name in {"set_interval", "set_interval_unit", "set_interval_value", "add_entity", "pick_entity_multi", "stagger_names", "stagger_gap", "confirm_delete_group"}:
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
        if data.startswith("tg|"):
            _, group_id_s, kind, entity_id_s, page_s, wizard_tag = data.split("|")
            group_id, entity_id, page = int(group_id_s), int(entity_id_s), int(page_s)
            wizard = None if wizard_tag == "-" else wizard_tag
            linked = self.db.group_sources(group_id) if kind == "source" else self.db.group_destinations(group_id)
            linked_ids = {int(row["id"]) for row in linked}
            if entity_id in linked_ids:
                self.db.unlink_entity(group_id, entity_id, kind)
            else:
                self.db.link_entity(group_id, entity_id, kind)
            try:
                await event.edit(self.entity_picker_text(group_id, kind), buttons=self.entity_picker_keyboard(group_id, kind, page, wizard))
            except Exception:
                log.exception("خطا در به‌روزرسانی چک‌باکس مبدا/مقصد")
            return
        if data.startswith("pg|"):
            _, group_id_s, kind, page_s, wizard_tag = data.split("|")
            group_id, page = int(group_id_s), int(page_s)
            wizard = None if wizard_tag == "-" else wizard_tag
            try:
                await event.edit(self.entity_picker_text(group_id, kind), buttons=self.entity_picker_keyboard(group_id, kind, page, wizard))
            except Exception:
                log.exception("خطا در صفحه‌بندی لیست مبدا/مقصد")
            return
        if data.startswith("wnext|"):
            _, group_id_s, step = data.split("|")
            group_id = int(group_id_s)
            if step == "destination":
                self.db.set_panel_action(event.sender_id, "pick_entity_multi", {"group_id": group_id, "kind": "destination", "page": 0, "wizard": "destination"})
                await event.respond(self.entity_picker_text(group_id, "destination"), buttons=self.entity_picker_keyboard(group_id, "destination", 0, "destination"))
                await event.respond(
                    "اگر مقصد جدیدی داری که در این لیست نیست، یوزرنیمش را با @ بفرست تا اضافه و مستقیم به این گروه وصل شود.",
                    buttons=self.cancel_buttons(),
                )
                return
            if step == "mode":
                self.db.set_panel_action(event.sender_id, "group_menu", {"group_id": group_id})
                await event.respond(
                    "حالا حالت ارسال این گروه را انتخاب کن:\n📨 حالت یک‌بار: هر پیام فقط یک‌بار فوروارد می‌شود.\n🔁 حالت دائمی: پیام‌های امروز با فاصله‌ای که خودت تنظیم می‌کنی، دوباره فوروارد می‌شوند.",
                    buttons=self.group_buttons(group_id),
                )
                return
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
        if text == REFRESH_SOURCE_TITLES:
            self.db.pop_panel_action(event.sender_id)
            updated = await self.refresh_entity_titles("source")
            note = f"نام {updated} مبدا به‌روزرسانی شد." if self.forward_client else "اکانت فوروارد وصل نیست؛ نام واقعی گرفته نشد."
            await event.respond(f"{note}\n\n{self.entities_text('source')}", buttons=self.entities_buttons("source"))
            return True
        if text == REFRESH_DESTINATION_TITLES:
            self.db.pop_panel_action(event.sender_id)
            updated = await self.refresh_entity_titles("destination")
            note = f"نام {updated} مقصد به‌روزرسانی شد." if self.forward_client else "اکانت فوروارد وصل نیست؛ نام واقعی گرفته نشد."
            await event.respond(f"{note}\n\n{self.entities_text('destination')}", buttons=self.entities_buttons("destination"))
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
            self.db.set_panel_action(event.sender_id, "stagger_names")
            await event.respond(
                "اسم گروه‌ها را با کاما (,) جدا کن و بفرست.\nنمونه: تست 1, تست 2, تست 3\nاگر اسم گروهی تکراری بود، می‌توانی به‌جای اسم، شناسه‌اش را بفرستی.\nفاصله شروع را در مرحله بعد می‌پرسم. گروه‌ها خاموش می‌شوند و به ترتیب با فاصله روشن می‌شوند.",
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
            await event.respond("حالت دائمی فعال شد. واحد فاصله تکرار را انتخاب کن.", buttons=self.interval_unit_buttons())
            return True
        if text == ADD_GROUP_SOURCE:
            self.db.set_panel_action(event.sender_id, "pick_entity_multi", {"group_id": group_id, "kind": "source", "page": 0})
            await event.respond(self.entity_picker_text(group_id, "source"), buttons=self.entity_picker_keyboard(group_id, "source", 0))
            await event.respond(
                "اگر مبدای جدیدی داری که در این لیست نیست، یوزرنیمش را با @ بفرست تا اضافه و مستقیم به این گروه وصل شود.",
                buttons=self.cancel_buttons(),
            )
            return True
        if text == ADD_GROUP_DESTINATION:
            self.db.set_panel_action(event.sender_id, "pick_entity_multi", {"group_id": group_id, "kind": "destination", "page": 0})
            await event.respond(self.entity_picker_text(group_id, "destination"), buttons=self.entity_picker_keyboard(group_id, "destination", 0))
            await event.respond(
                "اگر مقصد جدیدی داری که در این لیست نیست، یوزرنیمش را با @ بفرست تا اضافه و مستقیم به این گروه وصل شود.",
                buttons=self.cancel_buttons(),
            )
            return True
        if text == DELETE_GROUP:
            group = self.db.get_group(group_id)
            name = str(group["name"]) if group else str(group_id)
            self.db.set_panel_action(event.sender_id, "confirm_delete_group", {"group_id": group_id})
            await event.respond(
                f"مطمئنی گروه «{name}» حذف شود؟\nمبداها و مقصدهای وصل‌شده به این گروه و کل صف ارسالش هم حذف می‌شود. این کار قابل برگشت نیست.",
                buttons=reply_keyboard([[CONFIRM_DELETE_GROUP], [CANCEL_DELETE_GROUP]]),
            )
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
        real_title = await self.fetch_real_title(peer)
        if real_title:
            title = real_title
        entity_id = self.db.add_entity(kind, title, peer)
        label = "مبدا" if kind == "source" else "مقصد"
        await event.respond(f"{label} ثبت شد.\nنام: {title}\nآدرس: {peer}", buttons=self.entities_buttons(kind))

    async def fetch_real_title(self, peer: str) -> str | None:
        if not self.forward_client:
            return None
        try:
            entity = await self.forward_client.get_entity(peer)
        except Exception:
            log.exception("نتوانستم اسم واقعی %s را از تلگرام بگیرم", peer)
            return None
        title = getattr(entity, "title", None)
        if title and str(title).strip():
            return str(title).strip()
        first_name = getattr(entity, "first_name", None) or ""
        last_name = getattr(entity, "last_name", None) or ""
        full_name = f"{first_name} {last_name}".strip()
        return full_name or None

    async def refresh_entity_titles(self, kind: str) -> int:
        if not self.forward_client:
            return 0
        updated = 0
        for row in self.db.list_entities(kind):
            real_title = await self.fetch_real_title(str(row["peer"]))
            if real_title and real_title != str(row["title"]).strip():
                self.db.update_entity_title(int(row["id"]), real_title)
                updated += 1
        return updated

    async def handle_pick_entity_multi(self, event, payload, text: str) -> None:
        group_id = int(payload["group_id"])
        kind = payload["kind"]
        wizard = payload.get("wizard")
        title, peer = split_title_peer(text)
        peer = normalize_peer(peer)
        real_title = await self.fetch_real_title(peer)
        if real_title:
            title = real_title
        entity_id = self.db.add_entity(kind, title, peer)
        self.db.link_entity(group_id, int(entity_id), kind)
        new_payload = {"group_id": group_id, "kind": kind, "page": 0}
        if wizard:
            new_payload["wizard"] = wizard
        self.db.set_panel_action(event.sender_id, "pick_entity_multi", new_payload)
        label = "مبدا" if kind == "source" else "مقصد"
        await event.respond(
            f"{label} «{title}» اضافه و به گروه وصل شد.\n\n" + self.entity_picker_text(group_id, kind),
            buttons=self.entity_picker_keyboard(group_id, kind, 0, wizard),
        )

    async def handle_confirm_delete_group(self, event, payload, text: str) -> None:
        group_id = int(payload["group_id"])
        group = self.db.get_group(group_id)
        name = str(group["name"]) if group else str(group_id)
        if text == CONFIRM_DELETE_GROUP:
            self.db.delete_group(group_id)
            await event.respond(f"گروه «{name}» حذف شد.", buttons=self.main_buttons())
            return
        self.db.set_panel_action(event.sender_id, "group_menu", {"group_id": group_id})
        await event.respond("حذف لغو شد.\n\n" + self.group_text(group_id), buttons=self.group_buttons(group_id))

    async def handle_stagger_names(self, event, text: str) -> None:
        tokens = [part.strip() for part in text.split(",") if part.strip()]
        if not tokens:
            raise ValueError("حداقل اسم یا شناسه یک گروه لازم است.")

        group_ids: list[int] = []
        labels: list[str] = []
        for token in tokens:
            if token.isdigit():
                group = self.db.get_group(int(token))
                if not group:
                    raise ValueError(f"گروهی با شناسه {token} پیدا نشد.")
                group_ids.append(int(group["id"]))
                labels.append(str(group["name"]))
                continue

            matches = self.db.find_groups_by_name(token)
            if not matches:
                raise ValueError(f"گروهی با اسم «{token}» پیدا نشد.")
            if len(matches) > 1:
                ids_text = "، ".join(str(row["id"]) for row in matches)
                raise ValueError(f"چند گروه با اسم «{token}» وجود دارد. به‌جای اسم، یکی از این شناسه‌ها را بفرست: {ids_text}")
            group_ids.append(int(matches[0]["id"]))
            labels.append(str(matches[0]["name"]))

        self.db.set_panel_action(event.sender_id, "stagger_gap", {"group_ids": group_ids, "labels": labels})
        await event.respond(
            "حالا فاصله شروع بین گروه‌ها را بفرست.\nنمونه: 2m یا 30s یا 1h",
            buttons=self.cancel_buttons(),
        )

    async def handle_stagger_gap(self, event, payload, text: str) -> None:
        group_ids = [int(value) for value in payload.get("group_ids", [])]
        labels = payload.get("labels") or [str(value) for value in group_ids]
        if not group_ids:
            raise ValueError("گروهی برای زمان‌بندی انتخاب نشده بود. از اول شروع کن.")

        gap_seconds = parse_duration(text)
        self.db.schedule_staggered_start(group_ids, gap_seconds)
        names_text = "، ".join(labels)
        await event.respond(
            f"{len(group_ids)} گروه زمان‌بندی شد ({names_text}).\nفاصله شروع: {format_duration(gap_seconds)}",
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
                [HOME],
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
            lines.append(f"- {display_entity_label(row)} - {state}")
        return "\n".join(lines)

    def entities_buttons(self, kind: str):
        add_label = ADD_SOURCE if kind == "source" else ADD_DESTINATION
        refresh_label = REFRESH_SOURCE_TITLES if kind == "source" else REFRESH_DESTINATION_TITLES
        return reply_keyboard([[add_label], [refresh_label], [HOME, BACK]])

    def groups_text(self) -> str:
        groups = self.db.list_groups()
        if not groups:
            return "هنوز گروه فوروارد ساخته نشده."
        lines = ["گروه‌های فوروارد"]
        for group in groups:
            state = "روشن" if group["enabled"] else "خاموش"
            mode = "یک‌بار" if group["mode"] == "once" else "دائمی"
            lines.append(f"- {group['name']} - {state} - {mode} - {format_duration(group['interval_seconds'])}")
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
        state = "روشن" if group["enabled"] else "خاموش"
        mode = "ارسال یک‌بار" if group["mode"] == "once" else "ارسال دائمی"
        sources = "\n".join(f"- {display_entity_label(row)}" for row in status["sources"]) or "-"
        destinations = "\n".join(f"- {display_entity_label(row)}" for row in status["destinations"]) or "-"
        return (
            f"گروه {group['id']} - {group['name']}\n"
            f"وضعیت: {state}\n"
            f"حالت: {mode}\n"
            f"فاصله: {format_duration(group['interval_seconds'])}\n"
            f"\nمبداها:\n{sources}\n"
            f"\nمقصدها:\n{destinations}"
        )

    def group_buttons(self, group_id: int):
        return reply_keyboard(
            [
                [TOGGLE_GROUP, RESET_GROUP],
                [MODE_ONCE, MODE_REPEAT],
                [ADD_GROUP_SOURCE],
                [ADD_GROUP_DESTINATION],
                [DELETE_GROUP],
                [GROUPS, HOME],
            ]
        )

    def interval_unit_buttons(self):
        return reply_keyboard([[INTERVAL_SECONDS, INTERVAL_MINUTES, INTERVAL_HOURS], [HOME, BACK]])

    def entity_picker_text(self, group_id: int, kind: str) -> str:
        label = "مبداها" if kind == "source" else "مقصدها"
        return f"روی هرکدام از {label} بزن تا به این گروه اضافه یا از آن حذف شود.\nوقتی تمام شد «✅ اتمام و بازگشت به گروه» را بزن."

    def entity_picker_keyboard(self, group_id: int, kind: str, page: int, wizard: str | None = None):
        all_rows = self.db.list_entities(kind)
        linked = self.db.group_sources(group_id) if kind == "source" else self.db.group_destinations(group_id)
        linked_ids = {int(row["id"]) for row in linked}
        page_size = 10
        total_pages = max(1, (len(all_rows) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        page_items = all_rows[start:start + page_size]
        wizard_tag = wizard or "-"

        rows = []
        if not page_items:
            rows.append([Button.inline("هنوز چیزی ثبت نشده", b"noop")])
        for row in page_items:
            entity_id = int(row["id"])
            checked = "☑️" if entity_id in linked_ids else "⬜"
            label = f"{checked} {display_entity_label(row)}"
            rows.append([Button.inline(label, f"tg|{group_id}|{kind}|{entity_id}|{page}|{wizard_tag}".encode())])

        nav_row = []
        if page > 0:
            nav_row.append(Button.inline("« قبلی", f"pg|{group_id}|{kind}|{page - 1}|{wizard_tag}".encode()))
        if page < total_pages - 1:
            nav_row.append(Button.inline("بعدی »", f"pg|{group_id}|{kind}|{page + 1}|{wizard_tag}".encode()))
        if nav_row:
            rows.append(nav_row)

        if wizard == "source":
            rows.append([Button.inline("✅ ادامه: انتخاب مقصدها", f"wnext|{group_id}|destination".encode())])
        elif wizard == "destination":
            rows.append([Button.inline("✅ ادامه: تنظیم حالت ارسال", f"wnext|{group_id}|mode".encode())])
        else:
            rows.append([Button.inline("✅ اتمام و بازگشت به گروه", f"open_group:{group_id}".encode())])
        return rows

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
        peer = normalize_peer(peer.strip())
        title = title.strip() or peer
        return title, peer

    peer = extract_peer(text)
    if peer:
        return peer, peer
    return text.strip(), text.strip()


def display_entity_label(row) -> str:
    title = str(row["title"]).strip()
    peer = str(row["peer"]).strip()
    normalized_peer = peer.lstrip("@").rstrip("/").lower()
    normalized_title = title.lstrip("@").rstrip("/").lower()
    if normalized_title == normalized_peer:
        return peer
    title_as_peer = extract_peer(title)
    if title_as_peer and title_as_peer.lstrip("@").lower() == normalized_peer:
        return peer
    return f"{title} ({peer})"


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