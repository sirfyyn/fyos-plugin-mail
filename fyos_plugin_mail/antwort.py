"""Antworten eines IMAP-Servers lesen: Literale, geklammerte Listen, FETCH-, LIST- und STATUS-Zeilen.

``imaplib`` gibt die Daten eines Befehls als Liste zurück. Eine Antwortzeile ist ein ``bytes``; enthält sie ein Literal
(``{342}`` am Zeilenende, danach 342 Bytes), kommt sie als Tupel ``(Zeile bis zur Klammer, Literal)``, und der Rest der
Antwort steht in der nächsten Zeile. Eine Antwort endet beim ersten ``bytes``-Element. Hier wird daraus ein Strom von
Werten:

* ``str``    ein Atom (Zahl, Schlüsselwort, Kennzeichen wie ``\\Seen``)
* ``bytes``  ein IMAP-String (in Anführungszeichen oder als Literal), genau so, wie der Server ihn schickte
* ``None``   ``NIL``
* ``list``   eine Klammergruppe

Nichts hier spricht mit dem Server; es sind reine Lesefunktionen und deshalb einzeln testbar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import utf7

_PLATZHALTER = re.compile(rb"\x00L(\d+)\x00")
_LITERALMARKE = re.compile(rb"\{\d+\+?\}$")
_ATOMENDE = frozenset(b' \t\r\n()"\x00')
TIEFE_MAX = 40


class AntwortFehler(ValueError):
    """Die Antwort des Servers ließ sich nicht lesen."""


def zerlegen(daten: list[Any]) -> list[tuple[bytes, list[bytes]]]:
    """Teilt die Daten eines Befehls in Antworten. Jede ist ein Puffer, in dem Literale durch Platzhalter ersetzt sind,
    und die Liste der Literale."""
    antworten: list[tuple[bytes, list[bytes]]] = []
    puffer = b""
    literale: list[bytes] = []
    angefangen = False
    for stueck in daten:
        if stueck is None:
            continue
        if isinstance(stueck, tuple):
            zeile, literal = stueck[0], stueck[1]
            puffer += _LITERALMARKE.sub(b"", zeile) + b"\x00L%d\x00" % len(literale)
            literale.append(literal)
            angefangen = True
            continue
        if isinstance(stueck, (bytes, bytearray)):
            antworten.append((puffer + bytes(stueck), literale))
            puffer, literale, angefangen = b"", [], False
    if angefangen:
        raise AntwortFehler("die Antwort des Servers bricht mitten in einem Literal ab")
    return antworten


class _Leser:
    def __init__(self, puffer: bytes, literale: list[bytes]) -> None:
        self.puffer = puffer
        self.literale = literale
        self.i = 0

    def ende(self) -> bool:
        self._leerraum()
        return self.i >= len(self.puffer)

    def _leerraum(self) -> None:
        while self.i < len(self.puffer) and self.puffer[self.i] in b" \t\r\n":
            self.i += 1

    def wert(self, tiefe: int = 0) -> Any:
        self._leerraum()
        if self.i >= len(self.puffer):
            raise AntwortFehler("unerwartetes Ende der Antwort")
        zeichen = self.puffer[self.i]
        if zeichen == 0x28:  # (
            return self._gruppe(tiefe)
        if zeichen == 0x22:  # "
            return self._zitat()
        if zeichen == 0x00:
            treffer = _PLATZHALTER.match(self.puffer, self.i)
            if treffer and int(treffer.group(1)) < len(self.literale):
                self.i = treffer.end()
                return self.literale[int(treffer.group(1))]
            raise AntwortFehler("unerwartetes Zeichen in der Antwort")
        if zeichen == 0x29:  # )
            raise AntwortFehler("unerwartete Klammer in der Antwort")
        return self._atom()

    def _gruppe(self, tiefe: int) -> list[Any]:
        if tiefe >= TIEFE_MAX:
            raise AntwortFehler("die Antwort ist zu tief verschachtelt")
        self.i += 1
        aus: list[Any] = []
        while True:
            self._leerraum()
            if self.i >= len(self.puffer):
                raise AntwortFehler("Klammer nicht geschlossen")
            if self.puffer[self.i] == 0x29:
                self.i += 1
                return aus
            aus.append(self.wert(tiefe + 1))

    def _zitat(self) -> bytes:
        self.i += 1
        aus = bytearray()
        while self.i < len(self.puffer):
            zeichen = self.puffer[self.i]
            if zeichen == 0x5C and self.i + 1 < len(self.puffer):  # Rückstrich: das nächste Zeichen gilt wörtlich
                self.i += 1
                aus.append(self.puffer[self.i])
            elif zeichen == 0x22:
                self.i += 1
                return bytes(aus)
            else:
                aus.append(zeichen)
            self.i += 1
        raise AntwortFehler("Anführungszeichen nicht geschlossen")

    def _atom(self) -> str | None:
        anfang = self.i
        while self.i < len(self.puffer):
            zeichen = self.puffer[self.i]
            if zeichen == 0x5B:  # [ ... ]: Abschnittsangabe wie BODY[HEADER.FIELDS (FROM TO)] gehört zum Atom
                ende = self.puffer.find(b"]", self.i)
                if ende < 0:
                    raise AntwortFehler("eckige Klammer nicht geschlossen")
                self.i = ende + 1
                continue
            if zeichen in _ATOMENDE:
                break
            self.i += 1
        text = self.puffer[anfang:self.i].decode("latin-1")
        if not text:
            raise AntwortFehler("leeres Atom in der Antwort")
        return None if text.upper() == "NIL" else text


def werte(puffer: bytes, literale: list[bytes] | None = None) -> list[Any]:
    """Alle Werte einer Zeile nacheinander (Gruppen als Listen)."""
    leser = _Leser(puffer, literale or [])
    aus: list[Any] = []
    while not leser.ende():
        aus.append(leser.wert())
    return aus


# ── FETCH ───────────────────────────────────────────────────────────────────────────────────────────────
def _schluessel(roh: str) -> str:
    return re.sub(r"\s+", " ", roh.strip()).upper()


def _zahl(wert: Any) -> int | None:
    return int(wert) if isinstance(wert, str) and wert.isdigit() else None


def _text(wert: Any) -> str:
    return wert.decode("utf-8", errors="replace") if isinstance(wert, (bytes, bytearray)) else (wert or "")


def fetch_antworten(daten: list[Any]) -> list[dict[str, Any]]:
    """Die FETCH-Antworten eines ``UID FETCH`` als Wörterbücher. Schlüssel in Großbuchstaben: ``UID`` (Zahl), ``FLAGS``
    (Liste von Text), ``INTERNALDATE`` (Text), ``RFC822.SIZE`` (Zahl), ``BODYSTRUCTURE`` (verschachtelte Listen),
    ``X-GM-LABELS`` (Liste von Text) und je Abschnitt ``BODY[…]`` oder ``BODY[…]<ursprung>`` (``bytes``). Antworten ohne
    ``UID`` (der Server meldet sie ungefragt) fehlen."""
    aus: list[dict[str, Any]] = []
    for puffer, literale in zerlegen(daten):
        leser = _Leser(puffer, literale)
        laufnummer = leser.wert()
        if not isinstance(laufnummer, str):
            raise AntwortFehler("FETCH-Antwort ohne Laufnummer")
        paare = leser.wert()
        if not isinstance(paare, list) or len(paare) % 2:
            raise AntwortFehler("FETCH-Antwort ohne Paare aus Name und Wert")
        eintrag: dict[str, Any] = {}
        for name, wert in zip(paare[::2], paare[1::2]):
            if not isinstance(name, str):
                raise AntwortFehler("FETCH-Antwort mit unlesbarem Namen")
            schluessel = _schluessel(name)
            if schluessel in ("UID", "RFC822.SIZE"):
                zahl = _zahl(wert)
                if zahl is None:
                    raise AntwortFehler(f"{schluessel} ist keine Zahl")
                eintrag[schluessel] = zahl
            elif schluessel in ("FLAGS", "X-GM-LABELS"):
                eintrag[schluessel] = [_text(x) for x in wert] if isinstance(wert, list) else []
            elif schluessel == "INTERNALDATE":
                eintrag[schluessel] = _text(wert)
            else:
                eintrag[schluessel] = wert
        if "UID" in eintrag:
            aus.append(eintrag)
    return aus


def abschnitt(antwort: dict[str, Any], angabe: str) -> bytes | None:
    """Der Inhalt von ``BODY[angabe]`` (mit oder ohne Ursprung ``<0>``) aus einer FETCH-Antwort; ``None``, wenn der
    Server ihn nicht lieferte oder ``NIL`` schickte."""
    vorn = f"BODY[{_schluessel(angabe)}]"
    for schluessel, wert in antwort.items():
        if schluessel == vorn or schluessel.startswith(vorn + "<"):
            return bytes(wert) if isinstance(wert, (bytes, bytearray)) else None
    if _schluessel(angabe).startswith("HEADER"):
        # Manche Server nennen die Felder der Kopfzeilen in anderer Reihenfolge oder Schreibweise zurück.
        for schluessel, wert in antwort.items():
            if schluessel.startswith("BODY[HEADER"):
                return bytes(wert) if isinstance(wert, (bytes, bytearray)) else None
    return None


# ── LIST und STATUS ─────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Ordner:
    name: str  # im Klartext (IMAP-UTF-7 ist aufgelöst)
    attribute: frozenset[str]  # klein, mit Rückstrich: {"\\hasnochildren", "\\sent"}
    trenner: str = ""

    @property
    def waehlbar(self) -> bool:
        return not ({"\\noselect", "\\nonexistent"} & self.attribute)


def ordner_liste(daten: list[Any]) -> list[Ordner]:
    """Die Ordner aus den Antworten auf ``LIST``."""
    aus: list[Ordner] = []
    for puffer, literale in zerlegen(daten):
        teile = werte(puffer, literale)
        if len(teile) != 3 or not isinstance(teile[0], list):
            continue
        attribute, trenner, name = teile
        if isinstance(name, str):
            text = name
        elif isinstance(name, (bytes, bytearray)):
            text = bytes(name).decode("utf-8", errors="replace")
        else:
            continue
        aus.append(Ordner(
            name=utf7.dekodieren(text),
            attribute=frozenset(_text(a).lower() for a in attribute if isinstance(a, (str, bytes))),
            trenner=_text(trenner),
        ))
    return aus


def status_zahlen(daten: list[Any]) -> dict[str, int]:
    """Die Zahlen aus einer Antwort auf ``STATUS``: ``{"UNSEEN": 12}``."""
    for puffer, literale in zerlegen(daten):
        teile = werte(puffer, literale)
        for teil in teile:
            if isinstance(teil, list):
                return {
                    _text(name).upper(): int(zahl)
                    for name, zahl in zip(teil[::2], teil[1::2])
                    if isinstance(zahl, str) and zahl.isdigit()
                }
    return {}
