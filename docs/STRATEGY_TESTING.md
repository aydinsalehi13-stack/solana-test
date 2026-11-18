# Which strategy is profitable? The tests to run once the laptop is recording

No result here is a promise. Memecoins can go to zero in seconds. Nothing below changes a setting of yours or switches the mode:
it only measures. Every number comes from recorded real data, or the test says "no data".

## What can be tested, and how

| | orion_fast | prompt |
|---|---|---|
| Replay on recorded data | yes (`replay.py`, deterministic) | no: it needs live FOMO quotes and audits |
| Test in PAPER, live, realistic fees | yes | yes (the only way for now) |
| Recorded ticks kept | 7 days (then deleted by `retention`) | n/a |

## Step 0: record real data (laptop, first 3 to 7 days)
1. Install and start the desk (`python setup.py`, `python autostart.py`, then follow its lines). Log in to FOMO yourself.
2. Leave it running. The recorder stores every tick it sees. Check: `python replay.py run` prints "N ticks, D day(s)".
3. For the trade stream add `PUMPPORTAL_API_KEY` to `.env` (you type it). Without it orion pauses entries on a slow feed.
4. Copy `data/desk.db` somewhere safe each night: ticks older than 7 days are deleted, so a month of history needs these copies.

## Step 1: orion_fast on the recorded days
1. `python replay.py sweep`: how much edge disappears at 0 / 2 / 5 / 20 / 40 s of latency. If the edge is gone by 5 s, it cannot be traded from a laptop.
2. `python replay.py walk`: walk-forward (pick on day N, report on day N+1) over the experiment grid: bigger tickets, wider stops with smaller size,
   break-even stop, volatility stops, sell-pressure exit, 2 consecutive bursts, pullback entry, skip the first 60 s.
3. Read the verdict. A candidate may run as a new PAPER strategy (`orion_v2.json`) only if, out of sample: **profit factor above 1.3, at least 50 trades,
   max drawdown under 15%**. Otherwise it says "no edge found" and we keep testing. You approve any switch.
4. Also look at: net after fees, win rate, average win / loss, trades per day, and the share of profit from the top 3 trades (a strategy that lives on 3 lucky trades is not an edge).

## Step 2: prompt in PAPER (cannot be replayed)
1. Run PAPER nights with realistic fees (already in `fees.py`). Each morning: the night report (`reports/`) and `python night_check.py`.
2. Judge on at least 50 closed trades, same bar: profit factor above 1.3, drawdown under 15%. Fewer trades than that is noise.
3. The weekly review suggests at most 3 setting changes with the numbers behind them. Nothing is applied until you approve each one in the app.

## Step 3: compare fairly
Same nights, same fee model, same starting cash ($150 each). Compare net profit after fees, profit factor, max drawdown, and how many trades it took.
Report losses as plainly as wins. If neither passes the bar, the answer is "no strategy yet", not a lowered bar.

## Known reasons to doubt each one (from the cost math, not a backtest)
- orion_fast: a stopped $6 trade loses about 20% of the ticket; it needs about a 12% rise to break even (`docs/orion_cost_math.md`). The old run lost 31.7% over 94 trades.
- prompt: a stop loses about 29% with fees, so it needs roughly 40% winners; that assumes dips on graduated coins really bounce, which is unproven.

## Before any real money
LIVE stays locked until all gates in CLAUDE.md section 0 are met, including a $2 test trade you approve and you typing `LIVE`.
