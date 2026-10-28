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
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, SecretStr

from app.core.exceptions import BadRequestError
from app.core.secret_kinds import AwsCredentialsSecret, StorableSecret
from app.services.rag.connectors import ConnectorConfig, RemoteFile
from app.services.rag.connectors.object_store import ObjectStoreConnector, StoredObject
from app.services.rag.connectors.s3 import S3Connector

pytestmark = pytest.mark.anyio


class BlobConfig(BaseModel):
    container: str = Field(title="Container")


class BlobConnector(ObjectStoreConnector):
    """A store that calls its container a container, as Azure's console does."""

    CONNECTOR_TYPE: ClassVar[str] = "blob"
    SCHEME: ClassVar[str] = "azblob"
    CONTAINER_FIELD: ClassVar[str] = "container"
    CONFIG_MODEL: ClassVar[type[BaseModel]] = BlobConfig

    def __init__(self, objects: list[StoredObject] | None = None) -> None:
        self.objects = objects or []
        self.listed: list[tuple[str, str]] = []
        self.downloaded: list[tuple[str, str, Path]] = []
        self.fetched_etags: list[str | None] = []

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
        etag: str | None,
    ) -> None:
        self.downloaded.append((container, key, dest_path))
        self.fetched_etags.append(etag)
        dest_path.write_bytes(b"contents")


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

        landed = await connector.download_file(
            RemoteFile(
                id="legal/nda.pdf", name="nda.pdf", source_path="azblob://docs/legal/nda.pdf"
            ),
            tmp_path,
            config={"container": "ignored-here"},
        )

        assert connector.downloaded == [("docs", "legal/nda.pdf", tmp_path / "nda.pdf")]
        assert landed.read_bytes() == b"contents"

    async def test_the_download_asks_for_the_version_the_listing_reported(self, tmp_path: Path):
        """The sync files what `_download` wrote under the listed revision, so
        the store has to be told which version that is - an object replaced
        after the listing must not be stored under the old `ETag`."""
        connector = BlobConnector()

        await connector.download_file(
            RemoteFile(
                id="nda.pdf",
                name="nda.pdf",
                source_path="azblob://docs/nda.pdf",
                revision='"0x8DC1A2B3C4D5E6F"',
            ),
            tmp_path,
        )

        assert connector.fetched_etags == ['"0x8DC1A2B3C4D5E6F"']

    async def test_a_name_climbing_out_of_the_sync_directory_is_refused(self, tmp_path: Path):
        """Inherited from `BaseSyncConnector.download_file`, and asserted here
        because it is the property a new store most easily loses: the destination
        is not the connector's to choose."""
        connector = BlobConnector()

        with pytest.raises(BadRequestError):
            await connector.download_file(
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

    async def test_a_download_asks_the_client_for_the_key_it_listed(self, tmp_path: Path):
        client = MagicMock()
        client.download_file.side_effect = lambda bucket, key, path: Path(path).write_bytes(b"pdf")
        with patch.object(S3Connector, "_get_s3_client", return_value=client):
            landed = await S3Connector().download_file(
                RemoteFile(
                    id="legal/nda.pdf", name="nda.pdf", source_path="s3://acme/legal/nda.pdf"
                ),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )

        client.download_file.assert_called_once_with("acme", "legal/nda.pdf", str(landed))
        assert landed.read_bytes() == b"pdf"

    async def test_a_download_with_a_listed_etag_is_conditional_on_it(self, tmp_path: Path):
        """`download_file` takes no `IfMatch`, so a listed `ETag` means a
        `GetObject` that S3 itself refuses for any other version."""
        client = MagicMock()
        client.get_object.return_value = {"Body": io.BytesIO(b"pdf")}
        with patch.object(S3Connector, "_get_s3_client", return_value=client):
            landed = await S3Connector().download_file(
                RemoteFile(
                    id="legal/nda.pdf",
                    name="nda.pdf",
                    source_path="s3://acme/legal/nda.pdf",
                    revision='"9b2cf535f27731c974343645a3985328"',
                ),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )

        client.get_object.assert_called_once_with(
            Bucket="acme", Key="legal/nda.pdf", IfMatch='"9b2cf535f27731c974343645a3985328"'
        )
        client.download_file.assert_not_called()
        assert landed.read_bytes() == b"pdf"

    async def test_an_object_changed_since_its_listing_fails_its_download(self, tmp_path: Path):
        """Raised, not swallowed: the sync counts the file as failed, stores no
        revision for bytes it never read, and the next listing has the new one."""
        client = MagicMock()
        client.get_object.side_effect = ClientError(
            {
                "Error": {
                    "Code": "PreconditionFailed",
                    "Message": "At least one of the pre-conditions you specified did not hold",
                }
            },
            "GetObject",
        )
        with (
            patch.object(S3Connector, "_get_s3_client", return_value=client),
            pytest.raises(ClientError),
        ):
            await S3Connector().download_file(
                RemoteFile(
                    id="legal/nda.pdf",
                    name="nda.pdf",
                    source_path="s3://acme/legal/nda.pdf",
                    revision='"9b2cf535f27731c974343645a3985328"',
                ),
                tmp_path,
                config={"bucket": "acme"},
                credential=self._credential(),
            )

        assert not (tmp_path / "nda.pdf").exists()
