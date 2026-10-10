"""Die Verbindung zur Postfach-Quelle des Kerns (``mcp_hub.postfach``, ab fyos-hub 0.1.1).

Das Plugin meldet sich dort als Ablage an, damit Leitstand, Tagesbrief und Startseite seine Mails lesen, ohne IMAP zu
können oder eine Adresse zu kennen; und seine Werkzeuge ``mail.*`` lesen über dieselbe Quelle, so dass ein eingetragener
Mail-Dienst (``FYOS_POSTFACH_URL``) dort Vorrang hat. Fehlt das Modul (ein älterer Kern), läuft das Plugin trotzdem: die
Werkzeuge arbeiten dann direkt mit den IMAP-Konten, nur die Anmeldung als Quelle entfällt.

Das Modul des Kerns wird bei jedem Aufruf neu nachgeschlagen (``modul()``), nicht beim Import: so folgt das Plugin einem
Kern, der erst später fertig geladen ist, und die Tests können beide Fälle herstellen.

Die Fehler des Plugins (``MailFehler``) werden an der Grenze zum Kern zu seinen Fehlerklassen (``nach_aussen``): nur die
nimmt ``AblageQuelle`` mit ihrem Text und Status an, jede andere Ausnahme würde dort zu „Das Mail-Plugin meldet einen
Fehler (Typ)“.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

from .fehler import MailFehler, NichtEingerichtet


def modul() -> ModuleType | None:
    """Das Modul ``mcp_hub.postfach`` oder ``None``, wenn der Kern es nicht hat."""
    try:
        return importlib.import_module("mcp_hub.postfach")
    except ImportError:  # fyos-hub 0.1.0 und älter
        return None


def verfuegbar() -> bool:
    return modul() is not None


def fehlertypen() -> tuple[type[BaseException], ...]:
    """Die Fehlerklassen, die ein Aufruf bei der Quelle des Kerns werfen kann (für ``except``)."""
    m = modul()
    return (MailFehler, m.QuelleFehler) if m is not None else (MailFehler,)


def ist_nicht_eingerichtet(fehler: BaseException) -> bool:
    m = modul()
    return isinstance(fehler, NichtEingerichtet) or (m is not None and isinstance(fehler, m.NichtEingerichtet))


def status_von(fehler: BaseException) -> int:
    return int(getattr(fehler, "status", 502) or 502)


def nach_aussen(fehler: MailFehler) -> BaseException:
    """Der Fehler in der Form, die der Kern erwartet (ohne Kern bleibt es der eigene)."""
    m = modul()
    if m is None:
        return fehler
    if isinstance(fehler, NichtEingerichtet):
        return m.NichtEingerichtet(str(fehler))
    return m.QuelleFehler(str(fehler), fehler.status)


def anmelden(ablage: Any) -> bool:
    """Meldet ``ablage`` als Quelle der Mails an. ``False``, wenn der Kern keine Postfach-Quelle hat."""
    m = modul()
    if m is None:
        return False
    m.ablage_anmelden(ablage)
    return True


def abmelden(ablage: Any) -> None:
    """Nimmt ``ablage`` zurück, aber nur, wenn sie noch die angemeldete ist (eine andere bleibt)."""
    m = modul()
    if m is not None and m.angemeldete_ablage() is ablage:
        m.ablage_anmelden(None)
