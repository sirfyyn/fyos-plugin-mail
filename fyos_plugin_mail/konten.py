"""Konten: was der Besitzer einträgt und wie das Plugin es liest.

**Ein Konto aus der Umgebung** (der einfache Weg, auch das Formular des Installers füllt diese Felder):

===========================  =============================================================================================
``FYOS_MAIL_HOST``           IMAP-Server, nur der Name (``imap.example.org``), ohne ``imaps://``
``FYOS_MAIL_PORT``           Port, Vorgabe 993 (IMAPS); mit ``FYOS_MAIL_SICHERHEIT=starttls`` Vorgabe 143
``FYOS_MAIL_NUTZER``         Anmeldename, meist die Mail-Adresse
``FYOS_MAIL_PASSWORT``       App-Passwort (nie das Haupt-Passwort des Anbieters)
``FYOS_MAIL_NAME``           Anzeigename des Kontos; ohne Angabe der Host
``FYOS_MAIL_ORDNER``         Ordner, die Suchen ohne ``in:`` durchsuchen, durch Komma getrennt; Vorgabe ``INBOX``
``FYOS_MAIL_SICHERHEIT``     ``imaps`` (Verbindung ist von Anfang an verschlüsselt) oder ``starttls``; ohne Angabe
                             ``starttls`` für Port 143, sonst ``imaps``. Unverschlüsselt verbindet das Plugin nie.
===========================  =============================================================================================

**Weitere Konten** stehen als JSON-Liste in ``FYOS_MAIL_KONTEN`` (oder in der ``config:`` des Plugins unter ``konten:``):
je Konto ``name`` (Pflicht), ``host``, ``port``, ``nutzer``, ``passwort_env``, ``ordner`` (Liste) und ``sicherheit``.
``passwort_env`` ist der **Name einer Umgebungsvariablen**, die das App-Passwort enthält. Ein Passwort im JSON selbst lehnt
das Plugin ab (es stünde in Konfigurationsdateien und Sicherungen).

Ein unbrauchbarer Eintrag bricht nichts ab: er wird ein Konto mit ``fehler`` und zeigt seinen Grund in jeder Antwort.
Das Passwort steht in keiner Darstellung eines Kontos (``repr``, ``als_dict``) und in keiner Fehlermeldung.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

KONTEN_MAX = 20
NAME_MAX = 60
NUTZER_MAX = 200
ORDNER_MAX = 10
ORDNERNAME_MAX = 200
SICHERHEIT = ("imaps", "starttls")
STANDARD_PORT = {"imaps": 993, "starttls": 143}
_JSON_FELDER = {"name", "host", "port", "nutzer", "passwort_env", "ordner", "sicherheit"}
_UMGEBUNGSNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
_NAME_VERBOTEN = re.compile(r"[:#/\\\x00-\x1f\x7f]")
_STEUERZEICHEN = re.compile(r"[\x00-\x1f\x7f]")
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_VORSATZ = ("imap.", "imaps.", "mail.", "secure.", "imap-mail.", "www.")
_UNVERSCHLUESSELT = {"none", "keine", "klartext", "plain", "plaintext", "unverschluesselt", "unverschlüsselt", "no", "nein"}


@dataclass(frozen=True)
class Konto:
    """Ein IMAP-Konto. Das Passwort steht in keiner Darstellung (``repr``, ``als_dict``)."""

    name: str
    host: str = ""
    port: int = 993
    sicherheit: str = "imaps"  # imaps | starttls
    nutzer: str = ""
    passwort: str = field(default="", repr=False)
    ordner: tuple[str, ...] = ("INBOX",)
    fehler: str = ""  # gesetzt: der Eintrag ist unbrauchbar, das ist der Grund

    @property
    def adresse(self) -> str:
        """Host und Port; ohne Host leer."""
        if not self.host:
            return ""
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    @property
    def kennung(self) -> str:
        """Kurze, stabile Kennung des Kontos (für Mail-Kennungen): zehn Hex-Zeichen des Namens."""
        return kennung_von(self.name)

    def als_dict(self) -> dict[str, Any]:
        """Für Antworten: Name, Art, Adresse, Nutzer und Ordner; nie das Passwort."""
        aus: dict[str, Any] = {"name": self.name, "art": "imap"}
        if self.adresse:
            aus["adresse"] = self.adresse
            aus["sicherheit"] = self.sicherheit
        if self.nutzer:
            aus["nutzer"] = self.nutzer
        if self.host:
            aus["ordner"] = list(self.ordner)
        if self.fehler:
            aus["fehler"] = self.fehler
        return aus


def kennung_von(text: str) -> str:
    """Zehn Hex-Zeichen aus dem Namen (ohne Rücksicht auf Groß- und Kleinschreibung); stabil über Neustarts."""
    return hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:10]


def ordner_kennung(name: str) -> str:
    """Zehn Hex-Zeichen aus dem Ordnernamen im Klartext. Ordnernamen unterscheiden Groß- und Kleinschreibung (nur der
    Posteingang nicht), deshalb bleibt sie hier erhalten."""
    return hashlib.sha256(("INBOX" if name.upper() == "INBOX" else name).encode("utf-8")).hexdigest()[:10]


def _name_aus(host: str) -> str:
    name = host.lower()
    for vorsatz in _VORSATZ:
        if name.startswith(vorsatz) and name.count(".") >= 2:
            name = name[len(vorsatz):]
            break
    return name[:NAME_MAX] or "mail"


def _name_pruefen(roh: Any) -> str:
    if not isinstance(roh, str):
        raise ValueError("name muss ein Text sein")
    name = " ".join(roh.split())
    if not name:
        raise ValueError("name ist leer")
    if len(name) > NAME_MAX:
        raise ValueError(f"name ist länger als {NAME_MAX} Zeichen")
    if _NAME_VERBOTEN.search(name):
        raise ValueError("name enthält ein unzulässiges Zeichen (: # / \\)")
    return name


def _host_pruefen(roh: Any) -> str:
    if not isinstance(roh, str) or not roh.strip():
        raise ValueError("host fehlt")
    host = roh.strip()
    if "://" in host:
        raise ValueError("host ist nur der Servername (imap.example.org), ohne imaps:// und ohne Pfad")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if ":" in host:  # IPv6
        try:
            return str(ipaddress.IPv6Address(host))
        except ValueError:
            raise ValueError("host ist weder ein Servername noch eine IPv6-Adresse (Port gehört in port)") from None
    if not _HOSTNAME.fullmatch(host):
        raise ValueError(
            "host ist kein gültiger Servername (Buchstaben, Ziffern, Punkt und Bindestrich; "
            "Umlaute in der Punycode-Schreibweise xn--…)"
        )
    return host


def _sicherheit_pruefen(roh: Any) -> str | None:
    """``None``, wenn nichts angegeben ist."""
    if roh in (None, ""):
        return None
    if not isinstance(roh, str):
        raise ValueError("sicherheit muss imaps oder starttls sein")
    wert = roh.strip().lower()
    if wert in _UNVERSCHLUESSELT:
        raise ValueError("unverschlüsselte Verbindungen gibt es nicht; sicherheit ist imaps oder starttls")
    if wert in ("imaps", "ssl", "tls"):
        return "imaps"
    if wert == "starttls":
        return "starttls"
    raise ValueError("sicherheit muss imaps oder starttls sein")


def _port_pruefen(roh: Any, standard: int) -> int:
    if roh in (None, ""):
        return standard
    if isinstance(roh, bool):
        raise ValueError("port muss eine Zahl sein")
    try:
        port = int(str(roh).strip())
    except ValueError:
        raise ValueError("port muss eine Zahl sein") from None
    if not 1 <= port <= 65535:
        raise ValueError("port muss zwischen 1 und 65535 liegen")
    return port


def _nutzer_pruefen(roh: Any) -> str:
    if not isinstance(roh, str) or not roh.strip():
        raise ValueError("nutzer fehlt")
    nutzer = roh.strip()
    if len(nutzer) > NUTZER_MAX or _STEUERZEICHEN.search(nutzer):
        raise ValueError("nutzer ist zu lang oder enthält Steuerzeichen")
    return nutzer


def _ordner_pruefen(roh: Any) -> tuple[str, ...]:
    if roh in (None, "", []):
        return ("INBOX",)
    if isinstance(roh, str):
        teile: list[Any] = roh.split(",")
    elif isinstance(roh, list):
        teile = roh
    else:
        raise ValueError("ordner muss eine Liste von Ordnernamen sein")
    ordner: list[str] = []
    for teil in teile:
        if not isinstance(teil, str):
            raise ValueError("ordner muss eine Liste von Ordnernamen sein")
        name = teil.strip()
        if not name:
            continue
        if len(name) > ORDNERNAME_MAX or _STEUERZEICHEN.search(name):
            raise ValueError("ein Ordnername ist zu lang oder enthält Steuerzeichen")
        if name.casefold() == "inbox":
            name = "INBOX"  # der Posteingang heißt auf jedem Server so, gleich wie man ihn schreibt
        if name not in ordner:
            ordner.append(name)
    if not ordner:
        return ("INBOX",)
    if len(ordner) > ORDNER_MAX:
        raise ValueError(f"höchstens {ORDNER_MAX} Ordner je Konto")
    return tuple(ordner)


def _konto_bauen(
    name: str | None, host: Any, port: Any, sicherheit: Any, nutzer: Any, passwort: str, ordner: Any,
    passwort_hinweis: str,
) -> Konto:
    """Prüft einen Eintrag; ``ValueError`` mit dem Grund (nie mit dem Passwort)."""
    host_text = _host_pruefen(host)
    gewaehlt = _sicherheit_pruefen(sicherheit)
    port_zahl = _port_pruefen(port, STANDARD_PORT[gewaehlt or "imaps"])
    art = gewaehlt or ("starttls" if port_zahl == 143 else "imaps")
    nutzer_text = _nutzer_pruefen(nutzer)
    if not passwort:
        raise ValueError(f"es fehlt das App-Passwort ({passwort_hinweis})")
    if re.search(r"[\x00-\x08\x0a-\x1f\x7f]", passwort):
        raise ValueError(f"das App-Passwort enthält ein Steuerzeichen oder einen Zeilenumbruch ({passwort_hinweis})")
    return Konto(
        name=_name_pruefen(name) if name else _name_aus(host_text), host=host_text, port=port_zahl, sicherheit=art,
        nutzer=nutzer_text, passwort=passwort, ordner=_ordner_pruefen(ordner),
    )


def _eintrag_aus_json(roh: Any, umgebung: Mapping[str, str]) -> Konto:
    if not isinstance(roh, dict):
        raise ValueError("ein Konto muss ein Objekt mit name, host, port, nutzer, passwort_env und ordner sein")
    if "passwort" in roh or "password" in roh:
        raise ValueError("Passwörter gehören nicht in die Liste: passwort_env nennt stattdessen eine Umgebungsvariable")
    fremd = sorted(str(k) for k in set(roh) - _JSON_FELDER)
    if fremd:
        raise ValueError(f"unbekannte Felder: {', '.join(fremd)}")
    if roh.get("name") in (None, ""):
        raise ValueError("name fehlt (mit mehreren Konten unterscheidet er sie)")
    ref = roh.get("passwort_env")
    if ref in (None, ""):
        raise ValueError("passwort_env fehlt: der Name der Umgebungsvariablen mit dem App-Passwort")
    if not isinstance(ref, str) or not _UMGEBUNGSNAME.fullmatch(ref):
        raise ValueError("passwort_env muss der Name einer Umgebungsvariablen sein")
    passwort = (umgebung.get(ref) or "").strip()
    if not passwort:
        raise ValueError(f"die Umgebungsvariable {ref} ist nicht gesetzt oder leer")
    return _konto_bauen(
        roh.get("name"), roh.get("host"), roh.get("port"), roh.get("sicherheit"), roh.get("nutzer"), passwort,
        roh.get("ordner"), "passwort_env",
    )


def _liste(roh: Any, quelle: str) -> list[Any]:
    if not isinstance(roh, list):
        raise ValueError(f"{quelle} muss eine Liste von Konten sein")
    return roh


def konten_lesen(umgebung: Mapping[str, str], config: Mapping[str, Any] | None = None) -> list[Konto]:
    """Alle Konten: das aus ``FYOS_MAIL_*``, dann die aus ``config: konten:``, dann die aus ``FYOS_MAIL_KONTEN``.
    Jeder unbrauchbare Eintrag wird ein Konto mit ``fehler``."""
    konten: list[Konto] = []
    namen: set[str] = set()

    def aufnehmen(konto: Konto) -> None:
        if len(konten) >= KONTEN_MAX:
            return
        if konto.name.casefold() in namen:
            konto = Konto(name=f"{konto.name} ({len(konten) + 1})", fehler="der Name ist schon vergeben")
        namen.add(konto.name.casefold())
        konten.append(konto)

    def fehlerhaft(bezeichnung: str, grund: str) -> None:
        name = bezeichnung
        while name.casefold() in namen:
            name += "+"
        aufnehmen(Konto(name=name, fehler=grund))

    host = (umgebung.get("FYOS_MAIL_HOST") or "").strip()
    if host:
        try:
            name = (umgebung.get("FYOS_MAIL_NAME") or "").strip() or None
            aufnehmen(_konto_bauen(
                name, host, umgebung.get("FYOS_MAIL_PORT"), umgebung.get("FYOS_MAIL_SICHERHEIT"),
                umgebung.get("FYOS_MAIL_NUTZER"), (umgebung.get("FYOS_MAIL_PASSWORT") or "").strip(),
                umgebung.get("FYOS_MAIL_ORDNER"), "FYOS_MAIL_PASSWORT",
            ))
        except ValueError as fehler:
            fehlerhaft("FYOS_MAIL_HOST", str(fehler))

    quellen: list[tuple[str, Any]] = []
    if config and config.get("konten") not in (None, ""):
        quellen.append(("konten in der Plugin-Konfiguration", config.get("konten")))
    json_text = (umgebung.get("FYOS_MAIL_KONTEN") or "").strip()
    if json_text:
        try:
            quellen.append(("FYOS_MAIL_KONTEN", json.loads(json_text)))
        except ValueError:
            fehlerhaft("FYOS_MAIL_KONTEN", "ist kein gültiges JSON (erwartet: eine Liste von Konten)")
    eintraege: list[tuple[str, Any]] = []
    for bezeichnung, roh in quellen:
        try:
            liste = _liste(roh, bezeichnung)
        except ValueError as fehler:
            fehlerhaft(bezeichnung, str(fehler))
            continue
        eintraege += [(f"{bezeichnung}, Eintrag {i}", e) for i, e in enumerate(liste, 1)]
    for bezeichnung, roh in eintraege:
        try:
            aufnehmen(_eintrag_aus_json(roh, umgebung))
        except ValueError as fehler:
            name = roh.get("name") if isinstance(roh, dict) and isinstance(roh.get("name"), str) else None
            fehlerhaft(" ".join(name.split())[:NAME_MAX] if name and name.strip() else bezeichnung, str(fehler))
    return konten
