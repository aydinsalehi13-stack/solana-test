"""Backtest many strategy combinations on real 5-minute candles of Solana memecoin pools created in the last month.

Honest by construction:
  * a signal seen on candle i is filled at the OPEN of candle i+1 (you are never faster than the next bar), plus slippage each side
  * fees: the desk's own FOMO fee tiers (fees.py) for the actual ticket size, each side
  * inside one candle the stop is checked before the take-profit (the worse order), and a stop fills at the stop or the open, whichever is lower
  * a ticket is capped at a share of that candle's real traded volume, so a $2,000 order cannot "fill" in a $300 candle
  * parameters are chosen on the FIRST half of the month and judged on the SECOND half (walk-forward); the in-sample number is never the result
Known bias that makes results look BETTER than reality: the pools come from lists of pools that are active now (survivorship),
5-minute candles hide wicks inside the bar, and rugs that froze trading still show candles here. Read every result as an upper bound.
"""
import glob, itertools, json, math, sys, time
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import fees

DATA = sys.argv[1]
START = 100.0
SLIP = 0.01          # 1% slippage per side on top of the fee (small pools)
VOL_SHARE = 0.05     # a ticket is at most 5% of the candle's traded volume
MIN_CANDLE_VOL = 500.0


def load():
    pools = []
    for f in glob.glob(DATA + "/*.json"):
        if f.endswith("pools.json"):
            continue
        d = json.load(open(f))
        p, c = d["pool"], d["candles"]
        if p["src"] in ("trending", "sol_tx") and not p["name"].endswith("/ SOL"):
            continue  # not a memecoin pair
        if len(c) < 30:
            continue
        pools.append((p, c))
    return pools


def side_fee(usd):
    return fees.side_cost(usd, curve=False).total / usd if usd > 0 else 0.0


# ---- entries: each returns the indices i where a signal is seen at the close of candle i ----
def entries(c, kind, a, b):
    out = []
    n = len(c)
    closes = [x[4] for x in c]
    vols = [x[5] for x in c]
    if kind == "breakout":          # close above the high of the last a candles, on b x the average volume
        for i in range(a, n - 1):
            hi = max(x[2] for x in c[i - a:i])
            av = sum(vols[i - a:i]) / a
            if closes[i] > hi and vols[i] >= b * av and vols[i] >= MIN_CANDLE_VOL:
                out.append(i)
    elif kind == "dip":             # fell a% from the high of the last 12 candles, then a green candle with volume (buyers back)
        for i in range(12, n - 1):
            hi = max(x[2] for x in c[i - 12:i])
            if hi > 0 and closes[i] <= hi * (1 - a / 100) and c[i][4] > c[i][1] and vols[i] >= MIN_CANDLE_VOL and closes[i] >= hi * (1 - b / 100):
                out.append(i)
    elif kind == "young":           # a candles after the pool opened, still above its first price by b%
        if n > a + 1 and START_OK.get(id(c)) and closes[a] >= c[0][1] * (1 + b / 100) and vols[a] >= MIN_CANDLE_VOL:
            out.append(a)
    elif kind == "trend":           # a green candles in a row with rising volume, total move at least b%
        for i in range(a, n - 1):
            run = c[i - a + 1:i + 1]
            if all(x[4] > x[1] for x in run) and run[-1][4] >= run[0][1] * (1 + b / 100) and vols[i] >= MIN_CANDLE_VOL and vols[i] >= vols[i - 1]:
                out.append(i)
    return out


START_OK = {}   # the candles really start when the pool was created (else "young" would be judged on a random later moment)
_CACHE = {}


def cached_entries(c, ent):
    k = (id(c), ent)
    if k not in _CACHE:
        _CACHE[k] = entries(c, *ent)
    return _CACHE[k]


def run_trade(c, i, stop, tp, trail, hold):
    """Enter at the open of candle i+1. Returns (exit_index, gross multiple of entry price, min candle volume seen at entry)."""
    entry = c[i + 1][1] * (1 + SLIP)
    if entry <= 0:
        return None
    peak, half_out, got = entry, False, 0.0
    stop_px = entry * (1 - stop / 100)
    for j in range(i + 1, min(len(c), i + 1 + hold)):
        o, h, l, cl = c[j][1], c[j][2], c[j][3], c[j][4]
        if l <= stop_px:                                   # stop first: the worse order inside a candle
            px = min(stop_px, o) * (1 - SLIP)
            return j, (got + (0.5 if half_out else 1.0) * px / entry), c[i + 1][5]
        if not half_out and tp and h >= entry * tp:        # take profit on half
            got += 0.5 * entry * tp * (1 - SLIP) / entry
            half_out = True
            stop_px = max(stop_px, entry)                  # the rest cannot lose
        peak = max(peak, h)
        if trail and peak > entry * 1.1 and cl <= peak * (1 - trail / 100):
            px = cl * (1 - SLIP)
            return j, (got + (0.5 if half_out else 1.0) * px / entry), c[i + 1][5]
    j = min(len(c), i + 1 + hold) - 1                      # time is up: sell at the close
    return j, (got + (0.5 if half_out else 1.0) * c[j][4] * (1 - SLIP) / entry), c[i + 1][5]


def trades_for(pools, ent, ex):
    kind, a, b = ent
    stop, tp, trail, hold = ex
    out = []
    for p, c in pools:
        free_from = 0
        for i in cached_entries(c, ent):
            if i < free_from:
                continue                                   # one position per coin at a time
            r = run_trade(c, i, stop, tp, trail, hold)
            if r is None:
                continue
            j, mult, vol = r
            out.append((c[i + 1][0], c[j][0], mult, vol, p["address"]))
            free_from = j + 1
    out.sort()
    return out


def portfolio(trades, frac, max_open=3):
    """Compounding from $100: each trade risks `frac` of current equity (capped by real candle volume), at most max_open at once."""
    cash, open_, eq_curve, n, wins, gross_win, gross_loss, peak, dd, fee_total = START, [], [], 0, 0, 0.0, 0.0, START, 0.0, 0.0
    for t_in, t_out, mult, vol, addr in trades:
        for o in sorted([o for o in open_ if o[0] <= t_in]):   # settle what has closed by now
            open_.remove(o)
            cash += o[1]
        if len(open_) >= max_open:
            continue
        equity = cash + sum(o[2] for o in open_)
        size = min(equity * frac, cash, vol * VOL_SHARE)
        if size < 2.0:
            continue
        f_in = side_fee(size)
        value_out = size * (1 - f_in) * mult
        f_out = side_fee(max(value_out, 0.01))
        proceeds = value_out * (1 - f_out)
        fee_total += size * f_in + value_out * f_out
        cash -= size
        open_.append((t_out, proceeds, size))
        pnl = proceeds - size
        n += 1
        if pnl > 0:
            wins += 1; gross_win += pnl
        else:
            gross_loss -= pnl
        eq_after = cash + sum(o[2] for o in open_) + pnl
        peak = max(peak, eq_after)
        dd = max(dd, (peak - eq_after) / peak if peak > 0 else 0)
    final = cash + sum(o[1] for o in open_)
    return {"final": final, "trades": n, "win_rate": 100 * wins / n if n else None, "pf": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else None),
            "max_dd": 100 * dd, "fees": fee_total}


ENTRIES = ([("breakout", a, b) for a in (6, 12, 24) for b in (1.5, 3, 5)] + [("dip", a, b) for a in (15, 25, 35) for b in (40, 60)] +
           [("young", a, b) for a in (2, 6, 12) for b in (0, 20, 50)] + [("trend", a, b) for a in (2, 3, 4) for b in (10, 25)])
EXITS = [(stop, tp, trail, hold) for stop in (8, 15, 25, 40) for tp in (0, 1.5, 2.0, 3.0) for trail in (0, 20, 40) for hold in (12, 48, 288)]
FRACS = (0.1, 0.25, 0.5, 1.0)


def main():
    pools = load()
    for p, c in pools:
        START_OK[id(c)] = abs(c[0][0] - p["created"]) < 1800
    all_ts = sorted(c[0][0] for _, c in pools)
    t_lo, t_hi = min(c[0][0] for _, c in pools), max(c[-1][0] for _, c in pools)
    cut = t_lo + (t_hi - t_lo) / 2
    print(f"{len(pools)} memecoin pools, {sum(len(c) for _, c in pools):,} five-minute candles, {time.strftime('%b %d', time.gmtime(t_lo))} to {time.strftime('%b %d', time.gmtime(t_hi))} (UTC); split at {time.strftime('%b %d %H:%M', time.gmtime(cut))}", flush=True)
    rows, t0 = [], time.time()
    combos = list(itertools.product(ENTRIES, EXITS))
    for k, (ent, ex) in enumerate(combos):
        tr = trades_for(pools, ent, ex)
        tr_a, tr_b = [t for t in tr if t[0] < cut], [t for t in tr if t[0] >= cut]
        for frac in FRACS:
            a, b = portfolio(tr_a, frac), portfolio(tr_b, frac)
            rows.append({"entry": ent, "exit": ex, "frac": frac, "train": a, "test": b})
        if k % 100 == 0:
            print(f"  {k}/{len(combos)} combos, {time.time() - t0:.0f}s", flush=True)
    json.dump(rows, open(sys.argv[2], "w"))
    print(f"{len(rows)} strategy x size combinations tested in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
