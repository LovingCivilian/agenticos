# This fork, specifically

The facts the fork-sync steps depend on. When one stops being true - a remote
renamed, a workflow re-enabled, the `gh` token gaining the `workflow` scope -
update it here.

## Remotes and branches

| | |
|---|---|
| `origin` | the fork, `LovingCivilian/agenticos`. Default branch `develop`; issues enabled. |
| `upstream` | `vstorm-co/agenticos`. Default branch `main`. |
| `main` | mirror of `upstream/main`, fast-forward only |
| `develop` | the fork's own work; base of every fork PR |

The script's defaults (`--fork origin --upstream upstream --mirror main --work
develop`) match this.

## Pushing

`origin` is an HTTPS remote. The `gh` login has the `workflow` and
`write:packages` scopes, and `gh auth setup-git` made it git's credential
helper for github.com, so a plain push works for branches, tags and workflow
changes alike:

- **A branch:** `git push origin <branch>`
- **Upstream's release tags**, by refspec, so no local scratch tag rides along:
  `git push origin 'refs/tags/v*:refs/tags/v*'`
- **A fork release tag**, by name: `git push origin fork/<version>`
- **The mirror:** `gh repo sync LovingCivilian/agenticos --branch main` asks
  GitHub to fast-forward it, and refuses if `main` has diverged. If it refuses,
  push the fast-forward instead: `git push origin upstream/main:refs/heads/main`.
  Git rejects that push unless it is a fast-forward, which is the guarantee
  wanted.

If a push of a tag or a workflow file fails with "refusing to allow an OAuth App
to create or update workflow ... without `workflow` scope", the login lost that
scope (`gh auth status` lists them). Restore it with `gh auth refresh -h
github.com -s workflow,write:packages`, which the user runs because it opens a
browser, or push over SSH meanwhile: `git push
git@github.com:LovingCivilian/agenticos.git <ref>`. Old upstream tags point at
commits carrying earlier workflow files, so tags need that scope too.

## Merging PRs

The fork allows merge commits, squash and rebase. Feature PRs into `develop` are
squashed, as upstream's are. **A sync PR is merged with "Create a merge commit"**
(`gh pr merge <n> --merge`), never squashed or rebased: squashing drops
upstream's commits from `develop`'s history, so the next sync replays every
upstream change since the old merge base and conflicts on all of them.

## Workflows on the fork

Disabled on the fork, because they act on upstream's infrastructure or would
publish from the fork: `images.yml` (pushes to `ghcr.io/vstorm-co/...`),
`deploy.yml` (needs `DEPLOY_SSH_KEY`), `deploy-queue.yml`, `docs.yml` (GitHub
Pages), `dependency-freshness.yml`. Kept: `ci.yml`, `codeql.yml`, and
`ai-review.yml`, which runs on manual dispatch only until upstream restores its
`pull_request` trigger (its label gate returns with it). Fork-only:
`fork-images.yml`, which publishes the fork's images (see below). Check the live state with
`gh api repos/LovingCivilian/agenticos/actions/workflows -q '.workflows[] | "\(.state) \(.path)"'`.
The report shows it as `fork_workflows`.

**The Actions quirk:** for its first runs the fork needed the Actions tab's "I
understand my workflows, go ahead and enable them" banner clicked, and then a
new push. Neither reopening a PR nor an empty commit started anything before
that. If CI shows no runs at all (`gh api repos/<fork>/actions/runs -q
.total_count`), check that banner and the CI workflow's own enable switch
before suspecting the code.

## Deploying the fork

Upstream's CI publishes images of upstream's code only, and `images.yml` is
disabled here. So the fork publishes its own, and a fork server runs them the
way upstream's manual deploy runs upstream's:

- **Build:** pushing a `fork/<version>` tag runs `fork-images.yml`. It
  publishes `ghcr.io/lovingcivilian/agenticos-{backend,frontend}` as
  `<version>`, `sha-<short>` and `latest`, for `linux/amd64`. A manual run
  (`gh workflow run fork-images.yml --ref <branch>`) publishes `sha-<short>`
  only.
- **Image names:** `docker-compose-prod.fork.yml` and
  `docker-compose-prod.frontend.fork.yml` replace upstream's image names with
  these. `GNUmakefile` includes upstream's `Makefile` and adds the overlays to
  `make prod`, `make prod-frontend`, `make prod-down` and `make prod-logs`.
- **Deploy, on the server:** check out the release tag, set
  `AGENTICOS_VERSION=<version>` in `backend/.env`, then `make prod` and
  `make prod-frontend`. The checkout's `origin` is this fork.
- **Never on a fork server:** `scripts/deploy.sh`, which waits for and pulls
  `ghcr.io/vstorm-co` images, and `make -f Makefile prod`, which skips
  `GNUmakefile`.
- **Not built here:** `sandboxd` stays upstream's published image.

## Checks to run after a merge

Never `make check`, `make test` or a whole-directory pytest on this machine
(`AGENTS.md`). Instead:

| What changed | Run |
|---|---|
| any backend code | `make lint-backend` (ruff, format, ty, vulture, deptry, guard scripts, the translation gate) |
| specific areas | `cd backend && uv run pytest <test files for those modules> -q` |
| migrations | `cd backend && uv run pytest tests/test_migration_chain.py -q`, and `make db-check` with a database |
| `docs/` | `python3 scripts/check_docs_i18n.py`, `python3 scripts/check_docs_paragraphs.py`, `make docs-build` |
| `docs/mcp.md` and the other guide pages | `python3 scripts/gen_architect_guide.py --check` (re-run it without `--check` to rewrite the copies) |
| `docs/assets/amigo.svg` | `python3 scripts/gen_amigo_assets.py --check` |
| frontend | `cd frontend && bunx vitest run <files>`, `make lint-frontend` |
| `docker-compose-prod*.yml`, `Makefile` | `cd backend && uv run pytest tests/test_fork_deploy.py -q`. A new service on upstream's backend or frontend image needs an entry in its `*.fork.yml` overlay, and a renamed `PROD_FILES` or `PROD_FRONTEND_FILES` needs `GNUmakefile` updated |
| `images.yml` | compare its build contexts and Dockerfiles with `fork-images.yml` |

CI on the pushed commit is what proves the rest. Read it with `gh run list
--repo LovingCivilian/agenticos --branch <branch>`, and use `gh run view <id>
--log-failed`, or `gh run view --job <id> --log` while the run is still going.

## Generated copies

- `scripts/gen_architect_guide.py` copies some `docs/` pages, `docs/mcp.md`
  among them, into
  `backend/app/core/catalog/skill_gallery/software/agenticos-guide/docs/`.
  `backend/tests/test_architect_guide.py` fails when a copy is stale. Editing
  `docs/mcp.md` on the fork means re-running it. The first fork CI failure was
  exactly this.
- `scripts/gen_amigo_assets.py` writes `docs/assets/amigo-walk.svg` from
  `docs/assets/amigo.svg`. It is checked by `backend/tests/test_amigo_assets.py`.

## Translations

Every published page under `docs/` has `.pl.md`, `.de.md` and `.es.md` siblings,
and so do `README.md`, `CONTRIBUTING.md`, `SECURITY.md` and `CODE_OF_CONDUCT.md`.
Each sibling records the English revision it translates in a `source_sha` stamp
- a front-matter line on site pages, an HTML comment (`<!-- source_sha: ... -->`)
on the four repository files - and `scripts/check_docs_i18n.py` fails `make
lint` on a stale one. `--update <file>...` restamps the named files, and only those: stamping a
file nobody retranslated hides a stale translation. `--anchors <page>` checks
that headings pin the same anchors in every language. `docs/howto/translate.md`
fixes the terminology. `CHANGELOG.md`, `CHANGELOG.fork.md`, `docs/design/` and
`docs/plans/` are not translated.

## Migrations

`backend/alembic/versions/NNNN_<name>.py`. A migration's `revision` is its file
stem (`"0138_visuals"`), and `down_revision` names its parent.
`backend/tests/test_migration_chain.py` allows exactly one head, no shared
parent, and no revision id longer than 32 characters (`alembic_version` is
`VARCHAR(32)`), so branches are never merged with an alembic merge revision. The later
migration is re-chained instead (scenario 4). The `alembic-migration` skill has
the rest.

## Lockfiles and versions

- `backend/uv.lock`: `cd backend && uv lock`, then `uv sync`.
- `frontend/bun.lock`: `cd frontend && bun install`.
- Version numbers in `backend/pyproject.toml`, `backend/uv.lock`'s
  `agenticos` entry and `frontend/package.json` are upstream's. Upstream bumps
  them in its `chore: cut X.Y.Z` commits. The fork never changes them.

## `CHANGELOG.fork.md`

The fork's own changes, in Keep a Changelog form. Upstream's `CHANGELOG.md` stays
identical to upstream's. If the file does not exist yet, create it with this
header:

```markdown
# Fork changelog

This file records the changes that this fork, `LovingCivilian/agenticos`, adds
to upstream [AgenticOS](https://github.com/vstorm-co/agenticos). Upstream
records its own changes in [`CHANGELOG.md`](CHANGELOG.md). This fork keeps that
file identical to the upstream file, so a sync never has a conflict there. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the
same as upstream.

A version has the form `<upstream version>-<revision>`. The first part is the
upstream release that the fork builds on. The second part is the fork revision
on that release. The revision starts again at 1 when the upstream base changes.
For example, `1.5.0-2` means "upstream 1.5.0, fork revision 2". Debian packages
use this convention (`deb-version(7)`), and so do forks such as LibreWolf and
ungoogled-chromium.

- Each release has the heading `## [<version>] - YYYY-MM-DD`, with the date of
  the cut. Its first line names the upstream release that it builds on.
  `[Unreleased]` has no date until the cut.
- The tag of a release is `fork/<version>`, never `v*`. The `v*` tags belong to
  upstream.
- `backend/pyproject.toml`, `backend/uv.lock` and `frontend/package.json` keep
  the upstream version. Thus a fork build reports the upstream release that it
  builds on.

## [Unreleased]
```

Entries use upstream's style: a bold lead sentence, the fork issue or PR number
after it (`(#3)`), then two to six lines on what a member or operator can now
do. Categories: `### Added`, `### Changed`, `### Fixed`, `### Removed`,
`### Security`.

**Cutting a fork release:**

1. The version is the upstream base `develop` sits on, plus the next revision,
   or `-1` on a new base. The report gives both candidates:
   `fork_changelog.next_version_now` (as `develop` stands) and
   `next_version_after_sync` (once `develop` holds upstream's newest release).
2. On a branch from `develop` (`chore/cut-fork-<version>`), rename `## [Unreleased]`
   to `## [<version>] - <today>`, and add `Based on upstream [<base>](CHANGELOG.md).`
   as its first line. Put a fresh, empty `## [Unreleased]` above it.
3. Open a PR into `develop`, and ask the user to merge it.
4. Tag the merge commit, after asking:
   `git tag fork/<version> <merge-sha>`, then `git push origin fork/<version>`. The push starts
   `fork-images.yml`, which publishes that version's images; watch it with
   `gh run list --repo LovingCivilian/agenticos --workflow fork-images.yml`.
5. Optionally, after asking:
   `gh release create fork/<version> --repo LovingCivilian/agenticos --title "<version>" --notes-file <that section>`.

## Labels

`gh label clone vstorm-co/agenticos --repo LovingCivilian/agenticos` adds every
label upstream has, and deletes none. The fork's one extra label is
`accessibility`, a GitHub default.
