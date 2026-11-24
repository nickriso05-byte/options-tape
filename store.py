"""
Shared storage for saved days: a private GitHub repo (default nickriso05-byte/options-tape-data).

Streamlit Cloud wipes its disk when the app restarts, so every saved day is also committed
to this repo, and any machine running the app (Cloud or local) downloads days it doesn't
have yet. Files live at data/<TICKER>/<YYYY-MM-DD>.parquet, the same layout as locally.

Config (env or Streamlit secrets):
  DATA_REPO        owner/name of the private repo
  DATA_REPO_TOKEN  fine-grained token with Contents read/write on that repo only.
                   Locally, if it's unset, the GitHub CLI's login (`gh auth token`) is used.
"""

from __future__ import annotations

import base64
import os
import subprocess
import time
from datetime import date
from pathlib import Path

import requests

GH = "https://api.github.com"
_list_cache: dict[str, tuple[float, list[date]]] = {}


def _secret(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name].strip()
    try:
        import streamlit as st
        v = st.secrets.get(name)
        return str(v).strip() if v else None
    except Exception:  # noqa: BLE001
        return None


def repo() -> str:
    return _secret("DATA_REPO") or "nickriso05-byte/options-tape-data"


def _token() -> str:
    t = _secret("DATA_REPO_TOKEN")
    if t:
        return t
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("No DATA_REPO_TOKEN and no GitHub CLI login") from e


def _h(raw: bool = False) -> dict:
    return {"Authorization": f"Bearer {_token()}", "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json"}


PART_MB = 20  # GitHub's blob API refuses requests much past ~35 MB, so each day is split into parts


def list_days(ticker: str, max_age: float = 120) -> list[date]:
    t = ticker.upper()
    hit = _list_cache.get(t)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    r = requests.get(f"{GH}/repos/{repo()}/contents/data/{t}", headers=_h(), timeout=20)
    if r.status_code == 404:
        days: list[date] = []
    else:
        r.raise_for_status()
        days = sorted(date.fromisoformat(x["name"][:10]) for x in r.json()
                      if x["type"] == "dir" or x["name"].endswith(".parquet"))
    _list_cache[t] = (time.time(), days)
    return days


def _get_raw(path: str, dest: Path) -> None:
    with requests.get(f"{GH}/repos/{repo()}/contents/{path}", headers=_h(raw=True), stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for c in r.iter_content(1 << 20):
                f.write(c)


def pull(ticker: str, d: date, dest: Path) -> Path:
    """Download one saved day: either data/T/<day>/part-*.parquet or a single data/T/<day>.parquet."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    folder = f"data/{ticker.upper()}/{d.isoformat()}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    r = requests.get(f"{GH}/repos/{repo()}/contents/{folder}", headers=_h(), timeout=20)
    if r.status_code == 404:
        _get_raw(folder + ".parquet", part)
    else:
        r.raise_for_status()
        names = sorted(x["name"] for x in r.json() if x["name"].endswith(".parquet"))
        tables = []
        for n in names:
            tmp = dest.with_name(f"{d.isoformat()}.{n}")
            _get_raw(f"{folder}/{n}", tmp)
            tables.append(pq.read_table(tmp))
            tmp.unlink()
        pq.write_table(pa.concat_tables(tables, promote_options="default"), part, compression="zstd")
    os.replace(part, dest)
    return dest


def _blob(data: bytes) -> str:
    r = requests.post(f"{GH}/repos/{repo()}/git/blobs", headers=_h(), timeout=300,
                      json={"content": base64.b64encode(data).decode(), "encoding": "base64"})
    r.raise_for_status()
    return r.json()["sha"]


def push(local: Path, ticker: str, d: date) -> None:
    """Commit one saved day, split into ~20 MB parts, in a single commit."""
    import io
    import math

    import pyarrow.parquet as pq

    table = pq.read_table(local)
    parts = max(1, math.ceil(local.stat().st_size / (PART_MB * 1024 * 1024)))
    step = math.ceil(table.num_rows / parts) or 1
    folder = f"data/{ticker.upper()}/{d.isoformat()}"
    entries = []
    for i in range(parts):
        buf = io.BytesIO()
        pq.write_table(table.slice(i * step, step), buf, compression="zstd")
        entries.append({"path": f"{folder}/part-{i:02d}.parquet", "mode": "100644", "type": "blob",
                        "sha": _blob(buf.getvalue())})
    base = f"{GH}/repos/{repo()}/git"
    for attempt in range(4):   # retry if main moved in between
        head = requests.get(f"{base}/ref/heads/main", headers=_h(), timeout=20)
        head.raise_for_status()
        parent = head.json()["object"]["sha"]
        tree = requests.get(f"{base}/commits/{parent}", headers=_h(), timeout=20).json()["tree"]["sha"]
        nt = requests.post(f"{base}/trees", headers=_h(), timeout=30, json={"base_tree": tree, "tree": entries})
        nt.raise_for_status()
        c = requests.post(f"{base}/commits", headers=_h(), timeout=30, json={
            "message": f"Add {ticker.upper()} {d.isoformat()}", "tree": nt.json()["sha"], "parents": [parent]})
        c.raise_for_status()
        u = requests.patch(f"{base}/refs/heads/main", headers=_h(), timeout=30, json={"sha": c.json()["sha"]})
        if u.status_code == 200:
            _list_cache.pop(ticker.upper(), None)
            return
        time.sleep(1 + attempt)
    u.raise_for_status()
