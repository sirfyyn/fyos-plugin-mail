"""fyos-post-einrichten: gegen eine zustandsbehaftete Attrappe von Listmonk (keine echte Instanz, kein Netz außer 127.0.0.1).

Geprüft wird: der Trockenlauf ändert nichts (außer der Anmeldesitzung), ``--ja`` richtet alles ein, ein zweiter Lauf ändert
nichts, die Token landen nur in der Datei (0600, nie überschrieben) und nie in der Ausgabe, und die Vorlage hält den Vertrag
mit dem Plugin.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fyos_plugin_mail import post, post_einrichten as ein
from fyos_plugin_mail.post_einrichten import Antwort, Fehler, Listmonk

ADMIN = "admin-konto"
PASSWORT = "admin-passwort-GEHEIM-0123"


def _daten(d, status=200):
    return Antwort(status, [], json.dumps({"data": d}, ensure_ascii=False).encode())


def _nein(status, text):
    return Antwort(status, [], json.dumps({"message": text}).encode())


class FalschesListmonk:
    """Ein Listmonk mit Gedächtnis: Listen, Abonnenten, Vorlagen, Rollen, Benutzer. Als ``Transport`` einsetzbar."""

    def __init__(self):
        self.anfragen: list[tuple[str, str, object]] = []
        self.listen: list[dict] = []
        self.abonnenten: list[dict] = []
        self.vorlagen: list[dict] = [{"id": 1, "name": "Default campaign template", "type": "campaign", "subject": "", "body": "x"}]
        self.rollen: list[dict] = []
        self.benutzer: list[dict] = []
        self.token: dict[str, str] = {}  # Benutzername -> Token
        self.naechste = 100
        self.bereit_ab = 0  # so viele /health-Anfragen scheitern zuerst
        self.login_ok = True

    def _id(self):
        self.naechste += 1
        return self.naechste

    def aenderungen(self):
        return [a for a in self.anfragen if a[0] in ("POST", "PUT", "DELETE") and a[1] != "/admin/login"]

    def __call__(self, methode, pfad, kopf, koerper):
        teile = urllib.parse.urlsplit(pfad)
        p, abfrage = teile.path, urllib.parse.parse_qs(teile.query)
        daten = json.loads(koerper.decode()) if koerper and kopf.get("Content-Type") == "application/json" else None
        self.anfragen.append((methode, p, daten if daten is not None else koerper))
        if p == "/health":
            if self.bereit_ab > 0:
                self.bereit_ab -= 1
                raise ConnectionRefusedError("noch nicht")
            return _daten(True)
        if p == "/admin/login":
            form = urllib.parse.parse_qs(koerper.decode())
            if self.login_ok and form.get("username") == [ADMIN] and form.get("password") == [PASSWORT]:
                return Antwort(302, ["session=abc123; Path=/; HttpOnly"], b"")
            return Antwort(200, [], b"<html>Login</html>")
        auth = kopf.get("Authorization", "")
        if auth.startswith("token "):
            benutzer, _, tok = auth[6:].partition(":")
            if self.token.get(benutzer) != tok:
                return _nein(403, "invalid API credentials")
            if p == "/api/profile":
                return _daten({"username": benutzer})
            return _nein(403, "permission denied")
        if "session=abc123" not in kopf.get("Cookie", ""):
            return _nein(403, "invalid session")
        if p == "/api/about":
            return _daten({"version": "v6.2.0"})
        if p == "/api/lists" and methode == "GET":
            return _daten({"results": self.listen})
        if p == "/api/lists" and methode == "POST":
            neu = {"id": self._id(), **daten}
            self.listen.append(neu)
            return _daten(neu)
        if p == "/api/subscribers" and methode == "GET":
            such = abfrage.get("search", [""])[0]
            return _daten({"results": [s for s in self.abonnenten if such in s["email"]]})
        if p == "/api/subscribers" and methode == "POST":
            neu = {
                "id": self._id(),
                "email": daten["email"],
                "name": daten["name"],
                "status": daten["status"],
                "lists": [{"id": i, "subscription_status": "confirmed" if daten.get("preconfirm_subscriptions") else "unconfirmed"} for i in daten["lists"]],
            }
            self.abonnenten.append(neu)
            return _daten(neu)
        if p == "/api/subscribers/lists" and methode == "PUT":
            for s in self.abonnenten:
                if s["id"] in daten["ids"]:
                    s["lists"] = [l for l in s["lists"] if l["id"] not in daten["target_list_ids"]]
                    s["lists"] += [{"id": i, "subscription_status": daten["status"]} for i in daten["target_list_ids"]]
            return _daten(True)
        if p == "/api/templates" and methode == "GET":
            return _daten([{k: v for k, v in t.items() if k != "body"} for t in self.vorlagen])
        if p == "/api/templates" and methode == "POST":
            neu = {"id": self._id(), **daten}
            self.vorlagen.append(neu)
            return _daten(neu)
        m = re.fullmatch(r"/api/templates/(\d+)", p)
        if m:
            vorlage = next(t for t in self.vorlagen if t["id"] == int(m[1]))
            if methode == "GET":
                return _daten(vorlage)
            vorlage.update(daten)
            return _daten(vorlage)
        if p == "/api/roles/users" and methode == "GET":
            return _daten(self.rollen)
        if p == "/api/roles/users" and methode == "POST":
            neu = {"id": self._id(), **daten}
            self.rollen.append(neu)
            return _daten(neu)
        m = re.fullmatch(r"/api/roles/users/(\d+)", p)
        if m and methode == "PUT":
            next(r for r in self.rollen if r["id"] == int(m[1])).update(daten)
            return _daten(True)
        if p == "/api/users" and methode == "GET":
            return _daten(self.benutzer)
        if p == "/api/users" and methode == "POST":
            tok = f"tok-{daten['username']}-{self._id()}-GEHEIM"
            rolle = next((r for r in self.rollen if r["id"] == daten["user_role_id"]), None)
            neu = {"id": self._id(), "username": daten["username"], "name": daten["name"], "type": daten["type"], "status": daten["status"],
                   "user_role": {"id": daten["user_role_id"], "name": rolle["name"] if rolle else None}}
            self.benutzer.append(neu)
            self.token[daten["username"]] = tok
            return _daten({**neu, "password": tok})
        m = re.fullmatch(r"/api/users/(\d+)", p)
        if m and methode == "DELETE":
            ziel = next(u for u in self.benutzer if u["id"] == int(m[1]))
            self.benutzer.remove(ziel)
            self.token.pop(ziel["username"], None)
            return _daten(True)
        if m and methode == "PUT":
            u = next(u for u in self.benutzer if u["id"] == int(m[1]))
            u.update({k: daten[k] for k in ("name", "type", "status")})
            u["user_role"] = {"id": daten["user_role_id"]}
            return _daten(u)
        raise AssertionError(f"unerwartet: {methode} {pfad}")


@pytest.fixture
def lm():
    return FalschesListmonk()


def zeilen():
    gesammelt: list[str] = []
    return gesammelt, gesammelt.append


def lauf(lm, ja=True, zugaenge=None, **kw):
    """Anmelden und einrichten wie `main`, mit der Attrappe als Transport; gibt (Exit, Ausgabezeilen) zurück."""
    z, aus = zeilen()
    c = Listmonk(lm, ja=ja)
    c.anmelden(ADMIN, PASSWORT)
    code = ein.einrichten(c, lm, zugaenge if zugaenge is not None else {"fyos": "fyos"}, ausgabe=aus, **kw)
    return code, z


def tokens_aus(pfad) -> dict[str, str]:
    return dict(zeile.split("=", 1) for zeile in pfad.read_text(encoding="utf-8").splitlines())


# ============================================================================================ Trockenlauf
def test_trockenlauf_aendert_nichts(lm, tmp_path):
    code, aus = lauf(lm, ja=False, abonnent="ada@example.org")
    assert code == 0
    assert lm.aenderungen() == []  # nur die Anmeldung ist ein POST
    assert [a for a in lm.anfragen if a[0] == "POST"] == [("POST", "/admin/login", lm.anfragen[0][2])]
    assert lm.listen == [] and lm.abonnenten == [] and lm.rollen == [] and lm.benutzer == []
    assert len(lm.vorlagen) == 1
    text = "\n".join(aus)
    assert "Trockenlauf" in text and "würde angelegt" in text and "--token-datei" in text


def test_trockenlauf_legt_keine_datei_an(lm, tmp_path):
    datei = tmp_path / "token.env"
    lauf(lm, ja=False, token_datei=datei)
    assert not datei.exists()


# ============================================================================================ Ausführen
def test_ja_richtet_vorlage_rolle_und_zugang_ein(lm, tmp_path):
    datei = tmp_path / "token.env"
    code, aus = lauf(lm, token_datei=datei)
    assert code == 0
    vorlage = next(v for v in lm.vorlagen if v["name"] == "Mitteilung")
    assert vorlage["type"] == "tx" and vorlage["subject"] == ein.MITTEILUNG_BETREFF and vorlage["body"] == ein.MITTEILUNG_HTML
    (rolle,) = lm.rollen
    assert rolle["name"] == "arbeit" and set(rolle["permissions"]) == set(ein.ROLLEN["arbeit"])
    (benutzer,) = lm.benutzer
    assert benutzer["username"] == "fyos" and benutzer["type"] == "api" and benutzer["user_role"]["id"] == rolle["id"]
    anlegen = [a for a in lm.anfragen if a[:2] == ("POST", "/api/users")][0][2]
    assert anlegen["password_login"] is False and anlegen["status"] == "enabled"
    assert tokens_aus(datei) == {"FYOS_POST_TOKEN_FYOS": lm.token["fyos"]}
    assert lm.listen == [] and lm.abonnenten == []  # ohne --abonnent nichts davon


def test_token_datei_hat_rechte_0600(lm, tmp_path):
    datei = tmp_path / "token.env"
    lauf(lm, token_datei=datei)
    if os.name == "posix":
        assert stat.S_IMODE(datei.stat().st_mode) == 0o600


def test_kein_geheimnis_in_der_ausgabe(lm, tmp_path, capsys):
    datei = tmp_path / "token.env"
    _, aus = lauf(lm, token_datei=datei, abonnent="ada@example.org")
    gesehen = "\n".join(aus) + capsys.readouterr().out
    assert lm.token["fyos"] not in gesehen and PASSWORT not in gesehen
    assert str(datei) in gesehen and "0600" in gesehen


def test_zweiter_lauf_aendert_nichts(lm, tmp_path):
    datei = tmp_path / "token.env"
    lauf(lm, token_datei=datei, abonnent="ada@example.org")
    umgebung = tokens_aus(datei)
    lm.anfragen.clear()
    code, aus = lauf(lm, token_datei=tmp_path / "zweite.env", abonnent="ada@example.org", umgebung_token=lambda n: umgebung[f"FYOS_POST_TOKEN_{n.upper()}"])
    assert code == 0 and lm.aenderungen() == []
    assert not (tmp_path / "zweite.env").exists()
    text = "\n".join(aus)
    assert "unverändert" in text and "Token gilt" in text


def test_abonnent_wird_in_die_private_liste_eingetragen(lm, tmp_path):
    lauf(lm, token_datei=tmp_path / "t.env", abonnent="ada@example.org")
    (liste,) = lm.listen
    assert liste["name"] == "Intern" and liste["type"] == "private" and liste["optin"] == "single"
    (s,) = lm.abonnenten
    assert s["email"] == "ada@example.org" and s["status"] == "enabled"
    assert s["lists"] == [{"id": liste["id"], "subscription_status": "confirmed"}]


def test_vorhandener_abonnent_ohne_bestaetigung_wird_bestaetigt(lm, tmp_path):
    lm.listen.append({"id": 5, "name": "Intern", "type": "private", "optin": "single", "status": "active"})
    lm.abonnenten.append({"id": 9, "email": "ada@example.org", "name": "ada", "status": "enabled", "lists": [{"id": 5, "subscription_status": "unconfirmed"}]})
    lauf(lm, token_datei=tmp_path / "t.env", abonnent="ada@example.org")
    assert lm.abonnenten[0]["lists"] == [{"id": 5, "subscription_status": "confirmed"}]
    assert len(lm.abonnenten) == 1


def test_gesperrter_abonnent_ist_ein_befund_kein_eingriff(lm, tmp_path):
    lm.listen.append({"id": 5, "name": "Intern", "type": "private", "optin": "single", "status": "active"})
    lm.abonnenten.append({"id": 9, "email": "ada@example.org", "name": "ada", "status": "blocklisted", "lists": [{"id": 5, "subscription_status": "confirmed"}]})
    code, aus = lauf(lm, token_datei=tmp_path / "t.env", abonnent="ada@example.org")
    assert code == 2 and any("blocklisted" in z for z in aus)
    assert lm.abonnenten[0]["status"] == "blocklisted"


def test_mehrere_zugaenge_mit_anzeigenamen(lm, tmp_path):
    datei = tmp_path / "t.env"
    zugaenge = ein.zugaenge_aus_einstellung("code=helfer/Code-Helfer,chat=helfer/Chat-Helfer,*=dienst/Dienst")
    assert zugaenge == {"helfer": "Code-Helfer", "dienst": "Dienst"}
    code, _ = lauf(lm, zugaenge=zugaenge, token_datei=datei)
    assert code == 0
    assert {u["username"]: u["name"] for u in lm.benutzer} == zugaenge
    assert set(tokens_aus(datei)) == {"FYOS_POST_TOKEN_HELFER", "FYOS_POST_TOKEN_DIENST"}


def test_zugaenge_aus_einstellung_vorgabe_und_fehler():
    assert ein.zugaenge_aus_einstellung("*=fyos") == {"fyos": "fyos"}
    with pytest.raises(Fehler):
        ein.zugaenge_aus_einstellung("unsinn")


# ============================================================================================ Token-Datei
def test_vorhandene_token_datei_wird_nie_ueberschrieben_und_nichts_geaendert(lm, tmp_path):
    datei = tmp_path / "token.env"
    datei.write_text("alt\n")
    with pytest.raises(Fehler) as e:
        lauf(lm, token_datei=datei)
    assert "gibt es schon" in str(e.value)
    assert datei.read_text() == "alt\n"
    assert lm.aenderungen() == []  # die Prüfung kommt vor der ersten Änderung


def test_ohne_token_datei_keine_aenderung_wenn_ein_zugang_neu_ist(lm):
    with pytest.raises(Fehler) as e:
        lauf(lm)
    assert "--token-datei" in str(e.value) and lm.aenderungen() == []


def test_datei_nicht_anlegbar_nimmt_den_zugang_zurueck(lm, tmp_path):
    with pytest.raises(Fehler) as e:
        lauf(lm, token_datei=tmp_path / "gibt-es-nicht" / "t.env")
    assert "lässt sich nicht anlegen" in str(e.value)
    assert lm.benutzer == [] and lm.token == {}  # kein Zugang mit verlorenem Token zurückgeblieben


def test_zugang_vorhanden_braucht_keine_datei(lm, tmp_path):
    datei = tmp_path / "t.env"
    lauf(lm, token_datei=datei)
    umgebung = tokens_aus(datei)
    lm.anfragen.clear()
    code, _ = lauf(lm, umgebung_token=lambda n: umgebung["FYOS_POST_TOKEN_FYOS"])
    assert code == 0


# ============================================================================================ Zugänge prüfen und erneuern
def test_vorhandener_zugang_ohne_token_in_der_umgebung_ist_ein_befund(lm, tmp_path):
    lauf(lm, token_datei=tmp_path / "t.env")
    code, aus = lauf(lm)
    assert code == 2
    text = "\n".join(aus)
    assert "FYOS_POST_TOKEN_FYOS" in text and "--token-neu fyos" in text


def test_falsches_token_ist_ein_befund_und_wird_nicht_ausgegeben(lm, tmp_path):
    lauf(lm, token_datei=tmp_path / "t.env")
    code, aus = lauf(lm, umgebung_token=lambda n: "falsches-token-0123456789")
    assert code == 2 and "nicht angenommen" in "\n".join(aus)
    assert "falsches-token-0123456789" not in "\n".join(aus)


def test_token_neu_loescht_und_legt_neu_an(lm, tmp_path):
    lauf(lm, token_datei=tmp_path / "t.env")
    altes, alte_id = lm.token["fyos"], lm.benutzer[0]["id"]
    neu = tmp_path / "neu.env"
    code, aus = lauf(lm, token_datei=neu, token_neu={"fyos"})
    assert code == 0 and "gelöscht, wird neu angelegt" in "\n".join(aus)
    assert lm.benutzer[0]["id"] != alte_id and lm.token["fyos"] != altes
    assert tokens_aus(neu) == {"FYOS_POST_TOKEN_FYOS": lm.token["fyos"]}


def test_token_neu_mit_unbekanntem_zugang_aendert_nichts(lm, tmp_path):
    with pytest.raises(Fehler) as e:
        lauf(lm, token_datei=tmp_path / "t.env", token_neu={"gibtsnicht"})
    assert "gibtsnicht" in str(e.value) and lm.aenderungen() == []


def test_zugang_mit_falscher_rolle_wird_angeglichen(lm, tmp_path):
    lauf(lm, token_datei=tmp_path / "t.env")
    lm.benutzer[0]["user_role"] = {"id": 1}
    lm.benutzer[0]["status"] = "disabled"
    umgebung = lm.token["fyos"]
    code, aus = lauf(lm, umgebung_token=lambda n: umgebung)
    assert code == 0 and "angeglichen" in "\n".join(aus)
    assert lm.benutzer[0]["status"] == "enabled" and lm.benutzer[0]["user_role"]["id"] == lm.rollen[0]["id"]


def test_geaenderte_rolle_wird_zurueckgesetzt(lm, tmp_path):
    lauf(lm, token_datei=tmp_path / "t.env")
    lm.rollen[0]["permissions"] = lm.rollen[0]["permissions"] + ["campaigns:send"]
    umgebung = lm.token["fyos"]
    lauf(lm, umgebung_token=lambda n: umgebung)
    assert "campaigns:send" not in lm.rollen[0]["permissions"]


def test_vorlage_wird_nachgezogen_wenn_sie_abweicht(lm, tmp_path):
    lauf(lm, token_datei=tmp_path / "t.env")
    vorlage = next(v for v in lm.vorlagen if v["name"] == "Mitteilung")
    vorlage["body"] = "verändert"
    umgebung = lm.token["fyos"]
    _, aus = lauf(lm, umgebung_token=lambda n: umgebung)
    assert vorlage["body"] == ein.MITTEILUNG_HTML and "aktualisiert" in "\n".join(aus)


# ============================================================================================ Vertrag mit dem Plugin
def test_vertrag_namen_und_schluessel_sind_gleich():
    assert ein.MITTEILUNG_NAME == post.MITTEILUNG_NAME == "Mitteilung"
    assert tuple(ein.MITTEILUNG_DATEN_SCHLUESSEL) == tuple(post.MITTEILUNG_DATEN_SCHLUESSEL)


def test_vorlage_nutzt_genau_die_schluessel_des_plugins():
    benutzt = set(re.findall(r"\.Tx\.Data\.(\w+)", ein.MITTEILUNG_HTML + ein.MITTEILUNG_BETREFF))
    assert benutzt == set(post.MITTEILUNG_DATEN_SCHLUESSEL)
    assert ein.MITTEILUNG_BETREFF == "{{ .Tx.Data.betreff }}"


def test_vorlage_nennt_keinen_betreiber_und_keine_feste_adresse():
    text = ein.MITTEILUNG_HTML
    assert not re.search(r"https?://(?!www\.w3\.org)", text) and "@" not in text
    sichtbar = " ".join(re.sub(r"<[^>]*>|\{\{[^}]*\}\}", " ", text).split())
    assert sichtbar == "Von über FYOS Post, . Kennung ."  # der ganze feste Text der Vorlage: kein Name, kein Betreiber


def test_rolle_darf_keine_kampagne_starten_und_nichts_verwalten():
    rechte = set(ein.ROLLEN["arbeit"])
    assert "campaigns:send" not in rechte and "tx:send" in rechte and "campaigns:manage" in rechte
    verboten = ("settings", "users", "roles", "sql", "import", "maintenance", "webhooks")
    assert not [r for r in rechte if r.split(":")[0] in verboten or "sql" in r]


# ============================================================================================ Client und Anmeldung
def test_anmeldung_abgewiesen_nennt_den_benutzer_nicht_das_passwort(lm):
    lm.login_ok = False
    c = Listmonk(lm, ja=False)
    with pytest.raises(Fehler) as e:
        c.anmelden(ADMIN, PASSWORT)
    assert ADMIN in str(e.value) and PASSWORT not in str(e.value)


def test_aendern_geht_im_trockenlauf_nicht_hinaus(lm):
    c = Listmonk(lm, ja=False)
    assert c.aendern("POST", "/api/lists", {"name": "x"}) is None
    assert lm.anfragen == []


def test_fehler_von_listmonk_wird_zum_klaren_text(lm):
    c = Listmonk(lm, ja=False)  # nicht angemeldet: 403
    with pytest.raises(Fehler) as e:
        c.lesen("/api/about")
    assert "HTTP 403" in str(e.value) and "invalid session" in str(e.value)


def test_warten_bis_bereit(lm):
    lm.bereit_ab = 2
    pausen: list[float] = []
    c = Listmonk(lm, ja=False, schlafen=pausen.append)
    c.warten_bis_bereit(frist_s=30)
    assert pausen == [1.0, 1.0]
    lm.bereit_ab = 10**6
    with pytest.raises(Fehler):
        Listmonk(lm, ja=False, schlafen=lambda s: None).warten_bis_bereit(frist_s=0)


def test_unkenntlich_schwaerzt_alle_bekannten_geheimnisse():
    assert ein.unkenntlich("a geheim1 b geheim12 c", {"geheim1", "geheim12", ""}) == "a *** b *** c"


# ============================================================================================ Kommando
def test_kommando_ohne_admin_zugang_endet_mit_1(monkeypatch, capsys):
    monkeypatch.setenv("FYOS_POST_URL", "http://listmonk.test")
    assert ein.main([]) == 1
    assert "FYOS_POST_ADMIN_NUTZER" in capsys.readouterr().err


def test_kommando_ohne_adresse_endet_mit_1(monkeypatch, capsys):
    assert ein.main([]) == 1
    assert "FYOS_POST_URL" in capsys.readouterr().err


def test_kommando_token_neu_braucht_ja(monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        ein.main(["--token-neu", "fyos"])
    assert e.value.code == 2


def test_kommando_ungueltiger_abonnent(capsys):
    with pytest.raises(SystemExit):
        ein.main(["--abonnent", "keine adresse"])


def test_kommando_ungueltige_zugaenge_enden_mit_1(monkeypatch, capsys):
    monkeypatch.setenv("FYOS_POST_URL", "http://listmonk.test")
    monkeypatch.setenv("FYOS_POST_ADMIN_NUTZER", ADMIN)
    monkeypatch.setenv("FYOS_POST_ADMIN_PASSWORT", PASSWORT)
    monkeypatch.setenv("FYOS_POST_ZUGAENGE", "unsinn")
    assert ein.main([]) == 1
    assert "FYOS_POST_ZUGAENGE" in capsys.readouterr().err


def test_kommando_ende_zu_ende(lm, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("POST_URL", "http://listmonk.test/")  # der alte Name geht auch
    monkeypatch.setenv("FYOS_POST_ADMIN_NUTZER", ADMIN)
    monkeypatch.setenv("FYOS_POST_ADMIN_PASSWORT", PASSWORT)
    monkeypatch.setenv("FYOS_POST_ZUGAENGE", "code=helfer/Code-Helfer,*=dienst/Dienst")
    gewaehlt: list[str] = []
    monkeypatch.setattr(ein, "http_transport", lambda basis: gewaehlt.append(basis) or lm)
    datei = tmp_path / "token.env"
    assert ein.main(["--ja", "--token-datei", str(datei), "--abonnent", "Ada@Example.org"]) == 0
    assert gewaehlt == ["http://listmonk.test"]
    ausgabe = capsys.readouterr()
    for geheim in (PASSWORT, *lm.token.values()):
        assert geheim not in ausgabe.out and geheim not in ausgabe.err
    assert {u["username"] for u in lm.benutzer} == {"helfer", "dienst"} and lm.abonnenten[0]["email"] == "ada@example.org"
    assert set(tokens_aus(datei)) == {"FYOS_POST_TOKEN_HELFER", "FYOS_POST_TOKEN_DIENST"}
    # Trockenlauf danach: exit 2, weil die Token in der Umgebung fehlen
    assert ein.main([]) == 2
    assert "Befund" in capsys.readouterr().out


def test_kommando_meldung_schwaerzt_das_passwort(lm, monkeypatch, capsys):
    monkeypatch.setenv("FYOS_POST_URL", "http://listmonk.test")
    monkeypatch.setenv("FYOS_POST_ADMIN_NUTZER", ADMIN)
    monkeypatch.setenv("FYOS_POST_ADMIN_PASSWORT", "falsch-" + PASSWORT)
    monkeypatch.setattr(ein, "http_transport", lambda basis: lm)
    assert ein.main([]) == 1
    fehler = capsys.readouterr().err
    assert "abgewiesen" in fehler and PASSWORT not in fehler


# ============================================================================================ echter HTTP-Transport
class Echo(BaseHTTPRequestHandler):
    aufgezeichnet: list[dict] = []

    def _lauf(self):
        laenge = int(self.headers.get("Content-Length") or 0)
        roh = self.rfile.read(laenge) if laenge else b""
        Echo.aufgezeichnet.append({"methode": self.command, "pfad": self.path, "kopf": dict(self.headers), "roh": roh})
        if self.path.endswith("/weiter"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/anderswo")
            self.send_header("Set-Cookie", "session=xyz; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        antwort = b'{"data": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(antwort)))
        self.end_headers()
        self.wfile.write(antwort)

    do_GET = do_POST = _lauf

    def log_message(self, *a):
        pass


@pytest.fixture
def echo():
    Echo.aufgezeichnet = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", Echo.aufgezeichnet
    server.shutdown()
    server.server_close()


def test_http_transport_sendet_wirklich_und_beachtet_den_pfadvorsatz(echo):
    url, aufgezeichnet = echo
    senden = ein.http_transport(url + "/listmonk/")
    a = senden("POST", "/api/lists", {"Content-Type": "application/json"}, b'{"a": 1}')
    assert a.status == 200 and json.loads(a.koerper) == {"data": True}
    assert aufgezeichnet[0]["pfad"] == "/listmonk/api/lists" and aufgezeichnet[0]["roh"] == b'{"a": 1}'


def test_http_transport_folgt_keiner_umleitung_und_liefert_das_cookie(echo):
    url, aufgezeichnet = echo
    a = ein.http_transport(url)("GET", "/weiter", {}, None)
    assert a.status == 302 and a.cookies == ["session=xyz; Path=/"]
    assert len(aufgezeichnet) == 1  # die Adresse der Umleitung wurde nicht aufgerufen


@pytest.mark.parametrize("basis", ["ftp://x.test", "listmonk.test", "file:///etc/passwd", "http://"])
def test_http_transport_nur_http_und_https(basis):
    with pytest.raises(Fehler):
        ein.http_transport(basis)
