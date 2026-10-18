---
source_sha: "4127fd6b43f9"
title: "Responde preguntas desde tu base de datos"
description: "Conecta un servidor MCP de Postgres autoalojado, detrás de un rol de solo lectura y un esquema de solo vistas, y deja que un agent lo consulte."
---

# Responde preguntas desde tu base de datos { #answer-questions-from-your-database }

Conecta un servidor MCP de Postgres para que un agent pueda responder
preguntas desde tu propia base de datos, y pon un rol de solo lectura y un
esquema de solo vistas entre el agent y tus tablas antes de hacerlo. Es un
procedimiento que ejecutar, no un informe de un despliegue medido: nada de
esto se ejecutó de principio a fin contra un agent real.

## Qué necesitas { #what-you-need }

- Una [instalación en marcha](../install.md) con un perfil de modelo.
- Un servidor MCP de Postgres que ejecutes tú mismo, alcanzable en una URL a
  la que este deployment pueda llamar. La entrada `postgres` del catálogo es
  exactamente esto: sin URL alojada, un token de portador que comprueba *tu
  propio servidor*, y una advertencia de apuntarlo a vistas de solo lectura en
  lugar de a una base primaria con acceso de escritura. Ver
  [el catálogo](../mcp.md#data-and-analytics).
- Para un servidor en la propia red de este deployment — un servicio de
  compose, una dirección privada —, su host en
  [`MCP_ALLOWED_HOSTS`](../configuration.md#project-settings). Ver
  [más abajo](#let-the-deployment-reach-a-server-on-its-own-network).
- `connections:manage`, para registrar la conexión de organización.

## Por qué un rol de solo lectura y un esquema de solo vistas { #why-a-read-only-role-and-a-views-only-schema }

**Una aprobación no lee el SQL por ti.** La política `approval` del vínculo puede
retener cada consulta para una persona — `all`, con el SQL exacto en la tarjeta —,
pero un agent que hace decenas de preguntas lo convierte en un sello automático, y
`writes` confía en que el servidor marque su herramienta de consulta como de solo
lectura. Ver
[qué llamadas esperan a una persona](../mcp.md#which-calls-wait-for-a-person).
La credencial de la base de datos misma tiene que ser la frontera, no un
ajuste sobre el agent.

Dos decisiones hacen ese trabajo:

- **Un rol sin privilegios de escritura**, de modo que lo peor que puede
  hacer una consulta equivocada o manipulada es leer algo que no debería —
  nunca cambiar o borrar una fila.
- **Un esquema de vistas, no las tablas base**, concedido a ese rol en vez de
  a las tablas mismas. Una vista puede eliminar columnas que un modelo no
  debería ver y preagregar lo que devuelve, lo cual es a la vez una frontera
  de privacidad y una consulta más barata de escribir para el agent.

## Prepara la entrada { #prepare-the-input }

Una pequeña tabla `orders` sintética, en una base de datos propia:

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

El esquema de solo vistas y el rol con el que se autentica la cadena de
conexión del servidor MCP:

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

La respuesta de referencia, para que puedas comprobar una respuesta contra
las filas de origen: `status_counts` da paid 6 pedidos / 26700 céntimos,
pending 1 / 2500, refunded 1 / 4200. `shop_readonly` consultando `orders`
directamente se rechaza con `permission denied for table orders` — las
vistas son la única puerta.

## Deja que el deployment alcance un servidor de su propia red { #let-the-deployment-reach-a-server-on-its-own-network }

La URL de una conexión MCP se rechaza al guardarla si su host resuelve a una
dirección loopback, privada, link-local o de CGNAT compartido. Ver
[una URL que este deployment no debe alcanzar se rechaza](../mcp.md#a-connection).
Conectar un servidor en `localhost` responde:

```text
This MCP server URL cannot be used: Blocked: 'localhost' resolves to
private/internal address '::1'. SSRF protection does not allow requests to
internal networks.
```

Un servidor en la propia red del deployment — el sitio habitual donde se
ejecuta uno por primera vez — se rechaza igual hasta que el operador incluye
su host en la lista, con su propio nombre y nunca `localhost`, una dirección
de loopback ni la máquina en la que se ejecuta el stack:

1. Ejecuta el servidor con un nombre como `postgres-mcp`, en una red que
   compartan la API y el runner de Prefect, para que ambos lo alcancen por ese
   nombre: en el stack de `docker-compose.yml`, como un servicio en su red
   `backend`. Toma su imagen y sus flags de arranque del README del propio
   servidor, pero no publiques ningún puerto, aunque el README lo haga: un
   puerto publicado permite llamarlo desde fuera, sin pasar por la API. Lee
   [qué puede alcanzar](../mcp.md#what-a-listed-host-opens) antes de elegir una
   imagen.

    En un servidor, ejecútalo desde un archivo de compose y un proyecto (`-p`)
    propios que se unan a la red del stack como `external: true`, igual que
    [hace el frontend de producción](../deploy.md#option-b-nginx):
    `agenticos_edge` para `docker-compose-prod.yml`, `agenticos_backend` en el
    servidor de desarrollo. No edites en su lugar un archivo de compose
    versionado: eso puede detener el checkout del siguiente despliegue.

2. Pon el host en `backend/.env`, como una lista JSON:

    ```bash
    MCP_ALLOWED_HOSTS=["postgres-mcp"]
    ```

3. Vuelve a crear el contenedor de la API con los archivos de compose que lo
   arrancaron, para que arranque con el nuevo entorno —
   `docker compose restart` conserva el antiguo. En el stack de
   `docker-compose.yml`:

    ```bash
    docker compose up -d app
    ```

    En un servidor, nunca ejecutes un `docker compose` a secas en el clon:
    fusiona `docker-compose.override.yml` y sustituye la API del servidor por
    un build local. Nombra en su lugar los archivos del despliegue — añade
    `-f docker-compose-prod.traefik.yml` en un host desplegado con
    `PROXY=traefik`, o usa `-f docker-compose-dev.yml` en el servidor de
    desarrollo:

    ```bash
    docker compose --env-file backend/.env -f docker-compose-prod.yml up -d app
    ```

    En un host que despliega [`scripts/deploy.sh`](../deploy.md#by-hand), pon
    delante de ese comando el tag de imagen que desplegó el script:
    `AGENTICOS_VERSION="sha-$(git rev-parse HEAD | cut -c1-7)"`. El script
    [lo fija solo mientras se ejecuta](../deploy.md#the-images); sin él, el
    comando arranca la versión que nombra `backend/.env`, o `latest`.

4. Conecta en la URL del servidor con ese nombre — por ejemplo
   `http://postgres-mcp:8000/mcp`, con el puerto y la ruta que da su README.
   La ruta tiene que coincidir con el transporte del servidor: un segmento
   `/sse` selecciona server-sent events, cualquier otra cosa HTTP en
   streaming.

Para un host de la lista valen tres cosas:

- **Se compara de forma exacta y no se resuelve.** La entrada es el host tal
  como lo escribe la URL, y guardar la conexión no lo busca en DNS. La URL
  sigue necesitando `http://` o `https://`.
- **Se conecta con un token de portador.** El inicio de sesión (OAuth) no se
  ofrece para un host de la lista, porque la comprobación del inicio de sesión
  ignora la lista.
- **Abre el host entero, a todas las cuentas.** Cualquier cuenta de este
  deployment puede conectarse a cualquier puerto de un host de la lista, así
  que el servidor, o un proxy delante de él, tiene que comprobar el token. El
  rol de solo lectura sigue acotando lo que cualquiera puede leer a través de
  él. Ver [qué abre un host de la lista](../mcp.md#what-a-listed-host-opens), y
  cómo deshacer una entrada.

Un deployment que deja la lista vacía todavía puede alcanzar un servidor a
través de una dirección enrutable delante de él: un host pequeño con una IP
pública, o un túnel.

El SQL de arriba se ejecutó y se comprobó directamente contra Postgres; el
rechazo del rol, los totales de las vistas citados y el rechazo de la URL son
reales. Lo que no se verificó es el resto de esta página: que se conecte un
servidor de la lista, y que un agent consulte de verdad a través de él.

## Conecta el servidor { #connect-the-server }

1. En **Toolbox → MCP servers** de un agent, elige **Connect a server** y
   selecciona **PostgreSQL** del catálogo, o conéctalo una vez desde **MCP
   servers** en los ajustes de la organización para que más de un agent
   pueda vincularlo.
2. Apunta la conexión a tu propio servidor MCP de Postgres en marcha (por
   ejemplo [`crystaldba/postgres-mcp`](https://github.com/crystaldba/postgres-mcp))
   configurado con la cadena de conexión de `shop_readonly` y su modo
   restringido de solo lectura — `http://postgres-mcp:8000/mcp` una vez que
   su host esté en la lista. Pega el token de portador que comprueba ese
   servidor — no la contraseña de la base de datos — como el token de la
   conexión.
3. En la vinculación del agent, reduce `allowed_tools` a las de solo lectura
   que expone el servidor, por encima de lo que ya permite la propia
   conexión.
4. Vincula solo esta conexión, define un budget, y pon instrucciones que
   nombren las dos vistas y le digan al agent que diga cuándo una pregunta
   necesita una columna o una tabla que las vistas no tienen, en vez de
   adivinar.

## Comprueba el resultado { #check-the-result }

| Comprobación | Referencia |
| --- | --- |
| Una pregunta que nombra el total pagado | 26700 (seis pedidos pagados) |
| Una pregunta sobre pedidos pendientes | 2500 (un pedido) |
| Una pregunta que nombra una columna que ninguna vista expone (por ejemplo el nombre del cliente) | El agent dice que no puede responder eso desde lo que tiene vinculado |
| Una consulta que el rol no puede ejecutar (un update, un delete) | Rechazada en la base de datos, `permission denied` |
| `allowed_tools` reducido a herramientas de solo lectura | Una herramienta de escritura que ofrece el servidor no está en absoluto en el conjunto de herramientas del modelo |

## Cuando algo sale mal { #when-it-goes-wrong }

- **La conexión se rechaza con un mensaje de SSRF.** La dirección del
  servidor es loopback, privada o de otro modo interna a la propia red de
  este deployment. Añade su host a `MCP_ALLOWED_HOSTS` tal como lo escribe la
  URL, vuelve a crear el contenedor de la API y guarda la conexión otra vez —
  ver [más arriba](#let-the-deployment-reach-a-server-on-its-own-network) —, o pon una dirección enrutable delante de él.
- **No se ofrece el inicio de sesión, o se rechaza iniciar OAuth, para un
  host de la lista.** Es lo esperado: la comprobación del inicio de
  sesión nunca lee la lista. Conéctate con el token de portador que comprueba
  el servidor.
- **El agent lee la tabla base en vez de las vistas.** A `shop_readonly` se
  le concedió acceso sobre `public` además de `reporting`, o el esquema de
  la tabla base nunca se le revocó. Vuelve a ejecutar el `REVOKE` de arriba.
- **Una consulta que ejecuta el agent parece haber cambiado algo.** No pudo
  haberlo hecho, si el rol de verdad no tiene ninguna concesión de escritura
  — comprueba las concesiones del rol antes de asumir que el agent se portó
  mal.
- **La conexión se sondea con éxito pero el Builder no lista ninguna
  herramienta.** Nada la ha sondeado todavía, o el último sondeo falló —
  `POST /mcp-connections/{id}/test` la refresca.
- **Dos agents necesitan acceso distinto a la misma base de datos.** Conecta
  el servidor dos veces, con dos nombres, cada uno con su propio rol y sus
  propias vistas — un servidor, dos credenciales, nunca un rol ensanchado
  para el agent más estricto.

## Registra la prueba { #record-the-trial }

Conserva el SQL que creó el fixture, las concesiones del rol, las
definiciones de las vistas, el `allowed_tools` de la conexión y el
`allowed_tools` a nivel de vinculación del agent. Una persona sigue
decidiendo qué columnas pertenecen a una vista antes de que un agent llegue
a ella siquiera — reducir el acceso después de que un agent ya esté haciendo
preguntas es una conversación mucho más difícil que decidirlo primero.
