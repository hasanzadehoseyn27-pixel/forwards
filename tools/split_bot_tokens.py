"""ربات‌های یک بازه را از bot_tokens.txt این سرور جدا می‌کند تا به سرور دیگر منتقل شوند.

    python tools/split_bot_tokens.py 20 28

- توکن ربات‌های شماره‌ی 20 تا 28 (همان شماره‌هایی که bots-status نشان می‌دهد)
  در فایل moved_bot_tokens.txt نوشته می‌شود (برای کپی به سرور جدید).
- در bot_tokens.txt به‌جای هر کدام یک خط «جانگه‌دار» گذاشته می‌شود تا شماره‌ی
  بقیه‌ی ربات‌ها عوض نشود (ربات جانگه‌دار خودکار «خراب» حساب و کنار گذاشته می‌شود).
- از فایل اصلی قبل از تغییر یک نسخه‌ی پشتیبان ساخته می‌شود.
توکن‌ها هیچ‌جا چاپ نمی‌شوند.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


def token_of(raw: str) -> str | None:
    # همان منطق load_bot_tokens در bestrobot/bot_pool.py
    line = raw.strip().lstrip("﻿")
    if not line or line.startswith("#"):
        return None
    if "=" in line and ":" in line.split("=", 1)[1]:
        line = line.split("=", 1)[1].strip()
    line = line.split()[0]
    if ":" not in line:
        return None
    return line


def main() -> None:
    if len(sys.argv) != 3:
        print("استفاده: python tools/split_bot_tokens.py 20 28")
        sys.exit(1)
    first, last = int(sys.argv[1]), int(sys.argv[2])
    path = PROJECT_DIR / "bot_tokens.txt"
    lines = path.read_text(encoding="utf-8").splitlines()

    seen: set[str] = set()
    index = 0
    moved: list[str] = []
    out_lines: list[str] = []
    for raw in lines:
        tok = token_of(raw)
        if tok is None or tok in seen:
            out_lines.append(raw)
            continue
        seen.add(tok)
        index += 1
        if first <= index <= last:
            moved.append(tok)
            out_lines.append(f"0:moved-to-other-server-bot-{index}  # منتقل شد")
        else:
            out_lines.append(raw)

    if len(moved) != last - first + 1:
        print(f"فقط {len(moved)} ربات در این بازه پیدا شد؛ هیچ تغییری داده نشد.")
        sys.exit(1)

    backup = path.with_name(f"bot_tokens.backup-{time.strftime('%Y%m%d-%H%M%S')}.txt")
    shutil.copyfile(path, backup)
    (PROJECT_DIR / "moved_bot_tokens.txt").write_text("\n".join(moved) + "\n", encoding="utf-8")
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"{len(moved)} ربات (شماره‌ی {first} تا {last}) جدا شد.")
    print(f"پشتیبان: {backup.name}")
    print("فایل برای سرور جدید: moved_bot_tokens.txt")


if __name__ == "__main__":
    main()
