# Implementation plan: gate the console's MCP controls on `mcp:manage`

Sources: `.dev-agent/requirements.md`, `.dev-agent/design.md`. Base:
`upstream/main` fb4ec90ac. Frontend paths are under `frontend/src/`. Do not
run whole-directory suites, `make check`, `make test` or `make test-frontend-cov`.

## Order

Write the tests first; they fail on the current code. Then make the code
change, then the comments, the docs and the changelog.

### 1. MCP servers page test (new)

File: `app/[locale]/(dashboard)/mcp-servers/page.integration.test.tsx`.

It mounts `McpServersPage` with the real `usePermissions` over a mocked API,
and shows that "Add a custom server" follows `mcp:manage`.

- Mock `@/lib/api-client`: spread `actual`, and make `apiClient` `{ get, post, patch, delete }` as `vi.fn()`.
- Mock `sonner`.
- Use a `QueryClient` (`retry: false`) that the test holds.
- `serve(permissions)`: see design.md. `/mcp-connections` rejects with
  `ApiError(403)` unless `mcp:manage` is held.
- Case `["mcp:manage"]`: the button is found and visible.
- Case `["connections:manage"]`:
  - find "GitHub";
  - wait until the permissions query (`qk.organizations.permissions("current")`) has status `"success"`;
  - then the button is `null`.
- Patterns: `runs/approvals-permission.integration.test.tsx` and
  `components/mcp/mcp-server-list.integration.test.tsx`.

### 2. Builder test (new)

File: `app/[locale]/(dashboard)/agents/[id]/mcp-gate.integration.test.tsx`.

Copy the harness of `agents/[id]/metadata-gate.integration.test.tsx`, then make
the changes listed in design.md:

- `tab=mcp`;
- an org binding to never-probed `c1`;
- one GitHub catalog entry;
- `useOrgMcpConnections.test = probe`;
- `useMcpServers` in the `@/hooks` mock;
- a partial mock of `@/hooks/use-mcp-servers` (`useMcpCatalog`, `useMcpCatalogPage`);
- `allowed = { agents:edit } + permission under test`.

Cases:

- `{agents:edit, mcp:manage}`:
  - (a) "Connect a server" is present. Click it. Inside the dialog "Connect an
    MCP server", "Add a custom server" is present.
  - (b) Click "Settings for GitHub in this agent", then "Load its tools".
    `probe` is called with `"c1"`, and the checkbox "create_issue" appears.
- `{agents:edit, connections:manage}`:
  - (c) Wait for "Settings for GitHub in this agent". "Connect a server" is absent.
  - (d) Use the same path as (b). Find the text "Nothing has checked this
    connection yet". `probe` was not called, and there is no "create_issue"
    checkbox.

Give the file a docstring. It names the three gates, says why the dialog's
negative case is the button's absence, and says the mocked list is served to
both callers.

### 3. Onboarding tests

`lib/onboarding/flows.test.ts`:

- :195 → `Perm.mcpManage`.
- :493–502:
  - deny `Perm.mcpManage`, and rename the title and comment to match;
  - add the reverse case: deny only `connections:manage`, answer the fork
    "yes", and expect `flow-agent-mcp-ask` plus the four detour steps.

`lib/onboarding/tour.test.ts`: assert that `mcp-add`'s permission is `Perm.mcpManage`.

Run steps 1–3. All the new assertions fail. Nothing else fails.

### 4. Code: 12 × `Perm.connectionsManage` → `Perm.mcpManage`

- `mcp-servers/page.tsx:91`
- `agents/[id]/page.tsx:1130, 1569, 1618`
- `lib/onboarding/flows.ts:369, 378, 599, 608, 618, 716, 723`
- `lib/onboarding/tour.ts:644`

Then grep `connectionsManage` in these four files. Only these remain: agents
:373, :1377, :1380; flows :416, :454; tour :499, :510, :522.

### 5. Comments and test titles (permission name only)

Change the 15 lines listed in design.md, "Comments and test titles", in:

- `agents/[id]/page.tsx`
- `flows.ts`
- `mcp-server-list.tsx`
- `use-mcp-servers.ts`
- `use-org-mcp-connections.ts`
- `org-mcp-connections-api.ts`
- `binding-tools.ts`
- `use-org-mcp-connections.test.tsx`
- `use-mcp-connections.test.tsx`
- `binding-tools.test.ts`
- `mcp-server-list.integration.test.tsx` (the title at :424)

Afterwards, a grep of `frontend/src` for `connections:manage` near
`mcp`/`MCP`/`probe`/`org list` must show only:

- `new-event-trigger-dialog.tsx:37`
- `new-event-trigger-dialog.test.tsx:57, :70, :72`

### 6. Docs (token only) and restamp

Edit `docs/howto/database-questions.md:23`, `docs/howto/meeting-to-tasks.md:13`,
`docs/howto/notion-agent.md:13, :20` and `docs/mcp.md:155`. Then edit their
`.pl/.de/.es` lines as listed in design.md. Then restamp:

```bash
uv run --directory backend python3 ../scripts/check_docs_i18n.py --update \
  ../docs/howto/database-questions.{pl,de,es}.md \
  ../docs/howto/meeting-to-tasks.{pl,de,es}.md \
  ../docs/howto/notion-agent.{pl,de,es}.md \
  ../docs/mcp.{pl,de,es}.md
```

The command must report "Stamped 12 translation(s)". Leave `docs/ROADMAP.md`
as it is.

### 7. `CHANGELOG.md`

Under `## [Unreleased]`, add `### Fixed` and the entry from design.md, word for
word.

## Verification

```bash
make lint-frontend
cd frontend && bunx vitest run \
  "src/app/[locale]/(dashboard)/mcp-servers/page.integration.test.tsx" \
  "src/app/[locale]/(dashboard)/agents/[id]/mcp-gate.integration.test.tsx" \
  src/lib/onboarding/flows.test.ts src/lib/onboarding/tour.test.ts \
  src/components/mcp/mcp-server-list.integration.test.tsx \
  src/hooks/use-org-mcp-connections.test.tsx src/hooks/use-mcp-connections.test.tsx \
  src/lib/binding-tools.test.ts
uv run --directory backend python3 ../scripts/check_docs_i18n.py
make docs-build
```

Before `make lint-frontend`, run `bunx prettier --write` on the two new tests.
Quote the `[locale]` paths; the quoted form works.

## Points the tests settle

- **MCP page, case 2.** Without the permissions-query wait, case 2 can pass on
  unfixed code. Check that it fails before step 4. If "GitHub" never appears,
  the page treats the org list's 403 as a page error. That is a real finding,
  so do not serve the list to hide it.
- **Builder dialog.** If the dialog's `McpServerList` asks for more than the
  two mocked hooks plus `useMcpServers`, add the missing hook to the mocks. Do
  not stub the list itself: the assertion is on the button a person sees.
- **Builder (b)/(d).** If "Load its tools" is not shown, the binding dialog did
  not pick up `c1` as `listedBy`. Check `catalog_key`, the binding's
  `connection_id` and `last_tools: null`.
- **Only :723 lacks a direct assertion.** It is the `create-mcp` pick step, and
  the step-4 grep checks it.
