"""Where the application keeps things.

Once this is installed under Program Files the folder holding the executable is
read-only, so downloaded history and saved sessions cannot live beside it. They
go to the user's own profile instead.

The search order below also means a portable copy — the whole folder on a USB
stick, say — keeps using its own ``data`` directory, and a machine that already
has history downloaded does not lose it just because the app was installed.
"""

from __future__ import annotations

import json
import os
import sys

#: Set this to keep the store somewhere else entirely — another drive, say,
#: since a full sweep of every forex pair is around sixteen gigabytes.
ENV_VAR = "BACKTESTER_DATA"

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: True when running from a PyInstaller build rather than from the source tree.
FROZEN = getattr(sys, "frozen", False)


def _profile_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Backtester")


def config_file() -> str:
    """Settings live in the profile even when the store does not.

    Otherwise the setting recording where the store is would have to be found
    inside the store, which cannot work.
    """
    return os.path.join(_profile_root(), "config.json")


def read_config() -> dict:
    try:
        with open(config_file(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_config(config: dict) -> None:
    os.makedirs(_profile_root(), exist_ok=True)
    tmp = config_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, config_file())


def language() -> str:
    """Empty until the question has been answered, which is what asks it."""
    return read_config().get("language", "")


def set_language(code: str) -> str:
    code = (code or "").strip().lower()
    if code not in ("en", "fa"):
        raise ValueError(f"unknown language {code!r}")
    config = read_config()
    config["language"] = code
    write_config(config)
    return code


def proxy() -> str:
    return read_config().get("proxy", "")


def set_proxy(url: str) -> str:
    """Remember a proxy and apply it to every request from now on."""
    from .sources import net
    url = (url or "").strip()
    config = read_config()
    config["proxy"] = url
    write_config(config)
    net.set_proxy(url)
    return url


def apply_saved_proxy() -> str:
    """Called at startup, so a saved proxy is in force before anything fetches."""
    from .sources import net
    saved = proxy()
    net.set_proxy(saved)
    return saved


def mirrors() -> list[dict]:
    """Every history server this build knows about, in the order to try them.

    The first is a GitHub release: free, unmetered, and needing no machine kept
    running. The second is our own server, which takes over when the first
    cannot be reached — which for the customers this is sold to is often.

    None of this is ever put to the customer. There is no setting for it and no
    message about it: they press Download and history arrives. The switch is
    decided while fetching the index, which is a few kilobytes, so by the time
    a real download starts the working server is already known and the transfer
    never has to restart in front of them.

    An address typed in by hand comes first when there is one. That is not a
    feature of the product — there is no box to type it in. It is the lever for
    the day one customer needs to be pointed somewhere specific.
    """
    out, seen = [], set()

    def add(entry_id, name, url):
        url = (url or "").strip().rstrip("/")
        if url and url not in seen:
            seen.add(url)
            out.append({"id": entry_id, "name": name, "url": url})

    add("custom", "Typed in by hand", read_config().get("mirror", ""))

    # A signed list from the publisher, if one has been fetched. It comes
    # before the compiled-in addresses because correcting them is its entire
    # purpose — an address that moved after this copy was built is one the
    # build cannot know about.
    try:
        from . import endpoints
        held = endpoints.cached()
        for s in (held or {}).get("servers", []):
            add(s.get("id", ""), s.get("name", ""), s.get("url", ""))
    except Exception:                              # noqa: BLE001
        pass

    for entry in baked_mirrors():
        add(entry["id"], entry["name"], entry["url"])
    return out


def baked_mirrors() -> list[dict]:
    """Only the addresses compiled into this build.

    Kept separate because the published list is fetched *from* these, and
    reading it through `mirrors()` would mean asking an address the list itself
    supplied — which is exactly the address that might be the broken one.
    """
    out = []
    try:
        from ._key import MIRRORS
        for m in MIRRORS:
            if m.get("url"):
                out.append({"id": m.get("id", ""), "name": m.get("name", ""),
                            "url": m["url"].rstrip("/")})
    except ImportError:
        pass
    if not out:
        try:
            # A build made before there was more than one. Without this it
            # would lose the only address it had and downloads would stop.
            from ._key import MIRROR
            if MIRROR:
                out.append({"id": "main", "name": "History server",
                            "url": MIRROR.rstrip("/")})
        except ImportError:
            pass
    return out


def mirror_choice() -> str:
    """Which one the user picked, or "auto" if they have not."""
    return read_config().get("mirror_choice", "auto") or "auto"


def set_mirror_choice(entry_id: str) -> str:
    entry_id = (entry_id or "auto").strip()
    known = {m["id"] for m in mirrors()} | {"auto"}
    if entry_id not in known:
        raise ValueError(f"no history server called {entry_id!r}")
    config = read_config()
    config["mirror_choice"] = entry_id
    write_config(config)
    return entry_id


def mirror_order() -> list[dict]:
    """The servers to try, in the order to try them.

    On "auto" that is all of them, best first, with whichever last worked
    promoted — so a customer whose international link is blocked pays the wait
    once rather than at every download.

    On an explicit choice it is that one alone. Falling through would quietly
    undo the choice, and someone who picks a particular server is usually doing
    it *because* the other one is wrong for them — stale, slow, or costing them
    international traffic they are paying for.
    """
    picks = mirrors()
    choice = mirror_choice()
    if choice != "auto":
        return [m for m in picks if m["id"] == choice] or picks[:1]

    good = read_config().get("mirror_ok", "")
    if good:
        # A typed-in address stays in front of the remembered one. Remembering
        # is a convenience; typing an address is an instruction, and the two
        # must not be able to reverse each other.
        picks.sort(key=lambda m: (m["id"] != "custom", m["id"] != good))
    return picks


def mirror() -> str:
    """The one address downloads use right now.

    Kept because most of the application only ever wants an address, and only
    the two places that actually reach the network care that there is a list.
    """
    order = mirror_order()
    return order[0]["url"] if order else ""


def remember_mirror(entry_id: str) -> None:
    """Note which server answered, so the next download starts there."""
    config = read_config()
    if config.get("mirror_ok") == entry_id:
        return                                   # nothing to write, most calls
    config["mirror_ok"] = entry_id
    write_config(config)


def set_mirror(url: str) -> str:
    """Where this installation gets history when the sources are unreachable."""
    url = (url or "").strip().rstrip("/")
    config = read_config()
    config["mirror"] = url
    write_config(config)
    return url


def set_data_dir(path: str) -> str:
    """Point the store somewhere else, and remember it."""
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(path.strip())))
    os.makedirs(path, exist_ok=True)
    config = read_config()
    config["data_dir"] = path
    write_config(config)
    return path


def _has_content(path: str) -> bool:
    try:
        return os.path.isdir(path) and any(os.scandir(path))
    except OSError:
        return False


def root() -> str:
    """The directory holding ``data`` and ``sessions`` by default."""
    override = os.environ.get(ENV_VAR)
    if override:
        return override

    beside_exe = os.path.dirname(sys.executable) if FROZEN else APP_DIR
    candidates = [_profile_root(), beside_exe] if FROZEN else [APP_DIR, _profile_root()]

    for candidate in candidates:
        if _has_content(os.path.join(candidate, "data")):
            return candidate

    # Nothing downloaded yet: put it where it can definitely be written.
    return _profile_root() if FROZEN else APP_DIR


def data_dir() -> str:
    """Where downloaded history lives.

    Precedence, strongest first:

      1. the environment variable — an explicit instruction for this run, and
         the only way to point a single launch somewhere else without
         disturbing the saved setting;
      2. the saved setting, since a full sweep is around sixteen gigabytes and
         may well belong on a different drive from the user profile;
      3. next to whatever `root()` picks.

    The order matters: with the setting winning, there would be no way to run
    the application against a different store once one had ever been chosen.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        # The variable *is* the directory. It used to mean "this, or a `data`
        # folder inside it if one happens to exist", which quietly moved the
        # store the first time such a folder appeared — and on a fresh machine
        # picked a different place than on one that had run before.
        return override
    chosen = read_config().get("data_dir")
    if chosen:
        return chosen
    return os.path.join(root(), "data")


def sessions_dir() -> str:
    return os.path.join(root(), "sessions")


def indicators_dir() -> str:
    """Where indicators added by hand live.

    In the profile rather than beside the executable, because that folder is
    read-only once the application is installed under Program Files — and
    because these belong to the person, not to the installation, so they
    survive an upgrade.
    """
    return os.path.join(_profile_root(), "indicators")


def web_dir() -> str:
    """The UI files, which PyInstaller unpacks beside the temporary bundle."""
    if FROZEN:
        return os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "web")
    return os.path.join(APP_DIR, "web")
