"""The PRICE half of the 'prompt' strategy, run on real Solana memecoin 5-minute candles, with the owner's own default settings.

What is modelled (settings.py defaults, not changed):  Scanner passes a coin at pool age 10 min if liquidity >= $20k and 1h change < 200%;
Brain sizes risk_pct 3% / stop 25% = 12% of cash at full score (6% below), capped at 2% of pool liquidity and $50, min $2;
Executor waits up to 30 min for price >= 15% under the 10-min high (but not a crash deeper than 35%) with buyers back (a green candle stands in);
Risk: stop -25.1%, take profit at 2x sells 50% then the rest trails 40% under the peak, dead money after 3 h under +5%.
NOT modelled (no historical data exists): the Analyst's 8 layers and score (rug / dev / bundle checks). It is therefore an UPPER bound on
the entries, and every number below includes coins the Analyst would have refused.
Fills: next candle open + 1% slippage per side, real FOMO fee tiers, stop checked before target in a candle, ticket <= 5% of candle volume.
"""
import itertools, json, sys, glob, time
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import backtest as B

CASH0 = 150.0
D = dict(pass_age=2, liq=20000, h1_max=200, dip_pct=15, crash=35, wait=6, stop=25.1, tp=2.0, tp_sell=0.5, trail=40, dead_c=36, dead_gain=5,
         risk_pct=3.0, full=True, pool_cap=2.0, max_ticket=50.0)


def sim_coin(p, c, k):
    """One coin: returns [(entry_ts, exit_ts, entry_price, multiple, liquidity)] or []."""
    n = len(c)
    if not B.START_OK.get(id(c)) or n < k["pass_age"] + 3 or p["reserve_usd"] < k["liq"]:
        return []
    i0 = k["pass_age"]                                              # passes the Scanner at pool age 10 min
    if i0 >= 12 and c[i0][4] > c[i0 - 12][1] * (1 + k["h1_max"] / 100):
        return []
    for i in range(i0, min(n - 2, i0 + k["wait"])):                 # the dip order waits up to 30 min
        hi = max(x[2] for x in c[max(0, i - 1):i + 1])              # high of the last 10 minutes
        drop = 100 * (1 - c[i][4] / hi) if hi > 0 else 0
        if drop > k["crash"]:
            return []                                               # crash: skipped
        if drop >= k["dip_pct"] and c[i][4] > c[i][1] and c[i][5] >= B.MIN_CANDLE_VOL:   # dip, with buyers back
            entry = c[i + 1][1] * (1 + B.SLIP)
            if entry <= 0:
                return []
            stop_px = entry * (1 - k["stop"] / 100)
            peak, tp_done, got = entry, False, 0.0
            for j in range(i + 1, n):
                o, h, l, cl = c[j][1], c[j][2], c[j][3], c[j][4]
                rem = (1 - k["tp_sell"]) if tp_done else 1.0
                if l <= stop_px:
                    return [(c[i + 1][0], c[j][0], entry, got + rem * min(stop_px, o) * (1 - B.SLIP) / entry, c[i + 1][5], p["reserve_usd"])]
                if not tp_done and h >= entry * k["tp"]:
                    got += k["tp_sell"] * entry * k["tp"] * (1 - B.SLIP) / entry; tp_done = True; peak = max(peak, h); rem = 1 - k["tp_sell"]
                    continue
                peak = max(peak, h)
                if tp_done and cl <= peak * (1 - k["trail"] / 100):
                    return [(c[i + 1][0], c[j][0], entry, got + rem * cl * (1 - B.SLIP) / entry, c[i + 1][5], p["reserve_usd"])]
                if j - (i + 1) >= k["dead_c"] and not tp_done and cl < entry * (1 + k["dead_gain"] / 100):
                    return [(c[i + 1][0], c[j][0], entry, got + rem * cl * (1 - B.SLIP) / entry, c[i + 1][5], p["reserve_usd"])]
            return [(c[i + 1][0], c[n - 1][0], entry, got + rem * c[n - 1][4] * (1 - B.SLIP) / entry, c[i + 1][5], p["reserve_usd"])]
    return []


def book(trades, k, max_night=30):
    cash, open_, n, wins, gw, gl, peak, dd = CASH0, [], 0, 0, 0.0, 0.0, CASH0, 0.0
    day, day_n, curve = None, 0, []
    for t_in, t_out, entry, mult, vol, liq in sorted(trades):
        for o in [o for o in open_ if o[0] <= t_in]:
            open_.remove(o); cash += o[1]
        d = int(t_in // 86400)
        if d != day: day, day_n = d, 0
        if day_n >= max_night:
            continue
        frac = k["risk_pct"] / k["stop"] * (1 if k["full"] else 0.5)
        size = min(cash * frac, liq * k["pool_cap"] / 100, k["max_ticket"], vol * B.VOL_SHARE)
        if size < 2.0 or size > cash:
            continue
        f_in = B.side_fee(size); value = size * (1 - f_in) * mult; proceeds = value * (1 - B.side_fee(max(value, 0.01)))
        cash -= size; open_.append((t_out, proceeds, size)); n += 1; day_n += 1
        pnl = proceeds - size
        if pnl > 0: wins += 1; gw += pnl
        else: gl -= pnl
        eq = cash + sum(o[2] for o in open_) + pnl
        peak = max(peak, eq); dd = max(dd, (peak - eq) / peak)
    final = cash + sum(o[1] for o in open_)
    return {"final": final, "trades": n, "win": 100 * wins / n if n else None, "pf": gw / gl if gl > 0 else None, "dd": 100 * dd}


def trades_all(pools, k):
    out = []
    for p, c in pools:
        out += sim_coin(p, c, k)
    return out


def main():
    pools = B.load()
    for p, c in pools:
        B.START_OK[id(c)] = abs(c[0][0] - p["created"]) < 1800
    young = [(p, c) for p, c in pools if B.START_OK[id(c)]]
    t_lo, t_hi = min(c[0][0] for _, c in pools), max(c[-1][0] for _, c in pools)
    cut = t_lo + (t_hi - t_lo) / 2
    print(f"{len(pools)} memecoin pools in the data; {len(young)} have their very first candles (the only ones prompt's 'pass at 10 minutes' can be tested on)")
    base = trades_all(pools, D)
    print(f"prompt's own defaults: {len(base)} coins reached a dip entry ({100 * len(base) / max(1, len(young)):.0f}% of the young pools)")
    a, b = [t for t in base if t[0] < cut], [t for t in base if t[0] >= cut]
    allr = book(base, D)
    print(" whole month:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in allr.items()}, "| from $150")
    for name, tr in (("first half", a), ("second half", b)):
        print(f" {name}:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in book(tr, D).items()})
    mults = [t[3] for t in base]
    if mults:
        mults.sort()
        print(f" per-trade multiple (before fees): median {mults[len(mults)//2]:.2f}x, stopped out {100 * sum(m < 0.8 for m in mults) / len(mults):.0f}%, reached 2x {100 * sum(m >= 1.4 for m in mults) / len(mults):.0f}%, best {mults[-1]:.2f}x, worst {mults[0]:.2f}x")
    print("\nSENSITIVITY (one setting at a time; the owner's settings are NOT changed, these are only ideas to prove in paper):")
    def one(name, values):
        for v in values:
            k = dict(D); k[name] = v
            if name == "wait": k["wait"] = v
            r = book(trades_all(pools, k), k)
            ra, rb = book([t for t in trades_all(pools, k) if t[0] < cut], k), book([t for t in trades_all(pools, k) if t[0] >= cut], k)
            print(f"  {name}={v}: month ${r['final']:.0f} ({r['trades']} tr, win {r['win'] and round(r['win'])}%, pf {r['pf'] and round(r['pf'], 2)}, dd {r['dd']:.0f}%)  1st half ${ra['final']:.0f} | 2nd half ${rb['final']:.0f}")
    one("dip_pct", (5, 10, 15, 20, 25)); one("stop", (10, 15, 25.1, 35, 50)); one("tp", (1.3, 1.5, 2.0, 3.0, 5.0)); one("trail", (15, 25, 40, 60)); one("pass_age", (2, 6, 12, 36)); one("full", (True, False))
    one("risk_pct", (1.0, 3.0, 6.0, 12.0))


if __name__ == "__main__":
    main()
