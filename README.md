English: [README.en.md](README.en.md)

# fyos-plugin-mail

Mails für **FYOS — For Your Own Sake**: das Plugin liest Postfächer per IMAP (mit App-Passwort), zeigt den Posteingang, sucht
und liest einzelne Mails, und es liest **nur**: Senden, Antworten, Löschen, Verschieben und Kennzeichnen gibt es nicht, und
der Gelesen-Status bleibt, wie er war. Außerdem ist es die **Quelle der Mails** für alle anderen Plugins: Leitstand,
Tagesbrief und Startseite fragen im Kern (`mcp_hub.postfach`) und bekommen die Mails dieser Konten, ohne IMAP zu können
und ohne eine Adresse zu kennen.

Wer FYOS nachbaut, braucht dafür keinen eigenen Mail-Dienst: ein Postfach mit App-Passwort genügt.

Das Paket bringt ein zweites Plugin mit: **FYOS Post** (`post`) schickt kurze Mitteilungen an Abonnenten und legt
Kampagnen-Entwürfe an, über einen eigenen [Listmonk](https://listmonk.app) (siehe [FYOS Post](#fyos-post-listmonk)). Das
Plugin `mail` bleibt dabei, was es war: es liest nur.

## Einrichten

Im Installer den Baustein **Mail** wählen (Felder: Server, Port, Nutzer, App-Passwort). Von Hand: das Paket in die
Python-Umgebung des Hubs installieren (`pip install fyos-plugin-mail`, aus dem Paketindex oder aus dem Git-Repository)
und die Umgebungsvariablen setzen. Der Hub lädt das Plugin dann von selbst über die Entry-Point-Gruppe `mcp_hub.plugins`.
Die Postfach-Quelle im Kern gibt es ab `fyos-hub` 0.1.1; mit einem älteren Kern laufen die Werkzeuge `mail.*` trotzdem
(sie lesen die Konten selbst), nur die Anmeldung als Quelle für Leitstand und Tagesbrief entfällt.

### Ein Konto

| Variable | Bedeutung |
|---|---|
| `FYOS_MAIL_HOST` | IMAP-Server, nur der Name (`imap.example.com`), ohne `imaps://` |
| `FYOS_MAIL_PORT` | Port. Vorgabe 993 (IMAPS), mit `FYOS_MAIL_SICHERHEIT=starttls` 143 |
| `FYOS_MAIL_NUTZER` | Anmeldename, meist die Mail-Adresse |
| `FYOS_MAIL_PASSWORT` | **App-Passwort**, nie das Haupt-Passwort des Anbieters |
| `FYOS_MAIL_NAME` | Anzeigename des Kontos; ohne Angabe der Host (ohne `imap.` oder `mail.` davor) |
| `FYOS_MAIL_ORDNER` | Ordner, die Suchen ohne `in:` durchsuchen, durch Komma getrennt. Vorgabe `INBOX` |
| `FYOS_MAIL_SICHERHEIT` | `imaps` (von Anfang an verschlüsselt) oder `starttls`. Ohne Angabe `starttls` für Port 143, sonst `imaps`. Unverschlüsselt verbindet das Plugin nie |

Typische Server (den genauen nennt der Anbieter): Gmail `imap.gmail.com` (Bestätigung in zwei Schritten einschalten, dann
unter „App-Passwörter“ eines anlegen), iCloud `imap.mail.me.com` (App-spezifisches Passwort unter appleid.apple.com),
Fastmail `imap.fastmail.com` (App-Passwort mit Zugriff auf Mail), eigene Server mit Dovecot meist `mail.<domain>`.
Anbieter, die nur OAuth anbieten (zum Beispiel Microsoft 365 und Outlook.com), unterstützt das Plugin nicht.

### Mehrere Konten

`FYOS_MAIL_KONTEN` ist eine JSON-Liste (oder `konten:` in der `config:` des Plugins, siehe
[`hub-config.beispiel.yaml`](hub-config.beispiel.yaml)). Je Konto `name` (Pflicht), `host`, `port`, `nutzer`,
`passwort_env`, `ordner` (Liste) und `sicherheit`; `passwort_env` ist der **Name einer Umgebungsvariablen** mit dem
App-Passwort:

```sh
export FYOS_MAIL_KONTEN='[
  {"name": "Arbeit", "host": "imap.example.org", "nutzer": "bob@example.org", "passwort_env": "MAIL_ARBEIT_PW"},
  {"name": "Verein", "host": "mail.example.net", "port": 143, "sicherheit": "starttls", "nutzer": "bob",
   "passwort_env": "MAIL_VEREIN_PW", "ordner": ["INBOX", "Anfragen"]}
]'
export MAIL_ARBEIT_PW='…'
export MAIL_VEREIN_PW='…'
```

Ein Passwort im JSON selbst lehnt das Plugin ab: es stünde in Konfigurationsdateien und Sicherungen. Das Konto aus
`FYOS_MAIL_HOST` und die der Liste gelten zusammen (höchstens 20). Ein unbrauchbarer Eintrag bricht nichts ab; er erscheint
als Konto mit Fehler in `mail.konten`, im Zustand des Plugins (`hub.health`) und in jeder Antwort.

**Warum das Formular des Installers nur ein Konto trägt.** Der Installer füllt die Felder, die ein Baustein im Katalog
nennt: fest benannte Felder mit je einer Umgebungsvariablen. Eine wachsende Liste von Konten bräuchte ein Listenfeld im
Formular, das es dort nicht gibt, und Passwörter in einer Liste landeten in der erzeugten Konfiguration. Darum richtet das
Formular das erste Konto ein (`FYOS_MAIL_*`); weitere stehen in `FYOS_MAIL_KONTEN` oder in der Hub-Konfiguration, mit
Passwörtern nur als Umgebungsvariablen oder Geheimnisse.

## Nur lesen

Das Plugin spricht genau diese IMAP-Befehle: `CAPABILITY`, `STARTTLS`, `LOGIN` (bei Nicht-ASCII-Zeichen
`AUTHENTICATE PLAIN`), `LIST`, `STATUS`, `EXAMINE`, `UID SEARCH`, `UID FETCH` und `LOGOUT`.

* Ordner öffnet es mit `EXAMINE` (schreibgeschützt), nie mit `SELECT`.
* Inhalte holt es mit `BODY.PEEK[…]`, das `\Seen` nicht setzt; den Gelesen-Status liest es aus `FLAGS`, die Zahl der
  Ungelesenen aus `STATUS (UNSEEN)`.
* `STORE`, `COPY`, `MOVE`, `EXPUNGE`, `APPEND`, `CREATE`, `DELETE`, `RENAME` und `SUBSCRIBE` sendet das Paket nie, und die
  Sitzung weist `UID`-Befehle außer `SEARCH` und `FETCH` sowie jede `FETCH`-Angabe ohne `PEEK` ab, bevor etwas gesendet
  wird. Anhänge lädt das Plugin nie, nur ihre Namen, Typen und Größen aus dem Aufbau der Mail.
* Beweis: `tests/test_nur_lesen.py` lässt jede Fähigkeit gegen eine IMAP-Attrappe laufen, die jeden Befehl protokolliert.
  Ein verbotener Befehl, ein `FETCH` ohne `PEEK` oder ein geändertes Kennzeichen lässt den Test scheitern. Eine Gegenprobe
  zeigt, dass die Attrappe jeden schreibenden Befehl bemerken würde und dass ein Lesen ohne `PEEK` sichtbar `\Seen` setzt.
  Dazu prüft der Test den Quelltext: welche Methoden der Verbindung das Paket aufruft, dass (außer der Sendeseite FYOS Post,
  die nur HTTP spricht und von `mail` nicht eingebunden wird) nur `imap.py` mit dem Netz spricht und dass kein schreibender
  Befehl als Text vorkommt.

Was das Plugin nicht verhindern kann: Der Anbieter sieht die Anmeldung und das Lesen in seinen Protokollen, und manche
Anbieter vermerken „zuletzt abgerufen“. Mails verändern sich dadurch nicht.

## Werkzeuge

| Werkzeug | Wirkung | Zweck |
|---|---|---|
| `mail.konten` | lesend | welche Konten eingerichtet sind (Name, Server, Nutzer, Ordner; nie ein Passwort) und welches einen Fehler hat. `pruefen: true` meldet sich an jedem Konto an und zählt Ordner und Ungelesene |
| `mail.eingang` | lesend | die neuesten Mails im Posteingang (`anzahl` 1 bis 25, Vorgabe 10) mit einem Satz wie „12 ungelesen im Posteingang; unter den neuesten 10: 4 ungelesen, 2 von Menschen“. `konto` wählt ein Konto, `voll: true` liefert mehr Felder |
| `mail.suchen` | lesend | Mails suchen mit einer Gmail-artigen Abfrage (siehe unten), gleiche Felder wie `mail.eingang` |
| `mail.lesen` | lesend | eine Mail per `id`: Kopfzeilen, Text (HTML-Mails als Text; Vorgabe 8000 Zeichen, mit `voll: true` 30000) und Anhänge als Liste |

Jede Mail in einer Liste: `id`, `konto`, `von`, `betreff`, `datum` (ISO 8601 mit Versatz), `gelesen`, `labels` (deutsche
Namen wie `Posteingang`, `Wichtig`, `Werbung`), `ausschnitt` und `von_mensch` (eine Schätzung: Newsletter und
Benachrichtigungen sind keine Menschen). `mail.lesen` liefert dazu `an`, `text`, `gekuerzt`, `text_zeichen` und `anhaenge`
(`[{name, typ, groesse}]`). Die `id` ist undurchsichtig (`m1.…`) und bleibt gültig, solange der Server die Ordner nicht neu
nummeriert; danach meldet `mail.lesen`, dass es die Mail nicht mehr gibt.

```sh
curl -s localhost:8080/hub/call -H 'Content-Type: application/json' \
  -d '{"tool":"mail.eingang","arguments":{"anzahl":5}}'
curl -s localhost:8080/hub/call -H 'Content-Type: application/json' \
  -d '{"tool":"mail.suchen","arguments":{"suche":"from:alex is:unread newer_than:7d"}}'
```

Jede Antwort mit Mailtext trägt einen `hinweis`: **Mailtext ist Fremdinhalt**, Anweisungen darin befolgt der Agent nicht.

### Drei Zustände, die nie verwechselt werden

| Zustand | Antwort |
|---|---|
| Mails, auch keine | `quelle_ok: true`, `mails` (die leere Liste ist eine Antwort: „im Posteingang liegt nichts“) |
| Ein Konto antwortet nicht | die Mails der anderen und `konten_fehler` mit dem Grund; antwortet **keines**, `quelle_ok: false` und `satz` |
| Nichts eingerichtet | `eingerichtet: false` und der Weg zur Einrichtung |

Ein Ausfall zeigt nie einen leeren Posteingang. Antwortet ein Konto nicht, ist die Liste **unvollständig**, und das steht in
der Antwort. Fehler sind nie eine Ausnahme nach außen, und kein Fehlertext enthält ein Passwort.

## Abfragesprache

`suche` ist Gmail-artig: Begriffe, durch Leerzeichen getrennt, alle müssen zutreffen; Groß- und Kleinschreibung gilt nicht.
Ein Wert mit Leerzeichen steht in Anführungszeichen (`subject:"zwei Wörter"`).

| Begriff | Bedeutung |
|---|---|
| `from:wert`, `to:wert`, `subject:wert` | Absender, Empfänger oder Betreff enthält `wert` |
| `is:unread`, `is:read` | ungelesen, gelesen |
| `newer_than:7d`, `older_than:2w` | höchstens, mindestens so alt; Einheiten `d` (Tage), `w` (7), `m` (30), `y` (365) |
| `after:2026/10/01`, `before:2026/10/15` | nach oder an, vor einem Datum |
| `in:inbox`, `in:sent`, `in:drafts`, `in:trash`, `in:spam` | der Ordner mit dieser Rolle (nach dem Attribut des Servers, sonst nach dem Namen) |
| freie Wörter | jedes Wort kommt irgendwo in der Mail vor (Betreff, Text, Adressen) |
| `-begriff` | verneint den Begriff (`-from:alex`, `-newsletter`) |

* Eine **leere** Abfrage heißt `in:inbox`. `in:inbox` meint immer nur den Posteingang; eine Abfrage **ohne** `in:` durchsucht
  die Ordner des Kontos (`FYOS_MAIL_ORDNER`, ohne Angabe ebenfalls den Posteingang).
* Gefunden werden die **zuletzt eingegangenen** Mails, höchstens `anzahl`; bei sehr großen Postfächern sucht das Plugin in
  Fenstern der neuesten Nachrichten statt im ganzen Ordner. Die Zahl der Ungelesenen gilt immer für den Posteingang.
* Ein **Gmail-Konto** (erkannt an der Erweiterung `X-GM-EXT-1`) bekommt die Abfrage unverändert (`X-GM-RAW`): dort gilt die
  ganze Gmail-Suche mit `OR`, Klammern, `label:`, `category:` und `has:attachment`.
* Bei jedem anderen Konto werden `label:`, `category:`, `has:attachment`, `OR`, Gruppen in Klammern und alles, was das
  Plugin nicht kennt, **überlesen** und in `ignoriert` genannt, nie als Fehler zurückgewiesen. `OR` und Klammern werden nicht
  als „und“ gelesen (das fände weniger, als gemeint ist): die Begriffe daran fallen weg, und die Treffer können mehr sein als
  gemeint.
* Höchstens 12 Begriffe und 200 Zeichen je Wert; eine Abfrage über 300 Zeichen wird gekürzt.

## Quelle der Mails für andere Plugins

Beim Start meldet sich das Plugin im Kern als Ablage an (`mcp_hub.postfach.ablage_anmelden`), sobald mindestens ein Konto
eingetragen ist. Andere Plugins rufen `mcp_hub.postfach.quelle()` und bekommen `konten()`, `liste(abfrage, anzahl, konto)`
und `lesen(id)` im Mailformat des Kerns (`ERWEITERN.md` im Repository `fyos-os`, §2.15). Für diesen Weg gilt die
**strengere Regel**: antwortet ein Konto nicht, wirft die Ablage einen `QuelleFehler` mit dem Namen des Kontos, statt eine
unvollständige Liste zu liefern. Der Leitstand zeigt dann einen Fehler und keinen leeren Posteingang.

Die Ablage geht erst nach einem Mail-Dienst an die Reihe: ist `FYOS_POSTFACH_URL` gesetzt (die Ausbaustufe mit eigenem
Dienst, optional mit einer Kopfzeile in `FYOS_POSTFACH_KOPF`), liest der ganze Hub über ihn, und die IMAP-Konten dieses
Plugins ruhen. Wer einen eigenen Dienst betreibt, setzt nur diese Variablen.

## FYOS Post (Listmonk)

Das Plugin `post` ist die Sendeseite: ein Agent schickt eine kurze **Mitteilung** an Abonnenten (`post.senden`) und bereitet
einen **Kampagnen-Entwurf** vor (`post.kampagne_entwurf`), den ein Mensch in der Oberfläche von Listmonk prüft und startet.
Listmonk ist ein eigener Dienst, den du betreibst (Datenbank, SMTP-Zugang, Absenderadresse); das Plugin spricht nur dessen
API. Ohne `FYOS_POST_URL` ist das Plugin **aus**: die Werkzeuge antworten „nicht eingerichtet“ und nennen den Weg, es geht
nichts ins Netz.

| Werkzeug | Wirkung | Zweck |
|---|---|---|
| `post.zustand` | lesend | ob Listmonk antwortet, welche Listen, Vorlagen und Entwürfe es gibt, wie viele Sendungen heute schon gingen |
| `post.senden` | sendet | eine Mitteilung (`betreff`, `text`, optional `an`) über die Vorlage „Mitteilung“; höchstens 5 Empfänger, Betreff bis 150, Text bis 5000 Zeichen und 200 Zeilen. `trocken: true` sendet nichts |
| `post.kampagne_entwurf` | schreibt | legt einen **Entwurf** an (`name`, `betreff`, `listen`, `text`, optional `format` und `vorlage`); nie ein Versand |
| `post.kampagne_testen` | sendet | schickt den Test einer vorhandenen Kampagne an höchstens 3 Abonnenten (`id`, optional `an`) |

### Einstellungen

Jede Einstellung kommt aus der Umgebung des Hubs oder aus der `config:` des Plugins (siehe
[`hub-config.beispiel.yaml`](hub-config.beispiel.yaml)); steht beides da, gewinnt die `config:`.

| Variable (Schlüssel in `config:`) | Bedeutung | Vorgabe |
|---|---|---|
| `FYOS_POST_URL` (`url`), Alias `POST_URL` | Adresse von Listmonk, `http://` oder `https://`, ohne Pfad zur API | keine: ohne sie ist das Plugin aus |
| `FYOS_POST_LINK` (`link`) | Adresse der Listmonk-Oberfläche, wie ein Mensch sie öffnet; nur damit nennen Antworten Links zu Entwürfen | keine: dann gibt es keine Links |
| `FYOS_POST_AN` (`an`) | Standardempfänger von `post.senden` und `post.kampagne_testen`, eine einfache Adresse eines vorhandenen Abonnenten | keine: dann ist `an` Pflicht |
| `FYOS_POST_ZUGAENGE` (`zugaenge`) | welche Fläche mit welchem API-Zugang sendet, siehe unten | `*=fyos` |
| `FYOS_POST_TOKEN_<ZUGANG>`, Alias `POST_TOKEN_<ZUGANG>` | das API-Token des Zugangs (Name in Großbuchstaben, `-` wird `_`); nur als Umgebungsvariable oder Geheimnis | keine |
| `FYOS_POST_TAGESGRENZE` (`tagesgrenze`) | höchstens so viele Sendungen je UTC-Tag, über alle Zugänge; jeder Empfänger zählt; `0` sperrt das Senden | 30 |
| `FYOS_POST_ZEITZONE` (`zeitzone`) | Zeitzone der Zeitangabe in der Fußzeile der Mitteilung (IANA-Name); unbekannt oder ohne Zeitzonendaten: UTC, und die Mail sagt es | `Europe/Berlin` |

Wer eine Mitteilung schickt, steht in deren Fußzeile (Name, Zeit, Kennung). `FYOS_AGENT_NAME` gibt dem Agenten einen Namen
dort, wo `FYOS_POST_ZUGAENGE` keinen nennt.

**Zugänge.** `FYOS_POST_ZUGAENGE` ist eine Liste `Fläche=Zugang[/Anzeigename]`, durch Komma getrennt. Flächen sind `os`, `chat`,
`code`, `agent`, `dienste` und `*` (alle übrigen). Beispiel: `code=helfer/Code-Helfer,chat=helfer/Chat-Helfer,*=dienst/Dienst`
heißt: Code- und Chat-Aufrufer senden mit dem Zugang `helfer` (Token `FYOS_POST_TOKEN_HELFER`), alle anderen mit `dienst`.
Ein Zugang ist ein API-Benutzer in Listmonk mit der Rolle `arbeit`, die **keine** Kampagne starten darf. Jede Fläche hat
höchstens einen Eintrag, der Zugangsname besteht aus `a-z`, `0-9`, `_` und `-` (bis 32 Zeichen). Ein ungültiger Eintrag ist
ein klarer Fehler in `post.zustand` und im Zustand des Plugins, nie ein stiller Ausweichzugang; eine Fläche ohne Eintrag und
ohne `*` hat keinen Zugang. Das Token eines Zugangs wird nie durch das eines anderen ersetzt.

### Einrichten in fünf Schritten

1. **Listmonk betreiben** (eigener Dienst mit Datenbank) und in dessen Einstellungen den SMTP-Zugang und die Absenderadresse
   eintragen; einen Administrator anlegen. Das Plugin ändert daran nichts.
2. **Das Paket installieren** (es bringt `post` und `mail` mit) und `FYOS_POST_URL` setzen.
3. **Listmonk vorbereiten:** `FYOS_POST_ADMIN_NUTZER` und `FYOS_POST_ADMIN_PASSWORT` (nur für diesen Lauf) setzen und
   `fyos-post-einrichten` aufrufen. Ohne `--ja` ist das ein Trockenlauf, der nur liest. Mit
   `fyos-post-einrichten --ja --token-datei ./token.env --abonnent dein@example.org` legt es die Vorlage „Mitteilung“, die
   Rolle `arbeit`, je Zugang einen API-Benutzer und die private Liste „Intern“ mit deiner Adresse als bestätigtem Abonnenten an.
   Der Lauf ist wiederholbar und ändert Vorhandenes nur, wenn es abweicht. Die Token stehen **nur** in der Datei (Rechte 0600,
   wird nie überschrieben), nie in der Ausgabe. Ein verlorenes Token lässt sich nicht nachlesen: `--token-neu ZUGANG`
   legt den Zugang neu an.
4. **Die Token in die Umgebung des Hubs übernehmen** (`FYOS_POST_TOKEN_<ZUGANG>`), die Datei löschen, `FYOS_POST_AN` und
   `FYOS_POST_LINK` setzen, den Hub neu starten.
5. **Prüfen:** `post.zustand` zeigt Listen, Vorlagen und den Zähler; `hub.health` prüft jeden Zugang mit seinem Token.

### Was zugesichert ist, und was nicht

* **„Angenommen“ heißt nicht „zugestellt“.** Listmonk nimmt die Mail an und reicht sie an den SMTP-Zugang weiter; ob sie im
  Postfach ankommt, hängt von diesem Zugang und vom Empfänger ab. Die Antworten sagen „angenommen“.
* **Nur an Abonnenten.** Mitteilungen gehen im Modus `default` von Listmonk und damit nur an vorhandene Abonnenten; ein
  fremder Empfänger wird mit einem klaren Fehler abgewiesen. Es gibt keinen Weg, Betreff, Absender oder Kopfzeilen zu setzen:
  alles Text steht nur in den Daten der Vorlage und bleibt Text.
* **Kampagnen startet nur ein Mensch.** Das Plugin legt Entwürfe an und verschickt Tests; es ruft nie `/status` auf und kennt
  nur die Methoden GET und POST. Die Rolle `arbeit` hat das Recht `campaigns:send` nicht.
* **Tagesgrenze.** Der Zähler liegt im Zähler des Hubs (Redis, sonst im Speicher des Prozesses) und bucht vor dem Senden;
  gescheiterte Sendungen werden zurückgenommen. Ist der Zähler nicht erreichbar, wird nicht gesendet.
* **Argumente gelten als vertraulich** (Betreff, Text, Empfänger): sie stehen nie im Klartext auf dem Ereignisbus. Ein Token
  steht nie in einer Antwort oder einem Log. Umleitungen folgt das Plugin nicht, und es nutzt keinen Proxy.

## Sicherheit

* **Nur verschlüsselt.** IMAPS (von Anfang an verschlüsselt) oder STARTTLS, vor jeder Anmeldung. Bietet der Server STARTTLS
  nicht an oder lehnt er es ab, scheitert die Verbindung, bevor ein Passwort gesendet wird. Mindestens TLS 1.2. Eine
  Einstellung für „unverschlüsselt“ gibt es nicht, und `FYOS_MAIL_SICHERHEIT=none` wird abgelehnt.
* **Zertifikat und Servername werden geprüft**, mit dem System-Speicher. Es gibt keinen Schalter, die Prüfung abzustellen.
  Hinter einer eigenen Zertifizierungsstelle (etwa bei einem eigenen Mailserver) trägt man sie über die Umgebung ein:
  `SSL_CERT_FILE=/pfad/zur/stelle.pem`.
* Das Passwort steht nie in einer Antwort, einem Log, einer Fehlermeldung oder `repr()`. Texte des Servers werden vor dem
  Zeigen gekürzt, von Steuerzeichen befreit und um das Passwort bereinigt.
* **Fremdinhalt.** Betreff, Absender und Text einer Mail werden nur gekürzt, von Steuerzeichen befreit und weitergegeben,
  nie ausgewertet oder ausgeführt. HTML wird zu Text (ohne Skripte und Stile); kein Bild, keine Adresse wird abgerufen.
  Fremde Zeichensätze, kaputte Kodierungen und bösartiges HTML werden ersetzt oder weggelassen, sie brechen nichts ab.
* Suchbegriffe gelangen nur als Wert in Anführungszeichen oder als Literal an den Server; sie können keinen Befehl
  einschmuggeln. Mails, die ein anderes Programm zum Löschen markiert hat, zeigt das Plugin nicht.
* Die Argumente der Werkzeuge gelten als vertraulich (Suchbegriffe, Kennungen), nie im Klartext auf dem Ereignisbus.
* Grenzen schützen den Hub: 20 Sekunden je Schritt und 60 je Sitzung, höchstens 4 Konten gleichzeitig, Antworten des Servers
  und Texte sind begrenzt, ein Anhang wird nie geladen.

## Grenzen

* Nur Anmeldung mit Passwort (`LOGIN`, `AUTHENTICATE PLAIN`). Kein OAuth, kein Kerberos.
* Nur lesen, absichtlich. Auch Entwürfe, Antworten und Weiterleitungen gibt es nicht.
* Suchen, die ein Server nicht kann, meldet er als Fehler (`Der Server … hat den Befehl abgelehnt`), mit seiner Antwort.
* Gmail-Labels erscheinen nur bei Gmail-Konten; andere Konten melden die Rolle des Ordners (`Posteingang`, `Gesendet` …).
* Grenzen: 20 Konten, 10 Ordner je Konto, 25 Mails je Werkzeugaufruf (100 für die Quelle), 50 Anhänge je Mail.

## Entwicklung

```sh
uv venv && . .venv/bin/activate
uv pip install -e /pfad/zu/fyos-os/hub     # der Hub (Paket fyos-hub)
uv pip install -e ".[test]"
python3 -m pytest -q
python3 tools/sauber.py .                  # keine privaten Adressen, Konten, Geheimnisse im Repo
```

Die Tests laufen gegen eine IMAP-Attrappe auf 127.0.0.1 (`tests/attrappe.py`: echtes Protokoll über einen Socket, mit
Literalen, Ordnern, Gmail-Erweiterung und TLS), nie gegen ein echtes Postfach. Die TLS-Tests erzeugen ihre Zertifikate mit
`openssl` und werden ohne es übersprungen; Tests, die den echten Kern brauchen (Anmeldung als Postfach-Quelle), werden
übersprungen, wenn `mcp_hub.postfach` fehlt.

| Pfad | Zweck |
|---|---|
| `fyos_plugin_mail/plugin.py` | `MailPlugin`: Werkzeuge, drei Zustände, Ablage für den Kern |
| `fyos_plugin_mail/ablage.py` | die Abfragen über alle Konten: Auswahl, Ausschnitte, Kennungen, Mailformat |
| `fyos_plugin_mail/imap.py` | die einzige Stelle, die IMAP spricht: Verbindung, TLS, Ordner, Suche, Holen, Fehlertexte |
| `fyos_plugin_mail/konten.py` | Konten aus Umgebung und Konfiguration |
| `fyos_plugin_mail/suche.py` | die Gmail-artige Abfrage lesen und nach IMAP SEARCH übersetzen |
| `fyos_plugin_mail/antwort.py`, `struktur.py`, `nachricht.py`, `utf7.py` | Antworten des Servers lesen, MIME-Aufbau, Kopfzeilen und Zeichensätze, Ordnernamen |
| `fyos_plugin_mail/kern.py` | die optionale Verbindung zu `mcp_hub.postfach` |
| `fyos_plugin_mail/post.py` | `PostPlugin`: FYOS Post (Listmonk), Zugänge je Fläche, Tagesgrenze, Health |
| `fyos_plugin_mail/post_einrichten.py` | das Kommando `fyos-post-einrichten` (Vorlage, Rolle, API-Zugänge in Listmonk) |
| `fyos_plugin_mail/fyos-katalog.yaml`, `bilder/` | der Katalogeintrag „FYOS Post“ für den Installer (Entry-Point `fyos.katalog`) |
| `fyos_plugin_mail/skills/mail/SKILL.md` | Wissen für den Agenten (wann welches Werkzeug, Fremdinhalt, die drei Zustände) |
| `hub-config.beispiel.yaml` | Eintrag in der Hub-Konfiguration |
| `.github/workflows/tests.yml` | GitHub Actions: Tests gegen den Hub aus dem Quellbaum von `fyos-os`, `tools/sauber.py` (Baum und Historie), gitleaks |

## Lizenz

Apache License, Version 2.0 (SPDX-Kennung `Apache-2.0`); Text in [`LICENSE`](LICENSE), Urheberhinweis in
[`NOTICE`](NOTICE). Das Plugin braucht nur die Standardbibliothek und `fyos-hub`.
