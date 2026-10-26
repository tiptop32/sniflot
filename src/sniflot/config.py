"""Settings read from environment variables (prefix ``SNIFLOT_``)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

WB_DEFAULT_SEARCH_URLS = (
    # The public search endpoint is undocumented and its version changes; the
    # adapter tries these in order and sticks to the first one that answers.
    "https://search.wb.ru/exactmatch/ru/common/v9/search",
    "https://search.wb.ru/exactmatch/ru/common/v18/search",
    "https://search.wb.ru/exactmatch/ru/common/v13/search",
)


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(value: str | None, default: float) -> float:
    return float(value) if value else default


def _int(value: str | None, default: int) -> int:
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("data/sniflot.db")
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = ""
    tz: str = "Europe/Moscow"
    auth_user: str = ""
    auth_password: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    scheduler_enabled: bool = True
    scheduler_tick_seconds: float = 20.0
    default_search_interval: int = 30
    default_watch_interval: int = 60

    http_timeout: float = 20.0
    user_agent: str = "Mozilla/5.0"

    wb_enabled: bool = True
    wb_dest: str = "-1257786"
    wb_search_urls: tuple[str, ...] = WB_DEFAULT_SEARCH_URLS
    wb_card_url: str = "https://card.wb.ru/cards/v4/detail"
    wb_min_interval: float = 1.5

    avito_enabled: bool = True
    avito_backend: str = "http"  # "http" or "browser"
    avito_region: str = "all"
    avito_min_interval: float = 30.0

    ozon_enabled: bool = True
    ozon_min_interval: float = 20.0

    # Real Chromium via Playwright, needed for Ozon and optional for Avito.
    browser_headless: bool = True
    browser_executable: str = ""
    browser_profile_dir: Path = Path("data/browser")
    browser_timeout: float = 45.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env

        def get(name: str) -> str | None:
            return e.get(f"SNIFLOT_{name}")

        urls = get("WB_SEARCH_URLS")
        return cls(
            db_path=Path(get("DB_PATH") or cls.db_path),
            host=get("HOST") or cls.host,
            port=_int(get("PORT"), cls.port),
            public_url=(get("PUBLIC_URL") or "").rstrip("/"),
            tz=get("TZ") or cls.tz,
            auth_user=get("AUTH_USER") or "",
            auth_password=get("AUTH_PASSWORD") or "",
            telegram_bot_token=get("TELEGRAM_BOT_TOKEN") or "",
            telegram_chat_id=get("TELEGRAM_CHAT_ID") or "",
            scheduler_enabled=_bool(get("SCHEDULER_ENABLED"), cls.scheduler_enabled),
            scheduler_tick_seconds=_float(get("SCHEDULER_TICK"), cls.scheduler_tick_seconds),
            default_search_interval=_int(get("SEARCH_INTERVAL"), cls.default_search_interval),
            default_watch_interval=_int(get("WATCH_INTERVAL"), cls.default_watch_interval),
            http_timeout=_float(get("HTTP_TIMEOUT"), cls.http_timeout),
            user_agent=get("USER_AGENT") or cls.user_agent,
            wb_enabled=_bool(get("WB_ENABLED"), cls.wb_enabled),
            wb_dest=get("WB_DEST") or cls.wb_dest,
            wb_search_urls=tuple(u.strip() for u in urls.split(",") if u.strip())
            if urls
            else WB_DEFAULT_SEARCH_URLS,
            wb_card_url=get("WB_CARD_URL") or cls.wb_card_url,
            wb_min_interval=_float(get("WB_MIN_INTERVAL"), cls.wb_min_interval),
            avito_enabled=_bool(get("AVITO_ENABLED"), cls.avito_enabled),
            avito_backend=(get("AVITO_BACKEND") or cls.avito_backend).lower(),
            avito_region=get("AVITO_REGION") or cls.avito_region,
            avito_min_interval=_float(get("AVITO_MIN_INTERVAL"), cls.avito_min_interval),
            ozon_enabled=_bool(get("OZON_ENABLED"), cls.ozon_enabled),
            ozon_min_interval=_float(get("OZON_MIN_INTERVAL"), cls.ozon_min_interval),
            # Headful (under Xvfb in the Docker image) when a display is available:
            # Ozon treats headless Chromium more strictly.
            browser_headless=_bool(get("BROWSER_HEADLESS"), not e.get("DISPLAY")),
            browser_executable=get("BROWSER_EXECUTABLE") or "",
            browser_profile_dir=Path(
                get("BROWSER_PROFILE_DIR") or Path(get("DB_PATH") or cls.db_path).parent / "browser"
            ),
            browser_timeout=_float(get("BROWSER_TIMEOUT"), cls.browser_timeout),
        )

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)
