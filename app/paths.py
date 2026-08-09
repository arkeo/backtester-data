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


def mirror() -> str:
    """Where history comes from.

    Compiled into the build, so nobody has to be told an address and nothing
    has to be typed in. A saved setting overrides it, which is what makes it
    possible to move customers to a different address later without rebuilding
    anything for them.
    """
    chosen = read_config().get("mirror", "")
    if chosen:
        return chosen
    try:
        from ._key import MIRROR
        return MIRROR
    except ImportError:
        return ""


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


def web_dir() -> str:
    """The UI files, which PyInstaller unpacks beside the temporary bundle."""
    if FROZEN:
        return os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "web")
    return os.path.join(APP_DIR, "web")
