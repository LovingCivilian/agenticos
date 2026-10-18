# MCP allowed hosts — plan first

An MCP server on the deployment's own network - a compose service such as
`postgres-mcp`, a server on the internal network - resolves to a private
address, and the SSRF guard refuses every connection to it with no way past.
`MCP_ALLOWED_HOSTS` is the operator's way past: a list of hosts the operator
vouches for. This plan was written and reviewed before any code, so the shape
was agreed once rather than argued over in review comments. The last section
says what shipped and where it differs.

The one-sentence design: **when a connection's own URL is saved, a host the
operator listed skips the private-address half of the SSRF check and is not
resolved, while the scheme, userinfo and port checks still run, and every
other check - OAuth sign-in, OAuth discovery, the consent URL, the catalog
audit and every pinned request - stays exactly as strict as before.**

Scope: bearer-token and unauthenticated servers. OAuth for a listed host is
out of scope (see [Out of scope](#out-of-scope-deliberately)).

## 0. What already exists

- `app.agents.mcp.validate_mcp_url(url)` is `validate_webhook_url` run in a
  thread. `app.core.sanitize` holds the one SSRF policy: an http or https
  scheme, a hostname, no userinfo, a valid port, and no private, reserved,
  loopback, link-local, multicast, unspecified or CGNAT address, for an IP
  literal or every DNS answer. `resolve_pinned_url` is that policy with the
  checked addresses kept, and `validate_webhook_url` is it with the answer
  thrown away.
- Refusals are `UrlRefusedError` or its subclass `SSRFBlockedError`, written
  in `sanitize.py`, naming the host and never the URL. Callers quote them to a
  person, so a new refusal has to be one of these, with a message written
  there.
- `validate_mcp_url` has three callers: `mcp_connection._checked_url` (saving a
  connection, the sign-in probe and OAuth start), `mcp_oauth.discover` (the
  consent URL, which a browser opens rather than the server fetching it) and
  `mcp_audit.probe_listing` (the catalog audit's second look). Everything the
  OAuth flow fetches goes through `PinnedAsyncClient`, whose transport calls
  `resolve_pinned_url` on every request.
- Two allowlists already exist for the same shape of problem:
  `MEM0_ALLOWED_HOSTS` and `BROWSER_CDP_ALLOWED_HOSTS`. Both are
  deployment-wide JSON lists, both match the host alone, and neither asks who
  wrote the URL they bound.
- `docs/howto/database-questions.md` could not connect its Postgres MCP server
  for exactly this reason.

## Decision 1 — the exemption is a parameter of `validate_webhook_url`, and of nothing else

`validate_webhook_url(url, *, trusted_hosts: frozenset[str] = frozenset())`.
`resolve_pinned_url` is split, with no change in behaviour, into
`_checked_target(url, allowed_schemes) -> (hostname, port)` (the form checks:
parse, scheme, hostname, userinfo, port) and `_pinned(hostname, port)` (the
address check). `validate_webhook_url` runs `_checked_target`, then `_pinned`
only when the hostname is not in `trusted_hosts`.

Why here: the policy stays in one module, and the form checks are the same
code for a listed host as for any other, so they cannot drift.
`resolve_pinned_url` gets no exemption and must never get one: it returns the
addresses a `PinnedAsyncClient` dials, so an exemption there would widen every
OAuth discovery hop. Without one, every pinned path stays strict by
construction. `sanitize.py` does not import `settings`; the caller passes the
set.

## Decision 2 — strict by default: `honour_allowlist`

`validate_mcp_url(url, *, honour_allowlist: bool = False)` reads
`MCP_ALLOWED_HOSTS` only when asked. A new caller that forgets the flag fails
closed. The consent-URL check in `mcp_oauth.py` and the audit's second look in
`mcp_audit.py` keep the default, so neither file changes.

## Decision 3 — only the four save paths ask for it

`mcp_connection._checked_url(url, *, honour_allowlist: bool = False)`. Only
`create`, `update`, `create_for_org` and `update_for_org` - the paths that save
a connection's own address - pass `True`. `probe_sign_in` and `_oauth_start`
keep the strict default.

Why those two stay strict: both run `mcp_oauth.discover` next. A listed URL let
through would be refused hop by hop by the pinned client instead, and
`mcp_oauth._send` logs each refused request's full URL - an operator-typed URL,
query-string key included, in the log. It would also turn today's field-level
400 into "did not advertise OAuth metadata". Kept strict, OAuth behaves exactly
as before.

## Decision 4 — a listed host is not resolved

The "unable to resolve" and "resolved to no address" refusals exist only so the
address check fails closed. With that check waived they protect nothing, and a
compose service that is not running yet, or a name only the worker's network
resolves, should not block saving the connection. The connection's own test
reports an unreachable server.

## Decision 5 — exact matching, normalised at comparison

The URL side is `urlparse(url).hostname`, which is already lower-cased and has
IPv6 brackets stripped. Each entry is normalised when compared, in
`app.agents.mcp._allowed_hosts()`: trimmed, lower-cased, brackets dropped,
blanks skipped, read from `settings` on every call. This follows
`BROWSER_CDP_ALLOWED_HOSTS`, and it holds when a test monkeypatches the
setting.

Nothing else is folded: no wildcards, no CIDR ranges, no ports, no trailing
dot (`postgres-mcp.` is not `postgres-mcp` - an absolute and a relative name
can resolve differently through `resolv.conf` search domains), and IPv6 is
compared as text (`fd00:0::1` is not `fd00::1`). A mismatch falls through to
the normal check, which is the fail-closed direction.

## Decision 6 — no log line for the exemption

The exemption is deployment configuration, not a per-request decision by a
tenant. The durable record already exists: the stored `mcp_connections.url`,
and the audit entry an organization connection writes with its URL. The refusal
path keeps its existing warning.

## Decision 7 — a JSON list, like the other three

`MCP_ALLOWED_HOSTS: list[str] = []`, parsed by pydantic-settings as JSON, the
pattern `CORS_ORIGINS`, `MEM0_ALLOWED_HOSTS` and `BROWSER_CDP_ALLOWED_HOSTS`
use. The operator writes `MCP_ALLOWED_HOSTS=["postgres-mcp"]`; a bare
`MCP_ALLOWED_HOSTS=postgres-mcp` fails at startup. The example in
`backend/.env.example` stays commented out, because `scripts/server-init.sh`
copies every uncommented line into a production `backend/.env`.

## Decision 8 — personal and organization connections both honour it

No role check on the exemption. This follows the other two allowlists: the URL
`MEM0_ALLOWED_HOSTS` and `BROWSER_CDP_ALLOWED_HOSTS` bound is written by anyone
holding `edit` on the agent, and neither asks who wrote it. Each says in its
docs what listing a host opens instead, and so does this one.

## Decision 9 — the data-protection report lists it

`agenticos cmd data-protection-report` prints `MEM0_ALLOWED_HOSTS` and
`BROWSER_CDP_ALLOWED_HOSTS` in its settings section, so a reviewer sees every
operator-named host the deployment talks to. `MCP_ALLOWED_HOSTS` gets the next
row, in the same shape.

## Every path an MCP URL takes

For `http://postgres-mcp:8000/mcp` with `MCP_ALLOWED_HOSTS=["postgres-mcp"]`:

| Path | Checks the URL? | Honours the list? | Result |
|---|---|---|---|
| Create or edit a personal connection's URL | yes | **yes** | saved |
| Create or edit an organization connection's URL | yes | **yes** | saved, audited with the URL |
| Connect a catalog entry | yes, through the create paths | **yes** | saved |
| The connection's test, the scheduled reachability check, an agent turn | no | - | probed or used |
| The sign-in probe and OAuth start | yes | no (Decision 3) | 400 naming `url`, as today |
| OAuth discovery, token and registration requests | yes, pinned | no (Decision 1) | refused |
| The OAuth consent URL | yes | no (Decision 2) | refused |
| The catalog audit, first request and second look | yes | no | `REFUSED` |
| The browser sign-in session's own check | yes | no (default `trusted_hosts`) | unaffected |

The catalog audit must stay strict: its results describe public listings and
ship in the bundled snapshot every deployment seeds, so a listing that names an
internal host must read `REFUSED` everywhere, whatever one deployment lists.

The setting is read at process start, so changing it means recreating the API
container with the compose files that started it; `docker compose restart`
keeps the old environment.

## What the list opens

The operator-facing statement of this is
[What a listed host opens](../mcp.md#what-a-listed-host-opens); the
configuration row, the howto, `.env.example` and the changelog link to it or
summarise it.

- **The whole host.** The match is on the host alone, so every port and path on
  it is reachable. Never list a loopback (`localhost`, `127.0.0.1`, `::1`), the
  machine the stack runs on, or a name one of the stack's own services answers
  to.
- **Every account.** The list is deployment-wide. Every account can save a
  personal connection to a listed host - under the default open sign-up, anyone
  who registers - and every `mcp:manage` holder an organization one. List only
  a server that checks a bearer token for each caller, or whose tools are safe
  for anyone who can sign in.
- **What the server reaches.** A server on the stack's network can reach the
  services on that network, past the reverse proxy.
- **Until the connection is deleted.** The address is checked only when a
  connection's URL is saved. Removing an entry does not disconnect a saved
  connection, and re-enabling a disabled one is not checked again.
- **DNS rebinding.** `validate_webhook_url` never protected against it - the
  MCP client resolves the name again when it connects - and for a listed host
  there is no address check left to rebind around. The list is the operator
  vouching for whatever the name resolves to.

## Work breakdown

One branch, in this order.

1. **`backend/app/core/config.py`** - the setting after
   `BROWSER_EGRESS_PROXY`, with a comment in the voice of the
   `BROWSER_CDP_ALLOWED_HOSTS` one: what it exempts, what never reads it, that
   empty keeps every internal address refused. No validator.
2. **`backend/app/core/sanitize.py`** - `_checked_target` and `_pinned` moved
   out of `resolve_pinned_url` verbatim, same messages, same comments;
   `resolve_pinned_url` keeps its signature; `validate_webhook_url` gains
   `trusted_hosts` (Decision 1). Under the 100% gate.
3. **`backend/app/agents/mcp.py`** - `_allowed_hosts()` and
   `validate_mcp_url(url, *, honour_allowlist=False)` (Decisions 2 and 5), the
   docstring saying when to pass `True` and when never to.
4. **`backend/app/services/mcp_connection.py`** - the flag on `_checked_url`,
   `True` at the four save paths only (Decision 3).
5. **`backend/app/commands/data_protection.py`** - the report row (Decision 9).
6. **No change** to `mcp_oauth.py` or `mcp_audit.py`.
7. **Tests**, written before the code - see [Tests](#tests).
8. **Docs** - `backend/.env.example`; a row in `docs/configuration.md`; in
   `docs/howto/database-questions.md`, the section that explained why the
   server could not be reached becomes how to let the deployment reach it;
   `docs/mcp.md`, whose `url` row and SSRF box would otherwise become false;
   each of those three pages in Polish, German and Spanish; the
   `mcp-connections` skill; a `CHANGELOG.md` entry under `[Unreleased]`. A
   MINOR release: something new an operator can do, with nothing an existing
   deployment has to act on.

## Tests

The three the requirements name: a listed private host is accepted (and not
resolved), a private host not on the list is refused, and a listed host with a
non-http scheme is refused. Around them, by file:

- `tests/test_mcp_allowed_hosts.py` - matching (case on either side, trimmed
  entries, blank entries, IPv6 with or without brackets, IPv4 literals); what
  the list does not open (another host, the form checks, an unparsable URL, the
  empty default, a name containing, under or inside a listed one, a wildcard,
  CIDR, leading-dot or `*.` entry, a trailing dot on either side, the call
  without the flag); no URL in any log record; OAuth discovery never dialling a
  listed host, and a consent URL at one still refused.
- `tests/test_ssrf.py` - `trusted_hosts` on `validate_webhook_url`: a trusted
  literal passes, a trusted name passes unresolved, an untrusted one is
  resolved and refused, every form refusal keeps its type for a trusted host,
  and the default ignores `MCP_ALLOWED_HOSTS`.
- `tests/test_mcp_connections.py` - all four save paths accept a listed host
  without DNS; `_checked_url` called without the flag ignores the list.
- `tests/api/test_mcp_connection_url_refusal.py` - the sign-in probe and OAuth
  start still answer 400 naming `url` for a listed host.
- `tests/test_mcp_audit.py` - the audit's first request and second look are
  both strict for a listed host.
- `tests/test_config.py` and `tests/test_data_protection_report.py` - the
  default, the JSON form, the report row.

Not tested by design: the connection's test and run-time use of a listed host,
which never read the URL check.

## Out of scope, deliberately

- **OAuth for a listed host.** Discovery would need the exemption threaded
  through the pinned client, and its consent URL is chosen by the remote
  server. A listed server is connected with a bearer token or none.
- **Checking again at run time.** Every MCP URL is checked only when it is
  saved, today. Re-checking at the connection's test or a turn would change
  that for every connection, not only listed ones.
- **`host:port` entries.** Every outbound allowlist here matches the host
  alone; ports would make this one the exception.
- **Refusing a malformed entry at startup.** An entry with a port, a scheme or
  a slash never matches and so fails closed, but silently. A startup check
  would make that loud; it is a small follow-up, not part of this change.

## Resolved in review

The design was reviewed before any code. Two questions were the maintainer's:

- **The env format** - a JSON list only, like the other list settings
  (Decision 7).
- **Who may use a listed host** - follow the repo's other allowlists, so
  personal and organization connections both do (Decision 8). Following the
  same convention brought the data-protection report row into scope
  (Decision 9).

The tests were reviewed in three rounds before the implementation, with
mutation runs against a throwaway implementation. The rounds added the cases a
wider matcher would slip past - a subdomain or a part of a listed name, a
wildcard, CIDR, leading-dot or `*.` entry, a trailing dot on the entry - plus
the audit's first request on its own, a URL in a log record's attributes, an
unparsable URL, and the strict defaults of `validate_webhook_url` and
`_checked_url`. One structural test was declined: asserting that `sanitize.py`
holds no reference to `settings` checks the module's shape rather than its
behaviour, and the behavioural default is already pinned.

The implementation was reviewed in three rounds. No code defect was found; the
findings were in what operators read: the howto's compose steps fitted the
local stack but not a server, the caveats in
[What the list opens](#what-the-list-opens) were missing or scattered, and the
docs named the wrong permission.

Found along the way, older than this change and left for their own issues:

- An IDNA-invalid hostname such as `http://a..b/` escapes the SSRF guard as a
  bare `UnicodeError` and answers 500 instead of a 400.
- The console and the API gate organization MCP connections on different
  permissions.
- `make dev-server-frontend` runs without a compose project of its own, so
  compose reports the dev-server backend containers as orphans.

## What shipped, and where it differs

The code is as planned. The differences are in the details and the docs.

- **The permission.** This plan said an organization connection is saved by a
  `connections:manage` holder. The routes are gated on `mcp:manage`, and that
  is what the docs say.
- **Brackets.** `_allowed_hosts` drops a leading `[` and a trailing `]` each on
  its own rather than as a pair. For a well-formed entry the result is the
  same.
- **One home for the caveats.** The plan put the warnings in the configuration
  row. They moved to a new section of `docs/mcp.md`,
  [What a listed host opens](../mcp.md#what-a-listed-host-opens), which grew to
  cover every port and path, the never-list, who can connect under open
  sign-up, what a server on the stack's network reaches, and deleting rather
  than disabling a connection. The configuration row is shorter and links to
  it.
- **The howto covers a server.** The plan's steps fitted the
  `docker-compose.yml` stack only. On a server the MCP server runs from a
  compose file and a project of its own, joins the stack's network as
  external, publishes no port, and the API container is recreated with the
  deploy's own compose files, never a bare `docker compose`; the howto links to
  `docs/deploy.md` for the details it owns.
- **Two sentences went from the howto.** Its fallback no longer offers a
  "VPN-reachable IP", which this check refuses, and it no longer says the same
  check guards a `cdp_url`, which has had its own allowlist for some time.
- **The changelog entry is in `CHANGELOG.fork.md`.** The plan put it under
  `[Unreleased]` in `CHANGELOG.md`. This fork keeps upstream's changelog
  identical to upstream's, so a sync never conflicts there, and records its own
  changes in `CHANGELOG.fork.md` instead.
- **Placement in `docs/mcp.md`.** Two new paragraphs sit later than the plan
  said, so they do not separate a pronoun from what it refers to.
- **Not run end to end.** The tests and the docs build pass, and the MCP
  integration tests run in CI only. Nobody has connected a listed server and
  had an agent query through it; the howto says so.
