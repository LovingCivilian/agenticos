# Implementation plan: a revision for each file (#28)

Write the steps in this order. Each step depends only on the steps above it. Run the
named test files after each step (`cd backend && uv run pytest <files> -q`).

## 1. `app/services/rag/connectors/__init__.py`

1. `RemoteFile.revision: str | None = None`, after `modified_at`.
   - Contract (docstring or field comment): the remote system's version of the file,
     read from the listing, never calculated from the bytes. Equal revisions at one
     `source_path` mean equal content. `None` means the connector reports none.
   - No other change. `list_files()` and `remote_version()` docstrings stay as they are.

## 2. `app/services/rag/connectors/object_store.py` and `s3.py`

1. `StoredObject.etag: str | None = None`. Replace the "Deliberately not an `etag`"
   paragraph with: the sync reads it as the revision, an equal ETag means unchanged
   content, and a multipart or re-uploaded object can get a new ETag for the same
   bytes, which costs one download and never skips a change.
2. `ObjectStoreConnector._listing()`: add `revision=obj.etag` to the `RemoteFile(...)`.
3. `S3Connector._objects()`: add `etag=obj.get("ETag")` to the `StoredObject(...)`.
   Keep the value verbatim, quotes included. The `_objects` docstring gets one sentence
   on that.
- Tests: `tests/test_object_store_connector.py`.

## 3. `app/services/rag/models.py`

1. `DocumentMetadata.revision: str | None = None`, after `content_hash`. One line in the
   class docstring: written only by a connector sync, from `RemoteFile.revision`.

## 4. `app/services/rag/vectorstore.py`

1. `_group_documents()`: add `"revision": metadata.get("revision")` to the
   `additional_info` dict, **after** `**(metadata.get("additional_info") or {})`. A
   comment says why the order matters: a parser's nested key must not decide a download.
2. `BaseVectorStore.restamp_document_revision(self, collection_name: str, document_id:
   str, revision: str, tenant: UUID | None = None) -> None`, `@abstractmethod`, placed
   after `restamp_documents_to_untagged`. The docstring holds the contract from
   `design.md`: every chunk of one document, only the `revision` key, scoped to
   `tenant`, `None` matches untagged rows, a missing table is a no-op, errors raise.
3. `PgVectorStore.restamp_document_revision()`, placed after
   `restamp_documents_to_untagged`:
   1. `if not await self._collection_exists(collection_name): return`.
   2. `table = self._table(collection_name)`, `org_clause, org_params =
      self._org_filter(tenant)`.
   3. Execute `UPDATE {table} SET metadata = metadata || jsonb_build_object('revision',
      CAST(:revision AS text)) WHERE parent_doc_id = :doc_id AND {org_clause}` with
      `{"revision": revision, "doc_id": self._sanitize_id(document_id), **org_params}`.
   4. `await session.commit()`, as the other writes in this class do.
   - Non-obvious: without the `CAST`, asyncpg fails with "could not determine data type
     of parameter". The file already ignores S608 for the interpolated `table`.
- Tests: `tests/test_rag_ingest_tenant_scope.py`, then
  `tests/integration/test_rag_ingest_tenant_scope.py` (run it alone, see
  `backend-tests` on mixing integration and unit files in one run).

## 5. `app/services/rag/ingestion.py`

1. `StoredDocument.revision: str | None = None`. Update the docstring (see design).
2. `_stored()`: `revision=meta.get("revision") or None`.
3. `ingest_file(..., revision: str | None = None)`, keyword-only, after `doc_date`. Set
   `document.metadata.revision = revision` next to `document.metadata.doc_date`. One
   sentence in the docstring: the connector's revision, stored beside `content_hash`.
4. `restamp_revision(self, collection_name: str, document_id: str, revision: str) ->
   None`: `await self.store.restamp_document_revision(collection_name, document_id,
   revision, tenant=self._tenant)`. No `try`. Place it after `document_ids_at`.
- Depends on steps 3 and 4.
- Tests: `tests/test_rag_document_lookup.py`.

## 6. `app/worker/tasks/rag_tasks.py`, `_sync_source`

1. After the `update_only` skip and before `connector.download_file(...)`: the revision
   skip, `sync_mode != "full" and remote_file.revision and remote_file.revision ==
   existing.revision`, then `skipped += 1` and `continue`. A comment states the two
   rules: it is after `kept.add()` so the file stays claimed (#1879), and `full` ignores
   it because `full` is the remedy for a revision that can miss a change.
2. Inside the hash-match branch, before `skipped += 1`: `if remote_file.revision and
   existing.document_id: await ingester.restamp_revision(collection_name,
   existing.document_id, remote_file.revision)`. A comment: a touched file, or a stored
   document from before this change, gets the listed revision, so the next run skips
   its download.
3. In the `ingester.ingest_file(...)` call: `revision=remote_file.revision`.
- Do not touch `sync_local_flow` (lines 591-611) or `app/commands/rag.py`.
- Depends on step 5.
- Tests: `tests/test_connector_sync_modes.py`, `tests/test_connector_sync_state.py`,
  `tests/test_sync_removal.py` (the last two must stay green unchanged).

## 7. Documentation

1. `docs/file-processing.md`: the three "Sync operations" paragraphs and the "A change
   signal" bullet, as `design.md` lists. Small edits on the existing text, in STE.
2. `docs/howto/add-sync-connector.md`: one paragraph under "Two optional hooks: change
   and cleanup", and one "Tips" bullet.
3. `docs/howto/configure-sync-sources.md`, the first bullet of "What a second sync
   does".
4. The `.pl`, `.de` and `.es` translation of each edited page: translate the same
   paragraphs. Use the terms in `docs/howto/translate.md`. Then
   `python3 scripts/check_docs_i18n.py --update <page>.<locale>.md` for each one.
5. `CHANGELOG.md`: one `### Added` entry under `## [Unreleased]`.
- Checks: `make lint` (the i18n gate, the paragraph and backtick guards, codespell).
  `make docs-build` is part of `make check`, so it does not run locally unless asked.

## 8. Aggregate check

`make lint-backend`, the test files above, and `make lint` for the docs. Report that the
full suites, the coverage gate, `make test-integration` as a whole and `make check` did
not run (Actions are off on the fork).

## Points the tests can decide either way

- **A failed refresh.** Decided: `restamp_revision` raises, and the per-file `except`
  counts the file as `failed`. AGENTS.md forbids a swallowed exception. The file stays
  claimed, because `kept.add()` ran first. The test "a refresh that raises" pins this.
- **The ETag quotes.** Decided: the value is kept verbatim. A test that asserts
  `revision == '"abc"'` pins it.
- **`revision` in `additional_info` when absent.** The design always writes the key
  (`None` when absent). A test may assert the key is present with `None`, or only that
  `StoredDocument.revision is None`. The second is less brittle.
- **The refresh condition.** The design calls the refresh for any listed revision on a
  hash match, because an equal revision already stopped at the skip. In `full` mode the
  hash-match branch does not run, so `full` never refreshes. It re-ingests, which writes
  the revision anyway.
- **A stored revision with a `None` listing** (a connector that stopped reporting one).
  The design leaves the stored value in place. The skip needs a listed revision, so the
  stale value is never read.
