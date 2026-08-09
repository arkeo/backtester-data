"""Binance — one HTTP request per MONTH of crypto M1 bars.

    https://data.binance.vision/data/spot/monthly/klines/{SYM}/1m/{SYM}-1m-{YYYY-MM}.zip

About 2 MB a month, 44,640 bars in a 31-day month: genuinely 24/7 with no
weekend holes, unlike every other market here.

Two traps in the archive itself:

* Files published from 2025 onwards carry a **header row**, older ones do not.
* Binance switched ``open_time`` from **milliseconds to microseconds** part way
  through. The magnitude tells them apart; nothing in the file does.

``data.binance.vision`` is a plain static bucket and is reachable where the
trading API is not — ``api.binance.com`` answers **HTTP 451** from this machine.
Nothing here touches the API, so that does not matter, but it is why the bulk
route is the only route used.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import numpy as np

from .. import store
from . import net

URL = ("https://data.binance.vision/data/spot/monthly/klines/"
       "{sym}/1m/{sym}-1m-{y:04d}-{m:02d}.zip")

#: The same bars, a day per file. Needed because the monthly archive for the
#: month in progress is not published until the month ends — so a monthly-only
#: fetch stops at the last day of last month, and the coins sit up to five
#: weeks behind everything else in the catalogue while looking complete.
DAY_URL = ("https://data.binance.vision/data/spot/daily/klines/"
           "{sym}/1m/{sym}-1m-{y:04d}-{m:02d}-{d:02d}.zip")


def _parse_csv(raw: bytes, point: float) -> np.ndarray:
    lines = raw.split(b"\n")
    n = len(lines)
    t = np.empty(n, dtype=np.int64)
    ohlcv = np.empty((n, 5), dtype=np.float64)

    k = 0
    for line in lines:
        if not line or line[0] not in b"0123456789":
            continue                      # blank line, or the 2025+ header row
        f = line.split(b",")
        if len(f) < 6:
            continue
        try:
            stamp = int(f[0])
            ohlcv[k] = (float(f[1]), float(f[2]), float(f[3]),
                        float(f[4]), float(f[5]))
        except ValueError:
            continue
        # Milliseconds until Binance switched to microseconds; a 2020s epoch in
        # ms is ~1.6e12 and in us is ~1.6e15, so the magnitude decides.
        t[k] = stamp // (1_000_000 if stamp > 1e14 else 1_000)
        k += 1

    bars = np.empty(k, dtype=store.BAR)
    bars["t"] = t[:k]
    bars["o"] = store.scale(ohlcv[:k, 0], point)
    bars["h"] = store.scale(ohlcv[:k, 1], point)
    bars["l"] = store.scale(ohlcv[:k, 2], point)
    bars["c"] = store.scale(ohlcv[:k, 3], point)
    bars["sp"] = 0                        # spot klines carry no bid/ask
    bars["v"] = np.clip(np.rint(ohlcv[:k, 4]), 0, 4e9)
    return bars


def _unzip(blob: bytes, what: str) -> bytes:
    if not blob.startswith(b"PK"):
        raise net.NotFound(f"{what}: not an archive")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise net.NotFound(f"{what}: archive holds no csv")
        return z.read(names[0])


def fetch_month(code: str, year: int, month: int, point: float) -> np.ndarray:
    """A month of bars, however it has to be assembled.

    The monthly archive when there is one; otherwise the days. A 404 here means
    one of two very different things — the month has not been packaged yet, or
    the coin did not exist — and only trying the days tells them apart.
    """
    try:
        blob = net.fetch(URL.format(sym=code, y=year, m=month), attempts=3)
        return _parse_csv(_unzip(blob, f"{code} {year}-{month:02d}"), point)
    except net.NotFound:
        return _fetch_days(code, year, month, point)


def _fetch_days(code: str, year: int, month: int, point: float) -> np.ndarray:
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta

    day = date(year, month, 1)
    days = []
    while day.month == month and day <= date.today():
        days.append(day)
        day += timedelta(days=1)

    def one(d: date):
        try:
            blob = net.fetch(DAY_URL.format(sym=code, y=d.year, m=d.month,
                                            d=d.day), attempts=2)
            return _parse_csv(_unzip(blob, f"{code} {d}"), point)
        except net.NotFound:
            return None                 # today's file, or before the coin listed

    with ThreadPoolExecutor(max_workers=4) as pool:
        parts = [b for b in pool.map(one, days) if b is not None and len(b)]

    if not parts:
        raise net.NotFound(f"{code} {year}-{month:02d}: no monthly or daily files")
    return np.concatenate(parts)


#: An int32 holds about 2.1e9, and prices are stored as `price / point`. A step
#: fine enough for a coin worth a millionth of a cent would overflow that for
#: one worth a hundred thousand dollars, so the step is also bounded by the
#: largest price seen.
_HEADROOM = 2.0e9


def detect_point(code: str, month: str) -> float:
    """Work out a pair's price step by looking at its prices.

    There are thousands of pairs and no listing anywhere that gives their tick
    sizes, so it is measured instead: the archive writes prices as decimal
    strings, and the finest place any of them actually uses is the step. A
    coin quoted to eight decimals and one quoted to two are then both stored
    exactly, rather than one of them being rounded to nothing.
    """
    y, m = month.split("-")
    blob = net.fetch(URL.format(sym=code, y=int(y), m=int(m)))
    raw = _unzip(blob, f"{code} {month}")

    decimals = 0
    biggest = 0.0
    for line in raw.split(b"\n"):
        if not line or line[0] not in b"0123456789":
            continue
        f = line.split(b",")
        if len(f) < 5:
            continue
        for field in f[1:5]:
            text = field.decode("ascii", "ignore").strip()
            if "." in text:
                # Trailing zeros are padding, not precision.
                decimals = max(decimals, len(text.split(".")[1].rstrip("0")))
        try:
            biggest = max(biggest, float(f[2]))
        except ValueError:
            continue

    point = 10.0 ** -min(decimals, 8)
    while biggest and biggest / point > _HEADROOM:
        point *= 10.0
    return point


def months_in(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1
