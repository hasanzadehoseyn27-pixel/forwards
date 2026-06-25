from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _path(value: str | None, default: str) -> Path:
    raw = value or default
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(int(part))
    return ids


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    admin_ids: set[int]
    instance_name: str
    data_dir: Path
    log_dir: Path
    db_path: Path
    user_session: Path
    bot_session: Path
    poll_interval_seconds: int
    repeat_scan_seconds: int
    worker_count: int
    max_parallel_sends: int
    min_send_delay_seconds: float
    max_job_attempts: int
    lock_stale_seconds: int
    watchdog_interval_seconds: int
    watchdog_timeout_seconds: int
    proxy_type: str | None
    proxy_host: str | None
    proxy_port: int | None
    proxy_username: str | None
    proxy_password: str | None

    @classmethod
    def load(cls) -> "Settings":
        load_env_file(PROJECT_DIR / ".env")
        data_dir = _path(os.getenv("DATA_DIR"), "data")
        log_dir = _path(os.getenv("LOG_DIR"), "logs")
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        missing = [name for name in ("API_ID", "API_HASH", "BOT_TOKEN") if not os.getenv(name)]
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"تنظیمات ناقص است. این مقدارها در .env نیستند: {joined}")

        instance = os.getenv("INSTANCE_NAME", "bestrobot").strip() or "bestrobot"
        return cls(
            api_id=int(os.environ["API_ID"]),
            api_hash=os.environ["API_HASH"],
            bot_token=os.environ["BOT_TOKEN"],
            admin_ids=_admin_ids(),
            instance_name=instance,
            data_dir=data_dir,
            log_dir=log_dir,
            db_path=data_dir / f"{instance}.sqlite3",
            user_session=_path(os.getenv("USER_SESSION"), f"data/{instance}-user.session"),
            bot_session=_path(os.getenv("BOT_SESSION"), f"data/{instance}-bot.session"),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 20),
            repeat_scan_seconds=_int("REPEAT_SCAN_SECONDS", 30),
            worker_count=_int("WORKER_COUNT", 3),
            max_parallel_sends=_int("MAX_PARALLEL_SENDS", 2),
            min_send_delay_seconds=_float("MIN_SEND_DELAY_SECONDS", 1.5),
            max_job_attempts=_int("MAX_JOB_ATTEMPTS", 5),
            lock_stale_seconds=_int("LOCK_STALE_SECONDS", 120),
            watchdog_interval_seconds=_int("WATCHDOG_INTERVAL_SECONDS", 180),
            watchdog_timeout_seconds=_int("WATCHDOG_TIMEOUT_SECONDS", 30),
            proxy_type=_optional("PROXY_TYPE"),
            proxy_host=_optional("PROXY_HOST"),
            proxy_port=_int("PROXY_PORT", 0) or None,
            proxy_username=_optional("PROXY_USERNAME"),
            proxy_password=_optional("PROXY_PASSWORD"),
        )

    def telethon_proxy(self) -> Any | None:
        if not self.proxy_host or not self.proxy_port:
            return None

        try:
            import socks
            import python_socks
        except ImportError as exc:
            raise RuntimeError("Proxy dependencies are not installed. Run: python -m pip install -r requirements.txt") from exc

        proxy_type = (self.proxy_type or "socks5").lower()
        mapping = {
            "socks5": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "http": socks.HTTP,
        }
        if proxy_type not in mapping:
            raise RuntimeError("PROXY_TYPE must be one of: socks5, socks4, http")

        if self.proxy_username or self.proxy_password:
            return (
                mapping[proxy_type],
                self.proxy_host,
                self.proxy_port,
                True,
                self.proxy_username,
                self.proxy_password,
            )
        return (mapping[proxy_type], self.proxy_host, self.proxy_port)