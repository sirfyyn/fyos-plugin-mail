"""Der Beweis, dass das Plugin nur liest und nichts hinterlässt.

Drei Arten von Belegen, damit keiner allein tragen muss:

1. **Auf der Leitung.** Jede Fähigkeit des Plugins läuft gegen eine Attrappe, die jeden Befehl protokolliert (``attrappe.py``).
   Die Verben müssen in der Liste der erlaubten Verben liegen und dürfen kein einziges der schreibenden enthalten. Jede
   ``FETCH``-Angabe mit ``BODY[…]`` steht mit ``PEEK``. Danach sind die Kennzeichen aller Mails unverändert (kein ``\\Seen``).
2. **Eine Gegenprobe.** Dieselbe Attrappe, von Hand mit ``imaplib`` bedient, bemerkt ``SELECT``, ``STORE``, ``COPY``,
   ``EXPUNGE``, ``APPEND`` und ein ``FETCH`` ohne ``PEEK``, und das Lesen ohne ``PEEK`` verändert sichtbar ein Kennzeichen.
   Ohne sie sagte ein leeres Protokoll nichts.
3. **Im Code.** Die Sitzung weist schreibende ``UID``-Befehle und nicht lesende ``FETCH``-Angaben ab, und eine Prüfung des
   Quelltextes zeigt, welche Methoden der Verbindung das Paket überhaupt aufruft.
"""

from __future__ import annotations

import ast
import contextlib
import imaplib
import re
from datetime import timedelta
from pathlib import Path

import pytest
from attrappe import (
    ERLAUBT,
    JETZT,
    Nachricht,
    Ordner,
    mail,
    mail_mit_bild,
    mail_mit_weiterleitung,
    mail_mit_zwei_textteilen,
    mail_signiert,
    n,
    roh_mail,
)

import fyos_plugin_mail
from fyos_plugin_mail import imap
from fyos_plugin_mail.imap import Sitzung
from fyos_plugin_mail.plugin import MailPlugin

pytestmark = pytest.mark.usefixtures("klartext")

#: Unabhängig von der Liste der Attrappe geschrieben: was nie auf der Leitung stehen darf
SCHREIBEND = frozenset({
    "SELECT", "STORE", "COPY", "MOVE", "EXPUNGE", "APPEND", "CREATE", "DELETE", "RENAME", "SUBSCRIBE", "UNSUBSCRIBE", "CLOSE",
    "UNSELECT", "CHECK", "IDLE", "SETACL", "DELETEACL", "SETQUOTA", "SETMETADATA", "ENABLE", "NOOP", "UID STORE", "UID COPY",
    "UID MOVE", "UID EXPUNGE", "XLIST", "REPLACE", "UID REPLACE", "AUTHENTICATE ANONYMOUS",
})
#: Was das Plugin senden darf (dasselbe wie die Attrappe versteht, hier ausgeschrieben)
LESEND = frozenset({"CAPABILITY", "STARTTLS", "LOGIN", "AUTHENTICATE", "LIST", "STATUS", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"})

ABFRAGEN = [
    "in:inbox", "from:erika", "subject:rechnung is:unread", "newer_than:7d", "older_than:1d", "after:2026/10/01 before:2026/10/11",
    "-newsletter", "Grüße", "from:Müller subject:Grüße", "in:sent", "in:trash", "has:attachment label:wichtig",
    "(from:a OR from:b) subject:c", "is:read", "-is:unread newer_than:3d",
    # Eingaben, die einen zweiten Befehl einschmuggeln sollen
    '"\r\nA1 STORE 1 +FLAGS (\\Deleted)', 'from:x\r\nA2 EXPUNGE', 'subject:"x" A3 APPEND INBOX {5}', "{9}\r\nUID STORE 1:* FLAGS \\Seen",
    '\\" ) A4 COPY 1:* Trash', "from:\x00STORE", "UID 1:* STORE", 'subject:"\\\\" A5 DELETE INBOX',
]


def mails(gmail: bool):
    """Neun Mails in allen Formen, die das Lesen kennen muss; ein Teil gelesen, ein Teil markiert."""
    qp = roh_mail([
        ("From", "Jürgen Müller <jm@example.net>"), ("To", "Test <test@example.com>"), ("Subject", "Latin-1"),
        ("Date", "Sat, 10 Oct 2026 08:00:00 +0000"), ("MIME-Version", "1.0"),
        ("Content-Type", 'text/plain; charset="iso-8859-1"'), ("Content-Transfer-Encoding", "quoted-printable"),
    ], b"Gr=FC=DFe aus K=F6ln\r\n")
    rohe = [
        mail(betreff="Einfach", von="Erika Beispiel <erika@example.org>"),
        mail(betreff="Alternativ", text="Klartext", html="<p>HTML</p>"),
        mail(betreff="Nur HTML", text=None, html="<p>Hallo <b>Welt</b></p>"),
        mail(betreff="Mit Anhang", anhaenge=(("Übersicht.pdf", "application/pdf", b"%PDF" * 200), ("", "application/zip", b"PK" * 50))),
        mail_mit_bild(), mail_signiert(), mail_mit_zwei_textteilen(), mail_mit_weiterleitung(), qp,
    ]
    flags = [set(), {"\\Seen"}, {"\\Flagged"}, {"\\Seen", "\\Answered"}, set(), {"\\Seen"}, {"\\Draft"}, set(), {"\\Seen", "\\Flagged"}]
    return [
        Nachricht(i, roh, flags=set(f), intern=JETZT - timedelta(hours=10 - i), labels=["\\Inbox"] if gmail else [])
        for i, (roh, f) in enumerate(zip(rohe, flags), 1)
    ]


def postfach(imap_server, gmail=False):
    return imap_server([
        Ordner("INBOX", mails(gmail)),
        Ordner("Gesendet", [n(1, "Ausgang", labels=["\\Sent"] if gmail else [])], attribute=("\\Sent",)),
        Ordner("Archiv", [n(1, "Im Archiv"), n(2, "Noch eins", gelesen=True)]),
    ], gmail=gmail)


#: Die Schlüssel, die in einer Suchzeile des Plugins vorkommen dürfen (alles andere steht in Anführungszeichen oder als Literal)
SUCHSCHLUESSEL = {
    "UID", "SEARCH", "UNDELETED", "SEEN", "UNSEEN", "NOT", "FROM", "TO", "SUBJECT", "TEXT", "SINCE", "BEFORE", "X-GM-RAW", "CHARSET",
    "UTF-8",
}


def suchzeile_pruefen(text: str) -> None:
    ohne = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)  # Werte in Anführungszeichen
    ohne = re.sub(r"<<.*?>>", "<<>>", ohne)  # Literale (die Attrappe zeigt sie so)
    for wort in ohne.split():
        assert wort in SUCHSCHLUESSEL or re.fullmatch(r'\d+:\*|\d+|""|<<>>|\d{2}-[A-Z][a-z]{2}-\d{4}', wort), (wort, text)


def test_die_pruefung_der_suchzeilen_erkennt_einen_eingeschmuggelten_befehl():
    suchzeile_pruefen('UID SEARCH UID 1:* UNDELETED FROM "a b" SINCE 03-Oct-2026 TEXT <<\'x\'>>')
    for schlecht in ('UID SEARCH UID 1:* UNDELETED STORE 1 +FLAGS', 'UID SEARCH ALL EXPUNGE', 'UID SEARCH TEXT "x" A1 COPY 1:* Trash'):
        with pytest.raises(AssertionError):
            suchzeile_pruefen(schlecht)


def alles_lesen(p: MailPlugin) -> None:
    """Ruft jede Fähigkeit des Plugins auf, die ein Postfach berührt."""
    p.call("konten", {"pruefen": True})
    gesehen: set[str] = set()
    for abfrage in ABFRAGEN:
        for voll in (False, True):
            antwort = p.call("suchen", {"suche": abfrage, "anzahl": 25, "voll": voll})
            gesehen |= {m["id"] for m in antwort.get("mails", [])}
    eingang = p.call("eingang", {"anzahl": 25, "voll": True})
    assert eingang["quelle_ok"] is True and len(eingang["mails"]) >= 9
    gesehen |= {m["id"] for m in eingang["mails"]}
    assert len(gesehen) >= 9
    for kennung in sorted(gesehen):
        for voll in (False, True):
            assert p.call("lesen", {"id": kennung, "voll": voll})["quelle_ok"] is True
    # was der Kern aufruft
    p.ablage_konten()
    liste = p.ablage_liste("in:inbox newer_than:2d -category:promotions", 25)
    for m in liste["mails"]:
        p.ablage_lesen(m["id"])
    p.ablage_liste("in:sent", 5)


@pytest.fixture(params=["imap", "gmail"])
def lauf(request, imap_server, monkeypatch):
    """Ein Plugin mit einem Konto (zwei Ordner) gegen die Attrappe, dazu die Attrappe; einmal wie ein IMAP-Server, einmal wie Gmail."""
    server = postfach(imap_server, gmail=request.param == "gmail")
    for name, wert in {
        "FYOS_MAIL_HOST": server.host, "FYOS_MAIL_PORT": str(server.port), "FYOS_MAIL_NUTZER": server.nutzer,
        "FYOS_MAIL_PASSWORT": server.passwort, "FYOS_MAIL_NAME": "Privat", "FYOS_MAIL_ORDNER": "INBOX, Archiv",
    }.items():
        monkeypatch.setenv(name, wert)
    return MailPlugin(heute=lambda: JETZT.date()), server


# ── 1. Auf der Leitung ──────────────────────────────────────────────────────────────────────────────────
def test_alle_faehigkeiten_senden_nur_lesende_befehle(lauf):
    p, server = lauf
    alles_lesen(p)
    verben = set(server.verben(mit_faehigkeiten=True))
    assert verben <= LESEND, f"unerwartete Befehle: {sorted(verben - LESEND)}"
    assert not verben & SCHREIBEND
    assert {"LOGIN", "LIST", "STATUS", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"} <= verben  # und es lief wirklich
    assert server.verboten == [], server.verboten
    assert server.intern_fehler == []


def test_die_alten_kennzeichen_bleiben_wie_sie_waren(lauf):
    p, server = lauf
    vorher = server.flags()
    assert any("\\Seen" not in f for f in vorher.values())  # es gibt ungelesene Mails, an denen sich ein Verstoß zeigen würde
    alles_lesen(p)
    assert server.flags() == vorher


def test_jedes_body_wird_mit_peek_geholt(lauf):
    p, server = lauf
    alles_lesen(p)
    abrufe = server.zeilen("UID FETCH")
    assert len(abrufe) > 20
    for text in abrufe:
        assert not re.search(r"BODY(?!\.PEEK)\[", text), text
        assert not re.search(r"RFC822(?!\.SIZE)", text), text
        assert "BODY.PEEK[" in text or "BODYSTRUCTURE" in text or "FLAGS" in text


def test_nur_der_posteingang_wird_gezaehlt_und_kein_ordner_geschrieben(lauf):
    p, server = lauf
    alles_lesen(p)
    assert set(server.zeilen("STATUS")) == {'STATUS "INBOX" (UNSEEN)'}
    assert all(z.startswith("EXAMINE ") for z in server.zeilen("EXAMINE"))
    assert not server.zeilen("SELECT")


def test_jede_sitzung_wird_beendet(lauf):
    p, server = lauf
    alles_lesen(p)
    assert server.verben().count("LOGOUT") == server.verben().count("LOGIN")


def test_die_liste_der_attrappe_und_die_unabhaengige_stimmen_ueberein():
    assert ERLAUBT == LESEND and not LESEND & SCHREIBEND


@pytest.mark.parametrize("abfrage", ABFRAGEN)
def test_keine_abfrage_schmuggelt_einen_befehl_ein(lauf, abfrage):
    p, server = lauf
    vorher = server.flags()
    p.call("suchen", {"suche": abfrage})
    with contextlib.suppress(Exception):  # eine unbrauchbare Abfrage darf scheitern, aber nichts senden
        p.ablage_liste(abfrage, 5)
    assert set(server.verben(mit_faehigkeiten=True)) <= LESEND
    assert server.verboten == [] and server.flags() == vorher and server.intern_fehler == []
    # Jede Suchzeile besteht außerhalb von Anführungszeichen und Literalen nur aus bekannten Schlüsseln: was ein Mensch
    # geschrieben hat, steht immer als Wert, nie als Befehl oder Schlüssel in der Zeile.
    for verb, text in server.befehle:
        assert verb in LESEND
        if verb == "UID SEARCH":
            suchzeile_pruefen(text)


# ── 2. Die Gegenprobe ───────────────────────────────────────────────────────────────────────────────────
def test_gegenprobe_die_attrappe_bemerkt_jeden_schreibenden_befehl(imap_server):
    s = imap_server([Ordner("INBOX", [n(1), n(2)]), Ordner("Archiv", [])])
    c = imaplib.IMAP4(s.host, s.port)
    c.login(s.nutzer, s.passwort)
    vorher = s.flags()

    def versuche(befehl, *args):
        with contextlib.suppress(imaplib.IMAP4.error):  # die Attrappe antwortet auf Schreibendes mit BAD
            befehl(*args)

    versuche(c.select, "INBOX")  # SELECT: öffnet zum Schreiben
    c.select("INBOX", readonly=True)  # EXAMINE ist erlaubt; imaplib braucht den Zustand, um die folgenden zu senden
    versuche(c.uid, "STORE", "1", "+FLAGS", "(\\Deleted)")
    versuche(c.uid, "COPY", "1", "Archiv")
    versuche(c.expunge)
    versuche(c.append, "INBOX", None, None, b"Subject: x\r\n\r\nx")
    versuche(c.create, "Neu")
    versuche(c.delete, "Archiv")
    versuche(c.uid, "FETCH", "1", "(BODY[])")
    c.logout()
    gemeldet = " | ".join(s.verboten)
    for erwartet in ("SELECT", "UID STORE", "UID COPY", "EXPUNGE", "APPEND", "CREATE", "DELETE", "UID FETCH BODY[]"):
        assert erwartet in gemeldet, (erwartet, s.verboten)
    assert len(s.verboten) == 8
    assert SCHREIBEND & set(s.verben(mit_faehigkeiten=True))  # und die unabhängige Liste hätte es ebenso gefunden
    assert s.flags() != vorher  # das FETCH ohne PEEK hat „gelesen“ gesetzt: ein Verstoß ist sichtbar
    assert "\\Seen" in s.flags()[("INBOX", 1)]


def test_gegenprobe_peek_laesst_das_kennzeichen_wie_es_ist(imap_server):
    s = imap_server([Ordner("INBOX", [n(1), n(2)])])
    c = imaplib.IMAP4(s.host, s.port)
    c.login(s.nutzer, s.passwort)
    vorher = s.flags()
    c.select("INBOX", readonly=True)
    c.uid("FETCH", "1", "(BODY.PEEK[])")
    c.uid("FETCH", "1:2", "(UID FLAGS BODY.PEEK[1])")
    c.logout()
    assert s.verboten == [] and s.flags() == vorher


def test_gegenprobe_das_plugin_wuerde_einen_verstoss_nicht_verstecken(lauf, monkeypatch):
    """Wird der Schutz im Code entfernt, schlägt die Prüfung auf der Leitung an: der Test ist nicht blind."""
    p, server = lauf
    monkeypatch.setattr(imap, "_items_pruefen", lambda items: None)
    original = Sitzung.holen

    def frech(self, uids, items):
        return original(self, uids, items.replace("BODY.PEEK[", "BODY["))

    monkeypatch.setattr(Sitzung, "holen", frech)
    p.call("eingang", {})
    assert server.verboten, "die Attrappe hat das FETCH ohne PEEK nicht bemerkt"


# ── 3. Im Code ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("befehl", ["STORE", "COPY", "MOVE", "EXPUNGE", "APPEND", "THREAD", "SORT", "store", "SEARCH FETCH"])
def test_die_sitzung_weist_schreibende_uid_befehle_ab(lauf, befehl):
    p, server = lauf
    konto = p._post.konten[0]
    with Sitzung(konto) as s:
        s.waehle("INBOX")
        if befehl in ("THREAD", "SORT", "SEARCH FETCH"):
            with pytest.raises(AssertionError):
                s._uid(befehl, "1")
        else:
            with pytest.raises(AssertionError):
                s._uid(befehl, "1", "+FLAGS", "(\\Deleted)")
    assert server.verboten == [] and not [v for v in server.verben() if v.startswith("UID") and v not in ("UID SEARCH", "UID FETCH")]


@pytest.mark.parametrize("items", [
    "BODY[]", "BODY[TEXT]", "BODY[1]", "BODY[HEADER]", "RFC822", "RFC822.TEXT", "RFC822.HEADER", "BODY.PEEK[1] BODY[1]",
    "BODY.PEEK[1]; STORE", "UID FLAGS (BODY[1])", "BODY.PEEK[1]) (BODY[1]", "BODY.PEEK[HEADER.FIELDS (FROM)] RFC822",
    "ALL", "FULL", "ENVELOPE", "X-GM-MSGID", "BINARY[1]", "BINARY.PEEK[1]",
])
def test_die_sitzung_weist_fetch_angaben_ab_die_das_kennzeichen_aendern_koennten(lauf, items):
    p, server = lauf
    with Sitzung(p._post.konten[0]) as s:
        s.waehle("INBOX")
        with pytest.raises(AssertionError):
            s.holen([1], items)
    assert server.zeilen("UID FETCH") == []


@pytest.mark.parametrize("items", [
    "UID", "UID FLAGS", "UID FLAGS INTERNALDATE BODYSTRUCTURE", "UID BODY.PEEK[1]", "UID BODY.PEEK[1.2]<0.100>",
    "UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]", "X-GM-LABELS", "RFC822.SIZE",
])
def test_erlaubte_fetch_angaben(lauf, items):
    p, server = lauf
    with Sitzung(p._post.konten[0]) as s:
        s.waehle("INBOX")
        s.holen([1], items)
    assert server.verboten == []


def test_die_sitzung_nimmt_keine_leere_oder_falsche_uid_liste(lauf):
    p, _ = lauf
    with Sitzung(p._post.konten[0]) as s:
        s.waehle("INBOX")
        for uids in ([], [0], [-1], ["1"], [1.5], ["1,2:*"], [None]):
            with pytest.raises(AssertionError):
                s.holen(uids, "UID FLAGS")


#: Was das Paket an der Verbindung von ``imaplib`` aufrufen darf; alles andere ist ein Fehler dieses Tests
AN_DER_VERBINDUNG = {"starttls", "shutdown", "logout", "login", "authenticate", "select", "response", "status", "uid", "list"}


def _ist_verbindung(knoten: ast.AST) -> bool:
    if isinstance(knoten, ast.Name):
        return knoten.id in {"c", "verbindung"}
    if isinstance(knoten, ast.Attribute):
        return knoten.attr == "_c"
    if isinstance(knoten, ast.Call):
        return isinstance(knoten.func, ast.Attribute) and knoten.func.attr == "_conn"
    return False


#: Die Sendeseite des Pakets (FYOS Post über Listmonk): sie spricht HTTP, nie IMAP oder SMTP. Die Zusage „nur lesen“ gilt dem
#: Plugin ``mail`` und damit allen übrigen Dateien; ``test_post_trennung`` unten hält beide Seiten auseinander.
POST_DATEIEN = {"post.py", "post_einrichten.py"}


def _quelltexte():
    ordner = Path(fyos_plugin_mail.__file__).parent
    return {p.name: ast.parse(p.read_text(encoding="utf-8")) for p in sorted(ordner.glob("*.py")) if p.name not in POST_DATEIEN}


def _importe(baum: ast.AST) -> set[str]:
    importiert: set[str] = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            importiert |= {a.name for a in knoten.names}
        elif isinstance(knoten, ast.ImportFrom) and knoten.level == 0:
            importiert.add(knoten.module or "")
        elif isinstance(knoten, ast.ImportFrom):  # relativ: `from . import x`, `from .post import y`
            importiert.add("." * knoten.level + (knoten.module or ""))
    return importiert


def test_post_trennung_die_sendeseite_spricht_nur_http_und_die_leseseite_kennt_sie_nicht():
    ordner = Path(fyos_plugin_mail.__file__).parent
    post_netz_verboten = {"imaplib", "smtplib", "poplib", "ftplib", "telnetlib", "nntplib", "subprocess", "socket", "requests", "httpx", "aiohttp"}
    for name in sorted(POST_DATEIEN):
        importiert = _importe(ast.parse((ordner / name).read_text(encoding="utf-8")))
        assert not importiert & post_netz_verboten, (name, importiert & post_netz_verboten)
        assert "http.client" in importiert or "urllib.request" in importiert, name  # sie sprechen wirklich HTTP
        assert "imap" not in {i.lstrip(".") for i in importiert}, name
    for name, baum in _quelltexte().items():
        zeigt_auf_post = {i for i in _importe(baum) if i.lstrip(".").split(".")[0] in {"post", "post_einrichten"}}
        assert not zeigt_auf_post, (name, zeigt_auf_post)  # das Plugin mail braucht die Sendeseite nicht


def test_das_paket_ruft_nur_diese_methoden_der_verbindung_auf():
    benutzt: dict[str, list[ast.Call]] = {}
    for baum in _quelltexte().values():
        for knoten in ast.walk(baum):
            if isinstance(knoten, ast.Call) and isinstance(knoten.func, ast.Attribute) and _ist_verbindung(knoten.func.value):
                benutzt.setdefault(knoten.func.attr, []).append(knoten)
    assert set(benutzt) <= AN_DER_VERBINDUNG, sorted(set(benutzt) - AN_DER_VERBINDUNG)
    assert {"select", "uid", "status", "list", "login", "logout"} <= set(benutzt)  # die Prüfung findet die Aufrufe überhaupt
    for aufruf in benutzt["select"]:  # nie SELECT: nur EXAMINE
        assert any(
            k.arg == "readonly" and isinstance(k.value, ast.Constant) and k.value.value is True for k in aufruf.keywords
        ), "select ohne readonly=True"


def test_nur_imap_py_spricht_mit_dem_netz_und_senden_kann_das_paket_nicht():
    netz = {"imaplib", "socket", "ssl", "smtplib", "poplib", "ftplib", "http", "http.client", "urllib", "urllib.request",
            "requests", "httpx", "aiohttp", "subprocess", "telnetlib", "nntplib"}
    for name, baum in _quelltexte().items():
        importiert: set[str] = set()
        for knoten in ast.walk(baum):
            if isinstance(knoten, ast.Import):
                importiert |= {a.name for a in knoten.names}
            elif isinstance(knoten, ast.ImportFrom):
                importiert.add(knoten.module or "")
        if name == "imap.py":
            assert importiert & netz == {"imaplib", "socket", "ssl"}, importiert & netz
        else:
            assert not importiert & netz, (name, importiert & netz)


def test_uid_wird_im_paket_nur_mit_search_und_fetch_aufgerufen():
    gefunden = set()
    for baum in _quelltexte().values():
        for knoten in ast.walk(baum):
            if (isinstance(knoten, ast.Call) and isinstance(knoten.func, ast.Attribute) and knoten.func.attr == "_uid"
                    and knoten.args and isinstance(knoten.args[0], ast.Constant)):
                gefunden.add(knoten.args[0].value)
    assert gefunden == {"SEARCH", "FETCH"}


def test_im_paket_kommt_kein_schreibender_imap_befehl_als_text_vor():
    """Auch als Zeichenkette steht kein schreibender Befehl im Quelltext (außer in Erklärungen, die ihn verneinen)."""
    ordner = Path(fyos_plugin_mail.__file__).parent
    for pfad in sorted(p for p in ordner.glob("*.py") if p.name not in POST_DATEIEN):
        for knoten in ast.walk(ast.parse(pfad.read_text(encoding="utf-8"))):
            if isinstance(knoten, ast.Constant) and isinstance(knoten.value, str) and len(knoten.value) < 40:
                for wort in ("STORE", "EXPUNGE", "APPEND", "SELECT", "COPY", "MOVE"):
                    assert not re.fullmatch(rf"(UID )?{wort}\b.*", knoten.value), (pfad.name, knoten.value)
