"""A real Chromium (Playwright) shared by sources that need JavaScript.

Ozon renders its catalogue with JavaScript and answers plain HTTP clients with
a challenge page; Avito can optionally be read this way too. This is an
ordinary browser: no stealth patches, no fingerprint spoofing, no CAPTCHA
solving. A persistent profile keeps cookies between runs like a normal user's
browser would. One page is loaded at a time, with a per-source minimum
interval, and images/fonts/media are not downloaded.

Playwright is an optional dependency (``pip install 'sniflot[browser]'`` and
``playwright install chromium``, or the ``full`` Docker image).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sniflot.config import Settings
from sniflot.sources.base import SourceError

log = logging.getLogger(__name__)

_SKIP_RESOURCES = {"image", "media", "font"}


@dataclass
class Page:
    url: str  # final URL after redirects
    status: int | None
    html: str


PageLoader = Callable[[str], Awaitable[Page]]


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


class BrowserFetcher:
    def __init__(
        self,
        settings: Settings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last: dict[str, float] = {}
        self._pw: Any = None
        self._context: Any = None

    async def _ensure(self) -> Any:
        if self._context is not None:
            return self._context
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceError(
                "нужен браузер: установите sniflot[browser] и Chromium или используйте образ full"
            ) from exc
        self._settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        try:
            self._context = await self._pw.chromium.launch_persistent_context(
                str(self._settings.browser_profile_dir),
                headless=self._settings.browser_headless,
                executable_path=self._settings.browser_executable or None,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                viewport={"width": 1366, "height": 900},
            )
        except Exception as exc:
            await self._pw.stop()
            self._pw = None
            raise SourceError(f"браузер не запустился: {exc}") from exc
        await self._context.route("**/*", _skip_heavy)
        log.info(
            "browser started (headless=%s, profile=%s)",
            self._settings.browser_headless,
            self._settings.browser_profile_dir,
        )
        return self._context

    async def get(
        self,
        url: str,
        *,
        source: str,
        min_interval: float,
        wait_selector: str | None = None,
        settle: float = 1.5,
    ) -> Page:
        timeout_ms = self._settings.browser_timeout * 1000
        async with self._lock:
            wait = self._last.get(source, -1e9) + min_interval - self._clock()
            if wait > 0:
                await self._sleep(wait)
            context = await self._ensure()
            page = await context.new_page()
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if wait_selector:
                    # A challenge page never shows the selector; the caller
                    # classifies whatever was rendered once this times out.
                    with contextlib.suppress(Exception):
                        await page.wait_for_selector(
                            wait_selector, state="attached", timeout=min(timeout_ms, 20_000)
                        )
                if settle:
                    await page.wait_for_timeout(settle * 1000)
                return Page(
                    url=page.url,
                    status=response.status if response is not None else None,
                    html=await page.content(),
                )
            except SourceError:
                raise
            except Exception as exc:
                if "closed" in str(exc).lower():
                    await self._reset()
                # Playwright appends a multi-line call log; the first line is the cause.
                first = (str(exc).strip().splitlines() or [""])[0]
                raise SourceError(f"браузер: {first}") from exc
            finally:
                self._last[source] = self._clock()
                with contextlib.suppress(Exception):
                    await page.close()

    def loader(
        self, source: str, min_interval: float, wait_selector: str | None = None
    ) -> PageLoader:
        async def load(url: str) -> Page:
            return await self.get(
                url, source=source, min_interval=min_interval, wait_selector=wait_selector
            )

        return load

    async def _reset(self) -> None:
        context, pw = self._context, self._pw
        self._context = self._pw = None
        with contextlib.suppress(Exception):
            if context is not None:
                await context.close()
        with contextlib.suppress(Exception):
            if pw is not None:
                await pw.stop()

    async def aclose(self) -> None:
        await self._reset()


async def _skip_heavy(route: Any) -> None:
    if route.request.resource_type in _SKIP_RESOURCES:
        await route.abort()
    else:
        await route.continue_()
