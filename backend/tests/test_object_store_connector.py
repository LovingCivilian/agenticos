"""What an object store's connector inherits rather than repeats (#988).

S3, Azure Blob and GCS are one connector with three clients, and #938 made the
other two conditional on this shape existing first - the alternative being three
connectors sharing a form by resemblance, where a fix to the listing loop lands
in one of them.

So these tests are written against a **subclass that is not S3**: what they hold
shut is the shared half - the `/`-terminated key that is a console's folder and
not a document, the `source_path` the whole sync path matches on, and the
container coming from whichever field the store's own console calls it. A test
that used `S3Connector` for this would prove the same code twice and say nothing
about the next store.

`TestTheS3ConnectorStillListsWhatItListed` is the other half: the refactor is
only correct if a stored source syncs exactly the way it did before it.
"""

import datetime
import inspect
import io
from pathlib import Path
from typing import Any, ClassVar, Self
from unittest.mock import MagicMock, patch

import pytest
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError, ReadTimeoutError
from pydantic import BaseModel, Field, SecretStr
from s3transfer.exceptions import S3DownloadFailedError

from app.core.exceptions import BadRequestError, ExternalServiceError
from app.core.secret_kinds import AwsCredentialsSecret, StorableSecret
from app.services.rag.connectors import (
    ChangedSinceListing,
    ConnectorConfig,
    FetchedFile,
    RemoteFile,
)
from app.services.rag.connectors.object_store import ObjectStoreConnector, StoredObject
from app.services.rag.connectors.s3 import S3Connector, _Received

pytestmark = pytest.mark.anyio


class BlobConfig(BaseModel):
    container: str = Field(title="Container")


class BlobConnector(ObjectStoreConnector):
    """A store that calls its container a container, as Azure's console does."""

    CONNECTOR_TYPE: ClassVar[str] = "blob"
    SCHEME: ClassVar[str] = "azblob"
    CONTAINER_FIELD: ClassVar[str] = "container"
    CONFIG_MODEL: ClassVar[type[BaseModel]] = BlobConfig

    def __init__(
        self, objects: list[StoredObject] | None = None, *, reports: str | None = None
    ) -> None:
        self.objects = objects or []
        self.reports = reports
        self.listed: list[tuple[str, str]] = []
        self.downloaded: list[tuple[str, str, Path]] = []
        self.sizes: list[int | None] = []

    def _objects(
        self,
        container: str,
        prefix: str,
        config: ConnectorConfig,
        credential: StorableSecret | None,
    ) -> list[StoredObject]:
        self.listed.append((container, prefix))
        return self.objects

    def _download(
        self,
        container: str,
        key: str,
        dest_path: Path,
        config: ConnectorConfig,
        credential: StorableSecret | None,
        *,
        size: int | None,
    ) -> str | None:
        self.downloaded.append((container, key, dest_path))
        self.sizes.append(size)
        dest_path.write_bytes(b"contents")
        return self.reports


class TestWhatEveryObjectStoreGetsForFree:
    async def test_the_container_comes_from_the_field_the_store_names(self):
        """`bucket` is S3's and GCS's word; Azure's is `container`. A shared
        listing that read `config["bucket"]` would make the next store's form
        lie about itself."""
        connector = BlobConnector([StoredObject(key="a.md")])

        await connector.list_files({"container": "docs", "prefix": "legal/"}, None)

        assert connector.listed == [("docs", "legal/")]

    async def test_a_key_ending_in_a_slash_is_not_a_document(self):
        """A console that creates a "folder" writes a zero-byte object with that
        name. Ingested, it is a document with no bytes and no filename."""
        connector = BlobConnector(
            [StoredObject(key="legal/"), StoredObject(key="legal/nda.pdf", size=12)]
        )

        files = (await connector.list_files({"container": "docs"}, None)).files

        assert [f.name for f in files] == ["nda.pdf"]

    async def test_the_dedup_key_is_scheme_container_and_key(self):
        """`source_path` is what the sync path matches a stored row on (#996), so
        it is built once here rather than per client."""
        connector = BlobConnector([StoredObject(key="legal/nda.pdf")])

        files = (await connector.list_files({"container": "docs"}, None)).files

        assert files[0].source_path == "azblob://docs/legal/nda.pdf"
        assert files[0].id == "legal/nda.pdf"

    async def test_two_containers_holding_one_basename_stay_two_documents(self):
        """The collision #990 removed on the vector side, reached from here: the
        name is a display name and the key is the identity."""
        connector = BlobConnector(
            [StoredObject(key="a/readme.md"), StoredObject(key="b/readme.md")]
        )

        files = (await connector.list_files({"container": "docs"}, None)).files

        assert {f.source_path for f in files} == {
            "azblob://docs/a/readme.md",
            "azblob://docs/b/readme.md",
        }

    async def test_a_listing_carries_the_size_and_the_modified_time(self):
        when = datetime.datetime(2026, 8, 20, 9, 30, tzinfo=datetime.UTC)
        connector = BlobConnector([StoredObject(key="a.md", size=41, modified_at=when)])

        files = (await connector.list_files({"container": "docs"}, None)).files

        assert (files[0].size, files[0].modified_at) == (41, when)

    async def test_an_objects_etag_is_its_revision(self):
        """What the sync compares before it downloads: every store has an
        ETag, and an equal one means unchanged content. The shared listing maps
        it, so the next store only has to report it."""
        connector = BlobConnector([StoredObject(key="a.md", etag='"0x8DC1A2B3C4D5E6F"')])

        files = (await connector.list_files({"container": "docs"}, None)).files

        assert files[0].revision == '"0x8DC1A2B3C4D5E6F"'

    async def test_an_object_listed_without_an_etag_has_no_revision(self):
        """No revision is never "unchanged": the file takes the download and the
        hash, as every file did before revisions."""
        connector = BlobConnector([StoredObject(key="a.md")])

        files = (await connector.list_files({"container": "docs"}, None)).files

        assert files[0].revision is None

    async def test_the_download_reads_the_container_back_out_of_the_source_path(
        self, tmp_path: Path
    ):
        """A `RemoteFile` reaches `_fetch` from a listing made earlier, and the
        config it is handed need not be the one that listed it - the address is."""
        connector = BlobConnector()

        landed = await connector.fetch_file(
            RemoteFile(
                id="legal/nda.pdf", name="nda.pdf", source_path="azblob://docs/legal/nda.pdf"
            ),
            tmp_path,
            config={"container": "ignored-here"},
        )

        assert connector.downloaded == [("docs", "legal/nda.pdf", tmp_path / "nda.pdf")]
        assert landed.path.read_bytes() == b"contents"

    async def test_a_fetch_answers_the_version_the_store_sent_not_the_listed_one(
        self, tmp_path: Path
    ):
        """The sync stores this beside the document. An object replaced after
        the listing arrives as its new bytes, and only the download can say
        which version those are."""
        connector = BlobConnector(reports='"0x8DC1A2B3C4D5E70"')

        fetched = await connector.fetch_file(
            RemoteFile(
                id="nda.pdf",
                name="nda.pdf",
                size=12,
                source_path="azblob://docs/nda.pdf",
                revision='"0x8DC1A2B3C4D5E6F"',
            ),
            tmp_path,
        )

        assert (fetched.path, fetched.revision) == (tmp_path / "nda.pdf", '"0x8DC1A2B3C4D5E70"')
        # The listed size, for a store that fetches a small object differently.
        assert connector.sizes == [12]

    async def test_a_name_climbing_out_of_the_sync_directory_is_refused(self, tmp_path: Path):
        """Inherited from `BaseSyncConnector.fetch_file`, and asserted here
        because it is the property a new store most easily loses: the destination
        is not the connector's to choose."""
        connector = BlobConnector()

        with pytest.raises(BadRequestError):
            await connector.fetch_file(
                RemoteFile(id="docs/..", name="..", source_path="azblob://docs/docs/.."),
                tmp_path,
                config={"container": "docs"},
            )

        assert connector.downloaded == []


class TestTheS3ConnectorStillListsWhatItListed:
    """The refactor is only correct if a stored source syncs unchanged."""

    @staticmethod
    def _client(pages: list[dict[str, Any]]) -> MagicMock:
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = pages
        return client

    @staticmethod
    def _credential() -> AwsCredentialsSecret:
        return AwsCredentialsSecret(
            aws_access_key_id="AKIATENANT",
            aws_secret_access_key=SecretStr("tenant-secret"),
            region_name="eu-west-1",
        )

    async def test_a_bucket_listing_is_s3_addressed_and_skips_folder_markers(self):
        client = self._client(
            [
                {
                    "Contents": [
                        {"Key": "legal/", "Size": 0},
                        {
                            "Key": "legal/nda.pdf",
                            "Size": 12,
                            "LastModified": "2026-08-20T09:30:00+00:00",
                        },
                    ]
                }
            ]
        )
        with patch.object(S3Connector, "_get_s3_client", return_value=client):
            listing = await S3Connector().list_files(
                {"bucket": "acme", "prefix": "legal/"}, self._credential()
            )
        files = listing.files

        assert [f.source_path for f in files] == ["s3://acme/legal/nda.pdf"]
        assert files[0].modified_at == datetime.datetime(2026, 8, 20, 9, 30, tzinfo=datetime.UTC)
        client.get_paginator.assert_called_once_with("list_objects_v2")
        client.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="acme", Prefix="legal/"
        )

    async def test_each_objects_etag_is_reported_verbatim_as_its_revision(self):
        """Quotes and the multipart `-<parts>` suffix included. The sync compares
        one listing with an earlier listing of the same API, so a value kept as
        S3 sent it cannot drift between two versions of a transformation. A
        multipart ETag is not an MD5 of the bytes, which does not matter: an
        equal ETag still means unchanged content, and a new one for the same
        bytes costs one download, never a skipped change."""
        client = self._client(
            [
                {
                    "Contents": [
                        {
                            "Key": "legal/nda.pdf",
                            "Size": 12,
                            "ETag": '"9b2cf535f27731c974343645a3985328"',
                        },
                        {
                            "Key": "legal/archive.zip",
                            "Size": 52428800,
                            "ETag": '"d41d8cd98f00b204e9800998ecf8427e-7"',
                        },
                        # An S3-compatible store that sends none.
                        {"Key": "legal/readme.md", "Size": 3},
                    ]
                }
            ]
        )
        with patch.object(S3Connector, "_get_s3_client", return_value=client):
            listing = await S3Connector().list_files({"bucket": "acme"}, self._credential())

        assert [(f.id, f.revision) for f in listing.files] == [
            ("legal/nda.pdf", '"9b2cf535f27731c974343645a3985328"'),
            ("legal/archive.zip", '"d41d8cd98f00b204e9800998ecf8427e-7"'),
            ("legal/readme.md", None),
        ]

    async def test_an_empty_prefix_is_omitted_rather_than_sent_empty(self):
        """What the connector did before it was a subclass, kept deliberately: a
        stored source with no prefix must make the same call it made yesterday."""
        client = self._client([{"Contents": []}])
        with patch.object(S3Connector, "_get_s3_client", return_value=client):
            await S3Connector().list_files({"bucket": "acme", "prefix": ""}, self._credential())

        client.get_paginator.return_value.paginate.assert_called_once_with(Bucket="acme")

    def test_the_listing_is_never_materialized_beside_the_one_it_builds(self):
        """`_objects` yields, and this is the guard on that.

        The shared `_listing` builds the `RemoteFile` list the sync path reads;
        a `_objects` that returned a list of `StoredObject` would hold a second
        copy of the largest thing a sync ever holds, live at the same time - a
        bucket of a million keys is the case where that stops being academic and
        starts being the reason a previously-working sync runs out of memory.
        Asserted on the function rather than on a measurement, because the shape
        is what is easy to revert by accident.
        """
        assert inspect.isgeneratorfunction(S3Connector._objects)

    @staticmethod
    def _remote(size: int | None) -> RemoteFile:
        return RemoteFile(
            id="legal/nda.pdf",
            name="nda.pdf",
            size=size,
            source_path="s3://acme/legal/nda.pdf",
            revision='"listed-etag"',
        )

    async def test_a_small_object_is_one_get_whose_etag_names_its_bytes(self, tmp_path: Path):
        """One request, and the `ETag` comes back with the bytes it describes, so
        nothing can change between the two. Not conditional on the listed one:
        `full` wants whatever the object is now."""
        client = MagicMock()
        client.get_object.return_value = {"Body": io.BytesIO(b"pdf"), "ETag": '"fetched-etag"'}
        with patch.object(S3Connector, "_get_s3_client", return_value=client):
            fetched = await S3Connector().fetch_file(
                self._remote(size=3),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )

        client.get_object.assert_called_once_with(Bucket="acme", Key="legal/nda.pdf")
        assert fetched.revision == '"fetched-etag"'
        assert fetched.path.read_bytes() == b"pdf"

    class _Transfers:
        """s3transfer's `TransferManager`, answering one download."""

        made: ClassVar[list[tuple[str, str, str]]] = []
        thresholds: ClassVar[list[int]] = []

        def __init__(
            self,
            _client: object,
            config: TransferConfig,
            *,
            fails: Exception | None = None,
            reported_size: int = len(b"large"),
            received: int = len(b"large"),
        ):
            self.fails = fails
            self.reported_size = reported_size
            self.received = received
            type(self).thresholds.append(config.multipart_threshold)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def download(
            self, bucket: str, key: str, filename: str, subscribers: list[Any]
        ) -> MagicMock:
            type(self).made.append((bucket, key, filename))

            def result() -> None:
                if self.fails is not None:
                    raise self.fails
                Path(filename).write_bytes(b"large")
                for subscriber in subscribers:
                    subscriber.on_progress(future=None, bytes_transferred=self.received)

            return MagicMock(
                result=result, meta=MagicMock(etag='"pinned-etag-3"', size=self.reported_size)
            )

    @pytest.mark.parametrize("size", [64 * 1024 * 1024, None])
    async def test_a_large_or_unsized_object_takes_the_managed_transfer(
        self, tmp_path: Path, size: int | None
    ):
        """Parallel ranged reads and their retries, which a single `GetObject`
        gave up. Every range after the first is pinned to one `ETag` with
        `IfMatch`, so the `ETag` the transfer reports is the bytes' own."""
        self._Transfers.made = []
        self._Transfers.thresholds = []
        client = MagicMock()
        with (
            patch.object(S3Connector, "_get_s3_client", return_value=client),
            patch("s3transfer.manager.TransferManager", self._Transfers),
        ):
            fetched = await S3Connector().fetch_file(
                self._remote(size=size),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )

        assert self._Transfers.made == [("acme", "legal/nda.pdf", str(tmp_path / "nda.pdf"))]
        # One byte, so the transfer never falls back to its own unpinned
        # `GetObject` for an object that turned out small after all.
        assert self._Transfers.thresholds == [1]
        client.get_object.assert_not_called()
        assert fetched.revision == '"pinned-etag-3"'
        assert fetched.path.read_bytes() == b"large"

    def test_what_a_retried_range_takes_back_is_not_counted(self):
        """s3transfer reports a retried range's lost bytes as negative
        progress. Counted as zero, every transfer with one retry would look
        like more bytes than the object holds, and fail."""
        received = _Received()

        for n in (8, 5, -5, 5):
            received.on_progress(future=None, bytes_transferred=n)

        assert received.bytes == 13

    @staticmethod
    def _store_now_at(etag: str) -> MagicMock:
        """A client whose HEAD answers the object's `ETag` as it is now."""
        client = MagicMock()
        client.head_object.return_value = {"ETag": etag}
        return client

    async def _fetch_through(
        self, tmp_path: Path, client: MagicMock, **transfer: Any
    ) -> FetchedFile:
        def transfers(client: object, config: TransferConfig) -> object:
            return self._Transfers(client, config, **transfer)

        with (
            patch.object(S3Connector, "_get_s3_client", return_value=client),
            patch("s3transfer.manager.TransferManager", transfers),
        ):
            return await S3Connector().fetch_file(
                self._remote(size=64 * 1024 * 1024),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )

    async def test_a_transfer_that_received_too_few_bytes_is_not_stored(self, tmp_path: Path):
        """s3transfer writes a range answered with 416 as nothing and raises
        nothing, so a file can be short or have a hole of zeros at full length.
        Counted by what arrived, not by the file's size, and a failure while
        the object is still the version the transfer was pinned to."""
        with pytest.raises(ExternalServiceError, match="sent 5 of the 67108864 bytes"):
            await self._fetch_through(
                tmp_path,
                self._store_now_at('"pinned-etag-3"'),
                reported_size=64 * 1024 * 1024,
            )

    async def test_a_hole_at_full_length_is_caught_by_what_arrived(self, tmp_path: Path):
        """A middle range answered with 416 after a later one was written: the
        file has its full length and zeros where those bytes belong. Its size
        says nothing; the progress the transfer reported does."""
        with pytest.raises(ExternalServiceError, match="sent 2 of the 5 bytes"):
            await self._fetch_through(
                tmp_path, self._store_now_at('"pinned-etag-3"'), reported_size=5, received=2
            )

    async def test_a_transfer_short_because_the_object_moved_is_changed(self, tmp_path: Path):
        with pytest.raises(ChangedSinceListing):
            await self._fetch_through(
                tmp_path,
                self._store_now_at('"moved-etag-4"'),
                reported_size=64 * 1024 * 1024,
            )

    async def test_an_empty_etag_is_no_revision(self, tmp_path: Path):
        """`""` compared equal to `""` would skip every later version of the
        object: a store that answers it for an object it has not read yet."""
        client = self._client([{"Contents": [{"Key": "a.md", "Size": 3, "ETag": ""}]}])
        client.get_object.return_value = {"Body": io.BytesIO(b"abc"), "ETag": ""}
        with patch.object(S3Connector, "_get_s3_client", return_value=client):
            listing = await S3Connector().list_files({"bucket": "acme"}, self._credential())
            fetched = await S3Connector().fetch_file(
                listing.files[0], tmp_path, config={"bucket": "acme"}, credential=self._credential()
            )

        assert (listing.files[0].revision, fetched.revision) == (None, None)

    @pytest.mark.parametrize(
        "error",
        [
            S3DownloadFailedError('Contents of stored object "legal/nda.pdf" did not match'),
            # Garage answers the code as "412", which s3transfer does not turn
            # into its own error - found against a real Garage, not a mock.
            ClientError(
                {"Error": {"Code": "412"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "GetObject",
            ),
        ],
        ids=["s3transfer", "a raw 412"],
    )
    async def test_an_object_changed_during_its_download_is_changed_not_failed(
        self, tmp_path: Path, error: Exception
    ):
        """A range read after the object moved is refused by its pinned `ETag`.
        That is a version the next listing names, not a broken download."""
        with pytest.raises(ChangedSinceListing):
            await self._fetch_through(tmp_path, self._store_now_at('"moved-etag-4"'), fails=error)

    @pytest.mark.parametrize(
        "error",
        [
            S3DownloadFailedError('Contents of stored object "legal/nda.pdf" did not match'),
            ClientError(
                {"Error": {"Code": "412"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "GetObject",
            ),
        ],
        ids=["s3transfer", "a raw 412"],
    )
    async def test_a_refusal_of_an_unchanged_object_is_a_failure_not_a_skip(
        self, tmp_path: Path, error: Exception
    ):
        """A store or proxy that refuses `IfMatch` for the version it holds.
        Taken for a change, the file would be skipped on every run while the
        sync reported success; the HEAD says it did not move."""
        with pytest.raises(ExternalServiceError, match="refused a read"):
            await self._fetch_through(tmp_path, self._store_now_at('"pinned-etag-3"'), fails=error)

    async def test_a_head_with_no_etag_is_no_evidence_of_a_move(self, tmp_path: Path):
        """A store whose HEAD sends no `ETag`, after a refused pinned read. Read
        as a change, the file would be skipped on every run while the sync
        reported success. With nothing to compare, it is a failure."""
        client = MagicMock()
        client.head_object.return_value = {}
        with pytest.raises(ExternalServiceError, match="refused a read"):
            await self._fetch_through(
                tmp_path,
                client,
                fails=S3DownloadFailedError('Contents of stored object "legal/nda.pdf"'),
            )

    _ABSENT = (
        # A pinned range of a deleted object, as S3 and MinIO answer it.
        pytest.param(
            ClientError(
                {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "GetObject",
            ),
            id="a pinned range",
        ),
        # The transfer's own HEAD, when the object went before it began.
        pytest.param(
            ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            ),
            id="the transfer's HEAD",
        ),
        pytest.param(
            S3DownloadFailedError('Contents of stored object "legal/nda.pdf"'),
            id="a 412 then gone",
        ),
    )

    @staticmethod
    def _found_by_no_head(*, listed: list[str]) -> MagicMock:
        """A client whose HEAD finds no object, and whose listing names `listed`."""
        client = MagicMock()
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"
        )
        client.list_objects_v2.return_value = {"Contents": [{"Key": key} for key in listed]}
        return client

    @pytest.mark.parametrize("error", _ABSENT)
    async def test_an_object_deleted_during_its_download_is_changed(
        self, tmp_path: Path, error: Exception
    ):
        """Gone is a change the next listing settles by leaving the file out -
        not a failure to report. A key that only starts with this one is
        another object, not this one still listed."""
        client = self._found_by_no_head(listed=["legal/nda.pdf.v2"])
        with pytest.raises(ChangedSinceListing, match="deleted"):
            await self._fetch_through(tmp_path, client, fails=error)

        client.list_objects_v2.assert_called_once_with(
            Bucket="acme", Prefix="legal/nda.pdf", MaxKeys=1
        )

    @pytest.mark.parametrize("error", _ABSENT)
    async def test_a_key_the_store_still_lists_is_a_failure_not_a_skip(
        self, tmp_path: Path, error: Exception
    ):
        """A 404 for a key the listing still names: a proxy that rewrites the
        path, a key the store decodes one way to list and another to read. The
        HEAD fails the same way and the listing does not. Read as a deletion,
        the file would be skipped on every run while the sync reported success."""
        client = self._found_by_no_head(listed=["legal/nda.pdf"])
        with pytest.raises(ExternalServiceError, match=r"lists nda\.pdf"):
            await self._fetch_through(tmp_path, client, fails=error)

    async def test_any_other_error_of_the_transfer_is_raised_as_it_is(self, tmp_path: Path):
        """Only a refusal or an absence is asked about. A denied read is the
        source's configuration, and no HEAD should turn it into a change."""
        client = MagicMock()
        denied = ClientError(
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            "GetObject",
        )
        with pytest.raises(ClientError, match="AccessDenied"):
            await self._fetch_through(tmp_path, client, fails=denied)

        client.head_object.assert_not_called()

    @staticmethod
    def _read_as_absent(code: str, *, listed: list[str]) -> MagicMock:
        """A client whose `GetObject` answers 404 with `code`, and whose listing names `listed`."""
        client = MagicMock()
        client.get_object.side_effect = ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject"
        )
        client.list_objects_v2.return_value = {"Contents": [{"Key": key} for key in listed]}
        return client

    async def _read_whole(self, tmp_path: Path, client: MagicMock) -> FetchedFile:
        with patch.object(S3Connector, "_get_s3_client", return_value=client):
            return await S3Connector().fetch_file(
                self._remote(size=3),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )

    @pytest.mark.parametrize("code", ["NoSuchKey", "404"], ids=["S3's code", "a 404 with no body"])
    async def test_a_small_object_gone_since_its_listing_is_changed(
        self, tmp_path: Path, code: str
    ):
        """Decided by the status, as the managed transfer decides it: a 404 with
        no body reaches botocore with the code "404", and read by its code, a
        small object deleted after its listing was a failure where a large one
        was a skip. Then by a listing that no longer names the key."""
        client = self._read_as_absent(code, listed=["legal/nda.pdf.v2"])
        with pytest.raises(ChangedSinceListing, match="deleted before"):
            await self._read_whole(tmp_path, client)

        client.list_objects_v2.assert_called_once_with(
            Bucket="acme", Prefix="legal/nda.pdf", MaxKeys=1
        )

    async def test_a_small_object_the_store_still_lists_is_a_failure(self, tmp_path: Path):
        """The same 404, for a key the listing still names, is a read the store
        cannot serve. It fails on every run, so a skip would hide it for good."""
        client = self._read_as_absent("NoSuchKey", listed=["legal/nda.pdf"])
        with pytest.raises(ClientError, match="NoSuchKey"):
            await self._read_whole(tmp_path, client)

    async def test_a_small_object_in_a_bucket_that_is_gone_is_a_failure(self, tmp_path: Path):
        """The bucket is the source's configuration, and a sync that lost it has
        to say so: the listing that would confirm a deletion raises too."""
        client = self._read_as_absent("NoSuchBucket", listed=[])
        client.list_objects_v2.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "ListObjectsV2",
        )
        with pytest.raises(ClientError, match="NoSuchBucket"):
            await self._read_whole(tmp_path, client)

    async def test_a_412_on_the_single_get_is_raised_as_it_is(self, tmp_path: Path):
        """That read is conditional on nothing, so no change can be behind it."""
        client = MagicMock()
        client.get_object.side_effect = ClientError(
            {"Error": {"Code": "412"}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "GetObject"
        )
        with (
            patch.object(S3Connector, "_get_s3_client", return_value=client),
            pytest.raises(ClientError),
        ):
            await S3Connector().fetch_file(
                self._remote(size=3),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )

    @staticmethod
    def _breaking_off() -> dict[str, Any]:
        """A body that sends two bytes, then times out."""
        broken = MagicMock(side_effect=[b"pd", ReadTimeoutError(endpoint_url="store")])
        return {"Body": MagicMock(read=broken), "ETag": '"first-try"'}

    async def test_a_small_object_whose_body_breaks_off_is_read_again(self, tmp_path: Path):
        """botocore retries a request only until its headers arrive. The managed
        transfer used to read a body again after a timeout, and one `GetObject`
        must not fail a file the old download recovered."""
        client = MagicMock()
        client.get_object.side_effect = [
            self._breaking_off(),
            {"Body": io.BytesIO(b"pdf"), "ETag": '"second-try"'},
        ]
        with patch.object(S3Connector, "_get_s3_client", return_value=client):
            fetched = await S3Connector().fetch_file(
                self._remote(size=3),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )

        assert client.get_object.call_count == 2
        # Written from the start again, so the first try's two bytes are gone.
        assert (fetched.path.read_bytes(), fetched.revision) == (b"pdf", '"second-try"')

    async def test_a_body_that_keeps_breaking_off_fails_after_the_retries(self, tmp_path: Path):
        client = MagicMock()
        client.get_object.side_effect = lambda **_: self._breaking_off()
        with (
            patch.object(S3Connector, "_get_s3_client", return_value=client),
            pytest.raises(ReadTimeoutError),
        ):
            await S3Connector().fetch_file(
                self._remote(size=3),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )

        assert client.get_object.call_count == TransferConfig().num_download_attempts

    async def test_any_other_refusal_is_raised_as_it_is(self, tmp_path: Path):
        client = MagicMock()
        client.get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            "GetObject",
        )
        with (
            patch.object(S3Connector, "_get_s3_client", return_value=client),
            pytest.raises(ClientError),
        ):
            await S3Connector().fetch_file(
                self._remote(size=3),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )
