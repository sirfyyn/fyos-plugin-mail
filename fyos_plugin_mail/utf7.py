"""Ordnernamen im Format „IMAP-UTF-7“ (RFC 3501, Abschnitt 5.1.3).

Ohne ``ENABLE UTF8=ACCEPT`` schreiben IMAP-Server Ordnernamen so: druckbares ASCII steht, wie es ist (``&`` als ``&-``),
alles andere wird als UTF-16 in einer abgewandelten Base64 (``,`` statt ``/``, ohne Auffüllen) zwischen ``&`` und ``-``
geschrieben. Das Plugin spricht Ordner im Klartext (``Entwürfe``) und wandelt nur an der Grenze zum Server.
"""

from __future__ import annotations

import base64
import binascii
import re

_KODIERT = re.compile(r"&([^-]*)-")


def kodieren(text: str) -> str:
    """Ein Ordnername im Klartext als IMAP-UTF-7 (nur ASCII)."""
    aus: list[str] = []
    puffer: list[str] = []

    def leeren() -> None:
        if puffer:
            roh = "".join(puffer).encode("utf-16-be", errors="surrogatepass")
            aus.append("&" + base64.b64encode(roh).decode("ascii").rstrip("=").replace("/", ",") + "-")
            puffer.clear()

    for zeichen in text:
        if 0x20 <= ord(zeichen) <= 0x7E:
            leeren()
            aus.append("&-" if zeichen == "&" else zeichen)
        else:
            puffer.append(zeichen)
    leeren()
    return "".join(aus)


def dekodieren(text: str) -> str:
    """Ein Ordnername aus IMAP-UTF-7 im Klartext. Was sich nicht lesen lässt, bleibt unverändert stehen."""

    def ersetzen(treffer: re.Match[str]) -> str:
        if treffer.group(1) == "":
            return "&"
        b64 = treffer.group(1).replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            return base64.b64decode(b64, validate=True).decode("utf-16-be")
        except (binascii.Error, ValueError):
            return treffer.group(0)

    return _KODIERT.sub(ersetzen, text)
