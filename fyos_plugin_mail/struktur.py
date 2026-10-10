"""Der MIME-Aufbau einer Mail aus der Antwort ``BODYSTRUCTURE``.

Der Server beschreibt den Aufbau (Teile, Typen, Kodierungen, Größen, Dateinamen), ohne die Teile zu schicken. Daraus
entscheidet das Plugin, welche Teile es holt (der Text) und was es nur auflistet (Anhänge), und lädt nie einen Anhang.

Teile werden mit der Abschnittsnummer des Servers angesprochen: ``1``, ``2``, ``1.2``. Eine Mail ohne Unterteile hat
genau einen Abschnitt, ``1``.
"""

from __future__ import annotations

import binascii
import email.errors
import email.header
import email.utils
import re
from dataclasses import dataclass, field
from typing import Any

from .antwort import AntwortFehler

TEILE_MAX = 300
TIEFE_MAX = 20
OHNE_NAME = "(ohne Namen)"
NAME_MAX = 200
#: Teile, die zu einer Signatur gehören und nichts sind, was der Mensch als Anhang kennt
_SIGNATUREN = frozenset({"application/pgp-signature", "application/pkcs7-signature", "application/x-pkcs7-signature"})
_STEUERZEICHEN = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class Teil:
    nummer: str
    typ: str  # klein: "text/plain", "multipart/alternative"
    parameter: dict[str, str] = field(default_factory=dict)  # Namen klein
    kodierung: str = ""  # klein: "7bit", "base64", "quoted-printable"
    groesse: int = 0  # Oktette, wie übertragen (also kodiert)
    disposition: str = ""  # "attachment" | "inline" | ""
    disposition_parameter: dict[str, str] = field(default_factory=dict)
    content_id: str = ""
    kinder: list[Teil] = field(default_factory=list)

    @property
    def mehrteilig(self) -> bool:
        return self.typ.startswith("multipart/")

    @property
    def hauptart(self) -> str:
        return self.typ.partition("/")[0]


def _text(wert: Any) -> str:
    if isinstance(wert, (bytes, bytearray)):
        return bytes(wert).decode("utf-8", errors="replace")
    return wert if isinstance(wert, str) else ""


def _parameter(roh: Any) -> dict[str, str]:
    """Eine Parameterliste ``("charset" "utf-8" "name" "x.pdf")`` als Wörterbuch, Namen klein."""
    if not isinstance(roh, list):
        return {}
    return {_text(n).lower(): _text(w) for n, w in zip(roh[::2], roh[1::2]) if _text(n)}


def _disposition(roh: Any) -> tuple[str, dict[str, str]]:
    if not isinstance(roh, list) or not roh:
        return "", {}
    return _text(roh[0]).lower(), _parameter(roh[1]) if len(roh) > 1 else {}


def _zahl(wert: Any) -> int:
    return int(wert) if isinstance(wert, str) and wert.isdigit() else 0


class _Zaehler:
    def __init__(self) -> None:
        self.anzahl = 0


def _teil(roh: Any, nummer: str, tiefe: int, zaehler: _Zaehler) -> Teil:
    zaehler.anzahl += 1
    if zaehler.anzahl > TEILE_MAX:
        raise AntwortFehler("die Mail hat zu viele Teile")
    if tiefe > TIEFE_MAX:
        raise AntwortFehler("die Mail ist zu tief verschachtelt")
    if not isinstance(roh, list) or not roh:
        raise AntwortFehler("BODYSTRUCTURE ist nicht lesbar")
    if isinstance(roh[0], list):  # mehrteilig: erst die Kinder, dann Untertyp und Erweiterungsdaten
        i = 0
        while i < len(roh) and isinstance(roh[i], list):
            i += 1
        untertyp = _text(roh[i]).lower() if i < len(roh) else "mixed"
        erweiterung = roh[i + 1:]
        disposition, disposition_parameter = _disposition(erweiterung[1]) if len(erweiterung) > 1 else ("", {})
        teil = Teil(
            nummer=nummer, typ=f"multipart/{untertyp or 'mixed'}",
            parameter=_parameter(erweiterung[0]) if erweiterung else {},
            disposition=disposition, disposition_parameter=disposition_parameter,
        )
        for n, kind in enumerate(roh[:i], 1):
            teil.kinder.append(_teil(kind, f"{nummer}.{n}" if nummer else str(n), tiefe + 1, zaehler))
        return teil
    haupt, unter = _text(roh[0]).lower(), _text(roh[1]).lower() if len(roh) > 1 else ""
    # Nach der Größe: Textteile haben die Zeilenzahl, message/rfc822 Umschlag, Körper und Zeilenzahl; dann folgen
    # MD5, Disposition, Sprache, Ort.
    md5 = 8 if haupt == "text" else 10 if (haupt, unter) == ("message", "rfc822") else 7
    disposition, disposition_parameter = _disposition(roh[md5 + 1]) if len(roh) > md5 + 1 else ("", {})
    return Teil(
        nummer=nummer or "1", typ=f"{haupt}/{unter}" if unter else haupt,
        parameter=_parameter(roh[2]) if len(roh) > 2 else {},
        content_id=_text(roh[3]) if len(roh) > 3 else "",
        kodierung=_text(roh[5]).lower() if len(roh) > 5 else "",
        groesse=_zahl(roh[6]) if len(roh) > 6 else 0,
        disposition=disposition, disposition_parameter=disposition_parameter,
    )


def baum(bodystructure: Any) -> Teil:
    """Der Aufbau als Baum von ``Teil``. ``AntwortFehler``, wenn die Antwort unbrauchbar ist."""
    return _teil(bodystructure, "", 0, _Zaehler())


# ── Dateinamen und Größen ───────────────────────────────────────────────────────────────────────────────
def _entschluesseln(text: str) -> str:
    """Kodierte Wörter (``=?utf-8?B?…?=``) im Dateinamen, wie viele Programme sie in den Namen schreiben."""
    if "=?" not in text:
        return text
    try:
        return str(email.header.make_header(email.header.decode_header(text))) or text
    except (email.errors.HeaderParseError, UnicodeError, LookupError, ValueError, binascii.Error):
        return text


def dateiname(teil: Teil) -> str:
    """Der Dateiname des Teils, falls er einen hat: aus der Disposition (``filename``) oder aus dem Typ (``name``),
    nach RFC 2231 (``filename*=utf-8''…``) und RFC 2047 aufgelöst, auf den Namen ohne Pfad gekürzt."""
    for parameter in (teil.disposition_parameter, teil.parameter):
        paare = [(n, w) for n, w in parameter.items() if n.split("*", 1)[0] in ("filename", "name")]
        if not paare:
            continue
        try:
            entschluesselt = email.utils.decode_params([("content-type", "text/plain"), *paare])[1:]
        except (ValueError, TypeError):
            entschluesselt = paare
        for name, wert in entschluesselt:
            if name in ("filename", "name"):
                if isinstance(wert, tuple) and len(wert) == 3:
                    # ``decode_params`` setzt den Wert eines ``filename*=`` in Anführungszeichen; ``collapse_rfc2231_value``
                    # nimmt sie bei einem Tripel nicht wieder ab (``Message.get_param`` tut es vorher).
                    wert = (wert[0], wert[1], email.utils.unquote(wert[2]))
                try:
                    text = email.utils.collapse_rfc2231_value(wert)
                except (LookupError, ValueError, TypeError):
                    text = wert if isinstance(wert, str) else ""
                text = _entschluesseln(text)
                text = _STEUERZEICHEN.sub("", text.replace("\\", "/").rsplit("/", 1)[-1]).strip()
                if text:
                    return text[:NAME_MAX]
    return ""


def groesse_dekodiert(teil: Teil) -> int:
    """Die Größe in Bytes nach dem Entkodieren, aus der Größe der Übertragung gerechnet. Bei base64 mit den üblichen
    76 Zeichen je Zeile stimmt sie auf wenige Bytes (das Füllzeichen am Ende ist nicht bekannt, sie ist dann höchstens
    zwei Bytes zu groß), sonst ist es die Größe der Übertragung."""
    if teil.kodierung == "base64":
        zeichen = teil.groesse - 2 * -(-teil.groesse // 78)  # je Zeile (76 Zeichen) zwei Zeichen Zeilenende
        return max(0, zeichen * 3 // 4)
    return teil.groesse


# ── Auswahl ─────────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Auswertung:
    text: list[Teil] = field(default_factory=list)  # Textteile ohne Dateinamen, text/plain, in der Reihenfolge der Mail
    html: list[Teil] = field(default_factory=list)  # ebenso text/html
    anhaenge: list[Teil] = field(default_factory=list)


def _ist_eingebettet(teil: Teil, eltern: str) -> bool:
    """Ein Teil, auf den das HTML verweist (Logo, Bild), kein Anhang, den der Mensch kennt."""
    if teil.typ in _SIGNATUREN:
        return True
    return bool(teil.content_id) and eltern == "multipart/related" and teil.hauptart != "text"


def _auswerten(teil: Teil, eltern: str) -> Auswertung:
    if teil.mehrteilig:
        ergebnisse = [_auswerten(k, teil.typ) for k in teil.kinder]
        if teil.typ == "multipart/alternative":
            # Dieselbe Mail in mehreren Fassungen: die erste, die Text hat, jeweils für Klartext und HTML.
            text = next((e.text for e in ergebnisse if e.text), [])
            html = next((e.html for e in ergebnisse if e.html), [])
        else:
            text = [t for e in ergebnisse for t in e.text]
            html = [t for e in ergebnisse for t in e.html]
        return Auswertung(text, html, [a for e in ergebnisse for a in e.anhaenge])
    if teil.disposition == "attachment":
        return Auswertung(anhaenge=[teil])
    if teil.typ in ("text/plain", "text/html") and not dateiname(teil):
        return Auswertung(text=[teil]) if teil.typ == "text/plain" else Auswertung(html=[teil])
    if _ist_eingebettet(teil, eltern):
        return Auswertung()
    return Auswertung(anhaenge=[teil])


def auswerten(wurzel: Teil) -> Auswertung:
    """Welche Teile den Text bilden und welche Anhänge sind."""
    return _auswerten(wurzel, "")


def anhang_beschreibung(teil: Teil) -> dict[str, Any]:
    return {"name": dateiname(teil) or OHNE_NAME, "typ": teil.typ, "groesse": groesse_dekodiert(teil)}
