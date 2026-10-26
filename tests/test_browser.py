"""Real Chromium against a local server. Skipped when Playwright/Chromium is missing.

Point ``SNIFLOT_TEST_CHROMIUM`` at a Chromium binary if Playwright's own
browsers are not installed.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sniflot.config import Settings
from sniflot.sources.base import SearchParams, SourceError
from sniflot.sources.browser import BrowserFetcher, playwright_available
from sniflot.sources.ozon import OzonSource

from .conftest import fixture_text

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not playwright_available(), reason="playwright is not installed"),
]

HITS: dict[str, int] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        HITS[path] = HITS.get(path, 0) + 1
        if path == "/search/":
            # Like Ozon: the HTML has no products until JavaScript renders them.
            tiles = fixture_text("ozon_search_dom.html").split('<div id="layoutPage">')[1]
            script = (
                "setTimeout(() => { document.getElementById('root').innerHTML = "
                f"{json.dumps(tiles)}; }}, 400);"
            )
            body = (
                '<html><head><title>s</title></head><body><div id="root">загрузка…</div>'
                f'<img src="/img.png"><script>{script}</script></body></html>'
            )
            self._send(200, body)
        elif path.startswith("/product/"):
            self._send(200, fixture_text("ozon_product.html"))
        elif path == "/set-cookie":
            self._send(200, "<p>ok</p>", {"Set-Cookie": "seen=1; Max-Age=3600; Path=/"})
        elif path == "/echo-cookie":
            self._send(200, f"<p id='c'>{self.headers.get('Cookie', '')}</p>")
        else:
            self._send(404, "<h1>Такой страницы не существует</h1>")


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    exe = os.environ.get("SNIFLOT_TEST_CHROMIUM", "")
    if not exe and Path("/opt/pw-browsers/chromium").exists():
        exe = "/opt/pw-browsers/chromium"
    return Settings(
        browser_executable=exe, browser_profile_dir=tmp_path / "profile", browser_timeout=20
    )


async def _fetcher(settings: Settings) -> BrowserFetcher:
    fetcher = BrowserFetcher(settings)
    try:
        await fetcher._ensure()
    except SourceError as exc:
        pytest.skip(f"chromium unavailable: {exc}")
    return fetcher


async def test_renders_javascript_and_reports_status(server: str, settings: Settings) -> None:
    fetcher = await _fetcher(settings)
    try:
        page = await fetcher.get(
            f"{server}/search/?text=x",
            source="t",
            min_interval=0,
            wait_selector="a[href*='/product/']",
        )
        assert page.status == 200 and "/product/naushniki-tws-mini-2222222/" in page.html
        assert HITS.get("/img.png", 0) == 0  # images are not downloaded
        missing = await fetcher.get(f"{server}/nope", source="t", min_interval=0, settle=0)
        assert missing.status == 404
    finally:
        await fetcher.aclose()


async def test_ozon_source_over_real_browser(server: str, settings: Settings) -> None:
    fetcher = await _fetcher(settings)

    def local(selector: str):  # type: ignore[no-untyped-def]
        async def load(url: str):  # type: ignore[no-untyped-def]
            return await fetcher.get(
                url.replace("https://www.ozon.ru", server),
                source="ozon",
                min_interval=0,
                wait_selector=selector,
            )

        return load

    ozon = OzonSource(settings, local("a[href*='/product/']"), local("h1"), fetcher.aclose)
    try:
        result = await ozon.search(SearchParams("наушники"))
        assert [o.external_id for o in result.offers] == ["2222222", "3333333"]
        offer = await ozon.fetch("https://www.ozon.ru/product/naushniki-sonic-x1-1234567/")
        assert offer.price == 229000 and offer.available is True
    finally:
        await ozon.aclose()


async def test_profile_keeps_cookies_between_runs(server: str, settings: Settings) -> None:
    first = await _fetcher(settings)
    await first.get(f"{server}/set-cookie", source="t", min_interval=0, settle=0)
    await first.aclose()
    second = await _fetcher(replace(settings))
    try:
        page = await second.get(f"{server}/echo-cookie", source="t", min_interval=0, settle=0)
        assert "seen=1" in page.html
    finally:
        await second.aclose()
