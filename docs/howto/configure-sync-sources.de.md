---
source_sha: "80123e3974ff"
---

# Sync-Quellen einrichten { #configure-sync-sources }

Sync-Quellen holen Dokumente aus externen Diensten (Google Drive, S3/MinIO, eine
öffentliche Website, ein Git-Repository, eine SharePoint-Site, ein OneDrive oder
ein Ordner auf einem SFTP-Server) selbsttätig in Knowledge-Collections. Jede Quelle speichert
einen Connector-Typ, eine Ziel-Collection, connector-spezifische Einstellungen,
einen Sync-Modus, einen optionalen Zeitplan und die id des
[Vault-Secrets](../secrets.md), das sie authentifiziert - eine Website braucht
keines.

Läuft ein Sync, listet der Connector die entfernten Dateien auf, lädt sie in ein
temporäres Verzeichnis herunter und schickt sie durch die übliche
Ingestion-Pipeline (parsen, chunken, einbetten, speichern). Ist die Auflistung
vollständig, werden Dokumente entfernt, die die Quelle früher eingebracht hat und
nicht mehr auflistet. Ein Eintrag in `SyncLog` hält das Ergebnis jedes einzelnen
Sync-Vorgangs fest.

### Die Architektur auf einen Blick { #architecture-at-a-glance }

| Baustein | Ort | Rolle |
|-----------|----------|------|
| `BaseSyncConnector` | `app/services/rag/connectors/__init__.py` | Abstrakte Basis aller Connectoren |
| `RemoteFile` | `app/services/rag/connectors/__init__.py` | Pydantic-Modell, das eine entfernte Datei beschreibt |
| `CONNECTOR_REGISTRY` | `app/services/rag/connectors/__init__.py` | Bildet Connector-Typ-Strings auf Klassen ab |
| `SyncSource` (DB-Modell) | `app/db/models/sync_source.py` | Speichert die Konfiguration der Quellen |
| `SyncLog` (DB-Modell) | `app/db/models/sync_log.py` | Verfolgt einzelne Sync-Vorgänge |
| `SyncSourceService` | `app/services/sync_source.py` | Fachlogik für CRUD und Auslösen |
| RAG-CLI-Befehle | `app/commands/rag.py` | CLI-Oberfläche zur Verwaltung der Quellen |
| RAG-API-Routen | `app/api/routes/v1/rag.py` | REST-API zur Verwaltung der Quellen |

## Schnelleinstieg -- CLI { #quick-start-cli }

### Verfügbare Connector-Typen auflisten { #list-available-connector-types }

```bash
# Lists the configured sources and the connector type of each
uv run agenticos cmd rag-sources
```

Welche Connector-Typen dieses Deployment hat (`gdrive`, `s3`, `web`, `git`,
`sharepoint`, `sftp`), zeigt `GET /api/v1/rag/sync/connectors`, wie in [Beispiel:
verfügbare Connectoren ermitteln](#example-discover-available-connectors).

### Eine Google-Drive-Quelle anlegen -- Sync alle 2 Stunden { #add-a-google-drive-source-sync-every-2-hours }

```bash
uv run agenticos cmd rag-source-add \
  --name "Legal docs" \
  --type gdrive \
  --org 0c8f2b1e-... \
  --collection legal \
  --config '{"folder_id": "1abc123def", "include_subfolders": true}' \
  --sync-mode new_only \
  --schedule 120
```

### Eine S3-Quelle anlegen -- nur manueller Sync { #add-an-s3-source-manual-sync-only }

```bash
uv run agenticos cmd rag-source-add \
  --name "Marketing" \
  --type s3 \
  --org 0c8f2b1e-... \
  --collection marketing \
  --config '{"bucket": "my-docs", "prefix": "marketing/"}' \
  --sync-mode full \
  --schedule 0
```

### Eine Git-Quelle anlegen -- die Doku eines Repositorys, jede Nacht { #add-a-git-source-a-repositorys-docs-nightly }

```bash
uv run agenticos cmd rag-source-add \
  --name "Handbook" \
  --type git \
  --org 0c8f2b1e-... \
  --collection handbook \
  --config '{"repository_url": "https://github.com/acme/handbook.git", "branch": "main", "path_prefix": "docs"}' \
  --sync-mode new_only \
  --schedule 1440
```

Wählen Sie anschließend in der Oberfläche sein Access-Token als Credential der
Quelle aus, oder senden Sie `secret_id` mit einem `PATCH` — siehe
[Git-Repository einrichten](#git-repository-setup).

### Eine SharePoint-Quelle anlegen -- ein Bibliotheksordner, alle 6 Stunden { #add-a-sharepoint-source-one-library-folder-every-6-hours }

```bash
uv run agenticos cmd rag-source-add \
  --name "HR policies" \
  --type sharepoint \
  --org 0c8f2b1e-... \
  --collection hr \
  --secret-id <vault-secret-id> \
  --config '{"site_url": "https://contoso.sharepoint.com/sites/HR", "folder_path": "Policies"}' \
  --sync-mode new_only \
  --schedule 360
```

`--secret-id` ist die Microsoft-Entra-App im Vault der Organisation — siehe
[SharePoint und OneDrive einrichten](#sharepoint-and-onedrive-setup).

### Eine SFTP-Quelle anlegen -- ein Ordner, jede Nacht { #add-an-sftp-source-one-folder-nightly }

```bash
uv run agenticos cmd rag-source-add \
  --name "Partner reports" \
  --type sftp \
  --org 0c8f2b1e-... \
  --collection partner-reports \
  --secret-id <vault-secret-id> \
  --config '{"host": "sftp.example.com", "root_path": "/outgoing", "host_key": "ssh-ed25519 AAAAC3Nz..."}' \
  --sync-mode new_only \
  --schedule 1440
```

`--secret-id` ist der SSH-Login im Vault der Organisation, und `host_key` ist der
öffentliche Host-Key des Servers — siehe [SFTP einrichten](#sftp-setup).

### Einen Sync von Hand auslösen { #trigger-sync-manually }

```bash
# Sync a single source by ID
uv run agenticos cmd rag-source-sync <source-id>

# Sync all active sources
uv run agenticos cmd rag-source-sync --all
```

### Eine Quelle entfernen { #remove-a-source }

```bash
uv run agenticos cmd rag-source-remove <source-id>
```

Die `<source-id>` ist eine UUID, die beim Anlegen der Quelle ausgegeben wird und
in der Auflistung von `rag-sources` steht.

## Schnelleinstieg -- Oberfläche { #quick-start-ui }

1. Öffnen Sie **Knowledge Base** und dort den Tab **Sync**.
2. Klicken Sie auf **"+ Add Source"**.
3. Wählen Sie einen Connector-Typ (Google Drive, S3, Website, Git
   repository, SharePoint & OneDrive, SFTP server). Die Formularfelder
   werden aus dem JSON Schema des `CONFIG_MODEL` des Connectors erzeugt. Eine
   Website hat keinen Credential-Schritt.
4. Füllen Sie die connector-spezifischen Konfigurationsfelder aus (etwa Folder-ID,
   Bucket-Name).
5. Wählen Sie eine Ziel-Collection, einen Sync-Modus und ein Zeitintervall.
6. Klicken Sie auf **"Create Source"**.
7. Mit **"Sync Now"** lösen Sie einen sofortigen Sync aus, oder Sie warten, bis
   der Zeitplan von selbst greift.

Um eine Source später zu ändern, klicken Sie auf den Stift in ihrer Zeile. Derselbe
Assistent öffnet sich mit den Werten der Source. Der Connector lässt sich nicht
ändern, und **"Save Changes"** sendet nur die Felder, die Sie geändert haben. Die
Source bleibt der Wissensdatenbank zugeordnet, in der sie ist.

Die Oberfläche ruft dieselbe REST-API auf, die unten dokumentiert ist: Was Sie in
der Oberfläche tun können, können Sie auch mit `curl` oder einem beliebigen
HTTP-Client tun.

## Sync-Modi { #sync-modes }

| Modus | Verhalten |
|------|----------|
| `full` | Alles neu synchronisieren. Alle Dateien werden (erneut) aufgenommen, vorhandene Dokumente ersetzt. Dieser Modus ignoriert Revisionen und Hashes. Führen Sie ihn aus, wenn die Revision eines Connectors eine Änderung übersehen kann. |
| `new_only` | Neue Dateien hinzufügen und geänderte aktualisieren. Änderungen werden über die Revision des Connectors, dann über einen SHA-256-Hash erkannt — unveränderte Dateien werden übersprungen. |
| `update_only` | Nur Dateien aktualisieren, die bereits in der Collection liegen. Neue Dateien werden übersprungen. Die Revision des Connectors und danach ein SHA-256-Hash überspringen unveränderte Dateien. |

!!! tip "`new_only` für die meisten Abläufe"

    Er fügt neue Dateien hinzu und aktualisiert geänderte, während unveränderte
    übersprungen werden — das ist der schnellste inkrementelle Sync.
    `update_only` frischt vorhandene Dokumente auf, ohne neue hinzuzunehmen;
    `full` ist jedes Mal ein sauberer Neuimport.

### Was ein Sync entfernt { #what-a-sync-removes }

In jedem Modus entfernt ein Sync die Dokumente, die seine Quelle früher
eingebracht hat und nicht mehr auflistet: eine von der Website genommene Seite,
eine aus dem Drive-Ordner gelöschte Datei, ein aus dem Bucket entferntes Objekt.
Das Sync-Protokoll zählt sie unter `removed`.

Er entfernt nichts, solange die Auflistung nicht **vollständig** war. Ein Crawl,
der an seinem Seitenlimit angehalten hat oder eine der Seiten nicht lesen konnte,
hat nicht gesehen, was er nicht auflistet. Dieser Lauf behält jedes Dokument und
sagt das in der Meldung des Sync-Protokolls. Der nächste Sync mit einer
vollständigen Auflistung entfernt, was verschwunden ist. Ein Dokument, das nicht
entfernt werden konnte, zählt als fehlgeschlagene Datei, und der nächste Sync
versucht es erneut.

Pro Quelle läuft immer nur ein Sync. Ein Sync, der gestartet wird, während ein
anderer Sync derselben Quelle noch läuft, startet nicht, und sein Protokoll sagt
das.

Entfernt werden nur die eigenen Dokumente der Quelle. Ein Upload oder ein
Dokument, das eine andere Quelle in dieselbe Collection gebracht hat, wird nie
angerührt. Listen zwei Quellen einer Collection dasselbe Dokument auf, bleibt
es erhalten, bis beide es nicht mehr auflisten. Ein Dokument, das aufgenommen
wurde, bevor seine Quelle dies festhielt (September 2026), bleibt erhalten, bis
ein Sync der Quelle es wieder auflistet.

### Was ein zweiter Sync tut { #what-a-second-sync-does }

Ein Sync nach dem ersten tut so wenig, wie die Quelle es zulässt:

- **Eine unveränderte Datei kostet höchstens einen Download, nie eine
  Einbettung.** Ihr SHA-256 stimmt mit dem des gespeicherten Dokuments überein,
  also wird sie als `skipped` gezählt und weder erneut geparst noch eingebettet.
  Ein Connector, der für jede Datei eine Revision meldet, spart auch den Download.
  S3 meldet das `ETag` jedes Objekts, also überspringen `new_only` und
  `update_only` ein Objekt mit unverändertem `ETag` vor dem Download. Ein
  SFTP-Server meldet Größe und Änderungszeit jeder Datei, also überspringen
  dieselben Modi eine Datei, deren Größe und Zeit unverändert sind. Diese Revision
  übersieht eine Bearbeitung, die beide beibehält. `full` lädt jede Datei herunter
  und liest auch eine solche Bearbeitung.
- **Eine unveränderte Quelle kostet eine Anfrage.** Ein Connector, der sagen
  kann, auf welchem Stand sein gesamter Inhalt ist — der Head-Commit eines
  Git-Branches, der Änderungsfeed einer SharePoint-Bibliothek —, hält diesen Stand nach jedem Lauf fest, der ohne
  fehlgeschlagene Datei endete. Der nächste Lauf mit `new_only` oder
  `update_only`, der unter derselben Konfiguration denselben Wert vorfindet, hält
  an, bevor er irgendetwas auflistet: Sein Protokoll zeigt keine verarbeiteten
  Dateien, und er entfernt nichts. Eine Änderung der Konfiguration, der Collection oder des Modus lässt
  den nächsten Lauf wieder alles lesen, und `full` hält nie vorzeitig an.
- **Eine Datei, die ein abgebrochener Sync halb fertig hinterließ, wird in
  Ordnung gebracht.** Ein Worker, der nach dem Speichern der Vektoren einer Datei
  und vor ihrem Festhalten anhielt, lässt sie ohne verfolgende Zeile zurück. Der
  nächste Sync dieser Quelle löscht, was nichts verfolgt, und nimmt die Datei
  erneut auf; solange eine solche Datei wartet, hält er nicht vorzeitig an.

Ein Lauf mit einer fehlgeschlagenen Datei hält keinen Stand fest, sodass der
nächste Lauf die Quelle vollständig liest und die Datei erneut versucht.

## Zeitplan { #schedule }

Das Feld `schedule_minutes` steuert, wie oft die Quelle selbsttätig
synchronisiert:

| Wert | Bedeutung |
|-------|---------|
| `0` (oder `null`) | Nur manuell -- über CLI oder Oberfläche ausgelöst |
| `30` | Alle 30 Minuten |
| `120` | Alle 2 Stunden |
| `1440` | Einmal am Tag |

!!! warning "Ein Zeitplan braucht den Worker"

    Der Zeitplan `dispatch-due-syncs` wacht alle 60 Sekunden auf und reiht für
    jede fällige Quelle einen Sync ein, ohne den Container `worker`, den
    `make dev` startet, bewirkt `schedule_minutes` also nichts. Ohne ihn reiht
    auch ein manueller Auslöser (CLI, API oder Oberfläche) seinen Sync ein, und
    der wartet ebenso auf einen Worker. Eine Quelle hat nie zwei Syncs
    gleichzeitig.

## Google Drive einrichten { #google-drive-setup }

### 1. Ein Dienstkonto anlegen { #1-create-a-service-account }

1. Öffnen Sie die [Google Cloud Console](https://console.cloud.google.com/).
2. Legen Sie ein neues Projekt an (oder wählen Sie ein vorhandenes).
3. Aktivieren Sie die **Google Drive API**.
4. Gehen Sie zu **IAM & Admin > Service Accounts** und legen Sie ein neues
   Dienstkonto an.
5. Erzeugen Sie einen JSON-Schlüssel für das Dienstkonto und laden Sie ihn
   herunter.

### 2. Den Drive-Ordner freigeben { #2-share-your-drive-folder }

1. Öffnen Sie Google Drive und gehen Sie zu dem Ordner, den Sie synchronisieren
   wollen.
2. Klicken Sie auf **Share** und fügen Sie die E-Mail-Adresse des Dienstkontos
   hinzu (sie sieht aus wie `name@project.iam.gserviceaccount.com`).
3. Vergeben Sie mindestens **Viewer**-Zugriff.

### 3. Der Quelle den Schlüssel geben { #3-give-the-source-the-key }

Fügen Sie den Inhalt der JSON-Schlüsseldatei in das Feld **Service Account JSON**
der Quelle ein. Eine `gdrive`-Quelle läuft auf dem Credential, das ihre eigene
Konfiguration trägt, und auf keinem anderen — es gibt keinen deploymentweiten
Rückfall, denn der würde die `folder_id` einer Quelle darüber entscheiden lassen,
was unter dem Dienstkonto des Betreibers aufgelistet wird.

`GOOGLE_DRIVE_CREDENTIALS_FILE` in `.env` gilt allein für den CLI-Befehl
`rag-sync-gdrive`.

### 4. Die Folder-ID herausfinden { #4-get-the-folder-id }

Die Folder-ID ist das letzte Segment der URL des Google-Drive-Ordners:

```
https://drive.google.com/drive/folders/1abc123def456ghi
                                        ^^^^^^^^^^^^^^^
                                        This is the folder ID
```

### 5. Konfigurationsfelder des Google-Drive-Connectors { #5-google-drive-connector-config-fields }

| Feld | Typ | Pflicht | Vorgabe | Beschreibung |
|-------|------|----------|---------|-------------|
| `folder_id` | string | Ja | -- | Die Folder-ID aus der Google-Drive-URL |
| `include_subfolders` | boolean | Nein | `true` | Dateien aus Unterordnern rekursiv einbeziehen |

Das Dienstkonto selbst ist **kein** Konfigurationsfeld. Legen Sie es als
Credential der Art `gcp_service_account` im Vault ab und verweisen Sie die Quelle
mit `secret_id` darauf: So wird es einmal gespeichert und von jeder Quelle
referenziert, die es braucht, statt in jede einzelne eingefügt zu werden
([#937](https://github.com/vstorm-co/agenticos/issues/937)). Es unter `config` zu
senden, wird abgelehnt.

Eine `folder_id` darf nur enthalten, was Google vergibt — Buchstaben, Ziffern,
`-` und `_`. Alles andere wird beim Anlegen der Quelle abgelehnt, denn die id
wird in die Drive-Abfrage interpoliert, und ein einzelnes Anführungszeichen darin
erweitert, was die Abfrage auflistet.

Google Docs, Sheets und Slides werden beim Herunterladen selbsttätig in portable
Formate exportiert (PDF, XLSX, PPTX). Eine Datei, deren Drive-Name
Pfadtrenner enthält, wird als eine Datei innerhalb des Sync-Verzeichnisses
geschrieben, nie unter dem Pfad, den ihr Name buchstabiert.

## S3 / MinIO einrichten { #s3-minio-setup }

### 1. Die Umgebung konfigurieren { #1-configure-the-environment }

Ergänzen Sie Ihre `.env` um die folgenden Variablen:

```bash
S3_RAG_ENDPOINT=https://s3.amazonaws.com   # or your MinIO URL, e.g. http://localhost:9000
S3_RAG_ACCESS_KEY=your-access-key
S3_RAG_SECRET_KEY=your-secret-key
S3_RAG_REGION=us-east-1                    # required for AWS, optional for MinIO
```

Bei MinIO lautet der Endpunkt üblicherweise `http://minio:9000` (Docker) oder
`http://localhost:9000` (lokal).

### 2. Konfigurationsfelder des S3-Connectors { #2-s3-connector-config-fields }

| Feld | Typ | Pflicht | Vorgabe | Beschreibung |
|-------|------|----------|---------|-------------|
| `bucket` | string | Ja | -- | Name des S3-Buckets |
| `prefix` | string | Nein | `""` | Key-Präfix, das den Sync eingrenzt (etwa `documents/legal/`). Für den ganzen Bucket leer lassen. |

## Eine Website einrichten { #website-setup }

Eine `web`-Quelle liest eine öffentliche Website, meist die Dokumentationsseite
eines Produkts. Sie braucht kein Credential und keinen Vault-Eintrag. Geben Sie
ihr eine Start-URL, und sie folgt entweder den Links von dieser Seite aus oder
liest die Seiten, die eine Sitemap auflistet.

```bash
uv run agenticos cmd rag-source-add \
  --name "Product docs" \
  --type web \
  --org 0c8f2b1e-... \
  --collection product-docs \
  --config '{"root_url": "https://docs.example.com/guide/", "max_depth": 3}' \
  --sync-mode new_only \
  --schedule 1440
```

### Konfigurationsfelder des Website-Connectors { #website-connector-config-fields }

| Feld | Typ | Pflicht | Vorgabe | Beschreibung |
|-------|------|----------|---------|-------------|
| `root_url` | string | Ja | -- | Die Seite, von der der Crawl ausgeht. Ihr Host ist der einzige Host, den die Quelle liest. |
| `max_depth` | integer | Nein | `2` | Wie viele Links weit von der Start-URL aus gefolgt wird, `0` bis `10`. `0` liest nur die Startseite. |
| `path_prefix` | string | Nein | der Ordner der Start-URL | Nur Seiten, deren Pfad damit beginnt, werden gelesen. `https://docs.example.com/guide/intro` liest vorgabemäßig `/guide/`; `/` setzen für den ganzen Host. |
| `sitemap_url` | string | Nein | -- | Die Seiten lesen, die diese Sitemap auflistet, statt Links zu folgen. Sie muss auf dem Host der Start-URL liegen und `https://` verwenden, wenn die Start-URL es tut. Einem Sitemap-Index wird bis zu seinen Sitemaps gefolgt. |
| `max_pages` | integer | Nein | `500` | Der Crawl hält an, nachdem er so viele Seiten gelesen hat, `1` bis `5000`. |

### Was einen Crawl begrenzt { #what-bounds-a-crawl }

- **Ein Host und ein Pfad.** Links auf andere Hosts und auf Pfade außerhalb von
  `path_prefix` werden nicht verfolgt. Einer Weiterleitung, die sie verlässt,
  ebenfalls nicht. Eine Start-URL auf `https://` wird nie für `http://`
  verlassen: Einem Link oder einer Weiterleitung auf eine unverschlüsselte Seite
  wird nicht gefolgt.
- **Das Netz des Deployments ist unerreichbar.** Jede Anfrage - robots.txt, die
  Sitemap, jede Seite und jede Weiterleitung - wird gegen dieselbe SSRF-Richtlinie
  geprüft wie Webhooks und MCP-Server. Sie geht an die Adresse, die die Prüfung
  bestanden hat. Eine Start-URL, die auf eine private, Loopback-, Link-Local- oder
  Cloud-Metadata-Adresse auflöst, wird beim Speichern der Quelle abgelehnt.
- **robots.txt wird befolgt**, für Sitemaps und Seiten, einschließlich
  `Crawl-delay` bis zu zehn Sekunden. Der Crawler gibt sich als
  `AgenticOS-Crawler` zu erkennen. Er wartet zwischen Anfragen mindestens eine
  halbe Sekunde, und eine Seite, die `noindex` oder `nofollow` angibt, wird
  respektiert. Eine Seite, die eine Sitemap noch auflistet, nachdem sie `noindex`
  angibt oder verschwunden ist, wird aus der Collection entfernt.
- **Größe und Zeit.** Eine Seite über 5 MB wird nicht gelesen. Der Crawl hält bei
  `max_pages` an. Ein Sync hört nach sechs Stunden auf, die Website zu lesen, und
  ein Sync, der angehalten hat, entfernt nichts.

Jede Seite wird als Markdown-Dokument gespeichert, das ihren Text und die URL
enthält, von der sie stammt, ohne ihren Query-String. Navigation, Kopf- und Fußzeilen und Skripte bleiben
außen vor. Eine Seite wird nur neu eingebettet, wenn sich ihr Text ändert. Ein
neuer Build-Stempel oder ein Tracking-Skript im Markup zählt nicht als Änderung.

### Wer lesen kann, was sie importiert { #who-can-read-what-it-imports }

Eine Website-Quelle hat kein Credential, also reicht sie so weit, wie die Website
jedem im Internet zeigt. Über einen Login kommt sie nie hinaus. Alles, was sie
importiert, ist für jeden durchsuchbar, der die Collection durchsuchen kann, die
sie speist - wie bei jeder anderen Quelle. Siehe
[wer am Ende lesen kann, was eine Quelle aufgenommen hat](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

Importiert werden nur HTML-Seiten. Ein PDF oder eine andere Datei, die von einer
Seite verlinkt ist, wird nicht heruntergeladen.

## Git-Repository einrichten { #git-repository-setup }

Eine `git`-Quelle liest die Dokumentation eines Repositorys über HTTPS — von
GitHub, GitLab oder jedem anderen Host, der git über HTTPS ausliefert. Sie
braucht die Clone-URL und ein Access-Token, nicht die API der jeweiligen
Plattform.

### 1. Ein Token für genau ein Repository ausstellen { #1-issue-a-token-for-the-one-repository }

**Die Reichweite des Tokens ist die Reichweite der Quelle.** Alles, was die
Quelle aufnimmt, wird für jeden durchsuchbar, der die Collection lesen darf. Ein
Token, das jedes private Repository seines Besitzers lesen kann, kann also alle
davon diesem Publikum zugänglich machen. Siehe [wer am Ende lesen kann, was eine
Quelle aufgenommen
hat](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

- **GitHub:** ein fine-grained Personal Access Token, *Only select repositories*,
  mit dem einen Repository und **Contents: Read-only** als einziger Berechtigung.
- **GitLab:** ein Project Access Token auf dem einen Projekt, Rolle **Reporter**,
  nur mit dem Scope **`read_repository`**.

Geben Sie ihm ein Ablaufdatum. Läuft es ab, schlägt der nächste Sync der Quelle
mit *the repository refused the source's token* fehl, und die Abhilfe ist ein
neues Token im selben Vault-Secret.

### 2. Es im Vault ablegen { #2-add-it-to-the-vault }

Legen Sie das Token als **Git-Zugriffstoken** im Vault ab, zusammen mit dem
**Host**, zu dem es gehört: `github.com`, `gitlab.com` oder Ihr eigener Server
wie `git.example.com:8443`. Einen Host mit Nicht-ASCII-Buchstaben geben Sie in
seiner kodierten Form ein, etwa `xn--bcher-kva.example` für `bücher.example`.
Wählen Sie das Token dann im Credential-Schritt der Quelle aus. Es wird als HTTP-Header `Authorization` gesendet, nie in der URL und nie in
einer Befehlszeile, die ein anderer Prozess lesen kann.

**Der Host gehört zum Token, nicht zur Quelle.** Wer eine Quelle bearbeitet,
wählt ihre Repository-URL, und ein Token wird nur an den Host gesendet, mit dem
es abgelegt wurde. Das Bearbeiten einer Quelle kann das Token der Organisation
also nicht auf einen anderen Server richten, und keine andere Art von Schlüssel,
etwa der API key eines Modell-Providers, lässt sich für eine Git-Quelle
überhaupt auswählen.

### 3. Konfigurationsfelder des Git-Connectors { #3-git-connector-config-fields }

| Feld | Typ | Pflicht | Vorgabe | Beschreibung |
|-------|------|----------|---------|-------------|
| `repository_url` | string | Ja | -- | Die HTTPS-Clone-URL, etwa `https://github.com/acme/handbook.git`. Ohne Benutzername oder Token darin. |
| `branch` | string | Nein | `main` | Der zu lesende Branch. |
| `path_prefix` | string | Nein | -- | Ein Verzeichnis im Repository, etwa `docs`. Für das ganze Repository leer lassen. |
| `include` | list of strings | Nein | `**/*.md`, `**/*.txt` | Welche Dateien aufgenommen werden, als Muster im Stil von `.gitignore` relativ zu `path_prefix`. |

Die Vorgabe ist die Dokumentation, nicht der ganze Baum: Der Quellcode eines
Repositorys ist kein Korpus, und ihn aufzunehmen füllt eine Knowledge Base mit
Code, den niemand durchsuchen wollte. Ergänzen Sie ein Muster wie `**/*.pdf` für
ein weiteres Format, das der Parser der Collection liest. Ein Muster darf nicht
mit `!` beginnen.

Jede Datei ist ein Dokument mit der Adresse
`git://<host>/<owner>/<repo>@<branch>/<path>`, mit `:<port>` nach dem Host, wenn
der Port nicht 443 ist. Der Branch ist Teil der Adresse, sodass zwei Quellen, die zwei Branches eines Repositorys in eine Collection
lesen, getrennte Dokumente behalten.

### 4. Was ein Sync überträgt { #4-what-a-sync-transfers }

Die erste Anfrage jedes Syncs ist `git ls-remote` für den Branch — etwa ein
Kilobyte. Hat sich der Head-Commit seit dem letzten sauberen Lauf nicht bewegt,
hält der Sync dort an. Hat er sich bewegt, erstellt der Connector einen flachen,
partiellen, sparse Klon: ein Commit und nur die Dateien, auf die die
Include-Muster passen. Die Dokumentation eines Monorepos kostet daher ihre
Dokumentation, nicht seinen Quellbaum.

Bevor der Klon etwas auf die Festplatte des Workers schreibt, misst der Connector
jede Datei, die er schreiben würde. Eine Datei über der Dokumentgrenze der
Wissensbasis (`MAX_UPLOAD_SIZE_MB`, standardmäßig 50 MB) oder insgesamt mehr als
512 MB an Dateien werden abgelehnt, und nichts wird geschrieben.

Symbolischen Links und Submodulen wird nicht gefolgt, und ein Link wird nicht als
Dokument aufgenommen.

### Netzwerkregeln { #network-rules }

Die URL muss `https://` sein. Ihr Host wird einmal aufgelöst und wie jede andere
Adresse geprüft, die ein Mandant wählt: Ein Host, der auf eine private,
Loopback- oder Link-Local-Adresse auflöst, wird beim Speichern der Quelle
abgelehnt und beim Sync erneut, und git verbindet sich nur mit den Adressen, die
diese Prüfung zugelassen hat. Weiterleitungen wird nicht gefolgt. Ein Deployment
hinter einem Egress-Proxy (`HTTPS_PROXY`) nutzt ihn weiterhin; der Proxy löst
den Host dann selbst auf.

Das Worker-Image enthält `git`. Ein Worker aus einem anderen Image braucht `git`
2.37 oder neuer in seinem `PATH`.

## SharePoint und OneDrive einrichten { #sharepoint-and-onedrive-setup }

Eine `sharepoint`-Quelle liest eine Dokumentbibliothek über Microsoft Graph: die
Bibliothek einer SharePoint-Site oder das OneDrive einer Person, das Microsoft 365
als eigene Site führt. Sie kann die ganze Bibliothek lesen oder einen Ordner
darin. Sie meldet sich als App-Registrierung in Microsoft Entra an, die ein
Administrator Ihres Tenants einmal anlegt.

### 1. Eine App in Microsoft Entra registrieren { #1-register-an-app-in-microsoft-entra }

Öffnen Sie im [Microsoft Entra admin center](https://entra.microsoft.com)
**App registrations → New registration**. Geben Sie ihr einen Namen wie *AgenticOS
sync*, behalten Sie **Accounts in this organizational directory only** bei und
lassen Sie die Redirect-URI leer. Die Quelle meldet sich als die App selbst an,
nicht als eine Person.

Notieren Sie in der **Overview** der App die **Application (client) ID** und die
**Directory (tenant) ID**. Fügen Sie unter **Certificates & secrets** ein Client
Secret hinzu und kopieren Sie seinen **Value**. Der Wert wird nur einmal
angezeigt. Die *ID* des Secrets ist nicht das, was die Quelle braucht.

### 2. Ihr eine Site gewähren, nicht den Tenant { #2-grant-it-one-site-not-the-tenant }

**Die Reichweite der App ist die Reichweite der Quelle.** Alles, was die Quelle
aufnimmt, wird für jeden durchsuchbar, der die Collection lesen kann, die sie
speist. Siehe [wer am Ende lesen kann, was eine Quelle aufgenommen
hat](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

Fügen Sie unter **API permissions** die Microsoft-Graph-Berechtigung
**`Sites.Selected`** vom Typ **application** hinzu und erteilen Sie dafür die
Administratorzustimmung. Für sich allein liest `Sites.Selected` nichts. Ein
Administrator gewährt der App anschließend Lesezugriff auf die eine Site, die die
Quelle liest:

```http
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
Content-Type: application/json

{
  "roles": ["read"],
  "grantedToIdentities": [
    {"application": {"id": "<client-id>", "displayName": "AgenticOS sync"}}
  ]
}
```

Dieser Aufruf braucht `Sites.FullControl.All`, deshalb macht ihn ein
Administrator aus Graph Explorer heraus oder mit `Grant-PnPAzureADAppSitePermission`
aus PnP PowerShell, und nicht mit der App selbst.
`GET https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/HR?$select=id`
liefert die Site-id. Ein OneDrive wird auf dieselbe Weise gewährt, über seine Site:
`contoso-my.sharepoint.com:/personal/jane_contoso_com`.

!!! danger "`Files.Read.All` oder `Sites.Read.All` macht jede Bibliothek im Tenant lesbar"

    Diese Berechtigungen werden tenantweit erteilt. Eine Quelle mit ihnen liest
    trotzdem nur die Bibliothek, die sie nennt. Aber wer die Quelle bearbeiten
    kann, kann ihre Site-URL auf jede Site und jedes OneDrive der Organisation
    richten, und der nächste Sync macht diese Bibliothek für jeden durchsuchbar,
    der die Collection lesen kann. Nichts in diesem Produkt kann erkennen, welche
    Berechtigung die App erhalten hat, denn ein Token sagt es nicht. Verwenden Sie
    `Sites.Selected` und gewähren Sie eine App pro Publikum.

### 3. Sie im Vault ablegen { #3-add-it-to-the-vault }

Legen Sie die App als **Microsoft Entra app** im Vault ab: die Tenant-id (oder die
Domain des Tenants, etwa `contoso.onmicrosoft.com`), die Client-id und den Wert
des Client Secrets. Wählen Sie sie dann im Credential-Schritt der Quelle aus. Das
Secret wird nur an `login.microsoftonline.com` gesendet, zur Anmeldung.

Ein Client Secret läuft ab, spätestens nach zwei Jahren. Ist es abgelaufen,
schlägt der nächste Sync mit *Microsoft Entra refused the app registration's
credentials (invalid_client)* fehl. Fügen Sie der App ein neues Secret hinzu und
ersetzen Sie den Wert im selben Vault-Secret. Jede Quelle, die es nutzt,
übernimmt den neuen Wert bei ihrem nächsten Sync.

### 4. Konfigurationsfelder des SharePoint-Connectors { #4-sharepoint-connector-config-fields }

| Feld | Typ | Pflicht | Vorgabe | Beschreibung |
|-------|------|----------|---------|-------------|
| `site_url` | string | Ja | -- | Die Site, etwa `https://contoso.sharepoint.com/sites/HR`, oder ein OneDrive, etwa `https://contoso-my.sharepoint.com/personal/jane_contoso_com`. Nur die Site: Eine aus dem Browser kopierte Bibliotheks- oder Seiten-URL wird abgelehnt. |
| `library` | string | Nein | die Standardbibliothek der Site | Der Name der Bibliothek, wie SharePoint ihn anzeigt, etwa `Documents`. Ein OneDrive hat nur seine Standardbibliothek. |
| `folder_path` | string | Nein | -- | Ein Ordner innerhalb der Bibliothek, etwa `Policies/HR`. Für die ganze Bibliothek leer lassen. |
| `include_subfolders` | boolean | Nein | `true` | Ob die Ordner unterhalb von `folder_path` gelesen werden. |
| `extensions` | list of strings | Nein | `.pdf`, `.docx`, `.md`, `.txt` | Welche Dateitypen aufgenommen werden, höchstens 100, jeder höchstens 16 Zeichen lang. Ergänzen Sie `.pptx`, `.xlsx` oder einen anderen Typ nur, wenn der Parser der Collection ihn liest: Eine Datei, die der Parser nicht lesen kann, zählt bei jedem Sync als fehlgeschlagene Datei. |

Jede Datei ist ein Dokument mit der Adresse `sharepoint://<drive-id>/<item-id>`.
Die Adresse folgt der Datei, nicht ihrem Pfad, sodass eine Datei, die innerhalb
der Bibliothek umbenannt oder verschoben wird, ihr Dokument behält. Das Dokument
behält den Dateinamen, mit dem es aufgenommen wurde, bis sich der Inhalt der Datei
ändert.

OneNote-Notizbücher und Dateien anderer Typen werden nicht aufgenommen.

### 5. Was ein Sync überträgt { #5-what-a-sync-transfers }

Jeder Sync beginnt mit dem Änderungsfeed von Microsoft Graph für die Bibliothek.
Hat sich in der Bibliothek seit dem letzten sauberen Lauf nichts geändert, hält
der Sync dort an: eine Anfrage und keine Auflistung. Der Feed umfasst die ganze
Bibliothek, weil Graph ihn nur für die Wurzel einer Bibliothek anbietet. Eine
Änderung in einem anderen Ordner derselben Bibliothek lässt den nächsten Sync
daher den Ordner der Quelle erneut auflisten.

Hat sich etwas geändert, listet der Sync den Ordner auf und lädt jede Datei der
konfigurierten Typen herunter. Eine Datei, deren Inhalt sich nicht geändert hat,
wird dann vor dem Einbetten übersprungen, sodass nur neue und geänderte Dateien
eine Einbettung kosten. Eine Datei wird von der Adresse heruntergeladen, die Graph
für sie angibt, ohne das Token der App. Eine Datei über der Dokumentgrenze der
Wissensbasis (`MAX_UPLOAD_SIZE_MB`, standardmäßig 50 MB) wird nicht
heruntergeladen und zählt als fehlgeschlagene Datei.

Graph bremst einen Client, der schnell liest. Eine Anfrage, die Graph drosselt
(HTTP 429) oder die mit einem 5xx oder einem Netzwerkfehler scheitert, wird
insgesamt viermal versucht, und der Sync wartet so lange, wie Graphs
`Retry-After` verlangt, bis zu einer Minute. Ein Ordner, der sich dann immer noch
nicht auflisten lässt, wird im Sync-Protokoll genannt, und dieser Sync entfernt
nichts, weil die Dateien in diesem Ordner nicht gesehen wurden. Die Dateien in
den anderen Ordnern werden trotzdem aufgenommen.

Eine Datei, die nach der Auflistung und vor ihrem Download gelöscht wurde, wird
entfernt wie eine Datei, die die Auflistung nicht genannt hat.

### Netzwerk { #network }

Der Worker braucht ausgehendes HTTPS zu `login.microsoftonline.com`,
`graph.microsoft.com` und den `*.sharepoint.com`-Hosts Ihres Tenants. Nichts, was
der Bearbeiter einer Quelle eintippt, wird zu einer Adresse, mit der sich der
Worker verbindet: Die Site-URL ist nur ein Name, den Graph nachschlägt. Die
nationalen Clouds von Microsoft, etwa US Government und China, nutzen andere Hosts
und werden nicht unterstützt.

## SFTP einrichten { #sftp-setup }

Eine `sftp`-Quelle liest einen Ordnerbaum auf einem SFTP-Server über SSH, etwa den
Ablageordner eines Partners oder einen Dateiserver. Sie meldet sich mit einem
SSH-Login aus dem Vault an. Sie vertraut nur dem Host-Key in ihren eigenen
Einstellungen und liest nichts von der eigenen SSH-Einrichtung des Workers.

### 1. Den Host-Key des Servers beschaffen { #1-get-the-servers-host-key }

Der Host-Key ist der einzige Beweis dafür, dass der Worker mit dem Server spricht,
den die Quelle nennt. Holen Sie ihn beim Administrator des Servers ein. Oder lesen
Sie ihn selbst vom Server und vergleichen Sie seinen Fingerabdruck mit dem des
Administrators:

```bash
ssh-keyscan -t ed25519 -p 22 sftp.example.com > host_key.txt
ssh-keygen -lf host_key.txt
```

`ssh-keyscan` liest den Schlüssel über das Netzwerk und kann deshalb nicht
beweisen, dass der Schlüssel der des Servers ist. `ssh-keygen -lf` gibt den
`SHA256:`-Fingerabdruck des Schlüssels aus. Der Administrator liest denselben
Fingerabdruck auf dem Server mit
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`. Stimmen die beiden nicht
überein, verwenden Sie den Schlüssel nicht.

`ssh-keyscan` schreibt den Hostnamen vor den Schlüssel. Die Quelle braucht nur den
Schlüsseltyp und den Schlüssel:

```bash
cut -d' ' -f2- host_key.txt
```

Der Connector pinnt einen Schlüssel vom Typ `ssh-ed25519`, `ssh-ed448`,
`ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521` oder `ssh-rsa`.
Für einen Server ohne Ed25519-Schlüssel verwenden Sie `-t ecdsa` oder `-t rsa`. Der
Worker fragt den Server nur nach einem Schlüssel des gepinnten Typs. Ein Server mit
Schlüsseln mehrerer Typen antwortet mit dem Schlüssel dieses Typs.

Ein Server ohne Schlüssel des gepinnten Typs beendet den ersten Sync, während die
Verbindung aufgebaut wird. Der Satz im Sync-Protokoll hängt vom Server ab: [die
Verbindung ging verloren](#sftp-the-connection-to-was-lost), [der Server hat die
Verbindung geschlossen](#sftp-closed-the-connection) oder [die beiden konnten sich
nicht einigen](#sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher).
Prüfen Sie bei jedem der drei zuerst den Schlüsseltyp.

### 2. Einen SSH-Login im Vault ablegen { #2-add-an-ssh-login-to-the-vault }

**Die Reichweite des Kontos ist die Reichweite der Quelle.** Alles, was die Quelle
aufnimmt, wird für jeden durchsuchbar, der die Collection lesen kann, die sie
speist. Bitten Sie den Administrator um ein Konto, das nur den Ordner lesen kann,
den die Quelle liest. Siehe [wer am Ende lesen kann, was eine Quelle aufgenommen
hat](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

Bitten Sie den Administrator außerdem, das Konto in einem Ordner einzuschließen,
der nur enthält, was synchronisiert werden darf: `ChrootDirectory` bei OpenSSH,
`DefaultRoot` bei ProFTPD. Das ist auf jedem Server notwendig, weil ein Link, der
in den Ordner gelegt wird, den Sync eine andere Datei lesen lassen kann, die das
Konto lesen kann. Ein chroot, der auch andere Dateien enthält, legt diese trotzdem
offen. Siehe [was ein SFTP-Sync überträgt](#4-what-an-sftp-sync-transfers).

Ziehen Sie einen Schlüssel einem Passwort vor, und ziehen Sie einen
Ed25519-Schlüssel vor. Erzeugen Sie ein Schlüsselpaar für die Quelle:

```bash
ssh-keygen -t ed25519 -C agenticos-sync -f agenticos_sync
```

Nimmt der Server keinen Ed25519-Schlüssel an, erzeugen Sie stattdessen einen
ECDSA-Schlüssel. Apache MINA SSHD ohne seine EdDSA-Bibliothek ist ein solcher
Server.

```bash
ssh-keygen -t ecdsa -b 256 -C agenticos-sync -f agenticos_sync
```

Der Administrator fügt `agenticos_sync.pub` zu den `authorized_keys` des Kontos
hinzu. Der private Schlüssel, `agenticos_sync`, kommt in den Vault.

Legen Sie den Login im Vault für den Dienst **SFTP server** als **SSH login** ab:

- **User name**: das Konto auf dem Server.
- **Host**: wo der Login verwendet werden darf, etwa `sftp.example.com`. Für einen
  anderen Port als 22 ergänzen Sie den Port: `sftp.example.com:2222`. Schreiben
  Sie eine IPv6-Adresse in eckigen Klammern, etwa `[2001:db8::7]:2222`. Der Vault
  speichert den Host so, wie eine Quelle ihn schreibt: in Kleinbuchstaben, ohne
  abschließenden Punkt, eine IPv6-Adresse in ihrer Kurzform und einen Namen mit
  Nicht-ASCII-Buchstaben in seiner kodierten Form, etwa `xn--bcher-kva.example`
  für `bücher.example`. Ein Name darf einen Unterstrich enthalten, etwa
  `sftp_server`. Der Host eines Git-Zugriffstokens darf das nicht.
- **Private key**: die ganze Schlüsseldatei, im Format OpenSSH, PKCS#8 oder
  PKCS#1, höchstens 16.384 Zeichen. Ist der Schlüssel verschlüsselt, geben Sie
  auch seine **Key passphrase** an, höchstens 1.024 Zeichen. Ein RSA-Schlüssel darf höchstens 4096 Bit haben, und seine zwei Primzahlen
  müssen gleich groß sein, wie `ssh-keygen` sie erzeugt. Ein Schlüssel auf einem
  Security Key (`sk-`) lässt sich nicht verwenden. Ein verschlüsselter
  Schlüssel darf höchstens 128 bcrypt-Runden (OpenSSH) verlangen, oder 1.000.000
  Iterationen und einen abgeleiteten Schlüssel von 64 Bytes (PKCS#8).
  `ssh-keygen` verwendet 16 Runden.
- **Password**: nur für einen Server, der keinen Schlüssel annimmt. Es muss
  mindestens acht und höchstens 1.024 Zeichen lang sein. Ein Server, der ein Passwort nur über einen
  keyboard-interactive Login annimmt, etwa PAM mit `PasswordAuthentication no`,
  lehnt es ab. Der Worker beantwortet nie eine keyboard-interactive Abfrage,
  verwenden Sie dort also einen Schlüssel.

Ein Login enthält einen privaten Schlüssel oder ein Passwort, nie beides. Der Vault
öffnet den Schlüssel mit seiner Passphrase, wenn Sie den Login speichern, sodass
ein Schlüssel, der sich nicht öffnen lässt, sofort abgelehnt wird, nicht erst beim
ersten Sync. Er öffnet den Schlüssel in einem eigenen Prozess, der höchstens 5
Sekunden brauchen darf. Ein Ed25519-Schlüssel braucht Millisekunden. Ein
RSA-Schlüssel mit 4096 Bit oder ein Schlüssel an den obigen Grenzen braucht
weniger als zwei Sekunden. Wählen Sie den Login dann im Credential-Schritt der
Quelle aus.

**Der Host gehört zum Login, nicht zur Quelle.** Wer eine Quelle bearbeitet, tippt
ihren Host ein, und der Connector sendet den Login nur an den Host und Port, für
den er abgelegt wurde. Ein für `sftp.example.com` abgelegter Login wird nur auf
Port 22 verwendet. Das Bearbeiten einer Quelle kann den Login der Organisation also
nicht an einen anderen Server senden.

!!! warning "Wer die Quelle bearbeitet, wählt auch den Host-Key, dem sie vertraut"

    Ein Bearbeiter, der außerdem den Verkehr des Workers zum Host des Logins
    abfangen kann, kann einen eigenen Schlüssel pinnen und ein Passwort empfangen.
    Ein Login mit Schlüssel gibt den Schlüssel nicht preis: Der Server erhält nur
    eine Signatur, die nur für seine eigene Session gültig ist. Das ist der Grund,
    einen Schlüssel vorzuziehen.

### 3. Konfigurationsfelder des SFTP-Connectors { #3-sftp-connector-config-fields }

| Feld | Typ | Pflicht | Vorgabe | Beschreibung |
|-------|------|----------|---------|-------------|
| `host` | string | Ja | -- | Hostname oder IP-Adresse des Servers, etwa `sftp.example.com`. Nur der Host: ohne Schema, Benutzername, Port oder Pfad. |
| `port` | integer | Nein | `22` | Der SSH-Port des Servers, `1` bis `65535`. |
| `root_path` | string | Ja | -- | Der absolute Pfad des zu lesenden Ordners, etwa `/outgoing`. Es gibt keine Vorgabe: Auf einem Server, der das Konto nicht auf seinen Home-Ordner beschränkt, ist `/` das ganze Dateisystem. |
| `host_key` | string | Ja | -- | Der öffentliche Host-Key des Servers, eine Zeile wie `ssh-ed25519 AAAA...`, aus [Schritt 1](#1-get-the-servers-host-key). Ein Kommentar nach dem Schlüssel wird verworfen. |
| `include_subfolders` | boolean | Nein | `true` | Ob die Ordner unterhalb von `root_path` gelesen werden. |
| `extensions` | list of strings | Nein | `.pdf`, `.docx`, `.md`, `.txt` | Welche Dateitypen aufgenommen werden, höchstens 100, jeder höchstens 16 Zeichen lang. Ergänzen Sie `.pptx`, `.xlsx` oder einen anderen Typ nur, wenn der Parser der Collection ihn liest: Eine Datei, die der Parser nicht lesen kann, zählt bei jedem Sync als fehlgeschlagene Datei. |

Das Formular zeigt diese Felder als **Host**, **Port**, **Folder**, **Host key**,
**Include subfolders** und **File types**. Der Connector schreibt jeden Ordner in
einer einheitlichen Form: `/outgoing//2026/` und `/outgoing/./2026` werden beide
zu `/outgoing/2026`. Ein Ordner mit `..` darin wird abgelehnt.

Jede Datei ist ein Dokument mit der Adresse `sftp://<user>@<host>:<port><path>`,
etwa `sftp://sync@sftp.example.com:22/outgoing/2026/report.pdf`. Der Benutzername
kommt aus dem Login und wird prozentkodiert. Der Pfad ist der volle Pfad der Datei
auf dem Server. So behalten zwei Konten auf einem Server getrennte Dokumente, und
eine Quelle, die auf einen engeren Ordner umgestellt wird, behält die Dokumente,
die sie noch auflistet. Ein Login, der auf einen anderen Benutzernamen geändert
wird, oder eine Quelle, die auf einen anderen Host oder Port umgestellt wird,
ändert jede Adresse. Der nächste Sync liest dann jede Datei erneut, und eine
vollständige Auflistung entfernt die Dokumente unter den alten Adressen. Planen
Sie einen Serverumzug mit diesen Kosten im Blick.

### 4. Was ein SFTP-Sync überträgt { #4-what-an-sftp-sync-transfers }

SFTP hat keinen Änderungsfeed, also listet jeder Sync den ganzen Ordnerbaum auf.
Die Auflistung liefert für jede Datei Größe und Änderungszeit, in ganzen Sekunden.
Dieses Paar ist die Revision der Datei, also überspringen `new_only` und
`update_only` eine Datei, deren Größe und Zeit unverändert sind, vor dem Download.

!!! warning "Eine Bearbeitung, die Größe und Zeit beibehält, wird übersehen"

    `put -p` behält die Zeit bei, die die Datei beim Hochladenden hatte, ein
    `SETSTAT` kann die Zeit zurücksetzen, und ein Neuschreiben mit derselben Größe
    kann in dieselbe Sekunde fallen. Jedes davon ändert eine Datei und behält ihre
    Revision bei, also überspringen `new_only` und `update_only` die Datei. Führen
    Sie die Quelle im Modus `full` aus, um jede Datei erneut zu lesen. OpenSSH,
    SFTPGo, ProFTPD und Apache MINA SSHD verhalten sich alle so.

Ein Download liest die Datei in Anfragen zu 255 KiB, eine nach der anderen, sodass
eine Datei von 50 MB etwa 200 Roundtrips kostet. Der Connector vergleicht die
Stat-Werte der Datei vor und nach dem Lesen. Eine Datei, die sich während des
Lesens geändert hat oder ersetzt wurde oder nach der Auflistung gelöscht wurde,
wird als `skipped` gezählt. Der nächste Sync liest ihre neue Version oder entfernt
ihr Dokument.

Ein Upload, der noch läuft, ändert die Datei meist während des Lesens, sodass der
Sync die Datei überspringt. Kommt während des Lesens kein Byte an, kann der Sync
den bereits angekommenen Teil aufnehmen, und der nächste Sync liest die
vollständige Datei. Ein Upload unter einem temporären Namen, der nach Abschluss
umbenannt wird, wird nie teilweise gelesen, wenn der temporäre Name einen Typ hat,
der nicht in **File types** steht, etwa `.part` oder `.filepart`.

Eine Datei über der Dokumentgrenze der Wissensbasis (`MAX_UPLOAD_SIZE_MB`,
standardmäßig 50 MB) wird nicht heruntergeladen. Sie zählt bei jedem Sync als
fehlgeschlagene Datei und bleibt so sichtbar. Der Connector zählt außerdem die
Bytes während des Lesens, sodass ein Server, der mehr sendet, als seine Stat-Werte
angaben, die Grenze nicht überschreiten kann.

Die Auflistung lässt diese Einträge aus:

- **Links und Spezialdateien.** Die Auflistung listet nie einen symbolischen Link
  auf, egal worauf er zeigt, sodass der Sync keine Schleife durchläuft. Ein FIFO,
  ein Socket oder ein Gerät wird ebenfalls nicht aufgelistet. Ein Link kann
  trotzdem eine aufgelistete Datei oder einen Ordner nach der Auflistung ersetzen:
  siehe die Warnung unten.
- **Namen, die keine Adresse sein können.** Ein Name, der leer ist, `/` oder ein
  NUL-Byte enthält, länger als 255 Bytes ist oder kein UTF-8 ist, wird
  ausgelassen. Ebenso ein Name, dessen Pfad länger als 4.096 Bytes ist. Das
  Sync-Protokoll nennt den Ordner, und die Auflistung bleibt vollständig.
- **Einträge unbekannten Typs.** Ein Eintrag, dessen Typ der Server nicht angibt,
  wird ausgelassen. Die Auflistung ist dann unvollständig, also entfernt dieser
  Lauf nichts.
- **Ordner, die der Server nicht auflistet.** Ein Ordner unterhalb von
  `root_path`, dessen Auflistung der Server verweigert, meist mangels
  Berechtigung, oder auf den er so antwortet, dass der Worker die Antwort nicht
  dekodieren kann, wird im Sync-Protokoll und im Worker-Log genannt. Dieser Lauf
  entfernt nichts.

!!! warning "Ein Link im Ordner kann den Sync eine andere Datei lesen lassen"

    SFTP Version 3 kann keinen Pfad öffnen, ohne einem Link zu folgen, und kein
    getesteter Server bietet einen Weg daran vorbei. Wer im Ordner schreiben kann,
    kann eine aufgelistete Datei nach der Auflistung durch einen Link ersetzen.
    Der Download liest dann das Ziel des Links, wenn das Konto es lesen kann, und
    der Sync nimmt es als die aufgelistete Datei auf.

    Dieses Wettrennen gibt es auf jedem Server. Wo der Stat des Servers einen Link
    als Link meldet, wie bei OpenSSH, SFTPGo und Apache MINA SSHD, muss der Link
    in den wenigen Anfragen vor dem Download eingesetzt und vor dem Ende des
    Downloads wieder entfernt werden. Die gespeicherte Revision ist dann die der
    aufgelisteten Datei, sodass `new_only` und `update_only` den falschen Inhalt
    behalten, bis sich die aufgelistete Datei ändert. Bei ProFTPD, dessen Stat
    Links folgt, reicht das Fenster von der Auflistung bis zum Download, und der
    Link kann bleiben.

    Ein Ordner lässt sich auf dieselbe Weise ersetzen, auf jedem Server, und
    dieser Link kann bleiben. Ein Stat eines Pfads folgt einem Link in jedem
    Ordner des Pfads und prüft nur den letzten Namen. Ersetzt jemand einen Ordner
    durch einen Link, nachdem der Sync den Ordner darüber aufgelistet hat, listet
    der Sync an seiner Stelle das Ziel des Links auf und nimmt diese Dateien unter
    dem Pfad des Ordners auf. Keine Prüfung des Connectors bemerkt das.

    Die Abhilfe liegt auf dem Server:

    - Das Konto kann nur lesen, was synchronisiert werden darf: ein chroot, dessen
      Inhalt der synchronisierte Baum ist (`ChrootDirectory` mit
      `ForceCommand internal-sftp` bei OpenSSH, `DefaultRoot` bei ProFTPD), oder
      ein Konto, dessen Berechtigungen nur diesen Baum erreichen. Ein chroot, der
      auch andere Dateien enthält, legt diese über einen Link trotzdem offen. Bei
      Apache MINA SSHD hält ein virtuelles Root einen Link nicht auf, schließen
      Sie das Konto also im Betriebssystem ein. SFTPGo lehnt einen Link ab, der
      aus dem Root des Benutzers hinauszeigt.
    - Die Konten, die hochladen, können weder Links noch Hardlinks anlegen:
      `internal-sftp -P symlink,hardlink` bei OpenSSH, `<Limit SYMLINK> DenyAll`
      und `SFTPExtensions -hardlink` bei ProFTPD und keine Berechtigung
      `create_symlinks` bei SFTPGo. Bei ProFTPD hält `<Limit SYMLINK>` allein
      einen Hardlink nicht auf.
    - Ein Hardlink kommt an jeder Prüfung und an einem chroot vorbei. Die
      Linux-Einstellung `fs.protected_hardlinks=1`, auf gängigen Distributionen
      die Vorgabe, hindert einen Benutzer daran, eine Datei zu verlinken, die ihm
      nicht gehört und die er nicht lesen und schreiben kann.

    Der Connector prüft außerdem, ob die geöffnete Datei die aufgelistete ist.
    Besitzer und Gruppe müssen denen der Auflistung entsprechen. Die
    Berechtigungen müssen denen des Stats direkt vor dem Öffnen entsprechen und
    zusätzlich denen der Auflistung, wenn dieser Stat eine andere Größe oder
    Änderungszeit angibt als die Auflistung. Die Berechtigungen einer Auflistung
    lassen sich fälschen, wie es `DirFakeMode` von ProFTPD tut, sodass eine
    unveränderte Datei dort trotzdem gelesen wird. Eine Datei, die sich auf einem
    solchen Server wirklich geändert hat, schlägt einmal fehl, und der nächste
    Sync liest sie.

    Das ist eine zusätzliche Schutzschicht, keine Abhilfe, und sie deckt nur eine
    ersetzte Datei ab. Bei OpenSSH und ProFTPD erkennt sie einen Tausch gegen
    eine Datei mit anderem Besitzer, anderer Gruppe oder anderen Berechtigungen,
    und die Datei schlägt mit "The server opened a file for … that is not the
    file it listed. …" fehl.

    Bei Apache MINA SSHD und SFTPGo, die diese Prüfung anhand des Pfads
    beantworten, bewirkt sie nichts. Sie übersieht außerdem einen Hardlink, ein
    Ziel mit demselben Besitzer, derselben Gruppe und denselben Berechtigungen und
    einen Ordner, der durch einen Link ersetzt wurde, bevor der Sync ihn
    aufgelistet hat. Bei ProFTPD übersieht sie einen Link, der vor dem Stat
    eingesetzt wurde und auf eine Datei mit dem aufgelisteten Besitzer, der
    aufgelisteten Gruppe, Größe und Änderungszeit zeigt.

Das Worker-Log ist die Ausgabe des Worker-Prozesses. In einem Compose-Deployment
lesen Sie es mit `docker compose logs worker`. Es enthält die eigene Antwort des
Servers, die das Sync-Protokoll nie zeigt, und den Grund, aus dem eine Datei
fehlgeschlagen ist.

Jeder Satz, den die Auflistung dem Sync-Protokoll hinzufügt, zählt als
fehlgeschlagene Datei. Der Lauf hat also den Status `error`, bis die Ursache auf
dem Server behoben ist oder ein engerer Ordner sie ausschließt. Das Sync-Protokoll
zeigt höchstens drei Hinweise und zählt die übrigen als "And … more.". Beheben Sie
die angezeigten Ursachen und synchronisieren Sie erneut, um die nächsten zu sehen.

Ein Steuerzeichen in einem Datei- oder Ordnernamen, etwa ein Zeilenumbruch oder
ein Unicode-Zeilen- oder -Absatztrenner (U+2028, U+2029), erscheint im
Sync-Protokoll und in den eigenen Sätzen des Connectors im Worker-Log als `?`. Die
Datei wird trotzdem unter ihrem eigenen Namen synchronisiert. Die Zeile
`Failed to sync …` des Workers zeigt den Namen so, wie der Log-Filter ihn
maskiert, etwa `\n` für einen Zeilenumbruch.

Ein Sync verwendet eine Verbindung für die Auflistung und jeden Download. Der
Worker sendet alle 30 Sekunden ein Keepalive, und ein Server, der drei verpasst,
gilt als nicht mehr erreichbar. Eine Verbindung oder eine SFTP-Session, die der
Server zwischen zwei Dateien geschlossen hat, wird für die nächste Datei erneut
geöffnet.

Die Auflistung liest über diese Verbindung bis zu 4 Ordner gleichzeitig, mit etwa
4 Roundtrips pro Ordner. Eine kleine Datei kostet dann etwa 7 Roundtrips: zwei
Stats des Pfads, das Öffnen, zwei Stats der geöffneten Datei, ein Lesen und das
Schließen. Bei 80 ms pro Roundtrip ist das etwa eine halbe Sekunde pro Datei, vor
dem Parsen und dem Einbetten.

Ein Sync darf 4 Stunden laufen, sodass ein erster Import eines großen Baums über
eine langsame Verbindung mehr als einen Lauf brauchen kann. Jeder Lauf behält, was
die Läufe davor aufgenommen haben, und `new_only` und `update_only` überspringen
diese Dateien vor dem Download, sodass der nächste Lauf dort weitermacht, wo der
letzte aufgehört hat.

Ein fehlgeschlagener Verbindungsaufbau wird im selben Sync nicht erneut versucht.
Das gilt für einen abgelehnten Login, eine Zeitüberschreitung und eine Verbindung,
die verloren ging, bevor die SFTP-Session begann. Jede Datei danach schlägt mit
demselben Satz fehl, und es wird kein zweiter Login gesendet. Ein geändertes
Passwort kostet also einen abgelehnten Login pro Sync, nicht einen pro Datei.
Jeder Sync sendet trotzdem einen, sodass ein kurzer Zeitplan oder wiederholte
manuelle Syncs das Sperrlimit des Servers erreichen können. Wird der Login
abgelehnt, korrigieren Sie ihn oder halten Sie die Quelle an.

!!! warning "Ein falscher Login kann auf einem OpenSSH-Server die anderen Quellen stoppen"

    OpenSSH 9.8 und neuer lehnt Verbindungen von einer Adresse eine Zeit lang ab,
    nachdem von ihr aus Logins fehlgeschlagen sind (`PerSourcePenalties`,
    standardmäßig aktiv). Alle Quellen eines Deployments verbinden sich meist von
    einer einzigen Adresse aus, der Adresse der Worker. Eine Quelle mit falschem
    Passwort kann also die Syncs anderer Quellen auf demselben Server mit [die
    Verbindung ging verloren](#sftp-the-connection-to-was-lost) fehlschlagen
    lassen, obwohl deren eigene Logins richtig sind.

### Was einen SFTP-Sync begrenzt { #what-bounds-an-sftp-sync }

Feste Grenzen schützen den Worker vor einem defekten oder feindseligen Server. Sie
sind keine Einstellungen. Eine Auflistung, die eine der ersten sechs erreicht, hält
vorzeitig an, nennt die Grenze im Sync-Protokoll und entfernt nichts. Wählen Sie einen
engeren Ordner, bis ein Sync ohne die Meldung endet.

| Grenze | Wert | Wenn sie erreicht wird |
|---|---|---|
| Ordnerebenen unterhalb von `root_path` | 32 | Die Auflistung hält an |
| Ordner in der Auflistung | 10.000 | Die Ordner darüber hinaus werden nicht gelesen |
| Namen in der Auflistung, `.` und `..` eingeschlossen | 1.000.000 | Die Auflistung hält an |
| Dateien der konfigurierten Typen in der Auflistung | 100.000 | Die Auflistung hält an |
| Der Speicher, den die Pfade der aufgelisteten Dateien belegen | 64 MB, etwa ein Byte pro Zeichen bei einem ASCII-Pfad und bis zu vier bei anderen Zeichen | Die Auflistung hält an |
| Die ganze Auflistung | 30 Minuten | Die Auflistung hält an |
| Ein Name und ein Pfad | 255 Bytes und 4.096 Bytes | Der Name wird ausgelassen |
| Verbindungsaufbau und Login | 30 Sekunden | Der Verbindungsaufbau schlägt fehl |
| Eine SFTP-Anfrage | 30 Sekunden | Der Worker trennt die Verbindung. Eine Anfrage der Auflistung lässt den Sync fehlschlagen, eine Anfrage eines Downloads lässt diese Datei fehlschlagen |
| Der Download einer Datei | 10 Minuten | Die Datei schlägt fehl |
| Die Größe einer Datei | `MAX_UPLOAD_SIZE_MB`, standardmäßig 50 MB | Die Datei schlägt fehl |

### SFTP-Netzwerkregeln { #sftp-network-rules }

Der Host der Quelle wird aufgelöst und geprüft wie jede andere Adresse, die ein
Mandant wählt. Ein Host, der auf eine private, Loopback-, Link-Local-, CGNAT- oder
reservierte Adresse auflöst, wird beim Speichern der Quelle abgelehnt und beim Sync
erneut. Der Worker verbindet sich dann nur mit den Adressen, die die Prüfung
bestanden haben, und löst den Namen nie ein zweites Mal auf.

Ein Dateiserver im Firmennetz hat eine private Adresse. Ein Betreiber kann ihn in
`SYNC_ALLOWED_HOSTS` eintragen (siehe
[Projekteinstellungen](../configuration.md#project-settings)). Setzen Sie die
Liste für die API und für den Worker. Die API löst den Host auf und prüft ihn,
wenn eine Quelle gespeichert wird, und der Worker tut es erneut, wenn die Quelle
synchronisiert, also müssen auch beide den Namen auflösen können.

Ein gelisteter Host darf auf eine private oder CGNAT-Adresse auflösen. Er darf nie auf eine Loopback-,
Link-Local-, Multicast- oder Cloud-Metadaten-Adresse auflösen. Die Liste gilt für
das ganze Deployment: Die Quellen jeder Organisation erreichen jeden Port eines
gelisteten Hosts. Nur SFTP-Quellen lesen die Liste. Git- und Website-Quellen
behalten die einfache Adressprüfung.

Schreiben Sie jeden Eintrag wie den **Host** einer Quelle: nur einen Namen oder
eine IP-Adresse. Die Liste vergleicht den Host, den die Quelle eintippt, nicht die
Adresse, auf die er auflöst. Für eine Quelle, deren **Host** `files.corp.example`
ist, listen Sie `files.corp.example`: Ihre Adresse `10.0.0.5` allein lässt sie
nicht zu. Ein Eintrag mit einem Port, einem Schema, einem Pfad, einem
Bereich, einem Platzhalter oder einem abschließenden Punkt verhindert, dass die
Einstellungen geladen werden, und der Startfehler nennt den Eintrag. Ebenso ein
Eintrag, der als Loopback-, Link-Local- oder Metadaten-Host gelesen wird, sobald
er wie der Host einer Quelle vereinheitlicht ist, etwa `localhost` in Buchstaben
voller Breite.

Jede Organisation kann einen eigenen Login für einen gelisteten Host ablegen und
ihn dort versuchen. Eine Organisation ohne gültigen Login erfährt trotzdem, ob der
Host auf SSH antwortet und ob sein Host-Key passt. Listen Sie also nur einen
Server, dessen Konten die Zugriffskontrolle sind.

Der Worker verbindet sich direkt mit dem SSH-Port des Servers. Ein HTTP-Proxy wie
`HTTPS_PROXY` gilt dafür nicht.

Der Worker liest nichts von seiner eigenen SSH-Einrichtung:

- `~/.ssh/config`, `~/.ssh/known_hosts` und die Schlüssel in `~/.ssh`
- einen SSH-Agent unter `SSH_AUTH_SOCK`
- ein Kerberos-Ticket, dessen Schlüsselaustausch die Prüfung des Host-Keys
  überspringt

Der gepinnte Schlüssel ist der einzige Host-Key, dem der Worker vertraut. Der
Worker versucht nur die eine Login-Methode, die der Vault-Login enthält, und nie
keyboard-interactive. Ein Server, der den Worker ohne Login hereinlässt, wird
abgelehnt, weil dieser Server nichts geprüft hat.

Kompression ist abgeschaltet. asyncssh begrenzt nicht, auf welche Größe sich ein
komprimiertes Paket entpackt, sodass ein Server den Worker für jedes Paket
Hunderte Megabytes belegen lassen könnte. Ein Server, der nur komprimierte
Verbindungen anbietet, wird mit [dem Satz zum
Schlüsselaustausch](#sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher)
abgelehnt.

!!! note "Ein Worker braucht einen lokalen Benutzernamen"

    asyncssh ruft bei jedem Verbindungsaufbau `getpass.getuser()` auf, und nichts
    schaltet das ab. Der Name wird nicht gesendet. Das mitgelieferte Image hat
    einen passwd-Eintrag für seinen Benutzer. Ein Worker, der als Benutzer ohne
    passwd-Eintrag läuft, braucht die Variable `LOGNAME`. Das gilt für ein eigenes
    Image und für das mitgelieferte Image, wenn es mit einer anderen User-ID
    gestartet wird, etwa mit `docker run --user` oder einem Kubernetes-`runAsUser`.
    Ohne sie schlägt jeder SFTP-Sync fehl, und das Sync-Protokoll zeigt "The sync
    did not finish (OSError) - check the source's credential, then run it again.
    …". Das Credential ist nicht die Ursache.

## API-Referenz { #api-reference }

Alle Endpunkte für Sync-Quellen liegen unter `/api/v1/rag/sync/`. Das Auflisten
verlangt `collections:view`, und alles, was eine Quelle verändert, verlangt
`collections:edit` — in beiden Fällen bezogen auf die Collection, zu der die
Quelle gehört. Eine Adminrolle kommt darin nicht vor. Siehe
[wer eine Collection erreichen darf](../file-processing.md#who-may-reach-a-collection).

### CRUD für Sync-Quellen { #sync-sources-crud }

| Methode | Endpunkt | Beschreibung |
|--------|----------|-------------|
| `GET` | `/api/v1/rag/sync/sources` | Alle eingerichteten Sync-Quellen auflisten |
| `POST` | `/api/v1/rag/sync/sources` | Eine neue Sync-Quelle anlegen |
| `PATCH` | `/api/v1/rag/sync/sources/{id}` | Eine vorhandene Sync-Quelle ändern |
| `DELETE` | `/api/v1/rag/sync/sources/{id}` | Eine Sync-Quelle löschen |
| `POST` | `/api/v1/rag/sync/sources/{id}/trigger` | Einen Sync von Hand auslösen |

### Connectoren und Protokolle { #connectors-logs }

| Methode | Endpunkt | Beschreibung |
|--------|----------|-------------|
| `GET` | `/api/v1/rag/sync/connectors` | Verfügbare Connector-Typen samt Konfigurationsschema auflisten |
| `GET` | `/api/v1/rag/sync/logs` | Die Sync-Historie auflisten (nach `collection_name` filterbar) |

### Beispiel: eine Quelle über die API anlegen { #example-create-a-source-via-api }

```bash
curl -X POST http://localhost:8000/api/v1/rag/sync/sources \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Legal Drive",
    "connector_type": "gdrive",
    "collection_name": "legal",
    "config": {
      "folder_id": "1abc123def",
      "include_subfolders": true
    },
    "sync_mode": "new_only",
    "schedule_minutes": 120
  }'
```

### Beispiel: einen Sync über die API auslösen { #example-trigger-a-sync-via-api }

```bash
curl -X POST http://localhost:8000/api/v1/rag/sync/sources/{source_id}/trigger \
  -H "Authorization: Bearer $TOKEN"
```

### Beispiel: die Sync-Historie ansehen { #example-check-sync-history }

```bash
curl http://localhost:8000/api/v1/rag/sync/logs?limit=10 \
  -H "Authorization: Bearer $TOKEN"
```

### Beispiel: verfügbare Connectoren ermitteln { #example-discover-available-connectors }

```bash
curl http://localhost:8000/api/v1/rag/sync/connectors \
  -H "Authorization: Bearer $TOKEN"
```

Die Antwort enthält das `config_schema` jedes Connectors, aus dem das Frontend
seine dynamischen Formulare erzeugt. Es ist auch nützlich, um Integrationen
programmatisch zu bauen.

## Eine Quelle ändern { #updating-a-source }

Mit `PATCH` ändern Sie eine beliebige Teilmenge der Felder einer vorhandenen
Quelle:

```bash
curl -X PATCH http://localhost:8000/api/v1/rag/sync/sources/{source_id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "sync_mode": "full",
    "schedule_minutes": 60,
    "is_active": false
  }'
```

Änderbare Felder: `name`, `config`, `sync_mode`, `schedule_minutes`,
`is_active`, `collection_name`.

Setzen Sie `is_active` auf `false`, um eine Quelle anzuhalten, ohne sie zu
löschen.

## Sync-Vorgänge überwachen { #monitoring-sync-operations }

Jeder Sync erzeugt einen `SyncLog`-Eintrag mit den folgenden Feldern:

| Feld | Beschreibung |
|-------|-------------|
| `source` | Connector-Typ oder `"local"` für die Aufnahme über die CLI |
| `collection_name` | Die Ziel-Collection |
| `status` | `running`, `done` oder `error` |
| `mode` | `full`, `new_only` oder `update_only` |
| `total_files` | Anzahl der gefundenen Dateien |
| `ingested` | Erfolgreich aufgenommen (neu) |
| `updated` | Erfolgreich erneut aufgenommen (ersetzt) |
| `skipped` | Übersprungen: bereits vorhanden oder unverändert, neu in `update_only`, oder während des Laufs geändert oder gelöscht und dem nächsten Lauf überlassen |
| `failed` | Aufnahme fehlgeschlagen, einschließlich Seiten oder Dateien, die die Auflistung nicht lesen konnte, und Dokumenten, die nicht entfernt werden konnten |
| `removed` | Entfernt, weil die Quelle sie nicht mehr auflistet (siehe [was ein Sync entfernt](#what-a-sync-removes)) |
| `error_message` | Was schiefging oder warum nichts entfernt wurde. Ein Lauf kann `done` sein und trotzdem eine Meldung tragen, etwa wenn ein Crawl an seinem Seitenlimit angehalten hat oder wenn sich Dateien während des Laufs geändert haben oder gelöscht wurden und dem nächsten Lauf überlassen werden. Ob der Lauf fehlgeschlagen ist, steht in `status` |
| `started_at` | Wann der Sync begann |
| `completed_at` | Wann der Sync endete |

Die Protokolle sehen Sie in der Ausgabe der CLI oder über die API:

```bash
curl http://localhost:8000/api/v1/rag/sync/logs?collection_name=legal&limit=5 \
  -H "Authorization: Bearer $TOKEN"
```

## Eigene Connectoren ergänzen { #adding-custom-connectors }

Um einen neuen Connector-Typ zu ergänzen (etwa Notion, Confluence, Dropbox),
siehe [Einen Sync-Connector ergänzen](./add-sync-connector.md).

Die Kurzfassung:

1. Legen Sie in `app/services/rag/connectors/` eine Klasse an, die von
   `BaseSyncConnector` erbt.
2. Implementieren Sie `list_files()`, `_fetch()` und optional
   `validate_config()`.
3. Deklarieren Sie `SECRET_KIND` — welche Art von Vault-Secret sie
   authentifiziert — und ein `CONFIG_MODEL`, ein Pydantic-Modell, das sagt, wie
   die Dokumente zu finden sind. Das Credential ist nie eines seiner Felder.
4. Tragen Sie sie in `CONNECTOR_REGISTRY` in
   `app/services/rag/connectors/__init__.py` ein.

Einmal eingetragen, erscheint der Connector selbsttätig in der CLI, in der API
und in der Oberfläche.

## Fehlersuche { #troubleshooting }

### "No sync sources configured" { #no-sync-sources-configured }

Sie haben noch keine Quellen angelegt. Legen Sie eine mit `rag-source-add` (CLI)
oder `POST /api/v1/rag/sync/sources` (API) an.

### "Unknown connector type" { #unknown-connector-type }

Der angegebene Connector-Typ steht nicht in `CONNECTOR_REGISTRY`. Prüfen Sie die
verfügbaren Typen mit `rag-sources` oder `GET /api/v1/rag/sync/connectors`.
Google Drive (`gdrive`) ist verfügbar.
S3 (`s3`) ist verfügbar.
Website (`web`) ist verfügbar.
Git (`git`) ist verfügbar.
SFTP (`sftp`) ist verfügbar.

### Google Drive: "this source has no credential" { #google-drive-this-source-has-no-credential }

Die `secret_id` der Quelle ist leer, oder das benannte Vault-Secret wurde
gelöscht. Legen Sie das Dienstkonto-JSON im Vault ab und wählen Sie es im
Credential-Schritt der Quelle aus — `GOOGLE_DRIVE_CREDENTIALS_FILE` tritt nicht
an seine Stelle, und nur der CLI-Befehl `rag-sync-gdrive` liest diese
Einstellung.

### "A Google Drive source needs a service account credential" { #a-google-drive-source-needs-a-service-account-credential }

Die `secret_id` benennt ein Credential der falschen Art — etwa ein
AWS-Schlüsselpaar. Eine Drive-Quelle nimmt ein `gcp_service_account`, eine
S3-Quelle ein `aws_credentials`-Paar; der Assistent bietet nur die passenden an,
sodass dies über die API erreichbar ist.

### Google Drive: "folder ID may contain only letters, digits, '-' and '\_'" { #google-drive-folder-id-may-contain-only-letters-digits-and-_ }

Der Wert ist keine Drive-Folder-ID. Nehmen Sie sie aus der Ordner-URL: Sie ist
das letzte Segment, und nichts sonst aus dieser URL gehört in das Feld.

### Google Drive: "Cannot access folder" { #google-drive-cannot-access-folder }

Vergewissern Sie sich, dass Sie den Ordner für die E-Mail-Adresse des
Dienstkontos freigegeben haben. Das Dienstkonto braucht mindestens
Viewer-Zugriff.

### S3: "Cannot access bucket" { #s3-cannot-access-bucket }

Prüfen Sie, ob `S3_RAG_ACCESS_KEY`, `S3_RAG_SECRET_KEY` und `S3_RAG_ENDPOINT` in
der `.env` richtig gesetzt sind. Achten Sie bei MinIO darauf, dass der Endpunkt
den Port enthält (etwa `http://localhost:9000`).

### Website: "resolves to private/internal address" { #website-resolves-to-privateinternal-address }

Die Start-URL oder die Sitemap zeigt in das Netz des Deployments, oder ihr Name
löst dorthin auf. Eine Website-Quelle liest nur öffentliche Adressen. Um eine
interne Website zu indexieren, veröffentlichen Sie ihre Seiten an einem
öffentlichen Ort oder laden Sie die Dateien direkt hoch.

### Website: "The site's robots.txt could not be read, so it was not crawled" { #website-the-sites-robotstxt-could-not-be-read-so-it-was-not-crawled }

`/robots.txt` auf dem Host der Start-URL lief dreimal hintereinander in eine
Zeitüberschreitung oder antwortete mit einem Serverfehler (5xx). Der Crawler rät
nicht, was eine unerreichbare robots.txt erlauben würde, also bricht der Lauf ab.
Eine fehlende robots.txt (404) oder eine verbotene (403) bedeutet keine Regeln,
und der Crawl läuft weiter.

### Website: "The start URL … did not lead to an HTML page" { #website-the-start-url-did-not-lead-to-an-html-page }

Die Start-URL antwortete mit 404, leitete auf einen anderen Host oder aus
`path_prefix` heraus weiter oder lieferte etwas anderes als HTML. Öffnen Sie sie
in einem Browser und nehmen Sie dann die Adresse, bei der sie landet, als
`root_url`.

### Website: "robots.txt does not allow the start URL" { #website-robotstxt-does-not-allow-the-start-url }

Die Website bittet Crawler, diesen Pfad zu meiden. Wählen Sie eine Start-URL, die
die Website erlaubt, oder bitten Sie den Besitzer der Website, `AgenticOS-Crawler`
zuzulassen.

### Website: "… answered HTTP 403" oder "… could not be reached" { #website-answered-http-403-or-could-not-be-reached }

Die Seite verlangt einen Login, oder die Website hat den Crawler abgewiesen. War
die Antwort eine Zeitüberschreitung, 429 oder 5xx, schlug sie nach drei Versuchen
fehl. Jede solche Seite zählt als fehlgeschlagene Datei. In diesem Lauf wird
nichts entfernt, weil die Seiten dahinter nicht gesehen wurden.

### "The source could not be listed completely, so documents it may no longer hold were kept" { #the-source-could-not-be-listed-completely-so-documents-it-may-no-longer-hold-were-kept }

Die Auflistung brach vorzeitig ab: Ein Crawl erreichte `max_pages`, einige
Seiten konnten nicht gelesen werden, ein SharePoint-Ordner ließ sich nicht
auflisten, oder eine SFTP-Auflistung erreichte eine Grenze oder konnte einen
Ordner oder einen Eintrag nicht lesen. Was gefunden wurde, ist aufgenommen, und
nichts wurde entfernt. Bei einer Website erhöhen Sie `max_pages` oder grenzen den
Crawl mit `path_prefix` ein, bis ein Lauf ohne diese Meldung endet. Bei SharePoint
und SFTP siehe die Einträge zur Auflistung weiter unten.

### Git: "The repository refused the source's token" { #git-the-repository-refused-the-sources-token }

Das Token ist abgelaufen, wurde widerrufen oder kann dieses Repository nicht
lesen. Stellen Sie ein neues aus, wie unter
[Git-Repository einrichten](#git-repository-setup) beschrieben, und ersetzen Sie
den Wert des Vault-Secrets, das die Quelle nutzt; jede Quelle mit diesem Secret
übernimmt ihn bei ihrem nächsten Sync.

### Git: "The repository was not found, or the source's token cannot see it" { #git-the-repository-was-not-found-or-the-sources-token-cannot-see-it }

Prüfen Sie zuerst die Clone-URL. Ein privates Repository antwortet einem Token,
das es nicht lesen kann, mit *not found* statt mit *forbidden*, deshalb zeigt
sich ein fine-grained Token, das für ein anderes Repository ausgestellt wurde,
auf diese Weise.

### Git: "The repository has no branch named …" { #git-the-repository-has-no-branch-named }

Das Feld `branch` nennt einen Branch, den das Repository nicht hat. Die Vorgabe
ist `main`; der Standard-Branch eines älteren Repositorys heißt womöglich
`master`.

### Git: "… is … MB, and a synced file may be at most … MB" { #git-is-mb-and-a-synced-file-may-be-at-most-mb }

Eine Datei, auf die die Include-Muster passen, ist größer als die Dokumentgrenze
der Wissensbasis. Schränken Sie `include` oder `path_prefix` so ein, dass die
Datei wegfällt. Für diesen Sync wurde nichts geschrieben.

### Git: "… over the … MB one sync may check out" { #git-over-the-mb-one-sync-may-check-out }

Alle Dateien, auf die die Include-Muster passen, sind zusammen größer als 512 MB.
Schränken Sie `include` oder `path_prefix` ein, oder teilen Sie das Repository auf
mehrere Quellen mit je eigenem Präfix auf.

### Git: "This token was added for …, and the repository is on …" { #git-this-token-was-added-for-and-the-repository-is-on }

Das Repository der Quelle liegt auf einem anderen Host als dem, mit dem ihr Token
abgelegt wurde. Entweder ist die URL falsch, oder die Quelle braucht ein Token,
das für diesen Host abgelegt wurde. An den Host des Repositorys wurde nichts
gesendet.

### Git: "A Git source needs a Git access token" { #git-a-git-source-needs-a-git-access-token }

Die Quelle nennt ein Secret einer anderen Art, etwa einen API key. Legen Sie das
Token als **Git-Zugriffstoken** mit seinem Host ab und wählen Sie dieses aus.

### "Another sync of this source is still running" { #another-sync-of-this-source-is-still-running }

Ein Lauf wurde ausgelöst, während ein anderer Lauf derselben Quelle noch im Gang
war, also ist er nicht gestartet. Der laufende Lauf endet regulär; lösen Sie
danach erneut aus, falls sich die Quelle in der Zwischenzeit geändert hat.

### Git: "… resolves to a private address" { #git-resolves-to-a-private-address }

Der Host des Repositorys löst innerhalb des Netzwerks des Deployments auf, also
wird die Quelle abgelehnt. Ein selbst betriebener Git-Server unter einer internen
Adresse ist für eine Sync-Quelle nicht erreichbar.

### Git: "git is not installed on this worker" { #git-git-is-not-installed-on-this-worker }

Der Worker läuft aus einem Image ohne `git`. Das mitgelieferte
`backend/Dockerfile` installiert es; einem eigenen Image muss es hinzugefügt
werden.

### Git: ein Sync endete ohne verarbeitete Dateien { #git-a-sync-finished-with-no-files-processed }

Der Head-Commit des Branches ist derjenige, den der letzte saubere Lauf unter
derselben Konfiguration gelesen hat, also gab es nichts zu tun. Stellen Sie die
Quelle für einen Lauf auf `full`, um ungeachtet dessen wieder alles zu lesen.

### SharePoint: "Microsoft Entra refused the app registration's credentials (…)" { #sharepoint-microsoft-entra-refused-the-app-registrations-credentials }

Microsoft Entra hat die App nicht angemeldet. `invalid_client` bedeutet meist,
dass das Client Secret abgelaufen ist oder der Vault die ID des Secrets statt
seines Werts enthält. `unauthorized_client` oder `invalid_request` bedeutet meist,
dass die Tenant-id oder die Client-id falsch ist. Prüfen Sie die drei Werte gegen
die **Overview** und **Certificates & secrets** der App und ersetzen Sie sie im
Vault-Secret.

### SharePoint: "Microsoft Graph did not accept the app's token for …" { #sharepoint-microsoft-graph-did-not-accept-the-apps-token-for }

Die App hat sich angemeldet, aber ihr Token trägt keine Microsoft-Graph-Berechtigung.
Fügen Sie die Berechtigung `Sites.Selected` vom Typ **application** hinzu, keine
delegierte, und erteilen Sie die Administratorzustimmung, wie in
[Schritt 2](#2-grant-it-one-site-not-the-tenant) beschrieben.

### SharePoint: "Microsoft Graph denied the app access to … (accessDenied)" { #sharepoint-microsoft-graph-denied-the-app-access-to-accessdenied }

Die App hat `Sites.Selected`, aber keine Gewährung auf dieser Site. Gewähren Sie
ihr `read` auf der Site, wie in [Schritt 2](#2-grant-it-one-site-not-the-tenant)
beschrieben. Nennt die Meldung einen Ordner unterhalb des Ordners der Quelle, hat
dieser Ordner eigene Berechtigungen, die die App ausschließen. Der Rest der
Bibliothek wurde aufgenommen, und nichts wurde entfernt.

### SharePoint: "There is no SharePoint site at …, or the app cannot see it" { #sharepoint-there-is-no-sharepoint-site-at-or-the-app-cannot-see-it }

Prüfen Sie zuerst die Site-URL: Sie ist die eigene Adresse der Site, etwa
`https://contoso.sharepoint.com/sites/HR`, nicht eine Bibliothek oder Seite darin.
Mit `Sites.Selected` kann auch eine Site, auf der die App keine Gewährung hat, so
antworten.

### SharePoint: "The site has no document library named …" { #sharepoint-the-site-has-no-document-library-named }

Die Meldung listet die Bibliotheken auf, die die App sehen kann. Kopieren Sie
einen dieser Namen in `library`, oder lassen Sie `library` leer für die
Standardbibliothek der Site. Die Standardbibliothek ist in der Sprache der Site
benannt, etwa *Documents* oder *Dokumente*.

### SharePoint: "The library has no folder …" oder "… is a file, not a folder" { #sharepoint-the-library-has-no-folder-or-is-a-file-not-a-folder }

`folder_path` nennt keinen Ordner in der Bibliothek. Er ist relativ zur obersten
Ebene der Bibliothek, etwa `Policies/HR`, und wiederholt den Namen der Bibliothek
nicht.

### SharePoint: "The folder … could not be listed: …" { #sharepoint-the-folder-could-not-be-listed }

Ein Ordner unterhalb des Ordners der Quelle konnte nicht gelesen werden. Der Grund
folgt nach dem Doppelpunkt. Die anderen Ordner wurden aufgenommen, und in diesem
Lauf wurde nichts entfernt.

### SharePoint: "Microsoft 365 stayed unavailable or kept throttling … after 4 attempts" { #sharepoint-microsoft-365-stayed-unavailable-or-kept-throttling-after-4-attempts }

Graph drosselte oder scheiterte auch nach vier Versuchen noch. Der nächste Sync
beginnt von vorn. Passiert das bei jedem Sync, planen Sie die Quelle seltener ein,
oder teilen Sie eine sehr große Bibliothek auf mehrere Quellen mit je eigenem
`folder_path` auf.

### SharePoint: "A SharePoint source needs a Microsoft Entra app credential" { #sharepoint-a-sharepoint-source-needs-a-microsoft-entra-app-credential }

Die Quelle nennt ein Secret einer anderen Art, etwa einen API key. Legen Sie die
App als **Microsoft Entra app** im Vault ab und wählen Sie diese aus.

### SharePoint: ein Sync endete ohne verarbeitete Dateien { #sharepoint-a-sync-finished-with-no-files-processed }

In der Bibliothek hat sich seit dem letzten sauberen Lauf unter derselben
Konfiguration nichts geändert, also gab es nichts zu tun. Stellen Sie die Quelle
für einen Lauf auf `full`, um ungeachtet dessen wieder alles zu lesen.

### SFTP: "Paste only the key type and the key, without the host name or the options in front of them." { #sftp-paste-only-the-key-type-and-the-key-without-the-host-name-or-the-options-in-front-of-them }

**Host key** enthält eine Zeile aus `ssh-keyscan` oder `known_hosts` mit dem
Hostnamen vor dem Schlüssel, oder eine `authorized_keys`-Zeile mit Optionen davor.
Entfernen Sie das erste Feld. Für die Datei aus
[Schritt 1](#1-get-the-servers-host-key) erledigt das
`cut -d' ' -f2- host_key.txt`.

### SFTP: "Paste the server's public host key as one line, with its type first, …" { #sftp-paste-the-servers-public-host-key-as-one-line-with-its-type-first }

**Host key** enthält etwas anderes als eine Zeile mit einem öffentlichen Schlüssel:
einen Fingerabdruck aus `ssh-keygen -lf`, zwei Zeilen, einen PEM-Block oder einen
beschädigten Schlüssel. Fügen Sie den Schlüssel selbst ein, etwa
`ssh-ed25519 AAAAC3Nz...`. Ein PEM-Block wird abgelehnt, bevor er gelesen wird,
denn ein versehentlich eingefügter privater Schlüssel darf nicht gelesen werden.

### SFTP: "The host key must be of type ssh-ed25519, ssh-ed448, …" { #sftp-the-host-key-must-be-of-type-ssh-ed25519-ssh-ed448 }

Der Schlüssel hat einen Typ, den der Connector nicht pinnt, etwa `ssh-dss`, ein
Host-Zertifikat oder ein Security Key. Holen Sie vom Server einen Schlüssel eines
der genannten Typen, etwa mit `ssh-keyscan -t ed25519`, `-t ecdsa` oder `-t rsa`.

### SFTP: "The host is a host name or an IP address alone, …" oder "Put the port in the Port field, …" { #sftp-the-host-is-a-host-name-or-an-ip-address-alone-or-put-the-port-in-the-port-field }

**Host** enthält mehr als den Host, etwa `sftp://sftp.example.com/outgoing`,
`sync@sftp.example.com` oder `sftp.example.com:2222`. Tippen Sie nur den Namen oder
die Adresse ein. Tragen Sie den Port in **Port** ein und den Ordner in **Folder**.

Ein abschließender Punkt wird ebenfalls abgelehnt: Entfernen Sie ihn. Ein Name
enthält nur Buchstaben, Ziffern, Bindestriche, Unterstriche und Punkte, etwa
`sftp_server` oder `files-01.corp.example`.

### SFTP: ein Ordner oder ein Dateityp, der beim Speichern der Quelle abgelehnt wird { #sftp-a-folder-or-a-file-type-refused-when-the-source-is-saved }

- "The folder is an absolute path on the server, e.g. /outgoing." **Folder**
  beginnt nicht mit `/`. Schreiben Sie den vollen Pfad, wie das Konto ihn auf dem
  Server sieht.
- "The folder may not hold '..'. Name the folder itself." Schreiben Sie den eigenen
  Pfad des Ordners, ohne `..`.
- "The folder may not hold a line break or another control character." Entfernen
  Sie das Zeichen. Beim Einfügen kann eines hineingeraten, auch ein Unicode-Zeilen-
  oder -Absatztrenner (U+2028, U+2029).
- "The folder may be at most 1024 characters long." Wählen Sie einen Ordner mit
  einem kürzeren Pfad.
- "'…' is not a file extension, e.g. .pdf." Ein Eintrag in **File types** besteht
  nicht aus einem Punkt und 1 bis 10 Buchstaben oder Ziffern.
- "At least one file type is needed." **File types** ist leer. Ergänzen Sie
  mindestens einen Typ, etwa `.pdf`.
- "At most 100 file types can be listed." **File types** hat mehr als 100
  Einträge. Behalten Sie die Typen, die der Parser der Collection liest.
- "A file type is at most 16 characters, e.g. .pdf." Ein Eintrag in **File types**
  ist länger als 16 Zeichen. Schreiben Sie nur die Endung, etwa `.pdf`.

Die **File types** einer SharePoint-Quelle liefern dieselben letzten vier Sätze.
Die beiden Grenzen gelten, wenn eine Quelle gespeichert wird, und zählen die Typen,
nachdem leere und doppelte Typen entfernt wurden. Eine Quelle, die mit mehr Typen
gespeichert ist, synchronisiert weiter und wird erst abgelehnt, wenn sie erneut
gespeichert wird.

### SFTP: "Blocked: '…' resolves to private/internal address '…'. …" { #sftp-blocked-resolves-to-privateinternal-address }

Der Host löst innerhalb des Netzwerks des Deployments auf. Ein Betreiber kann einen
Dateiserver im Firmennetz in `SYNC_ALLOWED_HOSTS` eintragen, wie unter
[SFTP-Netzwerkregeln](#sftp-network-rules) beschrieben. Ein Host, der auf eine
Loopback-, Link-Local- oder Cloud-Metadaten-Adresse auflöst, bleibt abgelehnt, auch
wenn er gelistet ist. Bei einem Host, der als IP-Adresse eingegeben wurde, lautet
der Satz "Blocked: '…' resolves to a private/internal address. SSRF protection does
not allow requests to internal networks." Ursache und Abhilfe sind dieselben.

Listen Sie den Host so, wie die Quelle ihn nennt, nicht die Adresse aus dem Satz.
Setzen Sie `SYNC_ALLOWED_HOSTS` für die API und für den Worker. Wird eine Quelle
beim Speichern abgelehnt, fehlt ihr Host in der Liste der API. Wird sie erst beim
Sync abgelehnt, fehlt er in der Liste des Workers.

"Blocked: unable to resolve hostname …" bedeutet, dass der Host überhaupt nicht
aufgelöst werden konnte. Prüfen Sie seine Schreibweise. Die API löst den Host
beim Speichern der Quelle auf und der Worker beim Sync der Quelle, also müssen
beide den Namen auflösen können.

### "A credential does not go in a source's configuration" { #a-credential-does-not-go-in-a-sources-configuration }

`config` enthält ein Feld, das ein Credential ist, etwa `password`, `private_key`
oder `passphrase` bei einer SFTP-Quelle. Legen Sie das Credential im Vault ab und
setzen Sie die `secret_id` der Quelle darauf.

### SFTP: "An SSH login needs a private key or a password, and not both." { #sftp-an-ssh-login-needs-a-private-key-or-a-password-and-not-both }

Der Login hat sowohl einen privaten Schlüssel als auch ein Passwort, oder keines
von beiden. Geben Sie ihm eine Art, sich anzumelden: einen privaten Schlüssel, mit
seiner Passphrase, falls er eine hat, oder ein Passwort. "A passphrase goes with a
private key." bedeutet, dass der Login eine Passphrase und ein Passwort hat.
Entfernen Sie die Passphrase.

### SFTP: "The private key could not be read. …" { #sftp-the-private-key-could-not-be-read }

Der Vault konnte den Schlüssel beim Speichern des Logins nicht lesen. Fügen Sie
die ganze Schlüsseldatei ein, von ihrer `-----BEGIN`-Zeile bis zu ihrer
`-----END`-Zeile. Ist der Schlüssel verschlüsselt, geben Sie auch seine Passphrase
an. "The private key could not be read with this passphrase." bedeutet, dass die
Passphrase falsch ist oder dass das Eingefügte kein Schlüssel ist, den der Vault
liest.

Die erste Zeile `-----BEGIN … PRIVATE KEY-----` muss eine von
`OPENSSH PRIVATE KEY`, `PRIVATE KEY`, `ENCRYPTED PRIVATE KEY`, `RSA PRIVATE KEY`,
`EC PRIVATE KEY` oder `DSA PRIVATE KEY` sein. Ein Schlüssel in binärer DER-Form
wird abgelehnt, ebenso ein PKCS#8-Schlüssel unter einem PEM-Header
`Proc-Type: 4,ENCRYPTED`. Exportieren Sie einen solchen Schlüssel erneut in einer
dieser Formen. Einen Schlüssel, den `ssh-keygen` liest, schreibt
`ssh-keygen -p -f <key file>` im OpenSSH-Format. Ein PuTTY-Schlüssel (`.ppk`)
wird ebenfalls abgelehnt, und `ssh-keygen` kann ihn nicht lesen. Exportieren Sie
ihn im OpenSSH-Format mit PuTTYgen (**Conversions** → **Export OpenSSH key**)
oder mit `puttygen <key>.ppk -O private-openssh -o <key file>`.

### SFTP: "The private key is encrypted with too many key derivation rounds. …" { #sftp-the-private-key-is-encrypted-with-too-many-key-derivation-rounds }

Ein verschlüsselter Schlüssel gibt an, wie viel Arbeit die Prüfung seiner
Passphrase kostet. Der Vault lehnt einen Schlüssel ab, der mehr als 128
bcrypt-Runden (OpenSSH) verlangt, oder mehr als 1.000.000 Iterationen oder einen
abgeleiteten Schlüssel von mehr als 64 Bytes (PKCS#8). Er liest das aus dem
Schlüssel, bevor er irgendetwas entschlüsselt. Verschlüsseln Sie den Schlüssel
erneut mit weniger Runden, etwa mit `ssh-keygen -p -a 16 -f <key file>`, und
fügen Sie ihn erneut ein.

### SFTP: "The private key takes too long to open. …" { #sftp-the-private-key-takes-too-long-to-open }

Der vollständige Satz lautet "The private key takes too long to open. Use an
Ed25519 key, or an RSA key of at most 4096 bits.". Der Vault öffnet jeden
Schlüssel in einem eigenen Prozess und beendet ihn nach 5 Sekunden. Ein
Schlüssel, dessen verschlüsselter Teil mehr Arbeit verlangt, als in diese Zeit
passt, oder ein sehr ausgelasteter API-Host führt zu diesem Satz. Erzeugen Sie einen
Ed25519-Schlüssel, wie in [Schritt 2](#2-add-an-ssh-login-to-the-vault)
beschrieben, und speichern Sie den Login erneut.

### SFTP: "The private key is an RSA key of more than 4096 bits. …" { #sftp-the-private-key-is-an-rsa-key-of-more-than-4096-bits }

Der vollständige Satz lautet "The private key is an RSA key of more than 4096
bits. Use an Ed25519 key, or an RSA key of at most 4096 bits.". Die Zeit für die
Prüfung eines RSA-Schlüssels wächst schnell mit seiner Größe, deshalb lehnt der
Vault einen Schlüssel über 4096 Bit ab, ohne ihn zu prüfen. Erzeugen Sie einen
Ed25519-Schlüssel oder einen RSA-Schlüssel mit höchstens 4096 Bit, und fügen Sie
seine öffentliche Hälfte zu den `authorized_keys` des Kontos hinzu.

### SFTP: "The private key is an RSA key whose two primes are not of the same size. …" { #sftp-the-private-key-is-an-rsa-key-whose-two-primes-are-not-of-the-same-size }

Der vollständige Satz lautet "The private key is an RSA key whose two primes are
not of the same size. Use an Ed25519 key, or an RSA key that ssh-keygen made.".
Der Vault lehnt einen RSA-Schlüssel, dessen zwei Primzahlen sich in der Größe um
mehr als 16 Bit unterscheiden, ab, ohne ihn zu prüfen. `ssh-keygen` und OpenSSL
erzeugen beide Primzahlen gleich groß, und die Prüfung eines Schlüssels mit einer
großen Primzahl dauert lange. Erzeugen Sie mit `ssh-keygen` einen neuen
Schlüssel, wie in [Schritt 2](#2-add-an-ssh-login-to-the-vault) beschrieben, und
fügen Sie seine öffentliche Hälfte zu den `authorized_keys` des Kontos hinzu.

### SFTP: "The private key is held on a security key, which a sync cannot use to log in. …" { #sftp-the-private-key-is-held-on-a-security-key-which-a-sync-cannot-use-to-log-in }

Der vollständige Satz lautet "The private key is held on a security key, which a
sync cannot use to log in. Use an Ed25519 key without a security key.". Die
Schlüsseldatei ist ein `sk-`-Schlüssel: ein Verweis auf einen Hardware-Schlüssel,
etwa einer, der mit `ssh-keygen -t ed25519-sk` erzeugt wurde. Der Worker hat
keinen Hardware-Schlüssel, kann damit also nicht signieren. Erzeugen Sie einen
Ed25519-Schlüssel mit `ssh-keygen -t ed25519`, wie in
[Schritt 2](#2-add-an-ssh-login-to-the-vault) beschrieben.

### SFTP: "Other private keys are being checked now. Try again in a moment." { #sftp-other-private-keys-are-being-checked-now-try-again-in-a-moment }

Im selben API-Prozess werden bereits zwei private Schlüssel geprüft, und der Vault
prüft höchstens zwei gleichzeitig. Das Speichern wird sofort abgelehnt, mit Status
503 und dem Code `SERVICE_BUSY`, damit keine Anfrage wartet, während sie eine
Datenbankverbindung hält. Speichern Sie den Login nach einigen Sekunden erneut.

### SFTP: "The private key is too long. Paste only the key file, at most 16,384 characters." { #sftp-the-private-key-is-too-long-paste-only-the-key-file-at-most-16384-characters }

Das Feld **Private key** enthält mehr als 16.384 Zeichen. Die Schlüsseldatei eines
Ed25519-Schlüssels hat etwa 400 Zeichen, die eines RSA-Schlüssels mit 4096 Bit
etwa 3.300. Das Eingefügte enthält also mehr als den Schlüssel, oder es ist kein
Schlüssel. Fügen Sie nur die Datei des privaten Schlüssels ein, von ihrer
`-----BEGIN`-Zeile bis zu ihrer `-----END`-Zeile.

### SFTP: "The passphrase is too long: at most 1,024 characters." { #sftp-the-passphrase-is-too-long-at-most-1024-characters }

Das Feld **Key passphrase** enthält mehr als 1.024 Zeichen. Fügen Sie nur die
Passphrase ein, die den Schlüssel öffnet. Eine Passphrase, die tatsächlich länger
ist, lässt sich nicht speichern: Verschlüsseln Sie den Schlüssel erneut mit einer
kürzeren, mit `ssh-keygen -p -f <key file>`.

### SFTP: "The password is too long: at most 1,024 characters." { #sftp-the-password-is-too-long-at-most-1024-characters }

Das Feld **Password** enthält mehr als 1.024 Zeichen. Fügen Sie nur das Passwort
ein. Für einen Server, dessen Passwort länger ist, verwenden Sie stattdessen einen
Schlüssel, wie in [Schritt 2](#2-add-an-ssh-login-to-the-vault) beschrieben.

### SFTP: "The private key could not be checked. Try again later." { #sftp-the-private-key-could-not-be-checked-try-again-later }

Der Prozess, der den Schlüssel öffnet, endete mit einem Fehler und gab keine
Antwort. Das sagt nichts über den Schlüssel aus. Die API antwortet mit Status 500.
Speichern Sie den Login nach kurzer Zeit erneut. Schlägt es wieder fehl, enthält
das API-Log die Zeile `ssh_key_check_failed`. Der Exit-Status des Prozesses steht
im Feld `returncode` des Eintrags, das nur ein Log-Handler zeigt, der zusätzliche
Felder anzeigt, etwa Logfire. Der API-Host muss einen Python-Prozess starten
können, und dieser Prozess muss asyncssh importieren können.

### SFTP: "The host is a host name or an IP address with an optional port, …" { #sftp-the-host-is-a-host-name-or-an-ip-address-with-an-optional-port }

**Host** im Login ist nicht ein einzelner Host mit optionalem Port. Schreiben Sie
einen Namen oder eine IP-Adresse, dann `:` und den Port, wenn er nicht 22 ist,
etwa `sftp.example.com` oder `[2001:db8::7]:2222`. Eine IPv6-Adresse steht in
eckigen Klammern. Ein Schema, ein Pfad, ein Platzhalter oder ein Leerzeichen wird
abgelehnt. Ein Name enthält nur Buchstaben, Ziffern, Bindestriche, Unterstriche
und Punkte.

### SFTP: "This SFTP source has no credential. …" { #sftp-this-sftp-source-has-no-credential }

Die `secret_id` der Quelle ist leer, der Login, den sie nannte, wurde gelöscht,
oder der Worker konnte den Login nicht öffnen. Der Worker prüft die Form des
Schlüssels jedes Mal, wenn er den Login öffnet, sodass auch ein gespeicherter
Schlüssel, der diese Prüfung nicht mehr besteht, diesen Satz liefert. Das
Worker-Log enthält dann `sync_source_secret_unusable`. Legen Sie einen SSH-Login im
Vault ab, wie in [Schritt 2](#2-add-an-ssh-login-to-the-vault) beschrieben, oder
speichern Sie den Schlüssel und seine Passphrase erneut im Login. Wählen Sie ihn
dann im Credential-Schritt der Quelle aus.

### SFTP: "An SFTP source needs an SSH login, and the credential it names is not one." { #sftp-an-sftp-source-needs-an-ssh-login-and-the-credential-it-names-is-not-one }

Eine Quelle, die ein Credential einer anderen Art nennt, etwa einen API key, wird
beim Speichern abgelehnt, mit "A SFTP server source needs a ssh_credentials
credential, and that one is …". Das gilt für den Assistenten, die API und
`rag-source-add`. Wählen Sie einen **SSH login**. Der Satz in der Überschrift ist
die eigene Prüfung des Connectors zur Sync-Zeit, und eine Quelle, die mit einem
SSH-Login gespeichert wurde, liefert ihn nicht.

### SFTP: "This SSH login was added for …, and the source names …" { #sftp-this-ssh-login-was-added-for-and-the-source-names }

Host oder Port der Quelle sind nicht die, für die der Login abgelegt wurde.
Entweder ist **Host** oder **Port** falsch, oder die Quelle braucht einen Login,
der für diesen Host abgelegt wurde. Ein Login, der ohne Port abgelegt wurde, wird
nur auf Port 22 verwendet. An den Host der Quelle wurde nichts gesendet.

### SFTP: "… did not prove that it holds the host key in this source's settings. …" { #sftp-did-not-prove-that-it-holds-the-host-key-in-this-sources-settings }

Der Server antwortete mit einem anderen Host-Key als dem in **Host key**. Der
Schlüssel des Servers hat sich geändert, etwa weil der Server neu installiert
wurde, oder der Worker hat nicht den echten Server erreicht. Vergleichen Sie den
Fingerabdruck erneut mit dem des Administrators, wie in
[Schritt 1](#1-get-the-servers-host-key) beschrieben, bevor Sie den Schlüssel
ändern. Es wurde kein Login gesendet.

### SFTP: "… and this worker could not agree on a host key or a cipher. …" { #sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher }

Prüfen Sie bei einem ersten Sync zuerst den Typ des Host-Keys. Der Worker fragt den
Server nur nach einem Schlüssel des Typs in **Host key**, und Apache MINA SSHD
antwortet so, wenn es keinen Schlüssel dieses Typs hat. Holen Sie den Schlüssel des
Servers in einem anderen Typ, wie in [Schritt 1](#1-get-the-servers-host-key)
beschrieben. Ist der Typ richtig, nimmt der Server keine der Ciphers an, die der
Worker anbietet. Fragen Sie den Administrator, welche er zulässt. Ein Server, der
nur komprimierte Verbindungen anbietet, liefert ebenfalls diesen Satz, weil der
Worker Kompression nie einschaltet.

### SFTP: "The connection to … was lost." { #sftp-the-connection-to-was-lost }

Prüfen Sie bei einem ersten Sync zuerst den Typ des Host-Keys. OpenSSH und SFTPGo
beenden die Verbindung so, wenn sie keinen Schlüssel des Typs in **Host key**
haben.

OpenSSH 9.8 und neuer lehnt außerdem jede Verbindung von einer Adresse ab, von der
aus einige Logins fehlgeschlagen sind (`PerSourcePenalties`). Alle Quellen eines
Deployments verbinden sich meist von einer einzigen Adresse aus, der Adresse der
Worker. Eine Quelle mit falschem Passwort kann also die Syncs anderer Quellen auf
demselben Server eine Zeit lang mit diesem Satz fehlschlagen lassen.

Suchen Sie eine Quelle auf demselben Host, deren letzter Sync "The login to … did
not complete." ergab. Korrigieren Sie ihren Login, oder halten Sie sie an.
Synchronisieren Sie dann erneut, wenn die Sperre abgelaufen ist: Standardmäßig
wächst sie auf höchstens 10 Minuten. Das Server-Log zeigt die Sperre als
`srclimit_penalise`. Der Administrator des Servers kann die Adresse der Worker
auch mit `PerSourcePenaltyExemptList` ausnehmen. Das hebt die Sperre für die
Quellen jeder Organisation auf dem Deployment auf, korrigieren Sie also zuerst den
Login.

Andernfalls haben das Netzwerk oder der Server die Verbindung beendet oder
zurückgesetzt oder die SFTP-Session beendet, oder der Server hat auf drei
Keepalives nicht geantwortet. Eine Auflistung, die die Verbindung verliert, lässt den ganzen Sync fehlschlagen, und
dieser Lauf entfernt nichts. Ein Download, der sie verliert, lässt diese Datei
fehlschlagen, und die nächste Datei öffnet eine neue Verbindung.

### SFTP: "… closed the connection." { #sftp-closed-the-connection }

Prüfen Sie bei einem ersten Sync zuerst den Typ des Host-Keys. ProFTPD beendet die
Verbindung so, wenn es keinen Schlüssel des Typs in **Host key** hat. Andernfalls
hat der Server die Verbindung beim Aufbau beendet, etwa weil er keine weiteren
Sessions zulässt oder die Adresse des Workers ablehnt. Das Worker-Log enthält den
eigenen Grund des Servers.

### SFTP: "The login to … as … did not complete. …" { #sftp-the-login-to-as-did-not-complete }

Der Server hat den Benutzernamen, den Schlüssel oder das Passwort abgelehnt.
Prüfen Sie sie im Vault. Prüfen Sie bei einem Schlüssel, ob der Administrator
seine öffentliche Hälfte zu den `authorized_keys` des Kontos hinzugefügt hat. Ein
Server, der ein Passwort nur über einen keyboard-interactive Login annimmt, lehnt
einen Passwort-Login ab, verwenden Sie dort also einen Schlüssel. Ein Server kann
auch den Typ des Schlüssels ablehnen. Apache MINA SSHD ohne seine EdDSA-Bibliothek
lehnt einen Ed25519-Schlüssel ab, versuchen Sie dort also einen ECDSA-Schlüssel.
Ein Server, der den Worker ohne Login hereinlässt, wird ebenfalls mit diesem Satz
abgelehnt.

Der Sync behält diese Ablehnung, sodass der Server einen fehlgeschlagenen Login
erhält, nicht einen pro Datei. Jeder spätere Sync sendet einen weiteren. Unter
OpenSSH 9.8 und neuer lassen einige davon den Server die Adresse der Worker eine
Zeit lang ablehnen, und die anderen Quellen auf diesem Server schlagen dann mit
[die Verbindung ging verloren](#sftp-the-connection-to-was-lost) fehl. Korrigieren
Sie den Login, oder halten Sie die Quelle an.

### SFTP: "The private key in the Vault could not be read with its passphrase." { #sftp-the-private-key-in-the-vault-could-not-be-read-with-its-passphrase }

Der Worker konnte den privaten Schlüssel des Logins nicht mit der daneben
gespeicherten Passphrase öffnen. Der Vault prüft den Schlüssel, wenn der Login
gespeichert wird, also kommt das selten vor. Speichern Sie den Schlüssel und seine
Passphrase erneut im Login.

### SFTP: "… has no SFTP service for …" { #sftp-has-no-sftp-service-for }

Der Server hat den Login angenommen, gibt dem Konto aber keinen SFTP-Dienst. Das
Konto ist womöglich nur für eine Shell eingerichtet, oder der Server betreibt kein
SFTP-Subsystem. Bitten Sie den Administrator, SFTP für das Konto zuzulassen.

### SFTP: "… answered the start of the SFTP session in a way this worker cannot use." { #sftp-answered-the-start-of-the-sftp-session-in-a-way-this-worker-cannot-use }

Der Server hat die Anfrage nach der SFTP-Version in einer Form beantwortet, die der
Worker nicht verwenden kann, oder mit einer Antwort, die der Worker nicht
dekodieren kann. Der Worker spricht SFTP Version 3, wie OpenSSH,
SFTPGo, ProFTPD und Apache MINA SSHD. Das Worker-Log enthält die eigene Antwort des
Servers. Fragen Sie den Administrator nach den SFTP-Einstellungen des Servers.

### SFTP: "… did not answer within 30 seconds." { #sftp-did-not-answer-within-30-seconds }

Der Server hat auf den Verbindungsaufbau oder auf eine SFTP-Anfrage nicht innerhalb
von 30 Sekunden geantwortet. Beim Verbindungsaufbau ist die übliche Ursache eine
Firewall, die die Pakete des Workers verwirft. Prüfen Sie, ob der Worker den
SSH-Port des Servers erreichen kann. Während des Syncs hat der Server aufgehört zu
antworten. Der Worker trennt die Verbindung, und die Auflistung oder diese Datei
schlägt fehl. Die Auflistung liest mehrere Ordner gleichzeitig, sodass ein
einziger Ordner, der hängt, die ganze Auflistung mit diesem Satz fehlschlagen
lässt, und dieser Lauf entfernt nichts.

### SFTP: "… could not be reached on port …" { #sftp-could-not-be-reached-on-port }

Der Server hat die Verbindung auf diesem Port abgelehnt, oder das Netzwerk hat
keine Route zu ihm. Prüfen Sie **Port**, und prüfen Sie, ob eine Firewall den
Worker den Server erreichen lässt. Hat der Host mehr als eine Adresse, versucht der
Worker jede davon, bevor er aufgibt.

### SFTP: "The sync did not finish (OSError) - check the source's credential, …" { #sftp-the-sync-did-not-finish-oserror-check-the-sources-credential }

Bei einer SFTP-Quelle ist das Credential meist nicht die Ursache. Dieser Satz
bedeutet meist, dass der Worker als Benutzer ohne passwd-Eintrag läuft, sodass die
SSH-Bibliothek den lokalen Benutzernamen des Workers nicht lesen kann. Setzen Sie
`LOGNAME` für den Worker, wie im Hinweis "Ein Worker braucht einen lokalen
Benutzernamen" unter [SFTP-Netzwerkregeln](#sftp-network-rules) beschrieben. Das
Worker-Log enthält den vollständigen Fehler.

### SFTP: "There is no folder … on …, or it is not a folder." { #sftp-there-is-no-folder-on-or-it-is-not-a-folder }

**Folder** nennt keinen Ordner, den das Konto sehen kann. Prüfen Sie den Pfad. Auf
einem Server, der das Konto auf einen Home-Ordner oder ein chroot beschränkt,
beginnt der Pfad bei diesem Ordner, nicht bei der Wurzel des Servers. OpenSSH gibt
dieselbe Antwort, wenn **Folder** eine Datei nennt. Andere Server antworten bei
einer Datei mit "… could not list the folder …".

### SFTP: "The account … may not read the folder … on …" { #sftp-the-account-may-not-read-the-folder-on }

Das Konto hat keine Berechtigung, **Folder** aufzulisten. Bitten Sie den
Administrator, dem Konto Lesezugriff auf den Ordner zu geben, oder wählen Sie einen
Ordner, den das Konto lesen kann.

### SFTP: "… could not list the folder …" { #sftp-could-not-list-the-folder }

Der vollständige Satz lautet "… could not list the folder …. Check that it is a
folder the account may read.". SFTPGo, ProFTPD und Apache MINA SSHD antworten so,
wenn **Folder** eine Datei nennt, prüfen Sie also zuerst den Pfad. Andernfalls hat
der Server die Auflistung aus einem anderen Grund nicht ausgeführt oder eine
Antwort gesendet, die der Worker nicht dekodieren kann. Das Worker-Log enthält die
eigene Antwort des Servers.

### SFTP: "The listing of … stopped …. Choose a narrower folder." { #sftp-the-listing-of-stopped-choose-a-narrower-folder }

Die Auflistung hat eine der [Grenzen](#what-bounds-an-sftp-sync) erreicht, und der
Satz nennt, welche: 32 Ordnerebenen, 10000 Ordner, 1000000 Namen, 100000 Dateien,
64 MB an Dateipfaden oder 1800 Sekunden. Was die Auflistung gefunden hat, ist aufgenommen, und nichts
wurde entfernt. Eine Grenze lässt sich nicht anheben. Wählen Sie einen engeren
**Folder** oder teilen Sie den Baum auf mehrere Quellen auf, bis ein Sync ohne
diesen Satz endet.

### SFTP: "The folder … could not be read." { #sftp-the-folder-could-not-be-read }

Der übergeordnete Ordner listet diesen Ordner auf, aber der Server hat sich
geweigert, ihn zu öffnen, meist weil das Konto keine Berechtigung hat, oder er hat
dazu eine Antwort gesendet, die der Worker nicht dekodieren kann. Die anderen
Ordner wurden aufgenommen, und in diesem Lauf wurde nichts entfernt. Geben Sie dem
Konto Lesezugriff auf den Ordner, oder verschieben Sie den Ordner aus dem Baum.

### SFTP: Namen, die in der Auflistung ausgelassen wurden { #sftp-names-left-out-of-the-listing }

Das Sync-Protokoll kann für einen Ordner diese Sätze enthalten:

- "The server listed … names in … that are not file names, so they were left
  out." Die Namen sind leer, enthalten `/` oder ein NUL-Byte oder sind länger
  als 255 Bytes, oder ihre Pfade sind länger als 4.096 Bytes.
- "… names in … are not UTF-8, so those files were left out."
- "The server did not say what … entries in … are, so they were left out."

Benennen Sie bei den ersten beiden die Dateien auf dem Server um. Die Auflistung
bleibt vollständig. Beim dritten hat der Server für einige Einträge keinen Typ
angegeben. Die Auflistung ist dann unvollständig, also entfernt dieser Lauf
nichts. Fragen Sie den Administrator nach dem Server. Das Sync-Protokoll zeigt
höchstens drei Hinweise. Bringen Sie die genannten Ordner in Ordnung und
synchronisieren Sie erneut, um die nächsten zu sehen. Ein Steuerzeichen im Namen
eines Ordners oder ein Unicode-Zeilen- oder -Absatztrenner erscheint als `?`.

### SFTP: eine als fehlgeschlagen gezählte Datei { #sftp-a-file-counted-as-failed }

Das Sync-Protokoll zählt die Datei unter `failed`, und die anderen Dateien werden
synchronisiert. Das Worker-Log nennt die Datei und den Grund, in einer Zeile wie
`Failed to sync report.pdf: report.pdf is 61 MB, and a synced file may be at most 50 MB.`

| Grund | Ursache | Was zu tun ist |
|---|---|---|
| "… is … MB, and a synced file may be at most … MB." | Die Datei ist größer als `MAX_UPLOAD_SIZE_MB`. Sie schlägt bei jedem Sync fehl, bevor ein Byte gelesen wird | Verschieben Sie die Datei aus dem Ordner, entfernen Sie ihren Typ aus **File types**, oder heben Sie `MAX_UPLOAD_SIZE_MB` an |
| "The server sent more than … MB of …, and a synced file may be at most … MB." | Der Server hat mehr als die Grenze gesendet, obwohl seine Stat-Werte eine kleinere Größe angaben | Fragen Sie den Administrator nach der Datei |
| "The account … may not read …" | Die Berechtigungen der Datei weisen das Konto ab | Geben Sie dem Konto Lesezugriff auf die Datei |
| "The server refused to read …" | Der Server hat die Datei aus einem anderen Grund abgelehnt oder eine Antwort gesendet, die der Worker nicht dekodieren kann. Das Worker-Log enthält seine Antwort | Fragen Sie den Administrator |
| "The server listed … as a file, and now says it is not one." | Der Pfad ist jetzt ein Ordner, ein Link oder eine Spezialdatei, oder die Antworten des Servers widersprechen sich | Die nächste Auflistung klärt es. Schlägt es bei jedem Sync fehl, fragen Sie den Administrator |
| "The server lists …, but answers that it does not exist." | Der Ordner nennt die Datei noch, aber der Server antwortet für sie mit "no such file" | Fragen Sie den Administrator. Der Server widerspricht sich selbst |
| "The server answers that … does not exist, and its folder could not be listed to confirm it." | Der Server hat mit "no such file" geantwortet und konnte danach den Ordner der Datei nicht auflisten, oder nicht innerhalb der 10 Minuten der Datei | Prüfen Sie die Berechtigungen des Ordners. Der nächste Sync versucht es erneut |
| "The server sent … of the … bytes … holds." | Der Server hat weniger Bytes gesendet, als seine Stat-Werte angeben | Der nächste Sync versucht es erneut. Schlägt es wieder fehl, fragen Sie den Administrator |
| "The server answered a read of … with more than was asked for." | Der Server hat mehr Bytes gesendet, als eine Anfrage verlangt hat | Fragen Sie den Administrator. Der Server ist defekt |
| "The server opened a file for … that is not the file it listed. …" | Die geöffnete Datei hat einen anderen Besitzer oder eine andere Gruppe als die Auflistung, oder andere Berechtigungen als der Stat vor dem Öffnen oder, bei einer Datei, deren Größe oder Zeit sich verschoben hat, als die Auflistung | Siehe [ihren Eintrag](#sftp-the-server-opened-a-file-for-that-is-not-the-file-it-listed) |
| "… did not download within 600 seconds." | Der Download hat länger als 10 Minuten gedauert. Eine Datei von 50 MB braucht etwa 85 KB/s | Prüfen Sie die Geschwindigkeit des Netzwerks zum Server |
| "… did not answer within 30 seconds." oder "The connection to … was lost." | Der Server hat aufgehört zu antworten, oder die Verbindung endete während des Downloads | Die nächste Datei öffnet eine neue Verbindung, und der nächste Sync liest diese Datei |

Wenn während des Syncs eine neue Verbindung fehlschlägt, behält der Sync diese
Ablehnung. Jede Datei danach schlägt mit demselben Satz fehl. Siehe die Einträge
zum Verbindungsaufbau weiter oben.

### SFTP: "The server opened a file for … that is not the file it listed. …" { #sftp-the-server-opened-a-file-for-that-is-not-the-file-it-listed }

Der vollständige Satz lautet "The server opened a file for … that is not the file
it listed. Check that nobody can replace files in this folder with links.". Die
Datei, die der Server geöffnet hat, hat einen anderen Besitzer oder eine andere
Gruppe, als die Auflistung angab, oder andere Berechtigungen als der Stat direkt
vor dem Öffnen. Gibt dieser Stat eine andere Größe oder Änderungszeit an als die
Auflistung, müssen die Berechtigungen auch denen der Auflistung entsprechen. Die
übliche Ursache ist ein Link anstelle der aufgelisteten Datei
oder eines Ordners darüber, der den Sync eine andere Datei lesen lassen kann.

Die Datei wird nicht aufgenommen. Siehe [die Warnung zu
Links](#4-what-an-sftp-sync-transfers), und schließen Sie das Konto auf dem
Server ein. Eine Datei, die ein anderes Konto zwischen der Auflistung und dem
Download erneut hochgeladen hat, liefert diesen Satz einmal, und der nächste Sync
liest sie. Ebenso eine Datei, die sich auf einem Server geändert hat, der die
Berechtigungen in seiner Auflistung fälscht, etwa ProFTPD mit `DirFakeMode`.

### SFTP: "… files changed or were deleted while this sync ran, …" { #sftp-files-changed-or-were-deleted-while-this-sync-ran }

Diese Dateien haben sich während des Lesens geändert oder wurden nach der
Auflistung gelöscht. Sie werden als `skipped` gezählt, und der nächste Sync liest
sie oder entfernt ihre Dokumente. Der Connector protokolliert für jede Datei einen
dieser Sätze, auf der Info-Ebene: "… changed while it was downloaded.", "… was
replaced while it was downloaded." oder "… was deleted before it was downloaded."
(oder "while it was downloaded."). Der mitgelieferte Worker gibt keine Info-Zeilen
aus, sein Log zeigt sie also standardmäßig nicht. Zeigt dieselbe Datei das bei jedem Sync, ist
eine mögliche Ursache ein Server, der die aktuelle Zeit als Änderungszeit der Datei
meldet. Fragen Sie den Administrator nach dem Server.

### Geplante Syncs laufen nicht { #scheduled-syncs-are-not-running }

Es muss ein System für Hintergrundaufgaben laufen. Prüfen Sie, ob Ihr
Worker-Prozess aktiv ist:

Ohne Worker funktionieren nur manuelle Auslöser über CLI oder API.
