"""Die Verbindung zur Postfach-Quelle des Kerns, mit und ohne ``mcp_hub.postfach`` (fyos-hub 0.1.1 und älter).

Die Tests mit Kern laufen nur, wenn der installierte Kern die Quelle hat; die ohne Kern laufen immer (sie stellen den alten
Kern her, indem sie den Import verhindern).
"""

from __future__ import annotations

import sys

import pytest

from fyos_plugin_mail import kern
from fyos_plugin_mail.fehler import HINWEIS_EINRICHTEN, MailFehler, NichtEingerichtet

try:
    from mcp_hub import postfach
except ImportError:  # fyos-hub 0.1.0
    postfach = None

mit_kern = pytest.mark.skipif(postfach is None, reason="braucht einen Kern ab 0.1.1 (mcp_hub.postfach)")


@pytest.fixture
def ohne_kern(monkeypatch):
    """Ein Kern vor 0.1.1: ``import mcp_hub.postfach`` schlägt fehl."""
    monkeypatch.setitem(sys.modules, "mcp_hub.postfach", None)


class Ablage:
    def ablage_konten(self):
        return []

    def ablage_liste(self, abfrage, anzahl, konto=None):
        return {"mails": [], "ungelesen": 0}

    def ablage_lesen(self, id):  # noqa: A002
        return {"id": id}


# ── Modul ───────────────────────────────────────────────────────────────────────────────────────────────
@mit_kern
def test_modul_mit_kern():
    assert kern.modul() is postfach and kern.verfuegbar()


def test_modul_ohne_kern(ohne_kern):
    assert kern.modul() is None and not kern.verfuegbar()


@mit_kern
def test_der_kern_wird_bei_jedem_aufruf_neu_gefragt(monkeypatch):
    assert kern.verfuegbar()
    monkeypatch.setitem(sys.modules, "mcp_hub.postfach", None)
    assert not kern.verfuegbar()
    monkeypatch.undo()
    assert kern.verfuegbar()


# ── Fehler ──────────────────────────────────────────────────────────────────────────────────────────────
@mit_kern
def test_fehlertypen():
    assert kern.fehlertypen() == (MailFehler, postfach.QuelleFehler)


def test_fehlertypen_ohne_kern(ohne_kern):
    assert kern.fehlertypen() == (MailFehler,)


@mit_kern
def test_nicht_eingerichtet_erkennt_beide_herkunfte():
    assert kern.ist_nicht_eingerichtet(NichtEingerichtet())
    assert kern.ist_nicht_eingerichtet(postfach.NichtEingerichtet())
    assert not kern.ist_nicht_eingerichtet(MailFehler("Anmeldung abgelehnt", 401))
    assert not kern.ist_nicht_eingerichtet(postfach.QuelleFehler("aus", 503))
    assert not kern.ist_nicht_eingerichtet(ValueError("x"))


def test_nicht_eingerichtet_ohne_kern(ohne_kern):
    assert kern.ist_nicht_eingerichtet(NichtEingerichtet())
    assert not kern.ist_nicht_eingerichtet(MailFehler("x"))
    assert not kern.ist_nicht_eingerichtet(KeyError("x"))


def test_der_fehler_ohne_konto_nennt_den_weg_zur_einrichtung():
    f = NichtEingerichtet()
    assert f.status == 501 and str(f) == HINWEIS_EINRICHTEN
    assert "FYOS_MAIL_HOST" in str(f) and "FYOS_MAIL_PASSWORT" in str(f) and "FYOS_MAIL_KONTEN" in str(f)
    assert str(NichtEingerichtet("eigener Satz")) == "eigener Satz"


@pytest.mark.parametrize(("fehler", "erwartet"), [
    (MailFehler("x", 401), 401),
    (MailFehler("x"), 502),
    (MailFehler("x", 503), 503),
    (NichtEingerichtet(), 501),
    (ValueError("x"), 502),
    (type("F", (Exception,), {"status": 0})("x"), 502),
    (type("F", (Exception,), {"status": "404"})("x"), 404),
])
def test_status(fehler, erwartet):
    assert kern.status_von(fehler) == erwartet


@mit_kern
def test_nach_aussen_wird_zur_fehlerklasse_des_kerns():
    aussen = kern.nach_aussen(MailFehler("Anmeldung abgelehnt", 401))
    assert isinstance(aussen, postfach.QuelleFehler) and not isinstance(aussen, postfach.NichtEingerichtet)
    assert str(aussen) == "Anmeldung abgelehnt" and aussen.status == 401


@mit_kern
def test_nach_aussen_bei_nicht_eingerichtet():
    aussen = kern.nach_aussen(NichtEingerichtet())
    assert isinstance(aussen, postfach.NichtEingerichtet) and aussen.status == 501
    assert str(aussen) == HINWEIS_EINRICHTEN


def test_nach_aussen_ohne_kern_bleibt_der_eigene_fehler(ohne_kern):
    f = MailFehler("x", 503)
    assert kern.nach_aussen(f) is f


# ── Anmelden ────────────────────────────────────────────────────────────────────────────────────────────
@mit_kern
def test_anmelden_und_abmelden():
    ablage = Ablage()
    assert kern.anmelden(ablage) is True
    assert postfach.angemeldete_ablage() is ablage
    assert postfach.quelle().art == "ablage"
    kern.abmelden(ablage)
    assert postfach.angemeldete_ablage() is None and postfach.quelle().art == "keine"


@mit_kern
def test_abmelden_nimmt_nur_die_eigene_zurueck():
    meine, fremde = Ablage(), Ablage()
    kern.anmelden(fremde)
    kern.abmelden(meine)
    assert postfach.angemeldete_ablage() is fremde
    kern.abmelden(fremde)
    assert postfach.angemeldete_ablage() is None


@mit_kern
def test_abmelden_ohne_anmeldung_ist_still():
    kern.abmelden(Ablage())
    assert postfach.angemeldete_ablage() is None


def test_anmelden_ohne_kern(ohne_kern):
    assert kern.anmelden(Ablage()) is False
    kern.abmelden(Ablage())  # wirft nicht
