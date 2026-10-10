"""Die Mails der eingerichteten Konten: suchen, auflisten, lesen. Gemeinsam für den Kern und für die Werkzeuge.

``Postfach`` kennt die Konten und führt ihre Antworten zusammen; mit dem Server spricht nur ``imap.Sitzung``. Eine
Abfrage öffnet je Konto eine Sitzung (die Konten zugleich), sucht in den Ordnern, holt von den neuesten Treffern nur
Kopfzeilen, Kennzeichen, den MIME-Aufbau und den Anfang des Textes, und führt alles nach Datum zusammen. Eine einzelne
Mail liest ``lesen`` aus ihrer Kennung.

**Kennung einer Mail** (``m1.<konto>.<ordner>.<uidvalidity>.<uid>``, passt in das Muster des Kerns): zehn Hex-Zeichen
des Kontonamens, zehn des Ordnernamens, die ``UIDVALIDITY`` des Ordners und die UID. Ändert der Server die Gültigkeit
(der Ordner wurde neu aufgebaut), gehört die UID zu einer anderen Mail: dann meldet ``lesen`` „gibt es nicht mehr“, statt
eine fremde Mail zu zeigen. Eine Kennung verrät nichts über die Mail.

**Strenge.** ``abfragen`` meldet je Konto Erfolg oder Fehler; ob ein Fehler die ganze Antwort verwirft, entscheidet der
Aufrufer. Die Quelle für den Kern (Leitstand, Tagesbrief) verwirft sie: eine Liste ohne die Mails eines Kontos, das
gerade nicht antwortet, sähe aus wie ein leerer Posteingang. Die Werkzeuge ``mail.*`` liefern dagegen, was erreichbar
ist, und nennen die anderen Konten in ``konten_fehler``.

**Reihenfolge.** Neueste zuerst nach dem Eingangsdatum, das der Server vermerkt hat (``INTERNALDATE``, dieselbe Uhr wie die
Tagesgrenzen von ``newer_than``). Unter den Mails eines Ordners gilt zuerst die höchste UID: wer Mails später in den Ordner
kopiert, kann sie knapp außerhalb der neuesten ``anzahl`` sehen, die der Server nach UID aussucht.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from . import antwort, nachricht, struktur, suche, utf7
from .fehler import MailFehler, NichtEingerichtet
from .imap import ImapFehler, Sitzung
from .konten import Konto, ordner_kennung

_log = logging.getLogger("fyos_plugin_mail")

#: So viele Konten werden zugleich abgefragt
FAEDEN_MAX = 4
#: Wie viele Mails eine Abfrage höchstens liefert (so viele nimmt auch der Kern an)
ANZAHL_MAX = 100
#: So viele Zeichen Text liefert ``lesen`` höchstens (der Kern schneidet bei derselben Zahl)
TEXT_MAX = 30000
AUSSCHNITT_MAX = 200
ZEILE_MAX = 300
BETREFF_MAX = 300
LABEL_MAX = 60
LABELS_MAX = 20
ANHAENGE_MAX = 50
#: So viele Bytes eines Klartextteils werden für den Ausschnitt geholt, bei HTML mehr (der Anfang besteht oft aus Stil)
AUSSCHNITT_BYTES = 2048
AUSSCHNITT_BYTES_HTML = 12288
#: So viele Bytes eines Textteils holt ``lesen`` höchstens (genug für ``TEXT_MAX`` Zeichen auch bei Kodierungen mit Aufschlag)
LESE_BYTES_MAX = 300_000
KOPF_FELDER = "FROM TO SUBJECT DATE"
_KENNUNG = re.compile(r"m1\.([0-9a-f]{10})\.([0-9a-f]{10})\.(\d{1,10})\.(\d{1,10})")
_EPOCHE = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: Ordnerrolle → Name der Mail im Mailformat
_ROLLE_LABEL = {"inbox": "Posteingang", "sent": "Gesendet", "drafts": "Entwurf", "trash": "Papierkorb", "spam": "Spam"}
#: Gmail meldet seine Systemlabels als Kennzeichen (``\Inbox``) oder als Namen; hier klein und mit ``_`` statt Leerzeichen
_GMAIL_LABEL = {
    "inbox": "Posteingang", "sent": "Gesendet", "important": "Wichtig", "starred": "Markiert", "draft": "Entwurf",
    "drafts": "Entwurf", "trash": "Papierkorb", "spam": "Spam", "chat": "Chat", "yellow_star": "Gelber Stern",
    "category_personal": "Privat", "category_promotions": "Werbung", "category_updates": "Aktualisierungen",
    "category_social": "Soziale Netzwerke", "category_forums": "Foren",
}


@dataclass
class Liste:
    """Das Ergebnis einer Abfrage über die gewählten Konten."""

    mails: list[dict[str, Any]] = field(default_factory=list)
    ungelesen: int | None = None  # im Posteingang der gewählten Konten; ``None``, wenn ein Konto fehlt oder es nicht sagt
    fehler: dict[str, MailFehler] = field(default_factory=dict)  # Konto -> Grund
    erfolge: int = 0  # so viele Konten haben geantwortet
    ignoriert: tuple[str, ...] = ()  # Begriffe der Abfrage, die dieses Plugin nicht auswertet


@dataclass
class _Roh:
    """Eine Mail, wie der Server sie beschreibt, vor dem Mailformat."""

    konto: Konto
    ordner: str
    gueltig: int
    uid: int
    rolle: str
    flags: frozenset[str]
    labels_gmail: list[str] | None  # ``None`` bei einem Konto ohne Gmail-Erweiterung
    intern: datetime | None
    kopf: nachricht.Kopf
    wurzel: struktur.Teil | None
    ausschnitt: str = ""


# ── Kennungen ───────────────────────────────────────────────────────────────────────────────────────────
def kennung_bauen(konto: Konto, ordner: str, gueltig: int, uid: int) -> str:
    return f"m1.{konto.kennung}.{ordner_kennung(ordner)}.{int(gueltig)}.{int(uid)}"


def kennung_lesen(text: Any) -> tuple[str, str, int, int]:
    """(Kontokennung, Ordnerkennung, UIDVALIDITY, UID) einer Kennung dieses Plugins; ``ValueError`` sonst."""
    treffer = _KENNUNG.fullmatch(text) if isinstance(text, str) else None
    if not treffer or int(treffer.group(4)) < 1:
        raise ValueError("id ist keine Mail-Kennung dieses Plugins (sie stammt aus mail.eingang oder mail.suchen, unverändert übernehmen)")
    return treffer.group(1), treffer.group(2), int(treffer.group(3)), int(treffer.group(4))


# ── Mailformat ──────────────────────────────────────────────────────────────────────────────────────────
def _iso(zeitpunkt: datetime | None) -> str:
    if zeitpunkt is None:
        return ""
    try:
        return zeitpunkt.isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return ""


def _zeitpunkt(roh: _Roh) -> datetime:
    return roh.intern or roh.kopf.datum or _EPOCHE


def _reihenfolge(roh: _Roh) -> tuple[datetime, int]:
    """Neueste zuerst; bei gleichem Zeitpunkt (Mails derselben Sekunde) die mit der höheren UID."""
    return _zeitpunkt(roh), roh.uid


def _norm(label: str) -> str:
    return label.strip().lstrip("\\").casefold().replace(" ", "_")


def _labels(roh: _Roh) -> list[str]:
    namen: list[str] = []

    def dazu(name: str) -> None:
        name = nachricht.zeile(name, LABEL_MAX)
        if name and name not in namen and len(namen) < LABELS_MAX:
            namen.append(name)

    if roh.labels_gmail is not None:
        for label in roh.labels_gmail:
            dazu(_GMAIL_LABEL.get(_norm(label)) or utf7.dekodieren(label.lstrip("\\")))
    else:
        dazu(_ROLLE_LABEL.get(roh.rolle) or roh.ordner)
        if "\\flagged" in roh.flags:
            dazu("Markiert")
        if "\\draft" in roh.flags:
            dazu("Entwurf")
    return namen


def _mail(roh: _Roh) -> dict[str, Any]:
    return {
        "id": kennung_bauen(roh.konto, roh.ordner, roh.gueltig, roh.uid),
        "konto": roh.konto.name,
        "von": nachricht.zeile(roh.kopf.von, ZEILE_MAX),
        "an": nachricht.zeile(roh.kopf.an, ZEILE_MAX),
        "betreff": nachricht.zeile(roh.kopf.betreff, BETREFF_MAX) or nachricht.OHNE_BETREFF,
        "datum": _iso(roh.kopf.datum or roh.intern),
        "gelesen": "\\seen" in roh.flags,
        "labels": _labels(roh),
        "ausschnitt": roh.ausschnitt,
    }


# ── Text ────────────────────────────────────────────────────────────────────────────────────────────────
def _teil_text(teil: struktur.Teil, roh: bytes, abgeschnitten: bool) -> str:
    """Der Text eines geholten Teils: Übertragungskodierung aufgehoben, Zeichensatz gelesen, HTML zu Text."""
    daten = nachricht.entkodieren(roh, teil.kodierung, abgeschnitten)
    text = nachricht.dekodieren(daten, teil.parameter.get("charset", ""), abgeschnitten)
    if teil.typ == "text/html":
        if abgeschnitten:
            text = re.sub(r"<[^>]*$", "", text)  # ein angeschnittenes Tag am Ende ist kein Text
        return nachricht.html_zu_text(text)
    return nachricht.text_bereinigen(text)


def _abgeschnitten(teil: struktur.Teil, geholt: int, maximal: int) -> bool:
    return geholt >= maximal and (teil.groesse == 0 or teil.groesse > maximal)


def _ausschnitte(s: Sitzung, liste: list[_Roh]) -> None:
    """Holt für die Mails den Anfang ihres Textes: aus dem Klartext, und nur wenn der leer ist oder fehlt, aus dem HTML.
    Ein Befehl je Abschnittsnummer, nie mehr als nötig."""
    kandidaten: list[tuple[_Roh, list[struktur.Teil]]] = []
    for roh in liste:
        if roh.wurzel is not None:
            a = struktur.auswerten(roh.wurzel)
            teile = a.text[:1] + a.html[:1]
            if teile:
                kandidaten.append((roh, teile))
    for stufe in (0, 1):
        gruppen: dict[tuple[str, bool], list[tuple[_Roh, struktur.Teil]]] = {}
        for roh, teile in kandidaten:
            if not roh.ausschnitt and stufe < len(teile):
                gruppen.setdefault((teile[stufe].nummer, teile[stufe].typ == "text/html"), []).append((roh, teile[stufe]))
        for (nummer, html), paare in gruppen.items():
            maximal = AUSSCHNITT_BYTES_HTML if html else AUSSCHNITT_BYTES
            try:
                daten = s.teile([roh.uid for roh, _ in paare], nummer, maximal)
            except ImapFehler as fehler:
                if fehler.art != "server" or fehler.status == 413:
                    raise
                _log.info("mail: %s: Ausschnitte nicht geholt (%s)", s.konto.name, fehler)  # die Liste gilt auch ohne sie
                continue
            for roh, teil in paare:
                stueck = daten.get(roh.uid)
                if stueck:
                    text = _teil_text(teil, stueck, _abgeschnitten(teil, len(stueck), maximal))
                    roh.ausschnitt = nachricht.ausschnitt_aus(text, AUSSCHNITT_MAX)


def _volltext(s: Sitzung, uid: int, a: struktur.Auswertung, grenze: int) -> tuple[str, bool]:
    """Der Text einer Mail (Klartext, sonst HTML als Text), höchstens ``grenze`` Zeichen, und ob er gekürzt ist."""
    for gruppe in (a.text, a.html):
        stuecke: list[str] = []
        zeichen = 0
        gekuerzt = False
        for teil in gruppe:
            if zeichen > grenze:
                gekuerzt = True
                break
            maximal = min(teil.groesse, LESE_BYTES_MAX) if teil.groesse > 0 else LESE_BYTES_MAX
            geholt = s.teile([uid], teil.nummer, maximal).get(uid, b"")
            abgeschnitten = _abgeschnitten(teil, len(geholt), maximal)
            text = _teil_text(teil, geholt, abgeschnitten)
            gekuerzt = gekuerzt or abgeschnitten
            if text:
                stuecke.append(text)
                zeichen += len(text) + 2
        text = "\n\n".join(stuecke)
        if text.strip():
            return text[:grenze], gekuerzt or len(text) > grenze
    return "", False


# ── Zusammenführen ──────────────────────────────────────────────────────────────────────────────────────
def _parallel(aufgaben: list[tuple[Konto, Callable[[], Any]]]) -> list[tuple[Konto, Any, MailFehler | None]]:
    """Führt die Aufgaben zugleich aus; je Aufgabe (Konto, Ergebnis, Fehler) in der Reihenfolge der Eingabe."""

    def lauf(f: Callable[[], Any]) -> tuple[Any, MailFehler | None]:
        try:
            return f(), None
        except MailFehler as fehler:
            return None, fehler
        except Exception as fehler:  # noqa: BLE001 — jedes Konto meldet seinen Fehler einzeln
            _log.warning("mail: unerwarteter Fehler (%s)", type(fehler).__name__, exc_info=_log.isEnabledFor(logging.DEBUG))
            return None, MailFehler(f"Unerwarteter Fehler ({type(fehler).__name__})", 502)

    if len(aufgaben) <= 1:
        return [(k, *lauf(f)) for k, f in aufgaben]
    with ThreadPoolExecutor(max_workers=min(FAEDEN_MAX, len(aufgaben)), thread_name_prefix="mail") as pool:
        laeufe = [(k, pool.submit(lauf, f)) for k, f in aufgaben]
        return [(k, *lauf_.result()) for k, lauf_ in laeufe]


class Postfach:
    """Die Konten und was man mit ihnen liest. ``sitzung`` macht aus einem Konto eine Sitzung (Tests setzen eine eigene)."""

    def __init__(
        self,
        konten: list[Konto],
        sitzung: Callable[[Konto], Sitzung] | None = None,
        heute: Callable[[], date] | None = None,
    ) -> None:
        self.konten = list(konten)
        self._sitzung: Callable[[Konto], Sitzung] = sitzung or Sitzung
        self._heute: Callable[[], date] = heute or date.today

    @property
    def eingerichtet(self) -> bool:
        """Ist mindestens ein Konto eingetragen (auch ein unbrauchbares: dann zeigen die Leser den Fehler)?"""
        return bool(self.konten)

    def konten_liste(self) -> list[dict[str, Any]]:
        return [k.als_dict() for k in self.konten]

    def _waehlen(self, konto: str | None) -> list[Konto]:
        if konto is None:
            return list(self.konten)
        for k in self.konten:
            if k.name.casefold() == konto.casefold():
                return [k]
        vorhanden = ", ".join(repr(k.name) for k in self.konten[:20]) or "keine"
        raise ValueError(f"konto {konto!r} gibt es nicht; vorhanden: {vorhanden}")

    # ── Abfragen ──────────────────────────────────────────────────────────────────────────────────────
    def abfragen(self, abfrage: str, anzahl: int, konto: str | None = None) -> Liste:
        """Die neuesten ``anzahl`` Mails zur Abfrage. ``ValueError`` bei einer unbrauchbaren Abfrage oder einem
        unbekannten Konto, ``NichtEingerichtet`` ohne Konten; Fehler einzelner Konten stehen in ``Liste.fehler``."""
        if not self.konten:
            raise NichtEingerichtet()
        plan = suche.planen(abfrage)
        n = max(1, min(int(anzahl), ANZAHL_MAX))
        gewaehlt = self._waehlen(konto)
        liste = Liste(ignoriert=plan.ignoriert)
        heute = self._heute()
        aufgaben: list[tuple[Konto, Callable[[], Any]]] = []
        for k in gewaehlt:
            if k.fehler:
                liste.fehler[k.name] = MailFehler(f"Konfiguration fehlerhaft: {k.fehler}", 503)
            else:
                aufgaben.append((k, lambda k=k: self._konto_abfrage(k, plan, n, heute)))
        alle: list[_Roh] = []
        zaehler: list[int | None] = []
        nur_gmail = True
        for k, ergebnis, fehler in _parallel(aufgaben):
            if fehler is not None:
                liste.fehler[k.name] = fehler
                continue
            roh, ungelesen, gmail = ergebnis
            liste.erfolge += 1
            alle += roh
            zaehler.append(ungelesen)
            nur_gmail = nur_gmail and gmail
        alle.sort(key=_reihenfolge, reverse=True)
        liste.mails = [_mail(r) for r in alle[:n]]
        if not liste.fehler and zaehler and None not in zaehler:
            liste.ungelesen = sum(z for z in zaehler if z is not None)
        if liste.erfolge and nur_gmail:
            liste.ignoriert = ()  # Gmail wertet die ganze Abfrage selbst aus
        return liste

    def _ordner_fuer(self, s: Sitzung, konto: Konto, plan: suche.Plan) -> list[str]:
        """Die Ordner, die diese Abfrage durchsucht: die aus ``in:``, sonst die des Kontos."""
        if not plan.rollen:
            return list(konto.ordner)
        namen: list[str] = []
        for rolle in plan.rollen:
            name = s.ordner_fuer(rolle)
            if name is None:
                raise ImapFehler(
                    f"Im Konto {konto.name!r} gibt es keinen Ordner für in:{rolle} (vorhanden: {s.ordner_namen() or 'keine'}).",
                    "ordner", 404)
            if name not in namen:
                namen.append(name)
        return namen

    def _konto_abfrage(
        self, konto: Konto, plan: suche.Plan, anzahl: int, heute: date,
    ) -> tuple[list[_Roh], int | None, bool]:
        """(Mails, ungelesene im Posteingang, ob das Konto Gmail ist)."""
        with self._sitzung(konto) as s:
            ungelesen = s.ungelesen()  # vor dem Öffnen eines Ordners: STATUS gehört nicht an den gewählten Ordner
            gesammelt: list[_Roh] = []
            for name in self._ordner_fuer(s, konto, plan):
                gueltig = s.waehle(name)
                gruppen = [suche.gmail_gruppe(plan)] if s.gmail else suche.imap_gruppen(plan, heute)
                uids = s.neueste(gruppen, anzahl)
                if uids:
                    gesammelt += self._koepfe(s, konto, name, gueltig, uids)
            gmail = s.gmail
        gesammelt.sort(key=_reihenfolge, reverse=True)
        return gesammelt[:anzahl], ungelesen, gmail

    @staticmethod
    def _kopf_items(gmail: bool) -> str:
        return (f"UID FLAGS INTERNALDATE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS ({KOPF_FELDER})]"
                + (" X-GM-LABELS" if gmail else ""))

    @staticmethod
    def _roh_aus(konto: Konto, ordner: str, gueltig: int, rolle: str, a: dict[str, Any], gmail: bool) -> _Roh:
        try:
            wurzel: struktur.Teil | None = struktur.baum(a.get("BODYSTRUCTURE"))
        except (ValueError, TypeError, IndexError):  # AntwortFehler ist ein ValueError
            wurzel = None  # die Mail bleibt lesbar im Kopf, ohne Ausschnitt und Anhänge
        return _Roh(
            konto=konto, ordner=ordner, gueltig=gueltig, uid=a["UID"], rolle=rolle,
            flags=frozenset(str(f).lower() for f in a.get("FLAGS", [])),
            labels_gmail=list(a.get("X-GM-LABELS", [])) if gmail else None,
            intern=nachricht.internaldatum(a.get("INTERNALDATE", "")),
            kopf=nachricht.kopf_lesen(antwort.abschnitt(a, f"HEADER.FIELDS ({KOPF_FELDER})")),
            wurzel=wurzel,
        )

    def _koepfe(self, s: Sitzung, konto: Konto, ordner: str, gueltig: int, uids: list[int]) -> list[_Roh]:
        rolle = s.rolle_von(ordner)
        gefragt = set(uids)
        roh = [
            self._roh_aus(konto, ordner, gueltig, rolle, a, s.gmail)
            for a in s.holen(uids, self._kopf_items(s.gmail)) if a["UID"] in gefragt
        ]
        roh.sort(key=_reihenfolge, reverse=True)
        _ausschnitte(s, roh)
        return roh

    # ── Lesen ─────────────────────────────────────────────────────────────────────────────────────────
    def lesen(self, kennung: str, text_max: int = TEXT_MAX) -> dict[str, Any]:
        """Eine Mail im Mailformat samt ``text``, ``gekuerzt`` und ``anhaenge``. ``ValueError`` bei einer fremden
        Kennung, ``MailFehler`` (Status 404), wenn es die Mail nicht mehr gibt."""
        if not self.konten:
            raise NichtEingerichtet()
        konto_k, ordner_k, gueltig, uid = kennung_lesen(kennung)
        konto = next((k for k in self.konten if k.kennung == konto_k), None)
        if konto is None:
            raise MailFehler("Das Konto dieser Mail ist nicht mehr eingerichtet.", 404)
        if konto.fehler:
            raise MailFehler(f"Konfiguration des Kontos {konto.name!r} fehlerhaft: {konto.fehler}", 503)
        try:
            return self._lesen(konto, ordner_k, gueltig, uid, max(1, min(int(text_max), TEXT_MAX)))
        except MailFehler:
            raise
        except Exception as fehler:  # noqa: BLE001
            _log.warning("mail: unerwarteter Fehler beim Lesen (%s)", type(fehler).__name__,
                         exc_info=_log.isEnabledFor(logging.DEBUG))
            raise MailFehler(f"Unerwarteter Fehler ({type(fehler).__name__})", 502) from None

    def _lesen(self, konto: Konto, ordner_k: str, gueltig: int, uid: int, text_max: int) -> dict[str, Any]:
        weg = MailFehler("Die Mail gibt es nicht mehr (verschoben, gelöscht oder der Ordner wurde neu aufgebaut).", 404)
        with self._sitzung(konto) as s:
            name = "INBOX" if ordner_kennung("INBOX") == ordner_k else next(
                (o.name for o in s.ordner() if ordner_kennung(o.name) == ordner_k), None)
            if name is None:
                raise weg
            if s.waehle(name) != gueltig:
                raise weg
            antworten = [a for a in s.holen([uid], self._kopf_items(s.gmail)) if a["UID"] == uid]
            if not antworten:
                raise weg
            roh = self._roh_aus(konto, name, gueltig, s.rolle_von(name), antworten[0], s.gmail)
            if roh.wurzel is None:
                raise MailFehler(f"{konto.host} hat den Aufbau der Mail nicht lesbar beschrieben.", 502)
            auswertung = struktur.auswerten(roh.wurzel)
            text, gekuerzt = _volltext(s, uid, auswertung, text_max)
        roh.ausschnitt = nachricht.ausschnitt_aus(text, AUSSCHNITT_MAX)
        anhaenge = [struktur.anhang_beschreibung(t) for t in auswertung.anhaenge]
        return {**_mail(roh), "text": text, "gekuerzt": gekuerzt, "anhaenge": anhaenge[:ANHAENGE_MAX]}

    # ── Prüfen ────────────────────────────────────────────────────────────────────────────────────────
    def pruefen(self, konto: str | None = None) -> list[dict[str, Any]]:
        """Meldet sich bei den Konten an und nennt ihre Ordner und die Zahl der ungelesenen Mails im Posteingang. Liest
        keine Mail. Je Konto ``{name, status, ordner_vorhanden?, ungelesen?, gmail?, fehler?}``."""
        gewaehlt = self._waehlen(konto)
        zeilen: dict[str, dict[str, Any]] = {}
        aufgaben: list[tuple[Konto, Callable[[], Any]]] = []
        for k in gewaehlt:
            if k.fehler:
                zeilen[k.name] = {"name": k.name, "status": "fehler", "fehler": f"Konfiguration fehlerhaft: {k.fehler}"}
            else:
                aufgaben.append((k, lambda k=k: self._konto_pruefen(k)))
        for k, ergebnis, fehler in _parallel(aufgaben):
            zeilen[k.name] = (
                {"name": k.name, "status": "fehler", "fehler": str(fehler)} if fehler is not None else ergebnis)
        return [zeilen[k.name] for k in gewaehlt]

    def _konto_pruefen(self, konto: Konto) -> dict[str, Any]:
        with self._sitzung(konto) as s:
            ordner = [o.name for o in s.ordner()]
            ungelesen = s.ungelesen()
            gmail = s.gmail
        zeile: dict[str, Any] = {"name": konto.name, "status": "ok", "ordner_vorhanden": ordner[:50], "gmail": gmail}
        if ungelesen is not None:
            zeile["ungelesen"] = ungelesen
        return zeile
