"""RAG sync connectors - extensible source adapters for document ingestion."""

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from app.core.secret_kinds import SecretKind, StorableSecret
from app.services.rag.remote_names import destination_within

logger = logging.getLogger(__name__)


ConnectorConfig = dict[str, Any]
"""How to *find* a source's documents, as the wizard posted it.

`Any` and meant: this is the source's own JSONB column, read with
`config.get(...)`. What a connector accepts is described by its `CONFIG_MODEL`,
a Pydantic model whose `model_json_schema()` is the form the wizard draws and
whose fields drive the required-field check `validate_config` derives - JSON
Schema, the same shape a capability publishes, rather than a bespoke mapping
(#1093). A credential never arrives here (#937).
"""


class EmptyConfig(BaseModel):
    """A connector with nothing to configure - the base's default `CONFIG_MODEL`."""


class RemoteFile(BaseModel):
    """Metadata for a file from a remote source.

    `revision` is the file's version as the remote system reports it in the
    listing - an S3 `ETag`, a Git blob sha - never a value calculated from the
    bytes, because its whole point is to be known before the download. In
    `new_only` and `update_only`, a listed revision equal to the one stored
    beside the document's `content_hash` skips the download. So equal
    revisions at one `source_path` must mean equal content. A connector whose
    revision can miss a change says so in its docstring, and `full` is the
    remedy for it.

    What is stored is not this value but the one `_fetch` reports for the bytes
    it wrote (`FetchedFile.revision`): a file replaced between the listing and
    the download arrives as its new version, and filed under the listed one it
    would be skipped for good once its old bytes came back. A skipped file never
    reaches `_fetch`, so a connector that refuses a file there for a reason the
    revision does not cover - a robots.txt rule, a size limit lowered since -
    checks it in the listing too, or reports no revision. `None` is "no
    revision": the file takes the download and the hash, as every file did
    before.
    """

    id: str
    name: str
    mime_type: str | None = None
    size: int | None = None
    modified_at: datetime | None = None
    revision: str | None = None
    source_path: str  # Dedup key: "gdrive://file_id", "s3://bucket/key"


class RemoteListing(BaseModel):
    """What a source holds, and whether a sync may treat the answer as the whole of it.

    `complete` is what removal hangs on. A sync removes the documents its source
    brought in earlier and no longer lists, and that is only safe against a
    listing that is known to be everything: a crawl that stopped at its page
    ceiling, or could not read one page of a site, did not see the pages behind
    it, and removing those would empty a collection because of one timeout.
    Drive and S3 answer a listing whole or raise, so theirs is always complete.

    `problems` are sentences this repository wrote - a URL's host, a status
    code, never a page's own text - so the sync log can show them. Each one is a
    page the source could not read, and counts as a failed file.
    """

    files: list[RemoteFile]
    complete: bool = True
    problems: list[str] = []


class WithdrawnFile(Exception):
    """A listed file its source turned out not to hold, found only when it was fetched.

    A sitemap names a page before anything reads it, so its listing cannot know
    that the page now says `noindex` or has gone. Raised from `_fetch`, this tells
    the sync that the file is not a failure to retry but no longer the source's:
    it is left out of what the listing vouches for, so a document it brought in
    earlier is removed like any other the source stopped listing. `message` is a
    sentence this repository wrote, as a listing's `problems` are.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ChangedSinceListing(Exception):
    """A listed file that changed, or went, while it was being fetched.

    An object store refuses a ranged read pinned to a version the object has
    left, which is the only way a download that takes many requests can tell
    that it would otherwise write one version's start and another's end. Raised
    from `_fetch`, this tells the sync that the file is neither broken nor
    withdrawn: it is skipped, its document stays claimed, and the next listing
    names the version it has now, or leaves it out. `message` is a sentence
    this repository wrote, as a listing's `problems` are.

    Raised only on evidence the remote system gives - a version that moved, a
    listing that no longer names the file - never on a failed read alone. A
    read that fails the same way on every run would otherwise be skipped on
    every run while the sync reported success.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class FetchedFile(BaseModel):
    """Where a downloaded file landed, and which version of it the bytes are.

    `revision` is what the remote system said about the bytes it sent - an
    `ETag` from the response - and `None` when the connector cannot say. It is
    what the sync stores beside the document, so `None` means the next run
    downloads the file again rather than trust a listing it cannot check.
    """

    path: Path
    revision: str | None = None


class ConfigRefusal(BaseModel):
    """Why a connector will not accept a config, and which of its fields.

    `validate_config` used to answer `tuple[bool, str | None]`, so a refusal
    that knew exactly which field was wrong could not say so: the sentence
    reached the wire and the sync-source wizard, which draws one input per
    `CONFIG_MODEL` field, marked none of them (#897).

    `field` is a `CONFIG_MODEL` field name, as the connector declares it. Where that
    sits in the document the wizard posted is not a connector's to know:
    `SyncSourceService` roots it and builds the refusal with `refused_field`,
    which is the only thing that decides what reaches the wire. Singular here
    and plural there on purpose - a connector refuses on the first thing it
    finds wrong, and `details["fields"]` is a list because a *pydantic* refusal
    reports every field at once (#891).

    It is optional because a connector is entitled to refuse a config without
    blaming one field of it - connectivity that fails, two credentials that do
    not belong to the same account - and forcing the folder-id case's shape onto
    that would only produce an invented field name.
    """

    message: str
    field: str | None = None


class BaseSyncConnector(ABC):
    """Base class for all sync source connectors.

    To add a new connector:
    1. Create a new class inheriting BaseSyncConnector
    2. Implement list_files() and _fetch()
    3. Define CONFIG_MODEL, a Pydantic model of its config fields
    4. Register in CONNECTOR_REGISTRY
    """

    CONNECTOR_TYPE: ClassVar[str] = ""
    DISPLAY_NAME: ClassVar[str] = ""
    CONFIG_MODEL: ClassVar[type[BaseModel]] = EmptyConfig
    # Which vault secret authenticates this connector. `CONFIG_MODEL` describes
    # how to *find* the documents and holds nothing that has to be kept: the
    # credential arrives separately, unsealed from the organization's vault by
    # whoever is running the sync (#937). A connector that needs none says
    # `SecretKind.NONE` - a public docs crawler, when there is one.
    SECRET_KIND: ClassVar[SecretKind] = SecretKind.NONE

    @abstractmethod
    async def list_files(
        self, config: ConnectorConfig, credential: StorableSecret | None
    ) -> RemoteListing:
        """List files available for sync from this source.

        `credential` is the unsealed vault secret, or `None` when the source has
        no `secret_id` or its secret has been deleted. A connector needing one
        raises rather than reaching for a deployment-wide fallback: there is no
        such thing here, and inventing one would let a source read under the
        operator's identity rather than its own.

        A listing that cannot vouch for being everything says so with
        `complete=False` rather than raising, so what it did find is still
        ingested - see `RemoteListing`.
        """

    async def fetch_file(
        self,
        file: RemoteFile,
        dest_dir: Path,
        config: ConnectorConfig | None = None,
        credential: StorableSecret | None = None,
    ) -> FetchedFile:
        """Download one file into `dest_dir`: where it landed, and which version it is.

        **Where the file lands is this class's answer, not a connector's.** The
        two shipped connectors used to decide it separately and disagreed: S3
        reduced its key to a single component, Drive wrote `dest_dir /
        file.name` verbatim, and a Drive file named `../../../evil` therefore
        left the temporary directory the worker had made for it. Handing an
        implementation a path rather than a directory is what makes the answer
        inherited instead of remembered - see `destination_within` for the
        refusal itself.

        Raises:
            ChangedSinceListing: the file changed while it was being fetched.
        """
        dest_path = destination_within(dest_dir, file.name)
        revision = await self._fetch(file, dest_path, config or {}, credential)
        return FetchedFile(path=dest_path, revision=revision)

    @abstractmethod
    async def _fetch(
        self,
        file: RemoteFile,
        dest_path: Path,
        config: ConnectorConfig,
        credential: StorableSecret | None,
    ) -> str | None:
        """Write `file`'s bytes to `dest_path`, which is already inside the sync directory.

        Answers the revision of the bytes written, as the remote system said in
        the response that carried them, or `None` when it said nothing a later
        listing could be compared with. Never the listed `file.revision` on
        trust: the file may have changed since.
        """

    async def remote_version(
        self, config: ConnectorConfig, credential: StorableSecret | None, previous: str | None
    ) -> str | None:
        """What the source's whole content is at now, if that is cheap to ask.

        A value the sync stores after a run that finished cleanly. When the next
        run reads the same value under the same configuration, nothing upstream
        has changed and it stops before listing or downloading anything - a Git
        branch's head commit is the case this exists for, answered by one
        `ls-remote` instead of a clone.

        `previous` is the value the last clean run under this same configuration
        stored, or `None`. A source that can only say what changed *since* a
        point - SharePoint's delta link - answers `previous` back when nothing
        has; one that can say where it is outright, as a commit does, ignores it.

        `None`, the default, means the source cannot say, and every run lists.
        A connector that answers must answer a *different* value whenever any
        listed file's bytes, or the listing itself, could have changed.
        """
        return None

    async def aclose(self) -> None:  # noqa: B027
        """Release what one sync made - a clone, a session. Called once the sync is over.

        Nothing, by default: a connector that holds nothing between calls has
        nothing to release. The `noqa` is that decision - an optional hook, not
        an abstract one every connector must spell out.
        """

    async def validate_config(self, config: ConnectorConfig) -> ConfigRefusal | None:
        """Why this config would not be accepted, or `None` if it would.

        The required-field check is derived from `CONFIG_MODEL`: a field with no
        default is required, and the wizard drew it under the model's `title`, so
        the refusal names that rather than the key underneath it. An override
        that has more to check calls this first and returns what it answers,
        unchanged.
        """
        for name, field in self.CONFIG_MODEL.model_fields.items():
            if field.is_required() and not config.get(name):
                return ConfigRefusal(
                    message=f"Missing required field: {field.title or name}", field=name
                )
        return None


CONNECTOR_REGISTRY: dict[str, type[BaseSyncConnector]] = {}
from app.services.rag.connectors.google_drive import GoogleDriveConnector

CONNECTOR_REGISTRY["gdrive"] = GoogleDriveConnector
from app.services.rag.connectors.s3 import S3Connector

CONNECTOR_REGISTRY["s3"] = S3Connector
from app.services.rag.connectors.web import WebConnector

CONNECTOR_REGISTRY["web"] = WebConnector
from app.services.rag.connectors.git import GitConnector

CONNECTOR_REGISTRY["git"] = GitConnector
from app.services.rag.connectors.sharepoint import SharePointConnector

CONNECTOR_REGISTRY["sharepoint"] = SharePointConnector
