# Implementation plan: `MCP_ALLOWED_HOSTS`

Source: `.dev-agent/requirements.md` and `.dev-agent/design.md` (decisions D1-D8, path table §5, tests T1-T18). Do the steps in this order. Each step builds on the ones before it.

Conventions:

- ruff line length is **100** (`backend/pyproject.toml:373`), not the 120 stated in `code-style.md`.
- Use keyword-only flags and precise types. Do not use `Any`.
- Comments explain constraints, not what the code does.
- Every refusal stays `UrlRefusedError` or `SSRFBlockedError`, with messages written in `sanitize.py`.
- Never put a URL in a log line.

## 1. `backend/app/core/config.py`

1. After `BROWSER_EGRESS_PROXY: str = ""` (line 756), before `@field_validator("CORS_ORIGINS")` (758), add a comment block and then:
   ```python
   MCP_ALLOWED_HOSTS: list[str] = []
   ```
   The comment, in the voice of the `BROWSER_CDP_ALLOWED_HOSTS` block (733-741), says three things:
   - What the setting is: hosts an MCP connection's own URL may name although they resolve inside the deployment's network, such as a compose service like `postgres-mcp`.
   - What happens to a listed host: it skips the private-address half of the SSRF check in `app.agents.mcp.validate_mcp_url`, still needs http(s), and is not resolved. The list is exact and case-folded, with no globs, CIDR or ports.
   - What never reads it: OAuth discovery, the consent URL and the catalog audit. Empty keeps every internal address refused.
2. Add no validator. Normalisation happens at comparison (D5). The env format is a JSON list (D8). A bare value fails startup with `SettingsError`, and that is intended (D8, settled with the user).

## 2. `backend/app/core/sanitize.py` (100% line and branch gate)

This step depends on nothing. Behaviour with no `trusted_hosts` must stay byte-for-byte the same.

1. Add the private helper `_checked_target(url: str, allowed_schemes: frozenset[str]) -> tuple[str, int]`. Move today's lines 138-170 into it unchanged, in the same order, with the same messages and the same comments:
   - parse (`UrlRefusedError("The URL could not be parsed")`)
   - scheme (`SSRFBlockedError`)
   - hostname present (`UrlRefusedError`)
   - userinfo (`SSRFBlockedError`)
   - port (`UrlRefusedError("The URL has an invalid port")`)

   It returns `(hostname, port)`. Keep the #861 port comment with the port read. "Read before the IP-literal branch" still holds, because `_pinned` runs after.
2. Add the private helper `_pinned(hostname: str, port: int) -> PinnedAddress`. Move today's lines 172-203 into it unchanged: the IP-literal branch, `getaddrinfo`, the unresolvable and empty-answer refusals, the per-answer check, and the deduplicated `PinnedAddress`.
3. In `resolve_pinned_url` (89), keep the signature and the docstring. The body becomes:
   ```python
   if allowed_schemes is None:
       allowed_schemes = WEBHOOK_ALLOWED_SCHEMES
   return _pinned(*_checked_target(url, allowed_schemes))
   ```
   In the docstring paragraph at 95-98, say that `validate_webhook_url` is this function minus the answer, **plus one exemption a caller names** (`trusted_hosts`). Add one sentence: this function has no exemption and must not get one, because every `PinnedAsyncClient`, and so every OAuth discovery hop, relies on it.
4. In `validate_webhook_url` (206), the signature becomes `validate_webhook_url(url: str, *, trusted_hosts: frozenset[str] = frozenset()) -> str`. The body:
   ```python
   hostname, port = _checked_target(url, WEBHOOK_ALLOWED_SCHEMES)
   if hostname not in trusted_hosts:
       _pinned(hostname, port)
   return url
   ```
   - Add `trusted_hosts` under `Args:`. Its contract: hostnames or IP literals exactly as `urlparse(url).hostname` spells them (lower case, IPv6 without brackets, a trailing dot kept). The caller normalises, and nothing here folds case. A listed host passes the form checks and is then neither resolved nor range-checked.
   - The `Raises:` section does not change.
   - In the paragraph at 212-218, mention that a connection's own address can be exempted by the operator's `MCP_ALLOWED_HOSTS` through `validate_mcp_url`. The rebinding note still applies, and for a listed host there is no address check at all.
5. Do not touch `browser_sign_in_session.py:115`. Its call `validate_webhook_url(url)` gets the empty default.

## 3. `backend/app/agents/mcp.py` (gated). Depends on steps 1 and 2.

1. Imports: add `from app.core.config import settings` in the local-imports group (lines 40-41, alphabetical: `app.agents.spec`, `app.core.config`, `app.core.sanitize`). There is no cycle: `mcp_oauth.py` already imports both.
2. Directly above `validate_mcp_url` (line 90), add:
   ```python
   def _allowed_hosts() -> frozenset[str]:
       hosts = (
           entry.strip().removeprefix("[").removesuffix("]").lower()
           for entry in settings.MCP_ALLOWED_HOSTS
       )
       return frozenset(host for host in hosts if host)
   ```
   The docstring says three things:
   - It returns `MCP_ALLOWED_HOSTS` spelled the way `urlparse` spells a host: trimmed, lower-cased, one pair of IPv6 brackets dropped, blanks skipped.
   - It folds nothing else: no trailing dot, port or glob (D6).
   - It reads the setting on every call, so a test's monkeypatch is seen and there is nothing to invalidate.
3. Change `validate_mcp_url` to `async def validate_mcp_url(url: str, *, honour_allowlist: bool = False) -> str`:
   ```python
   trusted = _allowed_hosts() if honour_allowlist else frozenset()
   return await asyncio.to_thread(validate_webhook_url, url, trusted_hosts=trusted)
   ```
   Extend the docstring with an `Args:` entry for `honour_allowlist`:
   - Pass `True` only for a connection's own address, saved by the person making the connection (`mcp_connection._checked_url` from create and update).
   - A listed host then skips the private-address check and DNS, but not the scheme, userinfo or port checks.
   - Keep the default `False` for everything else: the OAuth consent URL (`mcp_oauth.discover`) and the catalog audit's second look (`mcp_audit.probe_listing`). Those URLs are chosen by a remote server, or judged the same for every deployment.

   Add `Raises:` as in `validate_webhook_url`.

   Keep the existing paragraph about `PinnedAsyncClient` (97-101).

## 4. `backend/app/services/mcp_connection.py` (gated). Depends on step 3.

1. `_checked_url` (line 131) becomes `async def _checked_url(url: str, *, honour_allowlist: bool = False) -> str`, and line 161 becomes `return await validate_mcp_url(url, honour_allowlist=honour_allowlist)`. The `except UrlRefusedError` block (162-165) does not change.

   Add a docstring paragraph explaining D3:
   - The four connection-address callers pass `True`.
   - `probe_sign_in` and `_oauth_start` do not. They go on to `mcp_oauth.discover`, which never honours the list. A listed URL let through here would be refused hop by hop instead, and `mcp_oauth._send` logs each refused request's full URL. OAuth for a listed host is out of scope, and today's 400 is the honest answer.
2. Pass `honour_allowlist=True` at four places:
   - `create` line 673: `url = await _checked_url(data.url, honour_allowlist=True)`
   - `update` line 736
   - `create_for_org` line 1715
   - `update_for_org` line 1789

   **Leave lines 860 (`probe_sign_in`) and 1041 (`_oauth_start`) unchanged.**
3. In the module docstring (lines 23-24), change "SSRF validation of submitted URLs (same policy as webhooks)" to say that a host on the deployment's `MCP_ALLOWED_HOSTS` skips only the private-address check, and only on a connection's own address.

## 4a. `backend/app/commands/data_protection.py` (gated). Depends on step 1.

1. In `_settings_section()`, directly after the `BROWSER_CDP_ALLOWED_HOSTS` row (lines 216-219), add:
   ```python
   ["MCP_ALLOWED_HOSTS", ", ".join(settings.MCP_ALLOWED_HOSTS) or "none"],
   ```
   This is the same shape as `MEM0_ALLOWED_HOSTS` at line 215 (D10). Change nothing else in the section, including its note.

## 5. No code change: `backend/app/agents/mcp_oauth.py`, `backend/app/services/mcp_audit.py`

- Confirm `mcp_oauth.py:342` still reads `await validate_mcp_url(str(asm.authorization_endpoint))` with no flag.
- Confirm `mcp_audit.py:249` still reads `await validate_mcp_url(url)` with no flag.

The tests T10, T12 and T17 pin both.

## 6. Tests (written first by the test phase; listed here so the implementation meets them)

- **`backend/tests/test_mcp_connections.py`**
  - Change the `_allow_any_url` helper (lines 111-118) to `async def _passthrough(url: str, **_kwargs: object) -> str`. Without it, the 34 tests that use it fail on the new `honour_allowlist=` keyword.
  - Add T15:
    - In `TestMcpConnectionService`, beside `test_create_blocks_internal_urls` (2191).
    - In `TestOrganizationConnections`, beside 3861 (stage `repo.create_org_scoped.return_value` as the neighbouring create tests do; use the `audit` fixture) and beside 4101 (`update_for_org`, IPv6 literal).
  - Do not call `_allow_any_url` in these tests. Patch `app.core.sanitize.socket.getaddrinfo` with a recorder and assert that nothing was asked.
- **New `backend/tests/test_mcp_allowed_hosts.py`**: `pytestmark = pytest.mark.anyio`, tests T1-T12.
  - For T11 and T12, swap `mcp_oauth._client` for `lambda: real(httpx2.MockTransport(handler))` (pattern at `test_mcp_connections.py:4750-4754`).
  - Use public IP literals `93.184.216.34`/`.35` for the server and metadata, and `10.0.0.5` for the listed private host, so no test touches DNS.
- **`backend/tests/test_ssrf.py`**: T13 as a new `TestTrustedHosts` class. For T14, add a third parametrised test to `TestEveryRefusalIsWrittenHere`: the form-only refusals with their hosts in `trusted_hosts`.
- **`backend/tests/test_mcp_audit.py`**: T17 (a) and (b) in `TestOneServer`, next to `test_an_address_inside_the_network_is_refused` (152). For (b), the fake `socket.getaddrinfo` answers public for calls 1-2 (`_resolves`, then the pinned request) and private from call 3 (`validate_mcp_url`). Stub `probe_mcp_server` so that a regression would read `OPEN`.
- **`backend/tests/api/test_mcp_connection_url_refusal.py`**: T16 (OAuth start and probe), with `monkeypatch.setattr(settings, "MCP_ALLOWED_HOSTS", ["127.0.0.1"])` and the module's `_LOOPBACK_URL`.
- **`backend/tests/test_config.py`**: T18, using `Settings(_env_file=None)` and `monkeypatch.delenv` / `setenv` so a developer's `backend/.env` cannot change the result.
- **`backend/tests/test_data_protection_report.py`**: T19. In `TestTheReportItself`:
  - `test_settings_never_print_a_credential` (371): also set `MCP_ALLOWED_HOSTS` to `["postgres-mcp"]` and assert `"postgres-mcp" in cells`.
  - `test_an_unconfigured_deployment_reports_the_quiet_answer` (385): also set it to `[]`. Its `"none" in cells` assertion still holds.
  - Or put both cases in one new test beside them, if extending the existing ones would blur what each one is about.

## 7. `backend/.env.example`

After the CORS block (lines 184-187), before `# === Deploying to a server ===` (189), add:

```text
# === MCP servers on this deployment's own network ===
# Hosts an MCP connection may point at although they resolve to a private,
# loopback or other internal address - a compose service such as postgres-mcp,
# a server on the internal network. A JSON list, each host spelled exactly as
# in the server's URL: no scheme, port, wildcard or range; an IPv6 address
# without brackets. A listed host still needs http:// or https:// and is not
# resolved when the connection is saved. Every member of every organization
# can connect a listed host, so list only servers that check a bearer token,
# and never this stack's own services (db, redis, app, localhost). OAuth
# sign-in to a listed host stays refused. Empty refuses every internal address.
# MCP_ALLOWED_HOSTS=["postgres-mcp"]
```

The example line **must stay commented**. `scripts/server-init.sh` (lines 130-140) copies every line it does not rewrite into a production `backend/.env`. Codespell runs over this file.

## 8. `docs/configuration.md`, then `.pl.md`, `.de.md`, `.es.md`

- Add one row to the **Project settings** table after the `BROWSER_EGRESS_PROXY` row (line 58). Table cells are exempt from the 115-word paragraph check. Content per design §8:
  - `` | `MCP_ALLOWED_HOSTS` | `[]` (empty) | … | ``
  - JSON list, like `CORS_ORIGINS`: `["postgres-mcp"]`
  - exact and case-folded; no globs, CIDR ranges, ports or trailing-dot folding; IPv6 without brackets
  - a listed host still needs `http`/`https`, no credentials and a valid port, and is not resolved
  - covers a connection's own URL only, for bearer-token and unauthenticated servers; OAuth sign-in to a listed host is still refused, because discovery is checked without the list
  - deployment-wide: every member of every organization can connect a listed host, so list only servers that check a token, and never the stack's own services
  - removing an entry does not disconnect existing connections; disable or delete them
  - empty keeps every internal address refused
  - link `[MCP](mcp.md#a-connection)`
- Do not add it to the Production checklist.
- Translations: add the same row in each locale at the same position. The variable names, values and code stay English; the prose is translated (`docs/howto/translate.md`). There are no heading changes.

## 9. `docs/howto/database-questions.md`, then `.pl.md`, `.de.md`, `.es.md`

1. Intro (lines 8-14): keep "a procedure to run, not a report of a measured deployment … nothing here was run end to end against a live agent". Drop "cannot be reached from this installation, for a reason explained below". **Do not claim an end-to-end run unless one was actually performed.**
2. Under `## What you need`, add a bullet: a server on this deployment's own network (a compose service, a private address) needs its host in [`MCP_ALLOWED_HOSTS`](../configuration.md#project-settings).
3. Add a new `## Let the deployment reach a server on its own network`, inserted **between** `## Prepare the input` and `## Connect the server`:
   - The default refusal, keeping the quoted `This MCP server URL cannot be used: Blocked: …` text block from lines 117-121.
   - Numbered steps:
     1. Run the server as a compose service on the stack's `backend` network, named e.g. `postgres-mcp`. Take its run flags from the server's own README; do not invent flags.
     2. Set `MCP_ALLOWED_HOSTS=["postgres-mcp"]` in `backend/.env` (a JSON list).
     3. `docker compose up -d app`, which recreates the container with the new environment (`restart` keeps the old one).
     4. Connect at `http://postgres-mcp:8000/mcp`. The path must match the server's transport: a `/sse` segment selects SSE.
   - Then three notes:
     - A listed host is exact and not resolved. It still needs http(s).
     - Bearer token or no credential only. Sign-in (OAuth) is not offered for a listed host.
     - Deployment-wide: any member can connect it, so the server, or a proxy in front of it, must check the token. The read-only role still bounds what anyone can read.
   - Keep the paragraph "The SQL above was run and checked … What was not verified …", reworded so that it does not cite the refusal as the reason.
   - Paragraphs must stay at 115 words or fewer (`scripts/check_docs_paragraphs.py`).
4. Delete `## Why this could not be run here`. Fold its still-true sentences into step 3. Nothing links to its anchor (checked).
5. `## Connect the server`, step 2: name the URL, e.g. `http://postgres-mcp:8000/mcp` once its host is listed.
6. `## When it goes wrong`:
   - Rewrite the SSRF bullet: add the host to `MCP_ALLOWED_HOSTS` exactly as the URL spells it, recreate the API container, and save the connection again, or put a routable address in front of it.
   - Add a bullet: "Sign-in is not offered, or starting OAuth is refused, for a listed host". This is expected, because OAuth discovery is never exempted. Use the server's bearer token.
7. Translations:
   - Mirror the structure.
   - Pin the new heading `{ #let-the-deployment-reach-a-server-on-its-own-network }` at the same position.
   - Remove the `{ #why-this-could-not-be-run-here }` section.
   - Translate `title` and `description` only if they changed (they need not).
   - Check the anchor order with `python3 scripts/check_docs_i18n.py --anchors docs/howto/database-questions.md`.

## 10. `docs/mcp.md`, then `.pl.md`, `.de.md`, `.es.md`

Translations are required: the English text changes, so `check_docs_i18n.py` would fail on a stale fingerprint.

1. Line 30, the `url` row: "SSRF-validated before we ever request it, unless its host is on the deployment's [`MCP_ALLOWED_HOSTS`](configuration.md#project-settings)".
2. Warning box (37-54): after the first paragraph, add one indented paragraph:
   - The one way past is the operator's: a host listed in `MCP_ALLOWED_HOSTS` is not checked against those ranges, or resolved, when a connection is created or edited.
   - Its scheme, userinfo and port still are.
   - Starting an OAuth flow, or the sign-in check, against a listed host is still refused.
3. Under `### Every URL in that flow is checked, not just the one you typed`, after line 341, add one sentence: `MCP_ALLOWED_HOSTS` does not reach this flow, because the pinned client checks every request without it.
4. "Two edges remain", second bullet (361): append that a host on `MCP_ALLOWED_HOSTS` is not range-checked at all, so listing it is the operator vouching for whatever the name resolves to.
5. No heading changes.

## 11. `.claude/skills/mcp-connections/SKILL.md`

After the sentence at lines 103-105 ("The **connection's own URL** is checked at save …"), add:

> `MCP_ALLOWED_HOSTS` exempts a listed host from the private-address half of that save-time check, and from nothing else. It works through `validate_mcp_url(url, honour_allowlist=True)`, which only `_checked_url`'s create and update callers pass. The consent URL, the sign-in probe, OAuth start, the catalog audit and every `PinnedAsyncClient` request stay strict. Never pass the flag for a URL a remote server chose, and never add an exemption to `resolve_pinned_url`.

## 12. `CHANGELOG.md`

Under `## [Unreleased]` (line 18), add `### Added` and one bullet in the house style (bold lead sentence, then 2-4 lines):

> **`MCP_ALLOWED_HOSTS`, for an MCP server on the deployment's own network.** A connection to a host that resolves to a private, loopback or other internal address, such as a compose service like `postgres-mcp`, was refused with no way past. An operator can now list such hosts in a JSON list. Matching is exact and case-insensitive, with no wildcards. A listed host skips the private-address check and is not resolved, but still needs `http` or `https`. It is for bearer-token and unauthenticated servers. OAuth sign-in, its discovery and its consent URL are checked without the list, as before. Empty, the default, changes nothing.

There is no issue number to cite.

## 13. Translation stamping, then verification

1. After the 9 translated files have actually been retranslated, run:
   ```bash
   python3 scripts/check_docs_i18n.py --update \
     docs/configuration.pl.md docs/configuration.de.md docs/configuration.es.md \
     docs/howto/database-questions.pl.md docs/howto/database-questions.de.md docs/howto/database-questions.es.md \
     docs/mcp.pl.md docs/mcp.de.md docs/mcp.es.md
   ```
   Then run `python3 scripts/check_docs_i18n.py` with no arguments; it must be green.
2. Run the narrow local checks below. Leave the full suites to CI, as `AGENTS.md` asks:
   ```bash
   cd backend && uv run pytest tests/test_ssrf.py tests/test_mcp_allowed_hosts.py tests/test_mcp_audit.py \
     tests/test_config.py tests/api/test_mcp_connection_url_refusal.py tests/test_pinned_http.py \
     tests/test_data_protection_report.py -q
   cd backend && uv run pytest tests/test_mcp_connections.py -q      # the _passthrough change + T15
   make lint-backend                                                 # ruff, format, ty, vulture, deptry
   python3 scripts/check_docs_paragraphs.py && python3 scripts/check_docs_i18n.py
   ```
3. Coverage on `app/agents/mcp.py`, `app/core/sanitize.py` and `app/services/mcp_connection.py` is gated in CI (`make test`). Before pushing, check locally with:
   ```bash
   uv run pytest <files above> --cov=app.core.sanitize --cov=app.agents.mcp --cov-branch
   ```

## 14. Ambiguities the tests could settle either way (design picks one)

1. **Probe and OAuth start for a listed host.** The design keeps them strict (400 `url`, T16). The alternative lets them through to discovery: the probe would answer `sign_in: false`, and OAuth start a discovery `OAuthError`. That was rejected because `_send` logs the operator's full URL (D3).
2. **Resolution of a listed host.** The design does not resolve it (T1 asserts that DNS is never asked). The alternative resolves it and refuses only when it does not resolve.
3. **Trailing dot.** Not folded (T9). The alternative folds `host.` into `host`.
4. **IPv6 spelling.** Brackets are stripped from entries, and the comparison is textual (T8), so `fd00:0::1` does not match `fd00::1`. The alternative canonicalises IP literals with `ipaddress`.
5. **Blank entries.** Ignored (T7). A URL with no host is refused earlier anyway.
6. **Malformed entries** (`postgres-mcp:8000`, `http://postgres-mcp`). They silently never match, and nothing validates them at load. The alternative is a startup refusal in `config.py`.
7. **Env format.** Settled: JSON list only. `MCP_ALLOWED_HOSTS=postgres-mcp` fails startup (T18 asserts the JSON form only). See design D8.
8. **Personal connections.** Settled: they honour the list, following the repo's other allowlists (T15, personal create). See design D9.
9. **No log line** on the exemption (D7). No test asserts the absence of a log line. If the review wants one, add `logger.info(..., host)` in `validate_mcp_url`. That requires `mcp.py` to know whether the branch was taken, for example by checking `urlparse(url).hostname in trusted` after the call returns.
10. **T17(b)** depends on the order of `getaddrinfo` calls in `probe_listing` (`_resolves`, then the pinned request, then `validate_mcp_url`). If `probe_listing` is reordered, the stub must follow.
