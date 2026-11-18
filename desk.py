"""Builds the 'prompt' strategy desk: five bots, the paper book, the night session, and the browser link."""
from __future__ import annotations

import asyncio
import logging
import os
import time

import config
import fomo_api
import sources
from bots.analyst import Analyst
from bots.brain import Brain
from bots.common import Deps
from bots.executor import Executor
from bots.risk import Risk
from bots.scanner import Scanner
from book import PaperBook
from intel.jupiter import Jupiter
from session import Session
from settings import Settings

log = logging.getLogger("desk.desk")


def body_for(endpoint: str, mint: str):
    """Request bodies must be copied from the tab's own network requests (logs/raw/requests/), never guessed.
    Until the owner's real bodies are captured and mapped, FOMO warnings/details are 'no data' and nothing passes."""
    raise fomo_api.NeedsCapturedBody(f"{endpoint}: no captured request body mapped yet")


class PromptDesk:
    def __init__(self, deps: Deps, scanner: Scanner, analyst: Analyst, brain: Brain, executor: Executor, risk: Risk, orion=None):
        self.d, self.scanner, self.analyst, self.brain, self.executor, self.risk = deps, scanner, analyst, brain, executor, risk
        self.orion = orion
        self.extra: list = []  # (name, coroutine function) supervised alongside the bots (Telegram, reports)
        self.stop_hooks: list = []  # called on shutdown (the screenshot thread)

    async def reprove_session(self) -> dict:
        """After a wake: load FOMO fresh and read the header again (S0). Never clicks, never logs in for the owner."""
        b = self.d.browser
        res = {"ts": self.d.clock(), "ok": False, "error": "no browser attached"}
        if b is not None:
            try:
                async with b.lane.hold("wake"):
                    await b.ensure_ready(fresh=True)
                res = {"ts": self.d.clock(), "ok": True, "error": None}
            except Exception as e:  # noqa: BLE001
                res = {"ts": self.d.clock(), "ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
        self.d.hub.status["session_proof"] = lambda r=res: r
        log.info("FOMO session re-proved after wake: %s", "ok" if res["ok"] else res["error"])
        return res

    async def browser_link(self) -> None:
        """Keep trying to attach to the desk Chrome. Without it there is no quote, so no paper fill (honest skip)."""
        from bus import beating_sleep
        from browser import Browser

        b = None
        woke = asyncio.Event()
        self.d.hub.bus.on("wake", lambda ev: woke.set())
        while True:
            self.d.hub.heartbeats.beat("browser_link")
            if woke.is_set():
                woke.clear()
                if b is not None:
                    await self.reprove_session()
            if b is None or b.browser is None or not b.browser.is_connected():
                try:
                    b = Browser()
                    await b.connect()
                    page = await b.ensure_page()
                    fomo_api.attach_request_recorder(page)
                    self.d.browser = b
                    self.d.hub.status["lane"] = lambda b=b: {"holder": b.lane.holder}
                    self.d.fomo = fomo_api.FomoApi(fomo_api.page_fetcher(b.get_page), self.d.hub)
                    log.info("attached to the desk Chrome")
                except Exception as e:  # noqa: BLE001
                    b = None
                    self.d.browser = self.d.fomo = None
                    log.info("desk Chrome not reachable yet: %s", e)
            await beating_sleep(self.d.hub, "browser_link", 30.0)


async def build(hub, db, clients: sources.Clients, dex: sources.DexScreener, clock=time.time, fallbacks=None, feed=None) -> PromptDesk:
    settings = await Settings.load(db)
    book = PaperBook(db, hub, settings, clock)
    await book.load()
    if not await db.fetchone("SELECT id FROM balance WHERE strategy=? LIMIT 1", (book.strategy,)):
        await book.snapshot()  # an opening balance row, so the dashboard shows the bank from the first minute
    session = Session(db, settings, book, clock)
    await session.open()
    session.mark_lock_checked_today()
    deps = Deps(db=db, hub=hub, settings=settings, book=book, session=session, dex=dex,
                rpc=sources.Rpc(clients, helius_key=os.environ.get("HELIUS_API_KEY") or None),
                rugcheck=sources.RugCheck(clients), jupiter=Jupiter(clients), coinbase=sources.Coinbase(clients),
                body_for=body_for, clock=clock)
    scanner, analyst, brain, executor = Scanner(deps), Analyst(deps), Brain(deps), Executor(deps)
    risk = Risk(deps, executor)

    def new_night(_):
        scanner.new_night()
        analyst.new_night()

    hub.bus.on("session_roll", new_night)
    hub.status["prompt"] = lambda: {"cash": round(book.cash, 2), "open": len(book.positions), "waiting": len(book.waiting),
                                   "room": len(scanner.room), "trades_tonight": session.trades,
                                   "browser": deps.browser is not None}
    orion = None
    if os.environ.get("DESK_ORION") != "0":  # the fast strategy has its own bank and never touches FOMO web
        from bots.orion import Orion

        orion = Orion(deps, fallbacks, feed)
    def marks() -> dict:
        out = {m: {"price": book.last_prices.get(m), "tokens": p.tokens, "realized": p.realized_usd} for m, p in book.positions.items()}
        if orion is not None:
            for m, p in orion.engine.positions.items():
                mm = orion.engine.mints.get(m)
                out[m] = {"price": mm.price_usd if mm else None, "tokens": p["tokens"], "realized": p["proceeds"]}
        return out

    def equity() -> dict:
        """What each strategy is worth right now, from memory: the same sum the balance rows store, without waiting for the next row."""
        return {"prompt": book.equity({}), "orion_fast": orion.engine.equity() if orion is not None else None}

    hub.status["equity"] = equity

    def brain_size(payload: dict):
        """What the Brain would size for an audited coin (used by the Telegram 'buy X' preview)."""
        from bots.brain import size_trade

        return size_trade(book.free_cash(), payload["score"], payload.get("pool_liq"), settings, shill=bool(payload.get("shill")),
                          trades_tonight=session.trades, night_loss_pct=session.loss_pct(book.equity({})))

    hub.objects.update(settings=settings, book=book, marks=marks, risk_pausable=risk.pausable, orion=orion, analyst=analyst,
                       brain_size=brain_size, rpc=deps.rpc)
    pd = PromptDesk(deps, scanner, analyst, brain, executor, risk, orion)
    _attach_telegram(pd, hub, db, clock)
    return pd


def _attach_telegram(pd: PromptDesk, hub, db, clock) -> None:
    """Telegram, the 08:00 report and the screenshot thread. Only when TELEGRAM_BOT_TOKEN is in .env."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    import logging as _logging

    import chrome_launcher
    import live
    import osutil
    import telegram as tg

    client = tg.TelegramClient(token)
    ip = osutil.lan_ip()
    lan = f"http://{ip or '127.0.0.1'}:{config.desk_port()}" if config.desk_host() not in ("127.0.0.1", "localhost") else None
    bot = tg.DeskBot(client, db, hub, clock, lan)
    errors = tg.ErrorCounter()
    _logging.getLogger("desk").addHandler(errors)
    no_chrome = os.environ.get("DESK_NO_CHROME") == "1"
    health = tg.HealthWatcher(hub, errors, cdp_check=None if no_chrome else (lambda: chrome_launcher.cdp_alive() is not None))
    notifier = tg.Notifier(client, bot, hub, health)
    shots = live.LiveShots(api_url=f"http://127.0.0.1:{config.desk_port()}", token=os.environ.get("APP_TOKEN", ""), bot_token=token, db_path=config.DB_PATH)
    hub.objects["live_shots"] = shots
    for topic in ("trade_open", "trade_close", "trade_partial"):
        hub.bus.on(topic, lambda ev: shots.request_now("order"))  # a screenshot on every order (debounced inside)
    shots.start()
    pd.extra += [("telegram", lambda: tg.poll_loop(bot, hub)), ("notifier", notifier.run), ("report_job", lambda: tg.report_job(bot, hub, clock))]
    pd.stop_hooks += [lambda: asyncio.to_thread(shots.stop), client.aclose]  # the thread's join must not block the loop
