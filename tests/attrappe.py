"""IMAP-Attrappe: ein kleiner Server auf 127.0.0.1, der das Protokoll wirklich spricht.

Warum eine Attrappe am Socket und kein Ersatz für ``imaplib``? Das Plugin soll beweisen, dass es nur liest. Das zeigt sich erst
an den Bytes, die auf der Leitung stehen: ``imaplib`` läuft hier echt (Literale, Anführungszeichen, Fortsetzungszeilen), und die
Attrappe protokolliert jeden Befehl, den sie erhält. Ein Befehl, der ein Postfach ändern könnte (``SELECT``, ``STORE``,
``COPY``, ``EXPUNGE``, ``APPEND`` …), oder eine ``FETCH``-Angabe, die ``\\Seen`` setzen würde (``BODY[…]`` ohne ``PEEK``,
``RFC822``), landet in ``verboten`` und wird mit ``BAD`` beantwortet; die Tests prüfen, dass ``verboten`` leer bleibt.

Die Attrappe kann: ``CAPABILITY``, ``STARTTLS``, ``LOGIN``, ``AUTHENTICATE PLAIN``, ``LIST``, ``STATUS``, ``EXAMINE``,
``UID SEARCH`` (``ALL``, ``UNDELETED``, ``SEEN``, ``UNSEEN``, ``FROM``, ``TO``, ``SUBJECT``, ``TEXT``, ``SINCE``, ``BEFORE``,
``UID``, ``NOT``, ``CHARSET`` und bei Gmail ``X-GM-RAW``), ``UID FETCH`` (``UID``, ``FLAGS``, ``INTERNALDATE``,
``RFC822.SIZE``, ``BODYSTRUCTURE``, ``X-GM-LABELS`` und ``BODY[…]`` mit Abschnittsnummern, ``HEADER.FIELDS`` und Teilabruf
``<0.n>``) und ``LOGOUT``. Alles andere ist ``BAD``. Mit einem Zertifikat spricht sie TLS (``tls``) oder bietet ``STARTTLS`` an.

Es sind Attrappen mit erfundenen Konten; ein echtes Postfach kommt in diesen Tests nie vor.
"""

from __future__ import annotations

import base64
import email
import email.header
import email.policy
import email.utils
import re
import socketserver
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage, Message
from typing import Any

ERLAUBT = frozenset({
    "CAPABILITY", "STARTTLS", "LOGIN", "AUTHENTICATE", "LIST", "STATUS", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT",
})
_MONATE = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FETCH_ITEM = re.compile(
    r"UID|FLAGS|INTERNALDATE|RFC822\.SIZE|BODYSTRUCTURE|X-GM-LABELS|RFC822(?:\.TEXT|\.HEADER)?"
    r"|BODY(?:\.PEEK)?\[[^\]]*\](?:<\d+\.\d+>)?"
)
JETZT = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


# ── Mails bauen ─────────────────────────────────────────────────────────────────────────────────────────
def mail(
    von: str = "Alex Muster <alex@example.org>",
    an: str = "Test <test@example.com>",
    betreff: str = "Hallo",
    text: str | None = "Ein kurzer Text.",
    html: str | None = None,
    anhaenge: tuple[tuple[str, str, bytes], ...] = (),
    datum: datetime | None = None,
    charset: str = "utf-8",
    cte: str | None = None,
    zusatz: dict[str, str] | None = None,
) -> bytes:
    """Eine Mail als Rohdaten (CRLF). ``anhaenge``: (Dateiname, Typ ``haupt/unter``, Inhalt)."""
    m = EmailMessage(policy=email.policy.SMTP)
    m["From"] = von
    m["To"] = an
    m["Subject"] = betreff
    m["Date"] = email.utils.format_datetime(datum or JETZT - timedelta(hours=1))
    for k, v in (zusatz or {}).items():
        m[k] = v
    if text is not None:
        m.set_content(text, charset=charset, cte=cte) if cte else m.set_content(text, charset=charset)
        if html is not None:
            m.add_alternative(html, subtype="html", charset=charset)
    elif html is not None:
        m.set_content(html, subtype="html", charset=charset)
    for name, typ, inhalt in anhaenge:
        haupt, _, unter = typ.partition("/")
        m.add_attachment(inhalt, maintype=haupt, subtype=unter, **({"filename": name} if name else {}))
    return m.as_bytes()


def roh_mail(kopf: list[tuple[str, str]], koerper: bytes) -> bytes:
    """Eine Mail aus handgeschriebenen Kopfzeilen und Körper (für kaputte und seltene Formen)."""
    zeilen = b"".join(f"{k}: {v}\r\n".encode("utf-8") for k, v in kopf)
    return zeilen + b"\r\n" + koerper


def _grund_kopf(betreff: str = "Rohe Mail") -> list[tuple[str, str]]:
    return [
        ("From", "Alex Muster <alex@example.org>"), ("To", "Test <test@example.com>"), ("Subject", betreff),
        ("Date", email.utils.format_datetime(JETZT - timedelta(hours=1))), ("MIME-Version", "1.0"),
    ]


def mail_mit_bild() -> bytes:
    """HTML mit einem eingebetteten Bild (multipart/related mit Content-ID): das Bild ist kein Anhang."""
    m = EmailMessage(policy=email.policy.SMTP)
    m["From"], m["To"], m["Subject"] = "Alex Muster <alex@example.org>", "Test <test@example.com>", "Mit Logo"
    m["Date"] = email.utils.format_datetime(JETZT - timedelta(hours=1))
    m.set_content("Klartext der Mail")
    m.add_alternative('<p>Hallo mit <img src="cid:logo"></p>', subtype="html")
    m.get_payload()[1].add_related(b"\x89PNG-ATTRAPPE" * 10, "image", "png", cid="<logo>")
    return m.as_bytes()


def mail_signiert() -> bytes:
    """multipart/signed mit abgetrennter Signatur: die Signatur ist kein Anhang, den ein Mensch kennt."""
    koerper = (
        b"--sig\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nSignierter Text\r\n"
        b"--sig\r\nContent-Type: application/pgp-signature; name=\"signature.asc\"\r\n\r\nSIGNATUR-ATTRAPPE\r\n--sig--\r\n"
    )
    return roh_mail(
        _grund_kopf("Signiert") + [("Content-Type", 'multipart/signed; protocol="application/pgp-signature"; boundary="sig"')],
        koerper)


def mail_mit_zwei_textteilen() -> bytes:
    koerper = (
        b"--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nErster Teil\r\n"
        b"--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nZweiter Teil\r\n--b1--\r\n"
    )
    return roh_mail(_grund_kopf("Zwei Teile") + [("Content-Type", 'multipart/mixed; boundary="b1"')], koerper)


def mail_mit_weiterleitung() -> bytes:
    """Eine Mail, die eine andere als Anhang trägt (message/rfc822)."""
    innen = EmailMessage(policy=email.policy.SMTP)
    innen["From"], innen["To"], innen["Subject"] = "Innen <innen@example.net>", "Test <test@example.com>", "Die weitergeleitete Mail"
    innen.set_content("Inhalt der inneren Mail")
    m = EmailMessage(policy=email.policy.SMTP)
    m["From"], m["To"], m["Subject"] = "Alex Muster <alex@example.org>", "Test <test@example.com>", "Fwd"
    m["Date"] = email.utils.format_datetime(JETZT - timedelta(hours=1))
    m.set_content("Siehe unten")
    m.add_attachment(innen)
    return m.as_bytes()


@dataclass
class Nachricht:
    uid: int
    roh: bytes
    flags: set[str] = field(default_factory=set)
    intern: datetime = JETZT - timedelta(hours=1)
    labels: list[str] = field(default_factory=list)  # nur Gmail (X-GM-LABELS)

    def __post_init__(self) -> None:
        self.msg: Message = email.message_from_bytes(self.roh)


def n(
    uid: int,
    betreff: str = "Hallo",
    von: str = "Alex Muster <alex@example.org>",
    text: str | None = "Ein kurzer Text.",
    gelesen: bool = False,
    vor: timedelta = timedelta(hours=1),
    flags: tuple[str, ...] | set[str] = (),
    labels: tuple[str, ...] | list[str] = (),
    **kw: Any,
) -> Nachricht:
    """Eine Nachricht der Attrappe, ``vor`` der Attrappen-Zeit eingegangen (Kopf- und Eingangsdatum gleich)."""
    markiert = set(flags) | ({"\\Seen"} if gelesen else set())
    roh = mail(von=von, betreff=betreff, text=text, datum=JETZT - vor, **kw)
    return Nachricht(uid, roh, flags=markiert, intern=JETZT - vor, labels=list(labels))


@dataclass
class Ordner:
    name: str  # so, wie der Server ihn schreibt (IMAP-UTF-7)
    nachrichten: list[Nachricht] = field(default_factory=list)
    attribute: tuple[str, ...] = ()
    gueltig: int | None = 1700000001  # UIDVALIDITY; ``None``: der Server nennt sie nicht
    naechste: int | None = None  # UIDNEXT; ohne Angabe höchste UID + 1


# ── Struktur ────────────────────────────────────────────────────────────────────────────────────────────
def _q(s: str | None) -> str:
    if s is None:
        return "NIL"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _roh_parameter(wert: str) -> list[tuple[str, str]]:
    """Die Parameter eines Kopfwerts unverändert (``filename*0*=…`` bleibt, wie es dasteht)."""
    teile: list[str] = []
    aktuell = ""
    zitat = False
    for z in wert:
        if z == '"':
            zitat = not zitat
        if z == ";" and not zitat:
            teile.append(aktuell)
            aktuell = ""
        else:
            aktuell += z
    teile.append(aktuell)
    aus = []
    for t in teile[1:]:
        name, _, w = t.strip().partition("=")
        if name:
            aus.append((name.strip().lower(), w.strip().strip('"')))
    return aus


def _param_liste(paare: list[tuple[str, str]]) -> str:
    return "(" + " ".join(f"{_q(n.upper())} {_q(w)}" for n, w in paare) + ")" if paare else "NIL"


def _koerper(teil: Message) -> bytes:
    # Der Körper, wie er auf der Leitung stand: ``get_payload()`` würde 8-Bit-Text mit Zeichensatz schon dekodieren.
    nutzlast = teil._payload  # noqa: SLF001
    if isinstance(nutzlast, list):  # message/rfc822: die eingebettete Mail
        return nutzlast[0].as_bytes() if nutzlast else b""
    return str(nutzlast).encode("ascii", "surrogateescape")


def _kopfwert_roh(teil: Message, name: str) -> str | None:
    for k, v in teil.raw_items():
        if k.lower() == name.lower():
            return " ".join(str(v).split())
    return None


def _struktur(teil: Message) -> str:
    typ = _kopfwert_roh(teil, "Content-Type") or "text/plain"
    disp_roh = _kopfwert_roh(teil, "Content-Disposition")
    disp = "NIL"
    if disp_roh:
        art = disp_roh.split(";", 1)[0].strip().lower()
        disp = f"({_q(art.upper())} {_param_liste(_roh_parameter(disp_roh))})"
    if teil.is_multipart() and teil.get_content_maintype() == "multipart":
        kinder = "".join(_struktur(k) for k in teil.get_payload())
        return f"({kinder} {_q(teil.get_content_subtype().upper())} {_param_liste(_roh_parameter(typ))} {disp} NIL NIL)"
    haupt, unter = teil.get_content_maintype(), teil.get_content_subtype()
    koerper = _koerper(teil)
    cte = _kopfwert_roh(teil, "Content-Transfer-Encoding") or "7bit"
    felder = (f"{_q(haupt.upper())} {_q(unter.upper())} {_param_liste(_roh_parameter(typ))} "
              f"{_q(_kopfwert_roh(teil, 'Content-ID'))} NIL {_q(cte.upper())} {len(koerper)}")
    zeilen = koerper.count(b"\n")
    if haupt == "text":
        return f"({felder} {zeilen} NIL {disp} NIL NIL)"
    if (haupt, unter) == ("message", "rfc822"):
        return f"({felder} NIL NIL {zeilen} NIL {disp} NIL NIL)"
    return f"({felder} NIL {disp} NIL NIL)"


# ── Suchen ──────────────────────────────────────────────────────────────────────────────────────────────
class _Schlecht(Exception):
    """Der Befehl ist für den Server unverständlich (Antwort BAD)."""


def _dekodiert(wert: str | None) -> str:
    if not wert:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(wert)))
    except Exception:  # noqa: BLE001
        return wert


def _text_der(n: Nachricht) -> str:
    stuecke = [f"{k}: {_dekodiert(str(v))}" for k, v in n.msg.raw_items()]
    for teil in n.msg.walk():
        if teil.get_content_maintype() == "text":
            daten = teil.get_payload(decode=True)
            if isinstance(daten, bytes):
                stuecke.append(daten.decode(teil.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(stuecke)


def _menge(text: str, hoechste: int) -> set[int]:
    aus: set[int] = set()
    for teil in text.split(","):
        a, _, b = teil.partition(":")
        von = hoechste if a == "*" else int(a)
        bis = (hoechste if b == "*" else int(b)) if b else von
        lo, hi = min(von, bis), max(von, bis)
        aus.update(range(lo, hi + 1))
    return aus


def _datum(text: str) -> date:
    tag, monat, jahr = text.split("-")
    return date(int(jahr), _MONATE.index(monat.capitalize()) + 1, int(tag))


def _gmail_trifft(abfrage: str, n: Nachricht, jetzt: datetime) -> bool:
    """Eine kleine Fassung der Gmail-Suche für die Attrappe: in:, is:, from:, subject:, newer_than:, category:, Wörter, ``-``."""
    teile = re.findall(r'-?(?:[a-z_]+:)?(?:"[^"]*"|\S+)', abfrage)
    labels = {x.lstrip("\\").casefold().replace(" ", "_") for x in n.labels}
    for t in teile:
        negiert = t.startswith("-")
        t = t.lstrip("-")
        schluessel, _, wert = t.partition(":") if re.match(r"[a-z_]+:", t) else ("", "", t)
        wert = wert.strip('"')
        if schluessel == "in":
            ok = wert.lower() in labels
        elif schluessel == "category":
            ok = f"category_{wert.lower()}" in labels
        elif schluessel == "is":
            ok = ("\\Seen" not in n.flags) if wert == "unread" else ("\\Seen" in n.flags)
        elif schluessel == "from":
            ok = wert.casefold() in _dekodiert(n.msg.get("From")).casefold()
        elif schluessel == "subject":
            ok = wert.casefold() in _dekodiert(n.msg.get("Subject")).casefold()
        elif schluessel == "newer_than":
            tage = int(re.sub(r"\D", "", wert))
            ok = n.intern >= jetzt - timedelta(days=tage)
        else:
            ok = (schluessel + ":" + wert if schluessel else wert).casefold() in _text_der(n).casefold()
        if ok == negiert:
            return False
    return True


# ── Server ──────────────────────────────────────────────────────────────────────────────────────────────
class _Tcp(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, adresse: tuple[str, int], handler: Any, attrappe: Attrappe) -> None:
        super().__init__(adresse, handler)
        self.attrappe = attrappe

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Ein Client, der das Zertifikat ablehnt oder auflegt, ist in den Tests Absicht und kein Fehler der Attrappe.
        if isinstance(sys.exc_info()[1], (ssl.SSLError, OSError)):
            return
        super().handle_error(request, client_address)


class Attrappe:
    """Ein IMAP-Server mit ein paar Ordnern. ``Attrappe(...).start()``, am Ende ``stop()``."""

    def __init__(
        self,
        ordner: dict[str, list[Nachricht]] | list[Ordner] | None = None,
        *,
        nutzer: str = "alex",
        passwort: str = "geheim",
        gmail: bool = False,
        tls: ssl.SSLContext | None = None,
        starttls: ssl.SSLContext | None = None,
        login_gesperrt: bool = False,
        jetzt: datetime = JETZT,
    ) -> None:
        if isinstance(ordner, list):
            self.ordner = {o.name: o for o in ordner}
        else:
            self.ordner = {name: Ordner(name, list(ms)) for name, ms in (ordner or {"INBOX": []}).items()}
        self.nutzer, self.passwort = nutzer, passwort
        self.gmail = gmail
        self.tls, self.starttls_kontext = tls, starttls
        self.login_gesperrt = login_gesperrt
        self.jetzt = jetzt
        self.trenner = "/"
        # Verhalten, das Tests einschalten
        self.stumm = False  # nimmt die Verbindung an und sagt nichts (Zeitüberschreitung)
        self.begruessung_mit_faehigkeiten = True  # False: ``imaplib`` fragt selbst mit CAPABILITY
        self.trennt_nach_login = False
        self.login_text = "NO [AUTHENTICATIONFAILED] Authentication failed."
        self.liste_als_literal = False
        self.unaufgefordert = False
        self.verzoegerung_s = 0.0
        self.suche_antwort_max: int | None = None  # kappt die Zahl der UIDs je SEARCH-Antwort (zu große Antworten)
        self.fehler_bei: dict[str, str] = {}  # Befehl -> Antwortzeile "NO text" (erzwungener Fehler)
        self.teilabruf_fehler: str | None = None  # Antwortzeile für jedes FETCH mit Teilabruf <0.n> (z. B. "NO nein")
        # Protokoll
        self.befehle: list[tuple[str, str]] = []
        self.verboten: list[str] = []
        self.intern_fehler: list[str] = []
        self.verbindungen = 0
        self._sperre = threading.Lock()
        self._server: _Tcp | None = None
        self._thread: threading.Thread | None = None

    # ── Lebenslauf ────────────────────────────────────────────────────────────────────────────────────
    def start(self) -> Attrappe:
        self._server = _Tcp(("127.0.0.1", 0), _Verbindung, self)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True, name="imap-attrappe")
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    # ── Auswertung durch die Tests ────────────────────────────────────────────────────────────────────
    def verben(self, mit_faehigkeiten: bool = False) -> list[str]:
        """Die Verben aller Befehle in der Reihenfolge. ``CAPABILITY`` fehlt, wenn nicht verlangt: ob ``imaplib`` danach fragt,
        hängt davon ab, ob schon die Begrüßung die Fähigkeiten nennt, und ist für die Tests ohne Belang."""
        with self._sperre:
            return [v for v, _ in self.befehle if mit_faehigkeiten or v != "CAPABILITY"]

    def zeilen(self, verb: str | None = None) -> list[str]:
        with self._sperre:
            return [t for v, t in self.befehle if verb is None or v == verb]

    def flags(self) -> dict[tuple[str, int], frozenset[str]]:
        """Kennzeichen aller Mails (zum Prüfen, dass nichts geändert wurde)."""
        return {(o.name, n.uid): frozenset(n.flags) for o in self.ordner.values() for n in o.nachrichten}

    def _protokoll(self, verb: str, text: str) -> None:
        with self._sperre:
            self.befehle.append((verb, text))

    def _verboten(self, text: str) -> None:
        with self._sperre:
            self.verboten.append(text)

    def faehigkeiten(self, verschluesselt: bool) -> str:
        teile = ["IMAP4rev1", "UIDPLUS"]
        if self.starttls_kontext is not None and not verschluesselt:
            teile.append("STARTTLS")
        if self.login_gesperrt:
            teile.append("LOGINDISABLED")
        else:
            teile.append("AUTH=PLAIN")
        if self.gmail:
            teile.append("X-GM-EXT-1")
        return " ".join(teile)

    def finde_ordner(self, name: str) -> Ordner | None:
        if name.upper() == "INBOX":
            return next((o for o in self.ordner.values() if o.name.upper() == "INBOX"), None)
        return self.ordner.get(name)


def _token_liste(daten: bytes, literale: list[bytes]) -> list[Any]:
    """Zerlegt die Argumente eines Befehls: Atome und Zeichenketten als ``str``, Klammern als Listen."""
    stapel: list[list[Any]] = [[]]
    i, n = 0, len(daten)
    while i < n:
        z = daten[i:i + 1]
        if z in (b" ", b"\t"):
            i += 1
        elif z == b"(":
            stapel.append([])
            i += 1
        elif z == b")":
            fertig = stapel.pop()
            stapel[-1].append(fertig)
            i += 1
        elif z == b'"':
            j = i + 1
            aus = bytearray()
            while j < n and daten[j:j + 1] != b'"':
                if daten[j:j + 1] == b"\\" and j + 1 < n:
                    j += 1
                aus.append(daten[j])
                j += 1
            stapel[-1].append(aus.decode("utf-8", errors="replace"))
            i = j + 1
        elif z == b"\x00":
            ende = daten.index(b"\x00", i + 1)
            stapel[-1].append(literale[int(daten[i + 2:ende])].decode("utf-8", errors="replace"))
            i = ende + 1
        else:
            j = i
            while j < n and daten[j:j + 1] not in (b" ", b"\t", b"(", b")", b'"', b"\x00"):
                if daten[j:j + 1] == b"[":
                    j = daten.index(b"]", j)
                j += 1
            stapel[-1].append(daten[i:j].decode("utf-8", errors="replace"))
            i = j
    return stapel[0]


class _Verbindung(socketserver.StreamRequestHandler):
    server: _Tcp

    # Jede Antwortzeile geht einzeln hinaus. Unter Linux wartet das Nagle-Verfahren beim zweiten kurzen Schreiben auf die
    # (verzögerte) Bestätigung des Empfängers, rund 40 ms je Austausch: die Tests brauchten in der CI sechs Minuten statt
    # fünfzehn Sekunden (auf macOS fällt das nicht auf). Mit TCP_NODELAY geht jede Zeile sofort hinaus.
    disable_nagle_algorithm = True

    # ── Hilfen ────────────────────────────────────────────────────────────────────────────────────────
    @property
    def a(self) -> Attrappe:
        return self.server.attrappe

    def setup(self) -> None:
        if self.a.tls is not None:
            self.request = self.a.tls.wrap_socket(self.request, server_side=True)
        super().setup()
        self.verschluesselt = self.a.tls is not None
        self.ausgewaehlt: Ordner | None = None
        self.angemeldet = False

    def senden(self, daten: str | bytes) -> None:
        if isinstance(daten, str):
            daten = daten.encode("utf-8")
        if self.a.verzoegerung_s:
            time.sleep(self.a.verzoegerung_s)
        self.wfile.write(daten + (b"" if daten.endswith(b"\r\n") else b"\r\n"))
        self.wfile.flush()

    def befehl_lesen(self) -> tuple[bytes, list[bytes]] | None:
        teile: list[bytes] = []
        literale: list[bytes] = []
        while True:
            zeile = self.rfile.readline(2_000_000)
            if not zeile:
                return None
            zeile = zeile.rstrip(b"\r\n")
            treffer = re.search(rb"\{(\d+)(\+?)\}$", zeile)
            if treffer:
                teile.append(zeile[: treffer.start()])
                if not treffer.group(2):
                    self.senden("+ bereit")
                literale.append(self.rfile.read(int(treffer.group(1))))
                teile.append(b"\x00L%d\x00" % (len(literale) - 1))
                continue
            teile.append(zeile)
            return b"".join(teile), literale

    # ── Schleife ──────────────────────────────────────────────────────────────────────────────────────
    def handle(self) -> None:
        a = self.a
        with a._sperre:
            a.verbindungen += 1
        if a.stumm:
            time.sleep(30)
            return
        if a.begruessung_mit_faehigkeiten:
            self.senden(f"* OK [CAPABILITY {a.faehigkeiten(self.verschluesselt)}] Attrappe bereit")
        else:
            self.senden("* OK Attrappe bereit")
        while True:
            try:
                gelesen = self.befehl_lesen()
            except (ConnectionError, ssl.SSLError, OSError):
                return
            if gelesen is None:
                return
            daten, literale = gelesen
            tag, _, rest = daten.partition(b" ")
            tag_text = tag.decode("ascii", errors="replace")
            try:
                tokens = _token_liste(rest, literale)
            except (ValueError, IndexError):  # kein Befehl: zum Beispiel der Anfang eines TLS-Handschlags an einem Klartextport
                self.senden(f"{tag_text} BAD nicht lesbar")
                continue
            if not tokens or not isinstance(tokens[0], str):
                self.senden(f"{tag_text} BAD leerer Befehl")
                continue
            verb = tokens[0].upper()
            if verb == "UID" and len(tokens) > 1 and isinstance(tokens[1], str):
                verb = "UID " + tokens[1].upper()
            klartext = re.sub(rb"\x00L(\d+)\x00", lambda m: b"<<" + repr(literale[int(m.group(1))].decode("utf-8", "replace")).encode() + b">>", rest)
            text = klartext.decode("utf-8", errors="replace")
            if verb == "LOGIN":  # das Passwort steht nie im Protokoll; der Nutzer, wie er gesendet wurde (mit Anführungszeichen)
                text = re.sub(r'^(LOGIN\s+(?:"(?:[^"\\]|\\.)*"|\S+))\s.*$', r"\1 ***", text)
            a._protokoll(verb, text)
            if verb not in ERLAUBT:
                a._verboten(text)
                self.senden(f"{tag_text} BAD Befehl nicht erlaubt (Attrappe): {verb}")
                continue
            if verb in a.fehler_bei:
                self.senden(f"{tag_text} {a.fehler_bei[verb]}")
                continue
            try:
                weiter = getattr(self, "b_" + verb.replace(" ", "_").lower())(tag_text, tokens, text)
            except _Schlecht as grund:
                self.senden(f"{tag_text} BAD {grund}")
                continue
            except (ConnectionError, ssl.SSLError, OSError):
                return
            except Exception as fehler:  # noqa: BLE001 — ein Fehler der Attrappe soll der Test sehen, nicht nur ein Verbindungsabbruch
                with a._sperre:
                    a.intern_fehler.append(f"{type(fehler).__name__}: {fehler}")
                self.senden(f"{tag_text} BAD interner Fehler der Attrappe")
                continue
            if weiter is False:
                return
            if a.trennt_nach_login and verb in ("LOGIN", "AUTHENTICATE"):
                return

    # ── Befehle ───────────────────────────────────────────────────────────────────────────────────────
    def b_capability(self, tag: str, tokens: list[Any], text: str) -> None:
        self.senden(f"* CAPABILITY {self.a.faehigkeiten(self.verschluesselt)}")
        self.senden(f"{tag} OK CAPABILITY abgeschlossen")

    def b_starttls(self, tag: str, tokens: list[Any], text: str) -> None:
        if self.a.starttls_kontext is None or self.verschluesselt:
            self.senden(f"{tag} BAD STARTTLS nicht unterstützt")
            return
        self.senden(f"{tag} OK Verschlüsselung jetzt aufbauen")
        self.connection = self.a.starttls_kontext.wrap_socket(self.connection, server_side=True)
        self.rfile = self.connection.makefile("rb")
        self.wfile = self.connection.makefile("wb")  # gepuffert: schreibt auch große Antworten ganz
        self.verschluesselt = True

    def b_login(self, tag: str, tokens: list[Any], text: str) -> None:
        if self.a.login_gesperrt:
            self.senden(f"{tag} NO [PRIVACYREQUIRED] LOGIN ist nicht erlaubt")
            return
        if len(tokens) == 3 and tokens[1] == self.a.nutzer and tokens[2] == self.a.passwort:
            self.angemeldet = True
            self.senden(f"{tag} OK [CAPABILITY {self.a.faehigkeiten(self.verschluesselt)}] angemeldet")
        else:
            self.senden(f"{tag} {self.a.login_text}")

    def b_authenticate(self, tag: str, tokens: list[Any], text: str) -> None:
        if len(tokens) < 2 or str(tokens[1]).upper() != "PLAIN":
            raise _Schlecht("nur PLAIN")
        self.senden("+ ")
        zeile = self.rfile.readline().strip()
        try:
            _, nutzer, passwort = base64.b64decode(zeile).decode("utf-8").split("\0")
        except Exception:  # noqa: BLE001
            raise _Schlecht("unlesbare Anmeldung") from None
        if nutzer == self.a.nutzer and passwort == self.a.passwort:
            self.angemeldet = True
            self.senden(f"{tag} OK angemeldet")
        else:
            self.senden(f"{tag} {self.a.login_text}")

    def _angemeldet(self) -> None:
        if not self.angemeldet:
            raise _Schlecht("erst anmelden")

    def b_list(self, tag: str, tokens: list[Any], text: str) -> None:
        self._angemeldet()
        for o in self.a.ordner.values():
            attribute = " ".join(("\\HasNoChildren", *o.attribute))
            name = o.name
            if self.a.liste_als_literal:
                kodiert = name.encode("utf-8")
                self.senden(f'* LIST ({attribute}) "{self.a.trenner}" {{{len(kodiert)}}}'.encode() + b"\r\n" + kodiert)
            else:
                self.senden(f'* LIST ({attribute}) "{self.a.trenner}" "{name}"')
        self.senden(f"{tag} OK LIST abgeschlossen")

    def b_status(self, tag: str, tokens: list[Any], text: str) -> None:
        self._angemeldet()
        ordner = self.a.finde_ordner(str(tokens[1]))
        if ordner is None:
            self.senden(f"{tag} NO [NONEXISTENT] Ordner gibt es nicht")
            return
        ungelesen = sum(1 for n in ordner.nachrichten if "\\Seen" not in n.flags)
        self.senden(f'* STATUS "{tokens[1]}" (UNSEEN {ungelesen})')
        self.senden(f"{tag} OK STATUS abgeschlossen")

    def b_examine(self, tag: str, tokens: list[Any], text: str) -> None:
        self._angemeldet()
        ordner = self.a.finde_ordner(str(tokens[1]))
        if ordner is None:
            self.senden(f"{tag} NO [NONEXISTENT] Ordner gibt es nicht")
            return
        self.ausgewaehlt = ordner
        hoechste = max((n.uid for n in ordner.nachrichten), default=0)
        self.senden("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
        self.senden(f"* {len(ordner.nachrichten)} EXISTS")
        self.senden("* 0 RECENT")
        if ordner.gueltig is not None:
            self.senden(f"* OK [UIDVALIDITY {ordner.gueltig}] UIDs gültig")
        self.senden(f"* OK [UIDNEXT {ordner.naechste or hoechste + 1}] vorhergesagt")
        self.senden(f"{tag} OK [READ-ONLY] EXAMINE abgeschlossen")

    def b_uid_search(self, tag: str, tokens: list[Any], text: str) -> None:
        self._angemeldet()
        if self.ausgewaehlt is None:
            raise _Schlecht("kein Ordner gewählt")
        o = self.ausgewaehlt
        hoechste = max((n.uid for n in o.nachrichten), default=0)
        schluessel = tokens[2:]
        if schluessel[:1] and str(schluessel[0]).upper() == "CHARSET":
            schluessel = schluessel[2:]
        treffer = []
        for n in o.nachrichten:
            i, ok = 0, True
            while i < len(schluessel):
                wert, i = self._schluessel(schluessel, i, n, hoechste)
                ok = ok and wert
            if ok:
                treffer.append(n.uid)
        if self.a.suche_antwort_max is not None:
            treffer = treffer[-self.a.suche_antwort_max:]
        self.senden("* SEARCH" + "".join(f" {u}" for u in treffer))
        self.senden(f"{tag} OK SEARCH abgeschlossen")

    def _schluessel(self, t: list[Any], i: int, n: Nachricht, hoechste: int) -> tuple[bool, int]:
        k = t[i]
        if not isinstance(k, str):
            raise _Schlecht("Suchschlüssel erwartet")
        K = k.upper()
        try:
            if K == "NOT":
                wert, j = self._schluessel(t, i + 1, n, hoechste)
                return (not wert), j
            if K == "ALL":
                return True, i + 1
            if K == "UNDELETED":
                return "\\Deleted" not in n.flags, i + 1
            if K == "UNSEEN":
                return "\\Seen" not in n.flags, i + 1
            if K == "SEEN":
                return "\\Seen" in n.flags, i + 1
            if K in ("FROM", "TO", "SUBJECT"):
                feld = _dekodiert(n.msg.get(k.title()))
                return str(t[i + 1]).casefold() in feld.casefold(), i + 2
            if K in ("TEXT", "BODY"):
                return str(t[i + 1]).casefold() in _text_der(n).casefold(), i + 2
            if K in ("SINCE", "BEFORE"):
                tag = _datum(str(t[i + 1]))
                heute = n.intern.date()
                return (heute >= tag if K == "SINCE" else heute < tag), i + 2
            if K == "UID":
                return n.uid in _menge(str(t[i + 1]), hoechste), i + 2
            if K == "X-GM-RAW" and self.a.gmail:
                return _gmail_trifft(str(t[i + 1]), n, self.a.jetzt), i + 2
        except (IndexError, ValueError):
            raise _Schlecht(f"Suchschlüssel {k} unvollständig") from None
        raise _Schlecht(f"unbekannter Suchschlüssel {k}")

    def b_uid_fetch(self, tag: str, tokens: list[Any], text: str) -> None:
        self._angemeldet()
        if self.ausgewaehlt is None:
            raise _Schlecht("kein Ordner gewählt")
        treffer = re.match(r"UID FETCH (\S+) \((.*)\)\s*$", text)
        if not treffer:
            raise _Schlecht("FETCH-Angabe nicht lesbar")
        o = self.ausgewaehlt
        hoechste = max((n.uid for n in o.nachrichten), default=0)
        menge = _menge(treffer.group(1), hoechste)
        items = _FETCH_ITEM.findall(treffer.group(2))
        if self.a.teilabruf_fehler and any(re.search(r"<\d+\.\d+>$", i) for i in items):
            self.senden(f"{tag} {self.a.teilabruf_fehler}")
            return
        for item in items:
            K = item.upper()
            if K.startswith("RFC822") and K != "RFC822.SIZE" or (K.startswith("BODY[")):
                self.a._verboten(f"UID FETCH {item}")  # würde \Seen setzen
        if self.a.unaufgefordert:
            self.senden("* 1 FETCH (FLAGS (\\Seen))")
        for seq, n in enumerate(o.nachrichten, 1):
            if n.uid in menge:
                self.senden(self._fetch_antwort(seq, n, items))
        self.senden(f"{tag} OK FETCH abgeschlossen")

    def _fetch_antwort(self, seq: int, n: Nachricht, items: list[str]) -> bytes:
        teile: list[bytes] = []
        for item in items:
            K = item.upper()
            if K == "UID":
                teile.append(b"UID %d" % n.uid)
            elif K == "FLAGS":
                teile.append(b"FLAGS (" + " ".join(sorted(n.flags)).encode() + b")")
            elif K == "INTERNALDATE":
                d = n.intern
                versatz = d.utcoffset() or timedelta(0)
                minuten = int(versatz.total_seconds() // 60)
                zeichen = "+" if minuten >= 0 else "-"
                teile.append(
                    f'INTERNALDATE "{d.day:2d}-{_MONATE[d.month - 1]}-{d.year} {d:%H:%M:%S} '
                    f'{zeichen}{abs(minuten) // 60:02d}{abs(minuten) % 60:02d}"'.encode())
            elif K == "RFC822.SIZE":
                teile.append(b"RFC822.SIZE %d" % len(n.roh))
            elif K == "BODYSTRUCTURE":
                teile.append(b"BODYSTRUCTURE " + _struktur(n.msg).encode("utf-8"))
            elif K == "X-GM-LABELS":
                teile.append(b"X-GM-LABELS (" + " ".join(x if x.startswith("\\") else _q(x) for x in n.labels).encode() + b")")
            elif K.startswith("BODY"):
                teile.append(self._abschnitt(n, item))
            else:  # RFC822 und Verwandte: nur damit der Verstoß sichtbar wird
                n.flags.add("\\Seen")
                teile.append(item.encode() + b" " + _literal(n.roh))
        return b"* %d FETCH (" % seq + b" ".join(teile) + b")\r\n"

    def _abschnitt(self, n: Nachricht, item: str) -> bytes:
        peek = ".PEEK" in item.upper()
        treffer = re.match(r"BODY(?:\.PEEK)?\[([^\]]*)\](?:<(\d+)\.(\d+)>)?", item, re.I)
        assert treffer is not None
        angabe, anfang, laenge = treffer.group(1), treffer.group(2), treffer.group(3)
        if not peek:
            n.flags.add("\\Seen")  # so verhält sich ein echter Server; die Tests melden es als Verstoß
        daten = self._abschnitt_daten(n, angabe)
        schluessel = f"BODY[{angabe}]"
        if anfang is not None and daten is not None:
            daten = daten[int(anfang): int(anfang) + int(laenge)]
            schluessel += f"<{anfang}>"
        if daten is None:
            return schluessel.encode() + b" NIL"
        return schluessel.encode() + b" " + _literal(daten)

    @staticmethod
    def _abschnitt_daten(n: Nachricht, angabe: str) -> bytes | None:
        kopf, _, rumpf = n.roh.partition(b"\r\n\r\n")
        if angabe == "":
            return n.roh
        if angabe.upper() == "HEADER":
            return kopf + b"\r\n\r\n"
        if angabe.upper() == "TEXT":
            return rumpf
        treffer = re.match(r"HEADER\.FIELDS \(([^)]*)\)", angabe, re.I)
        if treffer:
            felder = {f.lower() for f in treffer.group(1).split()}
            ausgewaehlt: list[bytes] = []
            aktuell: list[bytes] = []
            for zeile in kopf.split(b"\r\n"):
                if zeile[:1] in (b" ", b"\t") and aktuell:
                    aktuell.append(zeile)
                    continue
                if aktuell:
                    ausgewaehlt.extend(aktuell)
                name = zeile.split(b":", 1)[0].decode("ascii", "replace").strip().lower()
                aktuell = [zeile] if name in felder else []
            ausgewaehlt.extend(aktuell)
            return b"".join(z + b"\r\n" for z in ausgewaehlt) + b"\r\n"
        if not re.fullmatch(r"\d+(\.\d+)*", angabe):
            return None
        teil: Message = n.msg
        for nummer in (int(x) for x in angabe.split(".")):
            if teil.is_multipart() and teil.get_content_maintype() == "multipart":
                kinder = teil.get_payload()
                if not 1 <= nummer <= len(kinder):
                    return None
                teil = kinder[nummer - 1]
            elif nummer != 1:
                return None
        return _koerper(teil)

    def b_logout(self, tag: str, tokens: list[Any], text: str) -> bool:
        self.senden("* BYE Attrappe beendet die Verbindung")
        self.senden(f"{tag} OK LOGOUT abgeschlossen")
        return False


def _literal(daten: bytes) -> bytes:
    return b"{%d}\r\n" % len(daten) + daten
