"""Keep only the recent part of an instrument's history, and free the rest.

    python -m app.trim EURUSD --years 2
    python -m app.trim all --years 5
    python -m app.trim --list

A full sweep of the catalogue is around sixteen gigabytes, and most of it is
depth nobody is using. This rewrites M1 keeping only the last N years, rebuilds
the derived timeframes, and rewrites the manifest so the discarded years are
simply "not downloaded" again rather than being silently treated as complete —
without that last part they could never be fetched back.
"""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import catalog, fetch, store


def trim(symbol: str, years: float) -> dict:
    """Drop everything older than ``years`` ago. Returns before/after sizes."""
    symbol = symbol.upper()
    before = _size(symbol)
    cutoff = datetime.now(timezone.utc) - timedelta(days=365.25 * years)
    cut = int(cutoff.timestamp())

    m1 = store.load(symbol, "M1")
    if not len(m1):
        return {"symbol": symbol, "before": before, "after": before, "bars": 0}

    keep = m1[m1["t"] >= cut]
    if len(keep) == len(m1):
        return {"symbol": symbol, "before": before, "after": before,
                "bars": len(m1), "unchanged": True}

    tmp = store.bar_path(symbol, "M1") + ".tmp"
    np.ascontiguousarray(keep).tofile(tmp)
    store._replace(tmp, store.bar_path(symbol, "M1"))
    store.refresh_meta(symbol)
    _forget_before(symbol, cutoff.date())

    return {"symbol": symbol, "before": before, "after": _size(symbol),
            "bars": len(keep), "dropped": len(m1) - len(keep)}


def _forget_before(symbol: str, cutoff: date) -> None:
    """Remove discarded periods from the manifest so they can be fetched again."""
    inst = catalog.get(symbol)
    manifest = fetch.read_manifest(symbol)

    def stale(unit: str) -> bool:
        try:
            if inst.source == "histdata":
                return int(unit) < cutoff.year
            if inst.source == "binance":
                y, m = unit.split("-")
                return date(int(y), int(m), 1) < cutoff.replace(day=1)
            return date.fromisoformat(unit) < cutoff
        except ValueError:
            return False

    fetch.write_manifest(symbol, {
        "done": sorted(u for u in manifest["done"] if not stale(u)),
        "empty": sorted(u for u in manifest["empty"] if not stale(u)),
    })


def _size(symbol: str) -> int:
    d = store.sym_dir(symbol)
    if not os.path.isdir(d):
        return 0
    return sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))


def downloaded() -> list[str]:
    from . import paths
    root = paths.data_dir()
    if not os.path.isdir(root):
        return []
    return sorted(s for s in os.listdir(root)
                  if os.path.isfile(os.path.join(root, s, "M1.bin")))


def _cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("symbols", nargs="*", help="symbols, or 'all'")
    ap.add_argument("--years", type=float, default=2)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    have = downloaded()
    if args.list or not args.symbols:
        total = 0
        for s in have:
            size = _size(s)
            total += size
            meta = store.read_meta(s)
            first = store.from_unix(meta["first"]).date() if meta.get("first") else "-"
            last = store.from_unix(meta["last"]).date() if meta.get("last") else "-"
            print(f"  {s:9s} {size / 1e6:7.0f} MB  {meta.get('bars', 0):>11,d} bars"
                  f"  {first} .. {last}")
        print(f"\n  {len(have)} instruments, {total / 1e9:.2f} GB")
        return

    wanted = have if args.symbols == ["all"] else [s.upper() for s in args.symbols]
    freed = 0
    for symbol in wanted:
        r = trim(symbol, args.years)
        freed += r["before"] - r["after"]
        note = "unchanged" if r.get("unchanged") else f"dropped {r.get('dropped', 0):,} bars"
        print(f"  {r['symbol']:9s} {r['before'] / 1e6:7.0f} -> {r['after'] / 1e6:6.0f} MB"
              f"  {note}")
    print(f"\n  freed {freed / 1e9:.2f} GB")


if __name__ == "__main__":
    _cli()
