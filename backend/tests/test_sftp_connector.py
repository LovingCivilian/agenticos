"""The SFTP sync connector's rules that need no server (#29).

Each test pins one rule of `app.services.rag.connectors.sftp` in isolation: how
the config spells a host, a root folder and a host key, the address a file is
filed under, the credential checks, the options asyncssh is given, the order in
which pinned addresses are dialled, our sentence for each asyncssh failure, the
abort order of a request past its deadline, and the deployment's allowlist of
private servers.

asyncssh's own exception classes and real keys are used, never stand-ins, so a
mapping cannot pass on a copy of the thing it maps. `asyncssh.connect` is
replaced by a recorder, so nothing is dialled. The connect tests patch
`resolve_pinned_url` in the module, as `test_git_connector.py` does. The
allowlist tests patch only `getaddrinfo`, as `test_ssrf.py` does, so the real
guard decides. `tests/integration/test_sftp_connector.py` runs the connector
against an SFTP server.
"""

import asyncio
import base64
import inspect
import logging
import socket
import stat
import struct
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit

import anyio
import asyncssh
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.exceptions import AppException, BadRequestError, ExternalServiceError
from app.core.sanitize import PinnedAddress, SSRFBlockedError, UrlRefusedError
from app.core.secret_kinds import ApiKeySecret, GitTokenSecret, SshCredentialsSecret
from app.services.rag.connectors import ConfigRefusal, RemoteFile
from app.services.rag.connectors import sftp as sftp_module
from app.services.rag.connectors.sftp import SftpConfig, SftpConnector

pytestmark = pytest.mark.anyio

HOST = "sftp.example.com"
PASSWORD = "correct-horse-battery-staple"
PASSPHRASE = "passphrase-of-the-key"
SERVER_TEXT = "SERVER-TEXT"
MTIME = 1_700_000_000
# The part of the refusal of a host key pasted with something in front of it
# (`design-amendments.md`, A3) that the other host key refusals do not say.
IN_FRONT = "without the host name or the options"
PINNED = PinnedAddress(hostname=HOST, port=22, ips=("93.184.216.34",))

_HOST_KEY = asyncssh.generate_private_key("ssh-ed25519")

# Read before any test replaces `asyncssh.connect`, so a recorded call is bound to
# the real signature whether the connector passes an argument by position or by name.
_CONNECT = inspect.signature(asyncssh.connect)


def _line(key: asyncssh.SSHKey) -> str:
    """A key's OpenSSH public line, `<type> <base64>`, without its comment."""
    return " ".join(key.export_public_key().decode().split()[:2])


HOST_KEY_LINE = _line(_HOST_KEY)


def _echoes(shown: str, secret: str, *, not_secret: tuple[str, ...] = (), width: int = 12) -> bool:
    """Whether `shown` holds a run of `width` characters that is `secret`'s own.

    A part is enough: a log line can cut a value. PEM armour lines are not secret.
    Nor is a run that also occurs in `not_secret`: a private key file holds its
    public half, and every key of one format starts with the same header.
    """
    body = "".join(line for line in secret.splitlines() if not line.startswith("-----"))
    runs = {body[i : i + width] for i in range(len(body) - width + 1)}
    return any(run in shown for run in runs if not any(run in text for text in not_secret))


def _config(**overrides: object) -> dict[str, object]:
    return {"host": HOST, "port": 22, "root_path": "/drop", "host_key": HOST_KEY_LINE, **overrides}


def _without(name: str) -> dict[str, object]:
    return {key: value for key, value in _config().items() if key != name}


def _login(host: str = HOST, **fields: object) -> SshCredentialsSecret:
    """A login as `sync`, bound to `host`: by password, or by `private_key` when given."""
    if "private_key" not in fields:
        fields["password"] = PASSWORD
    return SshCredentialsSecret.model_validate({"username": "sync", "host": host, **fields})


def _key_login(key: asyncssh.SSHKey, form: str, host: str = HOST) -> SshCredentialsSecret:
    """A login by `key`, as the vault holds it: `openssh` as `ssh-keygen` writes it
    without a passphrase, or `encrypted-openssh` or `encrypted-pkcs8` with PASSPHRASE."""
    if form == "openssh":
        return _login(host, private_key=key.export_private_key("openssh").decode())
    pem = key.export_private_key(
        "openssh" if form == "encrypted-openssh" else "pkcs8-pem",
        PASSPHRASE,
        rounds=16,
        ignore_few_rounds=True,
    ).decode()
    return _login(host, private_key=pem, passphrase=PASSPHRASE)


def _corrupted_line(key_type: str) -> str:
    """A public key line of `key_type` that asyncssh refuses with a plain `ValueError`.

    The type and every length stay right, so only the key itself is wrong: the
    ECDSA point gets an unknown prefix byte ("Unsupported elliptic curve point
    type"), and the RSA exponent is made even ("e must be odd.").
    """
    blob = base64.b64decode(_line(asyncssh.generate_private_key(key_type)).split()[1])
    fields: list[bytes] = []
    while blob:
        size = struct.unpack(">I", blob[:4])[0]
        fields.append(blob[4 : 4 + size])
        blob = blob[4 + size :]
    if key_type == "ssh-rsa":
        fields[1] = b"\x04"
    else:
        fields[2] = b"\x05" + fields[2][1:]
    packed = b"".join(struct.pack(">I", len(field)) + field for field in fields)
    return f"{key_type} {base64.b64encode(packed).decode()}"


def _remote_file() -> RemoteFile:
    return RemoteFile(
        id="/drop/a.pdf", name="a.pdf", source_path=f"sftp://sync@{HOST}:22/drop/a.pdf"
    )


def _file(size: int) -> asyncssh.SFTPAttrs:
    """What a v3 server sends for a regular file in a listing."""
    return asyncssh.SFTPAttrs(
        type=asyncssh.FILEXFER_TYPE_REGULAR,
        size=size,
        permissions=stat.S_IFREG | 0o644,
        atime=MTIME,
        mtime=MTIME,
    )


def _folder() -> asyncssh.SFTPAttrs:
    return asyncssh.SFTPAttrs(
        type=asyncssh.FILEXFER_TYPE_DIRECTORY,
        size=4096,
        permissions=stat.S_IFDIR | 0o755,
        atime=MTIME,
        mtime=MTIME,
    )


def _pin(
    monkeypatch: pytest.MonkeyPatch,
    pinned: PinnedAddress = PINNED,
    *,
    ran_on: list[int] | None = None,
) -> list[str]:
    """Answer the connector's address check with `pinned`. Returns the URLs it checked.

    `ran_on`, when given, gets the id of the thread each check ran on.
    """
    checked: list[str] = []

    def resolve(
        url: str,
        allowed_schemes: frozenset[str] | None = None,
        *,
        trusted_hosts: frozenset[str] = frozenset(),
    ) -> PinnedAddress:
        checked.append(url)
        if ran_on is not None:
            ran_on.append(threading.get_ident())
        return pinned

    monkeypatch.setattr(sftp_module, "resolve_pinned_url", resolve)
    return checked


def _resolve_to(monkeypatch: pytest.MonkeyPatch, *ips: str) -> list[tuple[str, int]]:
    """Make every name resolve to `ips`, below the real guard. Returns what was looked up."""
    asked: list[tuple[str, int]] = []

    def getaddrinfo(host: str, port: int, *_args: object, **_kwargs: object) -> list[tuple]:
        asked.append((host, port))
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (ip, port),
            )
            for ip in ips
        ]

    monkeypatch.setattr("app.core.sanitize.socket.getaddrinfo", getaddrinfo)
    return asked


class _Sftp:
    """An SFTP session that answers a listing from a map of folder paths to entries."""

    def __init__(
        self, folders: dict[bytes, list[tuple[bytes, asyncssh.SFTPAttrs]]] | None = None
    ) -> None:
        self.folders = folders or {}

    async def scandir(self, path: bytes) -> AsyncIterator[asyncssh.SFTPName]:
        for name, attrs in self.folders[path]:
            yield asyncssh.SFTPName(name, attrs=attrs)

    def exit(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _Connection:
    """An SSH connection that records, in order, what is done to it."""

    def __init__(
        self,
        *,
        sftp: _Sftp | None = None,
        refuse_sftp: BaseException | None = None,
        stall_close: bool = False,
    ) -> None:
        self.sftp = sftp or _Sftp()
        self.refuse_sftp = refuse_sftp
        self.stall_close = stall_close
        self.order: list[str] = []
        self.aborted = asyncio.Event()
        self.sftp_options: dict[str, object] | None = None

    async def start_sftp_client(self, **options: object) -> _Sftp:
        self.sftp_options = options
        if self.refuse_sftp is not None:
            raise self.refuse_sftp
        return self.sftp

    def is_closed(self) -> bool:
        return "abort" in self.order or "close" in self.order

    def abort(self) -> None:
        self.order.append("abort")
        self.aborted.set()

    def close(self) -> None:
        self.order.append("close")

    async def wait_closed(self) -> None:
        if self.stall_close:
            # A server that never answers the close: only an abort ends the wait.
            await self.aborted.wait()
        self.order.append("wait_closed")


class _Dialler:
    """Stands in for `asyncssh.connect`. Records each call, then gives the next answer.

    An answer that is an exception is raised. A call past the last answer raises
    `IndexError`, which no connector catches, so an extra dial fails the test.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *answers: object) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, object]] = []
        monkeypatch.setattr(asyncssh, "connect", self.connect)

    async def connect(self, *args: object, **kwargs: object) -> object:
        bound = _CONNECT.bind(*args, **kwargs)
        bound.apply_defaults()
        self.calls.append(dict(bound.arguments))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def hosts(self) -> list[object]:
        return [call["host"] for call in self.calls]


@pytest.fixture
def worker_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A worker whose own SSH setup would answer each default asyncssh reads.

    A config file that names another user, another host, a host key alias and a
    proxy command, a known_hosts file, a key that a server could accept, a
    certificate folder, an agent socket path and a local user name. An option the
    connector leaves at its default shows one of them.
    """
    ssh = tmp_path / "home" / ".ssh"
    (ssh / "crt").mkdir(parents=True)
    worker_key = asyncssh.generate_private_key("ssh-ed25519")
    (ssh / "id_ed25519").write_bytes(worker_key.export_private_key())
    (ssh / "config").write_text(
        "Host *\n"
        "  User from-config\n"
        "  HostName 192.0.2.66\n"
        "  HostKeyAlias worker-alias\n"
        "  ProxyCommand nc %h %p\n"
        "  IdentityFile ~/.ssh/id_ed25519\n"
    )
    (ssh / "known_hosts").write_text(f"* {_line(worker_key)}\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    monkeypatch.setenv("USER", "worker-user")
    monkeypatch.setenv("LOGNAME", "worker-user")
    return tmp_path / "home"


class TestWhatTheFormAccepts:
    @pytest.mark.security
    @pytest.mark.parametrize(
        ("host", "says"),
        [
            pytest.param("sftp://sftp.example.com", None, id="scheme"),
            pytest.param("sftp.example.com/drop", None, id="path"),
            pytest.param("sync@sftp.example.com", None, id="user"),
            pytest.param("sftp.example.com:2222", "Port field", id="port"),
            pytest.param("[2606:4700::1111]:2222", None, id="ipv6-with-port"),
            pytest.param("fe80::1%eth0", None, id="ipv6-zone"),
            pytest.param("sftp example.com", None, id="whitespace"),
            pytest.param("sftp..example.com", None, id="empty-label"),
            pytest.param("-sftp.example.com", None, id="label-starts-with-hyphen"),
            pytest.param("sftp-.example.com", None, id="label-ends-with-hyphen"),
            pytest.param("sftp.example.com.", None, id="trailing-dot"),
            pytest.param(".".join(["a" * 63] * 3 + ["a" * 62]), None, id="254-characters"),
            pytest.param("a" * 64 + ".example.com", None, id="label-of-64"),
        ],
    )
    async def test_a_host_with_a_scheme_path_user_or_port_is_refused_on_the_host_field(
        self, host: str, says: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The host field holds one host and nothing else.

        A scheme, a path, a user or a port in it would give the URL that the
        address check reads a second meaning. Each one is refused on the field,
        before anything is resolved. A port gets a sentence that says where it goes.
        """
        checked = _pin(monkeypatch)

        refusal = await SftpConnector().validate_config(_config(host=host))

        assert refusal is not None
        assert refusal.field == "host"
        if says is not None:
            assert says in refusal.message
        assert checked == []

    @pytest.mark.parametrize(
        ("host", "stored"),
        [
            pytest.param("Bücher.Example", "xn--bcher-kva.example", id="idna"),
            pytest.param("  SFTP.Example.COM  ", "sftp.example.com", id="case-and-spaces"),
            pytest.param("[2606:4700::1111]", "2606:4700::1111", id="ipv6-brackets"),
            pytest.param("[2606:4700:0:0:0:0:0:ABCD]", "2606:4700::abcd", id="ipv6-compressed"),
            pytest.param("2606:4700::1111", "2606:4700::1111", id="ipv6-bare"),
            pytest.param("93.184.216.34", "93.184.216.34", id="ipv4"),
            pytest.param(
                ".".join(["a" * 63] * 3 + ["a" * 61]),
                ".".join(["a" * 63] * 3 + ["a" * 61]),
                id="253-characters-in-labels-of-63",
            ),
        ],
    )
    def test_an_internationalized_host_is_stored_idna_encoded_and_an_ipv6_host_without_brackets(
        self, host: str, stored: str
    ) -> None:
        """A host is stored as `urlparse` spells the host of the URL it builds.

        The address check, the credential's host binding and the allowlist all
        compare that spelling exactly. So a name is lower case and IDNA-encoded,
        and an IPv6 address has no brackets and its compressed form.
        """
        assert SftpConfig.model_validate(_config(host=host)).host == stored

    @pytest.mark.security
    @pytest.mark.parametrize("port", [0, -1, 65536])
    async def test_port_zero_is_refused_rather_than_read_as_eighty(
        self, port: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The address check reads `sftp://host:0` as port 80, its default.

        So port 0 would be checked as one port and given to asyncssh as another.
        It is refused on the port field, before the check runs. So is a port past
        65535.
        """
        checked = _pin(monkeypatch)

        refusal = await SftpConnector().validate_config(_config(port=port))

        assert refusal is not None
        assert refusal.field == "port"
        assert checked == []

    @pytest.mark.security
    @pytest.mark.parametrize(
        "root",
        [
            pytest.param("drop", id="relative"),
            pytest.param("./drop", id="dot-relative"),
            pytest.param("/drop/../etc", id="dot-dot"),
            pytest.param("/..", id="dot-dot-at-root"),
            pytest.param("/drop/a\x00b", id="nul"),
            pytest.param("/drop/a\nb", id="newline"),
            pytest.param("/drop/a\x7fb", id="delete"),
            pytest.param("/" + "a" * 1024, id="over-1024"),
        ],
    )
    async def test_a_relative_root_or_one_with_dot_dot_is_refused(self, root: str) -> None:
        """The root folder is an absolute path that names one folder.

        A relative path depends on where the server starts the session, and `..`
        reaches above the folder the editor chose. A control character cannot be
        part of a folder the form shows.
        """
        refusal = await SftpConnector().validate_config(_config(root_path=root))

        assert refusal is not None
        assert refusal.field == "root_path"

    @pytest.mark.parametrize(
        ("root", "kept"),
        [
            pytest.param("/drop//2026/./q3/", "/drop/2026/q3", id="slashes"),
            pytest.param("/", "/", id="root"),
            pytest.param("//drop", "/drop", id="leading-double-slash"),
            pytest.param("//", "/", id="root-double-slash"),
            pytest.param("  /drop/2026/ \n", "/drop/2026", id="surrounding-whitespace"),
            pytest.param("\t/drop\r\n", "/drop", id="tab-and-line-end"),
            pytest.param(
                "/" + "/".join(["a" * 255] * 4),
                "/" + "/".join(["a" * 255] * 4),
                id="1024-characters",
            ),
        ],
    )
    def test_a_root_folder_is_kept_absolute_with_single_slashes(self, root: str, kept: str) -> None:
        """A file's address holds the full path. One folder has one spelling, so two
        sources that name it in two ways file its documents under one address.
        `PurePosixPath` keeps a leading `//`, which POSIX leaves to the system, so
        the connector folds it itself (`design-amendments.md`, A17). Whitespace
        around the folder, such as the line end of a pasted path, is trimmed before
        anything is checked, so it is neither refused nor kept (A19)."""
        assert SftpConfig.model_validate(_config(root_path=root)).root_path == kept

    @pytest.mark.security
    @pytest.mark.parametrize(
        ("value", "pem", "private", "in_front"),
        [
            pytest.param(
                lambda: f"{HOST} {HOST_KEY_LINE}", False, False, True, id="known-hosts-line"
            ),
            pytest.param(
                lambda: f"[{HOST}]:2222 {HOST_KEY_LINE}",
                False,
                False,
                True,
                id="known-hosts-port",
            ),
            pytest.param(
                lambda: f'from="10.0.0.0/8" {HOST_KEY_LINE}',
                False,
                False,
                True,
                id="authorized-keys",
            ),
            pytest.param(
                lambda: f"256 SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s {HOST} (ED25519)",
                False,
                False,
                False,
                id="fingerprint",
            ),
            pytest.param(
                lambda: _HOST_KEY.export_private_key("openssh").decode(),
                True,
                True,
                False,
                id="private-openssh",
            ),
            pytest.param(
                lambda: _HOST_KEY.export_private_key("pkcs8-pem").decode(),
                True,
                True,
                False,
                id="private-pkcs8",
            ),
            pytest.param(
                lambda: _HOST_KEY.export_public_key("pkcs8-pem").decode(),
                True,
                False,
                False,
                id="public-pem",
            ),
            pytest.param(
                lambda: _line(asyncssh.generate_private_key("ssh-dss")),
                False,
                False,
                False,
                id="dsa",
            ),
            pytest.param(
                lambda: (
                    _HOST_KEY.generate_host_certificate(_HOST_KEY, "files")
                    .export_certificate()
                    .decode()
                    .strip()
                ),
                False,
                False,
                False,
                id="host-certificate",
            ),
            pytest.param(
                lambda: f"ssh-rsa {HOST_KEY_LINE.split()[1]}",
                False,
                False,
                False,
                id="type-not-the-key",
            ),
            pytest.param(
                lambda: f"ssh-ed25519 {HOST_KEY_LINE}", False, False, False, id="type-twice"
            ),
            pytest.param(
                lambda: _corrupted_line("ecdsa-sha2-nistp256"),
                False,
                False,
                False,
                id="ecdsa-point-not-on-a-known-form",
            ),
            pytest.param(
                lambda: _corrupted_line("ssh-rsa"), False, False, False, id="rsa-even-exponent"
            ),
            pytest.param(
                lambda: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIbroken",
                False,
                False,
                False,
                id="broken",
            ),
            pytest.param(
                lambda: f"{HOST_KEY_LINE}\n{_line(asyncssh.generate_private_key('ssh-ed25519'))}",
                False,
                False,
                False,
                id="two-keys",
            ),
        ],
    )
    async def test_a_known_hosts_line_a_pem_block_or_an_unknown_key_type_is_refused_as_a_host_key(
        self,
        value: Callable[[], str],
        pem: bool,
        private: bool,
        in_front: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The host key is one OpenSSH public key line of a type the config names.

        `ssh-keyscan` prints a known_hosts line, with the host first, and an
        authorized_keys line can have options first. Such a line is told to remove
        what is in front of the key. A key type pasted twice has neither in front.
        Any other paste that is not one key line, such as the fingerprint
        `ssh-keygen -lf` prints, two lines or a PEM block, gets the general sentence.
        Each sentence is ours: the text asyncssh or `cryptography` raises never
        reaches the form, and the refusal does not repeat the paste. A corrupted
        ECDSA or RSA line makes them raise a plain `ValueError`, not `KeyImportError`
        (`design-amendments.md`, A12).

        A PEM block is refused before asyncssh reads it, because asyncssh accepts a
        private key there and answers its public half. So a private key pasted by
        mistake is never read, and the refusal does not repeat it. A type outside
        the list (DSA, a certificate) is refused.

        A line whose type is not its key's (`ssh-rsa` in front of an Ed25519 key)
        is refused by asyncssh's own check: `import_public_key` raises
        `KeyImportError` ("Public key algorithm mismatch"). The connector keeps no
        check of its own (`design-amendments.md`, A2), so that case pins
        asyncssh's behaviour.
        """
        pasted = value()
        read: list[object] = []
        real_import = asyncssh.import_public_key
        try:
            real_import(pasted.strip())
        except ValueError as exc:
            library: str | None = str(exc)
        else:
            library = None

        def import_public_key(data: object, *args: object, **kwargs: object) -> asyncssh.SSHKey:
            read.append(data)
            return real_import(data, *args, **kwargs)

        monkeypatch.setattr(asyncssh, "import_public_key", import_public_key)
        _pin(monkeypatch)

        refusal = await SftpConnector().validate_config(_config(host_key=pasted))

        assert refusal is not None
        assert refusal.field == "host_key"
        assert (IN_FRONT in refusal.message) is in_front
        assert library is None or library not in refusal.message
        assert max(pasted.split(), key=len) not in refusal.message
        if pem:
            assert read == []
        if private:
            assert not _echoes(refusal.message, pasted)

    @pytest.mark.parametrize(
        "key_type",
        [
            "ssh-ed25519",
            "ssh-ed448",
            "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384",
            "ecdsa-sha2-nistp521",
            "ssh-rsa",
        ],
    )
    def test_a_host_key_line_is_kept_without_its_comment(self, key_type: str) -> None:
        """The pin is `<type> <base64>`. The comment names the machine that made the
        key, which is not part of the key, so the stored value leaves it out."""
        key = asyncssh.generate_private_key(key_type, comment="admin@files")
        pasted = f"  {key.export_public_key().decode()}  "

        assert SftpConfig.model_validate(_config(host_key=pasted)).host_key == _line(key)

    def test_the_host_key_field_is_drawn_as_a_text_area(self) -> None:
        """An RSA host key line is about 550 characters. The wizard draws a field
        marked `x-textarea` as a text area, so the whole key can be pasted and read."""
        schema = SftpConfig.model_json_schema()

        assert schema["properties"]["host_key"].get("x-textarea") is True

    @pytest.mark.security
    @pytest.mark.parametrize(
        ("config", "field"),
        [
            pytest.param(_without("host"), "host", id="no-host"),
            pytest.param(_without("root_path"), "root_path", id="no-root"),
            pytest.param(_without("host_key"), "host_key", id="no-host-key"),
            pytest.param(_config(port="twenty-two"), "port", id="port-not-a-number"),
            pytest.param(
                _config(include_subfolders="sometimes"), "include_subfolders", id="not-a-bool"
            ),
            pytest.param(_config(extensions=["*.pdf"]), "extensions", id="not-an-extension"),
            pytest.param(_config(extensions=[" "]), "extensions", id="no-extension"),
        ],
    )
    async def test_validate_config_names_the_field_it_refuses(
        self, config: dict[str, object], field: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wizard draws one input per config field and marks the one a refusal
        names. The message is one sentence: pydantic's error text, with its prefix,
        the value typed and a link, is not shown."""
        checked = _pin(monkeypatch)

        refusal = await SftpConnector().validate_config(config)

        assert refusal is not None
        assert refusal.field == field
        assert "Value error" not in refusal.message
        assert "input_value" not in refusal.message
        assert "\n" not in refusal.message
        assert checked == []

    @pytest.mark.security
    @pytest.mark.parametrize(
        "refused",
        [
            pytest.param(
                SSRFBlockedError("Blocked: 'files.internal' resolves to a private address."),
                id="inside-the-network",
            ),
            pytest.param(UrlRefusedError("The URL's host name is not valid"), id="not-a-url"),
        ],
    )
    async def test_validate_config_refuses_a_host_inside_the_network_on_the_host_field(
        self, refused: UrlRefusedError, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The address check runs on the form as well as at sync time, so a host
        inside the network is refused where it was typed. The guard's sentence names
        only the host, so the form shows it as it is."""
        _pin(monkeypatch)
        assert await SftpConnector().validate_config(_config()) is None

        def resolve(*_args: object, **_kwargs: object) -> PinnedAddress:
            raise refused

        monkeypatch.setattr(sftp_module, "resolve_pinned_url", resolve)

        assert await SftpConnector().validate_config(_config()) == ConfigRefusal(
            message=str(refused), field="host"
        )

    @pytest.mark.parametrize("call_site", ["form", "sync"])
    async def test_the_address_check_runs_off_the_event_loop(
        self, call_site: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The address check resolves the host name, and a lookup blocks. The form
        check runs on the API's request loop, so a name server that does not answer
        would stop every other request and agent stream there. Both checks run in a
        thread (`design-amendments.md`, A17)."""
        loop_thread = threading.get_ident()
        ran_on: list[int] = []
        _pin(monkeypatch, ran_on=ran_on)
        _Dialler(monkeypatch, _Connection())

        if call_site == "form":
            assert await SftpConnector().validate_config(_config()) is None
        else:
            await SftpConnector()._client(SftpConfig.model_validate(_config()), _login())

        assert ran_on
        assert all(thread != loop_thread for thread in ran_on)

    @pytest.mark.security
    @pytest.mark.parametrize(
        ("config", "bound", "url"),
        [
            pytest.param(_config(), HOST, "sftp://sftp.example.com:22", id="default-port"),
            pytest.param(
                _config(port=2222), f"{HOST}:2222", "sftp://sftp.example.com:2222", id="port"
            ),
            pytest.param(
                _config(host="2606:4700::1111"),
                "[2606:4700::1111]",
                "sftp://[2606:4700::1111]:22",
                id="ipv6",
            ),
        ],
    )
    async def test_the_url_checked_for_ssrf_always_names_the_port(
        self, config: dict[str, object], bound: str, url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`resolve_pinned_url` reads a URL without a port as port 80, for every
        scheme but https. So the URL the guard checks always names the port, and the
        port the guard pins is the port asyncssh dials."""
        parsed = SftpConfig.model_validate(config)
        looked_up = _resolve_to(monkeypatch, "93.184.216.34")
        dials = _Dialler(monkeypatch, _Connection())

        assert parsed.url() == url
        assert await SftpConnector().validate_config(config) is None
        await SftpConnector()._client(parsed, _login(bound))

        assert all(port == parsed.port for _host, port in looked_up)
        assert dials.calls[0]["port"] == parsed.port


class TestTheAddress:
    async def test_the_address_names_the_user_host_and_port_and_keeps_the_path_raw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file is filed under `sftp://<user>@<host>:<port><path>`.

        Two accounts on one server can see different files at one path, so the
        user is in the address with the server. The path is the full remote path,
        not encoded: the document's file name is read from it.
        """
        sftp = _Sftp(
            {
                b"/drop": [("report q3 ü.pdf".encode(), _file(10)), (b"2026", _folder())],
                b"/drop/2026": [(b"a b.md", _file(5))],
            }
        )
        _pin(
            monkeypatch,
            PinnedAddress(hostname="xn--bcher-kva.example", port=2222, ips=("93.184.216.34",)),
        )
        _Dialler(monkeypatch, _Connection(sftp=sftp))
        connector = SftpConnector()

        listing = await connector.list_files(
            _config(host="Bücher.example", port=2222), _login("xn--bcher-kva.example:2222")
        )

        authority = "sftp://sync@xn--bcher-kva.example:2222"
        assert {f.id: (f.name, f.source_path) for f in listing.files} == {
            "/drop/report q3 ü.pdf": ("report q3 ü.pdf", f"{authority}/drop/report q3 ü.pdf"),
            "/drop/2026/a b.md": ("a b.md", f"{authority}/drop/2026/a b.md"),
        }

    async def test_an_ipv6_source_lists_with_a_login_bound_to_its_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The vault stores an IPv6 host in brackets, as a URL writes it, and the
        source stores it without them. The login still allows the source, and the
        address puts the host in brackets, so its port stays apart from it."""
        sftp = _Sftp({b"/drop": [(b"a.pdf", _file(3))]})
        _pin(
            monkeypatch,
            PinnedAddress(hostname="2606:4700::1111", port=2222, ips=("2606:4700::1111",)),
        )
        dials = _Dialler(monkeypatch, _Connection(sftp=sftp))

        listing = await SftpConnector().list_files(
            _config(host="2606:4700::1111", port=2222), _login("[2606:4700::1111]:2222")
        )

        assert [f.source_path for f in listing.files] == [
            "sftp://sync@[2606:4700::1111]:2222/drop/a.pdf"
        ]
        assert dials.hosts == ["2606:4700::1111"]

    def test_a_user_name_with_at_or_slash_is_encoded_in_the_address(self) -> None:
        """The user name comes from the vault and can hold `@`, `/` or `:`. Each is
        encoded, so the address still names the configured host and port."""
        authority = SftpConfig.model_validate(_config()).authority("ops@corp/x:y")
        parts = urlsplit(authority)

        assert authority == "sftp://ops%40corp%2Fx%3Ay@sftp.example.com:22"
        assert (parts.hostname, parts.port) == (HOST, 22)
        assert unquote(parts.username or "") == "ops@corp/x:y"


class TestTheCredential:
    @pytest.mark.security
    @pytest.mark.parametrize("entry", ["list", "fetch"])
    @pytest.mark.parametrize(
        ("credential", "says"),
        [
            pytest.param(None, "no credential", id="none"),
            pytest.param(
                GitTokenSecret(token=SecretStr("github_pat_secret_value"), host=f"{HOST}:22"),
                "SSH login",
                id="git-token-for-the-same-host",
            ),
            pytest.param(
                ApiKeySecret(api_key=SecretStr("sk-model-provider-key")),
                "SSH login",
                id="api-key",
            ),
        ],
    )
    async def test_a_source_without_a_credential_or_with_another_kind_is_refused(
        self,
        entry: str,
        credential: GitTokenSecret | ApiKeySecret | None,
        says: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An SFTP source logs in only with an SSH login from the vault.

        Another kind of secret is refused even when it names the same host: its
        value was not stored for this server. The Git token here is bound to this
        host and port, so its own host binding would let it through, and only the
        kind check refuses it. These checks come before the host binding, so the
        sentence names no host. Nothing is resolved or dialled first.
        """
        checked = _pin(monkeypatch)
        dials = _Dialler(monkeypatch)
        connector = SftpConnector()

        with pytest.raises(BadRequestError) as caught:
            if entry == "list":
                await connector.list_files(_config(), credential)
            else:
                await connector.fetch_file(_remote_file(), tmp_path, _config(), credential)

        assert checked == []
        assert dials.calls == []
        assert says in caught.value.message
        assert HOST not in caught.value.message
        assert "secret_value" not in caught.value.message
        assert "provider-key" not in caught.value.message

    @pytest.mark.security
    @pytest.mark.parametrize("entry", ["list", "fetch"])
    @pytest.mark.parametrize(
        ("bound", "host", "port", "names"),
        [
            pytest.param("other.example.com", HOST, 22, f"{HOST}:22", id="another-host"),
            pytest.param(f"{HOST}.evil.test", HOST, 22, f"{HOST}:22", id="a-longer-host"),
            pytest.param(HOST, HOST, 2222, f"{HOST}:2222", id="another-port"),
            pytest.param(f"{HOST}:2222", HOST, 22, f"{HOST}:22", id="bound-to-another-port"),
            pytest.param(
                "[2606:4700::1111]",
                "2606:4700::1111",
                2222,
                "2606:4700::1111",
                id="ipv6-on-another-port",
            ),
        ],
    )
    async def test_a_secret_bound_to_another_host_or_port_is_refused_before_anything_is_dialled(
        self,
        entry: str,
        bound: str,
        host: str,
        port: int,
        names: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An editor of a source chooses the host. The SSH login names the one host
        and port it may be sent to. A source on another one is refused before
        anything is resolved or dialled, so the password never leaves the worker.
        The sentence names the login's host and the source's host and port, and
        nothing of the secret."""
        config = _config(host=host, port=port)
        checked = _pin(monkeypatch)
        dials = _Dialler(monkeypatch)
        connector = SftpConnector()

        with pytest.raises(BadRequestError) as caught:
            if entry == "list":
                await connector.list_files(config, _login(bound))
            else:
                await connector.fetch_file(_remote_file(), tmp_path, config, _login(bound))

        assert checked == []
        assert dials.calls == []
        assert bound in caught.value.message
        assert names in caught.value.message
        assert PASSWORD not in caught.value.message


class TestTheConnection:
    @pytest.mark.security
    @pytest.mark.parametrize(
        "mode", ["openssh", "encrypted-openssh", "encrypted-pkcs8", "password"]
    )
    def test_the_connection_options_read_nothing_from_the_worker(
        self, mode: str, worker_home: Path
    ) -> None:
        """Each asyncssh default that reads the worker's own SSH setup is replaced.

        The pin is the only trusted host key, with no X.509 certificate and no
        Kerberos, whose key exchange skips the host key check. The user is the
        vault's. There is no agent, no host-based login, no keyboard-interactive
        login, no proxy, and only the one login method the secret holds. A key the
        vault holds encrypted is opened with its passphrase.
        """
        vault_key = asyncssh.generate_private_key("ssh-ed25519")
        login = _login() if mode == "password" else _key_login(vault_key, mode)

        options = SftpConnector()._options(login, HOST_KEY_LINE)

        pinned = asyncssh.import_public_key(HOST_KEY_LINE)
        assert [list(keys) for keys in options.known_hosts] == [[pinned], [], [], [], [], [], []]
        assert options.x509_trusted_certs is None
        assert options.x509_trusted_cert_paths == []
        assert options.username == "sync"
        assert options.host != "192.0.2.66"
        assert options.host_key_alias != "worker-alias"
        assert options.proxy_command is None
        assert options.tunnel is None
        assert not options.agent_path
        assert options.pkcs11_provider is None
        assert options.host_based_auth is False
        assert options.gss_host is None
        assert options.gss_kex is False
        assert options.gss_auth is False
        assert options.kbdint_auth is False
        assert options.disable_trivial_auth is True
        if mode != "password":
            assert [k.public_data for k in options.client_keys] == [vault_key.public_data]
            assert (options.public_key_auth, options.password_auth) == (True, False)
            assert options.preferred_auth == ["publickey"]
        else:
            assert not options.client_keys
            assert (options.public_key_auth, options.password_auth) == (False, True)
            assert options.preferred_auth == ["password"]

    def test_the_connection_options_bound_the_connect_and_keep_it_alive(self) -> None:
        """asyncssh has no overall connect deadline by default, gives a login two
        minutes, and sends no keepalive. A server that takes the TCP connection and
        then says nothing would hold the sync, so each one is set."""
        options = SftpConnector()._options(_login(), HOST_KEY_LINE)

        assert options.connect_timeout == sftp_module.CONNECT_SECONDS
        assert options.login_timeout == sftp_module.CONNECT_SECONDS
        assert options.keepalive_interval == sftp_module.KEEPALIVE_SECONDS
        assert options.keepalive_count_max == 3

    @pytest.mark.security
    async def test_each_pinned_address_is_dialled_numerically_and_never_the_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard checked these addresses. Dialling the name would resolve it a
        second time, and a name can answer a public address to the guard and a
        private one to the dial. `AI_NUMERICHOST` makes asyncssh refuse to resolve
        anything. `config=None` keeps `~/.ssh/config` out of `connect()` itself, and
        the options are the hardened ones, with the pinned host key and the vault's
        user. `connect()` gets nothing else: asyncssh applies a keyword passed beside
        `options` over them, so `gss_kex=True` there would undo the pin
        (`design-amendments.md`, A17)."""
        ips = ("2606:4700::1111", "93.184.216.34")
        _pin(monkeypatch, PinnedAddress(hostname=HOST, port=2222, ips=ips))
        dials = _Dialler(monkeypatch, ConnectionRefusedError(111, "refused"), _Connection())

        await SftpConnector()._client(
            SftpConfig.model_validate(_config(port=2222)), _login(f"{HOST}:2222")
        )

        assert dials.hosts == list(ips)
        for call in dials.calls:
            assert call["port"] == 2222
            assert call["flags"] == socket.AI_NUMERICHOST
            assert call["config"] is None
            options = call["options"]
            assert isinstance(options, asyncssh.SSHClientConnectionOptions)
            assert [list(keys) for keys in options.known_hosts] == [
                [asyncssh.import_public_key(HOST_KEY_LINE)],
                [],
                [],
                [],
                [],
                [],
                [],
            ]
            assert options.username == "sync"
            beside = {
                name: value
                for name, value in call.items()
                if name not in {"host", "port", "flags", "config", "options"}
            }
            assert beside == {
                "tunnel": (),
                "family": (),
                "local_addr": (),
                "sock": None,
                "kwargs": {},
            }

    async def test_the_sftp_session_takes_names_as_bytes_and_speaks_version_3(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no path encoding, asyncssh gives each name as bytes, so one name that
        is not UTF-8 cannot end the whole listing. Version 3 is what every server
        tested speaks, and its mtime, which the revision is made of, is whole seconds."""
        _pin(monkeypatch)
        connection = _Connection()
        _Dialler(monkeypatch, connection)

        await SftpConnector()._client(SftpConfig.model_validate(_config()), _login())

        assert connection.sftp_options == {"path_encoding": None, "sftp_version": 3}

    async def test_a_connection_lost_during_a_request_is_dropped_and_the_next_file_opens_a_new_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An SFTP session can be lost while its SSH connection stays open. Kept, it
        would answer each later file of the sync with the same loss. So the
        connection is aborted and forgotten, and the next file opens a new one."""
        _pin(monkeypatch)
        lost, fresh = _Connection(), _Connection()
        dials = _Dialler(monkeypatch, lost, fresh)
        connector = SftpConnector()
        parsed = SftpConfig.model_validate(_config())
        await connector._client(parsed, _login())

        async def request() -> None:
            raise asyncssh.SFTPConnectionLost("lost")

        with pytest.raises(ExternalServiceError):
            await connector._call(request())

        assert await connector._client(parsed, _login()) is fresh.sftp
        assert "abort" in lost.order
        assert len(dials.calls) == 2

    async def test_a_refused_sftp_session_does_not_leave_its_connection_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The login succeeded and the server refused the SFTP session. The SSH
        connection is open. It must end by the time the sync is over: a connection
        that the connector forgot without an abort, `aclose` can no longer close."""
        _pin(monkeypatch)
        connection = _Connection(
            refuse_sftp=asyncssh.ChannelOpenError(asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, "no")
        )
        _Dialler(monkeypatch, connection)
        connector = SftpConnector()

        with pytest.raises(BadRequestError):
            await connector._client(SftpConfig.model_validate(_config()), _login())
        await connector.aclose()

        assert connection.is_closed()

    @pytest.mark.parametrize(
        ("answer", "dialled"),
        [
            pytest.param(None, 0, id="address-check-refused"),
            pytest.param(lambda: ConnectionRefusedError(111, "refused"), 1, id="address-refused"),
            pytest.param(TimeoutError, 1, id="no-answer"),
            pytest.param(
                lambda: asyncssh.ConnectionLost("lost"), 1, id="lost-during-the-key-exchange"
            ),
            pytest.param(lambda: asyncssh.PermissionDenied("denied"), 1, id="login-refused"),
            pytest.param(
                lambda: _Connection(
                    refuse_sftp=asyncssh.ChannelOpenError(
                        asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, "no"
                    )
                ),
                1,
                id="session-refused",
            ),
            pytest.param(
                lambda: _Connection(refuse_sftp=asyncssh.SFTPConnectionLost("lost")),
                1,
                id="lost-during-the-session-start",
            ),
        ],
    )
    async def test_a_connect_that_fails_is_not_tried_again_in_the_same_sync(
        self,
        answer: Callable[[], object] | None,
        dialled: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A connect that failed fails again for the next file. A server that does
        not answer would cost each remaining file the connect timeout, and a refused
        password would be sent again. So every failed connect, of any kind, is kept
        for the rest of the sync, and each later file gets the same refusal at once,
        with nothing checked or dialled again (`design-amendments.md`, A19). A
        connection that was open and then lost between files is not a failed connect,
        and is opened again (the test of a connection lost during a request)."""
        if answer is None:
            checked: list[str] = []

            def resolve(url: str, *_args: object, **_kwargs: object) -> PinnedAddress:
                checked.append(url)
                raise SSRFBlockedError("Blocked: 'sftp.example.com' resolves to a private address.")

            monkeypatch.setattr(sftp_module, "resolve_pinned_url", resolve)
            dials = _Dialler(monkeypatch)
        else:
            checked = _pin(monkeypatch)
            dials = _Dialler(monkeypatch, answer())
        connector = SftpConnector()
        parsed = SftpConfig.model_validate(_config())

        with pytest.raises(AppException) as failed:
            await connector._client(parsed, _login())
        with pytest.raises(AppException) as again:
            await connector._client(parsed, _login())

        assert again.value is failed.value
        assert len(checked) == 1
        assert len(dials.calls) == dialled

    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(
                lambda: asyncssh.KeyEncryptionError("Incorrect passphrase"), id="wrong-passphrase"
            ),
            pytest.param(lambda: ValueError("Invalid private key"), id="value-error"),
            pytest.param(
                lambda: UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
                id="unicode-decode-error",
            ),
        ],
    )
    async def test_a_private_key_that_does_not_open_is_not_tried_again_in_the_same_sync(
        self, make: Callable[[], ValueError], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key that does not open with its passphrase will not open for the next
        file either. The refusal is kept for the rest of the sync, so each later file
        gets it at once, with no second attempt to open the key and nothing dialled.

        asyncssh does not wrap every parse failure: `cryptography` raises a plain
        `ValueError`, and a cut paste a `UnicodeDecodeError`. Each one is a key that
        does not open (`design-amendments.md`, A12)."""
        _pin(monkeypatch)
        dials = _Dialler(monkeypatch)
        login = _key_login(asyncssh.generate_private_key("ssh-ed25519"), "encrypted-openssh")
        tried: list[object] = []

        def import_private_key(*args: object, **_kwargs: object) -> asyncssh.SSHKey:
            tried.append(args)
            raise make()

        monkeypatch.setattr(asyncssh, "import_private_key", import_private_key)
        connector = SftpConnector()
        parsed = SftpConfig.model_validate(_config())

        with pytest.raises(BadRequestError) as first:
            await connector._client(parsed, login)
        with pytest.raises(BadRequestError) as again:
            await connector._client(parsed, login)

        assert again.value is first.value
        assert len(tried) == 1
        assert dials.calls == []

    @pytest.mark.security
    @pytest.mark.parametrize("mode", ["password", "key"])
    @pytest.mark.parametrize("where", ["dial", "session", "request"])
    async def test_no_log_record_holds_a_secret_of_the_login(
        self,
        mode: str,
        where: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The connector logs each failed address and each refusal, and asyncssh logs
        its own steps. No record at any level holds the password, the passphrase or
        a part of the private key, as stored or opened, so the worker log is not a
        second vault. The attributes a call adds with `extra=` count too: a
        structured handler sends them.

        Each case first meets an address that refuses the connection, then a
        refused login, a refused SFTP session, or a session lost during a request.
        """
        caplog.set_level(logging.DEBUG)
        key, other = (asyncssh.generate_private_key("ssh-ed25519") for _ in range(2))
        if mode == "key":
            login = _key_login(key, "encrypted-pkcs8")
            stored = login.private_key.get_secret_value() if login.private_key else ""
            opened = [key.export_private_key(form).decode() for form in ("openssh", "pkcs8-pem")]
            secrets = [stored, PASSPHRASE, *opened]
        else:
            login = _login()
            secrets = [PASSWORD]
        not_secret = (
            key.export_public_key().decode(),
            other.export_private_key("openssh").decode(),
            other.export_private_key("pkcs8-pem").decode(),
            _key_login(other, "encrypted-pkcs8").private_key.get_secret_value(),
        )
        _pin(
            monkeypatch,
            PinnedAddress(hostname=HOST, port=22, ips=("93.184.216.34", "93.184.216.35")),
        )
        answers: dict[str, object] = {
            "dial": asyncssh.PermissionDenied(SERVER_TEXT),
            "session": _Connection(
                refuse_sftp=asyncssh.ChannelOpenError(
                    asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, SERVER_TEXT
                )
            ),
            "request": _Connection(),
        }
        _Dialler(monkeypatch, ConnectionRefusedError(111, "refused"), answers[where])
        connector = SftpConnector()
        parsed = SftpConfig.model_validate(_config())

        async def request() -> None:
            raise asyncssh.SFTPConnectionLost(SERVER_TEXT)

        if where == "request":
            await connector._client(parsed, login)
            with pytest.raises(ExternalServiceError):
                await connector._call(request())
        else:
            with pytest.raises(BadRequestError):
                await connector._client(parsed, login)
        await connector.aclose()

        if where != "request":
            assert any(r.levelno >= logging.WARNING for r in caplog.records)
        shown = "\n".join([caplog.text, *(repr(vars(record)) for record in caplog.records)])
        for secret in secrets:
            assert not _echoes(shown, secret, not_secret=not_secret)

    @pytest.mark.parametrize(
        ("answers", "dialled", "refused"),
        [
            pytest.param(
                lambda conn: [ConnectionRefusedError(111, "refused"), TimeoutError(), conn],
                3,
                None,
                id="unreachable-then-answers",
            ),
            pytest.param(
                lambda conn: [asyncssh.PermissionDenied("denied"), conn, conn],
                1,
                BadRequestError,
                id="login-refused",
            ),
            pytest.param(
                lambda conn: [asyncssh.HostKeyNotVerifiable("not trusted"), conn, conn],
                1,
                BadRequestError,
                id="host-key-refused",
            ),
        ],
    )
    async def test_a_refused_address_moves_on_to_the_next_and_a_login_refusal_does_not(
        self,
        answers: Callable[[_Connection], list[object]],
        dialled: int,
        refused: type[AppException] | None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An address that refuses the TCP connection, or does not answer, says
        nothing about the server, so the next pinned address is tried. A refused
        login or host key is the server's answer, and each address would give it
        again. The first one stops the connect, so a wrong password is sent once."""
        ips = ("93.184.216.34", "93.184.216.35", "93.184.216.36")
        _pin(monkeypatch, PinnedAddress(hostname=HOST, port=22, ips=ips))
        connection = _Connection()
        dials = _Dialler(monkeypatch, *answers(connection))
        connector = SftpConnector()
        parsed = SftpConfig.model_validate(_config())

        if refused is None:
            assert await connector._client(parsed, _login()) is connection.sftp
        else:
            with pytest.raises(refused):
                await connector._client(parsed, _login())

        assert dials.hosts == list(ips[:dialled])

    @pytest.mark.parametrize(
        ("answers", "says"),
        [
            pytest.param(
                lambda: [TimeoutError(), ConnectionRefusedError(111, "refused")],
                "could not be reached",
                id="timeout-then-refused",
            ),
            pytest.param(
                lambda: [ConnectionRefusedError(111, "refused"), TimeoutError()],
                "did not answer",
                id="refused-then-timeout",
            ),
        ],
    )
    async def test_when_every_address_fails_the_last_one_decides_the_sentence(
        self,
        answers: Callable[[], list[BaseException]],
        says: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A name can resolve to an IPv6 address that does not answer and an IPv4
        address that refuses at once. Each address is tried, and the sentence is the
        one for the last address tried (`design-amendments.md`, A17)."""
        ips = ("2606:4700::1111", "93.184.216.34")
        _pin(monkeypatch, PinnedAddress(hostname=HOST, port=22, ips=ips))
        dials = _Dialler(monkeypatch, *answers())

        with pytest.raises(ExternalServiceError) as caught:
            await SftpConnector()._client(SftpConfig.model_validate(_config()), _login())

        assert says in caught.value.message
        assert dials.hosts == list(ips)

    @pytest.mark.security
    @pytest.mark.parametrize(
        ("where", "make", "refusal_type", "says"),
        [
            pytest.param(
                "mapped",
                lambda: asyncssh.HostKeyNotVerifiable(SERVER_TEXT),
                BadRequestError,
                "did not prove",
                id="host-key-not-verifiable",
            ),
            pytest.param(
                "mapped",
                lambda: asyncssh.KeyExchangeFailed(SERVER_TEXT),
                BadRequestError,
                "could not agree",
                id="key-exchange-failed",
            ),
            pytest.param(
                "mapped",
                lambda: asyncssh.PermissionDenied(SERVER_TEXT),
                BadRequestError,
                "login",
                id="permission-denied",
            ),
            pytest.param(
                "mapped",
                lambda: asyncssh.ConnectionLost(SERVER_TEXT),
                ExternalServiceError,
                "was lost",
                id="connection-lost",
            ),
            pytest.param(
                "mapped",
                lambda: asyncssh.ChannelOpenError(
                    asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, SERVER_TEXT
                ),
                BadRequestError,
                "no sftp service",
                id="channel-open-error",
            ),
            pytest.param(
                "mapped",
                lambda: asyncssh.KeyImportError(SERVER_TEXT),
                BadRequestError,
                "private key",
                id="key-import-error",
            ),
            pytest.param(
                "mapped",
                lambda: asyncssh.KeyEncryptionError(SERVER_TEXT),
                BadRequestError,
                "private key",
                id="key-encryption-error",
            ),
            pytest.param(
                "mapped",
                lambda: asyncssh.ProtocolError(SERVER_TEXT),
                ExternalServiceError,
                "closed the connection",
                id="protocol-error",
            ),
            pytest.param(
                "mapped",
                lambda: asyncssh.DisconnectError(asyncssh.DISC_BY_APPLICATION, SERVER_TEXT),
                ExternalServiceError,
                "closed the connection",
                id="disconnect",
            ),
            pytest.param(
                "dial",
                lambda: ConnectionRefusedError(111, SERVER_TEXT),
                ExternalServiceError,
                "could not be reached",
                id="dial-refused",
            ),
            pytest.param(
                "dial",
                lambda: TimeoutError(SERVER_TEXT),
                ExternalServiceError,
                "did not answer",
                id="dial-timeout",
            ),
            pytest.param(
                "dial",
                lambda: asyncssh.PermissionDenied(SERVER_TEXT),
                BadRequestError,
                "login",
                id="dial-login-refused",
            ),
            pytest.param(
                "dial",
                lambda: asyncssh.HostKeyNotVerifiable(SERVER_TEXT),
                BadRequestError,
                "did not prove",
                id="dial-host-key-refused",
            ),
            pytest.param(
                "session",
                lambda: asyncssh.ChannelOpenError(
                    asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, SERVER_TEXT
                ),
                BadRequestError,
                "no sftp service",
                id="session-refused",
            ),
            pytest.param(
                "request",
                lambda: asyncssh.SFTPConnectionLost(SERVER_TEXT),
                ExternalServiceError,
                "was lost",
                id="request-sftp-connection-lost",
            ),
            pytest.param(
                "request",
                lambda: asyncssh.ConnectionLost(SERVER_TEXT),
                ExternalServiceError,
                "was lost",
                id="request-connection-lost",
            ),
            pytest.param(
                "request",
                lambda: asyncssh.DisconnectError(asyncssh.DISC_CONNECTION_LOST, SERVER_TEXT),
                ExternalServiceError,
                "was lost",
                id="request-disconnect",
            ),
            pytest.param(
                "key",
                lambda: asyncssh.KeyImportError(SERVER_TEXT),
                BadRequestError,
                "private key",
                id="key-import-error-at-connect",
            ),
            pytest.param(
                "key",
                lambda: asyncssh.KeyEncryptionError(SERVER_TEXT),
                BadRequestError,
                "private key",
                id="key-encryption-error-at-connect",
            ),
            pytest.param(
                "key",
                lambda: ValueError(SERVER_TEXT),
                BadRequestError,
                "private key",
                id="value-error-at-connect",
            ),
        ],
    )
    async def test_no_refusal_carries_the_servers_own_text(
        self,
        where: str,
        make: Callable[[], BaseException],
        refusal_type: type[AppException],
        says: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A server writes the reason of its disconnect and its SFTP status text.
        The sync log and the API show our sentence only, of the class the design
        maps each failure to. The server's text goes to the worker log alone.

        Each sentence but the private key's names the host, so the log says which
        source failed. `_refused` writes the server's text to a warning record. The
        text of a key error is the library's, and can hold bytes of the key, so it
        reaches no record, and the refusal is raised `from None`
        (`design-amendments.md`, A12).

        `mapped` asks `_refused` directly. `dial` raises from `asyncssh.connect`,
        `session` from `start_sftp_client`, `request` from inside `_call` on a
        connection that `_client` opened, and `key` from `import_private_key` when
        `_client` opens the vault's key. asyncssh's key errors are `ValueError`s,
        not `asyncssh.Error`s, so `_connect` names them.
        """
        caplog.set_level(logging.DEBUG)
        exc = make()

        refusal = await self._refusal(where, exc, monkeypatch)

        assert isinstance(refusal, refusal_type)
        assert says in refusal.message.lower()
        assert SERVER_TEXT not in refusal.message
        assert SERVER_TEXT not in repr(refusal.details)
        records = "\n".join([caplog.text, *(repr(vars(record)) for record in caplog.records)])
        if says == "private key":
            assert SERVER_TEXT not in records
        else:
            assert HOST in refusal.message
        if where == "key":
            assert refusal.__cause__ is None
            assert refusal.__suppress_context__
        if where == "mapped" and says != "private key":
            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert any(SERVER_TEXT in caplog.handler.format(r) for r in warnings)

    @staticmethod
    async def _refusal(
        where: str, exc: BaseException, monkeypatch: pytest.MonkeyPatch
    ) -> AppException:
        connector = SftpConnector()
        parsed = SftpConfig.model_validate(_config())
        login = _login()
        if where == "mapped":
            return connector._refused(exc, parsed, login)
        _pin(monkeypatch)
        if where == "key":
            # Made before the patch: the vault checks the key as it is stored.
            login = _key_login(asyncssh.generate_private_key("ssh-ed25519"), "encrypted-openssh")

            def import_private_key(*_args: object, **_kwargs: object) -> asyncssh.SSHKey:
                raise exc

            monkeypatch.setattr(asyncssh, "import_private_key", import_private_key)
            _Dialler(monkeypatch)
            with pytest.raises(AppException) as caught:
                await connector._client(parsed, login)
            return caught.value
        if where == "request":
            _Dialler(monkeypatch, _Connection())
            await connector._client(parsed, login)

            async def request() -> None:
                raise exc

            with pytest.raises(AppException) as caught:
                await connector._call(request())
            return caught.value
        _Dialler(monkeypatch, _Connection(refuse_sftp=exc) if where == "session" else exc)
        with pytest.raises(AppException) as caught:
            await connector._client(parsed, login)
        return caught.value


class TestTheChangeSignal:
    async def test_an_sftp_source_has_no_version_so_every_run_lists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SFTP has no value that moves when any file below the root changes. A
        stored version that matched would make `new_only` and `update_only` stop
        before the listing, and miss every new, changed and deleted file. So the
        connector has none, and asks the server nothing for it."""
        checked = _pin(monkeypatch)
        dials = _Dialler(monkeypatch)

        assert await SftpConnector().remote_version(_config(), _login(), "previous") is None
        assert checked == []
        assert dials.calls == []


class TestTheBounds:
    async def test_a_request_past_its_deadline_aborts_the_connection_before_it_waits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancelled asyncssh request cleans up by asking the server, and a server
        that stalled answers nothing. So the connection is aborted first: that
        fails every pending request at once. Only then is the cancelled request
        awaited. In the other order the sync would wait for ever.

        The fake request does what asyncssh does: when it is cancelled, it waits
        for an answer that only the abort brings.
        """
        monkeypatch.setattr(sftp_module, "REQUEST_SECONDS", 0.05)
        connection = _Connection()
        connector = SftpConnector()
        connector._conn = connection
        connector._sftp = connection.sftp

        async def stalled() -> bytes:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                connection.order.append("cancelled")
                await connection.aborted.wait()
                connection.order.append("cleaned up")
                raise
            return b""

        with anyio.fail_after(5), pytest.raises(ExternalServiceError, match="did not answer"):
            await connector._call(stalled())

        assert connection.order == ["abort", "cancelled", "cleaned up"]

    async def test_aclose_is_safe_twice_and_before_a_connection(self) -> None:
        """A sync calls `aclose` when it is over, whether a connection was opened or
        not. A second call is harmless."""
        await SftpConnector().aclose()

        connection = _Connection()
        connector = SftpConnector()
        connector._conn = connection
        connector._sftp = connection.sftp
        await connector.aclose()
        await connector.aclose()

        assert connection.order[:2] == ["close", "wait_closed"]
        assert "abort" not in connection.order

    async def test_a_connection_that_does_not_close_in_time_is_aborted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`wait_closed` waits for the server's side of the close. A server that does
        not answer would hold the end of the sync, so the wait is bounded and the
        connection is aborted when it expires."""
        monkeypatch.setattr(sftp_module, "REQUEST_SECONDS", 0.05)
        connection = _Connection(stall_close=True)
        connector = SftpConnector()
        connector._conn = connection
        connector._sftp = connection.sftp

        with anyio.fail_after(5):
            await connector.aclose()

        assert connection.order[:2] == ["close", "abort"]


class TestTheStat:
    @pytest.mark.parametrize(
        ("size", "mtime", "revision"),
        [
            pytest.param(10, MTIME, f"10:{MTIME}", id="both"),
            pytest.param(0, 0, "0:0", id="zero-is-a-value"),
            pytest.param(None, MTIME, None, id="no-size"),
            pytest.param(10, None, None, id="no-mtime"),
        ],
    )
    def test_the_stat_revision_needs_both_size_and_mtime(
        self, size: int | None, mtime: int | None, revision: str | None
    ) -> None:
        """The revision is the size and the mtime in whole seconds, read from the
        attributes' own fields. Without one of them two versions of a file could
        share a revision, so there is none: the file is read and hashed each run.
        Zero is a value, not a gap."""
        attrs = asyncssh.SFTPAttrs(
            type=asyncssh.FILEXFER_TYPE_REGULAR, size=size, atime=5, mtime=mtime
        )

        read = sftp_module._Stat.of(attrs)

        assert read == sftp_module._Stat(
            type=asyncssh.FILEXFER_TYPE_REGULAR, size=size, mtime=mtime
        )
        assert read.revision == revision

    def test_the_file_type_constants_are_asyncssh_s(self) -> None:
        """The module keeps its own copies so that importing it does not import
        asyncssh. They must be asyncssh's values."""
        assert (
            sftp_module._REGULAR,
            sftp_module._DIRECTORY,
            sftp_module._SYMLINK,
            sftp_module._UNKNOWN,
        ) == (
            asyncssh.FILEXFER_TYPE_REGULAR,
            asyncssh.FILEXFER_TYPE_DIRECTORY,
            asyncssh.FILEXFER_TYPE_SYMLINK,
            asyncssh.FILEXFER_TYPE_UNKNOWN,
        )


_LISTED_SPELLINGS = [
    pytest.param("files.corp.example", " Files.Corp.Example ", "10.0.0.5", id="name"),
    pytest.param("bücher.corp", "Bücher.Corp", "10.0.0.5", id="idna-name"),
    pytest.param("fd00::5", "[FD00:0::5]", "fd00::5", id="ipv6-literal"),
    pytest.param("10.0.0.5", " 10.0.0.5 ", "10.0.0.5", id="ipv4-literal"),
]
"""A private source host, the same host as an operator may list it, and the address dialled.

In these tests a name resolves to 10.0.0.5. An address literal is not resolved.
"""


class TestTheAllowlist:
    """`SYNC_ALLOWED_HOSTS`: the servers on the company network a source may name.

    The real guard runs here. Only `getaddrinfo` is replaced.
    """

    def test_a_listed_host_is_spelled_as_a_source_host_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard compares the URL's host with the list exactly. So an entry is
        spelled as a source's host field spells it: lower case, no brackets, the
        compressed IPv6 form, IDNA. A blank entry is no host. An entry with a port,
        a trailing dot or a scheme matches no source host, so it is left out."""
        monkeypatch.setattr(
            settings,
            "SYNC_ALLOWED_HOSTS",
            [
                " Files.Corp.Example ",
                "[2001:DB8::7]",
                "2001:db8:0:0:0:0:0:8",
                "Bücher.example",
                "",
                "   ",
                "port.example:2222",
                "dot.example.",
                "sftp://url.example",
            ],
        )

        listed = sftp_module._listed_hosts()

        assert listed == frozenset(
            {"files.corp.example", "2001:db8::7", "2001:db8::8", "xn--bcher-kva.example"}
        )
        for host in ("FILES.corp.example", "[2001:db8::7]", "[2001:0db8::0008]", "bücher.example"):
            parsed = SftpConfig.model_validate(_config(host=host))
            assert urlsplit(parsed.url()).hostname in listed

    @pytest.mark.security
    @pytest.mark.parametrize(("host", "listed", "dialled"), _LISTED_SPELLINGS)
    async def test_a_private_host_is_refused_unlisted_and_accepted_listed_by_validate_config(
        self, host: str, listed: str, dialled: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server on a private address is refused, unless the operator listed it.
        A name and an address literal are treated the same. The operator may spell
        the entry in another way than the source does: the check reads the list as
        `_listed_hosts` spells it, never the setting as it was typed."""
        _resolve_to(monkeypatch, "10.0.0.5")
        monkeypatch.setattr(settings, "SYNC_ALLOWED_HOSTS", [])

        refusal = await SftpConnector().validate_config(_config(host=host))

        assert refusal is not None
        assert refusal.field == "host"

        monkeypatch.setattr(settings, "SYNC_ALLOWED_HOSTS", [listed])

        assert await SftpConnector().validate_config(_config(host=host)) is None

    @pytest.mark.security
    @pytest.mark.parametrize(("host", "listed", "dialled"), _LISTED_SPELLINGS)
    async def test_a_private_host_is_dialled_only_when_listed(
        self, host: str, listed: str, dialled: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sync repeats the check before it dials. An unlisted private server is
        never dialled. A listed one, however the operator spelled the entry, is
        dialled at the address that was checked."""
        _resolve_to(monkeypatch, "10.0.0.5")
        parsed = SftpConfig.model_validate(_config(host=host))
        login = _login(f"[{parsed.host}]" if ":" in parsed.host else parsed.host)

        monkeypatch.setattr(settings, "SYNC_ALLOWED_HOSTS", [])
        unlisted = _Dialler(monkeypatch)
        with pytest.raises(BadRequestError):
            await SftpConnector()._client(parsed, login)
        assert unlisted.calls == []

        monkeypatch.setattr(settings, "SYNC_ALLOWED_HOSTS", [listed])
        dials = _Dialler(monkeypatch, _Connection())
        await SftpConnector()._client(parsed, login)
        assert dials.hosts == [dialled]
        assert dials.calls[0]["flags"] == socket.AI_NUMERICHOST

    @pytest.mark.security
    @pytest.mark.parametrize(
        "address", ["127.0.0.1", "169.254.169.254", "::ffff:127.0.0.1", "100.100.100.200"]
    )
    async def test_a_listed_host_that_resolves_to_loopback_link_local_or_metadata_is_still_refused(
        self, address: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A listed name is still resolved and checked. It may answer a private
        address, but never the worker itself, a link-local address or a cloud
        metadata service. A DNS record cannot open what the list may not name. The
        entry is spelled as an operator may type it."""
        monkeypatch.setattr(settings, "SYNC_ALLOWED_HOSTS", [" FILES.corp.Example "])
        _resolve_to(monkeypatch, address)
        dials = _Dialler(monkeypatch)
        config = _config(host="files.corp.example")

        refusal = await SftpConnector().validate_config(config)

        assert refusal is not None
        assert refusal.field == "host"
        with pytest.raises(BadRequestError):
            await SftpConnector()._client(
                SftpConfig.model_validate(config), _login("files.corp.example")
            )
        assert dials.calls == []

    @pytest.mark.security
    async def test_a_host_on_the_mcp_allowlist_is_not_a_sync_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who listed a host for MCP servers did not list it for SFTP
        logins. The connector reads its own setting only."""
        monkeypatch.setattr(settings, "MCP_ALLOWED_HOSTS", ["files.corp.example"])
        monkeypatch.setattr(settings, "SYNC_ALLOWED_HOSTS", [])
        _resolve_to(monkeypatch, "10.0.0.5")
        dials = _Dialler(monkeypatch)
        config = _config(host="files.corp.example")

        refusal = await SftpConnector().validate_config(config)

        assert sftp_module._listed_hosts() == frozenset()
        assert refusal is not None
        assert refusal.field == "host"
        with pytest.raises(BadRequestError):
            await SftpConnector()._client(
                SftpConfig.model_validate(config), _login("files.corp.example")
            )
        assert dials.calls == []
