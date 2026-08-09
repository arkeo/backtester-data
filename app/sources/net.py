"""One HTTP helper for every source, because every source needs the same care.

The rules learned the hard way while measuring these feeds:

* A browser ``User-Agent`` is mandatory. Dukascopy answers 429 to anything else.
* **429 means slow down, not absent.** Only 404 means "no data for that period".
  An earlier probe in this project reported whole instruments as unavailable
  purely because it was swallowing 429s, after thousands of files for those very
  instruments had already downloaded.
* Timeouts and resets are routine at this request volume and must be retried
  rather than treated as failure.

Proxies
-------
Two of the three sources are unreachable from some countries. A system-wide VPN
fixes that for everything, but the popular *browser extension* kind does not:
it proxies the browser only, so the application still goes out over the plain
connection and nothing appears to work whether the "VPN" is on or off. That
symptom is what this module's proxy support exists for — the address is
configured in the application and used for every request it makes.

Both HTTP and SOCKS5 proxies are handled, the latter implemented here rather
than pulled in as a dependency, because SOCKS5 on localhost is what almost
every one of those tools actually exposes.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class NotFound(Exception):
    """The server said 404: this period genuinely has no data."""


class Blocked(Exception):
    """The server answered, but refused us — a 403 or 451.

    Worth its own type because it means something completely different from a
    timeout: the request arrived and was rejected, usually on location. No
    amount of retrying will help, and the fix is a proxy rather than patience.
    """


# --------------------------------------------------------------------------
# proxy
# --------------------------------------------------------------------------

_proxy: str = ""
_opener: urllib.request.OpenerDirector | None = None


def set_proxy(url: str) -> None:
    """Route every later request through ``url``, or through none if empty."""
    global _proxy, _opener
    _proxy = (url or "").strip()
    _opener = _build_opener(_proxy)


def get_proxy() -> str:
    return _proxy


def _build_opener(proxy: str) -> urllib.request.OpenerDirector:
    if not proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}))       # ignore system proxy env too

    parts = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
    if parts.scheme in ("socks5", "socks5h", "socks"):
        return urllib.request.build_opener(_Socks5Handler(parts))
    return urllib.request.build_opener(urllib.request.ProxyHandler({
        "http": proxy, "https": proxy,
    }))


class _Socks5Connection(http.client.HTTPSConnection):
    """An HTTPS connection whose socket is opened through a SOCKS5 proxy."""

    def __init__(self, *args, proxy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._proxy = proxy

    def connect(self):
        sock = socket.create_connection(
            (self._proxy.hostname, self._proxy.port or 1080), self.timeout)
        _socks5_handshake(sock, self.host, self.port or 443, self._proxy)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _socks5_handshake(sock, host: str, port: int, proxy) -> None:
    user, password = proxy.username, proxy.password
    methods = b"\x00\x02" if user else b"\x00"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    reply = _recv_exactly(sock, 2)
    if reply[0] != 5:
        raise OSError("the proxy did not answer as SOCKS5")

    if reply[1] == 2:
        if not user:
            raise OSError("the proxy wants a username and password")
        u, p = user.encode(), (password or "").encode()
        sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        if _recv_exactly(sock, 2)[1] != 0:
            raise OSError("the proxy rejected the username or password")
    elif reply[1] != 0:
        raise OSError("the proxy offered no authentication method we can use")

    # Send the hostname rather than an address, so the proxy resolves it. That
    # matters: resolving locally is exactly what a blocked network gets wrong.
    name = host.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name
                 + struct.pack(">H", port))
    resp = _recv_exactly(sock, 4)
    if resp[1] != 0:
        raise OSError(f"the proxy refused to connect ({_SOCKS_ERRORS.get(resp[1], resp[1])})")
    if resp[3] == 1:
        _recv_exactly(sock, 4)
    elif resp[3] == 3:
        _recv_exactly(sock, _recv_exactly(sock, 1)[0])
    elif resp[3] == 4:
        _recv_exactly(sock, 16)
    _recv_exactly(sock, 2)


_SOCKS_ERRORS = {
    1: "general failure", 2: "not allowed", 3: "network unreachable",
    4: "host unreachable", 5: "connection refused", 6: "TTL expired",
    7: "command not supported", 8: "address type not supported",
}


def _recv_exactly(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("the proxy closed the connection")
        buf += chunk
    return buf


class _Socks5PlainConnection(http.client.HTTPConnection):
    """The same, for plain HTTP.

    Every source here is HTTPS, but without this an ``http://`` request would
    quietly ignore the proxy and go out over the direct connection — which on a
    blocked network is the one failure mode that looks like a working proxy.
    """

    def __init__(self, *args, proxy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._proxy = proxy

    def connect(self):
        sock = socket.create_connection(
            (self._proxy.hostname, self._proxy.port or 1080), self.timeout)
        _socks5_handshake(sock, self.host, self.port or 80, self._proxy)
        self.sock = sock


class _Socks5Handler(urllib.request.HTTPSHandler, urllib.request.HTTPHandler):
    def __init__(self, proxy):
        urllib.request.HTTPSHandler.__init__(self)
        self._proxy = proxy

    def https_open(self, req):
        def build(host, **kwargs):
            kwargs.pop("context", None)
            return _Socks5Connection(host, proxy=self._proxy,
                                     context=ssl.create_default_context(), **kwargs)
        return self.do_open(build, req)

    def http_open(self, req):
        return self.do_open(
            lambda host, **kw: _Socks5PlainConnection(host, proxy=self._proxy, **kw),
            req)


set_proxy("")


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def fetch(url: str, *, headers: dict | None = None, data: bytes | None = None,
          timeout: int = 60, attempts: int = 6) -> bytes:
    """GET (or POST, if ``data`` is given) with backoff.

    Raises NotFound on 404 and Blocked on 403/451, both immediately: they are
    answers, not failures to retry.
    """
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)

    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with _opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from None
            if e.code in (403, 451):
                raise Blocked(f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}"
                              ) from None
            last = e
            if e.code == 429:
                time.sleep(min(2 ** attempt, 30))
                continue
            time.sleep(1 + attempt)
        except Exception as e:          # timeouts, resets, DNS hiccups
            last = e
            time.sleep(1 + attempt)

    raise RuntimeError(f"giving up on {url}: {last}")


def download(url: str, dest: str, *, on_progress=None, timeout: int = 120,
             attempts: int = 3, chunk: int = 1 << 18) -> int:
    """GET straight to a file, saying how far along it is.

    `fetch` reads the whole body before the caller sees a single byte. That is
    right for a year of quotes and wrong for a published bundle: a full history
    runs to hundreds of megabytes, which on a domestic connection is minutes of
    a window that shows nothing and cannot be told apart from one that has hung.

    ``on_progress(received, total)`` is called as it goes; ``total`` is 0 when
    the server declines to say.
    """
    hdrs = {"User-Agent": USER_AGENT}
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with _opener.open(req, timeout=timeout) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                # Restarted from the beginning on a retry rather than resumed:
                # a partial file that a retry appended to would be silently
                # corrupt, and the sealing would report it as a wrong key.
                with open(dest, "wb") as f:
                    while True:
                        block = resp.read(chunk)
                        if not block:
                            break
                        f.write(block)
                        got += len(block)
                        if on_progress:
                            on_progress(got, total)
                if total and got < total:
                    raise OSError(f"connection closed after {got} of {total} bytes")
                return got
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from None
            if e.code in (403, 451):
                raise Blocked(f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}"
                              ) from None
            last = e
            time.sleep(1 + attempt)
        except Exception as e:              # timeouts, resets, short reads
            last = e
            time.sleep(1 + attempt)

    raise RuntimeError(f"giving up on {url}: {last}")


def probe(url: str, timeout: int = 15) -> dict:
    """One quick attempt, reporting what happened rather than raising.

    This is what turns "the downloads do not work" into an answer: it
    distinguishes a name that will not resolve from a connection that is
    refused, from a server that answers and says no.
    """
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with _opener.open(req, timeout=timeout) as resp:
            resp.read(2048)
            return {"ok": True, "status": resp.status,
                    "ms": round((time.time() - started) * 1000)}
    except urllib.error.HTTPError as e:
        blocked = e.code in (403, 451)
        return {"ok": False, "status": e.code, "blocked": blocked,
                "ms": round((time.time() - started) * 1000),
                "detail": "refused, most likely on location" if blocked
                          else f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        name = type(reason).__name__
        detail = {
            "gaierror": "the name does not resolve — DNS is blocked or offline",
            "timeout": "no answer before the timeout — filtered, or very slow",
            "ConnectionRefusedError": "the connection was refused",
            "SSLCertVerificationError": "the TLS certificate did not verify — "
                                        "something is intercepting the connection",
            "SSLError": "the secure connection failed — often interception",
        }.get(name, str(reason))
        return {"ok": False, "status": None, "ms": round((time.time() - started) * 1000),
                "detail": detail}
    except Exception as e:              # noqa: BLE001
        return {"ok": False, "status": None,
                "ms": round((time.time() - started) * 1000), "detail": str(e)}
