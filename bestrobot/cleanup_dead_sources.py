"""
اسکریپت نگهداری: همه‌ی مبداها (source) را با خود تلگرام چک می‌کند و هرکدام که
دیگر معتبر نیستند (یوزرنیم عوض شده/حذف شده) را از دیتابیس پاک می‌کند.

اجرا:
    pm2 stop <نام-پروسه>
    .venv\\Scripts\\python.exe -m bestrobot.cleanup_dead_sources
    pm2 start <نام-پروسه>

توجه: قبل از اجرا حتما پروسه‌ی اصلی را با pm2 stop متوقف کن، چون هر دو
از یک فایل سشن استفاده می‌کنند و اجرای همزمان می‌تواند مشکل ایجاد کند.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError

from .config import Settings
from .db import Database


async def check_one(client: TelegramClient, peer: str) -> tuple[bool, str]:
    """برمی‌گرداند: (مرده است یا نه, دلیل)."""
    for _ in range(3):
        try:
            await client.get_entity(peer)
            return False, ""
        except FloodWaitError as exc:
            wait_seconds = int(getattr(exc, "seconds", 30)) + 2
            if wait_seconds > 90:
                print(f"  محدودیت طولانی ({wait_seconds} ثانیه) روی {peer}؛ فعلا رد می‌شود، بعدا دوباره اجرا کن.")
                return False, "flood-wait-too-long-skipped"
            print(f"  محدودیت موقت روی {peer}؛ {wait_seconds} ثانیه صبر و تلاش دوباره...")
            await asyncio.sleep(wait_seconds)
            continue
        except RPCError as exc:
            return True, exc.__class__.__name__
        except ValueError as exc:
            return True, str(exc)
    return False, "بعد از چند بار تلاش هنوز FloodWait بود؛ رد شد (مرده حساب نشد)"


async def main() -> None:
    settings = Settings.load()
    db = Database(settings.db_path)

    proxy = settings.telethon_proxy()
    client = TelegramClient(str(settings.user_session), settings.api_id, settings.api_hash, proxy=proxy)
    await client.start()

    sources = db.fetchall("SELECT id, peer, title FROM entities WHERE kind='source'")
    print(f"تعداد کل مبداها: {len(sources)}")

    dead: list[tuple[int, str]] = []
    for index, row in enumerate(sources, start=1):
        peer = str(row["peer"])
        is_dead, reason = await check_one(client, peer)
        if is_dead:
            dead.append((int(row["id"]), peer))
            print(f"[{index}/{len(sources)}] مرده: {peer} ({reason})")
        else:
            print(f"[{index}/{len(sources)}] سالم: {peer}")
        await asyncio.sleep(2)  # فاصله‌ی امن‌تر بین درخواست‌ها

    if not dead:
        print("هیچ مبدای مرده‌ای پیدا نشد.")
    else:
        print(f"\nتعداد {len(dead)} مبدای مرده پیدا شد:")
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
