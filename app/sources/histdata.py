"""HistData — one HTTP request per YEAR of M1 bars.

By a wide margin the cheapest deep history available: seventeen years of gold
arrives in about two minutes and seventy megabytes. It carries no bid/ask, so
bars land with spread 0 and are topped up from Dukascopy later if the user
wants true spread.

Getting the file is not a plain GET. The download page hides a one-shot token
that must be posted back, with a Referer, to a separate endpoint:

    1. GET  /download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{pair}/{year}
    2. scrape the hidden inputs tk, date, datemonth, platform, timeframe, fxpair
    3. POST them to /get.php with Referer set to the page from step 1
    4. the response is a ZIP holding one CSV

Timestamps
----------
HistData describes its stamps as "EST", and the project handoff recorded that
as fixed UTC-5 with no daylight saving. **That is wrong**, and it was checked
rather than assumed: the same EURUSD minutes were pulled from Dukascopy, which
is unambiguously UTC, and compared.

    2024-01-10 (winter)  HistData + 5h == Dukascopy, 1438/1438 closes identical
    2024-07-10 (summer)  HistData + 4h == Dukascopy, 1371/1371 closes identical

So the stamps do observe daylight saving. Walking the transitions day by day
then showed they do **not** switch on the US dates:

    Mar 08  +5h   Mar 27  +5h   |  Apr 02  +4h        US switched Mar 10
    Oct 24  +4h   Oct 30  +5h   |  Nov 01  +5h        US switched Nov 03

The changeovers are the **last Sunday in March and the last Sunday in October**
— the European calendar — applied to a US offset. That is the MetaTrader broker
convention (a server clock that shifts on EU dates), which is presumably where
the feed originates.

Between the European and American changeovers there is a three-week window in
spring and a one-week window in autumn where a US-rule conversion is exactly
one hour out. On a chart that is invisible; underneath it, every session filter
and every "what happened at the New York open" is wrong for those weeks.

There is no ambiguity to resolve at either transition: both fall early on a
Sunday morning, inside the weekend gap, where no bar exists.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlencode

import numpy as np

from .. import store
from . import net

PAGE = ("https://www.histdata.com/download-free-forex-historical-data/"
        "?/ascii/1-minute-bar-quotes/{pair}/{year}")
PAGE_MONTH = PAGE + "/{month}"
POST = "https://www.histdata.com/get.php"

EST = 5 * 3600      # New York standard time
EDT = 4 * 3600      # New York daylight time

_FIELDS = ("tk", "date", "datemonth", "platform", "timeframe", "fxpair")
_INPUT = re.compile(
    rb'<input[^>]*id\s*=\s*["\']([a-z]+)["\'][^>]*value\s*=\s*["\']([^"\']*)["\']',
    re.I)


def _form_fields(html: bytes, pair: str, year: int) -> dict:
    found = {k.decode().lower(): v.decode() for k, v in _INPUT.findall(html)}
    fields = {f: found.get(f, "") for f in _FIELDS}
    if not fields["tk"]:
        raise RuntimeError("histdata: download token not found on the page")
    # The page omits these when the whole year is offered as one file.
    fields.setdefault("date", str(year))
    fields["fxpair"] = fields["fxpair"] or pair.upper()
    fields["platform"] = fields["platform"] or "ASCII"
    fields["timeframe"] = fields["timeframe"] or "M1"
    return fields


def _parse_csv(raw: bytes, point: float) -> np.ndarray:
    """``YYYYMMDD HHMMSS;open;high;low;close;volume`` -> our bar array."""
    lines = raw.split(b"\n")
    n = len(lines)
    t = np.empty(n, dtype=np.int64)
    ohlc = np.empty((n, 4), dtype=np.float64)

    # Timestamps are fixed width, so the calendar fields can be sliced straight
    # out of the bytes instead of being handed to a date parser 400,000 times.
    k = 0
    for line in lines:
        if len(line) < 20:
            continue
        try:
            stamp, o, h, l, c, _ = line.split(b";")
            y = int(stamp[0:4]); mo = int(stamp[4:6]); d = int(stamp[6:8])
            hh = int(stamp[9:11]); mi = int(stamp[11:13])
            ohlc[k] = (float(o), float(h), float(l), float(c))
        except (ValueError, IndexError):
            continue
        t[k] = _days_from_civil(y, mo, d) * 86400 + hh * 3600 + mi * 60
        k += 1

    t = _to_utc(t[:k])
    ohlc = ohlc[:k]

    bars = np.empty(k, dtype=store.BAR)
    bars["t"] = t
    bars["o"] = store.scale(ohlc[:, 0], point)
    bars["h"] = store.scale(ohlc[:, 1], point)
    bars["l"] = store.scale(ohlc[:, 2], point)
    bars["c"] = store.scale(ohlc[:, 3], point)
    bars["sp"] = 0          # HistData has no bid/ask
    bars["v"] = 0           # its volume column is always zero anyway
    return bars


def _to_utc(t_local: np.ndarray) -> np.ndarray:
    """New-York-local seconds -> UTC seconds, honouring daylight saving."""
    if len(t_local) == 0:
        return t_local
    offsets = np.full(len(t_local), EST, dtype=np.int64)
    first = datetime.fromtimestamp(int(t_local.min()), timezone.utc).year
    last = datetime.fromtimestamp(int(t_local.max()), timezone.utc).year
    for year in range(first, last + 1):
        start, end = dst_window(year)
        offsets[(t_local >= start) & (t_local < end)] = EDT
    return t_local + offsets


_MONTH_LENGTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _first_weekday(y: int, m: int, weekday: int) -> int:
    """Day of month of the first ``weekday`` (Mon=0) in that month."""
    # 1970-01-01 was a Thursday, which is 3 with Monday as 0.
    w1 = (_days_from_civil(y, m, 1) + 3) % 7
    return 1 + (weekday - w1) % 7


def _last_weekday(y: int, m: int, weekday: int) -> int:
    """Day of month of the last ``weekday`` (Mon=0) in that month."""
    days = _MONTH_LENGTH[m - 1]
    if m == 2 and (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
        days = 29
    last = (_days_from_civil(y, m, days) + 3) % 7
    return days - (last - weekday) % 7


def dst_window(year: int) -> tuple[int, int]:
    """The daylight-saving window, as naive seconds on HistData's own clock.

    Last Sunday in March to last Sunday in October — the European calendar,
    which has been unchanged since 1996 and so covers HistData's whole range.
    The 02:00 boundary is nominal; both transitions land in the weekend gap, so
    only the date can matter.
    """
    SUN = 6
    start = _last_weekday(year, 3, SUN)
    end = _last_weekday(year, 10, SUN)
    return (_days_from_civil(year, 3, start) * 86400 + 2 * 3600,
            _days_from_civil(year, 10, end) * 86400 + 2 * 3600)


def _days_from_civil(y: int, m: int, d: int) -> int:
    """Howard Hinnant's civil-to-days. Exact, and no datetime object per bar."""
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _download(page_url: str, code: str, year: int, point: float) -> np.ndarray:
    html = net.fetch(page_url)
    fields = _form_fields(html, code, year)

    blob = net.fetch(POST, data=urlencode(fields).encode(),
                     headers={"Referer": page_url,
                              "Content-Type": "application/x-www-form-urlencoded"})
    if not blob.startswith(b"PK"):
        raise net.NotFound(f"{page_url}: no archive returned")

    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise net.NotFound(f"{page_url}: archive holds no csv")
        raw = z.read(names[0])

    return _parse_csv(raw, point)


def fetch_month(code: str, year: int, month: int, point: float) -> np.ndarray:
    return _download(PAGE_MONTH.format(pair=code, year=year, month=month),
                     code, year, point)


def fetch_year(code: str, year: int, point: float) -> np.ndarray:
    """All M1 bars HistData holds for ``code`` in ``year``, already in UTC.

    A completed year is one archive. The **year in progress is not** — its
    page carries an empty download token and the data is only offered month by
    month — so that case falls back to twelve smaller requests and stitches
    them together. Without this the current year silently fails and the
    history quietly stops last December.
    """
    page_url = PAGE.format(pair=code, year=year)
    try:
        return _download(page_url, code, year, point)
    except RuntimeError as e:
        if "token" not in str(e):
            raise

    parts = []
    for month in range(1, 13):
        try:
            bars = fetch_month(code, year, month, point)
        except (net.NotFound, RuntimeError):
            continue          # month not published yet, or still to come
        if len(bars):
            parts.append(bars)
    if not parts:
        raise net.NotFound(f"{code} {year}: nothing published yet")
    return np.concatenate(parts)
