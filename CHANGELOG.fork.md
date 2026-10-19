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

### Added

- **Deploy the fork from source on its own server** (#8). Upstream publishes
  images of its own code only. Now `scripts/fork-deploy.sh` builds the backend
  image and the frontend image from a commit or a tag on the server, and then
  deploys them. Two overlays, `docker-compose-prod.fork.yml` and
  `docker-compose-prod.frontend.fork.yml`, run these local images instead of
  the upstream images. The script builds before it stops anything, so a failed
  build leaves the running stack as it was. It keeps the last two builds for a
  rollback. Do not use `make prod` or `scripts/deploy.sh` on a fork server.
  They start the upstream images, without the fork's changes.

## [1.6.1-1] - 2026-10-19

Based on upstream [1.6.1](CHANGELOG.md).

### Added

- **`MCP_ALLOWED_HOSTS`, for an MCP server on the deployment's own network**
  (#3). Before this change, the SSRF guard refused every host with a private,
  loopback or other internal address, for example the compose service
  `postgres-mcp`. There was no way past this refusal. Now an operator can list
  such hosts in a JSON list. The match is exact and case-insensitive, with no
  wildcards. A listed host skips the private-address check, and the guard does
  not resolve it. The host still needs `http` or `https`. The list is for
  servers that use a bearer token or no authentication. OAuth sign-in, its
  discovery and its consent URL do not use the list, as before. An entry opens
  every port on its host to every account on the deployment. Thus list only a
  server that checks a token for each caller. Or list a server whose tools are
  safe for all users who can sign in. Never list a loopback address or a
  service of the stack itself. The default is an empty list, and an empty list
  changes nothing.
