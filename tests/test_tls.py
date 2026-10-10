"""Verschlüsselung: nur TLS, Zertifikat und Servername werden geprüft, nie geht ein Passwort unverschlüsselt hinaus.

Die anderen Tests umgehen TLS (Fixture ``klartext``), damit sie kein Zertifikat brauchen. Diese hier nicht: sie sprechen
mit der Attrappe über echtes TLS, mit Zertifikaten, die das Programm ``openssl`` für den Lauf erzeugt (eine Zertifizierungs-
stelle, die der Test über ``SSL_CERT_FILE`` bekannt macht, eine fremde, und Zertifikate für richtigen und falschen
Namen). Fehlt ``openssl``, werden sie übersprungen.
"""

from __future__ import annotations

import imaplib
import shutil
import ssl
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from attrappe import n

from fyos_plugin_mail import imap
from fyos_plugin_mail.imap import ImapFehler, Sitzung

OPENSSL = shutil.which("openssl")
pytestmark = pytest.mark.skipif(OPENSSL is None, reason="openssl nicht gefunden (die Tests erzeugen Zertifikate damit)")

PASSWORT = "nur-fuer-den-test-xyz"

CA_KONFIG = """\
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_ca
[dn]
CN = {name}
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
"""

SERVER_KONFIG = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = Attrappe
[v3_server]
basicConstraints = CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = {san}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


def _openssl(ordner: Path, *argumente: str) -> None:
    subprocess.run([OPENSSL, *argumente], cwd=ordner, check=True, capture_output=True, timeout=120)


def _stelle(ordner: Path, name: str) -> Path:
    """Eine Zertifizierungsstelle ``name``; gibt ihr Zertifikat zurück (der Schlüssel liegt daneben)."""
    (ordner / f"{name}.cnf").write_text(CA_KONFIG.format(name=f"Test-Stelle {name}"))
    _openssl(ordner, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.pem",
             "-days", "3", "-config", f"{name}.cnf")
    return ordner / f"{name}.pem"


def _ausstellen(ordner: Path, stelle: str, name: str, san: str) -> SimpleNamespace:
    """Ein Serverzertifikat, von ``stelle`` unterschrieben, für die Namen in ``san``."""
    (ordner / f"{name}.cnf").write_text(SERVER_KONFIG.format(san=san))
    _openssl(ordner, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
             "-config", f"{name}.cnf")
    _openssl(ordner, "x509", "-req", "-in", f"{name}.csr", "-CA", f"{stelle}.pem", "-CAkey", f"{stelle}.key",
             "-CAcreateserial", "-out", f"{name}.pem", "-days", "3", "-extfile", f"{name}.cnf", "-extensions", "v3_server")
    return SimpleNamespace(zertifikat=ordner / f"{name}.pem", schluessel=ordner / f"{name}.key")


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    ordner = tmp_path_factory.mktemp("pki")
    eigene = _stelle(ordner, "eigene")
    fremde = _stelle(ordner, "fremde")
    gut = "IP:127.0.0.1,DNS:localhost"
    return SimpleNamespace(
        eigene_stelle=eigene,
        fremde_stelle=fremde,
        richtig=_ausstellen(ordner, "eigene", "richtig", gut),
        falscher_name=_ausstellen(ordner, "eigene", "falscher-name", "DNS:anderer-host.example.org"),
        von_fremder_stelle=_ausstellen(ordner, "fremde", "von-fremder-stelle", gut),
    )


def kontext(zertifikat: SimpleNamespace) -> ssl.SSLContext:
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(str(zertifikat.zertifikat), str(zertifikat.schluessel))
    return server


@pytest.fixture
def vertraut(monkeypatch, pki):
    """Der Besitzer hat seine Zertifizierungsstelle in ``SSL_CERT_FILE`` eingetragen (so steht es in der README)."""
    monkeypatch.setenv("SSL_CERT_FILE", str(pki.eigene_stelle))


def posteingang():
    return {"INBOX": [n(1, "Hallo", text="Ein Text.")]}


def verbinden(server, konto_von, **felder):
    with Sitzung(konto_von(server, passwort=PASSWORT, **felder), frist=5.0) as s:
        return [o.name for o in s.ordner()]


# ── IMAPS ───────────────────────────────────────────────────────────────────────────────────────────────
def test_imaps_mit_bekannter_stelle_und_richtigem_namen(imap_server, konto_von, pki, vertraut):
    server = imap_server(posteingang(), passwort=PASSWORT, tls=kontext(pki.richtig))
    assert verbinden(server, konto_von) == ["INBOX"]
    assert server.verben() == ["LOGIN", "LIST", "LOGOUT"]


def test_imaps_ohne_bekannte_stelle_wird_abgelehnt_und_kein_passwort_geht_hinaus(imap_server, konto_von, pki):
    # gleiche Gegenstelle wie im Test davor, nur ohne SSL_CERT_FILE: die Stelle ist dem Rechner unbekannt
    server = imap_server(posteingang(), passwort=PASSWORT, tls=kontext(pki.richtig))
    with pytest.raises(ImapFehler) as e:
        verbinden(server, konto_von)
    assert e.value.art == "tls" and e.value.status == 503
    assert "nicht vertrauenswürdig" in str(e.value) and "SSL_CERT_FILE" in str(e.value)
    assert PASSWORT not in str(e.value)
    assert server.befehle == []  # der Handschlag scheitert, bevor ein einziger Befehl gesendet wird


def test_imaps_mit_fremder_stelle_wird_abgelehnt(imap_server, konto_von, pki, vertraut):
    server = imap_server(posteingang(), passwort=PASSWORT, tls=kontext(pki.von_fremder_stelle))
    with pytest.raises(ImapFehler) as e:
        verbinden(server, konto_von)
    assert e.value.art == "tls" and "nicht vertrauenswürdig" in str(e.value)
    assert server.befehle == []


def test_imaps_mit_falschem_namen_wird_abgelehnt(imap_server, konto_von, pki, vertraut):
    server = imap_server(posteingang(), passwort=PASSWORT, tls=kontext(pki.falscher_name))
    with pytest.raises(ImapFehler) as e:
        verbinden(server, konto_von)
    assert e.value.art == "tls" and "nicht vertrauenswürdig" in str(e.value) and "127.0.0.1" in str(e.value)
    assert server.befehle == []


def test_klartextserver_am_imaps_port(imap_server, konto_von):
    server = imap_server(posteingang(), passwort=PASSWORT)  # spricht kein TLS
    with pytest.raises(ImapFehler) as e:
        verbinden(server, konto_von)
    assert e.value.art == "tls" and "993 gehört zu imaps, 143 zu starttls" in str(e.value)
    assert "LOGIN" not in server.verben(mit_faehigkeiten=True) and PASSWORT not in str(e.value)


# ── STARTTLS ────────────────────────────────────────────────────────────────────────────────────────────
def test_starttls_kommt_vor_der_anmeldung(imap_server, konto_von, pki, vertraut):
    server = imap_server(posteingang(), passwort=PASSWORT, starttls=kontext(pki.richtig))
    assert verbinden(server, konto_von, sicherheit="starttls") == ["INBOX"]
    verben = server.verben(mit_faehigkeiten=True)
    assert verben.index("STARTTLS") < verben.index("LOGIN")
    assert verben[-1] == "LOGOUT" and server.verboten == []


def test_starttls_mit_unbekannter_stelle_schickt_kein_passwort(imap_server, konto_von, pki):
    server = imap_server(posteingang(), passwort=PASSWORT, starttls=kontext(pki.richtig))
    with pytest.raises(ImapFehler) as e:
        verbinden(server, konto_von, sicherheit="starttls")
    assert e.value.art == "tls" and "nicht vertrauenswürdig" in str(e.value)
    assert "LOGIN" not in server.verben(mit_faehigkeiten=True)


def test_starttls_mit_falschem_namen(imap_server, konto_von, pki, vertraut):
    server = imap_server(posteingang(), passwort=PASSWORT, starttls=kontext(pki.falscher_name))
    with pytest.raises(ImapFehler) as e:
        verbinden(server, konto_von, sicherheit="starttls")
    assert e.value.art == "tls" and "LOGIN" not in server.verben(mit_faehigkeiten=True)


def test_ohne_starttls_beim_server_gibt_es_keine_anmeldung_im_klartext(imap_server, konto_von):
    server = imap_server(posteingang(), passwort=PASSWORT)  # bietet STARTTLS nicht an
    with pytest.raises(ImapFehler) as e:
        verbinden(server, konto_von, sicherheit="starttls")
    assert e.value.art == "tls" and "kein STARTTLS" in str(e.value) and "unverschlüsselt verbindet das Plugin nie" in str(e.value)
    assert "LOGIN" not in server.verben(mit_faehigkeiten=True) and PASSWORT not in str(e.value)


def test_lehnt_der_server_starttls_ab_geht_es_nicht_im_klartext_weiter(imap_server, konto_von, pki, vertraut):
    server = imap_server(posteingang(), passwort=PASSWORT, starttls=kontext(pki.richtig))
    server.fehler_bei["STARTTLS"] = "NO nicht jetzt"
    with pytest.raises(ImapFehler) as e:
        verbinden(server, konto_von, sicherheit="starttls")
    assert e.value.art == "tls" and "unverschlüsselt verbindet das Plugin nie" in str(e.value)
    assert "LOGIN" not in server.verben(mit_faehigkeiten=True)


def test_tls_server_am_starttls_port_haengt_nicht_ewig(imap_server, konto_von, pki, vertraut):
    server = imap_server(posteingang(), passwort=PASSWORT, tls=kontext(pki.richtig))  # wartet auf einen TLS-Anfang
    start = time.monotonic()
    with pytest.raises(ImapFehler) as e, Sitzung(konto_von(server, passwort=PASSWORT, sicherheit="starttls"), frist=0.5):
        pass
    assert e.value.art in ("zeit", "tls", "verbindung") and time.monotonic() - start < 10


# ── Einstellungen ───────────────────────────────────────────────────────────────────────────────────────
def test_der_kontext_prueft_alles_und_verlangt_mindestens_tls_1_2():
    k = imap._kontext()
    assert k.verify_mode == ssl.CERT_REQUIRED and k.check_hostname is True
    assert k.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_es_gibt_im_paket_keinen_weg_die_pruefung_abzustellen():
    quellen = {p.name: p.read_text(encoding="utf-8") for p in Path(imap.__file__).parent.glob("*.py")}
    for verboten in ("CERT_NONE", "CERT_OPTIONAL", "_create_unverified_context", "_create_default_https_context",
                     "check_hostname", "verify_mode", "set_ciphers", "PROTOCOL_TLSv1", "PROTOCOL_SSLv", "OP_NO_"):
        gefunden = [name for name, text in quellen.items() if verboten in text]
        assert gefunden == [], (verboten, gefunden)


def test_unverschluesselt_wird_nur_fuer_den_start_von_starttls_geoeffnet():
    """``imaplib.IMAP4(`` (ohne SSL) kommt im Paket genau einmal vor: in ``_oeffnen``, unmittelbar vor ``starttls``."""
    quellen = [p.read_text(encoding="utf-8") for p in Path(imap.__file__).parent.glob("*.py")]
    treffer = [text for text in quellen if "imaplib.IMAP4(" in text]
    assert len(treffer) == 1 and treffer[0].count("imaplib.IMAP4(") == 1
    assert "starttls(ssl_context=kontext)" in treffer[0]
    assert imaplib.IMAP4_SSL.__name__ in treffer[0]
