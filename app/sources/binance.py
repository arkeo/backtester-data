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


def fetch_month(code: str, year: int, month: int, point: float) -> np.ndarray:
    blob = net.fetch(URL.format(sym=code, y=year, m=month))
    if not blob.startswith(b"PK"):
        raise net.NotFound(f"{code} {year}-{month:02d}: not an archive")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise net.NotFound(f"{code} {year}-{month:02d}: archive holds no csv")
        raw = z.read(names[0])
    return _parse_csv(raw, point)


def months_in(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1
