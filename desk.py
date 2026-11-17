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

    async def browser_link(self) -> None:
        """Keep trying to attach to the desk Chrome. Without it there is no quote, so no paper fill (honest skip)."""
        from bus import beating_sleep
        from browser import Browser

        b = None
        while True:
            self.d.hub.heartbeats.beat("browser_link")
            if b is None or b.browser is None or not b.browser.is_connected():
                try:
                    b = Browser()
                    await b.connect()
                    page = await b.ensure_page()
                    fomo_api.attach_request_recorder(page)
                    self.d.browser = b
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
    return PromptDesk(deps, scanner, analyst, brain, executor, risk, orion)
