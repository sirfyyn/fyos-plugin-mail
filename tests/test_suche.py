"""Die Gmail-artige Abfrage lesen und nach IMAP SEARCH übersetzen. Reines Rechnen auf Texten, ohne Netz."""

from __future__ import annotations

import random
import re
from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from fyos_plugin_mail import suche
from fyos_plugin_mail.suche import Begriff, Gruppe, gmail_gruppe, imap_datum, imap_gruppen, planen, zitieren

HEUTE = date(2026, 10, 10)


def b(art: str, wert: str = "", negiert: bool = False) -> Begriff:
    return Begriff(art, wert, negiert)


# ── lesen ───────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("abfrage", ["", "   ", "\n\t", None])
def test_leere_abfrage_heisst_posteingang(abfrage):
    plan = planen(abfrage)
    assert (plan.text, plan.begriffe, plan.rollen, plan.ignoriert) == ("in:inbox", (), ("inbox",), ())


@pytest.mark.parametrize(("abfrage", "erwartet"), [
    ("from:alex", [b("von", "alex")]),
    ("FROM:Alex", [b("von", "Alex")]),
    ("to:sam@example.net", [b("an", "sam@example.net")]),
    ("subject:rechnung", [b("betreff", "rechnung")]),
    ('subject:"zwei Wörter"', [b("betreff", "zwei Wörter")]),
    ('subject:"offen', [b("betreff", "offen")]),
    ("is:unread", [b("ungelesen")]),
    ("is:read", [b("ungelesen", negiert=True)]),
    ("-is:read", [b("ungelesen")]),
    ("-is:unread", [b("ungelesen", negiert=True)]),
    ("newer_than:2d", [b("neuer", "2")]),
    ("older_than:2D", [b("aelter", "2")]),
    ("newer_than:1w older_than:2m newer_than:1y", [b("neuer", "7"), b("aelter", "60"), b("neuer", "365")]),
    ("after:2026/10/01", [b("nach", "2026-10-01")]),
    ("before:2026-10-05", [b("vor", "2026-10-05")]),
    ("rechnung oktober", [b("wort", "rechnung"), b("wort", "oktober")]),
    ('"zwei Wörter"', [b("wort", "zwei Wörter")]),
    ("-newsletter", [b("wort", "newsletter", True)]),
    ("-from:alex", [b("von", "alex", True)]),
    ("a AND b", [b("wort", "a"), b("wort", "b")]),
    ("12:30 Uhr", [b("wort", "12:30"), b("wort", "Uhr")]),  # kein Operator, ein Wort mit Doppelpunkt
    ("alex@example.org", [b("wort", "alex@example.org")]),
    ("(Entwurf)", [b("wort", "Entwurf")]),  # eine Klammer, die sich selbst schließt, ist keine Gruppe
    ("- rechnung", [b("wort", "rechnung")]),  # ein einzelner Strich verneint nichts
    ("is:unread from:alex newer_than:2d", [b("ungelesen"), b("von", "alex"), b("neuer", "2")]),
])
def test_begriffe(abfrage, erwartet):
    plan = planen(abfrage)
    assert list(plan.begriffe) == erwartet
    assert plan.ignoriert == () and plan.rollen == ()


@pytest.mark.parametrize(("abfrage", "rollen"), [
    ("in:inbox", ("inbox",)),
    ("in:sent", ("sent",)),
    ("IN:Sent", ("sent",)),
    ("in:drafts in:draft", ("drafts",)),
    ("in:trash in:spam", ("trash", "spam")),
    ("in:junk", ("spam",)),
    ("in:inbox in:sent in:inbox", ("inbox", "sent")),
])
def test_ordner_mit_in(abfrage, rollen):
    plan = planen(abfrage)
    assert plan.rollen == rollen and plan.begriffe == () and plan.ignoriert == ()


def test_ohne_in_sind_die_rollen_leer_damit_die_ordner_des_kontos_gelten():
    assert planen("is:unread").rollen == () and planen("rechnung").rollen == ()


@pytest.mark.parametrize(("abfrage", "ignoriert"), [
    ("label:wichtig", ("label:wichtig",)),
    ("-category:promotions", ("-category:promotions",)),
    ("category:social", ("category:social",)),
    ("has:attachment", ("has:attachment",)),
    ("larger:5M filename:pdf", ("larger:5M", "filename:pdf")),
    ("in:anywhere", ("in:anywhere",)),
    ("-in:inbox", ("-in:inbox",)),
    ("newer_than:abc", ("newer_than:abc",)),
    ("newer_than:2x", ("newer_than:2x",)),
    ("after:2026/02/30", ("after:2026/02/30",)),
    ("after:morgen", ("after:morgen",)),
    ("is:starred", ("is:starred",)),
    ("cc:alex", ("cc:alex",)),
])
def test_was_das_plugin_nicht_versteht_wird_ueberlesen_und_genannt(abfrage, ignoriert):
    plan = planen(abfrage)
    assert plan.ignoriert == ignoriert and plan.begriffe == () and plan.rollen == ()


def test_ueberlesenes_stoert_den_rest_nicht():
    plan = planen("in:inbox newer_than:2d -category:promotions -category:social -category:forums")
    assert plan.begriffe == (b("neuer", "2"),) and plan.rollen == ("inbox",)
    assert plan.ignoriert == ("-category:promotions", "-category:social", "-category:forums")


@pytest.mark.parametrize(("abfrage", "begriffe", "ignoriert"), [
    # OR und Gruppen versteht nur Gmail; ihre Begriffe einzeln als „und“ zu lesen, fände weniger als gemeint
    ("from:a OR from:b", [], ["from:a", "OR", "from:b"]),
    ("is:unread from:a OR from:b", [b("ungelesen")], ["from:a", "OR", "from:b"]),
    ("(from:a OR from:b) is:unread", [b("ungelesen")], ["from:a", "OR", "from:b"]),
    ("{from:a from:b} is:unread", [b("ungelesen")], ["from:a", "from:b"]),
    ("rechnung OR mahnung newer_than:2d", [b("neuer", "2")], ["rechnung", "OR", "mahnung"]),
    ('(subject:"a b" OR c) d', [b("wort", "d")], ["subject:a b", "OR", "c"]),
    ("a OR b OR c d", [b("wort", "d")], ["a", "OR", "b", "OR", "c"]),
    ("-(from:a OR from:b) e", [b("wort", "e")], ["-from:a", "OR", "from:b"]),
    ("or", [b("wort", "or")], []),  # klein geschrieben ist es ein Wort
    ("OR", [], ["OR"]),
])
def test_oder_und_gruppen(abfrage, begriffe, ignoriert):
    plan = planen(abfrage)
    assert list(plan.begriffe) == begriffe and list(plan.ignoriert) == ignoriert


def test_steuerzeichen_sind_leerraum():
    assert planen("from:alex\x00\r\n\tis:unread\x1b").begriffe == (b("von", "alex"), b("ungelesen"))


def test_zu_viele_begriffe():
    planen(" ".join(f"w{i}" for i in range(suche.BEGRIFFE_MAX)))
    with pytest.raises(ValueError, match="zu viele Begriffe"):
        planen(" ".join(f"w{i}" for i in range(suche.BEGRIFFE_MAX + 1)))
    # was überlesen wird, zählt nicht
    planen(" ".join(f"label:l{i}" for i in range(suche.BEGRIFFE_MAX * 3)))


def test_zu_langer_wert():
    planen("x" * suche.WERT_MAX)
    with pytest.raises(ValueError, match="länger"):
        planen("x" * (suche.WERT_MAX + 1))
    with pytest.raises(ValueError, match="länger"):
        planen("from:" + "x" * (suche.WERT_MAX + 1))


def test_der_text_des_plans_zieht_leerraum_zusammen():
    assert planen("  from:alex    is:unread \n").text == "from:alex is:unread"


def test_beliebige_eingaben_werfen_hoechstens_value_error():
    zufall = random.Random(20261010)
    stuecke = ["from:", "to:", "subject:", "is:unread", "newer_than:", "in:", "-", "(", ")", "{", "}", '"', "OR", "AND", "x",
               "2d", "alex", "label:", "\x00", " ", "  ", "\\", "ü", "'", ":", "after:2026/01/01", "😀", "𝔘"]
    for _ in range(2000):
        abfrage = "".join(zufall.choice(stuecke) for _ in range(zufall.randint(0, 30)))
        try:
            plan = planen(abfrage)
        except ValueError:
            continue
        assert plan.text and len(plan.begriffe) <= suche.BEGRIFFE_MAX
        imap_gruppen(plan, HEUTE)
        gmail_gruppe(plan)


def test_plan_und_begriff_sind_unveraenderlich():
    plan = planen("from:alex")
    with pytest.raises(FrozenInstanceError):
        plan.text = "x"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.begriffe[0].wert = "x"  # type: ignore[misc]


# ── IMAP SEARCH ─────────────────────────────────────────────────────────────────────────────────────────
def gruppen(abfrage: str) -> list[Gruppe]:
    return imap_gruppen(planen(abfrage), HEUTE)


def test_leere_abfrage_sucht_nur_nach_nicht_geloeschtem():
    assert gruppen("") == [Gruppe(("UNDELETED",))]


def test_jede_suche_beginnt_mit_undeleted():
    for abfrage in ("", "from:alex", "is:unread", "newer_than:2d", "rechnung", "Übersicht", "-from:alex"):
        assert all(g.schluessel[0] == "UNDELETED" for g in gruppen(abfrage)), abfrage


@pytest.mark.parametrize(("abfrage", "schluessel"), [
    ("from:alex", ("UNDELETED", "FROM", '"alex"')),
    ("to:sam@example.net", ("UNDELETED", "TO", '"sam@example.net"')),
    ("subject:rechnung", ("UNDELETED", "SUBJECT", '"rechnung"')),
    ("rechnung", ("UNDELETED", "TEXT", '"rechnung"')),
    ("is:unread", ("UNDELETED", "UNSEEN")),
    ("is:read", ("UNDELETED", "SEEN")),
    ("newer_than:2d", ("UNDELETED", "SINCE", "08-Oct-2026")),
    ("older_than:2d", ("UNDELETED", "BEFORE", "08-Oct-2026")),
    ("newer_than:1w", ("UNDELETED", "SINCE", "03-Oct-2026")),
    ("after:2026/10/01", ("UNDELETED", "SINCE", "01-Oct-2026")),
    ("before:2026-10-05", ("UNDELETED", "BEFORE", "05-Oct-2026")),
    ("-from:alex", ("UNDELETED", "NOT", "FROM", '"alex"')),
    ("-newsletter", ("UNDELETED", "NOT", "TEXT", '"newsletter"')),
    ("-newer_than:2d", ("UNDELETED", "NOT", "SINCE", "08-Oct-2026")),
    ("from:alex is:unread newer_than:2d", ("UNDELETED", "FROM", '"alex"', "UNSEEN", "SINCE", "08-Oct-2026")),
    ('subject:"zwei Wörter"'.replace("Wörter", "Woerter"), ("UNDELETED", "SUBJECT", '"zwei Woerter"')),
    ("in:inbox", ("UNDELETED",)),  # der Ordner wird nicht gesucht, sondern gewählt
    ("label:wichtig", ("UNDELETED",)),  # überlesen
])
def test_imap_schluessel(abfrage, schluessel):
    assert gruppen(abfrage) == [Gruppe(schluessel)]


def test_zitieren():
    assert zitieren("alex") == '"alex"'
    assert zitieren('a"b') == '"a\\"b"'
    assert zitieren("a\\b") == '"a\\\\b"'
    assert zitieren('\\"') == '"\\\\\\""'
    assert zitieren("") == '""'


def test_ein_wert_kann_kein_imap_kommando_einschleusen():
    """Was der Mensch (oder ein Modell, oder der Text einer Mail) in die Abfrage schreibt, wird zu zitierten Werten.
    Jedes Element einer Suche ist ein Schlüsselwort aus einer festen Menge, ein Datum im IMAP-Format oder ein Wert in
    Anführungszeichen, in dem jedes Anführungszeichen und jeder Rückstrich maskiert ist."""
    schluesselwoerter = {"UNDELETED", "FROM", "TO", "SUBJECT", "TEXT", "UNSEEN", "SEEN", "NOT", "SINCE", "BEFORE"}
    datum = re.compile(r"\d\d-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{4}")
    boese = ['x" ALL', 'x" ALL "', "x\\", 'x" OR "y', "}{", "{5}", "x)(", '")', 'x"\\" ALL', "\\\\\"", '" DELETED "',
             "x\x00y", "A002 DELETE INBOX", "x} A002 LOGOUT", "{99999}", "~"]
    for text in boese:
        for operator in ("subject:", "from:", "to:", "", "-", "-from:"):
            for gruppe in imap_gruppen(planen(operator + text), HEUTE):
                for element in gruppe.schluessel:
                    if element.startswith('"'):
                        assert element.endswith('"') and len(element) >= 2, (text, element)
                        rest = element[1:-1].replace("\\\\", "").replace('\\"', "")
                        assert '"' not in rest and "\\" not in rest, (text, element)
                    else:
                        assert element in schluesselwoerter or datum.fullmatch(element), (operator + text, element)


def test_umlaute_gehen_als_literal():
    (gruppe,) = gruppen("subject:Übersicht")
    assert gruppe == Gruppe(("UNDELETED", "SUBJECT"), "Übersicht".encode())


def test_mehrere_umlautwoerter_werden_zu_mehreren_suchen_mit_den_uebrigen_bedingungen():
    g = gruppen("is:unread Übersicht Größe from:alex")
    assert g == [
        Gruppe(("UNDELETED", "UNSEEN", "FROM", '"alex"', "TEXT"), "Übersicht".encode()),
        Gruppe(("UNDELETED", "UNSEEN", "FROM", '"alex"', "TEXT"), "Größe".encode()),
    ]


def test_umlaut_in_verneinung_und_betreff():
    assert gruppen("-Übersicht") == [Gruppe(("UNDELETED", "NOT", "TEXT"), "Übersicht".encode())]
    assert gruppen("from:Jürgen") == [Gruppe(("UNDELETED", "FROM"), "Jürgen".encode())]


def test_steuer_und_sonderzeichen_ausserhalb_von_ascii_gehen_als_literal():
    plan = planen("rechnung")
    assert imap_gruppen(suche.Plan("x", (b("wort", "a\x7fb"),), (), ()), HEUTE) == [Gruppe(("UNDELETED", "TEXT"), b"a\x7fb")]
    assert imap_gruppen(suche.Plan("x", (b("wort", "😀"),), (), ()), HEUTE) == [Gruppe(("UNDELETED", "TEXT"), "😀".encode())]
    assert plan.begriffe  # (der Plan selbst bleibt unberührt)


@pytest.mark.parametrize(("tag", "erwartet"), [
    (date(2026, 10, 8), "08-Oct-2026"),
    (date(2026, 1, 1), "01-Jan-2026"),
    (date(2026, 12, 31), "31-Dec-2026"),
    (date(2026, 5, 9), "09-May-2026"),
])
def test_imap_datum_ist_unabhaengig_von_der_sprache_des_rechners(tag, erwartet):
    assert imap_datum(tag) == erwartet


def test_gmail_bekommt_die_abfrage_unveraendert():
    assert gmail_gruppe(planen("from:alex -category:promotions")) == Gruppe(("X-GM-RAW", '"from:alex -category:promotions"'))
    assert gmail_gruppe(planen("")) == Gruppe(("X-GM-RAW", '"in:inbox"'))
    assert gmail_gruppe(planen("(from:a OR from:b) is:unread")) == Gruppe(("X-GM-RAW", '"(from:a OR from:b) is:unread"'))
    assert gmail_gruppe(planen('subject:"a b"')) == Gruppe(("X-GM-RAW", '"subject:\\"a b\\""'))


def test_gmail_mit_umlauten_als_literal():
    assert gmail_gruppe(planen("Übersicht")) == Gruppe(("X-GM-RAW",), "Übersicht".encode())
