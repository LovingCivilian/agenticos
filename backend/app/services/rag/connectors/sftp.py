"""SFTP sync connector: a folder tree on an SFTP server, read over SSH with a pinned host key.

**The login is bound to one host.** The credential is an `SshCredentialsSecret` in
the organization's vault: a user name with a private key or a password, and the
one host and port the login may be sent to. Whoever edits the source types the
host; whoever added the login chose where it may go, so editing a source cannot
aim the password at a server of the editor's own - the answer `GitTokenSecret`
gives for a token (#987). The binding is checked before anything is resolved or
dialled.

**The address names the account, the host and the port.** A file is filed under
`sftp://<user>@<host>:<port><path>`: the user name percent-encoded, so an `@` or
a `/` in it cannot move where the host starts, and the full remote path kept
raw, because the document's file name is read from it. Two accounts on one
chrooted server see different files at the same path, so two sources share a
document only when they see one file. A login rotated to another user changes
every address, and the next sync reads that account's files again.

**The change signal is the size and the mtime, and it can miss an edit.** SFTP
has no version for a file, so the revision is `size:mtime` from the listing, in
whole seconds. An edit that keeps both - `put -p`, a `SETSTAT`, a rewrite within
the same second - is skipped by `new_only` and `update_only`; a `full` run reads
every file. A file over the size cap is listed with no revision, so it reaches
`_fetch` on every run and fails there, visibly. `_fetch` returns the revision of
the bytes it wrote, from an LSTAT after the read, and raises `ChangedSinceListing`
only on the server's own evidence: stats before and after the read that differ,
or a listing of the file's folder that no longer names it.

**Nothing of the worker's own SSH setup is read.** By default asyncssh reads
`~/.ssh/config`, `~/.ssh/known_hosts`, the keys in `~/.ssh`, the agent at
`SSH_AUTH_SOCK` and a Kerberos ticket, whose key exchange skips the host key
check. `_options` turns each one off: the pinned key is the only trusted host
key, the login is the vault's alone, and only its one method is tried. The host
is checked and pinned by `resolve_pinned_url`, as the other remote-chosen
addresses are (#840, #860), and asyncssh dials the checked addresses with
`AI_NUMERICHOST`, so the name is never resolved a second time. A host on
`SYNC_ALLOWED_HOSTS` may resolve to a private address, never to a loopback,
link-local or metadata one.

**What a server sends is untrusted.** A name with `/` or NUL, or one that is not
UTF-8, is left out and noted. A link or a special file is never listed or
opened, so no loop is walked and no file outside the root is read. The walk
stops at `MAX_DEPTH`, `MAX_FOLDERS`, `MAX_NAMES`, `MAX_FILES`,
`MAX_LISTED_BYTES` and `LISTING_SECONDS`, and says so. asyncssh puts no deadline
on a request, so each one has `REQUEST_SECONDS` and a stall aborts the
connection. A reply asyncssh cannot decode is the server's fault, as a refusal
is. A file is read in requests of `CHUNK_BYTES`, never through `get()`, its
bytes are capped at `MAX_UPLOAD_SIZE_MB` whatever its stats claim, and the
whole fetch, a listing of its folder included, has `FILE_SECONDS`.

**The key is opened once a sync, and the vault bounds what that costs.** A
private key's own numbers decide what opening it costs, and `cryptography`
validates an RSA key holding the GIL, so a thread alone would not keep a huge
key off the event loop. The vault opened this key when it was stored, in a
child process with a budget of 5 seconds, and refused an RSA key over 4096
bits. Opening it here costs what it cost then: about 0.2 s for the largest RSA
key, and never more than that budget. The connector opens it at the sync's
first connect, in a thread, so a KDF that releases the GIL, as bcrypt and
PBKDF2 do, leaves the loop free, and every reconnect reuses it.

**The link race, and what closes it.** SFTP v3 cannot open a path without
following a link, and no server tested honours SFTP v6's `NOFOLLOW`, so on every
server a file swapped for a link after the listing is opened through it. The
listing never lists a link, and where LSTAT reports a link as a link (OpenSSH,
SFTPGo, MINA) only a swap between the LSTAT and the OPEN gets through; on
ProFTPD, whose LSTAT follows links, the window runs from the listing to the OPEN.
As a defence in depth, `_fetch` compares the open handle's FSTAT with the owner
and group the listing reported and with the mode of the LSTAT just before the
OPEN, and refuses another file. A listing's mode can be faked, as ProFTPD's
`DirFakeMode` does, so it is compared only when that LSTAT's size or mtime is
not the listing's: on ProFTPD a link put in place before the LSTAT makes the
LSTAT answer as the target, whose size or mtime then shows. That catches a file
swap to a file with another owner, group or mode where FSTAT reads the open file
(OpenSSH, ProFTPD); on ProFTPD a swap before the LSTAT to a file of the same
owner, group, size and mtime is read whatever its mode. It cannot catch a swap
on MINA or SFTPGo, which answer FSTAT from the path, nor a swap to a file with
the same owner, group and mode, nor a hard link, which has the target's own
attributes in the listing too. Nor does it see a folder: a folder swapped for a link after the
walk read its parent is listed through the link, on every server, and the
listing then holds the target's own owners. The fix is on the server: an
account that can read only what may be synced, and uploaders who cannot create
links or hard links.

**`getpass`.** asyncssh calls `getpass.getuser()` on every connect, and nothing
turns it off; the name is not sent. The shipped image has a passwd entry for
its user. A worker image of your own needs one, or `LOGNAME`, or every connect
raises `OSError`.

asyncssh is imported where it is used, never here: every API worker and
background worker imports this module (#2225).
"""

import asyncio
import copy
import logging
import posixpath
import re
import socket
import sys
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from operator import attrgetter, itemgetter
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, ClassVar, Literal, NamedTuple, Self, cast
from urllib.parse import quote

from pydantic import BaseModel, Field, ValidationError, ValidationInfo, field_validator

from app.core.config import settings
from app.core.exceptions import AppException, BadRequestError, ExternalServiceError
from app.core.sanitize import UrlRefusedError, resolve_pinned_url, spelled_host
from app.core.secret_kinds import (
    KEY_PARSE_ERRORS,
    SecretKind,
    SshCredentialsSecret,
    StorableSecret,
)
from app.services.rag.connectors import (
    DEFAULT_EXTENSIONS,
    STORED_CONFIG,
    BaseSyncConnector,
    ChangedSinceListing,
    ConfigRefusal,
    ConnectorConfig,
    RemoteFile,
    RemoteListing,
    normalized_extensions,
    saving,
)

if TYPE_CHECKING:
    from asyncssh import (
        SFTPAttrs,
        SFTPClient,
        SFTPClientFile,
        SFTPError,
        SFTPName,
        SSHClientConnection,
        SSHClientConnectionOptions,
    )

logger = logging.getLogger(__name__)

# asyncssh's `connect_timeout` covers the TCP connect, the banner, the key
# exchange and the login together. It has none by default, and the login alone
# gets two minutes.
CONNECT_SECONDS = 30.0
# asyncssh sends no keepalive by default, so a server that went away without a
# word would hold the sync until the job's own limit.
KEEPALIVE_SECONDS = 30.0
# One SFTP request. asyncssh waits for an answer with no deadline of its own.
REQUEST_SECONDS = 30.0
# The whole listing, and one file's download (50 MB at about 85 KB/s). Both are
# checked between requests, never by cancelling one: see `_call`.
LISTING_SECONDS = 1800.0
FILE_SECONDS = 600.0
# Folders read at the same time, on the one SFTP session. A folder costs about four
# round trips (OPENDIR, the READDIRs, the end, CLOSE); one at a time, a link of
# 50 ms would reach `LISTING_SECONDS` short of `MAX_FOLDERS`. Not more: asyncssh
# decodes a whole READDIR reply before the walk sees an entry, and a hostile reply
# of 4 MiB decodes to about 123 MiB, so four readers hold at most about 500 MiB.
LISTING_CONCURRENCY = 4
# What one READ asks for. OpenSSH answers at most 256 KiB less 1 KiB.
CHUNK_BYTES = 255 * 1024
# Guards against a hostile or broken server, not choices an editor makes, so none
# is a config field: a source that reaches one is told to choose a narrower folder.
MAX_DEPTH = 32
MAX_FOLDERS = 10_000
MAX_NAMES = 1_000_000
MAX_FILES = 100_000
# What a real file system allows (`NAME_MAX`, `PATH_MAX`). A packet may carry a
# name of megabytes, and a path at depth d holds d names, so a longer one is left
# out, as a name with `/` is.
MAX_NAME_BYTES = 255
MAX_PATH_BYTES = 4096
# The listed files' paths together, counted as the memory Python holds for each
# (`sys.getsizeof`). CPython stores a string at 1, 2 or 4 bytes a character, by
# its widest one, so one emoji makes a path cost four times its length, and a
# count of UTF-8 bytes would let a path of emoji hold four times the budget. An
# ASCII path costs about its length, so a real tree is not charged for what a
# hostile one could hold. Each listed file holds its path twice, so the listing
# holds about twice this at most, where `MAX_FILES` paths of `MAX_PATH_BYTES`
# would hold gigabytes. A real tree of 100,000 ASCII paths of about 100
# characters counts 14 MB of it.
MAX_LISTED_BYTES = 64 * 1024 * 1024

# SFTP's file types, as asyncssh numbers them. Copied, so that importing this
# module does not import asyncssh; `tests/test_sftp_connector.py` pins them.
_REGULAR = 1
_DIRECTORY = 2
_SYMLINK = 3
_UNKNOWN = 5

_HOST_KEY_TYPES = (
    "ssh-ed25519",
    "ssh-ed448",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)
_SCHEMES = frozenset({"sftp"})
_MIB = 1024 * 1024

# A key type as OpenSSH writes one: `ssh-ed25519`, `ecdsa-sha2-nistp256`, a
# certificate's `ssh-ed25519-cert-v01@openssh.com`, a security key's `sk-...`.
_KEY_TYPE = r"(?:sk-)?(?:ssh|ecdsa)-[a-z0-9@.-]+"
_PUBLIC_KEY_LINE = re.compile(
    rf"(?P<type>{_KEY_TYPE})[ \t]+(?P<blob>[A-Za-z0-9+/]+={{0,2}})(?:[ \t].*)?"
)
_PORT_IN_HOST = re.compile(r"(?:\[[^\]]*\]|[^:\[\]]*):[0-9]+")

_NOT_A_HOST = (
    "The host is a host name or an IP address alone, e.g. sftp.example.com, "
    "with no scheme, user name, port or path."
)
_NOT_A_HOST_KEY = (
    "Paste the server's public host key as one line, with its type first, "
    "e.g. ssh-ed25519 AAAAC3Nz..."
)
# C0, DEL, C1, and the line and paragraph separators: see `_shown`.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _shown(text: str) -> str:
    """A remote name, or a server's own message, as our sentences and log lines show it.

    Each control character becomes `?`. A log line is given the escaped text,
    never the exception: the log filter escapes the arguments it is handed as
    text, and the formatter would turn an exception into text after it ran.

    A name is chosen by whoever can upload to the server. A line break in a
    folder's name would otherwise write a line of its own into the sync log and
    the failure notification. C0, DEL and C1 are escaped, NEL among them, and so
    are U+2028 and U+2029, which `str.splitlines`, mail clients and log tools
    also break a line at. The file itself is listed and read under its own name.
    """
    return _CONTROL.sub("?", text)


def _listed_hosts() -> frozenset[str]:
    """`SYNC_ALLOWED_HOSTS`, read now, each entry spelled as a source's host is.

    `resolve_pinned_url` compares the URL's host with the set exactly, and the
    URL carries the host as `SftpConfig` spells it, so the entries are spelled
    the same way. A blank entry, or one that is not one host - with a port, a
    trailing dot or a scheme - could match no source's host, so it is left out.
    """
    listed: set[str] = set()
    for entry in settings.SYNC_ALLOWED_HOSTS:
        try:
            listed.add(spelled_host(entry))
        except ValueError:
            continue
    return frozenset(listed)


class SftpConfig(BaseModel):
    """Which SFTP server a source reads, which folder on it, and which files in that folder.

    The login is not here - it is an `SshCredentialsSecret` the source names in
    `secret_id` (#937), bound to the host it may be sent to. The host key is
    here: it is public, and it is the only proof that the server is the one the
    source names.

    `root_path` has no default. On a server that does not keep the account in
    its home folder, `/` is the whole file system.
    """

    host: str = Field(
        title="Host",
        description="The server's host name or IP address, e.g. sftp.example.com",
    )
    port: int = Field(default=22, ge=1, le=65535, title="Port")
    root_path: str = Field(
        title="Folder",
        description="The absolute path of the folder to read, e.g. /outgoing",
    )
    host_key: str = Field(
        title="Host key",
        description=(
            "The server's public host key, one line such as ssh-ed25519 AAAA... Get it from "
            "the server's administrator, or from ssh-keyscan with the fingerprint compared."
        ),
        # An RSA key line is about 550 characters.
        json_schema_extra={"x-textarea": True},
    )
    include_subfolders: bool = Field(default=True, title="Include subfolders")
    extensions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXTENSIONS),
        title="File types",
        description=(
            "Only files with these extensions are read. Add .pptx, .xlsx and the other "
            "types the collection's parser reads."
        ),
    )

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        """One host alone, spelled as `spelled_host` spells it for the address check,
        the login's host binding and `SYNC_ALLOWED_HOSTS`, which compare it exactly.

        A port gets a sentence of its own, which says where it goes.
        """
        if _PORT_IN_HOST.fullmatch(value.strip()):
            raise ValueError("Put the port in the Port field, and the host alone here.")
        try:
            return spelled_host(value)
        except ValueError:
            raise ValueError(_NOT_A_HOST) from None

    @field_validator("root_path")
    @classmethod
    def _root_path(cls, value: str) -> str:
        """An absolute folder, spelled once: `//` and `/./` collapse, and so does a leading `//`.

        A file's address holds the full path, so a folder spelled two ways would
        file its documents under two addresses. `PurePosixPath` keeps a leading
        `//`, which POSIX leaves to the system, so it is folded here.
        """
        value = value.strip()
        if not value.startswith("/"):
            raise ValueError("The folder is an absolute path on the server, e.g. /outgoing.")
        if _CONTROL.search(value):
            raise ValueError("The folder may not hold a line break or another control character.")
        path = PurePosixPath(value)
        if ".." in path.parts:
            raise ValueError("The folder may not hold '..'. Name the folder itself.")
        folder = "/" + str(path).lstrip("/")
        if len(folder) > 1024:
            raise ValueError("The folder may be at most 1024 characters long.")
        return folder

    @field_validator("host_key")
    @classmethod
    def _host_key(cls, value: str) -> str:
        """One OpenSSH public key line of a type in `_HOST_KEY_TYPES`, kept without its comment.

        `ssh-keyscan` prints the host in front of the key, and an authorized_keys
        line can have options there, so such a line is told what to remove. A PEM
        block is refused before asyncssh reads it: asyncssh accepts a private key
        there and answers its public half, and a private key pasted by mistake
        must not be read. Every sentence is ours: the text asyncssh or
        `cryptography` raises for a broken key never reaches the form.
        """
        line = value.strip()
        match = _PUBLIC_KEY_LINE.fullmatch(line)
        if match is None:
            fields = line.split()
            if (
                len(fields) >= 2
                and re.fullmatch(_KEY_TYPE, fields[1])
                and not re.fullmatch(_KEY_TYPE, fields[0])
            ):
                raise ValueError(
                    "Paste only the key type and the key, without the host name or the "
                    "options in front of them."
                )
            raise ValueError(_NOT_A_HOST_KEY)
        pinned = f"{match['type']} {match['blob']}"
        if match["type"] not in _HOST_KEY_TYPES:
            raise ValueError(
                f"The host key must be of type {', '.join(_HOST_KEY_TYPES[:-1])} or "
                f"{_HOST_KEY_TYPES[-1]}."
            )
        import asyncssh

        try:
            asyncssh.import_public_key(pinned)
        except KEY_PARSE_ERRORS:
            raise ValueError(_NOT_A_HOST_KEY) from None
        return pinned

    @field_validator("extensions")
    @classmethod
    def _extensions(cls, value: list[str], info: ValidationInfo) -> list[str]:
        return normalized_extensions(value, bounded=saving(info))

    def server(self) -> str:
        """`<host>:<port>`, an IPv6 host in brackets so that the port stays apart from it."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    def url(self) -> str:
        """`sftp://<host>:<port>`, what the address check reads.

        The port is always written: `resolve_pinned_url` reads a URL without one
        as port 80 for every scheme but https, and would check one port while
        asyncssh dials another.
        """
        return f"sftp://{self.server()}"

    def authority(self, username: str) -> str:
        """`sftp://<user>@<host>:<port>`, what every `source_path` of this source begins with.

        The user name comes from the vault and may hold `@`, `/` or `:`, so it is
        percent-encoded and cannot move where the host starts.
        """
        return f"sftp://{quote(username, safe='')}@{self.server()}"


@dataclass(frozen=True, slots=True)
class _Stat:
    """A file's type, size and mtime, as one SFTP answer gave them.

    What the stats before and after a read are compared by. The atime is left
    out: reading a file moves it on most servers, and that is no change.
    """

    type: int
    size: int | None
    mtime: int | None

    @classmethod
    def of(cls, attrs: SFTPAttrs) -> Self:
        return cls(type=attrs.type, size=attrs.size, mtime=attrs.mtime)

    @property
    def revision(self) -> str | None:
        """`size:mtime`, or `None` without both: two versions could then share one."""
        if self.size is None or self.mtime is None:
            return None
        return f"{self.size}:{self.mtime}"

    def moved_from(self, other: Self) -> bool:
        """Whether the size or the mtime differs from `other`'s, where both carry it."""
        return any(
            mine is not None and theirs is not None and mine != theirs
            for mine, theirs in ((self.size, other.size), (self.mtime, other.mtime))
        )


class _Identity(NamedTuple):
    """Whose a file is and what it may be: its owner, group and permission bits.

    A link swapped in for the file after the listing opens another file, and
    where FSTAT reads the open file, that file's owner, group or mode shows.
    `_fetch` says which answer each field is compared with. The v4 owner and
    group names are kept as their hashes, so one entry costs the same however
    long a server makes them, and the entries of a listing stay bounded by
    `MAX_FILES`.
    """

    uid: int | None = None
    gid: int | None = None
    owner: int | None = None
    group: int | None = None
    permissions: int | None = None

    @classmethod
    def of(cls, attrs: SFTPAttrs) -> Self:
        return cls(
            uid=attrs.uid,
            gid=attrs.gid,
            owner=None if attrs.owner is None else hash(attrs.owner),
            group=None if attrs.group is None else hash(attrs.group),
            permissions=attrs.permissions,
        )

    def differs(self, other: Self) -> bool:
        """Whether a field both answers carry differs. A field one leaves out says nothing."""
        return any(
            mine is not None and theirs is not None and mine != theirs
            for mine, theirs in zip(self, other, strict=True)
        )


class _Listed(NamedTuple):
    """What the listing said of one file, for `_fetch` to check the open file against."""

    identity: _Identity
    stat: _Stat


# A file this connector did not list: nothing to compare but what the fetch reads.
_UNLISTED = _Listed(_Identity(), _Stat(type=_REGULAR, size=None, mtime=None))


@dataclass(frozen=True)
class _Target:
    """The file one `_fetch` reads: its path as the server spells it, its name, and the account.

    `name` is for sentences, so it is the name as `_shown` shows it. `deadline`
    is when `FILE_SECONDS` runs out, for the reads and for a listing of the
    file's folder alike.
    """

    path: bytes
    name: str
    account: str
    deadline: float


@dataclass
class _Walk:
    """One listing: what bounds it, what it found, and whether it stopped short.

    Up to `LISTING_CONCURRENCY` folders are read into it at once. Each check of a
    bound and its count run with no await between them, so the readers share it
    without a lock.
    """

    parsed: SftpConfig
    extensions: frozenset[str]
    authority: str
    cap: int
    deadline: float
    queue: deque[tuple[str, int]]
    folders: int = 0
    # A folder was not queued, because `MAX_FOLDERS` were opened or queued.
    full: bool = False
    names: int = 0
    # The memory the listed files' paths hold, against `MAX_LISTED_BYTES`.
    listed_bytes: int = 0
    files: list[RemoteFile] = field(default_factory=list)
    # What the listing said of each file, by path, for `_fetch`.
    listed: dict[str, _Listed] = field(default_factory=dict)
    # What a bound stopped, and each folder's own sentences beside its path: the
    # folders finish in an order timing decides, and the sync log should not.
    problems: list[str] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    complete: bool = True
    stopped: bool = False

    def stop(self, reached: str) -> None:
        """End the walk at a bound, with the one problem that tells the user what to do.

        The user cannot raise a bound, so the problem is what tells them to
        narrow the root, and the listing is incomplete, so nothing the walk did
        not reach is removed.
        """
        self.complete = False
        self.stopped = True
        self.problems.append(
            f"The listing of {self.parsed.host} stopped {reached}. Choose a narrower folder."
        )

    def past_deadline(self) -> bool:
        """Whether `LISTING_SECONDS` has passed. The walk is stopped when it has."""
        if time.monotonic() < self.deadline:
            return False
        self.stop(f"after {LISTING_SECONDS:g} seconds, so what it had not reached was not read")
        return True

    def note(self, folder: str, left_out: int, undecodable: int, untyped: int) -> None:
        """One sentence for each kind of entry left out of `folder`, naming the folder only.

        A name that is not a file name - empty, with `/` or NUL, or longer than a
        file system allows - or one that is not UTF-8 could have no address, so
        no document can exist for it and the listing stays complete. An entry
        whose type the server did not say may be a file: without it the listing
        could vouch for nothing, and a complete listing would remove its document.
        """
        shown = _shown(folder)
        if left_out:
            self.notes.append(
                (
                    folder,
                    f"The server listed {left_out} names in {shown} that are not file names, "
                    "so they were left out.",
                )
            )
        if undecodable:
            self.notes.append(
                (
                    folder,
                    f"{undecodable} names in {shown} are not UTF-8, so those files were left out.",
                )
            )
        if untyped:
            self.notes.append(
                (
                    folder,
                    f"The server did not say what {untyped} entries in {shown} are, "
                    "so they were left out.",
                )
            )
            self.complete = False

    def unreadable(self, folder: str) -> None:
        """A folder listed in its parent and refused when opened: what it holds is
        unknown, so the listing is incomplete and the documents from it stay."""
        self.notes.append((folder, f"The folder {_shown(folder)} could not be read."))
        self.complete = False

    def listing(self) -> RemoteListing:
        """What the walk found, in an order that does not depend on timing: the files
        by path, then each folder's sentences by its path, then the bounds."""
        notes = [sentence for _, sentence in sorted(self.notes, key=itemgetter(0))]
        return RemoteListing(
            files=sorted(self.files, key=attrgetter("id")),
            complete=self.complete,
            problems=notes + self.problems,
        )


def _date(mtime: int | None) -> datetime | None:
    """The mtime as a date, or `None` when there is none or a date cannot hold it.

    From SFTP v4 an mtime is 64 bits wide, and a server may answer v4 to a v3
    client. The revision keeps the whole number either way.
    """
    if mtime is None:
        return None
    try:
        return datetime.fromtimestamp(mtime, UTC)
    except OverflowError, ValueError, OSError:
        return None


def _remote_file(path: str, name: str, stat: _Stat, walk: _Walk) -> RemoteFile:
    """A listed file, with no revision when it is over the cap.

    Then it always reaches `_fetch`, which refuses it from its stats, so the
    failure stays visible. Skipped as unchanged, it would vanish.
    """
    over = stat.size is not None and stat.size > walk.cap
    return RemoteFile(
        id=path,
        name=name,
        size=stat.size,
        modified_at=_date(stat.mtime),
        revision=None if over else stat.revision,
        source_path=f"{walk.authority}{path}",
    )


def _megabytes(size: int) -> str:
    return f"{size / _MIB:.1f}".removesuffix(".0")


def _readable(attrs: SFTPAttrs, target: _Target, cap: int) -> _Stat:
    """The stat of a path or a handle about to be read, refused when it cannot be.

    A type that is not a regular file contradicts the listing. It is a failure,
    not a change: one answer proves nothing about the file, and a server that
    answered so on every run would have the file skipped on every run.

    Raises:
        ExternalServiceError: the server says the file is not a regular file.
        BadRequestError: the server says the file is over the cap.
    """
    stat = _Stat.of(attrs)
    if stat.type != _REGULAR:
        raise ExternalServiceError(
            message=f"The server listed {target.name} as a file, and now says it is not one."
        )
    if stat.size is not None and stat.size > cap:
        raise BadRequestError(
            message=(
                f"{target.name} is {_megabytes(stat.size)} MB, and a synced file may be at most "
                f"{_megabytes(cap)} MB."
            ),
            details={"size_bytes": stat.size, "limit_bytes": cap},
        )
    return stat


def _scandir(sftp: SFTPClient, folder: bytes) -> AsyncGenerator[SFTPName]:
    """The entries of `folder`, one READDIR at a time.

    asyncssh annotates `scandir` as an iterator, and it is an async generator:
    its `aclose` sends the folder's CLOSE, which `_call` bounds like any request.
    `contextlib.aclosing` would wait for that CLOSE with no deadline.
    """
    return cast("AsyncGenerator[SFTPName]", sftp.scandir(folder))


class SftpConnector(BaseSyncConnector):
    """Reads a folder tree on an SFTP server into a collection.

    One instance serves one sync: it holds the connection between `list_files`,
    the downloads and `aclose`. A connection the server closed between files is
    opened again, and so is one whose SFTP session alone ended - OpenSSH's
    `ChannelTimeout` ends an idle session and keeps the connection open. A
    connect that failed is not tried again: its refusal is kept for the rest of
    the sync, so a changed password costs one refused login rather than one for
    each file, and a server's lockout policy is not set off.
    """

    CONNECTOR_TYPE: ClassVar[str] = "sftp"
    DISPLAY_NAME: ClassVar[str] = "SFTP server"
    SECRET_KIND: ClassVar[SecretKind] = SecretKind.SSH_CREDENTIALS
    CONFIG_MODEL: ClassVar[type[BaseModel]] = SftpConfig

    def __init__(self) -> None:
        self._conn: SSHClientConnection | None = None
        self._sftp: SFTPClient | None = None
        # Done once the session's channel has closed.
        self._ended: asyncio.Future[None] | None = None
        self._refusal: AppException | None = None
        # The source's host, for the sentences of a request that failed.
        self._host = ""
        # The hashes of the names in each folder `_gone` listed: see `_gone`.
        self._parents: dict[bytes, frozenset[int]] = {}
        # What the last listing said of each file: see `_fetch`.
        self._listed: dict[str, _Listed] = {}
        # The config as the sync passed it, and as `_parse` read it.
        self._config: tuple[ConnectorConfig, SftpConfig] | None = None
        # The options of the sync's first connect, with the key they opened, for
        # the login and the host key they were made for.
        self._opened: tuple[tuple[SshCredentialsSecret, str], SSHClientConnectionOptions] | None = (
            None
        )
        # A connection `_call` aborted because a request on it stalled.
        self._stalled: SSHClientConnection | None = None

    async def validate_config(self, config: ConnectorConfig) -> ConfigRefusal | None:
        """The required fields, then every field's shape, then where the host points.

        The address check runs here as well as at sync time, so a host inside the
        network is refused on the form that typed it. It sees no credential, so
        whether the login is bound to this host is the sync's to answer.
        """
        refusal = await super().validate_config(config)
        if refusal is not None:
            return refusal
        try:
            parsed = SftpConfig.model_validate(config)
        except ValidationError as exc:
            error = exc.errors(include_url=False, include_input=False)[0]
            field_name = str(error["loc"][0]) if error["loc"] else None
            return ConfigRefusal(
                message=str(error["msg"]).removeprefix("Value error, "), field=field_name
            )
        try:
            # In a thread: a resolver that does not answer must not stop the
            # request loop this runs on.
            await asyncio.to_thread(
                resolve_pinned_url, parsed.url(), _SCHEMES, trusted_hosts=_listed_hosts()
            )
        except UrlRefusedError as exc:
            return ConfigRefusal(message=str(exc), field="host")
        return None

    async def list_files(
        self, config: ConnectorConfig, credential: StorableSecret | None
    ) -> RemoteListing:
        """Every file of the configured types in the folder, and below it unless told not to.

        Complete unless an entry's type was not said, a folder below the root
        could not be read, or a bound stopped the walk: each is a problem, and
        what the walk did not see keeps its documents.

        Raises:
            BadRequestError: no SSH login, one for another host, a host inside
                the network, a refused host key or login, and a root folder that
                is missing or that the account may not read.
            ExternalServiceError: a server that cannot be reached, stops
                answering, or drops the connection during the listing, which
                fails it whole so that nothing is removed.
        """
        parsed = self._parse(config)
        login = self._login(credential, parsed)
        sftp = await self._client(parsed, login)
        return await self._walk(sftp, parsed, login)

    async def _fetch(
        self,
        file: RemoteFile,
        dest_path: Path,
        config: ConnectorConfig,
        credential: StorableSecret | None,
    ) -> str | None:
        """Write the bytes of `file.id` to `dest_path`; the revision of those bytes.

        LSTAT, OPEN, FSTAT, the reads, FSTAT, LSTAT, then CLOSE. Each FSTAT is
        compared with the other and each LSTAT with the other, never across: a
        server whose FSTAT and LSTAT disagree about an unchanged file would then
        have it skipped as changed on every run. The FSTATs moving is the file
        the handle reads changing; the LSTATs moving is another file renamed over
        the path, which the handle does not see. The revision returned is the
        last LSTAT's, because the next listing reads LSTAT attributes.

        "No such file" from any request about the file is evidence only once a
        listing of its folder no longer names it (`_gone`). The handle's first
        FSTAT must also carry the owner and group the listing reported, and the
        permission bits of the first LSTAT - and of the listing too, when that
        LSTAT's size or mtime is not the listing's - each where both answers carry
        it: another file opened through a link swapped in after the listing is
        refused before a byte of it is read. The listing's mode alone does not
        count, because ProFTPD's `DirFakeMode` fakes it, so a file that changed
        since the listing on such a server fails once, and the next run reads it.

        Raises:
            ChangedSinceListing: the stats moved during the read, or the file is
                gone and its folder no longer names it.
            BadRequestError: no usable login, a file over the cap, or a read the
                server refused the account.
            ExternalServiceError: anything else the server answered that breaks
                the chain of evidence - a type that is not a file, an open file
                that is not the listed one, more bytes
                than asked for, fewer than the stats say, a file it lists and
                says does not exist - and a server that stopped answering.
        """
        parsed = self._parse(config)
        login = self._login(credential, parsed)
        sftp = await self._client(parsed, login)
        target = _Target(
            path=file.id.encode(),
            name=_shown(file.name),
            account=login.username,
            deadline=time.monotonic() + FILE_SECONDS,
        )
        cap = settings.MAX_UPLOAD_SIZE_MB * _MIB

        path_stat = await self._ask(sftp, target, sftp.lstat(target.path), "before")
        before = _readable(path_stat, target, cap)
        # `block_size=0` makes each read one request, and leaves no reader task
        # behind when a deadline aborts the connection.
        handle = await self._ask(sftp, target, sftp.open(target.path, "rb", block_size=0), "before")
        try:
            first = await self._ask(sftp, target, handle.stat(), "before")
            opened = _readable(first, target, cap)
            listed, seen = self._listed.get(file.id, _UNLISTED), _Identity.of(first)
            # The owner and group the listing reported; the mode of the LSTAT just
            # before the OPEN, since ProFTPD's `DirFakeMode` fakes a listing's mode;
            # and the listing's mode as well once the file moved since the listing,
            # since ProFTPD's LSTAT follows a link and then answers as its target.
            expected = listed.identity._replace(permissions=path_stat.permissions)
            if expected.differs(seen) or (
                before.moved_from(listed.stat) and listed.identity.differs(seen)
            ):
                raise ExternalServiceError(
                    message=(
                        f"The server opened a file for {target.name} that is not the file it "
                        "listed. Check that nobody can replace files in this folder with links."
                    )
                )
            received = await self._read(sftp, target, handle, dest_path, cap, opened.size)
            after = _Stat.of(await self._ask(sftp, target, handle.stat(), "while"))
            moved = _Stat.of(await self._ask(sftp, target, sftp.lstat(target.path), "while"))
        finally:
            await self._close(handle, target)

        if after != opened:
            raise ChangedSinceListing(f"{target.name} changed while it was downloaded.")
        if moved != before:
            raise ChangedSinceListing(f"{target.name} was replaced while it was downloaded.")
        if received > cap:
            raise BadRequestError(
                message=(
                    f"The server sent more than {_megabytes(cap)} MB of {target.name}, and a "
                    f"synced file may be at most {_megabytes(cap)} MB."
                ),
                details={"limit_bytes": cap},
            )
        size = after.size if after.size is not None else moved.size
        if size is not None and size != received:
            raise ExternalServiceError(
                message=f"The server sent {received} of the {size} bytes {target.name} holds."
            )
        return moved.revision

    async def aclose(self) -> None:
        """Close the connection. Safe to call twice, and before one was opened.

        `wait_closed` waits for the server's side of the close, so it is bounded,
        and the connection is aborted when the server does not answer.
        """
        conn = self._forget()
        if conn is None:
            return
        conn.close()
        try:
            await asyncio.wait_for(conn.wait_closed(), REQUEST_SECONDS)
        except TimeoutError:
            conn.abort()

    def _parse(self, config: ConnectorConfig) -> SftpConfig:
        """The source's config, parsed once for the sync, and again only for another config.

        The sync hands the same config to the listing and to each file, and
        parsing it reads the host key with asyncssh, so a sync of many files
        would otherwise parse it once for each. It is the stored config, so the
        limits that apply when one is saved are not applied again
        (`STORED_CONFIG`).
        """
        if self._config is None or self._config[0] != config:
            self._config = (
                copy.deepcopy(config),
                SftpConfig.model_validate(config, context=STORED_CONFIG),
            )
        return self._config[1]

    @staticmethod
    def _login(credential: StorableSecret | None, parsed: SftpConfig) -> SshCredentialsSecret:
        """The source's SSH login, if it may be sent to the source's host.

        Raises:
            BadRequestError: no credential, one of another kind, or a login added
                for another host or port. Before anything is resolved or dialled,
                so the password never leaves the worker.
        """
        if credential is None:
            raise BadRequestError(
                message=(
                    "This SFTP source has no credential. Add an SSH login to the Vault and "
                    "choose it as the source's credential."
                )
            )
        if not isinstance(credential, SshCredentialsSecret):
            raise BadRequestError(
                message="An SFTP source needs an SSH login, and the credential it names is not one."
            )
        if not credential.allows(parsed.host, parsed.port):
            # Both hosts are named: neither is secret, and they are the whole answer.
            raise BadRequestError(
                message=(
                    f"This SSH login was added for {credential.host}, and the source names "
                    f"{parsed.server()}. A login is sent only to the host it was added for."
                )
            )
        return credential

    @staticmethod
    def _options(login: SshCredentialsSecret, host_key: str) -> SSHClientConnectionOptions:
        """asyncssh's options, with every default that reads the worker's own setup replaced.

        The pinned key is the only trusted host key: the seven-list form of
        `known_hosts`, because an empty one reads `~/.ssh/known_hosts` and a `str`
        in it is a file name. No X.509 trust, no agent, no host-based login, no
        Kerberos, no proxy, and only the one method the login holds -
        keyboard-interactive would send the password to any prompt that asks for
        one. A server that lets the client in without a login checked nothing,
        so it is refused.

        Compression is off: asyncssh inflates a packet with no bound on what it
        inflates to, so a server could make the worker allocate hundreds of
        megabytes for each packet of 256 KiB.

        Raises:
            ValueError: the private key does not open with its passphrase, or
                fails the vault's checks; or another of `KEY_PARSE_ERRORS` for a
                damaged key.
        """
        import asyncssh

        # Parsed again before asyncssh reads the key. Parsing a login runs the
        # vault's cheap key checks (`app.core.secret_kinds`): a PEM label asyncssh
        # would make a pattern of without escaping it, a key derivation over the
        # ceiling. A login leaves the vault parsed; this keeps every key that
        # reaches asyncssh one that passed them, however the login was built.
        login = SshCredentialsSecret.model_validate(login.model_dump())
        key = login.private_key
        passphrase = login.passphrase.get_secret_value() if login.passphrase else None
        password = login.password.get_secret_value() if login.password else None
        return asyncssh.SSHClientConnectionOptions(
            config=None,
            known_hosts=([asyncssh.import_public_key(host_key)], [], [], [], [], [], []),
            x509_trusted_certs=None,
            x509_trusted_cert_paths=[],
            username=login.username,
            client_keys=(
                [asyncssh.import_private_key(key.get_secret_value(), passphrase)] if key else None
            ),
            ignore_encrypted=False,
            password=password,
            agent_path=None,
            agent_forwarding=False,
            pkcs11_provider=None,
            host_based_auth=False,
            gss_host=None,
            gss_kex=False,
            gss_auth=False,
            public_key_auth=key is not None,
            password_auth=key is None,
            kbdint_auth=False,
            preferred_auth="publickey" if key else "password",
            disable_trivial_auth=True,
            compression_algs=["none"],
            proxy_command=None,
            tunnel=None,
            connect_timeout=CONNECT_SECONDS,
            login_timeout=CONNECT_SECONDS,
            keepalive_interval=KEEPALIVE_SECONDS,
            keepalive_count_max=3,
        )

    @staticmethod
    def _refused(exc: Exception, parsed: SftpConfig, login: SshCredentialsSecret) -> AppException:
        """Our sentence for a connect or a session start that failed.

        The server's text goes to the worker log and nowhere else. A key error's
        text is the library's and can quote the key, so not even the log gets it.
        """
        import asyncssh

        host = parsed.host
        if isinstance(exc, KEY_PARSE_ERRORS):
            logger.warning("The private key of the SSH login for %s did not open", host)
            return BadRequestError(
                message="The private key in the Vault could not be read with its passphrase."
            )
        logger.warning(
            "SFTP connect to %s failed: %s: %s", host, type(exc).__name__, _shown(str(exc))
        )
        if isinstance(exc, asyncssh.HostKeyNotVerifiable):
            return BadRequestError(
                message=(
                    f"{host} did not prove that it holds the host key in this source's settings. "
                    "Check the key with the server's administrator."
                )
            )
        if isinstance(exc, asyncssh.KeyExchangeFailed):
            return BadRequestError(
                message=(
                    f"{host} and this worker could not agree on a host key or a cipher. Check "
                    "that the server has a key of the type in this source's settings."
                )
            )
        if isinstance(exc, asyncssh.PermissionDenied):
            return BadRequestError(
                message=(
                    f"The login to {host} as {login.username} did not complete. Check the user "
                    "name and the key or password in the Vault."
                )
            )
        if isinstance(exc, asyncssh.ConnectionLost):
            return ExternalServiceError(message=f"The connection to {host} was lost.")
        if isinstance(exc, asyncssh.ChannelOpenError):
            return BadRequestError(message=f"{host} has no SFTP service for {login.username}.")
        if isinstance(exc, asyncssh.SFTPError):
            return ExternalServiceError(
                message=(
                    f"{host} answered the start of the SFTP session in a way this worker "
                    "cannot use."
                )
            )
        return ExternalServiceError(message=f"{host} closed the connection.")

    def _drop(self) -> None:
        """Abort the connection and forget it. The next `_client` opens a new one."""
        conn = self._forget()
        if conn is not None:
            conn.abort()

    def _forget(self) -> SSHClientConnection | None:
        """Forget the connection and its session, and stop watching the session: the connection."""
        conn, ended = self._conn, self._ended
        self._conn, self._sftp, self._ended = None, None, None
        if ended is not None:
            ended.cancel()
        return conn

    async def _call[T](self, request: Awaitable[T]) -> T:
        """Await one SFTP request, for at most `REQUEST_SECONDS`.

        asyncssh has no deadline of its own, and `asyncio.timeout` cannot be one:
        a cancelled folder iterator sends a CLOSE and waits for its answer, and a
        server that stalled one request stalls the next, so nothing would end the
        wait. So the connection is aborted first, which fails every pending
        request at once, and only then is the cancelled request awaited. The
        same happens when the sync itself is cancelled during the wait. The other
        requests pending on that connection - the listing reads several folders at
        once - fail with it, and report the stall that caused it rather than a lost
        connection.

        A request that fails because the session or the connection is gone - the
        channel closed (`SFTPConnectionLost`, then `SFTPNoConnection` for every
        later request), the server disconnected, or the TCP connection failed
        with an `OSError` such as a reset - drops the connection too, so the
        next file opens a new one.

        A reply asyncssh cannot decode raises `PacketDecodeError`, a `ValueError`
        that is no `SFTPError`. It is the server's fault like any refusal, so it is
        raised as `SFTPBadMessage`, for the caller to map: a folder below the root
        that could not be read, the root's own failure, or a file the server
        refused to read. The session stays in step: each reply has its length.

        Raises:
            ExternalServiceError: no answer in time, or the connection was lost.
                The connection is dropped either way.
            SFTPError: any other refusal, for the caller to put in words.
        """
        import asyncssh
        from asyncssh.packet import PacketDecodeError

        conn = self._conn
        task = asyncio.ensure_future(request)
        try:
            await asyncio.wait({task}, timeout=REQUEST_SECONDS)
        finally:
            stalled = not task.done()
            if stalled:
                self._stalled = conn
                self._drop()
                task.cancel()
                await asyncio.wait({task})
        no_answer = f"{self._host} did not answer within {REQUEST_SECONDS:g} seconds."
        if stalled:
            raise ExternalServiceError(message=no_answer)
        try:
            return task.result()
        except (
            asyncssh.DisconnectError,
            asyncssh.SFTPConnectionLost,
            asyncssh.SFTPNoConnection,
            OSError,
        ) as exc:
            self._drop()
            logger.warning("The SFTP connection to %s was lost: %s", self._host, _shown(str(exc)))
            if conn is not None and conn is self._stalled:
                # Aborted because another request on it stalled: that is the cause.
                raise ExternalServiceError(message=no_answer) from exc
            raise ExternalServiceError(message=f"The connection to {self._host} was lost.") from exc
        except PacketDecodeError as exc:
            raise asyncssh.SFTPBadMessage(f"An answer could not be decoded: {exc}") from exc

    async def _client(self, parsed: SftpConfig, login: SshCredentialsSecret) -> SFTPClient:
        """This sync's SFTP session: the open one, or a new one.

        Raises:
            AppException: the refusal of this connect, or of an earlier one in
                this sync, which is raised again without dialling.
        """
        if self._refusal is not None:
            # With a fresh traceback: raised as it is, the one exception would
            # gather the frames of every remaining file of the sync.
            raise self._refusal.with_traceback(None)
        sftp, ended = self._sftp, self._ended
        if (
            sftp is not None
            and ended is not None
            and not ended.done()
            and self._conn is not None
            and not self._conn.is_closed()
        ):
            return sftp
        # None yet, a connection the server closed between files, or a session
        # that ended while its connection stayed open.
        self._drop()
        self._host = parsed.host
        try:
            self._conn = conn = await self._connect(parsed, login)
            sftp = await self._start(conn, parsed, login)
        except AppException as exc:
            self._refusal = exc
            self._drop()
            raise
        self._sftp = sftp
        self._ended = asyncio.ensure_future(sftp.wait_closed())
        return sftp

    async def _connect(
        self, parsed: SftpConfig, login: SshCredentialsSecret
    ) -> SSHClientConnection:
        """Check the host, then dial each address the check pinned, in order.

        An address that refuses the connection, or does not answer, says nothing
        about the server, so the next one is tried, and the last one tried
        decides the sentence. A refused host key or login is the server's own
        answer and each address would give it again, so the first one stops the
        connect: a wrong password is sent once.

        Raises:
            BadRequestError: a host inside the network, a key that does not
                open, or a refusal from the server.
            ExternalServiceError: no address could be reached, or the server
                broke off the connection.
        """
        import asyncssh

        try:
            pinned = await asyncio.to_thread(
                resolve_pinned_url, parsed.url(), _SCHEMES, trusted_hosts=_listed_hosts()
            )
        except UrlRefusedError as exc:
            # The guard's sentence names the host and never the URL, so it may be stored.
            raise BadRequestError(message=str(exc)) from exc
        opened = self._opened
        if opened is not None and opened[0] == (login, parsed.host_key):
            # The key was opened by the sync's first connect: a reconnect reuses it.
            options = opened[1]
        else:
            try:
                # In a thread, and bounded by the vault's check when the key was
                # stored: see the module docstring.
                options = await asyncio.to_thread(self._options, login, parsed.host_key)
            except KEY_PARSE_ERRORS as exc:
                # Their text can quote the key, so they are not chained.
                raise self._refused(exc, parsed, login) from None
            self._opened = ((login, parsed.host_key), options)
        failure: OSError | None = None
        for ip in pinned.ips:
            try:
                # Nothing beside `options` but where to dial: a keyword here would
                # be applied over the hardened options.
                return await asyncssh.connect(
                    ip, pinned.port, flags=socket.AI_NUMERICHOST, config=None, options=options
                )
            except asyncssh.Error as exc:
                raise self._refused(exc, parsed, login) from exc
            except OSError as exc:
                logger.warning(
                    "SFTP connect to %s at %s port %d failed: %s: %s",
                    parsed.host,
                    ip,
                    pinned.port,
                    type(exc).__name__,
                    _shown(str(exc)),
                )
                failure = exc
        if isinstance(failure, TimeoutError):
            raise ExternalServiceError(
                message=f"{parsed.host} did not answer within {CONNECT_SECONDS:g} seconds."
            ) from failure
        raise ExternalServiceError(
            message=f"{parsed.host} could not be reached on port {parsed.port}."
        ) from failure

    async def _start(
        self, conn: SSHClientConnection, parsed: SftpConfig, login: SshCredentialsSecret
    ) -> SFTPClient:
        """Start the SFTP session: version 3, with names as bytes.

        With no path encoding, asyncssh answers each name as bytes, so one name
        that is not UTF-8 cannot end the whole listing. Version 3 is what every
        server tested speaks, and its mtime, which the revision is made of, is
        whole seconds.

        Raises:
            BadRequestError: the server has no SFTP service for the account.
            ExternalServiceError: the session start did not answer in time, or
                answered what asyncssh cannot use.
        """
        import asyncssh

        try:
            return await self._call(conn.start_sftp_client(path_encoding=None, sftp_version=3))
        except asyncssh.Error as exc:
            raise self._refused(exc, parsed, login) from exc

    async def _walk(
        self, sftp: SFTPClient, parsed: SftpConfig, login: SshCredentialsSecret
    ) -> RemoteListing:
        """The tree below the root, from a queue and never by recursion.

        Up to `LISTING_CONCURRENCY` folders are read at once on the one session,
        each taken from the queue as another finishes, so a slow link costs its
        round trips once for several folders. The root is read alone, since the
        queue holds nothing else yet. Each bound is checked before the step it
        limits: the time before each folder and each entry, the folder count
        before each folder is queued. The queue holds no more folders than may be
        opened, and a folder that does not fit is left out, while the folders
        already queued are still read. A request that stalls aborts the
        connection, which fails the other folders' requests too, so the listing
        fails whole.

        Raises:
            BadRequestError: the root is missing, not a folder, or not the
                account's to read.
            ExternalServiceError: the server failed the root's listing, or
                stopped answering, or the connection was lost.
        """
        walk = _Walk(
            parsed=parsed,
            extensions=frozenset(parsed.extensions),
            authority=parsed.authority(login.username),
            cap=settings.MAX_UPLOAD_SIZE_MB * _MIB,
            deadline=time.monotonic() + LISTING_SECONDS,
            queue=deque([(parsed.root_path, 0)]),
        )
        reading: set[asyncio.Future[None]] = set()
        try:
            while True:
                while walk.queue and len(reading) < LISTING_CONCURRENCY and not walk.stopped:
                    folder, depth = walk.queue.popleft()
                    if walk.past_deadline():
                        break
                    walk.folders += 1
                    reading.add(
                        asyncio.ensure_future(self._visit(sftp, walk, folder, depth, login))
                    )
                if not reading:
                    break
                done, reading = await asyncio.wait(reading, return_when=asyncio.FIRST_COMPLETED)
                for failure in [task.exception() for task in done]:
                    if failure is not None:
                        raise failure
        finally:
            # A failed listing, or a cancelled sync: the other readers end with it.
            for task in reading:
                task.cancel()
            await asyncio.gather(*reading, return_exceptions=True)
        if walk.full:
            walk.stop(f"at {MAX_FOLDERS} folders, so the folders after them were not read")
        self._listed = walk.listed
        return walk.listing()

    async def _visit(
        self, sftp: SFTPClient, walk: _Walk, folder: str, depth: int, login: SshCredentialsSecret
    ) -> None:
        """Read one folder of the walk. A refused root fails the listing; a refused
        folder below it is a problem, and the walk goes on."""
        import asyncssh

        try:
            await self._read_folder(sftp, walk, folder, depth)
        except asyncssh.SFTPError as exc:
            if depth == 0:
                raise self._root_refused(exc, walk.parsed, login) from exc
            logger.warning(
                "SFTP: %s did not list %s: %s", walk.parsed.host, _shown(folder), _shown(str(exc))
            )
            walk.unreadable(folder)

    async def _read_folder(self, sftp: SFTPClient, walk: _Walk, folder: str, depth: int) -> None:
        """List one folder into `walk`, and queue its subfolders.

        A name is checked before its type, so a folder with a bad name is never
        opened: `/etc` or `../up` would take the walk out of the root wherever
        the server resolves them. The type is the entry's own, never read from
        its permissions, and a link is skipped whatever it points at.
        """
        encoded = folder.encode()
        entries = _scandir(sftp, encoded)
        left_out = undecodable = untyped = 0
        try:
            while (entry := await self._call(anext(entries, None))) is not None:
                # Another reader may have reached a bound: one problem says so.
                if walk.stopped or walk.past_deadline():
                    break
                if walk.names >= MAX_NAMES:
                    walk.stop(f"at {MAX_NAMES} names, so the names after them were not read")
                    break
                walk.names += 1
                # Bytes, because the session has no path encoding.
                raw = cast("bytes", entry.filename)
                if raw in (b".", b".."):
                    continue
                if (
                    not raw
                    or b"/" in raw
                    or b"\x00" in raw
                    or len(raw) > MAX_NAME_BYTES
                    or len(posixpath.join(encoded, raw)) > MAX_PATH_BYTES
                ):
                    left_out += 1
                    continue
                try:
                    name = raw.decode()
                except UnicodeDecodeError:
                    undecodable += 1
                    continue
                kind, path = entry.attrs.type, posixpath.join(folder, name)
                if kind == _DIRECTORY:
                    if not walk.parsed.include_subfolders:
                        continue
                    if depth >= MAX_DEPTH:
                        walk.stop(
                            f"at {MAX_DEPTH} folder levels below {_shown(walk.parsed.root_path)}, so the "
                            "folders below them were not read"
                        )
                        break
                    if walk.folders + len(walk.queue) >= MAX_FOLDERS:
                        walk.full = True
                        continue
                    walk.queue.append((path, depth + 1))
                    continue
                if kind == _UNKNOWN:
                    untyped += 1
                    continue
                if kind == _SYMLINK:
                    # Never followed, so no loop is walked and nothing outside the
                    # root is read.
                    continue
                if kind != _REGULAR or PurePosixPath(name).suffix.lower() not in walk.extensions:
                    # A FIFO, a socket or a device - a FIFO opened on OpenSSH blocks
                    # the server process - or a type no parser reads.
                    continue
                if len(walk.files) >= MAX_FILES:
                    walk.stop(f"at {MAX_FILES} files, so the files after them were not read")
                    break
                size = sys.getsizeof(path)
                if walk.listed_bytes + size > MAX_LISTED_BYTES:
                    walk.stop(
                        f"at {_megabytes(MAX_LISTED_BYTES)} MB of file paths, so the files after "
                        "them were not read"
                    )
                    break
                walk.listed_bytes += size
                stat = _Stat.of(entry.attrs)
                walk.files.append(_remote_file(path, name, stat, walk))
                walk.listed[path] = _Listed(_Identity.of(entry.attrs), stat)
        finally:
            await self._call(entries.aclose())
        walk.note(folder, left_out, undecodable, untyped)

    @staticmethod
    def _root_refused(
        exc: SFTPError, parsed: SftpConfig, login: SshCredentialsSecret
    ) -> AppException:
        """Why the root folder could not be listed, as a refusal that fails the listing.

        A root that cannot be listed is not an empty folder: a complete listing of
        nothing would remove every document the source brought in.
        """
        import asyncssh

        root, host = _shown(parsed.root_path), parsed.host
        logger.warning("SFTP: %s did not list the root %s: %s", host, root, _shown(str(exc)))
        if isinstance(exc, asyncssh.SFTPNoSuchFile):
            # SFTP v3 answers "no such file" for a folder that is a file.
            return BadRequestError(
                message=f"There is no folder {root} on {host}, or it is not a folder."
            )
        if isinstance(exc, asyncssh.SFTPPermissionDenied):
            return BadRequestError(
                message=f"The account {login.username} may not read the folder {root} on {host}."
            )
        # SFTPGo, ProFTPD and Apache MINA SSHD answer this way for a root that names a file.
        return ExternalServiceError(
            message=(
                f"{host} could not list the folder {root}. Check that it is a folder the "
                "account may read."
            )
        )

    async def _ask[T](
        self,
        sftp: SFTPClient,
        target: _Target,
        request: Awaitable[T],
        when: Literal["before", "while"],
    ) -> T:
        """One request about the file `_fetch` reads, its refusals in our words.

        `when` says whether a byte of the file was read yet, for the sentence
        of a file that was deleted.

        Raises:
            ChangedSinceListing: "no such file", and its folder no longer names it.
            BadRequestError: the account may not read the file.
            ExternalServiceError: any other refusal, and "no such file" for a
                file its folder still names.
        """
        import asyncssh

        try:
            return await self._call(request)
        except asyncssh.SFTPNoSuchFile as exc:
            raise await self._unread(sftp, target, when) from exc
        except asyncssh.SFTPPermissionDenied as exc:
            logger.warning("SFTP: %s refused %s: %s", self._host, target.name, _shown(str(exc)))
            raise BadRequestError(
                message=f"The account {target.account} may not read {target.name}."
            ) from exc
        except asyncssh.SFTPError as exc:
            logger.warning("SFTP: %s failed %s: %s", self._host, target.name, _shown(str(exc)))
            raise ExternalServiceError(
                message=f"The server refused to read {target.name}."
            ) from exc

    async def _read(
        self,
        sftp: SFTPClient,
        target: _Target,
        handle: SFTPClientFile,
        dest_path: Path,
        cap: int,
        size: int | None,
    ) -> int:
        """Copy the handle's bytes to `dest_path`, one request at a time: how many arrived.

        Never `read(-1)`, which reads to the size the server claims, and never
        `get()`, which has no byte cap and takes a short file without a word.
        The read stops once the bytes pass the cap, whatever the stats said, and
        once they reach `size`, the handle's size before the read: a last READ
        would only answer the end of the file, one round trip for nothing. A file
        that grew meanwhile still shows in the stats after the read.

        Raises:
            ExternalServiceError: a reply longer than the request - asyncssh
                hands it over untrimmed - or `FILE_SECONDS` passed.
        """
        received = 0
        with dest_path.open("wb") as out:
            while received <= cap and received != size:
                if time.monotonic() >= target.deadline:
                    raise ExternalServiceError(
                        message=f"{target.name} did not download within {FILE_SECONDS:g} seconds."
                    )
                data = cast(
                    "bytes",
                    await self._ask(sftp, target, handle.read(CHUNK_BYTES, received), "while"),
                )
                if not data:
                    break
                if len(data) > CHUNK_BYTES:
                    raise ExternalServiceError(
                        message=f"The server answered a read of {target.name} with more than was asked for."
                    )
                out.write(data)
                received += len(data)
        return received

    async def _close(self, handle: SFTPClientFile, target: _Target) -> None:
        """Close a file handle, and never fail the file for it.

        Both stats come before the CLOSE, so one the server refuses changes
        nothing about the file and is only logged. One that stalls drops the
        connection through `_call`, and the next file opens a new one.
        """
        import asyncssh

        try:
            await self._call(handle.close())
        except (asyncssh.SFTPError, ExternalServiceError) as exc:
            logger.warning(
                "SFTP: %s did not close %s: %s", self._host, target.name, _shown(str(exc))
            )

    async def _unread(
        self, sftp: SFTPClient, target: _Target, when: Literal["before", "while"]
    ) -> ChangedSinceListing | ExternalServiceError:
        """What a "no such file" means, asked of the file's folder."""
        if await self._gone(sftp, target):
            return ChangedSinceListing(f"{target.name} was deleted {when} it was downloaded.")
        return ExternalServiceError(
            message=f"The server lists {target.name}, but answers that it does not exist."
        )

    async def _gone(self, sftp: SFTPClient, target: _Target) -> bool:
        """Whether a listing of the file's folder, made after the sync's own, no longer names it.

        "No such file" alone is not that evidence: a server can answer it for a
        file it lists, and taken for a deletion, that file would be skipped on
        every run while the sync reported success.

        A folder's listing is kept for the sync. A file it does not name is gone,
        so many deletions in one folder cost one listing, not one each. A file it
        still names makes the folder be listed once more, and that listing
        replaces the kept one, so a file deleted since is still seen. The kept
        names together stay under `MAX_NAMES`: past it, the older listings are
        let go, and a later deletion lists its folder again.

        Raises:
            ExternalServiceError: the folder could not be listed, or holds more
                than `MAX_NAMES` names before this one, so it cannot say.
        """
        folder, name = posixpath.split(target.path)
        kept = self._parents.get(folder)
        if kept is not None and hash(name) not in kept:
            return True
        listed = await self._names_in(sftp, folder, name, target)
        if listed is None:
            return False
        self._parents.pop(folder, None)
        if sum(map(len, self._parents.values())) + len(listed) > MAX_NAMES:
            self._parents.clear()
        self._parents[folder] = listed
        return True

    async def _names_in(
        self, sftp: SFTPClient, folder: bytes, name: bytes, target: _Target
    ) -> frozenset[int] | None:
        """The names in `folder`, as their hashes, or `None` as soon as one of them is `name`.

        The names are compared as bytes, so a folder that also holds a name that
        is not UTF-8 still answers. Only their hashes are kept: a million long
        names would otherwise be kept whole. Two names that share a hash cost one
        more listing, which compares the names themselves. A folder that is gone
        holds nothing.

        Raises:
            ExternalServiceError: as `_gone` says.
        """
        import asyncssh

        cannot_say = ExternalServiceError(
            message=(
                f"The server answers that {target.name} does not exist, and its folder could "
                "not be listed to confirm it."
            )
        )
        entries = _scandir(sftp, folder)
        hashes: set[int] = set()
        try:
            count = 0
            while (entry := await self._call(anext(entries, None))) is not None:
                # The file's own deadline: each READDIR has its own, and a server
                # can answer each just in time with one name.
                if count >= MAX_NAMES or time.monotonic() >= target.deadline:
                    raise cannot_say
                count += 1
                raw = cast("bytes", entry.filename)
                if raw == name:
                    return None
                hashes.add(hash(raw))
        except asyncssh.SFTPNoSuchFile:
            return frozenset()
        except asyncssh.SFTPError as exc:
            logger.warning(
                "SFTP: %s did not list the folder of %s: %s",
                self._host,
                target.name,
                _shown(str(exc)),
            )
            raise cannot_say from exc
        finally:
            await self._call(entries.aclose())
        return frozenset(hashes)
