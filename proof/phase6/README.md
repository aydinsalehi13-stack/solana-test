No pictures are kept here.

The dashboard tests (`tests/test_dashboard_qa.py`) write screenshots into this folder when they run. Those tests fill a throwaway
database with made-up rows (`tests/seed.py`) only to check that every screen draws correctly, so the pictures are not shipped:
they are ignored by git, and `tests/test_no_fake.py` fails if one is ever committed.

The app itself has no demo mode and holds no demo data. Every number it shows comes from the running desk, or it says "no data".
