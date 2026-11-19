"""Download real Solana memecoin pool history from GeckoTerminal (public API, ~30 calls/min). Data only; nothing trades."""
import json, sys, time, pathlib, urllib.request, urllib.error
OUT = pathlib.Path(sys.argv[1]); OUT.mkdir(exist_ok=True)
BASE = "https://api.geckoterminal.com/api/v2"
GAP = 2.2
last = [0.0]

def get(path):
    for attempt in range(6):
        wait = GAP - (time.time() - last[0])
        if wait > 0: time.sleep(wait)
        last[0] = time.time()
        try:
            req = urllib.request.Request(BASE + path, headers={"accept": "application/json", "user-agent": "research-script"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429: print("429", flush=True); time.sleep(12); continue
            if e.code in (404, 422): return None
            time.sleep(5)
        except Exception:
            time.sleep(5)
    return None

now = time.time(); MONTH = 30 * 86400
pools = {}
def take(j, src):
    for p in (j or {}).get("data", []):
        a = p["attributes"]
        try: created = time.mktime(time.strptime(a["pool_created_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except Exception: continue
        if now - created > MONTH or now - created < 6 * 3600: continue      # created in the last month, at least 6 h of life
        pools.setdefault(a["address"], {"address": a["address"], "name": a["name"], "created": created, "src": src,
                                        "reserve_usd": float(a.get("reserve_in_usd") or 0), "fdv": float(a.get("fdv_usd") or 0)})

lists = [("/networks/solana/new_pools?page=%d", "new"), ("/networks/solana/dexes/pumpswap/pools?page=%d&sort=h24_tx_count_desc", "pumpswap_tx"),
         ("/networks/solana/dexes/pumpswap/pools?page=%d&sort=h24_volume_usd_desc", "pumpswap_vol"), ("/networks/solana/trending_pools?page=%d", "trending"),
         ("/networks/solana/pools?page=%d&sort=h24_tx_count_desc", "sol_tx"), ("/networks/solana/dexes/pump-fun/pools?page=%d", "pumpfun")]
import os
if (OUT / "pools.json").exists():
    pools = {p["address"]: p for p in json.load(open(OUT / "pools.json"))}
    lists = []
for tpl, src in lists:
    for page in range(1, 11):
        j = get(tpl % page)
        if not j or not j.get("data"): break
        take(j, src)
    print(src, len(pools), flush=True)
(OUT / "pools.json").write_text(json.dumps(list(pools.values())))

done = 0
for addr, p in sorted(pools.items(), key=lambda kv: kv[1]["created"]):
    f = OUT / f"{addr}.json"
    if f.exists(): done += 1; continue
    candles, before = [], int(now)
    for _ in range(4):                                   # up to 4000 five-minute candles (~14 days) back from now
        j = get(f"/networks/solana/pools/{addr}/ohlcv/minute?aggregate=5&limit=1000&before_timestamp={before}&currency=usd")
        rows = ((j or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
        if not rows: break
        candles += rows
        before = int(min(r[0] for r in rows)) - 1
        if len(rows) < 1000 or before < p["created"]: break
    candles = sorted({int(r[0]): r for r in candles}.values())
    f.write_text(json.dumps({"pool": p, "candles": candles}))
    done += 1
    if done % 20 == 0: print("ohlcv", done, "/", len(pools), flush=True)
print("DONE", done, flush=True)
