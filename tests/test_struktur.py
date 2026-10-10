"""Der MIME-Aufbau aus BODYSTRUCTURE: Baum, Dateinamen (RFC 2231 und 2047), Größen, Auswahl von Text und Anhängen."""

from __future__ import annotations

import base64

import pytest

from fyos_plugin_mail import struktur
from fyos_plugin_mail.antwort import AntwortFehler, werte
from fyos_plugin_mail.struktur import Teil, anhang_beschreibung, auswerten, baum, dateiname, groesse_dekodiert


def bs(roh: bytes):
    """Eine BODYSTRUCTURE-Zeile, wie sie der Server schickt, als Werte (Zeichenketten als bytes, Atome als str)."""
    (wert,) = werte(roh)
    return wert


TEXT = b'("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 12 1 NIL NIL NIL NIL)'
HTML = b'("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 400 9 NIL NIL NIL NIL)'
PDF = (b'("APPLICATION" "PDF" ("NAME" "rechnung.pdf") NIL NIL "BASE64" 7800 NIL '
       b'("ATTACHMENT" ("FILENAME" "rechnung.pdf")) NIL NIL)')
LOGO = (b'("IMAGE" "PNG" ("NAME" "logo.png") "<logo@example.org>" NIL "BASE64" 500 NIL '
        b'("INLINE" ("FILENAME" "logo.png")) NIL NIL)')


def mehr(*teile: bytes, art: bytes = b"MIXED") -> bytes:
    return b"(" + b"".join(teile) + b' "' + art + b'" ("BOUNDARY" "grenze") NIL NIL NIL)'


# ── Baum ────────────────────────────────────────────────────────────────────────────────────────────────
def test_ein_einzelner_teil_hat_die_nummer_1():
    wurzel = baum(bs(TEXT))
    assert (wurzel.nummer, wurzel.typ, wurzel.kodierung, wurzel.groesse) == ("1", "text/plain", "7bit", 12)
    assert wurzel.parameter == {"charset": "utf-8"} and not wurzel.mehrteilig and wurzel.hauptart == "text"
    assert wurzel.disposition == "" and wurzel.kinder == [] and wurzel.content_id == ""


def test_mehrteilige_mail_nummeriert_wie_der_server():
    wurzel = baum(bs(mehr(mehr(TEXT, HTML, art=b"ALTERNATIVE"), PDF)))
    assert wurzel.typ == "multipart/mixed" and wurzel.mehrteilig and wurzel.hauptart == "multipart"
    assert wurzel.parameter == {"boundary": "grenze"}
    alternativ, pdf = wurzel.kinder
    assert (alternativ.nummer, alternativ.typ) == ("1", "multipart/alternative")
    assert [(k.nummer, k.typ) for k in alternativ.kinder] == [("1.1", "text/plain"), ("1.2", "text/html")]
    assert (pdf.nummer, pdf.typ, pdf.disposition, pdf.disposition_parameter) == ("2", "application/pdf", "attachment", {"filename": "rechnung.pdf"})


def test_parameter_haben_kleine_namen_und_behalten_die_werte():
    teil = baum(bs(b'("TEXT" "PLAIN" ("CHARSET" "ISO-8859-1" "Format" "flowed") NIL NIL "8BIT" 5 1 NIL NIL NIL NIL)'))
    assert teil.parameter == {"charset": "ISO-8859-1", "format": "flowed"} and teil.kodierung == "8bit"


def test_disposition_und_content_id_von_bildern():
    logo = baum(bs(LOGO))
    assert (logo.disposition, logo.content_id, logo.typ, logo.groesse) == ("inline", "<logo@example.org>", "image/png", 500)


def test_message_rfc822_hat_die_disposition_hinter_dem_umschlag():
    roh = (b'("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 2000 ("Fri, 10 Oct 2026 12:00:00 +0000" "Betreff" NIL NIL NIL NIL NIL NIL NIL NIL) '
           + TEXT + b' 40 NIL ("ATTACHMENT" ("FILENAME" "weitergeleitet.eml")) NIL NIL)')
    teil = baum(bs(roh))
    assert (teil.typ, teil.disposition, teil.disposition_parameter, teil.groesse) == ("message/rfc822", "attachment", {"filename": "weitergeleitet.eml"}, 2000)


def test_mehrteilig_ohne_erweiterungsdaten_und_ohne_untertyp():
    assert baum(bs(b"(" + TEXT + b' "MIXED")')).typ == "multipart/mixed"
    assert baum(bs(b"(" + TEXT + b" " + HTML + b")")).typ == "multipart/mixed"  # der Server nennt keinen Untertyp
    assert baum(bs(b"(" + TEXT + b' "Alternative")')).typ == "multipart/alternative"


def test_fehlende_und_leere_felder_stuerzen_nicht():
    assert baum(bs(b'("TEXT" "PLAIN")')).typ == "text/plain"
    assert baum(bs(b'("TEXT")')).typ == "text"
    assert baum(bs(b'("APPLICATION" "OCTET-STREAM" NIL NIL NIL "BASE64" NIL NIL NIL NIL NIL)')).groesse == 0
    teil = baum(bs(b'("TEXT" "PLAIN" NIL NIL NIL NIL "x" NIL NIL "kaputt" NIL NIL)'))  # Größe keine Zahl, Disposition keine Liste
    assert teil.groesse == 0 and teil.disposition == ""


@pytest.mark.parametrize("roh", [None, [], "TEXT", b"TEXT", 5])
def test_unbrauchbare_antworten(roh):
    with pytest.raises(AntwortFehler, match="BODYSTRUCTURE"):
        baum(roh)


def test_zu_viele_teile_sind_ein_fehler():
    kinder = [bs(TEXT)] * (struktur.TEILE_MAX + 1)
    with pytest.raises(AntwortFehler, match="zu viele Teile"):
        baum([*kinder, b"mixed"])
    baum([*kinder[:50], b"mixed"])


def test_zu_tief_verschachtelt_ist_ein_fehler():
    roh = bs(TEXT)
    for _ in range(struktur.TIEFE_MAX + 1):
        roh = [roh, b"mixed"]
    with pytest.raises(AntwortFehler, match="zu tief"):
        baum(roh)
    roh = bs(TEXT)
    for _ in range(struktur.TIEFE_MAX):
        roh = [roh, b"mixed"]
    assert baum(roh).mehrteilig


# ── Dateinamen ──────────────────────────────────────────────────────────────────────────────────────────
def mit_name(**parameter: str) -> Teil:
    return Teil(nummer="2", typ="application/pdf", disposition="attachment", disposition_parameter=dict(parameter))


def b64wort(text: str, zeichensatz: str = "utf-8") -> str:
    return f"=?{zeichensatz}?B?{base64.b64encode(text.encode(zeichensatz)).decode()}?="


def test_einfacher_dateiname():
    assert dateiname(mit_name(filename="rechnung.pdf")) == "rechnung.pdf"


def test_name_im_inhaltstyp_gilt_ohne_disposition():
    assert dateiname(Teil(nummer="2", typ="image/png", parameter={"name": "bild.png"})) == "bild.png"


def test_die_disposition_geht_dem_inhaltstyp_vor():
    teil = Teil(nummer="2", typ="image/png", parameter={"name": "alt.png"}, disposition="attachment",
                disposition_parameter={"filename": "neu.png"})
    assert dateiname(teil) == "neu.png"


def test_rfc_2231_mit_zeichensatz_hat_keine_anfuehrungszeichen():
    # Regression: decode_params setzt den Wert in Anführungszeichen, und sie standen im Dateinamen
    assert dateiname(mit_name(**{"filename*": "utf-8''%C3%9Cbersicht.pdf"})) == "Übersicht.pdf"
    assert dateiname(Teil(nummer="2", typ="application/pdf", parameter={"name*": "utf-8''%C3%9Cbersicht.pdf"})) == "Übersicht.pdf"


def test_rfc_2231_in_stuecken():
    teil = mit_name(**{"filename*0*": "utf-8''%C3%9Cber", "filename*1*": "sicht", "filename*2": ".pdf"})
    assert dateiname(teil) == "Übersicht.pdf"


def test_rfc_2231_mit_latin1_und_sprache():
    assert dateiname(mit_name(**{"filename*": "iso-8859-1'de'%DCbersicht.pdf"})) == "Übersicht.pdf"


def test_rfc_2231_mit_unbekanntem_zeichensatz_bleibt_lesbar():
    assert dateiname(mit_name(**{"filename*": "gibt-es-nicht''a%20b.txt"})) == "a b.txt"


@pytest.mark.parametrize(("roh", "erwartet"), [
    (b64wort("Übersicht.pdf"), "Übersicht.pdf"),
    ("=?iso-8859-1?Q?=DCbersicht.pdf?=", "Übersicht.pdf"),
    ("=?utf-8?Q?Rechnung_Oktober.pdf?=", "Rechnung Oktober.pdf"),
    ("=?gibt-es-nicht?B?YWJj?=", "=?gibt-es-nicht?B?YWJj?="),  # kein Absturz, der Name bleibt wie er war
    ("=?utf-8?B?!!!?=", "=?utf-8?B?!!!?="),
    ("a=?b.txt", "a=?b.txt"),
])
def test_kodierte_woerter_nach_rfc_2047(roh, erwartet):
    assert dateiname(mit_name(filename=roh)) == erwartet


@pytest.mark.parametrize(("roh", "erwartet"), [
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\alex\\Rechnung.pdf", "Rechnung.pdf"),
    ("/tmp/", ""),
    ("a/b\\c.pdf", "c.pdf"),
    ("a\r\nb.pdf", "ab.pdf"),
    ("tab\there.pdf", "tabhere.pdf"),
    ("   leer.pdf  ", "leer.pdf"),
    ("nul\x00l.pdf", "null.pdf"),
])
def test_pfade_und_steuerzeichen_fallen_weg(roh, erwartet):
    assert dateiname(mit_name(filename=roh)) == erwartet


def test_ein_name_ist_hoechstens_200_zeichen_lang():
    assert len(dateiname(mit_name(filename="x" * 500 + ".pdf"))) == struktur.NAME_MAX


def test_ohne_namen():
    assert dateiname(Teil(nummer="1", typ="text/plain")) == ""
    assert dateiname(mit_name(filename="")) == ""
    assert dateiname(mit_name(filename="/")) == ""
    assert dateiname(mit_name(charset="utf-8")) == ""


def test_ein_leerer_name_in_der_disposition_laesst_den_inhaltstyp_gelten():
    teil = Teil(nummer="2", typ="image/png", parameter={"name": "bild.png"}, disposition="attachment",
                disposition_parameter={"filename": ""})
    assert dateiname(teil) == "bild.png"


# ── Größen ──────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("kodierung", "groesse", "erwartet"), [
    ("7bit", 1234, 1234),
    ("quoted-printable", 1234, 1234),
    ("", 0, 0),
    ("base64", 7800, 5700),  # 100 Zeilen zu 78 Zeichen: 7600 Zeichen Nutzlast, drei Viertel davon
    ("base64", 78, 57),
    ("base64", 4, 1),
    ("base64", 1, 0),
    ("base64", 0, 0),
])
def test_groesse_nach_dem_entkodieren(kodierung, groesse, erwartet):
    assert groesse_dekodiert(Teil(nummer="1", typ="application/pdf", kodierung=kodierung, groesse=groesse)) == erwartet


def test_groesse_von_base64_stimmt_auf_wenige_bytes():
    # echte Kodierung mit Zeilenumbruch nach 76 Zeichen, wie sie Mailprogramme schreiben
    import binascii

    for laenge in (1, 57, 58, 1000, 4096, 100_000):
        roh = bytes(range(256)) * (laenge // 256 + 1)
        roh = roh[:laenge]
        kodiert = b"".join(binascii.b2a_base64(roh[i:i + 57]).rstrip(b"\n") + b"\r\n" for i in range(0, laenge, 57))
        teil = Teil(nummer="1", typ="application/pdf", kodierung="base64", groesse=len(kodiert))
        # das Füllzeichen am Ende ist nicht bekannt: die Schätzung ist nie kleiner und höchstens zwei Bytes größer
        assert 0 <= groesse_dekodiert(teil) - laenge <= 2, (laenge, groesse_dekodiert(teil))


# ── Auswahl ─────────────────────────────────────────────────────────────────────────────────────────────
def nummern(teile: list[Teil]) -> list[str]:
    return [t.nummer for t in teile]


def test_einfache_textmail():
    a = auswerten(baum(bs(TEXT)))
    assert (nummern(a.text), nummern(a.html), nummern(a.anhaenge)) == (["1"], [], [])


def test_nur_html():
    a = auswerten(baum(bs(HTML)))
    assert (nummern(a.text), nummern(a.html), nummern(a.anhaenge)) == ([], ["1"], [])


def test_alternativ_liefert_klartext_und_html():
    a = auswerten(baum(bs(mehr(TEXT, HTML, art=b"ALTERNATIVE"))))
    assert (nummern(a.text), nummern(a.html), nummern(a.anhaenge)) == (["1"], ["2"], [])


def test_alternativ_in_beliebiger_reihenfolge():
    a = auswerten(baum(bs(mehr(HTML, TEXT, art=b"ALTERNATIVE"))))
    assert (nummern(a.text), nummern(a.html)) == (["2"], ["1"])


def test_alternativ_nimmt_je_art_die_erste_fassung():
    zweite = mehr(TEXT, HTML, art=b"ALTERNATIVE")
    a = auswerten(baum(bs(mehr(TEXT, HTML, zweite, art=b"ALTERNATIVE"))))
    assert (nummern(a.text), nummern(a.html)) == (["1"], ["2"])


def test_alternativ_ueberspringt_fassungen_ohne_text():
    a = auswerten(baum(bs(mehr(PDF, TEXT, art=b"ALTERNATIVE"))))
    assert nummern(a.text) == ["2"] and nummern(a.anhaenge) == ["1"]


def test_gemischt_mit_anhang():
    a = auswerten(baum(bs(mehr(mehr(TEXT, HTML, art=b"ALTERNATIVE"), PDF))))
    assert (nummern(a.text), nummern(a.html), nummern(a.anhaenge)) == (["1.1"], ["1.2"], ["2"])


def test_mehrere_textteile_bleiben_in_der_reihenfolge_der_mail():
    a = auswerten(baum(bs(mehr(TEXT, PDF, TEXT, HTML))))
    assert (nummern(a.text), nummern(a.html), nummern(a.anhaenge)) == (["1", "3"], ["4"], ["2"])


def test_ein_textteil_als_anhang_ist_kein_text():
    roh = b'("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 12 1 NIL ("ATTACHMENT" ("FILENAME" "notiz.txt")) NIL NIL)'
    a = auswerten(baum(bs(mehr(TEXT, roh))))
    assert (nummern(a.text), nummern(a.anhaenge)) == (["1"], ["2"])


def test_ein_textteil_mit_dateinamen_ist_ein_anhang():
    roh = b'("TEXT" "HTML" ("CHARSET" "utf-8" "NAME" "seite.html") NIL NIL "7BIT" 12 1 NIL ("INLINE" ("FILENAME" "seite.html")) NIL NIL)'
    a = auswerten(baum(bs(mehr(TEXT, roh))))
    assert (nummern(a.text), nummern(a.html), nummern(a.anhaenge)) == (["1"], [], ["2"])


def test_bild_zum_html_ist_kein_anhang_aber_ein_bild_in_gemischt_schon():
    verwandt = mehr(HTML, LOGO, art=b"RELATED")
    a = auswerten(baum(bs(mehr(verwandt))))
    assert (nummern(a.html), nummern(a.anhaenge)) == (["1.1"], [])
    a = auswerten(baum(bs(mehr(HTML, LOGO))))
    assert (nummern(a.html), nummern(a.anhaenge)) == (["1"], ["2"])


def test_ein_ausdruecklicher_anhang_in_related_bleibt_ein_anhang():
    roh = (b'("IMAGE" "PNG" ("NAME" "foto.png") "<foto@example.org>" NIL "BASE64" 500 NIL '
           b'("ATTACHMENT" ("FILENAME" "foto.png")) NIL NIL)')
    a = auswerten(baum(bs(mehr(HTML, roh, art=b"RELATED"))))
    assert nummern(a.anhaenge) == ["2"]


def test_signaturen_sind_keine_anhaenge():
    signatur = b'("APPLICATION" "PGP-SIGNATURE" ("NAME" "signature.asc") NIL NIL "7BIT" 800 NIL NIL NIL NIL)'
    smime = b'("APPLICATION" "PKCS7-SIGNATURE" ("NAME" "smime.p7s") NIL NIL "BASE64" 3000 NIL ("ATTACHMENT" ("FILENAME" "smime.p7s")) NIL NIL)'
    a = auswerten(baum(bs(mehr(TEXT, signatur, art=b"SIGNED"))))
    assert (nummern(a.text), nummern(a.anhaenge)) == (["1"], [])
    # auch wenn das Programm die Signatur als Anhang auszeichnet, in S/MIME, ist es eine Anlage mit Namen: sie bleibt sichtbar
    a = auswerten(baum(bs(mehr(TEXT, smime, art=b"SIGNED"))))
    assert nummern(a.anhaenge) == ["2"]


def test_weitergeleitete_mail_ist_ein_anhang_mit_namen():
    roh = (b'("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 2000 ("Fri, 10 Oct 2026 12:00:00 +0000" "Betreff" NIL NIL NIL NIL NIL NIL NIL NIL) '
           + TEXT + b' 40 NIL ("INLINE" ("FILENAME" "weitergeleitet.eml")) NIL NIL)')
    a = auswerten(baum(bs(mehr(TEXT, roh))))
    assert nummern(a.anhaenge) == ["2"]
    assert anhang_beschreibung(a.anhaenge[0])["name"] == "weitergeleitet.eml"


def test_unbekannte_teile_sind_anhaenge():
    roh = b'("APPLICATION" "OCTET-STREAM" NIL NIL NIL "BASE64" 1000 NIL NIL NIL NIL)'
    a = auswerten(baum(bs(mehr(TEXT, roh))))
    assert nummern(a.anhaenge) == ["2"] and anhang_beschreibung(a.anhaenge[0])["name"] == struktur.OHNE_NAME


def test_beschreibung_eines_anhangs_hat_nur_angaben():
    a = auswerten(baum(bs(mehr(TEXT, PDF))))
    assert anhang_beschreibung(a.anhaenge[0]) == {"name": "rechnung.pdf", "typ": "application/pdf", "groesse": 5700}


def test_auswertung_eines_baums_aendert_ihn_nicht():
    wurzel = baum(bs(mehr(mehr(TEXT, HTML, art=b"ALTERNATIVE"), PDF)))
    erste = auswerten(wurzel)
    zweite = auswerten(wurzel)
    assert nummern(erste.text) == nummern(zweite.text) and nummern(erste.anhaenge) == nummern(zweite.anhaenge)
