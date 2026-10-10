"""Die einzige Stelle, die IMAP spricht. **Nur lesen, nur verschlüsselt, nichts hinterlassen.**

Was dieses Modul an einen Server schickt, ist abschließend:

=======================  ===========================================================================================
``CAPABILITY``           welche Erweiterungen der Server kann (von ``imaplib`` beim Verbinden selbst gestellt)
``STARTTLS``             nur bei ``sicherheit: starttls``, vor jeder Anmeldung
``LOGIN``/``AUTHENTICATE``  Anmeldung, erst nach dem Aufbau der Verschlüsselung
``LIST``                 Ordner und ihre Rollen (gesendet, Papierkorb …)
``STATUS INBOX (UNSEEN)``  Zahl der ungelesenen Mails
``EXAMINE``              Ordner **schreibgeschützt** öffnen (nie ``SELECT``)
``UID SEARCH``           suchen
``UID FETCH``            nur ``FLAGS``, ``INTERNALDATE``, ``RFC822.SIZE``, ``BODYSTRUCTURE``, ``X-GM-LABELS`` und
                         ``BODY.PEEK[…]``; ``PEEK`` lässt den Gelesen-Status, wie er ist
``LOGOUT``               Sitzung beenden
=======================  ===========================================================================================

``STORE``, ``COPY``, ``MOVE``, ``EXPUNGE``, ``APPEND``, ``DELETE``, ``CREATE``, ``RENAME`` und ``SUBSCRIBE`` kommen in
diesem Paket nicht vor, und die Prüfungen in ``_uid`` und ``_items_pruefen`` würden sie auch abweisen. Dazu ist der Ordner
schon durch ``EXAMINE`` schreibgeschützt: selbst ein Fehler hier könnte nichts am Postfach ändern. Spuren bleiben nur beim
Anbieter selbst (er sieht den Zugriff in seinem Anmeldeprotokoll).

**Verschlüsselung.** Es gibt nur ``imaps`` (Verbindung von Anfang an verschlüsselt) und ``starttls``. Das Zertifikat und der
Servername werden geprüft (``ssl.create_default_context()``, mindestens TLS 1.2); einen Schalter, die Prüfung abzustellen, gibt
es nicht. Eine eigene Zertifizierungsstelle trägt man über die Umgebung ein (``SSL_CERT_FILE``), nicht hier.

**Meldungen.** Jeder Fehler wird ein ``ImapFehler`` mit einem verständlichen deutschen Satz. Das Passwort steht in keiner
Meldung, auch nicht, wenn der Server es zurückspiegelt.
"""

from __future__ import annotations

import imaplib
import logging
import re
import socket
import ssl
import time
from collections.abc import Sequence
from typing import Any

from . import antwort, utf7
from .fehler import MailFehler
from .konten import Konto
from .suche import Gruppe, zitieren

_log = logging.getLogger("fyos_plugin_mail")

#: So lange wartet ein einzelner Schritt (Verbinden, Lesen einer Antwort) auf den Server
FRIST_S = 20.0
#: So lange darf eine ganze Sitzung dauern; danach bricht der nächste Schritt mit einer Zeitüberschreitung ab
GESAMT_FRIST_S = 60.0
#: Suchen beginnen bei den letzten so vielen UIDs und weiten sich aus, wenn das nicht reicht (große Postfächer)
FENSTER_UIDS = 3000
SERVERTEXT_MAX = 160
#: Rolle → Attribut aus RFC 6154 (``LIST`` meldet es hinter dem Ordner)
_ROLLEN_ATTRIBUT = {
    "sent": "\\sent", "drafts": "\\drafts", "trash": "\\trash", "spam": "\\junk", "alle": "\\all", "archiv": "\\archive",
}
#: Ohne Attribut: gängige Namen (klein), verglichen mit dem letzten Teil des Ordnerpfads
_ROLLEN_NAMEN = {
    "sent": ("sent", "sent items", "sent messages", "sent mail", "gesendet", "gesendete elemente", "gesendete objekte",
             "gesendete nachrichten", "gesendete mails"),
    "drafts": ("drafts", "draft", "entwürfe", "entwurf"),
    "trash": ("trash", "deleted items", "deleted messages", "papierkorb", "gelöschte elemente", "gelöschte objekte"),
    "spam": ("junk", "spam", "junk e-mail", "junk-e-mail", "junk email"),
}
_ITEM_ERLAUBT = re.compile(
    r"UID|FLAGS|INTERNALDATE|RFC822\.SIZE|BODYSTRUCTURE|X-GM-LABELS"
    r"|BODY\.PEEK\[(?:\d+(?:\.\d+)*|HEADER\.FIELDS \([A-Z-]+(?: [A-Z-]+)*\))\](?:<\d+\.\d+>)?"
)
_TLS_HINWEIS_URSACHEN = ("WRONG_VERSION_NUMBER", "UNKNOWN_PROTOCOL", "RECORD_LAYER_FAILURE", "PACKET_LENGTH_TOO_LONG")


class ImapFehler(MailFehler):
    """Ein Fehler im Umgang mit dem Server, mit einem Satz, den man dem Besitzer zeigen darf (nie mit Passwort)."""

    def __init__(self, text: str, art: str = "server", status: int = 502) -> None:
        super().__init__(text, status)
        self.art = art  # verbindung | zeit | tls | anmeldung | ordner | server


def _kontext() -> ssl.SSLContext:
    kontext = ssl.create_default_context()  # prüft Zertifikat und Servername, nimmt SSL_CERT_FILE auf
    kontext.minimum_version = ssl.TLSVersion.TLSv1_2
    return kontext


def _oeffnen(konto: Konto, frist: float) -> imaplib.IMAP4:
    """Verbindet verschlüsselt. Es gibt keinen Weg ohne Verschlüsselung."""
    kontext = _kontext()
    if konto.sicherheit == "imaps":
        return imaplib.IMAP4_SSL(konto.host, konto.port, ssl_context=kontext, timeout=frist)
    verbindung = imaplib.IMAP4(konto.host, konto.port, timeout=frist)
    try:
        verbindung.starttls(ssl_context=kontext)  # wirft, wenn der Server STARTTLS nicht anbietet oder ablehnt
    except BaseException as fehler:
        try:
            verbindung.shutdown()
        except Exception:  # noqa: BLE001
            pass
        if isinstance(fehler, imaplib.IMAP4.error):  # in dieser Phase heißt jeder Fehler des Protokolls: kein STARTTLS
            raise ImapFehler(
                f"{konto.host} bietet kein STARTTLS an oder lehnt es ab; unverschlüsselt verbindet das Plugin nie.",
                "tls", 503) from None
        raise
    return verbindung


def _items_pruefen(items: str) -> None:
    """Nur lesende ``FETCH``-Angaben: alles, was den Gelesen-Status ändern könnte (``BODY[…]`` ohne ``PEEK``,
    ``RFC822``, ``RFC822.TEXT``), ist hier ein Programmierfehler und wird nie gesendet."""
    rest = _ITEM_ERLAUBT.sub("", items)
    if rest.strip():
        raise AssertionError(f"FETCH-Angabe nicht erlaubt: {rest.strip()[:60]!r}")


def _ordnerarg(name: str) -> str:
    return zitieren(utf7.kodieren(name))


class Sitzung:
    """Eine Verbindung zu einem Konto. Als Kontextmanager benutzen: sie meldet sich an und am Ende wieder ab.

    ``imaplib`` ist nicht thread-sicher; jede Sitzung gehört einem Thread."""

    def __init__(self, konto: Konto, frist: float | None = None, gesamt: float | None = None) -> None:
        self.konto = konto
        self.frist = FRIST_S if frist is None else frist
        self.gesamt = GESAMT_FRIST_S if gesamt is None else gesamt
        self._ende = time.monotonic() + self.gesamt
        self._c: imaplib.IMAP4 | None = None
        self._ordner: list[antwort.Ordner] | None = None
        self.gmail = False
        self.ordner_offen = ""
        self.uidnext: int | None = None

    # ── Lebenslauf ────────────────────────────────────────────────────────────────────────────────────
    def __enter__(self) -> Sitzung:
        self.oeffnen()
        return self

    def __exit__(self, *_: object) -> None:
        self.schliessen()

    def oeffnen(self) -> None:
        self._ende = time.monotonic() + self.gesamt
        try:
            self._c = _oeffnen(self.konto, self.frist)
            self._anmelden()
            self.gmail = "X-GM-EXT-1" in self._c.capabilities
        except ImapFehler:
            self.schliessen()
            raise
        except BaseException as fehler:
            self.schliessen()
            if not isinstance(fehler, Exception):
                raise
            raise self._uebersetzen(fehler, anmeldung=True) from None

    def schliessen(self) -> None:
        verbindung, self._c = self._c, None
        if verbindung is None:
            return
        try:
            verbindung.logout()
        except Exception:  # noqa: BLE001 — die Verbindung ist vielleicht schon weg; es bleibt nichts zu tun
            pass
        try:
            verbindung.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def _anmelden(self) -> None:
        c = self._conn()
        if "LOGINDISABLED" in c.capabilities:
            raise ImapFehler(
                f"Der Server {self.konto.host} erlaubt keine Anmeldung mit Passwort (LOGINDISABLED).", "anmeldung", 401)
        nutzer, passwort = self.konto.nutzer, self.konto.passwort
        if nutzer.isascii() and passwort.isascii():
            c.login(zitieren(nutzer), passwort)  # imaplib setzt das Passwort selbst in Anführungszeichen
        elif "AUTH=PLAIN" in c.capabilities:
            c.authenticate("PLAIN", lambda _herausforderung: ("\0" + nutzer + "\0" + passwort).encode("utf-8"))
        else:
            raise ImapFehler(
                f"Nutzer oder Passwort enthalten Zeichen außerhalb von ASCII, und {self.konto.host} bietet dafür "
                "keine Anmeldung an (AUTH=PLAIN).", "anmeldung", 401)

    def _conn(self) -> imaplib.IMAP4:
        if self._c is None:
            raise ImapFehler("Die Verbindung ist nicht offen.", "verbindung", 503)
        if time.monotonic() > self._ende:
            raise ImapFehler(
                f"Zeitüberschreitung: {self.konto.adresse} braucht zu lange (mehr als {self.gesamt:g} Sekunden).",
                "zeit", 503)
        return self._c

    # ── Fehler ────────────────────────────────────────────────────────────────────────────────────────
    def _bereinigt(self, text: Any) -> str:
        """Ein Text des Servers, kurz, einzeilig, ohne das Passwort."""
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8", errors="replace")
        text = " ".join(str(text).split())
        if self.konto.passwort:
            text = text.replace(self.konto.passwort, "***")
        text = re.sub(r"[\x00-\x1f\x7f]", "", text)
        return text if len(text) <= SERVERTEXT_MAX else text[: SERVERTEXT_MAX - 1] + "…"

    def _servertext(self, fehler: BaseException) -> str:
        return self._bereinigt(fehler.args[0]) if fehler.args else ""

    def _grund(self, daten: Any) -> str:
        """Der Text hinter ``NO`` aus den Daten einer abgelehnten Antwort, als Anhang für einen Satz (``: Text``) oder leer."""
        letzter = daten[-1] if isinstance(daten, list) and daten else None
        text = self._bereinigt(letzter) if isinstance(letzter, (bytes, bytearray, str)) else ""
        return f": {text}" if text else ""

    def _uebersetzen(self, fehler: Exception, anmeldung: bool = False) -> ImapFehler:
        """Macht aus einer Ausnahme der Bibliotheken einen verständlichen Satz."""
        k = self.konto
        if isinstance(fehler, ssl.SSLCertVerificationError):
            grund = self._bereinigt(getattr(fehler, "verify_message", "") or "Prüfung fehlgeschlagen")
            return ImapFehler(
                f"Das Zertifikat von {k.host} ist nicht vertrauenswürdig ({grund}). Das Plugin verbindet nur mit einem "
                "geprüften Zertifikat; bei einer eigenen Zertifizierungsstelle gehört sie in SSL_CERT_FILE.", "tls", 503)
        if isinstance(fehler, ssl.SSLError):
            ursache = str(getattr(fehler, "reason", "") or "")
            hinweis = (" Port und Sicherheit prüfen: 993 gehört zu imaps, 143 zu starttls."
                       if ursache in _TLS_HINWEIS_URSACHEN else "")
            return ImapFehler(f"Die verschlüsselte Verbindung zu {k.adresse} kam nicht zustande ({ursache or 'TLS-Fehler'}).{hinweis}",
                              "tls", 503)
        if isinstance(fehler, socket.gaierror):
            return ImapFehler(f"Der Server {k.host} wurde nicht gefunden (Name nicht aufgelöst).", "verbindung", 503)
        if isinstance(fehler, (TimeoutError, socket.timeout)):
            return ImapFehler(f"Zeitüberschreitung: {k.adresse} antwortet nicht (nach {self.frist:g} Sekunden).", "zeit", 503)
        if isinstance(fehler, ConnectionRefusedError):
            return ImapFehler(f"Die Verbindung zu {k.adresse} wurde abgelehnt (Server, Port und Sicherheit prüfen).",
                              "verbindung", 503)
        if isinstance(fehler, imaplib.IMAP4.abort):
            return ImapFehler(f"Die Verbindung zu {k.adresse} wurde vom Server beendet.", "verbindung", 503)
        if isinstance(fehler, imaplib.IMAP4.error):
            text = self._servertext(fehler)
            if anmeldung:
                return ImapFehler(
                    f"Die Anmeldung bei {k.host} wurde abgelehnt. Nutzer und App-Passwort prüfen (viele Anbieter "
                    f"verlangen ein eigenes App-Passwort, nicht das Haupt-Passwort).{(' Antwort des Servers: ' + text) if text else ''}",
                    "anmeldung", 401)
            if "got more than" in text:
                return ImapFehler(
                    "Die Suche trifft zu viele Mails für eine Antwort; bitte eingrenzen (zum Beispiel newer_than:30d).",
                    "server", 413)
            return ImapFehler(f"Der Server {k.host} hat den Befehl abgelehnt.{(' Antwort: ' + text) if text else ''}",
                              "server", 502)
        if isinstance(fehler, OSError):
            return ImapFehler(f"Netzwerkfehler bei {k.adresse} ({type(fehler).__name__}).", "verbindung", 503)
        _log.warning("mail: unerwarteter Fehler (%s)", type(fehler).__name__, exc_info=_log.isEnabledFor(logging.DEBUG))
        return ImapFehler(f"Unerwarteter Fehler bei {k.adresse} ({type(fehler).__name__}).", "server", 502)

    def _ausfuehren(self, aufruf: Any) -> Any:
        try:
            return aufruf()
        except ImapFehler:
            raise
        except Exception as fehler:  # noqa: BLE001
            raise self._uebersetzen(fehler) from None

    # ── Ordner ────────────────────────────────────────────────────────────────────────────────────────
    def ordner(self) -> list[antwort.Ordner]:
        """Alle wählbaren Ordner (einmal je Sitzung gefragt)."""
        if self._ordner is None:
            typ, daten = self._ausfuehren(lambda: self._conn().list())
            if typ != "OK":
                raise ImapFehler(f"{self.konto.host} liefert die Ordnerliste nicht{self._grund(daten)}.", "server", 502)
            try:
                self._ordner = [o for o in antwort.ordner_liste(daten) if o.waehlbar]
            except antwort.AntwortFehler:
                raise ImapFehler(f"{self.konto.host} liefert eine unlesbare Ordnerliste.", "server", 502) from None
        return self._ordner

    def ordner_fuer(self, rolle: str) -> str | None:
        """Der Name des Ordners mit dieser Rolle (``inbox``, ``sent``, ``drafts``, ``trash``, ``spam``, ``alle``,
        ``archiv``) oder ``None``. Erst das Attribut des Servers (RFC 6154), dann gängige Namen."""
        if rolle == "inbox":
            return "INBOX"
        attribut = _ROLLEN_ATTRIBUT.get(rolle)
        for o in self.ordner():
            if attribut and attribut in o.attribute:
                return o.name
        for o in self.ordner():
            letzter = o.name.rsplit(o.trenner, 1)[-1] if o.trenner else o.name
            if letzter.casefold() in _ROLLEN_NAMEN.get(rolle, ()):
                return o.name
        return None

    def rolle_von(self, name: str) -> str:
        """Die Rolle eines Ordners (``inbox``, ``sent`` …) oder ``""``."""
        if name.upper() == "INBOX":
            return "inbox"
        for o in self.ordner():
            if o.name == name:
                for rolle, attribut in _ROLLEN_ATTRIBUT.items():
                    if attribut in o.attribute:
                        return rolle
        for rolle in ("sent", "drafts", "trash", "spam"):
            if self.ordner_fuer(rolle) == name:
                return rolle
        return ""

    def ordner_namen(self, grenze: int = 20) -> str:
        try:
            namen = [o.name for o in self.ordner()]
        except ImapFehler:
            return ""
        return ", ".join(namen[:grenze]) + (" …" if len(namen) > grenze else "")

    def waehle(self, name: str) -> int:
        """Öffnet einen Ordner **schreibgeschützt** (``EXAMINE``). Liefert dessen ``UIDVALIDITY``."""
        c = self._conn()
        try:
            typ, _daten = c.select(_ordnerarg(name), readonly=True)
        except imaplib.IMAP4.error:
            typ = "NO"
        except Exception as fehler:  # noqa: BLE001
            raise self._uebersetzen(fehler) from None
        if typ != "OK":
            vorhanden = self.ordner_namen()
            raise ImapFehler(
                f"Den Ordner {name!r} gibt es im Konto {self.konto.name!r} nicht oder er lässt sich nicht öffnen"
                + (f" (vorhanden: {vorhanden})." if vorhanden else "."), "ordner", 404)
        _code, werte = c.response("UIDVALIDITY")
        gueltig = werte[-1] if werte else None
        if not gueltig or not gueltig.isdigit():
            raise ImapFehler(f"{self.konto.host} meldet für {name!r} keine UIDVALIDITY.", "server", 502)
        _code, naechste = c.response("UIDNEXT")
        letzte = naechste[-1] if naechste else None
        self.uidnext = int(letzte) if letzte and letzte.isdigit() else None
        self.ordner_offen = name
        return int(gueltig)

    def ungelesen(self) -> int | None:
        """Ungelesene Mails im Posteingang (``STATUS INBOX (UNSEEN)``); ``None``, wenn der Server sie nicht nennt."""
        c = self._conn()
        try:
            typ, daten = c.status(_ordnerarg("INBOX"), "(UNSEEN)")
        except imaplib.IMAP4.error:
            return None
        except Exception as fehler:  # noqa: BLE001
            raise self._uebersetzen(fehler) from None
        if typ != "OK":
            return None
        try:
            return antwort.status_zahlen(daten).get("UNSEEN")
        except antwort.AntwortFehler:
            return None

    # ── Suchen und Holen ──────────────────────────────────────────────────────────────────────────────
    def _uid(self, befehl: str, *args: str) -> tuple[str, list[Any]]:
        if befehl not in ("SEARCH", "FETCH"):
            raise AssertionError(f"UID {befehl} ist nicht erlaubt")  # nur lesen
        c = self._conn()
        return self._ausfuehren(lambda: c.uid(befehl, *args))

    def _suche_eine(self, gruppe: Gruppe, ab: int | None = None) -> set[int]:
        c = self._conn()
        argumente = (["UID", f"{int(ab)}:*"] if ab else []) + list(gruppe.schluessel)
        if gruppe.literal is not None:
            argumente = ["CHARSET", "UTF-8", *argumente]
            c.literal = gruppe.literal  # imaplib hängt es als Literal hinter das letzte Argument
        try:
            typ, daten = self._uid("SEARCH", *argumente)
        finally:
            c.literal = None
        if typ != "OK":
            raise ImapFehler(f"{self.konto.host} hat die Suche abgelehnt{self._grund(daten)}.", "server", 502)
        uids: set[int] = set()
        for zeile in daten:
            if isinstance(zeile, (bytes, bytearray)):
                uids.update(int(x) for x in zeile.split() if x.isdigit())
        return uids

    def suchen(self, gruppen: Sequence[Gruppe], ab: int | None = None) -> list[int]:
        """Die UIDs, die alle Gruppen finden, neueste (höchste UID) zuerst; mit ``ab`` nur UIDs von da an."""
        treffer: set[int] | None = None
        for gruppe in gruppen:
            gefunden = self._suche_eine(gruppe, ab)
            treffer = gefunden if treffer is None else treffer & gefunden
            if not treffer:
                break
        return sorted(treffer or (), reverse=True)

    def neueste(self, gruppen: Sequence[Gruppe], anzahl: int) -> list[int]:
        """Die UIDs der höchstens ``anzahl`` neuesten Treffer im geöffneten Ordner. Gesucht wird zuerst nur unter den
        zuletzt eingegangenen Mails (``FENSTER_UIDS``), dann in einem fünffach größeren Fenster und so fort: ein
        Posteingang mit Hunderttausenden Mails liefert so nie eine Antwort, die für ``imaplib`` zu groß ist."""
        ab = max(1, self.uidnext - FENSTER_UIDS) if self.uidnext else None
        while True:
            uids = self.suchen(gruppen, ab)
            if len(uids) >= anzahl or ab is None or ab <= 1:
                return uids[:anzahl]
            ab = max(1, self.uidnext - 5 * (self.uidnext - ab)) if self.uidnext else 1

    def holen(self, uids: Sequence[int], items: str) -> list[dict[str, Any]]:
        """``UID FETCH`` mit nur lesenden Angaben; die Antworten als Wörterbücher (siehe ``antwort.fetch_antworten``)."""
        _items_pruefen(items)
        if not uids or not all(isinstance(u, int) and u > 0 for u in uids):
            raise AssertionError("UID-Liste unbrauchbar")
        typ, daten = self._uid("FETCH", ",".join(str(u) for u in uids), f"({items})")
        if typ != "OK":
            raise ImapFehler(f"{self.konto.host} hat die Mails nicht geliefert{self._grund(daten)}.", "server", 502)
        try:
            return antwort.fetch_antworten(daten)
        except antwort.AntwortFehler:
            raise ImapFehler(f"{self.konto.host} hat eine unlesbare Antwort geschickt.", "server", 502) from None

    def teile(self, uids: Sequence[int], abschnitt: str, maximal: int) -> dict[int, bytes]:
        """Die ersten ``maximal`` Bytes eines Abschnitts (``1``, ``1.2``) je UID, ohne den Gelesen-Status zu ändern."""
        aus: dict[int, bytes] = {}
        for a in self.holen(uids, f"UID BODY.PEEK[{abschnitt}]<0.{int(maximal)}>"):
            roh = antwort.abschnitt(a, abschnitt)
            if roh is not None:
                aus[a["UID"]] = roh
        return aus
