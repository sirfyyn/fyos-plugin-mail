"""fyos-post-einrichten — Listmonk für das Plugin ``post`` einrichten (wiederholbar, Trockenlauf als Standard).

Listmonk hält Rollen, API-Benutzer und Vorlagen in seiner Datenbank; sie lassen sich nicht als Datei beschreiben. Dieses
Kommando setzt genau das, was das Plugin ``post`` braucht, über die API von Listmonk, und gibt nie ein Geheimnis aus.

## Was es tut

    D  Abonnent    nur mit ``--abonnent ADRESSE``: die Adresse in die private Liste „Intern“ (wird angelegt), bestätigt. Der
                   Transaktionsversand von ``post.senden`` geht nur an vorhandene Abonnenten.
    E  Vorlage     „Mitteilung“ (Transaktion): Betreff ``{{ .Tx.Data.betreff }}``, der Text Zeile für Zeile, darunter wer die
                   Mail wann geschickt hat. Die Schlüssel der Daten (MITTEILUNG_DATEN_SCHLUESSEL) sind der Vertrag mit dem
                   Plugin; tests/test_post_einrichten.py hält beide Seiten gleich.
    F  Rolle       ``arbeit``: Listen, Abonnenten, Vorlagen und Kampagnen vorbereiten und testen, Transaktionsmails senden;
                   OHNE ``campaigns:send`` (Kampagnen starten), ohne Importe und ``sql_query``, ohne Benutzer, Rollen und
                   Einstellungen.
    G  Zugänge     je Zugang aus ``FYOS_POST_ZUGAENGE`` (Vorgabe ``*=fyos``) ein API-Benutzer mit der Rolle ``arbeit``. Das
                   Token steht nur in der Antwort beim Anlegen und geht ausschließlich in die Datei ``--token-datei`` (Zeilen
                   ``FYOS_POST_TOKEN_<ZUGANG>=…``); ein verlorenes Token lässt sich nicht nachlesen: ``--token-neu ZUGANG``
                   löscht den Zugang und legt ihn neu an. Bestehende Zugänge prüft das Kommando mit dem Token aus der Umgebung.

Nicht dabei, mit Absicht: SMTP-Einstellungen (Absender und Mail-Weg stellst du in Listmonk ein), Beispieldaten, eine Testmail.

## Aufruf

    fyos-post-einrichten                                    # Trockenlauf: liest nur, sagt was zu tun wäre
    fyos-post-einrichten --ja --token-datei ./token.env     # ausführen (wiederholbar)
    fyos-post-einrichten --ja --abonnent dein@example.org   # dazu einen ersten Abonnenten
    fyos-post-einrichten --ja --token-datei ./neu.env --token-neu fyos

Verbindung: ``--url`` oder ``FYOS_POST_URL`` (Alias ``POST_URL``), der Admin-Zugang aus ``FYOS_POST_ADMIN_NUTZER`` und
``FYOS_POST_ADMIN_PASSWORT`` (bleibt im Speicher). Der Trockenlauf meldet sich an (das legt in Listmonk eine Sitzung an) und
liest, mehr nicht.

Exit: 0 alles gut, 1 Fehler, 2 Befunde (z. B. ein Token, das nicht mehr passt).
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple

from .post import _ADRESSE, PostFehler, token_name, zugaenge_lesen

LISTE_INTERN = "Intern"

#: Die allgemeine Mitteilung für ``post.senden``. Name, Betreff und Schlüssel sind der Vertrag mit dem Plugin.
MITTEILUNG_NAME = "Mitteilung"
MITTEILUNG_BETREFF = "{{ .Tx.Data.betreff }}"
MITTEILUNG_DATEN_SCHLUESSEL = ("betreff", "zeilen", "von", "zeit", "kennung")

ROLLE_ARBEIT = "arbeit"
ROLLEN: dict[str, list[str]] = {
    ROLLE_ARBEIT: [
        "lists:get_all",
        "lists:manage_all",
        "subscribers:get",
        "subscribers:get_all",
        "subscribers:manage",
        "tx:send",
        "campaigns:get",
        "campaigns:get_all",
        "campaigns:get_analytics",
        "campaigns:manage",
        "campaigns:manage_all",
        "templates:get",
        "templates:manage",
        "media:get",
        "media:manage",
        "bounces:get",
    ],
}

#: Die Mitteilung: Text Zeile für Zeile (`zeilen`), darunter wer sie wann geschickt hat. Nur Ausgabe und eine Schleife, kein
#: Aufruf, keine Teilvorlage; Listmonk setzt die Werte HTML-sicher ein (html/template), Text bleibt Text.
MITTEILUNG_HTML = """\
<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:16px;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1b1b1b;background:#ffffff;">
  <div style="margin:0 0 18px 0;">
    {{ range .Tx.Data.zeilen }}{{ . }}<br>
    {{ end }}
  </div>
  <p style="margin:0;font-size:12px;color:#8a8a8a;">Von {{ .Tx.Data.von }} über FYOS Post, {{ .Tx.Data.zeit }}. Kennung {{ .Tx.Data.kennung }}.</p>
</body>
</html>
"""


class Fehler(Exception):
    """Ein Schritt ist gescheitert. Der Text enthält nie ein Geheimnis."""


class Antwort(NamedTuple):
    status: int
    cookies: list[str]  # Set-Cookie-Zeilen
    koerper: bytes


Transport = Callable[[str, str, dict[str, str], "bytes | None"], Antwort]
Ausgabe = Callable[[str], None]


def unkenntlich(text: str, geheimnisse: set[str]) -> str:
    """Ersetzt jedes bekannte Geheimnis im Text — für Fehlermeldungen, die fremde Ausgaben weitergeben."""
    for g in sorted((g for g in geheimnisse if g), key=len, reverse=True):
        text = text.replace(g, "***")
    return text


# ── HTTP gegen Listmonk ──────────────────────────────────────────────────────────────────────────────────────
def http_transport(basis: str, frist: float = 30.0) -> Transport:
    """Der echte Weg: eine Verbindung je Anfrage (Listmonk lädt sich nach dem Speichern selbst neu); http oder https,
    keine Umleitung, kein Proxy."""
    teile = urllib.parse.urlsplit(basis)
    if teile.scheme not in ("http", "https") or not teile.hostname:
        raise Fehler("Die Adresse von Listmonk muss mit http:// oder https:// beginnen")
    https = teile.scheme == "https"
    host, port = teile.hostname, teile.port or (443 if https else 80)
    vorsatz = teile.path.rstrip("/")

    def senden(methode: str, pfad: str, kopf: dict[str, str], koerper: bytes | None) -> Antwort:
        if https:
            c: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=frist, context=ssl.create_default_context())
        else:
            c = http.client.HTTPConnection(host, port, timeout=frist)
        try:
            c.request(methode, vorsatz + pfad, body=koerper, headers=kopf)
            r = c.getresponse()
            daten = r.read()
            return Antwort(r.status, list(r.msg.get_all("Set-Cookie") or []), daten)
        finally:
            c.close()

    return senden


class Listmonk:
    """Kleiner Client für die API von Listmonk. Ändernde Aufrufe gehen im Trockenlauf nicht hinaus."""

    def __init__(self, transport: Transport, ja: bool, token: str | None = None, schlafen: Callable[[float], None] = time.sleep):
        self.transport = transport
        self.ja = ja
        self.token = token  # "benutzer:token" für `Authorization: token …`
        self.cookies: dict[str, str] = {}
        self.schlafen = schlafen

    def _kopf(self, inhalt: str | None) -> dict[str, str]:
        kopf = {"Accept": "application/json"}
        if inhalt:
            kopf["Content-Type"] = inhalt
        if self.token:
            kopf["Authorization"] = f"token {self.token}"
        elif self.cookies:
            kopf["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return kopf

    @staticmethod
    def _meldung(a: Antwort) -> str:
        try:
            d = json.loads(a.koerper.decode("utf-8", "replace"))
            if isinstance(d, dict) and d.get("message"):
                return str(d["message"])[:200]
        except ValueError:
            pass
        return a.koerper.decode("utf-8", "replace").strip()[:120] or "ohne Text"

    def _anfrage(self, methode: str, pfad: str, daten: Any = None) -> Any:
        koerper = json.dumps(daten, ensure_ascii=False).encode("utf-8") if daten is not None else None
        a = self.transport(methode, pfad, self._kopf("application/json" if koerper is not None else None), koerper)
        if not 200 <= a.status < 300:
            raise Fehler(f"{methode} {pfad.split('?')[0]}: HTTP {a.status}: {self._meldung(a)}")
        try:
            d = json.loads(a.koerper.decode("utf-8")) if a.koerper else {}
        except ValueError as e:
            raise Fehler(f"{methode} {pfad.split('?')[0]}: keine JSON-Antwort") from e
        return d.get("data", d) if isinstance(d, dict) else d

    def lesen(self, pfad: str, **abfrage: str) -> Any:
        if abfrage:
            pfad += "?" + urllib.parse.urlencode(abfrage)
        return self._anfrage("GET", pfad)

    def aendern(self, methode: str, pfad: str, daten: Any = None) -> Any:
        """POST/PUT/DELETE. Im Trockenlauf wird nichts gesendet; der Rückgabewert ist dann None."""
        if not self.ja:
            return None
        return self._anfrage(methode, pfad, daten)

    def anmelden(self, benutzer: str, passwort: str) -> None:
        form = urllib.parse.urlencode({"username": benutzer, "password": passwort, "next": "/admin"}).encode()
        a = self.transport("POST", "/admin/login", {"Content-Type": "application/x-www-form-urlencoded"}, form)
        gesetzt: dict[str, str] = {}
        for zeile in a.cookies:
            name, _, rest = zeile.partition("=")
            gesetzt[name.strip()] = rest.split(";", 1)[0]
        if a.status not in (302, 303) or "session" not in gesetzt:
            raise Fehler(f"Anmeldung als {benutzer} abgewiesen (HTTP {a.status}): stimmen FYOS_POST_ADMIN_NUTZER und FYOS_POST_ADMIN_PASSWORT?")
        self.cookies = {"session": gesetzt["session"]}

    def bereit(self) -> bool:
        try:
            a = self.transport("GET", "/health", {"Accept": "application/json"}, None)
        except (OSError, http.client.HTTPException):
            return False
        return a.status == 200

    def warten_bis_bereit(self, frist_s: float = 60.0, schritt_s: float = 1.0) -> None:
        t_ende = time.monotonic() + frist_s
        while not self.bereit():
            if time.monotonic() >= t_ende:
                raise Fehler(f"Listmonk antwortet nach {frist_s:.0f} s nicht (/health)")
            self.schlafen(schritt_s)


def ergebnisse(daten: Any) -> list[dict]:
    """Listen-Antworten: mal {"results": [...]}, mal eine blanke Liste."""
    if isinstance(daten, dict):
        return [d for d in (daten.get("results") or []) if isinstance(d, dict)]
    return [d for d in (daten or []) if isinstance(d, dict)]


# ── Token-Datei ──────────────────────────────────────────────────────────────────────────────────────────────
class TokenDatei:
    """Nimmt neue Token auf: Datei mit Rechten 0600, nie eine vorhandene überschreiben, nie etwas ausgeben außer dem Pfad."""

    def __init__(self, pfad: Path | None):
        self.pfad = pfad
        self._f: Any = None
        self.geschrieben = 0

    def pruefen(self) -> None:
        """Vor jeder Änderung: ist die Datei brauchbar? Wirft ``Fehler``."""
        if self.pfad is None:
            raise Fehler("Neue Zugänge brauchen --token-datei PFAD: das Token steht nur beim Anlegen in der Antwort von Listmonk")
        if self.pfad.exists():
            raise Fehler(f"{self.pfad} gibt es schon: nichts überschrieben (Datei zuerst einzeln löschen oder einen anderen Pfad wählen)")

    def schreiben(self, zugang: str, token: str) -> None:
        assert self.pfad is not None
        if self._f is None:
            try:
                fd = os.open(self.pfad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raise Fehler(f"{self.pfad} gibt es schon: nichts überschrieben") from None
            except OSError as e:
                raise Fehler(f"{self.pfad} lässt sich nicht anlegen ({type(e).__name__})") from None
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass  # Dateisysteme ohne Rechte (selten); angelegt wurde die Datei mit 0600
            self._f = os.fdopen(fd, "w", encoding="utf-8")
        self._f.write(f"FYOS_POST_TOKEN_{token_name(zugang)}={token}\n")
        self._f.flush()
        self.geschrieben += 1

    def schliessen(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


# ── Schritte ─────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Zustand:
    intern_id: int | None = None
    rollen: dict[str, int | None] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)  # Zugang -> neues Token; nur im Speicher
    befunde: list[str] = field(default_factory=list)

    def geheimnisse(self) -> set[str]:
        return set(self.tokens.values())


def schritt_abonnent(c: Listmonk, z: Zustand, ausgabe: Ausgabe, adresse: str | None) -> None:
    if not adresse:
        return
    liste = next((l for l in ergebnisse(c.lesen("/api/lists", per_page="all")) if l.get("name") == LISTE_INTERN), None)
    if liste is not None:
        z.intern_id = liste["id"]
        if (liste.get("type"), liste.get("optin"), liste.get("status")) != ("private", "single", "active"):
            z.befunde.append(f"Liste „{LISTE_INTERN}“ ist nicht privat/einfach/aktiv: nicht angefasst")
        ausgabe(f"D  Liste „{LISTE_INTERN}“: vorhanden (Nr. {z.intern_id})")
    else:
        neu = c.aendern(
            "POST",
            "/api/lists",
            {
                "name": LISTE_INTERN,
                "type": "private",
                "optin": "single",
                "status": "active",
                "tags": ["intern"],
                "description": "Interne Mitteilungen und Tests (keine Besucher, keine öffentliche Anmeldung).",
            },
        )
        z.intern_id = neu.get("id") if isinstance(neu, dict) else None
        ausgabe(f"D  Liste „{LISTE_INTERN}“: {'angelegt' if c.ja else 'würde angelegt'}")
    treffer = [s for s in ergebnisse(c.lesen("/api/subscribers", search=adresse, per_page="all")) if str(s.get("email", "")).lower() == adresse]
    if not treffer:
        c.aendern(
            "POST",
            "/api/subscribers",
            {
                "email": adresse,
                "name": adresse.split("@", 1)[0],
                "status": "enabled",
                "lists": [z.intern_id] if z.intern_id else [],
                "preconfirm_subscriptions": True,
            },
        )
        ausgabe(f"D  Abonnent {adresse}: {'angelegt' if c.ja else 'würde angelegt'}, in „{LISTE_INTERN}“ bestätigt")
        return
    s = treffer[0]
    if s.get("status") != "enabled":
        z.befunde.append(f"Abonnent {adresse} hat den Status {s.get('status')}: nicht angefasst")
    mitglied = [l for l in (s.get("lists") or []) if l.get("id") == z.intern_id]
    if mitglied and mitglied[0].get("subscription_status") == "confirmed":
        ausgabe(f"D  Abonnent {adresse}: in „{LISTE_INTERN}“ bestätigt")
        return
    c.aendern("PUT", "/api/subscribers/lists", {"ids": [s["id"]], "action": "add", "target_list_ids": [z.intern_id], "status": "confirmed"})
    ausgabe(f"D  Abonnent {adresse}: {'wird' if c.ja else 'würde'} in „{LISTE_INTERN}“ bestätigt eingetragen")


def schritt_vorlage(c: Listmonk, z: Zustand, ausgabe: Ausgabe) -> None:
    """Legt die Transaktionsvorlage „Mitteilung“ an oder zieht sie nach, wenn Betreff oder Aufbau abweichen."""
    soll = {"name": MITTEILUNG_NAME, "type": "tx", "subject": MITTEILUNG_BETREFF, "body": MITTEILUNG_HTML}
    for v in ergebnisse(c.lesen("/api/templates", no_body="true")):
        if v.get("name") == MITTEILUNG_NAME and v.get("type") == "tx":
            voll = c.lesen(f"/api/templates/{v['id']}")
            if voll.get("subject") == MITTEILUNG_BETREFF and voll.get("body") == MITTEILUNG_HTML:
                ausgabe(f"E  Vorlage „{MITTEILUNG_NAME}“: unverändert (Nr. {v['id']})")
            else:
                c.aendern("PUT", f"/api/templates/{v['id']}", soll)
                ausgabe(f"E  Vorlage „{MITTEILUNG_NAME}“: {'aktualisiert' if c.ja else 'würde aktualisiert'} (Nr. {v['id']})")
            return
    c.aendern("POST", "/api/templates", soll)
    ausgabe(f"E  Vorlage „{MITTEILUNG_NAME}“: {'angelegt' if c.ja else 'würde angelegt'}")


def schritt_rollen(c: Listmonk, z: Zustand, ausgabe: Ausgabe) -> None:
    vorhanden = {r.get("name"): r for r in (c.lesen("/api/roles/users") or []) if isinstance(r, dict)}
    for name, rechte in ROLLEN.items():
        r = vorhanden.get(name)
        if r is None:
            neu = c.aendern("POST", "/api/roles/users", {"name": name, "permissions": rechte})
            z.rollen[name] = neu.get("id") if isinstance(neu, dict) else None
            ausgabe(f"F  Rolle {name}: {'angelegt' if c.ja else 'würde angelegt'} ({len(rechte)} Rechte)")
        elif set(r.get("permissions") or []) != set(rechte):
            c.aendern("PUT", f"/api/roles/users/{r['id']}", {"name": name, "permissions": rechte})
            z.rollen[name] = r["id"]
            ausgabe(f"F  Rolle {name}: Rechte {'angeglichen' if c.ja else 'würden angeglichen'}")
        else:
            z.rollen[name] = r["id"]
            ausgabe(f"F  Rolle {name}: unverändert")


def token_gilt(transport: Transport, benutzer: str, token: str) -> bool:
    probe = Listmonk(transport, ja=False, token=f"{benutzer}:{token}")
    try:
        profil = probe.lesen("/api/profile")
    except (Fehler, OSError, http.client.HTTPException):
        return False
    return isinstance(profil, dict) and profil.get("username") == benutzer


def zugaenge_aus_einstellung(text: str) -> dict[str, str]:
    """``FYOS_POST_ZUGAENGE`` → ``{Zugang: Anzeigename}`` (der erste Name, den ein Eintrag nennt; sonst der Zugang)."""
    try:
        paare = zugaenge_lesen(text)
    except PostFehler as e:
        raise Fehler(e.text) from None
    aus: dict[str, str] = {}
    for zugang, name in paare.values():
        if zugang not in aus or (name and aus[zugang] == zugang):
            aus[zugang] = name or aus.get(zugang) or zugang
    return aus


def schritt_zugaenge(
    c: Listmonk,
    z: Zustand,
    ausgabe: Ausgabe,
    transport: Transport,
    zugaenge: dict[str, str],
    token_datei: TokenDatei,
    token_neu: set[str],
    umgebung_token: Callable[[str], str],
) -> None:
    vorhanden = {u.get("username"): u for u in ergebnisse(c.lesen("/api/users"))}
    rolle_id = z.rollen.get(ROLLE_ARBEIT)
    for name, anzeige in zugaenge.items():
        u = vorhanden.get(name)
        if u is not None and name in token_neu:
            c.aendern("DELETE", f"/api/users/{u['id']}")
            ausgabe(f"G  Zugang {name}: {'gelöscht, wird neu angelegt' if c.ja else 'würde gelöscht und neu angelegt'}")
            u = None
        if u is None:
            neu = c.aendern(
                "POST",
                "/api/users",
                {"username": name, "name": anzeige, "type": "api", "status": "enabled", "password_login": False, "user_role_id": rolle_id},
            )
            if not c.ja:
                ausgabe(f"G  Zugang {name} ({ROLLE_ARBEIT}): würde angelegt, Token in die Datei --token-datei")
                continue
            token = (neu or {}).get("password") if isinstance(neu, dict) else None
            if not token:
                raise Fehler(f"Zugang {name} angelegt, aber Listmonk lieferte kein Token: Zugang in der Oberfläche löschen und neu starten")
            z.tokens[name] = token
            try:
                token_datei.schreiben(name, token)
            except Fehler:
                # Ohne gesichertes Token ist der Zugang wertlos (nicht nachlesbar): zurücknehmen, damit ein neuer Lauf sauber anlegt.
                with contextlib.suppress(Fehler):
                    c.aendern("DELETE", f"/api/users/{neu['id']}")
                raise
            ausgabe(f"G  Zugang {name} ({ROLLE_ARBEIT}): angelegt, Token in der Token-Datei")
            continue
        # vorhanden: Rolle und Status prüfen, Token gegenprüfen. Die Liste nennt die Rolle als Objekt `user_role`.
        aktuelle_rolle = (u.get("user_role") or {}).get("id", u.get("user_role_id"))
        if rolle_id is not None and (aktuelle_rolle != rolle_id or u.get("status") != "enabled" or u.get("type") != "api"):
            c.aendern(
                "PUT",
                f"/api/users/{u['id']}",
                {"username": name, "name": anzeige, "type": "api", "status": "enabled", "password_login": False, "user_role_id": rolle_id},
            )
            ausgabe(f"G  Zugang {name}: Rolle/Status {'angeglichen' if c.ja else 'würden angeglichen'}")
        token = umgebung_token(name)
        if not token:
            z.befunde.append(f"Zugang {name} existiert, aber FYOS_POST_TOKEN_{token_name(name)} ist nicht gesetzt: erneuern mit --token-neu {name}")
        elif not token_gilt(transport, name, token):
            z.befunde.append(f"Das Token von {name} (FYOS_POST_TOKEN_{token_name(name)}) wird von Listmonk nicht angenommen: erneuern mit --token-neu {name}")
        else:
            ausgabe(f"G  Zugang {name}: vorhanden, Token gilt")


def einrichten(
    c: Listmonk,
    transport: Transport,
    zugaenge: dict[str, str],
    *,
    abonnent: str | None = None,
    token_datei: Path | None = None,
    token_neu: set[str] | None = None,
    umgebung_token: Callable[[str], str] = lambda name: "",
    ausgabe: Ausgabe = print,
) -> int:
    z = Zustand()
    datei = TokenDatei(token_datei)
    neu = set(token_neu or ())
    unbekannt = sorted(neu - set(zugaenge))
    if unbekannt:
        raise Fehler(f"--token-neu: {', '.join(unbekannt)} steht nicht in FYOS_POST_ZUGAENGE (bekannt: {', '.join(zugaenge)})")
    about = c.lesen("/api/about")
    ausgabe(f"FYOS Post: Listmonk {about.get('version', '?') if isinstance(about, dict) else '?'}, {'Ausführung' if c.ja else 'Trockenlauf (nichts wird geändert)'}")
    try:
        # Vor jeder Änderung: braucht ein neuer Zugang eine Token-Datei, und ist sie brauchbar? Sonst ginge das Token verloren.
        angelegt = {u.get("username") for u in ergebnisse(c.lesen("/api/users"))}
        braucht = [n for n in zugaenge if n not in angelegt or n in neu]
        if braucht and c.ja:
            datei.pruefen()
        elif braucht:
            ausgabe(f"   Hinweis: für {', '.join(braucht)} braucht der Lauf mit --ja eine neue Datei --token-datei PFAD")
        schritt_abonnent(c, z, ausgabe, abonnent)
        schritt_vorlage(c, z, ausgabe)
        schritt_rollen(c, z, ausgabe)
        schritt_zugaenge(c, z, ausgabe, transport, zugaenge, datei, neu, umgebung_token)
    except Fehler as e:
        raise Fehler(unkenntlich(str(e), z.geheimnisse())) from None
    finally:
        datei.schliessen()
    if datei.geschrieben:
        ausgabe(f"Token-Datei geschrieben: {datei.pfad} (Rechte 0600; die Werte werden nicht angezeigt). In die Umgebung des Hubs übernehmen, dann die Datei löschen.")
    for b in z.befunde:
        ausgabe(f"Befund: {b}")
    if not c.ja:
        ausgabe("Trockenlauf beendet: nichts geändert. Zum Ausführen: --ja")
    return 2 if z.befunde else 0


def _umgebung(*namen: str) -> str:
    for name in namen:
        wert = os.environ.get(name, "").strip()
        if wert:
            return wert
    return ""


def _umgebung_token(zugang: str) -> str:
    return _umgebung("FYOS_POST_TOKEN_" + token_name(zugang), "POST_TOKEN_" + token_name(zugang))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fyos-post-einrichten",
        description="Listmonk für das Hub-Plugin `post` einrichten: Vorlage „Mitteilung“, Rolle „arbeit“, API-Zugänge. "
        "Ohne --ja ein Trockenlauf, der nur liest.",
    )
    ap.add_argument("--ja", action="store_true", help="ausführen (ohne: Trockenlauf, liest nur)")
    ap.add_argument("--url", default=None, help="Adresse von Listmonk (sonst FYOS_POST_URL oder POST_URL)")
    ap.add_argument("--abonnent", metavar="ADRESSE", help="diese Adresse in die private Liste „Intern“ eintragen (Empfänger für post.senden)")
    ap.add_argument("--token-datei", type=Path, metavar="PFAD", help="Datei (0600) für die Token neu angelegter Zugänge; wird nicht überschrieben")
    ap.add_argument("--token-neu", action="append", default=[], metavar="ZUGANG", help="Zugang löschen und neu anlegen (verlorenes Token)")
    a = ap.parse_args(argv)
    if a.token_neu and not a.ja:
        ap.error("--token-neu ändert Zugänge und braucht --ja")
    adresse = None
    if a.abonnent:
        adresse = a.abonnent.strip().lower()
        if not _ADRESSE.match(adresse):
            ap.error("--abonnent: keine einfache E-Mail-Adresse")

    geheimnisse: set[str] = set()
    try:
        url = (a.url or _umgebung("FYOS_POST_URL", "POST_URL")).rstrip("/")
        if not url:
            raise Fehler("Die Adresse von Listmonk fehlt: --url oder FYOS_POST_URL")
        nutzer = _umgebung("FYOS_POST_ADMIN_NUTZER")
        passwort = _umgebung("FYOS_POST_ADMIN_PASSWORT")
        if not nutzer or not passwort:
            raise Fehler("Der Admin-Zugang von Listmonk fehlt: FYOS_POST_ADMIN_NUTZER und FYOS_POST_ADMIN_PASSWORT setzen")
        geheimnisse.add(passwort)
        zugaenge = zugaenge_aus_einstellung(_umgebung("FYOS_POST_ZUGAENGE") or "*=fyos")
        transport = http_transport(url)
        c = Listmonk(transport, ja=a.ja)
        c.warten_bis_bereit()
        c.anmelden(nutzer, passwort)
        return einrichten(
            c,
            transport,
            zugaenge,
            abonnent=adresse,
            token_datei=a.token_datei,
            token_neu=set(a.token_neu),
            umgebung_token=_umgebung_token,
            ausgabe=lambda s: print(s, flush=True),
        )
    except Fehler as e:
        print("FEHLER: " + unkenntlich(str(e), geheimnisse), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
