import asyncio
import json
import time

import httpx
import pytest

import api_dash
import config
import links
from api import create_app
from bus import Hub
from botkit import make_deps
from db import Database
from seed import DEMO_MINTS, seed_demo
from serverkit import LiveServer

TOKEN = "dash-token-1234567890"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def env(tmp_path):
    db = Database(tmp_path / "d.db")
    await db.start()
    hub = Hub()
    await seed_demo(db)
    app = create_app(db, hub, TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H) as c:
        yield c, db, hub
    await db.stop()


def anon(c):
    """A client that really sends no token (httpx merges default headers, so headers={} would not)."""
    return httpx.AsyncClient(transport=c._transport, base_url="http://t")


async def test_every_dashboard_endpoint_needs_the_token(env):
    c, _, _ = env
    c = anon(c)
    for path in ("/api/live", "/api/chart", "/api/activity", "/api/orders", f"/api/coin/{DEMO_MINTS[3]}", f"/api/audit/{DEMO_MINTS[3]}",
                 f"/api/intel/{DEMO_MINTS[3]}", "/api/bots/scanner/decisions", "/api/screen.jpg", "/api/account", "/api/settings", "/api/mode", "/api/stream"):
        r = await c.get(path)
        assert r.status_code == 401, path
    for path in ("/api/bots/scanner/pause", f"/api/sell/{DEMO_MINTS[3]}", "/api/settings", "/api/mode"):
        assert (await c.post(path, json={})).status_code == 401, path


async def test_live_payload_is_built_from_the_database(env):
    c, _, hub = env
    hub.heartbeats.beat("scanner")
    j = (await c.get("/api/live")).json()
    prompt, orion = j["strategies"]
    assert prompt["name"] == "prompt" and prompt["trades"] == 3 and prompt["wins"] == 2 and prompt["win_rate"] == pytest.approx(200 / 3)
    assert orion["trades"] == 4 and orion["win_rate"] == 25.0 and orion["open"][0]["symbol"] == "PIXEL"
    assert prompt["open"][0]["symbol"] == "BORK" and prompt["open"][0]["pnl_mark_usd"] is None  # no live mark: null, not a guess
    assert [a["name"] for a in j["agents"]] == api_dash.AGENTS
    sc = next(a for a in j["agents"] if a["name"] == "scanner")
    assert sc["running"] is True and len(sc["rate"]) == 10 and sum(sc["rate"]) > 0 and sc["last"]["bot"] == "scanner"
    assert 0 <= sc["pass_rate"] <= 100
    assert next(a for a in j["agents"] if a["name"] == "risk")["running"] is False  # no heartbeat: not 'running'
    assert j["radar"] and all(r["decision"] in ("pass", "kill", "reject", "drop") for r in j["radar"])
    kills = {(k["bot"], k["bucket"]): k["count"] for k in j["kills"]}
    assert kills[("scanner", "liquidity")] > 0 and kills[("scanner", "top 10")] > 0 and ("analyst", "layer 3 dev wallet") in kills
    assert j["night"]["trades"] == 3 and j["night"]["passes"] > 0 and j["mode"] == "PAPER"


async def test_open_positions_get_live_marks_from_the_desk(env):
    c, _, hub = env
    hub.objects["marks"] = lambda: {DEMO_MINTS[3]: {"price": 0.6, "tokens": 36.0, "realized": 0.0}}
    j = (await c.get("/api/live")).json()
    pos = j["strategies"][0]["open"][0]
    assert pos["price"] == 0.6 and pos["pnl_mark_usd"] == pytest.approx(36 * 0.6 - 18.0) and pos["pnl_mark_pct"] == pytest.approx(100 * 3.6 / 18)


def test_bucket_names():
    assert api_dash.bucket("liq $14.2K") == "liquidity" and api_dash.bucket("layer 8 Delegations: no data: delegations") == "layer 8 delegations"
    assert api_dash.bucket("top 10 hold 38%") == "top 10" and api_dash.bucket("something odd happened here") == "something odd happened"
    assert api_dash.bucket(None) == "other"


async def test_chart_ranges_series_and_markers(env):
    c, _, _ = env
    j = (await c.get("/api/chart?range=1h")).json()
    assert [s["name"] for s in j["series"]] == ["prompt", "orion_fast"] and all(s["points"] for s in j["series"])
    assert all(p[0] >= j["now"] - 3600 for s in j["series"] for p in s["points"])
    big = (await c.get("/api/chart?range=all")).json()
    assert len(big["series"][0]["points"]) > len(j["series"][0]["points"]) and len(big["series"][0]["points"]) <= 401
    assert {m["strategy"] for m in big["markers"]} == {"prompt", "orion_fast"}
    assert (await c.get("/api/chart?range=nope")).status_code == 400


async def test_activity_orders_and_bot_decisions(env):
    c, _, _ = env
    a = (await c.get("/api/activity?limit=5")).json()
    assert len(a["closed"]) == 5 and a["closed"][0]["closed_at"] >= a["closed"][-1]["closed_at"] and a["activity"][0]["kind"] == "buy"
    assert (await c.get("/api/orders")).json()["orders"][0]["quote_text"] == "36 BORK (-0.6%)"
    d = (await c.get("/api/bots/analyst/decisions?limit=7")).json()
    assert len(d["decisions"]) == 7 and all(x["bot"] == "analyst" for x in d["decisions"])
    assert (await c.get("/api/bots/hacker/decisions")).status_code == 404


async def test_coin_view_has_candles_and_a_links_py_url(env):
    c, _, _ = env
    j = (await c.get(f"/api/coin/{DEMO_MINTS[3]}")).json()
    import links

    assert j["fomo_url"] == links.coin_url(DEMO_MINTS[3]) and j["coin"]["symbol"] == "BORK" and len(j["candles"]) > 10
    assert all(len(x) == 5 and x[2] >= x[3] for x in j["candles"])  # t, o, h, l, c : high >= low
    assert j["position"]["strategy"] == "prompt" and j["position"]["symbol"] == "BORK"
    assert (await c.get("/api/coin/UnknownMintXXXXXXXXXX")).status_code == 404
    assert (await c.get("/api/coin/bad%20mint!")).status_code in (400, 404)


async def test_audit_and_intel(env):
    c, _, _ = env
    j = (await c.get(f"/api/audit/{DEMO_MINTS[3]}")).json()
    assert [l["layer"] for l in j["layers"]] == list(range(1, 9)) and j["layers"][2]["value_text"] == "0.9% · sold 0" and j["score"]["score"] == 90
    assert (await c.get("/api/audit/NoAuditHereXXXXXX")).json()["layers"] == []
    assert (await c.get(f"/api/intel/{DEMO_MINTS[8]}")).json()["council"] == "council go (+2)"


async def test_pause_resume_and_risk_cannot_be_paused_with_an_open_position(env):
    c, _, hub = env
    assert (await c.post("/api/bots/scanner/pause")).json() == {"bot": "scanner", "paused": True} and "scanner" in hub.paused
    assert (await c.get("/api/live")).json()["agents"][0]["paused"] is True
    assert (await c.post("/api/bots/scanner/resume")).json()["paused"] is False and "scanner" not in hub.paused
    hub.objects["risk_pausable"] = lambda: False
    r = await c.post("/api/bots/risk/pause")
    assert r.status_code == 409 and "open" in r.json()["error"] and "risk" not in hub.paused
    hub.objects["risk_pausable"] = lambda: True
    assert (await c.post("/api/bots/risk/pause")).status_code == 200
    assert (await c.post("/api/bots/nobody/pause")).status_code == 404


async def test_sell_now_validates_and_queues_through_the_bus(env):
    c, _, hub = env
    m = DEMO_MINTS[3]
    assert (await c.post(f"/api/sell/{m}", json={"pct": 33, "confirm": True})).status_code == 400
    assert (await c.post(f"/api/sell/{m}", json={"pct": 50, "confirm": False})).status_code == 400  # asks to confirm
    assert (await c.post(f"/api/sell/{DEMO_MINTS[9]}", json={"pct": 50, "confirm": True})).status_code == 404  # nothing open
    r = await c.post(f"/api/sell/{m}", json={"pct": 50, "confirm": True})
    assert r.status_code == 200 and r.json()["mode"] == "PAPER"
    assert hub.bus.recent(topic="manual_sell")[-1]["data"] == {"mint": m, "pct": 50}
    assert (await c.post(f"/api/sell/{DEMO_MINTS[8]}", json={"pct": 100, "confirm": True})).status_code == 200  # an orion position too


async def test_screen_frame_states(env, tmp_path, monkeypatch):
    c, _, _ = env
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    r = await c.get("/api/screen.jpg")
    assert r.status_code == 200 and r.json() == {"state": "waiting", "message": "Waiting for the desk Chrome"}
    (tmp_path / "data" / "screen.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    r = await c.get("/api/screen.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and "no-store" in r.headers["cache-control"]
    (tmp_path / "data" / "screen.paused").write_text("x")
    r = await c.get("/api/screen.jpg")
    assert r.status_code == 200 and r.json() == {"state": "paused", "message": "Paused · private"}


async def test_settings_are_owner_validated_and_logged(env):
    c, db, hub = env
    j = (await c.get("/api/settings")).json()["settings"]
    byk = {s["key"]: s for s in j}
    assert byk["kill_liq_usd"]["value"] == 20000 and byk["kill_liq_usd"]["lo"] == 0 and byk["lock_time"]["kind"] == "time" and byk["hours_24_7"]["kind"] == "bool"
    r = await c.post("/api/settings", json={"key": "pass_score", "value": 75})
    assert r.json() == {"key": "pass_score", "old": 70, "new": 75}
    assert (await c.post("/api/settings", json={"key": "pass_score", "value": 500})).status_code == 400
    assert (await c.post("/api/settings", json={"key": "nope", "value": 1})).status_code == 400
    row = await db.fetchone("SELECT key, old, new, who FROM change_log")
    assert row == {"key": "pass_score", "old": "70", "new": "75", "who": "owner"}
    assert next(s for s in (await c.get("/api/settings")).json()["settings"] if s["key"] == "pass_score")["value"] == 75


async def test_a_live_desk_settings_object_is_shared_with_the_bots(env):
    c, _, hub = env
    import settings as st

    shared = st.Settings()
    hub.objects["settings"] = shared
    await c.post("/api/settings", json={"key": "dip_pct", "value": 20})
    assert shared["dip_pct"] == 20  # the bots hold this very object: the change applies at once


async def test_mode_never_goes_live(env, tmp_path, monkeypatch):
    c, db, _ = env
    j = (await c.get("/api/mode")).json()
    assert j["mode"] == "PAPER" and len(j["gates"]) == 3 and not all(g["met"] for g in j["gates"])
    r = await c.post("/api/mode", json={"mode": "LIVE", "confirm": "LIVE"})
    assert r.status_code == 409 and "gates not met" in r.json()["error"]
    assert (await c.post("/api/mode", json={"mode": "LIVE"})).status_code == 400  # needs the typed confirmation
    # even with every gate met, this build refuses: LIVE clicking is not implemented
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "d2")
    (tmp_path / "d2").mkdir()
    for f in ("acceptance.json", "live_test.json"):
        (tmp_path / "d2" / f).write_text('{"passed": true}')
    await db.execute("UPDATE nights SET started_at=1000, ended_at=30000 WHERE id=1")  # a night only counts when the desk really ran it:
    await db.execute("INSERT INTO uptime_segments(start_ts, end_ts) VALUES (1000, 30000)")  # coverage,
    await db.execute("INSERT INTO metrics_log(ts, lag_p95_ms, lag_max_ms, restarts) VALUES (2000, 30, 90, 0)")  # loop lag
    await db.execute("INSERT INTO restart_log(ts, kind, reason) VALUES (900, 'desk_start', 'desk process started')")  # and the restart log
    assert all(g["met"] for g in (await c.get("/api/mode")).json()["gates"])
    r = await c.post("/api/mode", json={"mode": "LIVE", "confirm": "LIVE"})
    assert r.status_code == 501
    assert (await c.get("/api/mode")).json()["mode"] == "PAPER" and await db.get_setting("mode") is None  # never written
    assert (await c.post("/api/mode", json={"mode": "PAPER"})).json() == {"mode": "PAPER"}


async def test_account_masks_the_address(env):
    c, _, _ = env
    j = (await c.get("/api/account")).json()
    assert j["handle"] == "fluffcore3" and j["address"] == "So11...1112" and "So11111111111111111111111111111111111111112" not in json.dumps(j)
    assert len(j["nights"]) == 1


async def test_static_dashboard_is_public_but_data_is_not(env, tmp_path):
    c, _, _ = env
    c = anon(c)
    assert (await c.get("/")).status_code in (200, 404)  # the page holds no data
    assert (await c.get("/api/state")).status_code == 401


# ---- the live event stream ---------------------------------------------------------------------------------------------------------------
async def test_event_stream_delivers_trimmed_events_and_drops_internal_topics(tmp_path):
    db = Database(tmp_path / "s.db")
    await db.start()
    hub = Hub()
    srv = await LiveServer(db, hub, TOKEN).start()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            assert (await c.get(srv.url + "/api/stream")).status_code == 401
            async with c.stream("GET", srv.url + "/api/stream", headers=H) as r:
                assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
                lines = r.aiter_lines()
                assert json.loads((await anext(lines))[6:])["topic"] == "hello"
                await asyncio.sleep(0.2)
                hub.bus.publish("trade", {"mint": "M", "traderPublicKey": "wallet", "solAmount": 1})  # internal: never streamed
                hub.bus.publish("decision", {"ts": 1.0, "bot": "scanner", "mint": "M", "symbol": "PAWS", "decision": "kill", "reason": "liq $14.2K",
                                             "value": "14200", "strategy": "prompt", "secret_field": "nope"})
                hub.bus.publish("push", {"text": "PAPER · Bought PAWS", "link": links.coin_url("M"), "strategy": "prompt", "ts": 1})
                for i in range(30):
                    hub.bus.publish("new_token", {"mint": f"T{i}", "symbol": "S", "name": "N", "creator": "creatorwallet", "late": False})
                seen = []
                while len(seen) < 3:
                    ln = await asyncio.wait_for(anext(lines), 5)
                    if ln.startswith("data: ") and json.loads(ln[6:])["topic"] != "tick":  # the once-a-second live tick has its own test
                        seen.append(json.loads(ln[6:]))
                assert [e["topic"] for e in seen[:2]] == ["decision", "push"]  # 'trade' was dropped
                assert seen[0]["data"]["reason"] == "liq $14.2K" and "secret_field" not in seen[0]["data"]
                assert seen[2]["topic"] == "new_token" and "creator" not in seen[2]["data"]
                await asyncio.sleep(0.3)
                more = []
                while True:
                    try:
                        ln = await asyncio.wait_for(anext(lines), 0.4)
                    except asyncio.TimeoutError:
                        break
                    if ln.startswith("data: "):
                        more.append(ln)
                assert len(more) + 1 <= 4  # 30 new tokens in a burst: sampled to 4 a second
    finally:
        await srv.stop()
        await db.stop()


async def test_the_stream_pushes_a_live_tick_every_second_from_memory_and_never_invents_a_value(tmp_path):
    db = Database(tmp_path / "s.db")
    await db.start()
    hub = Hub()
    hub.heartbeats.beat("scanner")
    hub.paused.add("brain")
    srv = await LiveServer(db, hub, TOKEN).start()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            async with c.stream("GET", srv.url + "/api/stream", headers=H) as r:
                lines = r.aiter_lines()

                async def tick():
                    while True:
                        ln = await asyncio.wait_for(anext(lines), 5)
                        if ln.startswith("data: ") and json.loads(ln[6:])["topic"] == "tick":
                            return json.loads(ln[6:])

                t0 = time.time()
                first = await tick()
                assert time.time() - t0 < 0.5  # the first one comes with the connection, not a second later
                assert first["data"]["equity"] is None and first["data"]["marks"] == {} and first["data"]["feed"] is None  # nothing known: nothing claimed
                assert list(first["data"]["ages"]) == ["scanner"] and first["data"]["paused"] == ["brain"]
                hub.status["equity"] = lambda: {"prompt": 151.5, "orion_fast": None}
                hub.objects["marks"] = lambda: {"MintA": {"price": 0.5, "tokens": 10.0, "realized": 1.0}, "MintB": {"price": None, "tokens": 3.0, "realized": 0.0}}
                hub.status["feed"] = lambda: {"badge": "PumpPortal live"}
                t1 = time.time()
                second = await tick()
                assert time.time() - t1 < 0.6  # a moved number is pushed within a quarter of a second, not at the next full second
                assert second["data"]["equity"] == {"prompt": 151.5, "orion_fast": None} and second["data"]["feed"] == "PumpPortal live"
                assert second["data"]["marks"] == {"MintA": [0.5, 10.0, 1.0], "MintB": [None, 3.0, 0.0]}  # a coin with no price stays without one
                third = await tick()
                assert 0.7 < third["ts"] - second["ts"] < 1.5 and third["data"]["equity"] == second["data"]["equity"]  # nothing moved: once a second
    finally:
        await srv.stop()
        await db.stop()


async def test_live_counts_each_agents_decisions_in_the_last_hour(env):
    c, db, _ = env
    agents = {a["name"]: a for a in (await c.get("/api/live")).json()["agents"]}
    n = (await db.fetchone("SELECT COUNT(*) n FROM decisions WHERE bot='scanner' AND ts > ?", (time.time() - 3600,)))["n"]
    assert agents["scanner"]["per_hour"] == n > 0
