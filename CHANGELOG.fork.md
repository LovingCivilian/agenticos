# Fork changelog

Changes this fork, `LovingCivilian/agenticos`, carries on top of upstream
[AgenticOS](https://github.com/vstorm-co/agenticos). Upstream's own changes are
in [`CHANGELOG.md`](CHANGELOG.md), which this fork keeps identical to upstream's
so a sync never conflicts there. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), like upstream's.

A version is `<upstream version>-<revision>`: the upstream release the fork is
built on, then this fork's revision on top of it, restarting at 1 whenever the
upstream base changes. `1.5.0-2` reads "upstream 1.5.0, fork revision 2". This
is the convention Debian packages follow (`deb-version(7)`), and so do forks
such as LibreWolf and ungoogled-chromium.

- Each release is `## [<version>] - YYYY-MM-DD`, dated the day it is cut, and
  opens with the upstream release it is built on. `[Unreleased]` has no date
  until then.
- A release is tagged `fork/<version>`, never `v*`, which is upstream's.
- `backend/pyproject.toml`, `backend/uv.lock` and `frontend/package.json` keep
  upstream's version, so a fork build reports the upstream release it is built
  on.

## [Unreleased]

### Added

- **`MCP_ALLOWED_HOSTS`, for an MCP server on the deployment's own network**
  (#3). A connection to a host that resolves to a private, loopback or other
  internal address - a compose service such as `postgres-mcp` - was refused with
  no way past. An operator can now list such hosts, as a JSON list. Matching is
  exact and case-insensitive, with no wildcards; a listed host skips the
  private-address check and is not resolved, but still needs `http` or `https`.
  It is for bearer-token and unauthenticated servers: OAuth sign-in, its
  discovery and its consent URL are checked without the list, as before. An
  entry opens every port on that host to every account on the deployment, so
  name only a server that checks a token for each caller, or whose tools are
  safe for anyone who can sign in, and never a loopback or one of the stack's
  own services. Empty, the default, changes nothing.
