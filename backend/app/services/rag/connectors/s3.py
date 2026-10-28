"""S3/MinIO sync connector for RAG ingestion.

The credential is an `AwsCredentialsSecret` in the organization's vault, named
by the source's `secret_id` and unsealed by whoever runs the sync. It used to be
`access_key_id` and `secret_access_key` fields inside the source's own `config`,
encrypted with one deployment-wide Fernet key (#937).

`endpoint_url` and `region` stay in the config and still fall back to the
`S3_RAG_*` settings: neither names a principal.

What is left here is boto3's vocabulary and nothing else - the listing loop, the
`source_path` shape and the destination are `ObjectStoreConnector`'s, so an Azure
or GCS connector is a client rather than a second copy of them (#988).
"""

import logging
import shutil
import threading
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.exceptions import BadRequestError, ExternalServiceError
from app.core.secret_kinds import AwsCredentialsSecret, SecretKind, StorableSecret
from app.services.rag.connectors import ChangedSinceListing, ConnectorConfig
from app.services.rag.connectors.object_store import ObjectStoreConnector, StoredObject

if TYPE_CHECKING:
    from botocore.client import BaseClient

logger = logging.getLogger(__name__)


class _Received:
    """The bytes an s3transfer download received, as its progress reports them.

    A subscriber in s3transfer's sense by shape, not by base class, so this
    module does not import s3transfer wherever it is imported (#2225).
    s3transfer reports each chunk it writes and takes back what a retried range
    had sent, so the sum is what arrived - which a range answered with 416, and
    written as nothing, never adds to.
    """

    def __init__(self) -> None:
        self.bytes = 0
        self._lock = threading.Lock()

    def on_progress(self, future: object, bytes_transferred: int, **_: object) -> None:
        with self._lock:
            self.bytes += bytes_transferred


class S3Config(BaseModel):
    """Where an S3-compatible source's documents live.

    The key pair is not here - it is an `AwsCredentialsSecret` the source names
    in `secret_id` (#937). Only `bucket` is required; `endpoint_url` and `region`
    fall back to the `S3_RAG_*` settings server-side, so their config default is a
    placeholder rather than an authoritative value and rides on `x-placeholder`,
    which `SchemaForm` shows as a grey hint instead of the field's value.
    """

    bucket: str = Field(title="Bucket Name")
    prefix: str | None = Field(
        default=None,
        title="Path Prefix",
        description="e.g. 'documents/legal/' - leave empty for entire bucket",
    )
    endpoint_url: str | None = Field(
        default=None,
        title="Custom Endpoint URL",
        description="For MinIO or compatible services (e.g., http://minio:9000). Leave empty for AWS S3.",
    )
    region: str | None = Field(
        default=None, title="Region", json_schema_extra={"x-placeholder": "us-east-1"}
    )


class S3Connector(ObjectStoreConnector):
    """S3-compatible sync connector.

    Works with AWS S3, MinIO, and any S3-compatible storage.

    The key pair is an `AwsCredentialsSecret` unsealed from the organization's
    vault, named by the source's `secret_id`. It used to sit in the per-source
    `config` dict encrypted with one deployment-wide key (#937). The endpoint and
    region still fall back to `S3_RAG_*`: neither names a principal, they say
    where the store is rather than who is asking.
    """

    CONNECTOR_TYPE: ClassVar[str] = "s3"
    DISPLAY_NAME: ClassVar[str] = "S3 / MinIO"
    SECRET_KIND: ClassVar[SecretKind] = SecretKind.AWS_CREDENTIALS
    SCHEME: ClassVar[str] = "s3"
    CONTAINER_FIELD: ClassVar[str] = "bucket"
    CONFIG_MODEL: ClassVar[type[BaseModel]] = S3Config

    def _get_s3_client(
        self, config: ConnectorConfig, credential: StorableSecret | None
    ) -> BaseClient:
        """Build a boto3 S3 client from the vault secret the source names.

        The key and secret come from the organization's vault and nowhere else.
        They used to fall back to `S3_RAG_ACCESS_KEY` / `S3_RAG_SECRET_KEY`, and
        that is the same shape removed from the Drive connector alongside it: a
        fallback means the caller's `bucket` chooses what is read under the
        *operator's* identity rather than their own, which turns one field of a
        source's configuration into a reach across organizations. Worse than the
        Drive case, because both settings default to empty - so the fallback
        resolved to `None`, boto3 fell through to the container's own credential
        chain, and the reach was whatever the task role could see.

        Only the endpoint and region still fall back. Neither names a principal;
        they say where the store is, not who is asking. The region prefers the
        credential's own, since an AWS key pair is issued against one.

        Raises:
            BadRequestError: the source names no credential, its secret has been
                deleted, or the secret is not an AWS key pair.
        """
        if credential is None:
            raise BadRequestError(
                message=(
                    "This S3 source has no credential. Pick an AWS key pair in the "
                    "Vault and point the source at it."
                )
            )
        if not isinstance(credential, AwsCredentialsSecret):
            raise BadRequestError(
                message=("An S3 source needs an AWS key pair, and the one it names is not one.")
            )
        client_kwargs: dict[str, Any] = {
            "aws_access_key_id": credential.aws_access_key_id,
            "aws_secret_access_key": credential.aws_secret_access_key.get_secret_value(),
            "region_name": config.get("region") or credential.region_name or settings.S3_RAG_REGION,
        }
        if credential.aws_session_token is not None:
            client_kwargs["aws_session_token"] = credential.aws_session_token.get_secret_value()
        endpoint = config.get("endpoint_url") or settings.S3_RAG_ENDPOINT
        if endpoint:
            client_kwargs["endpoint_url"] = endpoint
        # Imported where a client is built: boto3 is the S3 sources' alone, and
        # every API worker and background worker imports this module (#2225).
        import boto3
        from botocore.config import Config

        return boto3.client("s3", **client_kwargs, config=Config(signature_version="s3v4"))

    def _objects(
        self,
        container: str,
        prefix: str,
        config: ConnectorConfig,
        credential: StorableSecret | None,
    ) -> Iterator[StoredObject]:
        """One page at a time, as `list_objects_v2` hands them over.

        A generator, so a bucket of a million keys is one page in memory rather
        than a list of all of them beside the one the caller is building.

        `Prefix` is omitted rather than sent empty, which is what the connector
        did before this became a subclass: boto3 accepts `Prefix=""` and means
        the same thing by it, and the two are kept the same call so a stored
        source lists exactly what it listed yesterday.

        boto3 hands `LastModified` over as a `datetime`; the string branch came
        with this loop and stays with it, in the client's own method, because
        which of the two arrives is the client's own vocabulary.

        `ETag` is kept as S3 sends it, double quotes included: the sync compares
        it with an earlier listing from this same call, so a value left verbatim
        cannot differ between two versions of a transformation. An
        S3-compatible store that sends none gives `None`, and that file takes
        the download and the hash.
        """
        client = self._get_s3_client(config, credential)
        params: dict[str, Any] = {"Bucket": container}
        if prefix:
            params["Prefix"] = prefix

        for page in client.get_paginator("list_objects_v2").paginate(**params):
            for obj in page.get("Contents", []):
                modified_at = obj.get("LastModified")
                if isinstance(modified_at, str):
                    modified_at = datetime.fromisoformat(modified_at)
                yield StoredObject(
                    key=obj["Key"],
                    size=obj.get("Size"),
                    modified_at=modified_at,
                    # An empty `ETag` is no revision: a store that sends `""`
                    # for an object it has not read yet would make every
                    # version of it look the same.
                    etag=obj.get("ETag") or None,
                )

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
        """One `GetObject` for a small object, the managed transfer for the rest.

        A small object is one request, so its `ETag` arrives with the bytes it
        describes, and the full-object checksum botocore checks on a whole
        `GetObject` still applies. A large or unsized one goes through
        s3transfer, which reads it in parallel ranges with retries and pins
        every range to one `ETag` with `IfMatch`: the `ETag` it reports is then
        the bytes' own. Neither read is conditional on the listed `ETag`, so
        `full` reads whatever the object is now.

        A pinned transfer that is refused, or that received fewer bytes than the
        object holds, asks the store once more before it says why: an object
        replaced mid-download looks the same as a store or proxy that mishandles
        `IfMatch`, and taken for the first, the second would skip the file on
        every run while the sync reported success. s3transfer turns S3's
        `PreconditionFailed` into `S3DownloadFailedError`, but not a store that
        names the code "412", as Garage does, so a raw `ClientError` is checked
        by its status.

        Raises:
            ChangedSinceListing: the object changed, or went, during the transfer.
            ExternalServiceError: the store refused a read pinned to its own
                current `ETag`, or sent fewer bytes than it says the object holds.
        """
        # Imported here for the reason `_get_s3_client` gives (#2225).
        from boto3.s3.transfer import TransferConfig
        from botocore.exceptions import ClientError
        from s3transfer.exceptions import S3DownloadFailedError
        from s3transfer.manager import TransferManager

        client = self._get_s3_client(config, credential)
        transfer = TransferConfig()
        if size is not None and size < transfer.multipart_threshold:
            return self._get_whole(
                client,
                container,
                key,
                dest_path,
                attempts=transfer.num_download_attempts,
                chunk_size=transfer.io_chunksize,
            )
        name = Path(key).name
        received = _Received()
        future = None
        try:
            # A threshold of one byte, so every read is ranged and pinned:
            # s3transfer reads an object under its threshold in one `GetObject`
            # without `IfMatch` and still reports its HEAD's `ETag`, and an object
            # listed large, or listed with no size, may be small by now.
            with TransferManager(client, TransferConfig(multipart_threshold=1)) as manager:
                future = manager.download(container, key, str(dest_path), subscribers=[received])
                future.result()
        except (S3DownloadFailedError, ClientError) as exc:
            # A 404 too: a pinned read of an object deleted since is answered
            # `NoSuchKey`, not 412, and so is the transfer's own HEAD when the
            # object went before it began.
            refused = isinstance(exc, S3DownloadFailedError) or (
                exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (404, 412)
            )
            if not refused:
                raise
            pinned = future.meta.etag if future is not None else None
            raise self._unread(
                client,
                container,
                key,
                pinned,
                f"The store refused a read of {name} pinned to its own current ETag.",
            ) from exc
        # s3transfer writes a range the store answers with 416 as nothing and
        # raises no error, so a store that checks the range before `IfMatch`
        # leaves a short file, or a hole of zeros where a middle range was.
        # Stored under the `ETag` the transfer began with, it would be skipped
        # for good once that version came back.
        expected = future.meta.size
        if expected is not None and received.bytes != expected:
            raise self._unread(
                client,
                container,
                key,
                future.meta.etag,
                f"The store sent {received.bytes} of the {expected} bytes {name} holds.",
            )
        return future.meta.etag or None

    @staticmethod
    def _get_whole(
        client: BaseClient,
        container: str,
        key: str,
        dest_path: Path,
        *,
        attempts: int,
        chunk_size: int,
    ) -> str | None:
        """One `GetObject`, retried as s3transfer retries a read, answering its `ETag`.

        botocore retries a request only until its headers arrive, and a body
        that stops halfway - a read timeout, a dropped connection - would fail
        the file where the managed transfer used to try again. Each attempt is
        a whole response, written from the start of the file, so the `ETag`
        returned is the bytes' own.

        Raises:
            ChangedSinceListing: the object was deleted after it was listed.
        """
        from botocore.exceptions import ClientError
        from s3transfer.utils import S3_RETRYABLE_DOWNLOAD_ERRORS

        attempt = 1
        while True:
            try:
                response = client.get_object(Bucket=container, Key=key)
                with closing(response["Body"]) as body, dest_path.open("wb") as out:
                    shutil.copyfileobj(body, out, chunk_size)
                return response.get("ETag") or None
            except ClientError as exc:
                # Listed, and gone by the time it was read: the next listing
                # leaves it out and the sync removes its document. Not
                # `NoSuchBucket`, which is the source's own configuration.
                if exc.response.get("Error", {}).get("Code") != "NoSuchKey":
                    raise
                raise ChangedSinceListing(
                    f"{Path(key).name} was deleted before it was downloaded."
                ) from exc
            except S3_RETRYABLE_DOWNLOAD_ERRORS:
                if attempt >= attempts:
                    raise
                logger.debug("Retrying the download of %s, attempt %d", key, attempt + 1)
                attempt += 1

    @staticmethod
    def _unread(
        client: BaseClient, container: str, key: str, pinned: str | None, refusal: str
    ) -> ChangedSinceListing | ExternalServiceError:
        """Why a pinned transfer did not deliver its version, asked of the store.

        Moved when the object's `ETag` is no longer the pinned one, or the
        object is gone. Otherwise the store failed a read of a version it still
        holds, which is a failure for the sync to count and show, not a change.
        """
        from botocore.exceptions import ClientError

        name = Path(key).name
        try:
            current = client.head_object(Bucket=container, Key=key).get("ETag")
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            return ChangedSinceListing(f"{name} was deleted while it was downloaded.")
        if pinned is not None and current != pinned:
            return ChangedSinceListing(f"{name} changed while it was downloaded.")
        return ExternalServiceError(message=refusal, details={"etag": pinned})
