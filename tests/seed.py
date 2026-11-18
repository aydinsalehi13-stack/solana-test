"""DEMO DATA for tests and QA screenshots only. Every mint is called DemoMint*. The product itself never makes up numbers:
the dashboard shows whatever is in the database, and these rows are only ever inserted by tests."""
import json
import math
import random
import time

DEMO_MINTS = [f"DemoMint{i:02d}" + "x" * 26 + "pump" for i in range(12)]
SYMS = ["PAWS", "MOCHI", "LUNA", "BORK", "NIMBUS", "YOSHI", "KIRBY", "OTTO", "PIXEL", "ZEN", "TOAST", "RIVER"]
LAYER_NAMES = ["Mint authority", "Freeze authority", "Dev wallet", "Top 10", "Liquidity", "Pool age", "Bundles", "Delegations"]
LAYER_VALUES = ["Revoked", "Revoked", "0.9% · sold 0", "14%", "100% burned", "14 min", "2 bundled · insiders 4%", "None"]


async def seed_demo(db, now=None, seed=11):
    now = now or time.time()
    rng = random.Random(seed)
    await db.execute("INSERT INTO nights(strategy, started_at, start_cash) VALUES ('prompt', ?, 150)", (now - 5 * 3600,))
    await db.execute("UPDATE nights SET trades=3")
    # equity curves: 6 hours, both strategies from 150
    a, b = 150.0, 150.0
    rows_a, rows_b = [], []
    for i in range(0, 361, 3):
        ts = now - (360 - i) * 60
        a += rng.gauss(0.02, 0.35)
        b += rng.gauss(-0.04, 0.6)
        rows_a.append((ts, "prompt", a - 12, a, "PAPER"))
        rows_b.append((ts, "orion_fast", b, b))
    await db.executemany("INSERT INTO balance(ts, strategy, cash_usd, equity_usd, mode) VALUES (?,?,?,?,?)", rows_a)
    await db.executemany("INSERT INTO strat_balance(ts, strategy, cash_usd, equity_usd) VALUES (?,?,?,?)", [(t, s, c, c) for t, s, c, _ in rows_b])
    # closed + open positions
    for i, (sym, pnl, why) in enumerate([("PAWS", 4.8, "take profit 2x"), ("MOCHI", -4.1, "stop -25.3%"), ("LUNA", 1.9, "trail -40% from peak")]):
        await db.execute("INSERT INTO positions(mint, symbol, opened_at, entry_price, size_usd, tokens, peak_price, stop_price, closed_at, exit_price, "
                         "exit_reason, pnl_usd, fees_usd, mode, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (DEMO_MINTS[i], sym, now - 4000 + i * 600, 1.0, 16.0, 16.0, 1.2, 0.75, now - 3000 + i * 500, 1.1, why, pnl, 0.6, "PAPER", "prompt"))
    await db.execute("INSERT INTO positions(mint, symbol, opened_at, entry_price, size_usd, tokens, peak_price, stop_price, mode, strategy, meta, realized_usd) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (DEMO_MINTS[3], "BORK", now - 900, 0.5, 18.0, 36.0, 0.6, 0.375, "PAPER", "prompt", json.dumps({"initial_tokens": 36.0}), 0.0))
    for i, (sym, pnl, why) in enumerate([("NIMBUS", -1.3, "stop -8%"), ("YOSHI", 0.9, "tp2 +30%"), ("KIRBY", -1.2, "stop -8%"), ("OTTO", -0.6, "time stop")]):
        await db.execute("INSERT INTO strat_positions(strategy, mint, symbol, opened_at, closed_at, entry_price, exit_price, size_usd, tokens, pnl_usd, fees_usd, exit_rule, data) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("orion_fast", DEMO_MINTS[4 + i], sym, now - 3500 + i * 700, now - 3300 + i * 700, 1.0, 0.9, 6.0, 6.0, pnl, 0.65, why,
                                                                json.dumps({"council": "council go (+1)"})))
    await db.execute("INSERT INTO strat_positions(strategy, mint, symbol, opened_at, entry_price, size_usd, tokens, fees_usd, data) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("orion_fast", DEMO_MINTS[8], "PIXEL", now - 120, 2.0, 6.0, 3.0, 0.3, json.dumps({"council": "council go (+2)"})))
    # decisions: a mix of kills / passes / sizes / buys across the agents
    kinds = [("scanner", "kill", "liq $14.2K"), ("scanner", "kill", "top 10 hold 38%"), ("scanner", "kill", "copycat of paws (ran +450% today)"),
             ("scanner", "pass", "pass"), ("scanner", "kill", "holders leaving -4% in 10m"), ("analyst", "reject", "layer 3 Dev wallet: dev holds 31%"),
             ("analyst", "pass", "8/8 green, score 84"), ("analyst", "reject", "layer 8 Delegations: no data: delegations"), ("brain", "size", "risk size"),
             ("brain", "skip", "under $2"), ("executor", "wait", "dip order $18.00 waits up to 30 min"), ("executor", "buy", "dip -16.2% with buyers back"),
             ("executor", "skip", "no dip"), ("risk", "sell", "stop -25.3%"), ("orion", "skip", "entries paused: GeckoTerminal (20-50 s late): too slow to trade on"),
             ("orion", "buy", "burst signal"), ("orion", "sell", "stop -8%")]
    drows = []
    for i in range(140):
        bot, dec, why = kinds[i % len(kinds)]
        k = i % 12
        drows.append((now - i * 25 - rng.random() * 10, bot, DEMO_MINTS[k], SYMS[k], dec, why, None, "orion_fast" if bot == "orion" else "prompt", None))
    await db.executemany("INSERT INTO decisions(ts, bot, mint, symbol, decision, reason, value, strategy, data) VALUES (?,?,?,?,?,?,?,?,?)", drows)
    # one audited coin with all 8 layers + a score, candles for the coin view
    m = DEMO_MINTS[3]
    await db.executemany('INSERT INTO audits(mint, ts, layer, name, value_text, "pass", source) VALUES (?,?,?,?,?,?,?)',
                         [(m, now - 800 + i, i + 1, LAYER_NAMES[i], LAYER_VALUES[i], 1, "rpc") for i in range(8)])
    await db.execute('INSERT INTO scores(mint, ts, alive, organic, flush, chase, social, social_pts, fresh_pct, score, "pass", reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     (m, now - 790, 1.0, 0.67, 1.0, 0.05, None, 0, 12.0, 90, 1, "organic: txns; flush: txns; social: off"))
    await db.execute("INSERT INTO coins(mint, name, symbol, pool, created_at, migrated_at, curve_min, creator) VALUES (?,?,?,?,?,?,?,?)",
                     (m, "Bork Coin", "BORK", "PoolDemo", now - 4000, now - 2400, 12.0, "DevDemo"))
    await db.executemany("INSERT INTO prices(mint, ts, price, volume_m5, holders) VALUES (?,?,?,?,?)",
                         [(m, now - (120 - i) * 30, 0.5 + 0.1 * math.sin(i / 9) + i * 0.001, 1000, 400 + i) for i in range(120)])
    # the coin tape: one recent scan per demo coin with the 1 h change the scanner read
    h1 = [7.71, -0.41, 0.39, 15.99, -14.49, 1.42, -2.08, 4.2, -0.29, 22.6, None, 3.3]
    await db.executemany("INSERT INTO scans(mint, ts, price, liquidity_usd, h1_change_pct, verdict, reason) VALUES (?,?,?,?,?,?,?)",
                         [(DEMO_MINTS[i], now - 40 * i - 5, 0.01, 42000.0, h1[i], "pass" if i % 3 == 0 else "kill", "demo") for i in range(12)])
    await db.execute("INSERT INTO activity(ts, kind, bot, mint, text) VALUES (?,?,?,?,?)", (now - 100, "buy", "executor", m, "Bought BORK · $18.00"))
    await db.execute("INSERT INTO orders(ts, mint, symbol, side, usd, bot, reason, status, quote_text, fee_text, fill_usd, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (now - 900, m, "BORK", "buy", 18.0, "executor", "dip", "done", "36 BORK (-0.6%)", "$0.36 fee", 18.0, "prompt"))
    await db.executemany("INSERT INTO settings(key, value, updated_at) VALUES (?,?,?)", [("fomo_handle", "fluffcore3", now), ("fomo_address", "So11111111111111111111111111111111111111112", now)])
    await db.execute("SELECT 1")
