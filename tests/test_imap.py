"""``imap.py`` gegen die Attrappe: Anmeldung, Ordner, Suche, Holen und die Übersetzung von Fehlern. Nur lesende Befehle."""

from __future__ import annotations

import imaplib
import socket
import ssl

import pytest
from attrappe import JETZT, Nachricht, Ordner, mail

from fyos_plugin_mail import imap
from fyos_plugin_mail.imap import ImapFehler, Sitzung
from fyos_plugin_mail.konten import Konto
from fyos_plugin_mail.suche import Gruppe, imap_gruppen, planen

pytestmark = pytest.mark.usefixtures("klartext")


def nachrichten(n, **kw):
    return [Nachricht(uid=i, roh=mail(betreff=f"Mail {i}", **kw), flags={"\\Seen"} if i % 2 else set()) for i in range(1, n + 1)]


# ── Anmelden ────────────────────────────────────────────────────────────────────────────────────────────
def test_anmelden_und_abmelden(imap_server, konto_von):
    server = imap_server()
    with Sitzung(konto_von(server)) as s:
        assert s.gmail is False
    assert server.verben() == ["LOGIN", "LOGOUT"]
    assert server.verboten == [] and server.intern_fehler == []


@pytest.mark.parametrize("mit", [True, False])
def test_gmail_wird_an_der_erweiterung_erkannt(imap_server, konto_von, mit):
    server = imap_server(gmail=True)
    server.begruessung_mit_faehigkeiten = mit  # False: die Begrüßung nennt keine Fähigkeiten
    with Sitzung(konto_von(server)) as s:
        assert s.gmail is True
    # Das Plugin fragt nie selbst nach den Fähigkeiten, höchstens ``imaplib``: bis Python 3.13 immer einmal, ab 3.14 nur,
    # wenn die Begrüßung sie nicht nennt. Mehr als einmal wäre eine eigene Abfrage; ohne Fähigkeiten in der Begrüßung
    # kommt die Antwort genau einmal.
    abfragen = server.verben(mit_faehigkeiten=True).count("CAPABILITY")
    assert abfragen <= 1 and (mit or abfragen == 1)


def test_falsches_passwort_ist_verstaendlich_und_ohne_passwort(imap_server, konto_von):
    server = imap_server()
    server.login_text = "NO login failed for alex with password geheim-falsch"  # ein Server, der das Passwort zurückspiegelt
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(server, passwort="geheim-falsch")):
        pass
    text = str(e.value)
    assert e.value.art == "anmeldung" and e.value.status == 401
    assert "App-Passwort" in text and "abgelehnt" in text
    assert "geheim-falsch" not in text and "***" in text  # das Passwort ist überall ersetzt


def test_anmeldung_gesperrt_wird_vor_dem_senden_erkannt(imap_server, konto_von):
    server = imap_server(login_gesperrt=True)
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(server)):
        pass
    assert "LOGINDISABLED" in str(e.value) and e.value.status == 401
    assert "LOGIN" not in server.verben()  # nicht einmal der Versuch


def test_sonderzeichen_im_passwort(imap_server, konto_von):
    server = imap_server(passwort='a"b\\c d')
    with Sitzung(konto_von(server)):
        pass
    assert "LOGIN" in server.verben()


def test_passwort_mit_umlauten_geht_ueber_authenticate_plain(imap_server, konto_von):
    server = imap_server(passwort="pässwörd")
    with Sitzung(konto_von(server)):
        pass
    assert server.verben() == ["AUTHENTICATE", "LOGOUT"]


def test_nutzer_wird_in_anfuehrungszeichen_gesendet(imap_server, konto_von):
    server = imap_server(nutzer="alex@example.org")
    with Sitzung(konto_von(server)):
        pass
    assert server.zeilen("LOGIN") == ['LOGIN "alex@example.org" ***']  # das Passwort steht nie im Protokoll der Attrappe


def test_sitzung_ist_nach_dem_schliessen_nicht_mehr_offen(imap_server, konto_von):
    s = Sitzung(konto_von(imap_server()))
    s.oeffnen()
    s.schliessen()
    s.schliessen()  # zweimal ist harmlos
    with pytest.raises(ImapFehler, match="nicht offen"):
        s.ordner()


# ── Fehler auf dem Weg ──────────────────────────────────────────────────────────────────────────────────
def test_zeitueberschreitung(imap_server, konto_von):
    server = imap_server()
    server.stumm = True
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(server), frist=0.3):
        pass
    assert e.value.art == "zeit" and e.value.status == 503 and "Zeitüberschreitung" in str(e.value)
    assert f"{server.host}:{server.port}" in str(e.value)


def test_verbindung_abgelehnt(imap_server, konto_von):
    server = imap_server()
    konto = konto_von(server)
    server.stop()
    with pytest.raises(ImapFehler) as e, Sitzung(konto):
        pass
    assert e.value.art == "verbindung" and "abgelehnt" in str(e.value)


def test_verbindung_bricht_ab(imap_server, konto_von):
    server = imap_server()
    server.trennt_nach_login = True
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(server)) as s:
        s.ordner()
    # je nachdem, ob das Ende der Verbindung als Dateiende oder als Zurücksetzen ankommt
    assert e.value.art == "verbindung" and ("beendet" in str(e.value) or "ConnectionResetError" in str(e.value))


def test_gesamtfrist_beendet_eine_lange_sitzung(imap_server, konto_von):
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(imap_server()), gesamt=0.0):
        pass
    assert e.value.art == "zeit" and "braucht zu lange" in str(e.value)


def test_serverfehler_bei_ordnerliste(imap_server, konto_von):
    server = imap_server()
    server.fehler_bei["LIST"] = "NO nicht jetzt"
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(server)) as s:
        s.ordner()
    assert e.value.art == "server" and "Ordnerliste" in str(e.value) and "nicht jetzt" in str(e.value)


def _sitzung(host="mail.example.org", passwort="geheim"):
    return Sitzung(Konto(name="Privat", host=host, port=993, nutzer="alex", passwort=passwort))


def test_uebersetzen_zertifikat():
    fehler = ssl.SSLCertVerificationError(1, "certificate verify failed")
    fehler.verify_message = "self-signed certificate"
    e = _sitzung()._uebersetzen(fehler)
    assert e.art == "tls" and "nicht vertrauenswürdig" in str(e) and "self-signed certificate" in str(e)
    assert "SSL_CERT_FILE" in str(e)  # der Weg für eine eigene Zertifizierungsstelle


def test_uebersetzen_falscher_port_fuer_tls():
    fehler = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")
    fehler.reason = "WRONG_VERSION_NUMBER"
    e = _sitzung()._uebersetzen(fehler)
    assert e.art == "tls" and "993 gehört zu imaps, 143 zu starttls" in str(e) and "mail.example.org:993" in str(e)


def test_uebersetzen_anderer_tls_fehler_ohne_hinweis():
    fehler = ssl.SSLError(1, "[SSL: TLSV1_ALERT_PROTOCOL_VERSION] alert")
    fehler.reason = "TLSV1_ALERT_PROTOCOL_VERSION"
    e = _sitzung()._uebersetzen(fehler)
    assert "TLSV1_ALERT_PROTOCOL_VERSION" in str(e) and "Port" not in str(e)


@pytest.mark.parametrize(("ausnahme", "art", "stueck"), [
    (socket.gaierror(-2, "Name or service not known"), "verbindung", "wurde nicht gefunden"),
    (TimeoutError("timed out"), "zeit", "Zeitüberschreitung"),
    (socket.timeout("timed out"), "zeit", "antwortet nicht"),
    (ConnectionRefusedError(61, "refused"), "verbindung", "abgelehnt"),
    (imaplib.IMAP4.abort("socket error: EOF"), "verbindung", "beendet"),
    (ConnectionResetError(54, "reset"), "verbindung", "ConnectionResetError"),
    (OSError(113, "no route"), "verbindung", "Netzwerkfehler"),
    (KeyError("x"), "server", "Unerwarteter Fehler bei mail.example.org:993 (KeyError)"),
])
def test_uebersetzen_netzfehler(ausnahme, art, stueck):
    e = _sitzung()._uebersetzen(ausnahme)
    assert isinstance(e, ImapFehler) and e.art == art and stueck in str(e) and e.status in (502, 503)


def test_uebersetzen_serverfehler_und_zu_grosse_antwort():
    s = _sitzung()
    e = s._uebersetzen(imaplib.IMAP4.error("SEARCH command error: BAD [b'Parse error']"))
    assert e.art == "server" and "Parse error" in str(e)
    e = s._uebersetzen(imaplib.IMAP4.error("got more than 1000000 bytes"))
    assert e.status == 413 and "eingrenzen" in str(e)


def test_uebersetzen_streicht_das_passwort_aus_jedem_servertext():
    e = _sitzung(passwort="top-secret")._uebersetzen(imaplib.IMAP4.error("bad password top-secret for alex"), anmeldung=True)
    assert "top-secret" not in str(e) and "***" in str(e)
    e = _sitzung(passwort="top-secret")._uebersetzen(imaplib.IMAP4.error(b"also top-secret"), anmeldung=False)
    assert "top-secret" not in str(e)


def test_servertext_wird_gekuerzt_und_von_steuerzeichen_befreit():
    e = _sitzung()._uebersetzen(imaplib.IMAP4.error("a\r\nb\x00c " + "x" * 500), anmeldung=True)
    assert "\r" not in str(e) and "\n" not in str(e) and "\x00" not in str(e)
    assert len(str(e)) < 600 and "…" in str(e)


# ── Ordner ──────────────────────────────────────────────────────────────────────────────────────────────
def ordnerliste():
    return [
        Ordner("INBOX", nachrichten(3)),
        Ordner("Sent", [], attribute=("\\Sent",)),
        Ordner("Entw&APw-rfe", [], attribute=("\\Drafts",)),
        Ordner("Papierkorb", []),
        Ordner("Archiv", []),
        Ordner("Verborgen", [], attribute=("\\Noselect",)),
    ]


def test_ordner_rollen_und_namen(imap_server, konto_von):
    with Sitzung(konto_von(imap_server(ordnerliste()))) as s:
        assert [o.name for o in s.ordner()] == ["INBOX", "Sent", "Entwürfe", "Papierkorb", "Archiv"]  # ohne \Noselect
        assert s.ordner_fuer("inbox") == "INBOX"
        assert s.ordner_fuer("sent") == "Sent"  # nach dem Attribut
        assert s.ordner_fuer("drafts") == "Entwürfe"
        assert s.ordner_fuer("trash") == "Papierkorb"  # nach dem Namen, ohne Attribut
        assert s.ordner_fuer("spam") is None
        assert s.rolle_von("Entwürfe") == "drafts" and s.rolle_von("INBOX") == "inbox" and s.rolle_von("inbox") == "inbox"
        assert s.rolle_von("Papierkorb") == "trash" and s.rolle_von("Archiv") == ""
        assert s.ordner_namen() == "INBOX, Sent, Entwürfe, Papierkorb, Archiv"


def test_ordnerliste_nur_einmal_gefragt(imap_server, konto_von):
    server = imap_server(ordnerliste())
    with Sitzung(konto_von(server)) as s:
        s.ordner()
        s.ordner_fuer("sent")
        s.rolle_von("Archiv")
    assert server.verben().count("LIST") == 1


def test_ordner_namen_als_literal(imap_server, konto_von):
    server = imap_server(ordnerliste())
    server.liste_als_literal = True
    with Sitzung(konto_von(server)) as s:
        assert "Entwürfe" in [o.name for o in s.ordner()]


def test_ordnername_mit_trenner_nimmt_den_letzten_teil_fuer_die_rolle(imap_server, konto_von):
    server = imap_server([Ordner("INBOX"), Ordner("[Gmail]/Gesendet")])
    with Sitzung(konto_von(server)) as s:
        assert s.ordner_fuer("sent") == "[Gmail]/Gesendet"


def test_waehle_oeffnet_nur_lesend(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", nachrichten(3), gueltig=4711, naechste=99)])
    with Sitzung(konto_von(server)) as s:
        assert s.waehle("INBOX") == 4711
        assert s.uidnext == 99 and s.ordner_offen == "INBOX"
    assert "EXAMINE" in server.verben() and "SELECT" not in server.verben()
    assert server.verboten == []


def test_waehle_sendet_umlautnamen_als_imap_utf7(imap_server, konto_von):
    server = imap_server(ordnerliste())
    with Sitzung(konto_von(server)) as s:
        s.waehle("Entwürfe")
    assert server.zeilen("EXAMINE") == ['EXAMINE "Entw&APw-rfe"']


def test_waehle_fehlender_ordner_nennt_die_vorhandenen(imap_server, konto_von):
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(imap_server(ordnerliste()))) as s:
        s.waehle("Gibtsnicht")
    assert e.value.art == "ordner" and e.value.status == 404
    assert "'Gibtsnicht'" in str(e.value) and "Entwürfe" in str(e.value) and "Privat" in str(e.value)


def test_waehle_ohne_uidvalidity_ist_ein_fehler(imap_server, konto_von):
    # ohne Gültigkeit der UIDs ließe sich keine Kennung bauen, die eine spätere Neuordnung des Ordners überlebt
    server = imap_server([Ordner("INBOX", nachrichten(1), gueltig=None)])
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
    assert e.value.art == "server" and "UIDVALIDITY" in str(e.value)


def test_ungelesen_zaehlt_den_posteingang_ohne_ihn_zu_waehlen(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", nachrichten(5))])  # gerade Nummern ungelesen: 2 und 4
    with Sitzung(konto_von(server)) as s:
        assert s.ungelesen() == 2
    assert server.zeilen("STATUS") == ['STATUS "INBOX" (UNSEEN)'] and "EXAMINE" not in server.verben()


def test_ungelesen_ohne_antwort_ist_none(imap_server, konto_von):
    server = imap_server()
    server.fehler_bei["STATUS"] = "NO geht nicht"
    with Sitzung(konto_von(server)) as s:
        assert s.ungelesen() is None


# ── Suchen und Holen ────────────────────────────────────────────────────────────────────────────────────
def postfach_mit_absendern():
    return [
        Nachricht(1, mail(von="Erika Beispiel <erika@example.org>", betreff="Rechnung Oktober")),
        Nachricht(2, mail(von="Jürgen Müller <jm@example.net>", betreff="Grüße aus Köln")),
        Nachricht(3, mail(von="Erika Beispiel <erika@example.org>", betreff="Einladung")),
        Nachricht(4, mail(von="Jürgen Müller <jm@example.net>", betreff="Rechnung Köln", html=None)),
        Nachricht(5, mail(von="Info <info@example.com>", betreff="Newsletter"), flags={"\\Deleted"}),
    ]


def test_suchen_mit_ascii(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", postfach_mit_absendern())])
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        assert s.suchen(imap_gruppen(planen("from:erika subject:rechnung"), JETZT.date())) == [1]
        assert s.suchen(imap_gruppen(planen("in:inbox"), JETZT.date())) == [4, 3, 2, 1]  # neueste zuerst, ohne \Deleted
    assert server.zeilen("UID SEARCH")[0] == 'UID SEARCH UNDELETED FROM "erika" SUBJECT "rechnung"'


def test_suchen_mit_umlaut_geht_als_literal(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", postfach_mit_absendern())])
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        assert s.suchen(imap_gruppen(planen("from:Müller"), JETZT.date())) == [4, 2]
    assert server.zeilen("UID SEARCH") == ["UID SEARCH CHARSET UTF-8 UNDELETED FROM <<'Müller'>>"]


def test_suchen_mit_zwei_umlautwoertern_schneidet_die_ergebnisse(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", postfach_mit_absendern())])
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        gruppen = imap_gruppen(planen("from:Müller subject:Grüße"), JETZT.date())
        assert len(gruppen) == 2
        assert s.suchen(gruppen) == [2]
    assert len(server.zeilen("UID SEARCH")) == 2  # ein Befehl je Wort mit Umlaut: ein Befehl trägt nur ein Literal am Ende


def test_suchen_ohne_treffer_in_der_ersten_gruppe_fragt_nicht_weiter(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", postfach_mit_absendern())])
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        assert s.suchen(imap_gruppen(planen("from:Nirgendwö subject:Grüße"), JETZT.date())) == []
    assert len(server.zeilen("UID SEARCH")) == 1


def test_suchen_in_einem_uid_bereich(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", postfach_mit_absendern())])
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        assert s.suchen([Gruppe(("UNDELETED",))], ab=3) == [4, 3]
    assert server.zeilen("UID SEARCH") == ["UID SEARCH UID 3:* UNDELETED"]


def test_suche_mit_literal_und_bereich_haengt_das_literal_ans_ende(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", postfach_mit_absendern())])
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        assert s.suchen(imap_gruppen(planen("from:Müller"), JETZT.date()), ab=3) == [4]
    assert server.zeilen("UID SEARCH") == ["UID SEARCH CHARSET UTF-8 UID 3:* UNDELETED FROM <<'Müller'>>"]


def test_neueste_weitet_das_fenster_aus(imap_server, konto_von, monkeypatch):
    monkeypatch.setattr(imap, "FENSTER_UIDS", 5)
    mails = [Nachricht(i, mail(von="Selten <selten@example.org>" if i == 3 else "Viel <viel@example.org>")) for i in range(1, 41)]
    server = imap_server([Ordner("INBOX", mails)])  # UIDNEXT = 41
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        assert s.neueste(imap_gruppen(planen("from:selten"), JETZT.date()), 5) == [3]
    suchen = [z.split(" UNDELETED")[0] for z in server.zeilen("UID SEARCH")]
    assert suchen == ["UID SEARCH UID 36:*", "UID SEARCH UID 16:*", "UID SEARCH UID 1:*"]


def test_neueste_bleibt_im_ersten_fenster_wenn_es_reicht(imap_server, konto_von, monkeypatch):
    monkeypatch.setattr(imap, "FENSTER_UIDS", 10)
    server = imap_server([Ordner("INBOX", [Nachricht(i, mail()) for i in range(1, 51)])])
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        assert s.neueste([Gruppe(("UNDELETED",))], 5) == [50, 49, 48, 47, 46]
    assert server.zeilen("UID SEARCH") == ["UID SEARCH UID 41:* UNDELETED"]


def test_neueste_ohne_uidnext_sucht_im_ganzen_ordner(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", [Nachricht(i, mail()) for i in range(1, 8)])])
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        s.uidnext = None
        assert s.neueste([Gruppe(("UNDELETED",))], 3) == [7, 6, 5]
    assert server.zeilen("UID SEARCH") == ["UID SEARCH UNDELETED"]


def test_zu_grosse_suchantwort_wird_ein_verstaendlicher_fehler(imap_server, konto_von, monkeypatch):
    monkeypatch.setattr(imaplib, "_MAXLINE", 200)
    server = imap_server([Ordner("INBOX", [Nachricht(i, mail()) for i in range(1, 400)])])
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        s.suchen([Gruppe(("UNDELETED",))])
    assert e.value.status == 413 and "eingrenzen" in str(e.value)


def test_suche_abgelehnt(imap_server, konto_von):
    server = imap_server()
    server.fehler_bei["UID SEARCH"] = "NO Suche geht nicht"
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        s.suchen([Gruppe(("ALL",))])
    assert e.value.art == "server"


def test_holen_liefert_kopf_aufbau_und_kennzeichen(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", [Nachricht(7, mail(betreff="Guten Tag", text="Hallo Welt"), flags={"\\Seen"})])])
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        (a,) = s.holen([7], "UID FLAGS INTERNALDATE RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]")
    assert a["UID"] == 7 and a["FLAGS"] == ["\\Seen"] and a["RFC822.SIZE"] > 100
    assert a["INTERNALDATE"] == "10-Oct-2026 11:00:00 +0000"
    assert b"Subject: Guten Tag" in a["BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]"]
    assert a["BODYSTRUCTURE"][0] == b"TEXT" and a["BODYSTRUCTURE"][1] == b"PLAIN"
    assert server.zeilen("UID FETCH") == [
        "UID FETCH 7 (UID FLAGS INTERNALDATE RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])"]
    assert server.verboten == []


def test_teile_holt_den_anfang_eines_abschnitts(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", [Nachricht(1, mail(text="0123456789" * 10, cte="7bit"))])])
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        assert s.teile([1], "1", 25) == {1: ("0123456789" * 10).encode()[:25]}
        assert len(s.teile([1], "1", 1000)[1]) >= 100
    assert server.zeilen("UID FETCH")[0] == "UID FETCH 1 (UID BODY.PEEK[1]<0.25>)"


def test_unaufgeforderte_antworten_ohne_uid_stoeren_nicht(imap_server, konto_von):
    server = imap_server([Ordner("INBOX", nachrichten(3))])
    server.unaufgefordert = True
    with Sitzung(konto_von(server)) as s:
        s.waehle("INBOX")
        antworten = s.holen([1, 2, 3], "UID FLAGS")
    assert [a["UID"] for a in antworten] == [1, 2, 3]


def test_holen_von_nicht_vorhandenen_uids_liefert_nichts(imap_server, konto_von):
    with Sitzung(konto_von(imap_server([Ordner("INBOX", nachrichten(2))]))) as s:
        s.waehle("INBOX")
        assert s.holen([99], "UID FLAGS") == []


# ── Nur lesen ───────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("items", [
    "BODY[1]", "BODY[]", "BODY[HEADER]", "RFC822", "RFC822.TEXT", "RFC822.HEADER", "BODY.PEEK[]", "BODY.PEEK[TEXT]",
    "UID FLAGS BODY[1]", "UID; STORE", "BODY.PEEK[1] (RFC822)",
])
def test_fetch_angaben_die_den_gelesen_status_aendern_koennten_werden_nie_gesendet(items):
    with pytest.raises(AssertionError, match="FETCH-Angabe nicht erlaubt"):
        imap._items_pruefen(items)


@pytest.mark.parametrize("items", [
    "UID", "UID FLAGS INTERNALDATE RFC822.SIZE BODYSTRUCTURE X-GM-LABELS", "BODY.PEEK[1]", "BODY.PEEK[1.2]<0.2048>",
    "BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]",
])
def test_lesende_fetch_angaben_sind_erlaubt(items):
    imap._items_pruefen(items)


@pytest.mark.parametrize("befehl", ["STORE", "COPY", "MOVE", "EXPUNGE", "APPEND", "DELETE", "CREATE", "RENAME", "SELECT", "CLOSE"])
def test_uid_befehle_ausser_suchen_und_holen_gibt_es_nicht(imap_server, konto_von, befehl):
    server = imap_server()
    with Sitzung(konto_von(server)) as s, pytest.raises(AssertionError, match="nicht erlaubt"):
        s._uid(befehl, "1", "+FLAGS", "(\\Seen)")
    assert "UID" not in " ".join(server.verben())  # nichts davon ging über die Leitung


def test_holen_prueft_uids_und_angaben(imap_server, konto_von):
    with Sitzung(konto_von(imap_server())) as s:
        with pytest.raises(AssertionError):
            s.holen([], "UID")
        with pytest.raises(AssertionError):
            s.holen([0], "UID")
        with pytest.raises(AssertionError):
            s.holen(["1"], "UID")  # type: ignore[list-item]
        with pytest.raises(AssertionError):
            s.holen([1], "BODY[1]")
