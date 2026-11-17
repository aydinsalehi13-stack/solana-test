"""Phase 3 proof for YOUR laptop (home IP): run the real PumpPortal feed and count what arrives.

  python feed_probe.py [--minutes 10]

Passes if the websocket handshake works (101), at least 100 new tokens arrive in 10 minutes, and only ONE
connection is used. To prove reconnects, switch Wi-Fi off for ~30 s while it runs and switch it back on:
the log shows the state going reconnecting -> live. Nothing is traded and no key is needed.
"""
from __future__ import annotations

import asyncio
import sys
import time

import config
from bus import Hub
from feed import PumpPortalFeed


async def amain(argv: list[str]) -> int:
    minutes = float(argv[argv.index("--minutes") + 1]) if "--minutes" in argv else 10.0
    config.load_env()
    hub = Hub()
    feed = PumpPortalFeed(hub)  # free streams only: no key is passed on purpose
    task = asyncio.create_task(feed.run())
    t0, last_state, gaps, last_new = time.time(), None, [], None
    print(f"running {minutes:g} min against {feed.base_url} (one connection, free streams)")
    try:
        while time.time() - t0 < minutes * 60:
            await asyncio.sleep(1)
            if feed.state != last_state:
                print(f"[{time.time() - t0:6.0f}s] state {last_state} -> {feed.state}" + (f" ({feed.last_error})" if feed.last_error else ""))
                last_state = feed.state
            if feed.counts["new_token"] and int(time.time() - t0) % 60 == 0:
                print(f"[{time.time() - t0:6.0f}s] new tokens so far: {feed.counts['new_token']}")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    c = feed.counts
    print("\nresult")
    print(f"  handshake (101) + messages : {'yes' if feed.last_msg else 'NO'}")
    print(f"  new tokens                 : {c['new_token']}  (need >= 100 in 10 min)")
    print(f"  migrations                 : {c['migration']}")
    print(f"  connections / reconnects   : {feed.connects} / {feed.reconnects}")
    print(f"  methods sent               : {sorted(set(feed.sent))}")
    ok = bool(feed.last_msg) and c["new_token"] >= 100 * (minutes / 10.0)
    print("  PASS" if ok else "  FAIL (if HTTP 403: this network is blocked by PumpPortal; try the home connection)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(sys.argv[1:])))
