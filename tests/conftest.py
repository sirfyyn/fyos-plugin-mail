"""Gemeinsame Vorgaben: die Tests laufen ohne Redis, Qdrant und Anmeldung, ohne Proxy und ohne die Umgebung des Rechners.

Echte Postfächer kommen nie vor; die Server sind Attrappen auf 127.0.0.1 (``attrappe.py``) mit erfundenen Konten.
"""

from __future__ import annotations

import imaplib
import os

import pytest


def pytest_configure(config):
    os.environ.setdefault("MCP_HUB_AUDIT_SKIP_QDRANT", "1")
    for name in ("REDIS_URL", "QDRANT_URL", "FYOS_EMBED_URL", "HUB_SUCHE_EMBED_URL", "FYOS_OIDC_ISSUER"):
        os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def umgebung(monkeypatch):
    """Keine Mail- und Post-Umgebung des Rechners, kein Proxy, und keine Reste in der Postfach-Quelle des Kerns."""
    for name in list(os.environ):
        if name.startswith(("FYOS_MAIL_", "FYOS_POSTFACH_", "FYOS_POST_", "POST_URL", "POST_TOKEN_", "FYOS_AGENT_NAME", "SSL_CERT_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    from fyos_plugin_mail import kern

    postfach = kern.modul()
    if postfach is not None:
        postfach.ablage_anmelden(None)
        postfach.dienst_anmelden(None)
    yield
    if postfach is not None:
        postfach.ablage_anmelden(None)
        postfach.dienst_anmelden(None)


@pytest.fixture
def server_liste():
    """Sammelt Attrappen-Server und hält sie am Ende an."""
    server = []
    yield server
    for s in server:
        s.stop()


@pytest.fixture
def imap_server(server_liste):
    """``imap_server(ordner, **Einstellungen)`` startet eine IMAP-Attrappe (siehe ``attrappe.Attrappe``)."""
    from attrappe import Attrappe

    def neu(ordner=None, **kw):
        s = Attrappe(ordner, **kw).start()
        server_liste.append(s)
        return s

    return neu


@pytest.fixture
def klartext(monkeypatch):
    """Die Attrappe spricht ohne TLS, damit die meisten Tests kein Zertifikat brauchen. Nur diese Stelle (``imap._oeffnen``)
    wird ersetzt; ``test_tls.py`` prüft die verschlüsselten Wege gegen dieselbe Attrappe mit echtem Zertifikat."""
    from fyos_plugin_mail import imap

    def oeffnen(konto, frist):
        return imaplib.IMAP4(konto.host, konto.port, timeout=frist)

    monkeypatch.setattr(imap, "_oeffnen", oeffnen)


@pytest.fixture
def konto_von():
    """``konto_von(server, name="Privat", **felder)`` gibt das Konto zur Attrappe."""
    from fyos_plugin_mail.konten import Konto

    def neu(server, name="Privat", **felder):
        werte = {
            "host": server.host, "port": server.port, "sicherheit": "imaps", "nutzer": server.nutzer,
            "passwort": server.passwort, "ordner": ("INBOX",),
        }
        werte.update(felder)
        return Konto(name=name, **werte)

    return neu
