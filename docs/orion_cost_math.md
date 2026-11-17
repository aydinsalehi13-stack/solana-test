# orion_fast: what a trade costs before it can win

Computed by the engine itself with the spec's fee model (`fees.py`: FOMO tiers + pump fee 1.25% + slippage 0.5% +
impact 0.5% per side on curve fills, plus half the measured Jupiter round-trip loss on each leg). Not a backtest.

| ticket | round trip, Jupiter extra 0% | round trip, Jupiter round trip 3% | price must rise to break even (3%) |
|---|---|---|---|
| $6 (4% of $150) | 8.5% | 11.5% | ~12.2% |
| $15 | 8.5% | 11.5% | ~12.2% |
| $50 | 8.3% | 11.3% | ~12.0% |
| $100 | 6.4% | 9.4% | ~9.9% |

* A stopped-out $6 trade (stop -8%, gapped a little lower) loses **about 20% of the ticket** ($1.22), of which $0.64 is fees.
* TP1 at +15% barely clears the break-even rise. The -8% stop is smaller than the cost of the round trip itself.
* This is the same shape as the box result (62 of 94 exits at the stop, -31.7%). It is structural, not bad luck.
* Bigger tickets only help above ~$100, because the FOMO fee flattens ($0.95) and then drops to 0.45%.

Caveat: the Jupiter round-trip loss already contains pool fees and price impact, and the curve model adds its own pump
fee, slippage and impact. The two may partly double count. The model errs on the side of making paper look worse than
live, as required, but it means these numbers are an upper bound until compared with real fills.

Nothing here says the strategy cannot work: it says an entry needs an expected move well above ~12% to be worth taking,
and that tuning stops by feel is pointless. `python replay.py walk` tests the alternatives out of sample.
