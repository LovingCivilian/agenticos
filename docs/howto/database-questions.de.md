---
source_sha: "4127fd6b43f9"
title: "Fragen aus Ihrer Datenbank beantworten"
description: "Verbinden Sie einen selbst gehosteten Postgres-MCP-Server hinter einer reinen Leserolle und einem reinen View-Schema, und lassen Sie einen Agent ihn abfragen."
---

# Fragen aus Ihrer Datenbank beantworten { #answer-questions-from-your-database }

Verbinden Sie einen Postgres-MCP-Server, damit ein Agent Fragen aus Ihrer eigenen Datenbank beantworten kann, und schieben Sie vorher eine reine Leserolle und ein reines View-Schema zwischen den Agent und Ihre Tabellen. Dies ist eine Anleitung zum Durchführen, kein Bericht über ein gemessenes Deployment: Hier wurde nichts end-to-end gegen einen laufenden Agent ausgeführt.

## Was Sie brauchen { #what-you-need }

- Eine [laufende Installation](../install.md) mit einem Modellprofil.
- Ein selbst betriebener Postgres-MCP-Server, erreichbar unter einer URL, die dieses Deployment anwählen kann. Der Eintrag `postgres` im Katalog ist genau das: keine gehostete URL, ein Bearer-Token, das *Ihr eigener Server* prüft, und eine Warnung, ihn auf reine Leseansichten statt auf eine Primärdatenbank mit Schreibzugriff zu richten. Siehe [den Katalog](../mcp.md#data-and-analytics).
- Für einen Server im eigenen Netz dieses Deployments - einen compose-Dienst, eine private Adresse - sein Host in [`MCP_ALLOWED_HOSTS`](../configuration.md#project-settings). Siehe [unten](#let-the-deployment-reach-a-server-on-its-own-network).
- `connections:manage`, um die Organisationsverbindung zu registrieren.

## Warum eine reine Leserolle und ein reines View-Schema { #why-a-read-only-role-and-a-views-only-schema }

**Eine Freigabe liest das SQL nicht für Sie.** Die `approval`-Regel der Bindung kann jede Abfrage für eine Person zurückhalten - `all`, mit dem genauen SQL auf der Karte -, aber ein Agent, der Dutzende Fragen stellt, macht daraus ein Abnicken, und `writes` vertraut darauf, dass der Server sein Abfrage-Tool als schreibgeschützt markiert. Siehe [welche Aufrufe auf eine Person warten](../mcp.md#which-calls-wait-for-a-person). Die Datenbank-Zugangsdaten selbst müssen die Grenze sein, keine Einstellung am Agent.

Zwei Entscheidungen leisten das:

- **Eine Rolle ohne Schreibrechte**, sodass das Schlimmste, was eine falsche oder manipulierte Abfrage anrichten kann, ist, etwas zu lesen, das sie nicht sollte - nie eine Zeile zu ändern oder zu löschen.
- **Ein Schema aus Views, nicht aus den Basistabellen**, das dieser Rolle gewährt wird statt der Tabellen selbst. Eine View kann Spalten weglassen, die ein Modell nicht sehen sollte, und vorab aggregieren, was sie zurückgibt - das ist zugleich eine Datenschutzgrenze und eine günstigere Abfrage, die der Agent schreiben muss.

## Die Eingabe vorbereiten { #prepare-the-input }

Eine kleine synthetische Tabelle `orders`, in einer eigenen Datenbank:

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

Das reine View-Schema und die Rolle, mit der sich der Connection-String des MCP-Servers authentifiziert:

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

Die Referenzantwort, damit Sie eine Antwort mit den Quellzeilen abgleichen können: `status_counts` ergibt paid 6 Bestellungen / 26700 Cent, pending 1 / 2500, refunded 1 / 4200. Fragt `shop_readonly` direkt `orders` ab, wird das mit `permission denied for table orders` abgelehnt - die Views sind die einzige Tür.

## Das Deployment einen Server im eigenen Netz erreichen lassen { #let-the-deployment-reach-a-server-on-its-own-network }

Die URL einer MCP-Verbindung wird beim Speichern abgelehnt, wenn ihr Host auf eine Loopback-, private, Link-Local- oder Shared-CGNAT-Adresse auflöst. Siehe [eine URL, die dieses Deployment nicht erreichen darf, wird abgelehnt](../mcp.md#a-connection). Einen Server unter `localhost` zu verbinden, ergibt:

```text
This MCP server URL cannot be used: Blocked: 'localhost' resolves to
private/internal address '::1'. SSRF protection does not allow requests to
internal networks.
```

Ein Server im eigenen Netz des Deployments - der gewöhnliche Ort, einen zum ersten Mal laufen zu lassen - wird genauso abgelehnt, bis der Betreiber seinen Host einträgt - seinen eigenen Namen, nie `localhost`, eine Loopback-Adresse oder die Maschine, auf der der Stack läuft:

1. Betreiben Sie den Server unter einem Namen wie `postgres-mcp` in einem Netz, das die API und der Prefect-Runner teilen, damit beide ihn unter diesem Namen erreichen: im Stack aus `docker-compose.yml` als Dienst in dessen Netz `backend`. Image und Startflags entnehmen Sie dem README des Servers selbst, aber veröffentlichen Sie keinen Port, auch wenn das README es tut: Über einen veröffentlichten Port lässt er sich von außen aufrufen, an der API vorbei. Lesen Sie, [was er erreichen kann](../mcp.md#what-a-listed-host-opens), bevor Sie ein Image wählen.

    Auf einem Server betreiben Sie ihn aus einer eigenen compose-Datei unter einem eigenen Projekt (`-p`), die dem Netz des Stacks als `external: true` beitritt, so wie [das Produktions-Frontend es tut](../deploy.md#option-b-nginx): `agenticos_edge` für `docker-compose-prod.yml`, `agenticos_backend` auf dem Dev-Server. Bearbeiten Sie stattdessen keine versionierte compose-Datei: Das kann den Checkout beim nächsten Deployment aufhalten.

2. Setzen Sie den Host in `backend/.env`, als JSON-Liste:

    ```bash
    MCP_ALLOWED_HOSTS=["postgres-mcp"]
    ```

3. Erstellen Sie den API-Container mit den compose-Dateien neu, die ihn gestartet haben, damit er mit der neuen Umgebung startet - `docker compose restart` behält den alten. Im Stack aus `docker-compose.yml`:

    ```bash
    docker compose up -d app
    ```

    Führen Sie auf einem Server nie ein bloßes `docker compose` im Klon aus: Es führt `docker-compose.override.yml` mit der Basisdatei zusammen und ersetzt die API des Servers durch einen lokalen Build. Nennen Sie stattdessen die Dateien des Deployments - fügen Sie auf einem Host, der mit `PROXY=traefik` deployt wurde, `-f docker-compose-prod.traefik.yml` hinzu, oder nehmen Sie auf dem Dev-Server `-f docker-compose-dev.yml`:

    ```bash
    docker compose --env-file backend/.env -f docker-compose-prod.yml up -d app
    ```

    Auf einem Host, den [`scripts/deploy.sh`](../deploy.md#by-hand) deployt, stellen Sie dem Kommando den Image-Tag voran, den das Skript deployt hat: `AGENTICOS_VERSION="sha-$(git rev-parse HEAD | cut -c1-7)"`. Das Skript [pinnt ihn nur, solange es läuft](../deploy.md#the-images); ohne ihn startet das Kommando die Version, die `backend/.env` nennt, oder `latest`.

4. Verbinden Sie die URL des Servers unter diesem Namen - zum Beispiel `http://postgres-mcp:8000/mcp`, mit dem Port und dem Pfad aus seinem README. Der Pfad muss zum Transport des Servers passen: Ein Segment `/sse` wählt Server-Sent Events, alles andere streamable HTTP.

Für einen gelisteten Host gilt dreierlei:

- **Er wird exakt abgeglichen und nicht aufgelöst.** Der Eintrag ist der Host genau so, wie die URL ihn schreibt, und das Speichern der Verbindung schlägt ihn nicht im DNS nach. Die URL braucht weiterhin `http://` oder `https://`.
- **Er wird über ein Bearer-Token angebunden.** Die Anmeldung (OAuth) wird für einen gelisteten Host nicht angeboten, weil die Prüfung der Anmeldung die Liste ignoriert.
- **Er öffnet den ganzen Host, für jedes Konto.** Jedes Konto in diesem Deployment kann sich mit jedem Port eines gelisteten Hosts verbinden, also muss der Server oder ein Proxy davor das Token prüfen. Die reine Leserolle begrenzt weiterhin, was irgendjemand darüber lesen kann. Siehe [was ein gelisteter Host öffnet](../mcp.md#what-a-listed-host-opens), auch dazu, wie man einen Eintrag rückgängig macht.

Ein Deployment, das die Liste leer lässt, erreicht einen Server weiterhin über eine routbare Adresse davor: einen kleinen Host mit einer öffentlichen IP oder einen Tunnel.

Das obige SQL wurde direkt gegen Postgres ausgeführt und geprüft; die Rollenablehnung, die zitierten View-Summen und die Ablehnung der URL sind echt. Nicht verifiziert wurde der Rest dieser Seite: dass ein gelisteter Server verbunden wird und ein Agent tatsächlich über ihn abfragt.

## Den Server verbinden { #connect-the-server }

1. Wählen Sie unter **Toolbox → MCP servers** eines Agents **Connect a server** und wählen Sie **PostgreSQL** aus dem Katalog, oder verbinden Sie ihn einmal unter **MCP servers** in den Organisationseinstellungen, damit mehr als ein Agent ihn binden kann.
2. Richten Sie die Verbindung auf Ihren eigenen laufenden Postgres-MCP-Server (zum Beispiel [`crystaldba/postgres-mcp`](https://github.com/crystaldba/postgres-mcp)), konfiguriert mit dem Connection-String von `shop_readonly` und seinem eingeschränkten, reinen Lesemodus - `http://postgres-mcp:8000/mcp`, sobald sein Host gelistet ist. Fügen Sie das Bearer-Token ein, das dieser Server prüft - nicht das Datenbankpasswort - als Token der Verbindung.
3. Schränken Sie in der Bindung des Agents `allowed_tools` auf die reinen Lese-Tools ein, die der Server anbietet, zusätzlich zu dem, was die Verbindung selbst bereits erlaubt.
4. Binden Sie nur diese Verbindung, setzen Sie ein Budget, und setzen Sie Instruktionen, die die beiden Views nennen und dem Agent sagen, dass er sagen soll, wenn eine Frage eine Spalte oder eine Tabelle braucht, die die Views nicht führen, statt zu raten.

## Das Ergebnis prüfen { #check-the-result }

| Prüfung | Referenz |
| --- | --- |
| Eine Frage, die die bezahlte Summe nennt | 26700 (sechs bezahlte Bestellungen) |
| Eine Frage zu ausstehenden Bestellungen | 2500 (eine Bestellung) |
| Eine Frage, die eine Spalte nennt, die keine View zeigt (z. B. der Kundenname) | Der Agent sagt, dass er das aus dem, woran er gebunden ist, nicht beantworten kann |
| Eine Abfrage, die die Rolle nicht ausführen kann (ein Update, ein Delete) | Von der Datenbank abgelehnt, `permission denied` |
| `allowed_tools` auf reine Lese-Tools eingeschränkt | Ein Schreib-Tool, das der Server anbietet, steht im Toolset des Modells überhaupt nicht |

## Wenn etwas schiefgeht { #when-it-goes-wrong }

- **Die Verbindung wird mit einer SSRF-Meldung abgelehnt.** Die Adresse des Servers ist Loopback, privat oder anderweitig intern für das eigene Netzwerk dieses Deployments. Tragen Sie seinen Host genau so, wie die URL ihn schreibt, in `MCP_ALLOWED_HOSTS` ein, erstellen Sie den API-Container neu und speichern Sie die Verbindung erneut - siehe [oben](#let-the-deployment-reach-a-server-on-its-own-network) - oder stellen Sie eine routbare Adresse davor.
- **Für einen gelisteten Host wird keine Anmeldung angeboten, oder der Start von OAuth wird abgelehnt.** Das ist so gewollt: Die Prüfung der Anmeldung liest die Liste nie. Verbinden Sie mit dem Bearer-Token, das der Server prüft.
- **Der Agent liest die Basistabelle statt der Views.** `shop_readonly` wurde sowohl auf `public` als auch auf `reporting` gewährt, oder das Schema der Basistabelle wurde ihr nie entzogen. Führen Sie das obige `REVOKE` erneut aus.
- **Eine Abfrage, die der Agent ausführt, sieht aus, als hätte sie etwas geändert.** Das kann nicht sein, wenn die Rolle wirklich kein Schreibrecht hat - prüfen Sie die Rechte der Rolle, bevor Sie annehmen, der Agent habe sich falsch verhalten.
- **Die Verbindung wird erfolgreich geprüft, aber der Builder listet keine Tools.** Sie wurde noch nicht geprüft, oder die letzte Prüfung ist fehlgeschlagen - `POST /mcp-connections/{id}/test` aktualisiert sie.
- **Zwei Agents brauchen unterschiedlichen Zugriff auf dieselbe Datenbank.** Verbinden Sie den Server zweimal, unter zwei Namen, jeweils mit eigener Rolle und eigenen Views - ein Server, zwei Zugangsdaten, nie eine Rolle für den strengeren Agent aufgeweitet.

## Den Versuch festhalten { #record-the-trial }

Bewahren Sie das SQL, das das Test-Setup erstellt hat, die Rechte der Rolle, die View-Definitionen, das `allowed_tools` der Verbindung und das `allowed_tools` auf Ebene der Agent-Bindung auf. Eine Person entscheidet weiterhin, welche Spalten in eine View gehören, bevor ein Agent sie je erreicht - den Zugriff einzuschränken, nachdem ein Agent schon Fragen stellt, ist ein sehr viel schwierigeres Gespräch, als es zuerst zu entscheiden.
