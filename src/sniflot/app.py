"""Application factory: wires settings, storage, sources, notifier, scheduler, API and UI."""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from sniflot import __version__
from sniflot.api import router as api_router
from sniflot.config import Settings
from sniflot.db import Database
from sniflot.notify.base import LogNotifier, Notifier
from sniflot.notify.telegram import TelegramNotifier
from sniflot.repo import Repo
from sniflot.scheduler import run_scheduler
from sniflot.services import Service
from sniflot.sources.registry import build_sources
from sniflot.web import router as web_router

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def build_notifier(settings: Settings) -> Notifier:
    if settings.telegram_configured:
        return TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    return LogNotifier()


def build_service(settings: Settings) -> Service:
    repo = Repo(Database(settings.db_path))
    return Service(settings, repo, build_sources(settings), build_notifier(settings))


def create_app(settings: Settings | None = None, service: Service | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or build_service(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = None
        if settings.scheduler_enabled:
            task = asyncio.create_task(
                run_scheduler(service, settings.scheduler_tick_seconds, stop)
            )
        try:
            yield
        finally:
            stop.set()
            if task:
                await task
            await service.aclose()
            service.repo.db.close()

    app = FastAPI(title="sniflot", version=__version__, lifespan=lifespan)
    app.state.service = service
    app.state.settings = settings

    if settings.auth_user and settings.auth_password:
        expected = base64.b64encode(
            f"{settings.auth_user}:{settings.auth_password}".encode()
        ).decode()

        @app.middleware("http")
        async def basic_auth(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            header = request.headers.get("Authorization", "")
            scheme, _, value = header.partition(" ")
            if scheme.lower() != "basic" or not secrets.compare_digest(value, expected):
                return Response(
                    "authentication required",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="sniflot"'},
                )
            return await call_next(request)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(api_router)
    app.include_router(web_router)
    return app
