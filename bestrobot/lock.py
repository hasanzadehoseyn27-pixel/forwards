from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path


class AppLock:
    def __init__(self, path: Path, stale_seconds: int) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def acquire(self, force: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if self.path.exists() and not force:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                heartbeat = float(data.get("heartbeat", 0))
            except Exception:
                heartbeat = 0
            if now - heartbeat < self.stale_seconds:
                raise RuntimeError("یک نمونه دیگر از برنامه هنوز فعال به نظر می‌رسد. اگر مطمئن هستی مرده، چند دقیقه بعد اجرا کن یا --force-lock بده.")
        self._write()

    def start_heartbeat(self) -> None:
        self._task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while not self._closed:
            self._write()
            await asyncio.sleep(max(5, min(30, self.stale_seconds // 3)))

    def _write(self) -> None:
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "heartbeat": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
