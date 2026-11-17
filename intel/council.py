"""Advisory council (the IDEA from TradingAgents, none of its code): analyst -> bull/bear -> manager -> risk veto.

Rule based, $0, microseconds. It only writes one line per buy ("council go (+1)"); it never blocks an entry and
never changes size. The strategy's own gates decide.
"""
from __future__ import annotations


def council(signal: dict, safety: dict | None) -> dict:
    votes: list[tuple[str, int]] = []
    buyers = signal.get("buyers")
    if buyers is not None:  # analyst: is the crowd broad?
        votes.append(("analyst", 1 if buyers >= 10 else 0 if buyers >= 6 else -1))
    rise = signal.get("rise_pct")
    ratio = signal.get("buy_sell_ratio")
    bull = (1 if rise is not None and rise < 30 else 0) + (1 if ratio is not None and ratio >= 3 else 0)
    bear = (1 if rise is not None and rise >= 45 else 0) + (1 if ratio is not None and ratio < 2.2 else 0) \
        + (1 if signal.get("top_wallet_pct") is not None and signal["top_wallet_pct"] > 30 else 0)
    votes.append(("bull", bull))
    votes.append(("bear", -bear))
    net = sum(v for _, v in votes)  # manager
    rt = (safety or {}).get("rt_loss_pct")
    veto = rt is not None and rt > 8.0  # risk: a round trip that eats most of the stop
    verdict = "veto" if veto else "go" if net >= 1 else "hold"
    return {"verdict": verdict, "net": net, "line": f"council {verdict} ({net:+d})"}
