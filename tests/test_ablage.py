"""``ablage.py`` gegen die Attrappe: Konten zusammenführen, auflisten, lesen. Dazu das Mailformat des Kerns."""

from __future__ import annotations

import base64
import re
from datetime import timedelta

import pytest
from attrappe import (
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

from fyos_plugin_mail import ablage, struktur
from fyos_plugin_mail.ablage import Postfach, kennung_bauen, kennung_lesen
from fyos_plugin_mail.antwort import AntwortFehler
from fyos_plugin_mail.fehler import MailFehler, NichtEingerichtet
from fyos_plugin_mail.konten import Konto

pytestmark = pytest.mark.usefixtures("klartext")
KENNUNG = re.compile(r"m1\.[0-9a-f]{10}\.[0-9a-f]{10}\.\d{1,10}\.\d{1,10}")


def post(*konten, **kw):
    return Postfach(list(konten), heute=lambda: JETZT.date(), **kw)


@pytest.fixture
def eins(imap_server, konto_von):
    """``eins(ordner, name=…, konto_felder=…, **Server)`` gibt (Postfach mit einem Konto, Attrappe)."""

    def neu(ordner=None, name="Privat", konto_felder=None, **server):
        s = imap_server(ordner, **server)
        return post(konto_von(s, name, **(konto_felder or {}))), s

    return neu


# ── Kennungen ───────────────────────────────────────────────────────────────────────────────────────────
def test_kennung_hin_und_zurueck():
    k = Konto(name="Privat", host="mail.example.org")
    text = kennung_bauen(k, "INBOX", 1700000001, 42)
    assert KENNUNG.fullmatch(text) and len(text) < 60
    assert kennung_lesen(text) == (k.kennung, ablage.ordner_kennung("INBOX"), 1700000001, 42)


def test_kennung_passt_in_das_muster_des_kerns():
    pytest.importorskip("mcp_hub.postfach")
    from mcp_hub import postfach

    k = Konto(name="Privat", host="mail.example.org")
    assert postfach.ID_MUSTER.fullmatch(kennung_bauen(k, "Entwürfe/2026", 4294967295, 4294967295))


def test_kennung_verraet_nichts_ueber_konto_und_ordner():
    k = Konto(name="Privat", host="mail.example.org", nutzer="alex@example.org")
    text = kennung_bauen(k, "Rechnungen", 7, 9)
    assert "alex" not in text and "Rechnungen" not in text and "Privat" not in text and "example" not in text


def test_kennung_unterscheidet_ordner_gross_klein_aber_nicht_den_posteingang():
    k = Konto(name="Privat", host="mail.example.org")
    assert kennung_bauen(k, "inbox", 1, 1) == kennung_bauen(k, "INBOX", 1, 1)
    assert kennung_bauen(k, "Archiv", 1, 1) != kennung_bauen(k, "archiv", 1, 1)


@pytest.mark.parametrize("text", [
    "", "123", "m1", "m1.abc.def.1.1", "m2.0123456789.0123456789.1.1", "m1.0123456789.0123456789.1", "m1.0123456789.0123456789.1.0",
    "m1.0123456789.0123456789.x.1", "m1.0123456789.0123456789.1.1.", "../etc/passwd", "m1.0123456789.0123456789.1.1\n", None, 5,
    "m1.0123456789.0123456789.99999999999.1",
])
def test_fremde_kennungen_sind_ein_value_error(text):
    with pytest.raises(ValueError, match="Mail-Kennung"):
        kennung_lesen(text)


# ── Auflisten ───────────────────────────────────────────────────────────────────────────────────────────
def test_liste_im_mailformat_neueste_zuerst(eins):
    p, server = eins([Ordner("INBOX", [
        n(1, "Alt", vor=timedelta(days=3), gelesen=True),
        n(2, "Neu", von="Erika Beispiel <erika@example.org>", text="Guten Tag, hier ist der Text.", vor=timedelta(minutes=5)),
        n(3, "Mitte", vor=timedelta(hours=5), flags={"\\Flagged"}),
    ])])
    liste = p.abfragen("in:inbox", 10)
    assert [m["betreff"] for m in liste.mails] == ["Neu", "Mitte", "Alt"]
    neu = liste.mails[0]
    assert set(neu) == {"id", "konto", "von", "an", "betreff", "datum", "gelesen", "labels", "ausschnitt"}
    assert KENNUNG.fullmatch(neu["id"])
    assert neu["konto"] == "Privat" and neu["von"] == "Erika Beispiel <erika@example.org>" and neu["an"] == "Test <test@example.com>"
    assert neu["datum"] == "2026-10-10T11:55:00+00:00" and neu["gelesen"] is False
    assert neu["labels"] == ["Posteingang"] and neu["ausschnitt"] == "Guten Tag, hier ist der Text."
    assert liste.mails[1]["labels"] == ["Posteingang", "Markiert"] and liste.mails[2]["gelesen"] is True
    assert liste.ungelesen == 2 and liste.erfolge == 1 and liste.fehler == {} and liste.ignoriert == ()
    assert server.verboten == [] and server.intern_fehler == []


def test_liste_schneidet_auf_die_gewuenschte_anzahl(eins):
    # die UID wächst mit dem Eingang, wie in jedem Posteingang: die höchsten UIDs sind die neuesten Mails
    p, _ = eins([Ordner("INBOX", [n(i, f"Mail {i}", vor=timedelta(hours=9 - i)) for i in range(1, 9)])])
    assert [m["betreff"] for m in p.abfragen("in:inbox", 3).mails] == ["Mail 8", "Mail 7", "Mail 6"]
    assert len(p.abfragen("in:inbox", 500).mails) == 8  # mehr als vorhanden
    assert len(p.abfragen("in:inbox", 0).mails) == 1  # mindestens eine


def test_der_schnitt_gilt_den_hoechsten_uids_die_ausgabe_dem_datum(eins):
    # Mails, die später in einen Ordner kopiert wurden, haben hohe UIDs, aber alte Daten: sie werden mit ausgesucht
    # (nach UID), stehen dann aber nach Datum einsortiert.
    p, _ = eins([Ordner("INBOX", [
        n(1, "UID eins, gestern", vor=timedelta(days=1)),
        n(2, "UID zwei, vor einer Stunde", vor=timedelta(hours=1)),
        n(3, "UID drei, vor einem Monat", vor=timedelta(days=30)),
    ])])
    assert [m["betreff"] for m in p.abfragen("", 2).mails] == ["UID zwei, vor einer Stunde", "UID drei, vor einem Monat"]
    assert [m["betreff"] for m in p.abfragen("", 3).mails] == [
        "UID zwei, vor einer Stunde", "UID eins, gestern", "UID drei, vor einem Monat"]


def test_anzahl_ist_nach_oben_begrenzt(eins):
    p, server = eins([Ordner("INBOX", [n(i, f"Mail {i}") for i in range(1, 121)])])
    assert len(p.abfragen("in:inbox", 5000).mails) == ablage.ANZAHL_MAX == 100
    assert server.verboten == []


def test_ids_sind_ueber_aufrufe_stabil(eins):
    p, _ = eins([Ordner("INBOX", [n(1), n(2)])])
    assert [m["id"] for m in p.abfragen("in:inbox", 10).mails] == [m["id"] for m in p.abfragen("in:inbox", 10).mails]


def test_datum_aus_dem_kopf_mit_versatz_sonst_vom_server(eins):
    roh_ohne = roh_mail([("From", "A <a@example.org>"), ("Subject", "Ohne Datum")], b"Text\r\n")
    roh_kaputt = roh_mail([("From", "A <a@example.org>"), ("Subject", "Kaputtes Datum"), ("Date", "gestern Abend")], b"Text\r\n")
    roh_zone = roh_mail([("From", "A <a@example.org>"), ("Subject", "Mit Zone"), ("Date", "Sat, 10 Oct 2026 13:30:00 +0200")], b"Text\r\n")
    p, _ = eins([Ordner("INBOX", [
        Nachricht(1, roh_ohne, intern=JETZT - timedelta(hours=3)),
        Nachricht(2, roh_kaputt, intern=JETZT - timedelta(hours=2)),
        Nachricht(3, roh_zone, intern=JETZT - timedelta(hours=1)),
    ])])
    daten = {m["betreff"]: m["datum"] for m in p.abfragen("in:inbox", 10).mails}
    assert daten["Ohne Datum"] == "2026-10-10T09:00:00+00:00"
    assert daten["Kaputtes Datum"] == "2026-10-10T10:00:00+00:00"
    assert daten["Mit Zone"] == "2026-10-10T13:30:00+02:00"


def test_betreff_wird_entkodiert_und_ein_leerer_ersetzt(eins):
    leer = roh_mail([("From", "A <a@example.org>"), ("Date", "Sat, 10 Oct 2026 11:00:00 +0000")], b"Text\r\n")
    p, _ = eins([Ordner("INBOX", [
        n(1, "Grüße aus Köln – schön!", von="Jürgen Müller <jm@example.net>", vor=timedelta(hours=1)),
        Nachricht(2, leer, intern=JETZT - timedelta(hours=2)),
    ])])
    m1, m2 = p.abfragen("in:inbox", 10).mails
    assert m1["betreff"] == "Grüße aus Köln – schön!" and m1["von"] == "Jürgen Müller <jm@example.net>"
    assert m2["betreff"] == "(kein Betreff)" and m2["an"] == "" and m2["ausschnitt"] == "Text"


def test_steuerzeichen_und_zeilenumbrueche_im_kopf_stoeren_nicht(eins):
    roh = roh_mail([
        ("From", "Böse <boese@example.org>"), ("Subject", "Zeile eins\tZeile\x07zwei"), ("Date", "Sat, 10 Oct 2026 11:00:00 +0000"),
    ], b"Text\r\n")
    p, _ = eins([Ordner("INBOX", [Nachricht(1, roh)])])
    (m,) = p.abfragen("in:inbox", 10).mails
    assert "\n" not in m["betreff"] and "\x07" not in m["betreff"] and "\t" not in m["betreff"]


def test_labels_nach_ordner_und_kennzeichen(eins):
    p, _ = eins([
        Ordner("INBOX", []),
        Ordner("Archiv", [n(1, "Im Archiv", flags={"\\Draft", "\\Flagged"})]),
    ], konto_felder={"ordner": ("Archiv",)})
    (m,) = p.abfragen("newer_than:30d", 10).mails  # ohne in: gelten die Ordner des Kontos (eine leere Abfrage heißt in:inbox)
    assert m["labels"] == ["Archiv", "Markiert", "Entwurf"]


def test_in_inbox_meint_den_posteingang_auch_wenn_das_konto_andere_ordner_fuehrt(eins):
    p, server = eins([
        Ordner("INBOX", [n(1, "Im Posteingang")]),
        Ordner("Archiv", [n(1, "Im Archiv")]),
    ], konto_felder={"ordner": ("Archiv",)})
    assert [m["betreff"] for m in p.abfragen("in:inbox", 10).mails] == ["Im Posteingang"]
    assert [m["betreff"] for m in p.abfragen("", 10).mails] == ["Im Posteingang"]  # eine leere Abfrage heißt in:inbox
    assert [m["betreff"] for m in p.abfragen("newer_than:30d", 10).mails] == ["Im Archiv"]


def test_mehrere_ordner_eines_kontos_werden_nach_datum_zusammengefuehrt(eins):
    p, server = eins([
        Ordner("INBOX", [n(1, "Posteingang alt", vor=timedelta(days=2)), n(2, "Posteingang neu", vor=timedelta(hours=2))]),
        Ordner("Rechnungen", [n(1, "Rechnung", vor=timedelta(days=1))]),
    ], konto_felder={"ordner": ("INBOX", "Rechnungen")})
    liste = p.abfragen("newer_than:30d", 10)
    assert [m["betreff"] for m in liste.mails] == ["Posteingang neu", "Rechnung", "Posteingang alt"]
    assert [m["labels"] for m in liste.mails] == [["Posteingang"], ["Rechnungen"], ["Posteingang"]]
    assert len({m["id"] for m in liste.mails}) == 3  # gleiche UID in zwei Ordnern gibt zwei Kennungen
    assert server.verben().count("EXAMINE") == 2


def test_in_sent_nimmt_den_ordner_fuer_gesendete(eins):
    p, server = eins([
        Ordner("INBOX", [n(1, "Eingang")]),
        Ordner("Gesendet", [n(1, "Ausgang")], attribute=("\\Sent",)),
    ])
    assert [m["betreff"] for m in p.abfragen("in:sent", 10).mails] == ["Ausgang"]
    assert server.zeilen("EXAMINE") == ['EXAMINE "Gesendet"']


def test_in_inbox_und_in_sent_zusammen(eins):
    p, _ = eins([
        Ordner("INBOX", [n(1, "Eingang", vor=timedelta(hours=1))]),
        Ordner("Sent", [n(1, "Ausgang", vor=timedelta(hours=2))], attribute=("\\Sent",)),
    ])
    assert [m["betreff"] for m in p.abfragen("in:inbox in:sent", 10).mails] == ["Eingang", "Ausgang"]


def test_ordner_fuer_eine_rolle_fehlt_ist_ein_fehler_des_kontos(eins):
    p, _ = eins([Ordner("INBOX", [n(1)])])
    liste = p.abfragen("in:trash", 10)
    assert liste.mails == [] and liste.erfolge == 0
    assert "in:trash" in str(liste.fehler["Privat"]) and "vorhanden: INBOX" in str(liste.fehler["Privat"])
    assert liste.ungelesen is None  # nicht „0“: das Konto hat nicht geantwortet


def test_fehlender_ordner_im_konto_ist_ein_fehler_des_kontos(eins):
    p, _ = eins([Ordner("INBOX", [n(1)])], konto_felder={"ordner": ("Gibtsnicht",)})
    liste = p.abfragen("newer_than:30d", 10)
    assert "Gibtsnicht" in str(liste.fehler["Privat"]) and liste.fehler["Privat"].status == 404
    assert liste.mails == [] and liste.erfolge == 0


# ── Suchen ──────────────────────────────────────────────────────────────────────────────────────────────
def suchpost(eins):
    return eins([Ordner("INBOX", [
        n(1, "Rechnung Oktober", von="Erika Beispiel <erika@example.org>", text="Bitte zahlen.", vor=timedelta(hours=1)),
        n(2, "Grüße aus Köln", von="Jürgen Müller <jm@example.net>", text="Schöne Grüße vom Dom.", gelesen=True, vor=timedelta(hours=2)),
        n(3, "Einladung zur Feier", von="Erika Beispiel <erika@example.org>", text="Kommst du?", vor=timedelta(days=1)),
        n(4, "Alte Rechnung", von="Info <info@example.com>", text="Zahlung offen.", vor=timedelta(days=9)),
        n(5, "Gelöscht", von="Info <info@example.com>", flags={"\\Deleted"}),
    ])])


@pytest.mark.parametrize(("abfrage", "erwartet"), [
    ("from:erika", ["Rechnung Oktober", "Einladung zur Feier"]),
    ("from:ERIKA", ["Rechnung Oktober", "Einladung zur Feier"]),
    ("subject:rechnung", ["Rechnung Oktober", "Alte Rechnung"]),
    ("subject:\"alte rechnung\"", ["Alte Rechnung"]),
    ("to:test@example.com", ["Rechnung Oktober", "Grüße aus Köln", "Einladung zur Feier", "Alte Rechnung"]),
    ("is:unread", ["Rechnung Oktober", "Einladung zur Feier", "Alte Rechnung"]),
    ("is:read", ["Grüße aus Köln"]),
    ("-is:unread", ["Grüße aus Köln"]),
    ("newer_than:2d", ["Rechnung Oktober", "Grüße aus Köln", "Einladung zur Feier"]),
    ("newer_than:1w", ["Rechnung Oktober", "Grüße aus Köln", "Einladung zur Feier"]),
    ("older_than:7d", ["Alte Rechnung"]),
    ("after:2026/10/09", ["Rechnung Oktober", "Grüße aus Köln", "Einladung zur Feier"]),
    ("before:2026/10/02", ["Alte Rechnung"]),
    ("zahlen", ["Rechnung Oktober"]),  # freies Wort: im Text
    ("zahlung", ["Alte Rechnung"]),
    ("rechnung zahlen", ["Rechnung Oktober"]),  # alle Wörter müssen vorkommen
    ("-rechnung", ["Grüße aus Köln", "Einladung zur Feier"]),
    ("from:erika -subject:rechnung", ["Einladung zur Feier"]),
    ("from:erika is:unread newer_than:2d subject:feier", ["Einladung zur Feier"]),
    ("from:Müller", ["Grüße aus Köln"]),  # Umlaut: geht als Literal
    ("Grüße", ["Grüße aus Köln"]),
    ("from:Müller Grüße vom Dom", ["Grüße aus Köln"]),
    ("Köln Dom", ["Grüße aus Köln"]),
    ("from:Müller subject:Grüße", ["Grüße aus Köln"]),  # zwei Wörter mit Umlaut: zwei Suchen, geschnitten
    ("in:inbox", ["Rechnung Oktober", "Grüße aus Köln", "Einladung zur Feier", "Alte Rechnung"]),
    ("", ["Rechnung Oktober", "Grüße aus Köln", "Einladung zur Feier", "Alte Rechnung"]),
    ("nichts davon", []),
])
def test_abfragen(eins, abfrage, erwartet):
    p, server = suchpost(eins)
    liste = p.abfragen(abfrage, 10)
    assert [m["betreff"] for m in liste.mails] == erwartet, (server.zeilen("UID SEARCH"), liste.fehler, server.intern_fehler)
    assert liste.erfolge == 1 and liste.fehler == {}  # „nichts gefunden“ ist eine Antwort, kein Fehler
    assert server.verboten == [] and server.intern_fehler == []


def test_oder_und_gruppen_werden_nicht_ausgewertet_sondern_ueberlesen_und_genannt(eins):
    # Als „und“ gelesen ergäbe „from:erika OR from:info“ nichts. Lieber mehr Treffer als ein Filter, der etwas anderes meint.
    p, _ = suchpost(eins)
    liste = p.abfragen("(from:erika OR from:info) subject:rechnung", 10)
    assert [m["betreff"] for m in liste.mails] == ["Rechnung Oktober", "Alte Rechnung"]  # subject:rechnung gilt
    assert liste.ignoriert == ("from:erika", "OR", "from:info")
    liste = p.abfragen("is:unread from:erika OR subject:feier newer_than:2d", 10)
    assert liste.ignoriert == ("from:erika", "OR", "subject:feier")
    assert [m["betreff"] for m in liste.mails] == ["Rechnung Oktober", "Einladung zur Feier"]  # is:unread und newer_than:2d
    liste = p.abfragen("{from:erika from:info} -is:unread", 10)
    assert liste.ignoriert == ("from:erika", "from:info") and [m["betreff"] for m in liste.mails] == ["Grüße aus Köln"]


def test_ein_in_sich_geschlossenes_klammerwort_ist_ein_wort(eins):
    p, _ = suchpost(eins)
    liste = p.abfragen("(zahlen) AND subject:rechnung", 10)
    assert [m["betreff"] for m in liste.mails] == ["Rechnung Oktober"] and liste.ignoriert == ()


def test_ignoriert_wird_nur_gemeldet_wenn_ein_konto_ohne_gmail_antwortet(imap_server, konto_von):
    gmail = imap_server([Ordner("INBOX", [n(1, "Bei Gmail", labels=["\\Inbox"])])], gmail=True)
    anders = imap_server([Ordner("INBOX", [n(1, "Bei anderen")])])
    abfrage = "in:inbox has:attachment"
    assert post(konto_von(gmail, "G")).abfragen(abfrage, 10).ignoriert == ()  # Gmail wertet die ganze Abfrage aus
    assert post(konto_von(anders, "A")).abfragen(abfrage, 10).ignoriert == ("has:attachment",)
    assert post(konto_von(gmail, "G"), konto_von(anders, "A")).abfragen(abfrage, 10).ignoriert == ("has:attachment",)


def test_unbekannte_operatoren_werden_ueberlesen_und_genannt(eins):
    p, _ = suchpost(eins)
    liste = p.abfragen("in:inbox has:attachment -category:promotions label:wichtig from:erika", 10)
    assert [m["betreff"] for m in liste.mails] == ["Rechnung Oktober", "Einladung zur Feier"]
    assert liste.ignoriert == ("has:attachment", "-category:promotions", "label:wichtig")


@pytest.mark.parametrize("abfrage", ["wort " * 13, "x" * 201, "from:" + "y" * 300])
def test_unbrauchbare_abfragen_sind_ein_value_error(eins, abfrage):
    p, server = suchpost(eins)
    with pytest.raises(ValueError):
        p.abfragen(abfrage, 10)
    assert server.befehle == []  # nichts gesendet


def test_steuerzeichen_in_der_abfrage_gelangen_nicht_zum_server(eins):
    p, server = suchpost(eins)
    p.abfragen('from:erika\x00\r\nSTORE 1 +FLAGS (\\Deleted)', 10)
    für_suche = " ".join(server.zeilen("UID SEARCH"))
    assert "\x00" not in für_suche and "STORE" in für_suche  # als Suchwort in Anführungszeichen, nie als Befehl
    assert server.verboten == [] and "STORE" not in server.verben()


def test_ohne_konten_ist_nicht_eingerichtet(klartext):
    p = post()
    with pytest.raises(NichtEingerichtet) as e:
        p.abfragen("in:inbox", 10)
    assert e.value.status == 501 and "FYOS_MAIL_HOST" in str(e.value)
    with pytest.raises(NichtEingerichtet):
        p.lesen("m1.0123456789.0123456789.1.1")
    assert p.eingerichtet is False and p.konten_liste() == []


def test_unbekanntes_konto_ist_ein_value_error(eins):
    p, _ = suchpost(eins)
    with pytest.raises(ValueError, match="'Gibtsnicht' gibt es nicht; vorhanden: 'Privat'"):
        p.abfragen("in:inbox", 10, "Gibtsnicht")
    assert [m["konto"] for m in p.abfragen("in:inbox", 10, "PRIVAT").mails] == ["Privat"] * 4  # ohne Rücksicht auf Groß und Klein


# ── Ausschnitt ──────────────────────────────────────────────────────────────────────────────────────────
def ausschnitt_von(eins, roh, **kw):
    p, server = eins([Ordner("INBOX", [Nachricht(1, roh, intern=JETZT - timedelta(hours=1))])], **kw)
    (m,) = p.abfragen("", 10).mails
    return m["ausschnitt"]


def test_ausschnitt_klartext_wird_auf_eine_zeile_und_200_zeichen_gebracht(eins):
    text = "Erste Zeile\n\n\nZweite   Zeile mit\tTab. " + "Lang " * 100
    s = ausschnitt_von(eins, mail(text=text))
    assert s.startswith("Erste Zeile Zweite Zeile mit Tab. Lang") and "\n" not in s and len(s) <= 200 and s.endswith("…")


def test_ausschnitt_html_ohne_stil_und_skripte(eins):
    html = "<html><head><style>p{color:red}</style><title>T</title></head><body><script>var x=1;</script><p>Hallo <b>Welt</b>!</p><p>Zweiter Absatz</p></body></html>"
    assert ausschnitt_von(eins, mail(text=None, html=html)) == "Hallo Welt! Zweiter Absatz"


def test_ausschnitt_multipart_alternative_nimmt_den_klartext(eins):
    assert ausschnitt_von(eins, mail(text="Klartext-Fassung", html="<p>HTML-Fassung</p>")) == "Klartext-Fassung"


def test_ausschnitt_aus_html_wenn_der_klartext_leer_ist(eins):
    assert ausschnitt_von(eins, mail(text="", html="<p>Nur im HTML</p>")) == "Nur im HTML"


def test_ausschnitt_quoted_printable_in_latin1(eins):
    roh = roh_mail([
        ("From", "A <a@example.org>"), ("Subject", "Latin"), ("Date", "Sat, 10 Oct 2026 11:00:00 +0000"),
        ("Content-Type", 'text/plain; charset="iso-8859-1"'), ("Content-Transfer-Encoding", "quoted-printable"),
    ], b"Gr=FC=DFe aus K=F6ln, =20schon=\r\n weit.\r\n")
    assert ausschnitt_von(eins, roh) == "Grüße aus Köln, schon weit."


def test_ausschnitt_base64_in_utf8(eins):
    roh = roh_mail([
        ("From", "A <a@example.org>"), ("Subject", "B64"), ("Date", "Sat, 10 Oct 2026 11:00:00 +0000"),
        ("Content-Type", "text/plain; charset=utf-8"), ("Content-Transfer-Encoding", "base64"),
    ], base64.encodebytes("Grüße aus Köln ✓".encode("utf-8")).replace(b"\n", b"\r\n"))
    assert ausschnitt_von(eins, roh) == "Grüße aus Köln ✓"


def test_ausschnitt_unbekannter_zeichensatz_und_kaputtes_utf8_werfen_nicht(eins):
    roh = roh_mail([
        ("From", "A <a@example.org>"), ("Subject", "Kaputt"), ("Date", "Sat, 10 Oct 2026 11:00:00 +0000"),
        ("Content-Type", "text/plain; charset=gibt-es-nicht"), ("Content-Transfer-Encoding", "8bit"),
    ], b"Gr\xfc\xdfe ohne Zeichensatz\r\n")
    assert ausschnitt_von(eins, roh) == "Grüße ohne Zeichensatz"  # Windows-1252 als letzte Zuflucht


def test_ausschnitt_wird_nur_vom_anfang_geholt(eins, monkeypatch):
    monkeypatch.setattr(ablage, "AUSSCHNITT_BYTES", 64)
    s = ausschnitt_von(eins, mail(text="a" * 5000 + " Ende", cte="7bit"))
    assert s == "a" * 64  # abgeschnitten ohne Zeichen zu verlieren, die nicht da waren


def test_ausschnitt_mitten_in_einem_utf8_zeichen_abgeschnitten(eins, monkeypatch):
    monkeypatch.setattr(ablage, "AUSSCHNITT_BYTES", 11)
    s = ausschnitt_von(eins, mail(text="äöüäöüäöü", cte="8bit", charset="utf-8"))
    assert s == "äöüäö"  # das angeschnittene Zeichen fällt weg, es wird kein Ersatzzeichen daraus


def test_ausschnitt_angeschnittenes_html_tag_wird_nicht_zu_text(eins, monkeypatch):
    monkeypatch.setattr(ablage, "AUSSCHNITT_BYTES_HTML", 40)
    html = "<p>Hallo Welt</p><p>Mehr</p><a href=\"https://example.org/lang/lang\">Link</a>"
    s = ausschnitt_von(eins, mail(text=None, html=html, cte="7bit"))
    assert "href" not in s and "<" not in s and s.startswith("Hallo Welt")


def test_ausschnitte_holt_ein_befehl_je_abschnittsnummer(eins):
    p, server = eins([Ordner("INBOX", [n(i, f"Mail {i}", text=f"Text {i}", vor=timedelta(hours=10 - i)) for i in range(1, 7)])])
    assert [m["ausschnitt"] for m in p.abfragen("", 10).mails] == [f"Text {i}" for i in range(6, 0, -1)]
    teilabrufe = [z for z in server.zeilen("UID FETCH") if "<0." in z]
    assert len(teilabrufe) == 1 and teilabrufe[0].count(",") == 5  # sechs UIDs in einem Befehl


def test_ausschnitt_fehler_des_servers_laesst_die_liste_gelten(eins):
    p, server = eins([Ordner("INBOX", [n(1, text="Text")])])
    server.teilabruf_fehler = "NO Teilabruf geht nicht"
    (m,) = p.abfragen("", 10).mails
    assert m["betreff"] == "Hallo" and m["ausschnitt"] == ""


def test_kaputter_aufbau_gibt_eine_mail_ohne_ausschnitt(eins, monkeypatch):
    def kaputt(_):
        raise AntwortFehler("kaputt")

    monkeypatch.setattr(struktur, "baum", kaputt)
    p, _ = eins([Ordner("INBOX", [n(1)])])
    (m,) = p.abfragen("", 10).mails
    assert m["betreff"] == "Hallo" and m["ausschnitt"] == ""


# ── Gmail ───────────────────────────────────────────────────────────────────────────────────────────────
def gmail_post(eins):
    return eins([Ordner("INBOX", [
        n(1, "Persönlich", von="Erika <erika@example.org>", labels=["\\Inbox", "\\Important"], vor=timedelta(hours=1)),
        n(2, "Angebot", von="Shop <shop@example.com>", labels=["\\Inbox", "Category Promotions"], vor=timedelta(hours=2)),
        n(3, "Rechnung", von="Firma <firma@example.net>", labels=["\\Inbox", "Meine Rechnungen"], gelesen=True, vor=timedelta(days=1)),
        n(4, "Zu alt", von="Erika <erika@example.org>", labels=["\\Inbox"], vor=timedelta(days=5)),
    ])], gmail=True)


def test_gmail_sucht_mit_x_gm_raw_und_unveraendertem_text(eins):
    p, server = gmail_post(eins)
    abfrage = "in:inbox newer_than:2d -category:promotions -category:social -category:forums"
    liste = p.abfragen(abfrage, 10)
    assert [m["betreff"] for m in liste.mails] == ["Persönlich", "Rechnung"]  # das Werbe-Label schließt aus, das Alter auch
    assert server.zeilen("UID SEARCH") == [f'UID SEARCH UID 1:* X-GM-RAW "{abfrage}"']  # unverändert, nichts davon übersetzt
    assert liste.ignoriert == ()  # Gmail wertet auch die Kategorien aus: nichts wurde überlesen


def test_gmail_labels_auf_deutsch(eins):
    p, _ = gmail_post(eins)
    labels = {m["betreff"]: m["labels"] for m in p.abfragen("in:inbox", 10).mails}
    assert labels["Persönlich"] == ["Posteingang", "Wichtig"]
    assert labels["Angebot"] == ["Posteingang", "Werbung"]
    assert labels["Rechnung"] == ["Posteingang", "Meine Rechnungen"]  # eigene Labels bleiben, wie sie heißen


def test_gmail_holt_labels_nur_bei_gmail(eins):
    p, server = gmail_post(eins)
    p.abfragen("in:inbox", 10)
    assert any("X-GM-LABELS" in z for z in server.zeilen("UID FETCH"))
    p2, server2 = eins([Ordner("INBOX", [n(1)])])
    p2.abfragen("in:inbox", 10)
    assert not any("X-GM-LABELS" in z for z in server2.zeilen("UID FETCH"))


def test_gmail_suche_mit_umlaut_geht_als_literal(eins):
    p, server = gmail_post(eins)
    assert [m["betreff"] for m in p.abfragen("persönlich", 10).mails] == ["Persönlich"]
    assert server.zeilen("UID SEARCH") == ["UID SEARCH CHARSET UTF-8 UID 1:* X-GM-RAW <<'persönlich'>>"]


def test_gmail_ohne_undeleted_und_ohne_uebersetzung(eins):
    p, server = gmail_post(eins)
    p.abfragen("from:erika is:unread", 10)
    assert "UNDELETED" not in " ".join(server.zeilen("UID SEARCH"))


def test_ein_konto_ohne_gmail_bekommt_nie_x_gm_raw(eins):
    p, server = eins([Ordner("INBOX", [n(1)])])
    p.abfragen("in:inbox newer_than:2d", 10)
    assert "X-GM-RAW" not in " ".join(server.zeilen("UID SEARCH"))


# ── Mehrere Konten ──────────────────────────────────────────────────────────────────────────────────────
def zwei_konten(imap_server, konto_von):
    s1 = imap_server([Ordner("INBOX", [n(1, "Privat neu", vor=timedelta(hours=1)), n(2, "Privat alt", vor=timedelta(days=2), gelesen=True)])])
    s2 = imap_server([Ordner("INBOX", [n(1, "Arbeit mitte", vor=timedelta(hours=5)), n(2, "Arbeit ganz neu", vor=timedelta(minutes=10))])],
                     nutzer="beruf", passwort="anderes")
    return post(konto_von(s1, "Privat"), konto_von(s2, "Arbeit")), s1, s2


def test_zwei_konten_werden_nach_datum_zusammengefuehrt(imap_server, konto_von):
    p, s1, s2 = zwei_konten(imap_server, konto_von)
    liste = p.abfragen("in:inbox", 10)
    assert [(m["konto"], m["betreff"]) for m in liste.mails] == [
        ("Arbeit", "Arbeit ganz neu"), ("Privat", "Privat neu"), ("Arbeit", "Arbeit mitte"), ("Privat", "Privat alt")]
    assert liste.erfolge == 2 and liste.fehler == {} and liste.ungelesen == 3  # 1 + 2 ungelesen
    assert len({m["id"] for m in liste.mails}) == 4


def test_konto_filter_fragt_nur_dieses_konto(imap_server, konto_von):
    p, s1, s2 = zwei_konten(imap_server, konto_von)
    liste = p.abfragen("in:inbox", 10, "Arbeit")
    assert {m["konto"] for m in liste.mails} == {"Arbeit"} and liste.ungelesen == 2
    assert s1.befehle == []  # das andere Konto wurde nicht einmal angesprochen


def test_ein_konto_antwortet_nicht_dann_steht_es_in_den_fehlern_und_die_zahl_fehlt(imap_server, konto_von):
    p, s1, s2 = zwei_konten(imap_server, konto_von)
    s2.stop()
    liste = p.abfragen("in:inbox", 10)
    assert [m["konto"] for m in liste.mails] == ["Privat", "Privat"] and liste.erfolge == 1
    assert list(liste.fehler) == ["Arbeit"] and "abgelehnt" in str(liste.fehler["Arbeit"])
    assert liste.ungelesen is None  # 1 ungelesen wäre falsch: das zweite Konto fehlt


def test_ein_konto_mit_falschem_passwort(imap_server, konto_von):
    p, s1, s2 = zwei_konten(imap_server, konto_von)
    p2 = post(konto_von(s1, "Privat"), konto_von(s2, "Arbeit", passwort="falsch"))
    liste = p2.abfragen("in:inbox", 10)
    assert liste.fehler["Arbeit"].status == 401 and "App-Passwort" in str(liste.fehler["Arbeit"]) and "falsch" not in str(liste.fehler["Arbeit"])


def test_unbrauchbarer_eintrag_ist_ein_fehler_dieses_kontos(imap_server, konto_von):
    s = imap_server([Ordner("INBOX", [n(1)])])
    p = post(konto_von(s, "Privat"), Konto(name="Kaputt", fehler="es fehlt das App-Passwort (FYOS_MAIL_PASSWORT)"))
    liste = p.abfragen("in:inbox", 10)
    assert len(liste.mails) == 1 and "Konfiguration fehlerhaft" in str(liste.fehler["Kaputt"]) and "App-Passwort" in str(liste.fehler["Kaputt"])
    assert liste.ungelesen is None
    assert p.abfragen("in:inbox", 10, "Privat").fehler == {}


def test_ausnahme_in_einem_konto_wird_dessen_fehler(imap_server, konto_von, monkeypatch):
    p, s1, s2 = zwei_konten(imap_server, konto_von)
    echt = Postfach._konto_abfrage

    def kaputt(self, konto, *args):
        if konto.name == "Arbeit":
            raise KeyError("Bug")
        return echt(self, konto, *args)

    monkeypatch.setattr(Postfach, "_konto_abfrage", kaputt)
    liste = p.abfragen("in:inbox", 10)
    assert liste.erfolge == 1 and str(liste.fehler["Arbeit"]) == "Unerwarteter Fehler (KeyError)"  # nur der Typ, nie ein fremder Text


def test_konten_laufen_zugleich(imap_server, konto_von):
    ss = [imap_server([Ordner("INBOX", [n(1, f"Konto {i}")])]) for i in range(4)]
    for s in ss:
        s.verzoegerung_s = 0.05
    p = post(*[konto_von(s, f"K{i}") for i, s in enumerate(ss)])
    import time

    start = time.monotonic()
    liste = p.abfragen("in:inbox", 10)
    dauer = time.monotonic() - start
    assert len(liste.mails) == 4
    # nacheinander wären es vier Konten mal rund fünfzehn Antworten à 50 ms
    assert dauer < 4 * 15 * 0.05 * 0.6


# ── Lesen ───────────────────────────────────────────────────────────────────────────────────────────────
def lies(eins, roh, **kw):
    p, server = eins([Ordner("INBOX", [Nachricht(1, roh, intern=JETZT - timedelta(hours=1))])], **kw)
    (m,) = p.abfragen("", 10).mails
    return p.lesen(m["id"]), server


def test_lesen_einfacher_text(eins):
    m, server = lies(eins, mail(text="Hallo,\n\nhier der ganze Text.\n\nGruß"))
    assert m["text"] == "Hallo,\n\nhier der ganze Text.\n\nGruß" and m["gekuerzt"] is False and m["anhaenge"] == []
    assert m["betreff"] == "Hallo" and m["von"] == "Alex Muster <alex@example.org>" and m["gelesen"] is False
    assert m["ausschnitt"] == "Hallo, hier der ganze Text. Gruß"
    assert KENNUNG.fullmatch(m["id"]) and m["konto"] == "Privat" and m["labels"] == ["Posteingang"]


def test_lesen_ist_dieselbe_mail_wie_in_der_liste(eins):
    p, _ = eins([Ordner("INBOX", [n(1, "Eins"), n(2, "Zwei")])])
    kopf = p.abfragen("", 10).mails[1]
    voll = p.lesen(kopf["id"])
    assert {k: voll[k] for k in kopf} == kopf and set(voll) - set(kopf) == {"text", "gekuerzt", "anhaenge"}


def test_lesen_aendert_den_gelesen_status_nicht(eins):
    p, server = eins([Ordner("INBOX", [n(1, "Ungelesen"), n(2, "Gelesen", gelesen=True)])])
    vorher = server.flags()
    for m in p.abfragen("", 10).mails:
        p.lesen(m["id"])
    assert server.flags() == vorher and server.verboten == []
    assert [m["gelesen"] for m in p.abfragen("", 10).mails] == [True, False]  # zur selben Sekunde: die höhere UID zuerst


def test_lesen_html_wird_zu_text(eins):
    html = "<html><body><h1>Titel</h1><p>Hallo <b>Welt</b>,<br>zweite Zeile &amp; mehr.</p><ul><li>Eins</li><li>Zwei</li></ul></body></html>"
    m, _ = lies(eins, mail(text=None, html=html))
    assert m["text"] == "Titel\n\nHallo Welt,\nzweite Zeile & mehr.\n\nEins\nZwei"


def test_lesen_alternative_nimmt_den_klartext_und_nicht_beides(eins):
    m, _ = lies(eins, mail(text="So steht es im Klartext.", html="<p>So steht es im <b>HTML</b>.</p>"))
    assert m["text"] == "So steht es im Klartext."


def test_lesen_leerer_klartext_nimmt_das_html(eins):
    m, _ = lies(eins, mail(text="", html="<p>Der Inhalt steht im HTML.</p>"))
    assert m["text"] == "Der Inhalt steht im HTML."


def test_lesen_mehrere_textteile_werden_aneinandergehaengt(eins):
    m, _ = lies(eins, mail_mit_zwei_textteilen())
    assert m["text"] == "Erster Teil\n\nZweiter Teil" and m["anhaenge"] == []


def test_lesen_latin1_quoted_printable(eins):
    roh = roh_mail([
        ("From", "A <a@example.org>"), ("Subject", "Latin"), ("Date", "Sat, 10 Oct 2026 11:00:00 +0000"),
        ("Content-Type", 'text/plain; charset="iso-8859-1"'), ("Content-Transfer-Encoding", "quoted-printable"),
    ], b"Gr=FC=DFe aus K=F6ln\r\n")
    assert lies(eins, roh)[0]["text"] == "Grüße aus Köln"


def test_lesen_base64_mit_umbruechen(eins):
    text = "Zeile eins mit Umlauten äöüß.\nZeile zwei. " * 20
    roh = roh_mail([
        ("From", "A <a@example.org>"), ("Subject", "B64"), ("Date", "Sat, 10 Oct 2026 11:00:00 +0000"),
        ("Content-Type", "text/plain; charset=utf-8"), ("Content-Transfer-Encoding", "base64"),
    ], base64.encodebytes(text.encode("utf-8")).replace(b"\n", b"\r\n"))
    assert lies(eins, roh)[0]["text"] == text.strip()


def test_lesen_anhaenge_werden_aufgelistet_aber_nie_geladen(eins):
    pdf = b"%PDF-1.4 Attrappe " * 400
    roh = mail(text="Siehe Anhang", anhaenge=(
        ("Übersicht.pdf", "application/pdf", pdf),
        ("daten.csv", "text/csv", b"a;b\r\n1;2\r\n"),
        ("", "application/octet-stream", b"\x00\x01\x02" * 100),
    ))
    m, server = lies(eins, roh)
    assert m["text"] == "Siehe Anhang"
    nach_name = {a["name"]: a for a in m["anhaenge"]}
    assert set(nach_name) == {"Übersicht.pdf", "daten.csv", "(ohne Namen)"}
    assert nach_name["Übersicht.pdf"]["typ"] == "application/pdf" and abs(nach_name["Übersicht.pdf"]["groesse"] - len(pdf)) <= 3
    assert nach_name["daten.csv"]["typ"] == "text/csv" and nach_name["(ohne Namen)"]["groesse"] > 0
    abgerufen = [z for z in server.zeilen("UID FETCH") if "BODY.PEEK[" in z and "HEADER.FIELDS" not in z]
    assert all("BODY.PEEK[1]" in z for z in abgerufen)  # nur der Textteil, keiner der Anhänge (2, 3, 4)


def test_lesen_eingebettetes_bild_und_signatur_sind_keine_anhaenge(eins):
    assert lies(eins, mail_mit_bild())[0]["anhaenge"] == []
    m, _ = lies(eins, mail_signiert())
    assert m["anhaenge"] == [] and m["text"] == "Signierter Text"


def test_lesen_weitergeleitete_mail_ist_ein_anhang(eins):
    m, _ = lies(eins, mail_mit_weiterleitung())
    assert m["text"] == "Siehe unten"
    assert [(a["typ"], a["name"]) for a in m["anhaenge"]] == [("message/rfc822", "(ohne Namen)")]


def test_lesen_nur_anhang_ohne_text(eins):
    roh = mail(text=None, anhaenge=(("scan.pdf", "application/pdf", b"%PDF" * 50),))
    m, _ = lies(eins, roh)
    assert m["text"] == "" and m["gekuerzt"] is False and [a["name"] for a in m["anhaenge"]] == ["scan.pdf"]


def test_lesen_langer_text_wird_gekuerzt_und_so_gemeldet(eins):
    m, _ = lies(eins, mail(text="Wort " * 10000, cte="7bit"))  # 50 000 Zeichen
    assert len(m["text"]) == ablage.TEXT_MAX == 30000 and m["gekuerzt"] is True


def test_lesen_text_mit_eigener_grenze(eins):
    p, _ = eins([Ordner("INBOX", [n(1, text="a" * 500, cte="7bit")])])
    (kopf,) = p.abfragen("", 10).mails
    kurz = p.lesen(kopf["id"], 100)
    assert len(kurz["text"]) == 100 and kurz["gekuerzt"] is True
    assert p.lesen(kopf["id"], 5000)["gekuerzt"] is False


def test_lesen_holt_nicht_mehr_als_die_byte_grenze(eins, monkeypatch):
    monkeypatch.setattr(ablage, "LESE_BYTES_MAX", 2000)
    m, server = lies(eins, mail(text="Zeichen " * 1000, cte="7bit"))
    assert m["gekuerzt"] is True and 1500 < len(m["text"]) <= 2000
    assert any("<0.2000>" in z for z in server.zeilen("UID FETCH"))


def test_lesen_text_im_zweiten_ordner(eins):
    p, server = eins([Ordner("INBOX", []), Ordner("Archiv", [n(1, "Im Archiv", text="Archivtext")])],
                     konto_felder={"ordner": ("INBOX", "Archiv")})
    (kopf,) = p.abfragen("newer_than:30d", 10).mails
    assert p.lesen(kopf["id"])["text"] == "Archivtext" and server.zeilen("EXAMINE")[-1] == 'EXAMINE "Archiv"'


def test_lesen_im_ordner_mit_umlaut(eins):
    # der Server schreibt den Namen in modifiziertem UTF-7, das Konto und die Labels in Klartext
    p, server = eins([Ordner("INBOX", []), Ordner("Gesch&AOQ-ft", [n(1, "Vertrag", text="Halber Satz")])],
                     konto_felder={"ordner": ("Geschäft",)})
    (kopf,) = p.abfragen("newer_than:30d", 10).mails
    assert kopf["labels"] == ["Geschäft"] and p.lesen(kopf["id"])["text"] == "Halber Satz"
    assert server.zeilen("EXAMINE")[-1] == 'EXAMINE "Gesch&AOQ-ft"'


@pytest.mark.parametrize("aenderung", ["neue_gueltigkeit", "uid_weg", "ordner_weg"])
def test_lesen_was_es_nicht_mehr_gibt_ist_404_und_nie_eine_fremde_mail(eins, aenderung):
    p, server = eins([Ordner("INBOX", [n(1, "Meine Mail", text="Mein Text"), n(2, "Andere", text="Fremder Text")], gueltig=100)])
    kopf = next(m for m in p.abfragen("", 10).mails if m["betreff"] == "Meine Mail")
    if aenderung == "neue_gueltigkeit":
        server.ordner["INBOX"].gueltig = 101  # der Ordner wurde neu aufgebaut: dieselbe UID kann eine andere Mail sein
    elif aenderung == "uid_weg":
        server.ordner["INBOX"].nachrichten = server.ordner["INBOX"].nachrichten[1:]
    else:
        server.ordner.clear()
        server.ordner["Anderer"] = Ordner("Anderer", [])
    with pytest.raises(MailFehler) as e:
        p.lesen(kopf["id"])
    assert e.value.status == 404
    assert "gibt es" in str(e.value) and ("nicht mehr" in str(e.value) or "nicht oder" in str(e.value))  # die Mail, der Ordner


def test_lesen_konto_nicht_mehr_eingerichtet(eins, konto_von, imap_server):
    p, _ = eins([Ordner("INBOX", [n(1)])])
    (kopf,) = p.abfragen("", 10).mails
    anderes = post(konto_von(imap_server(), "Anders"))
    with pytest.raises(MailFehler, match="nicht mehr eingerichtet") as e:
        anderes.lesen(kopf["id"])
    assert e.value.status == 404


def test_lesen_fremde_kennung(eins):
    p, server = eins([Ordner("INBOX", [n(1)])])
    with pytest.raises(ValueError):
        p.lesen("19283746")
    assert server.befehle == []


def test_lesen_konto_mit_fehlerhafter_konfiguration(eins):
    p = post(Konto(name="Kaputt", fehler="host fehlt"))
    k = p.konten[0]
    with pytest.raises(MailFehler, match="Konfiguration des Kontos 'Kaputt' fehlerhaft"):
        p.lesen(kennung_bauen(k, "INBOX", 1, 1))


def test_lesen_unlesbarer_aufbau(eins, monkeypatch):
    p, _ = eins([Ordner("INBOX", [n(1)])])
    (kopf,) = p.abfragen("", 10).mails

    def kaputt(_):
        raise AntwortFehler("kaputt")

    monkeypatch.setattr(struktur, "baum", kaputt)
    with pytest.raises(MailFehler, match="Aufbau der Mail nicht lesbar") as e:
        p.lesen(kopf["id"])
    assert e.value.status == 502


def test_lesen_unerwarteter_fehler_ist_nur_ein_typ(eins, monkeypatch):
    p, _ = eins([Ordner("INBOX", [n(1)])])
    (kopf,) = p.abfragen("", 10).mails
    monkeypatch.setattr(ablage, "_volltext", lambda *a: 1 / 0)
    with pytest.raises(MailFehler) as e:
        p.lesen(kopf["id"])
    assert str(e.value) == "Unerwarteter Fehler (ZeroDivisionError)"


def test_lesen_gmail_labels(eins):
    p, _ = gmail_post(eins)
    kopf = next(m for m in p.abfragen("in:inbox", 10).mails if m["betreff"] == "Persönlich")
    assert p.lesen(kopf["id"])["labels"] == ["Posteingang", "Wichtig"]


def test_lesen_sendet_nur_lesende_befehle(eins):
    p, server = eins([Ordner("INBOX", [n(1, text="Text"), n(2, "Mit Anhang", text=None, html="<p>Hallo</p>")])])
    for m in p.abfragen("", 10).mails:
        p.lesen(m["id"])
    assert set(server.verben()) <= {"LOGIN", "LIST", "STATUS", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}
    assert server.verboten == []


# ── Prüfen ──────────────────────────────────────────────────────────────────────────────────────────────
def test_pruefen_nennt_ordner_und_ungelesene_ohne_eine_mail_zu_lesen(imap_server, konto_von):
    s1 = imap_server([Ordner("INBOX", [n(1), n(2, gelesen=True), n(3)]), Ordner("Sent", [], attribute=("\\Sent",))])
    s2 = imap_server([Ordner("INBOX", [])], gmail=True)
    s3 = imap_server()
    aus = konto_von(s3, "Aus")
    s3.stop()  # unter dieser Adresse lauscht niemand mehr
    p = post(konto_von(s1, "Privat"), konto_von(s2, "Mailkonto"), aus, Konto(name="Kaputt", fehler="host fehlt"))
    zeilen = {z["name"]: z for z in p.pruefen()}
    assert zeilen["Privat"] == {"name": "Privat", "status": "ok", "ordner_vorhanden": ["INBOX", "Sent"], "gmail": False, "ungelesen": 2}
    assert zeilen["Mailkonto"]["gmail"] is True and zeilen["Mailkonto"]["ungelesen"] == 0
    assert zeilen["Aus"]["status"] == "fehler" and "abgelehnt" in zeilen["Aus"]["fehler"]
    assert zeilen["Kaputt"] == {"name": "Kaputt", "status": "fehler", "fehler": "Konfiguration fehlerhaft: host fehlt"}
    assert [z["name"] for z in p.pruefen()] == ["Privat", "Mailkonto", "Aus", "Kaputt"]  # in der Reihenfolge der Konten
    assert "UID FETCH" not in s1.verben() and "EXAMINE" not in s1.verben()


def test_pruefen_eines_kontos_und_unbekanntes_konto(imap_server, konto_von):
    s = imap_server()
    p = post(konto_von(s, "Privat"))
    assert [z["name"] for z in p.pruefen("privat")] == ["Privat"]
    with pytest.raises(ValueError):
        p.pruefen("anders")
