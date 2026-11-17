These screenshots are produced by `tests/test_dashboard_qa.py` from a SEEDED DEMO database (`tests/seed.py`):
every coin is called `DemoMint*` and every number was inserted by the test. They show how the dashboard looks and behaves.
They are NOT results of the desk, and nothing in them was traded.

The dashboard itself never makes up a number: it shows whatever the desk has stored, and says "no data" otherwise.

- `desktop_1440.png` / `desktop_1440_light.png`: the full page at 1440 px, dark and light.
- `phone_390_*.png`: the four phone tabs at 390 px (Home, Agents, Activity, Account).
- `desktop_coin_sheet.png`: the coin sheet (candles, audit discs, Open on FOMO, two-step Sell now).

Not covered here: opening it on a real iPhone and confirming port 9222 is unreachable from the phone (needs your devices).
