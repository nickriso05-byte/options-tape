# Options Tape Lookup

Every individual options print for a ticker, for any trading day since 2022, from Unusual Whales.
Built 2026-11-24 for Brittan's NVDA unusual-options-activity check.

## Run it

Double-click `Options Tape.bat`, or:

    python -m streamlit run app.py --server.port 8551

Then open http://localhost:8551.

The API key is read from `.streamlit/secrets.toml` (`UW_API_TOKEN = "..."`), which git ignores.

## Hosting on Streamlit Community Cloud

1. New app → repo `nickriso05-byte/options-tape`, branch `main`, file `app.py`, Python 3.12.
2. Secrets:

       UW_API_TOKEN = "..."            # Unusual Whales API key
       APP_PASSWORD = "..."            # what Brittan types to get in
       DATA_REPO = "nickriso05-byte/options-tape-data"
       DATA_REPO_TOKEN = "github_pat_..."   # fine-grained token: Contents read/write, ONLY that repo

On Cloud the app streams the 2.8 GB tape instead of saving it, keeping only the ticker's rows.
Every saved day is also committed to the private `options-tape-data` repo (as ~20 MB parts, because
of GitHub's API size limit), so saved days survive app restarts and show up for everyone.

## Where the data comes from

| What | UW endpoint | Notes |
|---|---|---|
| Today's prints | `/api/option-trades?ticker_symbol=X` | Live, paged 500 at a time. UW rejects anything before today. |
| Past days | `/api/option-trades/full-tape/{date}` | One zip of the whole market for that day (~2.8 GB, ~9 GB CSV, ~17M prints). |
| Per-contract daily OI/volume | `/api/option-contract/{osi}/historic` | Used by the "One contract" tab. |

UW's per-contract flow endpoint (`/api/option-contract/{osi}/flow?date=`) **cannot** be used for history.
On 11/24 it returned 0 prints for Fri 11/20 and 43 prints (28.5k contracts) of Thursday's 52.5k for the 11/27 160C.

## How past days work

1. `uwtape.extract(day, [tickers])` downloads the day's market zip to `%USERPROFILE%\uw-tape-cache`
   (outside OneDrive, about 90 s at ~32 MB/s).
2. It scans the CSV for the tickers' rows and saves `data/<TICKER>/<day>.parquet`
   (NVDA: 0.7–1.6M prints, 27–62 MB per day). About 3 to 4 minutes per day in total.
3. The app reads only those parquet files, so a saved day opens instantly.

The last 10 zips are kept (`TAPE_KEEP_ZIP_DAYS`), so pulling another ticker for a recent day skips the download.

## Save days automatically

    python nightly.py                  # last trading day, tickers in watchlist.txt
    python nightly.py 2026-11-18       # a specific day
    python nightly.py 2026-11-18 AAPL,TSLA

## Checked against UW (11/27 160C)

- Fri 11/20: 20,921 contracts, $546,240 premium. Matches UW's daily history exactly.
- The 10:25 Friday 143/149 call spread shows as 5,000 x 5,000 on one timestamp.
- "4pm close" is the last print at or before 4:00 pm. UW's own `last_price` also counts
  after-hours prints to 4:15. For example, the Monday 160C was $8.95 at 4:00 pm vs. UW's $9.10.

## Reading it

- **Side** is where the print hit vs. the NBBO at that moment (Ask/Bid/Mid). For spread legs,
  the buyer/seller is an inference.
- **Spread legs** are flagged by OPRA multi-leg condition codes (mlet, mlat, mesl, tlet, ...).
  The Spreads tab rebuilds orders by matching legs with the exact same timestamp.
- **OI opened that day** moves UW's morning OI change back to the session it came from.
  For example, the +14.9k on Friday's row opened on Thursday.
