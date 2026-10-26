from __future__ import annotations

import json

import httpx
import pytest

from sniflot.notify.base import NotifyError
from sniflot.notify.messages import new_listings, rub
from sniflot.notify.telegram import TelegramNotifier
from sniflot.repo import Repo
from sniflot.sources.base import Offer

from .conftest import Clock

pytestmark = pytest.mark.anyio


async def test_sends_html_message() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier("123:abc", "42", transport=httpx.MockTransport(handler))
    await notifier.send("x" * 5000)
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/bot123:abc/sendMessage"
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"
    assert len(body["text"]) == 4096


async def test_error_does_not_leak_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    notifier = TelegramNotifier("123:secret", "42", transport=httpx.MockTransport(handler))
    with pytest.raises(NotifyError) as info:
        await notifier.send("hi")
    assert "chat not found" in str(info.value) and "secret" not in str(info.value)


def test_message_escapes_html(repo: Repo, clock: Clock) -> None:
    listing = repo.upsert_offer(
        Offer("s", "1", "https://s/1?a=1&b=2", "<script> & co", 123456), clock()
    ).listing
    text = new_listings("Поиск <1>", [listing], more=2)
    assert "&lt;script&gt; &amp; co" in text and "Поиск &lt;1&gt;" in text
    assert 'href="https://s/1?a=1&amp;b=2"' in text
    assert "…и ещё 2" in text
    assert rub(123456) == "1 234.56 ₽" and rub(100000) == "1 000 ₽"
