"""The local bar store.

One directory per instrument. ``M1.bin`` is the truth; every higher timeframe
is derived from it and can be thrown away and rebuilt. Each file is the raw
bytes of a numpy structured array, so reading a viewport is a memory-map and a
slice rather than a parse.

    data/EURUSD/meta.json
    data/EURUSD/M1.bin
    data/EURUSD/M5.bin  M15.bin  M30.bin  H1.bin  H4.bin  D1.bin  W1.bin

Times are unix seconds in **UTC**, always. Each source has its own idea of what
a timestamp means and the conversion happens at ingest, once, in the fetcher —
never later. HistData in particular stamps its bars in fixed EST with no
daylight saving, which silently shifts every session-filtered strategy by an
hour for half the year if it is taken at face value.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import numpy as np

from . import catalog, paths

# time, open, high, low, close, spread, volume.
# Prices are integers in units of the instrument's point (see catalog.py).
# Spread is also in points, and is 0 for sources that do not carry bid/ask.
BAR = np.dtype([
    ("t", "<u4"),
    ("o", "<i4"), ("h", "<i4"), ("l", "<i4"), ("c", "<i4"),
    ("sp", "<u4"), ("v", "<u4"),
])

MINUTE = 60
TIMEFRAMES = {
    "M1": 1 * MINUTE,
    "M5": 5 * MINUTE,
    "M15": 15 * MINUTE,
    "M30": 30 * MINUTE,
    "H1": 60 * MINUTE,
    "H4": 240 * MINUTE,
    "D1": 1440 * MINUTE,
    "W1": 7 * 1440 * MINUTE,
}
TF_ORDER = list(TIMEFRAMES)

# Unix epoch fell on a Thursday, so a raw ``t // 604800`` puts week boundaries
# on Thursdays. Monday is three days before the next Thursday, so shifting the
# key by three days lands every boundary on Monday 00:00 UTC — where the
# trading week actually starts. (test_core checks this; four days looks just as
# plausible and silently gives Sunday.)
_WEEK_SHIFT = 3 * 86400

def sym_dir(symbol: str) -> str:
    return os.path.join(paths.data_dir(), symbol.upper())


def bar_path(symbol: str, tf: str) -> str:
    return os.path.join(sym_dir(symbol), f"{tf}.bin")


def meta_path(symbol: str) -> str:
    return os.path.join(sym_dir(symbol), "meta.json")


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def bar_count(symbol: str, tf: str = "M1") -> int:
    path = bar_path(symbol, tf)
    if not os.path.exists(path):
        return 0
    return os.path.getsize(path) // BAR.itemsize


def load(symbol: str, tf: str = "M1") -> np.ndarray:
    """Read a whole timeframe into memory.

    Deliberately **not** a memory map. A map stays alive as long as any slice
    of it is referenced, and Windows will not let a mapped file be replaced —
    so downloading while the window is open failed with a bare access-denied
    the moment the server had served one chart. Reading a viewport does not go
    through here anyway; see ``window``.
    """
    path = bar_path(symbol, tf)
    if not os.path.exists(path) or os.path.getsize(path) < BAR.itemsize:
        return np.empty(0, dtype=BAR)
    return np.fromfile(path, dtype=BAR)


def _seek_index(f, n: int, target: int) -> int:
    """First record with ``t >= target``, found by seeking rather than reading.

    Twenty-three four-byte reads locate a bar in a seven-million-bar file, so a
    viewport never has to touch the rest of it.
    """
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        f.seek(mid * BAR.itemsize)
        t = int.from_bytes(f.read(4), "little")
        if t < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _replace(tmp: str, dest: str, attempts: int = 12) -> None:
    """os.replace, retried.

    A reader that happens to have the file open at this instant makes Windows
    refuse the rename. The readers are single requests measured in
    milliseconds, so waiting briefly is enough.
    """
    for i in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    os.replace(tmp, dest)


def read_meta(symbol: str) -> dict:
    try:
        with open(meta_path(symbol), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_meta(symbol: str, meta: dict) -> None:
    os.makedirs(sym_dir(symbol), exist_ok=True)
    tmp = meta_path(symbol) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, meta_path(symbol))


def window(symbol: str, tf: str, t0: int, t1: int, limit: int = 0) -> np.ndarray:
    """Bars with ``t0 <= t < t1``, read straight off disk.

    With ``limit`` set the *last* ``limit`` bars of the range are returned,
    which is what a chart asking for "the N bars before this moment" wants.
    """
    n = bar_count(symbol, tf)
    if n == 0:
        return np.empty(0, dtype=BAR)
    with open(bar_path(symbol, tf), "rb") as f:
        lo = _seek_index(f, n, t0)
        hi = _seek_index(f, n, t1)
        if limit and hi - lo > limit:
            lo = hi - limit
        if hi <= lo:
            return np.empty(0, dtype=BAR)
        f.seek(lo * BAR.itemsize)
        raw = f.read((hi - lo) * BAR.itemsize)
    return np.frombuffer(raw, dtype=BAR)


def bounds(symbol: str, tf: str = "M1") -> tuple[int | None, int | None]:
    """First and last timestamp, without reading the file in between."""
    n = bar_count(symbol, tf)
    if n == 0:
        return None, None
    with open(bar_path(symbol, tf), "rb") as f:
        first = int.from_bytes(f.read(4), "little")
        f.seek((n - 1) * BAR.itemsize)
        last = int.from_bytes(f.read(4), "little")
    return first, last


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def merge_m1(symbol: str, new: np.ndarray) -> int:
    """Fold new M1 bars into the store and return the resulting bar count.

    Existing bars win over incoming duplicates so that a re-download of a
    period we already hold as bid/ask (with a real spread) is not quietly
    downgraded by a spreadless source covering the same minutes.
    """
    if len(new) == 0:
        return len(load(symbol, "M1"))

    new = np.sort(new, order="t")
    # Drop duplicate timestamps inside the incoming batch itself.
    keep = np.empty(len(new), dtype=bool)
    keep[0] = True
    np.not_equal(new["t"][1:], new["t"][:-1], out=keep[1:])
    new = new[keep]

    old = np.asarray(load(symbol, "M1"))
    if len(old):
        fresh = new[~np.isin(new["t"], old["t"], assume_unique=True)]
        merged = np.concatenate([old, fresh]) if len(fresh) else old
        merged.sort(order="t")
    else:
        merged = new

    os.makedirs(sym_dir(symbol), exist_ok=True)
    tmp = bar_path(symbol, "M1") + ".tmp"
    merged.tofile(tmp)
    _replace(tmp, bar_path(symbol, "M1"))
    return len(merged)


def aggregate(bars: np.ndarray, period: int) -> np.ndarray:
    """Roll bars up into a coarser timeframe.

    Empty periods produce no bar at all rather than a flat placeholder, which
    is what every charting package does and what keeps weekends from taking up
    two fifths of a daily chart.
    """
    if len(bars) == 0:
        return np.empty(0, dtype=BAR)

    if period == TIMEFRAMES["W1"]:
        key = (bars["t"].astype(np.int64) + _WEEK_SHIFT) // period
        start = key * period - _WEEK_SHIFT
    else:
        key = bars["t"].astype(np.int64) // period
        start = key * period

    # Index where each new period begins.
    edges = np.flatnonzero(np.diff(key))
    heads = np.empty(len(edges) + 1, dtype=np.int64)
    heads[0] = 0
    heads[1:] = edges + 1

    out = np.empty(len(heads), dtype=BAR)
    out["t"] = start[heads]
    out["o"] = bars["o"][heads]
    out["c"] = bars["c"][np.append(heads[1:] - 1, len(bars) - 1)]
    out["h"] = np.maximum.reduceat(bars["h"], heads)
    out["l"] = np.minimum.reduceat(bars["l"], heads)
    out["v"] = np.add.reduceat(bars["v"].astype(np.int64), heads).clip(0, 2**32 - 1)
    counts = np.diff(np.append(heads, len(bars)))
    out["sp"] = (np.add.reduceat(bars["sp"].astype(np.int64), heads) // counts)
    return out


def rebuild_timeframes(symbol: str, m1: np.ndarray | None = None) -> dict:
    """Regenerate every derived timeframe from M1. Cheap enough to always do."""
    if m1 is None:
        m1 = load(symbol, "M1")
    counts = {"M1": len(m1)}
    for tf in TF_ORDER[1:]:
        agg = aggregate(m1, TIMEFRAMES[tf])
        tmp = bar_path(symbol, tf) + ".tmp"
        agg.tofile(tmp)
        _replace(tmp, bar_path(symbol, tf))
        counts[tf] = len(agg)
    return counts


def refresh_meta(symbol: str) -> dict:
    """Recompute the summary the UI reads: coverage, size, per-timeframe counts."""
    inst = catalog.get(symbol)
    m1 = load(symbol, "M1")          # read once, reused by the rebuild below
    counts = rebuild_timeframes(symbol, m1)
    meta = read_meta(symbol)
    meta.update({
        "symbol": inst.symbol,
        "name": inst.name,
        "group": inst.group,
        "point": inst.point,
        "digits": inst.digits,
        "contract": inst.contract,
        "source": inst.source,
        "bars": int(len(m1)),
        "counts": counts,
        "first": int(m1["t"][0]) if len(m1) else None,
        "last": int(m1["t"][-1]) if len(m1) else None,
        "has_spread": bool(len(m1) and int(m1["sp"].max()) > 0),
        "bytes": sum(
            os.path.getsize(bar_path(symbol, tf))
            for tf in TF_ORDER if os.path.exists(bar_path(symbol, tf))
        ),
    })
    write_meta(symbol, meta)
    return meta


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def to_unix(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def from_unix(t: int) -> datetime:
    return datetime.fromtimestamp(int(t), tz=timezone.utc)


def scale(prices, point: float):
    """Float prices -> stored integers, rounded rather than truncated."""
    return np.rint(np.asarray(prices, dtype=np.float64) / point).astype(np.int64)
