"""Notification channels. Add a new one by implementing :class:`Notifier`."""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class NotifyError(Exception):
    pass


class Notifier(Protocol):
    name: str

    async def send(self, text: str) -> None:
        """Deliver a message formatted with Telegram-style HTML (<b>, <a>, <i>)."""
        ...


class LogNotifier:
    """Fallback when no channel is configured: events still land in the log and the UI."""

    name = "log"

    async def send(self, text: str) -> None:
        log.info("notification: %s", text)


class MultiNotifier:
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers
        self.name = "+".join(n.name for n in notifiers) or "none"

    async def send(self, text: str) -> None:
        errors = []
        for notifier in self.notifiers:
            try:
                await notifier.send(text)
            except NotifyError as exc:
                errors.append(f"{notifier.name}: {exc}")
        if errors:
            raise NotifyError("; ".join(errors))
