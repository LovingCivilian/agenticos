---
title: "Answer questions from your database"
description: "Connect a self-hosted Postgres MCP server behind a read-only role and a views-only schema, and let an agent query it."
---

# Answer questions from your database

Connect a Postgres MCP server so an agent can answer questions from your own
database, and put a read-only role and a views-only schema between the agent
and your tables before you do. This is a procedure to run, not a report of a
measured deployment: nothing here was run end to end against a live agent.

## What you need

- A [running installation](../install.md) with a model profile.
- A Postgres MCP server you run yourself, reachable at a URL this deployment
  can dial. The catalog's `postgres` entry is exactly this: no hosted URL, a
  bearer token *your own server* checks, and a warning to point it at
  read-only views rather than a primary with write access. See
  [the catalog](../mcp.md#data-and-analytics).
- For a server on this deployment's own network - a compose service, a private
  address - its host in
  [`MCP_ALLOWED_HOSTS`](../configuration.md#project-settings). See
  [below](#let-the-deployment-reach-a-server-on-its-own-network).
- `connections:manage`, to register the organization connection.

## Why a read-only role and a views-only schema

**An approval does not read the SQL for you.** The binding's `approval` policy
can hold every query for a person - `all`, with the exact SQL on the card - but
an agent asking dozens of questions makes that a rubber stamp, and `writes`
trusts the server to mark its query tool read-only. See
[which calls wait for a person](../mcp.md#which-calls-wait-for-a-person). The
database credential itself has to be the boundary, not a setting on the agent.

Two decisions do that work:

- **A role with no write privileges**, so the worst a wrong or manipulated
  query can do is read something it should not - never change or delete a row.
- **A schema of views, not the base tables**, granted to that role instead of
  the tables themselves. A view can drop columns a model should not see and
  pre-aggregate what it returns, which is both a privacy boundary and a
  cheaper query for the agent to write.

## Prepare the input

A small synthetic `orders` table, in a database of its own:

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

The views-only schema and the role the MCP server's connection string
authenticates as:

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

The reference answer, so you can check a reply against the source rows:
`status_counts` gives paid 6 orders / 26700 cents, pending 1 / 2500, refunded
1 / 4200. `shop_readonly` querying `orders` directly is refused with
`permission denied for table orders` - the views are the only door.

## Let the deployment reach a server on its own network

An MCP connection's URL is refused when it is saved if its host resolves to a
loopback, private, link-local or shared-CGNAT address. See
[a URL this deployment must not reach is refused](../mcp.md#a-connection).
Connecting a server on `localhost` reads:

```text
This MCP server URL cannot be used: Blocked: 'localhost' resolves to
private/internal address '::1'. SSRF protection does not allow requests to
internal networks.
```

A server on the deployment's own network - the ordinary place to run one first -
is refused the same way until the operator lists its host - its own name, never
`localhost`, a loopback address or the machine the stack runs on:

1. Run the server under a name such as `postgres-mcp`, on a network the API and
   the Prefect runner share, so both reach it by that name: in the
   `docker-compose.yml` stack, as a service on its `backend` network. Take its
   image and run flags from the server's own README, but publish no port, even
   if the README does: a published port lets it be called from outside, past the
   API. Read [what it can reach](../mcp.md#what-a-listed-host-opens) before you
   choose an image.

    On a server, run it from a compose file and a project (`-p`) of its own that
    joins the stack's network as `external: true`, the way
    [the production frontend does](../deploy.md#option-b-nginx):
    `agenticos_edge` for `docker-compose-prod.yml`, `agenticos_backend` for the
    dev server. Do not edit a tracked compose file instead: that can stop the
    next deploy's checkout.

2. Set the host in `backend/.env`, as a JSON list:

    ```bash
    MCP_ALLOWED_HOSTS=["postgres-mcp"]
    ```

3. Recreate the API container with the compose files that started it, so it
   starts with the new environment - `docker compose restart` keeps the old
   one. In the `docker-compose.yml` stack:

    ```bash
    docker compose up -d app
    ```

    On a server, never run a bare `docker compose` in the clone: it merges
    `docker-compose.override.yml` and replaces the server's API with a local
    build. Name the deploy's files instead - add
    `-f docker-compose-prod.traefik.yml` on a host deployed with
    `PROXY=traefik`, or use `-f docker-compose-dev.yml` on the dev server:

    ```bash
    docker compose --env-file backend/.env -f docker-compose-prod.yml up -d app
    ```

    On a host [`scripts/deploy.sh`](../deploy.md#by-hand) deploys, put the image
    tag it deployed in front of that command:
    `AGENTICOS_VERSION="sha-$(git rev-parse HEAD | cut -c1-7)"`. The script
    [pins the tag only while it runs](../deploy.md#the-images); without the
    prefix, the command starts the version `backend/.env` names, or `latest`.

4. Connect at the server's URL on that name - for example
   `http://postgres-mcp:8000/mcp`, with the port and path its README gives. The
   path has to match the server's transport: a `/sse` segment selects
   server-sent events, anything else streamable HTTP.

Three things hold for a listed host:

- **It is matched exactly and not resolved.** The entry is the host exactly as
  the URL spells it, and saving the connection does not look it up. The URL
  still needs `http://` or `https://`.
- **It is connected with a bearer token.** Sign-in (OAuth) is not offered for
  a listed host, because the sign-in check ignores the list.
- **It opens the whole host, to every account.** Every account on this
  deployment can connect to any port on a listed host, so the server, or a
  proxy in front of it, has to check the token. The read-only role still bounds
  what anyone can read through it. See
  [what a listed host opens](../mcp.md#what-a-listed-host-opens), and how to
  undo an entry.

A deployment that keeps the list empty can still reach a server through a
routable address in front of it: a small host with a public IP, or a tunnel.

The SQL above was run and checked directly against Postgres; the role refusal
and the view totals quoted are real, and so is the URL refusal. What was not
verified is the rest of this page: a listed server being connected, and an agent
actually querying through it.

## Connect the server

1. In an agent's **Toolbox → MCP servers**, choose **Connect a server** and
   pick **PostgreSQL** from the catalog, or connect it once from **MCP
   servers** in the organization settings so more than one agent can bind it.
2. Point the connection at your own running Postgres MCP server (for example
   [`crystaldba/postgres-mcp`](https://github.com/crystaldba/postgres-mcp))
   configured with `shop_readonly`'s connection string and its restricted,
   read-only mode - `http://postgres-mcp:8000/mcp` once its host is listed.
   Paste the bearer token that server checks - not the database password - as
   the connection's token.
3. On the agent's binding, narrow `allowed_tools` to the read-only ones the
   server exposes, on top of whatever the connection itself already allows.
4. Bind only this connection, set a budget, and set instructions that name the
   two views and tell the agent to say when a question needs a column or a
   table the views do not carry, rather than guessing.

## Check the result

| Check | Reference |
| --- | --- |
| A question naming the paid total | 26700 (six paid orders) |
| A question about pending orders | 2500 (one order) |
| A question naming a column no view exposes (e.g. the customer name) | The agent says it cannot answer that from what it is bound to |
| A query the role cannot run (an update, a delete) | Refused at the database, `permission denied` |
| `allowed_tools` narrowed to read-only tools | A write tool the server offers is not in the model's toolset at all |

## When it goes wrong

- **The connection is refused with an SSRF message.** The server's address is
  loopback, private or otherwise internal to this deployment's own network. Add
  its host to `MCP_ALLOWED_HOSTS` exactly as the URL spells it, recreate the API
  container and save the connection again - see
  [above](#let-the-deployment-reach-a-server-on-its-own-network) - or put a
  routable address in front of it.
- **Sign-in is not offered, or starting OAuth is refused, for a listed host.**
  That is expected: the sign-in check never reads the list. Connect with the
  bearer token the server checks.
- **The agent reads the base table instead of the views.** `shop_readonly` was
  granted on `public` as well as `reporting`, or the base table's schema was
  never revoked from it. Re-run the `REVOKE` above.
- **A query the agent runs looks like it changed something.** It could not
  have, if the role truly has no write grant - check the role's grants before
  assuming the agent misbehaved.
- **The connection probes successfully but the Builder lists no tools.**
  Nothing has probed it yet, or the last probe failed - `POST
  /mcp-connections/{id}/test` refreshes it.
- **Two agents need different access to the same database.** Connect the
  server twice, under two names, each with its own role and its own views -
  one server, two credentials, never one role widened for the stricter agent.

## Record the trial

Keep the SQL that created the fixture, the role's grants, the view
definitions, the connection's `allowed_tools`, and the agent's binding-level
`allowed_tools`. A person still decides which columns belong in a view before
an agent ever reaches it - narrowing access after an agent is already asking
questions is a much harder conversation than deciding it first.
