"""Bot 2, Analyst: 8 layers in order, then a score (spec 5.2). Stops at the first red; the rest show 'not checked'.

NO DATA IS NEVER A PASS. A source error (after the owner's retries) is a red 'no data: ...'.
Layers are fetched concurrently (speed) but DECIDED in order, and the rest are cancelled on the first red.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import fields
import sources
from bots.common import Deps, clamp, decide, push, round_half_up, short, subscribe_queue

log = logging.getLogger("desk.analyst")

LAYER_NAMES = ["Mint authority", "Freeze authority", "Dev wallet", "Top 10", "Liquidity", "Pool age", "Bundles", "Delegations"]


@dataclass
class LayerResult:
    n: int
    name: str
    ok: bool
    value: str
    source: str = ""
    nodata: bool = False


def _nodata(n: int, what: str, source: str = "") -> LayerResult:
    return LayerResult(n, LAYER_NAMES[n - 1], False, f"no data: {what}", source, True)


# ---- the 8 layers, pure ----------------------------------------------------------------------------------------
def layer1_mint(info: dict | None) -> LayerResult:
    if not info:
        return _nodata(1, "mint authority", "rpc")
    revoked = info.get("mint_authority") is None
    return LayerResult(1, LAYER_NAMES[0], revoked, "Revoked" if revoked else "mint not revoked", "rpc")


def layer2_freeze(info: dict | None) -> LayerResult:
    if not info:
        return _nodata(2, "freeze authority", "rpc")
    revoked = info.get("freeze_authority") is None
    return LayerResult(2, LAYER_NAMES[1], revoked, "Revoked" if revoked else "freeze not revoked", "rpc")


def layer3_dev(creator: str | None, balance_now: float | None, balance_at_migration: float | None,
               supply: float | None, rugged_before: int, s) -> LayerResult:
    n, nm = 3, LAYER_NAMES[2]
    if rugged_before > 0:  # a creator found in the rugs table. A fresh dev with no history is NOT red.
        return LayerResult(n, nm, False, f"dev rugged {rugged_before} before", "rugs")
    if not creator or balance_now is None or not supply:
        return _nodata(3, "dev", "rpc")
    pct = 100.0 * balance_now / supply
    if pct >= s["dev_hold_max_pct"]:
        return LayerResult(n, nm, False, f"dev holds {pct:.0f}%", "rpc")
    if balance_at_migration is None:
        return _nodata(3, "dev", "scanner")  # cannot prove 'never sold since migration'
    sold = balance_at_migration - balance_now
    if sold > balance_at_migration * 1e-6:
        return LayerResult(n, nm, False, "dev sold", "rpc")
    return LayerResult(n, nm, True, f"{pct:.1f}% · sold 0", "rpc")


def layer4_top10(pct: float | None, s) -> LayerResult:
    if pct is None:
        return _nodata(4, "top 10", "rpc")
    ok = pct < s["top10_max_pct"]
    return LayerResult(4, LAYER_NAMES[3], ok, f"top 10 hold {pct:.0f}%" if not ok else f"{pct:.0f}%", "rpc")


def layer5_liquidity(report: dict | None, pool: str | None, liq_scanner: float | None, liq_now: float | None, s) -> LayerResult:
    lp = sources.lp_for_pool(report, pool)
    if lp is None or lp["locked_pct"] is None:
        return _nodata(5, "liquidity", "rugcheck")
    if lp["locked_pct"] < s["lp_locked_min_pct"]:
        return LayerResult(5, LAYER_NAMES[4], False, "liquidity unlocked", "rugcheck")
    if not liq_scanner or liq_now is None:
        return _nodata(5, "liquidity", "dexscreener")
    drop = 100.0 * (liq_scanner - liq_now) / liq_scanner
    if drop > s["liq_drop_max_pct"]:
        return LayerResult(5, LAYER_NAMES[4], False, f"liquidity down {drop:.0f}% since scan", "dexscreener")
    return LayerResult(5, LAYER_NAMES[4], True, f"{lp['locked_pct']:.0f}% burned", "rugcheck")


def layer6_age(pool_created_at: float | None, now: float, s) -> LayerResult:
    if pool_created_at is None:
        return _nodata(6, "pool age", "scanner")
    age = (now - pool_created_at) / 60.0
    ok = age >= s["pool_age_min"]
    return LayerResult(6, LAYER_NAMES[5], ok, f"{age:.0f} min" if ok else "pool too young", "scanner")


def layer7_bundles(graph: dict | None, report: dict | None, s) -> LayerResult:
    wallets = sources.biggest_bundle(graph)
    if wallets is None:
        return _nodata(7, "bundles", "rugcheck")
    if wallets >= s["bundle_wallets_max"]:
        return LayerResult(7, LAYER_NAMES[6], False, f"{wallets} bundled wallets", "rugcheck")
    ins = sources.insiders_pct(report)
    if ins is None:
        return _nodata(7, "bundles", "rugcheck")
    if ins >= s["insiders_max_pct"]:
        return LayerResult(7, LAYER_NAMES[6], False, f"insiders hold {ins:.0f}%", "rugcheck")
    return LayerResult(7, LAYER_NAMES[6], True, f"{wallets} bundled · insiders {ins:.0f}%", "rugcheck")


def layer8_delegations(deleg: dict | None, s) -> LayerResult:
    if deleg is None:
        return _nodata(8, "delegations", "rpc")
    by = deleg["by_delegate"]
    if by:
        top, count = max(by.items(), key=lambda kv: kv[1])
        if count > s["delegates_max"]:
            return LayerResult(8, LAYER_NAMES[7], False, f"{count} wallets → {short(top)}", "rpc")
    return LayerResult(8, LAYER_NAMES[7], True, "None", "rpc")


# ---- score ----------------------------------------------------------------------------------------------------
def compute_score(d: dict, s) -> dict:
    """alive + organic + flush + (1 - chase), optional social. Missing positive data counts as 0, missing chase as the
    worst case, so a gap can never raise a score."""
    notes: list[str] = []
    vol = d.get("volume_h1")
    alive = clamp(vol / s["alive_volume_h1"]) if vol is not None else 0.0
    ub, us = d.get("unique_buys_1h"), d.get("unique_sells_1h")
    if ub is not None:
        organic, sells_gt_buys = clamp(ub / s["organic_unique_buys"]), (us is not None and us > ub)
    else:
        b = d.get("buys_h1")
        organic = clamp(b / s["organic_txns_buys"]) if b is not None else 0.0
        sells_gt_buys = d.get("sells_h1") is not None and b is not None and d["sells_h1"] > b
        notes.append("organic: txns")
    penalties = []
    if sells_gt_buys:
        penalties.append("sellers > buyers (1h)")
    if (d.get("bundled") or 0) >= 3:
        penalties.append("3+ bundled wallets")
    if d.get("curve_min") is not None and d["curve_min"] < s["sniped_curve_min"]:
        penalties.append(f"sniped (curve {d['curve_min']:.0f} min)")
    if d.get("fresh_pct") is not None and d["fresh_pct"] >= s["fresh_pct_max"]:
        penalties.append(f"{d['fresh_pct']:.0f}% fresh wallets")
    for _ in penalties:
        organic *= s["organic_penalty"]
    b5, s5 = d.get("unique_buys_5m"), d.get("unique_sells_5m")
    if b5 is None or s5 is None:
        b5, s5 = d.get("buys_m5"), d.get("sells_m5")
        notes.append("flush: txns")
    if b5 is None or s5 is None:
        flush = 0.0
    else:
        flush = 1.0 if s5 <= b5 else 0.5 if s5 <= s["flush_ratio"] * b5 else 0.0
    ch = d.get("change_h1")
    chase = clamp(ch / s["chase_h1_pct"]) if ch is not None else 1.0
    core = alive + organic + flush + (1 - chase)
    social = d.get("social")
    w = s["social_weight"] if social is not None else 0.0
    score = round_half_up(100 * (core + w * (social or 0.0)) / (4 + w))
    if social is None:
        notes.append("social: off")
    return {"alive": alive, "organic": organic, "flush": flush, "chase": chase, "core": core, "social": social,
            "score": score, "social_pts": score - round_half_up(100 * core / 4), "notes": notes, "penalties": penalties}


def passes_gate(layers: list[LayerResult], score: float, s) -> bool:
    """All 8 layers green AND score >= pass_score. 7 of 8 is a no, whatever social says."""
    return len(layers) == 8 and all(l.ok for l in layers) and score >= s["pass_score"]


# ---- the bot --------------------------------------------------------------------------------------------------
class Analyst:
    fresh_spread_s = 10.0

    def __init__(self, deps: Deps):
        self.d = deps
        self.q = subscribe_queue(deps.hub, "scan_pass")
        self.rejected: set[str] = set()

    def new_night(self) -> None:
        self.rejected.clear()

    async def _retry(self, factory):
        n = int(self.d.settings["source_retries"])
        last: Exception | None = None
        for i in range(n):
            try:
                return await factory()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a failing source is 'no data', never a pass
                last = e
                if i < n - 1:
                    await self.d.sleep(2)
        raise last  # type: ignore[misc]

    async def _opt(self, factory):
        """Result or None (after retries): the layer then reports 'no data'."""
        try:
            return await self._retry(factory)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("source failed after retries: %s", e)
            return None

    async def _dev_data(self, coin: dict):
        d = self.d
        creator, mint = coin.get("creator"), coin["mint"]
        bal = await self._opt(lambda: d.rpc.token_balance(creator, mint)) if creator else None
        rows = await d.db.fetchall("SELECT mint FROM rugs WHERE creator=? AND mint<>?", (creator, mint)) if creator else []
        return bal, len(rows)

    async def _deleg(self, t_mint, mint: str):
        info = await t_mint
        if not info:
            return None
        return await self._opt(lambda: self.d.rpc.delegations(mint, info["decimals"]))

    async def audit(self, coin: dict) -> dict:
        d, s, mint, sym = self.d, self.d.settings, coin["mint"], coin.get("symbol")
        exclude = {x for x in (coin.get("pool"), coin.get("bonding_curve")) if x}
        t_mint = asyncio.create_task(self._opt(lambda: d.rpc.mint_info(mint)))
        tasks = {
            "mint": t_mint,
            "dev": asyncio.create_task(self._dev_data(coin)),
            "top": asyncio.create_task(self._opt(lambda: d.rpc.top10_pct(mint, exclude))),
            "rug": asyncio.create_task(self._opt(lambda: d.rugcheck.report(mint))),
            "graph": asyncio.create_task(self._opt(lambda: d.rugcheck.insiders_graph(mint))),
            "deleg": asyncio.create_task(self._deleg(t_mint, mint)),
            "dex": asyncio.create_task(self._opt(lambda: d.dex.snapshots([mint]))),
        }
        layers: list[LayerResult] = []
        t0 = time.perf_counter()
        deleg = None
        try:
            for n in range(1, 9):
                res = await self._run_layer(n, coin, tasks, exclude)
                if n == 8:
                    deleg = await tasks["deleg"]
                layers.append(res)
                await self._emit_layer(mint, res)
                d.hub.metrics.record(f"analyst.layer{n}", (time.perf_counter() - t0) * 1000.0)
                if not res.ok:
                    for rest in range(n + 1, 9):
                        await self._emit_layer(mint, LayerResult(rest, LAYER_NAMES[rest - 1], False, "not checked"), unchecked=True)
                    break
        finally:
            for t in tasks.values():
                t.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        red = next((l for l in layers if not l.ok), None)
        if red:
            reason = f"layer {red.n} {red.name}: {red.value}"
            await decide(d, "analyst", mint, sym, "reject", reason, value=red.value)
            if red.n == 8 and red.nodata:
                push(d, f"Layer 8 has no data for {sym or mint[:6]}: RPC refused getProgramAccounts. Add a Helius key.", mint)
            return {"passed": False, "reason": reason, "layers": layers, "score": None}
        scoring = await self._score(coin, tasks, deleg, layers)
        passed = passes_gate(layers, scoring["score"], s)
        await d.db.execute(
            'INSERT INTO scores(mint, ts, alive, organic, flush, chase, social, social_pts, fresh_pct, score, "pass", reason) '
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mint, d.clock(), scoring["alive"], scoring["organic"], scoring["flush"], scoring["chase"], scoring["social"],
             scoring["social_pts"], scoring.get("fresh_pct"), scoring["score"], int(passed),
             "; ".join(scoring["notes"] + scoring["penalties"])))
        note = "; ".join(scoring["notes"])
        if not passed:
            reason = f"score {scoring['score']} < {s['pass_score']:g}"
            await decide(d, "analyst", mint, sym, "reject", reason, value=scoring["score"], data={"notes": note})
            return {"passed": False, "reason": reason, "layers": layers, "score": scoring["score"], "scoring": scoring}
        await decide(d, "analyst", mint, sym, "pass", f"8/8 green, score {scoring['score']}", value=scoring["score"],
                     data={"notes": note, "social_pts": scoring["social_pts"]})
        snap = scoring["snap"] or {}
        payload = {"mint": mint, "symbol": sym, "name": coin.get("name"), "score": scoring["score"],
                   "social_pts": scoring["social_pts"], "shill": False, "pool_liq": snap.get("liquidity_usd"),
                   "price": snap.get("price_usd"), "scanner_liq": coin.get("liq_at_scan"), "creator": coin.get("creator"),
                   "creator_balance": scoring.get("creator_balance"), "pool": coin.get("pool"),
                   "curve_min": coin.get("curve_min"), "audited_at": d.clock()}
        d.hub.bus.publish("audit_pass", payload)
        return {"passed": True, "reason": "pass", "layers": layers, "score": scoring["score"], "scoring": scoring, "payload": payload}

    async def _run_layer(self, n: int, coin: dict, tasks: dict, exclude: set) -> LayerResult:
        s, now = self.d.settings, self.d.clock()
        if n in (1, 2):
            info = await tasks["mint"]
            return layer1_mint(info) if n == 1 else layer2_freeze(info)
        if n == 3:
            info = await tasks["mint"]
            bal, rugged = await tasks["dev"]
            self._creator_balance = bal
            return layer3_dev(coin.get("creator"), bal, coin.get("creator_balance_at_migration"),
                              info["supply"] if info else None, rugged, s)
        if n == 4:
            return layer4_top10(await tasks["top"], s)
        if n == 5:
            snaps = await tasks["dex"] or {}
            liq_now = (snaps.get(coin["mint"]) or {}).get("liquidity_usd")
            return layer5_liquidity(await tasks["rug"], coin.get("pool"), coin.get("liq_at_scan"), liq_now, s)
        if n == 6:
            return layer6_age(coin.get("migrated_at"), now, s)
        if n == 7:
            return layer7_bundles(await tasks["graph"], await tasks["rug"], s)
        return layer8_delegations(await tasks["deleg"], s)

    async def _emit_layer(self, mint: str, r: LayerResult, unchecked: bool = False) -> None:
        d = self.d
        ok = None if unchecked else int(r.ok)
        d.db.execute_nowait('INSERT INTO audits(mint, ts, layer, name, value_text, "pass", source) VALUES (?,?,?,?,?,?,?)',
                            (mint, d.clock(), r.n, r.name, r.value, ok, r.source))
        d.hub.bus.publish("audit_layer", {"mint": mint, "layer": r.n, "name": r.name, "value": r.value, "pass": ok})

    async def _fresh_pct(self, holders: list[tuple[str, float]], exclude: set) -> float | None:
        owners = [o for o, _ in holders if o not in exclude][:20]
        if not owners:
            return None
        fresh = known = 0
        gap = self.fresh_spread_s / max(1, len(owners))
        for o in owners:
            try:
                n = await self.d.rpc.signature_count(o, 20)
                known += 1
                fresh += n < 10
            except Exception:  # noqa: BLE001
                pass
            await self.d.sleep(gap)
        return 100.0 * fresh / known if known else None

    async def _score(self, coin: dict, tasks: dict, deleg: dict | None, layers: list[LayerResult]) -> dict:
        d, mint = self.d, coin["mint"]
        snaps = await tasks["dex"] or {}
        fresh_snaps = await self._opt(lambda: d.dex.snapshots([mint])) or snaps
        snap = fresh_snaps.get(mint) or snaps.get(mint)
        details = None
        if d.fomo is not None and d.body_for is not None:
            try:
                body = d.body_for("tokenDetails", mint)
                details = await self._retry(lambda: d.fomo.token_details(body))
            except Exception as e:  # noqa: BLE001
                log.info("tokenDetails unavailable: %s", e)
        g = lambda f: fields.get(f, details) if details is not None else None  # noqa: E731
        bundled = sources.biggest_bundle(await tasks["graph"]) or 0
        fresh = await self._fresh_pct(deleg["top_holders"], {coin.get("pool"), coin.get("creator")}) if deleg else None
        sn = snap or {}
        data = {"volume_h1": sn.get("volume_h1"), "unique_buys_1h": g("details.unique_buys_1h"),
                "unique_sells_1h": g("details.unique_sells_1h"), "buys_h1": sn.get("buys_h1"), "sells_h1": sn.get("sells_h1"),
                "unique_buys_5m": g("details.unique_buys_5m"), "unique_sells_5m": g("details.unique_sells_5m"),
                "buys_m5": sn.get("buys_m5"), "sells_m5": sn.get("sells_m5"), "change_h1": sn.get("change_h1"),
                "bundled": bundled, "curve_min": coin.get("curve_min"), "fresh_pct": fresh, "social": None}
        out = compute_score(data, d.settings)
        out.update(snap=snap, fresh_pct=fresh, creator_balance=getattr(self, "_creator_balance", None))
        return out

    async def run(self) -> None:
        while True:
            try:
                coin = await asyncio.wait_for(self.q.get(), timeout=20)
            except asyncio.TimeoutError:
                self.d.hub.heartbeats.beat("analyst")
                continue
            self.d.hub.heartbeats.beat("analyst")
            if coin["mint"] in self.rejected:
                await decide(self.d, "analyst", coin["mint"], coin.get("symbol"), "skip", "already rejected tonight")
                continue
            try:
                res = await self.audit(coin)
                if not res["passed"]:
                    self.rejected.add(coin["mint"])
            except Exception as e:  # noqa: BLE001
                log.exception("audit failed for %s: %s", coin.get("mint"), e)
