---
source_sha: "80123e3974ff"
---

# Konfiguracja źródeł synchronizacji { #configure-sync-sources }

Źródła synchronizacji samodzielnie pobierają dokumenty z usług zewnętrznych
(Google Drive, S3/MinIO, publiczna strona internetowa, repozytorium Git, witryna
SharePoint, OneDrive albo folder na serwerze SFTP) do kolekcji wiedzy. Każde źródło przechowuje typ connectora, kolekcję docelową, ustawienia właściwe dla
connectora, tryb synchronizacji, opcjonalny harmonogram oraz id
[sekretu w vault](../secrets.md), który je uwierzytelnia - strona internetowa
żadnego nie potrzebuje.

Gdy synchronizacja się uruchamia, connector wypisuje zdalne pliki, pobiera je do
katalogu tymczasowego i przepuszcza przez standardowy potok przetwarzania
(parsowanie, dzielenie na fragmenty, embedowanie, zapis). Gdy lista jest
kompletna, dokumenty, które źródło wprowadziło wcześniej, a których już nie
wypisuje, zostają usunięte. Wpis `SyncLog` odnotowuje wynik każdej operacji
synchronizacji.

### Architektura w skrócie { #architecture-at-a-glance }

| Komponent | Lokalizacja | Rola |
|-----------|----------|------|
| `BaseSyncConnector` | `app/services/rag/connectors/__init__.py` | Abstrakcyjna baza wszystkich connectorów |
| `RemoteFile` | `app/services/rag/connectors/__init__.py` | Model Pydantic opisujący zdalny plik |
| `CONNECTOR_REGISTRY` | `app/services/rag/connectors/__init__.py` | Mapuje nazwy typów connectorów na klasy |
| `SyncSource` (model DB) | `app/db/models/sync_source.py` | Utrwala konfiguracje źródeł |
| `SyncLog` (model DB) | `app/db/models/sync_log.py` | Śledzi poszczególne operacje synchronizacji |
| `SyncSourceService` | `app/services/sync_source.py` | Logika biznesowa CRUD i wyzwalania |
| Komendy CLI dla RAG | `app/commands/rag.py` | Interfejs CLI do zarządzania źródłami |
| Trasy API dla RAG | `app/api/routes/v1/rag.py` | REST API do zarządzania źródłami |

## Szybki start -- CLI { #quick-start-cli }

### Wypisz dostępne typy connectorów { #list-available-connector-types }

```bash
# Lists the configured sources and the connector type of each
uv run agenticos cmd rag-sources
```

Aby zobaczyć, jakie typy connectorów ma ten deployment (`gdrive`, `s3`, `web`,
`git`, `sharepoint`, `sftp`), użyj `GET /api/v1/rag/sync/connectors`, tak jak
w [przykładzie: wykrycie dostępnych
connectorów](#example-discover-available-connectors).

### Dodaj źródło Google Drive -- synchronizacja co 2 godziny { #add-a-google-drive-source-sync-every-2-hours }

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

### Dodaj źródło S3 -- tylko synchronizacja ręczna { #add-an-s3-source-manual-sync-only }

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

### Dodaj źródło Git -- dokumentacja repozytorium, co noc { #add-a-git-source-a-repositorys-docs-nightly }

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

Następnie wybierz jego token dostępu jako poświadczenie źródła w interfejsie albo
wyślij `secret_id` metodą `PATCH` — zobacz
[Konfiguracja repozytorium Git](#git-repository-setup).

### Dodaj źródło SharePoint -- jeden folder biblioteki, co 6 godzin { #add-a-sharepoint-source-one-library-folder-every-6-hours }

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

`--secret-id` to aplikacja Microsoft Entra w vault organizacji — zobacz
[Konfiguracja SharePoint i OneDrive](#sharepoint-and-onedrive-setup).

### Dodaj źródło SFTP -- jeden folder, co noc { #add-an-sftp-source-one-folder-nightly }

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

`--secret-id` to login SSH w vault organizacji, a `host_key` to publiczny klucz
hosta serwera — zobacz [Konfiguracja SFTP](#sftp-setup).

### Wyzwól synchronizację ręcznie { #trigger-sync-manually }

```bash
# Sync a single source by ID
uv run agenticos cmd rag-source-sync <source-id>

# Sync all active sources
uv run agenticos cmd rag-source-sync --all
```

### Usuń źródło { #remove-a-source }

```bash
uv run agenticos cmd rag-source-remove <source-id>
```

`<source-id>` to UUID wypisywany przy tworzeniu źródła i pokazywany na
liście `rag-sources`.

## Szybki start -- interfejs { #quick-start-ui }

1. Przejdź do **Knowledge Base** i otwórz zakładkę **Sync**.
2. Kliknij **"+ Add Source"**.
3. Wybierz typ connectora (Google Drive, S3, Website, Git
   repository, SharePoint & OneDrive, SFTP server). Pola formularza są
   generowane ze schematu JSON Schema z `CONFIG_MODEL` connectora. Strona
   internetowa nie ma kroku poświadczenia.
4. Wypełnij pola konfiguracji właściwe dla connectora (na przykład folder ID,
   nazwę bucketa).
5. Wybierz kolekcję docelową, tryb synchronizacji i interwał harmonogramu.
6. Kliknij **"Create Source"**.
7. Przyciskiem **"Sync Now"** wyzwolisz synchronizację natychmiast albo
   poczekasz, aż harmonogram zadziała sam.

Żeby później zmienić source, kliknij ołówek w jego wierszu. Otworzy się ten sam
kreator z wypełnionymi wartościami source'a. Connectora nie da się zmienić, a
**"Save Changes"** wysyła tylko pola, które zmieniłeś. Source zostaje w bazie
wiedzy, w której jest.

Interfejs wywołuje to samo REST API, które opisano poniżej: cokolwiek zrobisz
w interfejsie, zrobisz też przez `curl` lub dowolnego klienta HTTP.

## Tryby synchronizacji { #sync-modes }

| Tryb | Zachowanie |
|------|----------|
| `full` | Synchronizuje wszystko od nowa. Wszystkie pliki są (ponownie) przetwarzane, istniejące dokumenty zastępowane. Ignoruje rewizje i skróty, więc uruchom go, gdy rewizja Connectora może przeoczyć zmianę. |
| `new_only` | Dodaje nowe pliki i aktualizuje zmienione. Zmiany wykrywa po rewizji Connectora, a potem po skrócie SHA-256 — pliki bez zmian są pomijane. |
| `update_only` | Aktualizuje wyłącznie pliki już obecne w kolekcji. Nowe pliki są pomijane. Rewizja Connectora, a potem skrót SHA-256, pomijają pliki bez zmian. |

!!! tip "`new_only` do większości zastosowań"

    Dodaje nowe pliki i aktualizuje zmienione, pomijając te bez zmian — to
    najszybsza synchronizacja przyrostowa. `update_only` odświeża istniejące
    dokumenty, nie dodając nowych; `full` to za każdym razem czysty import
    od nowa.

### Co usuwa synchronizacja { #what-a-sync-removes }

W każdym trybie synchronizacja usuwa dokumenty, które jej źródło wprowadziło
wcześniej, a których już nie wypisuje: stronę zdjętą z witryny, plik skasowany z
folderu na Drive, obiekt usunięty z bucketa. Log synchronizacji liczy je w polu
`removed`.

Nie usuwa niczego, jeśli lista nie była **kompletna**. Crawl, który zatrzymał się
na limicie stron albo nie zdołał odczytać jednej z nich, nie widział tego, czego
nie wypisuje. Taki przebieg zachowuje wszystkie dokumenty i mówi o tym w
komunikacie logu synchronizacji. Następna synchronizacja z kompletną listą usuwa
to, czego już nie ma. Dokument, którego nie udało się usunąć, liczy się jako
nieudany plik, a następna synchronizacja próbuje ponownie.

Jedno źródło ma naraz tylko jedną synchronizację. Synchronizacja uruchomiona, gdy
inna synchronizacja tego samego źródła wciąż trwa, nie startuje, a jej log mówi o
tym.

Usuwane są wyłącznie dokumenty samego źródła. Przesłany plik ani dokument, który
do tej samej kolekcji wprowadziło inne źródło, nigdy nie są ruszane. Gdy dwa
źródła jednej kolekcji wymieniają ten sam dokument, zostaje on, dopóki oba nie
przestaną go wymieniać. Dokument przetworzony, zanim źródło zaczęło to
odnotowywać (wrzesień 2026), zostaje zachowany, dopóki synchronizacja źródła
znowu go nie wymieni.

### Co robi druga synchronizacja { #what-a-second-sync-does }

Każda synchronizacja po pierwszej robi tak mało, jak pozwala na to źródło:

- **Niezmieniony plik kosztuje najwyżej pobranie, nigdy embedowanie.** Jego
  SHA-256 zgadza się ze skrótem zapisanego dokumentu, więc jest liczony jako
  `skipped` i nigdy nie jest ponownie parsowany ani embedowany. Connector, który
  podaje rewizję każdego pliku, oszczędza także pobranie. S3 podaje `ETag` każdego
  obiektu, więc `new_only` i `update_only` pomijają obiekt z niezmienionym `ETag`
  przed pobraniem. Serwer SFTP podaje rozmiar i czas modyfikacji każdego pliku,
  więc te same tryby pomijają plik, którego rozmiar i czas się nie zmieniły. Taka
  rewizja przeocza edycję, która zachowuje oba. `full` pobiera każdy plik
  i odczytuje taką edycję.
- **Niezmienione źródło kosztuje jedno zapytanie.** Connector, który potrafi
  powiedzieć, w jakim stanie jest cała jego zawartość — w przypadku gałęzi Git
  jest to jej commit na czubku (head), a w przypadku biblioteki SharePoint jej
  strumień zmian — zapisuje tę wartość po każdym przebiegu,
  który zakończył się bez żadnego błędu. Następny przebieg `new_only` albo
  `update_only`, który zastanie tę samą wartość przy tej samej konfiguracji,
  zatrzymuje się, zanim cokolwiek wypisze: jego log nie pokazuje żadnych
  przetworzonych plików, a przebieg niczego nie usuwa. Zmiana konfiguracji, kolekcji albo trybu sprawia, że
  następny przebieg czyta wszystko od nowa, a `full` nigdy nie kończy się
  wcześniej.
- **Plik, który przerwana synchronizacja zostawiła w połowie, zostaje
  naprawiony.** Worker zatrzymany po zapisaniu wektorów pliku, a przed ich
  odnotowaniem, zostawia je bez śledzącego wiersza. Następna synchronizacja tego
  źródła usuwa to, czego nic nie śledzi, i ponownie przetwarza plik; dopóki taki
  plik czeka, nie kończy się wcześniej.

Przebieg z plikiem, którego nie udało się przetworzyć, nie zapisuje stanu, więc
następny przebieg czyta źródło w całości i ponawia próbę.

## Harmonogram { #schedule }

Pole `schedule_minutes` decyduje, jak często źródło synchronizuje się
samoczynnie:

| Wartość | Znaczenie |
|-------|---------|
| `0` (lub `null`) | Tylko ręcznie -- wyzwalane z CLI lub interfejsu |
| `30` | Co 30 minut |
| `120` | Co 2 godziny |
| `1440` | Raz dziennie |

!!! warning "Harmonogram wymaga workera"

    Harmonogram `dispatch-due-syncs` budzi się co 60 sekund i kolejkuje
    synchronizację każdego źródła, którego termin nadszedł, więc bez kontenera
    `worker`, który startuje `make dev`, `schedule_minutes` nie robi nic. Bez
    niego ręczne wyzwolenie (CLI, API lub interfejs) też kolejkuje swoją
    synchronizację, która również czeka na workera. Jedno źródło nigdy nie ma
    dwóch synchronizacji naraz.

## Konfiguracja Google Drive { #google-drive-setup }

### 1. Utwórz konto usługi { #1-create-a-service-account }

1. Wejdź do [Google Cloud Console](https://console.cloud.google.com/).
2. Utwórz nowy projekt (albo wybierz istniejący).
3. Włącz **Google Drive API**.
4. Przejdź do **IAM & Admin > Service Accounts** i utwórz nowe konto
   usługi.
5. Utwórz dla niego klucz JSON i pobierz go.

### 2. Udostępnij folder na Drive { #2-share-your-drive-folder }

1. Otwórz Google Drive i przejdź do folderu, który chcesz synchronizować.
2. Kliknij **Share** i dodaj adres e-mail konta usługi (wygląda tak:
   `name@project.iam.gserviceaccount.com`).
3. Przyznaj co najmniej dostęp **Viewer**.

### 3. Przekaż źródłu klucz { #3-give-the-source-the-key }

Wklej zawartość pliku z kluczem JSON w pole **Service Account JSON** źródła.
Źródło `gdrive` działa na poświadczeniu, które niesie jego własna konfiguracja,
i na niczym innym — nie ma zapasowego poświadczenia obowiązującego dla całego
deploymentu, bo pozwoliłoby ono, żeby `folder_id` źródła decydowało o tym, co
zostanie wypisane spod konta usługi operatora.

`GOOGLE_DRIVE_CREDENTIALS_FILE` w `.env` służy wyłącznie komendzie CLI
`rag-sync-gdrive`.

### 4. Odczytaj folder ID { #4-get-the-folder-id }

Folder ID to ostatni segment adresu URL folderu Google Drive:

```
https://drive.google.com/drive/folders/1abc123def456ghi
                                        ^^^^^^^^^^^^^^^
                                        This is the folder ID
```

### 5. Pola konfiguracji connectora Google Drive { #5-google-drive-connector-config-fields }

| Pole | Typ | Wymagane | Domyślnie | Opis |
|-------|------|----------|---------|-------------|
| `folder_id` | string | Tak | -- | Folder ID Google Drive z adresu URL |
| `include_subfolders` | boolean | Nie | `true` | Rekurencyjnie obejmuje pliki z podfolderów |

Samo konto usługi **nie** jest polem konfiguracji. Dodaj je do vault jako
poświadczenie rodzaju `gcp_service_account` i wskaż je źródłu przez `secret_id`:
zostaje zapisane raz i jest przywoływane przez każde źródło, które go potrzebuje,
zamiast być wklejane do każdego z osobna
([#937](https://github.com/vstorm-co/agenticos/issues/937)). Przesłanie go pod
`config` jest odrzucane.

`folder_id` może zawierać wyłącznie to, co wydaje Google — litery, cyfry, `-`
i `_`. Cokolwiek innego jest odrzucane przy tworzeniu źródła, bo id trafia
przez interpolację do zapytania Drive, a pojedynczy apostrof w nim poszerza
to, co zapytanie wypisuje.

Google Docs, Sheets i Slides są przy pobieraniu samoczynnie eksportowane do
formatów przenośnych (PDF, XLSX, PPTX). Plik, którego nazwa na Drive zawiera
separatory ścieżki, zapisywany jest jako jeden plik wewnątrz katalogu
synchronizacji, nigdy pod ścieżką, którą literuje jego nazwa.

## Konfiguracja S3 / MinIO { #s3-minio-setup }

### 1. Skonfiguruj środowisko { #1-configure-the-environment }

Dodaj do swojego pliku `.env` następujące zmienne:

```bash
S3_RAG_ENDPOINT=https://s3.amazonaws.com   # or your MinIO URL, e.g. http://localhost:9000
S3_RAG_ACCESS_KEY=your-access-key
S3_RAG_SECRET_KEY=your-secret-key
S3_RAG_REGION=us-east-1                    # required for AWS, optional for MinIO
```

W MinIO endpoint ma zwykle postać `http://minio:9000` (Docker) albo
`http://localhost:9000` (lokalnie).

### 2. Pola konfiguracji connectora S3 { #2-s3-connector-config-fields }

| Pole | Typ | Wymagane | Domyślnie | Opis |
|-------|------|----------|---------|-------------|
| `bucket` | string | Tak | -- | Nazwa bucketa S3 |
| `prefix` | string | Nie | `""` | Prefiks kluczy zawężający zakres synchronizacji (np. `documents/legal/`). Zostaw pusty, aby objąć cały bucket. |

## Konfiguracja strony internetowej { #website-setup }

Źródło `web` czyta publiczną stronę internetową, zwykle witrynę z dokumentacją
produktu. Nie potrzebuje poświadczenia ani wpisu w vault. Podaj mu początkowy
adres URL, a będzie albo podążać za linkami z tej strony, albo czytać strony
wypisane w sitemapie.

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

### Pola konfiguracji connectora strony internetowej { #website-connector-config-fields }

| Pole | Typ | Wymagane | Domyślnie | Opis |
|-------|------|----------|---------|-------------|
| `root_url` | string | Tak | -- | Strona, od której zaczyna się crawl. Jej host jest jedynym hostem, który źródło czyta. |
| `max_depth` | integer | Nie | `2` | Na ile linków od początkowego adresu URL podążać, od `0` do `10`. `0` czyta tylko stronę początkową. |
| `path_prefix` | string | Nie | folder początkowego adresu URL | Czytane są tylko strony, których ścieżka zaczyna się od tej wartości. `https://docs.example.com/guide/intro` domyślnie czyta `/guide/`; ustaw `/`, aby objąć cały host. |
| `sitemap_url` | string | Nie | -- | Czyta strony wypisane w tej sitemapie zamiast podążać za linkami. Musi leżeć na hoście początkowego adresu URL i używać `https://`, gdy używa go początkowy adres URL. Indeks sitemap jest rozwijany do jego sitemap. |
| `max_pages` | integer | Nie | `500` | Crawl zatrzymuje się po odczytaniu tylu stron, od `1` do `5000`. |

### Co ogranicza crawl { #what-bounds-a-crawl }

- **Jeden host i jedna ścieżka.** Linki do innych hostów i do ścieżek spoza
  `path_prefix` nie są śledzone. Przekierowanie, które je opuszcza, także nie.
  Początkowy adres URL na `https://` nigdy nie jest opuszczany na rzecz
  `http://`: link ani przekierowanie do strony bez szyfrowania nie są śledzone.
- **Sieć deploymentu jest poza zasięgiem.** Każde żądanie - robots.txt, sitemapa,
  każda strona i każde przekierowanie - jest sprawdzane według tej samej polityki
  SSRF co webhooki i serwery MCP. Wysyłane jest na adres, który przeszedł
  sprawdzenie. Początkowy adres URL, który rozwiązuje się na adres prywatny,
  loopback, link-local albo metadanych chmury, jest odrzucany przy zapisie źródła.
- **robots.txt jest przestrzegany** dla sitemap i stron, łącznie z `Crawl-delay`
  do dziesięciu sekund. Crawler przedstawia się jako `AgenticOS-Crawler`. Między
  żądaniami czeka co najmniej pół sekundy, a strona oznaczona `noindex` lub
  `nofollow` jest respektowana. Strona, którą sitemapa wciąż wypisuje, choć jest
  oznaczona `noindex` albo już nie istnieje, jest usuwana z kolekcji.
- **Rozmiar i czas.** Strona większa niż 5 MB nie jest czytana. Crawl zatrzymuje
  się na `max_pages`. Synchronizacja przestaje czytać witrynę po sześciu
  godzinach, a synchronizacja, która się zatrzymała, niczego nie usuwa.

Każda strona jest zapisywana jako dokument Markdown zawierający jej tekst i adres
URL, z którego pochodzi, bez query stringa. Nawigacja, nagłówki, stopki i skrypty są pomijane. Strona
jest embedowana ponownie tylko wtedy, gdy zmieni się jej tekst. Nowy znacznik
builda albo skrypt śledzący w znacznikach HTML nie liczy się jako zmiana.

### Kto może czytać to, co importuje { #who-can-read-what-it-imports }

Źródło strony internetowej nie ma poświadczenia, więc jego zasięg to wszystko, co
strona pokazuje każdemu w internecie. Nigdy nie przechodzi przez logowanie.
Wszystko, co importuje, może przeszukiwać każdy, kto może przeszukiwać zasilaną
przez nie kolekcję, tak jak w przypadku każdego innego źródła. Zobacz
[kto ostatecznie może czytać to, co źródło przetworzyło](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

Importowane są wyłącznie strony HTML. PDF ani inny plik podlinkowany ze strony nie
jest pobierany.

## Konfiguracja repozytorium Git { #git-repository-setup }

Źródło `git` czyta dokumentację repozytorium przez HTTPS — z GitHuba, GitLaba
albo dowolnego innego hosta, który serwuje git przez HTTPS. Potrzebuje adresu
URL do klonowania i tokena dostępu, a nie API którejkolwiek z tych platform.

### 1. Wystaw token dla jednego repozytorium { #1-issue-a-token-for-the-one-repository }

**Zasięg tokena to zasięg źródła.** Wszystko, co źródło przetworzy, staje się
możliwe do przeszukania przez każdego, kto może czytać kolekcję, więc token,
który może czytać każde prywatne repozytorium swojego właściciela, to token,
który może je wszystkie opublikować tym odbiorcom. Zobacz
[kto ostatecznie może czytać to, co przetworzyło
źródło](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

- **GitHub:** fine-grained personal access token, *Only select repositories*,
  z tym jednym repozytorium i **Contents: Read-only** jako jedynym uprawnieniem.
- **GitLab:** project access token w tym jednym projekcie, rola **Reporter**,
  wyłącznie zakres **`read_repository`**.

Nadaj mu datę wygaśnięcia. Gdy wygaśnie, następna synchronizacja źródła kończy
się błędem *the repository refused the source's token*, a naprawą jest nowy
token w tym samym sekrecie w vault.

### 2. Dodaj go do vault { #2-add-it-to-the-vault }

Dodaj token do vault jako **Git access token**, razem z **hostem**, do którego
należy: `github.com`, `gitlab.com` albo własny serwer, na przykład
`git.example.com:8443`. Host z literami spoza ASCII wpisuje się w postaci
zakodowanej, na przykład `xn--bcher-kva.example` dla `bücher.example`. Potem
wybierz token w kroku poświadczenia źródła. Jest
wysyłany jako nagłówek HTTP `Authorization`, nigdy w adresie URL i nigdy
w wierszu poleceń, który mógłby odczytać inny proces.

**Host należy do tokena, nie do źródła.** Adres URL repozytorium wybiera ten,
kto edytuje źródło, a token jest wysyłany wyłącznie do hosta, z którym został
dodany. Edycja źródła nie może więc skierować tokena organizacji na inny serwer,
a żadnego innego rodzaju klucza, na przykład klucza API dostawcy modelu, nie da
się w ogóle wybrać dla źródła Git.

### 3. Pola konfiguracji connectora Git { #3-git-connector-config-fields }

| Pole | Typ | Wymagane | Domyślnie | Opis |
|-------|------|----------|---------|-------------|
| `repository_url` | string | Tak | -- | Adres URL klonowania przez HTTPS, np. `https://github.com/acme/handbook.git`. Bez nazwy użytkownika i tokena. |
| `branch` | string | Nie | `main` | Gałąź do odczytu. |
| `path_prefix` | string | Nie | -- | Katalog wewnątrz repozytorium, np. `docs`. Zostaw pusty, aby objąć całe repozytorium. |
| `include` | lista stringów | Nie | `**/*.md`, `**/*.txt` | Które pliki przetwarzać, jako wzorce w stylu `.gitignore` względem `path_prefix`. |

Domyślnie jest to dokumentacja, a nie całe drzewo: kod źródłowy repozytorium nie
jest korpusem, a jego przetworzenie wypełnia bazę wiedzy kodem, którego nikt nie
chciał przeszukiwać. Dodaj wzorzec taki jak `**/*.pdf` dla innego formatu, który
czyta parser kolekcji. Wzorzec nie może zaczynać się od `!`.

Każdy plik jest dokumentem o adresie
`git://<host>/<owner>/<repo>@<branch>/<path>`, z `:<port>` po hoście, gdy port
nie jest 443. Gałąź jest częścią adresu, więc dwa źródła czytające dwie gałęzie
jednego repozytorium do jednej kolekcji mają osobne dokumenty.

### 4. Co przesyła synchronizacja { #4-what-a-sync-transfers }

Pierwszym zapytaniem każdej synchronizacji jest `git ls-remote` dla gałęzi —
około kilobajta. Gdy commit na czubku gałęzi nie przesunął się od ostatniego
czystego przebiegu, synchronizacja na tym się kończy. Gdy się przesunął,
connector wykonuje płytki, częściowy i rzadki (sparse) klon: jeden commit
i wyłącznie pliki pasujące do wzorców `include`. Dokumentacja monorepo kosztuje
więc tyle, co dokumentacja, a nie całe drzewo źródeł.

Zanim klon zapisze cokolwiek na dysk workera, connector mierzy każdy plik, który
by zapisał. Plik większy niż limit dokumentu bazy wiedzy (`MAX_UPLOAD_SIZE_MB`,
domyślnie 50 MB) albo łącznie ponad 512 MB plików zostaje odrzucony i nic nie
jest zapisywane.

Dowiązania symboliczne i submoduły nie są śledzone, a dowiązanie nie jest
przetwarzane jako dokument.

### Reguły sieciowe { #network-rules }

Adres URL musi zaczynać się od `https://`. Jego host jest rozwiązywany raz
i sprawdzany tak jak każdy inny adres wybrany przez tenanta: host, który
rozwiązuje się na adres prywatny, loopback albo link-local, jest odrzucany przy
zapisie źródła i ponownie przy synchronizacji, a git łączy się wyłącznie
z adresami, które ta kontrola zatwierdziła. Przekierowania nie są śledzone.
Deployment za proxy wyjściowym (`HTTPS_PROXY`) nadal z niego korzysta; proxy
rozwiązuje wtedy host samo.

Obraz workera zawiera `git`. Worker zbudowany z innego obrazu potrzebuje `git`
w wersji 2.37 lub nowszej na swoim `PATH`.

## Konfiguracja SharePoint i OneDrive { #sharepoint-and-onedrive-setup }

Źródło `sharepoint` czyta jedną bibliotekę dokumentów przez Microsoft Graph:
bibliotekę witryny SharePoint albo OneDrive konkretnej osoby, który Microsoft 365
przechowuje jako osobną witrynę. Może czytać całą bibliotekę albo jeden folder
w niej. Loguje się jako rejestracja aplikacji Microsoft Entra, którą administrator
Twojego tenanta tworzy jednorazowo.

### 1. Zarejestruj aplikację w Microsoft Entra { #1-register-an-app-in-microsoft-entra }

W [Microsoft Entra admin center](https://entra.microsoft.com) otwórz
**App registrations → New registration**. Nadaj jej nazwę, na przykład *AgenticOS
sync*, zostaw **Accounts in this organizational directory only** i nie wypełniaj
redirect URI. Źródło loguje się jako sama aplikacja, a nie jako osoba.

Na stronie **Overview** aplikacji zanotuj **Application (client) ID** oraz
**Directory (tenant) ID**. W **Certificates & secrets** dodaj client secret
i skopiuj jego **Value**. Wartość jest pokazywana tylko raz. *ID* sekretu to nie
jest to, czego potrzebuje źródło.

### 2. Przyznaj jej jedną witrynę, nie cały tenant { #2-grant-it-one-site-not-the-tenant }

**Zasięg aplikacji to zasięg źródła.** Wszystko, co źródło przetworzy, staje się
możliwe do przeszukania przez każdego, kto może czytać zasilaną przez nie
kolekcję. Zobacz [kto ostatecznie może czytać to, co przetworzyło
źródło](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

W **API permissions** dodaj uprawnienie Microsoft Graph typu **application**
**`Sites.Selected`** i udziel dla niego zgody administratora (admin consent).
Samo `Sites.Selected` nie pozwala niczego odczytać. Administrator przyznaje
następnie aplikacji dostęp do odczytu tej jednej witryny, którą czyta źródło:

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

To wywołanie wymaga `Sites.FullControl.All`, więc administrator wykonuje je
z Graph Explorer albo poleceniem PnP PowerShell
`Grant-PnPAzureADAppSitePermission`, a nie samą aplikacją.
`GET https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/HR?$select=id`
zwraca id witryny. OneDrive przyznaje się tak samo, przez jego witrynę:
`contoso-my.sharepoint.com:/personal/jane_contoso_com`.

!!! danger "`Files.Read.All` albo `Sites.Read.All` otwiera do odczytu każdą bibliotekę w tenancie"

    Na te uprawnienia zgoda jest udzielana dla całego tenanta. Źródło, które je
    ma, nadal czyta wyłącznie bibliotekę, którą wskazuje. Ale ten, kto może
    edytować źródło, może skierować jego adres URL witryny na dowolną witrynę albo
    dowolny OneDrive w organizacji, a następna synchronizacja udostępni tę
    bibliotekę do przeszukiwania każdemu, kto może czytać kolekcję. Nic w tym
    produkcie nie potrafi rozpoznać, jakie uprawnienie dostała aplikacja, bo token
    tego nie mówi. Używaj `Sites.Selected` i przyznawaj jedną aplikację na jedną
    grupę odbiorców.

### 3. Dodaj ją do vault { #3-add-it-to-the-vault }

Dodaj aplikację do vault jako **Microsoft Entra app**: tenant id (albo domenę
tenanta, na przykład `contoso.onmicrosoft.com`), client id oraz wartość client
secretu. Potem wybierz ją w kroku poświadczenia źródła. Sekret jest wysyłany
wyłącznie do `login.microsoftonline.com`, w celu zalogowania.

Client secret wygasa, najpóźniej po dwóch latach. Gdy wygaśnie, następna
synchronizacja kończy się błędem *Microsoft Entra refused the app registration's
credentials (invalid_client)*. Dodaj aplikacji nowy sekret i podmień wartość
w tym samym sekrecie w vault. Każde źródło, które z niego korzysta, pobierze nową
wartość przy następnej synchronizacji.

### 4. Pola konfiguracji connectora SharePoint { #4-sharepoint-connector-config-fields }

| Pole | Typ | Wymagane | Domyślnie | Opis |
|-------|------|----------|---------|-------------|
| `site_url` | string | Tak | -- | Witryna, np. `https://contoso.sharepoint.com/sites/HR`, albo OneDrive, np. `https://contoso-my.sharepoint.com/personal/jane_contoso_com`. Wyłącznie witryna: adres URL biblioteki lub strony skopiowany z przeglądarki jest odrzucany. |
| `library` | string | Nie | domyślna biblioteka witryny | Nazwa biblioteki tak, jak pokazuje ją SharePoint, np. `Documents`. OneDrive ma wyłącznie swoją bibliotekę domyślną. |
| `folder_path` | string | Nie | -- | Folder wewnątrz biblioteki, np. `Policies/HR`. Zostaw pusty, aby objąć całą bibliotekę. |
| `include_subfolders` | boolean | Nie | `true` | Czy czytać foldery poniżej `folder_path`. |
| `extensions` | lista stringów | Nie | `.pdf`, `.docx`, `.md`, `.txt` | Które typy plików przetwarzać, najwyżej 100, każdy najwyżej 16 znaków. Dodaj `.pptx`, `.xlsx` albo inny typ tylko wtedy, gdy czyta go parser kolekcji: plik, którego parser nie potrafi odczytać, liczy się jako nieudany plik przy każdej synchronizacji. |

Każdy plik jest dokumentem o adresie `sharepoint://<drive-id>/<item-id>`. Adres
podąża za plikiem, a nie za jego ścieżką, więc plik, którego nazwę zmieniono albo
który przeniesiono wewnątrz biblioteki, zachowuje swój dokument. Dokument zachowuje
nazwę pliku z chwili przetworzenia, dopóki nie zmieni się zawartość pliku.

Notesy OneNote i pliki innych typów nie są przetwarzane.

### 5. Co przesyła synchronizacja { #5-what-a-sync-transfers }

Każda synchronizacja zaczyna się od strumienia zmian Microsoft Graph dla
biblioteki. Gdy od ostatniego czystego przebiegu nic w bibliotece się nie
zmieniło, synchronizacja na tym się kończy: jedno zapytanie i żadnego wypisywania.
Strumień obejmuje całą bibliotekę, bo Graph udostępnia go wyłącznie dla korzenia
biblioteki. Zmiana w innym folderze tej samej biblioteki sprawia więc, że następna
synchronizacja ponownie wypisuje folder źródła.

Gdy coś się zmieniło, synchronizacja wypisuje folder i pobiera każdy plik
skonfigurowanych typów. Plik, którego zawartość się nie zmieniła, jest następnie
pomijany przed embedowaniem, więc embedowanie kosztują wyłącznie pliki nowe
i zmienione. Plik jest pobierany z adresu, który podaje dla niego Graph, bez
tokena aplikacji. Plik większy niż limit dokumentu bazy wiedzy
(`MAX_UPLOAD_SIZE_MB`, domyślnie 50 MB) nie jest pobierany i liczy się jako
nieudany plik.

Graph spowalnia klienta, który czyta szybko. Zapytanie, które Graph dławi
(HTTP 429) albo które kończy się błędem 5xx lub błędem sieci, jest próbowane
łącznie cztery razy, a synchronizacja czeka tak długo, jak prosi `Retry-After`
z Graph, najwyżej minutę. Folder, którego nadal nie da się wypisać, jest
wymieniany w logu synchronizacji, a ta synchronizacja niczego nie usuwa, bo pliki
w tym folderze nie zostały zobaczone. Pliki z pozostałych folderów są nadal
przetwarzane.

Plik usunięty po wypisaniu, a przed pobraniem, jest usuwany tak jak plik, którego
lista nie wymieniła.

### Sieć { #network }

Worker potrzebuje wyjściowego HTTPS do `login.microsoftonline.com`,
`graph.microsoft.com` oraz hostów `*.sharepoint.com` Twojego tenanta. Nic, co
wpisze osoba edytująca źródło, nie staje się adresem, z którym łączy się worker:
adres URL witryny to tylko nazwa, którą wyszukuje Graph. Chmury krajowe
Microsoftu, takie jak US Government i Chiny, używają innych hostów i nie są
obsługiwane.

## Konfiguracja SFTP { #sftp-setup }

Źródło `sftp` czyta drzewo folderów na serwerze SFTP przez SSH, na przykład
folder, do którego partner odkłada pliki, albo serwer plików. Loguje się loginem
SSH z vault. Ufa wyłącznie kluczowi hosta zapisanemu we własnych ustawieniach
i nie czyta niczego z własnej konfiguracji SSH workera.

### 1. Pobierz klucz hosta serwera { #1-get-the-servers-host-key }

Klucz hosta to jedyny dowód, że worker rozmawia z serwerem, który wskazuje
źródło. Uzyskaj go od administratora serwera. Albo odczytaj go z serwera
samodzielnie i porównaj jego odcisk (fingerprint) z odciskiem od administratora:

```bash
ssh-keyscan -t ed25519 -p 22 sftp.example.com > host_key.txt
ssh-keygen -lf host_key.txt
```

`ssh-keyscan` odczytuje klucz przez sieć, więc nie może dowieść, że klucz należy
do serwera. `ssh-keygen -lf` wypisuje odcisk klucza `SHA256:`. Administrator
odczytuje ten sam odcisk na serwerze poleceniem
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`. Jeśli oba się różnią, nie
używaj tego klucza.

`ssh-keyscan` zapisuje nazwę hosta przed kluczem. Źródło potrzebuje tylko typu
klucza i samego klucza:

```bash
cut -d' ' -f2- host_key.txt
```

Connector przypina klucz typu `ssh-ed25519`, `ssh-ed448`,
`ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521` albo
`ssh-rsa`. Dla serwera bez klucza Ed25519 użyj `-t ecdsa` albo `-t rsa`. Worker
prosi serwer wyłącznie o klucz przypiętego typu. Serwer z kluczami kilku typów
odpowiada kluczem tego typu.

Serwer bez klucza przypiętego typu kończy pierwszą synchronizację w trakcie
otwierania połączenia. Zdanie w logu synchronizacji zależy od serwera:
[połączenie zostało utracone](#sftp-the-connection-to-was-lost), [serwer zamknął
połączenie](#sftp-closed-the-connection) albo [obie strony nie mogły się
porozumieć](#sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher).
W każdym z tych trzech przypadków najpierw sprawdź typ klucza.

### 2. Dodaj login SSH do vault { #2-add-an-ssh-login-to-the-vault }

**Zasięg konta to zasięg źródła.** Wszystko, co źródło przetworzy, staje się
możliwe do przeszukania przez każdego, kto może czytać zasilaną przez nie
kolekcję. Poproś administratora o konto, które może czytać wyłącznie folder
czytany przez źródło. Zobacz [kto ostatecznie może czytać to, co przetworzyło
źródło](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

Poproś też administratora o zamknięcie konta w folderze, który zawiera tylko to,
co wolno synchronizować: `ChrootDirectory` w OpenSSH, `DefaultRoot` w ProFTPD.
Jest to konieczne na każdym serwerze, bo dowiązanie umieszczone w folderze może
sprawić, że synchronizacja przeczyta inny plik, który konto może czytać. Chroot,
który zawiera też inne pliki, nadal je udostępnia. Zobacz [co przesyła
synchronizacja SFTP](#4-what-an-sftp-sync-transfers).

Wybieraj klucz zamiast hasła, a spośród kluczy klucz Ed25519. Utwórz parę kluczy
dla źródła:

```bash
ssh-keygen -t ed25519 -C agenticos-sync -f agenticos_sync
```

Jeśli serwer nie przyjmuje klucza Ed25519, utwórz zamiast niego klucz ECDSA.
Takim serwerem jest Apache MINA SSHD bez swojej biblioteki EdDSA.

```bash
ssh-keygen -t ecdsa -b 256 -C agenticos-sync -f agenticos_sync
```

Administrator dodaje `agenticos_sync.pub` do `authorized_keys` konta. Klucz
prywatny, `agenticos_sync`, trafia do vault.

Dodaj login do vault dla usługi **SFTP server**, jako **SSH login**:

- **User name**: konto na serwerze.
- **Host**: gdzie wolno użyć loginu, na przykład `sftp.example.com`. Dla portu
  innego niż 22 dopisz port: `sftp.example.com:2222`. Adres IPv6 zapisz
  w nawiasach kwadratowych, na przykład `[2001:db8::7]:2222`. Vault zapisuje host
  tak, jak zapisuje go źródło: małymi literami, bez kropki na końcu, adres IPv6
  w postaci skróconej, a nazwę z literami spoza ASCII w postaci zakodowanej, na
  przykład `xn--bcher-kva.example` dla `bücher.example`. Nazwa może zawierać
  podkreślenie, na przykład `sftp_server`. Host tokena dostępu Git nie może.
- **Private key**: cały plik klucza, w formacie OpenSSH, PKCS#8 albo PKCS#1,
  najwyżej 16 384 znaki. Jeśli klucz jest zaszyfrowany, podaj też jego hasło
  klucza w **Key passphrase**, najwyżej 1024 znaki. Klucz RSA może mieć najwyżej 4096 bitów, a jego dwie liczby
  pierwsze muszą mieć ten sam rozmiar, tak jak tworzy je `ssh-keygen`. Klucza
  przechowywanego na kluczu sprzętowym (`sk-`) nie można użyć. Zaszyfrowany klucz
  może wymagać najwyżej 128 rund bcrypt (OpenSSH) albo 1 000 000 iteracji
  i 64-bajtowego klucza pochodnego (PKCS#8). `ssh-keygen` używa 16 rund.
- **Password**: tylko dla serwera, który nie przyjmuje klucza. Musi mieć co
  najmniej osiem znaków i najwyżej 1024. Serwer, który przyjmuje hasło wyłącznie przez logowanie
  keyboard-interactive, na przykład PAM z `PasswordAuthentication no`, je
  odrzuca. Worker nigdy nie odpowiada na monit keyboard-interactive, więc tam
  użyj klucza.

Login zawiera klucz prywatny albo hasło, nigdy oba naraz. Vault otwiera klucz
jego hasłem klucza (passphrase), gdy zapisujesz login, więc klucz, którego nie da
się otworzyć, zostaje odrzucony od razu, a nie przy pierwszej synchronizacji.
Vault otwiera klucz w osobnym procesie, który może działać najwyżej 5 sekund.
Klucz Ed25519 zajmuje milisekundy. Klucz RSA o 4096 bitach albo klucz na
powyższych limitach zajmuje mniej niż dwie sekundy. Potem wybierz login w kroku
poświadczenia źródła.

**Host należy do loginu, nie do źródła.** Host źródła wpisuje ten, kto edytuje
źródło, a connector wysyła login wyłącznie do hosta i portu, dla których został
dodany. Login dodany dla `sftp.example.com` jest używany tylko na porcie 22.
Edycja źródła nie może więc wysłać loginu organizacji na inny serwer.

!!! warning "Ten, kto edytuje źródło, wybiera też klucz hosta, któremu ono ufa"

    Edytujący, który może też przechwycić ruch workera do hosta loginu, może
    przypiąć własny klucz i odebrać hasło. Logowanie kluczem nie ujawnia klucza:
    serwer dostaje tylko podpis, który jest ważny dla jego własnej sesji. To jest
    powód, by wybierać klucz.

### 3. Pola konfiguracji connectora SFTP { #3-sftp-connector-config-fields }

| Pole | Typ | Wymagane | Domyślnie | Opis |
|-------|------|----------|---------|-------------|
| `host` | string | Tak | -- | Nazwa hosta albo adres IP serwera, np. `sftp.example.com`. Sam host: bez schematu, nazwy użytkownika, portu i ścieżki. |
| `port` | integer | Nie | `22` | Port SSH serwera, od `1` do `65535`. |
| `root_path` | string | Tak | -- | Bezwzględna ścieżka folderu do odczytu, np. `/outgoing`. Nie ma wartości domyślnej: na serwerze, który nie trzyma konta w jego folderze domowym, `/` to cały system plików. |
| `host_key` | string | Tak | -- | Publiczny klucz hosta serwera, jeden wiersz, taki jak `ssh-ed25519 AAAA...`, z [kroku 1](#1-get-the-servers-host-key). Komentarz po kluczu jest pomijany. |
| `include_subfolders` | boolean | Nie | `true` | Czy czytać foldery poniżej `root_path`. |
| `extensions` | lista stringów | Nie | `.pdf`, `.docx`, `.md`, `.txt` | Które typy plików przetwarzać, najwyżej 100, każdy najwyżej 16 znaków. Dodaj `.pptx`, `.xlsx` albo inny typ tylko wtedy, gdy czyta go parser kolekcji: plik, którego parser nie potrafi odczytać, liczy się jako nieudany plik przy każdej synchronizacji. |

Formularz pokazuje te pola jako **Host**, **Port**, **Folder**, **Host key**,
**Include subfolders** i **File types**. Connector zapisuje każdy folder w jednej
postaci: `/outgoing//2026/` i `/outgoing/./2026` stają się `/outgoing/2026`.
Folder zawierający `..` jest odrzucany.

Każdy plik jest dokumentem o adresie `sftp://<user>@<host>:<port><path>`, na
przykład `sftp://sync@sftp.example.com:22/outgoing/2026/report.pdf`. Nazwa
użytkownika pochodzi z loginu i jest zakodowana procentowo (percent-encoding).
Ścieżka to pełna ścieżka pliku na serwerze. Dwa konta na jednym serwerze mają
więc osobne dokumenty, a źródło zmienione na węższy folder zachowuje dokumenty,
które nadal wypisuje. Login zmieniony na inną nazwę użytkownika albo źródło
zmienione na inny host lub port zmienia każdy adres. Następna synchronizacja
czyta wtedy każdy plik od nowa, a kompletna lista usuwa dokumenty pod starymi
adresami. Planuj przeniesienie serwera z uwzględnieniem tego kosztu.

### 4. Co przesyła synchronizacja SFTP { #4-what-an-sftp-sync-transfers }

SFTP nie ma strumienia zmian, więc każda synchronizacja wypisuje całe drzewo
folderów. Lista podaje rozmiar i czas modyfikacji każdego pliku, w pełnych
sekundach. Ta para jest rewizją pliku, więc `new_only` i `update_only` pomijają
plik, którego rozmiar i czas się nie zmieniły, jeszcze przed pobraniem.

!!! warning "Edycja, która zachowuje rozmiar i czas, zostaje przeoczona"

    `put -p` zachowuje czas wysyłającego, `SETSTAT` może cofnąć czas, a zapis
    pliku od nowa o tym samym rozmiarze może zmieścić się w jednej sekundzie.
    Każde z nich zmienia plik i zachowuje jego rewizję, więc `new_only`
    i `update_only` pomijają ten plik. Uruchom źródło w trybie `full`, aby
    przeczytać każdy plik od nowa. Tak zachowują się OpenSSH, SFTPGo, ProFTPD
    i Apache MINA SSHD.

Pobieranie czyta plik żądaniami po 255 KiB, po jednym naraz, więc plik 50 MB
kosztuje około 200 obiegów żądanie-odpowiedź (round trip). Connector porównuje
statystyki pliku (stat) przed odczytem i po nim. Plik, który zmienił się lub
został podmieniony w trakcie odczytu albo został usunięty po wypisaniu, jest
liczony jako `skipped`. Następna synchronizacja czyta jego nową wersję albo usuwa
jego dokument.

Wysyłanie, które wciąż trwa, zwykle zmienia plik w trakcie odczytu, więc
synchronizacja pomija ten plik. Jeśli w trakcie odczytu nie dotrze żaden bajt,
synchronizacja może przetworzyć część, która już dotarła, a następna
synchronizacja czyta cały plik. Plik wysyłany pod tymczasową nazwą
i przemianowany po zakończeniu nigdy nie jest czytany w części, jeśli tymczasowa
nazwa ma typ spoza **File types**, taki jak `.part` albo `.filepart`.

Plik większy niż limit dokumentu bazy wiedzy (`MAX_UPLOAD_SIZE_MB`, domyślnie
50 MB) nie jest pobierany. Liczy się jako nieudany plik przy każdej
synchronizacji, więc pozostaje widoczny. Connector liczy też bajty w trakcie
odczytu, więc serwer, który wysyła więcej, niż podawały jego statystyki, nie
przekroczy limitu.

Lista pomija następujące wpisy:

- **Dowiązania i pliki specjalne.** Wypisywanie nigdy nie wypisuje dowiązania
  symbolicznego, niezależnie od tego, na co wskazuje, więc synchronizacja nie
  wpada w pętlę. FIFO, gniazdo ani urządzenie także nie są wypisywane. Dowiązanie
  może jednak po wypisaniu podmienić wypisany plik albo folder: zobacz
  ostrzeżenie poniżej.
- **Nazwy, które nie mogą być adresem.** Nazwa pusta, zawierająca `/` albo bajt
  NUL, dłuższa niż 255 bajtów lub niebędąca UTF-8 jest pomijana. Tak samo nazwa,
  której ścieżka jest dłuższa niż 4096 bajtów. Log synchronizacji wymienia
  folder, a lista pozostaje kompletna.
- **Wpisy nieznanego typu.** Wpis, którego typu serwer nie podaje, jest
  pomijany. Lista jest wtedy niekompletna, więc ten przebieg niczego nie usuwa.
- **Foldery, których serwer nie wypisuje.** Folder poniżej `root_path`, którego
  serwer odmawia wypisania, zwykle z braku uprawnień, albo o którym odpowiada
  w sposób, którego worker nie potrafi zdekodować, jest wymieniany w logu
  synchronizacji i w logu workera. Ten przebieg niczego nie usuwa.

!!! warning "Dowiązanie umieszczone w folderze może sprawić, że synchronizacja przeczyta inny plik"

    SFTP w wersji 3 nie potrafi otworzyć ścieżki bez podążenia za dowiązaniem,
    a żaden przetestowany serwer nie oferuje sposobu, by to obejść. Osoba, która
    może pisać w folderze, może po wypisaniu podmienić wypisany plik na
    dowiązanie. Pobieranie czyta wtedy cel dowiązania, jeśli konto może go
    czytać, a synchronizacja przetwarza go jako wypisany plik.

    Ten wyścig istnieje na każdym serwerze. Tam, gdzie stat serwera zgłasza
    dowiązanie jako dowiązanie, jak w OpenSSH, SFTPGo i Apache MINA SSHD,
    dowiązanie trzeba umieścić w ciągu kilku żądań przed pobraniem i usunąć
    ponownie przed końcem pobierania. Zapisana rewizja jest wtedy rewizją
    wypisanego pliku, więc `new_only` i `update_only` zachowują błędną treść,
    dopóki wypisany plik się nie zmieni. W ProFTPD, którego stat podąża za
    dowiązaniami, okno trwa od wypisania do pobrania, a dowiązanie może zostać.

    W ten sam sposób można podmienić folder, na każdym serwerze, i takie
    dowiązanie może zostać. Stat ścieżki podąża za dowiązaniem w każdym folderze
    ścieżki i sprawdza tylko ostatnią nazwę. Jeśli ktoś podmieni folder na
    dowiązanie po tym, jak synchronizacja wypisała folder nad nim, synchronizacja
    wypisuje w jego miejscu cel dowiązania i przetwarza te pliki pod ścieżką
    folderu. Żadne sprawdzenie connectora tego nie widzi.

    Naprawa jest po stronie serwera:

    - Konto może czytać tylko to, co wolno synchronizować: chroot, którego
      zawartością jest synchronizowane drzewo (`ChrootDirectory` z
      `ForceCommand internal-sftp` w OpenSSH, `DefaultRoot` w ProFTPD), albo
      konto, którego uprawnienia sięgają tylko tego drzewa. Chroot, który
      zawiera też inne pliki, nadal udostępnia je przez dowiązanie. W Apache
      MINA SSHD wirtualny korzeń nie zatrzymuje dowiązania, więc ogranicz konto
      w systemie operacyjnym. SFTPGo odrzuca dowiązanie wskazujące poza korzeń
      użytkownika.
    - Konta, które wysyłają pliki, nie mogą tworzyć dowiązań ani dowiązań
      twardych: `internal-sftp -P symlink,hardlink` w OpenSSH,
      `<Limit SYMLINK> DenyAll` i `SFTPExtensions -hardlink` w ProFTPD oraz brak
      uprawnienia `create_symlinks` w SFTPGo. W ProFTPD samo `<Limit SYMLINK>`
      nie zatrzymuje dowiązania twardego.
    - Dowiązanie twarde (hard link) omija każde sprawdzenie i chroot. Ustawienie
      Linuksa `fs.protected_hardlinks=1`, domyślne w popularnych dystrybucjach,
      nie pozwala użytkownikowi dowiązać pliku, którego nie jest właścicielem
      i którego nie może czytać i zapisywać.

    Connector sprawdza też, czy otwarty plik jest plikiem, który wypisał.
    Właściciel i grupa muszą być takie jak na liście. Uprawnienia muszą być
    takie jak w stat tuż przed otwarciem, a także takie jak na liście, gdy ten
    stat podaje inny rozmiar albo czas modyfikacji niż lista. Uprawnienia z listy
    mogą być sfałszowane, jak robi to `DirFakeMode` w ProFTPD, więc niezmieniony
    plik jest tam nadal czytany. Plik, który naprawdę się zmienił na takim
    serwerze, raz kończy się błędem, a następna synchronizacja go czyta.

    To obrona w głąb, a nie naprawa, i obejmuje tylko podmieniony plik.
    W OpenSSH i ProFTPD wykrywa podmianę na plik z innym właścicielem, grupą
    albo uprawnieniami, a plik kończy się błędem "The server opened a file for …
    that is not the file it listed. …".

    To sprawdzenie nic nie daje w Apache MINA SSHD i SFTPGo, które odpowiadają
    na nie na podstawie ścieżki. Nie wykrywa też dowiązania twardego, celu z tym
    samym właścicielem, grupą i uprawnieniami ani folderu podmienionego na
    dowiązanie, zanim synchronizacja go wypisała. W ProFTPD nie wykrywa
    dowiązania umieszczonego przed stat do pliku o właścicielu, grupie, rozmiarze
    i czasie modyfikacji podanych na liście.

Log workera to wyjście procesu workera. W deploymencie compose odczytasz go
poleceniem `docker compose logs worker`. Zawiera odpowiedź samego serwera, której
log synchronizacji nigdy nie pokazuje, oraz powód, dla którego plik się nie
powiódł.

Każde zdanie, które wypisywanie dodaje do logu synchronizacji, liczy się jako
nieudany plik. Przebieg ma więc status `error`, dopóki przyczyna nie zostanie
usunięta na serwerze albo dopóki węższy folder jej nie pominie. Log
synchronizacji pokazuje najwyżej trzy uwagi, a pozostałe liczy jako "And …
more.". Usuń przyczyny, które pokazuje, i zsynchronizuj ponownie, aby zobaczyć
kolejne.

Znak sterujący w nazwie pliku albo folderu, taki jak koniec wiersza albo
separator wiersza lub akapitu Unicode (U+2028, U+2029), jest pokazywany jako `?`
w logu synchronizacji i we własnych zdaniach connectora w logu workera. Plik jest
mimo to synchronizowany pod własną nazwą. Wiersz workera `Failed to sync …`
pokazuje nazwę tak, jak filtr logu zapisuje ją ze znakami ucieczki, na przykład
`\n` dla końca wiersza.

Synchronizacja używa jednego połączenia do wypisania listy i wszystkich pobrań.
Worker wysyła keepalive co 30 sekund, a serwer, który nie odpowie na trzy, jest
uznawany za niedostępny. Połączenie albo sesja SFTP, które serwer zamknął między
dwoma plikami, są otwierane ponownie dla następnego pliku.

Wypisywanie czyta naraz do 4 folderów przez to połączenie, około 4 obiegi na
folder. Mały plik kosztuje wtedy około 7 obiegów: dwa staty ścieżki, otwarcie, dwa
staty otwartego pliku, jeden odczyt i zamknięcie. Przy 80 ms na obieg to około
pół sekundy na plik, przed parsowaniem i embedowaniem.

Synchronizacja może trwać 4 godziny, więc pierwszy import dużego drzewa przez
wolne łącze może zająć więcej niż jeden przebieg. Każdy przebieg zachowuje to, co
przetworzyły przebiegi przed nim, a `new_only` i `update_only` pomijają te pliki
przed pobraniem, więc następny przebieg kontynuuje tam, gdzie zatrzymał się
poprzedni.

Nieudane nawiązanie połączenia nie jest ponawiane w tej samej synchronizacji.
Dotyczy to odrzuconego logowania, przekroczenia limitu czasu i połączenia
utraconego przed startem sesji SFTP. Każdy kolejny plik kończy się błędem z tym
samym zdaniem, a drugie logowanie nie jest wysyłane. Zmienione hasło kosztuje
więc jedno odrzucone logowanie na synchronizację, a nie jedno na plik. Każda
synchronizacja nadal wysyła jedno, więc krótki harmonogram albo powtarzane ręczne
synchronizacje mogą osiągnąć limit blokady serwera. Gdy logowanie jest
odrzucane, popraw je albo wstrzymaj źródło.

!!! warning "Jedno błędne logowanie może zatrzymać inne źródła na serwerze OpenSSH"

    OpenSSH 9.8 i nowsze przez pewien czas odrzucają połączenia z adresu, z
    którego logowania się nie powiodły (`PerSourcePenalties`, domyślnie
    włączone). Wszystkie źródła deploymentu zwykle łączą się z jednego adresu,
    adresu workerów. Jedno źródło z błędnym hasłem może więc sprawić, że
    synchronizacje innych źródeł na tym samym serwerze kończą się błędem
    [połączenie zostało utracone](#sftp-the-connection-to-was-lost), choć ich
    własne logowania są poprawne.

### Co ogranicza synchronizację SFTP { #what-bounds-an-sftp-sync }

Stałe limity chronią workera przed zepsutym albo wrogim serwerem. Nie są
ustawieniami. Wypisywanie, które osiągnie jeden z pierwszych sześciu,
zatrzymuje się przed końcem, wymienia ten limit w logu synchronizacji i niczego
nie usuwa.
Wybieraj węższy folder, aż synchronizacja skończy się bez tego komunikatu.

| Limit | Wartość | Gdy zostanie osiągnięty |
|---|---|---|
| Poziomy folderów poniżej `root_path` | 32 | Wypisywanie się zatrzymuje |
| Foldery na liście | 10 000 | Foldery ponad limit nie są czytane |
| Nazwy na liście, łącznie z `.` i `..` | 1 000 000 | Wypisywanie się zatrzymuje |
| Pliki skonfigurowanych typów na liście | 100 000 | Wypisywanie się zatrzymuje |
| Pamięć zajmowana przez ścieżki wypisanych plików | 64 MB, około jeden bajt na znak dla ścieżki ASCII i do czterech dla innych znaków | Wypisywanie się zatrzymuje |
| Całe wypisywanie | 30 minut | Wypisywanie się zatrzymuje |
| Jedna nazwa i jedna ścieżka | 255 bajtów i 4096 bajtów | Nazwa jest pomijana |
| Nawiązanie połączenia i logowanie | 30 sekund | Nawiązanie połączenia kończy się błędem |
| Jedno żądanie SFTP | 30 sekund | Worker zrywa połączenie. Żądanie wypisywania kończy błędem synchronizację, a żądanie pobierania kończy błędem ten plik |
| Pobranie jednego pliku | 10 minut | Plik kończy się błędem |
| Rozmiar jednego pliku | `MAX_UPLOAD_SIZE_MB`, domyślnie 50 MB | Plik kończy się błędem |

### Reguły sieciowe SFTP { #sftp-network-rules }

Host źródła jest rozwiązywany i sprawdzany tak jak każdy inny adres wybrany przez
tenanta. Host, który rozwiązuje się na adres prywatny, loopback, link-local,
CGNAT albo zarezerwowany, jest odrzucany przy zapisie źródła i ponownie przy
synchronizacji. Worker łączy się potem wyłącznie z adresami, które przeszły
sprawdzenie, i nigdy nie rozwiązuje nazwy po raz drugi.

Serwer plików w sieci firmowej ma adres prywatny. Operator może wpisać go do
`SYNC_ALLOWED_HOSTS` (zobacz [ustawienia
projektu](../configuration.md#project-settings)). Ustaw tę zmienną dla API i dla
workera. API rozwiązuje i sprawdza host przy zapisie źródła, a worker robi to
ponownie przy synchronizacji, więc oba muszą też umieć rozwiązać nazwę.

Host z listy może rozwiązywać się na adres prywatny albo CGNAT. Nigdy nie może
rozwiązywać się na adres loopback, link-local, multicast ani metadanych chmury. Lista obowiązuje cały
deployment: źródła każdej organizacji mogą sięgnąć do każdego portu hosta
z listy. Czytają ją tylko źródła SFTP. Źródła Git i stron internetowych
zachowują zwykłe sprawdzenie adresu.

Każdy wpis zapisuj tak jak **Host** źródła: sama nazwa albo sam adres IP. Lista
dopasowuje host, który wpisuje źródło, a nie adres, na który się on rozwiązuje.
Dla źródła, którego **Host** to `files.corp.example`, wpisz `files.corp.example`:
sam jego adres `10.0.0.5` na to nie pozwala. Wpis z portem, schematem, ścieżką, zakresem, symbolem wieloznacznym albo kropką na
końcu zatrzymuje wczytywanie ustawień, a błąd startu wymienia ten wpis. Tak samo
działa wpis, który po normalizacji takiej jak dla hosta źródła oznacza host
loopback, link-local albo metadanych, na przykład `localhost` zapisany literami
pełnej szerokości.

Każda organizacja może dodać własny login dla hosta z listy i spróbować go tam.
Organizacja bez ważnego logowania i tak dowiaduje się, czy host odpowiada przez
SSH i czy jego klucz hosta się zgadza. Wpisuj więc tylko serwer, którego konta są
kontrolą dostępu.

Worker łączy się z portem SSH serwera bezpośrednio. Proxy HTTP, takie jak
`HTTPS_PROXY`, go nie dotyczy.

Worker ignoruje całą własną konfigurację SSH:

- `~/.ssh/config`, `~/.ssh/known_hosts` i klucze w `~/.ssh`
- agenta SSH pod `SSH_AUTH_SOCK`
- bilet Kerberos, którego wymiana kluczy pomija sprawdzenie klucza hosta

Przypięty klucz to jedyny klucz hosta, któremu ufa worker. Worker próbuje
wyłącznie tej jednej metody logowania, którą zawiera login w vault, i nigdy
keyboard-interactive. Serwer, który wpuszcza workera bez logowania, jest
odrzucany, bo taki serwer niczego nie sprawdził.

Kompresja jest wyłączona. asyncssh nie ogranicza, do jakiego rozmiaru
rozpakowuje się skompresowany pakiet, więc serwer mógłby sprawić, że worker
zużyje setki megabajtów na każdy pakiet. Serwer, który oferuje wyłącznie
połączenia skompresowane, jest odrzucany [zdaniem o wymianie
kluczy](#sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher).

!!! note "Worker potrzebuje lokalnej nazwy użytkownika"

    asyncssh wywołuje `getpass.getuser()` przy każdym połączeniu i nic tego nie
    wyłącza. Nazwa nie jest wysyłana. Dostarczany obraz ma wpis passwd dla
    swojego użytkownika. Worker działający jako użytkownik bez wpisu passwd
    potrzebuje zmiennej `LOGNAME`. Dotyczy to własnego obrazu, a także
    dostarczanego obrazu uruchomionego z innym id użytkownika, na przykład przez
    `docker run --user` albo `runAsUser` w Kubernetes. Bez niej każda
    synchronizacja SFTP kończy się błędem, a log synchronizacji pokazuje "The
    sync did not finish (OSError) - check the source's credential, then run it
    again. …". Przyczyną nie jest poświadczenie.

## Dokumentacja API { #api-reference }

Wszystkie endpointy źródeł synchronizacji leżą pod `/api/v1/rag/sync/`.
Wypisanie listy wymaga `collections:view`, a wszystko, co zmienia źródło, wymaga
`collections:edit` — w obu przypadkach wobec kolekcji, do której źródło należy;
rola administratora w tym nie występuje. Zobacz
[kto może sięgnąć do kolekcji](../file-processing.md#who-may-reach-a-collection).

### CRUD źródeł synchronizacji { #sync-sources-crud }

| Metoda | Endpoint | Opis |
|--------|----------|-------------|
| `GET` | `/api/v1/rag/sync/sources` | Wypisuje wszystkie skonfigurowane źródła synchronizacji |
| `POST` | `/api/v1/rag/sync/sources` | Tworzy nowe źródło synchronizacji |
| `PATCH` | `/api/v1/rag/sync/sources/{id}` | Zmienia istniejące źródło synchronizacji |
| `DELETE` | `/api/v1/rag/sync/sources/{id}` | Usuwa źródło synchronizacji |
| `POST` | `/api/v1/rag/sync/sources/{id}/trigger` | Ręcznie wyzwala synchronizację |

### Connectory i logi { #connectors-logs }

| Metoda | Endpoint | Opis |
|--------|----------|-------------|
| `GET` | `/api/v1/rag/sync/connectors` | Wypisuje dostępne typy connectorów wraz ze schematami konfiguracji |
| `GET` | `/api/v1/rag/sync/logs` | Wypisuje historię synchronizacji (filtrowaną po `collection_name`) |

### Przykład: utworzenie źródła przez API { #example-create-a-source-via-api }

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

### Przykład: wyzwolenie synchronizacji przez API { #example-trigger-a-sync-via-api }

```bash
curl -X POST http://localhost:8000/api/v1/rag/sync/sources/{source_id}/trigger \
  -H "Authorization: Bearer $TOKEN"
```

### Przykład: sprawdzenie historii synchronizacji { #example-check-sync-history }

```bash
curl http://localhost:8000/api/v1/rag/sync/logs?limit=10 \
  -H "Authorization: Bearer $TOKEN"
```

### Przykład: wykrycie dostępnych connectorów { #example-discover-available-connectors }

```bash
curl http://localhost:8000/api/v1/rag/sync/connectors \
  -H "Authorization: Bearer $TOKEN"
```

Odpowiedź zawiera `config_schema` każdego connectora, na podstawie którego
frontend renderuje dynamiczne formularze. Przydaje się też przy programowym
budowaniu integracji.

## Zmiana źródła { #updating-a-source }

Metodą `PATCH` możesz zmienić dowolny podzbiór pól istniejącego źródła:

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

Pola, które można zmienić: `name`, `config`, `sync_mode`, `schedule_minutes`,
`is_active`, `collection_name`.

Ustaw `is_active` na `false`, aby wstrzymać źródło bez usuwania go.

## Monitorowanie operacji synchronizacji { #monitoring-sync-operations }

Każda synchronizacja tworzy wpis `SyncLog` z następującymi polami:

| Pole | Opis |
|-------|-------------|
| `source` | Typ connectora albo `"local"` dla przetwarzania z CLI |
| `collection_name` | Kolekcja docelowa |
| `status` | `running`, `done` albo `error` |
| `mode` | `full`, `new_only` albo `update_only` |
| `total_files` | Liczba znalezionych plików |
| `ingested` | Poprawnie przetworzone (nowe) |
| `updated` | Poprawnie przetworzone ponownie (zastąpione) |
| `skipped` | Pominięte: już obecne lub bez zmian, nowe w `update_only` albo zmienione lub usunięte w trakcie przebiegu i zostawione następnemu przebiegowi |
| `failed` | Nieudane przetworzenie, łącznie ze stronami lub plikami, których lista nie zdołała odczytać, oraz dokumentami, których nie udało się usunąć |
| `removed` | Usunięte, bo źródło już ich nie wypisuje (zobacz [co usuwa synchronizacja](#what-a-sync-removes)) |
| `error_message` | Co poszło nie tak albo dlaczego nic nie usunięto. Przebieg może mieć status `done` i mimo to komunikat, na przykład gdy crawl zatrzymał się na limicie stron albo gdy pliki zmieniły się lub zostały usunięte w trakcie przebiegu i czekają na następny przebieg. To, czy przebieg się nie powiódł, mówi `status` |
| `started_at` | Kiedy synchronizacja się zaczęła |
| `completed_at` | Kiedy synchronizacja się skończyła |

Logi obejrzysz w wyjściu CLI albo przez API:

```bash
curl http://localhost:8000/api/v1/rag/sync/logs?collection_name=legal&limit=5 \
  -H "Authorization: Bearer $TOKEN"
```

## Dodawanie własnych connectorów { #adding-custom-connectors }

Aby dodać nowy typ connectora (na przykład Notion, Confluence, Dropbox), zobacz
[Dodaj connector synchronizacji](./add-sync-connector.md).

W skrócie:

1. Utwórz klasę dziedziczącą po `BaseSyncConnector` w
   `app/services/rag/connectors/`.
2. Zaimplementuj `list_files()`, `_fetch()` i opcjonalnie `validate_config()`.
3. Zadeklaruj `SECRET_KIND` — rodzaj sekretu w vault, który go uwierzytelnia —
   oraz `CONFIG_MODEL`, model Pydantic mówiący, jak odnaleźć dokumenty.
   Poświadczenie nigdy nie jest jednym z jego pól.
4. Zarejestruj go w `CONNECTOR_REGISTRY` w
   `app/services/rag/connectors/__init__.py`.

Po zarejestrowaniu connector pojawia się samoczynnie w CLI, API i w
interfejsie.

## Rozwiązywanie problemów { #troubleshooting }

### "No sync sources configured" { #no-sync-sources-configured }

Nie utworzono jeszcze żadnego źródła. Utwórz je poleceniem `rag-source-add`
(CLI) albo `POST /api/v1/rag/sync/sources` (API).

### "Unknown connector type" { #unknown-connector-type }

Podany typ connectora nie występuje w `CONNECTOR_REGISTRY`. Sprawdź dostępne
typy poleceniem `rag-sources` albo `GET /api/v1/rag/sync/connectors`.
Google Drive (`gdrive`) jest dostępny.
S3 (`s3`) jest dostępny.
Strona internetowa (`web`) jest dostępna.
Git (`git`) jest dostępny.
SFTP (`sftp`) jest dostępny.

### Google Drive: "this source has no credential" { #google-drive-this-source-has-no-credential }

Pole `secret_id` źródła jest puste albo sekret w vault, który wskazywało,
został usunięty. Dodaj JSON konta usługi do vault i wybierz go w kroku
poświadczenia źródła — `GOOGLE_DRIVE_CREDENTIALS_FILE` go nie zastępuje,
a to ustawienie czyta wyłącznie komenda CLI `rag-sync-gdrive`.

### "A Google Drive source needs a service account credential" { #a-google-drive-source-needs-a-service-account-credential }

`secret_id` wskazuje poświadczenie niewłaściwego rodzaju — na przykład parę
kluczy AWS. Źródło Drive przyjmuje `gcp_service_account`, a źródło S3 parę
`aws_credentials`; kreator proponuje wyłącznie pasujące, więc ten przypadek
osiągalny jest przez API.

### Google Drive: "folder ID may contain only letters, digits, '-' and '\_'" { #google-drive-folder-id-may-contain-only-letters-digits-and-_ }

Wartość nie jest identyfikatorem folderu Drive. Weź ją z adresu URL folderu:
to ostatni segment, a nic innego z tego adresu do tego pola nie należy.

### Google Drive: "Cannot access folder" { #google-drive-cannot-access-folder }

Upewnij się, że udostępniono folder adresowi e-mail konta usługi. Konto usługi
potrzebuje co najmniej dostępu Viewer.

### S3: "Cannot access bucket" { #s3-cannot-access-bucket }

Sprawdź, czy `S3_RAG_ACCESS_KEY`, `S3_RAG_SECRET_KEY` i `S3_RAG_ENDPOINT` są
poprawnie ustawione w `.env`. W MinIO upewnij się, że endpoint zawiera port
(na przykład `http://localhost:9000`).

### Website: "resolves to private/internal address" { #website-resolves-to-privateinternal-address }

Początkowy adres URL albo sitemapa wskazuje do wnętrza sieci deploymentu albo
jego nazwa się tam rozwiązuje. Źródło strony internetowej czyta wyłącznie adresy
publiczne. Aby zaindeksować wewnętrzną witrynę, opublikuj jej strony gdzieś
publicznie albo prześlij pliki bezpośrednio.

### Website: "The site's robots.txt could not be read, so it was not crawled" { #website-the-sites-robotstxt-could-not-be-read-so-it-was-not-crawled }

`/robots.txt` na hoście początkowego adresu URL trzy razy z rzędu przekroczył
limit czasu albo odpowiedział błędem serwera (5xx). Crawler nie zgaduje, na co
pozwoliłby nieosiągalny robots.txt, więc przebieg się zatrzymuje. Brakujący
robots.txt (404) albo zabroniony (403) oznacza brak reguł i crawl rusza.

### Website: "The start URL … did not lead to an HTML page" { #website-the-start-url-did-not-lead-to-an-html-page }

Początkowy adres URL odpowiedział 404, przekierował na inny host albo poza
`path_prefix`, albo zwrócił coś innego niż HTML. Otwórz go w przeglądarce, a
potem użyj adresu, pod którym się kończy, jako `root_url`.

### Website: "robots.txt does not allow the start URL" { #website-robotstxt-does-not-allow-the-start-url }

Witryna prosi crawlery, by trzymały się z dala od tej ścieżki. Wybierz początkowy
adres URL, na który witryna pozwala, albo poproś jej właściciela o dopuszczenie
`AgenticOS-Crawler`.

### Website: "… answered HTTP 403" albo "… could not be reached" { #website-answered-http-403-or-could-not-be-reached }

Strona wymaga logowania albo witryna odmówiła crawlerowi. Jeśli odpowiedzią był
przekroczony limit czasu, 429 albo 5xx, strona zawiodła po trzech próbach. Każda
taka strona liczy się jako nieudany plik. W tym przebiegu nic nie jest usuwane, bo
strony za nią nie zostały zobaczone.

### "The source could not be listed completely, so documents it may no longer hold were kept" { #the-source-could-not-be-listed-completely-so-documents-it-may-no-longer-hold-were-kept }

Lista urwała się przed końcem: crawl osiągnął `max_pages`, części stron nie
udało się odczytać, nie udało się wypisać folderu SharePoint albo wypisywanie
SFTP osiągnęło limit lub nie zdołało odczytać folderu albo wpisu. To, co
znaleziono, zostało przetworzone, a nic nie usunięto. W przypadku strony
internetowej zwiększ `max_pages` albo zawęź crawl przez `path_prefix`, aż przebieg
skończy się bez tego komunikatu. W przypadku SharePoint i SFTP zobacz wpisy
dotyczące listy poniżej.

### Git: "The repository refused the source's token" { #git-the-repository-refused-the-sources-token }

Token wygasł, został unieważniony albo nie może czytać tego repozytorium. Wystaw
nowy zgodnie z opisem w [Konfiguracja repozytorium Git](#git-repository-setup)
i podmień wartość sekretu w vault, którego używa źródło; każde źródło korzystające
z tego sekretu pobierze ją przy następnej synchronizacji.

### Git: "The repository was not found, or the source's token cannot see it" { #git-the-repository-was-not-found-or-the-sources-token-cannot-see-it }

Najpierw sprawdź adres URL klonowania. Prywatne repozytorium odpowiada tokenowi,
który nie może go czytać, *not found* zamiast *forbidden*, więc fine-grained
token wystawiony dla innego repozytorium wygląda właśnie tak.

### Git: "The repository has no branch named …" { #git-the-repository-has-no-branch-named }

Pole `branch` wskazuje gałąź, której repozytorium nie ma. Domyślnie jest to
`main`; w starszym repozytorium gałęzią domyślną może być `master`.

### Git: "… is … MB, and a synced file may be at most … MB" { #git-is-mb-and-a-synced-file-may-be-at-most-mb }

Plik pasujący do wzorców `include` jest większy niż limit dokumentu bazy wiedzy.
Zawęź `include` albo `path_prefix` tak, by ten plik został pominięty. W tej
synchronizacji nic nie zostało zapisane.

### Git: "… over the … MB one sync may check out" { #git-over-the-mb-one-sync-may-check-out }

Wszystkie pliki pasujące do wzorców `include` mają łącznie ponad 512 MB. Zawęź
`include` albo `path_prefix` albo podziel repozytorium na kilka źródeł, każde
z własnym prefiksem.

### Git: "This token was added for …, and the repository is on …" { #git-this-token-was-added-for-and-the-repository-is-on }

Repozytorium źródła jest na innym hoście niż ten, z którym dodano jego token.
Albo adres URL jest błędny, albo źródło potrzebuje tokena dodanego dla tego
hosta. Do hosta repozytorium nic nie zostało wysłane.

### Git: "A Git source needs a Git access token" { #git-a-git-source-needs-a-git-access-token }

Źródło wskazuje sekret innego rodzaju, na przykład API key. Dodaj token jako
**Git access token**, razem z jego hostem, i wybierz właśnie ten.

### "Another sync of this source is still running" { #another-sync-of-this-source-is-still-running }

Przebieg został wyzwolony, gdy trwał inny przebieg tego samego źródła, więc nie
wystartował. Trwający przebieg kończy się normalnie; wyzwól synchronizację
ponownie po nim, jeśli źródło w międzyczasie się zmieniło.

### Git: "… resolves to a private address" { #git-resolves-to-a-private-address }

Host repozytorium rozwiązuje się na adres wewnątrz sieci deploymentu, więc
źródło jest odrzucane. Źródło synchronizacji nie może dotrzeć do
samodzielnie hostowanego serwera Git pod adresem wewnętrznym.

### Git: "git is not installed on this worker" { #git-git-is-not-installed-on-this-worker }

Worker działa z obrazu bez `git`. Dostarczany `backend/Dockerfile` go instaluje;
do własnego obrazu trzeba go dodać.

### Git: synchronizacja zakończyła się bez przetworzonych plików { #git-a-sync-finished-with-no-files-processed }

Commit na czubku gałęzi jest tym samym, który przeczytał ostatni czysty
przebieg, przy tej samej konfiguracji, więc nie było nic do zrobienia. Przełącz
źródło na `full` na jeden przebieg, aby mimo to przeczytać wszystko od nowa.

### SharePoint: "Microsoft Entra refused the app registration's credentials (…)" { #sharepoint-microsoft-entra-refused-the-app-registrations-credentials }

Microsoft Entra nie zalogował aplikacji. `invalid_client` zwykle oznacza, że
client secret wygasł albo vault przechowuje ID sekretu zamiast jego wartości.
`unauthorized_client` albo `invalid_request` zwykle oznacza błędny tenant id albo
client id. Porównaj te trzy wartości z **Overview** i **Certificates & secrets**
aplikacji i podmień je w sekrecie w vault.

### SharePoint: "Microsoft Graph did not accept the app's token for …" { #sharepoint-microsoft-graph-did-not-accept-the-apps-token-for }

Aplikacja się zalogowała, ale jej token nie niesie żadnego uprawnienia Microsoft
Graph. Dodaj uprawnienie typu **application** `Sites.Selected`, a nie delegowane,
i udziel zgody administratora, zgodnie z opisem w
[kroku 2](#2-grant-it-one-site-not-the-tenant).

### SharePoint: "Microsoft Graph denied the app access to … (accessDenied)" { #sharepoint-microsoft-graph-denied-the-app-access-to-accessdenied }

Aplikacja ma `Sites.Selected`, ale żadnego przyznania na tej witrynie. Przyznaj
jej `read` na witrynie, zgodnie z opisem w
[kroku 2](#2-grant-it-one-site-not-the-tenant). Jeśli komunikat wymienia folder
poniżej folderu źródła, ten folder ma własne uprawnienia, które wykluczają
aplikację. Reszta biblioteki została przetworzona, a nic nie usunięto.

### SharePoint: "There is no SharePoint site at …, or the app cannot see it" { #sharepoint-there-is-no-sharepoint-site-at-or-the-app-cannot-see-it }

Najpierw sprawdź adres URL witryny: to własny adres witryny, na przykład
`https://contoso.sharepoint.com/sites/HR`, a nie biblioteka czy strona wewnątrz
niej. Przy `Sites.Selected` tak samo może odpowiedzieć witryna, na której
aplikacja nie ma przyznania.

### SharePoint: "The site has no document library named …" { #sharepoint-the-site-has-no-document-library-named }

Komunikat wymienia biblioteki, które aplikacja widzi. Skopiuj jedną z tych nazw do
`library` albo zostaw `library` puste, aby użyć domyślnej biblioteki witryny.
Domyślna biblioteka jest nazwana w języku witryny, na przykład *Documents* albo
*Dokumente*.

### SharePoint: "The library has no folder …" albo "… is a file, not a folder" { #sharepoint-the-library-has-no-folder-or-is-a-file-not-a-folder }

`folder_path` nie wskazuje folderu w bibliotece. Jest podawany względem
najwyższego poziomu biblioteki, np. `Policies/HR`, i nie powtarza nazwy
biblioteki.

### SharePoint: "The folder … could not be listed: …" { #sharepoint-the-folder-could-not-be-listed }

Nie udało się odczytać folderu poniżej folderu źródła. Przyczyna następuje po
dwukropku. Pozostałe foldery zostały przetworzone, a w tym przebiegu nic nie
usunięto.

### SharePoint: "Microsoft 365 stayed unavailable or kept throttling … after 4 attempts" { #sharepoint-microsoft-365-stayed-unavailable-or-kept-throttling-after-4-attempts }

Graph nadal dławił zapytania albo zwracał błędy po czterech próbach. Następna
synchronizacja zaczyna od nowa. Jeśli dzieje się tak przy każdej synchronizacji,
planuj źródło rzadziej albo podziel bardzo dużą bibliotekę na kilka źródeł, każde
z własnym `folder_path`.

### SharePoint: "A SharePoint source needs a Microsoft Entra app credential" { #sharepoint-a-sharepoint-source-needs-a-microsoft-entra-app-credential }

Źródło wskazuje sekret innego rodzaju, na przykład API key. Dodaj aplikację do
vault jako **Microsoft Entra app** i wybierz właśnie tę.

### SharePoint: synchronizacja zakończyła się bez przetworzonych plików { #sharepoint-a-sync-finished-with-no-files-processed }

Od ostatniego czystego przebiegu przy tej samej konfiguracji nic w bibliotece się
nie zmieniło, więc nie było nic do zrobienia. Przełącz źródło na `full` na jeden
przebieg, aby mimo to przeczytać wszystko od nowa.

### SFTP: "Paste only the key type and the key, without the host name or the options in front of them." { #sftp-paste-only-the-key-type-and-the-key-without-the-host-name-or-the-options-in-front-of-them }

**Host key** zawiera wiersz z `ssh-keyscan` albo `known_hosts`, z nazwą hosta
przed kluczem, albo wiersz `authorized_keys` z opcjami przed kluczem. Usuń
pierwsze pole. Dla pliku z [kroku 1](#1-get-the-servers-host-key) robi to
`cut -d' ' -f2- host_key.txt`.

### SFTP: "Paste the server's public host key as one line, with its type first, …" { #sftp-paste-the-servers-public-host-key-as-one-line-with-its-type-first }

**Host key** zawiera coś innego niż jeden wiersz klucza publicznego: odcisk
z `ssh-keygen -lf`, dwa wiersze, blok PEM albo uszkodzony klucz. Wklej sam klucz,
na przykład `ssh-ed25519 AAAAC3Nz...`. Blok PEM jest odrzucany, zanim zostanie
odczytany, bo klucz prywatny wklejony przez pomyłkę nie może zostać odczytany.

### SFTP: "The host key must be of type ssh-ed25519, ssh-ed448, …" { #sftp-the-host-key-must-be-of-type-ssh-ed25519-ssh-ed448 }

Klucz jest typu, którego connector nie przypina, na przykład `ssh-dss`,
certyfikat hosta albo klucz sprzętowy (security key). Pobierz z serwera klucz
jednego z wymienionych typów, na przykład poleceniem `ssh-keyscan -t ed25519`,
`-t ecdsa` albo `-t rsa`.

### SFTP: "The host is a host name or an IP address alone, …" albo "Put the port in the Port field, …" { #sftp-the-host-is-a-host-name-or-an-ip-address-alone-or-put-the-port-in-the-port-field }

**Host** zawiera więcej niż sam host, na przykład
`sftp://sftp.example.com/outgoing`, `sync@sftp.example.com` albo
`sftp.example.com:2222`. Wpisz tylko nazwę albo adres. Port wpisz w **Port**,
a folder w **Folder**.

Odrzucana jest też kropka na końcu: usuń ją. Nazwa zawiera tylko litery, cyfry,
łączniki, podkreślenia i kropki, na przykład `sftp_server` albo
`files-01.corp.example`.

### SFTP: folder albo typ pliku odrzucony przy zapisie źródła { #sftp-a-folder-or-a-file-type-refused-when-the-source-is-saved }

- "The folder is an absolute path on the server, e.g. /outgoing." **Folder** nie
  zaczyna się od `/`. Wpisz pełną ścieżkę tak, jak widzi ją konto na serwerze.
- "The folder may not hold '..'. Name the folder itself." Wpisz własną ścieżkę
  folderu, bez `..`.
- "The folder may not hold a line break or another control character." Usuń ten
  znak. Może go wprowadzić wklejenie, także separator wiersza lub akapitu
  Unicode (U+2028, U+2029).
- "The folder may be at most 1024 characters long." Wybierz folder o krótszej
  ścieżce.
- "'…' is not a file extension, e.g. .pdf." Wpis w **File types** nie jest
  kropką, po której następuje od 1 do 10 liter lub cyfr.
- "At least one file type is needed." **File types** jest puste. Dodaj co
  najmniej jeden typ, na przykład `.pdf`.
- "At most 100 file types can be listed." **File types** ma więcej niż 100
  wpisów. Zostaw typy, które czyta parser kolekcji.
- "A file type is at most 16 characters, e.g. .pdf." Wpis w **File types** jest
  dłuższy niż 16 znaków. Wpisz samo rozszerzenie, na przykład `.pdf`.

**File types** źródła SharePoint daje te same cztery ostatnie zdania. Oba
limity obowiązują przy zapisie źródła i liczą typy po usunięciu pustych wpisów
i powtórzonych typów. Źródło zapisane z większą liczbą typów nadal się
synchronizuje i jest odrzucane dopiero przy ponownym zapisie.

### SFTP: "Blocked: '…' resolves to private/internal address '…'. …" { #sftp-blocked-resolves-to-privateinternal-address }

Host rozwiązuje się wewnątrz sieci deploymentu. Operator może wpisać serwer
plików z sieci firmowej do `SYNC_ALLOWED_HOSTS`, zgodnie z opisem w [regułach
sieciowych SFTP](#sftp-network-rules). Host, który rozwiązuje się na adres
loopback, link-local albo metadanych chmury, pozostaje odrzucony, także gdy jest
na liście. Dla hosta wpisanego jako adres IP zdanie brzmi "Blocked: '…' resolves
to a private/internal address. SSRF protection does not allow requests to
internal networks." Przyczyna i naprawa są te same.

Wpisz na listę host tak, jak nazywa go źródło, a nie adres ze zdania. Ustaw
`SYNC_ALLOWED_HOSTS` dla API i dla workera. Hosta źródła odrzuconego przy zapisie
brakuje na liście API. Hosta źródła odrzuconego dopiero przy synchronizacji
brakuje na liście workera.

"Blocked: unable to resolve hostname …" oznacza, że hosta w ogóle nie udało się
rozwiązać. Sprawdź jego pisownię. API rozwiązuje host przy zapisie źródła,
a worker przy synchronizacji, więc oba muszą umieć rozwiązać nazwę.

### "A credential does not go in a source's configuration" { #a-credential-does-not-go-in-a-sources-configuration }

`config` zawiera pole będące poświadczeniem, na przykład `password`,
`private_key` albo `passphrase` w przypadku źródła SFTP. Dodaj poświadczenie do
vault i ustaw na nie `secret_id` źródła.

### SFTP: "An SSH login needs a private key or a password, and not both." { #sftp-an-ssh-login-needs-a-private-key-or-a-password-and-not-both }

Login ma jednocześnie klucz prywatny i hasło albo nie ma żadnego z nich. Daj mu
jeden sposób logowania: klucz prywatny, z hasłem klucza (passphrase), jeśli je
ma, albo hasło (password). "A passphrase goes with a private key." oznacza, że
login ma hasło klucza i hasło. Usuń hasło klucza.

### SFTP: "The private key could not be read. …" { #sftp-the-private-key-could-not-be-read }

Vault nie zdołał odczytać klucza, gdy zapisywałeś login. Wklej cały plik klucza,
od wiersza `-----BEGIN` do wiersza `-----END`. Jeśli klucz jest zaszyfrowany,
podaj też jego hasło klucza. "The private key could not be read with this
passphrase." oznacza, że hasło klucza jest błędne albo że wklejona treść nie jest
kluczem, który odczytuje vault.

Pierwszy wiersz `-----BEGIN … PRIVATE KEY-----` musi być jednym z `OPENSSH PRIVATE
KEY`, `PRIVATE KEY`, `ENCRYPTED PRIVATE KEY`, `RSA PRIVATE KEY`, `EC PRIVATE KEY`
albo `DSA PRIVATE KEY`. Klucz w binarnej postaci DER jest odrzucany, podobnie jak
klucz PKCS#8 pod nagłówkiem PEM `Proc-Type: 4,ENCRYPTED`. Wyeksportuj taki klucz
ponownie w jednej z tych postaci. Klucz, który odczytuje `ssh-keygen`, polecenie
`ssh-keygen -p -f <key file>` zapisuje w postaci OpenSSH. Klucz PuTTY (`.ppk`)
także jest odrzucany, a `ssh-keygen` nie potrafi go odczytać. Wyeksportuj go
w postaci OpenSSH programem PuTTYgen (**Conversions** → **Export OpenSSH key**)
albo poleceniem `puttygen <key>.ppk -O private-openssh -o <key file>`.

### SFTP: "The private key is encrypted with too many key derivation rounds. …" { #sftp-the-private-key-is-encrypted-with-too-many-key-derivation-rounds }

Zaszyfrowany klucz podaje, ile pracy wymaga sprawdzenie jego hasła klucza. Vault
odrzuca klucz, który wymaga więcej niż 128 rund bcrypt (OpenSSH) albo więcej niż
1 000 000 iteracji lub klucza pochodnego dłuższego niż 64 bajty (PKCS#8).
Odczytuje to z klucza, zanim cokolwiek odszyfruje. Zaszyfruj klucz ponownie
z mniejszą liczbą rund, na przykład poleceniem
`ssh-keygen -p -a 16 -f <key file>`, i wklej go ponownie.

### SFTP: "The private key takes too long to open. …" { #sftp-the-private-key-takes-too-long-to-open }

Pełne zdanie brzmi "The private key takes too long to open. Use an Ed25519 key,
or an RSA key of at most 4096 bits.". Vault otwiera każdy klucz w osobnym
procesie i zatrzymuje go po 5 sekundach. To zdanie daje klucz, którego
zaszyfrowana część wymaga więcej pracy, niż mieści się w tych 5 sekundach, albo
bardzo obciążony host API. Utwórz
klucz Ed25519 zgodnie z opisem w [kroku 2](#2-add-an-ssh-login-to-the-vault)
i zapisz login ponownie.

### SFTP: "The private key is an RSA key of more than 4096 bits. …" { #sftp-the-private-key-is-an-rsa-key-of-more-than-4096-bits }

Pełne zdanie brzmi "The private key is an RSA key of more than 4096 bits. Use
an Ed25519 key, or an RSA key of at most 4096 bits.". Czas sprawdzenia klucza RSA
szybko rośnie wraz z jego rozmiarem, więc vault odrzuca klucz dłuższy niż 4096
bitów bez sprawdzania go. Utwórz klucz Ed25519 albo klucz RSA o długości
najwyżej 4096 bitów i dodaj jego publiczną połowę do `authorized_keys` konta.

### SFTP: "The private key is an RSA key whose two primes are not of the same size. …" { #sftp-the-private-key-is-an-rsa-key-whose-two-primes-are-not-of-the-same-size }

Pełne zdanie brzmi "The private key is an RSA key whose two primes are not of
the same size. Use an Ed25519 key, or an RSA key that ssh-keygen made.". Vault
odrzuca klucz RSA, którego dwie liczby pierwsze różnią się rozmiarem o więcej
niż 16 bitów, bez sprawdzania go. `ssh-keygen` i OpenSSL tworzą obie liczby
pierwsze tego samego rozmiaru, a sprawdzenie klucza z jedną dużą liczbą pierwszą
trwa długo. Utwórz nowy klucz poleceniem `ssh-keygen` zgodnie z opisem
w [kroku 2](#2-add-an-ssh-login-to-the-vault) i dodaj jego publiczną połowę do
`authorized_keys` konta.

### SFTP: "The private key is held on a security key, which a sync cannot use to log in. …" { #sftp-the-private-key-is-held-on-a-security-key-which-a-sync-cannot-use-to-log-in }

Pełne zdanie brzmi "The private key is held on a security key, which a sync
cannot use to log in. Use an Ed25519 key without a security key.". Plik klucza
to klucz `sk-`: uchwyt do klucza sprzętowego, na przykład utworzony poleceniem
`ssh-keygen -t ed25519-sk`. Worker nie ma klucza sprzętowego, więc nie może nim
podpisywać. Utwórz klucz Ed25519 poleceniem `ssh-keygen -t ed25519` zgodnie
z opisem w [kroku 2](#2-add-an-ssh-login-to-the-vault).

### SFTP: "Other private keys are being checked now. Try again in a moment." { #sftp-other-private-keys-are-being-checked-now-try-again-in-a-moment }

W tym samym procesie API sprawdzane są już dwa klucze prywatne, a vault sprawdza
najwyżej dwa naraz. Zapis jest od razu odrzucany ze statusem 503 i kodem
`SERVICE_BUSY`, aby żadne żądanie nie czekało, trzymając połączenie z bazą
danych. Zapisz login ponownie po kilku sekundach.

### SFTP: "The private key is too long. Paste only the key file, at most 16,384 characters." { #sftp-the-private-key-is-too-long-paste-only-the-key-file-at-most-16384-characters }

Pole **Private key** zawiera więcej niż 16 384 znaki. Plik klucza Ed25519 ma
około 400 znaków, a plik 4096-bitowego klucza RSA około 3300. Wklejona treść
zawiera więc więcej niż klucz albo nie jest kluczem. Wklej sam plik klucza
prywatnego, od wiersza `-----BEGIN` do wiersza `-----END`.

### SFTP: "The passphrase is too long: at most 1,024 characters." { #sftp-the-passphrase-is-too-long-at-most-1024-characters }

Pole **Key passphrase** zawiera więcej niż 1024 znaki. Wklej tylko hasło klucza,
które otwiera klucz. Hasła klucza, które naprawdę jest dłuższe, nie da się
zapisać: zaszyfruj klucz ponownie krótszym, poleceniem
`ssh-keygen -p -f <key file>`.

### SFTP: "The password is too long: at most 1,024 characters." { #sftp-the-password-is-too-long-at-most-1024-characters }

Pole **Password** zawiera więcej niż 1024 znaki. Wklej tylko hasło. Dla serwera,
którego hasło jest dłuższe, użyj zamiast niego klucza, zgodnie z opisem
w [kroku 2](#2-add-an-ssh-login-to-the-vault).

### SFTP: "The private key could not be checked. Try again later." { #sftp-the-private-key-could-not-be-checked-try-again-later }

Proces, który otwiera klucz, zatrzymał się z błędem i nie dał odpowiedzi. Nie
mówi to nic o samym kluczu. API odpowiada statusem 500. Po krótkim czasie zapisz
login ponownie. Jeśli znowu się nie uda, log API zawiera wiersz
`ssh_key_check_failed`. Kod wyjścia procesu jest w polu `returncode` rekordu,
które pokazuje tylko handler logów wyświetlający dodatkowe pola, taki jak
Logfire. Host API musi móc uruchomić proces Pythona, a ten proces musi móc
zaimportować asyncssh.

### SFTP: "The host is a host name or an IP address with an optional port, …" { #sftp-the-host-is-a-host-name-or-an-ip-address-with-an-optional-port }

**Host** w loginie nie jest jednym hostem z opcjonalnym portem. Wpisz nazwę albo
adres IP, a po nich `:` i port, jeśli nie jest to 22, na przykład
`sftp.example.com` albo `[2001:db8::7]:2222`. Adres IPv6 zapisuje się
w nawiasach kwadratowych. Schemat, ścieżka, symbol wieloznaczny albo spacja są
odrzucane. Nazwa zawiera tylko litery, cyfry, łączniki, podkreślenia i kropki.

### SFTP: "This SFTP source has no credential. …" { #sftp-this-sftp-source-has-no-credential }

Pole `secret_id` źródła jest puste, login, który wskazywało, został usunięty albo
worker nie zdołał otworzyć loginu. Worker sprawdza postać klucza za każdym razem,
gdy otwiera login, więc zapisany klucz, który już nie przechodzi tego
sprawdzenia, także daje to zdanie. Log workera zawiera wtedy
`sync_source_secret_unusable`. Dodaj login SSH do vault zgodnie z opisem
w [kroku 2](#2-add-an-ssh-login-to-the-vault) albo zapisz w loginie ponownie
klucz i jego hasło klucza. Potem wybierz go w kroku poświadczenia źródła.

### SFTP: "An SFTP source needs an SSH login, and the credential it names is not one." { #sftp-an-sftp-source-needs-an-ssh-login-and-the-credential-it-names-is-not-one }

Źródło, które wskazuje poświadczenie innego rodzaju, na przykład API key, jest
odrzucane przy zapisie ze zdaniem "A SFTP server source needs a ssh_credentials
credential, and that one is …". Dotyczy to kreatora, API i `rag-source-add`.
Wybierz **SSH login**. Zdanie z nagłówka pochodzi z własnego sprawdzenia
connectora przy synchronizacji, a źródło zapisane z loginem SSH go nie daje.

### SFTP: "This SSH login was added for …, and the source names …" { #sftp-this-ssh-login-was-added-for-and-the-source-names }

Host albo port źródła nie jest tym, dla którego dodano login. Albo **Host** lub
**Port** jest błędny, albo źródło potrzebuje loginu dodanego dla tego hosta.
Login dodany bez portu jest używany tylko na porcie 22. Do hosta źródła nic nie
zostało wysłane.

### SFTP: "… did not prove that it holds the host key in this source's settings. …" { #sftp-did-not-prove-that-it-holds-the-host-key-in-this-sources-settings }

Serwer odpowiedział innym kluczem hosta niż ten w **Host key**. Klucz serwera się
zmienił, na przykład dlatego, że serwer zainstalowano od nowa, albo worker nie
dotarł do prawdziwego serwera. Zanim zmienisz klucz, ponownie porównaj odcisk
z odciskiem od administratora, zgodnie z opisem w
[kroku 1](#1-get-the-servers-host-key). Żaden login nie został wysłany.

### SFTP: "… and this worker could not agree on a host key or a cipher. …" { #sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher }

Przy pierwszej synchronizacji najpierw sprawdź typ klucza hosta. Worker prosi
serwer wyłącznie o klucz typu podanego w **Host key**, a Apache MINA SSHD
odpowiada w ten sposób, gdy nie ma klucza tego typu. Pobierz klucz serwera innego
typu, zgodnie z opisem w [kroku 1](#1-get-the-servers-host-key). Jeśli typ jest
poprawny, serwer nie przyjmuje żadnego z szyfrów, które oferuje worker. Zapytaj
administratora, które z nich dopuszcza. Serwer, który oferuje wyłącznie
połączenia skompresowane, także daje to zdanie, bo worker nigdy nie włącza
kompresji.

### SFTP: "The connection to … was lost." { #sftp-the-connection-to-was-lost }

Przy pierwszej synchronizacji najpierw sprawdź typ klucza hosta. OpenSSH
i SFTPGo kończą połączenie w ten sposób, gdy nie mają klucza typu podanego
w **Host key**.

OpenSSH 9.8 i nowsze odrzucają też każde połączenie z adresu, z którego kilka
logowań się nie powiodło (`PerSourcePenalties`). Wszystkie źródła deploymentu
zwykle łączą się z jednego adresu, adresu workerów. Jedno źródło z błędnym
hasłem może więc sprawić, że synchronizacje innych źródeł na tym samym serwerze
przez pewien czas kończą się błędem z tym zdaniem.

Poszukaj źródła na tym samym hoście, którego ostatnia synchronizacja dała "The
login to … did not complete.". Popraw jego login albo je wstrzymaj. Potem
zsynchronizuj ponownie, gdy kara (penalty) się skończy: domyślnie rośnie ona
najwyżej do 10 minut. Log serwera pokazuje karę jako `srclimit_penalise`.
Administrator serwera może też zwolnić adres workerów z kar przez
`PerSourcePenaltyExemptList`. To usuwa karę dla źródeł każdej organizacji
w deploymencie, więc najpierw popraw login.

W innym przypadku sieć albo serwer zakończyły połączenie, zresetowały je albo
zakończyły sesję SFTP, albo serwer nie odpowiedział na trzy keepalive.
Wypisywanie, które traci połączenie, kończy błędem całą synchronizację, a ten
przebieg niczego nie usuwa. Pobieranie, które je traci, kończy błędem ten plik,
a następny plik otwiera nowe połączenie.

### SFTP: "… closed the connection." { #sftp-closed-the-connection }

Przy pierwszej synchronizacji najpierw sprawdź typ klucza hosta. ProFTPD kończy
połączenie w ten sposób, gdy nie ma klucza typu podanego w **Host key**.
W innym przypadku serwer zakończył połączenie w trakcie jego otwierania, na
przykład dlatego, że nie dopuszcza więcej sesji albo odrzuca adres workera. Log
workera zawiera powód podany przez sam serwer.

### SFTP: "The login to … as … did not complete. …" { #sftp-the-login-to-as-did-not-complete }

Serwer odrzucił nazwę użytkownika, klucz albo hasło. Sprawdź je w vault.
W przypadku klucza sprawdź, czy administrator dodał jego publiczną połowę do
`authorized_keys` konta. Serwer, który przyjmuje hasło wyłącznie przez logowanie
keyboard-interactive, odrzuca logowanie hasłem, więc tam użyj klucza. Serwer może
też odrzucić typ klucza. Apache MINA SSHD bez swojej biblioteki EdDSA odrzuca
klucz Ed25519, więc tam spróbuj klucza ECDSA. Serwer, który wpuszcza workera bez
logowania, też jest odrzucany z tym zdaniem.

Synchronizacja zapamiętuje tę odmowę, więc serwer dostaje jedno nieudane
logowanie, a nie jedno na plik. Każda późniejsza synchronizacja wysyła jeszcze
jedno. Na OpenSSH 9.8 i nowszych kilka z nich sprawia, że serwer przez pewien
czas odrzuca adres workerów, a inne źródła na tym serwerze kończą się wtedy
błędem [połączenie zostało utracone](#sftp-the-connection-to-was-lost). Popraw
login albo wstrzymaj źródło.

### SFTP: "The private key in the Vault could not be read with its passphrase." { #sftp-the-private-key-in-the-vault-could-not-be-read-with-its-passphrase }

Worker nie zdołał otworzyć klucza prywatnego loginu hasłem klucza zapisanym obok
niego. Vault sprawdza klucz przy zapisie loginu, więc zdarza się to rzadko.
Zapisz w loginie ponownie klucz i jego hasło klucza.

### SFTP: "… has no SFTP service for …" { #sftp-has-no-sftp-service-for }

Serwer przyjął login, ale nie daje kontu usługi SFTP. Konto może służyć wyłącznie
do powłoki (shell) albo serwer nie uruchamia podsystemu SFTP. Poproś
administratora o dopuszczenie SFTP dla tego konta.

### SFTP: "… answered the start of the SFTP session in a way this worker cannot use." { #sftp-answered-the-start-of-the-sftp-session-in-a-way-this-worker-cannot-use }

Serwer odpowiedział na żądanie wersji SFTP w postaci, której worker nie potrafi
użyć, albo odpowiedzią, której worker nie potrafi zdekodować. Worker mówi SFTP w wersji 3, tak jak OpenSSH, SFTPGo, ProFTPD i Apache MINA
SSHD. Log workera zawiera odpowiedź samego serwera. Zapytaj administratora
o ustawienia SFTP serwera.

### SFTP: "… did not answer within 30 seconds." { #sftp-did-not-answer-within-30-seconds }

Serwer nie odpowiedział w ciągu 30 sekund na nawiązanie połączenia albo na jedno
żądanie SFTP. Przy nawiązywaniu połączenia zwykłą przyczyną jest firewall, który
odrzuca pakiety workera. Sprawdź, czy worker może dotrzeć do portu SSH serwera.
W trakcie synchronizacji serwer przestał odpowiadać. Worker zrywa połączenie,
a wypisywanie albo ten plik kończy się błędem. Wypisywanie czyta kilka folderów
naraz, więc jeden folder, który utknie, kończy błędem z tym zdaniem całe
wypisywanie, a ten przebieg niczego nie usuwa.

### SFTP: "… could not be reached on port …" { #sftp-could-not-be-reached-on-port }

Serwer odrzucił połączenie na tym porcie albo sieć nie ma do niego trasy. Sprawdź
**Port** i sprawdź, czy firewall pozwala workerowi dotrzeć do serwera. Gdy host
ma więcej niż jeden adres, worker próbuje każdego z nich, zanim się zatrzyma.

### SFTP: "The sync did not finish (OSError) - check the source's credential, …" { #sftp-the-sync-did-not-finish-oserror-check-the-sources-credential }

W przypadku źródła SFTP przyczyną zwykle nie jest poświadczenie. To zdanie
zwykle oznacza, że worker działa jako użytkownik bez wpisu passwd, więc
biblioteka SSH nie może odczytać lokalnej nazwy użytkownika workera. Ustaw
`LOGNAME` dla workera, zgodnie z notatką "Worker potrzebuje lokalnej nazwy
użytkownika" w [regułach sieciowych SFTP](#sftp-network-rules). Log workera
zawiera pełny błąd.

### SFTP: "There is no folder … on …, or it is not a folder." { #sftp-there-is-no-folder-on-or-it-is-not-a-folder }

**Folder** nie wskazuje żadnego folderu, który widzi konto. Sprawdź ścieżkę. Na
serwerze, który trzyma konto w folderze domowym albo w chroot, ścieżka zaczyna
się od tego folderu, a nie od korzenia serwera. OpenSSH daje tę samą odpowiedź,
gdy **Folder** wskazuje plik. Inne serwery dają dla pliku "… could not list the
folder …".

### SFTP: "The account … may not read the folder … on …" { #sftp-the-account-may-not-read-the-folder-on }

Konto nie ma uprawnienia do wypisania folderu z pola **Folder**. Poproś
administratora o nadanie kontu dostępu do odczytu tego folderu albo wybierz
folder, który konto może czytać.

### SFTP: "… could not list the folder …" { #sftp-could-not-list-the-folder }

Pełne zdanie brzmi "… could not list the folder …. Check that it is a folder the
account may read.". SFTPGo, ProFTPD i Apache MINA SSHD odpowiadają w ten sposób,
gdy **Folder** wskazuje plik, więc najpierw sprawdź ścieżkę. W innym przypadku
serwer nie zdołał wypisać folderu z innego powodu albo wysłał odpowiedź, której
worker nie potrafi zdekodować. Log workera zawiera odpowiedź samego serwera.

### SFTP: "The listing of … stopped …. Choose a narrower folder." { #sftp-the-listing-of-stopped-choose-a-narrower-folder }

Wypisywanie osiągnęło jeden z [limitów](#what-bounds-an-sftp-sync), a zdanie
mówi, który: 32 poziomy folderów, 10000 folderów, 1000000 nazw, 100000 plików,
64 MB ścieżek plików albo 1800 sekund. To, co znalazło wypisywanie, zostało przetworzone, a nic nie
usunięto. Limitu nie da się podnieść. Wybierz węższy folder w polu **Folder**
albo podziel drzewo na kilka źródeł, aż synchronizacja skończy się bez tego
zdania.

### SFTP: "The folder … could not be read." { #sftp-the-folder-could-not-be-read }

Folder nadrzędny wymienia ten folder, ale serwer odmówił jego otwarcia, zwykle
dlatego, że konto nie ma uprawnienia, albo wysłał o nim odpowiedź, której worker
nie potrafi zdekodować. Pozostałe foldery zostały przetworzone,
a w tym przebiegu nic nie usunięto. Nadaj kontu dostęp do odczytu folderu albo
przenieś folder poza drzewo.

### SFTP: nazwy pominięte na liście { #sftp-names-left-out-of-the-listing }

Log synchronizacji może podać dla folderu następujące zdania:

- "The server listed … names in … that are not file names, so they were left
  out." Nazwy są puste, zawierają `/` albo bajt NUL lub są dłuższe niż 255
  bajtów, albo ich ścieżki są dłuższe niż 4096 bajtów.
- "… names in … are not UTF-8, so those files were left out."
- "The server did not say what … entries in … are, so they were left out."

W dwóch pierwszych przypadkach zmień nazwy plików na serwerze. Lista pozostaje
kompletna. W trzecim serwer nie podał typu niektórych wpisów. Lista jest wtedy
niekompletna, więc ten przebieg niczego nie usuwa. Zapytaj administratora
o serwer. Log synchronizacji pokazuje najwyżej trzy uwagi. Popraw foldery, które
wymienia, i zsynchronizuj ponownie, aby zobaczyć kolejne. Znak sterujący w nazwie
folderu albo separator wiersza lub akapitu Unicode jest pokazywany jako `?`.

### SFTP: plik liczony jako nieudany { #sftp-a-file-counted-as-failed }

Log synchronizacji liczy plik w `failed`, a pozostałe pliki są synchronizowane.
Log workera wymienia plik i powód, w wierszu takim jak `Failed to sync
report.pdf: report.pdf is 61 MB, and a synced file may be at most 50 MB.`

| Powód | Przyczyna | Co zrobić |
|---|---|---|
| "… is … MB, and a synced file may be at most … MB." | Plik jest większy niż `MAX_UPLOAD_SIZE_MB`. Kończy się błędem przy każdej synchronizacji, zanim zostanie odczytany choćby bajt | Przenieś plik poza folder, usuń jego typ z **File types** albo podnieś `MAX_UPLOAD_SIZE_MB` |
| "The server sent more than … MB of …, and a synced file may be at most … MB." | Serwer wysłał więcej niż limit, choć jego statystyki podawały mniejszy rozmiar | Zapytaj administratora o ten plik |
| "The account … may not read …" | Uprawnienia pliku nie dopuszczają konta | Nadaj kontu dostęp do odczytu pliku |
| "The server refused to read …" | Serwer odmówił odczytu pliku z innego powodu albo wysłał odpowiedź, której worker nie potrafi zdekodować. Log workera zawiera jego odpowiedź | Zapytaj administratora |
| "The server listed … as a file, and now says it is not one." | Ścieżka jest teraz folderem, dowiązaniem albo plikiem specjalnym albo odpowiedzi serwera są ze sobą sprzeczne | Rozstrzygnie to następne wypisywanie. Jeśli plik kończy się błędem przy każdej synchronizacji, zapytaj administratora |
| "The server lists …, but answers that it does not exist." | Folder wciąż wymienia plik, ale serwer odpowiada dla niego "no such file" | Zapytaj administratora. Serwer sam sobie przeczy |
| "The server answers that … does not exist, and its folder could not be listed to confirm it." | Serwer odpowiedział "no such file", a potem nie zdołał wypisać folderu pliku albo nie zdążył w ciągu 10 minut przeznaczonych na plik | Sprawdź uprawnienia folderu. Następna synchronizacja próbuje ponownie |
| "The server sent … of the … bytes … holds." | Serwer wysłał mniej bajtów, niż podają jego statystyki | Następna synchronizacja próbuje ponownie. Jeśli znowu się nie uda, zapytaj administratora |
| "The server answered a read of … with more than was asked for." | Serwer wysłał więcej bajtów, niż prosiło jedno żądanie | Zapytaj administratora. Serwer jest zepsuty |
| "The server opened a file for … that is not the file it listed. …" | Otwarty plik ma innego właściciela albo grupę niż na liście albo inne uprawnienia niż w stat przed otwarciem lub, dla pliku, którego rozmiar albo czas się zmienił, niż na liście | Zobacz [jego wpis](#sftp-the-server-opened-a-file-for-that-is-not-the-file-it-listed) |
| "… did not download within 600 seconds." | Pobieranie trwało dłużej niż 10 minut. Plik 50 MB wymaga około 85 KB/s | Sprawdź szybkość sieci do serwera |
| "… did not answer within 30 seconds." albo "The connection to … was lost." | Serwer przestał odpowiadać albo połączenie zakończyło się w trakcie pobierania | Następny plik otwiera nowe połączenie, a następna synchronizacja czyta ten plik |

Gdy w trakcie synchronizacji nie uda się nawiązać nowego połączenia,
synchronizacja zapamiętuje tę odmowę. Każdy kolejny plik kończy się błędem z tym
samym zdaniem. Zobacz wpisy dotyczące nawiązywania połączenia powyżej.

### SFTP: "The server opened a file for … that is not the file it listed. …" { #sftp-the-server-opened-a-file-for-that-is-not-the-file-it-listed }

Pełne zdanie brzmi "The server opened a file for … that is not the file it
listed. Check that nobody can replace files in this folder with links.". Plik,
który otworzył serwer, ma innego właściciela albo grupę niż podane na liście albo
inne uprawnienia niż w stat tuż przed otwarciem. Gdy ten stat podaje inny rozmiar
albo czas modyfikacji niż lista, uprawnienia muszą też być takie jak na liście.
Zwykłą przyczyną jest
dowiązanie umieszczone w miejscu wypisanego pliku albo folderu nad nim, które
może sprawić, że synchronizacja przeczyta inny plik.

Plik nie jest przetwarzany. Zobacz [ostrzeżenie
o dowiązaniach](#4-what-an-sftp-sync-transfers) i ogranicz konto na serwerze.
Plik, który inne konto wysłało ponownie między wypisaniem a pobraniem, daje to
zdanie raz, a następna synchronizacja go czyta. Tak samo plik, który zmienił się
na serwerze fałszującym uprawnienia na swojej liście, takim jak ProFTPD
z `DirFakeMode`.

### SFTP: "… files changed or were deleted while this sync ran, …" { #sftp-files-changed-or-were-deleted-while-this-sync-ran }

Te pliki zmieniły się w trakcie odczytu albo zostały usunięte po wypisaniu. Są
liczone jako `skipped`, a następna synchronizacja je czyta albo usuwa ich
dokumenty. Connector zapisuje w logu dla każdego pliku, na poziomie info, jedno
z tych zdań: "… changed while it was downloaded.", "… was replaced while it was
downloaded." albo "… was deleted before it was downloaded." (lub "while it was
downloaded."). Dostarczany worker nie wypisuje wierszy info, więc jego log
domyślnie ich nie pokazuje. Jeśli ten sam plik pokazuje to przy każdej synchronizacji, jedną
z przyczyn jest serwer, który podaje bieżący czas jako czas modyfikacji pliku.
Zapytaj administratora o serwer.

### Zaplanowane synchronizacje nie działają { #scheduled-syncs-are-not-running }

Musi działać system zadań w tle. Sprawdź, czy proces workera jest aktywny:

Bez workera zadziałają wyłącznie ręczne wyzwolenia z CLI lub API.
