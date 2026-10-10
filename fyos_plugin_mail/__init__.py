"""fyos_plugin_mail: Mails aus IMAP-Konten lesen (strikt lesend) und über einen eigenen Listmonk senden (FYOS Post).

Das Paket meldet sich über den Entry-Point ``mcp_hub.plugins`` beim Hub an (``plugin.py`` und ``post.py``), über ``fyos.skills``
mit seinem Skill und über ``fyos.katalog`` mit dem Katalogeintrag für FYOS Post. Die Module teilen sich die Arbeit so:

* ``konten``     Konten aus Umgebung und Konfiguration (nie ein Passwort in einer Darstellung)
* ``imap``       die einzige Stelle, die IMAP spricht: nur lesende Befehle, nur TLS, Zertifikat geprüft
* ``antwort``    Antworten des Servers lesen (FETCH, LIST, Literale)
* ``struktur``   MIME-Aufbau aus BODYSTRUCTURE: Textteile, Anhänge, Dateinamen
* ``nachricht``  Kopfzeilen, Kodierungen, HTML zu Text, Ausschnitt
* ``suche``      die Gmail-artige Abfrage in IMAP SEARCH übersetzen
* ``utf7``       Ordnernamen (IMAP-UTF-7)
* ``ablage``     Konten zusammenführen: liste und lesen für den Kern (``mcp_hub.postfach``)
* ``kern``       die Verbindung zur Postfach-Quelle des Kerns (ab fyos-hub 0.1.1)
* ``plugin``     die Werkzeuge ``mail.konten``, ``mail.eingang``, ``mail.suchen``, ``mail.lesen``
* ``post``       FYOS Post (Listmonk): ``post.zustand``, ``post.senden``, ``post.kampagne_entwurf``, ``post.kampagne_testen``
* ``post_einrichten``  das Kommando ``fyos-post-einrichten`` (Vorlage, Rolle und API-Zugänge in Listmonk)
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.2.0"

KATALOG_DATEI = Path(__file__).resolve().parent / "fyos-katalog.yaml"


def katalog_pfad() -> Path:
    """Pfad der Katalogdatei dieses Pakets (Baustein „FYOS Post“). Die Skizze liegt daneben unter ``bilder/``."""
    return KATALOG_DATEI
