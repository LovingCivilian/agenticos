"""The SFTP sync connector against an SFTP server: what a sync can trust.

The unit tests next door check what the connector asks asyncssh for. These run
it against an asyncssh SFTP server in the test's own event loop, on loopback,
with no Docker. A mock answers what it is told to. A server sends what a broken
or hostile server sends: a name with a slash, a size that moves during a read,
a reply longer than the request, a read that never ends. These tests prove what
the connector lists, what it reads, and what it refuses.

The in-process servers, each with one ed25519 host key made for the test:

- the disk server: real files and real symlinks under a chroot. It records
  every request, and can run a test's action after the first read of a file;
- the hostile server: an `SFTPServer` that makes up its answer for each path,
  and counts each request, so a test can prove that no byte was read;
- the login variants: a password server, a server that lets anyone in, and a
  server that asks for the password only through keyboard-interactive.

The servers listen on `127.0.0.1`, which the SSRF guard refuses, so each test
patches `resolve_pinned_url` in the connector's module to pin `sftp.test` to the
server. The unit tests check the guard itself.

The tests that take the `server` fixture also run against a real SFTP server
when these variables are set. With none of them set they skip; with some of them
set they fail, and name the ones that are missing:

    SFTP_TEST_HOST, SFTP_TEST_PORT   the server, usually 127.0.0.1 and a mapped port
    SFTP_TEST_USER                   the login
    SFTP_TEST_PASSWORD               or SFTP_TEST_KEY_FILE, an unencrypted key file;
                                     a password has 8 characters or more, as an SSH
                                     login's password in the vault does
    SFTP_TEST_HOST_KEY               the pinned line, of any type the host key field
                                     takes: `ssh-ed25519 AAAA...`, or `ssh-rsa AAAA...`
                                     for Apache MINA SSHD, which has no ed25519 key
    SFTP_TEST_ROOT                   an absolute folder the login may write to
    SFTP_TEST_TREE                   optional: the folder of the `mkdata.sh` tree,
                                     with its four links also under a `.txt` name

`mkdata.sh` gives its links no extension, so the extension filter alone would hide
them, whatever the connector does. The tree also needs `link-to-file.txt` to
`top.txt`, `link-outside.txt` to a file out of the root, `link-loop.txt` to itself
and `link-broken.txt` to a missing name, as `_plant_tree` makes them; a tree
without them fails the tests that use it.

Each test writes its files under a new folder of `SFTP_TEST_ROOT` with a second
SFTP client, and removes the folder afterwards. The link, loop and
unreadable-folder tests need `SFTP_TEST_TREE`, because a server need not let a
client make a link, and skip without it. Both folders are absolute, and neither
may hold the other: a listing of the tree would walk the folders of the tests
that run beside it. The recipes for OpenSSH, SFTPGo, ProFTPD and Apache MINA SSHD
are in a comment on #29.

Every test runs with `HOME` at an empty folder and no `SSH_AUTH_SOCK`, so the
runner's own `~/.ssh` and agent cannot change a result.
"""

import asyncio
import builtins
import contextlib
import io
import logging
import os
import posixpath
import secrets
import shutil
import socket
import stat
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from urllib.parse import quote

import anyio
import asyncssh
import pytest
from asyncssh import SFTPAttrs, SFTPName
from asyncssh.packet import SSHPacket
from asyncssh.sftp import FXP_CLOSE, SFTPServerHandler
from pydantic import SecretStr

from app.core.config import settings
from app.core.exceptions import BadRequestError, ExternalServiceError
from app.core.sanitize import PinnedAddress
from app.core.secret_kinds import MIN_CREDENTIAL_LENGTH, SshCredentialsSecret
from app.services.rag.connectors import (
    ChangedSinceListing,
    ConnectorConfig,
    RemoteFile,
    RemoteListing,
)
from app.services.rag.connectors import sftp as sftp_module
from app.services.rag.connectors.sftp import SftpConnector

pytestmark = pytest.mark.anyio

HOST = "sftp.test"
USER = "sync"
PASSWORD = "sync-password-1"
MTIME = 1_700_000_000
# Every atime is a day before its mtime, so a listing that read the wrong one shows it.
DAY = 86_400
MIB = 1024 * 1024

# The regular files of the `mkdata.sh` tree (servers section 1), relative to it.
# Everything else in it is a link, a loop, a broken link or a folder nobody may read.
TREE_FILES = (
    "top.txt",
    "subsecond.txt",
    "spaced name ü €.txt",
    "dir/inner.txt",
    "dir/sub/deep.txt",
)


def _login_refused(user: str = USER) -> str:
    return (
        f"The login to {HOST} as {user} did not complete. "
        "Check the user name and the key or password in the Vault."
    )


HOST_KEY_REFUSED = (
    f"{HOST} did not prove that it holds the host key in this source's settings. "
    "Check the key with the server's administrator."
)


def _regular(size: int | None, mtime: int = MTIME, atime: int | None = None) -> SFTPAttrs:
    """A regular file's attributes, as SFTP v3 and later versions send them.

    In v3 the type travels inside `permissions`, and the mtime only beside an
    atime, so both are set: without them asyncssh reads type 5 and no mtime. From
    v4 the type is a field of its own, so it is set too.
    """
    return SFTPAttrs(
        type=asyncssh.FILEXFER_TYPE_REGULAR,
        size=size,
        permissions=stat.S_IFREG | 0o644,
        atime=mtime - DAY if atime is None else atime,
        mtime=mtime,
    )


_FILE_TYPES = {
    stat.S_IFDIR: asyncssh.FILEXFER_TYPE_DIRECTORY,
    stat.S_IFLNK: asyncssh.FILEXFER_TYPE_SYMLINK,
    stat.S_IFIFO: asyncssh.FILEXFER_TYPE_FIFO,
    stat.S_IFSOCK: asyncssh.FILEXFER_TYPE_SOCKET,
    stat.S_IFCHR: asyncssh.FILEXFER_TYPE_CHAR_DEVICE,
}


def _typed(kind: int) -> SFTPAttrs:
    """An entry of the file type `kind` (`stat.S_IFLNK`, `S_IFIFO`, ...)."""
    return SFTPAttrs(
        type=_FILE_TYPES[kind], size=0, permissions=kind | 0o755, atime=MTIME - DAY, mtime=MTIME
    )


def _untyped(size: int) -> SFTPAttrs:
    """An entry a v3 server sent without permissions, so asyncssh reads its type as unknown."""
    return SFTPAttrs(size=size, atime=MTIME - DAY, mtime=MTIME)


def _line(key: asyncssh.SSHKey) -> str:
    """The OpenSSH public key line of `key`, as an operator pastes it into the host key field."""
    return key.export_public_key().decode().strip()


def _ssh_login(
    port: int,
    *,
    user: str = USER,
    key: asyncssh.SSHKey | None = None,
    password: str | None = None,
) -> SshCredentialsSecret:
    """An SSH login bound to `sftp.test` on `port`, with a key or a password."""
    return SshCredentialsSecret(
        username=user,
        host=f"{HOST}:{port}",
        private_key=SecretStr(key.export_private_key("openssh").decode()) if key else None,
        password=SecretStr(password) if password else None,
    )


def _config(port: int, host_key: str, root: str) -> ConnectorConfig:
    return {"host": HOST, "port": port, "root_path": root, "host_key": host_key}


def _pin(monkeypatch: pytest.MonkeyPatch, port: int, ip: str = "127.0.0.1") -> None:
    """Answer the SSRF guard for `sftp.test` with the test server's address.

    The guard refuses loopback, and the in-process servers listen there. The
    unit tests check the guard; here it only has to pin the name to the server.
    """
    pinned = PinnedAddress(hostname=HOST, port=port, ips=(ip,))

    def resolve(
        url: str,
        allowed_schemes: frozenset[str] | None = None,
        *,
        trusted_hosts: frozenset[str] = frozenset(),
    ) -> PinnedAddress:
        return pinned

    monkeypatch.setattr(sftp_module, "resolve_pinned_url", resolve)


@dataclass
class _Record:
    """What one in-process server saw: connections, logins and every SFTP request."""

    calls: list[tuple[str, bytes]] = field(default_factory=list)
    connections: list[asyncssh.SSHServerConnection] = field(default_factory=list)
    lost: int = 0
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    logins: list[str] = field(default_factory=list)
    passwords: list[str] = field(default_factory=list)
    prompts_answered: list[list[str]] = field(default_factory=list)
    sessions: int = 0
    denied: frozenset[bytes] = frozenset()
    after_first_read: dict[bytes, Callable[[], Awaitable[None]]] = field(default_factory=dict)

    def seen(self, request: str, path: str) -> int:
        """How many `request`s (`"lstat"`, `"open"`, `"read"`, ...) named `path`."""
        return self.calls.count((request, path.encode()))

    def touched(self, fragment: bytes) -> list[tuple[str, bytes]]:
        """Every request on a path that holds `fragment`."""
        return [call for call in self.calls if fragment in call[1]]

    def on_first_read(self, path: str, action: Callable[[], Awaitable[None]]) -> None:
        self.after_first_read[path.encode()] = action


class _Sshd(asyncssh.SSHServer):
    """The SSH side of every in-process server: who connected, and what each login sent.

    A key login is checked by asyncssh against the authorized key the server was
    started with. `password` turns the password method on, `anyone` lets a
    client in with the `none` method, and `prompt_only` offers the password only
    through keyboard-interactive, as a PAM server does.
    """

    def __init__(
        self,
        record: _Record,
        *,
        password: str | None = None,
        anyone: bool = False,
        prompt_only: bool = False,
    ) -> None:
        self._record = record
        self._password = password
        self._anyone = anyone
        self._prompt_only = prompt_only

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._record.connections.append(conn)

    def connection_lost(self, exc: Exception | None) -> None:
        self._record.lost += 1
        self._record.closed.set()

    def begin_auth(self, username: str) -> bool:
        self._record.logins.append(username)
        return not self._anyone

    def _right(self, username: str, password: str) -> bool:
        return (
            username == USER
            and self._password is not None
            and secrets.compare_digest(password.encode(), self._password.encode())
        )

    def password_auth_supported(self) -> bool:
        return self._password is not None and not self._prompt_only

    def validate_password(self, username: str, password: str) -> bool:
        self._record.passwords.append(password)
        return self._right(username, password)

    def kbdint_auth_supported(self) -> bool:
        return self._prompt_only

    def get_kbdint_challenge(
        self, username: str, lang: str, submethods: str
    ) -> tuple[str, str, str, list[tuple[str, bool]]]:
        return "", "", "", [("Password: ", False)]

    def validate_kbdint_response(self, username: str, responses: list[str]) -> bool:
        self._record.prompts_answered.append(list(responses))
        return len(responses) == 1 and self._right(username, responses[0])


@asynccontextmanager
async def _serving(
    record: _Record,
    host_key: asyncssh.SSHKey,
    sftp: Callable[
        [asyncssh.SSHServerChannel], asyncssh.SFTPServer | Awaitable[asyncssh.SFTPServer]
    ],
    *,
    authorized: asyncssh.SSHKey | None = None,
    password: str | None = None,
    anyone: bool = False,
    prompt_only: bool = False,
) -> AsyncIterator[int]:
    """Listen on a loopback port the system chooses; the port, while the block runs."""
    acceptor = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        authorized_client_keys=(
            asyncssh.import_authorized_keys(authorized.export_public_key().decode())
            if authorized
            else None
        ),
        server_factory=partial(
            _Sshd, record, password=password, anyone=anyone, prompt_only=prompt_only
        ),
        sftp_factory=sftp,
        gss_host=None,
    )
    try:
        yield acceptor.get_port()
    finally:
        for conn in record.connections:
            conn.abort()
        acceptor.close()
        await acceptor.wait_closed()


class _DiskSFTP(asyncssh.SFTPServer):
    """Real files under a chroot. Records each request, refuses to list a `denied`
    folder as a folder of mode 000 does, and runs a test's action once the first
    read of a file has its bytes."""

    def __init__(self, chan: asyncssh.SSHServerChannel, *, root: Path, record: _Record) -> None:
        super().__init__(chan, chroot=str(root))
        self._record = record
        self._open: dict[int, bytes] = {}
        record.sessions += 1

    def _path(self, file_obj: object) -> bytes:
        return self._open.get(id(file_obj), b"")

    # Not an async generator: the server calls this at OPENDIR, so a refusal raised
    # here answers the OPENDIR, as OpenSSH answers a folder it may not read.
    def scandir(self, path: bytes) -> AsyncIterator[SFTPName]:
        self._record.calls.append(("scandir", path))
        if path in self._record.denied:
            raise asyncssh.SFTPPermissionDenied("Permission denied")
        return super().scandir(path)

    def lstat(self, path: bytes) -> object:
        self._record.calls.append(("lstat", path))
        return super().lstat(path)

    def stat(self, path: bytes) -> object:
        self._record.calls.append(("stat", path))
        return super().stat(path)

    def open(self, path: bytes, pflags: int, attrs: SFTPAttrs) -> object:
        self._record.calls.append(("open", path))
        file_obj = super().open(path, pflags, attrs)
        self._open[id(file_obj)] = path
        return file_obj

    async def read(self, file_obj: object, offset: int, size: int) -> bytes:
        path = self._path(file_obj)
        self._record.calls.append(("read", path))
        data = cast(bytes, super().read(file_obj, offset, size))
        action = self._record.after_first_read.pop(path, None)
        if action is not None:
            await action()
        return data

    def fstat(self, file_obj: object) -> object:
        self._record.calls.append(("fstat", self._path(file_obj)))
        return super().fstat(file_obj)

    def close(self, file_obj: object) -> None:
        self._record.calls.append(("close", self._open.pop(id(file_obj), b"")))
        super().close(file_obj)


def _plant_tree(folder: Path, outside: Path) -> None:
    """The `mkdata.sh` tree (servers section 1), with the outside link kept inside `tmp_path`.

    The links with a `.txt` name are extra: a connector that read a link as the
    file it points at would list them, where the extension filter alone hides
    the extension-less names of `mkdata.sh`.
    """
    (folder / "dir" / "sub").mkdir(parents=True)
    (folder / "top.txt").write_bytes(b"top file\n")
    (folder / "subsecond.txt").write_bytes(b"subsecond\n")
    (folder / "spaced name ü €.txt").write_bytes(b"spaces and unicode\n")
    (folder / "dir" / "inner.txt").write_bytes(b"inner file\n")
    (folder / "dir" / "sub" / "deep.txt").write_bytes(b"deep\n")
    (folder / "unreadable").mkdir()
    (folder / "unreadable" / "x").write_bytes(b"hidden\n")
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"outside the root\n")
    for suffix in ("", ".txt"):
        (folder / f"link-to-file{suffix}").symlink_to("top.txt")
        (folder / f"link-outside{suffix}").symlink_to(outside)
        (folder / f"link-loop{suffix}").symlink_to(f"link-loop{suffix}")
        (folder / f"link-broken{suffix}").symlink_to("does-not-exist")
    (folder / "link-to-dir").symlink_to("dir")


@dataclass
class _Folder:
    """A folder of the hostile server: its entries, or names without end, and how it answers.

    `dots` sends `.` and `..` first, as every real server does. `delay` seconds
    pass before the first entry, or before the end of a folder with none. `stall`
    never answers the first READDIR while the test runs, `refusal` answers the
    OPENDIR with that error, and `drop` breaks the connection there. `endless`
    sends names without end: `"special"` links, FIFOs and sockets, or `"dots"`.
    `close_stall` holds the CLOSE of each listing of it opened from then on.
    """

    entries: list[tuple[bytes, SFTPAttrs]] = field(default_factory=list)
    endless: str = ""
    close_stall: bool = False
    dots: bool = True
    delay: float = 0.0
    stall: bool = False
    refusal: asyncssh.SFTPError | None = None
    drop: bool = False


# Which request each step of `_STEPS` is: the request, and how many came before it.
_STALLS = {
    "lstat": ("lstat", 0),
    "open": ("open", 0),
    "fstat-before-the-read": ("fstat", 0),
    "fstat-after-the-read": ("fstat", 1),
    "lstat-after-the-read": ("lstat", 1),
}


@dataclass
class _Fake:
    """A file of the hostile server: its answers to LSTAT, STAT, OPEN, FSTAT and READ.

    `lstats`, `stats` and `fstats` are the answers in order, the last one
    repeated; an `SFTPError` in them is raised. Without them the file is regular
    and the size of `data`, and a STAT, which follows a link, answers as the LSTAT
    does. `reads` answers each READ; without it, the slice of `data` asked for. A
    CLOSE raises `close_error`, or with `close_stall` is never answered while the
    test runs. `stall_at`, a step of `_STEPS`, is a request of the fetch that is
    never answered.
    """

    data: bytes = b""
    lstats: list[SFTPAttrs | asyncssh.SFTPError] | None = None
    stats: list[SFTPAttrs | asyncssh.SFTPError] | None = None
    fstats: list[SFTPAttrs | asyncssh.SFTPError] | None = None
    open_error: asyncssh.SFTPError | None = None
    reads: Callable[[int, int], Awaitable[bytes]] | None = None
    close_error: asyncssh.SFTPError | None = None
    close_stall: bool = False
    stall_at: str | None = None
    asked: dict[str, int] = field(default_factory=dict)
    paused: dict[str, int] = field(default_factory=dict)

    def answer(self, request: str) -> SFTPAttrs:
        given = {"lstat": self.lstats, "stat": self.stats or self.lstats, "fstat": self.fstats}
        answers = given[request] or [_regular(len(self.data))]
        count = self.asked.get(request, 0)
        self.asked[request] = count + 1
        found = answers[min(count, len(answers) - 1)]
        if isinstance(found, asyncssh.SFTPError):
            raise type(found)(found.reason)
        return found

    async def read(self, offset: int, size: int) -> bytes:
        if self.reads is not None:
            return await self.reads(offset, size)
        return self.data[offset : offset + size]


def _sending(
    produce: Callable[[int, int], bytes], *, delay: float = 0.0
) -> Callable[[int, int], Awaitable[bytes]]:
    """A READ that answers `produce(offset, size)`, after `delay` seconds."""

    async def read(offset: int, size: int) -> bytes:
        if delay:
            await asyncio.sleep(delay)
        return produce(offset, size)

    return read


@dataclass
class _Hostile:
    """The hostile server's made-up namespace, and how to reach it.

    Paths are absolute and there is no chroot: nothing it answers touches the
    disk. A test builds the folders and files it needs with `add_folder`,
    `add_entry`, `add_file` and `add_endless`, then syncs from it.
    """

    record: _Record = field(default_factory=_Record)
    folders: dict[bytes, _Folder] = field(default_factory=dict)
    files: dict[bytes, _Fake] = field(default_factory=dict)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    names_sent: int = 0
    init_stall: bool = False
    held: list[object] = field(default_factory=list)
    port: int = 0
    host_key: str = ""
    login: SshCredentialsSecret | None = None

    def config(self, root: str, **overrides: object) -> ConnectorConfig:
        return {**_config(self.port, self.host_key, root), **overrides}

    @property
    def credential(self) -> SshCredentialsSecret:
        assert self.login is not None
        return self.login

    def add_folder(self, path: str) -> _Folder:
        """The folder at `path`, made with its parents, each listed in its parent."""
        key = path.encode()
        if key not in self.folders:
            self.folders[key] = _Folder()
            parent = PurePosixPath(path).parent
            if str(parent) != path:
                self.add_folder(str(parent))
                self.add_entry(str(parent), PurePosixPath(path).name.encode(), _typed(stat.S_IFDIR))
        return self.folders[key]

    def add_entry(self, folder: str, name: bytes, attrs: SFTPAttrs) -> None:
        """One raw entry in `folder`'s listing: any bytes as a name, any attributes."""
        self.add_folder(folder).entries.append((name, attrs))

    def add_file(self, path: str, fake: _Fake, *, listed: SFTPAttrs | None = None) -> None:
        """A file `fake` answers for, listed in its folder as `listed` (regular, its size)."""
        place = PurePosixPath(path)
        self.add_entry(str(place.parent), place.name.encode(), listed or _regular(len(fake.data)))
        self.files[path.encode()] = fake

    def add_endless(self, path: str, kind: str = "special") -> None:
        """A folder whose listing never ends: links, FIFOs and sockets, or `.` and `..`."""
        self.add_folder(path).endless = kind

    async def pause(self, path: bytes, request: str) -> None:
        """Never answer `request` for `path`, while the test runs, if its file stalls there."""
        fake = self.files.get(path)
        if fake is None or fake.stall_at is None:
            return
        count = fake.paused.get(request, 0)
        fake.paused[request] = count + 1
        if _STALLS[fake.stall_at] == (request, count):
            await self.release.wait()

    def unlist(self, path: str) -> None:
        """Take `path` out of its folder's listing: it is gone, whatever its stats answer."""
        place = PurePosixPath(path)
        folder = self.folders[str(place.parent).encode()]
        folder.entries = [entry for entry in folder.entries if entry[0] != place.name.encode()]

    async def stall(self, offset: int, size: int) -> bytes:
        """A READ that is never answered while the test runs."""
        await self.release.wait()
        return b""

    def answer(self, path: bytes, request: str) -> SFTPAttrs:
        fake = self.files.get(path)
        if fake is not None:
            return fake.answer(request)
        if path in self.folders:
            return _typed(stat.S_IFDIR)
        raise asyncssh.SFTPNoSuchFile("No such file")

    async def names(self, folder: _Folder) -> AsyncIterator[SFTPName]:
        if folder.stall:
            await self.release.wait()
        if folder.delay:
            await asyncio.sleep(folder.delay)
        if folder.dots:
            for dots in (b".", b".."):
                yield SFTPName(dots, attrs=_typed(stat.S_IFDIR))
        for name, attrs in folder.entries:
            yield SFTPName(name, attrs=attrs)
        kinds = (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK)
        number = 0
        while folder.endless:
            self.names_sent += 1
            if folder.endless == "dots":
                yield SFTPName(b"." * (1 + number % 2), attrs=_typed(stat.S_IFDIR))
            else:
                yield SFTPName(b"junk-%09d" % number, attrs=_typed(kinds[number % len(kinds)]))
            number += 1


class _HostileSFTP(asyncssh.SFTPServer):
    """An SFTP server that answers each request from a `_Hostile` and records it."""

    def __init__(self, chan: asyncssh.SSHServerChannel, *, hostile: _Hostile) -> None:
        super().__init__(chan)
        self._hostile = hostile
        hostile.record.sessions += 1

    def _seen(self, request: str, path: bytes) -> None:
        self._hostile.record.calls.append((request, path))

    def scandir(self, path: bytes) -> AsyncIterator[SFTPName]:
        self._seen("scandir", path)
        # A real server resolves `..` and a doubled slash; so does this one.
        folder = self._hostile.folders.get(posixpath.normpath(path))
        if folder is None:
            raise asyncssh.SFTPNoSuchFile("No such file")
        if folder.refusal is not None:
            raise type(folder.refusal)(folder.refusal.reason)
        if folder.drop:
            self.connection.abort()
        names = self._hostile.names(folder)
        if folder.close_stall:
            self._hostile.held.append(names)
        return names

    async def lstat(self, path: bytes) -> SFTPAttrs:
        self._seen("lstat", path)
        await self._hostile.pause(path, "lstat")
        return self._hostile.answer(path, "lstat")

    def stat(self, path: bytes) -> SFTPAttrs:
        self._seen("stat", path)
        return self._hostile.answer(path, "stat")

    async def open(self, path: bytes, pflags: int, attrs: SFTPAttrs) -> bytes:
        self._seen("open", path)
        await self._hostile.pause(path, "open")
        fake = self._hostile.files.get(path)
        if fake is None:
            raise asyncssh.SFTPNoSuchFile("No such file")
        if fake.open_error is not None:
            raise type(fake.open_error)(fake.open_error.reason)
        return path

    async def read(self, file_obj: object, offset: int, size: int) -> bytes:
        path = cast(bytes, file_obj)
        self._seen("read", path)
        return await self._hostile.files[path].read(offset, size)

    async def fstat(self, file_obj: object) -> SFTPAttrs:
        path = cast(bytes, file_obj)
        self._seen("fstat", path)
        await self._hostile.pause(path, "fstat")
        return self._hostile.answer(path, "fstat")

    async def close(self, file_obj: object) -> None:
        path = cast(bytes, file_obj)
        self._seen("close", path)
        fake = self._hostile.files[path]
        if fake.close_stall:
            await self._hostile.release.wait()
        if fake.close_error is not None:
            raise type(fake.close_error)(fake.close_error.reason)


def _hold_folder_closes(monkeypatch: pytest.MonkeyPatch, hostile: _Hostile) -> None:
    """Hold the CLOSE of each folder listing in `hostile.held`, while the test runs.

    asyncssh's server answers a folder's CLOSE itself and never asks the
    `SFTPServer`, so the hold goes in front of its handler for that request.
    """
    answer = SFTPServerHandler._packet_handlers[FXP_CLOSE]

    async def close(handler: SFTPServerHandler, packet: SSHPacket) -> object:
        handle = SSHPacket(packet.get_remaining_payload()).get_string()
        listing = handler._dir_handles.get(handle)
        if listing is not None and any(listing is held for held in hostile.held):
            await hostile.release.wait()
        return await answer(handler, packet)

    monkeypatch.setitem(SFTPServerHandler._packet_handlers, FXP_CLOSE, close)


class _Writer(Protocol):
    """Changes a server's files behind the sync's back."""

    async def write(self, path: str, data: bytes, *, mtime: int = MTIME) -> None: ...

    async def grow(self, path: str, data: bytes) -> None: ...

    async def replace(self, path: str, data: bytes, *, mtime: int = MTIME) -> None: ...

    async def remove(self, path: str) -> None: ...

    async def remove_folder(self, path: str) -> None: ...


class _LocalWriter:
    """The disk server's files, changed on the disk under its chroot."""

    def __init__(self, chroot: Path) -> None:
        self._chroot = chroot

    def _local(self, path: str) -> Path:
        return self._chroot / path.lstrip("/")

    async def write(self, path: str, data: bytes, *, mtime: int = MTIME) -> None:
        local = self._local(path)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        os.utime(local, (mtime - DAY, mtime))

    async def grow(self, path: str, data: bytes) -> None:
        """More bytes at the end of the same file, as an upload in progress adds them."""
        local = self._local(path)
        local.write_bytes(local.read_bytes() + data)

    async def replace(self, path: str, data: bytes, *, mtime: int = MTIME) -> None:
        """Another file renamed over `path`, as an upload to a temporary name ends."""
        local = self._local(path)
        spare = local.with_name(f"{local.name}.new")
        spare.write_bytes(data)
        os.utime(spare, (mtime - DAY, mtime))
        spare.replace(local)

    async def remove(self, path: str) -> None:
        self._local(path).unlink()

    async def remove_folder(self, path: str) -> None:
        shutil.rmtree(self._local(path))

    def swap_for_link(self, path: str, target: Path) -> None:
        """`path` becomes a symbolic link to `target`, which may be outside the chroot."""
        local = self._local(path)
        local.unlink()
        local.symlink_to(target)


class _RemoteWriter:
    """A real server's files, changed through a second SFTP client."""

    def __init__(self, sftp: asyncssh.SFTPClient) -> None:
        self._sftp = sftp

    async def write(self, path: str, data: bytes, *, mtime: int = MTIME) -> None:
        await self._sftp.makedirs(str(PurePosixPath(path).parent), exist_ok=True)
        async with self._sftp.open(path, "wb") as file:
            await file.write(data)
        await self._sftp.utime(path, (mtime - DAY, mtime))

    async def grow(self, path: str, data: bytes) -> None:
        size = (await self._sftp.stat(path)).size or 0
        async with self._sftp.open(path, "r+b") as file:
            await file.write(data, size)

    async def replace(self, path: str, data: bytes, *, mtime: int = MTIME) -> None:
        """A remove, then a rename: a server without `posix-rename@openssh.com` takes it."""
        spare = f"{path}.new"
        await self.write(spare, data, mtime=mtime)
        await self._sftp.remove(path)
        await self._sftp.rename(spare, path)

    async def remove(self, path: str) -> None:
        await self._sftp.remove(path)

    async def remove_folder(self, path: str) -> None:
        await self._sftp.rmtree(path)


@dataclass
class _Served:
    """One SFTP server a test syncs from: in-process or real, with its own folder."""

    kind: str
    port: int
    host_key: str
    user: str
    login: SshCredentialsSecret
    root: str
    tree: str | None
    writer: _Writer
    record: _Record | None
    hook: Callable[[str, Callable[[], Awaitable[None]]], None]
    tree_problem: str | None = None

    def path(self, relative: str) -> str:
        """The absolute remote path of `relative`, under this test's own folder."""
        return str(PurePosixPath(self.root) / relative)

    def config(self, root: str | None = None, **overrides: object) -> ConnectorConfig:
        return {**_config(self.port, self.host_key, root or self.root), **overrides}

    def address(self, path: str) -> str:
        """The `source_path` a file at `path` must be listed under."""
        return f"sftp://{quote(self.user, safe='')}@{HOST}:{self.port}{path}"

    def login_with(
        self, *, key: asyncssh.SSHKey | None = None, password: str | None = None
    ) -> SshCredentialsSecret:
        return _ssh_login(self.port, user=self.user, key=key, password=password)

    def after_first_read(self, path: str, action: Callable[[], Awaitable[None]]) -> None:
        """Run `action` once the first read of `path` has its bytes, before the next one."""
        self.hook(path, action)

    def needs_tree(self) -> str:
        """The folder of the `mkdata.sh` tree, or a skip on a real server that has none.

        A tree without the `.txt` links fails here, so a test of links cannot pass
        only because the extension filter hid every link.
        """
        if self.tree is None:
            pytest.skip("SFTP_TEST_TREE is not set: no tree with links on this server")
        if self.tree_problem is not None:
            pytest.fail(self.tree_problem)
        return self.tree


@asynccontextmanager
async def _disk_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_key: asyncssh.SSHKey,
    client_key: asyncssh.SSHKey,
) -> AsyncIterator[_Served]:
    chroot = tmp_path / "srv"
    (chroot / "data").mkdir(parents=True)
    _plant_tree(chroot / "tree", outside=tmp_path / "outside" / "secret.txt")
    record = _Record(denied=frozenset({b"/tree/unreadable"}))
    sftp = partial(_DiskSFTP, root=chroot, record=record)
    async with _serving(record, host_key, sftp, authorized=client_key, password=PASSWORD) as port:
        _pin(monkeypatch, port)
        yield _Served(
            kind="in-process",
            port=port,
            host_key=_line(host_key),
            user=USER,
            login=_ssh_login(port, key=client_key),
            root="/data",
            tree="/tree",
            writer=_LocalWriter(chroot),
            record=record,
            hook=record.on_first_read,
        )


_REAL_SERVER = (
    "SFTP_TEST_HOST",
    "SFTP_TEST_PORT",
    "SFTP_TEST_USER",
    "SFTP_TEST_HOST_KEY",
    "SFTP_TEST_ROOT",
)


def _hook_client_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str, Callable[[], Awaitable[None]]], None]:
    """`after_first_read` on a real server, which runs no code of ours: the action
    runs on the client side, once the connector's first read has returned."""

    def hook(_path: str, action: Callable[[], Awaitable[None]]) -> None:
        original = asyncssh.SFTPClientFile.read
        pending = [action]

        async def read(
            self: asyncssh.SFTPClientFile, size: int = -1, offset: int | None = None
        ) -> bytes:
            data = cast(bytes, await original(self, size, offset))
            if pending:
                await pending.pop()()
            return data

        monkeypatch.setattr(asyncssh.SFTPClientFile, "read", read)

    return hook


def _check_real_folders(root: PurePosixPath, tree: PurePosixPath | None) -> None:
    """Fail at once, with what to set, on a root or tree a real-server run cannot use.

    A relative root fails every test at the source's own validation, after its
    folder was made. A tree that holds the root, or a root that holds the tree,
    puts the folders of the tests running beside it into the tree's listing.
    """
    if not root.is_absolute():
        pytest.fail(f"SFTP_TEST_ROOT must be an absolute folder, not {root}")
    if tree is None:
        return
    if not tree.is_absolute():
        pytest.fail(f"SFTP_TEST_TREE must be an absolute folder, not {tree}")
    if tree == root or tree in root.parents or root in tree.parents:
        pytest.fail(
            f"SFTP_TEST_TREE ({tree}) and SFTP_TEST_ROOT ({root}) must not hold one another: "
            "a listing of the tree would walk the folders other tests write"
        )


# The links of the tree that the extension filter of the link test keeps.
_TREE_LINKS = ("link-to-file.txt", "link-outside.txt", "link-loop.txt", "link-broken.txt")


async def _missing_tree_links(sftp: asyncssh.SFTPClient, tree: str) -> str | None:
    """Why the tree cannot prove that links are skipped, or `None` when it can.

    Asked with READDIR, which reports a link as a link on every server tested.
    ProFTPD's LSTAT answers with the target's attributes.
    """
    types = {entry.filename: entry.attrs.type for entry in await sftp.readdir(tree)}
    missing = [name for name in _TREE_LINKS if types.get(name) != asyncssh.FILEXFER_TYPE_SYMLINK]
    if not missing:
        return None
    return (
        f"SFTP_TEST_TREE ({tree}) needs these links, with names the extension filter "
        f"keeps: {', '.join(missing)}"
    )


@asynccontextmanager
async def _real_server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Served]:
    env = {name: os.environ.get(name, "") for name in _REAL_SERVER}
    password = os.environ.get("SFTP_TEST_PASSWORD", "")
    key_file = os.environ.get("SFTP_TEST_KEY_FILE", "")
    if not any(name.startswith("SFTP_TEST_") and value for name, value in os.environ.items()):
        pytest.skip("no real SFTP server: no SFTP_TEST_* variable is set")
    missing = [name for name, value in env.items() if not value]
    if not (password or key_file):
        missing.append("SFTP_TEST_PASSWORD or SFTP_TEST_KEY_FILE")
    if missing:
        pytest.fail(f"The real SFTP server is set up in part: set {', '.join(missing)} too")
    if password and len(password) < MIN_CREDENTIAL_LENGTH:
        pytest.fail(
            f"SFTP_TEST_PASSWORD must be at least {MIN_CREDENTIAL_LENGTH} characters, "
            "as an SSH login's password in the vault is"
        )
    if password and key_file:
        pytest.fail(
            "Set SFTP_TEST_PASSWORD or SFTP_TEST_KEY_FILE, not both: a login has one of them"
        )
    tree = os.environ.get("SFTP_TEST_TREE") or None
    _check_real_folders(PurePosixPath(env["SFTP_TEST_ROOT"]), PurePosixPath(tree) if tree else None)
    port = int(env["SFTP_TEST_PORT"])
    found = await asyncio.get_running_loop().getaddrinfo(
        env["SFTP_TEST_HOST"], port, type=socket.SOCK_STREAM
    )
    ip = str(found[0][4][0])
    key = asyncssh.import_private_key(Path(key_file).read_text()) if key_file else None
    user = env["SFTP_TEST_USER"]
    pinned = asyncssh.import_public_key(env["SFTP_TEST_HOST_KEY"])
    _pin(monkeypatch, port, ip)
    async with (
        asyncssh.connect(
            ip,
            port,
            config=None,
            known_hosts=([pinned], [], [], [], [], [], []),
            username=user,
            password=password or None,
            client_keys=[key] if key else None,
            agent_path=None,
            gss_host=None,
            preferred_auth="publickey" if key else "password",
        ) as conn,
        conn.start_sftp_client() as sftp,
    ):
        tree_problem = await _missing_tree_links(sftp, tree) if tree else None
        root = str(PurePosixPath(env["SFTP_TEST_ROOT"]) / f"agenticos-{uuid.uuid4().hex[:10]}")
        await sftp.mkdir(root)
        try:
            yield _Served(
                kind="real",
                port=port,
                host_key=env["SFTP_TEST_HOST_KEY"],
                user=user,
                login=_ssh_login(port, user=user, key=key, password=password or None),
                root=root,
                tree=tree,
                writer=_RemoteWriter(sftp),
                record=None,
                hook=_hook_client_reads(monkeypatch),
                tree_problem=tree_problem,
            )
        finally:
            await sftp.rmtree(root)


@pytest.fixture(autouse=True)
def worker_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty `HOME` and no agent, for every test in this file.

    asyncssh reads `~/.ssh` and `SSH_AUTH_SOCK` by default. A connector that
    stopped turning that off would otherwise pass or fail here by what the
    runner's own folder holds. A test that needs a lure writes it into this
    folder, or sets `HOME` again.
    """
    home = tmp_path / "worker-home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    return home


@pytest.fixture
def host_key() -> asyncssh.SSHKey:
    return asyncssh.generate_private_key("ssh-ed25519")


@pytest.fixture
def client_key() -> asyncssh.SSHKey:
    return asyncssh.generate_private_key("ssh-ed25519")


@pytest.fixture
def out(tmp_path: Path) -> Path:
    """Where a download lands: outside every server's chroot."""
    folder = tmp_path / "downloads"
    folder.mkdir()
    return folder


@pytest.fixture
async def disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_key: asyncssh.SSHKey,
    client_key: asyncssh.SSHKey,
) -> AsyncIterator[_Served]:
    """The in-process disk server, for the tests no real server can run."""
    async with _disk_server(tmp_path, monkeypatch, host_key, client_key) as served:
        yield served


@pytest.fixture(params=["in-process", "real"])
async def server(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_key: asyncssh.SSHKey,
    client_key: asyncssh.SSHKey,
) -> AsyncIterator[_Served]:
    """The disk server, and a real one when the `SFTP_TEST_*` variables name it."""
    if request.param == "real":
        async with _real_server(monkeypatch) as served:
            yield served
    else:
        async with _disk_server(tmp_path, monkeypatch, host_key, client_key) as served:
            yield served


@pytest.fixture
async def hostile(
    monkeypatch: pytest.MonkeyPatch, host_key: asyncssh.SSHKey, client_key: asyncssh.SSHKey
) -> AsyncIterator[_Hostile]:
    hostile = _Hostile()
    _hold_folder_closes(monkeypatch, hostile)

    async def sftp(chan: asyncssh.SSHServerChannel) -> _HostileSFTP:
        if hostile.init_stall:
            await hostile.release.wait()
        return _HostileSFTP(chan, hostile=hostile)

    async with _serving(hostile.record, host_key, sftp, authorized=client_key) as port:
        _pin(monkeypatch, port)
        hostile.port = port
        hostile.host_key = _line(host_key)
        hostile.login = _ssh_login(port, key=client_key)
        try:
            yield hostile
        finally:
            hostile.release.set()


@pytest.fixture
async def connector() -> AsyncIterator[SftpConnector]:
    """One sync's connector, closed when the test ends."""
    connector = SftpConnector()
    try:
        yield connector
    finally:
        await connector.aclose()


async def _listed(connector: SftpConnector, served: _Served) -> dict[str, RemoteFile]:
    listing = await connector.list_files(served.config(), served.login)
    return {file.id: file for file in listing.files}


async def _listed_hostile(
    connector: SftpConnector, hostile: _Hostile, root: str
) -> dict[str, RemoteFile]:
    listing = await connector.list_files(hostile.config(root), hostile.credential)
    return {file.id: file for file in listing.files}


@pytest.mark.parametrize("size", [64, 600 * 1024], ids=["one-read", "three-reads"])
async def test_an_unchanged_file_lists_and_reads_with_the_same_revision(
    server: _Served, connector: SftpConnector, out: Path, size: int
) -> None:
    """What `new_only` compares: the revision a listing reports, against the one a
    download stored. For an unchanged file they must be equal, or nothing is ever
    skipped. 600 KiB takes three reads of `CHUNK_BYTES`, so the bytes must also
    arrive whole and in order."""
    body = os.urandom(size)
    path = server.path("report.pdf")
    await server.writer.write(path, body, mtime=MTIME)

    listed = (await _listed(connector, server))[path]
    fetched = await connector.fetch_file(
        listed, out, config=server.config(), credential=server.login
    )

    assert listed.revision == f"{size}:{MTIME}"
    assert fetched.revision == listed.revision
    assert fetched.path.read_bytes() == body
    if server.record is not None:
        assert server.record.seen("open", path) == server.record.seen("close", path) == 1


async def test_a_file_changed_after_the_listing_is_read_as_its_new_version(
    server: _Served, connector: SftpConnector, out: Path
) -> None:
    """A listing is minutes old when a large source reaches a file. The download
    reports the revision of the bytes it wrote, from a stat after the read, never
    the listed one. Stored under the listed revision, the new bytes would be
    skipped for good once the old ones came back."""
    path = server.path("policy.md")
    await server.writer.write(path, b"the policy, before", mtime=MTIME)
    listed = (await _listed(connector, server))[path]
    edited = b"the policy, after an edit"
    await server.writer.write(path, edited, mtime=MTIME + 60)

    fetched = await connector.fetch_file(
        listed, out, config=server.config(), credential=server.login
    )

    assert fetched.path.read_bytes() == edited
    assert fetched.revision == f"{len(edited)}:{MTIME + 60}"
    assert fetched.revision == (await _listed(connector, server))[path].revision


@pytest.mark.parametrize("change", ["grown", "rewritten-in-place"])
async def test_a_file_changed_during_the_read_is_skipped_as_changed(
    server: _Served, connector: SftpConnector, out: Path, change: str
) -> None:
    """A file that is still being written when it is read. The handle's FSTAT after
    the read differs from the one before it: evidence from the server that the
    bytes are a mix of two versions. A rewrite of the same length moves only the
    mtime, and that counts as much as the size. The file is skipped, and the next
    run reads it whole."""
    path = server.path("growing.pdf")
    await server.writer.write(path, b"the first half", mtime=MTIME)
    listed = (await _listed(connector, server))[path]
    server.after_first_read(
        path,
        partial(server.writer.grow, path, b", and the second half")
        if change == "grown"
        else partial(server.writer.write, path, b"the other half", mtime=MTIME + 60),
    )

    with pytest.raises(ChangedSinceListing) as raised:
        await connector.fetch_file(listed, out, config=server.config(), credential=server.login)

    assert raised.value.message == "growing.pdf changed while it was downloaded."


@pytest.mark.parametrize(
    ("replacement", "mtime"),
    [(b"the new report, a longer one", MTIME), (b"the new report", MTIME + 60)],
    ids=["longer", "same-length"],
)
async def test_a_file_renamed_over_during_the_read_is_skipped_as_changed(
    server: _Served, connector: SftpConnector, out: Path, replacement: bytes, mtime: int
) -> None:
    """An upload to a temporary name, renamed over the file while it is read. The
    open handle still reads the old file, so its two FSTATs agree. Only the LSTAT
    of the path after the read shows that the path names another file now. A
    re-sent scan of the same length differs only by its mtime, and is caught as
    surely: missed, its revision would be stored for the old bytes."""
    path = server.path("report.pdf")
    await server.writer.write(path, b"the old report", mtime=MTIME)
    listed = (await _listed(connector, server))[path]
    server.after_first_read(path, partial(server.writer.replace, path, replacement, mtime=mtime))

    with pytest.raises(ChangedSinceListing) as raised:
        await connector.fetch_file(listed, out, config=server.config(), credential=server.login)

    # A real server whose FSTAT reads the path, not the handle, sees the new file
    # one step earlier and says "changed". The disk server's FSTAT reads the handle.
    if server.kind == "in-process":
        assert raised.value.message == "report.pdf was replaced while it was downloaded."


@pytest.mark.parametrize(
    ("removed", "when"),
    [("file", "before"), ("folder", "before"), ("file", "while")],
    ids=["the-file", "its-folder", "during-the-read"],
)
async def test_a_file_deleted_after_the_listing_is_skipped_as_changed(
    server: _Served, connector: SftpConnector, out: Path, removed: str, when: str
) -> None:
    """An answer of "no such file" alone is not evidence: a server can give it for a
    file it still has. A listing of the folder that no longer names the file is,
    though it still names another, and so is a folder that is gone. Then the file is skipped, and the next complete listing
    removes its document."""
    folder = server.path("outgoing")
    path = f"{folder}/withdrawn.pdf"
    await server.writer.write(path, b"withdrawn")
    await server.writer.write(f"{folder}/kept.pdf", b"kept")
    listed = (await _listed(connector, server))[path]
    remove = (
        partial(server.writer.remove_folder, folder)
        if removed == "folder"
        else partial(server.writer.remove, path)
    )
    if when == "before":
        await remove()
    else:
        server.after_first_read(path, remove)

    with pytest.raises(ChangedSinceListing) as raised:
        await connector.fetch_file(listed, out, config=server.config(), credential=server.login)

    assert raised.value.message == f"withdrawn.pdf was deleted {when} it was downloaded."


# The requests of a fetch, in order: LSTAT, OPEN, FSTAT, the reads, FSTAT, LSTAT.
_STEPS = ("lstat", "open", "fstat-before-the-read", "fstat-after-the-read", "lstat-after-the-read")


# Every request about the file in a fetch: the steps of `_STEPS`, and the READ itself.
_FILE_REQUESTS = (*_STEPS[:3], "read", *_STEPS[3:])


def _failing_at(step: str, error: asyncssh.SFTPError, data: bytes) -> _Fake:
    """A file whose fetch meets `error` at `step` of `_FILE_REQUESTS`, and nowhere else."""
    fake = _Fake(data=data)

    async def refuse(_offset: int, _size: int) -> bytes:
        raise type(error)(error.reason)

    if step == "lstat":
        fake.lstats = [error]
    elif step == "open":
        fake.open_error = error
    elif step == "fstat-before-the-read":
        fake.fstats = [error]
    elif step == "read":
        fake.reads = refuse
    elif step == "fstat-after-the-read":
        fake.fstats = [_regular(len(data)), error]
    else:
        fake.lstats = [_regular(len(data)), error]
    return fake


def _opened(step: str) -> int:
    """How many handles a fetch that fails at `step` opened, and so must close."""
    return 0 if step in ("lstat", "open") else 1


@pytest.mark.parametrize("step", _FILE_REQUESTS)
async def test_a_file_the_server_still_lists_but_cannot_stat_is_a_failure(
    hostile: _Hostile, connector: SftpConnector, out: Path, step: str
) -> None:
    """A server that lists a file and answers "no such file" for it, at any request
    of the fetch, the READ included. Taken for a deletion, the file would be skipped on every run while
    the sync reported success. The listing of the file's own folder still names it,
    so it is a failure the sync log shows. The file is one folder below the root,
    so a listing of any other folder would not name it, and its folder holds a name
    that is not UTF-8 before it, which the search compares as bytes. An open handle
    is closed."""
    path = "/files/sub/ghost.pdf"
    hostile.add_entry("/files/sub", b"r\xe9sum\xe9.pdf", _regular(4))
    hostile.add_file(path, _failing_at(step, asyncssh.SFTPNoSuchFile("No such file"), b"ghost"))
    listed = (await _listed_hostile(connector, hostile, "/files"))[path]

    with pytest.raises(ExternalServiceError) as raised:
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert raised.value.message == "The server lists ghost.pdf, but answers that it does not exist."
    assert hostile.record.seen("close", path) == _opened(step)
    if step in _STEPS[:3]:
        assert hostile.record.seen("read", path) == 0


@pytest.mark.parametrize("step", _FILE_REQUESTS)
async def test_a_file_that_answers_no_such_file_and_is_no_longer_listed_is_skipped_as_changed(
    hostile: _Hostile, connector: SftpConnector, out: Path, step: str
) -> None:
    """A file deleted after the listing answers "no such file" at whichever request
    meets the deletion first. The listing of its folder no longer names it, though
    it names another file: that is the evidence, and the file is skipped as changed. Before a byte is read it was
    deleted "before" it was downloaded, from the READ on "while". SFTPGo's FSTAT reads
    the path, so a deletion during the read can reach the FSTAT first. The folder
    also holds a name that is not UTF-8, which the search compares as bytes. An
    open handle is closed."""
    path = "/files/sub/withdrawn.pdf"
    hostile.add_entry("/files/sub", b"r\xe9sum\xe9.pdf", _regular(4))
    hostile.add_file(path, _failing_at(step, asyncssh.SFTPNoSuchFile("No such file"), b"withdrawn"))
    hostile.add_file("/files/sub/kept.pdf", _Fake(data=b"kept"))
    listed = (await _listed_hostile(connector, hostile, "/files"))[path]
    hostile.unlist(path)

    with pytest.raises(ChangedSinceListing) as raised:
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    when = "before" if step in _STEPS[:3] else "while"
    assert raised.value.message == f"withdrawn.pdf was deleted {when} it was downloaded."
    assert hostile.record.seen("close", path) == _opened(step)
    if when == "before":
        assert hostile.record.seen("read", path) == 0


async def test_a_server_whose_fstat_and_lstat_disagree_still_reads_an_unchanged_file(
    hostile: _Hostile, connector: SftpConnector, out: Path
) -> None:
    """A server may answer an FSTAT and an LSTAT of one unchanged file with two
    mtimes. Each stat is compared with a stat of its own kind, so the file is not
    taken for changed, and the revision returned is the LSTAT's, which the next
    listing reports too. Compared across, the file would be skipped on every run;
    with the FSTAT's revision stored, it would be downloaded on every run."""
    by_path = _regular(4, mtime=MTIME)
    by_handle = _regular(4, mtime=MTIME + 1)
    hostile.add_file(
        "/files/odd.pdf", _Fake(data=b"odd!", lstats=[by_path], fstats=[by_handle]), listed=by_path
    )
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/odd.pdf"]

    fetched = await connector.fetch_file(
        listed, out, config=hostile.config("/files"), credential=hostile.credential
    )

    assert fetched.path.read_bytes() == b"odd!"
    assert fetched.revision == listed.revision == f"4:{MTIME}"


async def test_a_file_whose_atime_alone_moves_during_the_read_reads_as_unchanged(
    hostile: _Hostile, connector: SftpConnector, out: Path
) -> None:
    """Reading a file moves its atime on most servers, so the stats after the read
    can differ from those before it in the atime alone. That is no change: the
    file reads, under the revision of its size and mtime."""
    before, after = _regular(4), _regular(4, atime=MTIME + 5)
    hostile.add_file(
        "/files/read.pdf", _Fake(data=b"read", lstats=[before, after], fstats=[before, after])
    )
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/read.pdf"]

    fetched = await connector.fetch_file(
        listed, out, config=hostile.config("/files"), credential=hostile.credential
    )

    assert fetched.path.read_bytes() == b"read"
    assert fetched.revision == listed.revision == f"4:{MTIME}"


# A link with the size and the mtime of the regular file it replaced.
_LINK_LIKE_THE_FILE = SFTPAttrs(
    size=4, permissions=stat.S_IFLNK | 0o777, atime=MTIME - DAY, mtime=MTIME
)


@pytest.mark.parametrize(
    ("moved", "sentence"),
    [
        ("fstat-mtime", "rewritten.pdf changed while it was downloaded."),
        ("fstat-type", "rewritten.pdf changed while it was downloaded."),
        ("lstat-type", "rewritten.pdf was replaced while it was downloaded."),
    ],
)
async def test_a_file_whose_mtime_or_type_alone_moves_during_the_read_is_skipped_as_changed(
    hostile: _Hostile, connector: SftpConnector, out: Path, moved: str, sentence: str
) -> None:
    """A stat after the read that differs from the one before it in one field only
    is still evidence. A file rewritten in place with bytes of the same length
    moves only the handle's mtime. A path that became a link of the same size and
    mtime moves only its type. Missed, either would be stored under a revision that
    the next listing reports unchanged."""
    fake = _Fake(data=b"same")
    if moved == "fstat-mtime":
        fake.fstats = [_regular(4), _regular(4, mtime=MTIME + 1)]
    elif moved == "fstat-type":
        fake.fstats = [_regular(4), _LINK_LIKE_THE_FILE]
    else:
        fake.lstats = [_regular(4), _LINK_LIKE_THE_FILE]
    hostile.add_file("/files/rewritten.pdf", fake)
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/rewritten.pdf"]

    with pytest.raises(ChangedSinceListing) as raised:
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert raised.value.message == sentence


@pytest.mark.security
@pytest.mark.parametrize("parent", ["refused", "failed", "too-long", "stalled"])
async def test_a_folder_listing_that_cannot_answer_is_no_evidence_that_a_file_is_gone(
    hostile: _Hostile,
    connector: SftpConnector,
    out: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent: str,
) -> None:
    """A file the server answers "no such file" for is gone only when a listing of
    its folder answers, and no longer names it. A folder the server refuses to
    list, fails to list, stops answering, or that holds more than `MAX_NAMES`
    names cannot answer. The file is out of the folder here too, so only that rule
    keeps it from being taken for a deletion and skipped on every run while the
    sync reports success. It is the server's failure, a refusal included: the
    folder was listed a moment ago. The server's own text stays out of it."""
    path = "/files/lost.pdf"
    for number in range(10):
        hostile.add_entry("/files", b"filler-%02d.tmp" % number, _regular(1))
    hostile.add_file(path, _Fake(data=b"lost", lstats=[asyncssh.SFTPNoSuchFile("No such file")]))
    listed = (await _listed_hostile(connector, hostile, "/files"))[path]
    hostile.unlist(path)
    folder = hostile.add_folder("/files")
    if parent == "refused":
        folder.refusal = asyncssh.SFTPPermissionDenied("SERVER-TEXT: permission denied")
    elif parent == "failed":
        folder.refusal = asyncssh.SFTPFailure("SERVER-TEXT: failure")
    elif parent == "too-long":
        monkeypatch.setattr(sftp_module, "MAX_NAMES", 5)
    else:
        monkeypatch.setattr(sftp_module, "REQUEST_SECONDS", 1.0)
        folder.stall = True

    with anyio.fail_after(30), pytest.raises(ExternalServiceError) as raised:
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert "SERVER-TEXT" not in raised.value.message
    assert "SERVER-TEXT" not in str(raised.value.details)


_REFUSED_READS: dict[str, tuple[type[asyncssh.SFTPError], type[Exception], str]] = {
    "permission-denied": (
        asyncssh.SFTPPermissionDenied,
        BadRequestError,
        f"The account {USER} may not read locked.pdf.",
    ),
    "failure": (
        asyncssh.SFTPFailure,
        ExternalServiceError,
        "The server refused to read locked.pdf.",
    ),
}


@pytest.mark.security
@pytest.mark.parametrize("answer", list(_REFUSED_READS))
@pytest.mark.parametrize("step", _FILE_REQUESTS)
async def test_a_read_the_server_refuses_fails_with_a_sentence_of_ours(
    hostile: _Hostile, connector: SftpConnector, out: Path, step: str, answer: str
) -> None:
    """A server refuses a read in its own words: for a folder of mode 000, a link
    out of the root on SFTPGo, a link loop, a disk that fails under a READ.
    Wherever the refusal comes, before the open, on the open handle, on the READ
    or on the path after the read, the sync says it in a sentence of ours:
    "permission denied" is the account's, anything else the server's. The
    server's text stays out, no byte is read after a refusal that comes first, and
    an open handle is closed."""
    path = "/files/locked.pdf"
    error_type, refusal, sentence = _REFUSED_READS[answer]
    hostile.add_file(path, _failing_at(step, error_type("SERVER-TEXT: no"), b"locked"))
    listed = (await _listed_hostile(connector, hostile, "/files"))[path]

    with pytest.raises(refusal) as raised:
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert isinstance(raised.value, BadRequestError | ExternalServiceError)
    assert raised.value.message == sentence
    assert "SERVER-TEXT" not in str(raised.value.details)
    assert hostile.record.seen("close", path) == _opened(step)
    if step in _STEPS[:3]:
        assert hostile.record.seen("read", path) == 0


@pytest.mark.security
async def test_names_with_a_slash_nul_dot_dot_or_an_absolute_path_are_left_out_and_noted(
    hostile: _Hostile, connector: SftpConnector
) -> None:
    """A server names the entries of a folder, and asyncssh checks nothing it sends.
    A name with a slash makes an address outside the folder that listed it, and a
    NUL ends a name early. The name is checked before the type, so a folder with
    such a name is never walked: `/etc` or `../up` would take the listing out of
    the root, where the server resolves them. The listing leaves every such entry
    out and says how many in which folder, without the names themselves. It stays
    complete: no document can exist at an address no listing could name."""
    for name in (b"a/b.pdf", b"../escape.pdf", b"/etc/passwd", b"/abs.pdf", b"nul\x00.pdf", b""):
        hostile.add_entry("/names", name, _regular(2))
    for name in (b"/etc", b"../up", b"a/../../up", b"d\x00ir", b""):
        hostile.add_entry("/names", name, _typed(stat.S_IFDIR))
    hostile.add_entry("/names", b"ok.pdf", _regular(2))
    hostile.add_file("/etc/passwd.pdf", _Fake(data=b"root"))
    hostile.add_file("/up/outside.pdf", _Fake(data=b"outside the root"))

    listing = await connector.list_files(hostile.config("/names"), hostile.credential)

    assert [file.id for file in listing.files] == ["/names/ok.pdf"]
    assert listing.problems == [
        "The server listed 11 names in /names that are not file names, so they were left out."
    ]
    assert listing.complete is True
    assert hostile.record.calls == [("scandir", b"/names")]


async def test_a_name_that_is_not_utf8_is_left_out_and_the_rest_are_listed(
    hostile: _Hostile, connector: SftpConnector
) -> None:
    """An address is text, and a name of Latin-1 bytes is not. Decoded for the whole
    folder at once, one such name would end the listing of every other file. Each
    name is decoded alone, so the rest are listed and the count is noted. A folder
    with such a name is left out the same way, and never opened: whatever it holds
    could have no address."""
    for name in (b"r\xe9sum\xe9.pdf", b"\xff\xfe.pdf", "résumé.pdf".encode(), b"plain.pdf"):
        hostile.add_entry("/latin1", name, _regular(4))
    hostile.add_entry("/latin1", b"d\xe9p\xf4t", _typed(stat.S_IFDIR))
    hostile.folders[b"/latin1/d\xe9p\xf4t"] = _Folder(entries=[(b"inside.pdf", _regular(6))])

    listing = await connector.list_files(hostile.config("/latin1"), hostile.credential)

    assert sorted(file.id for file in listing.files) == ["/latin1/plain.pdf", "/latin1/résumé.pdf"]
    assert listing.problems == ["3 names in /latin1 are not UTF-8, so those files were left out."]
    assert listing.complete is True
    assert hostile.record.calls == [("scandir", b"/latin1")]


@pytest.mark.security
async def test_links_to_a_file_a_folder_out_of_the_root_and_a_loop_are_never_listed_or_opened(
    server: _Served, connector: SftpConnector
) -> None:
    """READDIR reports a link as a link on every server tested, and a link points
    wherever the server's account can read: out of the root, at a folder, at
    itself. The listing skips every link, whatever it points at, so no loop is
    walked and no file outside the root is read."""
    tree = server.needs_tree()

    listing = await connector.list_files(server.config(tree, extensions=[".txt"]), server.login)

    assert sorted(file.id for file in listing.files) == sorted(
        f"{tree}/{name}" for name in TREE_FILES
    )
    if server.record is not None:
        assert server.record.touched(b"link-") == []


async def test_an_entry_of_unknown_type_makes_the_listing_incomplete(
    hostile: _Hostile, connector: SftpConnector
) -> None:
    """A v3 server that sends no permissions sends no type, and asyncssh reads the
    type as unknown. Left out of a listing that called itself complete, those
    files would have their documents removed. A FIFO, a socket or a device has a
    type: it is skipped, not counted, and never opened. Each folder's count is a
    sentence of its own that names that folder, so the user looks in the right one."""
    hostile.add_entry("/unknown", b"ok.pdf", _regular(2))
    hostile.add_entry("/unknown", b"mystery-1.pdf", _untyped(2))
    hostile.add_entry("/unknown", b"mystery-2.pdf", _untyped(2))
    hostile.add_entry("/unknown", b"pipe.pdf", _typed(stat.S_IFIFO))
    hostile.add_entry("/unknown", b"socket.pdf", _typed(stat.S_IFSOCK))
    hostile.add_entry("/unknown", b"device.pdf", _typed(stat.S_IFCHR))
    for number in range(3):
        hostile.add_entry("/unknown/sub", b"mystery-%d.pdf" % number, _untyped(2))

    listing = await connector.list_files(hostile.config("/unknown"), hostile.credential)

    assert [file.id for file in listing.files] == ["/unknown/ok.pdf"]
    assert listing.problems == [
        "The server did not say what 2 entries in /unknown are, so they were left out.",
        "The server did not say what 3 entries in /unknown/sub are, so they were left out.",
    ]
    assert listing.complete is False
    assert [call for call in hostile.record.calls if call[0] != "scandir"] == []


@pytest.mark.security
async def test_a_folder_without_read_permission_is_a_problem_and_the_listing_is_incomplete(
    server: _Served, connector: SftpConnector
) -> None:
    """A folder the account may not read is listed in its parent and refused when
    it is opened. What it holds is unknown, so the listing reports it and is not
    complete: the documents that came from it stay."""
    tree = server.needs_tree()

    listing = await connector.list_files(server.config(tree, extensions=[".txt"]), server.login)

    assert listing.problems == [f"The folder {tree}/unreadable could not be read."]
    assert listing.complete is False
    assert f"{tree}/dir/sub/deep.txt" in {file.id for file in listing.files}


@pytest.mark.parametrize("root", ["missing", "a-file", "unreadable"])
async def test_a_missing_or_unreadable_root_is_refused(
    server: _Served, connector: SftpConnector, root: str
) -> None:
    """A root that cannot be listed is not an empty folder. A listing of nothing
    that called itself complete would remove every document the source brought
    in, so the sync stops with a sentence that says why."""
    if root == "unreadable":
        path = f"{server.needs_tree()}/unreadable"
        expected = f"The account {server.user} may not read the folder {path} on {HOST}."
    else:
        path = server.path("no-such-folder" if root == "missing" else "report.pdf")
        if root == "a-file":
            await server.writer.write(path, b"a file, not a folder")
        expected = f"There is no folder {path} on {HOST}, or it is not a folder."
    # SFTPGo answers a READDIR of a file with "failure", not "no such file", and
    # any other error at the root fails the listing as the server's fault.
    refusals: tuple[type[Exception], ...] = (
        (BadRequestError, ExternalServiceError)
        if server.kind == "real" and root == "a-file"
        else (BadRequestError,)
    )

    with pytest.raises(refusals) as raised:
        await connector.list_files(server.config(path), server.login)

    if isinstance(raised.value, BadRequestError):
        assert raised.value.message == expected


async def test_a_source_without_subfolders_never_opens_one(
    disk: _Served, connector: SftpConnector
) -> None:
    """With `include_subfolders` off the root folder is read alone: a folder in it
    is not opened, and is no reason to call the listing incomplete."""
    record = disk.record
    assert record is not None
    await disk.writer.write(disk.path("top.pdf"), b"top")
    await disk.writer.write(disk.path("sub/inner.pdf"), b"inner")

    listing = await connector.list_files(disk.config(include_subfolders=False), disk.login)

    assert [file.id for file in listing.files] == [disk.path("top.pdf")]
    assert listing.complete is True
    assert listing.problems == []
    assert record.seen("scandir", disk.path("sub")) == 0


@pytest.mark.security
async def test_a_file_replaced_by_a_link_out_of_the_root_is_never_read_through_it(
    disk: _Served, connector: SftpConnector, out: Path, tmp_path: Path
) -> None:
    """A listed file that is a link out of the root by the time it is fetched.
    SFTP v3 has no open that refuses a link, so the stat before the open is an
    LSTAT, which sees the link, and the fetch fails before anything is opened. A
    STAT would follow the link, and a file outside the source's folder would be
    read into the collection. In-process only: ProFTPD's LSTAT follows links."""
    record = disk.record
    assert record is not None
    assert isinstance(disk.writer, _LocalWriter)
    path = disk.path("report.pdf")
    await disk.writer.write(path, b"the report")
    listed = (await _listed(connector, disk))[path]
    disk.writer.swap_for_link(path, tmp_path / "outside" / "secret.txt")

    with pytest.raises(ExternalServiceError) as raised:
        await connector.fetch_file(listed, out, config=disk.config(), credential=disk.login)

    assert (
        raised.value.message
        == "The server listed report.pdf as a file, and now says it is not one."
    )
    assert record.seen("open", path) == 0
    assert record.seen("stat", path) == 0


async def test_a_root_of_slash_lists_each_file_under_one_slash(
    hostile: _Hostile, connector: SftpConnector
) -> None:
    """A chrooted account, or an SFTPGo one, keeps its files at `/`, so `/` is a
    natural root. A file in it is `/name`, never `//name`: an address spelled two
    ways would ingest every document again once the spelling was corrected."""
    hostile.add_file("/report.pdf", _Fake(data=b"report"))
    hostile.add_file("/sub/notes.txt", _Fake(data=b"notes"))

    listing = await connector.list_files(hostile.config("/"), hostile.credential)

    assert sorted(file.id for file in listing.files) == ["/report.pdf", "/sub/notes.txt"]
    assert sorted(file.source_path for file in listing.files) == [
        f"sftp://{USER}@{HOST}:{hostile.port}/report.pdf",
        f"sftp://{USER}@{HOST}:{hostile.port}/sub/notes.txt",
    ]


@pytest.mark.security
@pytest.mark.parametrize("where", ["root", "subfolder"])
async def test_a_folder_the_server_fails_to_list_fails_the_root_and_is_a_problem_below_it(
    hostile: _Hostile, connector: SftpConnector, where: str
) -> None:
    """A failure that is not "no such folder" or "permission denied" is the
    server's. At the root the listing fails whole, so nothing is removed. Below
    it the folder is a problem and the listing is incomplete. The server's own
    text is in neither."""
    hostile.add_file("/broken/ok.pdf", _Fake(data=b"ok"))
    hostile.add_file("/broken/sub/inner.pdf", _Fake(data=b"inner"))
    failing = "/broken" if where == "root" else "/broken/sub"
    hostile.add_folder(failing).refusal = asyncssh.SFTPFailure("SERVER-TEXT: failure")

    if where == "root":
        with pytest.raises(ExternalServiceError) as raised:
            await connector.list_files(hostile.config("/broken"), hostile.credential)
        assert "SERVER-TEXT" not in raised.value.message
    else:
        listing = await connector.list_files(hostile.config("/broken"), hostile.credential)
        assert [file.id for file in listing.files] == ["/broken/ok.pdf"]
        assert listing.problems == ["The folder /broken/sub could not be read."]
        assert listing.complete is False


@pytest.mark.parametrize("stalled", ["root", "subfolder"])
async def test_a_stalled_listing_aborts_and_the_next_listing_reconnects(
    hostile: _Hostile, connector: SftpConnector, monkeypatch: pytest.MonkeyPatch, stalled: str
) -> None:
    """A server that stops answering a READDIR. asyncssh's folder iterator, when it
    is cancelled, sends a CLOSE and waits for the answer, and a server busy with
    the stalled request never gives it. The connection is aborted before that
    wait, so the listing fails in time, and fails whole, at the root or below it:
    nothing is removed. The next listing opens a new connection."""
    monkeypatch.setattr(sftp_module, "REQUEST_SECONDS", 1.0)
    hostile.add_file("/listing/ok.pdf", _Fake(data=b"ok"))
    hostile.add_folder("/listing" if stalled == "root" else "/listing/stuck").stall = True
    hostile.add_file("/fine/ok.pdf", _Fake(data=b"fine"))

    with anyio.fail_after(30):
        with pytest.raises(ExternalServiceError) as raised:
            await connector.list_files(hostile.config("/listing"), hostile.credential)
        listing = await connector.list_files(hostile.config("/fine"), hostile.credential)

    assert raised.value.message.startswith(f"{HOST} did not answer within ")
    assert [file.id for file in listing.files] == ["/fine/ok.pdf"]
    assert len(hostile.record.connections) == 2


@pytest.mark.parametrize("where", ["root", "subfolder"])
async def test_a_connection_lost_during_a_listing_fails_it_whole_and_the_next_listing_reconnects(
    hostile: _Hostile, connector: SftpConnector, where: str
) -> None:
    """A connection that breaks in the middle of a listing cuts it short wherever
    it broke. That is not an incomplete listing with a problem: the listing fails
    whole, so nothing is removed, and the next listing opens a new connection."""
    hostile.add_file("/listing/ok.pdf", _Fake(data=b"ok"))
    hostile.add_file("/listing/sub/inner.pdf", _Fake(data=b"inner"))
    hostile.add_folder("/listing" if where == "root" else "/listing/sub").drop = True
    hostile.add_file("/fine/ok.pdf", _Fake(data=b"fine"))

    with anyio.fail_after(30):
        with pytest.raises(ExternalServiceError) as raised:
            await connector.list_files(hostile.config("/listing"), hostile.credential)
        listing = await connector.list_files(hostile.config("/fine"), hostile.credential)

    assert raised.value.message == f"The connection to {HOST} was lost."
    assert [file.id for file in listing.files] == ["/fine/ok.pdf"]
    assert len(hostile.record.connections) == 2


async def test_an_sftp_session_that_never_starts_fails_in_time(
    hostile: _Hostile, connector: SftpConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SFTP session starts with a version request, and asyncssh waits for its
    answer without a deadline, as for any request. A server that lets the account
    in and never answers it fails the sync within the request deadline, and the
    connection is aborted rather than left waiting. It is a failed connect, and
    kept: a later call raises it again without a new login, so the server costs
    the sync one deadline, not one for each file."""
    monkeypatch.setattr(sftp_module, "REQUEST_SECONDS", 1.0)
    hostile.add_file("/files/ok.pdf", _Fake(data=b"ok"))
    hostile.init_stall = True

    with anyio.fail_after(30):
        with pytest.raises(ExternalServiceError) as raised:
            await connector.list_files(hostile.config("/files"), hostile.credential)
        with pytest.raises(ExternalServiceError) as again:
            await connector.list_files(hostile.config("/files"), hostile.credential)

    assert raised.value.message.startswith(f"{HOST} did not answer within ")
    assert again.value.message == raised.value.message
    assert len(hostile.record.connections) == 1
    assert hostile.record.sessions == 0
    with anyio.fail_after(5):
        await hostile.record.closed.wait()


@pytest.mark.security
@pytest.mark.parametrize("version", [2, 7])
async def test_a_server_that_answers_an_unsupported_sftp_version_is_refused(
    hostile: _Hostile, connector: SftpConnector, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """asyncssh takes a version answer from 3 to 6, and refuses any other with an
    error in its own words. The sync says it in a sentence of ours, aborts the
    half-open connection, and keeps the refusal, so it does not log in again for
    each file."""
    _answer_sftp_version(monkeypatch, version)
    hostile.add_file("/files/ok.pdf", _Fake(data=b"ok"))

    with anyio.fail_after(30):
        with pytest.raises(ExternalServiceError) as raised:
            await connector.list_files(hostile.config("/files"), hostile.credential)
        with pytest.raises(ExternalServiceError) as again:
            await connector.list_files(hostile.config("/files"), hostile.credential)

    assert raised.value.message == (
        f"{HOST} answered the start of the SFTP session in a way this worker cannot use."
    )
    assert again.value.message == raised.value.message
    assert len(hostile.record.connections) == 1
    with anyio.fail_after(5):
        await hostile.record.closed.wait()


@pytest.mark.parametrize("stopped_by", ["a-bound", "the-name-found"])
async def test_a_folder_close_that_is_never_answered_aborts_the_connection(
    hostile: _Hostile,
    connector: SftpConnector,
    out: Path,
    monkeypatch: pytest.MonkeyPatch,
    stopped_by: str,
) -> None:
    """A folder read only in part is closed before its end: when a bound stops the
    walk, and when the search for a file's name finds it. A CLOSE the server never
    answers holds every later request behind it, so it has the deadline of any
    request. The connection is aborted in time, and the next file reads on a new
    one."""
    monkeypatch.setattr(sftp_module, "REQUEST_SECONDS", 1.0)
    hostile.add_file("/fine/next.pdf", _Fake(data=b"the next file"))
    following = (await _listed_hostile(connector, hostile, "/fine"))["/fine/next.pdf"]
    if stopped_by == "a-bound":
        monkeypatch.setattr(sftp_module, "MAX_NAMES", 300)
        hostile.add_endless("/endless")
        hostile.add_folder("/endless").close_stall = True
        stopped = connector.list_files(hostile.config("/endless"), hostile.credential)
    else:
        path = "/files/sub/ghost.pdf"
        hostile.add_file(
            path, _Fake(data=b"ghost", lstats=[asyncssh.SFTPNoSuchFile("No such file")])
        )
        for number in range(5):
            hostile.add_entry("/files/sub", b"after-%d.pdf" % number, _regular(1))
        listed = (await _listed_hostile(connector, hostile, "/files"))[path]
        hostile.add_folder("/files/sub").close_stall = True
        stopped = connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    with anyio.fail_after(30):
        # The listing may end with what it read, or fail; either way, in time.
        with contextlib.suppress(ExternalServiceError):
            await stopped
        fetched = await connector.fetch_file(
            following, out, config=hostile.config("/fine"), credential=hostile.credential
        )

    assert fetched.path.read_bytes() == b"the next file"
    assert len(hostile.record.connections) == 2


_BARE: dict[str, SFTPAttrs] = {
    "mtime": SFTPAttrs(size=4, permissions=stat.S_IFREG | 0o644),
    "size": SFTPAttrs(permissions=stat.S_IFREG | 0o644, atime=MTIME - DAY, mtime=MTIME),
    "both": SFTPAttrs(permissions=stat.S_IFREG | 0o644),
}


@pytest.mark.parametrize("missing", list(_BARE))
async def test_a_file_without_a_size_or_an_mtime_lists_and_reads_without_a_revision(
    hostile: _Hostile, connector: SftpConnector, out: Path, missing: str
) -> None:
    """A server need not send a size or an mtime. Without either there is no
    revision: the file lists with none, so it is always downloaded, and the
    download reports none, so nothing is stored that a later listing could match.
    Without a size from either stat, the bytes received are not checked against
    one."""
    attrs = _BARE[missing]
    hostile.add_file(
        "/files/bare.pdf", _Fake(data=b"bare", lstats=[attrs], fstats=[attrs]), listed=attrs
    )
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/bare.pdf"]

    fetched = await connector.fetch_file(
        listed, out, config=hostile.config("/files"), credential=hostile.credential
    )

    assert listed.revision is None
    assert listed.size == (None if missing in ("size", "both") else 4)
    assert listed.modified_at == (
        None if missing in ("mtime", "both") else datetime.fromtimestamp(MTIME, UTC)
    )
    assert fetched.path.read_bytes() == b"bare"
    assert fetched.revision is None


def _answer_sftp_version(monkeypatch: pytest.MonkeyPatch, version: int) -> None:
    """Make asyncssh's server answer a v3 client with `version`, as a hostile one may.

    The server agrees on the smaller of the two versions with the builtin `min`.
    A `min` in asyncssh's own module answers that one call, `min(3, 3)`, with
    `version`, and every other call as the builtin does.
    """

    def agreed(*args: int, **kwargs: object) -> int:
        if args == (3, 3) and not kwargs:
            return version
        return builtins.min(*args, **kwargs)

    monkeypatch.setattr(asyncssh.sftp, "min", agreed, raising=False)


@pytest.mark.parametrize("mtime", [2**40, 2**64 - 1], ids=["past-the-year-9999", "the-largest"])
async def test_an_mtime_no_date_can_hold_lists_without_a_date_and_keeps_its_revision(
    hostile: _Hostile, connector: SftpConnector, monkeypatch: pytest.MonkeyPatch, mtime: int
) -> None:
    """asyncssh asks for SFTP v3 and takes any version from 3 to 6 that the server
    answers. From v4 an mtime is 64 bits wide, and the largest are no date a
    `datetime` can hold. Such a file still lists, with no `modified_at` and with
    the revision of its whole mtime, so a change is still seen, and one entry does
    not end the listing."""
    _answer_sftp_version(monkeypatch, 6)
    far = _regular(4, mtime=mtime, atime=MTIME)
    hostile.add_file("/files/far.pdf", _Fake(data=b"far!", lstats=[far], fstats=[far]), listed=far)
    hostile.add_file("/files/near.pdf", _Fake(data=b"near"))

    listing = await connector.list_files(hostile.config("/files"), hostile.credential)

    by_id = {file.id: file for file in listing.files}
    assert sorted(by_id) == ["/files/far.pdf", "/files/near.pdf"]
    assert by_id["/files/far.pdf"].modified_at is None
    assert by_id["/files/far.pdf"].revision == f"4:{mtime}"
    assert by_id["/files/near.pdf"].modified_at == datetime.fromtimestamp(MTIME, UTC)
    assert listing.complete is True


@pytest.mark.security
async def test_a_file_over_the_cap_is_listed_without_a_revision_and_refused_before_a_byte_is_read(
    hostile: _Hostile, connector: SftpConnector, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file over `MAX_UPLOAD_SIZE_MB` is refused at ingest anyway. With no
    revision it reaches `_fetch` on every run, which refuses it from its stats
    alone, so the failure stays visible. With a revision, `new_only` would skip it
    as unchanged and the sync would report success for a file it never read."""
    monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE_MB", 1)
    big = _regular(3 * MIB)
    hostile.add_file("/files/big.pdf", _Fake(lstats=[big], fstats=[big]), listed=big)
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/big.pdf"]

    with pytest.raises(BadRequestError) as raised:
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert listed.revision is None
    assert raised.value.message == "big.pdf is 3 MB, and a synced file may be at most 1 MB."
    assert raised.value.details == {"size_bytes": 3 * MIB, "limit_bytes": MIB}
    assert hostile.record.seen("open", "/files/big.pdf") == 0
    assert hostile.record.seen("read", "/files/big.pdf") == 0


async def test_a_file_exactly_at_the_cap_lists_with_a_revision_and_reads_whole(
    hostile: _Hostile, connector: SftpConnector, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap is the largest file a sync takes, as it is the largest upload. A file
    of exactly `MAX_UPLOAD_SIZE_MB` lists with its revision and reads whole; a
    byte more is what the cap refuses."""
    monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE_MB", 1)
    body = os.urandom(MIB)
    hostile.add_file("/files/full.pdf", _Fake(data=body))
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/full.pdf"]

    fetched = await connector.fetch_file(
        listed, out, config=hostile.config("/files"), credential=hostile.credential
    )

    assert listed.revision == f"{MIB}:{MTIME}"
    assert fetched.path.read_bytes() == body
    assert fetched.revision == listed.revision


@pytest.mark.security
async def test_a_file_that_grows_past_the_cap_during_the_read_is_refused(
    hostile: _Hostile, connector: SftpConnector, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A size from a stat is the server's word. A file whose stats say 10 bytes and
    that then sends without end is cut off once the bytes pass the cap, so a
    server cannot fill the worker's disk with one file."""
    monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE_MB", 1)
    # Without the cap the read would end only at this deadline, as another error.
    monkeypatch.setattr(sftp_module, "FILE_SECONDS", 5.0)
    hostile.add_file(
        "/files/swelling.pdf",
        _Fake(data=b"ten bytes.", reads=_sending(lambda _offset, size: b"x" * size, delay=0.02)),
    )
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/swelling.pdf"]

    with anyio.fail_after(30), pytest.raises(BadRequestError):
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert hostile.record.seen("read", "/files/swelling.pdf") <= MIB // sftp_module.CHUNK_BYTES + 2
    assert hostile.record.seen("close", "/files/swelling.pdf") == 1


@pytest.mark.security
async def test_a_handle_over_the_cap_is_refused_before_a_byte_is_read(
    hostile: _Hostile, connector: SftpConnector, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path's LSTAT says 10 bytes, and the open handle's FSTAT says 3 MB: a file
    replaced between the two, or a server whose answers disagree. The handle's size
    is checked as the path's is, so no byte of it is read, and the handle is
    closed."""
    monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE_MB", 1)
    hostile.add_file(
        "/files/big.pdf",
        _Fake(data=b"ten bytes.", lstats=[_regular(10)], fstats=[_regular(3 * MIB)]),
    )
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/big.pdf"]

    with pytest.raises(BadRequestError) as raised:
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert raised.value.message == "big.pdf is 3 MB, and a synced file may be at most 1 MB."
    assert raised.value.details == {"size_bytes": 3 * MIB, "limit_bytes": MIB}
    assert hostile.record.seen("read", "/files/big.pdf") == 0
    assert hostile.record.seen("close", "/files/big.pdf") == 1


@pytest.mark.security
async def test_a_server_that_sends_more_than_it_was_asked_for_fails_the_file(
    hostile: _Hostile, connector: SftpConnector, out: Path
) -> None:
    """asyncssh hands back a reply as the server sent it, longer than the request
    or not. Here the extra bytes make the file exactly the size its stats claim,
    so only the check of the reply against the request can refuse it."""
    size = sftp_module.CHUNK_BYTES + 1000
    stats = _regular(size)
    hostile.add_file(
        "/files/oversize.pdf",
        _Fake(
            lstats=[stats],
            fstats=[stats],
            reads=_sending(lambda offset, asked: b"x" * (asked + 1000) if offset == 0 else b""),
        ),
        listed=stats,
    )
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/oversize.pdf"]

    with pytest.raises(ExternalServiceError):
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert hostile.record.seen("read", "/files/oversize.pdf") == 1
    assert hostile.record.seen("close", "/files/oversize.pdf") == 1


@pytest.mark.parametrize("sized_by", ["fstat", "lstat"])
async def test_a_server_that_sends_fewer_bytes_than_it_says_fails_the_file(
    hostile: _Hostile, connector: SftpConnector, out: Path, sized_by: str
) -> None:
    """A file that ends early is not one the sync may store: its stats say 100
    bytes, and it sent 10. asyncssh's own `get()` accepts that without a word. A
    handle whose FSTAT gives no size is checked against the path's LSTAT."""
    hundred = _regular(100)
    fstat = hundred if sized_by == "fstat" else _regular(None)
    hostile.add_file(
        "/files/short.pdf",
        _Fake(data=b"0123456789", lstats=[hundred], fstats=[fstat]),
        listed=hundred,
    )
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/short.pdf"]

    with pytest.raises(ExternalServiceError) as raised:
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert raised.value.message == "The server sent 10 of the 100 bytes short.pdf holds."
    assert hostile.record.seen("close", "/files/short.pdf") == 1


_BoundCase = tuple[str, Callable[[RemoteListing], None]]


def _depth(hostile: _Hostile, monkeypatch: pytest.MonkeyPatch) -> _BoundCase:
    """`MAX_DEPTH` levels below the root are read, and not one more."""
    monkeypatch.setattr(sftp_module, "MAX_DEPTH", 2)
    levels = [
        "/deep/level-0.pdf",
        "/deep/a/level-1.pdf",
        "/deep/a/b/level-2.pdf",
        "/deep/a/b/c/level-3.pdf",
        "/deep/a/b/c/d/level-4.pdf",
    ]
    for path in levels:
        hostile.add_file(path, _Fake(data=b"level"))

    def check(listing: RemoteListing) -> None:
        assert sorted(file.id for file in listing.files) == sorted(levels[:3])
        assert hostile.record.seen("scandir", "/deep/a/b/c") == 0

    return "/deep", check


def _folders(hostile: _Hostile, monkeypatch: pytest.MonkeyPatch) -> _BoundCase:
    """At most `MAX_FOLDERS` folders are opened, the root among them."""
    monkeypatch.setattr(sftp_module, "MAX_FOLDERS", 3)
    for number in range(5):
        hostile.add_file(f"/wide/sub-{number}/file-{number}.pdf", _Fake(data=b"wide"))

    def check(listing: RemoteListing) -> None:
        opened = [call for call in hostile.record.calls if call[0] == "scandir"]
        assert len(opened) == 3

    return "/wide", check


def _names(hostile: _Hostile, monkeypatch: pytest.MonkeyPatch) -> _BoundCase:
    """A folder that never ends is read up to `MAX_NAMES` entries, of every type.
    Its names are links and special files, never listed, so no other bound can
    stop it, and a count of listed files alone would never reach the bound."""
    monkeypatch.setattr(sftp_module, "MAX_NAMES", 300)
    hostile.add_endless("/endless")

    def check(listing: RemoteListing) -> None:
        assert listing.files == []
        # The server sends 128 names a reply, so a stop at 300 has asked for 384 at most.
        assert hostile.names_sent <= 300 + 128

    return "/endless", check


def _names_of_dots(hostile: _Hostile, monkeypatch: pytest.MonkeyPatch) -> _BoundCase:
    """`.` and `..` are names too: a folder that sends nothing else, without end,
    is stopped at `MAX_NAMES` like any other."""
    monkeypatch.setattr(sftp_module, "MAX_NAMES", 300)
    hostile.add_endless("/dotted", "dots")

    def check(listing: RemoteListing) -> None:
        assert listing.files == []
        assert hostile.names_sent <= 300 + 128

    return "/dotted", check


def _names_across_folders(hostile: _Hostile, monkeypatch: pytest.MonkeyPatch) -> _BoundCase:
    """`MAX_NAMES` is one count for the whole listing: five folders of 100 links stay
    under it one by one, and pass it together, so the last ones are never opened."""
    monkeypatch.setattr(sftp_module, "MAX_NAMES", 300)
    for folder in range(5):
        for number in range(100):
            hostile.add_entry(f"/several/sub-{folder}", b"link-%03d" % number, _typed(stat.S_IFLNK))

    def check(listing: RemoteListing) -> None:
        assert hostile.record.seen("scandir", "/several/sub-4") == 0

    return "/several", check


def _files(hostile: _Hostile, monkeypatch: pytest.MonkeyPatch) -> _BoundCase:
    """At most `MAX_FILES` files are listed, and the problem says how to go on."""
    monkeypatch.setattr(sftp_module, "MAX_FILES", 3)
    for number in range(5):
        hostile.add_file(f"/many/file-{number}.pdf", _Fake(data=b"many"))

    def check(listing: RemoteListing) -> None:
        assert len(listing.files) == 3
        assert listing.problems == [
            f"The listing of {HOST} stopped at 3 files, so the files after them were not read. "
            "Choose a narrower folder."
        ]

    return "/many", check


def _time_between_empty_folders(hostile: _Hostile, monkeypatch: pytest.MonkeyPatch) -> _BoundCase:
    """`LISTING_SECONDS` passes in a walk of empty folders that send not even `.`
    and `..`, each READDIR slow. All of them are queued before the deadline, and no
    entry ever arrives to be checked, so the check before each folder is what
    stops the walk short of the last folder."""
    monkeypatch.setattr(sftp_module, "LISTING_SECONDS", 1.0)
    for number in range(10):
        empty = hostile.add_folder(f"/hollow/empty-{number}")
        empty.dots, empty.delay = False, 0.3
    hostile.add_folder("/hollow").dots = False

    def check(listing: RemoteListing) -> None:
        opened = [call for call in hostile.record.calls if call[1].startswith(b"/hollow/")]
        assert len(opened) < 10

    return "/hollow", check


def _time_inside_a_folder(hostile: _Hostile, monkeypatch: pytest.MonkeyPatch) -> _BoundCase:
    """`LISTING_SECONDS` passes while a folder answers its first READDIR: the
    entries that then arrive are not read. No folder comes after it, so only the
    check of its entries can stop the walk."""
    monkeypatch.setattr(sftp_module, "LISTING_SECONDS", 0.5)
    for number in range(5):
        hostile.add_file(f"/paced/late-{number}.pdf", _Fake(data=b"late"))
    hostile.add_folder("/paced").delay = 1.0

    def check(listing: RemoteListing) -> None:
        assert len(listing.files) < 5

    return "/paced", check


_BOUNDS: dict[str, Callable[[_Hostile, pytest.MonkeyPatch], _BoundCase]] = {
    "depth": _depth,
    "folders": _folders,
    "names": _names,
    "names-of-dots": _names_of_dots,
    "names-across-folders": _names_across_folders,
    "files": _files,
    "time-between-empty-folders": _time_between_empty_folders,
    "time-inside-a-folder": _time_inside_a_folder,
}


@pytest.mark.security
@pytest.mark.parametrize("bound", list(_BOUNDS))
async def test_a_listing_stops_at_each_bound_and_says_so(
    hostile: _Hostile, connector: SftpConnector, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    """The server decides how deep a tree goes, how many folders and names it holds,
    and how long each answer takes. Each bound stops the walk where it is reached,
    with one problem that tells the user to choose a narrower folder, and the
    listing is incomplete, so nothing the walk did not reach is removed."""
    root, check = _BOUNDS[bound](hostile, monkeypatch)

    with anyio.fail_after(60):
        listing = await connector.list_files(hostile.config(root), hostile.credential)

    assert listing.complete is False
    assert len(listing.problems) == 1
    assert listing.problems[0].endswith("Choose a narrower folder.")
    check(listing)


async def test_a_stalled_read_aborts_and_the_next_file_reconnects(
    hostile: _Hostile, connector: SftpConnector, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that stops answering in the middle of a file. asyncssh has no
    deadline of its own, and the server handles one request at a time, so the
    stalled read would stall the close and every request after it too. The
    connection is aborted, the file fails, and the next file is read on a new
    connection."""
    monkeypatch.setattr(sftp_module, "REQUEST_SECONDS", 1.0)
    hostile.add_file("/files/stalled.pdf", _Fake(data=b"never sent", reads=hostile.stall))
    hostile.add_file("/files/next.pdf", _Fake(data=b"the next file"))
    listed = await _listed_hostile(connector, hostile, "/files")
    config = hostile.config("/files")

    with anyio.fail_after(30):
        with pytest.raises(ExternalServiceError) as raised:
            await connector.fetch_file(
                listed["/files/stalled.pdf"], out, config=config, credential=hostile.credential
            )
        fetched = await connector.fetch_file(
            listed["/files/next.pdf"], out, config=config, credential=hostile.credential
        )

    assert raised.value.message.startswith(f"{HOST} did not answer within ")
    assert fetched.path.read_bytes() == b"the next file"
    assert len(hostile.record.connections) == 2
    with anyio.fail_after(5):
        await hostile.record.closed.wait()


@pytest.mark.parametrize("step", _STEPS)
async def test_a_fetch_request_that_is_never_answered_aborts_and_the_next_file_reconnects(
    hostile: _Hostile,
    connector: SftpConnector,
    out: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
) -> None:
    """asyncssh puts no deadline on any request, and a server can stop answering at
    any of them: the LSTAT before the open, the open, either FSTAT, the LSTAT after
    the read. Each has the request deadline, so the file fails in time with our
    sentence, the connection is aborted, and the next file reads on a new one."""
    monkeypatch.setattr(sftp_module, "REQUEST_SECONDS", 1.0)
    hostile.add_file("/files/stalled.pdf", _Fake(data=b"never all sent", stall_at=step))
    hostile.add_file("/files/next.pdf", _Fake(data=b"the next file"))
    listed = await _listed_hostile(connector, hostile, "/files")
    config = hostile.config("/files")

    with anyio.fail_after(30):
        with pytest.raises(ExternalServiceError) as raised:
            await connector.fetch_file(
                listed["/files/stalled.pdf"], out, config=config, credential=hostile.credential
            )
        fetched = await connector.fetch_file(
            listed["/files/next.pdf"], out, config=config, credential=hostile.credential
        )

    assert raised.value.message.startswith(f"{HOST} did not answer within ")
    assert fetched.path.read_bytes() == b"the next file"
    assert len(hostile.record.connections) == 2


async def test_a_connection_the_server_closed_between_files_is_opened_again(
    disk: _Served, connector: SftpConnector, out: Path
) -> None:
    """A server closes an idle connection while the sync embeds a long file: Azure's
    SFTP does after two minutes. That is not a failure of the next file, so the
    connection is opened again, and the next file reads."""
    record = disk.record
    assert record is not None
    first, second = disk.path("first.pdf"), disk.path("second.pdf")
    await disk.writer.write(first, b"the first file")
    await disk.writer.write(second, b"the second file")
    listed = await _listed(connector, disk)
    await connector.fetch_file(listed[first], out, config=disk.config(), credential=disk.login)

    for conn in record.connections:
        conn.close()
    with anyio.fail_after(5):
        await record.closed.wait()
    await asyncio.sleep(0.1)
    fetched = await connector.fetch_file(
        listed[second], out, config=disk.config(), credential=disk.login
    )

    assert fetched.path.read_bytes() == b"the second file"
    assert len(record.connections) == 2


async def test_a_download_past_its_deadline_fails_and_the_next_file_reads(
    hostile: _Hostile, connector: SftpConnector, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each request answers in time, and the file still never ends: a server that
    sends a trickle. The file's deadline is checked before each read, so the file
    fails, and the next file reads."""
    monkeypatch.setattr(sftp_module, "FILE_SECONDS", 0.5)
    body = bytes(20_000)
    hostile.add_file(
        "/files/trickle.pdf",
        _Fake(
            data=body, reads=_sending(lambda offset, _size: body[offset : offset + 1000], delay=0.2)
        ),
    )
    hostile.add_file("/files/next.pdf", _Fake(data=b"the next file"))
    listed = await _listed_hostile(connector, hostile, "/files")
    config = hostile.config("/files")

    with anyio.fail_after(30):
        with pytest.raises(ExternalServiceError):
            await connector.fetch_file(
                listed["/files/trickle.pdf"], out, config=config, credential=hostile.credential
            )
        fetched = await connector.fetch_file(
            listed["/files/next.pdf"], out, config=config, credential=hostile.credential
        )

    assert fetched.path.read_bytes() == b"the next file"
    assert hostile.record.seen("read", "/files/trickle.pdf") < 20
    assert hostile.record.seen("close", "/files/trickle.pdf") == 1
    assert hostile.record.seen("close", "/files/next.pdf") == 1


async def test_a_close_the_server_refuses_keeps_the_file_and_is_logged(
    hostile: _Hostile, connector: SftpConnector, out: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The bytes are written and both stats are taken before the CLOSE, so a CLOSE
    the server refuses changes nothing about the file. It is logged, for a server
    that keeps handles open."""
    hostile.add_file(
        "/files/closing.pdf", _Fake(data=b"closing", close_error=asyncssh.SFTPFailure("Failure"))
    )
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/closing.pdf"]

    with caplog.at_level(logging.WARNING, logger=sftp_module.__name__):
        fetched = await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert fetched.path.read_bytes() == b"closing"
    assert fetched.revision == listed.revision
    assert hostile.record.seen("close", "/files/closing.pdf") == 1
    assert [
        record
        for record in caplog.records
        if record.name.startswith(sftp_module.__name__) and record.levelno == logging.WARNING
    ]


async def test_a_close_that_stalls_drops_the_connection_and_the_next_file_reconnects(
    hostile: _Hostile, connector: SftpConnector, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLOSE the server never answers holds every later request on the
    connection behind it. The file's result stands, since both stats came before
    the CLOSE. The connection is dropped once the request deadline passes, and
    the next file is read on a new one."""
    monkeypatch.setattr(sftp_module, "REQUEST_SECONDS", 1.0)
    hostile.add_file("/files/stuck.pdf", _Fake(data=b"stuck", close_stall=True))
    hostile.add_file("/files/next.pdf", _Fake(data=b"the next file"))
    listed = await _listed_hostile(connector, hostile, "/files")
    config = hostile.config("/files")

    with anyio.fail_after(30):
        first = await connector.fetch_file(
            listed["/files/stuck.pdf"], out, config=config, credential=hostile.credential
        )
        second = await connector.fetch_file(
            listed["/files/next.pdf"], out, config=config, credential=hostile.credential
        )

    assert first.path.read_bytes() == b"stuck"
    assert first.revision == listed["/files/stuck.pdf"].revision
    assert second.path.read_bytes() == b"the next file"
    assert len(hostile.record.connections) == 2


@pytest.mark.security
async def test_a_refused_login_is_not_tried_again_in_the_same_sync(
    host_key: asyncssh.SSHKey,
    connector: SftpConnector,
    out: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A password changed during a sync costs one refused login, not one for each
    file. A server that locks an account after a few failures would otherwise
    lock it in the first sync after the change."""
    record = _Record()
    (tmp_path / "srv" / "drop").mkdir(parents=True)
    sftp = partial(_DiskSFTP, root=tmp_path / "srv", record=record)
    async with _serving(record, host_key, sftp, password=PASSWORD) as port:
        _pin(monkeypatch, port)
        config = _config(port, _line(host_key), "/drop")
        login = _ssh_login(port, password="an-old-password")
        file = RemoteFile(
            id="/drop/a.pdf", name="a.pdf", source_path=f"sftp://{USER}@{HOST}:{port}/drop/a.pdf"
        )

        with pytest.raises(BadRequestError) as listing:
            await connector.list_files(config, login)
        with pytest.raises(BadRequestError) as fetching:
            await connector.fetch_file(file, out, config=config, credential=login)
        with pytest.raises(BadRequestError):
            await connector.list_files(config, login)

    assert listing.value.message == _login_refused()
    assert fetching.value.message == listing.value.message
    assert record.passwords == ["an-old-password"]
    assert len(record.connections) == 1


@pytest.mark.security
async def test_a_host_key_mismatch_is_refused(
    server: _Served, connector: SftpConnector, worker_home: Path
) -> None:
    """The pinned key is the only proof that the server is the one the source
    names. A server that cannot prove it holds that key is refused before the
    login starts, so no password or signature reaches it. The worker's own known
    hosts trust the server here, so only the pin can refuse it. The impostor key is
    of the pinned key's type: of another type, the key exchange would fail first."""
    (worker_home / ".ssh" / "known_hosts").write_text(f"* {server.host_key}\n")
    impostor = _line(asyncssh.generate_private_key(server.host_key.split()[0]))

    with pytest.raises(BadRequestError) as raised:
        await connector.list_files(server.config(host_key=impostor), server.login)

    assert raised.value.message == HOST_KEY_REFUSED
    if server.record is not None:
        assert server.record.logins == []


@pytest.mark.security
async def test_a_server_without_the_pinned_key_type_is_refused(
    server: _Served, connector: SftpConnector
) -> None:
    """The client offers only the pinned key's type, so the server cannot answer
    with a key of another type, and a server without that type cannot be
    verified at all. Ed448: no server tested has one, and the disk server has
    only an ed25519 key. How the key exchange ends is the server's choice, and
    each way has its sentence. The asyncssh server and Apache MINA SSHD say why;
    OpenSSH 10.3 and SFTPGo 2.7.6 drop the connection; ProFTPD 1.3.8c disconnects
    "by application". In-process, the sentence is the one for the key type."""
    other_type = _line(asyncssh.generate_private_key("ssh-ed448"))
    key_type = (
        f"{HOST} and this worker could not agree on a host key or a cipher. "
        "Check that the server has a key of the type in this source's settings."
    )
    outcomes: dict[str, type[Exception]] = {
        key_type: BadRequestError,
        f"The connection to {HOST} was lost.": ExternalServiceError,
        f"{HOST} closed the connection.": ExternalServiceError,
    }
    allowed = [key_type] if server.kind == "in-process" else list(outcomes)

    with pytest.raises((BadRequestError, ExternalServiceError)) as raised:
        await connector.list_files(server.config(host_key=other_type), server.login)

    assert isinstance(raised.value, BadRequestError | ExternalServiceError)
    assert raised.value.message in allowed
    assert isinstance(raised.value, outcomes[raised.value.message])
    if server.record is not None:
        assert server.record.logins == []


@pytest.mark.security
@pytest.mark.parametrize("wrong", ["password", "key"])
async def test_a_wrong_password_and_a_wrong_key_are_refused(
    server: _Served, connector: SftpConnector, wrong: str
) -> None:
    """A login the server refuses is a sentence the user can act on: which host,
    which account, and where the login lives."""
    login = (
        server.login_with(password="not-the-password")
        if wrong == "password"
        else server.login_with(key=asyncssh.generate_private_key("ssh-ed25519"))
    )

    with pytest.raises(BadRequestError) as raised:
        await connector.list_files(server.config(), login)

    assert raised.value.message == _login_refused(server.user)


@pytest.mark.security
@pytest.mark.parametrize("method", ["key", "password"])
async def test_a_server_that_lets_anyone_in_is_refused(
    host_key: asyncssh.SSHKey,
    client_key: asyncssh.SSHKey,
    connector: SftpConnector,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """A server that accepts the `none` method checked nothing: it is not the
    server the login was made for, or it lets anyone read. The sync refuses it
    before it opens an SFTP session."""
    record = _Record()
    (tmp_path / "srv" / "drop").mkdir(parents=True)
    sftp = partial(_DiskSFTP, root=tmp_path / "srv", record=record)
    async with _serving(
        record, host_key, sftp, authorized=client_key, password=PASSWORD, anyone=True
    ) as port:
        _pin(monkeypatch, port)
        login = (
            _ssh_login(port, key=client_key)
            if method == "key"
            else _ssh_login(port, password=PASSWORD)
        )

        with pytest.raises(BadRequestError) as raised:
            await connector.list_files(_config(port, _line(host_key), "/drop"), login)

    assert raised.value.message == _login_refused()
    assert record.sessions == 0


@pytest.mark.security
async def test_a_password_is_never_sent_through_keyboard_interactive(
    host_key: asyncssh.SSHKey,
    connector: SftpConnector,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keyboard-interactive is a conversation the server writes, and asyncssh
    answers a prompt that says "password" with the password. A source logs in
    with the password method only, so a server that offers only the conversation
    is never sent the password, and the login fails.

    This guards the pair `kbdint_auth=False` and `preferred_auth`: asyncssh tries
    only the methods `preferred_auth` names, so either one alone keeps the
    password out of the conversation. The unit test of the options pins each."""
    record = _Record()
    (tmp_path / "srv" / "drop").mkdir(parents=True)
    sftp = partial(_DiskSFTP, root=tmp_path / "srv", record=record)
    async with _serving(record, host_key, sftp, password=PASSWORD, prompt_only=True) as port:
        _pin(monkeypatch, port)

        with pytest.raises(BadRequestError) as raised:
            await connector.list_files(
                _config(port, _line(host_key), "/drop"), _ssh_login(port, password=PASSWORD)
            )

    assert raised.value.message == _login_refused()
    assert record.prompts_answered == []


@contextmanager
def _opened_under(folder: Path) -> Iterator[list[str]]:
    """Every file opened under `folder` while the block runs, in any thread.

    asyncssh builds its options in an executor thread, so the spy replaces the
    process's own `open`, `io.open` and `os.open` for the block, and puts them back
    when it ends. pathlib reads through `io.open`, asyncssh through `open`.
    """
    prefix = str(folder)
    opened: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def note(file: object) -> None:
        if isinstance(file, str | bytes | os.PathLike):
            path = os.fsdecode(os.fspath(file))
            if path.startswith(prefix):
                opened.append(path)

    def spy_open(file: object, *args: object, **kwargs: object) -> object:
        note(file)
        return real_open(file, *args, **kwargs)

    def spy_os_open(path: object, *args: object, **kwargs: object) -> int:
        note(path)
        return real_os_open(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(builtins, "open", spy_open)
        patch.setattr(io, "open", spy_open)
        patch.setattr(os, "open", spy_os_open)
        yield opened


@asynccontextmanager
async def _agent_socket() -> AsyncIterator[tuple[str, list[str]]]:
    """An ssh-agent that holds no key: its socket path, and each connection to it.

    It answers as a real empty agent does, so a client that asks it goes on as it
    would, and the connection it records is the one sign that it was asked. In a
    folder of its own under the system temporary folder: a socket path holds about
    100 bytes, which a pytest `tmp_path` can pass.
    """
    folder = Path(tempfile.mkdtemp(prefix="agent-"))
    path = str(folder / "agent.sock")
    connections: list[str] = []
    no_identities = (5).to_bytes(4, "big") + bytes([12]) + (0).to_bytes(4, "big")
    failure = (1).to_bytes(4, "big") + bytes([5])

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append("connected")
        try:
            while True:
                length = int.from_bytes(await reader.readexactly(4), "big")
                request = await reader.readexactly(length)
                # 11 asks for the identities; anything else gets SSH_AGENT_FAILURE.
                writer.write(no_identities if request[:1] == bytes([11]) else failure)
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()

    unix_server = await asyncio.start_unix_server(answer, path=path)
    try:
        yield path, connections
    finally:
        unix_server.close()
        await unix_server.wait_closed()
        shutil.rmtree(folder)


@pytest.mark.security
@pytest.mark.parametrize(
    "case", ["a-wrong-pin", "a-wrong-password", "a-key-the-server-refuses", "the-right-key"]
)
async def test_the_worker_s_ssh_folder_and_agent_are_never_used(
    disk: _Served,
    connector: SftpConnector,
    client_key: asyncssh.SSHKey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """By default asyncssh reads the identity of whoever runs it: `~/.ssh/config`,
    its known hosts, its keys, and the agent at `SSH_AUTH_SOCK`. Here each of them
    would let the sync in: the known hosts trust the server, the key in `~/.ssh`
    is one the server accepts, and the config names another user and a proxy. A
    source connects with its own pin and its own login, or not at all, and
    nothing under `~/.ssh` is even opened."""
    ssh = tmp_path / "home" / ".ssh"
    ssh.mkdir(parents=True)
    proxied = tmp_path / "proxied"
    (ssh / "config").write_text(f"Host *\n    User intruder\n    ProxyCommand touch {proxied}\n")
    (ssh / "known_hosts").write_text(f"[127.0.0.1]:{disk.port} {disk.host_key}\n")
    (ssh / "id_ed25519").write_bytes(client_key.export_private_key("openssh"))
    (ssh / "id_ed25519").chmod(0o600)
    monkeypatch.setenv("HOME", str(ssh.parent))
    config, login, refusal = disk.config(), disk.login, _login_refused()
    if case == "a-wrong-pin":
        config = disk.config(host_key=_line(asyncssh.generate_private_key("ssh-ed25519")))
        refusal = HOST_KEY_REFUSED
    elif case == "a-wrong-password":
        login = disk.login_with(password="not-the-password")
    elif case == "a-key-the-server-refuses":
        login = disk.login_with(key=asyncssh.generate_private_key("ssh-ed25519"))

    async with _agent_socket() as (agent, agent_connections):
        monkeypatch.setenv("SSH_AUTH_SOCK", agent)
        with _opened_under(ssh) as opened:
            if case == "the-right-key":
                listing = await connector.list_files(config, login)
                assert listing.complete is True
            else:
                with pytest.raises(BadRequestError) as raised:
                    await connector.list_files(config, login)
                assert raised.value.message == refusal

    assert opened == []
    assert agent_connections == []
    assert not proxied.exists()
    record = disk.record
    assert record is not None
    assert "intruder" not in record.logins


async def test_a_source_syncs_a_tree_into_the_listing_with_its_addresses(
    server: _Served, connector: SftpConnector, out: Path
) -> None:
    """What a listing hands the sync: each file at its full remote path, under an
    address that names the account, the host and the port, so two sources share a
    document only when they see one file. Names with spaces, letters outside ASCII,
    `#`, `?` and `%` list and read as themselves: the fetch reads the path, never
    the address, where those three would end or change the path. A file of a type no parser reads is not
    listed, so it is not downloaded and failed on every run."""
    bodies = {
        "report.pdf": b"report",
        "spaced name ü €.md": b"spaces and unicode",
        "report #3 50% off?.md": b"url characters",
        "SCAN.PDF": b"a scan",
        "sub/notes.txt": b"notes",
        "sub/deeper/data.docx": b"a document",
        "archive.zip": b"an archive",
        "sub/photo.png": b"a photo",
    }
    for relative, body in bodies.items():
        await server.writer.write(server.path(relative), body, mtime=MTIME)

    listing = await connector.list_files(server.config(), server.login)
    by_id = {file.id: file for file in listing.files}
    spaced = by_id[server.path("spaced name ü €.md")]
    fetched = await connector.fetch_file(
        spaced, out, config=server.config(), credential=server.login
    )
    marked = by_id[server.path("report #3 50% off?.md")]
    fetched_marked = await connector.fetch_file(
        marked, out, config=server.config(), credential=server.login
    )

    assert sorted(by_id) == sorted(
        server.path(relative)
        for relative in (
            "report.pdf",
            "spaced name ü €.md",
            "report #3 50% off?.md",
            "SCAN.PDF",
            "sub/notes.txt",
            "sub/deeper/data.docx",
        )
    )
    assert {file.source_path for file in listing.files} == {server.address(path) for path in by_id}
    user = quote(server.user, safe="")
    assert spaced.source_path == f"sftp://{user}@{HOST}:{server.port}{spaced.id}"
    assert spaced.id == f"{server.root}/spaced name ü €.md"
    assert spaced.name == "spaced name ü €.md"
    assert spaced.size == len(b"spaces and unicode")
    assert spaced.modified_at == datetime.fromtimestamp(MTIME, UTC)
    assert listing.complete is True
    assert listing.problems == []
    assert fetched.path.read_bytes() == b"spaces and unicode"
    assert marked.id == f"{server.root}/report #3 50% off?.md"
    assert marked.source_path == f"sftp://{user}@{HOST}:{server.port}{marked.id}"
    assert fetched_marked.path.read_bytes() == b"url characters"


_NOT_REGULAR = {
    "link": _typed(stat.S_IFLNK),
    "fifo": _typed(stat.S_IFIFO),
    "socket": _typed(stat.S_IFSOCK),
    "device": _typed(stat.S_IFCHR),
    "folder": _typed(stat.S_IFDIR),
    "untyped": _untyped(6),
}


@pytest.mark.security
@pytest.mark.parametrize("kind", list(_NOT_REGULAR))
@pytest.mark.parametrize("which", ["lstat", "fstat"])
async def test_a_listed_file_that_is_no_longer_regular_when_fetched_fails(
    hostile: _Hostile, connector: SftpConnector, out: Path, which: str, kind: str
) -> None:
    """The listing said a regular file, and the server now says something else.
    SFTP v3 cannot open a path without following a link, so the type is checked
    by the LSTAT before the open and the FSTAT after it, and any type but a
    regular file is refused, not a link alone: a FIFO opened on OpenSSH blocks the
    server process, and a device reads without end. A type that changed is a
    failure, not a change: one answer of that kind proves nothing about the file,
    and as a change the file would be skipped on every run."""
    turned = _NOT_REGULAR[kind]
    hostile.add_file(
        "/files/turned.pdf",
        _Fake(
            data=b"turned",
            lstats=[turned] if which == "lstat" else None,
            # What a STAT answers: the regular file a link points at.
            stats=[_regular(6)],
            fstats=[turned] if which == "fstat" else None,
        ),
    )
    listed = (await _listed_hostile(connector, hostile, "/files"))["/files/turned.pdf"]

    with pytest.raises(ExternalServiceError) as raised:
        await connector.fetch_file(
            listed, out, config=hostile.config("/files"), credential=hostile.credential
        )

    assert (
        raised.value.message
        == "The server listed turned.pdf as a file, and now says it is not one."
    )
    assert hostile.record.seen("read", "/files/turned.pdf") == 0
    assert hostile.record.seen("stat", "/files/turned.pdf") == 0
    opened = 0 if which == "lstat" else 1
    assert hostile.record.seen("open", "/files/turned.pdf") == opened
    assert hostile.record.seen("close", "/files/turned.pdf") == opened
