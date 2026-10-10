"""Das Paket: Entry-Points, Skill, Version, Beispiel-Konfiguration und dass die README-Dateien die Einstellungen nennen."""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import re
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from fyos_plugin_mail import __version__, katalog_pfad
from fyos_plugin_mail.plugin import MailPlugin
from fyos_plugin_mail.post import PostPlugin
from mcp_hub import registry as hub_registry
from mcp_hub.config import HubConfig, PluginConfig, load_config
from mcp_hub.plugin import HubPlugin

WURZEL = Path(__file__).resolve().parent.parent
PAKET = WURZEL / "fyos_plugin_mail"
WERKZEUGE = ("mail.konten", "mail.eingang", "mail.suchen", "mail.lesen")
POST_WERKZEUGE = ("post.zustand", "post.senden", "post.kampagne_entwurf", "post.kampagne_testen")


def pyproject() -> dict:
    return tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))


# ── Anmeldung beim Hub ───────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["mail", "post"])
def test_entry_point_steht_in_der_pyproject_und_ist_ladbar(name):
    ziel = pyproject()["project"]["entry-points"]["mcp_hub.plugins"][name]
    modul, klasse = ziel.split(":")
    cls = getattr(importlib.import_module(modul), klasse)
    assert issubclass(cls, HubPlugin) and cls.name == name


def test_genau_zwei_plugins_in_der_entry_point_gruppe():
    assert set(pyproject()["project"]["entry-points"]["mcp_hub.plugins"]) == {"mail", "post"}


def test_hub_findet_das_installierte_paket_per_entry_point():
    eps = {ep.name: ep for ep in importlib.metadata.entry_points(group="mcp_hub.plugins") if ep.name in ("mail", "post")}
    if "mail" not in eps:
        pytest.skip("Paket nicht installiert (uv pip install -e .)")
    assert eps["mail"].load() is MailPlugin
    if "post" not in eps:
        pytest.skip("installiert ist eine ältere Fassung des Pakets ohne `post`")
    assert eps["post"].load() is PostPlugin
    reg = hub_registry.build_registry(HubConfig())
    assert "mail.eingang" in reg.tools and "post.zustand" in reg.tools


def test_hub_laedt_post_per_modul_und_ohne_adresse_ist_es_aus():
    cfg = HubConfig(plugins=[PluginConfig(name="post", module="fyos_plugin_mail.post")])
    reg = hub_registry.build_registry(cfg)
    assert "post" not in reg.nicht_geladen
    assert sorted(t for t in reg.tools if t.startswith("post.")) == sorted(POST_WERKZEUGE)
    assert hub_registry.plugin_aus(reg.plugins["post"].health())  # ohne FYOS_POST_URL: aus, nicht kaputt


def test_hub_laedt_das_plugin_per_modul():
    cfg = HubConfig(plugins=[PluginConfig(name="mail", module="fyos_plugin_mail.plugin")])
    reg = hub_registry.build_registry(cfg)
    assert "mail" not in reg.nicht_geladen
    assert sorted(t for t in reg.tools if t.startswith("mail.")) == sorted(WERKZEUGE)
    # ohne Konto: aus, nicht kaputt
    assert hub_registry.plugin_aus(reg.plugins["mail"].health())


def test_version_in_pyproject_und_paket_ist_gleich():
    assert pyproject()["project"]["version"] == __version__ == MailPlugin.version == PostPlugin.version


def test_abhaengigkeiten_sind_nach_unten_begrenzt_und_schlank():
    abhaengig = pyproject()["project"]["dependencies"]
    deps = {re.split(r"[<>=!~ ]", d, maxsplit=1)[0] for d in abhaengig}
    assert deps == {"fyos-hub"}  # sonst nur die Standardbibliothek
    # Die Werkzeuge laufen schon auf 0.1.0; mcp_hub.postfach (die Anmeldung als Quelle) gibt es ab 0.1.1 und ist optional.
    assert any(d.startswith("fyos-hub>=0.1.0") for d in abhaengig)


def test_nur_standardbibliothek_und_hub_werden_eingebunden():
    erlaubt = set(sys.stdlib_module_names) | {"fyos_plugin_mail", "mcp_hub"}
    fremd: dict[str, set[str]] = {}
    for datei in sorted(PAKET.glob("*.py")):
        baum = ast.parse(datei.read_text(encoding="utf-8"))
        for knoten in ast.walk(baum):
            namen: list[str] = []
            if isinstance(knoten, ast.Import):
                namen = [a.name for a in knoten.names]
            elif isinstance(knoten, ast.ImportFrom) and knoten.level == 0 and knoten.module:
                namen = [knoten.module]
            for n in namen:
                if n.split(".")[0] not in erlaubt:
                    fremd.setdefault(datei.name, set()).add(n)
    assert not fremd, f"nicht erlaubte Einbindungen: {fremd}"


def test_lizenz_ist_apache_und_es_gibt_keinen_pypi_ablauf():
    daten = pyproject()
    assert daten["project"]["license"] == "Apache-2.0"
    lizenz = (WURZEL / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in lizenz and "Version 2.0" in lizenz
    assert (WURZEL / "NOTICE").exists()
    # Tests und Sauberkeit laufen auf GitHub; die Veröffentlichung auf PyPI richtet der Betreiber ein, nicht dieses Paket
    assert not (WURZEL / ".github" / "workflows" / "pypi.yml").exists()


# ── Skill ────────────────────────────────────────────────────────────────────────────────────────────────
def test_skill_liegt_im_paket_und_der_kopf_ist_gueltiges_yaml():
    daten = pyproject()
    assert daten["project"]["entry-points"]["fyos.skills"]["mail"] == "fyos_plugin_mail"
    assert daten["tool"]["setuptools"]["package-data"]["fyos_plugin_mail"] == ["skills/*/*.md", "fyos-katalog.yaml", "bilder/*"]
    datei = PAKET / "skills" / "mail" / "SKILL.md"
    assert datei in set(PAKET.glob("skills/*/*.md"))  # das Muster der Paketdaten trifft sie
    skills = pytest.importorskip("mcp_hub.plugins.skills")
    kopf, rumpf = skills.front_matter(datei.read_text(encoding="utf-8"))
    assert kopf.get("name") == "mail" and kopf.get("fuer") == ["agent"]  # ein Doppelpunkt im Text bräche den Kopf
    assert len(kopf.get("description", "")) > 40 and kopf.get("ausloeser")
    eintrag = skills.eintrag_lesen(datei.parent, "mail")
    assert eintrag["name"] == "mail" and eintrag["beschreibung"] and rumpf.startswith("# Mail")


def test_skill_nennt_nur_vorhandene_werkzeuge_und_alle():
    text = (PAKET / "skills" / "mail" / "SKILL.md").read_text(encoding="utf-8")
    genannt = set(re.findall(r"`mail\.(\w+)`", text))
    vorhanden = {t.name for t in MailPlugin().describe_tools()}
    assert genannt == vorhanden == {w.split(".")[1] for w in WERKZEUGE}
    # die drei Zustände, der Fremdinhalt und die Grenze „nur lesen“ stehen im Skill
    for wort in ("quelle_ok", "konten_fehler", "eingerichtet: false", "hinweis", "Fremdinhalt", "liest nur"):
        assert wort in text, f"der Skill nennt {wort!r} nicht"


# ── Beispiel-Konfiguration, README ───────────────────────────────────────────────────────────────────────
def test_beispiel_konfiguration_ist_gueltig_und_das_plugin_liest_sie(monkeypatch):
    daten = yaml.safe_load((WURZEL / "hub-config.beispiel.yaml").read_text(encoding="utf-8"))
    mail_eintrag, post_eintrag = daten["plugins"]
    eintrag = mail_eintrag
    assert eintrag["name"] == "mail" and post_eintrag["name"] == "post"
    assert [s["env"] for s in eintrag["secrets"]] == ["FYOS_MAIL_PRIVAT_PW", "FYOS_MAIL_VEREIN_PW"]
    assert "passwort" not in {k for konto in eintrag["config"]["konten"] for k in konto}  # Passwörter stehen nie in der Liste
    # Der Hub liest die Datei mit seinem eigenen Leser genauso.
    hub = load_config(WURZEL / "hub-config.beispiel.yaml")
    assert [(p.name, [s.env for s in p.secrets]) for p in hub.plugins] == [
        ("mail", ["FYOS_MAIL_PRIVAT_PW", "FYOS_MAIL_VEREIN_PW"]),
        ("post", ["FYOS_POST_TOKEN_HELFER", "FYOS_POST_TOKEN_DIENST"])]
    monkeypatch.setenv("FYOS_MAIL_PRIVAT_PW", "ein-app-passwort-eins")
    monkeypatch.setenv("FYOS_MAIL_VEREIN_PW", "ein-app-passwort-zwei")
    p = MailPlugin(next(e for e in hub.plugins if e.name == "mail").config)  # liest nur; fragt kein Netz
    assert p._unbrauchbar() == []  # kein Konfigurationsfehler
    assert [(k.name, k.host, k.port, k.sicherheit, k.nutzer, k.ordner, k.passwort) for k in p._post.konten] == [
        ("Privat", "imap.example.com", 993, "imaps", "alex@example.com", ("INBOX",), "ein-app-passwort-eins"),
        ("Verein", "mail.example.org", 143, "starttls", "alex", ("INBOX", "Anfragen"), "ein-app-passwort-zwei"),
    ]
    assert p.health()["status"] == "healthy"


def test_beispiel_konfiguration_fuer_post_wird_gelesen(monkeypatch):
    hub = load_config(WURZEL / "hub-config.beispiel.yaml")
    eintrag = next(p for p in hub.plugins if p.name == "post")
    assert not [k for k in eintrag.config if "token" in k.lower()]  # Token stehen nie in der Konfiguration
    p = PostPlugin(eintrag.config)
    for zugang, token in (("HELFER", "h-token-0123456789"), ("DIENST", "d-token-0123456789")):
        monkeypatch.setenv(f"FYOS_POST_TOKEN_{zugang}", token)
    assert p._basis() == "http://listmonk:9000"  # noqa: SLF001
    assert p._link("/admin") == "https://post.example.org/admin"  # noqa: SLF001
    assert p._standard_empfaenger() == "team@example.org"  # noqa: SLF001
    assert p._grenze() == 30  # noqa: SLF001
    zuordnung = p._zugaenge()  # noqa: SLF001
    assert zuordnung["code"] == ("helfer", "Code-Helfer") and zuordnung["*"] == ("dienst", "Dienst")
    assert p._token("helfer") == "h-token-0123456789"  # noqa: SLF001
    # der Hub merkt sich die Werkzeuge, sobald das Plugin aus dieser Konfiguration gebaut ist
    assert sorted(t.name for t in p.describe_tools()) == sorted(w.split(".")[1] for w in POST_WERKZEUGE)


def test_beispiel_konfiguration_ohne_geheimnis_meldet_den_fehler_statt_zu_scheitern():
    p = MailPlugin(next(e for e in load_config(WURZEL / "hub-config.beispiel.yaml").plugins if e.name == "mail").config)
    # beide Passwörter fehlen: die Einträge sind da, aber unbrauchbar, und der Fehler nennt die Umgebungsvariable
    assert [(k.name, "FYOS_MAIL_" + k.name.upper() + "_PW" in k.fehler) for k in p._unbrauchbar()] == [
        ("Privat", True), ("Verein", True)]
    stand = p.health()
    assert stand["status"] == "unhealthy" and stand["konten"] == 0 and "Privat, Verein" in stand["reason"]


@pytest.mark.parametrize("datei", ["README.md", "README.en.md"])
def test_readme_nennt_jede_einstellung_und_jedes_werkzeug(datei):
    text = (WURZEL / datei).read_text(encoding="utf-8")
    quelltext = "\n".join(p.read_text(encoding="utf-8") for p in sorted(PAKET.glob("*.py")))
    variablen = set(re.findall(r"FYOS_MAIL_[A-Z]+", quelltext))
    assert variablen >= {"FYOS_MAIL_HOST", "FYOS_MAIL_PORT", "FYOS_MAIL_NUTZER", "FYOS_MAIL_PASSWORT", "FYOS_MAIL_NAME",
                         "FYOS_MAIL_ORDNER", "FYOS_MAIL_SICHERHEIT", "FYOS_MAIL_KONTEN"}
    for name in variablen:
        assert name in text, f"{datei} nennt {name} nicht"
    for werkzeug in WERKZEUGE:
        assert werkzeug in text
    for wort in ("FYOS_POSTFACH_URL", "FYOS_POSTFACH_KOPF", "mcp_hub.postfach", "Apache-2.0", "passwort_env",
                 "BODY.PEEK", "EXAMINE", "STARTTLS"):
        assert wort in text, f"{datei} nennt {wort!r} nicht"


@pytest.mark.parametrize("datei", ["README.md", "README.en.md"])
def test_readme_nennt_jede_post_einstellung_jedes_werkzeug_und_das_kommando(datei):
    text = (WURZEL / datei).read_text(encoding="utf-8")
    quelltext = "\n".join((PAKET / n).read_text(encoding="utf-8") for n in ("post.py", "post_einrichten.py"))
    variablen = set(re.findall(r"\bFYOS_POST_[A-Z]+\b", quelltext)) | {"POST_URL", "FYOS_AGENT_NAME"}
    assert variablen >= {"FYOS_POST_URL", "FYOS_POST_LINK", "FYOS_POST_AN", "FYOS_POST_ZUGAENGE", "FYOS_POST_TAGESGRENZE",
                         "FYOS_POST_ZEITZONE"}
    # FYOS_POST_TOKEN und FYOS_POST_ADMIN sind nur die Vorsätze von FYOS_POST_TOKEN_<ZUGANG> und FYOS_POST_ADMIN_*; die nennt die Liste unten
    for name in variablen - {"FYOS_POST_ADMIN", "FYOS_POST_TOKEN"}:
        assert name in text, f"{datei} nennt {name} nicht"
    for wort in ("FYOS_POST_TOKEN_<ZUGANG>", "POST_TOKEN_<ZUGANG>", "FYOS_POST_ADMIN_NUTZER", "FYOS_POST_ADMIN_PASSWORT",
                 "fyos-post-einrichten", "--token-datei", "--token-neu", "--abonnent", "campaigns:send", "listmonk"):
        assert wort in text, f"{datei} nennt {wort!r} nicht"
    for werkzeug in POST_WERKZEUGE:
        assert werkzeug in text, f"{datei} nennt {werkzeug} nicht"
    assert "Europe/Berlin" in text  # die Vorgabe der Zeitzone steht im Text


def test_readme_verweisen_auf_einander_und_vorhandene_dateien():
    for datei, andere in (("README.md", "README.en.md"), ("README.en.md", "README.md")):
        text = (WURZEL / datei).read_text(encoding="utf-8")
        assert f"({andere})" in text
        for ziel in re.findall(r"\]\(([\w./-]+)\)", text):
            assert (WURZEL / ziel).exists(), f"{datei}: {ziel} fehlt"


# ── Katalog für den Installer, Kommando ──────────────────────────────────────────────────────────────────
def test_katalog_entry_point_und_kommando_stehen_in_der_pyproject():
    daten = pyproject()["project"]
    assert daten["entry-points"]["fyos.katalog"] == {"mail": "fyos_plugin_mail:katalog_pfad"}
    assert daten["scripts"] == {"fyos-post-einrichten": "fyos_plugin_mail.post_einrichten:main"}
    modul, funktion = daten["scripts"]["fyos-post-einrichten"].split(":")
    assert callable(getattr(importlib.import_module(modul), funktion))


def test_katalog_pfad_zeigt_auf_die_datei_im_paket_und_das_bild_liegt_daneben():
    pfad = katalog_pfad()
    assert pfad == PAKET / "fyos-katalog.yaml" and pfad.is_file()
    daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    (baustein,) = daten["bausteine"]
    assert baustein["id"] == "post" and baustein["art"] == "plugin" and baustein["quelle"]["paket"] == "fyos-plugin-mail"
    for bild in baustein["bilder"]:
        assert (PAKET / "bilder" / bild).is_file()
        assert (PAKET / "bilder" / bild).read_text(encoding="utf-8").lstrip().startswith("<svg")


def test_katalog_felder_gehoeren_zu_den_einstellungen_des_plugins():
    from fyos_plugin_mail import post

    daten = yaml.safe_load(katalog_pfad().read_text(encoding="utf-8"))
    felder = {f["umgebung"]: f for f in daten["bausteine"][0]["konfiguration"]}
    quelltext = (PAKET / "post.py").read_text(encoding="utf-8")
    for umgebung in felder:
        assert umgebung in quelltext or umgebung.startswith("FYOS_POST_TOKEN_"), f"{umgebung} liest das Plugin nicht"
    assert felder["FYOS_POST_TOKEN_FYOS"]["typ"] == "geheim"
    assert felder["FYOS_POST_ZUGAENGE"]["standard"] == post.STANDARD_ZUGAENGE
    assert felder["FYOS_POST_TAGESGRENZE"]["standard"] == post.TAGESGRENZE
    assert not [f for f in felder.values() if f["typ"] != "geheim" and "TOKEN" in f["umgebung"]]  # Token sind immer „geheim“


def test_katalog_besteht_den_parser_des_installers():
    katalog = pytest.importorskip("fyos_installer.katalog")
    bausteine = katalog.parse(yaml.safe_load(katalog_pfad().read_text(encoding="utf-8")), "fyos-katalog.yaml", katalog_pfad().parent / "bilder")
    (b,) = bausteine
    assert b.id == "post" and [f.umgebung for f in b.konfiguration][0] == "FYOS_POST_URL"
    assert not [f for f in b.konfiguration if f.typ == "geheim" and f.standard]
