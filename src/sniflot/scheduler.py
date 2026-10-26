"""Background loop: every tick, run the searches and watches whose time has come.

Schedules live in SQLite (``next_run_at``), so they survive restarts and need
no separate job store.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sniflot.services import Service

log = logging.getLogger(__name__)


async def run_scheduler(service: Service, tick: float, stop: asyncio.Event) -> None:
    log.info("scheduler started, tick %.0fs", tick)
    while not stop.is_set():
        try:
            done = await service.run_due()
            if done:
                log.info("scheduler: %d task(s) done", done)
        except Exception:
            log.exception("scheduler tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=tick)
    log.info("scheduler stopped")
