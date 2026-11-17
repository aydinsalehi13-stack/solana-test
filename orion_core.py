"""orion_fast: the pure, deterministic engine (spec section 6). The SAME code runs live and in replay.

Everything the engine knows arrives as a tick dict with its own timestamp: there is no wall clock, no randomness
and no I/O in here, so two runs over the same ticks give identical trades. It never touches FOMO's buttons: fills
are simulated on real data.

Tick kinds (all recorded by recorder.py, in the order the engine saw them):
  sol     {ts, usd}                                SOL price
  create  {ts, mint, creator, v_sol, v_tokens, initial_buy}
  trade   {ts, mint, side, sol, tokens, wallet, v_sol, v_tokens, event_ts?}   live trade (PumpPortal keyed feed)
  snap    {ts, mint, price_usd, liquidity_usd, buys_m5, sells_m5, dex}        DexScreener snapshot
  safety  {ts, mint, ok, reasons, rt_loss_pct, sell_impact_pct}               prefetched hard safety card
  dev     {ts, mint, dev_sold}                                                creator check when no trade stream
  clock   {ts}                                                                keeps time stops moving

Fill model (spec 6.1): signal -> latency (1.5 s) -> the first REAL price after it (wait up to 20 s) -> costs:
FOMO fee tiers + pump fee + slippage + impact (+ the Jupiter extra). A gapped stop fills at the first real price
after the trigger, never at the stop line.
"""
from __future__ import annotations

import copy
import json
from collections import deque
from pathlib import Path

import fees
from intel.council import council

MODE = "PAPER"  # hard-coded: orion stays PAPER until a separate written ok from the owner (hard rule 1)
PARAMS_FILE = Path(__file__).resolve().parent / "strategies" / "orion_fast.json"


def load_params(path=None) -> dict:
    return json.loads((Path(path) if path else PARAMS_FILE).read_text(encoding="utf-8"))


def merge(base: dict, over: dict) -> dict:
    """Deep copy of base with nested overrides, e.g. merge(p, {"exits": {"stop_pct": 12}})."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


class _Mint:
    __slots__ = ("created_ts", "creator", "initial_buy", "v_sol", "v_tokens", "price_sol", "price_usd", "price_ts", "venue",
                 "liq_usd", "trades", "nondev_sell_seen", "dev_sold", "snaps", "last_tick_ts", "last_trade_ts", "safety",
                 "bursts", "armed", "cooldown_until", "entered", "pending_buy", "first_seen", "last_snap", "lag_s")

    def __init__(self, ts: float):
        self.created_ts = None
        self.creator = None
        self.initial_buy = None
        self.v_sol = self.v_tokens = self.price_sol = self.price_usd = self.price_ts = None
        self.venue = None
        self.liq_usd = None
        self.trades: deque = deque()  # (ts, side, sol, wallet, price_sol)
        self.nondev_sell_seen = False
        self.dev_sold = None  # None = unknown (no data), never assumed False
        self.snaps: deque = deque()  # (ts, price_usd, buys_m5, sells_m5, liq)
        self.last_tick_ts = ts
        self.last_trade_ts = None
        self.safety = None
        self.bursts: list = []
        self.armed = None
        self.cooldown_until = 0.0
        self.entered = False
        self.pending_buy = False
        self.first_seen = ts
        self.last_snap = None
        self.lag_s = 0.0  # how late we saw the latest event (our ts - the event's own time)


class Engine:
    def __init__(self, params: dict, start_equity: float | None = None):
        self.p = params
        self.start_equity = float(start_equity if start_equity is not None else params.get("start_cash", 150.0))
        self.cash = self.start_equity
        self.mints: dict[str, _Mint] = {}
        self.positions: dict[str, dict] = {}
        self.pending: list[dict] = []
        self.trades: list[dict] = []
        self.sol_usd: float | None = None
        self.now = 0.0
        self.day = None
        self.day_start_equity = self.start_equity
        self.halted = False
        self.pause_reason: str | None = None  # set by the live bot (slow feed): entries paused, with the reason
        self.equity_curve: list[tuple[float, float]] = []
        self._id = 0

    # ---- helpers ----------------------------------------------------------------------------------------------------
    def fee_params(self) -> fees.FeeParams:
        f = self.p["fill"]
        return fees.FeeParams(f["fomo_high_fee_pct"], f["pump_fee_pct"], f["slippage_pct"], f["impact_pct"])

    def equity(self) -> float:
        total = self.cash
        for mint, pos in self.positions.items():
            m = self.mints.get(mint)
            px = m.price_usd if m and m.price_usd else pos["entry_price"]
            total += pos["tokens"] * px
        return total

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def mark_prices(self) -> dict[str, float]:
        return {k: (self.mints[k].price_usd or p["entry_price"]) for k, p in self.positions.items()}

    # ---- main entry point -----------------------------------------------------------------------------------------------
    def on_tick(self, tick: dict) -> list[dict]:
        ts, kind = tick["ts"], tick["kind"]
        events: list[dict] = []
        self.now = max(self.now, ts)
        self._roll_day(ts, events)
        mint = tick.get("mint")
        if kind == "sol":
            self.sol_usd = tick["usd"]
        elif kind == "create":
            self._on_create(tick)
        elif kind == "trade":
            self._on_trade(tick)
        elif kind == "snap":
            self._on_snap(tick)
        elif kind == "safety":
            self._m(mint, ts).safety = tick
        elif kind == "dev":
            self._m(mint, ts).dev_sold = bool(tick["dev_sold"])
        m = self.mints.get(mint) if mint else None
        price_tick = kind in ("trade", "snap") and m is not None and m.price_usd is not None and m.price_ts == ts
        self._process_pending(ts, mint if price_tick else None, events)
        self._check_exits(ts, mint if price_tick else None, events)
        if price_tick:
            self._check_entry(m, mint, ts, kind, events)
            m.last_tick_ts = ts
        self._check_daily_loss(ts, events)
        if kind in ("trade", "snap", "clock") and (not self.equity_curve or ts - self.equity_curve[-1][0] >= 5.0):
            self.equity_curve.append((ts, self.equity()))
        return events

    def _m(self, mint: str, ts: float) -> _Mint:
        m = self.mints.get(mint)
        if m is None:
            m = self.mints[mint] = _Mint(ts)
        return m

    # ---- state updates -----------------------------------------------------------------------------------------------------
    def _on_create(self, t: dict) -> None:
        m = self._m(t["mint"], t["ts"])
        m.created_ts = t["ts"]
        m.creator, m.initial_buy = t.get("creator"), t.get("initial_buy")
        m.v_sol, m.v_tokens = t.get("v_sol"), t.get("v_tokens")
        if m.creator and m.dev_sold is None and t.get("watching_trades", True):
            m.dev_sold = False  # we saw the creation AND we watch its trades, so we would see a creator sale
        # without a trade stream dev_sold stays None (no data) until a 'dev' tick from an on-chain balance check

    def _on_trade(self, t: dict) -> None:
        ts, m = t["ts"], self._m(t["mint"], t["ts"])
        m.v_sol, m.v_tokens = t.get("v_sol", m.v_sol), t.get("v_tokens", m.v_tokens)
        if m.v_sol and m.v_tokens:
            m.price_sol = m.v_sol / m.v_tokens
            m.venue = "curve"
            if self.sol_usd:
                m.price_usd, m.price_ts = m.price_sol * self.sol_usd, ts
                m.liq_usd = max(0.0, m.v_sol - self.p["gates"]["curve_base_vsol"]) * self.sol_usd
        m.trades.append((ts, t["side"], t["sol"], t["wallet"], m.price_sol))
        while m.trades and m.trades[0][0] < ts - 180:
            m.trades.popleft()
        m.last_trade_ts = ts
        m.lag_s = max(0.0, ts - t.get("event_ts", ts))
        if t["side"] == "sell":
            if m.creator and t["wallet"] == m.creator:
                m.dev_sold = True
            else:
                m.nondev_sell_seen = True

    def _on_snap(self, t: dict) -> None:
        ts, m = t["ts"], self._m(t["mint"], t["ts"])
        m.venue = "amm" if t.get("dex") == "pumpswap" else "curve" if m.venue is None else m.venue
        m.price_usd, m.price_ts = t["price_usd"], ts
        m.lag_s = max(0.0, ts - t.get("event_ts", ts))
        if t.get("liquidity_usd") is not None:
            m.liq_usd = t["liquidity_usd"]
        m.snaps.append((ts, t["price_usd"], t.get("buys_m5"), t.get("sells_m5"), t.get("liquidity_usd")))
        while m.snaps and m.snaps[0][0] < ts - 120:
            m.snaps.popleft()
        m.last_snap = t

    def _roll_day(self, ts: float, events: list) -> None:
        day = int(ts // 86400)
        if self.day is None:
            self.day, self.day_start_equity = day, self.equity()
        elif day != self.day:
            self.day, self.day_start_equity, self.halted = day, self.equity(), False
            events.append({"type": "day_start", "ts": ts, "equity": self.day_start_equity})

    def _check_daily_loss(self, ts: float, events: list) -> None:
        if self.halted or self.day_start_equity <= 0:
            return
        loss = 100.0 * (self.day_start_equity - self.equity()) / self.day_start_equity
        if loss >= self.p["daily_loss_pct"]:
            self.halted = True
            events.append({"type": "halt", "ts": ts, "loss_pct": loss, "reason": f"daily loss stop hit ({loss:.1f}%)"})

    # ---- entry signals ------------------------------------------------------------------------------------------------------
    def burst_signal(self, m: _Mint, ts: float) -> dict | None:
        b = self.p["burst"]
        win = [t for t in m.trades if t[0] >= ts - b["window_s"]]
        buys = [t for t in win if t[1] == "buy"]
        if not buys or win[0][4] is None or win[-1][4] is None:
            return None
        sells = [t for t in win if t[1] == "sell"]
        buy_sol, sell_sol = sum(t[2] for t in buys), sum(t[2] for t in sells)
        per: dict[str, float] = {}
        for t in buys:
            per[t[3]] = per.get(t[3], 0.0) + t[2]
        top = 100.0 * max(per.values()) / buy_sol if buy_sol else 100.0
        rise = 100.0 * (win[-1][4] / win[0][4] - 1.0)
        ratio = len(buys) / len(sells) if sells else float("inf")
        sig = {"mode": "burst", "buys": len(buys), "buyers": len(per), "net_sol": buy_sol - sell_sol, "buy_sell_ratio": None if not sells else ratio,
               "top_wallet_pct": top, "rise_pct": rise}
        fails = []
        if len(buys) < b["min_buys"]:
            fails.append("buys")
        if len(per) < b["min_buyers"]:
            fails.append("buyers")
        if sig["net_sol"] < b["min_net_sol"]:
            fails.append("net sol")
        if ratio < b["min_buy_sell_ratio"]:
            fails.append("buy/sell")
        if top > b["max_top_wallet_pct"]:
            fails.append("top wallet")
        if not b["rise_min_pct"] <= rise <= b["rise_max_pct"]:
            fails.append("rise")
        sig["fails"] = fails
        return sig

    def snapshot_signal(self, m: _Mint, ts: float) -> dict | None:
        s = self.p["snapshot"]
        if not m.snaps:
            return None
        now = m.snaps[-1]
        older = [x for x in m.snaps if s["min_span_s"] <= ts - x[0] <= s["max_span_s"]]
        if not older or now[2] is None or now[3] is None:
            return None
        then = older[0]  # the oldest inside the span
        if then[2] is None or not then[1]:
            return None
        d_buys = now[2] - then[2]
        rise = 100.0 * (now[1] / then[1] - 1.0)
        ratio = now[2] / now[3] if now[3] else float("inf")
        sig = {"mode": "snapshot", "delta_buys": d_buys, "span_s": ts - then[0], "rise_pct": rise, "m5_buys": now[2],
               "m5_ratio": None if not now[3] else ratio, "buyers": None, "buy_sell_ratio": None if not now[3] else ratio,
               "top_wallet_pct": None}
        fails = []
        if d_buys < s["min_delta_buys"]:
            fails.append("delta buys")
        if not s["rise_min_pct"] <= rise <= s["rise_max_pct"]:
            fails.append("rise")
        if now[2] < s["min_m5_buys"]:
            fails.append("m5 buys")
        if ratio < s["min_m5_ratio"]:
            fails.append("m5 ratio")
        sig["fails"] = fails
        return sig

    def _check_entry(self, m: _Mint, mint: str, ts: float, kind: str, events: list) -> None:
        if m.entered or m.pending_buy or mint in self.positions:
            return
        live = m.last_trade_ts is not None and ts - m.last_trade_ts <= self.p["snapshot"]["live_trade_grace_s"]
        sig = self.burst_signal(m, ts) if (kind == "trade" and live) else self.snapshot_signal(m, ts) if (kind == "snap" and not live) else None
        if sig is None or sig["fails"]:
            return
        ex = self.p["experiments"]
        if sig["mode"] == "burst" and (ex.get("consecutive_bursts") or 1) > 1:  # two bursts a few seconds apart, not one spike
            if not m.bursts or ts - m.bursts[-1] >= ex["burst_gap_s"]:
                m.bursts.append(ts)
            m.bursts = [b for b in m.bursts if ts - b <= 120]
            if len(m.bursts) < ex["consecutive_bursts"]:
                return
        if ex.get("pullback_entry_pct"):  # do not chase: wait for a pullback after the burst
            a = m.armed
            if a is None or ts - a["ts"] > ex["pullback_timeout_s"]:
                m.armed = {"ts": ts, "peak": m.price_usd}
                return
            a["peak"] = max(a["peak"], m.price_usd)
            if m.price_usd > a["peak"] * (1 - ex["pullback_entry_pct"] / 100.0):
                return
        why = self._blocked(m, mint, ts)
        if why:
            events.append({"type": "skip", "ts": ts, "mint": mint, "reason": why, "mode": sig["mode"]})
            return
        size = self._size()
        if size > self.cash:
            events.append({"type": "skip", "ts": ts, "mint": mint, "reason": "cash", "mode": sig["mode"]})
            return
        lag = m.lag_s
        m.pending_buy = True
        f = self.p["fill"]
        order = {"id": self._next_id(), "kind": "buy", "mint": mint, "trigger_ts": ts, "due": ts + f["latency_s"],
                 "deadline": ts + f["latency_s"] + f["wait_s"], "signal_price": m.price_usd, "size": size, "signal": sig,
                 "safety": m.safety, "lag": lag, "venue": m.venue, "liq": m.liq_usd}
        self.pending.append(order)
        events.append({"type": "entry_signal", "ts": ts, "mint": mint, "signal": sig, "size": size, "order": order["id"]})

    def _size(self) -> float:
        eq = self.equity()
        size = eq * self.p["size_pct"] / 100.0
        rs = self.p["experiments"].get("risk_size_pct")
        if rs:  # size cut so the $ risk per trade stays <= rs% of equity at the stop
            size = min(size, eq * rs / 100.0 / (self.p["exits"]["stop_pct"] / 100.0))
        return max(size, self.p["min_ticket"])

    def _blocked(self, m: _Mint, mint: str, ts: float) -> str | None:
        g = self.p["gates"]
        if self.pause_reason:
            return f"entries paused: {self.pause_reason}"
        if self.halted:
            return "daily loss stop"
        if self.sol_usd is None:
            return "no data: SOL price"
        if len(self.positions) + sum(1 for o in self.pending if o["kind"] == "buy") >= self.p["max_open"]:
            return "max open"
        if ts < m.cooldown_until:
            return "cooldown"
        age = ts - (m.created_ts if m.created_ts is not None else m.first_seen)
        if m.created_ts is None:
            return "no data: creation unseen"
        if age < self.p["min_age_s"]:
            return f"age {age:.0f}s < {self.p['min_age_s']}s"
        if age > self.p["max_age_s"]:
            return f"age {age:.0f}s > {self.p['max_age_s']}s"
        if m.liq_usd is None or m.liq_usd < g["min_liquidity_usd"]:
            return "liquidity"
        if m.venue == "curve" and m.v_sol is not None and m.v_sol > g["max_curve_vsol"]:
            return "curve vSOL"
        if m.dev_sold is None:
            return "no data: dev"
        if m.dev_sold:
            return "dev sold"
        if not m.nondev_sell_seen and not self._snap_sell_seen(m):
            return "honeypot check: no other wallet has sold yet"
        if m.lag_s > g["max_signal_lag_s"]:
            return "signal lag"
        if m.safety is None:
            return "no data: safety card"
        if not m.safety.get("ok"):
            return "unsafe: " + ", ".join(m.safety.get("reasons") or ["safety"])
        return None

    @staticmethod
    def _snap_sell_seen(m: _Mint) -> bool:
        return bool(m.last_snap and (m.last_snap.get("sells_m5") or 0) >= 1)

    def wants_safety(self, mint: str, ts: float) -> bool:
        """True when this mint is worth the (rate-limited) safety prefetch: in the age window, no card yet, some interest."""
        m = self.mints.get(mint)
        if m is None or m.safety is not None or m.entered:
            return False
        age = ts - (m.created_ts if m.created_ts is not None else m.first_seen)
        if age < self.p["min_age_s"] - 10 or age > self.p["max_age_s"]:
            return False
        buys = sum(1 for t in m.trades if t[1] == "buy" and t[0] >= ts - 60)
        snap_buys = (m.last_snap or {}).get("buys_m5") or 0
        return buys >= self.p["burst"]["min_buys"] // 2 or snap_buys >= self.p["snapshot"]["min_m5_buys"] // 2

    # ---- pending fills ---------------------------------------------------------------------------------------------------------
    def _process_pending(self, ts: float, price_mint: str | None, events: list) -> None:
        for order in list(self.pending):
            m = self.mints.get(order["mint"])
            if order["kind"] == "buy":
                if ts > order["deadline"]:  # waited the full 20 s without a real price
                    self._cancel_buy(order, m, ts, "no real price within the wait", events)
                elif price_mint == order["mint"] and ts >= order["due"]:
                    self._fill_buy(order, m, ts, events)
            else:
                if price_mint == order["mint"] and ts >= order["due"]:
                    self._fill_sell(order, m, ts, m.price_usd, False, events)
                elif ts >= order["force_at"] and order["mint"] in self.positions:
                    px = m.price_usd or self.positions[order["mint"]]["entry_price"]
                    self._fill_sell(order, m, ts, px, True, events)  # no tick for the coin: marked at its last real price

    def _cancel_buy(self, order: dict, m: _Mint, ts: float, why: str, events: list) -> None:
        self.pending.remove(order)
        m.pending_buy = False
        m.cooldown_until = ts + self.p["gates"]["cooldown_s"]
        events.append({"type": "entry_cancel", "ts": ts, "mint": order["mint"], "reason": why, "order": order["id"]})

    def _fill_buy(self, order: dict, m: _Mint, ts: float, events: list) -> None:
        price = m.price_usd
        run = 100.0 * (price / order["signal_price"] - 1.0)
        if run > self.p["gates"]["cancel_run_pct"]:
            return self._cancel_buy(order, m, ts, f"ran +{run:.0f}% before the fill", events)
        if order["size"] > self.cash or len(self.positions) >= self.p["max_open"]:
            return self._cancel_buy(order, m, ts, "cash or max open at fill time", events)
        self.pending.remove(order)
        m.pending_buy, m.entered = False, True
        safety = order["safety"] or {}
        jup = max(0.0, (safety.get("rt_loss_pct") or 0.0) / 2.0)  # the real route worse than the model: extra on each leg
        cost = fees.side_cost(order["size"], curve=m.venue == "curve", jupiter_extra_pct=jup, params=self.fee_params())
        tokens = (order["size"] - cost.total) / price
        self.cash -= order["size"]
        ex = self.p["exits"]
        stop_pct = ex["stop_pct"]
        k = self.p["experiments"].get("vol_stop_k")
        if k:
            stop_pct = max(stop_pct, k * self._volatility_pct(m))
        c = council(order["signal"], safety)
        pos = {"id": order["id"], "mint": order["mint"], "entry_ts": ts, "entry_price": price, "size_usd": order["size"], "tokens0": tokens,
               "tokens": tokens, "stop_pct": stop_pct, "peak": price, "tp1": False, "tp2": False, "exiting": False, "be": False,
               "fees_buy": cost.total, "fees_sell": 0.0, "proceeds": 0.0, "legs": [], "signal": order["signal"], "safety": safety,
               "council": c["line"], "jup_extra_pct": jup, "round_trip_pct": safety.get("rt_loss_pct"), "signal_lag_s": order["lag"],
               "fill_latency_s": ts - order["trigger_ts"], "venue": m.venue, "liq_entry": m.liq_usd, "mode": order["signal"]["mode"]}
        self.positions[order["mint"]] = pos
        events.append({"type": "fill_buy", "ts": ts, "mint": order["mint"], "price": price, "size": order["size"], "tokens": tokens,
                       "fees": cost.total, "council": c["line"], "signal": order["signal"], "latency_s": pos["fill_latency_s"]})

    @staticmethod
    def _volatility_pct(m: _Mint) -> float:
        px = [t[4] for t in list(m.trades)[-20:] if t[4]] or [s[1] for s in list(m.snaps)[-20:] if s[1]]
        if len(px) < 3:
            return 0.0
        rets = [100.0 * (b / a - 1.0) for a, b in zip(px, px[1:]) if a]
        mean = sum(rets) / len(rets)
        return (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5

    # ---- exits -------------------------------------------------------------------------------------------------------------------
    def _check_exits(self, ts: float, price_mint: str | None, events: list) -> None:
        ex, exps = self.p["exits"], self.p["experiments"]
        for mint, pos in list(self.positions.items()):
            if pos["exiting"]:
                continue
            m = self.mints[mint]
            fresh = price_mint == mint
            price = m.price_usd or pos["entry_price"]
            if fresh:
                pos["peak"] = max(pos["peak"], price)
            entry, held = pos["entry_price"], ts - pos["entry_ts"]
            rule = None
            if m.dev_sold:
                rule = "dev sold"
            elif fresh and price <= entry * (1 - pos["stop_pct"] / 100.0) * (1 + 1e-9):
                rule = f"stop -{pos['stop_pct']:g}%"
            if rule is None and exps.get("breakeven_after_pct") and fresh:
                if not pos["be"] and price >= entry * (1 + exps["breakeven_after_pct"] / 100.0):
                    pos["be"] = True
                # buffer 0 = the entry price. "Entry + fees" is buffer = the round-trip cost (~9% on a $6 ticket), which sits
                # ABOVE a +6% arming level: the experiment grid tests both rather than assuming either.
                be_stop = entry * (1 + exps.get("breakeven_buffer_pct", 0.0) / 100.0)
                if pos["be"] and price <= be_stop:
                    rule = "break-even stop"
            if rule is None and fresh and not pos["tp1"] and price >= entry * (1 + ex["tp1_pct"] / 100.0) * (1 - 1e-9):
                pos["tp1"] = True
                self._queue_sell(pos, ts, ex["tp1_sell"] / 100.0 * pos["tokens0"], f"tp1 +{ex['tp1_pct']:g}%", False, events)
            if rule is None and fresh and not pos["tp2"] and price >= entry * (1 + ex["tp2_pct"] / 100.0) * (1 - 1e-9):
                pos["tp2"] = True
                self._queue_sell(pos, ts, ex["tp2_sell"] / 100.0 * pos["tokens0"], f"tp2 +{ex['tp2_pct']:g}%", False, events)
            if rule is None and fresh and pos["tp2"]:
                trail = ex["trail_pct"]
                if exps.get("vol_stop_k"):
                    trail = max(trail, exps["vol_stop_k"] * self._volatility_pct(m))
                if price <= pos["peak"] * (1 - trail / 100.0) * (1 + 1e-9):
                    rule = f"trail -{trail:g}%"
            if rule is None and held >= ex["time_stop_s"] and not pos["tp1"] and price <= entry:
                rule = "time stop"
            if rule is None and held >= ex["max_hold_s"]:
                rule = "max hold"
            if rule is None and not fresh and ts - m.last_tick_ts >= ex["stale_s"]:
                rule = "stale"
            if rule is None and fresh and exps.get("sell_pressure_exit"):
                if self._sell_pressure(m, ts, exps["sell_pressure_exit"]):
                    rule = "sell pressure"
            if rule is None and fresh and exps.get("liq_drop_exit_pct") and pos["liq_entry"] and m.liq_usd is not None:
                if m.liq_usd <= pos["liq_entry"] * (1 - exps["liq_drop_exit_pct"] / 100.0):
                    rule = "liquidity drop"
            if rule:
                pos["exiting"] = True
                self._queue_sell(pos, ts, None, rule, True, events)

    @staticmethod
    def _sell_pressure(m: _Mint, ts: float, cfg: dict) -> bool:
        win = [t for t in m.trades if t[0] >= ts - cfg["window_s"]]
        net = sum(t[2] if t[1] == "buy" else -t[2] for t in win)
        sellers = {t[3] for t in win if t[1] == "sell"}
        return net < 0 and len(sellers) >= cfg["min_sellers"]

    def _queue_sell(self, pos: dict, ts: float, tokens: float | None, rule: str, full: bool, events: list) -> None:
        f = self.p["fill"]
        due = ts + f["latency_s"]
        order = {"id": self._next_id(), "kind": "sell", "mint": pos["mint"], "trigger_ts": ts, "due": due, "force_at": due + f["exit_force_s"],
                 "tokens": tokens, "rule": rule, "full": full}
        self.pending.append(order)
        events.append({"type": "exit_signal", "ts": ts, "mint": pos["mint"], "rule": rule, "order": order["id"]})

    def _fill_sell(self, order: dict, m: _Mint, ts: float, price: float, forced: bool, events: list) -> None:
        self.pending.remove(order)
        pos = self.positions.get(order["mint"])
        if pos is None:
            return
        tokens = pos["tokens"] if order["full"] else min(order["tokens"], pos["tokens"])
        gross = tokens * price
        cost = fees.side_cost(gross, curve=pos["venue"] == "curve", jupiter_extra_pct=pos["jup_extra_pct"], params=self.fee_params())
        proceeds = gross - cost.total
        pos["tokens"] -= tokens
        pos["proceeds"] += proceeds
        pos["fees_sell"] += cost.total
        self.cash += proceeds
        pos["legs"].append({"ts": ts, "rule": order["rule"], "price": price, "tokens": tokens, "proceeds": proceeds, "fee": cost.total,
                            "latency_s": ts - order["trigger_ts"], "forced_at_last_price": forced})
        events.append({"type": "fill_sell", "ts": ts, "mint": order["mint"], "rule": order["rule"], "price": price, "proceeds": proceeds,
                       "fee": cost.total, "forced": forced})
        if pos["tokens"] <= pos["tokens0"] * 1e-9 or order["full"]:
            self._close(pos, ts, order["rule"], events)

    def _close(self, pos: dict, ts: float, rule: str, events: list) -> None:
        mint = pos["mint"]
        for o in list(self.pending):  # a closed position has no other orders
            if o["kind"] == "sell" and o["mint"] == mint:
                self.pending.remove(o)
        del self.positions[mint]
        pnl = pos["proceeds"] - pos["size_usd"]
        trade = {"mint": mint, "entry_ts": pos["entry_ts"], "exit_ts": ts, "hold_s": ts - pos["entry_ts"], "mode": pos["mode"],
                 "entry_price": pos["entry_price"], "size_usd": pos["size_usd"], "tokens": pos["tokens0"], "entry_reason": pos["mode"],
                 "signal": pos["signal"], "safety_card": pos["safety"], "council": pos["council"], "round_trip_pct": pos["round_trip_pct"],
                 "fees_buy": pos["fees_buy"], "fees_sell": pos["fees_sell"], "fees_total": pos["fees_buy"] + pos["fees_sell"],
                 "signal_lag_s": pos["signal_lag_s"], "fill_latency_s": pos["fill_latency_s"], "exit_rule": rule, "legs": pos["legs"],
                 "pnl_usd": pnl, "pnl_pct": 100.0 * pnl / pos["size_usd"], "venue": pos["venue"]}
        self.trades.append(trade)
        self.mints[mint].cooldown_until = ts + self.p["gates"]["cooldown_s"]
        events.append({"type": "trade_closed", "ts": ts, "mint": mint, "trade": trade})

    # ---- restart / sleep ----------------------------------------------------------------------------------------------------------
    def close_all_at_last_price(self, ts: float, reason: str = "desk restart · marked at last real price") -> list[dict]:
        """After a long sleep or a restart: close every open position at its last real price (never an invented one)."""
        events: list[dict] = []
        self.pending = [o for o in self.pending if o["kind"] != "buy"]
        for mint in list(self.positions):
            m = self.mints[mint]
            order = {"id": self._next_id(), "kind": "sell", "mint": mint, "trigger_ts": ts, "due": ts, "force_at": ts, "tokens": None,
                     "rule": reason, "full": True}
            self.pending.append(order)
            self._fill_sell(order, m, ts, m.price_usd or self.positions[mint]["entry_price"], True, events)
        return events
