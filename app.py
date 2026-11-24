"""
Options Tape Lookup: every individual options print for a ticker, any day.

Run:  streamlit run app.py
Data: Unusual Whales full tape (past days) + live trades endpoint (today). See uwtape.py / store.py.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

import gate
import uwtape as uw

st.set_page_config(page_title="Options Tape Lookup", layout="wide", page_icon="📈")

st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1440px;}
  h1, h2, h3 {letter-spacing: -0.02em;}
  .hero {display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-bottom: 2px;}
  .hero .tk {font-size: 2.3rem; font-weight: 700; letter-spacing: -0.03em; color:#111827;}
  .hero .sub {font-size: 1.05rem; color:#6b7280; font-weight: 500;}
  .chips {display:flex; gap:6px; flex-wrap:wrap; margin: 6px 0 18px;}
  .chip {background:#e9effd; color:#2549a8; border-radius: 999px; padding: 3px 11px; font-size: .82rem; font-weight: 600;}
  .chip.live {background:#e6f6ee; color:#0f7a4d;}
  .kpis {display:grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px;}
  .kpi {background:#fff; border:1px solid #e6e9ef; border-radius: 12px; padding: 14px 16px;}
  .kpi .l {font-size: .78rem; color:#6b7280; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;}
  .kpi .v {font-size: 1.45rem; font-weight: 700; color:#111827; margin-top: 4px; letter-spacing: -0.02em;}
  .kpi .s {font-size: .8rem; color:#9aa1ad; margin-top: 2px;}
  .bar {display:flex; height:6px; border-radius:6px; overflow:hidden; margin-top:9px; background:#eef0f3;}
  .bar .a {background:#16a34a;} .bar .b {background:#dc2626;} .bar .m {background:#cbd2dc;}
  @media (max-width: 900px) {.kpis {grid-template-columns: repeat(2, minmax(0, 1fr));}}
  .help {color:#6b7280; font-size:.88rem; line-height:1.5; margin: 2px 0 12px;}
  .foot {color:#9aa1ad; font-size:.8rem; margin-top: 28px;}
  [data-testid="stTabs"] button p {font-size: .98rem; font-weight: 600;}
  [data-testid="stSidebar"] h3 {font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; color:#6b7280; margin: 14px 0 2px;}
  .gate-note {text-align:center; color:#6b7280; margin-top: 10px;}
  .gate-note.bad {color:#dc2626;}
</style>""", unsafe_allow_html=True)

gate.require()

COLS = ["executed_at", "option_chain_id", "expiry", "option_type", "strike", "size", "price", "premium",
        "nbbo_bid", "nbbo_ask", "underlying_price", "side", "multi_leg", "sweep", "floor", "cross",
        "extended_hours", "open_interest", "volume", "implied_volatility", "canceled"]

N = st.column_config.NumberColumn
INT, USD, USD0 = N(format="%,d"), N(format="$%.2f"), N(format="$%,.0f")


# --------------------------------------------------------------------------- data

def short_label(expiry, strike, typ) -> str:
    e = pd.Timestamp(expiry)
    return f"{e:%b} {e.day} ${strike:g} {'call' if str(typ).startswith('C') else 'put'}"


def labels(df: pd.DataFrame) -> pd.Series:
    """'Nov 27 $160 call' for every row: built once per distinct contract, then mapped (a day of NVDA is >1M rows)."""
    if df.empty:
        return pd.Series([], dtype="category", index=df.index)
    u = df.drop_duplicates("option_chain_id")[["option_chain_id", "expiry", "strike", "option_type"]]
    m = {str(o): short_label(e, s, t) for o, e, s, t in u.itertuples(index=False)}
    ids = df["option_chain_id"]
    ids = ids if isinstance(ids.dtype, pd.CategoricalDtype) else ids.astype(str).astype("category")
    if len(set(m.values())) == len(m):
        return ids.cat.rename_categories(lambda c: m.get(str(c), str(c)))
    return ids.astype(str).map(m).astype("category")


def prep(df: pd.DataFrame, d: date) -> pd.DataFrame:
    """Columns the app needs + minute-of-day, which is slow to build on every rerun."""
    df = df[[c for c in COLS if c in df.columns]].copy()
    if "canceled" in df:
        df = df[~df["canceled"].fillna(False).astype(bool)].drop(columns="canceled")
    df["day"] = d
    df["mins"] = (df["executed_at"].dt.hour * 60 + df["executed_at"].dt.minute).astype("int16")
    for c in ("nbbo_bid", "nbbo_ask", "underlying_price", "implied_volatility", "price", "strike"):
        if c in df:
            df[c] = df[c].astype("float32")
    return df


def finish(df: pd.DataFrame) -> pd.DataFrame:
    for c in ("option_chain_id", "side", "option_type"):
        if c in df:
            df[c] = df[c].astype(str).astype("category")
    for c in ("day", "expiry"):
        df[c] = df[c].astype("category")
    for c in ("open_interest", "volume", "size"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int32")
    df["contract"] = labels(df)
    return df


# cache_resource: the frame is shared, never copied (a few days of NVDA is millions of rows). Never mutate it.
@st.cache_resource(show_spinner=False, max_entries=3)
def load_days(ticker: str, days: tuple[date, ...]) -> pd.DataFrame:
    parts = [prep(pd.read_parquet(uw.ensure_local(ticker, d)), d) for d in days]
    return finish(pd.concat(parts, ignore_index=True))


@st.cache_resource(show_spinner=False, ttl=300, max_entries=2)
def load_today(ticker: str) -> pd.DataFrame:
    bar = st.progress(0.0, text="Loading today's trades…")
    df = uw.live_today(ticker, progress=lambda m, f: bar.progress(f, text=m))
    bar.empty()
    return finish(prep(df, date.today()))


@st.cache_data(show_spinner=False, ttl=900)
def history(osi: str) -> pd.DataFrame:
    return uw.contract_history(osi)


@st.cache_data(show_spinner=False, ttl=120)
def saved(ticker: str) -> list[date]:
    return uw.saved_days(ticker)


def money(x: float) -> str:
    x = float(x or 0)
    if abs(x) >= 1e9: return f"${x/1e9:.2f}B"
    if abs(x) >= 1e6: return f"${x/1e6:.1f}M"
    if abs(x) >= 1e3: return f"${x/1e3:.0f}K"
    return f"${x:,.0f}"


def big(x: float) -> str:
    x = float(x or 0)
    if abs(x) >= 1e6: return f"{x/1e6:.2f}M"
    if abs(x) >= 1e4: return f"{x/1e3:.0f}K"
    return f"{x:,.0f}"


def side_colors(s: pd.Series) -> list[str]:
    return ["color:#15803d;font-weight:600" if v == "Ask" else "color:#dc2626;font-weight:600" if v == "Bid"
            else "color:#6b7280" for v in s]


def show_table(df: pd.DataFrame, cfg: dict, height: int = 560, color_side: bool = True):
    data = df.style.apply(side_colors, subset=["Side"]) if color_side and "Side" in df and len(df) <= 3000 else df
    st.dataframe(data, hide_index=True, width="stretch", height=height, column_config=cfg)


# --------------------------------------------------------------------------- sidebar: what to look at

with st.sidebar:
    st.markdown("### Ticker")
    ticker = st.text_input("Ticker", "NVDA", label_visibility="collapsed").upper().strip()
    have = saved(ticker)

    st.markdown("### Days")
    day_opts = sorted(have, reverse=True)
    days = st.multiselect("Days", day_opts, default=day_opts[:min(3, len(day_opts))],
                          format_func=lambda d: f"{d:%a, %b} {d.day}", label_visibility="collapsed",
                          placeholder="Pick saved days")
    with_today = st.toggle("Include today (live)", value=False,
                           help="Today's trades come straight from the Unusual Whales API. Big tickers take a minute.")

    with st.expander("Add a day", expanded=not have):
        st.markdown("<div class='help'>Past days come from the whole market's trade file for that day. "
                    "The first time takes about 4–6 minutes. After that, the day opens instantly for everyone.</div>",
                    unsafe_allow_html=True)
        new_day = st.date_input("Day", uw.last_trading_days(1)[0], max_value=date.today() - timedelta(days=1),
                                min_value=date(2022, 1, 3), format="MM/DD/YYYY")
        if st.button(f"Get {ticker} for {new_day:%b} {new_day.day}", type="primary", width="stretch"):
            if new_day.weekday() >= 5:
                st.error("That's a weekend.")
            elif new_day in have:
                st.info("Already saved. Pick it under Days.")
            else:
                bar = st.progress(0.0, text="Starting…")
                try:
                    n = uw.extract(new_day, [ticker], progress=lambda m, f: bar.progress(min(f, 1.0), text=m))
                    saved.clear()
                    st.success(f"Saved {n[ticker]:,} {ticker} trades for {new_day:%b} {new_day.day}.")
                    st.rerun()
                except FileNotFoundError as e:
                    st.error(str(e))
                except Exception as e:  # noqa: BLE001
                    st.error(f"Couldn't load that day: {e}")

if not have and not with_today:
    st.markdown(f"<div class='hero'><span class='tk'>{ticker}</span><span class='sub'>options tape</span></div>",
                unsafe_allow_html=True)
    st.info(f"No {ticker} days saved yet. Use **Add a day** in the sidebar, or turn on **Include today**.")
    st.stop()
if not days and not with_today:
    st.info("Pick at least one day in the sidebar.")
    st.stop()

with st.spinner("Reading the tape…"):
    frames = [load_days(ticker, tuple(sorted(days)))] if days else []
    if with_today:
        frames.append(load_today(ticker))
    raw = frames[0] if len(frames) == 1 else finish(pd.concat(frames, ignore_index=True))

if raw.empty:
    st.info("No trades on the days picked.")
    st.stop()

# --------------------------------------------------------------------------- sidebar: filters

with st.sidebar:
    st.markdown("### Contracts")
    typ = st.segmented_control("Calls or puts", ["Calls", "Puts", "Both"], default="Calls",
                               label_visibility="collapsed") or "Both"
    f = raw if typ == "Both" else raw[raw["option_type"] == typ[:-1]]

    exps = sorted(f["expiry"].unique())
    picked = days + ([date.today()] if with_today else [])
    latest, earliest = max(picked), min(picked)
    near = [e for e in exps if earliest <= e <= latest + timedelta(days=10)] or \
           [e for e in exps if e <= latest + timedelta(days=10)][-3:]
    pick_exp = st.multiselect("Expiration dates", exps, default=near or exps[:3],
                              format_func=lambda e: f"{e:%b} {e.day}, {e:%Y}", placeholder="All expirations")
    if pick_exp:
        f = f[f["expiry"].isin(pick_exp)]

    if not f.empty:
        smin, smax = float(f["strike"].min()), float(f["strike"].max())
        spot = float(raw["underlying_price"].median())
        lo, hi = max(smin, float(round(spot * 0.8))), min(smax, float(round(spot * 1.25)))
        strikes = st.slider("Strike prices", smin, smax, (min(lo, hi), max(lo, hi)), step=0.5, format="$%.0f")
        f = f[f["strike"].between(*strikes)]

    st.markdown("### Trade size")
    c1, c2 = st.columns(2)
    min_size = c1.number_input("Min contracts", 0, 100000, 0, step=50)
    min_prem = c2.number_input("Min premium ($)", 0, 100_000_000, 0, step=10_000)
    f = f[(f["size"] >= min_size) & (f["premium"] >= min_prem)]

    with st.expander("More filters"):
        sides = st.pills("Traded at", ["Ask", "Bid", "Mid", "No side"], default=["Ask", "Bid", "Mid", "No side"],
                         selection_mode="multi", help="Ask = paid the offer (usually a buyer). Bid = hit the bid (usually a seller).")
        f = f[f["side"].isin(sides or ["Ask", "Bid", "Mid", "No side"])]
        kind = st.segmented_control("Trade type", ["All", "Single", "Spread legs"], default="All") or "All"
        if kind == "Single":
            f = f[~f["multi_leg"]]
        elif kind == "Spread legs":
            f = f[f["multi_leg"]]
        if st.toggle("Sweeps only"):
            f = f[f["sweep"]]
        if not st.toggle("Include pre- and after-market", value=False):
            f = f[~f["extended_hours"]]
        t_from, t_to = st.slider("Time of day (ET)", value=(pd.Timestamp("09:30").time(), pd.Timestamp("16:15").time()),
                                 min_value=pd.Timestamp("07:00").time(), max_value=pd.Timestamp("18:00").time(),
                                 step=timedelta(minutes=5), format="h:mm a")
        f = f[f["mins"].between(t_from.hour * 60 + t_from.minute, t_to.hour * 60 + t_to.minute)]

# --------------------------------------------------------------------------- header

chips = "".join(f"<span class='chip'>{d:%a, %b} {d.day}</span>" for d in sorted(days))
chips += "<span class='chip live'>Today · live</span>" if with_today else ""
st.markdown(f"<div class='hero'><span class='tk'>{ticker}</span><span class='sub'>options tape · every trade</span></div>"
            f"<div class='chips'>{chips}</div>", unsafe_allow_html=True)

tot = float(f["size"].sum()) or 1.0
ask_c, bid_c = float(f.loc[f.side == "Ask", "size"].sum()), float(f.loc[f.side == "Bid", "size"].sum())
mid_c = tot - ask_c - bid_c
st.markdown(f"""
<div class='kpis'>
  <div class='kpi'><div class='l'>Trades</div><div class='v'>{len(f):,}</div><div class='s'>matching your filters</div></div>
  <div class='kpi'><div class='l'>Contracts</div><div class='v'>{big(f['size'].sum())}</div><div class='s'>{int(f['size'].sum()):,} total</div></div>
  <div class='kpi'><div class='l'>Premium</div><div class='v'>{money(f['premium'].sum())}</div><div class='s'>dollars that changed hands</div></div>
  <div class='kpi'><div class='l'>Ask vs. bid</div><div class='v'>{ask_c/tot:.0%} · {bid_c/tot:.0%}</div>
    <div class='bar'><div class='a' style='width:{ask_c/tot*100:.1f}%'></div><div class='m' style='width:{mid_c/tot*100:.1f}%'></div><div class='b' style='width:{bid_c/tot*100:.1f}%'></div></div></div>
  <div class='kpi'><div class='l'>Part of a spread</div><div class='v'>{f.loc[f.multi_leg, 'size'].sum()/tot:.0%}</div><div class='s'>of contracts</div></div>
</div>""", unsafe_allow_html=True)

tab_trades, tab_daily, tab_spreads, tab_contract = st.tabs(["Trades", "Daily totals", "Spreads", "Contract history"])

# --------------------------------------------------------------------------- trades

with tab_trades:
    later = sorted(set(days) | ({date.today()} if with_today else set()))
    a, b, c = st.columns([1.3, 1.3, 1])
    sort = a.segmented_control("Sort", ["Biggest", "Most premium", "Time"], default="Biggest") or "Biggest"
    mark_opts = ["Off"] + [f"{d:%a, %b} {d.day}" for d in later]
    mark = b.selectbox("What was it worth at the close of…", mark_opts,
                       help="Each trade's contract at its last trade by 4:00 pm ET that day (UW's own 'close' also "
                            "counts after-hours trades to 4:15). Blank means the contract had already expired.")
    more = c.toggle("Show all columns", value=False)

    view = {"Most premium": f.nlargest(5000, "premium"), "Biggest": f.nlargest(5000, "size")}.get(sort)
    if view is None:
        view = f.sort_values("executed_at")
    out = pd.DataFrame({
        "Time (ET)": view["executed_at"].dt.tz_localize(None),
        "Contract": view["contract"].astype(str),
        "Side": view["side"].astype(str),
        "Contracts": view["size"],
        "Price": view["price"],
        "Premium": view["premium"],
        "Stock": view["underlying_price"],
        "Type": (view["multi_leg"].map({True: "Spread leg", False: "Single"})
                 + view["sweep"].map({True: " · sweep", False: ""})
                 + view["floor"].map({True: " · floor", False: ""})),
    })
    if more:
        out["Bid"] = view["nbbo_bid"].values
        out["Ask"] = view["nbbo_ask"].values
        out["Open interest"] = view["open_interest"].values
        out["Volume so far"] = view["volume"].values
        out["IV"] = (view["implied_volatility"] * 100).values
        out["Symbol"] = view["option_chain_id"].astype(str).values
    if mark != "Off":
        md = later[mark_opts.index(mark) - 1]
        md_rows = raw[(raw["day"] == md) & ~raw["extended_hours"]].sort_values("executed_at")
        close = md_rows.groupby("option_chain_id", observed=True)["price"].last()
        close.index = close.index.astype(str)
        col = f"Price {md:%b} {md.day}"
        out[col] = view["option_chain_id"].astype(str).map(close).values
        out["Return"] = out[col] / out["Price"]
        out["Worth then"] = out[col] * out["Contracts"] * 100
    cfg = {"Time (ET)": st.column_config.DatetimeColumn(format="ddd MMM D, h:mm:ss a"),
           "Price": USD, "Premium": USD0, "Stock": USD, "Contracts": INT, "Bid": USD, "Ask": USD,
           "Open interest": INT, "Volume so far": INT, "IV": N(format="%.0f%%"),
           "Return": N(format="%.1fx"), "Worth then": USD0}
    for k in out.columns:
        if k.startswith("Price ") and k != "Price":
            cfg[k] = USD
    shown = out.head(3000)
    if len(f) > 3000:
        note = f"Showing 3,000 of {len(f):,} trades. The CSV has all of them."
    else:
        note = f"{len(f):,} trades."
    st.markdown(f"<div class='help'>{note} <b style='color:#15803d'>Ask</b> = paid the offer, "
                f"<b style='color:#dc2626'>Bid</b> = hit the bid.</div>", unsafe_allow_html=True)
    show_table(shown, cfg)

    def all_trades_csv() -> bytes:
        full = f.sort_values("executed_at")
        return pd.DataFrame({
            "time_et": full["executed_at"].dt.tz_localize(None), "contract": full["contract"].astype(str),
            "symbol": full["option_chain_id"].astype(str), "side": full["side"].astype(str), "contracts": full["size"],
            "price": full["price"], "premium": full["premium"], "bid": full["nbbo_bid"], "ask": full["nbbo_ask"],
            "stock": full["underlying_price"], "spread_leg": full["multi_leg"], "sweep": full["sweep"],
            "floor": full["floor"], "open_interest": full["open_interest"], "volume_so_far": full["volume"],
            "iv": full["implied_volatility"]}).to_csv(index=False).encode()

    st.download_button("Download all matching trades (CSV)", all_trades_csv, on_click="ignore",
                       file_name=f"{ticker}_trades.csv", mime="text/csv", icon=":material/download:")

# --------------------------------------------------------------------------- daily totals

with tab_daily:
    st.markdown("<div class='help'>Each contract's totals per day, for the trades that match your filters. "
                "For open interest, open the contract in <b>Contract history</b>.</div>", unsafe_allow_html=True)
    g = f.assign(ask=f["size"].where(f.side == "Ask", 0), bid=f["size"].where(f.side == "Bid", 0),
                 ml=f["size"].where(f.multi_leg, 0), px_x=f["price"] * f["size"])
    agg = g.groupby(["day", "contract"], observed=True).agg(
        trades=("size", "count"), contracts=("size", "sum"), ask=("ask", "sum"), bid=("bid", "sum"),
        spread_legs=("ml", "sum"), premium=("premium", "sum"), px_x=("px_x", "sum"),
        low=("price", "min"), high=("price", "max"), biggest=("size", "max"),
        stock_lo=("underlying_price", "min"), stock_hi=("underlying_price", "max")).reset_index()
    agg = agg[agg["contracts"] > 0].sort_values(["day", "contracts"], ascending=[True, False])
    daily = pd.DataFrame({
        "Day": [f"{d:%a, %b} {d.day}" for d in agg["day"]], "Contract": agg["contract"].astype(str),
        "Contracts": agg["contracts"], "% at ask": agg["ask"] / agg["contracts"] * 100,
        "% at bid": agg["bid"] / agg["contracts"] * 100, "% in spreads": agg["spread_legs"] / agg["contracts"] * 100,
        "Avg price": agg["px_x"] / agg["contracts"], "Low": agg["low"], "High": agg["high"],
        "Premium": agg["premium"], "Biggest trade": agg["biggest"], "Trades": agg["trades"],
        "Stock range": [f"${a:.2f} – ${b:.2f}" for a, b in zip(agg["stock_lo"], agg["stock_hi"])]})
    pct = N(format="%.0f%%")
    show_table(daily, {"Contracts": INT, "% at ask": pct, "% at bid": pct, "% in spreads": pct, "Avg price": USD,
                       "Low": USD, "High": USD, "Premium": USD0, "Biggest trade": INT, "Trades": INT}, color_side=False)
    st.download_button("Download this table (CSV)", lambda _d=daily: _d.to_csv(index=False).encode(), on_click="ignore",
                       file_name=f"{ticker}_daily_totals.csv", mime="text/csv", icon=":material/download:")

# --------------------------------------------------------------------------- spreads

with tab_spreads:
    st.markdown("<div class='help'>Orders with two or more legs, put back together by matching legs that traded at "
                "the exact same moment. Whether the trader bought or sold the spread is a judgment call from where "
                "each leg traded.</div>", unsafe_allow_html=True)
    min_leg = st.number_input("Biggest leg at least (contracts)", 0, 100000, 500, step=100)
    keys = f.loc[f["multi_leg"] & (f["size"] >= min_leg), "executed_at"].unique()
    ml = raw[raw["multi_leg"] & raw["executed_at"].isin(keys)]
    ml = ml[ml.groupby("executed_at")["size"].transform("count") >= 2]
    if ml.empty:
        st.info("No spreads match. Lower the minimum or widen the filters.")
    else:
        top = ml.groupby("executed_at")["size"].max().nlargest(2000).index
        ml = ml[ml["executed_at"].isin(top)].sort_values(["executed_at", "expiry", "option_type", "strike"])
        rows = []
        for ts, grp in ml.groupby("executed_at", sort=True):
            rows.append({
                "Time (ET)": ts.tz_localize(None),
                "Legs": "   +   ".join(f"{int(r.size):,} × {r.contract} @ ${r.price:.2f} ({r.side or '?'})"
                                       for r in grp.itertuples()),
                "Biggest leg": int(grp["size"].max()),
                "Premium (all legs)": float(grp["premium"].sum()),
                "Stock": float(grp["underlying_price"].iloc[0]),
                "Notes": ", ".join(x for x, on in (("floor trade", grp["floor"].any()), ("cross", grp["cross"].any()),
                                                   ("sweep", grp["sweep"].any())) if on),
            })
        sp = pd.DataFrame(rows)
        st.markdown(f"<div class='help'>{len(sp):,} spreads</div>", unsafe_allow_html=True)
        show_table(sp, {"Time (ET)": st.column_config.DatetimeColumn(format="ddd MMM D, h:mm:ss a"),
                        "Legs": st.column_config.TextColumn(width="large"), "Biggest leg": INT,
                        "Premium (all legs)": USD0, "Stock": USD}, color_side=False)
        st.download_button("Download spreads (CSV)", lambda _d=sp: _d.to_csv(index=False).encode(), on_click="ignore",
                           file_name=f"{ticker}_spreads.csv", mime="text/csv", icon=":material/download:")

# --------------------------------------------------------------------------- contract history

with tab_contract:
    vol = f.groupby("option_chain_id", observed=True)["size"].sum().sort_values(ascending=False)
    choices = [str(o) for o in vol.index[:300]]
    if not choices:
        st.info("No contracts match the filters.")
    else:
        u = f.drop_duplicates("option_chain_id")
        lab = dict(zip(u["option_chain_id"].astype(str), u["contract"].astype(str)))
        osi = st.selectbox("Contract (most traded first)", choices, format_func=lambda o: lab.get(o, o))
        try:
            h = history(osi)
        except Exception as e:  # noqa: BLE001
            h = pd.DataFrame()
            st.warning(f"Couldn't load this contract's daily history from Unusual Whales: {e}")
        if not h.empty:
            st.markdown("#### Day by day")
            st.markdown("<div class='help'>Open interest is published the next morning. <b>New open interest</b> "
                        "moves each change back to the day the positions were actually opened.</div>",
                        unsafe_allow_html=True)
            hd = pd.DataFrame({
                "Day": [f"{d:%a, %b} {d.day}" for d in h["date"]], "Volume": h["volume"],
                "% at ask": h.get("ask_volume") / h["volume"].where(h["volume"] > 0) * 100,
                "% at bid": h.get("bid_volume") / h["volume"].where(h["volume"] > 0) * 100,
                "Open": h.get("open_price"), "Low": h.get("low_price"), "High": h.get("high_price"),
                "Close": h.get("last_price"), "Premium": h.get("total_premium"),
                "New open interest": h["oi_opened_that_day"], "Open interest (morning)": h["open_interest"]})
            show_table(hd.head(30), {"Volume": INT, "% at ask": N(format="%.0f%%"), "% at bid": N(format="%.0f%%"),
                                     "Open": USD, "Low": USD, "High": USD, "Close": USD, "Premium": USD0,
                                     "New open interest": N(format="%+,d"), "Open interest (morning)": INT},
                       height=420, color_side=False)
        st.markdown("#### Biggest trades in the days loaded")
        one = raw[raw["option_chain_id"].astype(str) == osi].nlargest(25, "size")
        show_table(pd.DataFrame({
            "Time (ET)": one["executed_at"].dt.tz_localize(None), "Side": one["side"].astype(str),
            "Contracts": one["size"], "Price": one["price"], "Premium": one["premium"],
            "Stock": one["underlying_price"], "Type": one["multi_leg"].map({True: "Spread leg", False: "Single"})}),
            {"Time (ET)": st.column_config.DatetimeColumn(format="ddd MMM D, h:mm:ss a"), "Contracts": INT,
             "Price": USD, "Premium": USD0, "Stock": USD}, height=420)

st.markdown("<div class='foot'>Data: Unusual Whales. Past days come from the full options tape and today from the live "
            "trades feed. Canceled trades are removed. Spread legs are flagged by exchange multi-leg codes. "
            "Times are Eastern.</div>", unsafe_allow_html=True)
