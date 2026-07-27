"""
اسکریپت نگهداری: همه‌ی مبداها (source) را با خود تلگرام چک می‌کند و هرکدام که
دیگر معتبر نیستند (یوزرنیم عوض شده/حذف شده) را از دیتابیس پاک می‌کند.

هر مبدا در یکی از این سه حالت قرار می‌گیرد:
  - سالم:    تلگرام یوزرنیم را پیدا کرد؛ مطمئنیم زنده است.
  - مرده:    تلگرام گفت چنین یوزرنیمی وجود ندارد؛ مطمئنیم مرده است.
  - نامعلوم: به‌خاطر FloodWait طولانی اصلا چک نشد؛ باید بعدا دوباره اجرا شود.
             این‌ها هرگز به لیست "مرده" اضافه نمی‌شوند و پاک نمی‌شوند.

اجرا:
    pm2 stop <نام-پروسه>   (یا pm2 delete اگر autorestart مزاحم شد)
    .venv\\Scripts\\python.exe -m bestrobot.cleanup_dead_sources
    pm2 start ecosystem.config.js

توجه: قبل از اجرا حتما پروسه‌ی اصلی را متوقف کن، چون هر دو از یک فایل
سشن استفاده می‌کنند و اجرای همزمان باعث خطای "database is locked" می‌شود.
"""
from __future__ import annotations

import asyncio
from enum import Enum

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError

from .config import Settings
from .db import Database


class Status(Enum):
    HEALTHY = "سالم"
    DEAD = "مرده"
    UNKNOWN = "نامعلوم"


async def check_one(client: TelegramClient, peer: str) -> tuple[Status, str]:
    for _ in range(3):
        try:
            await client.get_entity(peer)
            return Status.HEALTHY, ""
        except FloodWaitError as exc:
            wait_seconds = int(getattr(exc, "seconds", 30)) + 2
            if wait_seconds > 90:
                return Status.UNKNOWN, f"FloodWait طولانی ({wait_seconds} ثانیه)؛ چک نشد"
            print(f"    محدودیت موقت؛ {wait_seconds} ثانیه صبر و تلاش دوباره...")
            await asyncio.sleep(wait_seconds)
            continue
        except RPCError as exc:
            return Status.DEAD, exc.__class__.__name__
        except ValueError as exc:
            return Status.DEAD, str(exc)
    return Status.UNKNOWN, "بعد از چند بار تلاش هنوز FloodWait بود؛ چک نشد"


async def main() -> None:
    settings = Settings.load()
    db = Database(settings.db_path)

    proxy = settings.telethon_proxy()
    client = TelegramClient(str(settings.user_session), settings.api_id, settings.api_hash, proxy=proxy)
    await client.start()

    sources = db.fetchall("SELECT id, peer, title FROM entities WHERE kind='source'")
    print(f"تعداد کل مبداها: {len(sources)}")

    dead: list[tuple[int, str]] = []
    unknown: list[str] = []
    healthy_count = 0

    for index, row in enumerate(sources, start=1):
        peer = str(row["peer"])
        status, reason = await check_one(client, peer)
        if status is Status.DEAD:
            dead.append((int(row["id"]), peer))
            print(f"[{index}/{len(sources)}] مرده: {peer} ({reason})")
        elif status is Status.UNKNOWN:
            unknown.append(peer)
            print(f"[{index}/{len(sources)}] نامعلوم (رد شد): {peer} ({reason})")
        else:
            healthy_count += 1
            print(f"[{index}/{len(sources)}] سالم: {peer}")
        await asyncio.sleep(2)  # فاصله‌ی امن‌تر بین درخواست‌ها

    print(f"\nخلاصه: سالم={healthy_count} | مرده={len(dead)} | نامعلوم/رد‌شده={len(unknown)}")

    if unknown:
        print(f"\nاین {len(unknown)} مورد چک نشدند (نه سالم نه مرده)؛ برای اطمینان دوباره اسکریپت را اجرا کن:")
        for peer in unknown:
            print(f"  - {peer}")

    if not dead:
        print("\nهیچ مبدای مرده‌ی مطمئنی پیدا نشد.")
    else:
        print(f"\nتعداد {len(dead)} مبدای مطمئنا مرده پیدا شد:")
        for entity_id, peer in dead:
            print(f"  - id={entity_id} peer={peer}")
        confirm = input("\nهمه‌ی این‌ها حذف شوند؟ (بنویس yes برای تایید): ")
        if confirm.strip().lower() == "yes":
            db.conn.execute("PRAGMA foreign_keys=ON")
            for entity_id, _ in dead:
                db.conn.execute("DELETE FROM entities WHERE id=?", (entity_id,))
            db.conn.commit()
            print("حذف شدند.")
        else:
            print("چیزی حذف نشد.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
