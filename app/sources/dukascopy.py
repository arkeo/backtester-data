"""Dukascopy — one HTTP request per DAY of M1 bars, with a real spread.

The handoff costed the Dow at ~87,000 requests and roughly ten hours, because
it assumed the only Dukascopy route was the hourly tick file. It is not: the
same feed serves **pre-aggregated M1 candles, one file per day**, separately
for bid and ask:

    /datafeed/{SYMBOL}/{year}/{month-1:02d}/{day:02d}/BID_candles_min_1.bi5
    /datafeed/{SYMBOL}/{year}/{month-1:02d}/{day:02d}/ASK_candles_min_1.bi5

That is 1,440 bars for 15 KB. Fourteen years of the Dow becomes about 3,650
days rather than 87,000 hours — and because bid and ask arrive as separate
files, every stored bar carries the spread that actually applied, which a
manual simulator needs in order to fill honestly.

Three things that cost hours if unknown:

* **The month is zero-based.** January is ``00``.
* **``.bi5`` is raw LZMA**, not gzip and not ``.xz``. It needs
  ``FORMAT_ALONE``, and it has no end-of-stream marker.
* Records are 24 bytes big-endian ``>IIIIIf``, and the order is
  **time, open, close, low, high, volume** — close before low, which is not
  the order anyone expects.

Prices arrive as integers scaled by a power of ten that depends on the
instrument: 10^5 for the five-decimal FX pairs, 10^3 for everything else
(yen pairs, metals, indices, commodities). Times are already UTC.
"""

from __future__ import annotations

import lzma
import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np

from .. import store
from . import net

# Every day needs a bid file and an ask file, and each request is dominated by
# round-trip latency rather than by size. Fetching the two sides sequentially
# therefore halves the throughput of the whole download for no reason. This
# pool is shared rather than created per day so the thread count stays bounded
# no matter how many days are in flight.
_SIDES = ThreadPoolExecutor(max_workers=32, thread_name_prefix="duka-side")

URL = ("https://datafeed.dukascopy.com/datafeed/{sym}/{y:04d}/{m:02d}/{d:02d}/"
       "{side}_candles_min_1.bi5")

REC = struct.Struct(">IIIIIf")   # offset_seconds, open, close, low, high, volume
REC_SIZE = REC.size              # 24


def scale_for(inst) -> int:
    """The power of ten Dukascopy scales this instrument's prices by."""
    if inst.duka_scale:
        return inst.duka_scale
    five_decimal_fx = (
        inst.group == "forex" and not inst.symbol.endswith(("JPY", "HUF"))
    )
    return 100_000 if five_decimal_fx else 1_000


def _decode(blob: bytes) -> np.ndarray | None:
    """One ``.bi5`` day -> ``(n, 6)`` float array, or None if the day is empty."""
    if not blob:
        return None
    raw = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(blob)
    n = len(raw) // REC_SIZE
    if n == 0:
        return None
    # A structured read beats 1,440 struct.unpack calls per day, and there are
    # thousands of days.
    dt = np.dtype([("t", ">u4"), ("o", ">u4"), ("c", ">u4"),
                   ("l", ">u4"), ("h", ">u4"), ("v", ">f4")])
    return np.frombuffer(raw[: n * REC_SIZE], dtype=dt)


def fetch_day(duka_symbol: str, day: date, point: float, scale: int) -> np.ndarray:
    """One day of M1 bars with spread. Empty array for weekends and holidays."""
    midnight = int(np.datetime64(day.isoformat(), "s").astype("int64"))

    def one(side):
        url = URL.format(sym=duka_symbol, y=day.year, m=day.month - 1,
                         d=day.day, side=side)
        try:
            return _decode(net.fetch(url))
        except net.NotFound:
            return None

    futures = {side: _SIDES.submit(one, side) for side in ("BID", "ASK")}

    # The two halves are not equally important, and treating them as though
    # they were threw away whole days for nothing. Bid is the price series —
    # without it there is no day. Ask only supplies the spread, and when the
    # feed refuses it the honest result is a day priced correctly with its
    # spread left unknown, exactly as every instrument whose source carries no
    # bid/ask is already stored. Failing the day instead meant a refusal on the
    # lesser half discarded a complete, already-paid-for download — and on a
    # feed that refuses a fifth of requests, that was most of the loss.
    bid = futures["BID"].result()
    try:
        ask = futures["ASK"].result()
    except Exception:                     # noqa: BLE001
        ask = None

    if bid is None:
        # No bid means no session. An ask-only day is not usable on its own.
        return np.empty(0, dtype=store.BAR)

    # Prices are quoted on the bid, exactly as MetaTrader charts them, and the
    # spread is carried alongside so a fill can be priced honestly.
    conv = 1.0 / (scale * point)     # raw integer -> our stored integer

    bars = np.empty(len(bid), dtype=store.BAR)
    bars["t"] = midnight + bid["t"].astype(np.int64)
    bars["o"] = np.rint(bid["o"].astype(np.float64) * conv)
    bars["h"] = np.rint(bid["h"].astype(np.float64) * conv)
    bars["l"] = np.rint(bid["l"].astype(np.float64) * conv)
    bars["c"] = np.rint(bid["c"].astype(np.float64) * conv)
    bars["v"] = np.rint(np.clip(bid["v"].astype(np.float64), 0, 4e9))

    if ask is not None and len(ask) == len(bid) and np.array_equal(
            np.asarray(ask["t"]), np.asarray(bid["t"])):
        spread = (ask["c"].astype(np.int64) - bid["c"].astype(np.int64))
        bars["sp"] = np.clip(np.rint(spread * conv), 0, 4e9)
    else:
        # Rare, but the two sides can disagree on which minutes traded. Line
        # them up by timestamp rather than dropping the spread entirely.
        bars["sp"] = 0
        if ask is not None:
            common, ib, ia = np.intersect1d(
                np.asarray(bid["t"]), np.asarray(ask["t"]), return_indices=True)
            if len(common):
                spread = (ask["c"][ia].astype(np.int64)
                          - bid["c"][ib].astype(np.int64))
                bars["sp"][ib] = np.clip(np.rint(spread * conv), 0, 4e9)

    return bars


def days_in(start: date, end: date):
    """Weekdays only. Dukascopy has no Saturday sessions and Sunday is a stub."""
    d = start
    step = timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += step
