from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sniflot.app import create_app
from sniflot.config import Settings
from sniflot.services import Service

from .conftest import Clock, FakeNotifier, FakeSource


@pytest.fixture
def client(service: Service) -> Iterator[TestClient]:
    settings = Settings(db_path=Path(":memory:"), scheduler_enabled=False)
    with TestClient(create_app(settings, service)) as c:
        yield c


def test_health_and_sources(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok" and body["sources"][0]["id"] == "fake"


def test_search_api(client: TestClient, source: FakeSource) -> None:
    source.offers = [source.offer("1", 300, "A"), source.offer("2", 100, "B чехол")]
    body = client.get("/api/v1/search", params={"q": "x", "sort": "price_asc"}).json()
    assert [r["external_id"] for r in body["results"]] == ["2", "1"]
    assert body["results"][0]["price"] == 100.0
    body = client.get("/api/v1/search", params={"q": "x", "exclude": "чехол"}).json()
    assert [r["external_id"] for r in body["results"]] == ["1"]
    assert client.get("/api/v1/search", params={"q": "x", "sources": "nope"}).status_code == 422


def test_saved_search_crud_and_run(client: TestClient, source: FakeSource) -> None:
    created = client.post(
        "/api/v1/searches",
        json={"query": "наушники", "max_price": 5000, "exclude_words": ["Чехол"]},
    )
    assert created.status_code == 201
    sid = created.json()["id"]
    assert created.json()["exclude_words"] == ["чехол"]
    patched = client.patch(
        f"/api/v1/searches/{sid}", json={"interval_minutes": 15, "enabled": False}
    )
    assert patched.json()["interval_minutes"] == 15 and patched.json()["enabled"] is False
    source.offers = [source.offer("1", 1000)]
    assert client.post(f"/api/v1/searches/{sid}/run").json()["status"] == "ok"
    results = client.get(f"/api/v1/searches/{sid}/results").json()
    assert results[0]["external_id"] == "1"
    assert client.delete(f"/api/v1/searches/{sid}").status_code == 204
    assert client.get(f"/api/v1/searches/{sid}").status_code == 404


def test_watch_by_url_history_export_and_favorite(client: TestClient, source: FakeSource) -> None:
    source.items["5"] = source.offer("5", 1500, available=True)
    created = client.post(
        "/api/v1/watches", json={"url": "https://fake.example/item/5", "target_price": 1000}
    )
    assert created.status_code == 201
    watch = created.json()
    assert watch["target_price"] == 1000.0 and watch["listing"]["price"] == 1500.0
    lid = watch["listing_id"]
    source.items["5"] = source.offer("5", 1200, available=True)
    assert client.post(f"/api/v1/watches/{watch['id']}/check").json()["status"] == "ok"

    detail = client.get(f"/api/v1/listings/{lid}").json()
    assert [p["price"] for p in detail["history"]] == [1500.0, 1200.0]
    csv_text = client.get(f"/api/v1/listings/{lid}/history.csv").text
    assert csv_text.splitlines()[0] == "at,price_rub,available" and "1200.0" in csv_text
    assert client.get(f"/api/v1/listings/{lid}/history.json").json()["listing"]["id"] == lid

    assert client.post(f"/api/v1/listings/{lid}/favorite", json={"favorite": True}).json()[
        "favorite"
    ]
    assert client.get("/api/v1/listings/favorites").json()[0]["id"] == lid

    bad = client.post("/api/v1/watches", json={"url": "https://example.org/x"})
    assert bad.status_code == 422


def test_events_and_notify_test(client: TestClient, notifier: FakeNotifier) -> None:
    body = client.post("/api/v1/notify/test").json()
    assert body["delivered"] and notifier.sent
    assert client.get("/api/v1/events").json()[0]["kind"] == "test"


def test_web_pages_render(client: TestClient, source: FakeSource) -> None:
    source.offers = [source.offer("1", 300, "Наушники <b>"), source.offer("2", 100, "Наушники 2")]
    source.items["1"] = source.offer("1", 300, "Наушники <b>")
    assert "Отслеживаемые" in client.get("/").text
    page = client.get("/search", params={"q": "наушники", "view": "cards"}).text
    assert "Наушники &lt;b&gt;" in page and "Сохранить как поиск" in page
    assert client.get("/search", params={"q": "наушники"}).status_code == 200

    response = client.post(
        "/searches",
        data={
            "query": "наушники",
            "sources": "fake",
            "max_price": "1 000",
            "exclude_words": "чехол",
            "interval_minutes": "30",
            "notify_new": "on",
            "notify_drop_pct": "5",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200 and "базовую линию" in detail.text
    sid = response.headers["location"].split("/")[2].split("?")[0]
    assert client.post(f"/searches/{sid}/run").status_code == 200

    response = client.post(
        "/watches", data={"url": "https://fake.example/item/1"}, follow_redirects=False
    )
    assert response.status_code == 303
    listing_page = client.get(response.headers["location"])
    assert "<svg" in listing_page.text and "Не отслеживать" in listing_page.text

    for path in ("/favorites", "/settings", "/add?url=https://x", "/"):
        assert client.get(path).status_code == 200, path
    assert "javascript:" in client.get("/settings").text
    bad = client.post("/watches", data={"url": "https://example.org/x"}, follow_redirects=False)
    assert "/add?url=" in bad.headers["location"] and "err=" in bad.headers["location"]


def test_basic_auth(service: Service) -> None:
    settings = Settings(scheduler_enabled=False, auth_user="me", auth_password="pw")
    with TestClient(create_app(settings, service)) as c:
        assert c.get("/api/v1/health").status_code == 401
        token = base64.b64encode(b"me:pw").decode()
        assert (
            c.get("/api/v1/health", headers={"Authorization": f"Basic {token}"}).status_code == 200
        )


def test_mcp_tools_registered(service: Service) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from sniflot.mcp_server import build

    tools = asyncio.run(build(service).list_tools())
    names = {t.name for t in tools}
    assert {"search_offers", "add_watch", "price_history", "list_watches", "create_search"} <= names


def test_clock_fixture_is_used(clock: Clock, service: Service) -> None:
    assert service.clock() == clock.now
