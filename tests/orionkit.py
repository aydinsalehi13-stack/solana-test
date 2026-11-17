"""Hand-built tick sequences for the orion engine tests. v_sol drives the curve price: price_usd = v_sol / 1e9 * SOL_USD."""
import orion_core as oc

SOL = 150.0
T0 = 1_700_000_000.0


def px(v_sol, sol=SOL):
    return v_sol / 1e9 * sol


def params(**over):
    return oc.merge(oc.load_params(), over)


def base(mint="M", t0=T0, safety_ok=True, sol=SOL, create=True, rt=3.0, safety=True, creator="DEV"):
    out = [{"kind": "sol", "ts": t0, "usd": sol}]
    if create:
        out.append({"kind": "create", "ts": t0, "mint": mint, "creator": creator, "v_sol": 30.0, "v_tokens": 1e9, "initial_buy": 5e7})
    if safety:
        out.append({"kind": "safety", "ts": t0 + 60, "mint": mint, "ok": safety_ok, "reasons": [] if safety_ok else ["mint authority"],
                    "rt_loss_pct": rt, "sell_impact_pct": 2.0})
    return out


def trade(ts, side, sol, wallet, v_sol, mint="M", **kw):
    return {"kind": "trade", "ts": ts, "mint": mint, "side": side, "sol": sol, "tokens": sol * 1e7, "wallet": wallet,
            "v_sol": v_sol, "v_tokens": 1e9, **kw}


def burst(mint="M", start=T0 + 100, buys=9, buyers=7, v0=70.0, v1=77.0, sell_first=True, step=2.0, sol_each=0.5, heavy=False,
          extra_sells=0, **kw):
    """A burst: (optionally) one sell by another wallet first, then `buys` buys cycling over `buyers` wallets."""
    seq = ([("sell", "S1", 0.2)] if sell_first else [])
    seq += [("sell", f"S{2 + i}", 0.1) for i in range(extra_sells)]
    seq += [("buy", f"B{i % buyers}", 3.0 if (heavy and i == 0) else sol_each) for i in range(buys)]
    out = []
    for i, (side, w, sol) in enumerate(seq):
        v = v0 + (v1 - v0) * i / max(1, len(seq) - 1)
        out.append(trade(start + i * step, side, sol, w, v, mint, **kw))
    return out


def run(eng, ticks):
    events = []
    for t in ticks:
        events += eng.on_tick(t)
    return events


def kinds(events, typ):
    return [e for e in events if e["type"] == typ]


def entered(params_=None, mint="M", **bkw):
    """An engine with one open position on `mint`. Returns (engine, entry_ts, events)."""
    eng = oc.Engine(params_ or params())
    ev = run(eng, base(mint) + burst(mint, **bkw))
    fills = kinds(ev, "fill_buy")
    assert len(fills) == 1, [e for e in ev if e["type"] in ("skip", "entry_cancel")]
    return eng, fills[0]["ts"], ev
