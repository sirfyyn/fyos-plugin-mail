"""Die Fehler des Plugins. ``kern.py`` macht daraus an der Grenze zum Hub die Fehler der Postfach-Quelle."""

from __future__ import annotations


class MailFehler(Exception):
    """Die Mails sind nicht zu lesen: ein Konto antwortet nicht, lehnt die Anmeldung ab, der Ordner fehlt …

    ``str(fehler)`` ist ein Satz für den Besitzer, nie mit Zugangsdaten. ``status`` entspricht dem Status eines
    Mail-Dienstes: 401 Anmeldung, 404 gibt es nicht, 413 zu groß, 502 Server, 503 nicht erreichbar."""

    def __init__(self, text: str, status: int = 502) -> None:
        super().__init__(text)
        self.status = status


HINWEIS_EINRICHTEN = (
    "Kein Postfach eingerichtet: im Installer den Baustein „Mail“ wählen oder FYOS_MAIL_HOST, FYOS_MAIL_NUTZER und "
    "FYOS_MAIL_PASSWORT (App-Passwort) setzen; mehrere Konten stehen in FYOS_MAIL_KONTEN."
)


class NichtEingerichtet(MailFehler):
    """Es ist kein Konto eingetragen."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text or HINWEIS_EINRICHTEN, 501)
