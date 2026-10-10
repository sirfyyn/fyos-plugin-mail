"""Das Plugin ``mail``: die vier Werkzeuge, ihre Zustände und die Anmeldung als Quelle des Kerns."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest
from attrappe import JETZT, Nachricht, Ordner, mail, n, roh_mail

from fyos_plugin_mail import kern
from fyos_plugin_mail.ablage import Postfach
from fyos_plugin_mail.fehler import HINWEIS_EINRICHTEN
from fyos_plugin_mail.plugin import HINWEIS, MailPlugin, mail_kurz, satz_liste, von_mensch

pytestmark = pytest.mark.usefixtures("klartext")
PASSWORT = "geheim"


@pytest.fixture
def einrichten(monkeypatch):
    """``einrichten(server, name="Privat", passwort=None, **Umgebung)`` trägt das Konto der Attrappe ein; gibt das Plugin."""

    def neu(server, name="Privat", passwort=None, **umgebung):
        monkeypatch.setenv("FYOS_MAIL_HOST", server.host)
        monkeypatch.setenv("FYOS_MAIL_PORT", str(server.port))
        monkeypatch.setenv("FYOS_MAIL_NUTZER", server.nutzer)
        monkeypatch.setenv("FYOS_MAIL_PASSWORT", server.passwort if passwort is None else passwort)
        monkeypatch.setenv("FYOS_MAIL_NAME", name)
        for k, v in umgebung.items():
            monkeypatch.setenv(k, v)
        return MailPlugin(heute=lambda: JETZT.date())

    return neu


@pytest.fixture
def mehrere(monkeypatch):
    """``mehrere((server, "Name"), …)`` trägt die Konten über ``FYOS_MAIL_KONTEN`` ein; gibt das Plugin."""

    def neu(*konten):
        eintraege = []
        for i, (server, name) in enumerate(konten):
            monkeypatch.setenv(f"MAIL_PW_{i}", server.passwort)
            eintraege.append({
                "name": name, "host": server.host, "port": server.port, "nutzer": server.nutzer, "passwort_env": f"MAIL_PW_{i}",
            })
        monkeypatch.setenv("FYOS_MAIL_KONTEN", json.dumps(eintraege))
        return MailPlugin(heute=lambda: JETZT.date())

    return neu


def standard_ordner():
    """Vier Mails; wie in jedem Posteingang wächst die UID mit dem Eingang (UID 4 ist die neueste)."""
    return [Ordner("INBOX", [
        n(1, "Ihre Bestellung", von="Versand <no-reply@example.net>", text="Versandt.", vor=timedelta(hours=4)),
        n(2, "Re: Rechnung", von="Alex Muster <alex@example.org>", text="Danke, bezahlt.", gelesen=True, vor=timedelta(hours=3)),
        n(3, "Neues im Oktober", von="Shop <newsletter@example.com>", text="Unsere Angebote.", vor=timedelta(hours=2)),
        n(4, "Einladung zum Sommerfest", von="Erika Beispiel <erika@example.org>", text="Kommst du am Samstag?", vor=timedelta(hours=1)),
    ])]


@pytest.fixture
def standard(imap_server):
    return imap_server(standard_ordner())


def ohne_passwort(antwort):
    text = json.dumps(antwort, ensure_ascii=False)
    assert PASSWORT not in text
    return antwort


# ── Beschreibung ────────────────────────────────────────────────────────────────────────────────────────
def test_die_werkzeuge_sind_nur_lesend_beschrieben():
    p = MailPlugin()
    werkzeuge = {w.name: w for w in p.describe_tools()}
    assert list(werkzeuge) == ["konten", "eingang", "suchen", "lesen"]
    assert p.name == "mail" and p.argumente_vertraulich is True
    for w in werkzeuge.values():
        assert w.tags == ["mail", "read"]
        assert w.input_schema["type"] == "object" and w.input_schema["additionalProperties"] is False
        assert "lesend" in w.description.lower() or "liest" in w.description.lower()
    eigenschaften = {name: set(w.input_schema["properties"]) for name, w in werkzeuge.items()}
    assert eigenschaften == {
        "konten": {"pruefen"}, "eingang": {"anzahl", "voll", "konto"}, "suchen": {"suche", "anzahl", "voll", "konto"},
        "lesen": {"id", "voll"},
    }
    assert werkzeuge["suchen"].input_schema["required"] == ["suche"] and werkzeuge["lesen"].input_schema["required"] == ["id"]
    # nichts an den Parametern lädt Anhänge, markiert, verschiebt oder sendet
    verboten = ("anhang", "senden", "antwort", "loesch", "verschieb", "markier", "flag", "kennzeich", "move", "delete")
    for name, felder in eigenschaften.items():
        assert not [f for f in felder if any(v in f.lower() for v in verboten)], name


def test_ein_unbekanntes_werkzeug_ist_ein_fehler_des_aufrufs():
    with pytest.raises(ValueError, match="kein Werkzeug"):
        MailPlugin().call("senden", {})


def test_call_nimmt_leere_argumente():
    assert MailPlugin().call("eingang", None)["grund"] == "nicht eingerichtet"


# ── Nicht eingerichtet ──────────────────────────────────────────────────────────────────────────────────
def test_konten_ohne_konto_ist_keine_fehlermeldung():
    a = MailPlugin().call("konten", {})
    assert a["quelle_ok"] is True and a["eingerichtet"] is False and a["konten"] == [] and a["satz"] == HINWEIS_EINRICHTEN


@pytest.mark.parametrize(("werkzeug", "args"), [
    ("eingang", {}), ("suchen", {"suche": "from:erika"}), ("lesen", {"id": "m1.0123456789.0123456789.1.1"}),
])
def test_ohne_konto_ist_nicht_eingerichtet_und_nie_keine_mails(werkzeug, args):
    a = MailPlugin().call(werkzeug, args)
    assert a["quelle_ok"] is False and a["grund"] == "nicht eingerichtet" and a["eingerichtet"] is False
    assert a["satz"] == HINWEIS_EINRICHTEN and "mails" not in a


def test_health_ohne_konto_ist_aus_und_nicht_krank():
    h = MailPlugin().health()
    assert h["ok"] is True and h["status"] == "aus" and "FYOS_MAIL_HOST" in h["reason"]


# ── mail.konten ─────────────────────────────────────────────────────────────────────────────────────────
def test_konten_zeigt_die_konten_ohne_passwort(einrichten, standard):
    a = ohne_passwort(einrichten(standard).call("konten", {}))
    assert a["quelle_ok"] is True and a["eingerichtet"] is True and a["quelle"] == "Mail-Plugin (IMAP-Konten)"
    (konto,) = a["konten"]
    assert konto["name"] == "Privat" and konto["art"] == "imap" and konto["nutzer"] == "alex" and konto["ordner"] == ["INBOX"]
    assert konto["adresse"] == f"127.0.0.1:{standard.port}" and "passwort" not in json.dumps(a).lower()
    assert a["satz"] == "1 Konto eingerichtet."
    assert standard.befehle == []  # ohne pruefen: kein Netz


def test_konten_pruefen_meldet_sich_an_und_liest_keine_mail(einrichten, standard):
    a = ohne_passwort(einrichten(standard).call("konten", {"pruefen": True}))
    (konto,) = a["konten"]
    assert konto["status"] == "ok" and konto["ordner_vorhanden"] == ["INBOX"] and konto["ungelesen"] == 3 and konto["gmail"] is False
    assert set(standard.verben()) <= {"LOGIN", "LIST", "STATUS", "LOGOUT"}  # kein Ordner geöffnet, nichts gelesen
    assert standard.verboten == []


def test_konten_pruefen_mit_falschem_passwort(einrichten, standard):
    a = ohne_passwort(einrichten(standard, passwort="falsches-passwort").call("konten", {"pruefen": True}))
    (konto,) = a["konten"]
    assert konto["status"] == "fehler" and "App-Passwort" in konto["fehler"] and "falsches-passwort" not in json.dumps(a)
    assert a["satz"] == "1 Konto eingerichtet, 1 davon mit Fehler."


def test_konten_mehrere_in_der_reihenfolge_der_eintraege(mehrere, imap_server):
    a = mehrere((imap_server(), "Privat"), (imap_server(), "Arbeit")).call("konten", {})
    assert [k["name"] for k in a["konten"]] == ["Privat", "Arbeit"] and a["satz"] == "2 Konten eingerichtet."


def test_konten_unbrauchbarer_eintrag_steht_mit_grund_da(monkeypatch):
    monkeypatch.setenv("FYOS_MAIL_HOST", "imap.example.org")
    monkeypatch.setenv("FYOS_MAIL_NUTZER", "alex")  # ohne Passwort
    p = MailPlugin()
    a = p.call("konten", {})
    assert a["quelle_ok"] is True and a["eingerichtet"] is True
    assert "FYOS_MAIL_PASSWORT" in a["konten"][0]["fehler"] and a["satz"] == "1 Konto eingerichtet, 1 davon mit Fehler."
    e = p.call("eingang", {})
    assert e["quelle_ok"] is False and "Konfiguration fehlerhaft" in e["grund"] and "eingerichtet" not in e
    h = p.health()
    assert h["ok"] is False and h["status"] == "unhealthy" and "FYOS_MAIL_HOST" in h["reason"]  # so heißt der Eintrag


# ── mail.eingang ────────────────────────────────────────────────────────────────────────────────────────
def test_eingang(einrichten, standard):
    a = ohne_passwort(einrichten(standard).call("eingang", {}))
    assert a["quelle_ok"] is True and a["ungelesen"] == 3 and a["hinweis"] == HINWEIS and a["stand"].endswith("+00:00")
    assert [m["betreff"] for m in a["mails"]] == ["Einladung zum Sommerfest", "Neues im Oktober", "Re: Rechnung", "Ihre Bestellung"]
    assert a["satz"] == "3 ungelesen im Posteingang; unter den neuesten 4: 3 ungelesen, 1 von Menschen."
    erste = a["mails"][0]
    assert set(erste) == {"id", "konto", "von", "betreff", "datum", "gelesen", "labels", "ausschnitt", "von_mensch"}
    assert erste["von"] == "Erika Beispiel <erika@example.org>" and erste["ausschnitt"] == "Kommst du am Samstag?"
    assert erste["konto"] == "Privat" and erste["gelesen"] is False and erste["labels"] == ["Posteingang"]
    assert [m["von_mensch"] for m in a["mails"]] == [True, False, True, False]
    assert "konten_fehler" not in a and "ignoriert" not in a


def test_eingang_voll_hat_den_empfaenger_und_einen_laengeren_ausschnitt(einrichten, imap_server):
    s = imap_server([Ordner("INBOX", [n(1, "Lang", text="wort " * 100)])])
    p = einrichten(s)
    kurz_ = p.call("eingang", {})["mails"][0]
    voll = p.call("eingang", {"voll": True})["mails"][0]
    assert "an" not in kurz_ and voll["an"] == "Test <test@example.com>"
    assert len(kurz_["ausschnitt"]) <= 140 < len(voll["ausschnitt"]) <= 200


def test_eingang_leerer_posteingang_ist_eine_antwort(einrichten, imap_server):
    a = einrichten(imap_server([Ordner("INBOX", [])])).call("eingang", {})
    assert a["quelle_ok"] is True and a["mails"] == [] and a["ungelesen"] == 0 and a["satz"] == "Keine Mails im Posteingang."


@pytest.mark.parametrize(("anzahl", "erwartet"), [
    (None, 10), (0, 1), (-5, 1), (1, 1), (3, 3), ("4", 4), (" 6 ", 6), (100, 25), (10**9, 25), (True, 10), (2.5, 10), ("viele", 10),
    ([], 10),
])
def test_eingang_anzahl(einrichten, imap_server, anzahl, erwartet):
    s = imap_server([Ordner("INBOX", [n(i, f"Mail {i}", vor=timedelta(minutes=40 - i)) for i in range(1, 41)])])
    a = einrichten(s).call("eingang", {"anzahl": anzahl})
    assert len(a["mails"]) == erwartet


def test_eingang_mit_konto_filter(mehrere, imap_server):
    s1 = imap_server([Ordner("INBOX", [n(1, "Privat 1")])])
    s2 = imap_server([Ordner("INBOX", [n(1, "Arbeit 1"), n(2, "Arbeit 2")])])
    p = mehrere((s1, "Privat"), (s2, "Arbeit"))
    assert {m["konto"] for m in p.call("eingang", {})["mails"]} == {"Privat", "Arbeit"}
    bisher = len(s1.befehle)
    a = p.call("eingang", {"konto": "arbeit"})
    assert {m["konto"] for m in a["mails"]} == {"Arbeit"} and a["ungelesen"] == 2
    assert len(s1.befehle) == bisher  # das andere Konto bleibt unberührt


def test_eingang_konto_falscher_art_und_unbekannt(mehrere, imap_server):
    p = mehrere((imap_server(), "Privat"))
    a = p.call("eingang", {"konto": 5})
    assert a["quelle_ok"] is False and a["grund"] == "konto muss ein Text sein" and a["satz"].startswith("Die Anfrage geht so nicht")
    a = p.call("eingang", {"konto": "Gibtsnicht"})
    assert a["quelle_ok"] is False and "gibt es nicht; vorhanden: 'Privat'" in a["grund"]


def test_eingang_ein_konto_antwortet_nicht(mehrere, imap_server):
    s1 = imap_server([Ordner("INBOX", [n(1, "Von Privat")])])
    s2 = imap_server([Ordner("INBOX", [n(1, "Von Arbeit")])])
    p = mehrere((s1, "Privat"), (s2, "Arbeit"))
    s2.stop()
    a = ohne_passwort(p.call("eingang", {}))
    assert a["quelle_ok"] is True and [m["betreff"] for m in a["mails"]] == ["Von Privat"]
    assert a["ungelesen"] is None  # 1 wäre falsch, das zweite Konto fehlt
    assert [f["konto"] for f in a["konten_fehler"]] == ["Arbeit"] and "abgelehnt" in a["konten_fehler"][0]["fehler"]
    assert a["satz"].endswith("Unvollständig: Arbeit antwortet nicht.")


def test_eingang_alle_konten_antworten_nicht(einrichten, imap_server):
    s = imap_server()
    p = einrichten(s)
    s.stop()
    a = p.call("eingang", {})
    assert a["quelle_ok"] is False and "mails" not in a and "abgelehnt" in a["grund"]
    assert a["satz"] == "Die Mails sind gerade nicht abrufbar."  # nie „keine Mails“


def test_eingang_falsches_passwort(einrichten, standard):
    a = ohne_passwort(einrichten(standard, passwort="falsches-passwort").call("eingang", {}))
    assert a["quelle_ok"] is False and "App-Passwort" in a["grund"] and "falsches-passwort" not in json.dumps(a)


# ── mail.suchen ─────────────────────────────────────────────────────────────────────────────────────────
def test_suchen(einrichten, standard):
    p = einrichten(standard)
    a = ohne_passwort(p.call("suchen", {"suche": "from:erika"}))
    assert a["quelle_ok"] is True and [m["betreff"] for m in a["mails"]] == ["Einladung zum Sommerfest"]
    assert a["satz"] == "1 Treffer für „from:erika“, 1 ungelesen, davon 1 von Menschen."
    assert a["ungelesen"] is None  # die Zahl im Posteingang gehört nicht zu einer Suche
    assert a["hinweis"] == HINWEIS


def test_suchen_ohne_treffer(einrichten, standard):
    a = einrichten(standard).call("suchen", {"suche": "gibtsnicht"})
    assert a["quelle_ok"] is True and a["mails"] == [] and a["satz"] == "Keine Mails zu „gibtsnicht“."


def test_suchen_nennt_was_ein_imap_konto_nicht_auswertet(einrichten, standard):
    a = einrichten(standard).call("suchen", {"suche": "from:erika has:attachment label:wichtig"})
    assert a["ignoriert"] == ["has:attachment", "label:wichtig"]
    assert a["satz"].endswith("Nicht ausgewertet: has:attachment, label:wichtig.")
    assert [m["betreff"] for m in a["mails"]] == ["Einladung zum Sommerfest"]  # der Rest der Abfrage gilt


@pytest.mark.parametrize("suche", [None, "", "   ", 5, ["from:erika"]])
def test_suchen_ohne_suchbegriff(einrichten, standard, suche):
    a = einrichten(standard).call("suchen", {"suche": suche})
    assert a["quelle_ok"] is False and a["grund"] == "kein Suchbegriff" and "Suchbegriff" in a["satz"]
    assert standard.befehle == []


def test_suchen_zu_viele_begriffe_ist_eine_falsche_eingabe(einrichten, standard):
    a = einrichten(standard).call("suchen", {"suche": "wort " * 100})
    assert a["quelle_ok"] is False and "zu viele Begriffe" in a["grund"] and a["satz"].startswith("Die Anfrage geht so nicht")


def test_suchen_in_sent_mit_ordner_fuer_gesendete(einrichten, imap_server):
    s = imap_server([
        Ordner("INBOX", [n(1, "Eingang")]),
        Ordner("Gesendet", [n(1, "Ausgang", von="Alex Muster <alex@example.org>")], attribute=("\\Sent",)),
    ])
    a = einrichten(s).call("suchen", {"suche": "in:sent"})
    assert [m["betreff"] for m in a["mails"]] == ["Ausgang"] and a["mails"][0]["labels"] == ["Gesendet"]


def test_suchen_voll_und_anzahl(einrichten, standard):
    a = einrichten(standard).call("suchen", {"suche": "newer_than:7d", "anzahl": 2, "voll": True})
    assert len(a["mails"]) == 2 and "an" in a["mails"][0]


# ── mail.lesen ──────────────────────────────────────────────────────────────────────────────────────────
def erste_id(p, betreff=None):
    mails = p.call("eingang", {})["mails"]
    return next(m["id"] for m in mails if betreff is None or m["betreff"] == betreff)


def test_lesen(einrichten, standard):
    p = einrichten(standard)
    a = ohne_passwort(p.call("lesen", {"id": erste_id(p, "Einladung zum Sommerfest")}))
    assert a["quelle_ok"] is True and a["hinweis"] == HINWEIS
    m = a["mail"]
    assert m["betreff"] == "Einladung zum Sommerfest" and m["text"] == "Kommst du am Samstag?" and m["gekuerzt"] is False
    assert m["von"] == "Erika Beispiel <erika@example.org>" and m["an"] == "Test <test@example.com>" and m["von_mensch"] is True
    assert m["gelesen"] is False and m["anhaenge"] == [] and m["text_zeichen"] == len("Kommst du am Samstag?")
    assert a["satz"] == "Mail „Einladung zum Sommerfest“ von Erika Beispiel <erika@example.org>."


def test_lesen_laesst_die_mail_ungelesen_und_aendert_nichts(einrichten, standard):
    p = einrichten(standard)
    vorher = standard.flags()
    for m in p.call("eingang", {})["mails"]:
        assert p.call("lesen", {"id": m["id"]})["quelle_ok"] is True
    assert standard.flags() == vorher and standard.verboten == []
    assert p.call("eingang", {})["ungelesen"] == 3


def test_lesen_text_wird_gekuerzt_und_voll_geht_weiter(einrichten, imap_server):
    s = imap_server([Ordner("INBOX", [n(1, "Lang", text=("Zeile mit Text und Zahl 12345.\n" * 700), cte="7bit")])])  # etwa 21 000 Zeichen
    p = einrichten(s)
    kennung = erste_id(p)
    a = p.call("lesen", {"id": kennung})["mail"]
    assert len(a["text"]) == 8000 and a["gekuerzt"] is True and a["text_zeichen"] > 20000
    voll = p.call("lesen", {"id": kennung, "voll": True})
    assert len(voll["mail"]["text"]) == voll["mail"]["text_zeichen"] > 20000 and voll["mail"]["gekuerzt"] is False
    assert "gekürzt" not in voll["satz"]
    kurz_ = p.call("lesen", {"id": kennung})
    assert "Text auf 8000 von " in kurz_["satz"]


def test_lesen_mit_anhaengen(einrichten, imap_server):
    roh = mail(text="Siehe Anhang", anhaenge=(("Rechnung.pdf", "application/pdf", b"%PDF-1.4" * 50), ("bild.png", "image/png", b"\x89PNG" * 10)))
    s = imap_server([Ordner("INBOX", [Nachricht(1, roh, intern=JETZT - timedelta(hours=1))])])
    p = einrichten(s)
    a = p.call("lesen", {"id": erste_id(p)})
    assert [(x["name"], x["typ"]) for x in a["mail"]["anhaenge"]] == [("Rechnung.pdf", "application/pdf"), ("bild.png", "image/png")]
    assert a["satz"].endswith(", 2 Anhänge.")
    angefordert = [z for z in s.zeilen("UID FETCH") if "BODY.PEEK[" in z and "HEADER.FIELDS" not in z]
    assert angefordert and all("BODY.PEEK[1]" in z for z in angefordert)  # nur der Text, nie ein Anhang


def test_lesen_html_als_text(einrichten, imap_server):
    s = imap_server([Ordner("INBOX", [n(1, "HTML", text=None, html="<p>Hallo <b>Welt</b></p><p>Zweiter Absatz</p>")])])
    p = einrichten(s)
    assert p.call("lesen", {"id": erste_id(p)})["mail"]["text"] == "Hallo Welt\n\nZweiter Absatz"


@pytest.mark.parametrize("kennung", [None, "", "  ", 5, ["m1"]])
def test_lesen_ohne_id(einrichten, standard, kennung):
    a = einrichten(standard).call("lesen", {"id": kennung})
    assert a["quelle_ok"] is False and a["grund"] == "ungültige Mail-ID" and "mail.eingang" in a["satz"]


@pytest.mark.parametrize("kennung", ["12345", "../../etc/passwd", "m1.zz.zz.1.1", "m1.0123456789.0123456789.1.0"])
def test_lesen_fremde_id(einrichten, standard, kennung):
    a = einrichten(standard).call("lesen", {"id": kennung})
    assert a["quelle_ok"] is False and a["grund"] == "ungültige Mail-ID"
    assert standard.befehle == []


def test_lesen_einer_mail_die_es_nicht_mehr_gibt(einrichten, standard):
    p = einrichten(standard)
    kennung = erste_id(p, "Re: Rechnung")
    standard.ordner["INBOX"].nachrichten = [x for x in standard.ordner["INBOX"].nachrichten if x.uid != 2]
    a = p.call("lesen", {"id": kennung})
    assert a["quelle_ok"] is False and "gibt es nicht mehr" in a["grund"] and a["satz"].startswith("Nicht gefunden:")


def test_lesen_nach_neuaufbau_des_ordners_zeigt_keine_fremde_mail(einrichten, standard):
    p = einrichten(standard)
    kennung = erste_id(p, "Re: Rechnung")
    standard.ordner["INBOX"].gueltig += 1
    a = p.call("lesen", {"id": kennung})
    assert a["quelle_ok"] is False and "mail" not in a and "gibt es nicht mehr" in a["grund"]


def test_lesen_ein_konto_antwortet_nicht(einrichten, imap_server):
    s = imap_server([Ordner("INBOX", [n(1)])])
    p = einrichten(s)
    kennung = erste_id(p)
    s.stop()
    a = p.call("lesen", {"id": kennung})
    assert a["quelle_ok"] is False and a["satz"] == "Die Mails sind gerade nicht abrufbar."


# ── Fehler gehen nie als Ausnahme nach außen ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("werkzeug", "args"), [
    ("konten", {"pruefen": True}), ("eingang", {}), ("suchen", {"suche": "x"}), ("lesen", {"id": "m1.0123456789.0123456789.1.1"}),
])
def test_ein_fehler_im_plugin_wird_zu_einer_antwort(einrichten, standard, monkeypatch, werkzeug, args):
    def kaputt(*a, **k):
        raise KeyError("Geheimes Detail")

    for name in ("abfragen", "lesen", "pruefen"):
        monkeypatch.setattr(Postfach, name, kaputt)
    a = einrichten(standard).call(werkzeug, args)
    if werkzeug == "konten":  # die Konten selbst stehen auch ohne Netz da; die Prüfung fällt aus
        assert a["quelle_ok"] is False
    assert a["quelle_ok"] is False and a["grund"] == "Unerwarteter Fehler (KeyError)" and "Geheimes" not in json.dumps(a)


def test_zugangsdaten_stehen_in_keinem_protokoll(einrichten, standard, caplog):
    caplog.set_level(logging.DEBUG)
    p = einrichten(standard)
    p.register()
    p.call("konten", {"pruefen": True})
    ids = [m["id"] for m in p.call("eingang", {})["mails"]]
    p.call("lesen", {"id": ids[0]})
    p.call("suchen", {"suche": "from:erika"})
    einrichten(standard, passwort="falsches-passwort").call("eingang", {})
    assert PASSWORT not in caplog.text and "falsches-passwort" not in caplog.text


# ── Hilfen ──────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("absender", "labels", "mensch"), [
    ("Erika Beispiel <erika@example.org>", [], True),
    ("erika@example.org", [], True),
    ("Shop <newsletter@example.com>", [], False),
    ("Versand <no-reply@example.net>", [], False),
    ("Versand <noreply@example.net>", [], False),
    ("Versand <do-not-reply@example.net>", [], False),
    ("Info <info@example.com>", [], False),
    ("Service <service@example.com>", [], False),
    ("Konto <notifications@example.com>", [], False),
    ("Postmaster <mailer-daemon@example.com>", [], False),
    ("Erika Beispiel <erika@example.org>", ["Werbung"], False),
    ("Erika Beispiel <erika@example.org>", ["Posteingang", "Foren"], False),
    ("Nur ein Name", [], False),
    ("", [], False),
    (None, None, False),
])
def test_von_mensch(absender, labels, mensch):
    assert von_mensch(absender, labels) is mensch


def test_mail_kurz_nimmt_was_da_ist():
    m = mail_kurz({"id": "x"}, voll=True)
    assert m == {
        "id": "x", "konto": "", "von": "", "betreff": "(kein Betreff)", "datum": "", "gelesen": False, "labels": [], "ausschnitt": "",
        "von_mensch": False, "an": "",
    }


def test_satz_liste_ohne_zahl():
    mails = [{"gelesen": False, "von_mensch": True}]
    assert satz_liste(mails, None) == "Posteingang; unter den neuesten 1: 1 ungelesen, 1 von Menschen."
    assert satz_liste([], 0, "x y") == "Keine Mails zu „x y“."


def test_vorhandene_roh_mail_ohne_betreff(einrichten, imap_server):
    roh = roh_mail([("From", "A <a@example.org>"), ("Date", "Sat, 10 Oct 2026 11:00:00 +0000")], b"Nur Text\r\n")
    s = imap_server([Ordner("INBOX", [Nachricht(1, roh)])])
    a = einrichten(s).call("eingang", {})
    assert a["mails"][0]["betreff"] == "(kein Betreff)"


# ── Der Kern: Anmeldung als Quelle ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def postfach():
    return pytest.importorskip("mcp_hub.postfach")


def test_registrierung_ist_leicht_und_ohne_netz(einrichten, standard):
    p = einrichten(standard)
    p.register()
    assert standard.befehle == [] and standard.verbindungen == 0


def test_anmeldung_als_quelle_und_abmeldung(einrichten, standard, postfach):
    p = einrichten(standard)
    assert postfach.quelle().art == "keine"
    p.register()
    p.registry_anbinden(object())
    assert postfach.angemeldete_ablage() is p
    q = postfach.quelle()
    assert q.art == "ablage" and q.eingerichtet and q.beschreibung() == "Mail-Plugin mail"
    assert p.health() == {"ok": True, "status": "healthy", "konten": 1, "quelle_der_mails": True}
    p.shutdown()
    assert postfach.angemeldete_ablage() is None and postfach.quelle().art == "keine"


def test_abmeldung_nimmt_eine_andere_ablage_nicht_mit(einrichten, standard, postfach):
    p = einrichten(standard)
    p.registry_anbinden(object())

    class Andere:
        def ablage_konten(self):
            return []

    andere = Andere()
    postfach.ablage_anmelden(andere)
    p.shutdown()
    assert postfach.angemeldete_ablage() is andere


def test_ohne_konto_meldet_sich_das_plugin_nicht_an(postfach):
    p = MailPlugin()
    p.registry_anbinden(object())
    assert postfach.angemeldete_ablage() is None and postfach.quelle().art == "keine"
    p.shutdown()


def test_ein_unbrauchbarer_eintrag_meldet_sich_an_damit_der_leser_den_fehler_sieht(monkeypatch, postfach):
    monkeypatch.setenv("FYOS_MAIL_HOST", "imap.example.org")  # kein Nutzer, kein Passwort
    p = MailPlugin()
    p.registry_anbinden(object())
    q = postfach.quelle()
    assert q.art == "ablage"
    with pytest.raises(postfach.QuelleFehler, match="Konfiguration fehlerhaft"):
        q.liste("in:inbox", 5)
    assert "fehler" in q.konten()[0]


def test_der_kern_liest_die_mails_des_plugins(einrichten, standard, postfach):
    p = einrichten(standard)
    p.registry_anbinden(object())
    q = postfach.quelle()
    liste = q.liste("in:inbox newer_than:2d -category:promotions -category:social -category:forums", 3)
    assert [m["betreff"] for m in liste["mails"]] == ["Einladung zum Sommerfest", "Neues im Oktober", "Re: Rechnung"]
    assert liste["ungelesen"] == 3
    erste = liste["mails"][0]
    assert set(erste) == {"id", "konto", "von", "an", "betreff", "datum", "gelesen", "labels", "ausschnitt"}
    assert erste["datum"] == "2026-10-10T11:00:00+00:00" and erste["konto"] == "Privat" and erste["labels"] == ["Posteingang"]
    voll = q.lesen(erste["id"])
    assert voll["text"] == "Kommst du am Samstag?" and voll["gekuerzt"] is False and voll["anhaenge"] == []
    assert q.konten() == [{"name": "Privat", "art": "imap", "adresse": f"127.0.0.1:{standard.port}", "nutzer": "alex"}]
    assert standard.verboten == []


def test_der_kern_bekommt_nie_eine_unvollstaendige_liste(mehrere, imap_server, postfach):
    s1 = imap_server([Ordner("INBOX", [n(1, "Von Privat")])])
    s2 = imap_server([Ordner("INBOX", [n(1, "Von Arbeit")])])
    p = mehrere((s1, "Privat"), (s2, "Arbeit"))
    p.registry_anbinden(object())
    s2.stop()
    with pytest.raises(postfach.QuelleFehler) as e:  # nicht „1 Mail“: das sähe aus wie ein vollständiger Posteingang
        postfach.quelle().liste("in:inbox", 5)
    assert "Arbeit" in str(e.value) and e.value.status == 503
    assert not isinstance(e.value, postfach.NichtEingerichtet)
    # die Werkzeuge dagegen zeigen, was erreichbar ist
    assert [m["betreff"] for m in p.call("eingang", {})["mails"]] == ["Von Privat"]


def test_der_kern_meldet_eine_unbekannte_kennung_und_ein_unbekanntes_konto_als_falsche_eingabe(einrichten, standard, postfach):
    p = einrichten(standard)
    p.registry_anbinden(object())
    q = postfach.quelle()
    with pytest.raises(ValueError):
        q.liste("in:inbox", 5, "Gibtsnicht")
    with pytest.raises(ValueError):
        q.lesen("m1.0123456789.0123456789.1.0")
    with pytest.raises(ValueError):
        q.lesen("12345")


def test_der_kern_bekommt_404_wenn_es_die_mail_nicht_mehr_gibt(einrichten, standard, postfach):
    p = einrichten(standard)
    p.registry_anbinden(object())
    q = postfach.quelle()
    kennung = q.liste("in:inbox", 5)["mails"][0]["id"]
    standard.ordner["INBOX"].nachrichten.clear()
    with pytest.raises(postfach.QuelleFehler) as e:
        q.lesen(kennung)
    assert e.value.status == 404 and "gibt es nicht mehr" in str(e.value)


def test_die_ablage_zeigt_dem_kern_nur_den_typ_eines_unerwarteten_fehlers(einrichten, standard, postfach, monkeypatch):
    p = einrichten(standard)
    p.registry_anbinden(object())
    monkeypatch.setattr(Postfach, "abfragen", lambda *a, **k: (_ for _ in ()).throw(KeyError("Geheimes Detail")))
    with pytest.raises(postfach.QuelleFehler) as e:
        postfach.quelle().liste("in:inbox", 5)
    assert "Geheimes" not in str(e.value) and "KeyError" in str(e.value)


def test_die_werkzeuge_lesen_ueber_einen_eingetragenen_mail_dienst(einrichten, standard, postfach, monkeypatch):
    """Hat der Besitzer einen eigenen Dienst (FYOS_POSTFACH_URL), hat der Vorrang; die IMAP-Konten ruhen."""
    aufrufe = []

    def roh(self, pfad, frist=None):
        aufrufe.append(pfad)
        if pfad.startswith("/api/inbox/list"):
            return 200, {"status": "live", "_stub": False, "messages": [{
                "id": "abc123", "subject": "Vom Dienst", "from": "Dienst <dienst@example.org>", "to": "alex@example.org",
                "date": "Sat, 10 Oct 2026 11:00:00 +0000", "snippet": "Hallo vom Dienst", "label_ids": ["INBOX", "UNREAD"],
                "unread": True}]}
        if pfad.startswith("/api/inbox/unread-count"):
            return 200, {"inbox_unread": 7}
        if pfad.startswith("/api/inbox/message/"):
            return 200, {"id": "abc123", "subject": "Vom Dienst", "from": "Dienst <dienst@example.org>", "to": "alex@example.org",
                         "date": "Sat, 10 Oct 2026 11:00:00 +0000", "label_ids": ["INBOX"], "unread": True,
                         "body_plain": "Der Text vom Dienst", "body_html": "", "attachments": [
                             {"filename": "a.pdf", "mime_type": "application/pdf", "size": 12}]}
        return 404, {}

    monkeypatch.setattr(postfach.Dienst, "roh", roh)
    monkeypatch.setenv("FYOS_POSTFACH_URL", "http://dienst.example.org")
    p = einrichten(standard)
    p.registry_anbinden(object())
    a = p.call("eingang", {})
    assert a["quelle_ok"] is True and a["ungelesen"] == 7 and [m["betreff"] for m in a["mails"]] == ["Vom Dienst"]
    assert a["mails"][0]["konto"] == "dienst" and a["mails"][0]["labels"] == ["Posteingang"]
    m = p.call("lesen", {"id": "abc123"})["mail"]
    assert m["text"] == "Der Text vom Dienst" and [x["name"] for x in m["anhaenge"]] == ["a.pdf"]
    k = p.call("konten", {"pruefen": True})
    assert k["quelle"] == "Mail-Dienst dienst.example.org" and [x["name"] for x in k["konten"]] == ["dienst"]
    assert "Mail-Dienst" in k["hinweis"]
    assert standard.befehle == [] and aufrufe  # die IMAP-Konten ruhen


def test_ein_ausgefallener_mail_dienst_ist_nie_keine_mails(einrichten, standard, postfach, monkeypatch):
    def roh(self, pfad, frist=None):
        raise postfach.QuelleFehler("Mail-Dienst dienst.example.org nicht erreichbar (Verbindung abgelehnt).")

    monkeypatch.setattr(postfach.Dienst, "roh", roh)
    monkeypatch.setenv("FYOS_POSTFACH_URL", "http://dienst.example.org")
    a = einrichten(standard).call("eingang", {})
    assert a["quelle_ok"] is False and "nicht erreichbar" in a["grund"] and a["satz"] == "Die Mails sind gerade nicht abrufbar."
    assert "mails" not in a


def test_die_werkzeuge_lesen_ueber_die_ablage_eines_anderen_plugins(imap_server, postfach):
    """Ist ein anderes Mail-Plugin die Quelle, liest dieses nicht daneben her."""

    class Fremd:
        name = "fremd"

        def ablage_konten(self):
            return [{"name": "fremdkonto", "art": "imap", "adresse": "x:993"}]

        def ablage_liste(self, abfrage, anzahl, konto=None):
            return {"mails": [{"id": "fremd1", "betreff": "Fremde Mail", "von": "A <a@example.org>"}], "ungelesen": 1}

        def ablage_lesen(self, id):
            return {"id": id, "betreff": "Fremde Mail", "text": "Text", "gekuerzt": False, "anhaenge": []}

    postfach.ablage_anmelden(Fremd())
    a = MailPlugin().call("eingang", {})
    assert a["quelle_ok"] is True and [m["betreff"] for m in a["mails"]] == ["Fremde Mail"]
    assert MailPlugin().call("lesen", {"id": "fremd1"})["mail"]["text"] == "Text"


# ── Ohne Postfach-Quelle im Kern (fyos-hub 0.1.0) ───────────────────────────────────────────────────────
@pytest.fixture
def alter_kern(monkeypatch):
    monkeypatch.setattr(kern, "modul", lambda: None)


def test_ohne_postfach_im_kern_laufen_die_werkzeuge_trotzdem(einrichten, standard, alter_kern):
    p = einrichten(standard)
    p.registry_anbinden(object())  # kein Fehler, nur keine Anmeldung
    assert p.health() == {"ok": True, "status": "healthy", "konten": 1, "quelle_der_mails": False}
    a = p.call("eingang", {})
    assert a["quelle_ok"] is True and len(a["mails"]) == 4 and a["ungelesen"] == 3
    assert p.call("lesen", {"id": a["mails"][0]["id"]})["mail"]["text"] == "Kommst du am Samstag?"
    assert p.call("konten", {})["quelle"] == "Mail-Plugin (IMAP-Konten)"
    p.shutdown()


def test_ohne_postfach_im_kern_bleiben_die_fehler_die_des_plugins(einrichten, standard, alter_kern):
    leer = MailPlugin()  # vor dem Eintragen des Kontos: nicht eingerichtet
    p = einrichten(standard)
    kennung = p.call("eingang", {})["mails"][0]["id"]
    standard.ordner["INBOX"].nachrichten.clear()
    with pytest.raises(Exception) as e:
        p.ablage_lesen(kennung)
    assert type(e.value).__module__.startswith("fyos_plugin_mail") and e.value.status == 404
    with pytest.raises(Exception) as e:
        leer.ablage_konten()
    assert type(e.value).__module__.startswith("fyos_plugin_mail") and e.value.status == 501
