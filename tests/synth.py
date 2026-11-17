"""SYNTHETIC market for testing the replay machinery. It proves mechanics (determinism, splits, tables), NOT profitability:
the price paths are random numbers, so nothing measured on this data says anything about real coins."""
import random

SOL = 150.0
DAY0 = 1_700_006_400.0  # a UTC midnight


def market(days=3, per_day=14, seed=7, mint_prefix="SYN"):
    rng = random.Random(seed)
    ticks = []
    for d in range(days):
        base_ts = DAY0 + d * 86400
        ticks.append({"kind": "sol", "ts": base_ts, "usd": SOL})
        for i in range(per_day):
            mint = f"{mint_prefix}{d}_{i}"
            t0 = base_ts + 600 + i * 1500 + rng.random() * 300
            va = 64.0 + rng.random() * 14.0
            seq = []
            seq.append({"kind": "create", "ts": t0, "mint": mint, "creator": "DEV", "v_sol": 30.0, "v_tokens": 1e9, "initial_buy": 5e7})
            seq.append({"kind": "safety", "ts": t0 + 60, "mint": mint, "ok": True, "reasons": [], "rt_loss_pct": 3.0, "sell_impact_pct": 2.0})
            v, t = va, t0 + 30
            for k in range(8):  # calm trades
                v *= 1 + (rng.random() - 0.5) * 0.01
                seq.append(_tr(t, "buy" if rng.random() < 0.55 else "sell", rng.random() * 0.3, f"C{k}", v, mint))
                t += 3
            t = t0 + 100
            seq.append(_tr(t, "sell", 0.2, "S1", v, mint))
            for k in range(10):  # the burst
                t += 2
                v *= 1.009
                seq.append(_tr(t, "buy", 0.5, f"B{k % 8}", v, mint))
            drift = rng.choice([0.012, 0.004, -0.004, -0.012, -0.02])  # what happens next
            for k in range(25):
                t += 2
                v *= 1 + drift + (rng.random() - 0.5) * 0.02
                seq.append(_tr(t, "buy" if rng.random() < 0.5 else "sell", 0.4, f"X{k % 6}", v, mint))
            ticks += seq
    ticks.sort(key=lambda x: (x["ts"], x.get("mint", "")))
    return ticks


def _tr(ts, side, sol, wallet, v, mint):
    return {"kind": "trade", "ts": ts, "mint": mint, "side": side, "sol": sol, "tokens": sol * 1e7, "wallet": wallet, "v_sol": v, "v_tokens": 1e9}
