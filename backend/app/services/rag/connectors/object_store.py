"""What every object store's connector does identically.

S3, Azure Blob Storage and Google Cloud Storage are one connector with three
clients: a container, a prefix, a credential, and a flat listing of objects with
a size and a modified time. #938 put the other two fifth on the list with a
condition attached - *"if `S3Connector` is refactored to an object-store shape,
these are configuration rather than code"* - and this is that shape, written
before either of them rather than after, because the alternative is three
connectors sharing a form by resemblance (#988).

What a subclass supplies is a client and its SDK's vocabulary: how a listing
paginates, and how one object is written to a path. What it inherits is
everything a reader of three connectors would otherwise have to check three
times - that a key ending in `/` is a directory marker and not a document, that
`source_path` is `<scheme>://<container>/<key>` and is the dedup key the whole
sync path matches on, and that the destination is the base class's answer rather
than the connector's (`BaseSyncConnector.download_file`, which is inherited for
the reason its own docstring gives).
"""

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from app.core.secret_kinds import StorableSecret
from app.services.rag.connectors import (
    BaseSyncConnector,
    ConnectorConfig,
    RemoteFile,
    RemoteListing,
)

logger = logging.getLogger(__name__)


class StoredObject(BaseModel):
    """One object in a listing, in the terms all three stores describe one in.

    `etag` is the object's revision: the sync compares it with the one
    stored beside the document and skips the download when they are equal, which
    saves the transfer and not only the embedding. Every object store has one,
    and it changes whenever the content does. It is not always a hash of the
    bytes: an S3 multipart upload's `ETag` is the MD5 of its parts' MD5s with a
    `-<parts>` suffix, and an SSE-KMS or SSE-C object's is no MD5 at all. So the
    same bytes uploaded again with another part size, or copied, can get a new
    one. That costs one download, after which the hash finds the content
    unchanged and the stored revision is refreshed - never a skipped change.
    What is stored is the `ETag` `_download` reports for the bytes it read, so
    an object replaced after the listing is filed under its own version. An
    `ETag` is only as exact as the store behind it: an object changed outside
    its S3 API - a SeaweedFS filer or FUSE write - can keep the old one, and
    `full` is the remedy.

    `modified_at` is carried because `RemoteFile` already has somewhere to put
    it.
    """

    key: str
    size: int | None = None
    modified_at: datetime | None = None
    etag: str | None = None


class ObjectStoreConnector(BaseSyncConnector):
    """A connector for a store addressed by container and key.

    Two hooks, and both are **blocking**, run on a worker thread by the methods
    below: boto3, `azure-storage-blob` and `google-cloud-storage` all ship
    synchronous clients, and a hook declared `async` over one of those would
    either block the event loop or be wrapped twice.
    """

    SCHEME: ClassVar[str] = ""
    """What a `source_path` from this store starts with - `s3`, `gs`, `azblob`."""

    CONTAINER_FIELD: ClassVar[str] = "bucket"
    """Which `CONFIG_MODEL` field names the container. S3 and GCS call it a
    bucket; Azure calls it a container, and the form should say what the store's
    own console says."""

    async def list_files(
        self, config: ConnectorConfig, credential: StorableSecret | None
    ) -> RemoteListing:
        """Every document under the configured prefix, newest state as listed.

        Complete by construction: the pagination either reaches the last page or
        raises, so there is no partial answer for a sync to mistake for a whole one.
        """
        files = await asyncio.to_thread(
            self._listing,
            config[self.CONTAINER_FIELD],
            config.get("prefix", ""),
            config,
            credential,
        )
        return RemoteListing(files=files)

    def _listing(
        self,
        container: str,
        prefix: str,
        config: ConnectorConfig,
        credential: StorableSecret | None,
    ) -> list[RemoteFile]:
        """The listing converted as it arrives, on the thread that fetched it.

        One list in memory rather than two: `_objects` yields, so a bucket of a
        million keys holds the `RemoteFile` list this returns and one
        `StoredObject` at a time - which is what the connector held before it had
        a shared class, and a peak worth not doubling on the largest sync anyone
        runs. Converting here rather than in `list_files` is what keeps the
        pagination *inside* the thread; a generator handed back to the event loop
        would do the next page's network round trip on it.

        A key ending in `/` is skipped here rather than in each client: a console
        that creates a "folder" writes a zero-byte object with that name, and
        ingesting one is a document with no bytes and a name of `''`.
        """
        return [
            RemoteFile(
                id=obj.key,
                name=Path(obj.key).name,
                size=obj.size,
                modified_at=obj.modified_at,
                revision=obj.etag,
                source_path=f"{self.SCHEME}://{container}/{obj.key}",
            )
            for obj in self._objects(container, prefix, config, credential)
            if not obj.key.endswith("/")
        ]

    async def _fetch(
        self,
        file: RemoteFile,
        dest_path: Path,
        config: ConnectorConfig,
        credential: StorableSecret | None,
    ) -> str | None:
        """Write one object to the path the base class chose, and answer its `ETag`."""
        container = file.source_path.removeprefix(f"{self.SCHEME}://").split("/", 1)[0]
        etag = await asyncio.to_thread(
            self._download, container, file.id, dest_path, config, credential, size=file.size
        )
        logger.info(
            "Downloaded %s://%s/%s (%d bytes)",
            self.SCHEME,
            container,
            file.id,
            dest_path.stat().st_size,
        )
        return etag

    @abstractmethod
    def _objects(
        self,
        container: str,
        prefix: str,
        config: ConnectorConfig,
        credential: StorableSecret | None,
    ) -> Iterable[StoredObject]:
        """This store's listing, paginated the way its SDK paginates.

        Blocking, consumed on a worker thread by `_listing`, and a **generator**
        rather than a list wherever the SDK pages: nothing needs every object at
        once, and materializing them is a second copy of the largest thing a sync
        holds.
        """

    @abstractmethod
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
        """Write one object's bytes to `dest_path`, and answer the `ETag` of those bytes.

        Blocking, on a worker thread. The `ETag` comes from the responses that
        carried the bytes, never from the listing: the object may have changed
        since, and the sync stores this one beside the document. A download that
        spans several requests pins them to one version, and raises
        `ChangedSinceListing` when the store refuses a later one because the
        object moved. `size` is the listed one, a hint for how to fetch.
        """
