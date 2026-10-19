"""انتقال گروه‌های فوروارد بین دو نصب ربات (مثلاً از یک سرور به سرور دیگر).

export-groups: گروه‌های انتخابی را با مبداها، مقصدها، حالت، فاصله، ایموجی
لازم، فیلتر خط‌خورده و کلمات ممنوعه در یک فایل JSON می‌نویسد. بدون --groups
فقط فهرست گروه‌ها را نشان می‌دهد (هیچ چیز را تغییر نمی‌دهد).

import-groups: همان فایل را در دیتابیس این نصب می‌سازد. گروه‌ها «خاموش»
ساخته می‌شوند تا بعد از آماده‌شدن اکانت و ربات‌ها از پنل روشن شوند.

هیچ‌کدام به اکانت تلگرام دست نمی‌زنند؛ پس وقتی ربات در حال اجراست هم امن‌اند.
"""

from __future__ import annotations

import json
from pathlib import Path

from .bot_pool import BotPool, load_bot_tokens
from .config import Settings, normalize_peer
from .db import Database, now_ts

FORMAT_VERSION = 1


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.append(int(part))
    return ids


def _print_groups(db: Database) -> None:
    groups = db.fetchall("SELECT * FROM forward_groups ORDER BY id")
    if not groups:
        print("هیچ گروه فورواردی نیست.")
        return
    for g in groups:
        gid = int(g["id"])
        state = "روشن" if g["enabled"] else "خاموش"
        minutes = int(g["interval_seconds"]) // 60
        print(f"[{gid}] {g['name']} — {state} — حالت {g['mode']} — هر {minutes} دقیقه")
        sources = db.group_sources(gid)
        dests = db.group_destinations(gid)
        print("     مبداها: " + ("، ".join(str(s["peer"]) for s in sources) or "-"))
        print("     مقصدها: " + ("، ".join(str(d["peer"]) for d in dests) or "-"))


async def command_export_groups(settings: Settings, db: Database, groups_arg: str, out: str) -> None:
    if not groups_arg.strip():
        _print_groups(db)
        print()
        print("برای خروجی گرفتن: export-groups --groups 3,7 --out groups.json")
        return

    group_ids = _parse_ids(groups_arg)
    # شناسه‌ی عددی مقصدها با Bot API (بدون اکانت) تا سرور جدید به عضویت اکانت نیازی نداشته باشد
    pool: BotPool | None = None
    tokens = load_bot_tokens(settings.bot_tokens_file)
    if tokens:
        pool = BotPool(tokens, db, relay_delay_seconds=settings.relay_delay_seconds)
        await pool.start(0, poll=False)

    try:
        exported = []
        for gid in group_ids:
            g = db.get_group(gid)
            if not g:
                print(f"گروه {gid} پیدا نشد؛ رد شد.")
                continue
            words = [str(r["word"]) for r in db.list_banned_words(gid)]
            sources = [{"title": str(s["title"]), "peer": str(s["peer"])} for s in db.group_sources(gid)]
            dests = []
            for d in db.group_destinations(gid):
                peer = str(d["peer"]).strip()
                chat_id = None
                if peer.lstrip("-").isdigit():
                    chat_id = int(peer)
                elif pool:
                    chat_id = await pool.resolve_chat_id("@" + normalize_peer(peer))
                dests.append({"title": str(d["title"]), "peer": peer, "chat_id": chat_id})
            exported.append(
                {
                    "name": str(g["name"]),
                    "mode": str(g["mode"]),
                    "interval_seconds": int(g["interval_seconds"]),
                    "required_emoji": g["required_emoji"],
                    "block_strikethrough": int(g["block_strikethrough"]),
                    "banned_words": words,
                    "sources": sources,
                    "destinations": dests,
                }
            )
            print(f"✅ [{gid}] {g['name']}: {len(sources)} مبدا، {len(dests)} مقصد")
            for d in dests:
                mark = d["chat_id"] if d["chat_id"] is not None else "⚠️ شناسه پیدا نشد"
                print(f"     مقصد {d['peer']} → {mark}")
    finally:
        if pool:
            await pool.stop()

    path = Path(out)
    path.write_text(
        json.dumps({"version": FORMAT_VERSION, "groups": exported}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nفایل ساخته شد: {path.resolve()}")


def command_import_groups(db: Database, file_arg: str) -> None:
    path = Path(file_arg)
    if not path.exists():
        print(f"فایل پیدا نشد: {path}")
        return
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    groups = data.get("groups") or []
    if not groups:
        print("گروهی داخل فایل نیست.")
        return

    for item in groups:
        name = str(item["name"]).strip()
        if db.find_groups_by_name(name):
            print(f"گروه «{name}» از قبل هست؛ دوباره ساخته نشد.")
            continue

        gid = db.add_group(name)
        # خاموش بساز؛ بعد از آماده‌شدن اکانت/ربات‌ها از پنل روشن کن
        db.update_group(
            gid,
            mode=item.get("mode", "once"),
            interval_seconds=int(item.get("interval_seconds", 1800)),
            enabled=0,
        )
        db.set_group_required_emoji(gid, item.get("required_emoji") or None)
        db.execute(
            "UPDATE forward_groups SET block_strikethrough=? WHERE id=?",
            (int(item.get("block_strikethrough", 1)), gid),
        )
        # کلمات ممنوعه دقیقاً مثل گروه اصلی (نه پیش‌فرض‌ها)
        db.execute("DELETE FROM group_banned_words WHERE group_id=?", (gid,))
        for word in item.get("banned_words") or []:
            db.execute(
                "INSERT OR IGNORE INTO group_banned_words(group_id, word, created_at) VALUES(?, ?, ?)",
                (gid, str(word), now_ts()),
            )

        for s in item.get("sources") or []:
            sid = db.add_entity("source", str(s["title"]), str(s["peer"]))
            db.link_entity(gid, sid, "source")

        for d in item.get("destinations") or []:
            # مقصد همان‌طور که در سرور قبلی ثبت شده (یوزرنیم یا شناسه‌ی عددی) ذخیره
            # می‌شود؛ موتور اگر اکانت آن را نشناسد، شناسه را از ربات‌های عضو می‌گیرد.
            peer = str(d["peer"]).strip()
            did = db.add_entity("destination", str(d["title"]), peer)
            db.link_entity(gid, did, "destination")

        minutes = int(item.get("interval_seconds", 1800)) // 60
        print(
            f"✅ «{name}» ساخته شد (خاموش) — {len(item.get('sources') or [])} مبدا، "
            f"{len(item.get('destinations') or [])} مقصد، هر {minutes} دقیقه"
        )

    print()
    print("قدم بعد: bots-start ، bots-sync ، بعد از پنل گروه‌ها را روشن کن.")
