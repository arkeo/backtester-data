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


def _checked_at(entry: dict) -> float:
    return entry.get("refreshed") or entry.get("last") or 0


def _age_hours(entry: dict) -> float:
    """How long since this instrument was last *looked at*.

    Not how old its newest bar is. Those are different questions and using the
    second for the first meant every instrument was permanently overdue: the
    newest bar on a Friday close is two days old all weekend, so a catalogue
    that was completely up to date still reported all sixty-three as stale and
    re-fetched, re-sealed and re-uploaded three gigabytes of it every three
    hours, for nothing.
    """
    return (time.time() - _checked_at(entry)) / 3600.0


def prune(index: dict, tag: str, do_upload: bool) -> list[str]:
    """Take off the mirror anything the application fetches for itself.

    Not merely "stop publishing it": a bundle that was published once stays on
    the release until something removes it, and the listing would go on
    offering it. Left alone, customers would keep taking the slower copy of
    something they can get directly.
    """
    from . import crypt
    gone = [s for s in index if catalog.is_direct(s)]
    for symbol in gone:
        # Every file the entry owned. An instrument is a set of year files now,
        # and deleting only the first would leave the rest on the release for
        # ever, paid for and pointing at nothing.
        for name in portable.files_in(index.pop(symbol)):
            if do_upload:
                subprocess.run(["gh", "release", "delete-asset", tag, name,
                                "--yes"], check=False)
        print(f"  removed {symbol} from the mirror")
    return gone


#: How many units a backfill must manage before it is worth attempting again.
#:
#: A day-served source refuses this machine's addresses outright — measured:
#: thirty-nine attempts in twenty-five minutes and not one bar, against
#: fifty-one days in seven minutes from an ordinary connection. Retrying that
#: every three hours costs the whole slice and achieves nothing, and worse, it
#: hides the fact that the instrument is not being filled at all behind a run
#: that looks busy. Recorded once, it is skipped until the backlog moves,
#: which is what happens when the history is published from somewhere that can
#: reach the source.
STALLED_AFTER = 2


def shut_all_day(symbol: str, entry: dict | None, now: float | None = None) -> bool:
    """True when nothing can have traded since this instrument was last looked at.

    There is exactly one case worth acting on and it is provable rather than
    guessed. Only whole days are published, so on a Sunday the newest
    publishable day is the Saturday — and nothing in this catalogue except
    crypto trades on a Saturday, anywhere, ever. So a Sunday run over sixty-odd
    instruments fetches sixty-odd times and finds nothing sixty-odd times, and
    that is a seventh of everything this job costs on both ends.

    Market *hours* are deliberately not modelled. Friday's close moves with
    American daylight saving and Sunday's open with it, so anything finer than
    "Saturdays are shut" would be a guess that silently drops real bars four
    weeks a year — the same class of mistake as reading HistData's stamps as a
    fixed offset.

    Guarded on having actually looked since Friday ended. If the Saturday run
    was missed, Friday is still unpublished, and skipping on the Sunday too
    would hold it back until Monday.
    """
    if catalog.is_direct(symbol):
        return False                     # crypto does not close
    at = datetime.fromtimestamp(
        time.time() if now is None else now, timezone.utc)
    if at.weekday() != 6:                # Monday is 0, so this is Sunday
        return False
    saturday = portable.day_start(now) - 86400
    return _checked_at(entry or {}) >= saturday


def choose(symbols: list[str], index: dict) -> list[str]:
    """Which instruments this run should touch, most useful first.

    Backfilling comes before topping up. An instrument nobody can download at
    all is a worse problem than one whose last day is missing, and the run has
    a time budget, so the order decides what a short run achieves.
    """
    missing, stale = [], []
    for symbol in symbols:
        entry = index.get(symbol)
        # A shut market has no *new* bars — but backfilling is not about new
        # bars. An instrument still reaching back to 2000 has years of units
        # to fetch and a Sunday is as good a day as any to fetch them, so the
        # skip applies only once an instrument is complete and merely being
        # topped up.
        if (entry is not None and not entry.get("backlog")
                and shut_all_day(symbol, entry)):
            continue
        if entry is None or entry.get("backlog", 1) > 0:
            if entry and entry.get("stalled", 0) >= STALLED_AFTER:
                # Still needs the history; this machine simply cannot fetch it.
                # It is topped up with the newest days like anything else,
                # which is affordable, and the backfill comes from elsewhere.
                if _age_hours(entry) > REFRESH_HOURS:
                    stale.append(symbol)
                continue
            missing.append(symbol)
        elif not entry.get("parts") or _unsealed(entry):
            # Published before the archive was cut into pieces, or before it
            # was sealed with a content key. Due regardless of age: the first
            # costs every mirror the whole file nightly, and the second means
            # the history opens without a licence. Both are self-limiting —
            # once done neither matches again.
            stale.append(symbol)
        elif _age_hours(entry) > REFRESH_HOURS:
            stale.append(symbol)

    missing.sort(key=fetch.priority)
    # Oldest first, so nothing can be left behind indefinitely by a run that
    # only ever gets through the front of the list.
    stale.sort(key=lambda s: _checked_at(index[s]))
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
    scratch = os.path.join(paths.root(), "incoming")
    os.makedirs(scratch, exist_ok=True)
    # Every piece: an instrument is a set of year files, and restoring only the
    # first would leave this run believing the other years were never fetched —
    # so it would fetch them all again from the sources, which is the whole
    # cost this job exists to avoid.
    for name in portable.files_in(entry):
        tmp = os.path.join(scratch, name)
        try:
            net.download(f"{base_url.rstrip('/')}/{name}", tmp,
                         timeout=180, attempts=3)
            portable.import_bundle(tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return store.bar_count(symbol)


def _unsealed(entry: dict | None) -> bool:
    """Whether any piece of this instrument still opens without a licence.

    True while a piece carries no sealing period, which means it was written
    with the publisher key — the one compiled into every copy of the
    application. Self-limiting in exactly the way the piece-split migration
    was: once every piece has a period, this never matches again.
    """
    parts = (entry or {}).get("parts") or []
    return any(not p.get("key") for p in parts)


def _stale(old: dict | None, new: dict) -> bool:
    """Whether a piece has to go up again.

    Almost always the answer is "only if the bars changed", and it has to stay
    that way: a piece re-sealed for any other reason draws a fresh nonce, and
    every mirror downstream reads the new bytes as new data. Re-sealing all
    three pieces monthly, when the content key rotates, would push the whole
    3.4 GB catalogue every month and undo the entire point of splitting it.

    The one exception is the switch-on. Everything published before content
    keys existed is sealed with the publisher key, which is compiled into every
    copy of the application — so it opens with or without a licence. Those
    pieces are re-sealed **once**, the first time a monthly key is in force,
    and then never again for this reason: a piece keeps whichever month sealed
    it, and a licence carries every period up to its expiry, so a bundle sealed
    in 2026 still opens for somebody who subscribes in 2028.

    Without this the archive would have stayed readable by anyone until it
    happened to change on its own — which for the pre-2026 piece is 1 January.
    """
    if old is None:
        return True
    if old.get("sha") != new.get("sha"):
        return True
    # A piece that is about to be published under a different name has to go
    # up under it, whatever else is true. Nothing else would put it there, and
    # the tidying step below deletes every name this instrument used to have —
    # so without this a rename would remove the old file and never upload the
    # new one, which is not a stale mirror but a missing instrument.
    if old.get("file") != new.get("file"):
        return True
    return not old.get("key") and bool(new.get("key"))


def seal(symbol: str, out_dir: str, was: dict | None = None,
         gained: int = 0) -> dict | None:
    """Write the sealed bundle for one instrument and describe it."""
    meta = store.read_meta(symbol)
    if not meta.get("bars"):
        return None
    left = backlog(symbol)
    # Three pieces — archive, year, month — not one file for the instrument.
    #
    # A single file was rewritten whole for every new trading day, so everyone
    # mirroring re-fetched all of it nightly to gain a day: over a thousand
    # bytes moved per byte gained, and more than the server's whole monthly
    # allowance. Only the month moves daily now, and it is small.
    r = portable.publish_one(symbol, out_dir, seal=True)
    return {
        "symbol": symbol,
        "parts": r["parts"],
        "bytes": r["bytes"],
        # From what was published, not from the store - see publish_one.
        "bars": r["bars"],
        "first": r.get("first") or meta.get("first"),
        "last": r.get("last") or meta.get("last"),
        "has_spread": meta.get("has_spread", False),
        # Not read by the application. It is how the next run knows whether
        # this instrument is finished being backfilled without downloading it
        # first to look.
        "backlog": left,
        # From when real quotes inside the minute can be had.
        #
        # The tick files come from the day-served feed, which begins in 2003
        # and carries every instrument in the catalogue except the coins. So
        # the answer is the later of that year and the instrument's own start
        # — and it is recorded here rather than probed by the application,
        # which would be thousands of requests to learn something that changes
        # once a decade.
        "ticks_from": _ticks_from(symbol, meta),
        # Consecutive runs that tried to backfill this and got nowhere. Reset
        # by any progress at all, so a source having a bad hour costs one
        # slice rather than being written off.
        #
        # Only counted while there is a backlog: an instrument that is already
        # complete gains nothing on a quiet weekend and is not stalled, it is
        # finished. Counting those marked the whole catalogue as stuck.
        "stalled": 0 if (gained > 0 or not left)
                   else (was or {}).get("stalled", 0) + 1,
        # When this was last checked, which is what staleness means.
        "refreshed": int(time.time()),
    }


def _ticks_from(symbol: str, meta: dict) -> int | None:
    """The first year this instrument has ticks for, or None if it has none."""
    inst = catalog.get(symbol)
    if not (inst.duka or inst.source == "dukascopy"):
        return None
    first = meta.get("first")
    began = store.from_unix(first).year if first else fetch.DAY_FEED_FROM
    return max(fetch.DAY_FEED_FROM, began)


def forget(symbol: str) -> None:
    shutil.rmtree(store.sym_dir(symbol), ignore_errors=True)


def upload(path: str, tag: str) -> None:
    subprocess.run(["gh", "release", "upload", tag, path, "--clobber"],
                   check=True)


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def slice_for(deadline: float | None, remaining: int = 1) -> float | None:
    """When this instrument must hand the run on.

    The cap is a floor, not a ceiling. Its job is to stop one slow instrument
    starving the queue behind it — so when there is barely a queue left there
    is nothing to protect, and holding the Dow to twenty-five minutes of a
    two-and-a-half hour run would just leave the machine idle. It takes the
    larger of its cap and its fair share of what is left.
    """
    now = time.time()
    if not deadline:
        return now + PER_SYMBOL_MINUTES * 60
    share = (deadline - now) / max(1, remaining)
    return min(now + max(PER_SYMBOL_MINUTES * 60, share), deadline)


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

    symbols = [s for s in symbols if not catalog.is_direct(s)]
    dropped = prune(index, tag, do_upload)
    if dropped:
        write_index(index, out_dir)
        if do_upload:
            upload(os.path.join(out_dir, portable.SEALED_INDEX), tag)

    todo = choose(symbols, index)
    print(f"\n{len(todo)} of {len(symbols)} instruments need work this run")
    if todo:
        print("  " + " ".join(todo[:12]) + (" ..." if len(todo) > 12 else ""))

    deadline = time.time() + minutes * 60 if minutes else None
    touched, out_of_time = [], False

    for done_so_far, symbol in enumerate(todo):
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

            before = store.bar_count(symbol)

            # An instrument that is only here to change shape needs no fetching
            # at all. It was checked within the refresh window, so there is
            # nothing new to get — and asking anyway costs it the full
            # per-instrument slice waiting on a unit that never arrives, which
            # is what turned a re-seal of seconds into twenty-five minutes.
            only_shape = (bool(entry)
                          and (not entry.get("parts") or _unsealed(entry))
                          and not entry.get("backlog")
                          and _age_hours(entry) <= REFRESH_HOURS)
            if only_shape:
                print("  already current; re-sealing only", flush=True)
                prog = fetch.Progress(symbol, 0)
            else:
                prog = _with_ticker(
                    symbol, slice_for(deadline, len(todo) - done_so_far))

            fresh = seal(symbol, out_dir, was=entry,
                         gained=store.bar_count(symbol) - before)
            if fresh is None:
                print("  nothing to publish", flush=True)
                if prog.failed:
                    print(f"  {len(prog.failed)} failed, first: {prog.failed[0]}")
                continue

            # Only the years that actually changed. Re-uploading a year that
            # is byte-identical would push a new random nonce, which every
            # mirror downstream would read as new data — reintroducing the
            # very cost the split was made to remove.
            had = {p["part"]: p for p in (entry or {}).get("parts", [])}
            moved = [p for p in fresh["parts"] if _stale(had.get(p["part"]), p)]
            if do_upload:
                for p in moved:
                    upload(os.path.join(out_dir, p["file"]), tag)
                # The single file this instrument used to be. Nothing else
                # removes it: prune only drops instruments that left the
                # catalogue, so without this the superseded copy would stay on
                # the release for ever — paid for, downloaded by every mirror,
                # and pointing at data now published twice.
                # Whatever this instrument used to be published as, minus
                # what it is now. Nothing else removes these: prune only drops
                # instruments that left the catalogue, so a superseded file
                # would sit on the release for ever, downloaded by every
                # mirror and pointing at data now published twice.
                now_files = {p["file"] for p in fresh["parts"]}
                for old_name in portable.files_in(entry or {}):
                    if old_name in now_files:
                        continue
                    gone = subprocess.run(
                        ["gh", "release", "delete-asset", tag, old_name,
                         "--yes"], check=False, capture_output=True, text=True)
                    if gone.returncode:
                        # Said out loud. Swallowing this left two superseded
                        # files on the release, 73 MB, which nothing would ever
                        # have removed and nothing would ever have mentioned —
                        # the index stops naming them, so they become invisible
                        # rather than wrong.
                        print(f"  WARNING could not remove {old_name}: "
                              f"{gone.stderr.strip()[:80]}", flush=True)
                    else:
                        print(f"  removed superseded {old_name}", flush=True)
            index[symbol] = fresh
            touched.append(symbol)

            first = store.from_unix(fresh["first"]).date() if fresh["first"] else "-"
            last = store.from_unix(fresh["last"]).date() if fresh["last"] else "-"
            stalled = (f"  STALLED x{fresh['stalled']}" if fresh["stalled"] else "")
            print(f"  {fresh['bars']:,} bars  {first} .. {last}  "
                  f"{fresh['bytes'] / 1e6:.0f} MB in {len(fresh['parts'])} pieces, "
                  f"{'+'.join(p['part'] for p in moved) or 'none'} rewritten "
                  f"({sum(y['bytes'] for y in moved) / 1e6:.1f} MB uploaded)  "
                  f"backlog={fresh['backlog']}{stalled}"
                  f"  ({time.time() - started:.0f}s)", flush=True)
            if fresh["stalled"] >= STALLED_AFTER:
                print(f"::warning::{symbol} cannot be backfilled from this "
                      f"machine; publish it from one that can reach the source",
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

    if do_upload and dropped and not touched:
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
            wanted += catalog.mirrored()
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
