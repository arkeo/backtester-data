"""Download orchestration: plan the work, do it in parallel, never repeat it.

A download is a list of **units** — a year for HistData, a day for Dukascopy, a
month for Binance. Every unit that finishes is recorded in the instrument's
manifest, so an interrupted twelve-year fetch resumes exactly where it stopped
instead of starting again. Units that come back 404 are recorded too: a market
holiday is a fact about the world, not a failure to retry forever.

Concurrency is deliberately modest. Four workers against Dukascopy is safe,
twelve is not — past that the feed answers 429 to everything and the run gets
slower, not faster.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import catalog, paths, store
from .sources import binance, dukascopy, histdata, net

WORKERS = 4
# Bars are accumulated in memory and folded into the store in batches. Merging
# after every unit would rewrite the whole M1 file thousands of times.
FLUSH_EVERY = 400

# A full sweep of every forex pair back to 2000 is around sixteen gigabytes, so
# a long unattended run has to be able to stop itself. Filling someone's system
# drive is a worse outcome than an incomplete history.
MIN_FREE_BYTES = 8 * 1024 ** 3


def free_space() -> int:
    try:
        target = paths.data_dir()
        while target and not os.path.isdir(target):
            parent = os.path.dirname(target)
            if parent == target:
                break
            target = parent
        return shutil.disk_usage(target).free
    except OSError:
        return MIN_FREE_BYTES * 2      # cannot tell; do not block on it


def manifest_path(symbol: str) -> str:
    return os.path.join(store.sym_dir(symbol), "manifest.json")


def read_manifest(symbol: str) -> dict:
    try:
        with open(manifest_path(symbol), "r", encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        m = {}
    m.setdefault("done", [])
    m.setdefault("empty", [])
    return m


def write_manifest(symbol: str, m: dict) -> None:
    os.makedirs(store.sym_dir(symbol), exist_ok=True)
    tmp = manifest_path(symbol) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f)
    os.replace(tmp, manifest_path(symbol))


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def plan(symbol: str, start: date | None = None, end: date | None = None) -> list[str]:
    """Every unit key needed to cover ``start``..``end`` for this instrument."""
    inst = catalog.get(symbol)
    today = datetime.now(timezone.utc).date()
    start = start or date(inst.first_year, 1, 1)
    end = min(end or today, today)
    if start.year < inst.first_year:
        start = date(inst.first_year, 1, 1)

    if inst.source == "histdata":
        return [str(y) for y in range(start.year, end.year + 1)]
    if inst.source == "binance":
        return [f"{y:04d}-{m:02d}" for y, m in binance.months_in(start, end)]
    if inst.source == "dukascopy":
        return [d.isoformat() for d in dukascopy.days_in(start, end)]
    raise ValueError(f"unknown source {inst.source!r}")


def volatile(symbol: str) -> set[str]:
    """Units that are still growing and so must never be considered finished.

    The current year's HistData archive gains a month every month, this
    month's Binance file gains a day every day, and the last few Dukascopy
    days may have been incomplete when they were fetched. Treating any of them
    as done freezes the history at whatever was published the first time.
    """
    inst = catalog.get(symbol)
    today = datetime.now(timezone.utc).date()
    if inst.source == "histdata":
        return {str(today.year)}
    if inst.source == "binance":
        return {f"{today.year:04d}-{today.month:02d}"}
    return {(today - timedelta(days=n)).isoformat() for n in range(4)}


def pending(symbol: str, units: list[str]) -> list[str]:
    m = read_manifest(symbol)
    known = (set(m["done"]) | set(m["empty"])) - volatile(symbol)
    return [u for u in units if u not in known]


# --------------------------------------------------------------------------
# fetching one unit
# --------------------------------------------------------------------------

class Health:
    """Whether a source is answering at all.

    HistData is one website with one page layout. If it is redesigned, or is
    simply down, every request fails only after six retries with backoff — so
    a run against a dead source spends minutes per unit discovering the same
    fact over and over. After a few consecutive failures the source is treated
    as down and skipped, and one success brings it straight back.
    """

    THRESHOLD = 3

    def __init__(self):
        self._lock = threading.Lock()
        self.failures = 0

    @property
    def down(self) -> bool:
        return self.failures >= self.THRESHOLD

    def record(self, ok: bool) -> None:
        with self._lock:
            self.failures = 0 if ok else self.failures + 1


HISTDATA = Health()


def sources_for(inst, histdata_down: bool) -> list[str]:
    """Which sources to try for this instrument, best first.

    Dukascopy is a genuinely independent path to the same bars — it was
    checked against HistData across nine days spanning both daylight-saving
    changeovers and every close matched exactly — so anything with a Dukascopy
    symbol has a second way in. It is much slower, one request per day against
    one per year, which is why it is the fallback rather than the default.
    """
    if inst.source == "histdata" and inst.duka:
        return ["dukascopy", "histdata"] if histdata_down else ["histdata", "dukascopy"]
    return [inst.source]


def _fetch_from(source: str, inst, unit: str) -> np.ndarray:
    if source == "histdata":
        return histdata.fetch_year(inst.code, int(unit), inst.point)
    if source == "binance":
        y, m = unit.split("-")
        return binance.fetch_month(inst.code, int(y), int(m), inst.point)
    if source == "dukascopy":
        if inst.source == "dukascopy":
            d = date.fromisoformat(unit)
            return dukascopy.fetch_day(inst.duka or inst.code, d, inst.point,
                                       dukascopy.scale_for(inst))
        # Standing in for a HistData year, so the whole year has to be walked
        # a day at a time.
        return _dukascopy_year(inst, int(unit))
    raise ValueError(source)


def _dukascopy_year(inst, year: int) -> np.ndarray:
    """One HistData year rebuilt from Dukascopy's per-day files."""
    today = datetime.now(timezone.utc).date()
    start = date(year, 1, 1)
    end = min(date(year, 12, 31), today)
    if start > end:
        raise net.NotFound(f"{inst.symbol} {year}: in the future")

    scale = dukascopy.scale_for(inst)
    days = list(dukascopy.days_in(start, end))
    parts: list[np.ndarray] = []

    def one(day):
        try:
            return dukascopy.fetch_day(inst.duka, day, inst.point, scale)
        except net.NotFound:
            return None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for bars in pool.map(one, days):
            if bars is not None and len(bars):
                parts.append(bars)

    if not parts:
        raise net.NotFound(f"{inst.symbol} {year}: nothing on Dukascopy either")
    return np.concatenate(parts)


def _fetch_unit(inst, unit: str, prog: "Progress | None" = None) -> np.ndarray:
    attempts = sources_for(inst, HISTDATA.down)
    last: Exception | None = None

    for i, source in enumerate(attempts):
        try:
            bars = _fetch_from(source, inst, unit)
            if source == "histdata":
                HISTDATA.record(True)
            if i > 0 and prog is not None:
                with prog.lock:
                    prog.message = f"{attempts[0]} unavailable, using {source}"
            return bars
        except net.NotFound:
            # The period genuinely does not exist at this source. That is an
            # answer, not a failure, and it does not mean the site is down.
            if source == "histdata":
                HISTDATA.record(True)
            raise
        except Exception as e:                      # noqa: BLE001
            if source == "histdata":
                HISTDATA.record(False)
            last = e

    raise last if last else RuntimeError(f"no source for {inst.symbol}")


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

class Progress:
    """Thread-safe snapshot of a running download, for the UI to poll."""

    def __init__(self, symbol: str, total: int):
        self.lock = threading.Lock()
        self.symbol = symbol
        self.total = total
        self.done = 0
        self.bars = 0
        self.failed: list[str] = []
        self.blocked = 0            # units refused outright, usually on location
        self.started = time.time()
        self.finished = False
        self.cancelled = False
        self.message = ""

    @property
    def ok(self) -> bool:
        """Did this actually achieve anything?

        A run where every single unit failed used to finish at 100% and report
        success, because progress counted attempts rather than results. On a
        network where the sources are unreachable that is the worst possible
        behaviour: the bar fills, the download says it is done, and there is no
        history and no error.
        """
        return not (self.failed and self.bars == 0)

    def snapshot(self) -> dict:
        with self.lock:
            elapsed = time.time() - self.started
            rate = self.done / elapsed if elapsed > 0 else 0
            left = (self.total - self.done) / rate if rate > 0 else None
            return {
                "symbol": self.symbol, "total": self.total, "done": self.done,
                "bars": self.bars, "failed": list(self.failed),
                "elapsed": round(elapsed, 1),
                "eta": round(left) if left is not None else None,
                "finished": self.finished, "cancelled": self.cancelled,
                "message": self.message,
                "blocked": self.blocked,
                "ok": self.ok,
            }


def download(symbol: str, start: date | None = None, end: date | None = None,
             workers: int = WORKERS, progress: Progress | None = None,
             deadline: float | None = None) -> dict:
    """Fetch everything missing for ``symbol`` and fold it into the store."""
    inst = catalog.get(symbol)
    units = pending(symbol, plan(symbol, start, end))
    prog = progress or Progress(symbol, len(units))
    with prog.lock:
        prog.total = len(units)

    if not units:
        with prog.lock:
            prog.finished = True
            prog.message = "already complete"
        return store.refresh_meta(symbol)

    manifest = read_manifest(symbol)
    done, empty = set(manifest["done"]), set(manifest["empty"])
    buffer: list[np.ndarray] = []
    buf_lock = threading.Lock()

    def flush():
        with buf_lock:
            batch, buffer[:] = list(buffer), []
        if not batch:
            return
        merged = np.concatenate(batch)
        total = store.merge_m1(symbol, merged)
        # Rebuild the derived timeframes and the summary on every batch, not
        # only at the end. A long download that is interrupted — closed window,
        # power cut, cancelled — otherwise leaves M1 bars on disk with no
        # meta.json and no higher timeframes, which the application reads as
        # "nothing downloaded". The bars were there; they just could not be
        # seen.
        store.refresh_meta(symbol)
        with prog.lock:
            prog.bars = total
        write_manifest(symbol, {"done": sorted(done), "empty": sorted(empty)})

    def work(unit: str):
        if prog.cancelled:
            return
        if deadline and time.time() > deadline:
            # Checked here, per unit, and not only per batch. A batch is up to
            # four hundred units and a single unit can take minutes when a
            # source is failing and the fallback is walking a year day by day,
            # so a batch-level check can overshoot the budget many times over.
            with prog.lock:
                prog.cancelled = True
                prog.message = "stopped: out of time for this run"
            return
        try:
            bars = _fetch_unit(inst, unit, prog)
            with buf_lock:
                if len(bars):
                    buffer.append(bars)
                done.add(unit)
        except net.NotFound:
            empty.add(unit)                    # holiday, or before listing
        except net.Blocked as e:
            with prog.lock:
                prog.blocked += 1
                prog.failed.append(f"{unit}: {e}")
        except Exception as e:                 # noqa: BLE001 - reported, not raised
            with prog.lock:
                prog.failed.append(f"{unit}: {e}")
        finally:
            with prog.lock:
                prog.done += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(units), FLUSH_EVERY):
            if prog.cancelled:
                break
            if free_space() < MIN_FREE_BYTES:
                with prog.lock:
                    prog.cancelled = True
                    prog.message = (f"stopped: only {free_space() / 1e9:.1f} GB free")
                break
            if deadline and time.time() > deadline:
                # Out of time rather than out of work. Everything fetched so
                # far is kept and the manifest records it, so the next run
                # carries on from here.
                with prog.lock:
                    prog.cancelled = True
                    prog.message = "stopped: out of time for this run"
                break
            chunk = units[i:i + FLUSH_EVERY]
            list(pool.map(work, chunk))
            flush()

    flush()
    meta = store.refresh_meta(symbol)
    with prog.lock:
        prog.finished = True
        prog.bars = meta.get("bars", 0)
        if prog.failed and not prog.message:
            prog.message = _diagnose(inst, prog)
    return meta


def _diagnose(inst, prog: "Progress") -> str:
    """Say what went wrong in terms the person in front of it can act on."""
    if prog.blocked:
        return (f"{inst.source} refused the request ({prog.blocked} of "
                f"{prog.done}). This usually means the country is blocked. "
                f"Set a proxy in Settings and test the connection.")
    if prog.bars == 0:
        return (f"{len(prog.failed)} of {prog.done} requests to {inst.source} "
                f"failed and nothing was downloaded. Test the connection in "
                f"Settings to see which sources are reachable.")
    return f"{len(prog.failed)} of {prog.done} requests failed; the rest arrived."



# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

#: What to fetch first when a run cannot fetch everything. The rest follow in
#: catalogue order behind these.
PRIORITY = [
    "EURUSD", "XAUUSD", "GBPUSD", "USDJPY", "US30", "BTCUSD",
    "US500", "USTEC", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD",
    "ETHUSD", "XAGUSD", "USOIL", "DE40", "EURJPY", "GBPJPY",
]


def priority(symbol: str) -> tuple[int, int]:
    try:
        return (0, PRIORITY.index(symbol))
    except ValueError:
        order = [i.symbol for i in catalog.INSTRUMENTS]
        return (1, order.index(symbol) if symbol in order else 999)


def _cli():
    import argparse

    ap = argparse.ArgumentParser(description="Download market history.")
    ap.add_argument("symbols", nargs="*",
                    help="instrument symbols, or a group name, or 'all'")
    ap.add_argument("--from", dest="start", help="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", help="YYYY-MM-DD")
    ap.add_argument("--years", type=int, help="just the last N years")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--minutes", type=float, default=0,
                    help="stop starting new work after this long, keeping "
                         "everything fetched so far. A scheduled run needs "
                         "this: the whole catalogue does not fit in one go, "
                         "and a run killed by a timeout publishes nothing.")
    ap.add_argument("--list", action="store_true", help="show the catalog and exit")
    args = ap.parse_args()

    if args.list or not args.symbols:
        for g in catalog.GROUPS:
            members = [i for i in catalog.INSTRUMENTS if i.group == g]
            print(f"\n{g} ({len(members)})")
            for i in members:
                print(f"  {i.symbol:9s} {i.name:34s} {i.source:10s} from {i.first_year}")
        return

    wanted: list[str] = []
    for s in args.symbols:
        low = s.lower()
        if low == "all":
            wanted += [i.symbol for i in catalog.INSTRUMENTS]
        elif low in catalog.GROUPS:
            wanted += [i.symbol for i in catalog.INSTRUMENTS if i.group == low]
        else:
            wanted.append(s.upper())

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    if args.years:
        start = datetime.now(timezone.utc).date() - timedelta(days=365 * args.years)

    # Fetch the instruments people actually ask for first.
    #
    # A run on a budget gets through only part of the catalogue, and plain
    # catalogue order put all forty-eight forex pairs — including the Czech
    # koruna — ahead of gold, the Dow and Bitcoin. Whatever the budget covers
    # should be the useful part.
    wanted = sorted(dict.fromkeys(wanted), key=priority)

    deadline = time.time() + args.minutes * 60 if args.minutes else None

    for sym in wanted:
        if deadline and time.time() > deadline:
            print(f"\n=== out of time; {sym} and the rest are left for the "
                  f"next run ===", flush=True)
            break
        units = pending(sym, plan(sym, start, end))
        print(f"\n=== {sym}: {len(units)} units to fetch ===", flush=True)
        prog = Progress(sym, len(units))

        stop = threading.Event()

        def tick():
            while not stop.wait(2.0):
                s = prog.snapshot()
                eta = f"{s['eta']}s" if s["eta"] is not None else "?"
                print(f"  {s['done']}/{s['total']}  bars={s['bars']:,}  eta {eta}",
                      end="\r", flush=True)

        t = threading.Thread(target=tick, daemon=True)
        t.start()
        try:
            meta = download(sym, start, end, args.workers, prog, deadline)
        finally:
            stop.set()
            t.join(timeout=1)

        first = store.from_unix(meta["first"]).date() if meta.get("first") else "-"
        last = store.from_unix(meta["last"]).date() if meta.get("last") else "-"
        print(f"  {meta.get('bars', 0):,} M1 bars  {first} .. {last}"
              f"  {meta.get('bytes', 0) / 1e6:.1f} MB"
              f"  spread={'yes' if meta.get('has_spread') else 'no'}")
        if prog.failed:
            print(f"  {len(prog.failed)} failed, first: {prog.failed[0]}")


if __name__ == "__main__":
    _cli()
