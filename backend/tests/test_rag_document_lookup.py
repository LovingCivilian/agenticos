"""One precedence, one scan, for "which stored document is this file" (#548, #566).

`find_existing` and `get_existing_hash` used to walk the collection with
different rules: the id lookup checked every document for a `source_path`
match before falling back to `filename`, while the hash lookup interleaved
the two and let a filename hit block a later source-path match. Row order
decided which document each answered with — `get_documents` had no ORDER BY —
so the sync modes in `rag_tasks.py` compared a live file's hash against a
different document's `content_hash` than the one they were about to replace:
an unchanged file re-embedded on every sync, or a changed one skipped as
already current.

Both answers now come from one call, which is the structural half of that fix:
a caller cannot pair one document's id with another's hash if it cannot ask for
one without the other. It is also one full-collection read per file rather than
the two the sync modes did and the two more `ingest_file` added (#566).

This module is template-inherited and outside the coverage gate, which is why
the precedence is pinned by name rather than trusted to a percentage.
"""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.rag.ingestion import IngestionService, StoredDocument
from app.services.rag.models import (
    Document,
    DocumentInfo,
    DocumentMetadata,
    DocumentPage,
    DocumentPageChunk,
    IngestionStatus,
)
from app.services.rag.vectorstore import BaseVectorStore, PgVectorStore

pytestmark = pytest.mark.anyio


def _store(docs: list[DocumentInfo], **overrides: object) -> MagicMock:
    """A mock store whose precedence is the real one.

    `find_existing_document` is bound from `BaseVectorStore`, so it runs the
    reference precedence over `get_documents` rather than a mock returning a
    truthy stand-in - the same code `PgVectorStore` overrides for the indexed
    path, and the thing every assertion below is actually about.
    """
    store = MagicMock()
    store.get_documents = AsyncMock(return_value=docs)
    store.find_existing_document = BaseVectorStore.find_existing_document.__get__(store)
    for name, value in overrides.items():
        setattr(store, name, value)
    return store


def _service(docs: list[DocumentInfo]) -> IngestionService:
    return IngestionService(processor=MagicMock(), vector_store=_store(docs))


def _doc(
    document_id: str,
    *,
    filename: str,
    source_path: str = "",
    content_hash: str = "",
    revision: str | None = None,
) -> DocumentInfo:
    return DocumentInfo(
        document_id=document_id,
        filename=filename,
        additional_info={
            "source_path": source_path,
            "content_hash": content_hash,
            "revision": revision,
        },
    )


class TestOnePrecedenceForBothAnswers:
    async def test_the_id_and_the_hash_name_the_same_document(self):
        """A filename match ordered before the source-path match must not split the answer."""
        service = _service(
            [
                _doc(
                    "doc-a",
                    filename="handbook.pdf",
                    source_path="/old/handbook.pdf",
                    content_hash="hash-a",
                ),
                _doc(
                    "doc-b",
                    filename="handbook.pdf",
                    source_path="/srv/sync/handbook.pdf",
                    content_hash="hash-b",
                ),
            ]
        )

        existing = await service.existing_document("kb", "/srv/sync/handbook.pdf")

        assert existing == StoredDocument(
            document_id="doc-b", content_hash="hash-b", source_path="/srv/sync/handbook.pdf"
        )

    async def test_a_filename_match_answers_when_no_source_path_does(self):
        service = _service(
            [
                _doc("doc-a", filename="other.pdf", content_hash="hash-a"),
                _doc("doc-b", filename="handbook.pdf", content_hash="hash-b"),
            ]
        )

        existing = await service.existing_document("kb", "/srv/sync/handbook.pdf")

        assert existing == StoredDocument(document_id="doc-b", content_hash="hash-b")

    async def test_a_hash_match_is_the_last_resort_behind_the_filename(self):
        """The order `ingest_file` used to get by calling two helpers in turn.

        A document whose hash matches is the same content under another name, so
        it is a match - but only where neither the path nor the name found one,
        because a file that moved keeps its identity and a file that was copied
        does not take over the original's.
        """
        service = _service(
            [
                _doc("doc-a", filename="copy.pdf", content_hash="hash-x"),
                _doc("doc-b", filename="handbook.pdf", content_hash="hash-y"),
            ]
        )

        by_name = await service.existing_document(
            "kb", "/srv/sync/handbook.pdf", content_hash="hash-x"
        )
        by_hash = await service.existing_document(
            "kb", "/srv/sync/absent.pdf", content_hash="hash-x"
        )

        assert by_name.document_id == "doc-b"
        assert by_hash.document_id == "doc-a"

    async def test_a_hash_is_only_matched_when_one_was_offered(self):
        """An unhashed file must not match the first document that has no hash."""
        service = _service([_doc("doc-a", filename="other.pdf")])

        assert await service.existing_document("kb", "/srv/sync/handbook.pdf") == StoredDocument()

    async def test_no_match_answers_neither_id_nor_hash(self):
        service = _service([_doc("doc-a", filename="other.pdf", content_hash="hash-a")])

        assert await service.existing_document("kb", "/srv/sync/handbook.pdf") == StoredDocument()

    async def test_a_store_failure_answers_no_match(self):
        """A listing that cannot be read is not evidence the document is absent -
        but treating it as a match would delete one on a failed query."""
        store = _store([], get_documents=AsyncMock(side_effect=RuntimeError("connection refused")))
        service = IngestionService(processor=MagicMock(), vector_store=store)

        assert await service.existing_document("kb", "/srv/sync/handbook.pdf") == StoredDocument()

    async def test_a_document_without_a_stored_hash_answers_none_not_empty(self):
        """`_group_documents` fills an absent hash with "" — callers gate on truthiness."""
        service = _service(
            [_doc("doc-a", filename="handbook.pdf", source_path="/srv/sync/handbook.pdf")]
        )

        existing = await service.existing_document("kb", "/srv/sync/handbook.pdf")

        assert existing == StoredDocument(
            document_id="doc-a", content_hash=None, source_path="/srv/sync/handbook.pdf"
        )


class TestTheRevisionTravelsWithTheDocument:
    """A connector's revision is a third fact about the stored document,
    returned with the id and the hash for the reason those two are returned
    together: a sync that compared a listed revision against another document's
    would skip a download on the strength of the wrong file (#548)."""

    async def test_the_revision_comes_with_the_id_and_the_hash_of_one_document(self):
        service = _service(
            [
                _doc(
                    "doc-a",
                    filename="handbook.pdf",
                    source_path="/old/handbook.pdf",
                    content_hash="hash-a",
                    revision='"etag-a"',
                ),
                _doc(
                    "doc-b",
                    filename="handbook.pdf",
                    source_path="s3://acme/handbook.pdf",
                    content_hash="hash-b",
                    revision='"etag-b"',
                ),
            ]
        )

        existing = await service.existing_document("kb", "s3://acme/handbook.pdf")

        assert existing == StoredDocument(
            document_id="doc-b",
            content_hash="hash-b",
            source_path="s3://acme/handbook.pdf",
            revision='"etag-b"',
        )

    async def test_a_document_stored_without_a_revision_answers_none(self):
        """Every document synced before revisions, and every upload and local sync."""
        service = _service(
            [
                _doc(
                    "doc-a",
                    filename="handbook.pdf",
                    source_path="s3://acme/handbook.pdf",
                    content_hash="hash-a",
                )
            ]
        )

        existing = await service.existing_document("kb", "s3://acme/handbook.pdf")

        assert existing.document_id == "doc-a"
        assert existing.revision is None

    async def test_a_parsers_own_revision_key_is_not_the_files_revision(self):
        """A parser's nested `additional_info` is spread into what the lookup
        returns - a DOCX's core properties include a `revision` counter. Read as
        the connector's revision, a document property would decide whether a
        file is downloaded."""
        execute = AsyncMock(
            return_value=MagicMock(
                fetchone=MagicMock(
                    return_value=(
                        "doc-a",
                        {
                            "source_path": "s3://acme/handbook.docx",
                            "content_hash": "h",
                            "additional_info": {"revision": "7"},
                        },
                    )
                )
            )
        )
        store = TestTheIndexedLookupIssuesOneStatementPerKey._store_over(execute)
        service = IngestionService(processor=MagicMock(), vector_store=store)

        existing = await service.existing_document("kb", "s3://acme/handbook.docx")

        assert existing.document_id == "doc-a"
        assert existing.revision is None

    async def test_the_files_revision_wins_over_a_parsers_key_of_the_same_name(self):
        execute = AsyncMock(
            return_value=MagicMock(
                fetchone=MagicMock(
                    return_value=(
                        "doc-a",
                        {
                            "source_path": "s3://acme/handbook.docx",
                            "content_hash": "h",
                            "revision": '"etag-a"',
                            "additional_info": {"revision": "7"},
                        },
                    )
                )
            )
        )
        store = TestTheIndexedLookupIssuesOneStatementPerKey._store_over(execute)

        hit = await store.find_existing_document(
            "kb", source_path="s3://acme/handbook.docx", content_hash="h"
        )

        assert hit is not None and hit.additional_info is not None
        assert hit.additional_info["revision"] == '"etag-a"'

    @staticmethod
    def _ingesting(*, tenant: uuid.UUID | None = None) -> tuple[AsyncMock, IngestionService]:
        insert = AsyncMock()
        store = _store([], insert_document=insert, delete_document=AsyncMock())
        document = Document(
            pages=[DocumentPage(page_num=1, content="body")],
            metadata=DocumentMetadata(filename="handbook.pdf", filesize=4, filetype="pdf"),
        )
        document.chunked_pages = [
            DocumentPageChunk(chunk_content="body", chunk_num=0, page_num=1, content="body")
        ]
        document.metadata.content_hash = "hash-a"
        processor = MagicMock(process_file=AsyncMock(return_value=document))
        return insert, IngestionService(processor=processor, vector_store=store, tenant=tenant)

    async def test_an_ingest_stores_the_revision_beside_the_hash(self):
        """On the document `insert_document` writes, so it is on every chunk -
        in the vector metadata, where a dead run's clean-up removes it with the
        vectors, rather than only on the tracking row."""
        insert, service = self._ingesting()

        result = await service.ingest_file(
            filepath=Path("handbook.pdf"),
            collection_name="kb",
            source_path="s3://acme/handbook.pdf",
            revision='"etag-2"',
        )

        assert result.status is IngestionStatus.DONE
        written: Document = insert.await_args.kwargs["document"]
        assert (written.metadata.content_hash, written.metadata.revision) == (
            "hash-a",
            '"etag-2"',
        )

    async def test_an_ingest_given_no_revision_stores_none(self):
        insert, service = self._ingesting()

        await service.ingest_file(
            filepath=Path("handbook.pdf"), collection_name="kb", source_path="handbook.pdf"
        )

        written: Document = insert.await_args.kwargs["document"]
        assert written.metadata.revision is None

    async def test_a_refreshed_revision_is_written_in_the_ingesters_tenant(self):
        """The same confinement as the replace-delete (#1684): a document id
        alone reaches every tenant's rows under it on a shared table. And at
        the address the revision was read for, which is also what lets the
        store find the rows by an index rather than a scan."""
        tenant = uuid.uuid4()
        seen: list[tuple[str, str, str, str, uuid.UUID | None]] = []

        async def restamp(
            collection_name: str,
            document_id: str,
            revision: str,
            *,
            source_path: str,
            tenant: uuid.UUID | None = None,
        ) -> None:
            seen.append((collection_name, document_id, revision, source_path, tenant))

        store = _store([], restamp_document_revision=AsyncMock(side_effect=restamp))
        service = IngestionService(processor=MagicMock(), vector_store=store, tenant=tenant)

        await service.restamp_revision("kb", "doc-a", '"etag-2"', "s3://acme/handbook.pdf")

        assert seen == [("kb", "doc-a", '"etag-2"', "s3://acme/handbook.pdf", tenant)]

    async def test_a_refresh_the_store_refuses_raises(self):
        """Unlike the lookup, which answers "no match" on a store error: a write
        that failed is a failure, and the sync counts it as one."""
        store = _store(
            [], restamp_document_revision=AsyncMock(side_effect=RuntimeError("connection refused"))
        )
        service = IngestionService(processor=MagicMock(), vector_store=store)

        with pytest.raises(RuntimeError, match="connection refused"):
            await service.restamp_revision("kb", "doc-a", '"etag-2"', "s3://acme/handbook.pdf")


class TestADocumentThatNamesItsOwnAddress:
    """The filename fallback may not claim one (#990).

    It exists so a file uploaded through the browser and later synced from the
    folder it came from is replaced rather than duplicated - an upload stores its
    filename as its `source_path`, so the two agree and it stays reachable by
    name. A document naming a *different* address is a different document, and
    matching it by basename loses one of the two.
    """

    async def test_two_keys_with_the_same_basename_are_two_documents(self):
        """An S3 bucket holding `a/readme.md` and `b/readme.md`. The second key
        found the first's document by name, so under `new_only` equal contents
        skipped it and unequal contents *replaced* the first - either way a
        first sync could not keep both, and said nothing."""
        service = _service(
            [
                _doc(
                    "doc-a",
                    filename="readme.md",
                    source_path="s3://bucket/a/readme.md",
                    content_hash="hash-a",
                )
            ]
        )

        existing = await service.existing_document("kb", "s3://bucket/b/readme.md")

        assert existing == StoredDocument()

    async def test_two_local_files_of_the_same_name_in_different_folders_are_two(self):
        """The same collision on the flow that had the modes right all along."""
        service = _service(
            [
                _doc(
                    "doc-a",
                    filename="notes.md",
                    source_path="/srv/docs/one/notes.md",
                    content_hash="hash-a",
                )
            ]
        )

        assert await service.existing_document("kb", "/srv/docs/two/notes.md") == StoredDocument()

    async def test_an_uploaded_document_is_still_replaced_by_its_sync(self):
        """The case the fallback is for, and the reason it is narrowed rather
        than removed: an upload's `source_path` is its filename."""
        service = _service(
            [_doc("doc-a", filename="handbook.md", source_path="handbook.md", content_hash="h")]
        )

        existing = await service.existing_document("kb", "gdrive://file-1/handbook.md")

        # Its own address, not the one asked for: the sync compares the two
        # before it trusts a revision.
        assert existing == StoredDocument(
            document_id="doc-a", content_hash="h", source_path="handbook.md"
        )


class TestHowManyTimesTheCollectionIsRead:
    """#566. Three lookups over one listing, and a sync asked for two of them."""

    async def test_both_answers_cost_one_read(self):
        store = _store(
            [
                _doc(
                    "doc-a",
                    filename="handbook.pdf",
                    source_path="/srv/sync/handbook.pdf",
                    content_hash="hash-a",
                )
            ]
        )
        service = IngestionService(processor=MagicMock(), vector_store=store)

        existing = await service.existing_document("kb", "/srv/sync/handbook.pdf")

        assert existing.document_id and existing.content_hash
        assert store.get_documents.await_count == 1

    async def test_an_ingest_that_replaces_reads_the_collection_once(self):
        """It read it twice: once for the path, then again for the hash.

        Both are decided in one pass now, so the count is one whether the path
        matched or the hash did.
        """
        store = _store([], insert_document=AsyncMock(), delete_document=AsyncMock())
        document = Document(
            pages=[DocumentPage(page_num=1, content="body")],
            metadata=DocumentMetadata(filename="handbook.pdf", filesize=4, filetype="pdf"),
        )
        document.chunked_pages = [
            DocumentPageChunk(chunk_content="body", chunk_num=0, page_num=1, content="body")
        ]
        document.metadata.content_hash = "hash-a"
        processor = MagicMock(process_file=AsyncMock(return_value=document))
        service = IngestionService(processor=processor, vector_store=store)

        result = await service.ingest_file(
            filepath=Path("handbook.pdf"),
            collection_name="kb",
            replace=True,
            source_path="/srv/sync/handbook.pdf",
        )

        assert result.status is IngestionStatus.DONE
        assert store.get_documents.await_count == 1


class TestReplacingADocument:
    """The new one is written before the old one is removed (#990).

    `insert_document` is where the embeddings are computed, so a provider that
    refuses between the two statements used to leave the collection holding
    *neither* - permanently, because `ingest_file` returns the failure rather
    than raising it and nothing retries. Both for the length of an insert is a
    state a search survives; neither is not.
    """

    @staticmethod
    def _replacing(insert: AsyncMock) -> tuple[MagicMock, IngestionService]:
        store = _store(
            [
                _doc(
                    "doc-old",
                    filename="handbook.pdf",
                    source_path="/srv/sync/handbook.pdf",
                    content_hash="hash-old",
                )
            ],
            insert_document=insert,
            delete_document=AsyncMock(),
        )
        document = Document(
            pages=[DocumentPage(page_num=1, content="body")],
            metadata=DocumentMetadata(filename="handbook.pdf", filesize=4, filetype="pdf"),
        )
        document.chunked_pages = [
            DocumentPageChunk(chunk_content="body", chunk_num=0, page_num=1, content="body")
        ]
        document.metadata.content_hash = "hash-new"
        processor = MagicMock(process_file=AsyncMock(return_value=document))
        return store, IngestionService(processor=processor, vector_store=store)

    async def test_a_failed_embedding_leaves_the_old_document_in_place(self):
        store, service = self._replacing(AsyncMock(side_effect=RuntimeError("provider refused")))

        result = await service.ingest_file(
            filepath=Path("handbook.pdf"),
            collection_name="kb",
            replace=True,
            source_path="/srv/sync/handbook.pdf",
        )

        assert result.status is IngestionStatus.ERROR
        store.delete_document.assert_not_awaited()

    async def test_a_successful_replacement_still_removes_the_old_one(self):
        store, service = self._replacing(AsyncMock())

        result = await service.ingest_file(
            filepath=Path("handbook.pdf"),
            collection_name="kb",
            replace=True,
            source_path="/srv/sync/handbook.pdf",
        )

        assert result.status is IngestionStatus.DONE
        assert result.replaced_document_id == "doc-old"
        # The replace-delete carries the ingester's bound tenant (None here), so
        # it can only remove the old document within that tenant's rows (#1684).
        store.delete_document.assert_awaited_once_with("kb", "doc-old", None)


class TestTheIndexedLookupIssuesOneStatementPerKey:
    """`PgVectorStore` answers the existence check by index, not by a scan (#1102).

    The real SQL and the indexes it leans on are exercised against a populated
    Postgres in `tests/integration/test_rag_existence_index.py`; here the
    statements are asserted on a mock session, which is where the *precedence*
    and the "one statement, then stop" shape are cheap to pin - a heap that
    happened to be ordered would pass a behavioural check falsely.
    """

    @staticmethod
    def _store_over(execute: AsyncMock) -> PgVectorStore:
        session = MagicMock(execute=execute)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        store = PgVectorStore.__new__(PgVectorStore)
        store.async_session = MagicMock(return_value=session_ctx)
        store._collection_exists = AsyncMock(return_value=True)  # type: ignore[method-assign]
        store._table = MagicMock(return_value="rag_kb")  # type: ignore[method-assign]
        return store

    @staticmethod
    def _result(row: tuple[str, dict[str, str]] | None) -> MagicMock:
        return MagicMock(fetchone=MagicMock(return_value=row))

    async def test_a_source_path_hit_is_one_indexed_statement(self):
        """The common case: the file's `source_path` is already stored. One
        `WHERE metadata->>'source_path'` statement answers it, and the filename
        and content_hash statements never run."""
        execute = AsyncMock(
            return_value=self._result(
                ("doc-a", {"source_path": "/p/handbook.pdf", "content_hash": "h"})
            )
        )
        store = self._store_over(execute)

        hit = await store.find_existing_document(
            "kb", source_path="/p/handbook.pdf", content_hash="h"
        )

        assert hit is not None and hit.document_id == "doc-a"
        assert execute.await_count == 1
        statement = str(execute.await_args.args[0])
        assert "metadata->>'source_path' = :v" in statement
        assert "ORDER BY parent_doc_id, id" in statement
        assert "LIMIT 1" in statement

    async def test_the_keys_are_tried_in_precedence_and_stop_at_the_first_hit(self):
        """source_path misses, filename misses, content_hash answers - three
        statements in that order, and the hash one's row is returned."""
        execute = AsyncMock(
            side_effect=[
                self._result(None),
                self._result(None),
                self._result(("doc-c", {"content_hash": "h"})),
            ]
        )
        store = self._store_over(execute)

        hit = await store.find_existing_document(
            "kb", source_path="/p/handbook.pdf", content_hash="h"
        )

        assert hit is not None and hit.document_id == "doc-c"
        keys = [str(call.args[0]) for call in execute.await_args_list]
        assert "metadata->>'source_path'" in keys[0]
        assert "metadata->>'filename'" in keys[1]
        assert "metadata->>'content_hash'" in keys[2]

    async def test_an_absent_collection_issues_no_statement(self):
        execute = AsyncMock()
        store = self._store_over(execute)
        store._collection_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]

        assert (
            await store.find_existing_document("kb", source_path="/p/x.pdf", content_hash="h")
        ) is None
        execute.assert_not_awaited()


class TestGetDocumentsIsDeterministic:
    async def test_the_document_listing_carries_an_order_by(self):
        """The defence half of #548: arbitrary heap order is what let the two
        old lookups disagree, so the listing pins its own order. Asserted on
        the statement — the real consequence needs a populated Postgres, and a
        heap that happens to be ordered would pass a behavioural check falsely.
        """
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        store = PgVectorStore.__new__(PgVectorStore)
        store.async_session = MagicMock(return_value=session_ctx)
        store._collection_exists = AsyncMock(return_value=True)  # type: ignore[method-assign]
        store._table = MagicMock(return_value="collection_kb")  # type: ignore[method-assign]

        await store.get_documents("kb")

        statement = str(session.execute.await_args.args[0])
        assert "ORDER BY parent_doc_id, id" in statement
