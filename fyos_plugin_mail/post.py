"""post — FYOS Post, der eigene Mail-Dienst (Listmonk): Mitteilungen senden, Kampagnen vorbereiten.

Das Plugin gehört zum Paket ``fyos-plugin-mail``, ist aber unabhängig vom Plugin ``mail`` (das Mails nur liest): ``mail`` liest
ein fremdes Postfach, ``post`` schickt über einen eigenen Listmonk, den der Besitzer betreibt. Vier Werkzeuge, mehr nicht:

    post.zustand            {voll?}                            lesend        Antwortet der Dienst? Listen, Vorlagen,
                                                                             letzte Kampagnen, Tageszähler
    post.senden             {betreff, text, an?, trocken?}     nach außen    eine Mitteilung als Mail an vorhandene
                                                                             Abonnenten (ohne ``an``: der Standardempfänger)
    post.kampagne_entwurf   {name, betreff, listen, text,      schreibend    eine Kampagne als ENTWURF anlegen
                             format?, vorlage?, trocken?}
    post.kampagne_testen    {id, an?, trocken?}                nach außen    Testmail einer Kampagne an vorhandene
                                                                             Abonnenten

Nicht dabei, mit Absicht: eine Kampagne starten (das verlangt in Listmonk das Recht ``campaigns:send``, das die Rolle
``arbeit`` nicht hat; ein Mensch tut es in der Oberfläche), Abonnenten anlegen oder importieren, Einstellungen, Vorlagen
ändern. Was Listmonk dafür braucht (Rolle, API-Zugänge, die Vorlage „Mitteilung“), legt ``fyos-post-einrichten`` an.

## Einstellungen (Umgebung; die Konfiguration des Plugins kann dieselben Werte unter ``url``, ``link``, ``an``, ``zugaenge``,
``tagesgrenze`` und ``zeitzone`` setzen und hat dann Vorrang)

    FYOS_POST_URL          Adresse von Listmonk (Alias POST_URL). Keine Vorgabe: leer heißt „nicht eingerichtet“.
    FYOS_POST_LINK         Adresse der Listmonk-Oberfläche für Links in den Antworten; leer = keine Links.
    FYOS_POST_AN           Standardempfänger (eine Adresse); leer = ``an`` ist Pflicht.
    FYOS_POST_ZUGAENGE     Paare ``schluessel=zugang[/Name]``, durch Komma getrennt (Vorgabe ``*=fyos``).
    FYOS_POST_TOKEN_<ZUGANG>  Token des API-Benutzers (Alias POST_TOKEN_<ZUGANG>), nie in einer Ausgabe.
    FYOS_POST_TAGESGRENZE  Sendungen je UTC-Tag über alle Zugänge (Vorgabe 30).
    FYOS_POST_ZEITZONE     Zeitzone für die Zeit in der Fußzeile (Vorgabe Europe/Berlin, ohne Zeitzonendaten UTC).

## Zugänge

Listmonk kennt API-Benutzer; jeder hat ein Token. ``FYOS_POST_ZUGAENGE`` ordnet die Fläche des Aufrufers (``registry.AUFRUFER``,
Fläche wie in ``_livestrom.flaeche_aus``: ``os``, ``chat``, ``code``, ``agent``, ``dienste``) einem Zugang zu; ``*`` nimmt alles
Übrige. ``Name`` erscheint als „Von …“ in der Fußzeile der Mail. Das ist **Zuordnung, keine Berechtigung**: der Aufrufername
stammt aus einer Kopfzeile und ist nicht fälschungssicher, und alle Zugänge dürfen dasselbe. Die Zuordnung sorgt dafür, dass
Listmonk den richtigen Benutzer sieht und dass die Fußzeile sagt, wer die Mail geschickt hat. Fehlt das Token des Zugangs, ist
das ein Fehler: es gibt nie einen Ausweg über das Token eines anderen Zugangs. Ein ungültiger Eintrag in
``FYOS_POST_ZUGAENGE`` ist ebenfalls ein Fehler (in ``post.zustand`` und im Health-Bericht), nie still ein anderer Zugang.

## Grenzen, die hier im Code stehen (die Rolle in Listmonk setzt sie nicht)

* **Nur vorhandene Abonnenten.** ``subscriber_mode`` ist immer ``default``. Die Modi ``external`` und ``fallback`` schickten Mail
  an beliebige Adressen; sie sind im Code gesperrt (GESPERRTE_MODI, geprüft in ``_pruefe_nutzlast``), kein Werkzeug hat ein
  Feld dafür.
* **Betreff und Text nur in ``data``.** Nie in ``subject`` oder ``altbody``: Listmonk wertet beides als Vorlage aus, und ein
  leerer ``subject`` nimmt den Betreff der Vorlage („Mitteilung“: ``{{ .Tx.Data.betreff }}``). Der Text steht Zeile für Zeile
  in ``data.zeilen`` und kommt HTML-sicher in die Mail. ``{{ … }}`` im Text bleibt Text.
* **Eine Anfrage je Empfänger.** Bei mehreren Empfängern in einer Anfrage sendet Listmonk an die bekannten und antwortet 400,
  wenn einer unbekannt ist; mit einer Anfrage je Empfänger weiß jeder Fehler, wen er betrifft.
* **Tagesgrenze: 30 Sendungen je UTC-Tag** (einstellbar) über alle Zugänge (Testmails von ``kampagne_testen`` zählen mit). Der
  Zähler lebt in ``mcp_hub.ansicht.ABLAGE`` (Redis, bei Redis-Ausfall im Prozess); erst reservieren, dann senden,
  Fehlgeschlagenes zurücknehmen. Ist der Zähler nicht zu lesen oder die Grenze erreicht, wird nichts gesendet. Das ist ein
  Sicherheitsgurt gegen Schleifen und Versehen, keine Rechteprüfung.
* **Keine Umleitungen.** Antwortet Listmonk mit einer Weiterleitung, geht das Token nicht an die neue Adresse. Das Plugin
  benutzt keinen Proxy.

„Angenommen“ ist nicht „zugestellt“: Listmonk antwortet, sobald die Mail in seiner Warteschlange liegt. Scheitert der Versand
danach (SMTP-Weg von Listmonk), steht der Fehler nur in den Protokollen von Listmonk.

Fehler sind nie eine Ausnahme nach außen, sondern ``{quelle_ok: false, grund, satz}`` (lesend) bzw. ``{ok: false, error, …}``
(schreibend; der Hub macht daraus ``tool_failed`` mit den Einzelheiten). Ist ``FYOS_POST_URL`` leer, ist das kein Ausfall,
sondern der Zustand „nicht eingerichtet“ (``eingerichtet: false``, dazu der Weg zum Einrichten). Falsche Eingaben sind
``ValueError`` (``bad_arguments``). Die Argumente gelten als vertraulich (Betreff und Text sind Mailinhalt) und gehen nicht auf
den Ereignisbus.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable
from zoneinfo import ZoneInfo

from mcp_hub import ansicht
from mcp_hub.plugin import HubPlugin, ToolDescriptor, build_dry_run_result, is_dry_run, tiefe_health
from mcp_hub.plugins._livestrom import flaeche_aus

from . import __version__

logger = logging.getLogger(__name__)

FRIST_S = 15.0
#: Alle Empfänger eines Aufrufs zusammen; der Hub bricht bei 120 s ab (registry.AUFRUF_ZEITLIMIT_S).
GESAMT_FRIST_S = 80.0
#: Health-Proben: der ganze Bericht darf 8 s dauern (Zeitlimit des Hubs), die Probe macht eine Anfrage für die Erreichbarkeit
#: und eine je Zugang. Jede Anfrage dauert höchstens HEALTH_FRIST_S, bei vielen Zugängen weniger (zusammen höchstens
#: HEALTH_GESAMT_S).
HEALTH_FRIST_S = 2.0
HEALTH_GESAMT_S = 6.0
ANTWORT_MAX_BYTES = 4_000_000

#: Vertrag mit ``post_einrichten.py`` (Vorlage „Mitteilung“): tests/test_post_einrichten.py hält beide Seiten gleich.
MITTEILUNG_NAME = "Mitteilung"
MITTEILUNG_DATEN_SCHLUESSEL = ("betreff", "zeilen", "von", "zeit", "kennung")

FLAECHEN = ("os", "chat", "code", "agent", "dienste")
STANDARD_ZUGAENGE = "*=fyos"
#: Name in der Fußzeile, wenn ``FYOS_POST_ZUGAENGE`` keinen ``/Name`` nennt (für ``agent`` zählt FYOS_AGENT_NAME).
NEUTRALE_NAMEN = {"os": "Shell", "chat": "Chat", "code": "Code-Agent", "agent": "Agent", "dienste": "FYOS-Dienst"}
ZUGANG_MUSTER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
NAME_MAX = 40
#: Schlüssel im Tageszähler, der den Zugangsnamen nicht gehören darf.
ZAEHLER_GESAMT = "gesamt"

GESPERRTE_MODI = ("external", "fallback")
FORMATE = ("markdown", "html", "plain")

TAGESGRENZE = 30
ZAEHLER_SEKUNDEN = 3 * 24 * 3600  # der Zähler eines UTC-Tages verfällt von selbst
MAX_EMPFAENGER = 5
MAX_TEST_EMPFAENGER = 3
BETREFF_MAX = 150
TEXT_MAX = 5000
ZEILEN_MAX = 200
KAMPAGNE_BETREFF_MAX = 200
KAMPAGNE_NAME_MAX = 200
KAMPAGNE_TEXT_MAX = 100_000
LISTEN_MAX = 10

STANDARD_ZEITZONE = "Europe/Berlin"

HINWEIS_EINRICHTEN = (
    "FYOS Post ist nicht eingerichtet: die Adresse von Listmonk fehlt (Umgebung FYOS_POST_URL). Einrichtung in fünf Schritten: "
    "README, Abschnitt „FYOS Post“ (Listmonk betreiben, FYOS_POST_URL setzen, `fyos-post-einrichten --ja` ausführen, Token "
    "in FYOS_POST_TOKEN_<ZUGANG> eintragen, Hub neu starten)."
)

_TAGS_LESEN = ["post", "read"]
_TAGS_SENDEN = ["post", "write", "extern"]
_TAGS_SCHREIBEN = ["post", "write"]

#: Steuerzeichen, Zeilentrenner und unsichtbare Richtungszeichen: im Betreff und im Text unerwünscht (Kopfzeilen-Einschleusung,
#: Überschriften, die anders aussehen, als sie sind). `_STEUER_TEXT` lässt den Zeilenumbruch stehen.
_UNSICHTBAR = "​-‏‪-‮⁠-⁤⁦-⁩﻿  "
_STEUER_ALLE = re.compile(rf"[\x00-\x1f\x7f-\x9f{_UNSICHTBAR}]")
_STEUER_TEXT = re.compile(rf"[\x00-\x09\x0b-\x1f\x7f-\x9f{_UNSICHTBAR}]")
_ADRESSE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,24}$")

_TOKEN_VORSAETZE = ("FYOS_POST_TOKEN_", "POST_TOKEN_")


class PostFehler(Exception):
    """Ein Gespräch mit Listmonk ist gescheitert. `text` ist gekappt und enthält weder Token noch Mailinhalt.

    `status`: HTTP-Status; 0 = keine Antwort (Netz, Frist), -1 = kein Zugang eingerichtet (Token fehlt oder keine Zuordnung),
    -2 = FYOS Post nicht eingerichtet (keine Adresse), -3 = Einstellung ungültig.
    """

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
        self.text = text


class _KeineUmleitung(urllib.request.HTTPRedirectHandler):
    """Folgt keiner Umleitung: das Token gehört an Listmonk und an keine Adresse, auf die eine Antwort zeigt."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


#: Ohne Proxy-Umgebung und ohne Umleitungen.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _KeineUmleitung())


def _http_senden(methode: str, url: str, kopf: dict[str, str], daten: bytes | None, frist: float) -> tuple[int, bytes]:
    """Einziger Netzzugriff. -> (Status, Körper); Status 0 mit dem Namen der Ausnahme, wenn keine Antwort kam."""
    req = urllib.request.Request(url, data=daten, method=methode, headers=kopf)
    try:
        with _OPENER.open(req, timeout=frist) as r:
            return r.status, r.read(ANTWORT_MAX_BYTES)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(ANTWORT_MAX_BYTES)
        except (OSError, http.client.HTTPException):
            return e.code, b""
    except urllib.error.URLError as e:  # Verbindungsfehler kommen als URLError mit der eigentlichen Ausnahme in `reason`
        return 0, (type(e.reason).__name__ if isinstance(e.reason, BaseException) else "URLError").encode()
    except (OSError, http.client.HTTPException, ValueError) as e:
        return 0, type(e).__name__.encode()


def _jetzt_iso(uhr: Callable[[], dt.datetime] | None = None) -> str:
    return (uhr() if uhr else dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")


def _kurz(text: Any, n: int = 160) -> str:
    return " ".join(str(text if text is not None else "").split())[:n]


def _ergebnisse(daten: Any) -> list[dict[str, Any]]:
    """Listen-Antworten von Listmonk: mal {"results": [...]}, mal eine blanke Liste."""
    if isinstance(daten, dict):
        daten = daten.get("results") or []
    return [d for d in (daten or []) if isinstance(d, dict)]


def _zahl(n: int, einzahl: str, mehrzahl: str) -> str:
    return f"{n} {einzahl if n == 1 else mehrzahl}"


# ── Einstellungen: Zugänge ───────────────────────────────────────────────────────────────────────────────────
def token_name(zugang: str) -> str:
    """Name der Umgebungsvariable (ohne Vorsatz) zu einem Zugang: groß, ``-`` wird ``_``."""
    return zugang.upper().replace("-", "_")


def zugaenge_lesen(text: str) -> dict[str, tuple[str, str | None]]:
    """``FYOS_POST_ZUGAENGE`` → ``{Fläche oder "*": (Zugang, Name oder None)}``. Wirft ``PostFehler(-3, …)``.

    Die Meldungen nennen den Eintrag nur nach seiner Nummer, nie seinen Wortlaut: wer ein Token in die falsche Variable
    schreibt, soll es nicht in einem Fehlerbericht wiederfinden."""
    aus: dict[str, tuple[str, str | None]] = {}
    nutzer: dict[str, str] = {}
    nr = 0
    for roh in text.split(","):
        if not roh.strip():
            continue
        nr += 1
        schluessel, gleich, rest = roh.partition("=")
        schluessel = schluessel.strip().lower()
        zugang, _, name = rest.partition("/")
        zugang, name = zugang.strip(), " ".join(_STEUER_ALLE.sub(" ", name).split())
        if not gleich:
            raise PostFehler(-3, f"FYOS_POST_ZUGAENGE: Eintrag {nr} ist kein Paar „Fläche=zugang“ (Beispiel: {STANDARD_ZUGAENGE})")
        if schluessel != "*" and schluessel not in FLAECHEN:
            raise PostFehler(-3, f"FYOS_POST_ZUGAENGE: Eintrag {nr}: die Fläche muss eine von {', '.join(FLAECHEN)} oder * sein")
        if schluessel in aus:
            raise PostFehler(-3, f"FYOS_POST_ZUGAENGE: Eintrag {nr}: die Fläche „{schluessel}“ steht zweimal da")
        if not ZUGANG_MUSTER.match(zugang) or zugang == ZAEHLER_GESAMT:
            raise PostFehler(
                -3,
                f"FYOS_POST_ZUGAENGE: Eintrag {nr}: der Zugang muss ein Listmonk-Benutzername sein "
                f"(a-z, 0-9, _ und -, höchstens 32 Zeichen, nicht „{ZAEHLER_GESAMT}“)",
            )
        if len(name) > NAME_MAX or "," in name:
            raise PostFehler(-3, f"FYOS_POST_ZUGAENGE: Eintrag {nr}: der Name nach „/“ hat höchstens {NAME_MAX} Zeichen")
        if nutzer.setdefault(token_name(zugang), zugang) != zugang:
            raise PostFehler(-3, f"FYOS_POST_ZUGAENGE: Eintrag {nr}: zwei Zugänge hätten dieselbe Token-Variable")
        aus[schluessel] = (zugang, name or None)
    if not aus:
        raise PostFehler(-3, "FYOS_POST_ZUGAENGE enthält keinen Eintrag (Beispiel: " + STANDARD_ZUGAENGE + ")")
    return aus


# ── Eingaben prüfen (ValueError -> bad_arguments, zählt nicht gegen den Breaker) ─────────────────────────────────
def _betreff(roh: Any) -> str:
    if not isinstance(roh, str) or not roh.strip():
        raise ValueError(f"betreff fehlt (1 bis {BETREFF_MAX} Zeichen, eine Zeile)")
    s = " ".join(_STEUER_ALLE.sub(" ", roh).split())
    if not s:
        raise ValueError(f"betreff fehlt (1 bis {BETREFF_MAX} Zeichen, eine Zeile)")
    if len(s) > BETREFF_MAX:
        raise ValueError(f"betreff hat {len(s)} Zeichen, erlaubt sind {BETREFF_MAX}")
    return s


def _text_zeilen(roh: Any) -> list[str]:
    """Der Mitteilungstext als Zeilen. Zu lang ist ein Fehler, kein stilles Kappen: ein halber Text wäre schlimmer."""
    if not isinstance(roh, str) or not roh.strip():
        raise ValueError(f"text fehlt (einfacher Text, bis {TEXT_MAX} Zeichen)")
    t = _STEUER_TEXT.sub("", roh.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    "))
    zeilen = [z.rstrip() for z in t.split("\n")]
    while zeilen and not zeilen[0]:
        zeilen.pop(0)
    while zeilen and not zeilen[-1]:
        zeilen.pop()
    if not zeilen:
        raise ValueError(f"text fehlt (einfacher Text, bis {TEXT_MAX} Zeichen)")
    zeichen = sum(len(z) for z in zeilen) + len(zeilen) - 1
    if zeichen > TEXT_MAX:
        raise ValueError(f"text hat {zeichen} Zeichen, erlaubt sind {TEXT_MAX}")
    if len(zeilen) > ZEILEN_MAX:
        raise ValueError(f"text hat {len(zeilen)} Zeilen, erlaubt sind {ZEILEN_MAX}")
    return zeilen


def _adresse_pruefen(a: str, feld: str) -> str:
    adresse = a.strip().lower()
    if not _ADRESSE.match(adresse):
        raise ValueError(f"{feld}: „{_kurz(a, 60)}“ ist keine einfache E-Mail-Adresse (ohne Namen, Klammern, Kommas)")
    return adresse


def _adressen(roh: Any, hoechstens: int, standard: str) -> list[str]:
    """Empfänger: kleingeschrieben, ohne Doppelte; ohne Angabe der Standardempfänger. Ob die Adresse Abonnent ist, entscheidet
    Listmonk."""
    if roh is None or roh == [] or roh == "":
        if not standard:
            raise ValueError(
                "an fehlt: eine Liste von E-Mail-Adressen (vorhandene Abonnenten). Einen Standardempfänger gibt es nicht: "
                "FYOS_POST_AN ist nicht gesetzt"
            )
        try:
            return [_adresse_pruefen(standard, "FYOS_POST_AN")]
        except ValueError:
            raise ValueError("an fehlt, und FYOS_POST_AN ist keine einfache E-Mail-Adresse") from None
    if isinstance(roh, str):
        roh = [roh]
    if not isinstance(roh, list) or not all(isinstance(a, str) for a in roh):
        raise ValueError("an: eine Liste von E-Mail-Adressen (vorhandene Abonnenten)")
    aus: list[str] = []
    for a in roh:
        adresse = _adresse_pruefen(a, "an")
        if adresse not in aus:
            aus.append(adresse)
    if len(aus) > hoechstens:
        raise ValueError(f"an: höchstens {hoechstens} Adressen, angegeben sind {len(aus)}")
    return aus


def _einzeilig(roh: Any, feld: str, hoechstens: int) -> str:
    if not isinstance(roh, str) or not roh.strip():
        raise ValueError(f"{feld} fehlt (1 bis {hoechstens} Zeichen, eine Zeile)")
    s = " ".join(_STEUER_ALLE.sub(" ", roh).split())
    if not s:
        raise ValueError(f"{feld} fehlt (1 bis {hoechstens} Zeichen, eine Zeile)")
    if len(s) > hoechstens:
        raise ValueError(f"{feld} hat {len(s)} Zeichen, erlaubt sind {hoechstens}")
    return s


def _ganzzahl(roh: Any, feld: str) -> int:
    if isinstance(roh, bool):
        raise ValueError(f"{feld} muss eine ganze Zahl sein")
    if isinstance(roh, int):
        n = roh
    elif isinstance(roh, str) and roh.strip().isdigit():
        n = int(roh.strip())
    else:
        raise ValueError(f"{feld} muss eine ganze Zahl sein")
    if n < 1:
        raise ValueError(f"{feld} muss größer als 0 sein")
    return n


def _listen_aufloesen(roh: Any, vorhanden: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Listen aus Nummern oder Namen -> die Listmonk-Listen. Unbekannte nennt der Fehler samt den vorhandenen."""
    if isinstance(roh, (int, str)) and not isinstance(roh, bool):
        roh = [roh]
    if not isinstance(roh, list) or not roh:
        raise ValueError("listen: mindestens eine Liste (Nummer oder Name, siehe post.zustand)")
    if len(roh) > LISTEN_MAX:
        raise ValueError(f"listen: höchstens {LISTEN_MAX} Listen")
    nach_id = {l["id"]: l for l in vorhanden if isinstance(l.get("id"), int)}
    nach_name = {str(l.get("name", "")).casefold(): l for l in vorhanden}
    treffer: list[dict[str, Any]] = []
    unbekannt: list[str] = []
    for x in roh:
        liste = None
        if isinstance(x, int) and not isinstance(x, bool):
            liste = nach_id.get(x)
        elif isinstance(x, str):
            s = x.strip()
            liste = nach_name.get(s.casefold()) or (nach_id.get(int(s)) if s.isdigit() else None)
        if liste is None:
            unbekannt.append(_kurz(x, 60))
        elif liste not in treffer:
            treffer.append(liste)
    if unbekannt:
        da = ", ".join(f"{l.get('name')} ({l.get('id')})" for l in vorhanden[:20]) or "keine"
        raise ValueError(f"listen: unbekannt: {', '.join(unbekannt)}. Vorhanden: {da}")
    return treffer


class PostPlugin(HubPlugin):
    name = "post"
    description = (
        "FYOS Post, der eigene Mail-Dienst (Listmonk): Zustand ansehen, eine Mitteilung als Mail an vorhandene "
        "Abonnenten senden, Kampagnen als Entwurf anlegen und testen. Kein Kampagnenstart, keine Importe."
    )
    version = __version__
    HEALTH_TIEF = True
    argumente_vertraulich = True  # Betreff und Text sind Mailinhalt

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self._cfg = dict(config or {})
        #: Tests: (methode, url, kopf, daten, frist) -> (status, körper) statt Netz
        self._http: Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]] | None = None
        #: Tests: eigene Ablage für den Tageszähler; sonst `ansicht.ABLAGE` (Redis, ersatzweise Speicher des Prozesses)
        self._ablage: Any = None
        #: Tests: Uhr (UTC) und Zeitmesser
        self._uhr: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)
        self._monoton: Callable[[], float] = time.monotonic

    def register(self) -> None:
        pass

    # ── Einstellungen ────────────────────────────────────────────────────────
    def _einstellung(self, schluessel: str, *umgebung: str) -> str:
        """Erst die Konfiguration des Plugins, dann die Umgebung (in der Reihenfolge der Namen); sonst leer."""
        wert = self._cfg.get(schluessel)
        if isinstance(wert, str) and wert.strip():
            return wert.strip()
        for name in umgebung:
            wert = os.environ.get(name, "").strip()
            if wert:
                return wert
        return ""

    def _basis(self) -> str:
        """Adresse von Listmonk. ``PostFehler(-2)`` ohne Adresse, ``PostFehler(-3)`` bei einer Adresse ohne http(s)."""
        url = self._einstellung("url", "FYOS_POST_URL", "POST_URL").rstrip("/")
        if not url:
            raise PostFehler(-2, "FYOS_POST_URL ist leer")
        if not re.match(r"^https?://[^\s/]", url, re.IGNORECASE):
            raise PostFehler(-3, "FYOS_POST_URL muss mit http:// oder https:// beginnen")
        return url

    def _link(self, pfad: str = "") -> str:
        """Adresse in der Listmonk-Oberfläche (``FYOS_POST_LINK``); leer, wenn keine eingetragen ist."""
        basis = self._einstellung("link", "FYOS_POST_LINK").rstrip("/")
        if not re.match(r"^https?://[^\s/]", basis, re.IGNORECASE):
            return ""
        return basis + pfad

    def _standard_empfaenger(self) -> str:
        return self._einstellung("an", "FYOS_POST_AN")

    def _grenze(self) -> int:
        roh = self._cfg.get("tagesgrenze", os.environ.get("FYOS_POST_TAGESGRENZE", TAGESGRENZE))
        try:
            return max(0, int(roh))
        except (TypeError, ValueError):
            return TAGESGRENZE

    def _zeitzone(self) -> tuple[dt.tzinfo, str]:
        """(Zeitzone, Zusatz hinter der Uhrzeit). Ohne Zeitzonendaten oder bei einem falschen Namen UTC, dann mit „UTC“."""
        name = self._einstellung("zeitzone", "FYOS_POST_ZEITZONE") or STANDARD_ZEITZONE
        try:
            return ZoneInfo(name), ""
        except Exception:  # noqa: BLE001 — unbekannter Name, kein tzdata, ungültiger Schlüssel
            return dt.timezone.utc, " UTC"

    def _zugaenge(self) -> dict[str, tuple[str, str | None]]:
        return zugaenge_lesen(self._einstellung("zugaenge", "FYOS_POST_ZUGAENGE") or STANDARD_ZUGAENGE)

    def _zugangsnamen(self) -> list[str]:
        """Die Zugänge der Einstellung, ohne Doppelte, in der Reihenfolge ihres Auftretens; leer bei einer ungültigen Einstellung."""
        try:
            namen = [z for z, _ in self._zugaenge().values()]
        except PostFehler:
            return []
        return list(dict.fromkeys(namen))

    # ── Zugang ───────────────────────────────────────────────────────────────
    @staticmethod
    def _token(zugang: str) -> str:
        for vorsatz in _TOKEN_VORSAETZE:
            wert = os.environ.get(vorsatz + token_name(zugang), "").strip()
            if wert:
                return wert
        return ""

    @staticmethod
    def _bekannte_token(*auch: str) -> list[str]:
        """Jedes Token, das die Umgebung kennt: keines davon verlässt das Plugin, auch nicht als Echo in einer Antwort."""
        werte = {v.strip() for k, v in os.environ.items() if k.startswith(_TOKEN_VORSAETZE)}
        werte.update(a for a in auch if a)
        return sorted((w for w in werte if len(w) >= 4), key=len, reverse=True)

    @staticmethod
    def _flaeche() -> str:
        from mcp_hub.registry import AUFRUFER  # noqa: PLC0415 — erst beim Aufruf, die Registry lädt dieses Plugin

        aufrufer = AUFRUFER.get()
        return flaeche_aus("" if aufrufer in (None, "unknown") else str(aufrufer))

    def _wer(self) -> tuple[str, str]:
        """(Zugang, Name in der Fußzeile) des Aufrufers. Zuordnung, keine Berechtigung (Modulkopf).

        Wirft ``PostFehler``: -2 ohne Adresse, -3 bei einer ungültigen Einstellung, -1 ohne Zugang für diese Fläche."""
        self._basis()
        zugaenge = self._zugaenge()
        flaeche = self._flaeche()
        treffer = zugaenge.get(flaeche) or zugaenge.get("*")
        if treffer is None:
            raise PostFehler(
                -1,
                f"Für die Fläche „{flaeche}“ ist in FYOS_POST_ZUGAENGE kein Zugang eingetragen (und kein *-Eintrag)",
            )
        zugang, name = treffer
        if not name:
            if flaeche == "agent":
                name = " ".join(_STEUER_ALLE.sub(" ", os.environ.get("FYOS_AGENT_NAME", "")).split())[:NAME_MAX]
            name = name or NEUTRALE_NAMEN[flaeche]
        return zugang, name

    def _api(self, zugang: str, methode: str, pfad: str, daten: Any = None, frist: float = FRIST_S) -> Any:
        """Ein Aufruf an Listmonk mit dem Token von `zugang`. -> der Inhalt von `data`; sonst PostFehler."""
        basis = self._basis()
        token = self._token(zugang)
        if not token:
            raise PostFehler(-1, f"Zugang „{zugang}“ ist nicht eingerichtet (FYOS_POST_TOKEN_{token_name(zugang)} leer)")
        kopf = {
            "Authorization": f"token {zugang}:{token}",
            "Accept": "application/json",
            "User-Agent": "FYOS-Hub/1.0 (post)",
        }
        koerper = None
        if daten is not None:
            koerper = json.dumps(daten, ensure_ascii=False).encode("utf-8")
            kopf["Content-Type"] = "application/json"
        senden = self._http or _http_senden
        status, roh = senden(methode, basis + pfad, kopf, koerper, frist)
        if status == 0:
            raise PostFehler(0, f"keine Antwort von Listmonk ({_kurz(roh.decode('utf-8', 'replace'), 60) or 'unbekannt'})")
        text = ""
        antwort: Any = None
        if roh:
            try:
                antwort = json.loads(roh.decode("utf-8", "replace"))
            except ValueError:
                text = _kurz(roh.decode("utf-8", "replace"), 120)
        if isinstance(antwort, dict) and antwort.get("message"):
            text = _kurz(antwort["message"])
        for bekannt in self._bekannte_token(token):  # nicht nur das eigene: kein Token verlässt das Plugin, auch nicht als Echo
            text = text.replace(bekannt, "***")
        if not 200 <= status < 300:
            raise PostFehler(status, text or "ohne Text")
        if roh and antwort is None:
            raise PostFehler(status, "keine JSON-Antwort von Listmonk")
        return antwort.get("data", antwort) if isinstance(antwort, dict) else antwort

    # ── Antworten ────────────────────────────────────────────────────────────
    def _stand(self) -> str:
        return _jetzt_iso(self._uhr)

    def _aus(self, grund: str, satz: str, **mehr: Any) -> dict[str, Any]:
        """Lesende Werkzeuge: Ausfall (und „nicht eingerichtet“) ist ein Zustand, kein Fehler."""
        return {"quelle_ok": False, "grund": _kurz(grund), "satz": satz, "stand": self._stand(), **mehr}

    def _fehler(self, grund: str, satz: str, **mehr: Any) -> dict[str, Any]:
        """Schreibende Werkzeuge: `ok: false` (der Hub macht daraus tool_failed und gibt die Einzelheiten mit)."""
        return {
            "ok": False,
            "error": _kurz(grund),
            "quelle_ok": False,
            "grund": _kurz(grund),
            "satz": satz,
            "stand": self._stand(),
            **mehr,
        }

    @staticmethod
    def _fehlerbild(e: PostFehler, zugang: str) -> tuple[str, str]:
        """(Grund, Satz) zu einem Fehler im Gespräch mit Listmonk."""
        if e.status == -2:
            return "nicht eingerichtet", HINWEIS_EINRICHTEN
        if e.status == -3:
            return e.text, f"FYOS Post ist falsch eingerichtet: {e.text}."
        if e.status == -1:
            return e.text, "FYOS Post ist für diesen Aufrufer nicht eingerichtet (Token oder Zuordnung fehlt)."
        if e.status == 0:
            return e.text, "FYOS Post (Listmonk) antwortet gerade nicht."
        grund = f"HTTP {e.status}: {e.text}"
        if e.status in (401, 403):
            return grund, f"Listmonk nimmt den Zugang „{zugang}“ nicht an (README, Abschnitt „FYOS Post“, Zugänge)."
        if e.status == 404:
            return grund, "Listmonk kennt diese Adresse oder dieses Objekt nicht."
        if e.status >= 500:
            return grund, "Listmonk meldet einen Fehler."
        return grund, "Listmonk hat die Anfrage abgelehnt."

    def _fehler_aus(self, e: PostFehler, zugang: str, folge: str = "Es wurde nichts gesendet.", **mehr: Any) -> dict[str, Any]:
        grund, satz = self._fehlerbild(e, zugang)
        if e.status == -2:
            mehr = {"eingerichtet": False, **mehr}
        return self._fehler(grund, f"{satz} {folge}", **mehr)

    def _zugang_oder_fehler(self, folge: str) -> tuple[tuple[str, str] | None, dict[str, Any] | None]:
        """((Zugang, Name), None) oder (None, Fehlerantwort eines schreibenden Werkzeugs)."""
        try:
            return self._wer(), None
        except PostFehler as e:
            return None, self._fehler_aus(e, "", folge)

    # ── Tageszähler ──────────────────────────────────────────────────────────
    def _ablage_objekt(self) -> Any:
        return self._ablage if self._ablage is not None else ansicht.ABLAGE

    def _tag(self) -> str:
        return self._uhr().astimezone(dt.timezone.utc).strftime("%Y-%m-%d")

    def _zaehler_schluessel(self) -> str:
        return f"fyos:hub:post:sendungen:{self._tag()}"

    def _gezaehlt(self) -> dict[str, int] | None:
        """Stand des heutigen Zählers ({"gesamt": n, "<zugang>": n}) oder None, wenn er nicht lesbar ist."""
        try:
            return {k: max(0, int(v)) for k, v in self._ablage_objekt().zaehler(self._zaehler_schluessel()).items()}
        except Exception as e:  # noqa: BLE001 — die Ablage darf ein Lesewerkzeug nicht kippen
            logger.warning("post: Tageszähler nicht lesbar: %s", type(e).__name__)
            return None

    def _reservieren(self, schluessel: str, zugang: str, n: int) -> tuple[bool, int | None]:
        """Zählt `n` Sendungen VOR dem Senden mit. -> (innerhalb der Grenze, Stand danach). Im Zweifel nein.

        Erst addieren, dann lesen, bei Überschreitung zurücknehmen: zwei gleichzeitige Aufrufe sehen beide den Anteil des
        anderen und können nicht gemeinsam über die Grenze kommen (im Zweifel weist dann auch einer zu viel ab).
        `schluessel` (der UTC-Tag) wird einmal je Aufruf bestimmt und für Buchung und Rücknahme benutzt: ein Aufruf über
        Mitternacht darf die Rücknahme nicht auf dem Zähler des neuen Tages verbuchen.
        """
        ablage = self._ablage_objekt()
        try:
            ablage.zaehlen_viele(schluessel, {ZAEHLER_GESAMT: n, zugang: n}, ZAEHLER_SEKUNDEN)
            gesamt = int(ablage.zaehler(schluessel).get(ZAEHLER_GESAMT, 0))
        except Exception as e:  # noqa: BLE001 — ohne verlässlichen Zähler wird nicht gesendet
            logger.warning("post: Tageszähler nicht nutzbar: %s", type(e).__name__)
            self._zurueck(schluessel, zugang, n)
            return False, None
        if gesamt > self._grenze() or gesamt < 1:  # zu viel, oder unsere eigene Buchung ist nicht zu sehen
            self._zurueck(schluessel, zugang, n)
            return False, max(0, gesamt - n)
        return True, gesamt

    def _zurueck(self, schluessel: str, zugang: str, n: int) -> None:
        if n <= 0:
            return
        try:
            self._ablage_objekt().zaehlen_viele(schluessel, {ZAEHLER_GESAMT: -n, zugang: -n}, ZAEHLER_SEKUNDEN)
        except Exception as e:  # noqa: BLE001
            logger.warning("post: Tageszähler nicht zurückgenommen: %s", type(e).__name__)

    def _heute(self) -> dict[str, Any]:
        stand = self._gezaehlt()
        out: dict[str, Any] = {"datum_utc": self._tag(), "grenze": self._grenze()}
        if stand is None:
            return {**out, "gesendet": None, "hinweis": "Zähler gerade nicht lesbar"}
        namen = self._zugangsnamen() or [k for k in stand if k != ZAEHLER_GESAMT]
        return {**out, "gesendet": stand.get(ZAEHLER_GESAMT, 0), "je_zugang": {z: stand.get(z, 0) for z in namen}}

    # ── Vorlage und Nutzlast ─────────────────────────────────────────────────
    def _mitteilung_id(self, zugang: str) -> int | None:
        for v in _ergebnisse(self._api(zugang, "GET", "/api/templates?no_body=true")):
            if v.get("name") == MITTEILUNG_NAME and v.get("type") == "tx" and isinstance(v.get("id"), int):
                return v["id"]
        return None

    @staticmethod
    def _tx_nutzlast(adresse: str, vorlage_id: int, daten: dict[str, Any]) -> dict[str, Any]:
        """Der Aufruf an /api/tx. Absichtlich ohne `subject`, `altbody`, `headers`, `from_email`: Listmonk wertet Betreff und
        Alternativtext als Vorlage aus, der Betreff kommt aus der Vorlage „Mitteilung“ und liest `data.betreff`."""
        nutzlast = {
            "subscriber_emails": [adresse],
            "subscriber_mode": "default",
            "template_id": vorlage_id,
            "content_type": "html",
            "messenger": "email",
            "data": daten,
        }
        PostPlugin._pruefe_nutzlast(nutzlast)
        return nutzlast

    @staticmethod
    def _pruefe_nutzlast(nutzlast: dict[str, Any]) -> None:
        """Letzte Sperre vor dem Netz: nur vorhandene Abonnenten, Betreff und Text nur in `data`. Wirft RuntimeError."""
        modus = nutzlast.get("subscriber_mode")
        if modus in GESPERRTE_MODI or modus != "default":
            raise RuntimeError("subscriber_mode muss default sein (external und fallback sind gesperrt)")
        verboten = {"subject", "altbody", "headers", "from_email", "subscriber_ids", "subscriber_email", "subscriber_id"}
        if verboten & set(nutzlast):
            raise RuntimeError(f"nicht erlaubt in /api/tx: {sorted(verboten & set(nutzlast))}")
        if set(nutzlast.get("data") or {}) != set(MITTEILUNG_DATEN_SCHLUESSEL):
            raise RuntimeError("data muss genau die Schlüssel der Vorlage Mitteilung tragen")
        if len(nutzlast.get("subscriber_emails") or []) != 1:
            raise RuntimeError("eine Anfrage je Empfänger")

    # ── post.zustand ─────────────────────────────────────────────────────────
    def zustand(self, voll: bool = False) -> dict[str, Any]:
        try:
            zugang, _ = self._wer()
            listen = _ergebnisse(self._api(zugang, "GET", "/api/lists?per_page=all"))
            vorlagen = _ergebnisse(self._api(zugang, "GET", "/api/templates?no_body=true"))
            kampagnen = _ergebnisse(
                self._api(zugang, "GET", "/api/campaigns?page=1&per_page=5&order_by=created_at&order=DESC&no_body=true")
            )
        except PostFehler as e:
            grund, satz = self._fehlerbild(e, "")
            logger.info("post: zustand: %s", grund)
            return self._aus(grund, satz, **({"eingerichtet": False} if e.status == -2 else {}))
        mitteilung = any(v.get("name") == MITTEILUNG_NAME and v.get("type") == "tx" for v in vorlagen)
        abonnenten = sum(int(l.get("subscriber_count") or 0) for l in listen)
        heute = self._heute()
        out: dict[str, Any] = {
            "quelle_ok": True,
            "zugang": zugang,
            "listen": [self._liste_kurz(l, voll) for l in listen],
            "vorlagen": [self._vorlage_kurz(v, voll) for v in vorlagen],
            "mitteilung_vorhanden": mitteilung,
            "kampagnen": [self._kampagne_kurz(k, voll) for k in kampagnen[:5]],
            "heute": heute,
            "stand": self._stand(),
        }
        if self._link():
            out["link"] = self._link()
        satz = (
            f"FYOS Post antwortet: {_zahl(len(listen), 'Liste', 'Listen')} mit zusammen "
            f"{_zahl(abonnenten, 'Abonnent', 'Abonnenten')}, {_zahl(len(vorlagen), 'Vorlage', 'Vorlagen')}, "
            f"{_zahl(len(kampagnen), 'Kampagne', 'Kampagnen')} (die letzten fünf gezeigt)."
        )
        if not mitteilung:
            satz += " Die Vorlage „Mitteilung“ fehlt: post.senden geht erst nach `fyos-post-einrichten --ja` (README, Abschnitt „FYOS Post“)."
        if heute.get("gesendet") is not None:
            satz += f" Heute {heute['gesendet']} von {heute['grenze']} Sendungen."
        out["satz"] = satz
        return out

    @staticmethod
    def _liste_kurz(l: dict[str, Any], voll: bool) -> dict[str, Any]:
        out = {"id": l.get("id"), "name": l.get("name"), "art": l.get("type"), "abonnenten": int(l.get("subscriber_count") or 0)}
        if voll:
            out.update({"anmeldung": l.get("optin"), "status": l.get("status"), "tags": l.get("tags") or [], "angelegt": l.get("created_at")})
        return out

    @staticmethod
    def _vorlage_kurz(v: dict[str, Any], voll: bool) -> dict[str, Any]:
        out = {"id": v.get("id"), "name": v.get("name"), "art": v.get("type")}
        if voll:
            out.update({"standard": bool(v.get("is_default")), "betreff": _kurz(v.get("subject"), 100), "angelegt": v.get("created_at")})
        return out

    def _kampagne_kurz(self, k: dict[str, Any], voll: bool) -> dict[str, Any]:
        out = {
            "id": k.get("id"),
            "name": _kurz(k.get("name"), 100),
            "betreff": _kurz(k.get("subject"), 100),
            "status": k.get("status"),
            "gesendet": k.get("sent"),
            "von": k.get("to_send"),
            "angelegt": k.get("created_at"),
        }
        if voll:
            out.update(
                {
                    "listen": [l.get("name") for l in (k.get("lists") or []) if isinstance(l, dict)],
                    "vorlage": k.get("template_id"),
                    "format": k.get("content_type"),
                    "gestartet": k.get("started_at"),
                }
            )
            if self._link():
                out["link"] = self._link(f"/admin/campaigns/{k.get('id')}")
        return out

    # ── post.senden ──────────────────────────────────────────────────────────
    def senden(self, args: dict[str, Any]) -> dict[str, Any]:
        try:  # ohne Adresse von Listmonk gibt es nichts zu prüfen: erst „nicht eingerichtet“, dann die Eingaben
            self._basis()
        except PostFehler as e:
            return self._fehler_aus(e, "")
        betreff = _betreff(args.get("betreff"))
        zeilen = _text_zeilen(args.get("text"))
        empfaenger = _adressen(args.get("an"), MAX_EMPFAENGER, self._standard_empfaenger())
        trocken = bool(args.get("trocken")) or is_dry_run(args)
        wer, fehler = self._zugang_oder_fehler("Es wurde nichts gesendet.")
        if wer is None:
            return fehler or {}
        zugang, von = wer
        try:
            vorlage_id = self._mitteilung_id(zugang)
        except PostFehler as e:
            return self._fehler_aus(e, zugang)
        if vorlage_id is None:
            return self._fehler(
                f"Vorlage „{MITTEILUNG_NAME}“ fehlt in FYOS Post",
                f"Die Vorlage „{MITTEILUNG_NAME}“ gibt es in FYOS Post noch nicht: einmal "
                "`fyos-post-einrichten --ja` ausführen (README, Abschnitt „FYOS Post“). "
                "Es wurde nichts gesendet.",
            )
        kennung = uuid.uuid4().hex[:8]
        zone, zusatz = self._zeitzone()
        daten = {
            "betreff": betreff,
            "zeilen": zeilen,
            "von": von,
            "zeit": self._uhr().astimezone(zone).strftime("%d.%m.%Y, %H:%M Uhr") + zusatz,
            "kennung": kennung,
        }
        n = len(empfaenger)
        # Alle Nutzlasten vor jeder Buchung bauen (auch im Trockenlauf): schlägt die Sperre in _pruefe_nutzlast an, ist der
        # Zähler noch unberührt.
        nutzlasten = [self._tx_nutzlast(a, vorlage_id, daten) for a in empfaenger]
        if trocken:
            heute = self._heute()
            danach = (heute["gesendet"] + n) if isinstance(heute.get("gesendet"), int) else None
            plan = build_dry_run_result(
                would_execute=f"Mitteilung „{_kurz(betreff, 60)}“ als Mail an {_zahl(n, 'Empfänger', 'Empfänger')} über FYOS Post senden",
                expected_side_effects=[f"{a}: eine Mail über Listmonk und seinen SMTP-Weg" for a in empfaenger]
                + [f"Tageszähler: +{n} (Grenze {self._grenze()})"],
                estimated_cost=n,
                plugin=self.name,
                method="senden",
                args_preview={"betreff": betreff, "an": empfaenger, "zeilen": len(zeilen), "zeichen": sum(len(z) for z in zeilen)},
            )
            ueber = danach is not None and danach > self._grenze()
            return {
                **plan,
                "quelle_ok": True,
                "trocken": True,
                "gesendet": False,
                "zugang": zugang,
                "von": von,
                "heute": heute,
                "satz": (
                    f"Trockenlauf: {_zahl(n, 'Mail', 'Mails')} an {', '.join(empfaenger)} würde als „{von}“ gesendet; nichts ist hinausgegangen."
                    + (" Die Tagesgrenze wäre überschritten, es würde abgelehnt." if ueber else "")
                ),
                "stand": self._stand(),
            }
        schluessel = self._zaehler_schluessel()
        ok, stand = self._reservieren(schluessel, zugang, n)
        if not ok:
            if stand is None:
                return self._fehler(
                    "Tageszähler nicht nutzbar",
                    "Der Tageszähler für Sendungen ist gerade nicht erreichbar; zur Sicherheit wurde nichts gesendet.",
                )
            return self._fehler(
                f"Tagesgrenze von {self._grenze()} Sendungen erreicht",
                f"Heute sind schon {stand} von {self._grenze()} Sendungen hinausgegangen (Tag nach UTC); diese {_zahl(n, 'Mail', 'Mails')} "
                "würde die Grenze überschreiten. Es wurde nichts gesendet.",
                heute=self._heute(),
            )
        gesendet: list[str] = []
        fehlgeschlagen: list[dict[str, Any]] = []
        beginn = self._monoton()
        for i, adresse in enumerate(empfaenger):
            if self._monoton() - beginn > GESAMT_FRIST_S:
                rest = empfaenger[i:]
                self._zurueck(schluessel, zugang, len(rest))
                fehlgeschlagen += [{"an": a, "gesendet": False, "grund": "nicht versucht (Frist des Aufrufs)"} for a in rest]
                break
            try:
                self._api(zugang, "POST", "/api/tx", nutzlasten[i])
                gesendet.append(adresse)
            except PostFehler as e:
                grund, _ = self._fehlerbild(e, zugang)
                if e.status == 0:
                    # Keine Antwort heißt nicht „nicht angenommen“: die Buchung bleibt stehen, der Ausgang ist offen.
                    fehlgeschlagen.append({"an": adresse, "gesendet": "unbekannt", "grund": grund})
                else:
                    self._zurueck(schluessel, zugang, 1)
                    fehlgeschlagen.append({"an": adresse, "gesendet": False, "grund": grund})
                if e.status in (-3, -2, -1, 0, 401, 403) or e.status >= 500:  # grundsätzliche Störung: die übrigen nicht auch noch
                    rest = empfaenger[i + 1 :]
                    self._zurueck(schluessel, zugang, len(rest))
                    fehlgeschlagen += [{"an": a, "gesendet": False, "grund": "nicht versucht (vorheriger Fehler)"} for a in rest]
                    break
        heute = self._heute()
        if not fehlgeschlagen:
            return {
                "ok": True,
                "quelle_ok": True,
                "angenommen": True,
                "an": gesendet,
                "von": von,
                "zugang": zugang,
                "kennung": kennung,
                "heute": heute,
                "satz": (
                    f"Mitteilung „{_kurz(betreff, 60)}“ an {', '.join(gesendet)} an FYOS Post übergeben (Kennung {kennung}). "
                    "„Angenommen“ heißt nicht „zugestellt“: scheitert der SMTP-Weg von Listmonk danach, steht der Fehler nur "
                    "im Protokoll von Listmonk."
                ),
                "stand": self._stand(),
            }
        erster = fehlgeschlagen[0]
        unbekannt_empfaenger = [f for f in fehlgeschlagen if str(f["grund"]).startswith("HTTP 400")]
        satz = (
            f"{_zahl(len(gesendet), 'Mail', 'Mails')} übergeben, {_zahl(len(fehlgeschlagen), 'Empfänger', 'Empfänger')} nicht "
            f"(Kennung {kennung})."
        )
        if unbekannt_empfaenger:
            satz += " HTTP 400 heißt meist: die Adresse ist in FYOS Post kein Abonnent; Mitteilungen gehen nur an vorhandene Abonnenten."
        if any(f["gesendet"] == "unbekannt" for f in fehlgeschlagen):
            satz += " Bei „unbekannt“ blieb die Antwort aus; die Mail kann trotzdem angenommen worden sein."
        return self._fehler(
            erster["grund"],
            satz,
            angenommen=bool(gesendet),
            an=gesendet,
            fehlgeschlagen=fehlgeschlagen,
            von=von,
            zugang=zugang,
            kennung=kennung,
            heute=heute,
        )

    # ── post.kampagne_entwurf ────────────────────────────────────────────────
    def kampagne_entwurf(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            self._basis()
        except PostFehler as e:
            return self._fehler_aus(e, "", "Es wurde keine Kampagne angelegt.")
        name = _einzeilig(args.get("name"), "name", KAMPAGNE_NAME_MAX)
        betreff = _einzeilig(args.get("betreff"), "betreff", KAMPAGNE_BETREFF_MAX)
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text fehlt (der Inhalt der Kampagne)")
        text = text.replace("\x00", "")
        if len(text) > KAMPAGNE_TEXT_MAX:
            raise ValueError(f"text hat {len(text)} Zeichen, erlaubt sind {KAMPAGNE_TEXT_MAX}")
        format_ = str(args.get("format") or "markdown").strip().lower()
        if format_ not in FORMATE:
            raise ValueError(f"format: {', '.join(FORMATE)}")
        vorlage = args.get("vorlage")
        vorlage = None if vorlage is None else _ganzzahl(vorlage, "vorlage")
        trocken = bool(args.get("trocken")) or is_dry_run(args)
        roh_listen = args.get("listen")
        wer, fehler = self._zugang_oder_fehler("Es wurde keine Kampagne angelegt.")
        if wer is None:
            return fehler or {}
        zugang, von = wer
        try:
            vorhanden = _ergebnisse(self._api(zugang, "GET", "/api/lists?per_page=all"))
        except PostFehler as e:
            return self._fehler_aus(e, zugang, "Es wurde keine Kampagne angelegt.")
        listen = _listen_aufloesen(roh_listen, vorhanden)
        nutzlast: dict[str, Any] = {
            "name": name,
            "subject": betreff,
            "lists": [l["id"] for l in listen],
            "type": "regular",
            "content_type": format_,
            "body": text,
            "messenger": "email",
        }
        if vorlage is not None:
            nutzlast["template_id"] = vorlage
        # Mehr nie: kein send_at, kein status, kein Aufruf von /status. Gestartet wird in der Oberfläche von einem Menschen.
        namen = [str(l.get("name")) for l in listen]
        if trocken:
            plan = build_dry_run_result(
                would_execute=f"Kampagne „{_kurz(name, 60)}“ als Entwurf an {', '.join(namen)} anlegen (nicht starten)",
                expected_side_effects=["Listmonk: ein Kampagnen-Entwurf; es geht keine Mail hinaus"],
                estimated_cost=1,
                plugin=self.name,
                method="kampagne_entwurf",
                args_preview={"name": name, "betreff": betreff, "listen": namen, "format": format_, "zeichen": len(text)},
            )
            return {
                **plan,
                "quelle_ok": True,
                "trocken": True,
                "angelegt": False,
                "satz": f"Trockenlauf: Kampagne „{_kurz(name, 60)}“ würde als Entwurf für {', '.join(namen)} angelegt; nichts ist angelegt.",
                "stand": self._stand(),
            }
        try:
            neu = self._api(zugang, "POST", "/api/campaigns", nutzlast)
        except PostFehler as e:
            return self._fehler_aus(e, zugang, "Es wurde keine Kampagne angelegt.")
        if not isinstance(neu, dict) or not isinstance(neu.get("id"), int):
            mehr = {"link": self._link("/admin/campaigns")} if self._link() else {}
            return self._fehler(
                "Antwort von Listmonk ohne Kampagnen-Nummer",
                "Ob die Kampagne angelegt wurde, ist unklar: in der Listmonk-Oberfläche nachsehen.",
                **mehr,
            )
        link = self._link(f"/admin/campaigns/{neu['id']}")
        status = neu.get("status")
        if status != "draft":
            return self._fehler(
                f"Kampagne Nr. {neu['id']} hat den Status {status}, erwartet war draft",
                "Listmonk hat die Kampagne nicht als Entwurf angelegt: bitte in der Oberfläche prüfen"
                + (f": {link}" if link else "."),
                id=neu["id"],
                status=status,
                **({"link": link} if link else {}),
            )
        ergebnis: dict[str, Any] = {
            "ok": True,
            "quelle_ok": True,
            "entwurf": True,
            "id": neu["id"],
            "name": _kurz(neu.get("name") or name, 100),
            "status": status,
            "listen": namen,
            "format": format_,
            "von": von,
            "zugang": zugang,
            "satz": (
                f"Entwurf „{_kurz(name, 60)}“ (Nr. {neu['id']}) liegt bereit. Gestartet wird er nur von einem Menschen in der "
                "Listmonk-Oberfläche" + (f": {link}" if link else ".")
            ),
            "stand": self._stand(),
        }
        if link:
            ergebnis["link"] = link
        return ergebnis

    # ── post.kampagne_testen ─────────────────────────────────────────────────
    def kampagne_testen(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            self._basis()
        except PostFehler as e:
            return self._fehler_aus(e, "")
        kid = _ganzzahl(args.get("id"), "id")
        empfaenger = _adressen(args.get("an"), MAX_TEST_EMPFAENGER, self._standard_empfaenger())
        trocken = bool(args.get("trocken")) or is_dry_run(args)
        wer, fehler = self._zugang_oder_fehler("Es wurde nichts gesendet.")
        if wer is None:
            return fehler or {}
        zugang, von = wer
        # Erst die Kampagne lesen: Listmonk verlangt für den Test ihre Felder noch einmal im Aufruf.
        try:
            k = self._api(zugang, "GET", f"/api/campaigns/{kid}")
        except PostFehler as e:
            if e.status == 404:
                return self._fehler(f"Kampagne Nr. {kid} gibt es nicht", f"Es gibt keine Kampagne Nr. {kid} (post.zustand zeigt die letzten). Es wurde nichts gesendet.")
            return self._fehler_aus(e, zugang)
        if not isinstance(k, dict):
            return self._fehler("Antwort von Listmonk ohne Kampagne", "Die Kampagne ließ sich nicht lesen. Es wurde nichts gesendet.")
        listen = [l["id"] for l in (k.get("lists") or []) if isinstance(l, dict) and isinstance(l.get("id"), int)]
        if not listen:
            link = self._link(f"/admin/campaigns/{kid}")
            return self._fehler(
                f"Kampagne Nr. {kid} hat keine Liste",
                "Die Kampagne hat keine Liste mehr (gelöscht?); Listmonk verlangt für den Test mindestens eine. "
                "In der Oberfläche eine Liste zuweisen" + (f": {link}" if link else "") + ". Es wurde nichts gesendet.",
            )
        nutzlast = {
            "name": k.get("name"),
            "subject": k.get("subject"),
            "from_email": k.get("from_email"),
            "body": k.get("body"),
            "altbody": k.get("altbody"),
            "content_type": k.get("content_type"),
            "messenger": k.get("messenger") or "email",
            "headers": k.get("headers") or [],
            "template_id": k.get("template_id"),
            "lists": listen,
            "subscribers": empfaenger,
        }
        n = len(empfaenger)
        titel = _kurz(k.get("name"), 60)
        if trocken:
            heute = self._heute()
            plan = build_dry_run_result(
                would_execute=f"Testmail der Kampagne „{titel}“ (Nr. {kid}) an {', '.join(empfaenger)} senden",
                expected_side_effects=[f"{a}: eine Testmail über Listmonk und seinen SMTP-Weg (nur wenn Abonnent)" for a in empfaenger]
                + [f"Tageszähler: +{n} (Grenze {self._grenze()})"],
                estimated_cost=n,
                plugin=self.name,
                method="kampagne_testen",
                args_preview={"id": kid, "an": empfaenger, "betreff": _kurz(k.get("subject"), 100)},
            )
            return {
                **plan,
                "quelle_ok": True,
                "trocken": True,
                "gesendet": False,
                "heute": heute,
                "satz": f"Trockenlauf: Testmail der Kampagne „{titel}“ würde an {', '.join(empfaenger)} gehen; nichts ist hinausgegangen.",
                "stand": self._stand(),
            }
        schluessel = self._zaehler_schluessel()
        ok, stand = self._reservieren(schluessel, zugang, n)
        if not ok:
            if stand is None:
                return self._fehler("Tageszähler nicht nutzbar", "Der Tageszähler für Sendungen ist gerade nicht erreichbar; zur Sicherheit wurde nichts gesendet.")
            return self._fehler(
                f"Tagesgrenze von {self._grenze()} Sendungen erreicht",
                f"Heute sind schon {stand} von {self._grenze()} Sendungen hinausgegangen (Tag nach UTC); diese Testmails würden die Grenze "
                "überschreiten. Es wurde nichts gesendet.",
                heute=self._heute(),
            )
        try:
            self._api(zugang, "POST", f"/api/campaigns/{kid}/test", nutzlast)
        except PostFehler as e:
            grund, satz = self._fehlerbild(e, zugang)
            if e.status != 0:  # ohne Antwort bleibt die Buchung stehen: die Testmail kann hinausgegangen sein
                self._zurueck(schluessel, zugang, n)
            if e.status == 400:
                satz = "Listmonk kennt keine der Adressen als Abonnent (Tests gehen nur an vorhandene Abonnenten) oder lehnt die Kampagne ab."
            return self._fehler(grund, satz + " Es wurde keine Testmail bestätigt.", id=kid)
        return {
            "ok": True,
            "quelle_ok": True,
            "angenommen": True,
            "id": kid,
            "kampagne": titel,
            "an": empfaenger,
            "von": von,
            "zugang": zugang,
            "heute": self._heute(),
            "satz": (
                f"Testmail der Kampagne „{titel}“ (Nr. {kid}) an {', '.join(empfaenger)} an FYOS Post übergeben. Adressen, die dort "
                "kein Abonnent sind, überspringt Listmonk. „Angenommen“ heißt nicht „zugestellt“."
            ),
            "stand": self._stand(),
        }

    # ── Hub ──────────────────────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        """Ohne Adresse „aus“ (kein Ausfall). Sonst tief: Listmonk antwortet UND jedes Token wird angenommen. Wirft nie."""
        try:
            try:
                self._basis()
            except PostFehler as e:
                if e.status == -2:
                    return {"ok": True, "status": "aus", "reason": "nicht eingerichtet (FYOS_POST_URL leer)"}
                return {"ok": False, "status": "unhealthy", "reason": e.text, "tiefe": "tief"}
            try:
                self._zugaenge()
            except PostFehler as e:
                return {"ok": False, "status": "unhealthy", "reason": e.text, "tiefe": "tief"}
            fehlend = [z for z in self._zugangsnamen() if not self._token(z)]
            if fehlend:
                namen = ", ".join(f"FYOS_POST_TOKEN_{token_name(z)}" for z in fehlend)
                return {
                    "ok": False,
                    "status": "unhealthy",
                    "reason": f"Token fehlt: {namen} (Zugang: {', '.join(fehlend)})",
                    "tiefe": "tief",
                }
            return tiefe_health(self, self._erreichbar, "profil", self._profile_pruefen)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": "unhealthy", "reason": f"{type(e).__name__}: {_kurz(e, 120)}", "tiefe": "tief"}

    def _health_frist(self) -> float:
        return min(HEALTH_FRIST_S, HEALTH_GESAMT_S / (1 + max(1, len(self._zugangsnamen()))))

    def _erreichbar(self) -> str:
        senden = self._http or _http_senden
        status, _ = senden("GET", self._basis() + "/health", {"Accept": "application/json"}, None, self._health_frist())
        if status != 200:
            raise RuntimeError(f"/health antwortet {status}" if status else "Listmonk antwortet nicht")
        return "listmonk"

    def _profile_pruefen(self) -> dict[str, Any]:
        for zugang in self._zugangsnamen():
            try:
                profil = self._api(zugang, "GET", "/api/profile", frist=self._health_frist())
            except PostFehler as e:
                return {"ok": False, "error": f"Zugang {zugang}: {self._fehlerbild(e, zugang)[0]}"}
            if not isinstance(profil, dict) or profil.get("username") != zugang:
                return {"ok": False, "error": f"Zugang {zugang}: Token gehört nicht zu diesem Benutzer"}
        return {"ok": True}

    def describe_tools(self) -> list[ToolDescriptor]:
        grenze = self._grenze()
        trocken = {
            "type": "boolean",
            "default": False,
            "description": "true: nur den Plan zeigen (Prüfungen laufen, es wird nichts gesendet oder angelegt).",
        }
        return [
            ToolDescriptor(
                name="zustand",
                title="FYOS Post: Zustand",
                description=(
                    "Zustand von FYOS Post, dem eigenen Mail-Dienst (Listmonk): antwortet er, welche Listen mit wie "
                    "vielen Abonnenten, welche Vorlagen (ist „Mitteilung“ da?), die letzten fünf Kampagnen und wie viele "
                    f"Sendungen heute schon hinausgingen (Grenze {grenze} am Tag, UTC). Ohne Adresse von Listmonk "
                    "ist FYOS Post „nicht eingerichtet“ (`eingerichtet: false`, mit dem Weg zum Einrichten). Nur lesend, "
                    "zeigt keine Adressen."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "voll": {
                            "type": "boolean",
                            "default": False,
                            "description": "Vollform: mehr Felder je Liste, Vorlage und Kampagne (Anmeldeart, Format, Ziellisten, Link).",
                        }
                    },
                    "additionalProperties": False,
                },
                tags=_TAGS_LESEN,
            ),
            ToolDescriptor(
                name="senden",
                title="Mitteilung per Mail senden",
                description=(
                    "Sendet eine Mitteilung als E-Mail über FYOS Post (Absender ist die in Listmonk eingestellte Adresse; die "
                    "Fußzeile nennt, wer sendet). Betreff und Text als einfacher Text; `an` nur Adressen, die in FYOS Post als "
                    f"Abonnent vorhanden sind (höchstens {MAX_EMPFAENGER}; ohne Angabe der Standardempfänger, falls eingetragen). "
                    f"Eine Mail je Empfänger, höchstens {grenze} Sendungen am Tag. SENDET WIRKLICH; zum Ausprobieren "
                    "`trocken: true`. Das Ergebnis heißt „angenommen“, nicht „zugestellt“ (Fehler danach stehen im Protokoll "
                    "von Listmonk)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "betreff": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": BETREFF_MAX,
                            "description": f"Betreffzeile, eine Zeile, höchstens {BETREFF_MAX} Zeichen. Wird nicht als Vorlage ausgewertet.",
                        },
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": TEXT_MAX,
                            "description": (
                                f"Text der Mitteilung, einfacher Text (kein HTML), Zeilenumbrüche bleiben; höchstens {TEXT_MAX} Zeichen "
                                f"und {ZEILEN_MAX} Zeilen, zu lang ist ein Fehler. Wird nicht als Vorlage ausgewertet."
                            ),
                        },
                        "an": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": MAX_EMPFAENGER,
                            "description": (
                                f"Empfänger: nur Adressen, die in FYOS Post als Abonnent vorhanden sind, höchstens {MAX_EMPFAENGER}. "
                                "Ohne Angabe: der Standardempfänger dieser Installation (FYOS_POST_AN); ist keiner eingetragen, "
                                "ist `an` Pflicht. Andere Adressen lehnt Listmonk ab."
                            ),
                        },
                        "trocken": trocken,
                    },
                    "required": ["betreff", "text"],
                    "additionalProperties": False,
                },
                tags=_TAGS_SENDEN,
            ),
            ToolDescriptor(
                name="kampagne_entwurf",
                title="Kampagne als Entwurf anlegen",
                description=(
                    "Legt in FYOS Post eine Kampagne (Serienmail) als ENTWURF an. Gestartet wird sie nie von hier: den Start "
                    "macht ein Mensch in der Listmonk-Oberfläche. `listen` sind Nummern oder Namen (siehe post.zustand), "
                    "`text` der Inhalt. Achtung: Listmonk wertet {{ … }} im Inhalt als Vorlage aus (z. B. {{ .Subscriber.Name }}). "
                    "Es geht keine Mail hinaus; zum Prüfen post.kampagne_testen."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": KAMPAGNE_NAME_MAX,
                            "description": "Interner Name der Kampagne (erscheint nur in der Oberfläche).",
                        },
                        "betreff": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": KAMPAGNE_BETREFF_MAX,
                            "description": "Betreffzeile der Mail, eine Zeile.",
                        },
                        "listen": {
                            "type": "array",
                            "items": {"type": ["integer", "string"]},
                            "minItems": 1,
                            "maxItems": LISTEN_MAX,
                            "description": "Ziellisten als Nummern oder Namen (Namen ohne Beachtung der Groß-/Kleinschreibung); siehe post.zustand.",
                        },
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": KAMPAGNE_TEXT_MAX,
                            "description": "Inhalt der Mail im gewählten Format. Listmonk-Vorlagenausdrücke {{ … }} werden ausgewertet.",
                        },
                        "format": {
                            "type": "string",
                            "enum": list(FORMATE),
                            "default": "markdown",
                            "description": "Format des Inhalts: markdown (Standard), html oder plain.",
                        },
                        "vorlage": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Nummer einer Kampagnenvorlage in Listmonk (nicht die Vorlage „Mitteilung“). Ohne Angabe gilt die Standardvorlage.",
                        },
                        "trocken": trocken,
                    },
                    "required": ["name", "betreff", "listen", "text"],
                    "additionalProperties": False,
                },
                tags=_TAGS_SCHREIBEN,
            ),
            ToolDescriptor(
                name="kampagne_testen",
                title="Testmail einer Kampagne senden",
                description=(
                    "Schickt eine Testmail der Kampagne `id` mit ihrem gespeicherten Inhalt an vorhandene Abonnenten "
                    f"(höchstens {MAX_TEST_EMPFAENGER}; ohne Angabe der Standardempfänger, falls eingetragen). Startet die "
                    f"Kampagne nicht. SENDET WIRKLICH und zählt zur Tagesgrenze von {grenze}; zum Ausprobieren `trocken: true`. "
                    "„Angenommen“ heißt nicht „zugestellt“."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Nummer der Kampagne (aus post.kampagne_entwurf oder post.zustand).",
                        },
                        "an": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": MAX_TEST_EMPFAENGER,
                            "description": (
                                f"Empfänger der Testmail: nur Abonnenten, die in FYOS Post vorhanden sind, höchstens {MAX_TEST_EMPFAENGER}. "
                                "Ohne Angabe: der Standardempfänger dieser Installation (FYOS_POST_AN); ist keiner eingetragen, "
                                "ist `an` Pflicht."
                            ),
                        },
                        "trocken": trocken,
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
                tags=_TAGS_SENDEN,
            ),
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        args = args or {}
        if method == "zustand":
            return self.zustand(bool(args.get("voll")))
        if method == "senden":
            return self.senden(args)
        if method == "kampagne_entwurf":
            return self.kampagne_entwurf(args)
        if method == "kampagne_testen":
            return self.kampagne_testen(args)
        raise ValueError(f"post hat kein Werkzeug {method!r}")
