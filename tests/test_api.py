import time

import httpx
import pytest

from api import create_app
from bus import Hub
from db import Database

TOKEN = "t0ken-for-tests-1234567890"


@pytest.fixture
async def client(tmp_path):
    db = Database(tmp_path / "a.db")
    await db.start()
    app = create_app(db, Hub(), TOKEN, bad_token_delay=0.8)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await db.stop()


async def test_ping_200_with_token(client):
    r = await client.get("/api/ping", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_x_app_token_header_also_works(client):
    assert (await client.get("/api/state", headers={"X-App-Token": TOKEN})).status_code == 200


@pytest.mark.parametrize("path", ["/api/ping", "/api/state", "/api/health"])
async def test_401_without_token(client, path):
    r = await client.get(path)
    assert r.status_code == 401


async def test_token_in_url_is_not_accepted(client):
    assert (await client.get(f"/api/state?token={TOKEN}")).status_code == 401


async def test_bad_token_is_delayed_08s(client):
    t = time.perf_counter()
    r = await client.get("/api/ping", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and time.perf_counter() - t >= 0.75


async def test_empty_server_token_fails_closed(tmp_path):
    db = Database(tmp_path / "b.db")
    await db.start()
    app = create_app(db, Hub(), "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for headers in ({}, {"Authorization": "Bearer "}, {"X-App-Token": ""}, {"Authorization": "Bearer x"}):
            assert (await c.get("/api/ping", headers=headers)).status_code == 503
    await db.stop()


async def test_state_starts_in_paper_with_no_invented_numbers(client):
    j = (await client.get("/api/state", headers={"Authorization": f"Bearer {TOKEN}"})).json()
    assert j["mode"] == "PAPER" and j["strategies"] == []


async def test_health_reports_db_pragmas(client):
    j = (await client.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"})).json()
    assert j["db"] == {"journal_mode": "wal", "synchronous": 1}


async def test_no_docs_or_openapi_leak(client):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(p)).status_code == 404


async def test_responses_are_no_store(client):
    r = await client.get("/api/ping", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.headers["cache-control"] == "no-store"
