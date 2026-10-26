"""Command line: ``sniflot serve | probe | run-due | mcp``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sniflot.config import Settings
from sniflot.sources.base import Condition, SearchParams, Sort, SourceError
from sniflot.sources.registry import build_sources


async def probe(settings: Settings, source_id: str, query: str, item: str | None) -> int:
    """Talk to a real source once and print what came back — the live check."""
    sources = build_sources(settings)
    source = sources.get(source_id)
    if source is None:
        print(f"source {source_id!r} is not enabled; enabled: {', '.join(sources) or 'none'}")
        return 2
    code = 0
    try:
        result = await source.search(
            SearchParams(query, sort=Sort.RELEVANCE, condition=Condition.ANY)
        )
        print(f"search OK: {len(result.offers)} offers (total reported: {result.total})")
        print(f"site url: {result.source_url}")
        for warning in result.warnings:
            print(f"warning: {warning}")
        for offer in result.offers[:5]:
            price = "—" if offer.price is None else f"{offer.price / 100:.2f} ₽"
            print(f"  {price:>14}  {offer.title[:70]}  {offer.url}")
        if not result.offers:
            code = 1
        ref = item or (result.offers[0].url if result.offers else None)
        if ref:
            offer = await source.fetch(ref)
            price = "—" if offer.price is None else f"{offer.price / 100:.2f} ₽"
            print(f"fetch OK: {price} available={offer.available} {offer.title[:70]}")
    except SourceError as exc:
        print(f"FAILED: {exc.__class__.__name__}: {exc}")
        code = 1
    finally:
        await source.aclose()
    return code


async def run_due(settings: Settings) -> int:
    from sniflot.app import build_service

    service = build_service(settings)
    try:
        done = await service.run_due()
        print(f"{done} task(s) done")
    finally:
        await service.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sniflot")
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run web UI, API and scheduler")
    p = sub.add_parser("probe", help="check a source against the real site")
    p.add_argument("source")
    p.add_argument("query")
    p.add_argument("--item", help="listing URL to fetch (default: first search result)")
    sub.add_parser("run-due", help="run due searches/watches once and exit (for cron)")
    sub.add_parser("mcp", help="MCP server over stdio (needs the 'mcp' extra)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    settings = Settings.from_env()
    if args.command == "serve":
        import uvicorn

        from sniflot.app import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        return 0
    if args.command == "probe":
        return asyncio.run(probe(settings, args.source, args.query, args.item))
    if args.command == "run-due":
        return asyncio.run(run_due(settings))
    if args.command == "mcp":
        from sniflot.mcp_server import run

        run(settings)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
