# Design: `MCP_ALLOWED_HOSTS`

Branch `feat/mcp-allowed-hosts-design`. Requirements: `.dev-agent/requirements.md`.

## 1. What changes, in one paragraph

An operator lists MCP server hosts in `MCP_ALLOWED_HOSTS` (a JSON list, empty by
default). When a member or an organization admin **saves a connection's own URL**
(create or edit, personal or organization), a URL whose host is on the list skips
the private-address half of the SSRF check and is not resolved. The form checks
still run: http/https scheme, a hostname, no userinfo, a valid port. Every other
check stays strict, with no change: OAuth sign-in probe, OAuth start, every OAuth
discovery hop, the consent URL, and the catalog audit. An empty list behaves
exactly as today.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | The exemption is a **keyword-only parameter on `validate_webhook_url`** in `sanitize.py` (`trusted_hosts: frozenset[str] = frozenset()`). It is **not** on `resolve_pinned_url`. | The policy stays in one module. The form checks (scheme, host, userinfo, port) run before the exemption and are the same code, so they cannot drift. `resolve_pinned_url` returns a `PinnedAddress` whose `ips` is "never empty", and every `PinnedAsyncClient` dials those IPs. If it had an exemption, the contract would break and OAuth discovery would be widened too. Without one, every pinned path stays strict by construction. `sanitize.py` does not import `settings`. The caller passes the set. |
| D2 | `validate_mcp_url(url, *, honour_allowlist: bool = False)` reads the setting. **The default is strict.** | Fail-closed: a new caller does not get the exemption by accident. The two strict callers stay **textually unchanged**: `mcp_oauth.py:342` (consent URL) and `mcp_audit.py:249` (catalog audit). This is zero churn in `mcp_oauth.py`. |
| D3 | `mcp_connection._checked_url(url, *, honour_allowlist: bool = False)`. Only the four **connection-address** call sites pass `True`: `create` :673, `update` :736, `create_for_org` :1715, `update_for_org` :1789. `probe_sign_in` :860 and `_oauth_start` :1041 keep the strict default. | Those two run `mcp_oauth.discover(url)` next. If they honoured the list, an allowlisted URL would reach discovery. There `PinnedAsyncClient` refuses every hop, and `_send` logs `logger.warning("Blocked MCP OAuth request to %s: %s", request.url, exc)` (`mcp_oauth.py:189-190`). That would put the **operator-typed URL, query-string key included, in the log**. It would also change OAuth's refusal from today's field-level 400 into "did not advertise OAuth metadata". Keeping them strict means the OAuth behaviour does not change at all. |
| D4 | An allowlisted host is **not resolved** at save. | The "unable to resolve" and "resolved to no address" refusals exist only so the address check fails closed. With the address check waived, they protect nothing. A compose service that is not running yet, or a name only the worker's network resolves, should not block saving. The connection's `/test` probe already reports an unreachable server with its own message. |
| D5 | Matching: exact string equality between `urlparse(url).hostname` and a normalised entry. Entries are normalised **at comparison** in `app/agents/mcp.py` (`_allowed_hosts()`): `strip()`, one pair of `[`/`]` dropped, `lower()`, blanks skipped. `config.py` has no validator. | This follows the `BROWSER_CDP_ALLOWED_HOSTS` precedent (`browser_use/__init__.py:334`, `_route.py:64` normalise at comparison). It also works when tests `monkeypatch.setattr(settings, ...)`, which bypasses validators. `urlparse` already lower-cases the URL side and strips IPv6 brackets (`http://[FD00::1]:8000/` → `fd00::1`, checked). |
| D6 | No wildcards, no CIDR, no ports, **no trailing-dot folding**. `postgres-mcp.` does not match `postgres-mcp`. IPv6 is compared as text, so `fd00:0::1` does not match `fd00::1`. | "Exact" is the requirement. A relative name and an absolute name can resolve differently through `resolv.conf` search domains. A mismatch fails closed: it falls through to the normal check. The operator can list both spellings. |
| D7 | **No new log line** when the exemption applies. | The exemption is static deployment config, not a per-request decision by a tenant. The durable record already exists: the stored `mcp_connections.url`, and `record_audit("mcp_connection.created"/updated, details={"url": ...})` for organization connections (`mcp_connection.py:1752`). `sanitize.py` has no logger, and its contract is that refusals name only the host. The refusal path keeps its existing warning in `_checked_url` (`mcp_connection.py:164`). |
| D8 | The env format follows `CORS_ORIGINS`, `MEM0_ALLOWED_HOSTS` and `BROWSER_CDP_ALLOWED_HOSTS`: a plain `list[str] = []` that pydantic-settings parses as JSON. You write `MCP_ALLOWED_HOSTS=["postgres-mcp"]`. | This is the exact existing pattern (`config.py:721-742`, `.env.example:184-187`). Checked with pydantic-settings 2.15.0: `MCP_ALLOWED_HOSTS=postgres-mcp` (bare) raises `SettingsError` at startup, `[]` and empty give `[]`. **Settled with the user:** JSON list only (was Q1). |
| D9 | **Personal and organization connections both honour the list.** There is no role check on the exemption. | **Settled with the user: follow the repo's existing allowlists.** `MEM0_ALLOWED_HOSTS` (`memory_mem0/_client.py:102`) and `BROWSER_CDP_ALLOWED_HOSTS` (`browser_use/__init__.py:334`) are deployment-wide operator lists, and the URL they bound is written by "anyone holding `edit` on that agent" (`browser_use/__init__.py:287`). Neither asks who wrote it. Both say what the list opens in their docs instead (`docs/configuration.md:55-56`). This setting does the same: the docs say to list only servers that check a token per caller. |
| D10 | `MCP_ALLOWED_HOSTS` gets a row in the data-protection report's settings section, next to the other two allowlists. | **Repo convention:** `app/commands/data_protection.py:215-219` reports `MEM0_ALLOWED_HOSTS` and `BROWSER_CDP_ALLOWED_HOSTS` as `", ".join(...) or "none"`. A reviewer reading that report should see every operator-named host this deployment talks to. The report is under the coverage gate (`pyproject.toml:598`). The command is not in the `scripts/docs_drift.py` map, and `docs/data-protection.md` does not list the report's rows, so no docs page changes for it (was Q4). |

## 3. Modules, functions and contracts

### `backend/app/core/config.py` (not under the coverage gate)

Add the declaration after `BROWSER_EGRESS_PROXY: str = ""` (line 756), before `@field_validator("CORS_ORIGINS")` (758):

```python
MCP_ALLOWED_HOSTS: list[str] = []
```

Put a comment above it, in the style of `BROWSER_CDP_ALLOWED_HOSTS`. Say what it exempts, what never reads it, and that empty keeps everything refused. Add no validator.

### `backend/app/core/sanitize.py` (gated, 100% line and branch coverage)

Split `resolve_pinned_url` (lines 89-203) without changing its behaviour:

- `_checked_target(url: str, allowed_schemes: frozenset[str]) -> tuple[str, int]`. This is today's lines 138-170, verbatim and in the same order: parse, then scheme, then hostname present, then userinfo, then port. It returns `(hostname, port)`. Raises `UrlRefusedError` (unparsable, no hostname, invalid port) or `SSRFBlockedError` (scheme, userinfo). Every message stays as it is today.
- `_pinned(hostname: str, port: int) -> PinnedAddress`. This is today's lines 172-203, verbatim: an IP literal, or every DNS answer, must not be blocked. Raises `SSRFBlockedError`.
- `resolve_pinned_url(url, allowed_schemes=None) -> PinnedAddress`. Same signature, same contract. The body becomes `_pinned(*_checked_target(url, schemes))`. Add one docstring sentence: **it has no exemption and must not get one** (D1).
- `validate_webhook_url(url: str, *, trusted_hosts: frozenset[str] = frozenset()) -> str`:
  - Runs `_checked_target(url, WEBHOOK_ALLOWED_SCHEMES)`.
  - If `hostname not in trusted_hosts`, it runs `_pinned(hostname, port)`.
  - Returns `url` unchanged.
  - Contract: `trusted_hosts` holds hostnames or IP literals exactly as `urlparse(...).hostname` spells them: lower case, IPv6 without brackets, trailing dot kept. The caller normalises. A listed host passes the form checks and is then neither resolved nor range-checked.
  - Raises: the same two types, so callers that quote the message stay safe (`UrlRefusedError` docstring, lines 20-33).
  - With an empty set the behaviour is identical to today.
- Update the docstrings: in `resolve_pinned_url` (lines 95-98), "validate_webhook_url is this function with the answer thrown away" gains "...plus an exemption a caller names for hosts an operator vouched for". In `validate_webhook_url` (lines 212-225), name `MCP_ALLOWED_HOSTS` through `app.agents.mcp.validate_mcp_url`.

### `backend/app/agents/mcp.py` (gated)

- Import `from app.core.config import settings`. This is safe: `mcp_oauth.py` already imports both, and `import app.agents.mcp` was checked.
- `_allowed_hosts() -> frozenset[str]` (private). Reads `settings.MCP_ALLOWED_HOSTS` **on every call**, with no cache, and normalises per D5.
- `validate_mcp_url(url: str, *, honour_allowlist: bool = False) -> str` (line 90):
  - `trusted = _allowed_hosts() if honour_allowlist else frozenset()`
  - `return await asyncio.to_thread(validate_webhook_url, url, trusted_hosts=trusted)`
  - The docstring says when to pass `True`: only for a connection's own address, saved by the person making the connection. It also says never to pass `True` for a URL that a remote server chose (discovery, consent) or that is judged the same for every deployment (the catalog audit).
  - Raises: as `validate_webhook_url`.

### `backend/app/services/mcp_connection.py` (gated)

- `_checked_url(url: str, *, honour_allowlist: bool = False) -> str` (line 131) passes the flag through. The refusal handling (lines 160-165) does not change. In the docstring, add one paragraph: the four connection-address callers pass `True`, the two OAuth callers do not, and why (D3).
- Pass `honour_allowlist=True` at lines 673, 736, 1715 and 1789.
- Module docstring (lines 23-27): "SSRF validation of submitted URLs (same policy as webhooks)" gains "except that a host on `MCP_ALLOWED_HOSTS` skips the private-address check".

### `backend/app/commands/data_protection.py` (gated)

- In `_settings_section()`, add `["MCP_ALLOWED_HOSTS", ", ".join(settings.MCP_ALLOWED_HOSTS) or "none"]` directly after the `BROWSER_CDP_ALLOWED_HOSTS` row (lines 216-219). Same shape as the other two (D10). The values are printed raw, as the other two are: they are hostnames the operator typed, not credentials.

### `backend/app/agents/mcp_oauth.py` and `backend/app/services/mcp_audit.py`

**No change.** Their calls `validate_mcp_url(str(asm.authorization_endpoint))` (342) and `validate_mcp_url(url)` (249) get the strict default. Their pinned requests go through `PinnedTransport.handle_async_request` → `resolve_pinned_url` (`pinned_http.py:111`), which has no exemption.

## 4. Data flow

```text
backend/.env  MCP_ALLOWED_HOSTS=["postgres-mcp"]
  → (compose env_file ./backend/.env, docker-compose.yml:44/75/219/404; app and worker alike)
  → Settings.MCP_ALLOWED_HOSTS: list[str]                       (JSON-decoded at process start)
  → app.agents.mcp._allowed_hosts() → frozenset{"postgres-mcp"} (normalised on each call)
  → validate_mcp_url(url, honour_allowlist=True)
  → sanitize.validate_webhook_url(url, trusted_hosts=…)  [in a thread]
       _checked_target: scheme ∈ {http,https}, host, no userinfo, port  → refusals unchanged
       host ∈ trusted?  yes → return url (no DNS)   no → _pinned(): today's address check
  → mcp_connection._checked_url → UrlRefusedError becomes refused_field("url", …) 400 as today
```

The setting is read at process start, so changing it means recreating the container.
`docker compose up -d app` recreates it with the new environment. `restart` does not.

## 5. Every path an MCP URL takes

| Path | Code | Checks the URL? | Honours the list? | Result for `http://postgres-mcp:8000/mcp`, listed |
|---|---|---|---|---|
| Create personal connection, `POST /me/mcp-connections` | `create` → `_checked_url` :673 | yes | **yes** | saved |
| Edit personal URL, `PATCH /me/mcp-connections/{id}` | `update` :736 (only when `url` is in the body) | yes | **yes** | saved |
| Create org connection, `POST /mcp-connections` | `create_for_org` :1715 | yes | **yes** | saved (audited with the URL) |
| Edit org URL | `update_for_org` :1789 | yes | **yes** | saved |
| Catalog entry connected (e.g. `postgres`, empty catalog `url`) | the same create paths | yes | **yes** | saved |
| `/test`, personal and org | `test`/`test_for_org` → `_probe` → `_reach` → `probe_mcp_server` (:519, :839-850) | **no** | n/a | probed |
| Scheduled reachability check | `_reach` at :596 | **no** | n/a | probed |
| Agent turn | `build_toolsets_for_agent` → `probe_toolsets` (`mcp.py:523`) | **no** | n/a | used |
| Sign-in probe, `POST /me/mcp-connections/probe` | `probe_sign_in` :860 | yes | **no** (D3) | 400 `url`. The dialog swallows it (`mcp-connection-dialog.tsx:161` `.catch(() => null)`) and the token field stays |
| OAuth start, personal and org | `_oauth_start` :1041 | yes | **no** (D3) | 400 `url`, as today |
| OAuth discovery hops, token, registration | `PinnedAsyncClient` → `resolve_pinned_url` | yes | **no**, structurally (D1) | refused |
| OAuth consent URL | `mcp_oauth.py:342` `validate_mcp_url(...)` | yes | **no** (default) | refused |
| Catalog audit, first request | `mcp_audit._ask` through `PinnedAsyncClient` (:229-232) | yes | **no** | `REFUSED` |
| Catalog audit, second look | `mcp_audit.py:249` `validate_mcp_url(url)` | yes | **no** (default) | `REFUSED` |
| Catalog audit `_resolves` | `mcp_audit.py:180-188`, a bare `getaddrinfo` | refuses nothing by address. It only tells "gone" from "refused" | n/a | unchanged |
| Schema validators | `McpConnectionCreate/Update.url`, `McpSignInProbe.url`, `McpOAuthStart.url`: length only (`schemas/mcp_connection.py`). `schemas/urls.py` `ServiceAddress` is not used by MCP | no | n/a | n/a |
| Frontend | no host restriction (checked `components/mcp/`, `lib/mcp-connections-api.ts`) | no | n/a | n/a |
| Browser sign-in session | `browser_sign_in_session.py:115` `validate_webhook_url(url)` | yes | **no** (default `trusted_hosts`) | unaffected |

The catalog audit does not need the list for the feature to work. It is not on the save, test or use path of a connection. It must not honour the list: its results describe public listings and ship in the bundled `mcp_checks.json` snapshot. A third-party registry listing that names an internal-looking host must read `REFUSED` on every deployment.

**Conclusion:** with `MCP_ALLOWED_HOSTS=["postgres-mcp"]`, a bearer-token or unauthenticated server at `http://postgres-mcp:8000/mcp` can be saved, tested and used. No other path changes.

## 6. OAuth isolation

There are three layers. Each one is tested on its own.

1. **Discovery is structurally strict** (D1). Every request goes through `PinnedTransport` → `resolve_pinned_url`, which has no exemption.
2. **The consent URL is strict by default** (D2). `mcp_oauth.py:342` calls `validate_mcp_url` without the flag. The file has no change.
3. **The OAuth entry points are strict before discovery** (D3). `probe_sign_in` and `_oauth_start` call `_checked_url` without the flag. That keeps today's 400 and keeps an operator-typed URL out of `_send`'s warning line.

The alternatives were rejected:

- A separate strict function at the consent site needs new imports in `mcp_oauth.py`.
- Making the list the default and opting out at `mcp_oauth.py:342` and `mcp_audit.py:249` fails open for future callers.

## 7. Security considerations

- **What the list opens.** For a listed host, the API (and the Prefect worker, which runs scheduled and triggered agent turns) makes server-side MCP requests to whatever that name resolves to at run time. That includes private, loopback and metadata ranges. The trust is in the resolver (Docker's embedded DNS, internal DNS). If the compose service is absent, a bare name can fall through to upstream DNS and `resolv.conf` search domains.
- **Deployment-wide, cross-tenant.** Every member of every organization can save a **personal** connection to a listed host. Every `connections:manage` holder in any organization can save an organization one. List only servers that authenticate each caller with a bearer token, or whose tools are safe for everyone. **Never list the platform's own services or a loopback** (`db`, `redis`, `app`, `localhost`, `127.0.0.1`). Inside the `app` container, loopback is the API itself. This follows the other allowlists (D9).
- **Per host, all ports.** Ports are not part of the match, so a listed host is reachable on any port.
- **Exact match** means `postgres-mcp.attacker.example`, `x-postgres-mcp` and `postgres-mcp.` never match. CIDR or globs would expose whole ranges, metadata included.
- **DNS rebinding.** `validate_webhook_url` never protected against it (it re-resolves at connect, #840, `sanitize.py:212-225`). For a listed host there is no address check left to rebind around. The list is the operator vouching for the name.
- **Parser differential.** Comparing urllib's `hostname` against what httpx dials is the same comparison the existing check resolves. This is not a new class of risk.
- **What still holds.** The scheme, userinfo and port refusals stay. Refusal messages still name only the host. Pinned paths and the consent URL are unchanged. An operator-typed URL never reaches `_send`'s URL-logging warning.
- **Save-time only.** Removing an entry does **not** disconnect existing connections. The address is checked when a connection is saved, never at `/test` or at run time, as today. Disable or delete those connections. See §10.

## 8. Documentation

Each English page edited below has `.pl.md`, `.de.md` and `.es.md` siblings. `scripts/check_docs_i18n.py` gates `make lint` on their `source_sha`. Retranslate the changed passages in all three in the same change, then run `python3 scripts/check_docs_i18n.py --update <each retranslated file>`, naming only the files you actually retranslated (`docs/howto/translate.md`). Translated headings pin the English anchors.

| File | Change |
|---|---|
| `backend/.env.example` | New block after `# === CORS ===` (lines 184-187): `# === MCP servers on this deployment's own network ===`. Explain the JSON-list format, the host spelling (no scheme, port, glob or CIDR; IPv6 without brackets), that a listed host still needs http(s) and is not resolved, that the list is deployment-wide and should hold only token-checked servers, that OAuth stays refused, and that empty is the default. Example line **commented out**: `# MCP_ALLOWED_HOSTS=["postgres-mcp"]`. `scripts/server-init.sh` copies every other line into a production `backend/.env`, so a live example would allowlist `postgres-mcp` on every new deployment. |
| `docs/configuration.md` (+3) | One row in **Project settings** after `BROWSER_EGRESS_PROXY` (line 58): default `[]` (empty); a JSON list like `CORS_ORIGINS`; exact and case-folded; no globs, CIDR, ports or trailing-dot folding; IPv6 without brackets; still http(s), no userinfo, valid port; not resolved; connection URLs only, bearer or unauthenticated; OAuth sign-in stays refused; deployment-wide warning; never list the platform's own services; removing an entry does not disconnect; empty refuses every internal address. **Not** added to the Production checklist: its default is the production-safe one. |
| `docs/howto/database-questions.md` (+3) | The intro keeps "nothing here was run end to end against a live agent" but drops "cannot be reached from this installation". "What you need" gains a bullet: a server on the deployment's own network needs its host in `MCP_ALLOWED_HOSTS`. Replace `## Why this could not be run here` with `## Let the deployment reach a server on its own network`, placed **before** `## Connect the server`. It covers the default refusal (keep the quoted message), the four steps (a compose service on the `backend` network; `MCP_ALLOWED_HOSTS=["postgres-mcp"]` in `backend/.env`; `docker compose up -d app`; the connection URL `http://postgres-mcp:8000/mcp`, with the path matching the server's transport, since a `/sse` segment selects SSE), the bearer-only and no-OAuth note, the deployment-wide caveat, and the existing honesty paragraph about what was and was not verified. "Connect the server" step 2 names the URL. In "When it goes wrong", the SSRF bullet becomes "add the host exactly as the URL spells it, recreate the API container, save again — or put a routable address in front"; add a bullet for sign-in not offered or OAuth refused for a listed host (expected). New anchor order (translations pin the same): `…prepare-the-input, let-the-deployment-reach-a-server-on-its-own-network, connect-the-server, check-the-result, …`. The old anchor `why-this-could-not-be-run-here` has no inbound links (checked). |
| `docs/mcp.md` (+3) | The current text becomes false. Row `url` (line 30): "SSRF-validated before we ever request it, unless its host is on [`MCP_ALLOWED_HOSTS`](configuration.md#project-settings)". In the warning box (37-54), add a paragraph: the operator's way past for a listed host, form checks still apply, OAuth start and sign-in still refused. Under "Every URL in that flow is checked" (328+), add one sentence: the pinned client never reads the list. The "Two edges remain" second bullet (361) gains: "a host on `MCP_ALLOWED_HOSTS` is not range-checked at all". No heading changes. |
| `.claude/skills/mcp-connections/SKILL.md` | Lines 101-105, "the connection's own URL is checked at save": add that `MCP_ALLOWED_HOSTS` exempts a listed host from the private-address half of that save-time check only, through `validate_mcp_url(url, honour_allowlist=True)` from `_checked_url`'s four connection-address callers. Never pass the flag for a URL a remote server chose, and never add an exemption to `resolve_pinned_url`. |
| `CHANGELOG.md` | Under `## [Unreleased]` (line 18), add `### Added` with one bullet (format of 1.2.0). Release type: MINOR. `CHANGELOG.md` has no translations (`scripts/docs_i18n.py:48`). |
| No change | `docs/security.md` (no MCP SSRF claim; only line 31, the web connector), `docs/data-protection.md`, `docs/reference/*` (no mkdocstrings for these modules), `AGENTS.md` (no new page), `mkdocs.yml`. `scripts/docs_drift.py` maps `agents/mcp*` and `services/mcp_*` → `docs/mcp.md`, and `core/config.py` → `docs/configuration.md`. Both are covered above. |

## 9. Test strategy

Settings are overridden with `monkeypatch.setattr(settings, "MCP_ALLOWED_HOSTS", [...])`, as in `test_browser_route.py:40`. DNS is faked with `monkeypatch.setattr("app.core.sanitize.socket.getaddrinfo", fake)` (`test_ssrf.py:143`, `test_mcp_connections.py:4441`), or with `socket.getaddrinfo` in `test_mcp_audit.py:33`. IP literals avoid DNS entirely. Async tests use `pytestmark = pytest.mark.anyio` or a per-test `@pytest.mark.anyio`. Gated modules touched: `app/agents/mcp.py`, `app/core/sanitize.py`, `app/services/mcp_connection.py` (and `mcp_audit.py`, unchanged). Every new branch below is hit both ways.

| # | Behaviour | File (layer) | Why there |
|---|---|---|---|
| T1 | **REQUIRED**: a listed private host is accepted: `http://postgres-mcp:8000/mcp` with list `["postgres-mcp"]`, and **getaddrinfo is never called** | new `tests/test_mcp_allowed_hosts.py` (unit, `validate_mcp_url(..., honour_allowlist=True)`) | the requirement names `validate_mcp_url`; D4 |
| T2 | **REQUIRED**: a private host not on the list is refused: `http://10.0.0.9:8000/mcp`, and `http://other-mcp:8000/mcp` with DNS → 10.0.0.9; `SSRFBlockedError`, the message names the host | same | the list widens nothing else |
| T3 | **REQUIRED**: a listed host on a non-http scheme is refused: `ftp://`, `ws://`, `file://` + `postgres-mcp`; `SSRFBlockedError` "scheme" | same | requirement |
| T4 | a listed host with userinfo is refused; one with `:client_secret=sh-x` as its port is refused as `UrlRefusedError` without echoing it | same | D1 form checks; #861 |
| T5 | empty list (default) with the flag on: a loopback literal and a privately resolving name are refused as today | same | requirement "empty keeps today's behaviour" |
| T6 | case-insensitive both ways: entry `Postgres-MCP` against URL `postgres-mcp`, and entry `postgres-mcp` against URL `POSTGRES-MCP` | same | D5 |
| T7 | entries are trimmed, blanks are ignored (`["  postgres-mcp  ", ""]`) | same | D5 |
| T8 | IPv6: entry `fd00::1` and entry `[FD00::1]` both accept `http://[fd00::1]:8000/mcp`; an IPv4 literal entry accepts its URL | same | D5 |
| T9 | exact only: `postgres-mcp.` (trailing dot), `postgres-mcp.attacker.test` (suffix), `x-postgres-mcp` (prefix) go through the normal check and are refused (DNS → private) | same | D6 |
| T10 | without the flag the list is not read: `validate_mcp_url(url)` with the list set → refused | same | D2, the guard for consent and audit |
| T11 | OAuth discovery never reaches a listed private host: `mcp_oauth.discover("http://10.0.0.5:8000/mcp")`, list `["10.0.0.5"]`, `_client` swapped for a `MockTransport` recorder → `OAuthError`, recorder saw nothing | same (unit) | D1; pattern `test_mcp_connections.py:4750` `_serving` |
| T12 | the consent URL at a listed private host is still refused: public server, metadata `authorization_endpoint: https://10.0.0.5/authorize`, list `["10.0.0.5"]` → `OAuthError` "blocked address" | same (unit) | D2; pattern `TestAUrlNoRequestCanBeBuiltFor._metadata` (4766) |
| T13 | `validate_webhook_url(url, trusted_hosts=…)`: a trusted literal passes; a trusted name passes unresolved; an untrusted name is resolved and refused as before | `tests/test_ssrf.py` (unit, sync, `security` mark) | `sanitize.py`'s own contract, gated |
| T14 | every form refusal is still `UrlRefusedError` when the host is trusted (a parametrised copy of `TestEveryRefusalIsWrittenHere._REFUSED`, excluding `http://127.0.0.1/hook`, with its hosts trusted) | `tests/test_ssrf.py` | "the type is the promise" (`sanitize.py:20-33`) |
| T15 | service: a personal `create` with a listed host stores the URL, with no DNS; the org `create_for_org` likewise; `update_for_org` moving the URL to a listed IPv6 host is accepted | `tests/test_mcp_connections.py` `TestMcpConnectionService` beside `test_create_blocks_internal_urls` (2191), `TestOrganizationConnections` beside 3861 and 4101 (unit, mocked repo) | D3 call sites; the real validator runs (no `_allow_any_url`) |
| T16 | API: `POST /me/mcp-connections/oauth/start` and `POST /me/mcp-connections/probe` with a listed loopback (`127.0.0.1`) still answer **400 `BAD_REQUEST`, `details.fields == [url]`**, the strict `_checked_url` refusal rather than a discovery `OAuthError` | `tests/api/test_mcp_connection_url_refusal.py` (API) | D3, OAuth isolation at the route; no DB is reached |
| T17 | the catalog audit stays strict for a listed host: (a) DNS → private at the first request → `REFUSED`; (b) DNS public for `_resolves` and the pinned request, private at the second look (`:249`), `probe_mcp_server` stubbed → `REFUSED`, not `OPEN` | `tests/test_mcp_audit.py` `TestOneServer` (unit) | §5; (b) pins `:249` strict |
| T18 | `Settings(_env_file=None)` with the env var cleared → `MCP_ALLOWED_HOSTS == []`; with `MCP_ALLOWED_HOSTS='["postgres-mcp"]'` set → `["postgres-mcp"]` | `tests/test_config.py` (unit) | the default and the documented env format (D8) |
| T19 | the data-protection report's settings section names a listed host (`["postgres-mcp"]` → `"postgres-mcp"` in the cells) and answers `none` for an empty list | `tests/test_data_protection_report.py` `TestTheReportItself` (unit), extending `test_settings_never_print_a_credential` (371) and `test_an_unconfigured_deployment_reports_the_quiet_answer` (385) the way `MEM0_ALLOWED_HOSTS` is set there | D10 |

Not tested, by design: `/test` and run-time use of a listed host. Neither reads the URL check (§5), and their existing tests cover the probe. An API-level create success is not tested either: the API fixture's mocked session cannot complete a repository write. T15 covers that layer.

## 10. Settled questions and a known limitation

- **Env format (was Q1): JSON list only.** `MCP_ALLOWED_HOSTS=["postgres-mcp"]`, like the other three list settings. A bare value fails startup (D8).
- **Who (was Q2): personal and organization connections**, following `MEM0_ALLOWED_HOSTS` and `BROWSER_CDP_ALLOWED_HOSTS` (D9).
- **Data-protection report (was Q4): in scope**, following the same two settings (D10, T19).
- **Known limitation (was Q3): save-time only.** Removing an entry leaves existing connections working, because an MCP URL is checked when a connection is saved and never at `/test` or at run time. That is today's behaviour for every MCP URL. Re-checking at run time would change it for every connection, not just listed ones, so it stays out of scope. The docs say to disable or delete those connections.
