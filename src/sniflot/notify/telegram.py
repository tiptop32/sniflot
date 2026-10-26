"""Telegram Bot API channel (``sendMessage``), no extra dependencies."""

from __future__ import annotations

import httpx

from sniflot.notify.base import NotifyError

LIMIT = 4096


class TelegramNotifier:
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._transport = transport
        self._timeout = timeout

    async def send(self, text: str) -> None:
        if len(text) > LIMIT:
            text = text[: LIMIT - 1] + "…"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifyError(f"network error: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            # The response body names the problem (bad chat id, blocked bot); the
            # token is part of the URL and must not end up in logs.
            try:
                description = response.json().get("description", "")
            except ValueError:
                description = ""
            raise NotifyError(f"HTTP {response.status_code} {description}".strip())
