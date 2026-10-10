"""Konten: was der Besitzer einträgt und wie das Plugin es liest."""

from __future__ import annotations

import json

import pytest

from fyos_plugin_mail.konten import (
    KONTEN_MAX,
    NAME_MAX,
    ORDNER_MAX,
    Konto,
    kennung_von,
    konten_lesen,
    ordner_kennung,
)

PASSWORT = "S3cr3t-Pä$$wort"


def einzel(**umgebung):
    basis = {"FYOS_MAIL_HOST": "imap.example.org", "FYOS_MAIL_NUTZER": "alex@example.org", "FYOS_MAIL_PASSWORT": PASSWORT}
    basis.update(umgebung)
    return konten_lesen({k: v for k, v in basis.items() if v is not None})


def nur(konten):
    assert len(konten) == 1, konten
    return konten[0]


# ── Ein Konto aus der Umgebung ──────────────────────────────────────────────────────────────────────────
def test_ein_konto_mit_den_vorgaben():
    k = nur(einzel())
    assert (k.name, k.host, k.port, k.sicherheit, k.nutzer, k.ordner, k.fehler) == (
        "example.org", "imap.example.org", 993, "imaps", "alex@example.org", ("INBOX",), "")
    assert k.passwort == PASSWORT and k.adresse == "imap.example.org:993"


def test_ohne_host_gibt_es_kein_konto():
    assert konten_lesen({}) == []
    assert konten_lesen({"FYOS_MAIL_NUTZER": "alex", "FYOS_MAIL_PASSWORT": "x"}) == []
    assert konten_lesen({"FYOS_MAIL_HOST": "   "}) == []


@pytest.mark.parametrize(("host", "name"), [
    ("imap.example.org", "example.org"), ("mail.example.org", "example.org"), ("imap.gmail.com", "gmail.com"),
    ("imap.mail.me.com", "mail.me.com"), ("secure.example.org", "example.org"), ("imap.example", "imap.example"),
    ("localhost", "localhost"), ("127.0.0.1", "127.0.0.1"), ("EXAMPLE.ORG", "example.org"),
])
def test_der_name_kommt_vom_host_wenn_keiner_angegeben_ist(host, name):
    assert nur(einzel(FYOS_MAIL_HOST=host)).name == name


def test_name_ordner_und_port_aus_der_umgebung():
    k = nur(einzel(FYOS_MAIL_NAME="  Privat  Mail ", FYOS_MAIL_PORT="1993", FYOS_MAIL_ORDNER="inbox, Archiv ,Rechnungen/2026,,Archiv"))
    assert k.name == "Privat Mail" and k.port == 1993 and k.sicherheit == "imaps"
    assert k.ordner == ("INBOX", "Archiv", "Rechnungen/2026")  # der Posteingang heißt immer INBOX, Doppelte fallen weg


@pytest.mark.parametrize(("sicherheit", "port", "erwartet_art", "erwartet_port"), [
    (None, None, "imaps", 993), ("", "", "imaps", 993), ("imaps", None, "imaps", 993), ("IMAPS", "1993", "imaps", 1993),
    ("ssl", None, "imaps", 993), ("tls", None, "imaps", 993), ("starttls", None, "starttls", 143),
    ("STARTTLS", "1143", "starttls", 1143), (None, "143", "starttls", 143), ("imaps", "143", "imaps", 143),
])
def test_sicherheit_und_port(sicherheit, port, erwartet_art, erwartet_port):
    k = nur(einzel(FYOS_MAIL_SICHERHEIT=sicherheit, FYOS_MAIL_PORT=port))
    assert (k.sicherheit, k.port) == (erwartet_art, erwartet_port)


@pytest.mark.parametrize("wert", ["none", "keine", "klartext", "plain", "plaintext", "unverschlüsselt", "no", "nein", "NONE"])
def test_unverschluesselt_gibt_es_nicht(wert):
    k = nur(einzel(FYOS_MAIL_SICHERHEIT=wert))
    assert "unverschlüsselte Verbindungen gibt es nicht" in k.fehler and k.host == ""


@pytest.mark.parametrize(("feld", "wert", "teil_des_grundes"), [
    ("FYOS_MAIL_SICHERHEIT", "irgendwas", "imaps oder starttls"),
    ("FYOS_MAIL_PORT", "abc", "port muss eine Zahl sein"),
    ("FYOS_MAIL_PORT", "0", "zwischen 1 und 65535"),
    ("FYOS_MAIL_PORT", "65536", "zwischen 1 und 65535"),
    ("FYOS_MAIL_PORT", "-1", "zwischen 1 und 65535"),
    ("FYOS_MAIL_HOST", "imaps://imap.example.org", "ohne imaps://"),
    ("FYOS_MAIL_HOST", "imap.example.org:993", "Port gehört in port"),
    ("FYOS_MAIL_HOST", "imap.example.org/INBOX", "kein gültiger Servername"),
    ("FYOS_MAIL_HOST", "imap example.org", "kein gültiger Servername"),
    ("FYOS_MAIL_HOST", "imap.exämple.org", "Punycode"),
    ("FYOS_MAIL_HOST", "-imap.example.org", "kein gültiger Servername"),
    ("FYOS_MAIL_NUTZER", "", "nutzer fehlt"),
    ("FYOS_MAIL_NUTZER", "a\nb", "Steuerzeichen"),
    ("FYOS_MAIL_NUTZER", "x" * 201, "zu lang"),
    ("FYOS_MAIL_PASSWORT", "", "FYOS_MAIL_PASSWORT"),
    ("FYOS_MAIL_PASSWORT", "ge\nheim", "Steuerzeichen oder einen Zeilenumbruch"),
    ("FYOS_MAIL_NAME", "a:b", "unzulässiges Zeichen"),
    ("FYOS_MAIL_NAME", "a/b", "unzulässiges Zeichen"),
    ("FYOS_MAIL_NAME", "x" * (NAME_MAX + 1), "länger als"),
    ("FYOS_MAIL_ORDNER", "a\nb", "Steuerzeichen"),
    ("FYOS_MAIL_ORDNER", ",".join(f"Ordner{i}" for i in range(ORDNER_MAX + 1)), "höchstens"),
])
def test_ein_unbrauchbarer_eintrag_wird_ein_konto_mit_grund(feld, wert, teil_des_grundes):
    k = nur(einzel(**{feld: wert}))
    assert teil_des_grundes in k.fehler, k.fehler
    assert k.name == "FYOS_MAIL_HOST" and k.host == "" and k.passwort == ""  # nichts davon bleibt benutzbar
    assert PASSWORT not in k.fehler


def test_ipv6_adressen():
    assert nur(einzel(FYOS_MAIL_HOST="::1")).adresse == "[::1]:993"
    assert nur(einzel(FYOS_MAIL_HOST="[2001:db8::1]")).host == "2001:db8::1"
    assert "weder ein Servername noch eine IPv6-Adresse" in nur(einzel(FYOS_MAIL_HOST="2001:db8::g")).fehler


def test_leerraum_um_die_werte_stoert_nicht():
    k = nur(einzel(FYOS_MAIL_HOST="  imap.example.org  ", FYOS_MAIL_NUTZER="  alex@example.org ", FYOS_MAIL_PASSWORT=f"  {PASSWORT}\n"))
    assert k.host == "imap.example.org" and k.nutzer == "alex@example.org" and k.passwort == PASSWORT


# ── Das Passwort bleibt geheim ──────────────────────────────────────────────────────────────────────────
def test_das_passwort_steht_in_keiner_darstellung():
    k = nur(einzel())
    for text in (repr(k), str(k), json.dumps(k.als_dict()), str(k.als_dict()), k.adresse):
        assert PASSWORT not in text
    assert "passwort" not in k.als_dict()


@pytest.mark.parametrize("feld", ["FYOS_MAIL_PORT", "FYOS_MAIL_HOST", "FYOS_MAIL_SICHERHEIT", "FYOS_MAIL_ORDNER", "FYOS_MAIL_NUTZER"])
def test_auch_fehlermeldungen_nennen_das_passwort_nie(feld):
    k = nur(einzel(**{feld: "ungültig\n!" if feld != "FYOS_MAIL_PORT" else "abc"}))
    assert k.fehler and PASSWORT not in k.fehler and PASSWORT not in json.dumps(k.als_dict())


def test_als_dict():
    k = nur(einzel(FYOS_MAIL_ORDNER="INBOX,Archiv"))
    assert k.als_dict() == {
        "name": "example.org", "art": "imap", "adresse": "imap.example.org:993", "sicherheit": "imaps",
        "nutzer": "alex@example.org", "ordner": ["INBOX", "Archiv"],
    }
    kaputt = nur(einzel(FYOS_MAIL_PORT="x"))
    assert kaputt.als_dict() == {"name": "FYOS_MAIL_HOST", "art": "imap", "fehler": kaputt.fehler}


# ── Weitere Konten als JSON ─────────────────────────────────────────────────────────────────────────────
def konten_json(eintraege, **umgebung):
    umgebung.setdefault("FYOS_MAIL_PW_A", "passwort-a")
    umgebung.setdefault("FYOS_MAIL_PW_B", "passwort-b")
    return konten_lesen({"FYOS_MAIL_KONTEN": json.dumps(eintraege), **umgebung})


def eintrag(**felder):
    return {"name": "Arbeit", "host": "imap.example.net", "nutzer": "alex", "passwort_env": "FYOS_MAIL_PW_A", **felder}


def test_mehrere_konten_aus_json():
    a, b = konten_json([eintrag(), eintrag(name="Verein", host="mail.example.com", port=143, nutzer="vorstand",
                                           passwort_env="FYOS_MAIL_PW_B", ordner=["INBOX", "Archiv"])])
    assert (a.name, a.host, a.port, a.sicherheit, a.passwort, a.fehler) == ("Arbeit", "imap.example.net", 993, "imaps", "passwort-a", "")
    assert (b.name, b.host, b.port, b.sicherheit, b.passwort, b.ordner, b.fehler) == (
        "Verein", "mail.example.com", 143, "starttls", "passwort-b", ("INBOX", "Archiv"), "")


def test_reihenfolge_umgebung_dann_plugin_konfiguration_dann_json():
    umgebung = {
        "FYOS_MAIL_HOST": "imap.example.org", "FYOS_MAIL_NUTZER": "a", "FYOS_MAIL_PASSWORT": "x", "FYOS_MAIL_NAME": "Eins",
        "FYOS_MAIL_PW_A": "pa", "FYOS_MAIL_PW_B": "pb",
        "FYOS_MAIL_KONTEN": json.dumps([eintrag(name="Drei", passwort_env="FYOS_MAIL_PW_B")]),
    }
    konten = konten_lesen(umgebung, {"konten": [eintrag(name="Zwei")]})
    assert [k.name for k in konten] == ["Eins", "Zwei", "Drei"] and not any(k.fehler for k in konten)


@pytest.mark.parametrize(("aenderung", "teil_des_grundes"), [
    ({"passwort": "geheim"}, "Passwörter gehören nicht in die Liste"),
    ({"password": "geheim"}, "Passwörter gehören nicht in die Liste"),
    ({"passwort_env": None}, "passwort_env fehlt"),
    ({"passwort_env": ""}, "passwort_env fehlt"),
    ({"passwort_env": "nicht gesetzt!"}, "Name einer Umgebungsvariablen"),
    ({"passwort_env": "FYOS_MAIL_LEER"}, "nicht gesetzt oder leer"),
    ({"passwort_env": 5}, "Name einer Umgebungsvariablen"),
    ({"fremd": 1, "noch": 2}, "unbekannte Felder: fremd, noch"),
    ({"name": ""}, "name fehlt"),
    ({"name": "a:b"}, "unzulässiges Zeichen"),
    ({"host": ""}, "host fehlt"),
    ({"port": "abc"}, "port muss eine Zahl sein"),
    ({"port": True}, "port muss eine Zahl sein"),
    ({"nutzer": ""}, "nutzer fehlt"),
    ({"ordner": "nicht-liste", "sicherheit": 5}, "sicherheit muss imaps oder starttls sein"),
    ({"ordner": [1, 2]}, "Liste von Ordnernamen"),
    ({"ordner": {"a": 1}}, "Liste von Ordnernamen"),
])
def test_unbrauchbare_json_eintraege(aenderung, teil_des_grundes):
    felder = eintrag()
    for schluessel, wert in aenderung.items():
        if wert is None:
            felder.pop(schluessel)
        else:
            felder[schluessel] = wert
    (k,) = konten_json([felder], FYOS_MAIL_LEER="")
    assert teil_des_grundes in k.fehler, k.fehler
    assert k.host == "" and k.passwort == "" and "geheim" not in k.fehler


def test_der_name_eines_unbrauchbaren_eintrags_bleibt_sichtbar():
    (k,) = konten_json([eintrag(name="Arbeit", host="")])
    assert k.name == "Arbeit" and "host fehlt" in k.fehler
    (k,) = konten_json([{"host": "x"}])  # ohne Namen: wo er steht
    assert k.name == "FYOS_MAIL_KONTEN, Eintrag 1" and "name fehlt" in k.fehler


def test_doppelte_namen_der_zweite_ist_unbrauchbar():
    a, b = konten_json([eintrag(name="Arbeit"), eintrag(name="ARBEIT")])
    assert not a.fehler and b.fehler == "der Name ist schon vergeben" and b.name != a.name and b.name.casefold() != a.name.casefold()


def test_umgebung_und_json_mit_demselben_namen():
    konten = konten_lesen({
        "FYOS_MAIL_HOST": "imap.example.org", "FYOS_MAIL_NUTZER": "a", "FYOS_MAIL_PASSWORT": "x", "FYOS_MAIL_NAME": "Arbeit",
        "FYOS_MAIL_PW_A": "pa", "FYOS_MAIL_KONTEN": json.dumps([eintrag(name="Arbeit")]),
    })
    assert [bool(k.fehler) for k in konten] == [False, True]


@pytest.mark.parametrize("text", ["kein json", "{", "[1, 2", "nichts"])
def test_kaputtes_json_ist_ein_konto_mit_fehler(text):
    (k,) = konten_lesen({"FYOS_MAIL_KONTEN": text})
    assert k.name == "FYOS_MAIL_KONTEN" and "kein gültiges JSON" in k.fehler


@pytest.mark.parametrize("wert", [{"a": 1}, "text", 5, True])
def test_json_das_keine_liste_ist(wert):
    (k,) = konten_lesen({"FYOS_MAIL_KONTEN": json.dumps(wert)})
    assert "muss eine Liste von Konten sein" in k.fehler


def test_eintraege_die_keine_objekte_sind():
    konten = konten_json([5, "x", None, [1]])
    assert len(konten) == 4 and all("Objekt" in k.fehler for k in konten)
    assert len({k.name for k in konten}) == 4  # jeder erscheint unter eigener Bezeichnung


def test_ein_leeres_json_ist_kein_konto():
    assert konten_lesen({"FYOS_MAIL_KONTEN": "[]"}) == [] and konten_lesen({"FYOS_MAIL_KONTEN": "  "}) == []


def test_nicht_mehr_als_die_hoechstzahl_an_konten():
    eintraege = [eintrag(name=f"K{i}") for i in range(KONTEN_MAX + 7)]
    assert len(konten_json(eintraege)) == KONTEN_MAX


def test_plugin_konfiguration_mit_falscher_form():
    (k,) = konten_lesen({}, {"konten": "kein"})
    assert "muss eine Liste von Konten sein" in k.fehler and k.name == "konten in der Plugin-Konfiguration"
    assert konten_lesen({}, {"konten": None}) == [] and konten_lesen({}, {"konten": ""}) == [] and konten_lesen({}, {}) == []


def test_ein_passwort_im_json_steht_in_keiner_meldung():
    (k,) = konten_json([eintrag(passwort="S3cr3t-im-Json")])
    assert "S3cr3t-im-Json" not in k.fehler and "S3cr3t-im-Json" not in repr(k)


# ── Kennungen ───────────────────────────────────────────────────────────────────────────────────────────
def test_kennung_des_kontos_ist_stabil_und_ohne_ruecksicht_auf_gross_und_klein():
    assert kennung_von("Privat") == kennung_von("PRIVAT") == kennung_von("privat") and len(kennung_von("Privat")) == 10
    assert kennung_von("Privat") != kennung_von("Arbeit")
    assert Konto(name="Privat").kennung == kennung_von("Privat")


def test_kennung_des_ordners():
    assert ordner_kennung("INBOX") == ordner_kennung("inbox") == ordner_kennung("Inbox")  # der Posteingang heißt auf jedem Server so
    assert ordner_kennung("Archiv") != ordner_kennung("archiv")  # alle anderen unterscheiden Groß- und Kleinschreibung
    assert len(ordner_kennung("Entwürfe")) == 10
    assert all(c in "0123456789abcdef" for c in ordner_kennung("Entwürfe"))
