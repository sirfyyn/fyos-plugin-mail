"""Die Gmail-artige Abfrage der Postfach-Quelle lesen und für IMAP SEARCH übersetzen.

Die gemeinsame Teilmenge, die jede Quelle kann (siehe ``mcp_hub.postfach``): ``from:``, ``to:``, ``subject:``,
``is:unread``, ``newer_than:Nd``, ``older_than:Nd``, ``in:inbox`` und freie Wörter. Dieses Plugin versteht darüber hinaus
``in:sent``, ``in:drafts``, ``in:trash``, ``in:spam``, ``is:read``, ``newer_than`` und ``older_than`` auch mit ``w`` (7
Tage), ``m`` (30 Tage) und ``y`` (365 Tage), ``after:JJJJ/MM/TT``, ``before:JJJJ/MM/TT`` und die Verneinung mit
vorangestelltem ``-`` (``-from:alex``, ``-newsletter``). Alles andere (``label:``, ``category:``, ``has:attachment`` …) wird
überlesen und in ``Plan.ignoriert`` genannt, nie als Fehler zurückgewiesen. Dasselbe gilt für ``OR`` und Gruppen in
Klammern: die Begriffe daran werden überlesen, weil jeder einzeln als „und“ gelesen weniger Treffer ergäbe, als gemeint
sind. Bei einem Gmail-Konto geht die Abfrage dagegen unverändert an Gmail (``X-GM-RAW``), dort gilt die ganze Gmail-Suche.

Die Übersetzung in IMAP SEARCH ist ein reines Rechnen auf Texten, ohne Netz. IMAP kennt Umlaute in Suchwörtern nur als
Literal und ein Befehl darf nur ein Literal am Ende tragen. Enthält die Abfrage mehrere Wörter mit Umlauten, wird sie
deshalb in mehrere Suchen zerlegt (je Wort mit Umlaut eine, dazu alle übrigen Bedingungen), und das Plugin schneidet die
Ergebnisse. Das ist gleichwertig, weil alle Bedingungen durch „und“ verbunden sind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

BEGRIFFE_MAX = 12
WERT_MAX = 200
_MONATE = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
#: ``in:wert`` → Rolle des Ordners
ROLLEN = {
    "inbox": "inbox", "sent": "sent", "drafts": "drafts", "draft": "drafts", "trash": "trash", "spam": "spam",
    "junk": "spam",
}
_AUSWERTBAR = frozenset({"from", "to", "subject", "is", "newer_than", "older_than", "in", "after", "before"})
#: Gmail-Operatoren, die dieses Plugin kennt und überliest. Andere Wörter mit Doppelpunkt (``12:30``, Adressen im Text)
#: bleiben freie Wörter.
_UEBERLESEN = frozenset({
    "label", "category", "has", "filename", "size", "larger", "smaller", "cc", "bcc", "deliveredto", "list",
    "rfc822msgid", "older", "newer", "replyto",
})
_BEKANNT = _AUSWERTBAR | _UEBERLESEN
_OPERATOR = re.compile(r"([A-Za-z_0-9]+):(?=\S)")
_STEUERZEICHEN = re.compile(r"[\x00-\x1f\x7f]")
_TAGE = re.compile(r"(\d{1,4})([dwmy])", re.I)
_DATUM = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")
_EINHEIT = {"d": 1, "w": 7, "m": 30, "y": 365}
_KLAMMERN_AUF = "({"
_KLAMMERN_ZU = ")}"


@dataclass(frozen=True)
class Begriff:
    art: str  # von | an | betreff | wort | ungelesen | neuer | aelter | nach | vor
    wert: str = ""  # Text; bei neuer und aelter die Zahl der Tage, bei nach und vor das Datum (JJJJ-MM-TT)
    negiert: bool = False


@dataclass(frozen=True)
class Plan:
    text: str  # die Abfrage mit zusammengezogenem Leerraum; ohne Angabe ``in:inbox``
    begriffe: tuple[Begriff, ...]
    rollen: tuple[str, ...]  # Ordner aus ``in:``, etwa ("inbox",); leer: die Ordner des Kontos
    ignoriert: tuple[str, ...]  # Begriffe, die das Plugin nicht auswertet (nur zur Auskunft)


def _zerlegen(text: str) -> list[tuple[bool, str, str, bool]]:
    """(verneint, Operator klein oder "", Wert, in einer Gruppe) je Begriff. Werte in Anführungszeichen behalten ihre
    Leerzeichen. Eine Gruppe ist, was zwischen ``(`` und ``)`` oder ``{`` und ``}`` steht (Gmail: Gruppierung, ``{a b}``
    heißt „a oder b“)."""
    aus: list[tuple[bool, str, str, bool]] = []
    i, n = 0, len(text)
    tiefe = 0
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text[i] == "-" and (i + 1 >= n or text[i + 1].isspace()):  # ein einzelner Strich verneint nichts
            i += 1
            continue
        verneint = False
        davor = tiefe
        while i < n:  # Vorspann: öffnende Klammern und ein Minus, in beliebiger Folge
            if text[i] in _KLAMMERN_AUF:
                tiefe += 1
            elif text[i] == "-" and not verneint and i + 1 < n and not text[i + 1].isspace():
                verneint = True
            else:
                break
            i += 1
        operator = ""
        treffer = _OPERATOR.match(text, i)
        if treffer and treffer.group(1).lower() in _BEKANNT:
            operator = treffer.group(1).lower()
            i = treffer.end()
        if i < n and text[i] == '"':
            ende = text.find('"', i + 1)
            wert, i = (text[i + 1:], n) if ende < 0 else (text[i + 1:ende], ende + 1)
            while tiefe > 0 and i < n and text[i] in _KLAMMERN_ZU:  # ``subject:"a b")``
                tiefe -= 1
                i += 1
        else:
            j = i
            while j < n and not text[j].isspace():
                j += 1
            wert, i = text[i:j], j
            while tiefe > 0 and wert and wert[-1] in _KLAMMERN_ZU:  # nur so viele schließende Klammern, wie offen sind
                wert = wert[:-1]
                tiefe -= 1
        if wert or operator:
            # in einer Gruppe: schon vorher offen, oder offen geblieben. ``(Entwurf)`` schließt sich selbst und gilt als Wort.
            aus.append((verneint, operator, wert.strip(), davor > 0 or tiefe > 0))
    return aus


def _ist_oder(teil: tuple[bool, str, str, bool]) -> bool:
    return not teil[0] and not teil[1] and teil[2] == "OR"


def _oder_gebunden(teile: list[tuple[bool, str, str, bool]], k: int) -> bool:
    """Gehört der Begriff ``k`` zu einer Gruppe oder steht er an einem ``OR``? Diese Verknüpfungen versteht nur Gmail.
    Ihre Begriffe einzeln als „und“ zu lesen, ergäbe weniger Treffer, als gemeint sind; deshalb werden sie überlesen
    und genannt."""
    return (
        teile[k][3] or _ist_oder(teile[k]) or (k > 0 and _ist_oder(teile[k - 1]))
        or (k + 1 < len(teile) and _ist_oder(teile[k + 1]))
    )


def _tage(wert: str) -> int | None:
    treffer = _TAGE.fullmatch(wert.strip())
    return int(treffer.group(1)) * _EINHEIT[treffer.group(2).lower()] if treffer else None


def _datum(wert: str) -> str | None:
    treffer = _DATUM.fullmatch(wert.strip())
    if not treffer:
        return None
    try:
        return date(int(treffer.group(1)), int(treffer.group(2)), int(treffer.group(3))).isoformat()
    except ValueError:
        return None


def planen(abfrage: str) -> Plan:
    """Liest eine Abfrage. ``ValueError``, wenn sie unbrauchbar groß ist; sonst nie ein Fehler."""
    text = " ".join(_STEUERZEICHEN.sub(" ", "" if abfrage is None else str(abfrage)).split()) or "in:inbox"
    begriffe: list[Begriff] = []
    rollen: list[str] = []
    ignoriert: list[str] = []
    teile = _zerlegen(text)
    for k, (verneint, operator, wert, _gruppe) in enumerate(teile):
        original = ("-" if verneint else "") + (operator + ":" if operator else "") + wert
        if len(wert) > WERT_MAX:
            raise ValueError(f"ein Suchbegriff ist länger als {WERT_MAX} Zeichen")
        if not operator and wert == "AND":
            continue  # „und“ ist ohnehin die Verknüpfung
        if _oder_gebunden(teile, k):
            ignoriert.append(original)  # lieber mehr Treffer als ein Filter, der etwas anderes meint, als geschrieben steht
        elif not operator:
            if wert:
                begriffe.append(Begriff("wort", wert, verneint))
            else:
                ignoriert.append(original)
        elif operator in ("from", "to", "subject"):
            if wert:
                begriffe.append(Begriff({"from": "von", "to": "an", "subject": "betreff"}[operator], wert, verneint))
            else:
                ignoriert.append(original)
        elif operator == "is" and wert.lower() in ("unread", "read"):
            begriffe.append(Begriff("ungelesen", "", verneint != (wert.lower() == "read")))
        elif operator in ("newer_than", "older_than") and (tage := _tage(wert)) is not None:
            begriffe.append(Begriff("neuer" if operator == "newer_than" else "aelter", str(tage), verneint))
        elif operator in ("after", "before") and (tag := _datum(wert)) is not None:
            begriffe.append(Begriff("nach" if operator == "after" else "vor", tag, verneint))
        elif operator == "in" and wert.lower() in ROLLEN and not verneint:
            if ROLLEN[wert.lower()] not in rollen:
                rollen.append(ROLLEN[wert.lower()])
        else:
            ignoriert.append(original)
    if len(begriffe) > BEGRIFFE_MAX:
        raise ValueError(f"die Abfrage hat zu viele Begriffe (höchstens {BEGRIFFE_MAX})")
    return Plan(text, tuple(begriffe), tuple(rollen), tuple(ignoriert))


# ── IMAP SEARCH ─────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Gruppe:
    """Eine Suche: Schlüssel in ASCII, und, wenn ein Wort Umlaute hat, dessen Wert als Literal nach dem letzten
    Schlüssel. Mit Literal steht ``CHARSET UTF-8`` vor den Schlüsseln."""

    schluessel: tuple[str, ...]
    literal: bytes | None = None


def zitieren(wert: str) -> str:
    """Ein ASCII-Wert als IMAP-String in Anführungszeichen."""
    return '"' + wert.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_datum(tag: date) -> str:
    return f"{tag.day:02d}-{_MONATE[tag.month - 1]}-{tag.year}"


def _ascii(wert: str) -> bool:
    return wert.isascii() and all(32 <= ord(z) < 127 for z in wert)


def _schluessel(b: Begriff, heute: date) -> tuple[list[str], str | None]:
    """(Schlüssel, Wert): ``Wert`` ist der Text, der hinter dem letzten Schlüssel steht; ``None`` bei Schlüsseln ohne Text."""
    nicht = ["NOT"] if b.negiert else []
    if b.art in ("von", "an", "betreff", "wort"):
        return nicht + [{"von": "FROM", "an": "TO", "betreff": "SUBJECT", "wort": "TEXT"}[b.art]], b.wert
    if b.art == "ungelesen":
        return ["SEEN" if b.negiert else "UNSEEN"], None
    if b.art in ("neuer", "aelter"):
        stichtag = heute - timedelta(days=int(b.wert))
        return nicht + ["SINCE" if b.art == "neuer" else "BEFORE", imap_datum(stichtag)], None
    stichtag = date.fromisoformat(b.wert)
    return nicht + ["SINCE" if b.art == "nach" else "BEFORE", imap_datum(stichtag)], None


def imap_gruppen(plan: Plan, heute: date) -> list[Gruppe]:
    """Die Suchen, deren Schnittmenge die Treffer sind. Mindestens eine. Mails, die der Besitzer mit einem anderen
    Programm zum Löschen markiert hat (``\\Deleted``), zeigt das Plugin nie (``UNDELETED``)."""
    einfach: list[str] = ["UNDELETED"]
    mit_literal: list[tuple[list[str], bytes]] = []
    for begriff in plan.begriffe:
        schluessel, wert = _schluessel(begriff, heute)
        if wert is None:
            einfach += schluessel
        elif _ascii(wert):
            einfach += schluessel + [zitieren(wert)]
        else:
            mit_literal.append((schluessel, wert.encode("utf-8")))
    if not mit_literal:
        return [Gruppe(tuple(einfach))]
    return [Gruppe(tuple(einfach) + tuple(schluessel), literal) for schluessel, literal in mit_literal]


def gmail_gruppe(plan: Plan) -> Gruppe:
    """Die Suche für ``X-GM-RAW``: die Abfrage unverändert, wie der Mensch (oder das Modell) sie geschrieben hat. Mit
    Zeichen außerhalb von ASCII geht sie als Literal."""
    if _ascii(plan.text):
        return Gruppe(("X-GM-RAW", zitieren(plan.text)))
    return Gruppe(("X-GM-RAW",), plan.text.encode("utf-8"))
