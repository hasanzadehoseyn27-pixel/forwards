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
from telethon.errors import RPCError

from .config import Settings
from .db import Database


async def main() -> None:
    settings = Settings.load()
    db = Database(settings.db_path)

    proxy = settings.telethon_proxy()
    client = TelegramClient(str(settings.user_session), settings.api_id, settings.api_hash, proxy=proxy)
    await client.start()

    sources = db.fetchall("SELECT id, peer, title FROM entities WHERE kind='source'")
    print(f"تعداد کل مبداها: {len(sources)}")

    dead: list[tuple[int, str]] = []
    for row in sources:
        peer = str(row["peer"])
        try:
            await client.get_entity(peer)
        except (ValueError, RPCError) as exc:
            dead.append((int(row["id"]), peer))
            print(f"مرده: {peer} ({exc.__class__.__name__})")
        await asyncio.sleep(1)  # برای اینکه به تلگرام فشار ناگهانی وارد نکنیم

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
