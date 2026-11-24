"""
Save yesterday's (or a given day's) trades for a watchlist, so lookups are instant.

    python nightly.py                     # last trading day, tickers in watchlist.txt
    python nightly.py 2026-11-18          # a specific day
    python nightly.py 2026-11-18 AAPL,TSLA

Skips any ticker/day already saved. UW publishes the full tape after the close;
run this in the evening or the next morning.
"""
import sys
import time
from datetime import date
from pathlib import Path

import uwtape as uw

HERE = Path(__file__).resolve().parent


def main() -> None:
    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else uw.last_trading_days(1)[0]
    if len(sys.argv) > 2:
        tickers = sys.argv[2].split(",")
    else:
        wl = HERE / "watchlist.txt"
        tickers = [t.strip() for t in wl.read_text().split() if t.strip()] if wl.exists() else ["NVDA"]
    todo = [t.upper() for t in tickers if not uw.parquet_path(t, d).exists()]
    if not todo:
        print(f"{d}: already saved for {', '.join(tickers)}")
        return
    t0 = time.time()
    last = [0.0]

    def prog(msg: str, frac: float) -> None:
        if frac - last[0] >= 0.1 or frac >= 1:
            print(f"{msg} {frac:.0%}", flush=True)
            last[0] = frac

    try:
        counts = uw.extract(d, todo, progress=prog)
    except FileNotFoundError as e:
        print(e)
        return
    print(f"{d}: saved {counts} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
