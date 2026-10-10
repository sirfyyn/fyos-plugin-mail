"""Ordnernamen in IMAP-UTF-7 (RFC 3501, Abschnitt 5.1.3)."""

from __future__ import annotations

import random

import pytest

from fyos_plugin_mail import utf7


@pytest.mark.parametrize(("klar", "kodiert"), [
    ("INBOX", "INBOX"),
    ("", ""),
    ("Entwürfe", "Entw&APw-rfe"),
    ("Gesendet/Archiv 2026", "Gesendet/Archiv 2026"),
    ("Ä", "&AMQ-"),
    ("A&B", "A&-B"),
    ("&", "&-"),
    ("&&", "&-&-"),
    ("日本語", "&ZeVnLIqe-"),
    ("~peter/mail/&ZeVnLIqe-/&U,BTFw-", "~peter/mail/&-ZeVnLIqe-/&-U,BTFw-"),  # Beispiel aus RFC 3501: das & davor wird &-
    ("Köln & Düsseldorf", "K&APY-ln &- D&APw-sseldorf"),
    ("😀", "&2D3eAA-"),  # ein Zeichen außerhalb der Grundebene: Ersatzpaar
])
def test_kodieren_und_dekodieren(klar, kodiert):
    assert utf7.kodieren(klar) == kodiert
    assert utf7.dekodieren(kodiert) == klar


def test_das_beispiel_aus_rfc_3501_wird_gelesen():
    assert utf7.dekodieren("~peter/mail/&U,BTFw-/&ZeVnLIqe-") == "~peter/mail/台北/日本語"


def test_kodiert_ist_immer_ascii():
    for name in ("Entwürfe", "日本語", "😀 Mails", "ÄÖÜ äöü ß"):
        assert utf7.kodieren(name).isascii()


@pytest.mark.parametrize("text", ["&", "&abc", "a&b", "&!!!-", "&A-", "&AAAA", "&AA=-"])
def test_was_sich_nicht_lesen_laesst_bleibt_stehen(text):
    # kein Absturz und kein Verlust: ein unlesbarer Name erscheint, wie der Server ihn schrieb
    ergebnis = utf7.dekodieren(text)
    assert isinstance(ergebnis, str)
    if text in ("&", "&abc", "a&b"):  # ohne schließenden Strich ist es kein kodierter Abschnitt
        assert ergebnis == text


def test_ungerade_zahl_von_bytes_bleibt_unveraendert_stehen():
    assert utf7.dekodieren("&AMQA-") == "&AMQA-"  # drei Bytes ergeben kein UTF-16


def test_hin_und_zurueck_fuer_zufaellige_namen():
    zufall = random.Random(20261010)
    alphabet = "abcXYZ019 /._-&~äöüßÄÖÜ€日本語😀é́​"
    for _ in range(400):
        name = "".join(zufall.choice(alphabet) for _ in range(zufall.randint(0, 24)))
        assert utf7.dekodieren(utf7.kodieren(name)) == name, name
        assert utf7.kodieren(name).isascii()


def test_steuerzeichen_werden_kodiert_statt_durchgereicht():
    # Zeilenumbruch und Nullbyte stehen nie im Klartext in einem Ordnernamen, der zum Server geht
    kodiert = utf7.kodieren("a\r\nb\x00c")
    assert "\r" not in kodiert and "\n" not in kodiert and "\x00" not in kodiert
    assert utf7.dekodieren(kodiert) == "a\r\nb\x00c"
