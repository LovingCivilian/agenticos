"""Tests for the general secret store and the shapes a secret can take.

The store exists for one concrete need - a custom capability calling an API the
platform knows nothing about - and it is only defensible because of the
constraint: **a secret is referenced, never handed around.** Most of what is
worth testing here is that constraint holding.
"""

import asyncio
import base64
import contextlib
import functools
import json
import math
import os
import sys
import threading
import time
import traceback
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
from asyncssh.asn1 import der_decode, der_encode
from asyncssh.packet import Byte, MPInt, SSHPacket, String, UInt32
from pydantic import ValidationError

from app.core import secret_kinds
from app.core.exceptions import (
    AlreadyExistsError,
    BadRequestError,
    ExternalServiceError,
    InternalError,
    NotFoundError,
)
from app.core.permissions import AuthContext, OrgRoleName
from app.core.secret_kinds import (
    KEY_PARSE_ERRORS,
    MAX_KEY_ITERATIONS,
    MAX_KEY_ROUNDS,
    MAX_PASSWORD_CHARACTERS,
    MAX_PBKDF2_KEY_BYTES,
    MAX_PRIVATE_KEY_CHARACTERS,
    MIN_CREDENTIAL_LENGTH,
    STORABLE_KINDS,
    ApiKeySecret,
    AwsCredentialsSecret,
    AzureOpenAISecret,
    GcpServiceAccountSecret,
    GithubOAuthAppSecret,
    GoogleOAuthAppSecret,
    NoSecret,
    SecretKind,
    SecretRequirement,
    describe_kinds,
    seal_secret,
    unseal_secret,
)
from app.core.vault import VaultScope, seal
from app.repositories import member_repo
from app.schemas.secret import SecretCreate, SecretUpdate
from app.services import organization_secret
from app.services.organization_secret import OrganizationSecretService
from app.services.ssh_key_check import KeyReport, inspect
from tests.test_model_profiles import service_account_json

pytestmark = pytest.mark.security


def _ctx(org_id=None) -> AuthContext:
    return AuthContext(
        user_id=uuid.uuid4(),
        organization_id=org_id or uuid.uuid4(),
        role=OrgRoleName.OWNER,
    )


def _db():
    db = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    return db


def _row(ctx, value, *, name="Weather API"):
    sealed = seal_secret(value, scope=VaultScope.organization(ctx.organization_id))
    row = MagicMock()
    row.id = uuid.uuid4()
    row.organization_id = ctx.organization_id
    row.name = name
    row.kind = value.kind.value
    row.sealed_secret = sealed.ciphertext
    row.hint = sealed.hint
    row.key_version = sealed.key_version
    return row


class TestSecretKinds:
    def test_none_is_not_a_shape_anyone_can_store(self):
        """The runtime holds "no credential"; nobody saves it as a secret."""
        assert SecretKind.NONE not in STORABLE_KINDS
        assert set(SecretKind) - {SecretKind.NONE} == STORABLE_KINDS

    def test_every_storable_kind_ships_a_form_schema(self):
        """The frontend generates its forms from these, so a missing one is a dead kind."""
        described = {info.kind for info in describe_kinds()}
        assert described == STORABLE_KINDS
        assert all(info.json_schema.get("properties") for info in describe_kinds())

    def test_a_capability_cannot_require_the_absence_of_a_secret(self):
        with pytest.raises(ValidationError):
            SecretRequirement(kind=SecretKind.NONE, description="nothing")

    def test_a_secret_masks_itself_in_a_repr(self):
        """The way a plaintext usually escapes is a model reaching a log line."""
        value = AwsCredentialsSecret(
            aws_access_key_id="AKIAEXAMPLE",
            aws_secret_access_key="do-not-print-me",
            region_name="eu-central-1",
        )
        assert "do-not-print-me" not in repr(value)
        # And `model_dump()` keeps the SecretStr rather than unwrapping it, so
        # dumping a model into a structured log stays harmless.
        assert "do-not-print-me" not in str(value.model_dump())

    def test_a_secret_serialises_to_its_real_value_for_the_vault(self):
        """The one place the plaintext has to appear is on the way into an envelope."""
        value = ApiKeySecret(api_key="sk-live-1234")
        assert "sk-live-1234" in value.model_dump_json()

    def test_a_github_oauth_app_needs_both_halves(self):
        """The id says which app, the secret authenticates it; neither may be blank."""
        with pytest.raises(ValidationError):
            GithubOAuthAppSecret(client_id="", client_secret="ghs-abcd")
        with pytest.raises(ValidationError):
            GithubOAuthAppSecret(client_id="Iv1.0123abcd", client_secret="")

    def test_a_github_oauth_app_secret_masks_itself_in_a_repr(self):
        """Only the client_secret is confidential; the client_id is public."""
        value = GithubOAuthAppSecret(
            client_id="Iv1.0123456789abcdef", client_secret="ghs-do-not-print-me"
        )
        assert "ghs-do-not-print-me" not in repr(value)
        assert "ghs-do-not-print-me" not in str(value.model_dump())
        # The id is not a secret, so it is fine for it to appear.
        assert "Iv1.0123456789abcdef" in repr(value)

    def test_a_github_oauth_app_is_hinted_by_its_public_client_id(self):
        """The client id names the app in GitHub's settings and is not confidential."""
        value = GithubOAuthAppSecret(
            client_id="Iv1.0123456789wxyz", client_secret="ghs-never-shown"
        )
        assert value.hint == "wxyz"

    def test_an_unknown_field_is_refused_rather_than_ignored(self):
        with pytest.raises(ValidationError):
            ApiKeySecret(api_key="sk-12345678", region_name="eu-west-1")

    @pytest.mark.parametrize(
        ("document", "message"),
        [
            ("not json at all", "downloaded JSON"),
            (json.dumps({"type": "authorized_user"}), "not a service account key"),
            (
                json.dumps({"type": "service_account", "project_id": "p"}),
                "missing: client_email, private_key",
            ),
        ],
    )
    def test_a_service_account_is_validated_while_the_form_is_open(self, document, message):
        """Otherwise the paste fails hours later as an authentication error."""
        with pytest.raises(ValidationError, match=message):
            GcpServiceAccountSecret(service_account_json=document)

    def test_a_service_account_reads_its_project_from_the_document(self):
        """Two places to state the project is one place for them to disagree."""
        value = GcpServiceAccountSecret(service_account_json=service_account_json("acme-prod"))
        assert value.project == "acme-prod"

    def test_a_service_account_is_hinted_by_the_account_it_authenticates_as(self):
        """The client email, not the private key - it is what names the account
        in the Google console, and it is not confidential."""
        value = GcpServiceAccountSecret(service_account_json=service_account_json())
        assert value.hint == ".com"

    def test_the_hint_identifies_a_credential_without_exposing_it(self):
        assert ApiKeySecret(api_key="sk-live-abcd9999").hint == "9999"
        assert (
            AwsCredentialsSecret(
                aws_access_key_id="AKIAEXAMPLE7777",
                aws_secret_access_key="never-shown",
                region_name="us-east-1",
            ).hint
            == "7777"
        )
        assert NoSecret().hint == ""

    @pytest.mark.parametrize(
        "build",
        [
            lambda: ApiKeySecret(api_key="1234567"),
            lambda: AzureOpenAISecret(
                api_key="1234567",
                azure_endpoint="https://demo.openai.azure.com",
                api_version="2024-10-21",
            ),
            lambda: AwsCredentialsSecret(
                aws_access_key_id="AKIAEXAMPLE7777",
                aws_secret_access_key="1234567",
                region_name="us-east-1",
            ),
            lambda: AwsCredentialsSecret(
                aws_access_key_id="AKIAEXAMPLE7777",
                aws_secret_access_key="long-enough-secret",
                region_name="us-east-1",
                aws_session_token="1234567",
            ),
            lambda: GithubOAuthAppSecret(client_id="Iv1.0123456789abcdef", client_secret="1234567"),
            lambda: GoogleOAuthAppSecret(
                client_id="1234-abc.apps.googleusercontent.com", client_secret="1234567"
            ),
        ],
        ids=["api_key", "azure", "aws_secret", "aws_session_token", "github", "google"],
    )
    def test_a_secret_too_short_to_hint_safely_is_refused(self, build):
        """`ApiKeySecret(api_key="1234").hint` used to be `"1234"` - the whole key,
        shown to everyone with `secrets:view` and written into the audit entry
        under a field described as "never the secret itself"."""
        with pytest.raises(ValidationError, match=f"at least {MIN_CREDENTIAL_LENGTH} characters"):
            build()

    def test_the_floor_is_the_shortest_value_a_hint_cannot_give_away(self):
        """Eight characters is accepted, and the form is told the floor up front."""
        assert ApiKeySecret(api_key="12345678").hint == "5678"
        properties = ApiKeySecret.model_json_schema()["properties"]
        assert properties["api_key"]["minLength"] == MIN_CREDENTIAL_LENGTH

    def test_the_hint_is_taken_from_the_payload_not_from_the_envelope(self):
        """Sealing a JSON document makes the vault's own hint punctuation."""
        sealed = seal_secret(
            ApiKeySecret(api_key="sk-live-abcd5555"),
            scope=VaultScope.organization(uuid.uuid4()),
        )
        assert sealed.hint == "5555"


class TestSealAndOpen:
    def test_a_structured_secret_survives_the_envelope_whole(self):
        scope = VaultScope.organization(uuid.uuid4())
        value = AzureOpenAISecret(
            api_key="azure-1234",
            azure_endpoint="https://demo.openai.azure.com",
            api_version="2024-10-21",
        )
        sealed = seal_secret(value, scope=scope)

        opened = unseal_secret(sealed.ciphertext, kind=SecretKind.AZURE_OPENAI, scope=scope)

        assert opened == value

    def test_a_github_oauth_app_survives_the_envelope_whole(self):
        scope = VaultScope.organization(uuid.uuid4())
        value = GithubOAuthAppSecret(
            client_id="Iv1.0123456789abcdef", client_secret="ghs-live-4242"
        )
        sealed = seal_secret(value, scope=scope)

        opened = unseal_secret(sealed.ciphertext, kind=SecretKind.GITHUB_OAUTH_APP, scope=scope)

        assert isinstance(opened, GithubOAuthAppSecret)
        assert opened.client_id == "Iv1.0123456789abcdef"
        assert opened.client_secret.get_secret_value() == "ghs-live-4242"
        # The identifying id becomes the hint, not the punctuation of the JSON.
        assert sealed.hint == "cdef"

    def test_an_envelope_whose_kind_disagrees_with_the_row_is_refused(self):
        """A swapped column would otherwise hand the wrong shape to a caller."""
        scope = VaultScope.organization(uuid.uuid4())
        sealed = seal_secret(ApiKeySecret(api_key="sk-live-1234"), scope=scope)

        with pytest.raises(BadRequestError) as refused:
            unseal_secret(sealed.ciphertext, kind=SecretKind.AWS_CREDENTIALS, scope=scope)

        assert refused.value.details == {"recorded": "aws_credentials", "sealed": "api_key"}

    def test_a_payload_that_does_not_validate_is_refused_without_naming_the_value(self):
        """A stored payload that no longer validates - a row edited underneath the
        schema, a rollback to a build whose `SecretKind` lacks a kind a newer one
        wrote - is refused without the decrypted credential reaching the message or
        a chained traceback. `validate_json`'s own `ValidationError` embeds the
        offending input, which here is the plaintext (#21)."""
        scope = VaultScope.organization(uuid.uuid4())
        sealed = seal(json.dumps({"kind": "bogus", "api_key": "sk-live-SUPERSECRET"}), scope=scope)

        with pytest.raises(BadRequestError) as refused:
            unseal_secret(sealed.ciphertext, kind=SecretKind.API_KEY, scope=scope)

        assert refused.value.details == {"recorded": "api_key"}
        assert "SUPERSECRET" not in str(refused.value)
        # The leak surface is the *rendered* traceback - what `logger.exception`
        # and an OTel span emit. `__cause__` is `None` with or without `from None`
        # (implicit chaining never sets it), so it does not pin the fix; the
        # property that keeps the chained `ValidationError` - which still holds the
        # plaintext in `__context__` - out of the formatting is `__suppress_context__`.
        assert refused.value.__suppress_context__ is True
        rendered = "".join(
            traceback.format_exception(
                type(refused.value), refused.value, refused.value.__traceback__
            )
        )
        assert "SUPERSECRET" not in rendered


PASSPHRASE = "Tr0ub4dor&3-zebra-quokka"
WRONG_PASSPHRASE = "Wr0ng-otter-9-velvet"
PASSWORD = "hunter2-but-longer-7"


@functools.cache
def _ssh_key(algorithm: str) -> asyncssh.SSHKey:
    """One key per algorithm for the session, made in memory. No key file is committed."""
    if algorithm == "ssh-rsa":
        return asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    return asyncssh.generate_private_key(algorithm)


@functools.cache
def _key_text(form: str, sealed_with: str = PASSPHRASE) -> str:
    """A private key as a person pastes it, in one of the formats asyncssh reads.

    PKCS#1 is RSA only, so that form uses an RSA key. An encrypted form is
    sealed with `sealed_with`. An encrypted OpenSSH key uses the bcrypt KDF. 16
    rounds is what `ssh-keygen` uses, and it keeps a test fast.
    """
    passphrase = sealed_with if form.endswith("-encrypted") else None
    match form.removesuffix("-encrypted"):
        case "openssh":
            data = _ssh_key("ssh-ed25519").export_private_key(
                "openssh", passphrase, rounds=16, ignore_few_rounds=True
            )
        case "pkcs8":
            data = _ssh_key("ssh-ed25519").export_private_key("pkcs8-pem", passphrase)
        case _:
            data = _ssh_key("ssh-rsa").export_private_key("pkcs1-pem", passphrase)
    return data.decode()


def _ssh_login(**fields: object):
    """An SSH login for sftp.example.com, with the fields a test gives."""
    values = {"username": "partner-sync", "host": "sftp.example.com", **fields}
    return secret_kinds.SshCredentialsSecret(**values)


def _echoes(shown: str, secret: str, width: int = 12) -> bool:
    """Whether `shown` holds any run of `width` characters of `secret`.

    A part is enough: a truncated repr shows the start and the end of a value.
    A key's PEM armour lines are not secret, so only its body counts.
    """
    body = "".join(line for line in secret.splitlines() if not line.startswith("-----"))
    return any(body[i : i + width] in shown for i in range(len(body) - width + 1))


def _what_a_refusal_shows(refused: pytest.ExceptionInfo[ValidationError]) -> str:
    """Every place a refusal's text can go: the form, a log line and a traceback."""
    exc = refused.value
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    messages = [error["msg"] for error in exc.errors()]
    return "\n".join([str(exc), repr(exc), rendered, *messages])


def _one_character_changed(text: str, line: int) -> str:
    """The paste with one character changed in the middle of one of its lines."""
    lines = text.splitlines(keepends=True)
    content = lines[line].rstrip("\n")
    middle = len(content) // 2
    replacement = "B" if content[middle] == "A" else "A"
    lines[line] = content[:middle] + replacement + content[middle + 1 :] + "\n"
    return "".join(lines)


def _first_body_line_lost(text: str) -> str:
    """The paste without the first line after its BEGIN line."""
    lines = text.splitlines(keepends=True)
    return "".join([lines[0], *lines[2:]])


def _chained_from_none(refused: pytest.ExceptionInfo[ValidationError]) -> bool:
    """Whether the refusal carries no exception of the parser's.

    A refusal located at its field carries none at all. A refusal raised as our
    own `ValueError` must drop the asyncssh exception it replaced.
    """
    error = (refused.value.errors()[0].get("ctx") or {}).get("error")
    return error is None or (error.__cause__ is None and error.__suppress_context__ is True)


def _pem(name: str, body: bytes, headers: str = "") -> str:
    """A PEM block named `name` around `body`, with `headers` above it, as a person pastes it."""
    encoded = base64.b64encode(body).decode()
    lines = [encoded[i : i + 64] for i in range(0, len(encoded), 64)]
    head = [headers.rstrip("\n"), ""] if headers else []
    return "\n".join([f"-----BEGIN {name}-----", *head, *lines, f"-----END {name}-----", ""])


def _pem_body(text: str) -> bytes:
    """The bytes inside a PEM block, its headers left out."""
    lines = text.strip().splitlines()[1:-1]
    return base64.b64decode("".join(line for line in lines if ":" not in line))


_OPENSSH_MAGIC = b"openssh-key-v1\0"


def _openssh_key_with(*, rounds: int = 16, kdf: bytes = b"bcrypt") -> str:
    """The encrypted OpenSSH test key with its key derivation header rewritten.

    Nothing is encrypted again. The header is all the parse reads, and a key
    sealed with 2**20 rounds would take an hour and a half to make.
    """
    packet = SSHPacket(_pem_body(_key_text("openssh-encrypted"))[len(_OPENSSH_MAGIC) :])
    cipher, _, options = packet.get_string(), packet.get_string(), packet.get_string()
    salt = SSHPacket(options).get_string()
    rest = packet.get_remaining_payload()
    header = String(cipher) + String(kdf) + String(String(salt) + UInt32(rounds))
    return _pem("OPENSSH PRIVATE KEY", _OPENSSH_MAGIC + header + rest)


def _pkcs8_key_with_iterations(
    count: int, *, pbe_version: int = 2, key_length: int | None = None
) -> str:
    """An encrypted PKCS#8 test key with its iteration count rewritten.

    asyncssh writes 2,048, for PBES2 and for PBES1 alike, and no PBKDF2 key
    length; `key_length` adds one after the count. Nothing is encrypted again,
    for the reason given above.
    """
    scheme = {} if pbe_version == 2 else {"cipher_name": "des-cbc", "hash_name": "md5"}
    text = _ssh_key("ssh-ed25519").export_private_key(
        "pkcs8-pem", PASSPHRASE, pbe_version=pbe_version, **scheme
    )

    def rewritten(node: object) -> object:
        if isinstance(node, tuple):
            items = tuple(rewritten(item) for item in node)
            if key_length is not None and len(node) >= 2 and node[1] == 2048:
                return (*items[:2], key_length, *items[2:])
            return items
        return count if isinstance(node, int) and node == 2048 else node

    body = der_encode(rewritten(der_decode(_pem_body(text.decode()))))
    return _pem("ENCRYPTED PRIVATE KEY", body)


def _pkcs1_key_with_a_negative_number() -> str:
    """The RSA test key with its private exponent made negative.

    `cryptography`'s RSA constructor refuses it with an `OverflowError`, which
    is not a `ValueError`.
    """
    numbers = list(der_decode(_pem_body(_key_text("pkcs1"))))
    numbers[3] = -numbers[3]
    return _pem("RSA PRIVATE KEY", der_encode(tuple(numbers)))


@functools.cache
def _rsa_4096() -> asyncssh.SSHKey:
    """The largest RSA key a login may hold. Made once: it takes seconds."""
    return asyncssh.generate_private_key("ssh-rsa", key_size=4096)


def _mersenne_rsa_key(p_exponent: int, q_exponent: int) -> str:
    """A valid PKCS#1 RSA key whose primes are the Mersenne primes 2**p - 1 and 2**q - 1.

    Known primes of any size are public, so anyone can build such a key in a
    moment, and validating it costs about the cube of its size.
    """
    p, q = (1 << p_exponent) - 1, (1 << q_exponent) - 1
    lam = (p - 1) * (q - 1) // math.gcd(p - 1, q - 1)
    e = next(c for c in range(65537, 1 << 20, 2) if math.gcd(c, lam) == 1)
    d = pow(e, -1, lam)
    numbers = (0, p * q, e, d, p, q, d % (p - 1), d % (q - 1), pow(q, -1, p))
    return _pem("RSA PRIVATE KEY", der_encode(numbers))


def _rsa_key_with_a_prime_of_one(passphrase: str | None = None) -> str:
    """An OpenSSH RSA key whose second prime is 1, plain or encrypted with `passphrase`.

    asyncssh's OpenSSH decoder computes `d % (q - 1)` before anything validates
    the key, so it raises `ZeroDivisionError`. Written by asyncssh's own export,
    with only the RSA numbers swapped in, because `cryptography` refuses to hold
    such a key.
    """
    key = _ssh_key("ssh-rsa")
    numbers = key.pyca_key.private_numbers()
    public = numbers.public_numbers
    section = b"".join(
        MPInt(x) for x in (public.n, public.e, numbers.d, numbers.iqmp, numbers.p, 1)
    )
    with patch.object(type(key), "encode_ssh_private", lambda _self: section):
        return key.export_private_key(
            "openssh", passphrase, rounds=16, ignore_few_rounds=True
        ).decode()


def _rsa_key_with_a_crt_exponent_off_by_two() -> str:
    """The RSA test key with dmp1 two too large: still odd, and wrong.

    The worker's import refuses it. An OpenSSH re-export would not show it,
    because that form computes dmp1 again.
    """
    numbers = list(der_decode(_pem_body(_key_text("pkcs1"))))
    numbers[6] += 2
    return _pem("RSA PRIVATE KEY", der_encode(tuple(numbers)))


def _security_key_private_key() -> str:
    """An OpenSSH `sk-ssh-ed25519` private key: a handle for a hardware authenticator.

    Built by hand, as `ssh-keygen -t ed25519-sk` writes one, so no device is needed.
    """
    algorithm = b"sk-ssh-ed25519@openssh.com"
    public_key = _ssh_key("ssh-ed25519").public_data[-32:]
    public = String(algorithm) + String(public_key) + String(b"ssh:")
    private = (
        UInt32(9) + UInt32(9) + public + Byte(1) + String(bytes(64)) + String(b"") + String(b"c")
    )
    private += bytes(range(1, (8 - len(private) % 8) % 8 + 1))
    body = (
        _OPENSSH_MAGIC
        + String(b"none")
        + String(b"none")
        + String(b"")
        + UInt32(1)
        + String(public)
        + String(private)
    )
    return _pem("OPENSSH PRIVATE KEY", body)


def _der_length(size: int) -> bytes:
    if size < 0x80:
        return bytes([size])
    octets = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(octets)]) + octets


def _quadratic_der_paste(form: str) -> str:
    """An ENCRYPTED PRIVATE KEY block that asyncssh's DER decoder reads in quadratic time.

    `sequence`: one SEQUENCE of 800,000 NULLs, a 2.19 MB request body, which the
    decoder copies the rest of for each element (15 s). `oid`: one OBJECT
    IDENTIFIER whose arc runs 400,000 bytes, a 0.55 MB body, which grows a big
    integer one byte at a time (8.5 s).
    """
    if form == "sequence":
        content = b"\x05\x00" * 800_000
        der = b"\x30" + _der_length(len(content)) + content
    else:
        arc = b"\x2a" + b"\xff" * 400_000 + b"\x01"
        oid = b"\x06" + _der_length(len(arc)) + arc
        der = b"\x30" + _der_length(len(oid)) + oid
    return _pem("ENCRYPTED PRIVATE KEY", der)


def _child(code: str) -> tuple[str, ...]:
    """A child process that runs `code` in place of the key check."""
    return (sys.executable, "-I", "-c", code)


def _recording_children(
    monkeypatch: pytest.MonkeyPatch, spawned: asyncio.Event | None = None, count: int = 1
) -> list[SimpleNamespace]:
    """Each child process the key check starts, with its pid and when it started.

    `spawned` is set once `count` children exist.
    """
    children: list[SimpleNamespace] = []
    spawn = asyncio.create_subprocess_exec

    async def recording(*command: str, **options: object) -> asyncio.subprocess.Process:
        started = time.perf_counter()
        process = await spawn(*command, **options)
        children.append(SimpleNamespace(pid=process.pid, started=started))
        if spawned is not None and len(children) >= count:
            spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    return children


def _alive(pid: int) -> bool:
    """Whether a process with this pid still exists, reaped or not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@dataclass
class _LoopLag:
    longest: float = 0.0
    most_threads: int = 0


@contextlib.asynccontextmanager
async def _loop_lag() -> AsyncIterator[_LoopLag]:
    """How late a 5 ms timer fires while the body runs, and the most threads alive.

    The measure of a free event loop: a call that holds the GIL or blocks the
    loop delays the timer by as long as it holds it.
    """
    lag = _LoopLag()
    done = False
    ticking = asyncio.Event()

    async def tick() -> None:
        last = time.perf_counter()
        ticking.set()
        while not done:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            lag.longest = max(lag.longest, now - last - 0.005)
            lag.most_threads = max(lag.most_threads, threading.active_count())
            last = now

    ticker = asyncio.ensure_future(tick())
    # The body starts only once the timer has its first reading. Work that takes
    # the GIL at once would otherwise run before the timer starts, and show no lag.
    await ticking.wait()
    try:
        yield lag
    finally:
        done = True
        await ticker


def _deeply_nested_der(depth: int = 2000) -> bytes:
    """SEQUENCEs nested `depth` deep. asyncssh's DER decoder recurses once a level."""
    body = b""
    for _ in range(depth):
        size = len(body).to_bytes((len(body).bit_length() + 7) // 8 or 1, "big")
        length = size if len(body) < 0x80 else bytes([0x80 | len(size)]) + size
        body = b"\x30" + length + body
    return body


def _storing():
    """The repository and the side effects of storing a secret, patched out.

    The second patch is the repository's `create`, which a test asserts on.
    """
    module = "app.services.organization_secret"
    return (
        patch(f"{module}.organization_secret_repo.get_by_name", new=AsyncMock(return_value=None)),
        patch(
            f"{module}.organization_secret_repo.create",
            new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
        ),
        patch(f"{module}.record_audit", new=AsyncMock()),
        patch(f"{module}.NotificationService", new=MagicMock(return_value=AsyncMock())),
    )


async def _refusal_when_stored(private_key: str, passphrase: str | None) -> tuple[str, bool]:
    """What the refusal to store this key shows, and whether it drops the parser's exception.

    Storing a login is two checks: the parse refuses what it can tell without
    decrypting, and the service refuses a key that does not open. The test
    reads whichever refused, through the path the vault form takes.
    """
    value = {
        "kind": "ssh_credentials",
        "username": "partner-sync",
        "host": "sftp.example.com",
        "private_key": private_key,
        **({} if passphrase is None else {"passphrase": passphrase}),
    }
    try:
        body = SecretCreate.model_validate(
            {"name": "Partner drop", "purpose": "sftp", "value": value}
        )
    except ValidationError as exc:
        refused = pytest.ExceptionInfo.from_exc_info((type(exc), exc, exc.__traceback__))
        return _what_a_refusal_shows(refused), _chained_from_none(refused)
    by_name, create, audit, notifications = _storing()
    with by_name, create as created, audit, notifications:
        try:
            await OrganizationSecretService(_db()).create(
                _ctx(), name=body.name, value=body.value, purpose=body.purpose
            )
        except BadRequestError as exc:
            created.assert_not_awaited()
            rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            shown = "\n".join([exc.message, str(exc.details), rendered])
            return shown, exc.__cause__ is None and exc.__context__ is None
    pytest.fail("The key was stored")


class TestSshCredentials:
    """The SSH login an SFTP source logs in with, and the one host it may be used with."""

    def test_an_ssh_login_takes_a_key_or_a_password_and_never_both_or_neither(self):
        """A login uses one method. With both, the connector would have to guess
        which one to offer. With neither, it has nothing to log in with."""
        assert _ssh_login(private_key=_key_text("openssh")).password is None
        assert _ssh_login(password=PASSWORD).private_key is None
        with pytest.raises(ValidationError, match="a private key or a password"):
            _ssh_login(private_key=_key_text("openssh"), password=PASSWORD)
        with pytest.raises(ValidationError, match="a private key or a password"):
            _ssh_login()

    def test_a_passphrase_without_a_key_is_refused(self):
        """A passphrase opens a key. Beside a password, it is a second secret that
        nothing reads."""
        with pytest.raises(ValidationError, match="passphrase goes with a private key"):
            _ssh_login(password=PASSWORD, passphrase=PASSPHRASE)

    @pytest.mark.parametrize(
        ("form", "passphrase"),
        [
            ("openssh", None),
            ("pkcs8", None),
            ("pkcs1", None),
            ("openssh-encrypted", PASSPHRASE),
            ("pkcs8-encrypted", PASSPHRASE),
            ("pkcs1-encrypted", PASSPHRASE),
            # asyncssh ignores a passphrase for a key that is not encrypted, in
            # all three formats. The login accepts what asyncssh accepts.
            ("openssh", PASSPHRASE),
            ("pkcs8", PASSPHRASE),
            ("pkcs1", PASSPHRASE),
        ],
        ids=[
            "openssh",
            "pkcs8",
            "pkcs1",
            "openssh-encrypted",
            "pkcs8-encrypted",
            "pkcs1-encrypted",
            "openssh-passphrase-ignored",
            "pkcs8-passphrase-ignored",
            "pkcs1-passphrase-ignored",
        ],
    )
    def test_an_openssh_pkcs8_and_pkcs1_key_are_accepted_and_an_encrypted_one_needs_its_passphrase(
        self, form: str, passphrase: str | None
    ):
        """The key is read when it is stored. A paste that cannot log in is
        refused while the form is open, not at the first sync."""
        key = _key_text(form)

        login = _ssh_login(private_key=key, passphrase=passphrase)

        assert login.private_key.get_secret_value() == key
        if form.endswith("-encrypted"):
            with pytest.raises(ValidationError, match="give its passphrase if it has one"):
                _ssh_login(private_key=key)

    @pytest.mark.parametrize(
        "paste",
        [
            pytest.param(
                lambda: _ssh_key("ssh-ed25519").export_public_key("openssh").decode(),
                id="public-key",
            ),
            pytest.param(
                lambda: _key_text("openssh")[: len(_key_text("openssh")) // 2],
                id="truncated-openssh",
            ),
            pytest.param(
                lambda: _key_text("pkcs1")[: len(_key_text("pkcs1")) // 2],
                id="truncated-pkcs1",
            ),
        ],
    )
    def test_a_public_key_or_a_truncated_paste_is_refused_without_echoing_it(self, paste):
        """The sentence is ours. asyncssh's own text is not shown, and no part of
        the paste reaches the form, a log line or a traceback.

        Two paths would carry the paste. Pydantic's `input_value` repeats the
        submitted fields in `str()` and `repr()` of the error, cut to their head
        and tail, so the end of the paste shows. `hide_input_in_errors` closes
        it. The asyncssh exception chained to our `ValueError` would carry its
        own text. `from None` closes that.
        """
        text = paste()

        with pytest.raises(ValidationError) as refused:
            _ssh_login(private_key=text)

        shown = _what_a_refusal_shows(refused)
        assert "input_value" not in str(refused.value)
        assert "The private key could not be read" in shown
        assert "Paste the whole key file" in shown
        assert not _echoes(shown, text)
        assert "Invalid private key" not in shown
        assert "Missing PEM footer" not in shown
        assert _chained_from_none(refused)

    @pytest.mark.anyio
    @pytest.mark.parametrize("form", ["openssh-encrypted", "pkcs8-encrypted", "pkcs1-encrypted"])
    async def test_a_wrong_passphrase_is_refused_without_echoing_the_key(self, form: str):
        """asyncssh answers a wrong passphrase with a different exception for each
        format. Each one gets the same sentence, and neither the key nor the
        passphrase that was typed is shown.

        Only decrypting the key shows that the passphrase is wrong, so the parse
        accepts the login and the service refuses it when it stores it. The
        refusal names the passphrase field, for the form to mark, and carries no
        exception of the parser's, chained or as context.
        """
        key = _key_text(form)
        login = _ssh_login(private_key=key, passphrase=WRONG_PASSPHRASE)

        by_name, create, audit, notifications = _storing()
        with (
            by_name,
            create as created,
            audit,
            notifications,
            pytest.raises(BadRequestError) as refused,
        ):
            await OrganizationSecretService(_db()).create(
                _ctx(), name="Partner drop", value=login, purpose="sftp"
            )

        sentence = "The private key could not be read with this passphrase."
        exc = refused.value
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        shown = "\n".join([exc.message, str(exc.details), rendered])
        assert exc.message == sentence
        assert exc.details == {"fields": [{"field": "value.passphrase", "message": sentence}]}
        assert not _echoes(shown, key)
        assert not _echoes(shown, WRONG_PASSPHRASE, 8)
        assert "Incorrect passphrase" not in shown
        assert "Unable to decrypt" not in shown
        assert exc.__cause__ is None
        assert exc.__context__ is None
        created.assert_not_awaited()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("paste", "passphrase", "sentence", "foreign"),
        [
            pytest.param(
                # Line 10 of a 2048-bit PKCS#1 key is inside the private exponent,
                # so the key still parses and then fails cryptography's check.
                lambda: _one_character_changed(_key_text("pkcs1"), line=10),
                None,
                "Paste the whole key file",
                ["Invalid private key", "modulus"],
                id="one-character-changed-pkcs1",
            ),
            pytest.param(
                lambda: _first_body_line_lost(_key_text("pkcs8-encrypted")),
                PASSPHRASE,
                "could not be read with this passphrase",
                ["codec", "utf-8", "decode", "invalid start byte"],
                id="encrypted-pkcs8-first-line-lost",
            ),
            pytest.param(
                _pkcs1_key_with_a_negative_number,
                None,
                "Paste the whole key file",
                ["can't convert", "negative int", "unsigned"],
                id="pkcs1-with-a-negative-number",
            ),
            pytest.param(
                lambda: _pem("PRIVATE KEY", _deeply_nested_der()),
                None,
                "Paste the whole key file",
                ["recursion", "Recursion"],
                id="pkcs8-nested-too-deep",
            ),
            pytest.param(
                lambda: _pem("ENCRYPTED PRIVATE KEY", _deeply_nested_der()),
                PASSPHRASE,
                "could not be read with this passphrase",
                ["recursion", "Recursion"],
                id="encrypted-pkcs8-nested-too-deep",
            ),
            pytest.param(
                # asyncssh compiles the label into its footer pattern, unescaped.
                lambda: "-----BEGIN [ PRIVATE KEY-----\nAAAA\n-----END [ PRIVATE KEY-----\n",
                None,
                "Paste the whole key file",
                ["character range", "unterminated", "position"],
                id="label-that-is-no-pattern",
            ),
        ],
    )
    async def test_a_damaged_key_that_the_parser_refuses_in_its_own_words_gets_our_sentence(
        self, paste, passphrase: str | None, sentence: str, foreign: list[str]
    ):
        """asyncssh does not wrap every parse failure. A changed character in a
        PKCS#1 key reaches cryptography's `ValueError`, an encrypted PKCS#8 key
        without its first line a `UnicodeDecodeError`, a negative number in an
        RSA key cryptography's `OverflowError`, a body nested too deep the DER
        decoder's `RecursionError`, and a label with a bracket the `re.error`
        of the footer pattern asyncssh builds from it. The last three are not
        `ValueError`, and fuzzing the parsers found no other type
        (`KEY_PARSE_ERRORS`). The refusal is still our sentence, wherever the
        paste is refused, and their text is not shown."""
        text = paste()
        with pytest.raises(KEY_PARSE_ERRORS) as parsed:
            asyncssh.import_private_key(text, passphrase)
        # The case is here for the error asyncssh does not wrap. If asyncssh
        # starts to wrap it, this case tests nothing new: choose another paste.
        assert not isinstance(parsed.value, asyncssh.KeyImportError | asyncssh.KeyEncryptionError)

        shown, unchained = await _refusal_when_stored(text, passphrase)

        assert "input_value" not in shown
        assert sentence in shown
        assert not [words for words in foreign if words in shown]
        assert not _echoes(shown, text)
        assert unchained

    @pytest.mark.parametrize(
        "paste",
        [
            pytest.param(lambda: _openssh_key_with(rounds=2**20), id="openssh-2**20-rounds"),
            pytest.param(lambda: _openssh_key_with(rounds=2**32 - 1), id="openssh-most-rounds"),
            pytest.param(
                lambda: _openssh_key_with(rounds=MAX_KEY_ROUNDS + 1), id="openssh-one-round-over"
            ),
            pytest.param(lambda: _pkcs8_key_with_iterations(2**31), id="pkcs8-pbes2-2**31"),
            pytest.param(
                lambda: _pkcs8_key_with_iterations(MAX_KEY_ITERATIONS + 1),
                id="pkcs8-pbes2-one-over",
            ),
            pytest.param(
                # Past what a C long holds: `hashlib` would raise `OverflowError`.
                lambda: _pkcs8_key_with_iterations(2**64, pbe_version=1),
                id="pkcs8-pbes1-2**64",
            ),
            pytest.param(
                # Each number within its ceiling, but PBKDF2 runs the count once
                # per block of the key length: about 46 minutes of work.
                lambda: _pkcs8_key_with_iterations(1_000_000, key_length=1_000_000),
                id="pkcs8-pbkdf2-key-length-1000000",
            ),
            pytest.param(
                lambda: _pkcs8_key_with_iterations(2048, key_length=MAX_PBKDF2_KEY_BYTES + 1),
                id="pkcs8-pbkdf2-key-length-one-over",
            ),
        ],
    )
    def test_a_key_that_asks_for_too_much_work_is_refused_before_it_is_decrypted(self, paste):
        """An encrypted key names its own key derivation cost, and the cost is
        paid in full before a wrong passphrase shows: about 5 ms a bcrypt round.
        The parse runs on the API's event loop, before any permission check. So
        it reads the cost from the key's header and refuses one over the
        ceiling, and the refusal takes no longer for 2**20 rounds, an hour and
        a half of decrypting, than for one round over."""
        text = paste()
        started = time.perf_counter()

        with pytest.raises(ValidationError, match="too many key derivation rounds") as refused:
            _ssh_login(private_key=text, passphrase=WRONG_PASSPHRASE)

        assert time.perf_counter() - started < 1.0
        assert not _echoes(_what_a_refusal_shows(refused), text)

    def test_a_key_at_the_ceiling_is_read(self):
        """`ssh-keygen -a 100` is common advice, and `-a 16` its default. Both are
        under the ceiling, and a key exactly at it is still taken."""
        _ssh_login(private_key=_openssh_key_with(rounds=MAX_KEY_ROUNDS), passphrase=PASSPHRASE)
        _ssh_login(
            private_key=_pkcs8_key_with_iterations(MAX_KEY_ITERATIONS), passphrase=PASSPHRASE
        )
        _ssh_login(
            private_key=_pkcs8_key_with_iterations(2048, key_length=MAX_PBKDF2_KEY_BYTES),
            passphrase=PASSPHRASE,
        )

    def test_a_pkcs8_key_under_a_pem_encryption_header_is_refused(self):
        """With a PEM encryption header, asyncssh decrypts the body with the
        passphrase first and reads the iteration count out of what it decrypted,
        which the parse cannot see without decrypting. No tool writes such a
        key, so it is refused as unreadable."""
        text = _pem(
            "ENCRYPTED PRIVATE KEY",
            _pem_body(_pkcs8_key_with_iterations(2048)),
            headers="Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00112233445566778899AABBCCDDEEFF",
        )

        with pytest.raises(ValidationError, match="could not be read with this passphrase"):
            _ssh_login(private_key=text, passphrase=PASSPHRASE)

    @pytest.mark.parametrize(
        "paste",
        [
            pytest.param(
                "-----BEGIN FOO PRIVATE KEY-----\nAAAA\n-----END FOO PRIVATE KEY-----\n",
                id="unknown-block",
            ),
            pytest.param(
                # asyncssh builds its footer pattern out of the label, unescaped.
                "-----BEGIN [ PRIVATE KEY-----\nAAAA\n-----END [ PRIVATE KEY-----\n",
                id="label-that-is-no-pattern",
            ),
            pytest.param(
                # A whole encrypted key, but for its first bytes.
                lambda: _pem(
                    "OPENSSH PRIVATE KEY",
                    b"openssh-key-v0\0"
                    + _pem_body(_key_text("openssh-encrypted"))[len(_OPENSSH_MAGIC) :],
                ),
                id="openssh-block-without-its-magic",
            ),
            pytest.param("0\x00" + "-" * 10, id="der"),
            pytest.param(lambda: "0\x00\n" + _key_text("openssh"), id="der-before-a-key"),
            pytest.param(lambda: _key_text("openssh") + "é", id="not-ascii"),
        ],
    )
    @pytest.mark.parametrize("passphrase", [None, PASSPHRASE])
    def test_a_paste_asyncssh_would_not_read_as_a_private_key_is_refused(
        self, paste, passphrase: str | None
    ):
        """The parse reads the block asyncssh would decrypt, with asyncssh's own
        block finder. A block of another name, an OpenSSH block that is not an
        OpenSSH key, binary DER - which asyncssh reads first when a paste starts
        with one - and a character past ASCII are no private key it reads, with
        a passphrase or without one."""
        text = paste() if callable(paste) else paste

        with pytest.raises(ValidationError, match="The private key could not be read"):
            _ssh_login(private_key=text, passphrase=passphrase)

    @pytest.mark.parametrize("form", ["sequence", "oid"])
    @pytest.mark.parametrize("parse", [SecretCreate, SecretUpdate], ids=["create", "update"])
    def test_a_paste_too_long_to_decode_is_refused_before_anything_reads_it(self, parse, form: str):
        """The header check decodes an encrypted key's DER, and the decoder is
        quadratic: these pastes held the event loop for 15 s and 8.5 s, before
        any permission check. The length limit refuses them before the model
        validator runs, with the field named and nothing of the paste shown."""
        paste = _quadratic_der_paste(form)
        body = {
            "name": "Partner drop",
            "value": {
                "kind": "ssh_credentials",
                "username": "partner-sync",
                "host": "sftp.example.com",
                "private_key": paste,
                "passphrase": PASSPHRASE,
            },
        }
        started = time.perf_counter()

        with pytest.raises(ValidationError) as refused:
            parse.model_validate_json(json.dumps(body))

        assert time.perf_counter() - started < 0.5
        [error] = refused.value.errors()
        assert error["loc"][-1] == "private_key"
        assert error["msg"] == (
            "The private key is too long. Paste only the key file, at most 16,384 characters."
        )
        assert not _echoes(_what_a_refusal_shows(refused), paste)

    @pytest.mark.parametrize(
        ("field", "sentence"),
        [
            ("password", "The password is too long: at most 1,024 characters."),
            ("passphrase", "The passphrase is too long: at most 1,024 characters."),
        ],
    )
    def test_a_password_or_passphrase_is_at_most_1024_characters(self, field: str, sentence: str):
        """Both reach the child process's stdin, and the passphrase the key
        derivation, which reads it whole. The refusal is ours, on the field, and
        does not show the value."""
        key = {} if field == "password" else {"private_key": _key_text("openssh")}
        value = "Sentinel-9" * (MAX_PASSWORD_CHARACTERS // 10 + 1)

        _ssh_login(**key, **{field: "x" * MAX_PASSWORD_CHARACTERS})
        with pytest.raises(ValidationError) as refused:
            _ssh_login(**key, **{field: value})

        [error] = refused.value.errors()
        assert (error["loc"], error["msg"]) == ((field,), sentence)
        assert "Sentinel-9" not in _what_a_refusal_shows(refused)

    def test_a_private_key_over_the_limit_is_refused_in_our_words(self):
        """One character over the limit, refused on the field, without the key."""
        # The label is split so the repository's private-key hook does not read
        # this made-up paste as a committed key.
        label = "OPENSSH " + "PRIVATE KEY"
        paste = f"-----BEGIN {label}-----\n" + "Q" * MAX_PRIVATE_KEY_CHARACTERS

        with pytest.raises(ValidationError) as refused:
            _ssh_login(private_key=paste)

        [error] = refused.value.errors()
        assert (error["loc"], error["msg"]) == (
            ("private_key",),
            "The private key is too long. Paste only the key file, at most 16,384 characters.",
        )
        assert "QQQQQQQQ" not in _what_a_refusal_shows(refused)

    def test_a_41638_bit_rsa_key_is_refused_by_its_length_before_it_is_opened(self):
        """Validated, this key held an event loop for seven minutes. Its paste is
        about 31,700 characters, so no child process starts for it."""
        with pytest.raises(ValidationError, match="The private key is too long"):
            _ssh_login(private_key=_mersenne_rsa_key(19937, 21701))

    def test_an_rsa_4096_key_in_each_form_is_within_the_length_limit(self):
        """The largest key a login may hold, in each form asyncssh reads,
        encrypted, is well under the limit, and is taken."""
        key = _rsa_4096()
        for form in ("openssh", "pkcs8-pem", "pkcs1-pem"):
            text = key.export_private_key(form, PASSPHRASE).decode()

            assert len(text) < MAX_PRIVATE_KEY_CHARACTERS / 3
            _ssh_login(private_key=text, passphrase=PASSPHRASE)

    @pytest.mark.parametrize(
        ("fields", "location", "sentence"),
        [
            pytest.param(
                {"private_key": lambda: _key_text("openssh-encrypted")},
                "private_key",
                "The private key could not be read. Paste the whole key file, and give its "
                "passphrase if it has one.",
                id="encrypted-without-its-passphrase",
            ),
            pytest.param(
                {"private_key": lambda: "not a key at all", "passphrase": lambda: PASSPHRASE},
                "passphrase",
                "The private key could not be read with this passphrase.",
                id="unreadable-with-a-passphrase",
            ),
            pytest.param(
                {
                    "private_key": lambda: _openssh_key_with(rounds=MAX_KEY_ROUNDS + 1),
                    "passphrase": lambda: PASSPHRASE,
                },
                "private_key",
                "The private key is encrypted with too many key derivation rounds. Encrypt it "
                "again with fewer, e.g. with ssh-keygen -p -a 16 -f <key file>.",
                id="too-many-rounds",
            ),
            pytest.param(
                {"password": lambda: PASSWORD, "passphrase": lambda: PASSPHRASE},
                "passphrase",
                "A passphrase goes with a private key.",
                id="passphrase-beside-a-password",
            ),
        ],
    )
    def test_each_cheap_key_refusal_is_located_at_its_field(
        self, fields: dict, location: str, sentence: str
    ):
        """Located at the field, the form marks the input it is about and shows
        the sentence as written, as it does for the same mistake the child
        process finds. No value travels with it."""
        with pytest.raises(ValidationError) as refused:
            _ssh_login(**{name: make() for name, make in fields.items()})

        [error] = refused.value.errors()
        assert (error["loc"], error["msg"], error["input"]) == ((location,), sentence, None)

    def test_a_label_that_makes_the_footer_pattern_backtrack_is_refused_fast(self):
        """asyncssh finds a block's footer with a pattern built from its BEGIN
        line, unescaped. `(a+)+` there backtracks twice as long for each `a` in
        a footer that does not match: 28 of them hold the event loop for
        seconds, 40 for hours. The label is checked before the pattern is built."""
        text = "-----BEGIN (a+)+ PRIVATE KEY-----\nAAAA\n-----END " + "a" * 28 + "X\n"
        started = time.perf_counter()

        with pytest.raises(ValidationError, match="Paste the whole key file"):
            _ssh_login(private_key=text)

        assert time.perf_counter() - started < 1.0

    def test_an_openssh_key_with_another_key_derivation_is_left_to_the_decrypt(self):
        """asyncssh refuses an encrypted OpenSSH key whose key derivation is not
        bcrypt before it derives anything. The parse reads the rounds in its
        options all the same, whichever derivation is named, and refuses it
        without a passphrase, as any encrypted key. With one, the parse takes it
        and decrypting it refuses it."""
        text = _openssh_key_with(kdf=b"scrypt")

        with pytest.raises(ValidationError, match="give its passphrase if it has one"):
            _ssh_login(private_key=text)
        with pytest.raises(ValidationError, match="too many key derivation rounds"):
            _ssh_login(
                private_key=_openssh_key_with(kdf=b"scrypt", rounds=2**20), passphrase=PASSPHRASE
            )
        _ssh_login(private_key=text, passphrase=PASSPHRASE)

        assert inspect(text, PASSPHRASE).opens is False

    def test_a_short_passphrase_opens_an_encrypted_key(self):
        """`ssh-keygen` accepts a passphrase of five characters. The passphrase is
        not a credential the hint reads, so it has no 8-character floor, and a
        real key sealed with a short one is accepted."""
        key = _key_text("openssh-encrypted", sealed_with="abcde")

        login = _ssh_login(private_key=key, passphrase="abcde")

        assert login.passphrase.get_secret_value() == "abcde"

    @pytest.mark.parametrize(
        "fields",
        [
            pytest.param({"password": "short7!"}, id="password"),
            pytest.param({"private_key": "1234567"}, id="private-key"),
        ],
    )
    def test_a_password_or_key_too_short_to_hint_safely_is_refused(self, fields):
        """Every field that authenticates is at least 8 characters, as for every
        kind. A shorter one is a truncated paste, or short enough that four
        characters of it give most of it away."""
        with pytest.raises(ValidationError, match=f"at least {MIN_CREDENTIAL_LENGTH} characters"):
            _ssh_login(**fields)

    @pytest.mark.parametrize(
        "username",
        [
            pytest.param("", id="empty"),
            pytest.param("sync user", id="space"),
            pytest.param("sync\tuser", id="tab"),
            pytest.param("sync\nX-Forged: 1", id="newline"),
            pytest.param("sync\n", id="trailing-newline"),
            pytest.param("sync\x00", id="nul"),
            pytest.param("sync\x1b[31m", id="escape"),
            pytest.param("sync\x7f", id="delete"),
            pytest.param("sync\x80", id="c1-first"),
            pytest.param("sync\x9b31m", id="c1-csi"),
            pytest.param("sync\x9f", id="c1-last"),
            pytest.param("a" * 256, id="too-long"),
        ],
    )
    def test_a_user_name_with_whitespace_or_a_control_character_is_refused(self, username: str):
        """The user name goes into the login refusal, the sync log and log lines. A
        line break or a control character there could forge a line, and U+009B
        starts a terminal escape as ESC [ does. SSH user names hold neither, and
        are at most 255 characters here."""
        with pytest.raises(ValidationError):
            _ssh_login(username=username, password=PASSWORD)

    def test_a_user_name_of_255_characters_or_with_at_is_accepted(self):
        """The limits refuse only what the rule names. A user name may hold `@`,
        as a cloud SFTP service's `account@domain` does."""
        assert _ssh_login(username="a" * 255, password=PASSWORD).username == "a" * 255
        assert _ssh_login(username="sync@corp.example", password=PASSWORD).hint == "mple"

    @pytest.mark.parametrize(
        "host",
        [
            pytest.param("", id="empty"),
            pytest.param("sftp://sftp.example.com", id="scheme"),
            pytest.param("sftp.example.com/drop", id="path"),
            pytest.param("sync@sftp.example.com", id="user"),
            pytest.param("sftp example.com", id="space"),
            pytest.param("sftp.example.com\n", id="newline"),
            pytest.param("2001:db8::7", id="ipv6-without-brackets"),
            pytest.param("[fe80::1%eth0]", id="ipv6-zone"),
            pytest.param("*.example.com", id="wildcard"),
            pytest.param("sftp.example.com..", id="two-trailing-dots"),
            pytest.param(":2222", id="port-without-a-host"),
            pytest.param("sftp.example.com:0", id="port-zero"),
            pytest.param("sftp.example.com:65536", id="port-too-high"),
            pytest.param("-sftp.example.com", id="label-starting-with-a-hyphen"),
        ],
    )
    def test_an_ssh_login_s_host_is_one_host_and_an_optional_port(self, host: str):
        """The host binds the login. A URL, a path or a user in it could never
        match the host a source dials, so the login would be refused at every
        sync. An IPv6 address needs its brackets, or its last group reads as a
        port. Port 0 cannot be dialled."""
        with pytest.raises(ValidationError) as refused:
            _ssh_login(host=host, password=PASSWORD)

        if host:
            assert "An IPv6 address goes in brackets" in str(refused.value)

    @pytest.mark.parametrize(
        ("typed", "stored"),
        [
            pytest.param("[2001:DB8:0:0::7]", "[2001:db8::7]", id="ipv6-compressed"),
            pytest.param("[2001:db8:0::7]:2222", "[2001:db8::7]:2222", id="ipv6-with-port"),
            pytest.param("SFTP.Example.com.", "sftp.example.com", id="case-and-trailing-dot"),
            pytest.param("sftp.example.com.:2222", "sftp.example.com:2222", id="dot-and-port"),
            pytest.param("bücher.example", "xn--bcher-kva.example", id="idna"),
            pytest.param("sftp.example.com:022", "sftp.example.com:22", id="port-leading-zero"),
            pytest.param("10.0.0.5:2222", "10.0.0.5:2222", id="ipv4-with-port"),
            # DNS and Docker resolve a name with an underscore, such as a
            # Compose service's.
            pytest.param("SFTP_Server:2222", "sftp_server:2222", id="underscore"),
        ],
    )
    def test_an_ssh_login_s_host_is_stored_as_a_source_spells_it(self, typed: str, stored: str):
        """`allows` compares the bound host with the source's host as strings, and
        `SftpConfig` spells the source's host in one way. A login bound to the
        same host in another spelling would be accepted here and refused at the
        first sync, so the host is stored in the source's spelling. A Git
        token's host keeps what was typed: its binding did not change."""
        assert _ssh_login(host=typed, password=PASSWORD).host == stored
        if typed.isascii() and "_" not in typed:
            git = secret_kinds.GitTokenSecret(token="ghp_" + "x" * 20, host=typed)
            assert git.host == typed

    def test_a_git_token_s_host_still_takes_no_underscore(self):
        """The SSH login takes `sftp_server`; the Git token's host rule did not
        change with it."""
        with pytest.raises(ValidationError, match="should match pattern"):
            secret_kinds.GitTokenSecret(token="ghp_" + "x" * 20, host="git_server")

    def test_a_long_internationalized_bound_host_still_opens_after_it_is_stored(self):
        """The host is stored IDNA-encoded, which is longer than the Unicode it was
        typed in, and the vault parses the stored host again each time it opens
        the login. A name of up to 253 encoded characters, and its port, opens;
        one longer is refused when it is typed, not when the vault opens it."""
        label = "bücher" + "a" * 45
        host = ".".join([label] * 4) + ".exaaaaaaample:2222"
        scope = VaultScope.organization(uuid.uuid4())
        login = _ssh_login(host=host, password=PASSWORD)

        opened = unseal_secret(
            seal_secret(login, scope=scope).ciphertext,
            kind=secret_kinds.SecretKind.SSH_CREDENTIALS,
            scope=scope,
        )

        assert len(login.host) > 253
        assert opened == login
        # 220 characters typed, 254 encoded.
        over = ".".join([label] * 4) + "." + "ü" * 12
        with pytest.raises(ValidationError, match="An IPv6 address goes in brackets"):
            _ssh_login(host=over + ":2222", password=PASSWORD)

    def test_an_ssh_login_is_hinted_by_its_user_name(self):
        """The user name says which login it is, and it is not secret, as an AWS
        access key id is not. Four characters of a key would be its PEM armour."""
        by_key = _ssh_login(username="partner-sync", private_key=_key_text("openssh"))
        by_password = _ssh_login(username="partner-sync", password=PASSWORD)

        assert by_key.hint == "sync"
        assert by_password.hint == "sync"
        sealed = seal_secret(by_password, scope=VaultScope.organization(uuid.uuid4()))
        assert sealed.hint == "sync"

    def test_an_ssh_login_masks_its_key_and_password_in_a_repr(self):
        """A model in a log line is how a plaintext usually escapes. The key, its
        passphrase and the password are masked. The user name is not secret."""
        key = _key_text("openssh-encrypted")
        by_key = _ssh_login(private_key=key, passphrase=PASSPHRASE)
        by_password = _ssh_login(password=PASSWORD)

        for login, hidden in ((by_key, [key, PASSPHRASE]), (by_password, [PASSWORD])):
            for surface in (repr(login), str(login), str(login.model_dump())):
                assert not [value for value in hidden if _echoes(surface, value, 8)]
        assert "partner-sync" in repr(by_password)

    @pytest.mark.parametrize(
        "parse",
        [
            pytest.param(
                lambda value: SecretCreate.model_validate({"name": "n", "value": value}),
                id="secret-create",
            ),
            pytest.param(
                lambda value: SecretCreate.model_validate_json(
                    json.dumps({"name": "n", "value": value})
                ),
                id="secret-create-json",
            ),
            pytest.param(
                lambda value: SecretUpdate.model_validate({"value": value}), id="secret-update"
            ),
            pytest.param(secret_kinds._STORABLE_ADAPTER.validate_python, id="vault-adapter"),
            pytest.param(
                lambda value: secret_kinds._STORABLE_ADAPTER.validate_json(json.dumps(value)),
                id="vault-adapter-json",
            ),
        ],
    )
    @pytest.mark.parametrize(
        "fields",
        [
            pytest.param(
                {"private_key": "PRIVATE-KEY-SENTINEL", "password": "PASSWORD-SENTINEL"},
                id="key-and-password",
            ),
            pytest.param(
                {"password": "PASSWORD-SENTINEL", "passphrase": "PASSPHRASE-SENTINEL"},
                id="passphrase-beside-a-password",
            ),
            pytest.param(
                {"private_key": "PRIVATE-KEY-SENTINEL", "passphrase": "PASSPHRASE-SENTINEL"},
                id="unreadable-key-and-its-passphrase",
            ),
            pytest.param({"password": "SENTNL!"}, id="password-too-short"),
        ],
    )
    def test_a_refused_login_is_not_repeated_in_the_error_on_any_path_that_parses_it(
        self, parse, fields: dict[str, str]
    ):
        """`hide_input_in_errors` is read from the outermost schema only. The vault
        form posts a `SecretCreate` or a `SecretUpdate`, and the vault opens a
        stored login through its adapter, so each of them sets it: a refused
        login's key, passphrase and password stay out of `str()` and `repr()` of
        the error, which is what reaches a log line."""
        value = {"kind": "ssh_credentials", "username": "sync", "host": "h.example", **fields}

        with pytest.raises(ValidationError) as refused:
            parse(value)

        for surface in (str(refused.value), repr(refused.value)):
            assert "SENTINEL" not in surface
            assert "SENTNL!" not in surface

    def test_an_ssh_login_survives_the_envelope_whole(self):
        """The vault seals the login as JSON, and the key check runs again when it
        opens. So the key keeps its line breaks and the passphrase its value, or
        the stored login stops opening."""
        scope = VaultScope.organization(uuid.uuid4())
        key = _key_text("openssh-encrypted")
        value = _ssh_login(private_key=key, passphrase=PASSPHRASE)

        opened = unseal_secret(
            seal_secret(value, scope=scope).ciphertext,
            kind=secret_kinds.SecretKind.SSH_CREDENTIALS,
            scope=scope,
        )

        assert opened == value
        assert opened.private_key.get_secret_value() == key
        assert opened.passphrase.get_secret_value() == PASSPHRASE

    @pytest.mark.parametrize(
        ("host", "hostname", "port", "allowed"),
        [
            ("sftp.example.com", "sftp.example.com", 22, True),
            ("SFTP.Example.com", "sftp.example.com", 22, True),
            ("sftp.example.com:22", "sftp.example.com", 22, True),
            ("sftp.example.com", "sftp.example.com", 2222, False),
            ("sftp.example.com:2222", "sftp.example.com", 2222, True),
            ("sftp.example.com:2222", "sftp.example.com", 22, False),
            # 443 is the Git default, not the SSH one.
            ("sftp.example.com", "sftp.example.com", 443, False),
            ("sftp.example.com", "sftp.example.com.evil.test", 22, False),
            ("sftp.example.com", "files.example.com", 22, False),
            ("[2001:db8::7]", "2001:db8::7", 22, True),
            ("[2001:db8::7]:2222", "2001:db8::7", 2222, True),
            ("[2001:db8::7]", "2001:db8::8", 22, False),
            ("xn--bcher-kva.example", "xn--bcher-kva.example", 22, True),
            # Bound in another spelling of the same host. SftpConfig compresses an
            # IPv6 address, takes no trailing dot and IDNA-encodes a name.
            ("[2001:DB8:0:0::7]", "2001:db8::7", 22, True),
            ("sftp.example.com.", "sftp.example.com", 22, True),
            ("bücher.example", "xn--bcher-kva.example", 22, True),
            ("sftp_server:2222", "sftp_server", 2222, True),
        ],
    )
    def test_an_ssh_login_allows_only_its_host_and_port_with_22_by_default(
        self, host: str, hostname: str, port: int, allowed: bool
    ):
        """A member who can edit a source could point it at a server of their own
        and receive the password. The host is sealed with the login, so the
        connector sends the login to that host and port only (#987)."""
        login = _ssh_login(host=host, password=PASSWORD)

        assert login.allows(hostname, port) is allowed

    def test_the_ssh_login_form_draws_the_key_as_a_masked_textarea(self):
        """A key has line breaks, and a password input removes them. So the key is
        a textarea, and masked like every field that authenticates."""
        info = next(
            info
            for info in describe_kinds()
            if info.kind == secret_kinds.SecretKind.SSH_CREDENTIALS
        )
        properties = info.json_schema["properties"]

        def masked(name: str) -> bool:
            branches = [properties[name], *properties[name].get("anyOf", [])]
            return any(branch.get("format") == "password" for branch in branches)

        assert properties["private_key"].get("x-textarea") is True
        assert masked("private_key")
        assert masked("passphrase")
        assert masked("password")
        assert "x-textarea" not in properties["password"]


class TestOpeningAnSshKeyWhenItIsStored:
    """Opening a key costs what the key asks for: its key derivation, and for RSA a
    validation that grows about as the cube of the modulus, with the GIL held. So
    the service opens it in a child process with a time limit, after the caller
    was authorized, and awaits it without holding a thread or the event loop."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "encrypted"])
    @pytest.mark.parametrize("algorithm", ["ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-4096"])
    async def test_a_key_that_opens_is_stored(self, algorithm: str, encrypted: bool):
        key = _rsa_4096() if algorithm == "rsa-4096" else _ssh_key(algorithm)
        passphrase = PASSPHRASE if encrypted else None
        text = key.export_private_key(
            "openssh", passphrase, rounds=16, ignore_few_rounds=True
        ).decode()
        login = _ssh_login(username="partner-sync", private_key=text, passphrase=passphrase)

        by_name, create, audit, notifications = _storing()
        with by_name, create as created, audit, notifications:
            await OrganizationSecretService(_db()).create(
                _ctx(), name="Partner drop", value=login, purpose="sftp"
            )

        stored = created.call_args.kwargs
        assert (stored["kind"], stored["hint"]) == ("ssh_credentials", "sync")
        assert PASSPHRASE not in stored["sealed_secret"]

    @pytest.mark.anyio
    @pytest.mark.parametrize("form", ["openssh-encrypted", "pkcs8-encrypted", "pkcs1-encrypted"])
    async def test_an_encrypted_key_of_each_form_is_stored(self, form: str):
        login = _ssh_login(private_key=_key_text(form), passphrase=PASSPHRASE)

        by_name, create, audit, notifications = _storing()
        with by_name, create as created, audit, notifications:
            await OrganizationSecretService(_db()).create(
                _ctx(), name="Partner drop", value=login, purpose="sftp"
            )

        created.assert_awaited_once()

    @pytest.mark.anyio
    async def test_a_rotation_to_a_key_that_does_not_open_is_refused(self):
        ctx = _ctx()
        row = _row(ctx, _ssh_login(password=PASSWORD))
        rotated = _ssh_login(
            private_key=_key_text("openssh-encrypted"), passphrase=WRONG_PASSPHRASE
        )

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.update", new=AsyncMock()
            ) as update,
            pytest.raises(BadRequestError) as refused,
        ):
            await OrganizationSecretService(_db()).update(ctx, row.id, value=rotated)

        assert refused.value.details["fields"][0]["field"] == "value.passphrase"
        assert refused.value.message == "The private key could not be read with this passphrase."
        update.assert_not_awaited()

    @pytest.mark.anyio
    async def test_a_key_without_a_passphrase_that_does_not_open_names_the_key(self):
        login = _ssh_login(private_key=_one_character_changed(_key_text("pkcs1"), line=10))

        by_name, create, audit, notifications = _storing()
        with by_name, create, audit, notifications, pytest.raises(BadRequestError) as refused:
            await OrganizationSecretService(_db()).create(
                _ctx(), name="Partner drop", value=login, purpose="sftp"
            )

        assert refused.value.details["fields"][0]["field"] == "value.private_key"
        assert "Paste the whole key file" in refused.value.message

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("p", "q"),
        [
            pytest.param(2203, 2281, id="4484-bits"),
            # Validated, this key took 5.4 s.
            pytest.param(4423, 4253, id="8676-bits"),
        ],
    )
    async def test_an_rsa_key_over_4096_bits_is_refused_without_being_validated(
        self, p: int, q: int
    ):
        """A valid RSA key of any size can be built from two known primes, and its
        validation grows about as the cube of the size. The child reports the
        size before it validates anything, so the refusal takes no longer for
        8,676 bits than for 4,484. A larger key's paste is over the length limit,
        and the parse refuses it first."""
        login = _ssh_login(private_key=_mersenne_rsa_key(p, q))
        started = time.perf_counter()

        by_name, create, audit, notifications = _storing()
        with by_name, create, audit, notifications, pytest.raises(BadRequestError) as refused:
            await OrganizationSecretService(_db()).create(
                _ctx(), name="Partner drop", value=login, purpose="sftp"
            )

        assert time.perf_counter() - started < organization_secret.KEY_CHECK_SECONDS
        sentence = (
            "The private key is an RSA key of more than 4096 bits. Use an Ed25519 key, "
            "or an RSA key of at most 4096 bits."
        )
        assert refused.value.details == {
            "fields": [{"field": "value.private_key", "message": sentence}]
        }

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("paste", "passphrase", "field", "sentence"),
        [
            pytest.param(
                _rsa_key_with_a_prime_of_one,
                None,
                "value.private_key",
                "The private key could not be read. Paste the whole key file, and give its "
                "passphrase if it has one.",
                id="prime-of-one",
            ),
            pytest.param(
                lambda: _rsa_key_with_a_prime_of_one(PASSPHRASE),
                PASSPHRASE,
                "value.passphrase",
                "The private key could not be read with this passphrase.",
                id="prime-of-one-encrypted",
            ),
            pytest.param(
                lambda: _rsa_key_with_a_crt_exponent_off_by_two(),
                None,
                "value.private_key",
                "The private key could not be read. Paste the whole key file, and give its "
                "passphrase if it has one.",
                id="crt-exponent-wrong",
            ),
            pytest.param(
                lambda: _mersenne_rsa_key(3217, 521),
                None,
                "value.private_key",
                "The private key is an RSA key whose two primes are not of the same size. "
                "Use an Ed25519 key, or an RSA key that ssh-keygen made.",
                id="unbalanced-primes",
            ),
            pytest.param(
                _security_key_private_key,
                None,
                "value.private_key",
                "The private key is held on a security key, which a sync cannot use to log "
                "in. Use an Ed25519 key without a security key.",
                id="security-key",
            ),
        ],
    )
    async def test_a_key_the_worker_cannot_use_or_that_costs_too_much_is_refused(
        self, paste, passphrase: str | None, field: str, sentence: str
    ):
        """A prime of 1 ends asyncssh's decoder in a `ZeroDivisionError`: a bad
        paste, not a server fault. Primes of very different sizes make a valid
        key that takes seconds to validate at each connection. A security key's
        file holds a handle for a device the worker does not have."""
        login = _ssh_login(private_key=paste(), passphrase=passphrase)

        by_name, create, audit, notifications = _storing()
        with by_name, create, audit, notifications, pytest.raises(BadRequestError) as refused:
            await OrganizationSecretService(_db()).create(
                _ctx(), name="Partner drop", value=login, purpose="sftp"
            )

        assert refused.value.details == {"fields": [{"field": field, "message": sentence}]}

    @pytest.mark.anyio
    async def test_the_key_is_opened_in_a_child_that_holds_no_thread_and_no_loop_time(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The key and its passphrase reach the child on stdin, never in its
        arguments or its environment, which other processes on the host can
        read. The child is Python in isolated mode, running the key check module
        alone. While it opens an RSA key of 8,676 bits, which opened in-process
        held the GIL and the event loop for 5.4 s, the loop keeps running and no
        thread is started."""
        login = _ssh_login(private_key=_mersenne_rsa_key(4423, 4253))
        spawned: list[tuple[tuple[str, ...], dict]] = []
        spawn = asyncio.create_subprocess_exec

        async def recording(*command: str, **options: object):
            spawned.append((command, options))
            return await spawn(*command, **options)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
        threads = threading.active_count()
        refused: BadRequestError | None = None
        by_name, create, audit, notifications = _storing()
        with by_name, create, audit, notifications:
            async with _loop_lag() as lag:
                try:
                    await OrganizationSecretService(_db()).create(
                        _ctx(), name="Partner drop", value=login, purpose="sftp"
                    )
                except BadRequestError as exc:
                    refused = exc

        # The stall this guards against was 5.4 s for this key, and more for a
        # larger one, so 1 s still tells the two apart on a loaded runner.
        assert lag.longest < 1.0
        assert lag.most_threads <= threads
        [(command, options)] = spawned
        assert command == (sys.executable, "-I", "-m", "app.services.ssh_key_check")
        assert options["env"] == {}
        assert not [part for part in command if "PRIVATE" in part]
        assert refused is not None
        assert "RSA key of more than 4096 bits" in refused.message

    @pytest.mark.anyio
    async def test_a_child_that_stalls_is_killed_in_time_and_the_key_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A key that takes longer than the limit to open is refused, and its child
        is killed and reaped: nothing keeps running after the answer. The loop is
        free while it waits: a wait that blocked it would hold it for the whole
        2 s limit."""
        monkeypatch.setattr(organization_secret, "KEY_CHECK_SECONDS", 2.0)
        monkeypatch.setattr(
            organization_secret, "_KEY_CHECK_COMMAND", _child("import time; time.sleep(60)")
        )
        children = _recording_children(monkeypatch)
        login = _ssh_login(private_key=_key_text("openssh"))
        started = time.perf_counter()

        by_name, create, audit, notifications = _storing()
        with by_name, create, audit, notifications, pytest.raises(BadRequestError) as refused:
            async with _loop_lag() as lag:
                await OrganizationSecretService(_db()).create(
                    _ctx(), name="Partner drop", value=login, purpose="sftp"
                )

        assert time.perf_counter() - started < 2.0 + 1.0
        # A wait that blocked the loop would hold it 2 s; 1 s leaves room for a
        # loaded runner and still tells the two apart.
        assert lag.longest < 1.0
        assert refused.value.message == (
            "The private key takes too long to open. Use an Ed25519 key, or an RSA key "
            "of at most 4096 bits."
        )
        assert refused.value.details["fields"][0]["field"] == "value.private_key"
        assert not _alive(children[0].pid)

    @pytest.mark.anyio
    async def test_the_key_check_is_killed_when_the_key_outlasts_its_time(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The child is what bounds what a header cannot show. A PBKDF2 key length
        multiplies the iterations; this key would take about seventeen seconds,
        and the real check is stopped at its limit."""
        monkeypatch.setattr(organization_secret, "KEY_CHECK_SECONDS", 1.0)
        started = time.perf_counter()

        report = await organization_secret._open_in_a_child(
            _pkcs8_key_with_iterations(20_000, key_length=320_000), WRONG_PASSPHRASE
        )

        assert report is None
        assert time.perf_counter() - started < 1.0 + 1.0

    @pytest.mark.anyio
    async def test_a_cancelled_request_leaves_no_child_behind(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            organization_secret, "_KEY_CHECK_COMMAND", _child("import time; time.sleep(60)")
        )
        spawned = asyncio.Event()
        children = _recording_children(monkeypatch, spawned)
        task = asyncio.ensure_future(organization_secret._open_in_a_child("key", None))
        await spawned.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not _alive(children[0].pid)

    @pytest.mark.anyio
    async def test_a_save_that_finds_both_slots_busy_is_refused_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Each check is a process, and a save already holds a database
        connection. A save that waited for a slot would hold it all the while, so
        a burst of saves would empty the pool for every other request. The third
        save at once is refused without waiting, and starts no process."""
        monkeypatch.setattr(
            organization_secret, "_KEY_CHECK_COMMAND", _child("import time; time.sleep(60)")
        )
        both = asyncio.Event()
        children = _recording_children(monkeypatch, both, count=2)
        running = [
            asyncio.ensure_future(organization_secret._open_in_a_child("key", None))
            for _ in range(2)
        ]
        await both.wait()
        login = _ssh_login(private_key=_key_text("openssh"))
        started = time.perf_counter()

        by_name, create, audit, notifications = _storing()
        try:
            with (
                by_name,
                create as created,
                audit,
                notifications,
                pytest.raises(ExternalServiceError) as refused,
            ):
                # Bounded, so a save that waited for a slot fails here, not hangs.
                await asyncio.wait_for(
                    OrganizationSecretService(_db()).create(
                        _ctx(), name="Partner drop", value=login, purpose="sftp"
                    ),
                    2.0,
                )
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)

        assert time.perf_counter() - started < 0.5
        assert refused.value.status_code == 503
        assert refused.value.code == "SERVICE_BUSY"
        assert refused.value.message == (
            "Other private keys are being checked now. Try again in a moment."
        )
        assert len(children) == 2
        created.assert_not_awaited()

    @pytest.mark.anyio
    async def test_a_child_that_fails_without_a_report_is_an_internal_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """A child that dies says nothing about the key, so the key is not refused
        as unreadable. Its exit status is logged, and nothing of the key."""
        monkeypatch.setattr(
            organization_secret, "_KEY_CHECK_COMMAND", _child("import sys; sys.exit(3)")
        )
        key = _key_text("openssh")

        with pytest.raises(InternalError) as failed:
            await organization_secret._open_in_a_child(key, PASSPHRASE)

        assert failed.value.message == "The private key could not be checked. Try again later."
        [record] = [entry for entry in caplog.records if entry.msg == "ssh_key_check_failed"]
        assert record.returncode == 3
        assert not _echoes(caplog.text, key)
        assert PASSPHRASE not in caplog.text

    @pytest.mark.anyio
    async def test_a_caller_who_may_not_edit_the_secret_starts_no_child(self):
        """`PATCH /secrets/{id}` has no gate: the service decides, on the row. A
        caller it refuses must not get a key opened first."""
        ctx = _ctx()
        row = _row(ctx, _ssh_login(password=PASSWORD))
        rotated = _ssh_login(
            private_key=_key_text("openssh-encrypted"), passphrase=WRONG_PASSPHRASE
        )

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "app.services.organization_secret.resolve_access", new=AsyncMock(return_value=False)
            ),
            patch("app.services.organization_secret._open_in_a_child") as opened,
            pytest.raises(NotFoundError),
        ):
            await OrganizationSecretService(_db()).update(ctx, row.id, value=rotated)

        opened.assert_not_called()

    @pytest.mark.anyio
    async def test_a_login_without_a_key_starts_no_child(self):
        by_name, create, audit, notifications = _storing()
        with (
            by_name,
            create as created,
            audit,
            notifications,
            patch("app.services.organization_secret._open_in_a_child") as opened,
        ):
            await OrganizationSecretService(_db()).create(
                _ctx(), name="Partner drop", value=_ssh_login(password=PASSWORD), purpose="sftp"
            )

        opened.assert_not_called()
        created.assert_awaited_once()

    @pytest.mark.parametrize(
        ("report", "passphrase", "refusal"),
        [
            pytest.param(None, None, "private_key", id="out-of-time"),
            pytest.param(
                KeyReport(opens=True, algorithm="ssh-rsa", rsa_bits=4097),
                None,
                "private_key",
                id="rsa-4097",
            ),
            pytest.param(KeyReport(opens=False), None, "private_key", id="unreadable"),
            pytest.param(KeyReport(opens=False), PASSPHRASE, "passphrase", id="wrong-passphrase"),
            pytest.param(
                KeyReport(opens=True, algorithm="ssh-rsa", rsa_bits=4096), None, None, id="rsa-4096"
            ),
            pytest.param(
                KeyReport(opens=True, algorithm="ssh-ed25519"), PASSPHRASE, None, id="ed25519"
            ),
            pytest.param(
                KeyReport(opens=True, algorithm="ssh-rsa", rsa_bits=4096, rsa_prime_gap=17),
                None,
                "private_key",
                id="primes-17-bits-apart",
            ),
            pytest.param(
                KeyReport(opens=True, algorithm="ssh-rsa", rsa_bits=4096, rsa_prime_gap=16),
                None,
                None,
                id="primes-16-bits-apart",
            ),
            pytest.param(
                KeyReport(opens=True, algorithm="sk-ecdsa-sha2-nistp256@openssh.com"),
                None,
                "private_key",
                id="security-key",
            ),
        ],
    )
    def test_the_child_s_report_decides_the_refusal(
        self, report: KeyReport | None, passphrase: str | None, refusal: str | None
    ):
        key = _key_text("openssh-encrypted") if passphrase else _key_text("openssh")
        login = _ssh_login(private_key=key, passphrase=passphrase)

        answer = login.opening_refusal(report)

        assert (answer and answer[0]) == refusal


class TestStoringASecret:
    @pytest.mark.anyio
    async def test_the_value_is_sealed_and_only_a_hint_is_kept(self):
        ctx = _ctx()
        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.create",
                new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
            ) as create,
            patch("app.services.organization_secret.record_audit", new=AsyncMock()) as audit,
        ):
            await OrganizationSecretService(_db()).create(
                ctx, name="Weather API", value=ApiKeySecret(api_key="wx-live-abcd4242")
            )

        stored = create.call_args.kwargs
        assert stored["hint"] == "4242"
        assert "wx-live-abcd4242" not in stored["sealed_secret"]
        assert stored["kind"] == "api_key"
        # The audit trail records what identifies the secret, never the secret.
        assert audit.call_args.kwargs["details"] == {
            "name": "Weather API",
            "kind": "api_key",
            "hint": "4242",
        }

    @pytest.mark.anyio
    async def test_a_taken_name_is_a_conflict_rather_than_a_crash(self):
        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get_by_name",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.create",
                new=AsyncMock(),
            ) as create,
            pytest.raises(AlreadyExistsError) as refused,
        ):
            await OrganizationSecretService(_db()).create(
                _ctx(), name="Weather API", value=ApiKeySecret(api_key="wx-12345678")
            )

        assert refused.value.status_code == 409
        assert create.await_count == 0

    @pytest.mark.anyio
    async def test_listing_is_scoped_to_the_caller(self):
        ctx = _ctx()
        with patch(
            "app.services.organization_secret.organization_secret_repo.list_secrets",
            new=AsyncMock(return_value=[]),
        ) as listed:
            await OrganizationSecretService(_db()).list_secrets(ctx)

        assert listed.call_args.kwargs["organization_id"] == ctx.organization_id

    @pytest.mark.anyio
    async def test_the_listing_carries_the_authors_id_for_a_stable_avatar_colour(self):
        """The "Added by" column draws a fallback avatar, and its colour is seeded
        on the author's id - so it must be on the row, not just their email. Seeding
        on the email instead gave the same person a different hue here than in
        member lists, which all seed on the id (#799)."""
        ctx = _ctx()
        author_id = uuid.uuid4()
        secret = _row(ctx, ApiKeySecret(api_key="wx-live-4242"))
        secret.description = None
        secret.purpose = "custom"
        secret.visibility = "org"
        secret.owner_user_id = None
        secret.created_by_user_id = author_id
        secret.created_at = datetime.now(UTC)
        secret.updated_at = None

        identity = member_repo.MemberIdentity(email="ada@acme.test", avatar_url=None)
        with (
            patch(
                "app.services.organization_secret.visible_resource_ids",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.list_secrets",
                new=AsyncMock(return_value=[secret]),
            ),
            patch(
                "app.services.organization_secret.member_repo.get_identities_for_users",
                new=AsyncMock(return_value={author_id: identity}),
            ),
            patch(
                "app.services.organization_secret.resource_grant_repo.count_for_resources",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.agents_using_for_secrets",
                new=AsyncMock(return_value={}),
            ),
        ):
            rows = await OrganizationSecretService(_db()).list_secrets(ctx)

        assert rows[0].created_by_user_id == author_id
        assert rows[0].created_by_email == "ada@acme.test"


class TestRotatingASecret:
    @pytest.mark.anyio
    async def test_a_new_value_replaces_the_envelope_and_the_hint(self):
        ctx = _ctx()
        row = _row(ctx, ApiKeySecret(api_key="wx-old-0000"))

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.update",
                new=AsyncMock(return_value=row),
            ) as update,
            patch("app.services.organization_secret.record_audit", new=AsyncMock()) as audit,
        ):
            await OrganizationSecretService(_db()).update(
                ctx, row.id, value=ApiKeySecret(api_key="wx-new-9999")
            )

        written = update.call_args.kwargs["update_data"]
        assert written["hint"] == "9999"
        assert "wx-new-9999" not in written["sealed_secret"]
        assert audit.call_args.kwargs["action"] == "secret.rotated"

    @pytest.mark.anyio
    async def test_a_rotation_cannot_change_the_shape_of_the_secret(self):
        """Agents were published against a capability requiring one shape.

        Swapping an API key for an AWS key pair under a stable id would break
        every one of them at run time, with nothing pointing back at this edit.
        """
        ctx = _ctx()
        row = _row(ctx, ApiKeySecret(api_key="wx-old-0000"))

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.update",
                new=AsyncMock(),
            ) as update,
            pytest.raises(BadRequestError) as refused,
        ):
            await OrganizationSecretService(_db()).update(
                ctx,
                row.id,
                value=AwsCredentialsSecret(
                    aws_access_key_id="AKIA1",
                    aws_secret_access_key="s-never-shown",
                    region_name="us-east-1",
                ),
            )

        assert "Create a new secret instead" in refused.value.message
        assert update.await_count == 0

    @pytest.mark.anyio
    async def test_a_rename_checks_the_new_name_is_free(self):
        ctx = _ctx()
        row = _row(ctx, ApiKeySecret(api_key="wx-key-0000"))

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.get_by_name",
                new=AsyncMock(return_value=MagicMock()),
            ),
            pytest.raises(AlreadyExistsError),
        ):
            await OrganizationSecretService(_db()).update(ctx, row.id, name="Taken")

    @pytest.mark.anyio
    async def test_a_rename_and_a_description_are_written_without_touching_the_value(self):
        ctx = _ctx()
        row = _row(ctx, ApiKeySecret(api_key="wx-key-0000"))

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.get_by_name",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.update",
                new=AsyncMock(return_value=row),
            ) as update,
            patch("app.services.organization_secret.record_audit", new=AsyncMock()) as audit,
        ):
            await OrganizationSecretService(_db()).update(
                ctx, row.id, name="Weather", description="Used by the forecast skill"
            )

        written = update.call_args.kwargs["update_data"]
        assert written == {"name": "Weather", "description": "Used by the forecast skill"}
        assert audit.call_args.kwargs["action"] == "secret.updated"

    @pytest.mark.anyio
    async def test_an_update_with_nothing_in_it_writes_nothing(self):
        ctx = _ctx()
        row = _row(ctx, ApiKeySecret(api_key="wx-key-0000"))

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.update",
                new=AsyncMock(),
            ) as update,
            patch("app.services.organization_secret.record_audit", new=AsyncMock()) as audit,
        ):
            assert await OrganizationSecretService(_db()).update(ctx, row.id) is row

        assert update.await_count == 0
        assert audit.await_count == 0

    @pytest.mark.anyio
    async def test_a_secret_from_another_organization_is_not_found(self):
        """The repository scopes the read, so a foreign id reads as absent."""
        secret_id = uuid.uuid4()
        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(NotFoundError) as refused,
        ):
            await OrganizationSecretService(_db()).update(_ctx(), secret_id, name="x")

        assert refused.value.details == {"secret_id": str(secret_id)}


class TestDeletingASecret:
    @pytest.mark.anyio
    async def test_a_deleted_secret_leaves_a_trail(self):
        ctx = _ctx()
        row = _row(ctx, ApiKeySecret(api_key="wx-key-0000"))

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.delete",
                new=AsyncMock(return_value=True),
            ) as delete,
            patch("app.services.organization_secret.record_audit", new=AsyncMock()) as audit,
        ):
            await OrganizationSecretService(_db()).delete(ctx, row.id)

        assert delete.call_args.kwargs["organization_id"] == ctx.organization_id
        assert audit.call_args.kwargs["action"] == "secret.deleted"
        # A deleted secret is the one record left of it; the value is not in it.
        assert audit.call_args.kwargs["details"] == {"name": row.name, "kind": row.kind}

    @pytest.mark.anyio
    async def test_deleting_one_that_is_not_ours_is_not_found(self):
        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.organization_secret.organization_secret_repo.delete",
                new=AsyncMock(),
            ) as delete,
            pytest.raises(NotFoundError),
        ):
            await OrganizationSecretService(_db()).delete(_ctx(), uuid.uuid4())

        assert delete.await_count == 0


class TestResolvingForARun:
    @pytest.mark.anyio
    async def test_a_referenced_secret_opens_for_its_own_organization(self):
        ctx = _ctx()
        row = _row(ctx, ApiKeySecret(api_key="wx-live-abcd4242"))

        with patch(
            "app.services.organization_secret.organization_secret_repo.get_many",
            new=AsyncMock(return_value={row.id: row}),
        ):
            resolved = await OrganizationSecretService(_db()).resolve_for_bindings(ctx, [row.id])

        assert resolved[row.id].api_key.get_secret_value() == "wx-live-abcd4242"

    @pytest.mark.anyio
    async def test_a_secret_sealed_for_another_organization_cannot_be_opened(self):
        """The tenant boundary the envelope exists to draw.

        The repository already filters by organization; this proves the second
        lock still holds if a row ever reached the wrong tenant anyway.
        """
        row = _row(_ctx(), ApiKeySecret(api_key="wx-live-abcd4242"))

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get_many",
                new=AsyncMock(return_value={row.id: row}),
            ),
            pytest.raises(BadRequestError, match="Failed to decrypt"),
        ):
            await OrganizationSecretService(_db()).resolve_for_bindings(_ctx(), [row.id])

    @pytest.mark.anyio
    async def test_an_id_that_no_longer_resolves_is_simply_absent(self):
        """Whether that matters is the registry's decision, not this one's.

        Only the registry knows which bindings actually required a secret, so a
        missing id becomes a refusal there rather than here.
        """
        with patch(
            "app.services.organization_secret.organization_secret_repo.get_many",
            new=AsyncMock(return_value={}),
        ):
            assert (
                await OrganizationSecretService(_db()).resolve_for_bindings(_ctx(), [uuid.uuid4()])
                == {}
            )

    @pytest.mark.anyio
    async def test_no_ids_means_no_query(self):
        db = _db()
        with patch(
            "app.services.organization_secret.organization_secret_repo.get_many",
            new=AsyncMock(return_value={}),
        ) as get_many:
            assert await OrganizationSecretService(db).resolve_for_bindings(_ctx(), []) == {}

        assert get_many.call_args.args[1] == []


class TestTheKeyThatAsksAProviderForItsCatalog:
    """`listing_key` is the vault's second reader, and the narrower one.

    It exists because the model form's suggestions come from the provider, and
    asking most providers costs a bearer token. The route used to open the vault
    itself - a route module importing the repository, `unseal_secret` and the
    scope - which is the layering #232 is about. What the plaintext is *for* has
    not changed: it goes to the provider, never to the caller.
    """

    @pytest.mark.anyio
    async def test_the_first_api_key_for_that_provider_is_used(self):
        ctx = _ctx()
        row = _row(ctx, ApiKeySecret(api_key="sk-openai-4242"))

        with patch(
            "app.services.organization_secret.organization_secret_repo.list_secrets",
            new=AsyncMock(return_value=[row]),
        ) as list_secrets:
            key = await OrganizationSecretService(_db()).listing_key(ctx, "openai")

        assert key == "sk-openai-4242"
        assert list_secrets.await_args.kwargs["purposes"] == ["openai"]

    @pytest.mark.anyio
    async def test_it_asks_only_for_this_organizations_keys(self):
        """The scope is the service's answer now, not a handler's argument."""
        ctx = _ctx()

        with patch(
            "app.services.organization_secret.organization_secret_repo.list_secrets",
            new=AsyncMock(return_value=[]),
        ) as list_secrets:
            await OrganizationSecretService(_db()).listing_key(ctx, "openai")

        assert list_secrets.await_args.kwargs["organization_id"] == ctx.organization_id

    @pytest.mark.anyio
    async def test_a_credential_that_is_not_a_bearer_token_is_skipped(self):
        """An AWS pair is a signing credential; no listing endpoint accepts one.

        Skipped rather than mangled into a header - and skipped rather than
        returned, so a stored pair does not hide the API key behind it.
        """
        ctx = _ctx()
        pair = _row(
            ctx,
            AwsCredentialsSecret(
                aws_access_key_id="AKIA4242",
                aws_secret_access_key="s3cret-key",
                region_name="us-east-1",
            ),
        )
        key = _row(ctx, ApiKeySecret(api_key="sk-openai-4242"))

        with patch(
            "app.services.organization_secret.organization_secret_repo.list_secrets",
            new=AsyncMock(return_value=[pair, key]),
        ):
            found = await OrganizationSecretService(_db()).listing_key(ctx, "bedrock")

        assert found == "sk-openai-4242"

    @pytest.mark.anyio
    async def test_no_stored_key_is_not_an_error(self):
        """Providers that publish a public catalog are asked without one."""
        with patch(
            "app.services.organization_secret.organization_secret_repo.list_secrets",
            new=AsyncMock(return_value=[]),
        ):
            assert await OrganizationSecretService(_db()).listing_key(_ctx(), "openrouter") is None


class TestTheKeyATranscriptionIsPaidWith:
    """`transcription_key`: the key named next to a speech-to-text pair (#1797).

    It opens the one key it is given and chooses none: no lookup by provider,
    no "first by name". A key that is gone, of another kind, for another
    provider, or - where the platform spends it for everybody - no longer
    organization-visible, is not usable, and nothing is opened in its place.
    The organization filter is held against a real database in
    `tests/integration/test_secret_tenant_isolation.py`.
    """

    @staticmethod
    def _speech_row(ctx, *, purpose="openai", visibility="org", value=None):
        row = _row(ctx, value or ApiKeySecret(api_key="sk-named-4242"), name="Speech")
        row.purpose = purpose
        row.visibility = visibility
        return row

    @pytest.mark.anyio
    async def test_the_named_key_is_opened_for_its_provider(self):
        ctx = _ctx()
        row = self._speech_row(ctx)

        with patch(
            "app.services.organization_secret.organization_secret_repo.get",
            new=AsyncMock(return_value=row),
        ) as got:
            key = await OrganizationSecretService(_db()).transcription_key(
                ctx.organization_id, row.id, provider="openai", shared=True
            )

        assert key == "sk-named-4242"
        assert got.await_args.args[1] == row.id
        assert got.await_args.kwargs == {"organization_id": ctx.organization_id}

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("change", "shared"),
        [
            ({"purpose": "groq"}, False),
            ({"kind": "azure_openai"}, False),
            ({"visibility": "private"}, True),
            ({"visibility": "team"}, True),
            ("missing", False),
        ],
        ids=["another-provider", "another-kind", "private-shared", "team-shared", "gone"],
    )
    async def test_a_key_that_is_not_usable_is_not_opened(self, change, shared: bool):
        ctx = _ctx()
        row = self._speech_row(ctx)
        if change == "missing":
            row = None
        else:
            for field, value in change.items():
                setattr(row, field, value)

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=row),
            ),
            patch("app.services.organization_secret.unseal_secret") as unseal,
        ):
            key = await OrganizationSecretService(_db()).transcription_key(
                ctx.organization_id, uuid.uuid4(), provider="openai", shared=shared
            )

        assert key is None
        unseal.assert_not_called()

    @pytest.mark.anyio
    async def test_a_private_key_lent_by_an_agent_is_usable(self):
        """Not shared: its publisher could view it, which publish checked."""
        ctx = _ctx()
        row = self._speech_row(ctx, visibility="private")

        with patch(
            "app.services.organization_secret.organization_secret_repo.get",
            new=AsyncMock(return_value=row),
        ):
            found = await OrganizationSecretService(_db()).usable_transcription_key(
                ctx.organization_id, row.id, provider="openai", shared=False
            )

        assert found is row

    @pytest.mark.anyio
    @pytest.mark.security
    async def test_a_key_sealed_for_another_organization_does_not_open(self):
        """Cryptographic, not a filter: the envelope is bound to its owner."""
        foreign = self._speech_row(_ctx())

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=foreign),
            ),
            pytest.raises(BadRequestError),
        ):
            await OrganizationSecretService(_db()).transcription_key(
                uuid.uuid4(), foreign.id, provider="openai", shared=True
            )


class TestTheGithubOAuthAppReader:
    """`github_oauth_app` is the vault's third reader, and the most narrowly scoped.

    It opens one kind of secret and lets only the public `client_id` escape; the
    `client_secret` it unseals goes to the provider's token endpoint inside the
    connect flow and nowhere else. It exists so a connect flow can run its token
    exchange with credentials the organization stored, without ever handing them to
    a route or a client.

    One reader over two kinds - a repository account and a mailbox - because the
    rule is the same for both, and it is the rule that matters: a client read from
    the deployment's environment would be a second mechanism for a credential at
    rest, which is what Gmail briefly had.
    """

    @pytest.mark.anyio
    async def test_it_unseals_the_organizations_github_oauth_app(self):
        ctx = _ctx()
        row = _row(
            ctx,
            GithubOAuthAppSecret(client_id="Iv1.0123456789abcdef", client_secret="ghsec-42"),
            name="GitHub OAuth App",
        )

        with patch(
            "app.services.organization_secret.organization_secret_repo.list_org_visible_by_kind",
            new=AsyncMock(return_value=[row]),
        ) as by_kind:
            value = await OrganizationSecretService(_db()).oauth_app(
                ctx, kind=SecretKind.GITHUB_OAUTH_APP
            )

        assert isinstance(value, GithubOAuthAppSecret)
        assert value.client_id == "Iv1.0123456789abcdef"
        assert value.client_secret.get_secret_value() == "ghsec-42"
        # Read org-scoped and by the GitHub kind - never another tenant's, never
        # another shape.
        assert by_kind.await_args.kwargs == {
            "organization_id": ctx.organization_id,
            "kind": SecretKind.GITHUB_OAUTH_APP.value,
        }

    @pytest.mark.anyio
    async def test_a_missing_secret_is_a_clean_not_found_not_a_500(self):
        """The connect UI shows this as "add a GitHub OAuth App secret first"."""
        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.list_org_visible_by_kind",
                new=AsyncMock(return_value=[]),
            ),
            pytest.raises(NotFoundError),
        ):
            await OrganizationSecretService(_db()).oauth_app(
                _ctx(), kind=SecretKind.GITHUB_OAUTH_APP
            )

    @pytest.mark.anyio
    async def test_it_reads_a_google_client_by_its_own_kind(self):
        """The same reader, the other credential - and read by the kind it was
        asked for, so a stored GitHub app can never be spent as a mailbox client."""
        ctx = _ctx()
        row = _row(
            ctx,
            GoogleOAuthAppSecret(
                client_id="1234-abc.apps.googleusercontent.com", client_secret="gsec-4242"
            ),
            name="Google OAuth client",
        )

        with patch(
            "app.services.organization_secret.organization_secret_repo.list_org_visible_by_kind",
            new=AsyncMock(return_value=[row]),
        ) as by_kind:
            value = await OrganizationSecretService(_db()).oauth_app(
                ctx, kind=SecretKind.GOOGLE_OAUTH_APP
            )

        assert isinstance(value, GoogleOAuthAppSecret)
        assert value.client_secret.get_secret_value() == "gsec-4242"
        assert by_kind.await_args.kwargs["kind"] == SecretKind.GOOGLE_OAUTH_APP.value

    @pytest.mark.anyio
    async def test_two_org_visible_apps_are_refused_not_picked_by_name_order(self):
        """With two org-visible OAuth Apps stored, the connect flow must not key
        the organization's GitHub connection to whichever name sorts first - the
        refusal names both candidates so the operator knows which rows collide,
        and no secret value rides in it."""
        ctx = _ctx()
        rows = [
            _row(
                ctx, GithubOAuthAppSecret(client_id="Iv1.a", client_secret="secret-1"), name="aaa"
            ),
            _row(
                ctx, GithubOAuthAppSecret(client_id="Iv1.b", client_secret="secret-2"), name="bbb"
            ),
        ]
        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.list_org_visible_by_kind",
                new=AsyncMock(return_value=rows),
            ),
            pytest.raises(BadRequestError) as excinfo,
        ):
            await OrganizationSecretService(_db()).oauth_app(ctx, kind=SecretKind.GITHUB_OAUTH_APP)
        assert excinfo.value.details["names"] == ["aaa", "bbb"]
        assert "s1" not in str(excinfo.value.details)


class TestTheKeyACallIsHungUpWith:
    """`voice_call_key`: the key a voice call was held with, opened only to hang it up (#54)."""

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "value",
        [
            ApiKeySecret(api_key="sk-voice-4242"),
            AzureOpenAISecret(
                api_key="azure-voice-4242",
                azure_endpoint="https://x.openai.azure.com",
                api_version="2025-04-01-preview",
            ),
        ],
        ids=["api-key", "azure"],
    )
    async def test_a_voice_key_unseals_only_in_its_own_organization(self, value):
        ctx = _ctx()
        row = _row(ctx, value, name="Voice")

        with patch(
            "app.services.organization_secret.organization_secret_repo.get",
            new=AsyncMock(return_value=row),
        ) as got:
            opened = await OrganizationSecretService(_db()).voice_call_key(
                ctx.organization_id, row.id
            )

        assert opened == value
        assert got.await_args.kwargs == {"organization_id": ctx.organization_id}

        with (
            patch(
                "app.services.organization_secret.organization_secret_repo.get",
                new=AsyncMock(return_value=row),
            ),
            pytest.raises(BadRequestError),
        ):
            await OrganizationSecretService(_db()).voice_call_key(uuid.uuid4(), row.id)

    @pytest.mark.anyio
    async def test_a_deleted_voice_key_reads_as_none(self):
        with patch(
            "app.services.organization_secret.organization_secret_repo.get",
            new=AsyncMock(return_value=None),
        ):
            assert (
                await OrganizationSecretService(_db()).voice_call_key(uuid.uuid4(), uuid.uuid4())
                is None
            )
