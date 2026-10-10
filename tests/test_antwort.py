"""Antworten eines IMAP-Servers lesen: Literale, Klammergruppen, FETCH, LIST und STATUS. Reine Lesefunktionen, ohne Netz."""

from __future__ import annotations

import pytest

from fyos_plugin_mail.antwort import (
    TIEFE_MAX,
    AntwortFehler,
    abschnitt,
    fetch_antworten,
    ordner_liste,
    status_zahlen,
    werte,
    zerlegen,
)

# ── zerlegen ────────────────────────────────────────────────────────────────────────────────────────────


def test_zerlegen_eine_einfache_antwort():
    assert zerlegen([b"1 (UID 5 FLAGS ())"]) == [(b"1 (UID 5 FLAGS ())", [])]


def test_zerlegen_mit_literal_und_rest():
    daten = [(b"1 (UID 5 BODY[1] {5}", b"Hallo"), b" FLAGS (\\Seen))"]
    (puffer, literale), = zerlegen(daten)
    assert literale == [b"Hallo"] and puffer == b"1 (UID 5 BODY[1] \x00L0\x00 FLAGS (\\Seen))"
    assert werte(puffer, literale) == ["1", ["UID", "5", "BODY[1]", b"Hallo", "FLAGS", ["\\Seen"]]]


def test_zerlegen_zwei_literale_in_einer_antwort_und_mehrere_antworten():
    daten = [
        (b"1 (UID 5 BODY[1] {3}", b"abc"), (b" BODY[2] {2}", b"de"), b")",
        (b"2 (UID 6 BODY[1] {1}", b"x"), b")",
    ]
    antworten = zerlegen(daten)
    assert [literale for _, literale in antworten] == [[b"abc", b"de"], [b"x"]]


def test_zerlegen_nicht_synchronisierendes_literal_und_leere_eintraege():
    (puffer, literale), = zerlegen([None, (b"1 (UID 5 BODY[1] {3+}", b"abc"), b")"])
    assert literale == [b"abc"] and b"{3" not in puffer


def test_zerlegen_ein_literal_ohne_schluss_ist_ein_fehler():
    with pytest.raises(AntwortFehler, match="mitten in einem Literal"):
        zerlegen([(b"1 (UID 5 BODY[1] {5}", b"Hallo")])


def test_zerlegen_nichts():
    assert zerlegen([]) == [] and zerlegen([None]) == []


# ── werte ───────────────────────────────────────────────────────────────────────────────────────────────
def test_werte_atome_listen_und_nil():
    assert werte(b"1 (UID 5 FLAGS (\\Seen \\Flagged) X NIL nil)") == ["1", ["UID", "5", "FLAGS", ["\\Seen", "\\Flagged"], "X", None, None]]
    assert werte(b"") == [] and werte(b"   ") == []
    assert werte(b"()") == [[]]


def test_werte_zeichenketten():
    assert werte(b'"a b" "c\\"d" "e\\\\f" ""') == [b"a b", b'c"d', b"e\\f", b""]
    assert werte(b'"Zeile\r\nzwei"') == [b"Zeile\r\nzwei"]


def test_werte_abschnittsangabe_gehoert_zum_atom():
    assert werte(b"BODY[HEADER.FIELDS (FROM TO)] x BODY[1.2]<0> y") == ["BODY[HEADER.FIELDS (FROM TO)]", "x", "BODY[1.2]<0>", "y"]


def test_werte_literal_an_beliebiger_stelle():
    assert werte(b"a \x00L1\x00 b \x00L0\x00", [b"null", b"eins"]) == ["a", b"eins", "b", b"null"]


@pytest.mark.parametrize("roh", [b"(", b"(a (b)", b")", b"a )", b'"abc', b"BODY[1", b"\x00", b"\x00L7\x00", b"a \x00L0\x00"])
def test_werte_kaputte_antworten(roh):
    with pytest.raises(AntwortFehler):
        werte(roh)


def test_werte_tiefe_ist_begrenzt():
    ok = b"(" * TIEFE_MAX + b")" * TIEFE_MAX
    assert werte(ok)
    zu_tief = b"(" * (TIEFE_MAX + 1) + b")" * (TIEFE_MAX + 1)
    with pytest.raises(AntwortFehler, match="zu tief"):
        werte(zu_tief)
    with pytest.raises(AntwortFehler):  # und eine halbe Million Klammern erschöpfen weder Stapel noch Zeit
        werte(b"(" * 500_000)


def test_werte_ist_linear_bei_langen_eingaben():
    import time

    start = time.monotonic()
    werte(b"(" + b"atom " * 400_000 + b")")
    assert time.monotonic() - start < 5


# ── fetch_antworten ─────────────────────────────────────────────────────────────────────────────────────
KOPF = b"From: a@example.org\r\nSubject: x\r\n\r\n"
VOLL = [
    (b'1 (UID 7 FLAGS (\\Seen \\Answered) INTERNALDATE "10-Oct-2026 12:00:00 +0200" RFC822.SIZE 1234 '
     b'BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 12 1 NIL NIL NIL NIL) '
     b'X-GM-LABELS (\\Inbox "Meine Rechnungen") BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)] {%d}' % len(KOPF), KOPF),
    b")",
]


def test_fetch_alles_zusammen():
    (a,) = fetch_antworten(VOLL)
    assert a["UID"] == 7 and a["FLAGS"] == ["\\Seen", "\\Answered"] and a["RFC822.SIZE"] == 1234
    assert a["INTERNALDATE"] == "10-Oct-2026 12:00:00 +0200" and a["X-GM-LABELS"] == ["\\Inbox", "Meine Rechnungen"]
    assert a["BODYSTRUCTURE"][:3] == [b"TEXT", b"PLAIN", [b"CHARSET", b"utf-8"]]
    assert a["BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]"] == KOPF


def test_abschnitt_findet_den_inhalt_unabhaengig_von_schreibweise_und_ursprung():
    (a,) = fetch_antworten(VOLL)
    assert abschnitt(a, "HEADER.FIELDS (FROM TO SUBJECT DATE)") == KOPF
    assert abschnitt(a, "header.fields  (from to subject date)") == KOPF
    assert abschnitt(a, "1") is None and abschnitt(a, "TEXT") is None


def test_abschnitt_mit_ursprung_und_nil():
    daten = [(b"1 (UID 5 BODY[1]<0> {5}", b"Hallo"), b" BODY[2] NIL)"]
    (a,) = fetch_antworten(daten)
    assert abschnitt(a, "1") == b"Hallo" and abschnitt(a, "2") is None and abschnitt(a, "3") is None


def test_abschnitt_kopfzeilen_in_anderer_reihenfolge_zurueck():
    daten = [(b"1 (UID 5 BODY[HEADER.FIELDS (TO FROM)] {9}", b"To: x\r\n\r\n"), b")"]
    (a,) = fetch_antworten(daten)
    assert abschnitt(a, "HEADER.FIELDS (FROM TO SUBJECT DATE)") == b"To: x\r\n\r\n"  # der Server ordnet um oder lässt Felder weg


def test_abschnitt_als_zeichenkette_in_anfuehrungszeichen():
    (a,) = fetch_antworten([b'1 (UID 5 BODY[1] "kurz")'])
    assert abschnitt(a, "1") == b"kurz"


def test_fetch_ungefragte_antworten_ohne_uid_fallen_weg():
    daten = [b"3 (FLAGS (\\Seen))", b"4 (UID 9 FLAGS ())"]
    assert [a["UID"] for a in fetch_antworten(daten)] == [9]


def test_fetch_mehrere_antworten_in_der_reihenfolge_des_servers():
    daten = [b"1 (UID 9 FLAGS ())", b"2 (UID 3 FLAGS (\\Seen))"]
    assert [a["UID"] for a in fetch_antworten(daten)] == [9, 3]


def test_fetch_flags_und_labels_ohne_liste_sind_leer():
    (a,) = fetch_antworten([b"1 (UID 5 FLAGS NIL X-GM-LABELS NIL)"])
    assert a["FLAGS"] == [] and a["X-GM-LABELS"] == []


@pytest.mark.parametrize("roh", [
    b"(UID 5)",  # keine Laufnummer
    b"1 UID",  # keine Paare
    b"1 (UID)",  # ungerade
    b"1 (UID x)",  # UID keine Zahl
    b"1 (UID 5 RFC822.SIZE x)",  # Größe keine Zahl
    b"1 ((a) 5)",  # Name keine Zeichenkette
])
def test_fetch_kaputte_antworten(roh):
    with pytest.raises(AntwortFehler):
        fetch_antworten([roh])


# ── LIST ────────────────────────────────────────────────────────────────────────────────────────────────
def test_ordner_liste():
    daten = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Sent) "/" "Gesendet"',
        b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
        b'(\\HasNoChildren) "." Entw&APw-rfe',
        (b'(\\HasNoChildren) "/" {11}', b"Archiv 2026"), b"",
        b"(\\Noselect) NIL Geteilt",
        b"Muell, der kein LIST ist",
    ]
    ordner = ordner_liste(daten)
    assert [o.name for o in ordner] == ["INBOX", "Gesendet", "[Gmail]", "Entwürfe", "Archiv 2026", "Geteilt"]
    assert [o.waehlbar for o in ordner] == [True, True, False, True, True, False]
    assert ordner[1].attribute == {"\\hasnochildren", "\\sent"} and ordner[3].trenner == "." and ordner[5].trenner == ""


def test_ordner_nonexistent_ist_nicht_waehlbar():
    (o,) = ordner_liste([b'(\\NonExistent) "/" "Weg"'])
    assert not o.waehlbar


def test_ordner_liste_leer_oder_none():
    assert ordner_liste([]) == [] and ordner_liste([None]) == []


# ── STATUS ──────────────────────────────────────────────────────────────────────────────────────────────
def test_status_zahlen():
    assert status_zahlen([b'"INBOX" (MESSAGES 231 UNSEEN 12)']) == {"MESSAGES": 231, "UNSEEN": 12}
    assert status_zahlen([b"INBOX (unseen 3)"]) == {"UNSEEN": 3}
    assert status_zahlen([b'"INBOX" (UNSEEN x MESSAGES 4)']) == {"MESSAGES": 4}
    assert status_zahlen([b'"INBOX"']) == {} and status_zahlen([]) == {} and status_zahlen([None]) == {}
