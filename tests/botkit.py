"""Fakes and helpers for the bot tests: no network, no browser, controllable clock."""
from types import SimpleNamespace

import fields
from bots.common import Deps
from book import PaperBook
from bus import Hub
from db import Database
from session import Session
from settings import Settings

T0 = 1_700_000_000.0
MINT = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump"
POOL = "PoolPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPP"


class Clock:
    def __init__(self, t=T0):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    async def sleep(self, d):
        self.slept.append(d)
        self.t += d


def snap(mint=MINT, price=1.0, liq=40000.0, h1=10.0, h24=0.0, buys5=12, sells5=5, vol1=30000.0, buys1=100, sells1=50,
         vol5=2500.0, mcap=200000.0, pair=POOL, known=True):
    return {"mint": mint, "pair": pair, "dex": "pumpswap", "ts": T0, "price_usd": price, "liquidity_usd": liq,
            "liquidity_known": known, "mcap": mcap, "volume_m5": vol5, "volume_h1": vol1, "buys_m5": buys5, "sells_m5": sells5,
            "buys_h1": buys1, "sells_h1": sells1, "change_m5": 0.0, "change_h1": h1, "change_h24": h24, "pair_created_at": T0 - 3600}


class FakeDex:
    def __init__(self):
        self.snaps, self.calls, self.fail = {}, [], False

    async def snapshots(self, mints):
        self.calls.append(list(mints))
        if self.fail:
            raise RuntimeError("dexscreener down")
        return {m: self.snaps[m] for m in mints if m in self.snaps}


class FakeRpc:
    def __init__(self):
        self.mint = {"mint_authority": None, "freeze_authority": None, "supply": 1e9, "decimals": 6}
        self.balances = {}
        self.top10 = 15.0
        self.deleg = {"accounts": 100, "by_delegate": {}, "top_holders": [(f"owner{i}", 1000.0 - i) for i in range(20)]}
        self.sigs = 50
        self.fail = set()  # method names that raise
        self.fail_times = {}  # method -> how many first calls fail
        self.calls = []
        self.block = set()  # methods that never return (to prove cancellation)

    async def _go(self, name, value, *args):
        self.calls.append(name)
        if name in self.block:
            import asyncio

            await asyncio.Event().wait()
        n = self.fail_times.get(name, 0)
        if n:
            self.fail_times[name] = n - 1
            raise RuntimeError(f"{name} flaky")
        if name in self.fail:
            raise RuntimeError(f"{name} refused")
        return value

    async def mint_info(self, mint):
        return await self._go("mint_info", self.mint)

    async def token_balance(self, owner, mint):
        return await self._go("token_balance", self.balances.get(owner))

    async def top10_pct(self, mint, exclude):
        return await self._go("top10_pct", self.top10)

    async def delegations(self, mint, decimals):
        return await self._go("delegations", self.deleg)

    async def signature_count(self, owner, limit=20):
        return await self._go("signature_count", self.sigs)


class FakeRug:
    def __init__(self, pool=POOL):
        self.rep = {"markets": [{"pubkey": pool, "lp": {"lpLockedPct": 100}}], "insiderNetworks": [{"tokenAmount": 4e7}],
                    "token": {"supply": 1e9}}
        self.graph = {"nodes": [{"id": "a"}, {"id": "b"}], "links": [{"source": "a", "target": "b"}]}
        self.fail = set()

    async def report(self, mint):
        if "report" in self.fail:
            raise RuntimeError("rugcheck down")
        return self.rep

    async def insiders_graph(self, mint):
        if "graph" in self.fail:
            raise RuntimeError("rugcheck down")
        return self.graph


class FakeJup:
    def __init__(self, rt=3.0, impact=2.0):
        self.rt, self.impact, self.fail, self.calls = rt, impact, False, []

    async def round_trip(self, mint, usd, sol_usd):
        self.calls.append((mint, usd, sol_usd))
        if self.fail:
            raise RuntimeError("no route")
        return {"rt_loss_pct": self.rt, "sell_impact_pct": self.impact}


class FakeCoinbase:
    async def spot(self):
        return 150.0


class FakeFomo:
    def __init__(self):
        self.warn = {"disableBuying": False, "disableSelling": False, "warnings": []}
        self.holders = {}
        self.fail = False

    async def token_warnings(self, body, bot="risk"):
        if self.fail:
            raise RuntimeError("logged out")
        return self.warn

    async def token_details(self, body, bot="analyst"):
        raise RuntimeError("details not captured")

    async def filter_tokens(self, mints, bot="scanner", droppable=True):
        if self.fail:
            raise RuntimeError("logged out")
        return [{"holders": self.holders.get(m)} for m in mints]


class FakeBrowser:
    """Stands in for browser.Browser. It has NO press_primary: a paper desk must never ask for it."""

    def __init__(self, price=1.0):
        self.price, self.buys, self.sells, self.error, self.reason = price, [], [], None, None

    async def buy_to_quote(self, mint, usd, bot="executor", order_id=None):
        self.buys.append((mint, usd, bot))
        if self.error:
            raise self.error
        if self.reason:
            return SimpleNamespace(ok=False, reason=self.reason)
        tokens = (usd - 0.36) / self.price
        return SimpleNamespace(ok=True, reason=None, quote_text=f"{tokens:.4f} PAWS (-0.61%)", fee_text="$0.36 fee",
                               tokens_text=f"{tokens:.4f} PAWS", fee_usd=0.36)

    async def sell_to_quote(self, mint, pct=100, bot="executor", order_id=None, urgent=False):
        self.sells.append((mint, pct, bot, urgent))
        return SimpleNamespace(ok=True, reason=None, quote_text="q", fee_text="$0.1 fee", tokens_text="1 X", fee_usd=0.1)


MAPPED = {
    "token.holders": {"source": "filterTokens", "path": "holders", "confirmed": True},
    "warnings.disable_buying": {"source": "tokenWarnings", "path": "disableBuying", "confirmed": True},
    "warnings.disable_selling": {"source": "tokenWarnings", "path": "disableSelling", "confirmed": True},
    "warnings.list": {"source": "tokenWarnings", "path": "warnings", "confirmed": True},
}


def patch_fields(monkeypatch):
    """The shipped fields_map.json has unmapped keys on purpose (no real sample yet). Tests map them."""
    monkeypatch.setattr(fields, "load_map", lambda path=None: MAPPED)


async def make_deps(tmp_path, overrides=None, **kw):
    db = Database(tmp_path / "bots.db")
    await db.start()
    clock = Clock()
    settings = Settings(overrides)
    book = PaperBook(db, Hub(), settings, clock)
    hub = Hub()
    book.hub = hub
    session = Session(db, settings, book, clock)
    await session.open()
    deps = Deps(db=db, hub=hub, settings=settings, book=book, session=session, dex=FakeDex(), fomo=FakeFomo(), rpc=FakeRpc(),
                rugcheck=FakeRug(), jupiter=FakeJup(), coinbase=FakeCoinbase(), browser=FakeBrowser(),
                body_for=lambda endpoint, mint: {"mint": mint}, clock=clock, sleep=clock.sleep)
    for k, v in kw.items():
        setattr(deps, k, v)
    return deps
