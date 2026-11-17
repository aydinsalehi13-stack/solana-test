# FOMO web: what the desk expects to see

Texts below come from the owner's spec (section 3.3). They are matched exactly. **Status: not yet verified against
the live page.** That needs the owner's login in the desk Chrome. Anything not described here: stop, screenshot to
`logs/fail/`, ask the owner. Verify with `python prove_session.py --mint <mint>` and fix selectors in `browser.py`.

## Page
- Header: search, "Paste", nav Tokens / Perps / Leaderboard / Feed, my cash, "Deposit more" (never clicked), avatar.
- Coin tiles: Market cap, Price, 24H change, 24H Vol., Liquidity, Holders, Top 10 holding.
- Right panel: tabs "Buy" | "Sell", a "$" field with placeholder "Enter amount", presets $10 $100 $500 $1000
  (sell: 10% 25% 50% 100%, Max), a quote line (e.g. `44K paws (-0.61%)`), a fee line (`$0.36 fee`), the primary button.
- "About" has "Contract address" (shortened like `4CxjZy...8spump`), plus "Your positions".
- Window must be at least 1440 px wide, otherwise FOMO shows "fomo on Web is only available on desktop...".

## Steps (PAPER stops at the quote; the primary button and Confirm are never pressed)
| Step | What the desk does | Done when |
|---|---|---|
| S0 | before every order | desk tab on fomo.family, avatar + cash in header, no pop-up, lane free |
| S1 | open `links.coin_url(mint)`; fallback search -> mint -> the Tokens row whose address matches (never Trending) | Contract address matches the mint start and end, Market cap has a number, chart has drawn. A landing/signup page = logged out |
| S2 | Buy tab, `$` field, size with 2 decimals (presets only for exact $10/$100/$500/$1000), wait for "Fetching quote..." to finish, read quote + fee, read the field back (retype once, else "amount mismatch") | quote and fee lines read. PAPER stops here: fill = this quote. Button is "Buy {symbol}" |
| S3 | Sell tab, 50% (or 10/25%), quote. "Minimum amount $2": use 100% if the position is over $2, else "dust" | quote read |
| S4 | Sell 100% | card leaves "Your positions" (LIVE only) |
| S5 | quote > 10 s: reload once, then skip (buys) or repeat (sells). "Price impact too high": buys skip, sells try 50% then the rest. Any box to tick: never ticked, buys skip. "Buy with Apple Pay": no cash, never clicked. Greyed button: abort + screenshot. Unknown pop-up: Escape once, screenshot, ask | |
| S6 | read-only: profile, Transactions, leaderboard via the API (never scrape) | |

## Never clicked (enforced by the deny-regex in `browser.py`, tested)
Withdraw, Deposit, Deposit more, Buy with Apple Pay, Orders, Perps, Send, settings, any export/key screen, the Swap
settings gear, /discover, a Thesis post, any link outside fomo.family, any download, any box to tick, Confirm,
log out / sign out / disconnect / delete.
