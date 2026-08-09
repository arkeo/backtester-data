"""Making a published bundle useless without the application.

What this does and does not achieve
-----------------------------------
The key ships inside the application, on the customer's own machine. Anyone
willing to disassemble it can recover the key, and anyone watching the network
can see the address. **This is not protection against a determined person and
nothing that runs on someone else's computer ever can be.**

What it does achieve is that a file pulled from the mirror is opaque: it is not
a zip, it does not open, there are no filenames in it, and there is nothing to
do with it but feed it back into the application. That removes the entire
casual case — a customer who finds the address and shares the link, or unzips a
bundle and passes the CSVs around. That is the realistic threat, and this stops
it.

It is deterrence, deliberately, and it should be budgeted as deterrence.

How
---
SHAKE256 as a stream cipher. It is a NIST extendable-output function, so one
call produces a keystream of exactly the length needed, and the XOR is a single
numpy operation — ten megabytes takes a few hundred milliseconds with nothing
outside the standard library. An HMAC over the ciphertext detects tampering and
tells a corrupt download apart from a wrong key.
"""

from __future__ import annotations

import hashlib
import hmac
import os

import numpy as np

MAGIC = b"BTD2"
NONCE_BYTES = 16
MAC_BYTES = 32
HEADER = len(MAGIC) + 1 + NONCE_BYTES


class WrongKey(Exception):
    """The file did not come from this publisher, or is damaged."""


def _keystream(key: bytes, nonce: bytes, length: int) -> np.ndarray:
    raw = hashlib.shake_256(b"backtester-stream" + key + nonce).digest(length)
    return np.frombuffer(raw, dtype=np.uint8)


def _mac_key(key: bytes, nonce: bytes) -> bytes:
    return hashlib.sha256(b"backtester-mac" + key + nonce).digest()


def encrypt(plain: bytes, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    data = np.frombuffer(plain, dtype=np.uint8)
    cipher = (data ^ _keystream(key, nonce, len(plain))).tobytes()
    body = MAGIC + bytes([1]) + nonce + cipher
    return body + hmac.new(_mac_key(key, nonce), body, hashlib.sha256).digest()


def decrypt(blob: bytes, key: bytes) -> bytes:
    if not looks_encrypted(blob):
        raise WrongKey("this is not a published bundle")
    if len(blob) < HEADER + MAC_BYTES:
        raise WrongKey("the file is truncated")

    nonce = blob[len(MAGIC) + 1:HEADER]
    body, mac = blob[:-MAC_BYTES], blob[-MAC_BYTES:]
    expected = hmac.new(_mac_key(key, nonce), body, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        # One message for both cases on purpose: which of the two it is, is not
        # something a caller should be able to probe for.
        raise WrongKey("this bundle was not published for this application, "
                       "or the download is damaged")

    cipher = np.frombuffer(body[HEADER:], dtype=np.uint8)
    return (cipher ^ _keystream(key, nonce, len(cipher))).tobytes()


def looks_encrypted(blob: bytes) -> bool:
    return blob[:len(MAGIC)] == MAGIC


def name_for(symbol: str, key: bytes) -> str:
    """An opaque, stable filename for an instrument.

    Publishing EURUSD.btdata tells anyone browsing the bucket exactly what is
    in it and how to guess the rest. This is derived from the key, so the same
    publisher always produces the same name and a different one produces
    different names.
    """
    digest = hashlib.sha256(b"backtester-name" + key + symbol.upper().encode())
    return digest.hexdigest()[:32] + ".bin"
