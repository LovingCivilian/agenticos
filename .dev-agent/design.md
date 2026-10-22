# Design: gate the console's MCP controls on `mcp:manage`

Base: `upstream/main` fb4ec90ac (unchanged at the time of writing). All line
numbers were checked on that tree. Paths under `frontend/src/` are shortened.

## Problem and root cause

The API gates organization MCP connections on `mcp:manage`. All 10 routes in
`backend/app/api/routes/v1/org_mcp_connections.py` (mounted at
`/mcp-connections`) carry `require(Perm.MCP_MANAGE)`: list :47, create :66,
OAuth start :79, GitHub OAuth :112, GitHub App :139, polled portal :163,
update :188, delete :205, tool calls :217, test (probe) :232. Commit b56ba1fe3
(2026-07-31, "Land the MVP platform work") changed them from
`CONNECTIONS_MANAGE` to `MCP_MANAGE`. It did not change the console. The docs
pages listed below also kept the old name.

`Perm.mcpManage` exists (`types/permissions.ts:62`), and newer code uses it
(`lib/dashboard/registry.ts:480`, `components/triggers/new-event-trigger-dialog.tsx:52`).
`can()` (`hooks/use-permissions.ts`) checks only that the permission is in the
set and ignores scope. The backend's `ctx.has()` does the same (any scope
except NONE). Builder holds `mcp:manage` at `SHARED`, so this is the right check.

Owner, admin and builder hold both permissions. Operator, member and viewer
hold neither. Custom roles are "Phase 2" (`docs/permissions.md:235`). So no
user sees the bug today.

## Code changes: 12 gates, `Perm.connectionsManage` → `Perm.mcpManage`

| File | Line | Control |
|---|---|---|
| `app/[locale]/(dashboard)/mcp-servers/page.tsx` | 91 | `canManageOrganization` on `McpServerList` ("Add a custom server", the organization scope in the connect dialog, writes on organization rows) |
| `app/[locale]/(dashboard)/agents/[id]/page.tsx` | 1130 | `canManageOrganization` on the `McpServerList` in the Builder's "Connect an MCP server" dialog |
| same | 1569 | Builder button "Connect a server" (`data-tour="agent-mcp-connect"`) |
| same | 1618 | probe passed to `toolsForBinding` (`POST /mcp-connections/{id}/test`) |
| `lib/onboarding/flows.ts` | 369, 378 | `flow-mcp-field-form`, `flow-mcp-field-connect` |
| same | 599, 608, 618 | `flow-agent-mcp-ask`, `flow-agent-mcp-connect`, `flow-mcp-field-pick` (Builder) |
| same | 716, 723 | `create-mcp` flow and its `flow-mcp-field-pick` |
| `lib/onboarding/tour.ts` | 644 | `mcp-add` stop (anchored on "Add a custom server") |

## Comments and test titles: permission name only

Every comment or test title in `frontend/src` that ties `connections:manage`
to an MCP connection gate or route (re-swept; nothing else found). In each
line, only `connections:manage` changes to `mcp:manage`.

```
app/[locale]/(dashboard)/agents/[id]/page.tsx:171   * `connections:manage`, which an agent author need not hold (#1341).
app/[locale]/(dashboard)/agents/[id]/page.tsx:350    // `connections:manage` (#1472).
lib/onboarding/flows.ts:571         // and on yes pointed at that button, gated on the `connections:manage` the
lib/onboarding/flows.ts:575         // `connections:manage`: every author is shown it, whatever the organization
components/mcp/mcp-server-list.tsx:93    /** False for a member without `connections:manage` - the organization column reads only. */
components/mcp/mcp-server-list.tsx:110   *   tools depending on whose session built it. Gated on `connections:manage`.
hooks/use-mcp-servers.ts:121        * without `connections:manage` gets a 403 there and can still read the
hooks/use-org-mcp-connections.ts:43  * `enabled: false` is not offered: a caller without `connections:manage` gets a
lib/org-mcp-connections-api.ts:8     * organization owns, gated on `connections:manage`, and the only ones an agent
lib/binding-tools.ts:18              * out to a third party and is gated on `connections:manage`, so `probe` is
hooks/use-org-mcp-connections.test.tsx:121   // A member without connections:manage gets a 403. An empty list would read
hooks/use-mcp-connections.test.tsx:82    * the organization's is not: a member without `connections:manage` gets a 403
hooks/use-mcp-connections.test.tsx:208       // The org list needs `connections:manage`; surfacing its 403 as the page's
lib/binding-tools.test.ts:59                 // `connections:manage` gates the probe; an agent author without it gets the
components/mcp/mcp-server-list.integration.test.tsx:424   it("lets a member without connections:manage read the organization column, not write it", async () => {
```

Plus `flows.test.ts:493–496` (see Tests). Left as they are, because they are
already right or are not about MCP:

- `new-event-trigger-dialog.tsx:37` and its test :57, :70, :72. They say that
  `connections:manage` is *not* the MCP gate.
- `portal-catalog.integration.test.tsx:539`. Its wording is generic, and the
  gate it tests is already `mcp:manage`.

Note: `mcp-server-list.tsx:93` and the title at :424 say that a member without
the permission can *read* the organization column. The list route has always
required the same permission as the writes, so in fact it 403s. That premise
was wrong before this change too. This change fixes only the name.

## Unchanged `connections:manage` gates (non-MCP, verified against routes)

- Model profiles (`model_providers.py:89`, `:112`): agents page :373, :1377,
  :1380; `flows.ts` :416, :454; `chat-model-picker.tsx:157`;
  `kb/ingestion-settings.tsx:650`.
- Org integrations (`org_integrations.py`, 6 routes): `tour.ts` :499, :510;
  `kb/reusable-integrations.tsx:69`.
- Local services (`local_services.py`): `tour.ts:522`; `kb/local-services.tsx:42`;
  `kb/inline-local-service.tsx:52`.
- Sandbox hosts (`sandbox_connections.py`): `sandboxes/page.tsx:46`.

No MCP gate is missing from the list. The `connections:view` gates are for
sandboxes only, and no message copy names either permission.

## Docs: token only, four pages, four languages

Each sentence is still true after the change. Builders hold `mcp:manage`, and
organization MCP connections need it.

`docs/mcp.md:155` reads: "Probing … is gated on `mcp:manage`; an agent author
holds `agents:edit` and needs the list to choose from, so the list is read
rather than fetched." Each clause is still true. The probe route requires
`mcp:manage`, and an author need not hold it. But the rationale it implies is
weaker than it reads: reading the list (`GET /mcp-connections`) needs
`mcp:manage` too, since b56ba1fe3. That was already so with the old name, so
it is not changed here. It is a note for the PR.

```diff
docs/howto/database-questions.md:23 / .pl.md:15 / .de.md:15 / .es.md:26
- - `connections:manage`, to register the organization connection.
+ - `mcp:manage`, to register the organization connection.
- - `connections:manage`, żeby zarejestrować połączenie organizacji.
+ - `mcp:manage`, żeby zarejestrować połączenie organizacji.
- - `connections:manage`, um die Organisationsverbindung zu registrieren.
+ - `mcp:manage`, um die Organisationsverbindung zu registrieren.
- - `connections:manage`, para registrar la conexión de organización.
+ - `mcp:manage`, para registrar la conexión de organización.

docs/howto/meeting-to-tasks.md:13 / .pl.md:14 / .de.md:14 / .es.md:14
- - **`connections:manage`** to add Linear or Jira as an organization-wide MCP connection, …
+ - **`mcp:manage`** to add Linear or Jira as an organization-wide MCP connection, …
- - **`connections:manage`**, żeby dodać Linear albo Jira jako połączenie MCP dla całej organizacji, …
+ - **`mcp:manage`**, żeby dodać Linear albo Jira jako połączenie MCP dla całej organizacji, …
- - **`connections:manage`**, um Linear oder Jira als organisationsweite MCP-Verbindung hinzuzufügen, …
+ - **`mcp:manage`**, um Linear oder Jira als organisationsweite MCP-Verbindung hinzuzufügen, …
- - **`connections:manage`** para añadir Linear o Jira como conexión MCP de toda la organización, …
+ - **`mcp:manage`** para añadir Linear o Jira como conexión MCP de toda la organización, …

docs/howto/notion-agent.md:13 and :20 / .pl/.de/.es.md:14 and :21
- - **`connections:manage`** to add Notion for the whole organization, …
+ - **`mcp:manage`** to add Notion for the whole organization, …
- … under **MCP servers → Organization** (`connections:manage`) makes one Notion connection …
+ … under **MCP servers → Organization** (`mcp:manage`) makes one Notion connection …
- - **`connections:manage`**, żeby dodać Notion dla całej organizacji, …
+ - **`mcp:manage`**, żeby dodać Notion dla całej organizacji, …
- … w **MCP servers → Organization** (`connections:manage`) sprawia, że …
+ … w **MCP servers → Organization** (`mcp:manage`) sprawia, że …
- - **`connections:manage`**, um Notion für die ganze Organisation hinzuzufügen, …
+ - **`mcp:manage`**, um Notion für die ganze Organisation hinzuzufügen, …
- … **MCP servers → Organization** hinzugefügt (`connections:manage`), antwortet …
+ … **MCP servers → Organization** hinzugefügt (`mcp:manage`), antwortet …
- - **`connections:manage`** para añadir Notion para toda la organización, …
+ - **`mcp:manage`** para añadir Notion para toda la organización, …
- … en **MCP servers → Organization** (`connections:manage`) hace que …
+ … en **MCP servers → Organization** (`mcp:manage`) hace que …

docs/mcp.md:155 / .pl.md:167 / .de.md:171 / .es.md:168
- `connections:manage`; an agent author holds `agents:edit` and needs the list to
+ `mcp:manage`; an agent author holds `agents:edit` and needs the list to
- bramkowane przez `connections:manage`; autor agenta ma `agents:edit` i potrzebuje
+ bramkowane przez `mcp:manage`; autor agenta ma `agents:edit` i potrzebuje
- zu einem Dritten und ist auf `connections:manage` beschränkt; der Autor eines
+ zu einem Dritten und ist auf `mcp:manage` beschränkt; der Autor eines
- protegido por `connections:manage`; el autor de un agent tiene `agents:edit` y
+ protegido por `mcp:manage`; el autor de un agent tiene `agents:edit` y
```

`docs/ROADMAP.md:126` stays (history).

Edit each English page first, which makes its translations stale. Then edit
the translations and restamp all 12. `--update` resolves paths from the
current directory, which is `backend/` here. The checker is clean at baseline.

```bash
uv run --directory backend python3 ../scripts/check_docs_i18n.py --update \
  ../docs/howto/database-questions.{pl,de,es}.md \
  ../docs/howto/meeting-to-tasks.{pl,de,es}.md \
  ../docs/howto/notion-agent.{pl,de,es}.md \
  ../docs/mcp.{pl,de,es}.md
```

## CHANGELOG.md

Add this under `## [Unreleased]`. Add the PR number `(#NNNN)` when the
upstream PR exists. The last sentence covers the docs. Upstream notes docs
that described the wrong thing (#498, "Four pages described the old split").
Here one sentence inside the same entry is enough. The button label is the
console's: "Connect a server".

```markdown
### Fixed

- **The console checks `mcp:manage` for the organization's MCP controls, as
  the API does.** The console checked `connections:manage` instead. This
  applied to the organization controls on the MCP servers page, to *Connect a
  server* and the tool check in the Builder, and to the onboarding steps for
  them. A role with only one of the two permissions saw a control that the API
  refused, or did not see a control that the API allowed. Each built-in role
  has both permissions or neither, so no built-in role changes. The MCP page
  and three how-to pages also named `connections:manage`, and now name
  `mcp:manage`.
```

## Tests

| Change | Test |
|---|---|
| `mcp-servers/page.tsx:91` | New `mcp-servers/page.integration.test.tsx` |
| agents page :1130, :1569, :1618 | New `agents/[id]/mcp-gate.integration.test.tsx` |
| `flows.ts` (7) | `flows.test.ts`: two tests change, one case added |
| `tour.ts:644` | `tour.test.ts`: one assertion added |
| comments, titles | Run the touched test files; no new assertions |
| docs | `check_docs_i18n.py`, `make docs-build` |
| E2E | None. Playwright runs as built-in roles, which hold both permissions, and permission gating is integration work (`.claude/rules/frontend.md`) |

Pages are outside the vitest coverage `include`, so the two page tests do not
change the gate.

### `app/[locale]/(dashboard)/mcp-servers/page.integration.test.tsx`

This test uses the real `usePermissions` over a mocked `/me/permissions`, as in
`runs/approvals-permission.integration.test.tsx`, plus the api-client mock and
MCP routing from `components/mcp/mcp-server-list.integration.test.tsx`. Mock
`sonner`. `vitest.setup.ts` already supplies `next-intl` (real `en.json`) and
`next/navigation`. Use a `QueryClient` (`retry: false`) that the test holds.

`serve(permissions)` answers GETs:

- `/me/permissions`: each permission at scope `"all"`.
- `/agents/mcp-catalog`: one GitHub entry.
- `/agents/mcp-servers?…`: `{ items, total, registry_total: 0 }`.
- `/mcp-connections`: `{ items: [], total: 0 }` when `mcp:manage` is held,
  otherwise reject with `new ApiError(403, "Insufficient permissions")`, as
  `require(Perm.MCP_MANAGE)` does.
- `/me/mcp-connections`: an empty list.
- Anything else: throw.

The organization control is the toolbar button "Add a custom server"
(`mcp-server-list.tsx:596`, `data-tour="mcp-add"`).

1. Only `mcp:manage`: `findByRole("button", { name: "Add a custom server" })` is visible.
2. Only `connections:manage`:
   - `await findByText("GitHub")`. The list rendered, so the org 403 did not
     replace the page.
   - Wait until `client.getQueryState(qk.organizations.permissions("current"))?.status === "success"`.
     `can()` is false while permissions load, so an absence before that proves nothing.
   - Then the button is `null`.

Both cases fail on the current code.

### `app/[locale]/(dashboard)/agents/[id]/mcp-gate.integration.test.tsx`

Make it a new file. The repo has one file per concern for this page
(`metadata-gate`, `model-panel`, `first-publish`, `rename`), and each has its
own harness. Base it on `metadata-gate.integration.test.tsx`, not
`model-panel`. It already mocks `useOrgMcpConnections` with `test`,
`usePlatformMcpTools` and `useMcpCatalog`. It drives the tab through
`useSearchParams` (`?tab=mcp`) and wraps in a `QueryClientProvider` and
`Suspense`. Its `usePermissions` mock is synchronous: `can` reads a set and
`isLoaded` is `true`. So there is no loading window and no false pass from it.

Changes to that harness:

- `allowed`: the base `{ agents:edit }` (the picker is `disabled` without it)
  plus the permission under test.
- `useSearchParams`: `tab=mcp`.
- Draft spec: `mcp_servers: [{ account: "organization", connection_id: "c1", allowed_tools: null, approval: "none" }]`.
- `@/hooks`:
  - `useMcpCatalog` returns one GitHub entry (`key: "github"`).
  - `useOrgMcpConnections` returns `{ connections: [C1], test: probe, isLoading: false, error: null }`.
    `C1` is `catalog_key: "github"`, `last_tools: null` (never probed).
  - `useMcpConnections` returns `{ connections: [], remove: vi.fn() }`.
  - Add `useMcpServers` returning `{ organization: { connections: [] }, personal: { connections: [] }, recordTools: vi.fn() }`
    for the dialog's `McpServerList`.
- `vi.mock("@/hooks/use-mcp-servers")`: spread the actual module, and replace
  `useMcpCatalog` with `{ servers: [] }` and `useMcpCatalogPage` with
  `{ servers: [], total: 0, registryTotal: 0, isLoading: false, isFetching: false }`.
  `McpServerList` imports these directly. Its toolbar renders with an empty list.
- `probe`: `vi.fn().mockResolvedValue({ ok: true, error: null, tools: [{ name: "create_issue", description: "" }] })`,
  set in `beforeEach`.
- Keep the metadata-gate stubs (PublishState, StaleReferences, the model
  pickers, VoiceCard, TestPanel).

How each gate is observed:

- **:1569.** The button "Connect a server" (unique in `en.json`). In the
  negative case, first wait for the MCP card's settings control:
  `findByRole("button", { name: "Settings for GitHub in this agent" })`.
  Then assert the button is absent.
- **:1130.** Click "Connect a server". Then, inside
  `findByRole("dialog", { name: "Connect an MCP server" })`, the button "Add a
  custom server" is present. The negative case cannot be reached: the dialog
  opens only from the :1569 button, which a caller without `mcp:manage` does
  not get. The honest negative is that button's absence. A page with :1569
  fixed and :1130 not fixed fails the positive case.
- **:1618.** Click "Settings for GitHub in this agent", then "Load its tools".
  With the probe: `probe` is called with `"c1"`, and
  `findByRole("checkbox", { name: "create_issue" })` shows the probed tool in
  the picker. Without it: `findByText(/Nothing has checked this connection yet/)`
  (the picker opened with no catalogue, so the handler ran), `probe` was not
  called, and there is no `create_issue` checkbox. `probe` is the mocked hook's
  `test`, which is the page's only way to send `POST /mcp-connections/{id}/test`.
  So the assertion is on the request, through the boundary this harness mocks.
  Caveat: in production, a caller without `mcp:manage` gets a 403 on the
  connection list itself, so this gate is defence in depth. The mock serves the
  list to both callers, so the gate is the only difference between them.

Cases (`describe` per permission set):

- `{agents:edit, mcp:manage}`:
  - (a) offers "Connect a server", and the catalog it opens can add an organization server;
  - (b) checks a never-probed connection before listing its tools.
- `{agents:edit, connections:manage}`:
  - (c) no "Connect a server";
  - (d) lists tools without probing.

All four fail on the current code:

- (a) and (c): the button follows `connections:manage`.
- (b): the probe is `null`.
- (d): the probe is called.

### Onboarding tests

`lib/onboarding/flows.test.ts` fails after the change. Update it:

- :195: `toBe(Perm.connectionsManage)` → `toBe(Perm.mcpManage)`.
- :493–502 "hides the MCP fork from a caller who cannot manage connections":
  - Change the title to "…who cannot manage MCP connections", the comment to
    "No `mcp:manage` …", and the predicate to `p !== Perm.mcpManage`.
  - Add the reverse case. With `p !== Perm.connectionsManage`, `EMPTY` state
    and choices `{ "flow-agent-mcp-ask": "yes" }`, the steps include
    `flow-agent-mcp-ask`, `flow-agent-mcp-connect`, `flow-mcp-field-pick`,
    `flow-mcp-field-form` and `flow-mcp-field-connect`.
  - This covers :369, :378, :599, :608 and :618. `stepsForFlow` filters each
    step on its own permission.
- :347–371 are about model steps. They stay.

`lib/onboarding/tour.test.ts`: in `describe("MCP catalog stops")`, assert
`byId.get("mcp-add")?.permission` is `Perm.mcpManage`.

The other onboarding tests use `can = () => true/false` or a rule about models
only, so they are not affected. Baseline: `flows.test.ts` and `tour.test.ts`
pass (76 tests).

## Out of scope

- Removing the gate from controls that also start a personal connection
  (`/me/mcp-connections` needs no permission): `create-mcp`, `mcpDialogSteps`,
  `flow-mcp-field-pick`. This is a PR follow-up.
- Any backend change.
- Observed, not changed: the Builder's binding dialog
  (`components/agents/mcp-binding-dialog.tsx:325–339`, through
  `McpServerPicker onConnect` → `useConnectEntry("organization")`) offers an
  organization connect with no permission check, only `agents:edit`. This is a
  PR follow-up.
- The "reads only" premise at `mcp-server-list.tsx:93` / test :424, and the
  rationale in `mcp.md:155`: names fixed, wording kept.

Suggested commit subject: `fix(mcp): gate the console's MCP controls on mcp:manage`.
