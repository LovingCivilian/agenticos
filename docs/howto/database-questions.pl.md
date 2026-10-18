---
source_sha: "4127fd6b43f9"
title: "Odpowiadaj na pytania z bazy danych"
description: "Podłącz samodzielnie hostowany serwer MCP dla Postgresa za rolą tylko do odczytu i schematem zawierającym wyłącznie widoki, a potem pozwól agentowi go odpytywać."
---

# Odpowiadaj na pytania z bazy danych { #answer-questions-from-your-database }

Podłącz serwer MCP dla Postgresa, żeby agent mógł odpowiadać na pytania z Twojej własnej bazy danych, a wcześniej postaw między agentem a tabelami rolę tylko do odczytu i schemat zawierający wyłącznie widoki. To instrukcja wykonania, a nie raport z pomiaru wdrożenia: nic tu nie zostało uruchomione od początku do końca z działającym agentem.

## Czego potrzebujesz { #what-you-need }

- [Działająca instalacja](../install.md) z profilem modelu.
- Serwer MCP dla Postgresa, który uruchamiasz sam, dostępny pod adresem URL, z którym to wdrożenie może się połączyć. Wpis `postgres` w katalogu jest dokładnie tym: bez hostowanego adresu, z tokenem bearer, który sprawdza *Twój własny serwer*, i z ostrzeżeniem, żeby kierować go na widoki tylko do odczytu, a nie na bazę główną z prawem zapisu. Zobacz [katalog](../mcp.md#data-and-analytics).
- Dla serwera w sieci samego wdrożenia - usługi compose, adresu prywatnego - jego host w [`MCP_ALLOWED_HOSTS`](../configuration.md#project-settings). Zobacz [niżej](#let-the-deployment-reach-a-server-on-its-own-network).
- `connections:manage`, żeby zarejestrować połączenie organizacji.

## Dlaczego rola tylko do odczytu i schemat z samymi widokami { #why-a-read-only-role-and-a-views-only-schema }

**Zatwierdzenie nie przeczyta za ciebie SQL-a.** Polityka `approval` powiązania może zatrzymać każde zapytanie dla człowieka - `all`, z dokładnym SQL-em na karcie - ale agent zadający dziesiątki pytań zamienia to w odruchowe klikanie, a `writes` ufa, że serwer oznaczy swoje narzędzie zapytań jako tylko do odczytu. Zobacz [które wywołania czekają na człowieka](../mcp.md#which-calls-wait-for-a-person). Granicą musi być samo poświadczenie do bazy, a nie ustawienie agenta.

Robią to dwie decyzje:

- **Rola bez uprawnień do zapisu**, żeby najgorsze, co może zrobić błędne albo zmanipulowane zapytanie, to odczytać coś, czego nie powinno, a nigdy nie zmienić ani nie usunąć wiersza.
- **Schemat z widokami, a nie tabelami bazowymi**, nadany tej roli zamiast samych tabel. Widok może pominąć kolumny, których model nie powinien widzieć, i z góry agregować to, co zwraca. To jednocześnie granica prywatności i tańsze zapytanie do napisania dla agenta.

## Przygotuj dane wejściowe { #prepare-the-input }

Mała syntetyczna tabela `orders` w osobnej bazie danych:

```sql
CREATE TABLE orders (
    id           serial PRIMARY KEY,
    customer     text NOT NULL,
    status       text NOT NULL CHECK (status IN ('paid', 'refunded', 'pending')),
    amount_cents integer NOT NULL,
    created_at   date NOT NULL
);

INSERT INTO orders (customer, status, amount_cents, created_at) VALUES
    ('Ada',     'paid',     4200, '2026-09-01'),
    ('Grace',   'paid',     1800, '2026-09-02'),
    ('Ada',     'refunded', 4200, '2026-09-03'),
    ('Rex',     'paid',     9900, '2026-09-05'),
    ('Grace',   'pending',  2500, '2026-09-06'),
    ('Linus',   'paid',     3300, '2026-09-06'),
    ('Rex',     'paid',     1500, '2026-09-08'),
    ('Ada',     'paid',     6000, '2026-09-09');
```

Schemat zawierający wyłącznie widoki i rola, jako którą uwierzytelnia się connection string serwera MCP:

```sql
CREATE SCHEMA reporting;

CREATE VIEW reporting.daily_paid_totals AS
SELECT created_at, count(*) AS paid_orders, sum(amount_cents) AS paid_amount_cents
FROM orders
WHERE status = 'paid'
GROUP BY created_at
ORDER BY created_at;

CREATE VIEW reporting.status_counts AS
SELECT status, count(*) AS orders, sum(amount_cents) AS amount_cents
FROM orders
GROUP BY status
ORDER BY status;

CREATE ROLE shop_readonly LOGIN PASSWORD 'change-me';
GRANT CONNECT ON DATABASE shop_demo TO shop_readonly;
GRANT USAGE ON SCHEMA reporting TO shop_readonly;
GRANT SELECT ON reporting.daily_paid_totals, reporting.status_counts TO shop_readonly;
REVOKE ALL ON SCHEMA public FROM shop_readonly;
```

Odpowiedź referencyjna, żeby sprawdzić odpowiedź ze źródłowymi wierszami: `status_counts` daje paid 6 zamówień / 26700 centów, pending 1 / 2500, refunded 1 / 4200. Bezpośrednie zapytanie `shop_readonly` do `orders` jest odrzucane komunikatem `permission denied for table orders`, więc widoki są jedynymi drzwiami.

## Pozwól wdrożeniu sięgnąć do serwera w jego własnej sieci { #let-the-deployment-reach-a-server-on-its-own-network }

Adres URL połączenia MCP jest odrzucany przy zapisie, jeśli jego host rozwiązuje się na adres loopback, prywatny, link-local albo ze współdzielonej puli CGNAT. Zobacz [adres URL, do którego to wdrożenie nie może sięgać, jest odrzucany](../mcp.md#a-connection). Próba podłączenia serwera pod `localhost` kończy się komunikatem:

```text
This MCP server URL cannot be used: Blocked: 'localhost' resolves to
private/internal address '::1'. SSRF protection does not allow requests to
internal networks.
```

Serwer w sieci samego wdrożenia, czyli tam, gdzie zwykle uruchamia się go po raz pierwszy, jest odrzucany tak samo, dopóki operator nie wpisze jego hosta na listę - jego własnej nazwy, nigdy `localhost`, adresu loopback ani maszyny, na której działa stack:

1. Uruchom serwer pod nazwą taką jak `postgres-mcp`, w sieci, którą dzielą API i runner Prefecta, żeby oba docierały do niego pod tą nazwą: w stacku `docker-compose.yml` jako usługę w jego sieci `backend`. Obraz i flagi uruchomienia weź z README samego serwera, ale nie publikuj żadnego portu, nawet jeśli README to robi: opublikowany port pozwala wywołać serwer z zewnątrz, z pominięciem API. Zanim wybierzesz obraz, przeczytaj, [do czego może sięgnąć](../mcp.md#what-a-listed-host-opens).

    Na serwerze uruchom go z własnego pliku compose i jako osobny projekt (`-p`), dołączając do sieci stacku jako `external: true`, tak jak [robi to frontend produkcyjny](../deploy.md#option-b-nginx): `agenticos_edge` dla `docker-compose-prod.yml`, `agenticos_backend` na serwerze deweloperskim. Nie edytuj zamiast tego śledzonego pliku compose: to może zatrzymać checkout przy kolejnym wdrożeniu.

2. Ustaw host w `backend/.env` jako listę JSON:

    ```bash
    MCP_ALLOWED_HOSTS=["postgres-mcp"]
    ```

3. Utwórz kontener API od nowa z plikami compose, które go uruchomiły, żeby wystartował z nowym środowiskiem - `docker compose restart` zostawia stary. W stacku `docker-compose.yml`:

    ```bash
    docker compose up -d app
    ```

    Na serwerze nigdy nie uruchamiaj w klonie samego `docker compose`: scala on `docker-compose.override.yml` i zastępuje API serwera lokalnym buildem. Zamiast tego podaj pliki wdrożenia - dodaj `-f docker-compose-prod.traefik.yml` na hoście wdrożonym z `PROXY=traefik` albo użyj `-f docker-compose-dev.yml` na serwerze deweloperskim:

    ```bash
    docker compose --env-file backend/.env -f docker-compose-prod.yml up -d app
    ```

    Na hoście, który wdraża [`scripts/deploy.sh`](../deploy.md#by-hand), poprzedź tę komendę tagiem obrazu, który ten skrypt wdrożył: `AGENTICOS_VERSION="sha-$(git rev-parse HEAD | cut -c1-7)"`. Skrypt [przypina go tylko na czas swojego działania](../deploy.md#the-images); bez niego komenda uruchamia wersję wskazaną w `backend/.env` albo `latest`.

4. Połącz się z URL-em serwera pod tą nazwą - na przykład `http://postgres-mcp:8000/mcp`, z portem i ścieżką podanymi w jego README. Ścieżka musi pasować do transportu serwera: segment `/sse` wybiera server-sent events, każda inna streamable HTTP.

Dla hosta z listy obowiązują trzy zasady:

- **Jest dopasowywany dokładnie i nie jest rozwiązywany.** Wpis to host zapisany dokładnie tak, jak w URL-u, a zapis połączenia nie sprawdza go w DNS. URL nadal wymaga `http://` albo `https://`.
- **Łączy się z nim tokenem bearer.** Logowanie (OAuth) nie jest oferowane dla hosta z listy, bo sprawdzenie logowania pomija tę listę.
- **Otwiera cały host, dla każdego konta.** Każde konto w tym wdrożeniu może połączyć się z dowolnym portem hosta z listy, więc serwer albo proxy przed nim musi sprawdzać token. Rola tylko do odczytu nadal ogranicza to, co ktokolwiek może przez niego odczytać. Zobacz, [co otwiera host z listy](../mcp.md#what-a-listed-host-opens) i jak cofnąć wpis.

Wdrożenie, które zostawia listę pustą, nadal może sięgnąć do serwera przez routowalny adres przed nim: mały host z publicznym IP albo tunel.

Powyższy SQL został uruchomiony i sprawdzony bezpośrednio w Postgresie. Odmowa dla roli, podane sumy z widoków i odmowa dla adresu URL są prawdziwe. Nie zweryfikowano reszty tej strony: podłączenia serwera z listy i tego, że agent faktycznie odpytuje przez niego bazę.

## Podłącz serwer { #connect-the-server }

1. W **Toolbox → MCP servers** agenta wybierz **Connect a server** i z katalogu **PostgreSQL** albo podłącz go raz w **MCP servers** w ustawieniach organizacji, żeby mogło go przypisać więcej niż jednego agenta.
2. Skieruj połączenie na własny działający serwer MCP dla Postgresa (na przykład [`crystaldba/postgres-mcp`](https://github.com/crystaldba/postgres-mcp)) skonfigurowany z connection stringiem `shop_readonly` i w ograniczonym trybie tylko do odczytu - `http://postgres-mcp:8000/mcp`, gdy jego host jest już na liście. Jako token połączenia wklej token bearer, który sprawdza ten serwer, a nie hasło do bazy.
3. W przypisaniu agenta zawęź `allowed_tools` do narzędzi tylko do odczytu, które udostępnia serwer, dodatkowo względem tego, na co pozwala już samo połączenie.
4. Przypisz tylko to połączenie, ustaw budżet i instrukcje, które nazywają dwa widoki i każą agentowi powiedzieć, gdy pytanie wymaga kolumny albo tabeli, której widoki nie mają, zamiast zgadywać.

## Sprawdź wynik { #check-the-result }

| Sprawdzenie | Kryterium |
| --- | --- |
| Pytanie o sumę opłaconych zamówień | 26700 (sześć opłaconych zamówień) |
| Pytanie o zamówienia oczekujące | 2500 (jedno zamówienie) |
| Pytanie o kolumnę, której nie udostępnia żaden widok (np. nazwę klienta) | Agent mówi, że nie odpowie na to na podstawie tego, co ma przypisane |
| Zapytanie, którego rola nie może wykonać (update, delete) | Odrzucone przez bazę, `permission denied` |
| `allowed_tools` zawężone do narzędzi tylko do odczytu | Narzędzia do zapisu, które oferuje serwer, w ogóle nie ma w zestawie narzędzi modelu |

## Gdy coś pójdzie nie tak { #when-it-goes-wrong }

- **Połączenie jest odrzucane z komunikatem o SSRF.** Adres serwera to loopback, adres prywatny albo inny wewnętrzny dla sieci tego wdrożenia. Dodaj jego host do `MCP_ALLOWED_HOSTS` dokładnie tak, jak zapisuje go URL, utwórz kontener API od nowa i zapisz połączenie ponownie - zobacz [wyżej](#let-the-deployment-reach-a-server-on-its-own-network) - albo postaw przed nim routowalny adres.
- **Logowanie nie jest oferowane albo start OAuth jest odrzucany dla hosta z listy.** Tak ma być: sprawdzenie logowania nigdy nie czyta tej listy. Połącz się z tokenem bearer, który sprawdza serwer.
- **Agent czyta tabelę bazową zamiast widoków.** `shop_readonly` dostała uprawnienia do `public` oprócz `reporting` albo nigdy nie odebrano jej schematu tabeli bazowej. Uruchom ponownie `REVOKE` z góry.
- **Zapytanie agenta wygląda, jakby coś zmieniło.** Nie mogło, jeśli rola naprawdę nie ma uprawnień do zapisu. Sprawdź uprawnienia roli, zanim uznasz, że agent zachował się źle.
- **Test połączenia przechodzi, ale Builder nie pokazuje żadnych narzędzi.** Nic go jeszcze nie odpytało albo ostatnie sprawdzenie się nie powiodło. `POST /mcp-connections/{id}/test` je odświeża.
- **Dwa agenty potrzebują różnego dostępu do tej samej bazy.** Podłącz serwer dwa razy, pod dwiema nazwami, każdy z własną rolą i własnymi widokami. Jeden serwer, dwa poświadczenia, nigdy jedna rola poszerzona pod bardziej wymagającego agenta.

## Zapisz próbę { #record-the-trial }

Zachowaj SQL, który utworzył dane testowe, uprawnienia roli, definicje widoków, `allowed_tools` połączenia i `allowed_tools` na poziomie przypisania agenta. Człowiek nadal decyduje, które kolumny należą do widoku, zanim agent w ogóle do niego sięgnie. Zawężanie dostępu, gdy agent już zadaje pytania, to znacznie trudniejsza rozmowa niż decyzja podjęta na początku.
