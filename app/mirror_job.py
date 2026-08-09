"""Keep the published mirror current — one instrument at a time.

    python -m app.mirror_job --base-url https://.../releases/download/history

Only the machine that publishes runs this. It is what the scheduled job calls.

Why one at a time
-----------------
The obvious shape is: keep the whole store on the build machine, top it up,
seal it, upload it. That is what this used to do, and it put a hard ceiling on
how much history could ever be offered — a full sweep of the catalogue is about
sixteen gigabytes, while the machine that runs the job has roughly fourteen
free and a ten gigabyte cache. The ceiling had nothing to do with the market
data and everything to do with where the work happened, which is a bad reason
to hand customers five years instead of twenty-six.

So the store is not kept between runs at all. For each instrument in turn:

    restore it from what is already published  ->  fetch what is missing  ->
    seal it  ->  upload it  ->  delete it

Peak disk is therefore one instrument, a few hundred megabytes, no matter how
large the catalogue grows. The published release *is* the saved state: the
bundles carry their own fetch manifests, so a run picks up exactly where the
last one stopped, and a run that is cut short loses nothing.

Nothing here is needed by the application a customer installs.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

from . import catalog, fetch, paths, portable, store
from .sources import net

#: How stale a published instrument may get before it is refreshed. A little
#: under a day, so an instrument comes round again once daily without the exact
#: hour drifting later and later.
REFRESH_HOURS = 20

#: The longest any one instrument may hold the run.
#:
#: Without this, the slowest instrument in the catalogue starves every other.
#: The Dow is fetched a day at a time — thousands of requests for its full
#: history — so it simply absorbed whole runs, and the mirror ended up with
#: currency pairs and no crypto at all, which is not a shortage of data but a
#: queue nobody else could get to the front of. Capped, it still finishes: it
#: keeps what it fetched, and takes its slice again next time.
PER_SYMBOL_MINUTES = 25


# --------------------------------------------------------------------------
# what is already out there
# --------------------------------------------------------------------------

def published(base_url: str) -> dict:
    """The index of what is on the mirror now, keyed by symbol."""
    try:
        index = portable.mirror_index(base_url)
    except Exception as e:                              # noqa: BLE001
        # First run, or an address that answers nothing yet. Either way there
        # is no state to build on, which is a starting point rather than a
        # failure.
        print(f"  nothing usable published yet ({e})")
        return {}
    entries = {e["symbol"]: e for e in index.get("instruments", [])}
    print(f"  {len(entries)} instruments already published")
    return entries


def backlog(symbol: str) -> int:
    """Units this instrument has never fetched.

    Deliberately not ``fetch.pending``: that one also returns the periods which
    are merely still growing — the current year, this month — and those are
    pending on every single run forever. What is wanted here is "how much
    history is missing", which is the count of units never looked at.
    """
    m = fetch.read_manifest(symbol)
    known = set(m["done"]) | set(m["empty"])
    return sum(1 for u in fetch.plan(symbol) if u not in known)


def _age_hours(entry: dict) -> float:
    last = entry.get("last") or 0
    return (time.time() - last) / 3600.0


def choose(symbols: list[str], index: dict) -> list[str]:
    """Which instruments this run should touch, most useful first.

    Backfilling comes before topping up. An instrument nobody can download at
    all is a worse problem than one whose last day is missing, and the run has
    a time budget, so the order decides what a short run achieves.
    """
    missing, stale = [], []
    for symbol in symbols:
        entry = index.get(symbol)
        if entry is None or entry.get("backlog", 1) > 0:
            missing.append(symbol)
        elif _age_hours(entry) > REFRESH_HOURS:
            stale.append(symbol)

    missing.sort(key=fetch.priority)
    # Oldest first, so nothing can be left behind indefinitely by a run that
    # only ever gets through the front of the list.
    stale.sort(key=lambda s: index[s].get("last") or 0)
    return missing + stale


# --------------------------------------------------------------------------
# one instrument
# --------------------------------------------------------------------------

def restore(symbol: str, base_url: str, entry: dict | None) -> int:
    """Put back what was published, so this run only fetches the difference."""
    if store.bar_count(symbol):
        return 0                                    # already here somehow
    if not entry:
        return 0                                    # never published
    url = f"{base_url.rstrip('/')}/{entry['file']}"
    scratch = os.path.join(paths.root(), "incoming")
    os.makedirs(scratch, exist_ok=True)
    tmp = os.path.join(scratch, entry["file"])
    try:
        net.download(url, tmp, timeout=180, attempts=3)
        portable.import_bundle(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return store.bar_count(symbol)


def seal(symbol: str, out_dir: str) -> dict | None:
    """Write the sealed bundle for one instrument and describe it."""
    meta = store.read_meta(symbol)
    if not meta.get("bars"):
        return None
    from . import crypt
    name = crypt.name_for(symbol, portable.publisher_key())
    r = portable.export([symbol], os.path.join(out_dir, name), seal=True)
    return {
        "symbol": symbol,
        "file": name,
        "bytes": r["bytes"],
        "bars": meta["bars"],
        "first": meta.get("first"),
        "last": meta.get("last"),
        "has_spread": meta.get("has_spread", False),
        # Not read by the application. It is how the next run knows whether
        # this instrument is finished being backfilled without downloading it
        # first to look.
        "backlog": backlog(symbol),
    }


def forget(symbol: str) -> None:
    shutil.rmtree(store.sym_dir(symbol), ignore_errors=True)


def upload(path: str, tag: str) -> None:
    subprocess.run(["gh", "release", "upload", tag, path, "--clobber"],
                   check=True)


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def slice_for(deadline: float | None) -> float | None:
    """When this instrument must hand the run on, whichever comes first."""
    mine = time.time() + PER_SYMBOL_MINUTES * 60
    return min(mine, deadline) if deadline else mine


def _with_ticker(symbol: str, deadline: float | None):
    """Run one download, printing a line every few seconds.

    A scheduled run that prints nothing for forty minutes is indistinguishable
    from a hung one, and the only way to tell is to cancel it — which throws
    the work away.
    """
    units = fetch.pending(symbol, fetch.plan(symbol))
    prog = fetch.Progress(symbol, len(units))
    stop = threading.Event()

    def tick():
        while not stop.wait(15.0):
            s = prog.snapshot()
            print(f"    {s['done']}/{s['total']} units  {s['bars']:,} bars",
                  flush=True)

    t = threading.Thread(target=tick, daemon=True)
    t.start()
    try:
        fetch.download(symbol, progress=prog, deadline=deadline)
    finally:
        stop.set()
        t.join(timeout=1)
    return prog


def run(base_url: str, out_dir: str, symbols: list[str], minutes: float,
        tag: str = "history", do_upload: bool = True) -> dict:
    if portable.publisher_key() == portable.DEFAULT_KEY:
        raise SystemExit(
            "refusing to publish with the default key. Set BACKTESTER_KEY.")

    os.makedirs(out_dir, exist_ok=True)
    print("=== what is published now ===", flush=True)
    index = published(base_url)

    todo = choose(symbols, index)
    print(f"\n{len(todo)} of {len(symbols)} instruments need work this run")
    if todo:
        print("  " + " ".join(todo[:12]) + (" ..." if len(todo) > 12 else ""))

    deadline = time.time() + minutes * 60 if minutes else None
    touched, out_of_time = [], False

    for symbol in todo:
        if deadline and time.time() > deadline:
            out_of_time = True
            print(f"\n=== out of time; {len(todo) - len(touched)} left for the "
                  f"next run ===", flush=True)
            break

        started = time.time()
        entry = index.get(symbol)
        print(f"\n=== {symbol} ===", flush=True)
        try:
            had = restore(symbol, base_url, entry)
            if had:
                print(f"  restored {had:,} bars from the mirror", flush=True)

            prog = _with_ticker(symbol, slice_for(deadline))

            fresh = seal(symbol, out_dir)
            if fresh is None:
                print("  nothing to publish", flush=True)
                if prog.failed:
                    print(f"  {len(prog.failed)} failed, first: {prog.failed[0]}")
                continue

            path = os.path.join(out_dir, fresh["file"])
            if do_upload:
                upload(path, tag)
            index[symbol] = fresh
            touched.append(symbol)

            first = store.from_unix(fresh["first"]).date() if fresh["first"] else "-"
            last = store.from_unix(fresh["last"]).date() if fresh["last"] else "-"
            print(f"  {fresh['bars']:,} bars  {first} .. {last}  "
                  f"{fresh['bytes'] / 1e6:.0f} MB  "
                  f"backlog={fresh['backlog']}  ({time.time() - started:.0f}s)",
                  flush=True)

            # The listing goes up after every instrument rather than at the
            # end. A run that dies half way then still leaves a mirror that
            # describes itself correctly, which matters because a customer
            # reads the listing before anything else.
            write_index(index, out_dir)
            if do_upload:
                upload(os.path.join(out_dir, portable.SEALED_INDEX), tag)

            os.remove(path)
        except Exception as e:                          # noqa: BLE001
            # One bad instrument must not cost the run. A source can change a
            # URL, a single archive can be corrupt; the rest of the catalogue
            # is unaffected and should still be published.
            print(f"::warning::{symbol} failed: {e}", flush=True)
        finally:
            forget(symbol)

    write_index(index, out_dir)
    if do_upload and touched:
        upload(os.path.join(out_dir, portable.SEALED_INDEX), tag)

    total_bars = sum(e.get("bars", 0) for e in index.values())
    total_bytes = sum(e.get("bytes", 0) for e in index.values())
    left = sum(1 for e in index.values() if e.get("backlog", 0) > 0)
    print(f"\n=== mirror now ===")
    print(f"  {len(index)} instruments, {total_bars:,} bars, "
          f"{total_bytes / 1e6:.0f} MB")
    print(f"  {len(touched)} updated this run, {left} still backfilling")
    return {"published": len(index), "touched": touched,
            "backfilling": left, "out_of_time": out_of_time}


def write_index(index: dict, out_dir: str) -> None:
    portable.write_index(sorted(index.values(), key=lambda e: e["symbol"]),
                         out_dir, seal=True)


def _cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("symbols", nargs="*", default=["all"],
                    help="symbols, a group name, or 'all'")
    ap.add_argument("--base-url", required=True,
                    help="where the published bundles are readable from")
    ap.add_argument("--to", default="mirror", help="scratch folder for bundles")
    ap.add_argument("--tag", default="history", help="the release tag")
    ap.add_argument("--minutes", type=float, default=45)
    ap.add_argument("--no-upload", action="store_true",
                    help="build everything but leave the release alone")
    args = ap.parse_args()

    wanted: list[str] = []
    for s in (args.symbols or ["all"]):
        low = s.lower()
        if low == "all":
            wanted += [i.symbol for i in catalog.INSTRUMENTS]
        elif low in catalog.GROUPS:
            wanted += [i.symbol for i in catalog.INSTRUMENTS if i.group == low]
        else:
            wanted.append(s.upper())
    wanted = sorted(dict.fromkeys(wanted), key=fetch.priority)

    started = datetime.now(timezone.utc)
    r = run(args.base_url, args.to, wanted, args.minutes, args.tag,
            do_upload=not args.no_upload)
    print(f"\n  {(datetime.now(timezone.utc) - started).seconds // 60} minutes")
    return r


if __name__ == "__main__":
    _cli()
