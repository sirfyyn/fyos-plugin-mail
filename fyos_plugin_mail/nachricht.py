"""Aus den Rohdaten einer Mail lesbaren Text machen: Kopfzeilen, Kodierungen, Zeichensätze, HTML, Ausschnitt.

Alles hier ist tolerant: Mails im Netz sind oft kaputt (falsche Zeichensätze, abgeschnittene Kodierungen, Kopfzeilen mit
Müll). Was sich nicht lesen lässt, wird ersetzt oder weggelassen, es wirft nie. Kopfzeilen, Betreff und Text sind
**Fremdinhalt**: sie werden nur gekürzt, von Steuerzeichen befreit und weitergegeben, nie ausgewertet.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import email.header
import email.parser
import email.policy
import email.utils
import quopri
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

HTML_MAX = 2 * 1024 * 1024
OHNE_BETREFF = "(kein Betreff)"
_STEUER = {c: None for c in (*range(32), 127)}
_NUR_TEXT = {c: None for c in (*range(32), 127) if c not in (9, 10)}
_MONATE = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_INTERNALDATE = re.compile(
    r'\s*"?\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)\s*"?\s*')
_KOPF_PARSER = email.parser.BytesHeaderParser(policy=email.policy.default)


# ── Zeilen und Zeiten ───────────────────────────────────────────────────────────────────────────────────
def zeile(wert: Any, grenze: int | None = None) -> str:
    """Ein Wert als eine Zeile: Leerraum zusammengezogen, Steuerzeichen entfernt, auf ``grenze`` Zeichen gekürzt."""
    text = " ".join(str(wert).split()).translate(_STEUER) if wert is not None else ""
    return text[:grenze] if grenze else text


def kurz(text: str, grenze: int) -> str:
    return text if len(text) <= grenze else text[: grenze - 1].rstrip() + "…"


def internaldatum(text: str) -> datetime | None:
    """``INTERNALDATE`` des Servers (``10-Oct-2026 12:34:56 +0200``) als Zeitpunkt mit Versatz."""
    treffer = _INTERNALDATE.fullmatch(text or "")
    if not treffer:
        return None
    tag, monat, jahr, stunde, minute, sekunde, vorzeichen, vh, vm = treffer.groups()
    nummer = _MONATE.get(monat.lower())
    if nummer is None:
        return None
    versatz = timedelta(hours=int(vh), minutes=int(vm)) * (-1 if vorzeichen == "-" else 1)
    try:
        return datetime(int(jahr), nummer, int(tag), int(stunde), int(minute), int(sekunde),
                        tzinfo=timezone(versatz))
    except ValueError:
        return None


def kopfdatum(text: str) -> datetime | None:
    """Das ``Date``-Feld einer Mail als Zeitpunkt; ``None``, wenn es nicht lesbar oder offensichtlich Unsinn ist."""
    try:
        zeit = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if zeit is None or not 1990 <= zeit.year <= 2100:
        return None
    return zeit if zeit.tzinfo is not None else zeit.replace(tzinfo=timezone.utc)


# ── Kopfzeilen ──────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Kopf:
    von: str = ""
    an: str = ""
    betreff: str = ""
    datum: datetime | None = None


def ausgebbar(text: str) -> str:
    """Text, der sich als UTF-8 schreiben lässt. Die Standardbibliothek (``email``) reicht Bytes, die sie nicht lesen kann,
    als einzelne Ersatzzeichen (U+DC80 bis U+DCFF) weiter, je nach Python-Version bei Kopfzeilen, die sie nicht
    auswerten kann (Python 3.11: ``To: Subject: a.org;\\xc3``); sie ließen sich weder ausgeben noch als JSON senden. Hier
    werden sie wieder zu Bytes und neu gelesen: gültiges UTF-8 wird Text, der Rest wird zu U+FFFD."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:  # andere einzelne Ersatzzeichen als die für 8-Bit-Bytes
        return text.encode("utf-8", "replace").decode("utf-8")


def _kopfwert(nachricht: Any, name: str) -> str:
    try:
        wert = nachricht[name]
        return ausgebbar(str(wert)) if wert is not None else ""
    except Exception:  # noqa: BLE001 — kaputte Kopfzeile: den rohen Text nehmen, kodierte Wörter lösen, sonst nichts
        for schluessel, roh in nachricht.raw_items():
            if schluessel.lower() == name.lower():
                try:
                    return ausgebbar(str(email.header.make_header(email.header.decode_header(str(roh)))))
                except Exception:  # noqa: BLE001
                    return ausgebbar(str(roh))
        return ""


def kopf_lesen(roh: bytes | None) -> Kopf:
    """Absender, Empfänger, Betreff und Datum aus den rohen Kopfzeilen (die Antwort auf ``HEADER.FIELDS``)."""
    if not roh:
        return Kopf()
    try:
        nachricht = _KOPF_PARSER.parsebytes(roh)
    except Exception:  # noqa: BLE001 — der Parser soll nie werfen; falls doch, ist die Mail ohne Kopf besser als keine
        return Kopf()
    return Kopf(
        von=_kopfwert(nachricht, "From"), an=_kopfwert(nachricht, "To"), betreff=_kopfwert(nachricht, "Subject"),
        datum=kopfdatum(_kopfwert(nachricht, "Date")),
    )


# ── Kodierungen ─────────────────────────────────────────────────────────────────────────────────────────
def entkodieren(roh: bytes, kodierung: str, abgeschnitten: bool = False) -> bytes:
    """Hebt die Übertragungskodierung (``base64``, ``quoted-printable``) auf. ``abgeschnitten``: der Teil endet mitten
    in der Kodierung, ein unvollständiger Schluss (``=A`` bei quoted-printable) wird weggelassen."""
    if kodierung == "base64":
        zeichen = re.sub(rb"[^A-Za-z0-9+/]", b"", roh)  # Zeilenenden, Füllzeichen und Müll fallen weg
        if len(zeichen) % 4 == 1:  # ein einzelnes Zeichen am Ende trägt kein ganzes Byte
            zeichen = zeichen[:-1]
        zeichen += b"=" * (-len(zeichen) % 4)
        try:
            return base64.b64decode(zeichen)
        except (binascii.Error, ValueError):
            return b""
    if kodierung == "quoted-printable":
        if abgeschnitten:
            roh = re.sub(rb"=[0-9A-Fa-f]?$", b"", roh)
        return quopri.decodestring(roh)
    return roh


def _kandidaten(zeichensatz: str) -> list[str]:
    liste: list[str] = []
    name = (zeichensatz or "").strip().strip('"').strip("'").lower()
    if name and len(name) <= 60 and name not in ("us-ascii", "ascii", "unknown-8bit", "x-unknown"):
        try:
            # Ein Byte probehalber: Python weist hier Namen ab, die es nicht gibt oder die keinen Text liefern (hex, zlib,
            # base64, quopri, rot13 …). Bei leerer Eingabe prüft es nichts.
            b"a".decode(name)
            liste.append(name)
        except UnicodeError:
            liste.append(name)  # eine Textkodierung, die dieses eine Byte nur nicht liest
        except Exception:  # noqa: BLE001 — der Name steht in der Mail; was auch schiefgeht, zählt als „kenne ich nicht“
            pass
    if "utf-8" not in liste:
        liste.append("utf-8")
    return liste


def dekodieren(roh: bytes, zeichensatz: str = "", abgeschnitten: bool = False) -> str:
    """Bytes als Text: der angegebene Zeichensatz, sonst UTF-8, sonst Windows-1252 (mit Ersatzzeichen). Endet der Teil
    mitten in einem Zeichen (``abgeschnitten``), fällt das letzte, unvollständige Zeichen weg. Der Zeichensatz kommt aus
    der Mail und ist nur ein Name: ``hex``, ``zlib``, ``base64`` und Verwandte, die keinen Text liefern, zählen nicht."""
    for kandidat in _kandidaten(zeichensatz):
        try:
            ergebnis = codecs.getincrementaldecoder(kandidat)("strict").decode(roh, final=not abgeschnitten)
        except Exception:  # noqa: BLE001 — auch ein seltsamer Zeichensatz darf das Lesen nie abbrechen
            continue
        if isinstance(ergebnis, str):
            return ergebnis
    return codecs.getincrementaldecoder("cp1252")("replace").decode(roh, final=not abgeschnitten)


def text_bereinigen(text: str) -> str:
    """Zeilenenden vereinheitlichen, Steuerzeichen entfernen, leere Zeilen zusammenziehen."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_NUR_TEXT).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── HTML ────────────────────────────────────────────────────────────────────────────────────────────────
class _HtmlText(HTMLParser):
    """Zieht den sichtbaren Text aus HTML. Linear in der Länge, auch bei kaputtem oder bösartigem HTML."""

    _VOR = frozenset((
        "br", "p", "div", "tr", "li", "ul", "ol", "table", "blockquote", "pre", "hr", "section", "article",
        "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    ))
    _NACH = frozenset((
        "p", "div", "table", "ul", "ol", "blockquote", "pre", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6",
    ))
    _ZELLE = frozenset(("td", "th"))
    _UNSICHTBAR = frozenset(("script", "style", "head", "title"))

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.teile: list[str] = []
        self._stumm: str | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "body":
            self._stumm = None  # ein ungeschlossenes <head> verschluckt nicht den Text
        elif self._stumm is None and tag in self._UNSICHTBAR:
            self._stumm = tag
        elif tag in self._VOR:
            self.teile.append("\n")
        elif tag in self._ZELLE:
            self.teile.append(" ")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if self._stumm is None and tag in self._VOR:
            self.teile.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == self._stumm:
            self._stumm = None
        elif tag in self._NACH:
            self.teile.append("\n")

    def handle_data(self, data: str) -> None:
        if self._stumm is None:
            self.teile.append(data)


def html_zu_text(html: str) -> str:
    """Der sichtbare Text einer HTML-Mail (ohne Skripte und Stile), Absätze durch Zeilenumbrüche getrennt."""
    parser = _HtmlText()
    try:
        parser.feed((html or "")[:HTML_MAX])
        parser.close()
    except Exception:  # noqa: BLE001 — Parser-Fehler bei kaputtem HTML: es bleibt, was bis dahin gelesen war
        pass
    text = "".join(parser.teile).translate(_NUR_TEXT)
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def ausschnitt_aus(text: str, grenze: int) -> str:
    """Der Anfang des Textes in einer Zeile."""
    return kurz(" ".join(text.split()).translate(_STEUER), grenze)
