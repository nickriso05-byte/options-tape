"""
Unusual Whales options tape: getting the trades.

Two sources, because UW serves today and past days differently:

* Today:     GET /api/option-trades?ticker_symbol=X, paged with older_than.
             UW rejects cursors before the latest trading day (HTTP 400).
* Past days: GET /api/option-trades/full-tape/{date}, which is one zip of every
             option print in the market that day (~2.8 GB zipped, ~9 GB of CSV).
             We download it once, keep only the tickers asked for, and save them
             as a small parquet file under data/<TICKER>/<date>.parquet. After
             that a lookup reads the parquet and is instant.

Per-contract daily history (OI, volume, bid/ask split) comes from
GET /api/option-contract/{osi}/historic.

The zips are kept in ZIP_DIR (outside OneDrive) for KEEP_ZIP_DAYS so another
ticker can be pulled from a day without downloading it again.
"""

from __future__ import annotations

import os
import re
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
import requests

API = "https://api.unusualwhales.com"
HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TAPE_DATA_DIR", HERE / "data"))
ZIP_DIR = Path(os.environ.get("TAPE_ZIP_DIR", Path.home() / "uw-tape-cache"))
KEEP_ZIP_DAYS = int(os.environ.get("TAPE_KEEP_ZIP_DAYS", "10"))

# Columns kept from the full tape (it has ~50). Everything the app shows or filters on.
KEEP = [
    "executed_at", "underlying_symbol", "option_chain_id", "expiry", "option_type", "strike",
    "size", "price", "premium", "nbbo_bid", "nbbo_ask", "underlying_price", "tags",
    "report_flags", "upstream_condition_detail", "open_interest", "volume",
    "implied_volatility", "delta", "exchange", "canceled", "aggregated_trade_id",
    "trading_hours", "id",
]

Progress = Callable[[str, float], None]


# --------------------------------------------------------------------------- auth

def token() -> str:
    for name in ("UW_API_TOKEN", "UW_API_KEY"):
        if os.environ.get(name):
            return os.environ[name].strip()
    try:
        import streamlit as st
        for name in ("UW_API_TOKEN", "UW_API_KEY"):
            v = st.secrets.get(name)
            if v:
                return str(v).strip()
    except Exception:  # noqa: BLE001 - no streamlit / no secrets file
        pass
    sec = HERE / ".streamlit" / "secrets.toml"
    if sec.exists():
        m = re.search(r'UW_API_(?:TOKEN|KEY)\s*=\s*"?([^"\s]+)', sec.read_text())
        if m:
            return m.group(1)
    raise RuntimeError("No Unusual Whales API key. Set UW_API_TOKEN (env or .streamlit/secrets.toml).")


_S: requests.Session | None = None


def session() -> requests.Session:
    global _S
    if _S is None:
        _S = requests.Session()
        _S.headers.update({"Authorization": f"Bearer {token()}", "Accept": "application/json"})
    return _S


def _get(path: str, **params) -> dict:
    for attempt in range(4):
        r = session().get(API + path, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(2 + attempt * 3)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


# --------------------------------------------------------------------------- dates

def last_trading_days(n: int, before: date | None = None) -> list[date]:
    """Weekdays only; holidays come back as 'no file' from UW and are skipped there."""
    d = before or date.today()
    out = []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def saved_days(ticker: str) -> list[date]:
    """Days saved locally plus days in the shared store (downloaded on first use)."""
    folder = DATA_DIR / ticker.upper()
    found = {date.fromisoformat(p.stem) for p in folder.glob("*.parquet")} if folder.exists() else set()
    try:
        import store
        found |= set(store.list_days(ticker))
    except Exception:  # noqa: BLE001 - store not configured / offline
        pass
    return sorted(found)


def ensure_local(ticker: str, d: date) -> Path:
    p = parquet_path(ticker, d)
    if not p.exists():
        import store
        store.pull(ticker, d, p)
    return p


def parquet_path(ticker: str, d: date) -> Path:
    return DATA_DIR / ticker.upper() / f"{d.isoformat()}.parquet"


# --------------------------------------------------------------------------- full tape

def zip_path(d: date) -> Path:
    return ZIP_DIR / f"{d.isoformat()}.zip"


def _tape_blocks(d: date, progress: Progress | None, what: str):
    """Yield the day's CSV as decompressed byte blocks.

    Uses a zip already in ZIP_DIR if there is one. Otherwise it streams the zip from UW and
    inflates it on the fly, so the 2.8 GB file never touches disk (needed on Streamlit Cloud).
    The zip holds one deflated CSV with its sizes in the local header (no data descriptor).
    """
    zp = zip_path(d)
    if zp.exists():
        z = zipfile.ZipFile(zp)
        info = z.infolist()[0]
        done = 0
        with z.open(info) as f:
            while True:
                blk = f.read(32 << 20)
                if not blk:
                    return
                done += len(blk)
                if progress:
                    progress(what, min(done / info.file_size, 1.0))
                yield blk
    import struct
    import zlib
    t0 = time.time()
    with session().get(f"{API}/api/option-trades/full-tape/{d.isoformat()}", stream=True, timeout=120) as r:
        if r.status_code in (403, 404, 422):
            raise FileNotFoundError(f"UW has no tape for {d:%b %d, %Y} (market holiday, not published yet, "
                                    f"or outside the plan's history). UW said: {r.text[:200]}")
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        it = r.iter_content(1 << 20)
        buf = b""
        while len(buf) < 30:
            buf += next(it)
        sig, _ver, _flags, method, _t, _d, _crc, _cs, _us, nlen, xlen = struct.unpack("<IHHHHHIIIHH", buf[:30])
        if sig != 0x04034B50 or method != 8:
            raise RuntimeError(f"Unexpected tape format (signature {sig:#x}, method {method}).")
        hdr = 30 + nlen + xlen
        while len(buf) < hdr:
            buf += next(it)
        inflate = zlib.decompressobj(-15)
        got = len(buf)
        pending = [buf[hdr:]]
        size = len(pending[0])
        for chunk in it:
            got += len(chunk)
            pending.append(chunk)
            size += len(chunk)
            if size >= (4 << 20):
                out = inflate.decompress(b"".join(pending))
                pending, size = [], 0
                if progress and total:
                    mbps = got / 1e6 / max(time.time() - t0, 0.1)
                    progress(f"{what} · {got/1e9:.1f} of {total/1e9:.1f} GB · {mbps:.0f} MB/s", got / total)
                if out:
                    yield out
                if inflate.eof:
                    break
        if pending and not inflate.eof:
            out = inflate.decompress(b"".join(pending))
            if out:
                yield out
        tail = inflate.flush()
        if tail:
            yield tail


def _lines_for(block: bytes, end: int, needle: bytes, sink) -> int:
    """Write every full CSV line in block[:end] whose 2nd field is the ticker. Returns how many."""
    n = 0
    i = block.find(needle, 0, end)
    while i != -1:
        s = block.rfind(b"\n", 0, i) + 1
        e = block.find(b"\n", i, end)
        # needle is ",TICKER," so it must sit right after the first field (the trade id)
        if block.find(b",", s, e) == i:
            sink.write(block[s:e + 1])
            n += 1
        i = block.find(needle, e, end)
    return n


def extract(d: date, tickers: Iterable[str], progress: Progress | None = None) -> dict[str, int]:
    """Pull the given tickers out of one day's market tape and save one parquet per ticker.

    Memory stays small: matching lines go to a temp CSV, which is then converted in chunks.
    Each saved file is also pushed to the shared store (store.py) when one is configured.
    """
    import shutil
    import tempfile

    import pyarrow as pa
    import pyarrow.parquet as pq

    tickers = [t.upper().strip() for t in tickers if t.strip()]
    needles = {t: f",{t},".encode() for t in tickers}
    tmpdir = Path(tempfile.mkdtemp(prefix="tape_"))
    sinks = {t: open(tmpdir / f"{t}.csv", "wb") for t in tickers}
    what = f"Reading the {d:%b %d} market tape for {', '.join(tickers)}"
    header = None
    carry = b""
    try:
        try:
            for blk in _tape_blocks(d, progress, what):
                blk = carry + blk
                if header is None:
                    nl = blk.find(b"\n")
                    header = blk[:nl + 1]
                    for f in sinks.values():
                        f.write(header)
                    blk = blk[nl + 1:]
                cut = blk.rfind(b"\n") + 1
                carry = blk[cut:]
                for t in tickers:
                    _lines_for(blk, cut, needles[t], sinks[t])
            if carry.strip():
                for t in tickers:
                    _lines_for(carry + b"\n", len(carry) + 1, needles[t], sinks[t])
        finally:
            for f in sinks.values():
                f.close()

        counts = {}
        for t in tickers:
            if progress:
                progress(f"Saving {t} for {d:%b %d}", 1.0)
            p = parquet_path(t, d)
            p.parent.mkdir(parents=True, exist_ok=True)
            part = p.with_suffix(".part")
            writer, schema, n = None, None, 0
            for chunk in pd.read_csv(tmpdir / f"{t}.csv", usecols=lambda c: c in KEEP, chunksize=250_000,
                                     dtype={"aggregated_trade_id": "string", "id": "string"}, low_memory=False):
                df = tidy(chunk, categories=False)
                table = pa.Table.from_pandas(df, preserve_index=False)
                if writer is None:
                    schema = table.schema
                    writer = pq.ParquetWriter(part, schema, compression="zstd")
                writer.write_table(table.cast(schema))
                n += len(df)
            if writer is None:  # the ticker had no prints that day
                pd.DataFrame(columns=KEEP).to_parquet(part, index=False)
            else:
                writer.close()
            os.replace(part, p)
            counts[t] = n
            try:
                import store
                store.push(p, t, d)
            except Exception as e:  # noqa: BLE001 - the shared copy is best-effort
                print(f"store.push failed for {t} {d}: {e}")
        return counts
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def prune_zips() -> None:
    zips = sorted(ZIP_DIR.glob("*.zip"))
    for z in zips[:-KEEP_ZIP_DAYS] if KEEP_ZIP_DAYS > 0 else zips:
        try:
            z.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- today (REST)

def live_today(ticker: str, progress: Progress | None = None, max_pages: int = 400) -> pd.DataFrame:
    """Every print so far today for one ticker, newest first, paged 500 at a time."""
    rows: list[dict] = []
    cursor = None
    for page in range(max_pages):
        params = {"ticker_symbol": ticker.upper(), "limit": 500}
        if cursor:
            params["older_than"] = cursor
        data = _get("/api/option-trades", **params).get("data", [])
        if not data:
            break
        rows.extend(data)
        oldest = data[-1]["executed_at"]
        if progress:
            progress(f"Loading today's {ticker.upper()} prints: {len(rows):,} so far (back to {to_et(oldest):%H:%M})", min(page / 60, 0.99))
        if len(data) < 500 or oldest == cursor:
            break
        cursor = oldest
    if not rows:
        return pd.DataFrame(columns=KEEP)
    df = pd.DataFrame(rows)
    df = df[[c for c in KEEP if c in df.columns]].drop_duplicates("id")
    for c in ("tags", "report_flags"):
        if c in df:
            df[c] = df[c].apply(lambda v: "{" + ",".join(v) + "}" if isinstance(v, list) else v)
    return tidy(df)


# --------------------------------------------------------------------------- contract history

def contract_history(osi: str) -> pd.DataFrame:
    """Daily volume / bid-ask split / OHLC / OI for one contract.

    UW stamps OI on the morning it's published, so the change on row D is what
    opened during the PREVIOUS session. 'oi_opened_that_day' moves it back to the
    session it actually came from.
    """
    j = _get(f"/api/option-contract/{osi}/historic")
    df = pd.DataFrame(j.get("chains", []))
    if df.empty:
        return df
    num = ["open_interest", "volume", "ask_volume", "bid_volume", "mid_volume", "open_price", "high_price",
           "low_price", "last_price", "avg_price", "total_premium", "multi_leg_volume", "sweep_volume",
           "floor_volume", "cross_volume", "trades", "implied_volatility"]
    for c in num:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.sort_values("date").reset_index(drop=True)
    df["oi_change_reported"] = df["open_interest"].diff()
    df["oi_opened_that_day"] = df["oi_change_reported"].shift(-1)
    return df.sort_values("date", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- shaping

ET = "America/New_York"


def to_et(ts) -> datetime:
    return pd.Timestamp(ts).tz_convert(ET) if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts).tz_localize("UTC").tz_convert(ET)


def _side(tags: str) -> str:
    t = str(tags or "")
    for s, label in (("ask_side", "Ask"), ("bid_side", "Bid"), ("mid_side", "Mid"), ("no_side", "No side")):
        if s in t:
            return label
    return ""


MULTI_CODES = ("mlet", "mlat", "mlct", "mlft", "mesl", "masl", "mfsl", "tlet", "tlct", "tlft", "tesl", "tasl", "tfsl", "tlat", "cbmo")


def tidy(df: pd.DataFrame, categories: bool = True) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["executed_at"] = pd.to_datetime(df["executed_at"], utc=True, format="mixed").dt.tz_convert(ET)
    for c in ("size", "price", "premium", "nbbo_bid", "nbbo_ask", "underlying_price", "strike", "open_interest",
              "volume", "implied_volatility", "delta"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df["option_type"] = df["option_type"].str.lower().map({"call": "Call", "put": "Put"}).fillna(df["option_type"])
    df["side"] = df["tags"].map(_side)
    cond = df["upstream_condition_detail"].fillna("").astype(str).str.lower()
    df["multi_leg"] = cond.isin(MULTI_CODES)
    flags = df["report_flags"].fillna("").astype(str)
    df["sweep"] = flags.str.contains("sweep")
    df["floor"] = flags.str.contains("floor")
    df["cross"] = flags.str.contains("cross")
    df["extended_hours"] = flags.str.contains("extended_hours")
    if "canceled" in df:
        df["canceled"] = df["canceled"].astype(str).str.lower().isin(["t", "true", "1"])
    return slim(df.sort_values("executed_at").reset_index(drop=True), categories)


DROP_AFTER_TIDY = ["tags", "report_flags", "id", "aggregated_trade_id", "trading_hours"]


def slim(df: pd.DataFrame, categories: bool = True) -> pd.DataFrame:
    """Drop the raw string columns once side/flags are derived; ~80 MB -> ~20 MB per NVDA day."""
    df = df.drop(columns=[c for c in DROP_AFTER_TIDY if c in df.columns])
    for c in ("option_chain_id", "side", "exchange", "upstream_condition_detail", "option_type", "underlying_symbol"):
        if c in df and categories:
            df[c] = df[c].astype("category")
    return df


def load(ticker: str, days: Iterable[date]) -> pd.DataFrame:
    parts = [pd.read_parquet(ensure_local(ticker, d)) for d in days]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    return df


if __name__ == "__main__":
    import sys
    d = date.fromisoformat(sys.argv[1])
    tks = sys.argv[2].split(",") if len(sys.argv) > 2 else ["NVDA"]
    t0 = time.time()
    print(extract(d, tks, progress=lambda m, f: print(f"\r{m} {f:5.0%}", end="", flush=True)))
    print(f"\n{time.time() - t0:.0f}s")
