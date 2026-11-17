"""Bot 1, Scanner: owns every coin that graduates off the pump.fun curve, counts and cuts (spec 5.1).

It never judges quality. It kills on the first hit, passes a coin once at pool age >= 10 min with no kill,
never passes a coin DexScreener does not know, and never passes on missing data.

'liq $0' from a pair DexScreener has not indexed yet is a re-check on the next poll, not a kill. The threshold is
never lowered to hide it; scans.reason records whether liquidity was unindexed or reported as 0 for the Phase 4
investigation.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from bots.common import Deps, decide, kfmt, norm_name, subscribe_queue

log = logging.getLogger("desk.scanner")


@dataclass
class Entry:
    mint: str
    migrated_at: float
    name: str | None = None
    symbol: str | None = None
    creator: str | None = None
    pool: str | None = None
    curve_min: float | None = None
    creator_balance0: float | None = None
    liq_at_scan: float | None = None
    bonding_curve: str | None = None
    holders_hist: deque = field(default_factory=lambda: deque(maxlen=40))
    passed: bool = False
    polls: int = 0
    liq_nodata_polls: int = 0


@dataclass
class Verdict:
    action: str  # kill | pass | wait | drop | need_data
    reason: str
    value: object = None


def evaluate_scan(*, now: float, entry: Entry, snap: dict | None, runners: dict, s, top10_pct: float | None = None,
                  warnings: dict | None = None, holders_growth: float | None = None, have_pass_data: bool = False) -> Verdict:
    """One decision for one coin. Order of the kill rules follows the spec; any missing datum is a wait, never a pass."""
    age_min = (now - entry.migrated_at) / 60.0
    if age_min >= s["drop_after_min"]:
        return Verdict("drop", f"not passed in {s['drop_after_min']:g} min")
    if snap is None:
        return Verdict("wait", "no data: DexScreener does not know this pair yet")
    liq = snap.get("liquidity_usd")
    if liq is None:
        return Verdict("wait", "liq unindexed (re-check next poll)")
    if liq <= 0:
        return Verdict("wait", "liq reported $0 (re-check next poll)")
    if liq < s["kill_liq_usd"]:
        return Verdict("kill", f"liq {kfmt(liq)}", liq)
    if have_pass_data and top10_pct is not None and top10_pct > s["kill_top10_pct"]:
        return Verdict("kill", f"top 10 hold {top10_pct:.0f}%", top10_pct)
    h1 = snap.get("change_h1")
    if h1 is not None and h1 > s["kill_h1_pct"]:
        return Verdict("kill", f"1h +{h1:.0f}%", h1)
    nm = norm_name(entry.name)
    if nm:
        for rn, (rmint, _, rh24) in runners.items():
            if rmint != entry.mint and (nm == rn or (min(len(nm), len(rn)) >= 3 and (nm in rn or rn in nm))):
                return Verdict("kill", f"copycat of {rn} (ran +{rh24:.0f}% today)", rn)
    if have_pass_data and warnings is not None:
        if warnings.get("disable_selling") or warnings.get("disable_buying"):
            return Verdict("kill", "FOMO: selling off" if warnings.get("disable_selling") else "FOMO: buying off")
        if warnings.get("list"):
            return Verdict("kill", f"FOMO warning: {str(warnings['list'])[:60]}")
    if age_min < s["pass_age_min"]:
        return Verdict("wait", f"pool age {age_min:.0f} min < {s['pass_age_min']:g}")
    if not have_pass_data:
        return Verdict("need_data", "eligible: fetch top 10, holders, FOMO warnings")
    if top10_pct is None:
        return Verdict("wait", "no data: top 10")
    if h1 is None:
        return Verdict("wait", "no data: 1h change")
    if warnings is None:
        return Verdict("wait", "no data: FOMO tokenWarnings")
    if holders_growth is None:
        return Verdict("wait", "no data: holders")
    if holders_growth < s["holders_growth_min_pct"]:
        return Verdict("kill", f"holders leaving {holders_growth:.0f}% in 10m", holders_growth)
    return Verdict("pass", "pass", liq)


def holders_growth_pct(hist, now: float, window_s: float = 600.0) -> float | None:
    """holders now / holders ~10 min ago - 1, in percent. None without a reading from about 10 min ago."""
    pts = [(t, h) for t, h in hist if h]
    if len(pts) < 2:
        return None
    t_now, h_now = pts[-1]
    older = [(t, h) for t, h in pts if t_now - t >= window_s * 0.8]
    if not older:
        return None
    t_old, h_old = min(older, key=lambda p: abs((t_now - p[0]) - window_s))
    return 100.0 * (h_now / h_old - 1.0)


class Scanner:
    def __init__(self, deps: Deps):
        self.d = deps
        self.room: dict[str, Entry] = {}
        self.runners: dict[str, tuple[str, float, float]] = {}  # normalized name -> (mint, ts, h24)
        self.passed_tonight: set[str] = set()
        self.dead: set[str] = set()  # killed / dropped tonight: never re-added
        self.q = subscribe_queue(deps.hub, "migration")

    def new_night(self) -> None:
        self.passed_tonight.clear()
        self.dead.clear()

    # ---- intake -----------------------------------------------------------------------------------------------------
    async def add(self, ev: dict) -> Entry | None:
        d, mint = self.d, ev["mint"]
        if mint in self.room or mint in self.dead or mint in self.passed_tonight:
            return None
        migrated_at = ev.get("seen_at") or d.clock()
        e = Entry(mint, migrated_at)
        row = await d.db.fetchone("SELECT ts, name, symbol, creator FROM new_tokens WHERE mint=?", (mint,))
        if row:
            e.name, e.symbol, e.creator = row["name"], row["symbol"], row["creator"]
            e.curve_min = max(0.0, (migrated_at - row["ts"]) / 60.0)  # time on the curve, from our own sighting
        if e.creator and d.rpc is not None:
            try:
                e.creator_balance0 = await d.rpc.token_balance(e.creator, mint)
            except Exception as ex:  # noqa: BLE001
                log.info("creator balance unknown for %s: %s", mint[:6], ex)  # stays None: layer 3 will say 'no data: dev'
        self.room[mint] = e
        d.db.execute_nowait(
            "INSERT OR REPLACE INTO coins(mint, name, symbol, pool, created_at, migrated_at, curve_min, creator, "
            "creator_balance_at_migration, holders) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mint, e.name, e.symbol, e.pool, row["ts"] if row else None, migrated_at, e.curve_min, e.creator, e.creator_balance0, None))
        d.hub.bus.publish("room_add", {"mint": mint, "symbol": e.symbol, "room": len(self.room)})
        return e

    # ---- poll -------------------------------------------------------------------------------------------------------
    async def poll(self) -> dict:
        """One scan cycle over the waiting room. Returns {'pass': n, 'kill': n, 'wait': n, 'drop': n}."""
        d, s, now = self.d, self.d.settings, self.d.clock()
        counts = {"pass": 0, "kill": 0, "wait": 0, "drop": 0}
        if not self.room:
            return counts
        mints = list(self.room)
        try:
            snaps = await d.dex.snapshots(mints)
        except Exception as e:  # noqa: BLE001
            log.warning("scanner poll: DexScreener failed: %s", e)
            return counts
        holders = await self._holders(mints)
        for mint in mints:
            e = self.room[mint]
            snap = snaps.get(mint)
            e.polls += 1
            if snap:
                if e.pool is None:
                    e.pool = snap.get("pair")
                if e.liq_at_scan is None and snap.get("liquidity_usd"):
                    e.liq_at_scan = snap["liquidity_usd"]
                h24 = snap.get("change_h24")
                if h24 is not None and h24 >= s["runner_h24_pct"] and norm_name(e.name):
                    self.runners[norm_name(e.name)] = (mint, now, h24)
            h = holders.get(mint)
            e.holders_hist.append((now, h))
            self._store_price(mint, snap, h, now)
            v = evaluate_scan(now=now, entry=e, snap=snap, runners=self._runners_today(now), s=s)
            top10 = warnings = growth = None
            if v.action == "need_data":
                top10, warnings = await self._pass_data(e)
                growth = holders_growth_pct(e.holders_hist, now)
                v = evaluate_scan(now=now, entry=e, snap=snap, runners=self._runners_today(now), s=s, top10_pct=top10,
                                  warnings=warnings, holders_growth=growth, have_pass_data=True)
            await self._record(e, snap, v, h, growth)
            counts[v.action] = counts.get(v.action, 0) + 1
            if v.action == "pass":
                e.passed = True
                self.passed_tonight.add(mint)
                del self.room[mint]
                d.hub.bus.publish("scan_pass", self._payload(e, snap))
            elif v.action in ("kill", "drop"):
                self.dead.add(mint)
                del self.room[mint]
        return counts

    def _runners_today(self, now: float) -> dict:
        return {k: v for k, v in self.runners.items() if now - v[1] < 86400}

    async def _holders(self, mints: list[str]) -> dict[str, int | None]:
        """One FOMO filterTokens call per 20 mints. Skipped when the queue is busy; failures leave holders unknown."""
        out: dict[str, int | None] = {}
        d = self.d
        if d.fomo is None:
            return out
        from fields import get

        for i in range(0, len(mints), 20):
            batch = mints[i:i + 20]
            try:
                resp = await d.fomo.filter_tokens(batch)
            except Exception as e:  # noqa: BLE001 - logged out, busy queue, network: holders stay unknown
                log.info("holders read skipped: %s", e)
                continue
            rows = resp if isinstance(resp, list) else None
            if rows is None:
                continue
            for mint, row in zip(batch, rows):  # positional: FOMO returns one row per requested token
                v = get("token.holders", row)
                out[mint] = int(v) if isinstance(v, (int, float)) else None
        return out

    async def _pass_data(self, e: Entry):
        d = self.d
        top10 = warnings = None
        if d.rpc is not None:
            try:
                top10 = await d.rpc.top10_pct(e.mint, {x for x in (e.pool, e.bonding_curve) if x})
            except Exception as ex:  # noqa: BLE001
                log.info("top 10 unavailable for %s: %s", e.mint[:6], ex)
        if d.fomo is not None and d.body_for is not None:
            try:
                from fields import get

                raw = await d.fomo.token_warnings(d.body_for("tokenWarnings", e.mint))
                warnings = {"disable_buying": get("warnings.disable_buying", raw), "disable_selling": get("warnings.disable_selling", raw),
                            "list": get("warnings.list", raw)}
                if all(v is None for v in warnings.values()):
                    warnings = None  # keys not mapped / response unreadable: no data, never 'no warnings'
            except Exception as ex:  # noqa: BLE001
                log.info("tokenWarnings unavailable for %s: %s", e.mint[:6], ex)
        return top10, warnings

    def _store_price(self, mint, snap, holders, now) -> None:
        if snap and snap.get("price_usd") is not None:
            self.d.db.execute_nowait("INSERT INTO prices(mint, ts, price, volume_m5, holders) VALUES (?,?,?,?,?)",
                                     (mint, now, snap["price_usd"], snap.get("volume_m5"), holders))

    async def _record(self, e: Entry, snap: dict | None, v: Verdict, holders, growth) -> None:
        d = self.d
        snap = snap or {}
        liq, mcap, vm5, vh1 = snap.get("liquidity_usd"), snap.get("mcap"), snap.get("volume_m5"), snap.get("volume_h1")
        liq_mcap = liq / mcap if liq is not None and mcap else None  # logged only, never kills or scores
        accel = vm5 * 12 / vh1 if vm5 is not None and vh1 else None  # logged only
        d.db.execute_nowait(
            "INSERT INTO scans(mint, ts, price, mcap, liquidity_usd, volume_m5, volume_h1, h1_change_pct, h24_change_pct, age_min, "
            "liq_mcap, accel, holders, holders_growth, verdict, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e.mint, d.clock(), snap.get("price_usd"), mcap, liq, vm5, vh1, snap.get("change_h1"), snap.get("change_h24"),
             (d.clock() - e.migrated_at) / 60.0, liq_mcap, accel, holders, growth, v.action,
             v.reason + ("" if snap.get("liquidity_known", True) else " [liquidity key absent]")))
        if v.action in ("kill", "pass", "drop"):
            await decide(d, "scanner", e.mint, e.symbol, v.action, v.reason, value=v.value)
        elif v.action == "wait" and "re-check" in v.reason:
            e.liq_nodata_polls += 1
            if e.liq_nodata_polls in (1, 5, 20):  # a few log lines, not one per poll
                await decide(d, "scanner", e.mint, e.symbol, "wait", v.reason)

    def _payload(self, e: Entry, snap: dict | None) -> dict:
        snap = snap or {}
        return {"mint": e.mint, "symbol": e.symbol, "name": e.name, "pool": e.pool, "creator": e.creator,
                "curve_min": e.curve_min, "creator_balance_at_migration": e.creator_balance0, "migrated_at": e.migrated_at,
                "liq_at_scan": snap.get("liquidity_usd"), "price_at_scan": snap.get("price_usd"),
                "bonding_curve": e.bonding_curve}

    async def run(self) -> None:
        from bus import beating_sleep

        last_poll = 0.0
        while True:
            try:
                while True:
                    ev = self.q.get_nowait()
                    await self.add(ev)
            except asyncio.QueueEmpty:
                pass
            self.d.hub.heartbeats.beat("scanner")
            if time.monotonic() - last_poll >= self.d.settings["scan_poll_s"]:
                last_poll = time.monotonic()
                try:
                    await self.poll()
                except Exception as e:  # noqa: BLE001
                    log.exception("scanner poll failed: %s", e)
            await beating_sleep(self.d.hub, "scanner", 1.0, step=1.0)
