"""Move downloaded history between machines.

Some networks cannot reach two of the three sources at all — a country block
is not something a retry or a better error message can fix. So history can be
downloaded on a machine that *can* reach them and carried to one that cannot:

    python -m app.portable export EURUSD XAUUSD --to D:\\forex.btdata
    python -m app.portable import D:\\forex.btdata
    python -m app.portable info D:\\forex.btdata

The file is an ordinary zip. Bars are already stored as packed integers, so it
compresses only a little — expect roughly two thirds of the folder size — but
it is one file, it carries its own description, and it needs no network at the
far end.
"""

from __future__ import annotations

import argparse
import io
import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone

import numpy as np

from . import crypt, paths, store

MANIFEST = "bundle.json"
SUFFIX = ".btdata"

#: The key a published mirror is sealed with. Override it before publishing —
#: `BACKTESTER_KEY` on the machine that publishes, and the same value compiled
#: into the application the customers run. Leaving it at the default means
#: anyone else's build can read your bundles.
#:
#: This is deterrence, not secrecy: the key ships inside the application. See
#: the module docstring in crypt.py for what that does and does not buy.
DEFAULT_KEY = b"backtester-default-publisher-key"


def publisher_key() -> bytes:
    """The key this copy seals and opens bundles with.

    Order matters:

      1. the environment, for publishing and for testing;
      2. a module written in at build time, which is how the installed
         application knows the key — a customer's machine has no environment
         variable set, so without this the built application would fall back to
         the default and be unable to open anything the mirror published;
      3. the default, which only exists so the project runs out of the box.
    """
    from_env = os.environ.get("BACKTESTER_KEY", "")
    if from_env:
        return from_env.encode()
    try:
        from ._key import KEY          # written by installer/build.py
        return KEY.encode() if isinstance(KEY, str) else KEY
    except ImportError:
        return DEFAULT_KEY


#: Files that belong in a bundle. Everything else in an instrument's folder is
#: a derived timeframe, and importing rebuilds those from M1 in a second or two
#: — so sending them is pure waste. Measured: they are 24% of the folder and
#: 29% of the compressed bundle, which over a whole catalogue is gigabytes of
#: someone's bandwidth spent shipping something the far end throws away.
SENDABLE = {"M1.bin", "meta.json", "manifest.json"}


def _worth_sending(name: str) -> bool:
    return name in SENDABLE


def _year_start(year: int) -> int:
    return int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())


def year_of(when: int) -> int:
    """The calendar year a bar belongs to, in UTC."""
    return datetime.fromtimestamp(int(when), timezone.utc).year


def export(symbols: list[str], dest: str, progress=None, seal: bool = False,
           year: int | None = None) -> dict:
    """Pack the given instruments into one file.

    With `year`, only that calendar year's bars go in. That is what makes a
    published archive updatable: a year that has ended never changes again, so
    only the current one is rewritten when a new day arrives.
    """
    symbols = [s.upper() for s in symbols]
    root = paths.data_dir()
    present = [s for s in symbols if os.path.isdir(os.path.join(root, s))]
    if not present:
        raise ValueError("none of those instruments are downloaded")

    # Only name it for the user's benefit. A sealed bundle is deliberately not
    # advertised as one, so a name that already has an extension is left alone.
    if not os.path.splitext(dest)[1]:
        dest += SUFFIX
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    described = []
    for symbol in present:
        meta = store.read_meta(symbol)
        if year is None:
            described.append({
                "symbol": symbol,
                "bars": meta.get("bars", 0),
                "first": meta.get("first"),
                "last": meta.get("last"),
                "has_spread": meta.get("has_spread", False),
            })
            continue
        # Year-scoped, so the description of 2011 does not move when 2026
        # gains a day.
        bars = store.load(symbol)
        lo = int(np.searchsorted(bars["t"], _year_start(year)))
        hi = int(np.searchsorted(bars["t"], _year_start(year + 1)))
        described.append({
            "symbol": symbol,
            "year": year,
            "bars": hi - lo,
            "first": int(bars["t"][lo]) if hi > lo else None,
            "last": int(bars["t"][hi - 1]) if hi > lo else None,
            "has_spread": meta.get("has_spread", False),
        })

    manifest = {"format": 1, "instruments": described}
    if year is None:
        # A timestamp is useful on a bundle somebody exported by hand and
        # fatal on a published one: it changes every run, so every year's file
        # would differ every day even where not one bar had moved.
        manifest["created"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")

    def put(z, name: str, data) -> None:
        """Write an entry, with no clock in it when this is a year file.

        zipfile stamps the current time into every entry it writes. That alone
        made two publishes of identical data produce different bytes — so every
        year's digest changed every night, and the split saved nothing at all.
        A fixed date is the earliest a zip can express.
        """
        if year is None:
            z.writestr(name, data)
            return
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        z.writestr(info, data)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        put(z, MANIFEST, json.dumps(manifest, indent=2))
        for i, symbol in enumerate(present):
            folder = os.path.join(root, symbol)
            for name in sorted(os.listdir(folder)):
                if not _worth_sending(name):
                    continue
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                if year is None:
                    z.write(path, f"{symbol}/{name}")
                    continue

                # A year's file must contain *only* that year, or it is not
                # frozen and the whole point is lost. Everything
                # instrument-wide has to be filtered or left out — the first
                # version shipped meta.json unchanged, so adding one day to
                # 2026 altered the bytes of 2000 through 2025 as well and every
                # customer re-downloaded the lot. The saving was exactly zero.
                if name == "M1.bin":
                    bars = np.fromfile(path, dtype=store.BAR)
                    lo = np.searchsorted(bars["t"], _year_start(year))
                    hi = np.searchsorted(bars["t"], _year_start(year + 1))
                    put(z, f"{symbol}/M1.bin",
                        np.ascontiguousarray(bars[lo:hi]).tobytes())
                elif name == "manifest.json":
                    # Which periods are covered — sliced to this year, so it
                    # still saves the receiver a re-fetch without dragging the
                    # other years' coverage along.
                    held = json.loads(open(path, "rb").read())
                    put(z, f"{symbol}/manifest.json", json.dumps({
                        k: sorted(x for x in held.get(k, [])
                                  if str(x).startswith(str(year)))
                        for k in ("done", "empty")}, indent=2))
                # meta.json is deliberately absent: it counts the whole
                # instrument, so it changes whenever any year does, and the
                # receiver rebuilds it from the merged bars anyway.
            if progress:
                progress(i + 1, len(present), symbol)

    payload = buffer.getvalue()
    # Taken before sealing, and that is not a detail. Sealing draws a fresh
    # random nonce every time — as it must, since a keystream cipher reusing
    # one would hand away the key — so the sealed bytes of identical data are
    # never identical. A digest of the ciphertext would therefore say
    # "everything changed" every single night. This describes the contents.
    digest = hashlib.sha256(payload).hexdigest()[:16]
    if seal:
        payload = crypt.encrypt(payload, publisher_key())
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(payload)
    os.replace(tmp, dest)

    return {"path": os.path.abspath(dest), "bytes": os.path.getsize(dest),
            "sha": digest, "instruments": described}


def opening_keys() -> list[bytes]:
    """Every key a bundle here might have been sealed with, best first.

    A licence's content keys come first, then the key compiled into the build.
    Two reasons for the order and for keeping both:

    The licence key is what makes the whole scheme worth anything — a bundle
    sealed with it cannot be read by a copy that has no licence, so removing
    the licence check does not remove the licence. The built-in key is what
    keeps every bundle published before any of this existed readable, so
    nobody's downloaded history stops working on the day this ships.
    """
    keys = []
    try:
        from . import licence
        keys.extend(licence.content_keys())
    except Exception:                          # noqa: BLE001
        pass
    keys.append(publisher_key())
    return keys


def _open(path_or_bytes) -> zipfile.ZipFile:
    """Open a bundle whether it was sealed or not.

    Plain bundles are what one machine hands another; sealed ones are what a
    mirror publishes. Both have to work, and the difference is a magic number
    rather than a filename, so nothing depends on how it was named.
    """
    if isinstance(path_or_bytes, (bytes, bytearray)):
        blob = bytes(path_or_bytes)
    else:
        with open(path_or_bytes, "rb") as f:
            blob = f.read()
    if crypt.looks_encrypted(blob):
        last = None
        for key in opening_keys():
            try:
                blob = crypt.decrypt(blob, key)
                break
            except crypt.WrongKey as e:
                last = e
        else:
            # Said as what it means to the person, not as a cipher failure:
            # almost always a subscription that has lapsed or never started.
            raise crypt.WrongKey(
                "this history needs a current licence to open") from last
    return zipfile.ZipFile(io.BytesIO(blob))


def info(path) -> dict:
    with _open(path) as z:
        try:
            return json.loads(z.read(MANIFEST))
        except KeyError:
            raise ValueError("this is not a Backtester history file") from None


def import_bundle(path: str, progress=None) -> dict:
    """Fold a bundle into the local store.

    Merged, not written over. Overwriting looks harmless until someone who has
    downloaded ten years imports a five-year bundle and silently loses half of
    it — and until a daily update, which is a fresh snapshot of a rolling
    window, throws away everything older than that window every single day.
    Merging makes both of those safe, and makes re-importing the same bundle a
    no-op rather than a risk.
    """
    manifest = info(path)
    root = paths.data_dir()
    os.makedirs(root, exist_ok=True)

    wanted = [d["symbol"] for d in manifest.get("instruments", [])]
    imported = []

    with _open(path) as z:
        names = [n for n in z.namelist() if n != MANIFEST]
        # Never trust a path out of an archive.
        for name in names:
            target = os.path.normpath(os.path.join(root, name))
            if not target.startswith(os.path.abspath(root)):
                raise ValueError(f"refusing a path that escapes the store: {name}")

        for i, symbol in enumerate(wanted):
            bars_name = f"{symbol}/M1.bin"
            if bars_name in names:
                incoming = np.frombuffer(z.read(bars_name), dtype=store.BAR)
                before = store.bar_count(symbol)
                total = store.merge_m1(symbol, incoming)
                added = total - before
            else:
                total = store.bar_count(symbol)
                added = 0

            # The manifest travels too, so the receiving machine knows which
            # periods are already covered and does not re-fetch them.
            src = f"{symbol}/manifest.json"
            if src in names:
                _merge_manifest(symbol, json.loads(z.read(src)))

            meta = store.refresh_meta(symbol)
            imported.append({"symbol": symbol, "bars": meta.get("bars", total),
                             "added": added})
            if progress:
                progress(i + 1, len(wanted), symbol)

    return {"instruments": imported}


def _merge_manifest(symbol: str, incoming: dict) -> None:
    from . import fetch
    current = fetch.read_manifest(symbol)
    fetch.write_manifest(symbol, {
        "done": sorted(set(current["done"]) | set(incoming.get("done", []))),
        "empty": sorted(set(current["empty"]) | set(incoming.get("empty", []))),
    })


INDEX = "index.json"
SEALED_INDEX = "i.bin"


def publish(symbols: list[str], out_dir: str, progress=None,
            seal: bool = True) -> dict:
    """Write one bundle per instrument, plus an index, ready to upload.

    The result is a folder of ordinary files served over plain HTTP — no
    application, no database, nothing to run. Upload it anywhere that will
    serve static files and point the application at the address; it then
    downloads history from there instead of from the original sources, which
    is the only route that cannot be blocked at the far end.
    """
    if seal and publisher_key() == DEFAULT_KEY:
        # The default is written in the source, and the source is public. A run
        # that quietly used it would produce bundles anyone could open while
        # looking exactly like sealed ones — the worst of both, and invisible.
        raise SystemExit(
            "refusing to publish sealed bundles with the default key.\n"
            "  Set BACKTESTER_KEY to your own value, and build the application\n"
            "  with the same one. Use --open if you genuinely want readable\n"
            "  bundles.")

    os.makedirs(out_dir, exist_ok=True)
    entries = []
    for i, symbol in enumerate(symbols):
        symbol = symbol.upper()
        meta = store.read_meta(symbol)
        if not meta.get("bars"):
            continue
        entries.append(publish_one(symbol, out_dir, seal))
        if progress:
            progress(i + 1, len(symbols), symbol)

    index = write_index(entries, out_dir, seal)
    return {"dir": os.path.abspath(out_dir), "index": index, "sealed": seal,
            "bytes": sum(e["bytes"] for e in entries)}


def publish_one(symbol: str, out_dir: str, seal: bool = True) -> dict:
    """One instrument, as one sealed file per calendar year.

    Why not one file for the whole instrument, which is simpler
    ---------------------------------------------------------
    Because it made every update cost the entire archive. Each instrument gains
    a trading day every day; a single-file bundle is rewritten whole to hold
    it, so anyone mirroring the archive re-downloaded 3.36 GB to collect about
    40 KB of new bars — over a thousand times more bytes than it gained, and
    more than a hundred gigabytes a month.

    Split by year, a year that has ended never changes again. Only the current
    one is rewritten, which is about a twentieth of the traffic. The same
    saving lands on every customer: updating means fetching this year, not
    fetching everything since 2000 again.

    Each year carries a digest, because the filenames are derived from the key
    and are therefore *stable* — the name of this year's file is the same today
    as yesterday even though its contents are not. Without something that
    changes, nobody downstream could tell a refreshed year from an untouched
    one.
    """
    meta = store.read_meta(symbol)
    bars = store.load(symbol)
    if not len(bars):
        raise ValueError(f"{symbol} has no bars to publish")

    years = []
    for year in range(year_of(int(bars["t"][0])), year_of(int(bars["t"][-1])) + 1):
        lo = int(np.searchsorted(bars["t"], _year_start(year)))
        hi = int(np.searchsorted(bars["t"], _year_start(year + 1)))
        if hi <= lo:
            continue                       # a year the market did not trade
        name = (crypt.name_for(f"{symbol}#{year}", publisher_key()) if seal
                else f"{symbol}-{year}{SUFFIX}")
        path = os.path.join(out_dir, name)
        r = export([symbol], path, seal=seal, year=year)
        years.append({"year": year, "file": name, "bytes": r["bytes"],
                      "bars": hi - lo, "sha": r["sha"]})

    return {
        "symbol": symbol,
        "years": years,
        "bytes": sum(y["bytes"] for y in years),
        "bars": meta.get("bars", int(len(bars))),
        "first": meta.get("first"),
        "last": meta.get("last"),
        "has_spread": meta.get("has_spread", False),
    }


def files_in(entry: dict) -> list[str]:
    """Every file an index entry refers to, old format or new."""
    if entry.get("years"):
        return [y["file"] for y in entry["years"]]
    return [entry["file"]] if entry.get("file") else []


def write_index(entries: list[dict], out_dir: str, seal: bool = True) -> dict:
    """Write the listing that tells the application what is on offer.

    Separate from `publish` because a mirror is not always built in one go: the
    scheduled job seals one instrument at a time and has to be able to rewrite
    the listing after each one without re-exporting the rest.
    """
    os.makedirs(out_dir, exist_ok=True)
    index = {
        "format": 2,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instruments": list(entries),
    }
    raw = json.dumps(index, indent=2).encode()
    if seal:
        # The index is sealed too. Otherwise it lists every instrument, its
        # dates and the exact filename to fetch — a complete map of the mirror
        # for anyone who opens it.
        with open(os.path.join(out_dir, SEALED_INDEX), "wb") as f:
            f.write(crypt.encrypt(raw, publisher_key()))
    else:
        with open(os.path.join(out_dir, INDEX), "wb") as f:
            f.write(raw)
    return index


def _years_path(symbol: str) -> str:
    return os.path.join(paths.data_dir(), symbol.upper(), "years.json")


def _years_held(symbol: str) -> dict:
    """Which published years this machine already has, and at what digest."""
    try:
        with open(_years_path(symbol), "r", encoding="utf-8") as f:
            held = json.load(f)
        return held if isinstance(held, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_year(symbol: str, year: int, sha: str) -> None:
    held = _years_held(symbol)
    held[str(year)] = sha
    path = _years_path(symbol)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(held, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def mirror_index(base_url: str) -> dict:
    """Read the index a published folder exposes."""
    from .sources import net
    base = base_url.strip().rstrip("/")
    if not base:
        raise ValueError("no mirror address set")
    raw = None
    for name in (SEALED_INDEX, INDEX):
        try:
            raw = net.fetch(f"{base}/{name}", timeout=30, attempts=2)
            break
        except net.NotFound:
            continue
    if raw is None:
        # By far the most common reason, and it looks identical to a wrong
        # address: the publisher has set the mirror up but the first run has
        # not finished, so the files genuinely are not there yet.
        raise ValueError(
            "Nothing published at that address yet. Either the address is "
            "wrong, or the publisher's first update has not finished — that "
            "one takes a while. Try again in an hour.")
    if crypt.looks_encrypted(raw):
        raw = crypt.decrypt(raw, publisher_key())
    try:
        index = json.loads(raw)
    except ValueError:
        raise ValueError("that address returned something that is not an "
                         "index") from None
    if "instruments" not in index:
        raise ValueError("that index is not in the expected format")
    return index


def mirror_fetch(base_url: str, symbol: str, progress=None,
                 on_bytes=None, on_stage=None) -> dict:
    """Download one instrument from a mirror and import it.

    ``on_bytes(received, total)`` and ``on_stage(text)`` exist because a full
    history is a large file: without them the whole operation is one silent
    wait, and there is no way to tell a slow connection from a stalled one.
    """
    from .sources import net
    base = base_url.strip().rstrip("/")
    if on_stage:
        on_stage("finding it")
    index = mirror_index(base)
    entry = next((e for e in index["instruments"]
                  if e["symbol"].upper() == symbol.upper()), None)
    if not entry:
        raise ValueError(f"{symbol} is not on that mirror")

    # Which pieces this machine still needs.
    #
    # A published archive is one file per calendar year, and a year that has
    # ended never changes again — so updating fetches this year, not everything
    # since 2000. The comparison is against a digest rather than the filename,
    # because names are derived from the publisher's key and stay the same when
    # the contents do not.
    have = _years_held(symbol)
    if entry.get("years"):
        wanted = [y for y in entry["years"] if have.get(str(y["year"])) != y["sha"]]
    else:
        # An archive published before the split. One file, all of it.
        wanted = [{"year": None, "file": entry["file"],
                   "bytes": entry.get("bytes", 0), "sha": ""}]

    if not wanted:
        if on_stage:
            on_stage("already up to date")
        return {"symbol": symbol, "bytes": 0, "instruments": [],
                "already": True}

    scratch = os.path.join(paths.root(), "incoming")
    os.makedirs(scratch, exist_ok=True)
    total = sum(int(y.get("bytes", 0)) for y in wanted)
    got = 0
    result = {"instruments": []}

    for piece in wanted:
        tmp = os.path.join(scratch, piece["file"])
        done_before = got
        try:
            if on_stage:
                on_stage("downloading")
            # Progress spans the whole job, not each file, or a customer
            # fetching twenty years watches the bar reset twenty times.
            net.download(
                f"{base}/{piece['file']}", tmp,
                on_progress=(lambda n, _t, base_=done_before:
                             on_bytes and on_bytes(base_ + n, total)),
                timeout=120, attempts=3)
            got += os.path.getsize(tmp)
            if on_stage:
                on_stage("adding it to your history")
            result = import_bundle(tmp, progress=progress)
            if piece["year"] is not None:
                _remember_year(symbol, piece["year"], piece["sha"])
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    return {"symbol": symbol, "bytes": got, **result}


def _cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    e = sub.add_parser("export")
    e.add_argument("symbols", nargs="+", help="symbols, or 'all'")
    e.add_argument("--to", required=True)

    i = sub.add_parser("import")
    i.add_argument("path")

    n = sub.add_parser("info")
    n.add_argument("path")

    b = sub.add_parser("publish", help="build a folder of bundles to upload")
    b.add_argument("symbols", nargs="+", help="symbols, or 'all'")
    b.add_argument("--to", required=True)
    b.add_argument("--open", action="store_true",
                   help="publish readable files instead of sealed ones")

    args = ap.parse_args()

    if args.command == "publish":
        from .trim import downloaded
        symbols = downloaded() if args.symbols == ["all"] else args.symbols
        r = publish(symbols, args.to, seal=not args.open,
                    progress=lambda n, t, s: print(f"  {n}/{t}  {s}", flush=True))
        print(f"\n  wrote {len(r['index']['instruments'])} bundles to {r['dir']}")
        print(f"  {r['bytes'] / 1e6:.0f} MB in total — upload the whole folder")
        if r["sealed"]:
            print("  sealed: the files are opaque and named by hash, and open "
                  "only in this application")
        return

    if args.command == "export":
        from .trim import downloaded
        symbols = downloaded() if args.symbols == ["all"] else args.symbols
        r = export(symbols, args.to,
                   progress=lambda n, t, s: print(f"  {n}/{t}  {s}", flush=True))
        print(f"\n  wrote {r['path']}  ({r['bytes'] / 1e6:.0f} MB, "
              f"{len(r['instruments'])} instruments)")
    elif args.command == "info":
        m = info(args.path)
        print(f"  created {m['created']}")
        for d in m["instruments"]:
            first = store.from_unix(d["first"]).date() if d["first"] else "-"
            last = store.from_unix(d["last"]).date() if d["last"] else "-"
            print(f"  {d['symbol']:9s} {d['bars']:>11,d} bars  {first} .. {last}"
                  f"{'  spread' if d['has_spread'] else ''}")
    else:
        r = import_bundle(args.path,
                          progress=lambda n, t, s: print(f"  {n}/{t}  {s}", flush=True))
        total = sum(d["bars"] for d in r["instruments"])
        print(f"\n  imported {len(r['instruments'])} instruments, {total:,} bars")


if __name__ == "__main__":
    _cli()
