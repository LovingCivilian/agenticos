# Design: a revision for each file in a connector sync (#28)

Base: upstream `9fddeb0f0` (v1.27.0). Line numbers are in
`backend/app/worker/tasks/rag_tasks.py` unless a section names another file.

## Summary

A connector reports a revision for each listed file. The sync stores the revision in
the vector metadata of the document, next to `content_hash`. The next run compares the
listed revision with the stored revision before the download. If they are equal, and
the mode is not `full`, the sync skips the file. The S3 connector reports the object's
`ETag`. No migration, no new setting and no API change.

## Changes by module

### `app/services/rag/connectors/__init__.py`

- `RemoteFile.revision: str | None = None`. The version of the file, as the remote
  system reports it in the listing. The connector does not calculate it from the
  bytes. `None` keeps the current behavior. The docstring states the contract: equal
  revisions mean equal content for this `source_path`. A connector whose revision can
  miss a change says so in its own docstring.
- Every current `RemoteFile(...)` call uses keyword arguments, so a defaulted field
  breaks no connector and no test.

### `app/services/rag/connectors/object_store.py`

- `StoredObject.etag: str | None = None`. Rewrite the class docstring: it says
  "Deliberately not an `etag`" because nothing read one. The caller now exists.
- `ObjectStoreConnector._listing()` maps `obj.etag` to `RemoteFile.revision`.
- The docstring records the S3 facts below, so the next store (Azure, GCS) maps its
  own value with the same care.

### `app/services/rag/connectors/s3.py`

- `S3Connector._objects()` passes `etag=obj.get("ETag")` to `StoredObject`.
- The value is kept verbatim, with its double quotes. `list_objects_v2` returns
  `"fba9dede5f27731c9771645a39863328"` with the quotes. The sync compares a listing with
  an earlier listing of the same API, so the quotes do not change the result. A
  transformation is one more thing that can differ between two versions of the code.
- A missing `ETag` gives `None`, and that file uses the hash path.

S3 facts (sources below):

- "The ETag reflects changes only to the contents of an object, not its metadata."
  So a changed object always gets a new ETag, and an equal ETag means unchanged content.
- A single-part upload, plaintext or SSE-S3, has an MD5 of the bytes as its ETag.
  SSE-C and SSE-KMS objects have an ETag that is not an MD5.
- A multipart upload's ETag is the MD5 of the concatenated part MD5s, plus `-<parts>`.
  The same bytes uploaded again with a different part size, or as one part, get a
  different ETag. A `CopyObject` of a multipart object also changes it.
- That case is scope item 4: the revision changed and the hash did not. It costs one
  download, and then the sync refreshes the stored revision. It never skips a change.

Sources:
<https://docs.aws.amazon.com/AmazonS3/latest/API/API_Object.html>,
<https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html>
("Using Content-MD5 and the ETag to verify uploaded objects", and the note on multipart
ETags), <https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html> (the
sample responses show the quotes).

### `app/services/rag/models.py`

- `DocumentMetadata.revision: str | None = None`. `_build_chunk_metadata` already
  spreads `model_dump()` onto every chunk, so the revision reaches the JSONB column with
  no change to the write path. An upload or a local sync writes `"revision": null`, as
  it already writes `"source": null`.

### `app/services/rag/vectorstore.py`

- `_group_documents()` adds `"revision": metadata.get("revision")` to the
  `additional_info` it builds. Without this line `existing_document()` never sees the
  revision, because the dict lists its keys explicitly. The key goes **after** the
  spread of the nested `additional_info`, so a parser's own `revision` key (for example
  a DOCX core property) can never decide a download.
- New abstract method on `BaseVectorStore`, copied from the shape of
  `restamp_documents_to_untagged()`:

  ```python
  @abstractmethod
  async def restamp_document_revision(
      self, collection_name: str, document_id: str, revision: str, tenant: UUID | None = None
  ) -> None:
  ```

  Contract: write `revision` into the metadata of every chunk of one document. Change no
  other key, and add no row. Scope the update to `tenant` with `_org_filter`, as
  `delete_document` does, so one organization cannot change another's rows on a shared
  table. `None` matches only untagged rows. A missing table is a no-op. A database error
  raises. It returns nothing.
- `PgVectorStore.restamp_document_revision()`:

  ```sql
  UPDATE {table}
  SET metadata = metadata || jsonb_build_object('revision', CAST(:revision AS text))
  WHERE parent_doc_id = :doc_id AND {org_clause}
  ```

  The `CAST` is necessary: asyncpg cannot infer the type of a bound parameter that
  `jsonb_build_object` takes as `"any"`. `document_id` goes through `_sanitize_id`, as in
  `delete_document`. The method commits its own session, as every `PgVectorStore` write
  does. This is the store on the vector engine, not a repository, so the
  no-commit rule for repositories does not apply.
- `PgVectorStore` is the only subclass of `BaseVectorStore`. The test fakes are
  `MagicMock` objects, so the abstract method breaks none of them.

### `app/services/rag/ingestion.py`

- `StoredDocument.revision: str | None = None`. `_stored()` reads
  `meta.get("revision") or None`. The docstring changes from "Both fields or neither" to
  "facts about one document": the id and the hash come together, and the revision is
  `None` for a document stored before this change or by a connector that reports none.
- `ingest_file(..., revision: str | None = None)`, keyword-only. It sets
  `document.metadata.revision = revision` beside `source` and `doc_date`, before
  `insert_document`.
- New method:

  ```python
  async def restamp_revision(self, collection_name: str, document_id: str, revision: str) -> None:
  ```

  It calls `store.restamp_document_revision(..., tenant=self._tenant)`. The tenant is the
  ingester's bound tenant, as for `document_ids_at`. It does not catch: a write that
  failed is a failure, unlike `existing_document`, which answers "no match" on an error.

### `app/worker/tasks/rag_tasks.py`, `_sync_source`

The loop after the change (new lines marked NEW):

```python
existing = await ingester.existing_document(collection_name, remote_file.source_path)
if existing.document_id:
    kept.add((remote_file.source_path, existing.document_id))       # line 1382
if sync_mode == "update_only" and not existing.document_id:
    skipped += 1
    continue
if (                                                                # NEW
    sync_mode != "full"
    and remote_file.revision
    and remote_file.revision == existing.revision
):
    skipped += 1
    continue
local_path = await connector.download_file(...)
if sync_mode != "full" and existing.content_hash and (hash == existing.content_hash):
    if remote_file.revision and existing.document_id:               # NEW
        await ingester.restamp_revision(
            collection_name, existing.document_id, remote_file.revision
        )
    skipped += 1
    continue
...
result = await ingester.ingest_file(..., revision=remote_file.revision)  # NEW argument
```

The refresh needs no `!=` check: in `new_only` and `update_only`, an equal revision
already stopped at the skip above. The local-directory sync (lines 591-611) and the CLI
(`app/commands/rag.py`) do not change. They pass no revision.

## Data flow, case by case

| Case | Download | Embed | Store write | Counter |
|---|---|---|---|---|
| No stored document, `new_only` or `full` | yes | yes | new document with the listed revision | `ingested` |
| No stored document, `update_only` | no (existing skip) | no | none | `skipped` |
| Stored document has no revision (first run after the upgrade), same bytes | yes, one time | no | `restamp_revision` writes the listed revision | `skipped` |
| Same revision, `new_only` or `update_only` | no | no | none | `skipped` |
| Revision changed, same hash | yes | no | `restamp_revision` writes the new revision | `skipped` |
| Revision changed, hash changed | yes | yes | replacement document with the new revision | `updated` |
| `full`, any revision | yes | yes | replacement document with the listed revision | `updated` |
| Connector reports `None` | yes | as today | as today, the stored revision stays `null` | as today |
| `restamp_revision` raises | yes | no | none | `failed` (the per-file `except`) |

In every case the stored document is in `kept` before any skip, so `claim_listed`
records it and `_remove_unlisted` does not remove it (upstream `#1879`).

`None` is never equal to `None` for the skip: the check needs a listed revision first.

**A dead run.** Run N inserts the new document (revision r2) and dies before it deletes
the old one (r1) and settles the row. The insert is one transaction, so the new
document is whole or absent. Run N+1: `_reconcile_stale_rows()` runs before the loop,
finds the new document untracked and deletes it. The loop then finds the old document
with r1, so r1 differs from r2, and the file goes through the download and the hash
again. If the worker died after the old document was deleted, the reconcile deletes the
new document and drops both rows. The loop finds no document and ingests the file. The
revision is in the vector metadata, so it goes with the vectors that the reconcile
deletes. A revision kept only on the tracking row could not do this.

**`remote_version()`** runs first and can stop the run before the listing. The revision
check is the next layer, then the hash. The two do not interact.

**What else sees the revision.** `RAGDocumentItem.additional_info` (the document
listing API) and the chunk metadata of a search result already carry `content_hash`. The
revision appears beside it. An ETag is not a secret.

## Sync modes: decision (a)

The local sync finds an unchanged file by its hash only. A connector's revision can now
decide "unchanged" before the download. An exact revision, such as an S3 ETag or a Git
blob SHA, gives the same answer as the hash. A revision that can miss a change, such as
SFTP size and mtime (#29), can make `new_only` and `update_only` skip a rare edit. The
documentation states this, and names `full` as the remedy. A connector whose revision
can miss a change says so in its docstring. There is no opt-in setting. The PR must ask
the maintainers to agree to this change of the mode definition.

## Documentation

- `docs/file-processing.md`, "Sync operations": the paragraph that defines the modes
  adds the revision skip before the download, and that `full` ignores it. The paragraph
  "Where the decision is taken differs between them" adds the revision check before the
  download and the refresh on a hash match. "Both flows, and they have to agree" gets
  decision (a).
- `docs/file-processing.md`, "What a new connector owes", "A change signal": the signal
  now has a field, `RemoteFile.revision`. The bullet says what to put in it, that
  `content_hash` stays the fallback, and that a revision that can miss a change goes in
  the connector's docstring. The object-store paragraph says that S3 reports its ETag.
- `docs/howto/add-sync-connector.md`, "Two optional hooks: change and cleanup": add a
  short paragraph on `RemoteFile.revision`, beside `remote_version()`: one value for the
  whole source versus one for each file. Add one line to "Tips".
- `docs/howto/configure-sync-sources.md`, "What a second sync does", says "An unchanged
  file costs a download, not an embedding". That sentence is no longer true for S3.
  AGENTS.md asks for the update in the same change, so this page changes too.
- The `.pl`, `.de` and `.es` file of each edited page changes too. Record each with
  `python3 scripts/check_docs_i18n.py --update <page>.<locale>.md`.
- `CHANGELOG.md`, `## [Unreleased]`, `### Added`: one entry. It says that `new_only` and
  `update_only` skip the download of a file with an unchanged revision, that S3 reports
  its ETag, that `full` downloads every file, and that the first sync after the upgrade
  downloads each file one more time. MINOR by the release table.

## Migration

None. No SQLAlchemy model changes. The revision is a new key in the existing JSONB
`metadata` column of the runtime `rag_<collection>` tables. The sync reads it and never
searches by it, so it needs no index. A row without the key reads as `None`, which is
the "no revision" case. `make db-check` is not necessary, but it is cheap to run.

## Test strategy

No module in this change is in the 100% coverage gate (`app/services/rag/*` and
`rag_tasks.py` are template-inherited). The tests below are named behaviors instead.

| Component | Layer | File | Why this layer |
|---|---|---|---|
| The skip, the refresh and the claim in `_sync_source` | Unit | `tests/test_connector_sync_modes.py` (extend) | It already runs `_run_source_sync` with the real `IngestionService` and the reference lookup. Only the store, the database and the ingest are mocks. |
| `StoredDocument.revision`, `_group_documents`, `ingest_file(revision=...)` | Unit | `tests/test_rag_document_lookup.py` (extend) | The precedence and the replace tests are there, with the `_doc` and `_replacing` helpers. |
| The SQL of `restamp_document_revision` carries the tenant conjunct | Unit | `tests/test_rag_ingest_tenant_scope.py`, `TestPgVectorStoreScopesEveryRowOp` (extend) | Every row operation has its shape test there. |
| `restamp_document_revision` against real rows | Integration | `tests/integration/test_rag_ingest_tenant_scope.py` (extend) | Only a real JSONB column shows that the `||` keeps the other keys and that the tenant predicate isolates rows. |
| The ETag in the listing | Unit | `tests/test_object_store_connector.py` (extend) | `BlobConnector` tests the shared half, and the S3 class tests boto3's vocabulary. |

No API test and no e2e test: no route, schema or screen changes.

Tests to add (names are suggestions, the behaviors are required):

- `test_connector_sync_modes.py`, a new class for the revision. Give `_connector` and
  `_stored` a keyword-only `revision`, and give the store mock an `AsyncMock` for
  `restamp_document_revision` that a test can read. Keep the current call sites as they
  are.
  - An unchanged revision skips the download and the ingest in `new_only` and
    `update_only` (parametrized). `skipped == 1`.
  - That skipped file is still in `claim_listed`'s `documents`.
  - `full` downloads and ingests the file, and passes `revision=` to `ingest_file`.
  - A changed revision with the same bytes: the download happens, the ingest does not,
    the store gets the new revision for `vector-doc-1`, and `skipped == 1`.
  - A changed revision with changed bytes: `ingest_file` gets the new revision,
    `updated == 1`, and no refresh happens.
  - A stored document with no revision: one download, the refresh writes the listed
    revision, no ingest.
  - A connector that reports `None`: the download happens and no refresh happens,
    even when the stored document has no revision either.
  - A refresh that raises counts the file as `failed`, not `skipped`.
- `test_rag_document_lookup.py`:
  - `existing_document` returns the revision with the id and the hash of one document.
  - A document without a revision answers `revision=None`.
  - A nested `additional_info["revision"]` from a parser does not become the revision.
  - `ingest_file(revision="etag-2")` puts the revision on the document that
    `insert_document` receives. Without the argument it is `None`.
  - `restamp_revision` passes the ingester's bound tenant to the store.
- `test_rag_ingest_tenant_scope.py` (unit): the statement contains
  `parent_doc_id = :doc_id AND (metadata->>'organization_id') = :org`, binds the
  revision, and uses `IS NULL` for a `None` tenant.
- `tests/integration/test_rag_ingest_tenant_scope.py`:
  - Org A cannot change the revision of Org B's document, even by its id.
  - The restamp changes every chunk of the named document, keeps `content_hash`,
    `source_path` and `organization_id`, and leaves the other document alone.
    `find_existing_document` then returns the new revision.
  - A missing collection is a no-op.
- `test_object_store_connector.py`:
  - The shared listing maps `StoredObject.etag` to `RemoteFile.revision`. An object
    without an ETag has no revision.
  - `S3Connector` reports each object's `ETag` verbatim, a single-part one and a
    multipart one with its `-<parts>` suffix.
