"""Keep this machine's copy of the published history current.

    python3 sync_mirror.py

Runs on the mirror server, on a timer. Asks GitHub what is currently published,
downloads whatever this machine does not already have, and removes whatever is
no longer offered.

Why the server pulls instead of the publisher pushing
-----------------------------------------------------
The history is 3.4 GB and grows. Uploading that from the publisher's home
connection would take hours and would have to be repeated after every refresh.
GitHub to a data centre is a different kind of link entirely — the same
transfer is minutes, and it costs the publisher nothing but a line in a timer.

It also means the publisher's machine does not have to be on, or reachable, or
told the server's address.

This server holds no keys
-------------------------
Every file here is already sealed and signed by the publisher. This machine
copies bytes; it never opens one. That is deliberate: if this server is broken
into, the intruder gets a pile of encrypted files and nothing that opens them.

It is also why no key is needed to *decide* what to sync — the release listing
is public metadata, so the sealed index never has to be read.

Nothing here needs anything that is not in the Python standard library.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

CONF = "/etc/backtest-mirror.conf"

#: A run that is interrupted must not leave a half file that looks whole, so
#: everything lands under this suffix first and is renamed only once complete.
PART = ".part"

USER_AGENT = "backtester-mirror/1"

#: Free space to keep clear. A mirror that fills the disk takes the machine
#: down with it, including the web server that was the point of the exercise.
KEEP_FREE = 2 * 1024 ** 3


def load_conf() -> dict:
    """Settings, from the file the installer wrote, overridable by environment."""
    conf = {"repo": "", "tag": "history", "dest": "/srv/backtest/files"}
    try:
        with open(CONF, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip().lower()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for k in list(conf):
        env = os.environ.get("BACKTESTER_" + k.upper())
        if env:
            conf[k] = env
    if not conf["repo"]:
        sys.exit(f"No repository set. Put  repo=owner/name  in {CONF}")
    return conf


def get(url: str, *, timeout: int = 60, attempts: int = 4) -> bytes:
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{url}: {last}")


#: When each mirrored file was last written by the publisher.
#:
#: Inside the mirror folder, which is the only path the service unit may write
#: to — `ProtectSystem=strict` with `ReadWritePaths=/srv/backtest/files`. Put a
#: level above it, as this first was, every write fails; the failure is caught
#: and printed, so nothing breaks loudly, and instead the record is simply
#: never kept and all 3.4 GB is re-fetched every single week.
#:
#: The reason to have wanted it outside does not hold up. It lists filenames,
#: and the filenames are hashes that are already public on the release this
#: mirrors — anyone can read the same list from GitHub. What the sealed index
#: withholds is which *instrument* each hash is, and that is not in here.
STAMPS = "mirror-stamps.json"

#: What a published asset looks like. Everything on the release is sealed and
#: named by hash, so the suffix is the whole test — and it is what keeps this
#: job from deleting a file somebody else put in the folder it serves.
MIRRORED = ".bin"


def _stamps_path(dest: str) -> str:
    return os.path.join(dest, STAMPS)


def _stamps(dest: str) -> dict:
    try:
        with open(_stamps_path(dest), "r", encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        # No record yet, or an unreadable one. Both mean "trust nothing", and
        # the run re-fetches — expensive once, wrong never.
        return {}


def _write_stamps(dest: str, stamps: dict) -> None:
    path = _stamps_path(dest)
    tmp = path + PART
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stamps, f)
        os.replace(tmp, path)
    except OSError as e:
        # Losing the record costs one re-fetch. It must never cost the run.
        print(f"  could not write {STAMPS}: {e}", file=sys.stderr)


def published(repo: str, tag: str) -> list[dict]:
    """What the release currently offers: name, size and where to get it."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    try:
        release = json.loads(get(url))
    except RuntimeError as e:
        # By far the most common causes, and they look identical from here:
        # the name is wrong, or the first publish has not run yet.
        sys.exit(f"Could not read the release listing.\n  {e}\n"
                 f"  Check that https://github.com/{repo} exists, is public, "
                 f"and has a release tagged '{tag}'.")
    return [
        {"name": a["name"], "size": int(a["size"]),
         # When the publisher last wrote it. Size alone stopped being enough
         # the day the archive started being re-sealed: sealing is a keystream
         # cipher, so the same bars under a different key come to the same
         # number of bytes to the byte, and a mirror comparing lengths would
         # have gone on serving the old, licence-free copy for ever.
         "stamp": a.get("updated_at") or a.get("created_at") or "",
         "url": a.get("browser_download_url") or
                f"https://github.com/{repo}/releases/download/{tag}/{a['name']}"}
        for a in release.get("assets", [])
    ]


def download(asset: dict, dest: str, attempts: int = 4) -> None:
    """Fetch one file, and only put it in place once all of it is there.

    Retried, because a single read timing out part way through a 60 MB file is
    an ordinary event on a long link and not a reason to give up on it. Without
    this one such blip failed the whole run, and a failed run skips the tidying
    step — so the machine kept both the old copy and the new one and the disk
    grew by the size of the archive.
    """
    final = os.path.join(dest, asset["name"])
    tmp = final + PART
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(asset["url"],
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as r,                     open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1024 * 1024)

            got = os.path.getsize(tmp)
            if got != asset["size"]:
                raise RuntimeError(
                    f"expected {asset['size']} bytes, got {got}")
            os.replace(tmp, final)
            return
        except (urllib.error.URLError, OSError, RuntimeError,
                TimeoutError) as e:
            last = e
            try:
                os.remove(tmp)
            except OSError:
                pass
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"{asset['name']}: {last}")


def main() -> int:
    conf = load_conf()
    dest = conf["dest"]
    os.makedirs(dest, exist_ok=True)

    assets = published(conf["repo"], conf["tag"])
    if not assets:
        print("The release is there but has no files in it yet. Nothing to do.")
        return 0

    have = {}
    for name in os.listdir(dest):
        path = os.path.join(dest, name)
        if name.endswith(PART):
            os.remove(path)                  # an interrupted run; start it again
        elif not name.endswith(MIRRORED):
            # Not ours to manage. The tidying step at the end deletes whatever
            # in this folder the release no longer offers, and every published
            # asset without exception is a `.bin` — so anything else here was
            # put here deliberately by somebody, and deleting it would be this
            # job overstepping. The record this script keeps is one such file,
            # and it would otherwise be deleted at the end of the very run
            # that wrote it; an installer served from the same folder is
            # another, and losing that one is a customer with no download.
            continue
        elif os.path.isfile(path):
            have[name] = os.path.getsize(path)

    # Size, and when the publisher last wrote it.
    #
    # Size alone was the rule, on the reasoning that a rewritten file is a
    # different length essentially always. That stopped being true when the
    # archive began to be re-sealed under a rotating key: the bars do not
    # change, only the key does, and a keystream cipher gives back exactly as
    # many bytes as it was handed. So the length matches, this mirror decides
    # it already has the file, and it goes on serving the copy that opens
    # without a licence — while the release it mirrors serves one that does
    # not. That is the whole scheme defeated, silently, on the one mirror the
    # customers it was built for actually reach.
    seen = _stamps(dest)
    todo = [a for a in assets
            if have.get(a["name"]) != a["size"]
            or (a.get("stamp") and seen.get(a["name"]) != a["stamp"])]
    total = sum(a["size"] for a in todo)

    print(f"published: {len(assets)} files, "
          f"{sum(a['size'] for a in assets) / 1e9:.2f} GB")
    print(f"to fetch:  {len(todo)} files, {total / 1e9:.2f} GB")

    if total:
        free = shutil.disk_usage(dest).free
        # Only the new bytes have to fit, not the whole mirror — most runs
        # replace a file with one about the same size.
        if free < total + KEEP_FREE:
            print(f"Not enough room: {free / 1e9:.1f} GB free, "
                  f"{(total + KEEP_FREE) / 1e9:.1f} GB needed.", file=sys.stderr)
            return 1

    done = failed = 0
    for i, asset in enumerate(todo, 1):
        try:
            started = time.time()
            download(asset, dest)
            took = max(time.time() - started, 0.001)
            print(f"  [{i}/{len(todo)}] {asset['name']}  "
                  f"{asset['size'] / 1e6:.0f} MB  "
                  f"{asset['size'] / took / 1e6:.0f} MB/s")
            done += 1
            # Written per file rather than once at the end, so a run killed
            # halfway does not re-fetch everything it already had.
            seen[asset["name"]] = asset.get("stamp", "")
            _write_stamps(dest, seen)
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            # One bad file must not stop the rest. The next run picks it up.
            print(f"  [{i}/{len(todo)}] {asset['name']}: {e}", file=sys.stderr)
            failed += 1

    # Anything published before and not now. Deleted last, and only once the
    # fetching went cleanly — a listing that failed to load properly must never
    # be read as "the publisher removed everything".
    removed = 0
    if not failed:
        offered = {a["name"] for a in assets}
        for name in have:
            if name not in offered:
                os.remove(os.path.join(dest, name))
                removed += 1

    print(f"done: {done} fetched, {failed} failed, {removed} removed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
