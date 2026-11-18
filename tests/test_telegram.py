import asyncio
import logging
import re
import time

import httpx
import pytest

import config
import links
import telegram as tg
from bus import Hub
from db import Database
from fake_telegram import TOKEN, FakeTelegram
from seed import DEMO_MINTS, seed_demo

OWNER, STRANGER = 111, 222
BORK = DEMO_MINTS[3]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeShots:
    def __init__(self):
        self.reasons = []

    def request_now(self, reason="request"):
        self.reasons.append(reason)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")  # never read this machine's own acceptance.json / live_test.json
    db = Database(tmp_path / "t.db")
    await db.start()
    await seed_demo(db)
    hub, fake, clock = Hub(), FakeTelegram(), Clock()
    for n in tg.AGENT_NAMES:
        hub.heartbeats.beat(n)
    hub.status["feed"] = lambda: {"badge": "PumpPortal live"}
    hub.objects["marks"] = lambda: {BORK: {"price": 0.62, "tokens": 36.0, "realized": 0.0}}
    bot = tg.DeskBot(fake.client(), db, hub, clock, lan_url="http://192.168.1.20:8787")
    await bot.load()
    e = type("Env", (), {})()
    e.db, e.hub, e.fake, e.bot, e.clock = db, hub, fake, bot, clock

    async def say(text, chat=OWNER, chat_type="private"):
        n = len(fake.sent)
        await bot.handle_update(fake.say(chat, text, chat_type))
        return [s.get("text") or s.get("caption") or "" for s in fake.sent[n:]]

    e.say = say
    await say("/start")  # links the owner
    yield e
    await db.stop()


# ---- pure formatting -----------------------------------------------------------------------------------------------------------
def test_status_line_matches_the_spec_example_exactly():
    cards = [{"name": "prompt", "balance": 150.0, "trades": 0, "pnl_pct": 0.0, "win_rate": None},
             {"name": "orion_fast", "balance": 102.52, "trades": 94, "pnl_pct": -31.653, "win_rate": 24.4}]  # -$47.48 of $150
    assert tg.status_line(cards, "PumpPortal live", 6, 6) == (
        "PAPER · prompt $150.00 (0 trades) · orion_fast $102.52 (−31.7%, 94 trades, 24% win) · feed PumpPortal live · 6 bots ok")
    assert "5 of 6 bots ok" in tg.status_line(cards, None, 5, 6) and "feed no data" in tg.status_line(cards, None, 5, 6)
    assert "prompt no data (0 trades)" in tg.status_line([{**cards[0], "balance": None}], "x", 6, 6)


def test_clean_paper_and_chunks():
    assert tg.clean("a\x00b\x1b[31m\nc") == "ab[31m\nc" and tg.paper("hi") == "PAPER · hi" and tg.paper("PAPER · hi") == "PAPER · hi"
    parts = tg.chunks("line\n" * 2000)
    assert len(parts) > 1 and all(len(p) <= tg.CHUNK for p in parts) and "".join(parts).count("line") == 2000
    assert tg.restart_text("scanner", 30) == "Restarted scanner, down <1 min" and tg.restart_text("risk", 300) == "Restarted risk, down 5 min"


# ---- owner lock ----------------------------------------------------------------------------------------------------------------------------
async def test_first_start_becomes_owner_everyone_else_is_ignored(env):
    assert env.bot.owner == OWNER and await env.db.get_setting(tg.OWNER_KEY) == str(OWNER)
    sent = len(env.fake.sent)
    for text in ("/start", "status", "sell BORK", "LIVE", "set pass_score to 1", "pause scanner"):
        assert await env.say(text, chat=STRANGER) == []
    assert len(env.fake.sent) == sent and env.bot.ignored >= 6
    assert "scanner" not in env.hub.paused and await env.db.get_setting("mode") is None
    await env.bot.handle_update(env.fake.tap(STRANGER, "yes"))  # a stranger's button tap is ignored too
    assert len(env.fake.sent) == sent


async def test_the_owner_survives_a_restart_and_groups_are_ignored(env):
    bot2 = tg.DeskBot(env.fake.client(), env.db, env.hub, env.clock)
    await bot2.load()
    assert bot2.owner == OWNER
    n = len(env.fake.sent)
    await bot2.handle_update(env.fake.say(OWNER, "status", chat_type="group"))
    assert len(env.fake.sent) == n


async def test_nobody_is_owner_until_someone_says_start(tmp_path):
    db = Database(tmp_path / "o.db")
    await db.start()
    fake = FakeTelegram()
    bot = tg.DeskBot(fake.client(), db, Hub())
    await bot.load()
    await bot.handle_update(fake.say(5, "status"))
    await bot.handle_update(fake.say(5, "hello"))
    assert bot.owner is None and fake.sent == []
    await db.stop()


async def test_every_message_starts_with_paper_and_nothing_leaks_the_token(env):
    for text in ("help", "status", "paper", "nonsense", "LIVE", "why BORK", "withdraw"):
        for m in await env.say(text):
            assert m.startswith("PAPER · "), (text, m[:40])
    assert all(TOKEN not in str(s) for s in env.fake.sent)


# ---- commands --------------------------------------------------------------------------------------------------------------------------------
async def test_status_reports_both_strategies_and_open_positions_with_links(env):
    (m,) = await env.say("status")
    assert m.startswith("PAPER · prompt $") and "orion_fast $147.65 (−1.6%, 4 trades, 25% win)" in m
    assert "feed PumpPortal live · 6 bots ok" in m
    assert "open [prompt] BORK $18.00 +$4.32 (+24.0%)" in m and links.coin_url(BORK) in m  # 36 tokens x 0.62 - $18
    assert (await env.say("/status@desk_bot"))[0].startswith("PAPER · prompt")  # a slash and a bot suffix work


async def test_why_shows_decisions_audit_and_score(env):
    (m,) = await env.say("why BORK")
    assert links.coin_url(BORK) in m and "audit:" in m and "Mint authority ok" in m and "score 90 (passed)" in m
    (m,) = await env.say("why nonexistentcoin")
    assert "do not know" in m
    (m,) = await env.say("why")
    assert m.endswith("Send: why <name, symbol or mint>")


async def test_an_ambiguous_name_asks_and_shows_each_coin_with_its_link(env):
    await env.db.executemany("INSERT INTO new_tokens(mint, ts, name, symbol) VALUES (?,?,?,?)",
                             [("TwinMintOne" + "a" * 20, 1, "Twin", "TWIN"), ("TwinMintTwo" + "b" * 20, 2, "Twin", "TWIN")])
    (m,) = await env.say("why twin")
    assert "More than one coin matches" in m and m.count("https://") == 2
    assert links.coin_url("TwinMintOne" + "a" * 20) in m and links.coin_url("TwinMintTwo" + "b" * 20) in m


async def test_sell_asks_first_and_only_a_yes_queues_it(env):
    (m,) = await env.say("sell BORK 50")
    assert "Sell 50% of BORK (paper)?" in m and "yes" in m and links.coin_url(BORK) in m
    kb = env.fake.sent[-1].get("reply_markup")
    assert [b["callback_data"] for b in kb["inline_keyboard"][0]] == ["yes", "no"]
    assert env.hub.bus.recent(topic="manual_sell") == []  # nothing yet
    (m,) = await env.say("yes")
    assert "Sell queued: 50% of BORK" in m and env.hub.bus.recent(topic="manual_sell")[-1]["data"] == {"mint": BORK, "pct": 50}
    assert "Nothing is waiting" in (await env.say("yes"))[0]  # a confirmation works once


async def test_sell_defaults_to_100_button_tap_works_no_cancels_and_expiry(env):
    await env.say("sell BORK")
    await env.bot.handle_update(env.fake.tap(OWNER, "yes"))
    assert env.hub.bus.recent(topic="manual_sell")[-1]["data"] == {"mint": BORK, "pct": 100}
    await env.say("sell BORK 25")
    assert "Cancelled" in (await env.say("no"))[0] and len(env.hub.bus.recent(topic="manual_sell")) == 1
    await env.say("sell BORK 25")
    env.clock.t += 91
    assert "Nothing is waiting" in (await env.say("yes"))[0] and len(env.hub.bus.recent(topic="manual_sell")) == 1


async def test_sell_validation_and_only_open_positions(env):
    assert "25, 50 or 100" in (await env.say("sell BORK 33"))[0]
    assert "open position" in (await env.say("sell LUNA"))[0]  # LUNA is closed
    assert "Which coin?" in (await env.say("sell"))[0]


class FakeAudit:
    def __init__(self, passed=True):
        from types import SimpleNamespace as NS

        self.passed, self.calls = passed, []
        self.NS = NS

    async def audit(self, coin):
        self.calls.append(coin)
        layers = [self.NS(n=i + 1, ok=self.passed or i < 2, value="x") for i in range(8)]
        if self.passed:
            return {"passed": True, "reason": "pass", "layers": layers, "score": 84,
                    "payload": {"mint": coin["mint"], "symbol": coin["symbol"], "score": 84, "pool_liq": 50000.0, "price": 1.0}}
        return {"passed": False, "reason": "layer 3 Dev wallet: dev holds 31%", "layers": layers[:3], "score": None}


async def test_buy_audits_first_shows_the_size_and_queues_a_dip_order_after_yes(env):
    from bots.brain import Sizing

    env.hub.objects.update(analyst=FakeAudit(True), book=object(), brain_size=lambda p: Sizing(False, 18.0, 12.0, "none", "risk size"))
    msgs = await env.say("buy BORK")
    assert "Auditing BORK" in msgs[0] and "8/8 green, score 84" in msgs[1] and "$18.00" in msgs[1] and "12.0% of cash" in msgs[1]
    assert env.hub.bus.recent(topic="order_request") == []
    (m,) = await env.say("yes")
    req = env.hub.bus.recent(topic="order_request")[-1]["data"]
    assert req["mint"] == BORK and req["size_usd"] == 18.0 and "dip order" in m
    assert env.hub.objects["analyst"].calls[0]["pool"] == "PoolDemo"  # built from what the Scanner saved


async def test_buy_never_overrides_a_failed_audit_or_buys_an_unscanned_coin(env):
    env.hub.objects.update(analyst=FakeAudit(False), book=object(), brain_size=lambda p: None)
    msgs = await env.say("buy BORK")
    assert "Not bought: layer 3 Dev wallet: dev holds 31%" in msgs[-1] and "never override" in msgs[-1]
    assert "Nothing is waiting" in (await env.say("yes"))[0] and env.hub.bus.recent(topic="order_request") == []
    assert "never scanned" in (await env.say(f"buy {DEMO_MINTS[9]}"))[-1] or "do not know" in (await env.say(f"buy {DEMO_MINTS[9]}"))[-1]


async def test_buy_without_the_bots_running_says_so(env):
    assert "needs the desk's bots" in (await env.say("buy BORK"))[0]


async def test_pause_resume_and_risk_guard(env):
    assert "paused" in (await env.say("pause scanner"))[0] and "scanner" in env.hub.paused
    assert "resumed" in (await env.say("resume scanner"))[0] and "scanner" not in env.hub.paused
    env.hub.objects["risk_pausable"] = lambda: False
    assert "cannot be paused while a position is open" in (await env.say("pause risk"))[0] and "risk" not in env.hub.paused
    assert "Which bot?" in (await env.say("pause nobody"))[0]


async def test_set_validates_and_logs_as_the_owner(env):
    assert "kill_liq_usd: 20000 → 25000 (logged)" in (await env.say("set kill_liq_usd to 25000"))[0]
    row = await env.db.fetchone("SELECT key, old, new, who FROM change_log")
    assert row == {"key": "kill_liq_usd", "old": "20000", "new": "25000", "who": "owner"}
    assert "Not changed" in (await env.say("set kill_liq_usd to -5"))[0] and "Not changed" in (await env.say("set nope to 1"))[0]
    assert "Use: set" in (await env.say("set kill_liq_usd"))[0]
    assert (await env.db.fetchone("SELECT COUNT(*) n FROM change_log"))["n"] == 1


async def test_live_is_locked_paper_confirms_and_lowercase_live_is_a_screenshot(env):
    (m,) = await env.say("LIVE")
    assert "LIVE is locked" in m and m.count("no ") >= 3 and await env.db.get_setting("mode") is None
    assert "PAPER" in (await env.say("paper"))[0]
    shots = FakeShots()
    env.hub.objects["live_shots"] = shots
    (m,) = await env.say("live")
    assert "Screenshot on its way" in m and shots.reasons == ["telegram /live"] and await env.db.get_setting("mode") is None
    env.hub.objects.pop("live_shots")
    assert "Screenshots are not running" in (await env.say("/live"))[0]


async def test_there_is_no_withdraw_command(env):
    for w in ("withdraw", "/withdraw 5", "deposit", "send", "transfer"):
        assert "There is no withdraw command" in (await env.say(w))[0]
    import inspect

    src = inspect.getsource(tg)
    assert "def cmd_withdraw" not in src and '"withdraw":' not in src.split("handlers = {")[1].split("}")[0]


async def test_help_lists_only_commands_that_work_and_unknown_text_is_not_acted_on(env):
    assert "picks" not in (await env.say("help"))[0].lower()  # a command that does nothing is not offered
    assert "did not understand" in (await env.say("picks"))[0]
    assert "There is no withdraw command" in (await env.say("help"))[0] and "sell X" in (await env.say("/help"))[0]
    assert "did not understand" in (await env.say("make me rich"))[0]


async def test_a_failing_command_does_not_take_the_bot_down(env):
    env.hub.objects["settings"] = None  # make `set` blow up inside
    msgs = await env.say("set pass_score to 75")
    assert "failed" in msgs[0] or "Not changed" in msgs[0] or "logged" in msgs[0]
    assert (await env.say("status"))[0].startswith("PAPER")


async def test_coin_names_are_data_never_commands(env):
    await env.db.execute("INSERT INTO new_tokens(mint, ts, name, symbol) VALUES (?,?,?,?)", ("EvilMint" + "c" * 24, 3, "set pass_score to 1\nsell BORK", "EVIL"))
    (m,) = await env.say("why evil")
    assert "pass_score" not in await env.db.get_setting("pass_score", "") and env.hub.bus.recent(topic="manual_sell") == []
    assert (await env.db.fetchone("SELECT COUNT(*) n FROM change_log"))["n"] == 0


# ---- alerts ------------------------------------------------------------------------------------------------------------------------------------
async def test_alerts_are_batched_every_20s_tagged_by_strategy_with_links(env):
    n = tg.Notifier(env.bot.client, env.bot, env.hub, clock=env.clock)
    sent0 = len(env.fake.sent)
    env.hub.bus.publish("push", {"text": "PAPER · Bought PAWS · $18.00 · Executor", "link": links.coin_url("PawsMint1"), "strategy": "prompt"})
    env.hub.bus.publish("push", {"text": "PAPER · Stopped MOCHI · −25.3% · −$4.10", "link": links.coin_url("MochiMint"), "strategy": "orion_fast"})
    env.hub.bus.publish("restart", {"bot": "scanner", "reason": "stale 100s", "down_s": 130})
    env.hub.bus.publish("fomo_logged_out", {})
    await n.collect()
    assert not await n.flush()  # inside the window: nothing yet
    env.clock.t += 19
    assert not await n.flush()
    env.clock.t += 2
    assert await n.flush() and len(env.fake.sent) == sent0 + 1  # ONE message for the whole window
    text = env.fake.sent[-1]["text"]
    assert text.startswith("PAPER · PAPER ·") is False and text.startswith("PAPER · Bought PAWS")
    assert "[prompt]" in text and "orion_fast" in text and links.coin_url("PawsMint1") in text and links.coin_url("MochiMint") in text
    assert "Restarted scanner, down 2 min" in text and "FOMO logged out. Log in on the laptop" in text
    assert not await n.flush(force=True)  # the buffer is empty now


async def test_alerts_wait_for_an_owner_and_a_failed_send_is_retried_not_lost(env):
    n = tg.Notifier(env.bot.client, env.bot, env.hub, clock=env.clock)
    env.hub.bus.publish("push", {"text": "PAPER · one", "strategy": "prompt"})
    await n.collect()
    env.fake.fail_methods.add("sendMessage")
    env.clock.t += 25
    assert not await n.flush()
    assert n.buf == ["PAPER · one [prompt]"] or n.buf[0].startswith("PAPER · one")  # kept
    env.fake.fail_methods.clear()
    assert await n.flush(force=True) and "one" in env.fake.sent[-1]["text"]
    env.bot.owner = None  # nobody linked: nothing is sent and nothing is lost
    env.hub.bus.publish("push", {"text": "PAPER · two", "strategy": "prompt"})
    await n.collect()
    assert not await n.flush(force=True) and len(n.buf) == 1


async def test_health_alerts_are_rate_limited_to_one_per_issue_per_30_minutes(env):
    clock = Clock()
    hub = Hub()
    hub.heartbeats = type(hub.heartbeats)(clock=clock)
    hub.heartbeats.beat("scanner")
    hub.status["feed"] = lambda: {"badge": "feed down"}
    errors = tg.ErrorCounter()
    w = tg.HealthWatcher(hub, errors, cdp_check=lambda: False, disk_free=lambda: 2 * 1024 ** 3, clock=clock)
    first = " | ".join(w.check())  # t=0: Chrome and disk are already bad; the bot is not silent yet and the feed-down timer starts
    assert "desk Chrome" in first and "Disk space is low: 2.0 GB" in first and "silent" not in first and "feed has been down" not in first
    clock.t += 130
    joined = " | ".join(w.check())
    assert "Bot scanner has been silent" in joined and "market feed has been down" in joined
    assert "desk Chrome" not in joined and "Disk" not in joined  # already sent once: not again inside 30 minutes
    for _ in range(12):
        errors.times.append(clock.t + 60)  # twelve errors in the last minute, on the same clock as the watcher
    for _ in range(25):
        hub.lag.add(900.0)
    clock.t += 60
    more = " | ".join(w.check())
    assert "lagging: event-loop p95 900 ms" in more and "burst of errors" in more
    assert "scanner" not in more and "Disk" not in more  # not repeated inside 30 minutes
    clock.t += 31 * 60
    again = " | ".join(w.check())
    assert "silent" in again and "Disk space" in again  # and it repeats after 30


async def test_health_is_quiet_when_all_is_well(env):
    hub = Hub()
    hub.heartbeats.beat("scanner")
    hub.status["feed"] = lambda: {"badge": "PumpPortal live"}
    w = tg.HealthWatcher(hub, tg.ErrorCounter(), cdp_check=lambda: True, disk_free=lambda: 500 * 1024 ** 3)
    assert w.check() == []


# ---- polling -------------------------------------------------------------------------------------------------------------------------------------
async def test_poll_loop_handles_updates_saves_the_offset_and_does_not_replay_after_a_restart(env):
    fake, hub = env.fake, env.hub
    fake.updates.clear()
    fake.say(OWNER, "paper")
    n = len(fake.sent)

    async def sleep(_):
        raise asyncio.CancelledError  # stop after one pass

    with pytest.raises(asyncio.CancelledError):
        await tg.poll_loop(env.bot, hub, timeout=0, sleep=sleep)
    assert len(fake.sent) == n + 1 and "PAPER" in fake.sent[-1]["text"] and hub.heartbeats.age("telegram") is not None
    assert int(await env.db.get_setting(tg.OFFSET_KEY)) == fake.updates[-1]["update_id"] + 1
    bot2 = tg.DeskBot(fake.client(), env.db, hub, env.clock)
    with pytest.raises(asyncio.CancelledError):
        await tg.poll_loop(bot2, hub, timeout=0, sleep=sleep)
    assert len(fake.sent) == n + 1  # the same command is not run twice


async def test_poll_loop_backs_off_1_2_4_seconds_when_telegram_fails(env):
    env.fake.fail_methods.add("getUpdates")
    sleeps = []

    async def sleep(d):
        sleeps.append(d)
        if len(sleeps) >= 7:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await tg.poll_loop(env.bot, env.hub, timeout=0, sleep=sleep)
    assert sleeps == [1, 1, 1, 2, 2, 2, 2][:7] or sleeps[:3] == [1, 1, 1]  # one second ticks, doubling after each failed poll


async def test_one_bad_update_does_not_stop_the_loop(env):
    env.fake.updates.clear()
    env.fake.updates.append({"update_id": 900, "message": {"chat": {"id": OWNER, "type": "private"}}})  # no text
    env.fake.say(OWNER, "paper")
    n = len(env.fake.sent)

    async def sleep(_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await tg.poll_loop(env.bot, env.hub, timeout=0, sleep=sleep)
    assert len(env.fake.sent) == n + 1


# ---- the client never leaks the token ----------------------------------------------------------------------------------------------------------------
async def test_a_network_error_never_puts_the_token_in_the_message(caplog):
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    c = tg.TelegramClient(TOKEN, transport=httpx.MockTransport(boom))
    with pytest.raises(tg.TelegramError) as e:
        await c.send_text(1, "hi")
    assert TOKEN not in str(e.value) and "ConnectError" in str(e.value)

    def bad_json(request):
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(tg.TelegramError) as e2:
        await tg.TelegramClient(TOKEN, transport=httpx.MockTransport(bad_json)).get_updates(0)
    assert TOKEN not in str(e2.value) and "HTTP 502" in str(e2.value)


# ---- the 08:00 report job ----------------------------------------------------------------------------------------------------------------------------
async def test_report_goes_out_once_after_report_time_with_usage_after_the_first_night(env, tmp_path, monkeypatch):
    import datetime as dt

    import report

    monkeypatch.setattr(config, "ROOT", tmp_path)
    ended = dt.datetime(2026, 11, 18, 6, 30).timestamp()  # the night closed at lock time, 06:30
    await env.db.execute("UPDATE nights SET started_at=?, ended_at=?, end_cash=? WHERE id=1", (ended - 8 * 3600, ended, 158.0))
    env.hub.objects["rpc"] = type("R", (), {"credits_today": lambda self: 42, "label": "helius"})()
    day = dt.datetime(2026, 11, 18, 7, 0).timestamp()
    env.clock.t = day
    assert await tg.report_once(env.bot, env.hub, env.clock) is None  # 07:00 < 08:00
    env.clock.t = dt.datetime(2026, 11, 18, 8, 5).timestamp()
    n = len(env.fake.sent)
    assert await tg.report_once(env.bot, env.hub, env.clock) == "2026-11-18"
    docs = [s for s in env.fake.sent[n:] if s["method"] == "sendDocument"]
    assert len(docs) == 1 and docs[0]["caption"].startswith("PAPER · Night report")
    msgs = env.fake.texts()
    assert any("Night report" in m and "http://192.168.1.20:8787/#report=" in m for m in msgs[-3:])
    assert any("What your first night cost" in m and "42 credits" in m for m in msgs[-3:])
    n2 = len(env.fake.sent)
    assert await tg.report_once(env.bot, env.hub, env.clock) is None and len(env.fake.sent) == n2  # once a day
    assert (tmp_path / "reports").exists()
