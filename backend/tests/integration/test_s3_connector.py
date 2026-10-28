"""The S3 sync connector against a real object store: what a revision is.

The unit tests next door assert what the connector *asks* a fake client for.
These ask a real S3-compatible store the questions the revision depends on: is
the `ETag` a listing reports the one a download of the same object reports, does
a download say which version it read when the object changed after the listing,
and does an object replaced in the middle of a multipart download fail as
changed rather than mix two versions. A mock answers whatever it is told to.

MinIO is the store, behind the `objectstore` compose profile:

    make docker-minio

Skipped when nothing answers on `FILE_STORAGE_S3_ENDPOINT` (default
`http://localhost:9000`), as `test_s3_file_storage.py` is.
"""

import io
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from botocore.client import BaseClient
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from pydantic import SecretStr

from app.core.exceptions import ExternalServiceError
from app.core.secret_kinds import AwsCredentialsSecret, StorableSecret
from app.services.rag.connectors import ChangedSinceListing, ConnectorConfig
from app.services.rag.connectors.s3 import S3Connector

pytestmark = pytest.mark.anyio

ENDPOINT = os.environ.get("FILE_STORAGE_S3_ENDPOINT", "http://localhost:9000")
ACCESS_KEY = os.environ.get("FILE_STORAGE_S3_ACCESS_KEY", "minioadmin")
SECRET_KEY = os.environ.get("FILE_STORAGE_S3_SECRET_KEY", "minioadmin")
# A store that serves one region refuses a request signed for another, and a
# `HEAD` - the managed transfer's first request - has no body for botocore's
# redirect to read the right one from. Garage serves `garage`.
REGION = os.environ.get("FILE_STORAGE_S3_REGION", "us-east-1")
MIB = 1024 * 1024
# Over s3transfer's 8 MiB threshold and uploaded in 5 MiB parts, so the object
# is stored multipart (an `ETag` ending `-4`) and downloaded in ranges.
LARGE = 17 * MIB


def _client() -> BaseClient:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name=REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 1},
            connect_timeout=2,
            read_timeout=10,
        ),
    )


def _put_multipart(client: BaseClient, bucket: str, key: str, body: bytes) -> None:
    from boto3.s3.transfer import TransferConfig

    client.upload_fileobj(
        io.BytesIO(body),
        bucket,
        key,
        Config=TransferConfig(multipart_threshold=5 * MIB, multipart_chunksize=5 * MIB),
    )


@pytest.fixture(scope="module")
def bucket() -> Iterator[str]:
    """A bucket of this run's own, emptied and removed afterwards."""
    from botocore.exceptions import BotoCoreError, ClientError

    client = _client()
    name = f"agenticos-test-{uuid.uuid4().hex[:12]}"
    try:
        client.create_bucket(Bucket=name)
    except (BotoCoreError, ClientError, OSError) as exc:
        pytest.skip(
            f"no S3-compatible store at {ENDPOINT} ({type(exc).__name__}) - make docker-minio"
        )
    try:
        yield name
    finally:
        for item in client.list_objects_v2(Bucket=name).get("Contents", []):
            client.delete_object(Bucket=name, Key=item["Key"])
        client.delete_bucket(Bucket=name)


@pytest.fixture
def prefix() -> str:
    """A key prefix of the test's own, so each lists only what it wrote."""
    return f"{uuid.uuid4().hex[:8]}/"


def _config(bucket: str, prefix: str) -> ConnectorConfig:
    return {"bucket": bucket, "prefix": prefix, "endpoint_url": ENDPOINT, "region": REGION}


def _credential() -> StorableSecret:
    return AwsCredentialsSecret(
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SecretStr(SECRET_KEY),
        region_name=REGION,
    )


async def _listed(bucket: str, prefix: str) -> dict[str, Any]:
    listing = await S3Connector().list_files(_config(bucket, prefix), _credential())
    return {f.id: f for f in listing.files}


@pytest.mark.parametrize("size", [64, LARGE], ids=["single-part", "multipart"])
async def test_a_download_reports_the_etag_its_listing_did(
    bucket: str, prefix: str, tmp_path: Path, size: int
) -> None:
    """What the skip compares: the listed `ETag` against the one stored, which
    a download reported. Unequal for the same object, nothing would ever skip;
    equal, a file listed unchanged is one whose stored bytes are current."""
    body = os.urandom(size)
    key = f"{prefix}report.bin"
    if size > 8 * MIB:
        _put_multipart(_client(), bucket, key, body)
    else:
        _client().put_object(Bucket=bucket, Key=key, Body=body)

    listed = (await _listed(bucket, prefix))[key]
    fetched = await S3Connector().fetch_file(
        listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
    )

    assert listed.revision is not None
    assert fetched.revision == listed.revision
    assert fetched.path.read_bytes() == body
    assert listed.revision.endswith('-4"') == (size > 8 * MIB)


async def test_an_object_replaced_after_its_listing_reports_its_new_version(
    bucket: str, prefix: str, tmp_path: Path
) -> None:
    """The listing is minutes old by the time a large source reaches a file.
    The download reads what the object is now and says so, so the new bytes
    are never stored under the old `ETag`."""
    client = _client()
    key = f"{prefix}policy.md"
    client.put_object(Bucket=bucket, Key=key, Body=b"the policy, before")
    listed = (await _listed(bucket, prefix))[key]
    client.put_object(Bucket=bucket, Key=key, Body=b"the policy, after an edit")

    fetched = await S3Connector().fetch_file(
        listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
    )

    assert fetched.path.read_bytes() == b"the policy, after an edit"
    assert fetched.revision not in (None, listed.revision)
    assert fetched.revision == (await _listed(bucket, prefix))[key].revision


async def test_an_object_listed_large_and_now_small_is_read_pinned(
    bucket: str, prefix: str, tmp_path: Path
) -> None:
    """The listed size picks the managed transfer, and the object is small by
    the time it is read. s3transfer would read a small object in one unpinned
    `GetObject` and report its HEAD's `ETag`; with every read ranged and pinned,
    what is reported is the version the bytes are."""
    client = _client()
    key = f"{prefix}shrunk.bin"
    _put_multipart(client, bucket, key, os.urandom(LARGE))
    listed = (await _listed(bucket, prefix))[key]
    client.put_object(Bucket=bucket, Key=key, Body=b"small now")

    fetched = await S3Connector().fetch_file(
        listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
    )

    assert fetched.path.read_bytes() == b"small now"
    assert fetched.revision == (await _listed(bucket, prefix))[key].revision
    assert fetched.revision != listed.revision


async def test_an_object_replaced_during_a_multipart_download_is_changed(
    bucket: str, prefix: str, tmp_path: Path
) -> None:
    """The ranges after the first are pinned to its `ETag` with `IfMatch`, so a
    replacement between two of them is refused by the store - never a file of
    one version's first part and another's rest."""
    client = _client()
    key = f"{prefix}archive.bin"
    _put_multipart(client, bucket, key, os.urandom(LARGE))
    listed = (await _listed(bucket, prefix))[key]
    real = S3Connector._get_s3_client
    gets: list[int] = []
    lock = threading.Lock()

    def replacing_before_the_second_range(
        self: S3Connector, config: ConnectorConfig, credential: StorableSecret | None
    ) -> BaseClient:
        connector_client = real(self, config, credential)

        # Before the second range is sent, not after the first returns: the
        # ranges go out in parallel, and only this order makes the refusal
        # certain rather than a race this test would sometimes lose.
        def replace(**_: object) -> None:
            with lock:
                gets.append(len(gets) + 1)
                if len(gets) == 2:
                    client.put_object(Bucket=bucket, Key=key, Body=b"replaced mid-download")

        connector_client.meta.events.register("before-call.s3.GetObject", replace)
        return connector_client

    with (
        patch.object(S3Connector, "_get_s3_client", replacing_before_the_second_range),
        pytest.raises(ChangedSinceListing),
    ):
        await S3Connector().fetch_file(
            listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
        )

    assert len(gets) >= 2


def _rewriting_ranged_reads(
    rewrite: Callable[[dict[str, Any], int], None], *, ranged_only: bool = True
) -> Callable[[S3Connector, ConnectorConfig, StorableSecret | None], BaseClient]:
    """`_get_s3_client`, with `rewrite(params, n)` applied to the n-th ranged
    `GetObject` before it is built - a store or proxy that answers a read of an
    unchanged object as if it had moved, played by the real store. With
    `ranged_only=False`, to every `GetObject`, ranged or not."""
    real = S3Connector._get_s3_client
    reads: list[int] = []
    lock = threading.Lock()

    def client_for(
        self: S3Connector, config: ConnectorConfig, credential: StorableSecret | None
    ) -> BaseClient:
        connector_client = real(self, config, credential)

        def rewrite_one(params: dict[str, Any], **_: object) -> None:
            if "Range" in params or not ranged_only:
                with lock:
                    reads.append(len(reads) + 1)
                    rewrite(params, len(reads))

        connector_client.meta.events.register("before-parameter-build.s3.GetObject", rewrite_one)
        return connector_client

    return client_for


async def test_a_store_refusing_its_own_etag_fails_the_file_rather_than_skip_it(
    bucket: str, prefix: str, tmp_path: Path
) -> None:
    """A 412 for an object that did not move. Read as a change, the file would
    be skipped on every run while the sync reported success; the HEAD after the
    refusal finds the same `ETag`, so it is a failure the log shows."""
    key = f"{prefix}static.bin"
    _put_multipart(_client(), bucket, key, os.urandom(LARGE))
    listed = (await _listed(bucket, prefix))[key]

    def wrong_etag(params: dict[str, Any], _n: int) -> None:
        params["IfMatch"] = '"00000000000000000000000000000000"'

    with (
        patch.object(S3Connector, "_get_s3_client", _rewriting_ranged_reads(wrong_etag)),
        pytest.raises(ExternalServiceError, match="refused a read"),
    ):
        await S3Connector().fetch_file(
            listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
        )


async def test_a_range_answered_with_416_is_caught_not_stored(
    bucket: str, prefix: str, tmp_path: Path
) -> None:
    """s3transfer writes a range the store answers with 416 as nothing and
    raises nothing: here the file keeps its full length with a hole where the
    second range belongs. What arrived is counted, so it is never stored."""
    key = f"{prefix}holed.bin"
    _put_multipart(_client(), bucket, key, os.urandom(LARGE))
    listed = (await _listed(bucket, prefix))[key]

    def unsatisfiable_second_range(params: dict[str, Any], n: int) -> None:
        if n == 2:
            params["Range"] = f"bytes={LARGE * 4}-"

    with (
        patch.object(
            S3Connector, "_get_s3_client", _rewriting_ranged_reads(unsatisfiable_second_range)
        ),
        pytest.raises(ExternalServiceError, match=r"bytes holed\.bin holds"),
    ):
        await S3Connector().fetch_file(
            listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
        )


async def test_an_object_replaced_between_the_transfers_head_and_read_is_changed(
    bucket: str, prefix: str, tmp_path: Path
) -> None:
    """Why every managed read is ranged: s3transfer reads an object under its
    threshold in one `GetObject` with no `IfMatch`, after a HEAD whose `ETag` it
    reports. Replaced between the two, those bytes would come back under the
    old `ETag`. Pinned, the read is refused, and the HEAD after it finds the
    object moved."""
    client = _client()
    key = f"{prefix}moving.md"
    _put_multipart(client, bucket, key, os.urandom(LARGE))
    listed = (await _listed(bucket, prefix))[key]
    # The same length as the replacement, so only the pin can tell them apart:
    # a count of the bytes received would catch a different length anyway.
    client.put_object(Bucket=bucket, Key=key, Body=b"small, version A")

    def replace_before_the_first_read(_params: dict[str, Any], n: int) -> None:
        if n == 1:
            client.put_object(Bucket=bucket, Key=key, Body=b"small, version B")

    with (
        patch.object(
            S3Connector,
            "_get_s3_client",
            _rewriting_ranged_reads(replace_before_the_first_read, ranged_only=False),
        ),
        pytest.raises(ChangedSinceListing, match="changed"),
    ):
        await S3Connector().fetch_file(
            listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
        )


async def test_an_object_deleted_during_a_multipart_download_is_changed(
    bucket: str, prefix: str, tmp_path: Path
) -> None:
    """A pinned range of a deleted object is answered `NoSuchKey`, not 412. It
    is gone, which the next listing settles - not a failure to report."""
    client = _client()
    key = f"{prefix}withdrawn.bin"
    _put_multipart(client, bucket, key, os.urandom(LARGE))
    listed = (await _listed(bucket, prefix))[key]

    def delete_before_the_second_range(_params: dict[str, Any], n: int) -> None:
        if n == 2:
            client.delete_object(Bucket=bucket, Key=key)

    with (
        patch.object(
            S3Connector, "_get_s3_client", _rewriting_ranged_reads(delete_before_the_second_range)
        ),
        pytest.raises(ChangedSinceListing, match="deleted"),
    ):
        await S3Connector().fetch_file(
            listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
        )


@pytest.mark.parametrize("size", [64, LARGE], ids=["single-part", "multipart"])
async def test_an_object_deleted_after_its_listing_is_changed(
    bucket: str, prefix: str, tmp_path: Path, size: int
) -> None:
    client = _client()
    key = f"{prefix}gone.bin"
    if size > 8 * MIB:
        _put_multipart(client, bucket, key, os.urandom(size))
    else:
        client.put_object(Bucket=bucket, Key=key, Body=os.urandom(size))
    listed = (await _listed(bucket, prefix))[key]
    client.delete_object(Bucket=bucket, Key=key)

    with pytest.raises(ChangedSinceListing, match="deleted"):
        await S3Connector().fetch_file(
            listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
        )


def _reads_sent_to(
    elsewhere: str,
) -> Callable[[S3Connector, ConnectorConfig, StorableSecret | None], BaseClient]:
    """`_get_s3_client`, with every `GetObject` and `HeadObject` sent for the key
    `elsewhere` - a proxy that rewrites a read's path, played by the real store.
    A listing is not rewritten, so it still names the key."""
    real = S3Connector._get_s3_client

    def client_for(
        self: S3Connector, config: ConnectorConfig, credential: StorableSecret | None
    ) -> BaseClient:
        connector_client = real(self, config, credential)

        def rewrite(params: dict[str, Any], **_: object) -> None:
            params["Key"] = elsewhere

        for operation in ("GetObject", "HeadObject"):
            connector_client.meta.events.register(f"before-parameter-build.s3.{operation}", rewrite)
        return connector_client

    return client_for


@pytest.mark.parametrize(
    ("size", "refusal", "says"),
    [(64, ClientError, "GetObject"), (LARGE, ExternalServiceError, r"lists unreadable\.bin")],
    ids=["single-part", "multipart"],
)
async def test_a_key_the_store_lists_but_cannot_read_fails_the_file(
    bucket: str, prefix: str, tmp_path: Path, size: int, refusal: type[Exception], says: str
) -> None:
    """A 404 on every read of a key the listing names. Taken for a deletion,
    the file would be skipped on every run while the sync reported success.
    The listing still names it, so it is a failure the log shows."""
    client = _client()
    key = f"{prefix}unreadable.bin"
    if size > 8 * MIB:
        _put_multipart(client, bucket, key, os.urandom(size))
    else:
        client.put_object(Bucket=bucket, Key=key, Body=os.urandom(size))
    listed = (await _listed(bucket, prefix))[key]

    with (
        patch.object(S3Connector, "_get_s3_client", _reads_sent_to(f"{key}.elsewhere")),
        pytest.raises(refusal, match=says),
    ):
        await S3Connector().fetch_file(
            listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
        )


class _BreaksOff:
    """A response body that times out once, after `after` bytes."""

    def __init__(self, body: StreamingBody, after: int) -> None:
        self._body = body
        self._after = after
        self._sent = 0

    def read(self, amt: int | None = None) -> bytes:
        if self._sent >= self._after:
            from botocore.exceptions import ReadTimeoutError

            raise ReadTimeoutError(endpoint_url=ENDPOINT)
        chunk = self._body.read(amt)
        self._sent += len(chunk)
        return chunk

    def close(self) -> None:
        self._body.close()


async def test_a_range_retried_after_a_timeout_is_counted_once(
    bucket: str, prefix: str, tmp_path: Path
) -> None:
    """s3transfer reads a range again after a timeout and takes back, as
    negative progress, what the first try had sent. Counted twice, the
    transfer would look larger than the object and fail a file that arrived
    whole."""
    client = _client()
    key = f"{prefix}flaky.bin"
    body = os.urandom(LARGE)
    _put_multipart(client, bucket, key, body)
    listed = (await _listed(bucket, prefix))[key]
    real = S3Connector._get_s3_client
    broken: list[bool] = []
    lock = threading.Lock()

    def client_breaking_one_range(
        self: S3Connector, config: ConnectorConfig, credential: StorableSecret | None
    ) -> BaseClient:
        connector_client = real(self, config, credential)

        def break_once(parsed: dict[str, Any], **_: object) -> None:
            with lock:
                if not broken and "Body" in parsed:
                    broken.append(True)
                    parsed["Body"] = _BreaksOff(parsed["Body"], after=3 * MIB)

        connector_client.meta.events.register("after-call.s3.GetObject", break_once)
        return connector_client

    with patch.object(S3Connector, "_get_s3_client", client_breaking_one_range):
        fetched = await S3Connector().fetch_file(
            listed, tmp_path, config=_config(bucket, prefix), credential=_credential()
        )

    assert broken == [True]
    assert fetched.path.read_bytes() == body
    assert fetched.revision == listed.revision


async def test_the_same_bytes_uploaded_again_keep_their_etag(bucket: str, prefix: str) -> None:
    """Why a re-upload of an unchanged file still skips: a single-part `ETag`
    is the bytes' MD5. Changed bytes get a new one, which is what makes an equal
    `ETag` mean unchanged content."""
    client = _client()
    key = f"{prefix}handbook.md"
    client.put_object(Bucket=bucket, Key=key, Body=b"the handbook")
    first = (await _listed(bucket, prefix))[key].revision
    client.put_object(Bucket=bucket, Key=key, Body=b"the handbook")
    again = (await _listed(bucket, prefix))[key].revision
    client.put_object(Bucket=bucket, Key=key, Body=b"the handbook, edited")
    edited = (await _listed(bucket, prefix))[key].revision

    assert first == again
    assert edited != first
