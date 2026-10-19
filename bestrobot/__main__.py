import sys

# زیر PM2/سرویس ویندوز کنسول ممکن است cp1252 باشد؛ چاپ متن فارسی نباید برنامه را بیندازد
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from .cli import main  # noqa: E402


if __name__ == "__main__":
    main()
