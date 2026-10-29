# Implementation plan: an SFTP sync connector (#29)

Source: `design.md` and `requirements.md`. No test exists yet. Each step names the tests
that pin it, by their number in `design.md` ("The tests"). Write the steps in this
order. Each step depends only on the steps above it. After each step, run its test
files from `backend/` (`uv run pytest <files> -q`). Keep the files of
`tests/integration/` together in one run, or run them alone.

The maintainer decided both questions: asyncssh with licence script change A1, and an
operator allowlist, `SYNC_ALLOWED_HOSTS`, read by the SFTP connector only (`design.md`,
the two "Decision" sections).

## 0. Dependency

1. `cd backend && uv add 'asyncssh[bcrypt]>=2.24.1'`. Check that `uv.lock` locks 2.24.1
   or later, and that it adds no distribution other than `asyncssh`
   (`cryptography`, `typing-extensions` and `bcrypt` are locked already).
2. In `backend/pyproject.toml`, put the comment above the line: the SFTP connector's
   client, and why the floor is 2.24.1 (GHSA-q8m6-9rfx-39rf, GHSA-rw4j-r22c-9gc3,
   not yet in advisory databases on 2026-10-29), and why the `bcrypt` extra is named
   although `bcrypt` is a direct dependency.
3. `backend/tests/test_import_cost.py`: `DEFERRED["asyncssh"] = "an SFTP source, and
   checking an SSH key as it is stored"`.

- Check: `uv run python -c "import asyncssh; print(asyncssh.__version__)"`.
- Tests: `tests/test_import_cost.py` (green now, and it must stay green at every later
  step).

## 1. `backend/app/core/secret_kinds.py`

Write in this order:

1. `_BOUND_HOST_PATTERN = r"^([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?$"`,
   moved out of `GitTokenSecret.host`.
2. `_bound_host_allows(bound: str, hostname: str, port: int | None, *, default_port:
   int) -> bool`. Contract: `hostname` as `urlsplit` spells it (lower case, IPv6
   without brackets). Bracket an IPv6 `hostname`. The expected string is the host
   alone when `port` is `None` or `default_port`, else `host:port`. Compare with
   `bound.lower()` with `:<default_port>` removed. This is the body of
   `GitTokenSecret.allows` (`secret_kinds.py:304-315`) with 443 made a parameter.
3. `GitTokenSecret.host` uses `_BOUND_HOST_PATTERN`. `GitTokenSecret.allows` returns
   `_bound_host_allows(self.host, hostname, port, default_port=443)`. No behaviour
   change.
4. `SecretKind.SSH_CREDENTIALS = "ssh_credentials"`, after `GIT_TOKEN`. One entry in
   the module docstring.
5. `_readable_private_key(key: str, passphrase: str | None) -> None`. Imports
   `asyncssh` inside the function. Calls `asyncssh.import_private_key(key,
   passphrase)`. On `asyncssh.KeyImportError` or `asyncssh.KeyEncryptionError`, raise
   `ValueError` with one of the two sentences in `design.md`, `from None`. Nothing of
   the key or of asyncssh's message goes into the sentence.
6. `SshCredentialsSecret(_SecretBase)`. The fields, in form order:
   - `kind: Literal[SecretKind.SSH_CREDENTIALS]`.
   - `username: str = Field(min_length=1, max_length=255, pattern=r"^[^\s\x00-\x1f\x7f]+$",
     title="User name")`.
   - `host: str = Field(min_length=1, max_length=253, pattern=_BOUND_HOST_PATTERN,
     title="Host", description="Where this login may be used, e.g. sftp.example.com or
     sftp.example.com:2222")`.
   - `private_key: CredentialStr | None = Field(default=None, title="Private key",
     description=..., json_schema_extra={"x-textarea": True})`. Copy the comment of
     `GithubAppSecret.private_key` (`secret_kinds.py:425-431`) about line breaks.
   - `passphrase: SealedStr | None = Field(default=None, min_length=1, title="Key
     passphrase")`.
   - `password: CredentialStr | None = Field(default=None, title="Password")`.

   Then the members:
   - The `hint` property: `self.username[-4:]`.
   - `allows(self, hostname: str, port: int) -> bool`:
     `_bound_host_allows(self.host, hostname, port, default_port=22)`.
   - `@model_validator(mode="after")`: exactly one of `private_key` and `password`,
     else `ValueError("An SSH login needs a private key or a password, and not
     both.")`. `passphrase` without `private_key`: `ValueError("A passphrase goes with a
     private key.")`. Then `_readable_private_key(...)` when there is a key.
   - The class docstring says why it binds a host (upstream `#987`) and why the hint is the user
     name. It also says why `passphrase` is a `SealedStr`, and that the key check also
     runs at unseal.
7. Add `SshCredentialsSecret` to `StorableSecret`, `TypedSecret` and `SecretValue`, after
   `GitTokenSecret`. `_KIND_MODELS[SecretKind.SSH_CREDENTIALS]`.
   `_KIND_LABELS[SecretKind.SSH_CREDENTIALS] = ("SSH login", "A user name with a private
   key or a password, for an SFTP server, and the one host it may be used with.")`.

- Depends on step 0.
- Tests: `tests/test_secrets.py` (22-30), `tests/test_git_connector.py` (unchanged,
  green), `tests/api/test_secret_routes.py` (add `"ssh_credentials"` to the set at
  lines 115-126).
- Test fixtures: generate keys with `asyncssh.generate_private_key("ssh-ed25519")`,
  and export them as OpenSSH (encrypted needs bcrypt), PKCS#8 PEM and PKCS#1 PEM
  (asyncssh §3, §7). Never commit a key file.

## 2. `backend/app/services/rag/connectors/__init__.py` and `sharepoint.py`

1. `__init__.py`, after `ConfigRefusal`: `DEFAULT_EXTENSIONS = (".pdf", ".docx", ".md",
   ".txt")`, `_EXTENSION = re.compile(r"^\.[a-z0-9]{1,10}$")`, and
   `normalized_extensions(value: list[str]) -> list[str]`, the loop of
   `SharePointConfig._extensions` (`sharepoint.py:198-213`) unchanged, with a
   docstring: lower case, a leading dot added, duplicates dropped, at least one, and
   which connectors use it.
2. `sharepoint.py`: remove `DEFAULT_EXTENSIONS` and `_EXTENSION`, import them from the
   package, and make `_extensions` return `normalized_extensions(value)`.

- Tests: `tests/test_sharepoint_connector.py` (unchanged, green). The 100% gate on
  `sharepoint.py` stays met: the validator is still called.

## 3. The allowlist: `backend/app/core/config.py` and the report

1. `config.py`, above `class Settings`: `_refuse_unlistable(setting: str, entries:
   list[str], consequence: str) -> list[str]`. Move the loop of
   `validate_mcp_allowed_hosts` (`config.py:888-912`) into it unchanged. The only
   differences: the setting name and the second sentence of the message are
   parameters. Return `entries` unchanged.
2. `validate_mcp_allowed_hosts` returns `_refuse_unlistable("MCP_ALLOWED_HOSTS", v, "A
   listed host skips the SSRF check, so remove it.")`. Its docstring stays.
3. `SYNC_ALLOWED_HOSTS: list[str] = []` after `MCP_ALLOWED_HOSTS`, with the comment in
   `design.md` (`config.py` under "Modules and changes").
4. `validate_sync_allowed_hosts` (`@field_validator("SYNC_ALLOWED_HOSTS")`, a
   classmethod) returns `_refuse_unlistable("SYNC_ALLOWED_HOSTS", v, "A listed host may
   resolve inside the network, so remove it.")`. Its docstring says why loopback and
   metadata stay refused, and points at `_is_never_reachable`, which refuses them again
   when a listed name resolves to one.
5. `app/commands/data_protection.py`, `_settings_section`: `["SYNC_ALLOWED_HOSTS", ",
   ".join(settings.SYNC_ALLOWED_HOSTS) or "none"]` after the `MCP_ALLOWED_HOSTS` row
   (line 246).

- Depends on nothing above.
- Tests: `tests/test_config.py` (59-63, and `TestMcpAllowedHosts` unchanged and green),
  `tests/test_data_protection_report.py` (69), `tests/test_mcp_allowed_hosts.py` and
  `tests/test_ssrf.py` (unchanged, green).

## 4. `backend/app/services/rag/connectors/sftp.py`

The module docstring first: the credential and its host binding, the address, the
revision and the edit it misses (`full` is the remedy), the hardening, the bounds, the
residual race, the `getpass` note. Then, in this order:

1. Imports. Standard library: `asyncio`, `contextlib`, `ipaddress`, `logging`,
   `posixpath`, `re`, `socket`, `time`, `collections.deque`, `collections.abc`
   (`AsyncIterator`, `Awaitable`), `dataclasses`, `datetime` (`UTC`, `datetime`),
   `pathlib` (`Path`, `PurePosixPath`), `typing` (`TYPE_CHECKING`, `ClassVar`),
   `urllib.parse.quote`. Third party: `pydantic`. Local: `settings`,
   `AppException`, `BadRequestError`, `ExternalServiceError`, `UrlRefusedError`,
   `resolve_pinned_url`, `SecretKind`, `SshCredentialsSecret`, `StorableSecret`, and
   from the package `BaseSyncConnector`, `ChangedSinceListing`, `ConfigRefusal`,
   `ConnectorConfig`, `DEFAULT_EXTENSIONS`, `normalized_extensions`, `RemoteFile`,
   `RemoteListing`. Under `TYPE_CHECKING`: `SFTPAttrs`, `SFTPClient`,
   `SFTPClientFile`, `SSHClientConnection`, `SSHClientConnectionOptions`, `SSHKey`
   from asyncssh. asyncssh itself only inside functions.
2. The constants of `design.md` ("Constants"), each with a one-line reason.
3. `_spelled_host(value: str) -> str`: strip, lower case, remove one pair of brackets.
   `ipaddress.ip_address` succeeds: return its compressed form (refuse a `%` zone
   first). Else: refuse any character outside `[a-z0-9.-]` and its Unicode letters,
   `encode("idna").decode("ascii")` (`UnicodeError` refused), each label 1-63
   characters and not starting or ending with `-`, total 1-253. A `:` with digits
   after it gets "Put the port in the Port field." Every refusal is a `ValueError` with
   our sentence.
4. `_listed_hosts() -> frozenset[str]`: for each entry of `settings.SYNC_ALLOWED_HOSTS`,
   read at call time, `_spelled_host(entry)`. A blank entry, or one it refuses, is left
   out. The docstring says why the spelling is the source host's: `resolve_pinned_url`
   compares `urlparse(url).hostname` with the set exactly (`sanitize.py:198-199`).
5. `SftpConfig(BaseModel)` with its fields, then:
   1. `_host` (`field_validator("host")`): returns `_spelled_host(value)`.
   2. `_root_path` (`field_validator("root_path")`): strip. Must start with `/`. No
      character below `\x20`, no `\x7f`. `PurePosixPath(value)`, no `..` part. Max
      1024. Return `str(path)`.
   3. `_host_key` (`field_validator("host_key")`): strip. One line. Match
      `^(?P<type>[a-z0-9-]+(@[a-z0-9.-]+)?) (?P<blob>[A-Za-z0-9+/]+={0,2})(?: .*)?$`.
      No match and three fields with the second a key type: "Paste the key without the
      host name that ssh-keyscan prints first." No match otherwise: "A host key is one
      line such as ssh-ed25519 AAAA...". Type not in `_HOST_KEY_TYPES`: say which types
      are accepted. Then `asyncssh.import_public_key(f"{type} {blob}")` (imported in
      the validator). `KeyImportError`: refused. `key.algorithm.decode("ascii") !=
      type`: refused. Return `f"{type} {blob}"`.
   4. `_extensions`: `normalized_extensions(value)`.
   5. `url() -> str` and `authority(username: str) -> str`, as in `design.md`.
6. `_Stat` frozen dataclass, `of(attrs)` and `revision`.
7. `_remote_file(path: str, name: str, stat: _Stat, authority: str, cap: int) ->
   RemoteFile`. Revision `None` when `stat.size` is over `cap`.
8. `_Walk`, a small mutable dataclass for one listing: `deadline`, `folders`,
   `names`, `files: list[RemoteFile]`, `problems: list[str]`, `complete: bool`,
   `stopped: bool`. A method `stop(sentence)` sets `complete=False`, `stopped=True`
   and appends the sentence once.
9. `SftpConnector`:
   1. Class variables, `__init__` (`_conn`, `_sftp`, `_refusal`, `_host` set to `None`
      or `""`).
   2. `validate_config` (contract in `design.md`), with
      `trusted_hosts=_listed_hosts()` on `resolve_pinned_url`.
   3. `_login(credential, parsed) -> SshCredentialsSecret` (static). Three refusals,
      in this order: none, wrong kind, host not allowed. The third names
      `credential.host` and `parsed.host:parsed.port`. Nothing is resolved before it.
   4. `_options(login, host_key) -> SSHClientConnectionOptions` (static). One literal
      call with every keyword of the hardening table in `design.md`. Key mode and
      password mode differ only in `client_keys`, `ignore_encrypted`, `password`,
      `public_key_auth`, `password_auth` and `preferred_auth`, written as conditional
      values in the one call. `import_public_key(host_key)` and
      `import_private_key(key, passphrase)` here.
   5. `_refused(exc, parsed, login) -> AppException` (static). The table in
      `design.md` ("Errors"). Logs `exc` at warning with the host and the class name.
      Never returns `exc`'s text.
   6. `_drop() -> None`: `abort()` the connection if any, and set `_conn` and `_sftp`
      to `None`.
   7. `_call[T](awaitable: Awaitable[T]) -> T`: the four steps in `design.md`
      ("Bounds and timeouts"). Abort before awaiting the cancelled task. Map
      `asyncssh.ConnectionLost`, `asyncssh.SFTPConnectionLost` and
      `asyncssh.DisconnectError` to a dropped connection and `ExternalServiceError`.
      Let other `SFTPError`s through unchanged, for the caller to map.
   8. `_connect(parsed, login) -> SSHClientConnection`. Resolve in a thread, with
      `trusted_hosts=_listed_hosts()`. Never `validate_webhook_url`, which skips the
      address check for a listed host. A
      `UrlRefusedError` becomes `BadRequestError(message=str(exc))`, as Git does
      (`git.py:581-585`). For each IP: `asyncssh.connect(ip, pinned.port,
      flags=socket.AI_NUMERICHOST, config=None, options=self._options(...))`.
      `OSError` and `TimeoutError`: log, try the next IP. `asyncssh.Error`: raise
      `_refused(...)` at once. After the last IP: the "could not be reached" or "did not
      answer" sentence, by the last failure.
   9. `_client(parsed, login) -> SFTPClient`. `_refusal` set: raise it. `_conn` open:
      return `_sftp`. Else `_connect`, then `_call(conn.start_sftp_client(
      path_encoding=None, sftp_version=3))`, with `ChannelOpenError` mapped. Any
      `AppException` on the way: set `_refusal`, abort a half-open connection, raise.
   10. `aclose()`.
   11. `_open_folder(sftp, path: bytes)` and `_next_entry(...)`: the folder iterator,
       advanced only through `_call`. The `await anext(iterator, None)` runs in a small
       coroutine so that `_call` gets a plain awaitable. Close it with
       `_call(iterator.aclose())` in a `finally`.
   12. `_read_folder(sftp, path: str, depth: int, walk: _Walk, parsed, login, queue)`.
       One folder. Each entry by the rules of `design.md` ("Listing"), the bounds
       checked before each step, the per-folder counts turned into problems at the end.
   13. `_walk(sftp, parsed, login) -> RemoteListing`. The root first, with its refusals
       (raise). Then the queue, breadth first. A subfolder's `SFTPError` is a problem and
       `complete=False`. Returns `RemoteListing(files=..., complete=..., problems=...)`.
   14. `list_files`: `SftpConfig.model_validate(config)`, `_login`, `_client`, `_walk`.
   15. `_gone(sftp, path: str) -> bool`. Parent with `posixpath.dirname`, name with
       `posixpath.basename`, both as bytes. The rules in `design.md` ("`_fetch`").
   16. `_unread(sftp, path, name, when: str) -> ChangedSinceListing | ExternalServiceError`:
       `_gone`, then the matching exception ("before" or "while" it was downloaded), as
       S3's `_unread` returns one (`s3.py:349-377`).
   17. `_stat_or_refusal(...)`: one helper for L1 and F1. Maps `SFTPNoSuchFile` to
       `_unread`, `SFTPPermissionDenied` to `BadRequestError`, a non-regular type to
       `ExternalServiceError`, a size over the cap to `BadRequestError`.
   18. `_fetch`: L1, `open(path, "rb", block_size=0)` through `_call`, F1, the read
       loop, F2, L2, close, the five decisions, in the order of `design.md`. The
       `FILE_SECONDS` deadline is a check before each read. `dest_path` is opened
       once with `open("wb")`.

- Depends on steps 1, 2 and 3.
- Tests: `tests/test_sftp_connector.py` (1-21, 64-68). The allowlist tests patch
  `app.core.sanitize.socket.getaddrinfo`, never `resolve_pinned_url`.
- Non-obvious points:
  - `asyncssh.connect()` defaults `config` to `()`, which reads `~/.ssh/config`, even
    when the options were built with `config=None` (asyncssh §1 trap 5). Pass
    `config=None` to both.
  - `known_hosts` must be a tuple of lists. A list whose first element is a `str` is
    read as file names (asyncssh §2).
  - Pin `import_public_key(...)`, never a private `SSHKey` (asyncssh §2).
  - `start_sftp_client(path_encoding=None)` makes every path bytes. Passing a `str`
    then raises `SFTPBadMessage`, which a test would show at once.

## 5. Registration and the small changes

1. `connectors/__init__.py`: `from app.services.rag.connectors.sftp import
   SftpConnector` and `CONNECTOR_REGISTRY["sftp"] = SftpConnector`, after SharePoint.
2. `app/services/rag/filters.py`: `SFTP = "sftp"` in `Source`. Update the docstring's
   "four origins" sentence.
3. `app/services/sync_source.py`: rename `_RETIRED_CREDENTIAL_FIELDS` to
   `_CREDENTIAL_FIELDS`, add `"password"`, `"private_key"`, `"passphrase"`, and rewrite
   its comment.
4. `app/core/catalog/services.json`: the `sftp` entry of `design.md`.

- Tests: `tests/test_untrusted_remote_names.py` (the guard covers SFTP now, unchanged),
  `tests/test_sync_source_credentials.py` (the kind map at lines 340-345 gets
  `"sftp": "ssh_credentials"`, and a parametrized refusal case for the three names),
  `tests/test_rag_filters.py` (line 34), `tests/test_secret_purposes.py` (unchanged,
  green: the catalog loads).

## 6. The gates in `backend/pyproject.toml`

1. `"app/services/rag/connectors/sftp.py"` after `sharepoint.py` in `[[tool.ty.overrides]]
   include` (line 650) and in `[tool.coverage.run] include` (line 1153), with the same
   one-line comment in both.

- Tests: `tests/test_coverage_gate.py`, `tests/test_ci_parity.py`.
- Check: `make lint-backend` (ruff, ty over the gated module, vulture, deptry).

## 7. `backend/tests/integration/test_sftp_connector.py`

1. Module docstring: what the in-process server proves that a mock cannot, the real
   server variables, and where the recipes live (#29 comment).
2. `pytestmark = pytest.mark.anyio`.
3. Fixtures: host key and client keys (generated per test), the disk server, the
   hostile server, the login variants, the `server` fixture parametrized over
   `"in-process"` and `"real"` (skipped unless `SFTP_TEST_HOST` is set), and
   `connector` with `resolve_pinned_url` patched in the `sftp` module. The patch is
   needed: the servers listen on `127.0.0.1`, which no allowlist opens.
4. Tests 31-58. Patch the bounds and deadlines with `monkeypatch.setattr` on the module,
   and `MAX_UPLOAD_SIZE_MB` on `settings`.

- Depends on steps 4 and 5.
- Run: `uv run pytest tests/integration/test_sftp_connector.py -q`. Then, with each real
  server of research-servers §2 up and its variables set, the same command. Record the
  result per server.

## 8. Mutation checks

For each row of the mutation table in `design.md`: undo the rule in the code, run the
named test, confirm it fails, restore. Record each result in the PR description.

## 9. Licence: asyncssh, change A1

1. `scripts/license_inventory.py`, `python_license` (lines 577-596): after the
   `License-Expression` branch and before the aliases, read a `License` field that is a
   valid SPDX expression with `packaging.licenses.canonicalize_license_expression`,
   evidence "License field". An `InvalidLicenseExpression` falls through to the
   aliases, unchanged.
2. `licenses/policy.toml`: `[review.python."asyncssh"]` from research-licence §1.6, with
   the module path filled in. `[review.python."tqdm"] license = "MPL-2.0 AND MIT"`.
3. `make licenses`, and commit `THIRD_PARTY_NOTICES.md`. Check that only the asyncssh,
   sniffio and tqdm rows change.
4. `docs/licenses.md`, "What the licences ask": a row for `EPL-2.0 OR GPL-2.0-or-later
   (asyncssh)`, 1, the obligations, and how they are met (the dist-info licence, the
   unmodified `.py` files, the source link). The MPL row stays.

- Tests: `tests/test_license_inventory.py` (a `License` field that is an expression is
  read as one, with its evidence, and a free-text field still falls through). `make
  licenses-check`.
- Check that the `asyncssh` row resolves to `EPL-2.0 OR GPL-2.0-or-later` with the
  evidence "License field", and that `make licenses-check` passes with the new entry.

## 10. Frontend

1. `frontend/src/components/rag/connector-icon.tsx`: import `Server` from
   `lucide-react`. Make `Glyph` choose `Server` for `"sftp"`, beside `Globe` for
   `"web"`, with `Database` for any other type. Update the docstring's last sentence.
2. `frontend/src/types/secrets.ts`: `| "ssh_credentials"` after `"git_token"`.

- Tests: `frontend/src/components/rag/connector-icon.test.tsx`, a case "draws an SFTP
  server as a server" (`svg.lucide-server`). From `frontend/`: `bunx vitest run
  src/components/rag/connector-icon.test.tsx`.
- Check: `make lint-frontend`.

## 11. Documentation and the changelog

1. `docs/howto/configure-sync-sources.md`: the changes listed in `design.md`
   ("Documentation"), the new "SFTP setup" section with the anchor `#sftp-setup`, and
   the troubleshooting entries, one per refusal sentence, headed as the others are
   (`### SFTP: "..."`).
2. `docs/file-processing.md`, `docs/secrets.md`, `docs/licenses.md`, `README.md`: the
   changes listed in `design.md`.
3. `docs/configuration.md`, "Project settings": the `SYNC_ALLOWED_HOSTS` row after
   `MCP_ALLOWED_HOSTS` (line 60). The SFTP setup section's "Network rules" links to it.
4. The `.pl`, `.de` and `.es` file of each: translate the same paragraphs with the
   terms in `docs/howto/translate.md`, then `python3 scripts/check_docs_i18n.py
   --update <page>.<locale>.md`.
5. `CHANGELOG.md`, `## [Unreleased]`, `### Added`: one entry. It names the connector,
   the `ssh_credentials` kind, the revision and its gap, and the new setting, empty by
   default.
6. Write all prose in STE.

- Checks: `make lint` (the i18n gate, the paragraph and backtick guards, codespell).
  Check the new anchors by hand: `--strict` does not check fragments.

## 12. Before review

1. The touched test files, `tests/test_security_marker.py`,
   `tests/test_coverage_gate.py`, `tests/test_ci_parity.py`, `make lint`.
2. Say which gates did not run: the full suites, the coverage gate as a whole,
   `make test-integration` as a whole, `make check`, `make docs-build`.
3. Save the real-server setup as a comment on #29 and in memory.
4. Remove `.dev-agent/` and the branch marker file, as `22f7d57fc` did on #28.

## Points the tests can decide either way

- **`x-textarea` on an optional field.** The design expects `Field(json_schema_extra=
  {"x-textarea": True})` on `CredentialStr | None` to land on the property, beside the
  `anyOf`, where `SchemaForm` reads it (`schema-form.tsx:252`). Test 30 decides. If it
  lands in a branch, move it with a `WithJsonSchema` or a `json_schema_extra` callable.
- **One request per read.** The design uses `block_size=0` because `open()` types
  `block_size` as `int`, and 0 takes the direct path (`sftp.py:3339-3340,3462-3467`).
  If ty accepts `None` (asyncssh documents it), either works. Test 44 decides: the
  hostile server must see one read request per call.
- **`_call` and an async iterator step.** `asyncio.ensure_future` on
  `iterator.__anext__()` may not type-check. A one-line coroutine around `anext` does.
- **`is_closed()` after the server closes.** Test 48 decides whether asyncssh marks a
  connection closed before the next request. If not, `_client` reopens after the first
  request fails with a lost connection, and that one file fails.
- **A passphrase given for an unencrypted key.** asyncssh ignores it for an OpenSSH key
  (asyncssh §3). Test 24 records what it does for PKCS#8 and PKCS#1. The design accepts
  what asyncssh accepts.
- **OpenSSH without the pinned key type.** `KeyExchangeFailed` or a plain
  `DisconnectError` (asyncssh §9). Test 51 on atmoz/sftp decides. Both map to a
  `BadRequestError`, with different sentences.
- **READDIR and LSTAT on each real server.** Test 31 decides whether the listed and the
  fetched revisions are equal. If a server disagrees, nothing is wrong, but every run
  downloads its files. Record it in the PR.
- **Close before or after L2.** The design takes L2 while the handle is open. Either
  order gives the same evidence. A test that stalls `close` (47) must still see the
  file's result.
- **A model-level error on the vault form.** A `model_validator` reports no field, so
  the form shows a sentence, not a marked input. No test needs to change that.
- **The number of problems a bound makes.** One. Test 46 pins it.
- **A listed entry the source host can never match.** An entry with a port or a
  trailing dot is left out by `_listed_hosts` and matches nothing, as the MCP list
  folds neither (`app/agents/mcp.py:114-128`). The settings validator does not refuse
  it, so `MCP_ALLOWED_HOSTS` and `SYNC_ALLOWED_HOSTS` keep one rule. Test 64 pins that it
  matches nothing. If the review wants a startup refusal for such an entry, it belongs
  in `validate_sync_allowed_hosts` only.
