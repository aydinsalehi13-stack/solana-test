# Market backtests (research only: nothing here trades, clicks or changes a setting)

Real Solana memecoin 5-minute candles from GeckoTerminal (public API, ~30 calls a minute), then simple backtests.

    python tools/market_backtest/fetch.py  <folder>                    # download the last month of candles (about 2 hours, rate limited)
    python tools/market_backtest/backtest.py <folder> results.json     # 17,280 entry x exit x size combinations, first half picks, second half judges
    python tools/market_backtest/prompt_bt.py <folder>                 # the price half of the 'prompt' strategy with its own default settings

Read every result as an UPPER bound: the pools come from lists of coins that are active now (survivorship), 5-minute candles hide wicks,
the Analyst's 8 layers cannot be replayed (no historical data), and the samples are small. A backtest is not proof: paper trading is.
