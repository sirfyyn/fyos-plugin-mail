"""post — FYOS Post (Listmonk): gegen eine Attrappe der Listmonk-API, ohne Netz außer 127.0.0.1.

Die Attrappe folgt dem Quelltext von Listmonk 6.2.0 (cmd/tx.go, cmd/campaigns.go): `POST /api/tx` mit `subscriber_mode: default`
antwortet 400, wenn ein Empfänger kein Abonnent ist; der Test einer Kampagne verlangt ihre Felder und Listen noch einmal im
Aufruf; wer keine Methode außer GET und POST und keinen Pfad auf `/status` hat, kann keine Kampagne starten. Die Attrappe
wirft einen AssertionError, wenn doch jemand PUT, PATCH, DELETE oder `/status` ruft.

Ein paar Tests laufen gegen einen echten HTTP-Server auf 127.0.0.1: Umleitungen, Verbindungsfehler und die Kopfzeilen auf dem
Draht lassen sich nur dort belegen.
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fyos_plugin_mail import post
from mcp_hub import ansicht
from mcp_hub import registry as hub_registry
from mcp_hub.registry import AUFRUFER, scope_from_tags

URL = "http://listmonk.test"
LINK = "https://post.example.org"
STANDARD = "post@example.org"
ZUGAENGE = "code=helfer/Code-Helfer,chat=helfer/Chat-Helfer,*=dienst/Dienst"
TOKEN = {"helfer": "h-token-GEHEIM-0123456789", "dienst": "d-token-GEHEIM-9876543210"}
HEUTE = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
SCHLUESSEL_HEUTE = "fyos:hub:post:sendungen:2026-10-10"
SCHLUESSEL_MORGEN = "fyos:hub:post:sendungen:2026-10-11"

try:
    ZoneInfo("Europe/Berlin")
    ZEITZONEN_DA = True
except Exception:  # noqa: BLE001 — kein tzdata auf diesem Rechner
    ZEITZONEN_DA = False

KAMPAGNE = {
    "id": 7,
    "name": "Oktober-Brief",
    "subject": "Neues im Oktober",
    "status": "draft",
    "from_email": "FYOS Post <absender@example.org>",
    "body": "# Hallo {{ .Subscriber.FirstName }}",
    "altbody": None,
    "content_type": "markdown",
    "messenger": "email",
    "headers": [],
    "template_id": 1,
    "lists": [{"id": 1, "name": "Intern"}],
    "sent": 0,
    "to_send": 1,
    "created_at": "2026-10-10T07:00:00Z",
    "started_at": None,
}


def _antwort(daten, status=200):
    return status, json.dumps({"data": daten}, ensure_ascii=False).encode()


def _fehler(status, meldung):
    return status, json.dumps({"message": meldung}).encode()


class FalschesListmonk:
    """Die Listmonk-API, soweit das Plugin sie benutzt. Als Haken `PostPlugin._http` einsetzbar."""

    def __init__(self):
        self.anfragen: list[dict] = []
        self.vorlagen = [
            {"id": 1, "name": "Default campaign template", "type": "campaign", "is_default": True},
            {"id": 5, "name": "Kontaktformular", "type": "tx", "is_default": False},
            {"id": 6, "name": "Mitteilung", "type": "tx", "is_default": False},
        ]
        self.listen = [
            {"id": 1, "name": "Intern", "type": "private", "optin": "single", "status": "active", "subscriber_count": 1, "tags": ["intern"]},
            {"id": 2, "name": "Freunde", "type": "private", "optin": "single", "status": "active", "subscriber_count": 0, "tags": []},
        ]
        self.abonnenten = {STANDARD, "ada@example.org", "ben@example.org", "cora@example.org", "dirk@example.org", "emil@example.org"}
        self.kampagnen = {7: dict(KAMPAGNE)}
        self.tx_antworten: dict[str, tuple[int, bytes]] = {}  # Adresse -> feste Antwort statt Erfolg
        self.status_beim_anlegen = "draft"
        self.alles_antwortet_mit: tuple[int, bytes] | None = None
        self.nach_tx = None  # Haken: wird nach jeder Tx-Anfrage gerufen (Uhr verstellen o. ä.)

    # -- Hilfen für Tests
    def aufrufe(self, methode: str, pfad: str) -> list[dict]:
        return [a for a in self.anfragen if a["methode"] == methode and a["pfad"] == pfad]

    def tx(self) -> list[dict]:
        return self.aufrufe("POST", "/api/tx")

    # -- die Attrappe selbst
    def __call__(self, methode, url, kopf, daten, frist):
        teile = urllib.parse.urlsplit(url)
        pfad = teile.path
        abfrage = urllib.parse.parse_qs(teile.query)
        nutzlast = json.loads(daten.decode("utf-8")) if daten else None
        self.anfragen.append({"methode": methode, "pfad": pfad, "abfrage": abfrage, "kopf": dict(kopf), "daten": nutzlast, "roh": daten, "frist": frist})
        assert methode in ("GET", "POST"), f"verbotene Methode {methode} {pfad}"
        assert not pfad.endswith("/status"), f"Kampagne gestartet? {methode} {pfad}"
        if self.alles_antwortet_mit is not None:
            return self.alles_antwortet_mit
        if pfad == "/health":
            return _antwort(True)
        benutzer, _, token = kopf.get("Authorization", "").removeprefix("token ").partition(":")
        if TOKEN.get(benutzer) != token:
            return _fehler(403, "invalid API credentials")
        if pfad == "/api/profile":
            return _antwort({"username": benutzer})
        if pfad == "/api/lists" and methode == "GET":
            return _antwort({"results": self.listen, "total": len(self.listen), "per_page": len(self.listen), "page": 1})
        if pfad == "/api/templates" and methode == "GET":
            return _antwort(self.vorlagen)
        if pfad == "/api/campaigns" and methode == "GET":
            rows = sorted(self.kampagnen.values(), key=lambda k: -k["id"])
            return _antwort({"results": rows, "total": len(rows), "per_page": 5, "page": 1})
        if pfad == "/api/campaigns" and methode == "POST":
            neu = {**KAMPAGNE, **{k: v for k, v in nutzlast.items() if k in ("name", "subject", "content_type", "body", "messenger")}}
            neu.update(id=8, status=self.status_beim_anlegen, lists=[l for l in self.listen if l["id"] in nutzlast["lists"]])
            self.kampagnen[8] = neu
            return _antwort(neu)
        teile_pfad = pfad.strip("/").split("/")
        if teile_pfad[:2] == ["api", "campaigns"] and len(teile_pfad) >= 3 and teile_pfad[2].isdigit():
            kid = int(teile_pfad[2])
            if kid not in self.kampagnen:
                return _fehler(404, "Campaign not found.")
            if len(teile_pfad) == 3 and methode == "GET":
                return _antwort(self.kampagnen[kid])
            if len(teile_pfad) == 4 and teile_pfad[3] == "test" and methode == "POST":
                for pflicht in ("name", "subject", "content_type", "body", "lists", "subscribers"):
                    if not nutzlast.get(pflicht):
                        return _fehler(400, f"invalid field: {pflicht}")
                if not set(nutzlast["subscribers"]) & self.abonnenten:
                    return _fehler(400, "None of the subscribers are known.")
                return _antwort(True)
        if pfad == "/api/tx" and methode == "POST":
            assert len(nutzlast["subscriber_emails"]) == 1
            adresse = nutzlast["subscriber_emails"][0]
            if self.nach_tx:
                self.nach_tx()
            if adresse in self.tx_antworten:
                return self.tx_antworten[adresse]
            if adresse not in self.abonnenten:
                return _fehler(400, f"Subscriber not found: {adresse}")
            return _antwort(True)
        raise AssertionError(f"unerwartet: {methode} {pfad}")


@pytest.fixture
def listmonk(monkeypatch):
    """Eine vollständige Einrichtung (Adresse, Link, Standardempfänger, Zuordnung, beide Token) und die Attrappe dazu."""
    monkeypatch.setenv("FYOS_POST_URL", URL)
    monkeypatch.setenv("FYOS_POST_LINK", LINK)
    monkeypatch.setenv("FYOS_POST_AN", STANDARD)
    monkeypatch.setenv("FYOS_POST_ZUGAENGE", ZUGAENGE)
    for zugang, token in TOKEN.items():
        monkeypatch.setenv(f"FYOS_POST_TOKEN_{zugang.upper()}", token)
    return FalschesListmonk()


def neues(listmonk=None, **config):
    """Ein Plugin mit Attrappe, eigener Ablage (ohne Redis) und fester Uhr."""
    p = post.PostPlugin(config)
    if listmonk is not None:
        p._http = listmonk
    ablage = ansicht.Ablage(redis=None)
    ablage._redis_versucht = True  # noqa: SLF001 — kein Redis im Test
    p._ablage = ablage
    p._uhr = lambda: HEUTE
    return p


@contextlib.contextmanager
def als(aufrufer: str):
    marke = AUFRUFER.set(aufrufer)
    try:
        yield
    finally:
        AUFRUFER.reset(marke)


ARGS = {"betreff": "Hallo", "text": "Eine Zeile."}
ENTWURF = {"name": "Oktober", "betreff": "Neuigkeiten", "listen": ["intern"], "text": "# Hallo"}
NUR_LESEN = {"zustand": {}}


def gebucht(p, schluessel=SCHLUESSEL_HEUTE) -> dict:
    return p._ablage.zaehler(schluessel)  # noqa: SLF001


def alle_werkzeuge(p):
    """Jedes der vier Werkzeuge mit gültigen Eingaben: (Name, Aufruf)."""
    return [
        ("zustand", lambda: p.call("zustand", {})),
        ("senden", lambda: p.call("senden", ARGS)),
        ("kampagne_entwurf", lambda: p.call("kampagne_entwurf", ENTWURF)),
        ("kampagne_testen", lambda: p.call("kampagne_testen", {"id": 7})),
    ]


# ============================================================================================ post.senden: Nutzlast
def test_senden_schickt_betreff_und_text_nur_in_data(listmonk):
    p = neues(listmonk)
    with als("mcp:claude-code"):
        r = p.call("senden", {"betreff": "Hallo {{ .Tx.Data.von }}", "text": "Zeile eins\nZeile {{ 1 }}\r\nDrei"})
    assert r["ok"] is True and r["angenommen"] is True
    (aufruf,) = listmonk.tx()
    n = aufruf["daten"]
    assert set(n) == {"subscriber_emails", "subscriber_mode", "template_id", "content_type", "messenger", "data"}
    assert n["subscriber_mode"] == "default" and n["template_id"] == 6 and n["subscriber_emails"] == [STANDARD]
    assert n["content_type"] == "html" and n["messenger"] == "email"
    for verboten in ("subject", "altbody", "headers", "from_email", "subscriber_ids"):
        assert verboten not in n
    assert set(n["data"]) == set(post.MITTEILUNG_DATEN_SCHLUESSEL)
    assert n["data"]["betreff"] == "Hallo {{ .Tx.Data.von }}"  # Text bleibt Text, wird weder ausgewertet noch abgelehnt
    assert n["data"]["zeilen"] == ["Zeile eins", "Zeile {{ 1 }}", "Drei"]
    ausserhalb = json.dumps({k: v for k, v in n.items() if k != "data"})
    assert "Hallo" not in ausserhalb and "Zeile" not in ausserhalb


@pytest.mark.skipif(not ZEITZONEN_DA, reason="keine Zeitzonendaten (tzdata) auf diesem Rechner")
def test_senden_fusszeile_nennt_wer_und_wann(listmonk):
    p = neues(listmonk)
    with als("agent-test"):
        p.call("senden", ARGS)
    d = listmonk.tx()[0]["daten"]["data"]
    assert d["von"] == "Dienst" and d["zeit"] == "10.10.2026, 14:00 Uhr"  # 12:00 UTC = 14:00 Sommerzeit in Berlin
    assert len(d["kennung"]) == 8


def test_zeitzone_ist_einstellbar_und_faellt_auf_utc_zurueck(listmonk, monkeypatch):
    monkeypatch.setenv("FYOS_POST_ZEITZONE", "UTC")
    neues(listmonk).call("senden", ARGS)
    assert listmonk.tx()[0]["daten"]["data"]["zeit"] == "10.10.2026, 12:00 Uhr"
    listmonk.anfragen.clear()
    monkeypatch.setenv("FYOS_POST_ZEITZONE", "Gibt/Es-Nicht")  # unbekannter Name: UTC, und die Mail sagt es
    neues(listmonk).call("senden", ARGS)
    assert listmonk.tx()[0]["daten"]["data"]["zeit"] == "10.10.2026, 12:00 Uhr UTC"
    listmonk.anfragen.clear()
    neues(listmonk, zeitzone="UTC").call("senden", ARGS)  # die Konfiguration des Plugins gewinnt
    assert listmonk.tx()[0]["daten"]["data"]["zeit"] == "10.10.2026, 12:00 Uhr"


def test_betreff_wird_einzeilig_und_ohne_steuerzeichen(listmonk):
    p = neues(listmonk)
    p.call("senden", {"betreff": "Zeile1\r\nBcc: x@y.z\tTab\x00Null‮RTL", "text": "x"})
    betreff = listmonk.tx()[0]["daten"]["data"]["betreff"]
    assert betreff == "Zeile1 Bcc: x@y.z Tab Null RTL"
    assert "\n" not in betreff and "\r" not in betreff


def test_text_wird_zeilenweise_aufbereitet(listmonk):
    p = neues(listmonk)
    p.call("senden", {"betreff": "x", "text": "\n\n  Eins \r\n\tZwei\x00\x07\n\n\nDrei\r\r"})
    assert listmonk.tx()[0]["daten"]["data"]["zeilen"] == ["  Eins", "    Zwei", "", "", "Drei"]


@pytest.mark.parametrize(
    "args",
    [
        {"betreff": "", "text": "x"},
        {"betreff": "   ", "text": "x"},
        {"betreff": "\x00\x01", "text": "x"},
        {"betreff": 5, "text": "x"},
        {"text": "x"},
        {"betreff": "x" * 151, "text": "x"},
        {"betreff": "x", "text": ""},
        {"betreff": "x", "text": " \n\t "},
        {"betreff": "x", "text": None},
        {"betreff": "x", "text": "y" * 5001},
        {"betreff": "x", "text": "\n".join(["a"] * 201)},
        {"betreff": "x"},
    ],
)
def test_eingaben_ausserhalb_der_grenzen_sind_ein_eingabefehler(listmonk, args):
    p = neues(listmonk)
    with pytest.raises(ValueError):
        p.call("senden", args)
    assert listmonk.anfragen == []  # vor jedem Netzzugriff geprüft
    assert gebucht(p) == {}


def test_grenzfaelle_noch_erlaubt(listmonk):
    p = neues(listmonk)
    assert p.call("senden", {"betreff": "b" * 150, "text": "t" * 5000})["ok"] is True
    assert p.call("senden", {"betreff": "b", "text": "\n".join(["a"] * 200)})["ok"] is True


def test_empfaenger_standard_kommt_aus_der_einstellung_und_wird_bereinigt(listmonk):
    p = neues(listmonk)
    p.call("senden", ARGS)
    p.call("senden", {**ARGS, "an": "Ada@Example.org"})  # ein einzelner Text geht auch
    p.call("senden", {**ARGS, "an": ["ben@example.org", "BEN@example.org", " cora@example.org "]})
    ziele = [a["daten"]["subscriber_emails"] for a in listmonk.tx()]
    assert ziele == [[STANDARD], ["ada@example.org"], ["ben@example.org"], ["cora@example.org"]]  # eine Anfrage je Empfänger


def test_ohne_standardempfaenger_ist_an_pflicht_und_die_meldung_sagt_es(listmonk, monkeypatch):
    monkeypatch.delenv("FYOS_POST_AN")
    p = neues(listmonk)
    for aufruf in (lambda: p.call("senden", ARGS), lambda: p.call("kampagne_testen", {"id": 7})):
        for leer in (None, [], ""):
            with pytest.raises(ValueError) as e:
                aufruf() if leer is None else p.call("senden", {**ARGS, "an": leer})
            assert "an fehlt" in str(e.value) and "FYOS_POST_AN" in str(e.value)
    assert listmonk.anfragen == [] and gebucht(p) == {}
    assert p.call("senden", {**ARGS, "an": ["ada@example.org"]})["ok"] is True  # mit Angabe geht es


def test_standardempfaenger_muss_eine_einfache_adresse_sein(listmonk, monkeypatch):
    monkeypatch.setenv("FYOS_POST_AN", "Name <post@example.org>")
    with pytest.raises(ValueError) as e:
        neues(listmonk).call("senden", ARGS)
    assert "FYOS_POST_AN" in str(e.value)
    assert listmonk.anfragen == []


@pytest.mark.parametrize(
    "an",
    [
        ["a@b"],
        ["Name <post@example.org>"],
        ["a@example.org, c@example.org"],
        ["a b@example.org"],
        [""],
        [5],
        ["ada@example.org", "ben@example.org", "cora@example.org", "dirk@example.org", "emil@example.org", "fred@example.org"],
        {"a": "b"},
    ],
)
def test_ungueltige_empfaenger_oder_zu_viele(listmonk, an):
    p = neues(listmonk)
    with pytest.raises(ValueError):
        p.call("senden", {**ARGS, "an": an})
    assert listmonk.anfragen == []


def test_modus_ist_immer_default_externe_modi_sind_gesperrt(listmonk):
    p = neues(listmonk)
    p.call("senden", {**ARGS, "an": [STANDARD, "ada@example.org"]})
    assert {a["daten"]["subscriber_mode"] for a in listmonk.tx()} == {"default"}
    schluessel = {f for t in p.describe_tools() for f in t.input_schema["properties"]}
    for name in ("modus", "subscriber_mode", "extern", "external", "fallback", "from_email", "absender", "headers", "subject", "altbody", "template_id"):
        assert name not in schluessel


@pytest.mark.parametrize("modus", post.GESPERRTE_MODI)
def test_sperre_vor_dem_netz_lehnt_externe_modi_ab(modus):
    nutzlast = post.PostPlugin._tx_nutzlast("a@example.org", 6, {k: "x" for k in post.MITTEILUNG_DATEN_SCHLUESSEL})
    post.PostPlugin._pruefe_nutzlast(nutzlast)  # der Normalfall besteht
    with pytest.raises(RuntimeError):
        post.PostPlugin._pruefe_nutzlast({**nutzlast, "subscriber_mode": modus})


@pytest.mark.parametrize(
    "zusatz",
    [{"subject": "x"}, {"altbody": "x"}, {"headers": []}, {"from_email": "x@example.org"}, {"subscriber_ids": [1]}, {"subscriber_email": "a@example.org"}],
)
def test_sperre_vor_dem_netz_lehnt_betreff_text_und_absender_ausserhalb_von_data_ab(zusatz):
    nutzlast = post.PostPlugin._tx_nutzlast("a@example.org", 6, {k: "x" for k in post.MITTEILUNG_DATEN_SCHLUESSEL})
    with pytest.raises(RuntimeError):
        post.PostPlugin._pruefe_nutzlast({**nutzlast, **zusatz})


def test_sperre_lehnt_fremde_data_schluessel_und_mehrere_empfaenger_ab():
    daten = {k: "x" for k in post.MITTEILUNG_DATEN_SCHLUESSEL}
    nutzlast = post.PostPlugin._tx_nutzlast("a@example.org", 6, daten)
    with pytest.raises(RuntimeError):
        post.PostPlugin._pruefe_nutzlast({**nutzlast, "data": {**daten, "extra": "x"}})
    with pytest.raises(RuntimeError):
        post.PostPlugin._pruefe_nutzlast({**nutzlast, "subscriber_emails": ["a@example.org", "c@example.org"]})


def test_unbekannte_argumente_lehnt_der_hub_ab_bevor_das_plugin_sie_sieht(listmonk):
    reg = hub_registry.PluginRegistry()
    hub_registry._register_one(reg, "post", neues(listmonk))
    r = hub_registry.dispatch(reg, "post.senden", {**ARGS, "subscriber_mode": "external", "an": ["fremd@example.org"]})
    assert r["ok"] is False and r["kind"] == "bad_arguments" and "subscriber_mode" in r["error"]
    assert listmonk.anfragen == []


# ============================================================================================ nicht eingerichtet
def test_ohne_adresse_ist_alles_nicht_eingerichtet_und_nichts_geht_ins_netz(monkeypatch):
    for name in ("FYOS_POST_URL", "POST_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FYOS_POST_TOKEN_FYOS", "irgendein-token-0123456789")  # auch mit Token: ohne Adresse geht nichts
    netz = FalschesListmonk()
    p = neues(netz)
    r = p.call("zustand", {})
    # weder „Ausfall“ noch „leer“: ein eigener Grund, der Weg zum Einrichten, und `eingerichtet: false`
    assert r["quelle_ok"] is False and r["eingerichtet"] is False and r["grund"] == "nicht eingerichtet"
    assert "FYOS_POST_URL" in r["satz"] and "fyos-post-einrichten" in r["satz"] and "ok" not in r and "error" not in r
    for name, aufruf in alle_werkzeuge(p)[1:]:
        e = aufruf()
        assert e["ok"] is False and e["eingerichtet"] is False and e["error"] == "nicht eingerichtet", name
        assert "FYOS_POST_URL" in e["satz"] and "fyos-post-einrichten" in e["satz"] and "nichts" in e["satz"].lower() or "keine Kampagne" in e["satz"]
    assert netz.anfragen == [] and gebucht(p) == {}
    # auch Eingaben, die sonst ein Eingabefehler wären, bekommen zuerst den Hinweis
    assert p.call("senden", {"betreff": ""})["ok"] is False


def test_ohne_adresse_meldet_der_hub_den_zustand_als_befund_der_senden_ist_tool_failed(monkeypatch):
    reg = hub_registry.PluginRegistry()
    hub_registry._register_one(reg, "post", post.PostPlugin())
    z = hub_registry.dispatch(reg, "post.zustand", {})
    assert z["ok"] is True and z["result"]["quelle_ok"] is False and z["result"]["eingerichtet"] is False
    s = hub_registry.dispatch(reg, "post.senden", ARGS)
    assert s["ok"] is False and s["kind"] == "tool_failed" and s["result"]["eingerichtet"] is False


def test_health_ohne_adresse_ist_aus_nicht_rot():
    h = post.PostPlugin().health()
    assert h["ok"] is True and h["status"] == "aus" and "FYOS_POST_URL" in h["reason"]
    assert hub_registry.plugin_aus(h)


def test_adresse_muss_http_oder_https_sein(listmonk, monkeypatch):
    for falsch in ("ftp://listmonk.test", "file:///etc/passwd", "listmonk.test", "javascript:alert(1)"):
        monkeypatch.setenv("FYOS_POST_URL", falsch)
        p = neues(listmonk)
        r = p.call("zustand", {})
        assert r["quelle_ok"] is False and "http" in r["grund"] and "eingerichtet" not in r
        assert p.call("senden", ARGS)["ok"] is False
        assert p.health()["status"] == "unhealthy"
    assert listmonk.anfragen == []


def test_aliase_post_url_und_post_token(listmonk, monkeypatch):
    monkeypatch.delenv("FYOS_POST_URL")
    monkeypatch.setenv("POST_URL", "http://alt.test/")
    for zugang in TOKEN:
        monkeypatch.delenv(f"FYOS_POST_TOKEN_{zugang.upper()}")
        monkeypatch.setenv(f"POST_TOKEN_{zugang.upper()}", TOKEN[zugang])
    p = neues(listmonk)
    with als("mcp:claude-code"):
        assert p.call("senden", ARGS)["ok"] is True
    assert listmonk.anfragen[0]["pfad"] == "/api/templates"  # der Aufruf ging an die Adresse des Alias
    (tx,) = listmonk.tx()
    assert tx["kopf"]["Authorization"] == f"token helfer:{TOKEN['helfer']}"


def test_neue_namen_gehen_den_alten_vor(listmonk, monkeypatch):
    monkeypatch.setenv("POST_URL", "http://alt.test")
    monkeypatch.setenv("POST_TOKEN_HELFER", "alt")
    p = neues(listmonk)
    with als("mcp:claude-code"):
        p.call("senden", ARGS)
    assert listmonk.tx()[0]["kopf"]["Authorization"] == f"token helfer:{TOKEN['helfer']}"
    assert p._basis() == URL  # noqa: SLF001


def test_konfiguration_des_plugins_gewinnt_gegen_die_umgebung(listmonk):
    p = neues(listmonk, url="http://anders.test/", link="https://oberflaeche.example.org", an="eigen@example.org", zugaenge="*=dienst/Konfig")
    listmonk.abonnenten.add("eigen@example.org")
    assert p._basis() == "http://anders.test"  # noqa: SLF001
    r = p.call("senden", ARGS)
    assert r["von"] == "Konfig" and listmonk.tx()[0]["daten"]["subscriber_emails"] == ["eigen@example.org"]
    assert p.call("zustand", {})["link"] == "https://oberflaeche.example.org"


# ============================================================================================ Zugang nach Aufrufer
@pytest.mark.parametrize(
    ("aufrufer", "zugang", "von"),
    [
        ("mcp:claude-code", "helfer", "Code-Helfer"),
        ("mcp:codex", "helfer", "Code-Helfer"),
        ("claude-chat", "helfer", "Chat-Helfer"),
        ("oauth:konnektor", "helfer", "Chat-Helfer"),
        ("fyos-web", "dienst", "Dienst"),
        ("agent-eins", "dienst", "Dienst"),
        ("mcp:undici", "dienst", "Dienst"),
        ("rest:python-urllib", "dienst", "Dienst"),
        ("unknown", "dienst", "Dienst"),
    ],
)
def test_zugang_und_absendername_folgen_der_flaeche_des_aufrufers(listmonk, aufrufer, zugang, von):
    p = neues(listmonk)
    with als(aufrufer):
        r = p.call("senden", ARGS)
    assert r["ok"] is True and r["zugang"] == zugang and r["von"] == von
    (aufruf,) = listmonk.tx()
    assert aufruf["kopf"]["Authorization"] == f"token {zugang}:{TOKEN[zugang]}"
    assert aufruf["daten"]["data"]["von"] == von
    # jede Anfrage dieses Aufrufs lief mit demselben Zugang, auch das Nachschlagen der Vorlage
    assert {a["kopf"]["Authorization"] for a in listmonk.anfragen} == {f"token {zugang}:{TOKEN[zugang]}"}


def test_ohne_aufrufer_gilt_der_stern_eintrag(listmonk):
    p = neues(listmonk)
    assert AUFRUFER.get() == "unknown"
    assert p.call("senden", ARGS)["zugang"] == "dienst"


def test_vorgabe_ist_ein_zugang_fyos_fuer_alle_mit_neutralen_namen(listmonk, monkeypatch):
    monkeypatch.delenv("FYOS_POST_ZUGAENGE")
    monkeypatch.setenv("FYOS_POST_TOKEN_FYOS", "f-token-GEHEIM-0123456789")
    monkeypatch.setattr(FalschesListmonk, "__call__", FalschesListmonk.__call__)  # die Attrappe kennt das Token unten
    TOKEN["fyos"] = "f-token-GEHEIM-0123456789"
    try:
        namen = {}
        for aufrufer, erwartet in (
            ("fyos-web", "Shell"),
            ("claude-chat", "Chat"),
            ("mcp:claude-code", "Code-Agent"),
            ("agent-eins", "Agent"),
            ("mcp:undici", "FYOS-Dienst"),
        ):
            p = neues(listmonk)
            with als(aufrufer):
                r = p.call("senden", ARGS)
            assert r["ok"] is True and r["zugang"] == "fyos", aufrufer
            namen[aufrufer] = (r["von"], erwartet)
        assert all(ist == soll for ist, soll in namen.values()), namen
        monkeypatch.setenv("FYOS_AGENT_NAME", "Werkbank\n1")
        with als("agent-eins"):
            assert neues(listmonk).call("senden", ARGS)["von"] == "Werkbank 1"  # der Name des Agenten, einzeilig
    finally:
        TOKEN.pop("fyos", None)


def test_name_aus_der_einstellung_geht_dem_neutralen_namen_vor(listmonk, monkeypatch):
    monkeypatch.setenv("FYOS_POST_ZUGAENGE", "agent=dienst/Eigener Name,*=helfer")
    monkeypatch.setenv("FYOS_AGENT_NAME", "wird nicht gebraucht")
    with als("agent-eins"):
        assert neues(listmonk).call("senden", ARGS)["von"] == "Eigener Name"
    with als("mcp:claude-code"):
        r = neues(listmonk).call("senden", ARGS)
    assert r["zugang"] == "helfer" and r["von"] == "Code-Agent"


@pytest.mark.parametrize(
    "einstellung",
    [
        "unsinn",
        "code",
        "kueche=dienst",
        "code=",
        "=dienst",
        "code=Gross",
        "code=mit leer",
        "code=gesamt",
        "code=dienst,code=helfer",
        "code=a-b,chat=a_b",
        "code=" + "x" * 33,
        "code=dienst/" + "N" * 41,
        "code=dienst/mit,komma",
        " , ,",
    ],
)
def test_ungueltiger_eintrag_ist_ein_klarer_fehler_nie_ein_anderer_zugang(listmonk, monkeypatch, einstellung):
    monkeypatch.setenv("FYOS_POST_ZUGAENGE", einstellung)
    p = neues(listmonk)
    r = p.call("zustand", {})
    assert r["quelle_ok"] is False and "FYOS_POST_ZUGAENGE" in r["grund"] and "eingerichtet" not in r
    s = p.call("senden", ARGS)
    assert s["ok"] is False and "FYOS_POST_ZUGAENGE" in s["error"]
    for name, aufruf in alle_werkzeuge(p)[2:]:
        assert aufruf()["ok"] is False, name
    h = p.health()
    assert h["status"] == "unhealthy" and "FYOS_POST_ZUGAENGE" in h["reason"]
    assert listmonk.anfragen == [] and gebucht(p) == {}  # kein Versuch, auch nicht mit dem Zugang „dienst“


def test_fehlermeldung_zur_einstellung_gibt_den_wortlaut_nicht_wieder(listmonk, monkeypatch):
    geheim = "dieses-token-gehoert-nicht-hierher-0123"
    monkeypatch.setenv("FYOS_POST_ZUGAENGE", geheim)
    p = neues(listmonk)
    for text in (json.dumps(p.call("zustand", {})), json.dumps(p.call("senden", ARGS)), json.dumps(p.health())):
        assert geheim not in text


def test_ohne_stern_eintrag_hat_eine_nicht_genannte_flaeche_keinen_zugang(listmonk, monkeypatch):
    monkeypatch.setenv("FYOS_POST_ZUGAENGE", "code=helfer")
    p = neues(listmonk)
    with als("mcp:claude-code"):
        assert p.call("senden", ARGS)["ok"] is True
    listmonk.anfragen.clear()
    with als("mcp:undici"):
        r = p.call("senden", ARGS)
    assert r["ok"] is False and "dienste" in r["grund"] and "FYOS_POST_ZUGAENGE" in r["grund"]
    assert listmonk.anfragen == []  # kein Ausweichen auf den einen vorhandenen Zugang


def test_fehlendes_token_faellt_nicht_auf_das_des_anderen_zurueck(listmonk, monkeypatch):
    monkeypatch.delenv("FYOS_POST_TOKEN_HELFER")
    p = neues(listmonk)
    with als("mcp:claude-code"):
        r = p.call("senden", ARGS)
    assert r["ok"] is False and "helfer" in r["grund"] and "nicht eingerichtet" in r["grund"] and "FYOS_POST_TOKEN_HELFER" in r["grund"]
    assert listmonk.anfragen == []  # kein Versuch, auch nicht mit dem Token des anderen Zugangs
    assert TOKEN["dienst"] not in json.dumps(r)
    with als("mcp:undici"):
        assert p.call("senden", ARGS)["ok"] is True  # der andere Zugang ist davon unberührt


def test_leeres_token_zaehlt_als_fehlend(listmonk, monkeypatch):
    monkeypatch.setenv("FYOS_POST_TOKEN_DIENST", "   ")
    p = neues(listmonk)
    r = p.call("zustand", {})
    assert r["quelle_ok"] is False and "nicht eingerichtet" in r["grund"] and listmonk.anfragen == []


def test_kein_token_in_ergebnissen_und_protokoll(listmonk, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    fremd = "x-token-eines-dritten-0123456789"  # ein Token, das die Umgebung kennt, obwohl kein Eintrag es benutzt
    monkeypatch.setenv("POST_TOKEN_ANDERER", fremd)
    listmonk.alles_antwortet_mit = _fehler(500, f"kaputt, Token war {TOKEN['helfer']} und {TOKEN['dienst']} und {fremd}")
    p = neues(listmonk)
    gesehen = []
    for aufrufer in ("mcp:undici", "mcp:claude-code"):
        with als(aufrufer):
            for _name, aufruf in alle_werkzeuge(p):
                gesehen.append(json.dumps(aufruf(), ensure_ascii=False))
    assert len(gesehen) == 8
    for text in gesehen + [caplog.text, json.dumps(p.health())]:
        for token in (*TOKEN.values(), fremd):
            assert token not in text
    assert "***" in gesehen[0]  # die Stelle ist geschwärzt, nicht gelöscht


def test_kein_token_in_einer_klartext_antwort_und_im_health(listmonk):
    listmonk.alles_antwortet_mit = (502, f"<html>Bad Gateway {TOKEN['dienst']}</html>".encode())
    p = neues(listmonk)
    for text in (json.dumps(p.call("zustand", {})), json.dumps(p.call("senden", ARGS)), json.dumps(p.health())):
        assert TOKEN["dienst"] not in text


# ============================================================================================ Tagesgrenze
def test_tagesgrenze_lehnt_ab_und_nimmt_die_buchung_zurueck(listmonk):
    p = neues(listmonk, tagesgrenze=3)
    for _ in range(3):
        assert p.call("senden", ARGS)["ok"] is True
    r = p.call("senden", ARGS)
    assert r["ok"] is False and "Tagesgrenze" in r["grund"] and "nichts gesendet" in r["satz"]
    assert len(listmonk.tx()) == 3
    assert gebucht(p)["gesamt"] == 3  # die abgelehnte Buchung ist zurückgenommen
    assert p.call("zustand", {})["heute"]["gesendet"] == 3


def test_tagesgrenze_aus_der_umgebung(listmonk, monkeypatch):
    monkeypatch.setenv("FYOS_POST_TAGESGRENZE", "2")
    p = neues(listmonk)
    assert p.call("senden", ARGS)["ok"] is True and p.call("senden", ARGS)["ok"] is True
    assert p.call("senden", ARGS)["ok"] is False
    monkeypatch.setenv("FYOS_POST_TAGESGRENZE", "viele")  # unlesbar: die Vorgabe
    assert neues(listmonk)._grenze() == post.TAGESGRENZE == 30  # noqa: SLF001
    monkeypatch.setenv("FYOS_POST_TAGESGRENZE", "0")  # 0 sperrt alles Senden
    assert neues(listmonk).call("senden", ARGS)["ok"] is False


def test_jeder_empfaenger_zaehlt_einzeln(listmonk):
    p = neues(listmonk, tagesgrenze=3)
    assert p.call("senden", {**ARGS, "an": [STANDARD, "ada@example.org"]})["ok"] is True
    r = p.call("senden", {**ARGS, "an": [STANDARD, "ada@example.org"]})  # 2 + 2 > 3: alles oder nichts
    assert r["ok"] is False and len(listmonk.tx()) == 2
    assert gebucht(p)["gesamt"] == 2
    assert p.call("senden", ARGS)["ok"] is True  # die dritte passt noch
    assert gebucht(p) == {"gesamt": 3, "dienst": 3}


def test_zaehler_fuehrt_je_zugang_mit(listmonk):
    p = neues(listmonk)
    with als("mcp:undici"):
        p.call("senden", ARGS)
    with als("mcp:claude-code"):
        p.call("senden", ARGS)
        p.call("senden", ARGS)
    assert gebucht(p) == {"gesamt": 3, "dienst": 1, "helfer": 2}
    assert p.call("zustand", {})["heute"] == {"datum_utc": "2026-10-10", "grenze": 30, "gesendet": 3, "je_zugang": {"helfer": 2, "dienst": 1}}


def test_der_schluessel_des_zaehlers_ist_der_bisherige(listmonk):
    p = neues(listmonk)
    p.call("senden", ARGS)
    assert p._zaehler_schluessel() == SCHLUESSEL_HEUTE  # noqa: SLF001 — ein Wechsel auf dieses Plugin setzt keinen Zähler zurück


def test_neuer_utc_tag_beginnt_bei_null(listmonk):
    p = neues(listmonk, tagesgrenze=1)
    assert p.call("senden", ARGS)["ok"] is True
    assert p.call("senden", ARGS)["ok"] is False
    p._uhr = lambda: HEUTE + dt.timedelta(days=1)
    assert p.call("senden", ARGS)["ok"] is True
    assert gebucht(p, SCHLUESSEL_MORGEN)["gesamt"] == 1


def test_standardgrenze_ist_30(listmonk):
    p = neues(listmonk)
    for _ in range(30):
        assert p.call("senden", ARGS)["ok"] is True
    assert p.call("senden", ARGS)["ok"] is False
    assert len(listmonk.tx()) == 30


def test_zaehler_nicht_nutzbar_heisst_nicht_senden(listmonk):
    class Kaputt:
        def zaehlen_viele(self, *a, **k):
            raise OSError("redis weg")

        def zaehler(self, *a, **k):
            raise OSError("redis weg")

    p = neues(listmonk)
    p._ablage = Kaputt()
    r = p.call("senden", ARGS)
    assert r["ok"] is False and "Tageszähler" in r["grund"] and listmonk.tx() == []
    assert p.call("zustand", {})["heute"]["gesendet"] is None  # Lesen bleibt möglich, sagt aber ehrlich „unbekannt“


def test_unsichtbare_eigene_buchung_heisst_nicht_senden(listmonk):
    class Vergesslich(ansicht.Ablage):
        def zaehler(self, schluessel):
            return {}

    p = neues(listmonk)
    p._ablage = Vergesslich(redis=None)
    p._ablage._redis_versucht = True
    assert p.call("senden", ARGS)["ok"] is False and listmonk.tx() == []


def test_rueknahme_nach_mitternacht_trifft_den_zaehler_des_alten_tages(listmonk):
    p = neues(listmonk)

    def nach_mitternacht():
        p._uhr = lambda: HEUTE + dt.timedelta(days=1)

    listmonk.nach_tx = nach_mitternacht
    r = p.call("senden", {**ARGS, "an": ["fremd@example.org"]})  # unbekannt: 400, Buchung wird zurückgenommen
    assert r["ok"] is False
    assert gebucht(p)["gesamt"] == 0
    assert gebucht(p, SCHLUESSEL_MORGEN) == {}  # der neue Tag bleibt unberührt, kein Minus


# ============================================================================================ Fehler von Listmonk
def test_unbekannter_empfaenger_ist_ein_klarer_fehler_und_zaehlt_nicht(listmonk):
    p = neues(listmonk)
    r = p.call("senden", {**ARGS, "an": ["fremd@example.org"]})
    assert r["ok"] is False and r["error"].startswith("HTTP 400") and "Subscriber not found" in r["error"]
    assert "Abonnent" in r["satz"] and r["fehlgeschlagen"][0]["an"] == "fremd@example.org"
    assert gebucht(p)["gesamt"] == 0


def test_teilweise_gesendet_nennt_wer_ankam_und_wer_nicht(listmonk):
    p = neues(listmonk)
    r = p.call("senden", {**ARGS, "an": [STANDARD, "fremd@example.org", "ada@example.org"]})
    assert r["ok"] is False and r["angenommen"] is True
    assert r["an"] == [STANDARD, "ada@example.org"]
    assert [f["an"] for f in r["fehlgeschlagen"]] == ["fremd@example.org"]
    assert len(listmonk.tx()) == 3  # ein 400 hält die übrigen nicht auf
    assert gebucht(p)["gesamt"] == 2


def test_teilergebnis_kommt_als_tool_failed_mit_den_einzelheiten(listmonk):
    reg = hub_registry.PluginRegistry()
    hub_registry._register_one(reg, "post", neues(listmonk))
    r = hub_registry.dispatch(reg, "post.senden", {**ARGS, "an": [STANDARD, "fremd@example.org"]})
    assert r["ok"] is False and r["kind"] == "tool_failed"
    assert r["result"]["an"] == [STANDARD] and r["result"]["fehlgeschlagen"][0]["an"] == "fremd@example.org"


def test_server_fehler_bricht_ab_und_versucht_die_uebrigen_nicht(listmonk):
    listmonk.tx_antworten[STANDARD] = _fehler(500, "message push timed out")
    p = neues(listmonk)
    r = p.call("senden", {**ARGS, "an": [STANDARD, "ada@example.org", "ben@example.org"]})
    assert r["ok"] is False and r["error"].startswith("HTTP 500")
    assert len(listmonk.tx()) == 1
    assert [f["grund"] for f in r["fehlgeschlagen"][1:]] == ["nicht versucht (vorheriger Fehler)"] * 2
    assert gebucht(p)["gesamt"] == 0


def test_zugang_abgelehnt_ist_ein_satz_mit_dem_zugangsnamen(listmonk):
    listmonk.alles_antwortet_mit = _fehler(403, "invalid API credentials")
    p = neues(listmonk)
    r = p.call("senden", ARGS)
    assert r["ok"] is False and "403" in r["error"] and "„dienst“" in r["satz"] and "nichts gesendet" in r["satz"]


def test_listmonk_nicht_erreichbar_beim_nachschlagen(listmonk):
    listmonk.alles_antwortet_mit = (0, b"ConnectionRefusedError")
    p = neues(listmonk)
    r = p.call("senden", ARGS)
    assert r["ok"] is False and "antwortet gerade nicht" in r["satz"] and gebucht(p) == {}


def test_ausbleibende_antwort_beim_senden_laesst_die_buchung_stehen(listmonk):
    listmonk.tx_antworten[STANDARD] = (0, b"TimeoutError")
    p = neues(listmonk)
    r = p.call("senden", {**ARGS, "an": [STANDARD, "ada@example.org"]})
    assert r["ok"] is False and r["fehlgeschlagen"][0]["gesendet"] == "unbekannt"
    assert "kann trotzdem angenommen" in r["satz"]
    assert len(listmonk.tx()) == 1 and gebucht(p)["gesamt"] == 1  # die erste Mail kann hinausgegangen sein, die zweite nicht versucht


def test_vorlage_fehlt_heisst_einrichten_und_nichts_senden(listmonk):
    listmonk.vorlagen = [v for v in listmonk.vorlagen if v["name"] != "Mitteilung"]
    p = neues(listmonk)
    r = p.call("senden", ARGS)
    assert r["ok"] is False and "fyos-post-einrichten --ja" in r["satz"] and listmonk.tx() == [] and gebucht(p) == {}


def test_nur_eine_transaktionsvorlage_namens_mitteilung_zaehlt(listmonk):
    listmonk.vorlagen = [{"id": 9, "name": "Mitteilung", "type": "campaign"}]
    p = neues(listmonk)
    assert p.call("senden", ARGS)["ok"] is False


def test_frist_des_aufrufs_bricht_die_restlichen_empfaenger_ab(listmonk):
    uhr = iter([0.0, 0.0, 100.0, 100.0, 100.0])
    p = neues(listmonk)
    p._monoton = lambda: next(uhr)
    r = p.call("senden", {**ARGS, "an": [STANDARD, "ada@example.org", "ben@example.org"]})
    assert r["ok"] is False and r["an"] == [STANDARD]
    assert [f["grund"] for f in r["fehlgeschlagen"]] == ["nicht versucht (Frist des Aufrufs)"] * 2
    assert gebucht(p)["gesamt"] == 1


# ============================================================================================ Trockenlauf
@pytest.mark.parametrize("schalter", [{"trocken": True}, {"__dry_run__": True}])
def test_trockenlauf_sendet_nichts_und_zaehlt_nicht(listmonk, schalter):
    p = neues(listmonk)
    r = p.call("senden", {**ARGS, **schalter, "an": [STANDARD, "ada@example.org"]})
    assert r["trocken"] is True and r["dry_run"] is True and r["gesendet"] is False
    assert r["expected_side_effects"][0].startswith(STANDARD) and "würde" in r["satz"]
    assert listmonk.tx() == [] and gebucht(p) == {}
    assert {a["methode"] for a in listmonk.anfragen} == {"GET"}  # nur nachgeschlagen (Vorlage)


def test_trockenlauf_sagt_wenn_die_grenze_es_ablehnen_wuerde(listmonk):
    p = neues(listmonk, tagesgrenze=1)
    p.call("senden", ARGS)
    r = p.call("senden", {**ARGS, "trocken": True})
    assert "Tagesgrenze" in r["satz"] and "abgelehnt" in r["satz"]


def test_trockenlauf_sieht_auch_eine_fehlende_vorlage(listmonk):
    listmonk.vorlagen = []
    r = neues(listmonk).call("senden", {**ARGS, "trocken": True})
    assert r["ok"] is False and "fyos-post-einrichten --ja" in r["satz"]


# ============================================================================================ post.kampagne_entwurf
def test_entwurf_legt_nur_einen_entwurf_an(listmonk):
    p = neues(listmonk)
    with als("mcp:claude-code"):
        r = p.call("kampagne_entwurf", ENTWURF)
    assert r["ok"] is True and r["entwurf"] is True and r["status"] == "draft" and r["id"] == 8
    assert r["link"] == f"{LINK}/admin/campaigns/8" and r["listen"] == ["Intern"] and r["zugang"] == "helfer"
    (anlegen,) = listmonk.aufrufe("POST", "/api/campaigns")
    assert anlegen["daten"] == {
        "name": "Oktober",
        "subject": "Neuigkeiten",
        "lists": [1],
        "type": "regular",
        "content_type": "markdown",
        "body": "# Hallo",
        "messenger": "email",
    }  # kein send_at, kein status, kein from_email
    assert anlegen["kopf"]["Authorization"] == f"token helfer:{TOKEN['helfer']}"
    assert gebucht(p) == {}  # kein Versand, kein Zähler
    assert "Mensch" in r["satz"] and r["link"] in r["satz"]


def test_entwurf_listen_nach_nummer_und_name(listmonk):
    p = neues(listmonk)
    r = p.call("kampagne_entwurf", {**ENTWURF, "listen": [1, "FREUNDE", "2", "intern"]})
    assert listmonk.aufrufe("POST", "/api/campaigns")[0]["daten"]["lists"] == [1, 2]
    assert r["listen"] == ["Intern", "Freunde"]


@pytest.mark.parametrize("listen", [["gibt-es-nicht"], [99], [True], [], None, "", [1, "nope"]])
def test_entwurf_unbekannte_oder_fehlende_listen(listmonk, listen):
    p = neues(listmonk)
    with pytest.raises(ValueError) as e:
        p.call("kampagne_entwurf", {**ENTWURF, "listen": listen})
    assert "listen" in str(e.value)
    assert listmonk.aufrufe("POST", "/api/campaigns") == []


def test_entwurf_nennt_die_vorhandenen_listen(listmonk):
    p = neues(listmonk)
    with pytest.raises(ValueError) as e:
        p.call("kampagne_entwurf", {**ENTWURF, "listen": ["xyz"]})
    assert "Intern (1)" in str(e.value) and "Freunde (2)" in str(e.value)


def test_entwurf_format_und_vorlage(listmonk):
    p = neues(listmonk)
    p.call("kampagne_entwurf", {**ENTWURF, "format": "HTML", "vorlage": 3})
    n = listmonk.aufrufe("POST", "/api/campaigns")[0]["daten"]
    assert n["content_type"] == "html" and n["template_id"] == 3
    for falsch in ({"format": "rtf"}, {"format": "richtext"}, {"vorlage": 0}, {"vorlage": "x"}, {"vorlage": True}, {"vorlage": -2}):
        with pytest.raises(ValueError):
            p.call("kampagne_entwurf", {**ENTWURF, **falsch})


@pytest.mark.parametrize(
    "falsch",
    [
        {"name": ""},
        {"name": "x" * 201},
        {"name": None},
        {"betreff": ""},
        {"betreff": "b" * 201},
        {"text": ""},
        {"text": "   "},
        {"text": "t" * 100_001},
        {"text": 5},
    ],
)
def test_entwurf_eingaben_werden_geprueft(listmonk, falsch):
    p = neues(listmonk)
    with pytest.raises(ValueError):
        p.call("kampagne_entwurf", {**ENTWURF, **falsch})
    assert listmonk.aufrufe("POST", "/api/campaigns") == []


def test_entwurf_name_und_betreff_sind_einzeilig(listmonk):
    p = neues(listmonk)
    p.call("kampagne_entwurf", {**ENTWURF, "name": "Zwei\nZeilen", "betreff": "Bcc:\r\nx"})
    n = listmonk.aufrufe("POST", "/api/campaigns")[0]["daten"]
    assert n["name"] == "Zwei Zeilen" and n["subject"] == "Bcc: x"


def test_entwurf_text_bleibt_unverfaelscht_bis_auf_nul(listmonk):
    p = neues(listmonk)
    text = "# Titel\n\n<b>fett</b> {{ .Subscriber.Name }}\x00"
    p.call("kampagne_entwurf", {**ENTWURF, "text": text})
    assert listmonk.aufrufe("POST", "/api/campaigns")[0]["daten"]["body"] == text.replace("\x00", "")


def test_entwurf_der_nicht_draft_ist_wird_gemeldet(listmonk):
    listmonk.status_beim_anlegen = "running"
    r = neues(listmonk).call("kampagne_entwurf", ENTWURF)
    assert r["ok"] is False and "draft" in r["error"] and r["id"] == 8 and r["link"].endswith("/campaigns/8")


def test_entwurf_trockenlauf_legt_nichts_an(listmonk):
    p = neues(listmonk)
    for schalter in ({"trocken": True}, {"__dry_run__": True}):
        r = p.call("kampagne_entwurf", {**ENTWURF, **schalter})
        assert r["trocken"] is True and r["angelegt"] is False and r["dry_run"] is True
    assert listmonk.aufrufe("POST", "/api/campaigns") == []


def test_entwurf_listmonk_fehler_ist_ok_false(listmonk):
    listmonk.alles_antwortet_mit = _fehler(500, "db weg")
    r = neues(listmonk).call("kampagne_entwurf", ENTWURF)
    assert r["ok"] is False and "500" in r["error"]


# ============================================================================================ post.kampagne_testen
def test_testen_liest_zuerst_die_kampagne_und_schickt_ihre_felder(listmonk):
    p = neues(listmonk)
    r = p.call("kampagne_testen", {"id": 7})
    assert r["ok"] is True and r["angenommen"] is True and r["an"] == [STANDARD] and r["kampagne"] == "Oktober-Brief"
    pfade = [(a["methode"], a["pfad"]) for a in listmonk.anfragen]
    assert pfade.index(("GET", "/api/campaigns/7")) < pfade.index(("POST", "/api/campaigns/7/test"))
    (test,) = listmonk.aufrufe("POST", "/api/campaigns/7/test")
    assert test["daten"] == {
        "name": "Oktober-Brief",
        "subject": "Neues im Oktober",
        "from_email": "FYOS Post <absender@example.org>",
        "body": "# Hallo {{ .Subscriber.FirstName }}",
        "altbody": None,
        "content_type": "markdown",
        "messenger": "email",
        "headers": [],
        "template_id": 1,
        "lists": [1],
        "subscribers": [STANDARD],
    }
    assert gebucht(p)["gesamt"] == 1
    assert "heißt nicht" in r["satz"] and "zugestellt" in r["satz"]  # „angenommen“ ist nicht „zugestellt“


def test_testen_unbekannte_kampagne(listmonk):
    p = neues(listmonk)
    r = p.call("kampagne_testen", {"id": 99})
    assert r["ok"] is False and "gibt es nicht" in r["grund"]
    assert listmonk.aufrufe("POST", "/api/campaigns/99/test") == [] and gebucht(p) == {}


def test_testen_kampagne_ohne_liste(listmonk):
    listmonk.kampagnen[7]["lists"] = []
    p = neues(listmonk)
    r = p.call("kampagne_testen", {"id": 7})
    assert r["ok"] is False and "keine Liste" in r["grund"] and "/admin/campaigns/7" in r["satz"]
    assert listmonk.aufrufe("POST", "/api/campaigns/7/test") == []


@pytest.mark.parametrize("kid", [0, -1, "x", True, None, 1.5])
def test_testen_id_muss_eine_nummer_sein(listmonk, kid):
    with pytest.raises(ValueError):
        neues(listmonk).call("kampagne_testen", {"id": kid})
    assert listmonk.anfragen == []


def test_testen_hoechstens_drei_empfaenger(listmonk):
    p = neues(listmonk)
    with pytest.raises(ValueError):
        p.call("kampagne_testen", {"id": 7, "an": [STANDARD, "ada@example.org", "ben@example.org", "cora@example.org"]})
    assert p.call("kampagne_testen", {"id": 7, "an": [STANDARD, "ada@example.org", "ben@example.org"]})["ok"] is True
    assert gebucht(p)["gesamt"] == 3


def test_testen_zaehlt_zur_tagesgrenze(listmonk):
    p = neues(listmonk, tagesgrenze=1)
    assert p.call("kampagne_testen", {"id": 7})["ok"] is True
    r = p.call("kampagne_testen", {"id": 7})
    assert r["ok"] is False and "Tagesgrenze" in r["grund"]
    assert len(listmonk.aufrufe("POST", "/api/campaigns/7/test")) == 1
    assert p.call("senden", ARGS)["ok"] is False  # die Grenze gilt gemeinsam mit post.senden


def test_testen_ohne_bekannte_adresse_nimmt_die_buchung_zurueck(listmonk):
    p = neues(listmonk)
    r = p.call("kampagne_testen", {"id": 7, "an": ["fremd@example.org"]})
    assert r["ok"] is False and "Abonnent" in r["satz"] and gebucht(p)["gesamt"] == 0


def test_testen_trockenlauf_liest_aber_sendet_nicht(listmonk):
    p = neues(listmonk)
    for schalter in ({"trocken": True}, {"__dry_run__": True}):
        r = p.call("kampagne_testen", {"id": 7, **schalter})
        assert r["trocken"] is True and r["gesendet"] is False and r["dry_run"] is True
    assert listmonk.aufrufe("POST", "/api/campaigns/7/test") == [] and gebucht(p) == {}


def test_kampagnen_werden_nie_gestartet(listmonk):
    """Über alle schreibenden Werkzeuge: keine andere Methode als GET und POST, nie ein Pfad auf /status (die Attrappe prüft es bei
    jeder Anfrage, hier der Blick über den ganzen Lauf)."""
    p = neues(listmonk)
    with als("mcp:claude-code"):
        p.call("kampagne_entwurf", ENTWURF)
        p.call("kampagne_testen", {"id": 8})
        p.call("kampagne_testen", {"id": 7})
        p.call("senden", ARGS)
    assert {a["methode"] for a in listmonk.anfragen} <= {"GET", "POST"}
    assert not [a for a in listmonk.anfragen if "status" in a["pfad"]]
    assert not [a for a in listmonk.anfragen if a["daten"] and ("send_at" in a["daten"] or "status" in a["daten"])]


def _api_aufrufe_im_quelltext() -> list[tuple[str, str]]:
    """(Methode, Pfad mit {} für Einsetzungen) jedes `self._api(...)`-Aufrufs im Quelltext des Plugins."""
    baum = ast.parse(Path(post.__file__).read_text(encoding="utf-8"))
    gefunden = []
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Call) and isinstance(knoten.func, ast.Attribute) and knoten.func.attr == "_api":
            methode, pfad = knoten.args[1], knoten.args[2]
            assert isinstance(methode, ast.Constant)
            if isinstance(pfad, ast.Constant):
                text = pfad.value
            else:
                assert isinstance(pfad, ast.JoinedStr)
                text = "".join(t.value if isinstance(t, ast.Constant) else "{}" for t in pfad.values)
            gefunden.append((methode.value, text))
    return gefunden


def test_quelltext_ruft_nur_diese_pfade_auf():
    aufrufe = set(_api_aufrufe_im_quelltext())
    assert aufrufe == {
        ("GET", "/api/lists?per_page=all"),
        ("GET", "/api/templates?no_body=true"),
        ("GET", "/api/campaigns?page=1&per_page=5&order_by=created_at&order=DESC&no_body=true"),
        ("GET", "/api/campaigns/{}"),
        ("GET", "/api/profile"),
        ("POST", "/api/tx"),
        ("POST", "/api/campaigns"),
        ("POST", "/api/campaigns/{}/test"),
    }
    assert not [m for m, _ in aufrufe if m not in ("GET", "POST")]
    assert not [p for _, p in aufrufe if "status" in p]


# ============================================================================================ post.zustand
def test_zustand_zeigt_den_dienst_ohne_adressen(listmonk):
    p = neues(listmonk)
    with als("mcp:undici"):
        r = p.call("zustand", {})
    assert r["quelle_ok"] is True and "ok" not in r and "error" not in r
    assert r["zugang"] == "dienst" and r["link"] == LINK and r["mitteilung_vorhanden"] is True
    assert r["listen"][0] == {"id": 1, "name": "Intern", "art": "private", "abonnenten": 1}
    assert {"id": 6, "name": "Mitteilung", "art": "tx"} in r["vorlagen"]
    assert r["kampagnen"][0]["id"] == 7 and r["kampagnen"][0]["status"] == "draft" and r["kampagnen"][0]["betreff"] == "Neues im Oktober"
    assert r["heute"] == {"datum_utc": "2026-10-10", "grenze": 30, "gesendet": 0, "je_zugang": {"helfer": 0, "dienst": 0}}
    assert "antwortet" in r["satz"] and "Heute 0 von 30" in r["satz"]
    assert "@" not in json.dumps(r)  # keine Adressen
    assert {a["methode"] for a in listmonk.anfragen} == {"GET"}


def test_zustand_voll_hat_mehr_felder(listmonk):
    p = neues(listmonk)
    kurz, voll = p.call("zustand", {}), p.call("zustand", {"voll": True})
    assert set(voll["listen"][0]) > set(kurz["listen"][0]) and set(voll["kampagnen"][0]) > set(kurz["kampagnen"][0])
    assert voll["kampagnen"][0]["link"].endswith("/admin/campaigns/7") and voll["kampagnen"][0]["listen"] == ["Intern"]


def test_ohne_link_gibt_es_nirgends_einen_link(listmonk, monkeypatch):
    monkeypatch.delenv("FYOS_POST_LINK")
    p = neues(listmonk)
    z = p.call("zustand", {"voll": True})
    assert z["quelle_ok"] is True and "link" not in z and "link" not in z["kampagnen"][0]
    e = p.call("kampagne_entwurf", ENTWURF)
    assert e["ok"] is True and "link" not in e and e["satz"].endswith("Listmonk-Oberfläche.")
    listmonk.status_beim_anlegen = "running"
    assert "link" not in p.call("kampagne_entwurf", ENTWURF)
    listmonk.kampagnen[7]["lists"] = []
    k = p.call("kampagne_testen", {"id": 7})
    assert k["ok"] is False and "http" not in json.dumps(k)


def test_link_muss_http_oder_https_sein(listmonk, monkeypatch):
    monkeypatch.setenv("FYOS_POST_LINK", "javascript:alert(1)")
    assert "link" not in neues(listmonk).call("zustand", {})


def test_zustand_ohne_mitteilung_sagt_was_zu_tun_ist(listmonk):
    listmonk.vorlagen = [v for v in listmonk.vorlagen if v["name"] != "Mitteilung"]
    r = neues(listmonk).call("zustand", {})
    assert r["mitteilung_vorhanden"] is False and "fyos-post-einrichten --ja" in r["satz"]


@pytest.mark.parametrize("antwort", [(0, b"ConnectionRefusedError"), (500, b"{}"), (403, b'{"message":"nein"}'), (200, b"<html>")])
def test_zustand_ausfall_ist_ein_zustand_keine_ausnahme(listmonk, antwort):
    listmonk.alles_antwortet_mit = antwort
    r = neues(listmonk).call("zustand", {})
    assert r["quelle_ok"] is False and r["grund"] and r["satz"] and "stand" in r and "eingerichtet" not in r
    assert "ok" not in r and "error" not in r  # der Hub meldet es als gelungenen Aufruf mit Befund


def test_zustand_kappt_lange_kampagnentexte(listmonk):
    listmonk.kampagnen[7]["subject"] = "S" * 500
    listmonk.kampagnen[7]["name"] = "N" * 500
    k = neues(listmonk).call("zustand", {})["kampagnen"][0]
    assert len(k["betreff"]) == 100 and len(k["name"]) == 100


# ============================================================================================ Health
def test_health_ist_tief_und_prueft_beide_token(listmonk):
    p = neues(listmonk)
    assert p.HEALTH_TIEF is True
    h = p.health()
    assert h["status"] == "ok" and h["probe"] == "profil"
    assert [a["pfad"] for a in listmonk.anfragen] == ["/health", "/api/profile", "/api/profile"]
    assert {a["kopf"].get("Authorization") for a in listmonk.anfragen[1:]} == {f"token helfer:{TOKEN['helfer']}", f"token dienst:{TOKEN['dienst']}"}


def test_health_ohne_token_nennt_wer_fehlt_und_fragt_nicht(listmonk, monkeypatch):
    monkeypatch.delenv("FYOS_POST_TOKEN_HELFER")
    h = neues(listmonk).health()
    assert h["ok"] is False and h["status"] == "unhealthy" and "FYOS_POST_TOKEN_HELFER" in h["reason"] and "DIENST" not in h["reason"]
    assert listmonk.anfragen == []


def test_health_falscher_benutzer_zum_token(listmonk):
    def falsch(methode, url, kopf, daten, frist):
        if url.endswith("/api/profile"):
            return _antwort({"username": "jemand-anders"})
        return listmonk(methode, url, kopf, daten, frist)

    p = neues(None)
    p._http = falsch
    h = p.health()
    assert h["status"] == "unhealthy" and "gehört nicht" in h["reason"]


def test_health_token_wird_abgelehnt(listmonk):
    def abgelehnt(methode, url, kopf, daten, frist):
        return _fehler(403, "invalid API credentials") if url.endswith("/api/profile") else _antwort(True)

    p = neues(None)
    p._http = abgelehnt
    h = p.health()
    assert h["status"] == "unhealthy" and "403" in h["reason"] and TOKEN["helfer"] not in json.dumps(h)


def test_health_listmonk_weg_ist_unreachable(listmonk):
    p = neues(None)
    p._http = lambda *a: (0, b"ConnectionRefusedError")
    assert p.health()["status"] == "unreachable"


def test_health_wirft_nie(listmonk):
    def kaputt(*a):
        raise RuntimeError("unerwartet")

    p = neues(None)
    p._http = kaputt
    h = p.health()
    assert h.get("ok") is False or h["status"] in ("unhealthy", "unreachable")


def test_health_frist_reicht_auch_bei_vielen_zugaengen(listmonk, monkeypatch):
    monkeypatch.setenv("FYOS_POST_ZUGAENGE", "os=a,chat=b,code=c,agent=d,dienste=e,*=f")
    for z in "abcdef":
        monkeypatch.setenv(f"FYOS_POST_TOKEN_{z.upper()}", f"token-{z}-0123456789")
        TOKEN[z] = f"token-{z}-0123456789"
    try:
        p = neues(listmonk)
        assert p.health()["status"] == "ok"
    finally:
        for z in "abcdef":
            TOKEN.pop(z, None)
    fristen = [a["frist"] for a in listmonk.anfragen]
    assert len(fristen) == 7 and sum(fristen) <= 8.0  # das Zeitlimit des Hubs für den ganzen Bericht
    assert max(fristen) <= post.HEALTH_FRIST_S


# ============================================================================================ Schemata und Einbindung
def test_schemata_sind_vollstaendig():
    werkzeuge = {t.name: t for t in post.PostPlugin().describe_tools()}
    assert set(werkzeuge) == {"zustand", "senden", "kampagne_entwurf", "kampagne_testen"}
    for t in werkzeuge.values():
        s = t.input_schema
        assert s["type"] == "object" and s["additionalProperties"] is False
        assert t.title and len(t.description) > 60
        for feld, d in s["properties"].items():
            assert d.get("description"), f"{t.name}.{feld} ohne description"
    assert werkzeuge["senden"].input_schema["required"] == ["betreff", "text"]
    assert werkzeuge["kampagne_entwurf"].input_schema["required"] == ["name", "betreff", "listen", "text"]
    assert werkzeuge["kampagne_testen"].input_schema["required"] == ["id"]
    assert "required" not in werkzeuge["zustand"].input_schema
    assert werkzeuge["senden"].input_schema["properties"]["betreff"]["maxLength"] == post.BETREFF_MAX == 150
    assert werkzeuge["senden"].input_schema["properties"]["text"]["maxLength"] == post.TEXT_MAX == 5000
    assert werkzeuge["senden"].input_schema["properties"]["an"]["maxItems"] == post.MAX_EMPFAENGER == 5
    assert werkzeuge["kampagne_testen"].input_schema["properties"]["an"]["maxItems"] == post.MAX_TEST_EMPFAENGER == 3


def test_beschreibungen_nennen_keine_adresse_und_die_eingestellte_grenze(listmonk, monkeypatch):
    text = json.dumps([(t.description, t.input_schema) for t in neues(listmonk).describe_tools()], ensure_ascii=False)
    assert STANDARD not in text and "@" not in text  # der Standardempfänger steht in keiner Werkzeugbeschreibung
    monkeypatch.setenv("FYOS_POST_TAGESGRENZE", "12")
    senden = {t.name: t for t in neues(listmonk).describe_tools()}["senden"]
    assert "höchstens 12 Sendungen" in senden.description


def test_scope_der_werkzeuge():
    t = {x.name: scope_from_tags(x.tags) for x in post.PostPlugin().describe_tools()}
    assert t == {"zustand": "READ", "senden": "EXTERN", "kampagne_entwurf": "WRITE", "kampagne_testen": "EXTERN"}


def test_kein_werkzeug_ist_ueber_den_browser_erreichbar():
    for x in post.PostPlugin().describe_tools():
        assert hub_registry.ist_flaechen_werkzeug(x) is False
        assert x.meta is None and "flaeche" not in x.tags


def test_argumente_gelten_als_vertraulich():
    assert post.PostPlugin.argumente_vertraulich is True  # Betreff und Text gehören nicht auf den Ereignisbus


def test_zeitlimit_des_hubs_reicht_fuer_den_schlimmsten_fall():
    # Vorlage (1 Anfrage) + 5 Empfänger, jede höchstens FRIST_S; die Gesamtfrist stoppt vorher
    assert post.GESAMT_FRIST_S + post.FRIST_S < hub_registry.AUFRUF_ZEITLIMIT_S
    assert post.HEALTH_GESAMT_S < 8.0


def test_unbekanntes_werkzeug_und_leere_argumente(listmonk):
    p = neues(listmonk)
    with pytest.raises(ValueError):
        p.call("loeschen", {})
    assert p.call("zustand", None)["quelle_ok"] is True


def test_plugin_laesst_sich_ohne_umgebung_bauen_und_registrieren():
    reg = hub_registry.PluginRegistry()
    hub_registry._register_one(reg, "post", post.PostPlugin())
    assert sorted(reg.tools) == ["post.kampagne_entwurf", "post.kampagne_testen", "post.senden", "post.zustand"]
    r = hub_registry.dispatch(reg, "post.zustand", {})
    assert r["ok"] is True and r["result"]["quelle_ok"] is False  # ohne Adresse: Zustand „nicht eingerichtet“, kein Absturz


# ============================================================================================ echtes HTTP auf 127.0.0.1
class Loopback:
    """Ein echter HTTP-Server auf 127.0.0.1, der die Attrappe bedient (oder eine feste Antwort gibt)."""

    def __init__(self, bedienung, weiterleitung_nach: str | None = None):
        self.mitgeschnitten: list[dict] = []
        aussen = self

        class H(BaseHTTPRequestHandler):
            def _lauf(self):
                laenge = int(self.headers.get("Content-Length") or 0)
                roh = self.rfile.read(laenge) if laenge else None
                aussen.mitgeschnitten.append({"methode": self.command, "pfad": self.path, "kopf": dict(self.headers), "roh": roh})
                if weiterleitung_nach and self.path.startswith("/weiter"):
                    self.send_response(302)
                    self.send_header("Location", weiterleitung_nach)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, koerper = bedienung(self.command, f"http://127.0.0.1{self.path}", dict(self.headers), roh, 5)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(koerper)))
                self.end_headers()
                self.wfile.write(koerper)

            do_GET = do_POST = _lauf

            def log_message(self, *a):  # noqa: D401
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def schliessen(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server():
    gestartet: list[Loopback] = []

    def starten(bedienung, weiterleitung_nach=None):
        s = Loopback(bedienung, weiterleitung_nach)
        gestartet.append(s)
        return s

    yield starten
    for s in gestartet:
        s.schliessen()


def test_auf_dem_draht_kopfzeilen_und_umlaute(listmonk, server):
    s = server(listmonk)
    p = neues(None, url=s.url)  # kein Haken: der echte Weg über urllib
    with als("mcp:claude-code"):
        r = p.call("senden", {"betreff": "Grüße", "text": "Straße\nÜbergröße"})
    assert r["ok"] is True
    tx = [a for a in s.mitgeschnitten if a["pfad"] == "/api/tx"][0]
    assert tx["kopf"]["Authorization"] == f"token helfer:{TOKEN['helfer']}"
    assert tx["kopf"]["Content-Type"] == "application/json" and tx["kopf"]["Accept"] == "application/json"
    gesendet = json.loads(tx["roh"].decode("utf-8"))
    assert gesendet["data"]["betreff"] == "Grüße" and gesendet["data"]["zeilen"] == ["Straße", "Übergröße"]
    assert gesendet["subscriber_mode"] == "default"


def test_auf_dem_draht_zustand_und_kampagne(listmonk, server):
    s = server(listmonk)
    p = neues(None, url=s.url)
    assert p.call("zustand", {})["quelle_ok"] is True
    assert p.call("kampagne_entwurf", ENTWURF)["entwurf"] is True
    assert p.call("kampagne_testen", {"id": 7})["angenommen"] is True


def test_umleitungen_werden_nicht_befolgt_das_token_bleibt_bei_listmonk(listmonk, server):
    fremd = server(lambda *a: _antwort(True))
    s = server(listmonk, weiterleitung_nach=f"{fremd.url}/geklaut")
    p = neues(None, url=s.url)
    with pytest.raises(post.PostFehler) as e:
        p._api("dienst", "GET", "/weiter")
    assert e.value.status == 302
    assert fremd.mitgeschnitten == []  # die Adresse, auf die verwiesen wurde, hat nie etwas bekommen
    r = p.call("zustand", {})  # und ein normaler Aufruf läuft weiter
    assert r["quelle_ok"] is True


def test_umleitungshandler_ist_der_einzige_und_proxies_sind_aus():
    handler = [h for h in post._OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]  # noqa: SLF001
    assert len(handler) == 1 and isinstance(handler[0], post._KeineUmleitung)
    req = urllib.request.Request("http://listmonk/api/lists", headers={"Authorization": "token x:y"})
    assert handler[0].redirect_request(req, None, 302, "Found", {}, "http://anders.example/") is None
    assert not [h for h in post._OPENER.handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies]


def test_verbindungsfehler_ist_ein_zustand_kein_absturz(listmonk):
    p = neues(None, url="http://127.0.0.1:1")  # nichts hört zu
    r = p.call("zustand", {})
    assert r["quelle_ok"] is False and "ConnectionRefused" in r["grund"]
    s = p.call("senden", ARGS)
    assert s["ok"] is False and gebucht(p) == {}


def test_kaputte_json_antwort_ist_ein_fehler_kein_absturz(listmonk, server):
    s = server(lambda *a: (200, b"<html>Bad Gateway</html>"))
    p = neues(None, url=s.url)
    r = p.call("zustand", {})
    assert r["quelle_ok"] is False and "JSON" in r["grund"]


def test_nichts_im_quelltext_der_dienst_oder_besitzer_verraet():
    """Das Plugin trägt keine Adresse, keinen Namen und keinen Dienst eines bestimmten Betreibers."""
    text = Path(post.__file__).read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}", text.replace("a@example.org", ""))  # keine Mail-Adresse
    assert not re.search(r"https?://[a-z0-9.-]+\.[a-z]{2,}", text)  # keine feste Adresse
    assert "svc.cluster.local" not in text and ".lan" not in text
