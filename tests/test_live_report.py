import asyncio
import json
import re
import sqlite3
import struct
import time

import httpx
import pytest

import config
import db as dbmod
import links
import live
import report
import settings as st
from bus import Hub
from conftest import _chrome_path
from db import Database
from fake_telegram import TOKEN, FakeTelegram
from seed import DEMO_MINTS, seed_demo
from serverkit import LiveServer
from test_telegram import Clock

OWNER = 111
REAL_ROOT = config.ROOT
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


# =============================== live screenshots =============================================================================
@pytest.fixture
def shots(tmp_path):
    path = tmp_path / "live.db"
    conn = sqlite3.connect(path)
    conn.executescript(dbmod.SCHEMA)
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?,?,0)", (live.OWNER_KEY, str(OWNER)))
    conn.commit()
    fake, clock = FakeTelegram(), Clock()
    state = {"fail_capture": False}

    def capture(out):
        if state["fail_capture"]:
            raise RuntimeError("browser would not start")
        open(out, "wb").write(PNG)
        return str(out)

    def make():
        return live.LiveShots(api_url="http://127.0.0.1:1", token="t", bot_token=TOKEN, db_path=path, clock=clock, capture_fn=capture,
                              status_fn=lambda: "PAPER · prompt $150.00 (0 trades) · feed PumpPortal live", transport=httpx.MockTransport(fake))

    loop = asyncio.new_event_loop()
    e = type("E", (), {})()
    e.path, e.fake, e.clock, e.state, e.make, e.loop = path, fake, clock, state, make, loop
    e.ls = make()
    e.conn = e.ls._conn()
    e.client = fake.client()
    e.tick = lambda ls=None: (ls or e.ls).tick(e.conn, loop, e.client, wait=0.05)
    yield e
    e.conn.close()
    loop.close()


def next_at(e):
    return float(e.conn.execute("SELECT value FROM settings WHERE key=?", (live.NEXT_KEY,)).fetchone()[0])


def test_first_start_schedules_one_interval_ahead_and_sends_nothing(shots):
    assert shots.tick() is False and shots.fake.sent == []
    assert next_at(shots) == shots.clock.t + 300  # 5 minutes by default


def test_a_screenshot_goes_out_when_due_with_the_status_line_and_the_schedule_moves_on(shots):
    shots.tick()
    shots.clock.t += 301
    assert shots.tick() is True
    s = shots.fake.sent[-1]
    assert s["method"] == "sendPhoto" and s["chat_id"] == str(OWNER) and s["caption"].startswith("PAPER · prompt $150.00") and s["_bytes"] > 60
    assert next_at(shots) == shots.clock.t + 300 and shots.ls.sent == 1
    shots.clock.t += 100
    assert shots.tick() is False and len(shots.fake.sent) == 1  # not due again yet


def test_the_interval_is_the_owners_setting(shots):
    shots.conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?,?,0)", (live.INTERVAL_KEY, "2"))
    shots.conn.commit()
    shots.tick()
    assert next_at(shots) == shots.clock.t + 120


def test_the_schedule_survives_a_restart(shots):
    shots.tick()
    due = next_at(shots)
    again = shots.make()  # a new process: same database
    shots.clock.t = due - 10
    assert shots.tick(again) is False and next_at(shots) == due  # it kept the old rhythm, no early shot, no reset
    shots.clock.t = due + 5
    assert shots.tick(again) is True


def test_an_order_triggers_one_screenshot_and_a_burst_is_debounced(shots):
    shots.tick()
    shots.ls.request_now("order")
    assert shots.tick() is True
    shots.ls.request_now("order")
    shots.ls.request_now("order")
    shots.clock.t += 3
    assert shots.tick() is False and len(shots.fake.sent) == 1  # inside the 10 s debounce
    shots.clock.t += 20
    shots.ls.request_now("order")
    assert shots.tick() is True and len(shots.fake.sent) == 2


def test_nothing_is_sent_before_someone_says_start(shots):
    shots.conn.execute("DELETE FROM settings WHERE key=?", (live.OWNER_KEY,))
    shots.conn.commit()
    shots.tick()
    shots.clock.t += 400
    assert shots.tick() is False and shots.fake.sent == []


def test_a_failed_screenshot_still_sends_the_status_and_a_telegram_failure_never_kills_the_thread(shots):
    shots.tick()
    shots.clock.t += 301
    shots.state["fail_capture"] = True
    assert shots.tick() is False
    s = shots.fake.sent[-1]
    assert s["method"] == "sendMessage" and "screenshot failed: RuntimeError" in s["text"] and s["text"].startswith("PAPER ·")
    assert shots.ls.failed == 1 and next_at(shots) > shots.clock.t
    shots.state["fail_capture"] = False
    shots.fake.fail_methods.add("sendPhoto")
    shots.clock.t += 301
    assert shots.tick() is False and shots.ls.failed == 2 and "Too Many Requests" in shots.ls.last_error


def test_the_thread_runs_on_its_own_and_stops_quickly(shots):
    shots.conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?,?,0)", (live.NEXT_KEY, "1"))  # overdue
    shots.conn.commit()
    ls = live.LiveShots(api_url="x", token="t", bot_token=TOKEN, db_path=shots.path, capture_fn=lambda out: (open(out, "wb").write(PNG), str(out))[1],
                        status_fn=lambda: "PAPER · status", transport=httpx.MockTransport(shots.fake))
    ls.start()
    for _ in range(100):
        if ls.sent:
            break
        time.sleep(0.05)
    t = time.time()
    ls.stop()
    assert ls.sent >= 1 and time.time() - t < 10 and not ls._thread.is_alive()
    assert shots.fake.sent[-1]["method"] == "sendPhoto"


CHROME = _chrome_path()


@pytest.mark.skipif(not CHROME, reason="no Chrome on this machine")
async def test_a_real_390px_screenshot_is_taken_as_a_signed_in_viewer(tmp_path):
    db = Database(tmp_path / "s.db")
    await db.start()
    await seed_demo(db)
    hub = Hub()
    hub.heartbeats.beat("scanner")
    srv = await LiveServer(db, hub, "screenshot-token-0123456789").start()
    try:
        out = tmp_path / "shot.png"
        await asyncio.to_thread(live.capture_screenshot, srv.url, "screenshot-token-0123456789", out, CHROME)
        data = out.read_bytes()
        width, height = struct.unpack(">II", data[16:24])
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and width == 780 and height == 1688  # 390 x 844 at 2x
        text = await asyncio.to_thread(live.fetch_status, srv.url, "screenshot-token-0123456789")
        assert text.startswith("PAPER · prompt $") and "orion_fast $" in text
    finally:
        await srv.stop()
        await db.stop()


# =============================== uptime / coverage ============================================================================
@pytest.fixture
async def rdb(tmp_path):
    d = Database(tmp_path / "r.db")
    await d.start()
    yield d
    await d.stop()


async def test_uptime_segments_extend_on_beats_and_a_gap_is_not_counted(rdb):
    t = 1_000_000.0
    for i in range(10):  # beats every 30 s: one segment
        await rdb.record_uptime(t + i * 30)
    await rdb.record_uptime(t + 270 + 3600)  # then the laptop slept an hour
    for i in range(1, 4):
        await rdb.record_uptime(t + 270 + 3600 + i * 30)
    segs = await rdb.fetchall("SELECT start_ts, end_ts FROM uptime_segments ORDER BY id")
    assert segs == [{"start_ts": t, "end_ts": t + 270}, {"start_ts": t + 3870, "end_ts": t + 3960}]
    assert await rdb.coverage_seconds(t, t + 4000) == 270 + 90  # the hour asleep is not coverage
    assert await rdb.coverage_seconds(t + 100, t + 200) == 100 and await rdb.coverage_seconds(t + 1000, t + 2000) == 0  # clipped to the window


# =============================== the night report =============================================================================
async def seeded_night(rdb, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    if not (tmp_path / "web").exists():
        (tmp_path / "web").symlink_to(REAL_ROOT / "web")  # the app still serves the dashboard files
    now = time.time()
    await seed_demo(rdb, now=now)
    await rdb.execute("UPDATE nights SET started_at=?, ended_at=?, start_cash=150, end_cash=158.4 WHERE id=1", (now - 8 * 3600, now - 60))
    t = now - 8 * 3600
    for i in range(0, 8 * 120):  # 6 of the 8 hours covered, with a 2-hour hole in the middle
        ts = t + i * 30
        if not (3 * 3600 <= i * 30 < 5 * 3600):
            await rdb.record_uptime(ts)
    return now


async def test_night_report_red_first_then_green_with_links_and_a_footer_of_plain_fomo(rdb, tmp_path, monkeypatch):
    await seeded_night(rdb, tmp_path, monkeypatch)
    await rdb.execute("UPDATE positions SET symbol=? WHERE mint=?", ("<script>alert(1)</script>", DEMO_MINTS[0]))
    res = await report.build_night_report(rdb, out_dir=tmp_path / "reports")
    page = (tmp_path / "reports" / f"night_{res['date']}.html").read_text()
    assert res["trades"] == 7 and "7 trades" in res["summary"]
    assert page.index("Trades that lost") < page.index("Trades that won") < page.index("Orders and their checks")
    losses = page[page.index("Trades that lost"):page.index("Trades that won")]
    wins = page[page.index("Trades that won"):page.index("Orders and their checks")]
    assert "MOCHI" in losses and "MOCHI" not in wins
    assert "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page  # coin names are data: escaped
    assert links.coin_url(DEMO_MINTS[1]).replace("&", "&amp;") in page
    assert "screen n/a (paper), api n/a (paper), chain n/a (paper)" in page  # PAPER has no real checks, and says so
    hrefs = re.findall(r'(?:href|src)="(https?://[^"]+)"', page)
    assert hrefs and all(h.startswith(links.SIGNUP_URL + "/coin?address=") for h in hrefs)
    assert f"<footer>{links.SIGNUP_URL}</footer>" in page


async def test_coverage_is_the_hours_the_desk_really_ran(rdb, tmp_path, monkeypatch):
    await seeded_night(rdb, tmp_path, monkeypatch)
    res = await report.build_night_report(rdb, out_dir=tmp_path / "reports")
    page = (tmp_path / "reports" / f"night_{res['date']}.html").read_text()
    assert 70 < res["coverage_pct"] < 78  # about 6 of 8 hours, never "100%"
    assert re.search(r"actually ran 6\.0 of 8\.0 hours \(7[0-9]%\)", page) and "not counted" in page


async def test_kills_by_reason_refusals_by_layer_fees_and_usage(rdb, tmp_path, monkeypatch):
    await seeded_night(rdb, tmp_path, monkeypatch)
    hub = Hub()
    hub.objects["rpc"] = type("R", (), {"credits_today": lambda self: 1234, "label": "helius"})()
    res = await report.build_night_report(rdb, hub, out_dir=tmp_path / "reports")
    page = (tmp_path / "reports" / f"night_{res['date']}.html").read_text()
    kills = page[page.index("Scanner kills by reason"):page.index("Analyst refusals by layer")]
    assert "liquidity: " in kills and "top 10: " in kills and "copycat: " in kills
    assert "layer 3 dev wallet: " in page and "layer 8 delegations: " in page
    assert "Fees paid: $" in page and "1234 credits" in page and "LLM spend: $0.00" in page


async def test_up_to_six_frames_are_embedded_and_missing_ones_say_so(rdb, tmp_path, monkeypatch):
    now = await seeded_night(rdb, tmp_path, monkeypatch)
    res = await report.build_night_report(rdb, out_dir=tmp_path / "reports")
    assert "no frames were recorded" in (tmp_path / "reports" / f"night_{res['date']}.html").read_text()
    fdir = config.DATA_DIR / "frames"
    fdir.mkdir(parents=True)
    for i in range(40):
        (fdir / f"{now - 7 * 3600 + i * 600:.3f}.jpg").write_bytes(b"\xff\xd8\xff\xd9" + bytes([i]))
    (fdir / f"{now - 99 * 3600:.3f}.jpg").write_bytes(b"\xff\xd8old")  # outside the night
    res = await report.build_night_report(rdb, out_dir=tmp_path / "reports")
    page = (tmp_path / "reports" / f"night_{res['date']}.html").read_text()
    assert page.count('<img src="data:image/jpeg;base64,') == 6


async def test_the_report_never_contains_a_secret(rdb, tmp_path, monkeypatch):
    await seeded_night(rdb, tmp_path, monkeypatch)
    secrets = {"APP_TOKEN": "app-token-VALUE-123456", "TELEGRAM_BOT_TOKEN": TOKEN, "HELIUS_API_KEY": "helius-VALUE-654321"}
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    hub = Hub()
    hub.objects["rpc"] = sourcesless_rpc()
    res = await report.build_night_report(rdb, hub, out_dir=tmp_path / "reports")
    page = (tmp_path / "reports" / f"night_{res['date']}.html").read_text()
    assert not [v for v in secrets.values() if v in page]


def sourcesless_rpc():
    return type("R", (), {"credits_today": lambda self: 5, "label": "helius"})()


async def test_report_without_any_night_still_renders_honestly(rdb, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    res = await report.build_night_report(rdb, out_dir=tmp_path / "reports")
    page = (tmp_path / "reports" / f"night_{res['date']}.html").read_text()
    assert res["trades"] == 0 and "0 trades" in res["summary"] and "none" in page and "night still open" in page


# ---- the API serves it behind the token ---------------------------------------------------------------------------------------------------------------
async def test_reports_are_served_only_with_the_token(rdb, tmp_path, monkeypatch):
    from api import create_app

    await seeded_night(rdb, tmp_path, monkeypatch)
    app = create_app(rdb, Hub(), "api-report-token-12345")
    H = {"Authorization": "Bearer api-report-token-12345"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/reports")).status_code == 401 and (await c.get("/api/report/2026-01-01")).status_code == 401
        assert (await c.get("/api/reports", headers=H)).json() == {"reports": []}
        made = (await c.post("/api/report/generate", headers=H)).json()
        assert made["trades"] == 7 and re.match(r"\d{4}-\d{2}-\d{2}$", made["date"])
        assert (await c.get("/api/reports", headers=H)).json()["reports"] == [made["date"]]
        r = await c.get(f"/api/report/{made['date']}", headers=H)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "Night report" in r.text
        assert (await c.get("/api/report/2020-01-01", headers=H)).status_code == 404
        assert (await c.get("/api/report/not-a-date", headers=H)).status_code == 400
        assert (await c.get("/api/report/..%2F..%2Fetc%2Fpasswd", headers=H)).status_code in (400, 404)


# =============================== weekly review ================================================================================
async def add_trade(db, i, green, score, fresh, now, strategy="prompt"):
    mint = f"WeeklyMint{i:03d}" + "z" * 20
    opened = now - 3 * 86400 + i * 1000
    await db.execute("INSERT INTO positions(mint, symbol, opened_at, entry_price, size_usd, tokens, stop_price, closed_at, pnl_usd, mode, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (mint, f"W{i}", opened, 1.0, 10.0, 10.0, 0.75, opened + 500, 3.0 if green else -2.5, "PAPER", strategy))
    await db.execute('INSERT INTO scores(mint, ts, score, fresh_pct, "pass") VALUES (?,?,?,?,1)', (mint, opened - 100, score, fresh))
    return mint


async def test_weekly_review_suggests_at_most_three_changes_with_numbers_and_applies_none(rdb):
    now = time.time()
    for i in range(6):
        await add_trade(rdb, i, True, 88.0, 10.0, now)
    for i in range(6, 12):
        await add_trade(rdb, i, False, 62.0, 70.0, now)
    s = st.Settings()
    before = s.as_dict()
    res = await report.weekly_review(rdb, s, now)
    assert res["trades_green"] == 6 and res["trades_red"] == 6 and res["applied"] == []
    assert res["metrics"]["score"] == {"green": 88.0, "red": 62.0, "ran2x": None}
    sug = {x["setting"]: x for x in res["suggestions"]}
    assert 1 <= len(res["suggestions"]) <= 3
    assert sug["pass_score"]["current"] == 70 and sug["pass_score"]["proposed"] == 75.0 and "88" in sug["pass_score"]["why"] and "62" in sug["pass_score"]["why"]
    assert sug["fresh_pct_max"]["current"] == 50 and sug["fresh_pct_max"]["proposed"] == 40.0  # the losers held more fresh wallets
    assert s.as_dict() == before and (await rdb.fetchone("SELECT COUNT(*) n FROM change_log"))["n"] == 0  # never applied


async def test_weekly_review_says_when_there_is_too_little_to_suggest(rdb):
    now = time.time()
    for i in range(3):
        await add_trade(rdb, i, i % 2 == 0, 88.0 if i % 2 == 0 else 60.0, 10.0, now)
    res = await report.weekly_review(rdb, now=now)
    assert res["suggestions"] == [] and "Too few trades" in res["notes"][0]


async def test_weekly_review_finds_skipped_coins_that_later_ran_2x_and_ignores_orion_trades(rdb):
    now = time.time()
    m = "RanTwiceMint" + "y" * 20
    await rdb.execute("INSERT INTO decisions(ts, bot, mint, symbol, decision, reason, strategy) VALUES (?,?,?,?,?,?,?)", (now - 5000, "scanner", m, "RAN", "kill", "liq $14.2K", "prompt"))
    await rdb.executemany("INSERT INTO prices(mint, ts, price) VALUES (?,?,?)", [(m, now - 4900, 1.0), (m, now - 4000, 1.5), (m, now - 3000, 2.2)])
    await add_trade(rdb, 1, True, 80.0, 10.0, now, strategy="orion_fast")  # not a prompt-strategy trade
    res = await report.weekly_review(rdb, now=now)
    assert res["skipped_ran_2x"] == 1 and res["trades_green"] == 0
    page = report.render_weekly(res, "2026-11-18")
    assert "none applied" in page and f"<footer>{links.SIGNUP_URL}</footer>" in page


# =============================== secrets in API responses ====================================================================
async def test_no_secret_value_appears_in_any_api_response(rdb, tmp_path, monkeypatch):
    from api import create_app

    await seeded_night(rdb, tmp_path, monkeypatch)
    secrets = {"APP_TOKEN": "api-sweep-token-VALUE-12345", "TELEGRAM_BOT_TOKEN": TOKEN, "HELIUS_API_KEY": "helius-sweep-VALUE-654321",
               "PUMPPORTAL_API_KEY": "pump-sweep-VALUE-111111", "JUPITER_API_KEY": "jup-sweep-VALUE-222222", "XAI_API_KEY": "xai-sweep-VALUE-333333"}
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    hub = Hub()
    hub.heartbeats.beat("scanner")
    app = create_app(rdb, hub, secrets["APP_TOKEN"])
    H = {"Authorization": f"Bearer {secrets['APP_TOKEN']}"}
    paths = ["/api/ping", "/api/state", "/api/health", "/api/live", "/api/chart", "/api/activity", "/api/orders", "/api/account", "/api/settings", "/api/mode",
             "/api/reports", f"/api/coin/{DEMO_MINTS[0]}", f"/api/audit/{DEMO_MINTS[0]}", f"/api/intel/{DEMO_MINTS[0]}", "/api/bots/scanner/decisions", "/api/screen.jpg"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        made = (await c.post("/api/report/generate", headers=H)).json()
        paths.append(f"/api/report/{made['date']}")
        for p in paths:
            r = await c.get(p, headers=H)
            assert r.status_code in (200, 404, 423), (p, r.status_code)
            body = r.text if "image" not in r.headers.get("content-type", "") else r.content.decode("latin-1")
            leaked = [k for k, v in secrets.items() if v in body]
            assert not leaked, (p, leaked)
