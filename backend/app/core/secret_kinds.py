"""The shapes a stored secret can have, and how each one is sealed.

A secret is not always a string. An AWS key pair is an id, a secret and a
region; an Azure OpenAI deployment is a key plus an endpoint and an API version;
a Vertex credential is a service account document. Forcing all of them into one
"API key" field produces a form somebody can fill in correctly and still end up
with a credential that fails at the first run - which is why a secret has a
*kind*, and why the kind decides which fields exist.

The kinds are the shapes that actually exist, and no more:

`none`
    Not a secret at all - the marker for an endpoint that needs no credential,
    which is the normal case for a self-hosted model server. It exists so the
    resolver can switch on a total set instead of treating "no credential" as a
    missing value, and it is the answer to "what do you store for Ollama on
    localhost"; an empty string is not, and the vault refuses one.
`api_key`
    One opaque token. All but three of the model providers want exactly this,
    and so does every third-party API a custom capability might call.
`azure_openai`
    Azure routes by deployment endpoint and pins an API version, so both travel
    with the key or the credential is unusable.
`aws_credentials`
    An access key id, a secret access key and a region - plus an optional
    session token for STS. The id is not secret and the secret is; a single
    field cannot express that.
`gcp_service_account`
    The service account JSON. Validated on the way in, because the failure mode
    of a malformed one is an authentication error hours later with nothing
    pointing back at the paste that caused it.
`github_oauth_app`
    A GitHub OAuth App's `client_id` and `client_secret`. The id is public and
    the secret is not - a single field cannot express that, the same reason
    `aws_credentials` is a pair.
`google_oauth_app`
    A Google OAuth client's `client_id` and `client_secret`, for connecting a
    mailbox a trigger reads. The same two fields as GitHub's and a separate kind
    on purpose: a kind names what a credential is *for*.
`ssh_credentials`
    An SSH login an SFTP sync source reads a folder with: a user name with a
    private key or a password, and the one host it may be used with. The host
    is sealed with the login, as a Git token's is, so a source's editor cannot
    send it to a server of their own. The key is read on the way in, for the
    same reason a service account is.
`entra_app`
    A Microsoft Entra app registration - tenant, client id and client secret -
    that a SharePoint or OneDrive sync source reads a site with. Three fields,
    because a client secret is useless without the tenant and the app it
    belongs to.
`openai_codex`
    A ChatGPT sign-in for the Codex backend: an access token, the refresh token
    that renews it and the ChatGPT account both belong to. Not typed by anybody -
    the sign-in flow in `app/services/openai_codex.py` writes it, and the
    runtime writes it again each time the tokens rotate, because a refresh token
    is spent by the refresh that replaces it.

Two unions, deliberately. :data:`StorableSecret` is what a person can save;
:data:`SecretValue` adds `none`, which the runtime can hold but nobody can
store. That difference is what keeps "a secret with no value" out of the API
schema.
"""

import json
import re
from dataclasses import replace
from enum import StrEnum
from typing import Annotated, Any, Literal, NamedTuple

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from app.core.exceptions import BadRequestError
from app.core.sanitize import spelled_host
from app.core.vault import SealedSecret, VaultScope, seal, unseal

# The child process that opens a key imports nothing of the application, so the
# catch set and the report live in its module. `KEY_PARSE_ERRORS` is named here
# too, for the code that already imports it from this module.
from app.services.ssh_key_check import KEY_PARSE_ERRORS, MAX_RSA_BITS, KeyReport


class SecretKind(StrEnum):
    """Which shape a stored secret has. Part of the API contract and of specs."""

    NONE = "none"
    API_KEY = "api_key"
    AZURE_OPENAI = "azure_openai"
    AWS_CREDENTIALS = "aws_credentials"
    GCP_SERVICE_ACCOUNT = "gcp_service_account"
    GITHUB_OAUTH_APP = "github_oauth_app"
    GITHUB_APP = "github_app"
    GOOGLE_OAUTH_APP = "google_oauth_app"
    GIT_TOKEN = "git_token"
    SSH_CREDENTIALS = "ssh_credentials"
    ENTRA_APP = "entra_app"
    OPENAI_CODEX = "openai_codex"


def _reveal(value: SecretStr) -> str:
    return value.get_secret_value()


# A secret field that masks itself in every repr - the way a plaintext key
# usually escapes is a dataclass or a Pydantic model being logged whole - but
# serialises to its real value in JSON, which is the one place it has to: on the
# way into the vault. `model_dump()` still yields the SecretStr, so the accident
# of dumping a model into a log line stays harmless.
SealedStr = Annotated[SecretStr, PlainSerializer(_reveal, when_used="json")]

MIN_CREDENTIAL_LENGTH = 8
"""The shortest value a credential field accepts.

:attr:`_SecretBase.hint` shows the last four characters of a credential to
everyone who may list secrets and writes them into the audit trail, so a value
shorter than this would be published whole by its own hint. The floor also
catches a truncated paste while the form is still open, rather than at the
first run.
"""


def _long_enough_to_hint(value: SecretStr) -> SecretStr:
    if len(value) < MIN_CREDENTIAL_LENGTH:
        raise ValueError(
            f"Must be at least {MIN_CREDENTIAL_LENGTH} characters - is the paste complete?"
        )
    return value


# A sealed field that authenticates: long enough that the four-character hint
# cannot give it away. `minLength` is declared on the schema the forms are
# generated from, and the validator carries the message a form can show.
CredentialStr = Annotated[
    SealedStr,
    AfterValidator(_long_enough_to_hint),
    Field(json_schema_extra={"minLength": MIN_CREDENTIAL_LENGTH}),
]


class _SecretBase(BaseModel):
    """Common configuration for every secret payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def hint(self) -> str:
        """Four characters an operator can recognise this credential by.

        Never the whole credential: every field that authenticates is a
        :data:`CredentialStr`, at least :data:`MIN_CREDENTIAL_LENGTH` long.
        Taken from the field that identifies the credential rather than from the
        one that authenticates it where the two differ - an AWS access key id is
        public, and showing four characters of it is strictly better than
        showing four of the secret access key.
        """
        raise NotImplementedError


class NoSecret(_SecretBase):
    """No credential at all - a model server reached over the network unauthenticated."""

    kind: Literal[SecretKind.NONE] = SecretKind.NONE

    @property
    def hint(self) -> str:
        return ""


class ApiKeySecret(_SecretBase):
    """One opaque token."""

    kind: Literal[SecretKind.API_KEY] = SecretKind.API_KEY
    api_key: CredentialStr = Field(title="API key", description="The token, sealed before storage")

    @property
    def hint(self) -> str:
        return self.api_key.get_secret_value()[-4:]


class AzureOpenAISecret(_SecretBase):
    """An Azure OpenAI deployment: key, endpoint and pinned API version."""

    kind: Literal[SecretKind.AZURE_OPENAI] = SecretKind.AZURE_OPENAI
    api_key: CredentialStr = Field(title="API key")
    azure_endpoint: str = Field(
        min_length=1,
        title="Endpoint",
        description="e.g. https://my-resource.openai.azure.com",
    )
    api_version: str = Field(min_length=1, title="API version", description="e.g. 2024-10-21")

    @property
    def hint(self) -> str:
        return self.api_key.get_secret_value()[-4:]


class AwsCredentialsSecret(_SecretBase):
    """An AWS key pair and the region its models are served from."""

    kind: Literal[SecretKind.AWS_CREDENTIALS] = SecretKind.AWS_CREDENTIALS
    aws_access_key_id: str = Field(min_length=1, title="Access key ID")
    aws_secret_access_key: CredentialStr = Field(title="Secret access key")
    region_name: str = Field(min_length=1, title="Region", description="e.g. us-east-1")
    aws_session_token: CredentialStr | None = Field(
        default=None,
        title="Session token",
        description="Only for temporary STS credentials",
    )

    @property
    def hint(self) -> str:
        # The access key id, not the secret: it is not confidential, and it is
        # what an operator sees in the AWS console next to the same key.
        return self.aws_access_key_id[-4:]


# What a service account document must carry to be usable as one.
_SERVICE_ACCOUNT_FIELDS = ("client_email", "private_key", "project_id")


class GcpServiceAccountSecret(_SecretBase):
    """A Google Cloud service account, as downloaded from the console."""

    kind: Literal[SecretKind.GCP_SERVICE_ACCOUNT] = SecretKind.GCP_SERVICE_ACCOUNT
    service_account_json: SealedStr = Field(
        title="Service account JSON", description="The whole downloaded JSON document"
    )
    location: str | None = Field(
        default=None,
        title="Region",
        description="Vertex region, e.g. europe-west4; omit for the provider default",
    )

    @field_validator("service_account_json")
    @classmethod
    def _is_a_service_account_document(cls, value: SecretStr) -> SecretStr:
        """Refuse anything that is not a service account key, while a form is open.

        A truncated paste or the wrong kind of Google credential is otherwise
        indistinguishable from a working one until a run fails, by which point
        nothing points at the paste.
        """
        try:
            document = json.loads(value.get_secret_value())
        except json.JSONDecodeError as exc:
            raise ValueError("Service account credentials must be the downloaded JSON") from exc
        if not isinstance(document, dict) or document.get("type") != "service_account":
            raise ValueError(
                "This is not a service account key - its 'type' is not service_account"
            )
        missing = [field for field in _SERVICE_ACCOUNT_FIELDS if not document.get(field)]
        if missing:
            raise ValueError(f"Service account key is missing: {', '.join(missing)}")
        return value

    @property
    def document(self) -> dict[str, Any]:
        """The parsed service account, for building a Google credential."""
        parsed: dict[str, Any] = json.loads(self.service_account_json.get_secret_value())
        return parsed

    @property
    def project(self) -> str:
        """The project the service account belongs to, taken from the document.

        Not a separate field: two places to state the project is one place for
        them to disagree, and the document is the one the provider authenticates
        against.
        """
        project: str = self.document["project_id"]
        return project

    @property
    def hint(self) -> str:
        email: str = self.document["client_email"]
        return email[-4:]


# The host a credential is bound to, as a person types it: a name or an
# IPv4 address, or an IPv6 address in brackets, then an optional port. Without
# the brackets, an IPv6 address's last group would read as a port.
_BOUND_HOST_PATTERN = r"^([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?$"


def _bound_host_allows(bound: str, hostname: str, port: int | None, *, default_port: int) -> bool:
    """Whether a credential bound to `bound` may be sent to this host and port.

    `hostname` as `urlsplit` answers it: lower-cased, and an IPv6 literal
    without its brackets - which the stored host keeps, as a URL writes it.
    The protocol's own port may be written or left out on either side, so
    `bound` matches with or without `:<default_port>`.
    """
    host = f"[{hostname}]" if ":" in hostname else hostname
    expected = host if port in (None, default_port) else f"{host}:{port}"
    return bound.lower().removesuffix(f":{default_port}") == expected.lower()


class GitTokenSecret(_SecretBase):
    """An access token for git over HTTPS, and the one host it may be sent to.

    A kind of its own rather than an `api_key`, because a Git source sends its
    token to a URL the source's editor types. With any API key eligible, a member
    who could edit a source could pick the organization's model key, point the
    source at a server of their own and read the key out of the basic-auth header
    (#987). Here the host is part of what the vault holds - set by whoever added
    the token, sealed with it - and the connector refuses to send the token
    anywhere else.
    """

    kind: Literal[SecretKind.GIT_TOKEN] = SecretKind.GIT_TOKEN
    token: CredentialStr = Field(
        title="Access token",
        description="A token that can read the repository, e.g. a fine-grained GitHub token",
    )
    host: str = Field(
        min_length=1,
        max_length=253,
        pattern=_BOUND_HOST_PATTERN,
        title="Host",
        description="Where the token may be sent, e.g. github.com or gitlab.example.com:8443",
    )

    @property
    def hint(self) -> str:
        return self.token.get_secret_value()[-4:]

    def allows(self, hostname: str, port: int | None) -> bool:
        """Whether a repository at this host and port may be sent the token.

        `hostname` as `urlsplit` answers it: lower-cased, and an IPv6 literal
        without its brackets - which the stored host keeps, as a URL writes it.
        An internationalized name arrives IDNA-encoded (`xn--...`), the only
        spelling `host` can store: `GitConfig` encodes the URL's host before
        anything reads it.
        """
        return _bound_host_allows(self.host, hostname, port, default_port=443)


MAX_KEY_ROUNDS = 128
"""The most bcrypt rounds an encrypted OpenSSH private key may ask for.

An encrypted key names its own key derivation cost, and that cost is paid in
full before a wrong passphrase shows: about 5 ms a round, in a field that holds
up to 2**32 - 1. `ssh-keygen` writes 16, and `-a 100` is common advice.
"""

MAX_KEY_ITERATIONS = 1_000_000
"""The most iterations an encrypted PKCS#8 private key's key derivation may ask for.

The same cost, named in the key's DER. OpenSSL and asyncssh write 2,048.
"""

MAX_PRIVATE_KEY_CHARACTERS = 16 * 1024
"""The longest private key an SSH login may hold, as pasted.

An RSA key of 4096 bits is about 3.3 KB in any form asyncssh reads, encrypted
included. The limit holds before anything decodes the paste: the DER decoder
the header check runs is quadratic in its input, and a crafted body of a few MB
held the event loop for seconds, in a request no permission check had seen yet.
"""

MAX_PASSWORD_CHARACTERS = 1024
"""The longest passphrase or password an SSH login may hold.

Both go to the child process's stdin, and the passphrase into the key
derivation, which reads it whole.
"""

# The refusal of an over-long field, by field: its limit and our sentence.
_LENGTH_LIMITS = {
    "private_key": (
        MAX_PRIVATE_KEY_CHARACTERS,
        "The private key is too long. Paste only the key file, at most "
        f"{MAX_PRIVATE_KEY_CHARACTERS:,} characters.",
    ),
    "passphrase": (
        MAX_PASSWORD_CHARACTERS,
        f"The passphrase is too long: at most {MAX_PASSWORD_CHARACTERS:,} characters.",
    ),
    "password": (
        MAX_PASSWORD_CHARACTERS,
        f"The password is too long: at most {MAX_PASSWORD_CHARACTERS:,} characters.",
    ),
}

MAX_PBKDF2_KEY_BYTES = 64
"""The longest key an encrypted PKCS#8 private key's PBKDF2 may be asked to derive.

PBKDF2 runs its whole iteration count once for each block of hash output, so a
key length multiplies the iterations. No cipher asyncssh decrypts a key with
takes more than 32 bytes.
"""

_UNREADABLE_KEY = (
    "The private key could not be read. Paste the whole key file, and give its "
    "passphrase if it has one."
)
_WRONG_PASSPHRASE = "The private key could not be read with this passphrase."
_COSTLY_KEY = (
    "The private key is encrypted with too many key derivation rounds. Encrypt it again "
    "with fewer, e.g. with ssh-keygen -p -a 16 -f <key file>."
)
_SLOW_KEY = (
    "The private key takes too long to open. Use an Ed25519 key, or an RSA key of at "
    f"most {MAX_RSA_BITS} bits."
)
_LARGE_RSA_KEY = (
    f"The private key is an RSA key of more than {MAX_RSA_BITS} bits. Use an Ed25519 key, "
    f"or an RSA key of at most {MAX_RSA_BITS} bits."
)
_UNBALANCED_RSA_KEY = (
    "The private key is an RSA key whose two primes are not of the same size. Use an "
    "Ed25519 key, or an RSA key that ssh-keygen made."
)
_SECURITY_KEY = (
    "The private key is held on a security key, which a sync cannot use to log in. Use "
    "an Ed25519 key without a security key."
)

_OPENSSH_MAGIC = b"openssh-key-v1\0"
# The private key blocks asyncssh reads, by their BEGIN line: OpenSSH, PKCS#8
# encrypted and not, and the three PKCS#1 ones.
_READABLE_BLOCKS = frozenset(
    f"-----BEGIN {name}PRIVATE KEY-----".encode()
    for name in ("OPENSSH ", "ENCRYPTED ", "", "RSA ", "EC ", "DSA ")
)


class _KeyHeader(NamedTuple):
    encrypted: bool
    too_costly: bool


def _pbe_work(tree: object) -> tuple[int, int]:
    """The largest integer, and the longest PBKDF2 key length, in a decoded DER tree.

    A PBKDF2 parameter SEQUENCE is the salt, the iteration count, then an
    optional key length. That is the only SEQUENCE in an encrypted key's
    algorithm parameters that starts with an OCTET STRING and two INTEGERs, so
    its third element is read as a key length wherever it sits. A loop, not
    recursion: the tree may be as deep as the decoder allowed.
    """
    largest, key_length, pending = 0, 0, [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, tuple):
            pending.extend(node)
            if (
                len(node) >= 3
                and isinstance(node[0], bytes)
                and isinstance(node[1], int)
                and isinstance(node[2], int)
            ):
                key_length = max(key_length, node[2])
        elif isinstance(node, int):
            largest = max(largest, node)
    return largest, key_length


def _first_private_key_line(data: bytes) -> bytes | None:
    """The BEGIN line asyncssh's block finder picks: the first that names a private key.

    asyncssh splits on line feeds only and strips each line's end, so this does
    the same.
    """
    for part in data.split(b"\n"):
        line = part.rstrip()
        if line.startswith(b"-----BEGIN ") and line.endswith(b" PRIVATE KEY-----"):
            return line
    return None


def _key_header(key: str) -> _KeyHeader:
    """Whether a pasted private key is encrypted, and whether it asks too much work.

    Read from the key's header, without decrypting anything, so it costs the
    same whatever the key asks for. asyncssh's own block finder picks the block,
    so this reads the block that `import_private_key` will decrypt. It refuses
    the work factors a header shows, so most costly keys are refused before any
    process starts. It cannot bound the rest: an RSA key's validation, and the
    numbers inside the encrypted part, show only when the key is opened. That is
    `app.services.ssh_key_check`'s child process and its time limit. In
    particular:

    - The block's BEGIN line must name one of the blocks asyncssh reads, before
      its block finder runs. It builds a pattern out of that line without
      escaping it, so another label could fail to compile, or backtrack for
      hours on a crafted footer.
    - An OpenSSH key's bcrypt rounds are its `kdfoptions` after the salt.
    - An encrypted PKCS#8 key's iteration count is an integer in its algorithm
      parameters. Each integer there is held to the ceiling, so no layout of
      PBES1, PBES2 or PKCS#12 parameters can hide one. Its PBKDF2 key length,
      which multiplies the iterations, is held to :data:`MAX_PBKDF2_KEY_BYTES`.
    - A PKCS#8 block with a PEM encryption header is refused: asyncssh would
      decrypt the body first and read the count out of what it decrypted.
    - DER is refused: a paste is text. And no PKCS#8 encryption algorithm can be
      named in DER made of ASCII characters, so asyncssh's DER branch could not
      derive a key from one anyway.

    asyncssh is imported here, not at module level: this module loads with every
    API process, and asyncssh is needed only where an SSH login is checked or
    used (#2225).

    Raises:
        ValueError: Or another of :data:`KEY_PARSE_ERRORS`, when the paste holds
            no private key block that asyncssh would read.
    """
    from asyncssh.asn1 import der_decode
    from asyncssh.packet import SSHPacket
    from asyncssh.public_key import _match_next

    data = key.encode("ascii")
    if _first_private_key_line(data) not in _READABLE_BLOCKS:
        raise ValueError("No private key block asyncssh reads")
    form, block, _ = _match_next(data, b"PRIVATE KEY")
    if form != "pem":
        raise ValueError("No PEM private key block")
    name, headers, body = block
    if name == b"OPENSSH":
        if not body.startswith(_OPENSSH_MAGIC):
            raise ValueError("Not an OpenSSH private key")
        packet = SSHPacket(body[len(_OPENSSH_MAGIC) :])
        cipher, _, kdf_options = packet.get_string(), packet.get_string(), packet.get_string()
        if cipher == b"none":
            return _KeyHeader(encrypted=False, too_costly=False)
        # bcrypt's options: a salt, then the rounds. asyncssh refuses any other
        # key derivation, so this reads the rounds whichever one is named.
        options = SSHPacket(kdf_options)
        options.get_string()
        return _KeyHeader(encrypted=True, too_costly=options.get_uint32() > MAX_KEY_ROUNDS)
    if name == b"ENCRYPTED":
        if headers.get(b"Proc-Type") == b"4,ENCRYPTED":
            raise ValueError("PKCS#8 under a PEM encryption header")
        iterations, key_length = _pbe_work(der_decode(body))
        return _KeyHeader(
            encrypted=True,
            too_costly=iterations > MAX_KEY_ITERATIONS or key_length > MAX_PBKDF2_KEY_BYTES,
        )
    if name == b"":
        return _KeyHeader(encrypted=False, too_costly=False)
    # PKCS#1: its encryption derives the key with one round of MD5, whatever the
    # block says, so it has no cost to bound.
    return _KeyHeader(encrypted=headers.get(b"Proc-Type") == b"4,ENCRYPTED", too_costly=False)


def _key_header_refusal(key: str, passphrase: str | None) -> tuple[str, str] | None:
    """The cheap key checks: the framing, the passphrase, the ceiling.

    Returns the field the refusal is about and our sentence, as
    :meth:`SshCredentialsSecret.opening_refusal` does for the child's checks, so
    one mistake reads the same whichever check finds it. A parser's own text
    can quote what it failed on - a `UnicodeDecodeError` carries the bytes it
    could not decode, which are key material - so it is dropped with its
    exception.
    """
    try:
        header = _key_header(key)
    except KEY_PARSE_ERRORS:
        if passphrase is None:
            return "private_key", _UNREADABLE_KEY
        return "passphrase", _WRONG_PASSPHRASE
    if header.too_costly:
        return "private_key", _COSTLY_KEY
    if header.encrypted and passphrase is None:
        return "private_key", _UNREADABLE_KEY
    return None


def _refused_on(field: str, sentence: str) -> ValidationError:
    """Our sentence as a validation error located at `field`, with no input.

    A model validator's own `ValueError` is located at the model, so the form
    that sent it marks no input and shows the sentence in a toast, behind
    pydantic's "Value error, " prefix. Located at the field, the form marks the
    input, and the sentence reads as written. No value travels with it.
    """
    return ValidationError.from_exception_data(
        "SshCredentialsSecret",
        [
            InitErrorDetails(
                type=PydanticCustomError("refused", "{sentence}", {"sentence": sentence}),
                loc=(field,),
                input=None,
            )
        ],
    )


# The host an SSH login is bound to, and its port when one is written: a name or
# an IPv4 address, or an IPv6 address in brackets.
_HOST_AND_PORT = re.compile(r"(?P<host>\[[^\]]*\]|[^:\[\]]*)(?::(?P<port>[0-9]{1,5}))?")
_NOT_A_BOUND_HOST = (
    "The host is a host name or an IP address with an optional port, e.g. "
    "sftp.example.com or [2001:db8::7]:2222. An IPv6 address goes in brackets."
)


class SshCredentialsSecret(_SecretBase):
    """An SSH login for an SFTP server, and the one host it may be used with.

    The host is bound for the reason a Git token's is (#987). The source's
    editor types the server's address, so with a login that any host could be
    sent, a member who could edit a source could point it at a server of their
    own and receive the password. Here the host is sealed with the login, set
    by whoever added it, and the connector refuses to log in anywhere else.

    The hint is the end of the user name. The user name says which login this
    is and is not secret, as an AWS access key id is not, and four characters
    of a key would be its PEM armour.

    `passphrase` is a `SealedStr`, not a `CredentialStr`: the hint does not
    read it, and `ssh-keygen` accepts a passphrase of five characters. It only
    opens the key, which is itself at least :data:`MIN_CREDENTIAL_LENGTH` long.

    The key is checked in two halves. Parsing a login checks only what is cheap:
    the field lengths first (:data:`MAX_PRIVATE_KEY_CHARACTERS` and
    :data:`MAX_PASSWORD_CHARACTERS`), so nothing decodes an over-long paste, then
    that the paste is a private key, that an encrypted one has a passphrase, and
    that the work factors its header shows are within
    :data:`MAX_KEY_ROUNDS`, :data:`MAX_KEY_ITERATIONS` and
    :data:`MAX_PBKDF2_KEY_BYTES`. Pydantic parses a login in a request body
    before the route has authorized anything, and each time the vault opens a
    stored one. Opening the key is the other half: `OrganizationSecretService`
    opens it in a child process with a time limit when it stores the login
    (`app.services.ssh_key_check`), and :meth:`opening_refusal` reads the
    child's report. So a paste that cannot log in is refused while the form is
    open: one that does not open, one held on a security key, which the worker
    cannot sign with. And a stored key is one that opened within the limit, as
    an RSA key of at most :data:`MAX_RSA_BITS` bits with primes of one size, as
    `ssh-keygen` makes them. That bounds what the connector pays to open it again
    at each connection: well under a second.
    """

    # A validator's error carries the whole input in `str()` and `repr()`, cut to
    # its head and tail. The passphrase is the last field, so it would be shown
    # whole, and the end of the key with it. Pydantic reads this setting from the
    # outermost model only, so it covers a login built directly, and
    # `SecretCreate`, `SecretUpdate` and the vault's adapter set it as well.
    model_config = ConfigDict(hide_input_in_errors=True)

    kind: Literal[SecretKind.SSH_CREDENTIALS] = SecretKind.SSH_CREDENTIALS
    username: str = Field(
        min_length=1,
        max_length=255,
        # The user name goes into refusals, the sync log and log lines, where a
        # line break or a control character, C1 included, could forge a line or
        # start a terminal escape.
        pattern=r"^[^\s\x00-\x1f\x7f-\x9f]+$",
        title="User name",
        description="The account on the SFTP server",
    )
    host: str = Field(
        min_length=1,
        # A name of 253 characters once IDNA-encoded, and a port. The vault parses
        # the stored, encoded host again each time it opens the login, so the
        # limit holds the encoded form, which `_spelled_as_a_source_host` keeps
        # to 253 characters before the port.
        max_length=253 + len(":65535"),
        title="Host",
        description="Where this login may be used, e.g. sftp.example.com or sftp.example.com:2222",
    )
    private_key: CredentialStr | None = Field(
        default=None,
        max_length=MAX_PRIVATE_KEY_CHARACTERS,
        title="Private key",
        description="The whole key file, in OpenSSH, PKCS#8 or PKCS#1 form. Preferred to a password",
        # A key has line breaks. The vault form is generated from this schema,
        # and a `CredentialStr` alone renders as `<input type="password">`,
        # where a browser strips them, so the key that reaches the vault cannot
        # be read. The same reason as `GithubAppSecret.private_key`.
        json_schema_extra={"x-textarea": True},
    )
    passphrase: SealedStr | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_PASSWORD_CHARACTERS,
        title="Key passphrase",
        description="Only for an encrypted private key",
    )
    password: CredentialStr | None = Field(
        default=None,
        max_length=MAX_PASSWORD_CHARACTERS,
        title="Password",
        description="Only for a server that takes no key",
    )

    @property
    def hint(self) -> str:
        return self.username[-4:]

    def allows(self, hostname: str, port: int) -> bool:
        """Whether an SFTP source at this host and port may log in with this login.

        `hostname` as `SftpConfig` spells it: lower-cased, IDNA-encoded, and an
        IPv6 literal without its brackets and compressed. The stored host is
        spelled the same way on the way in. Port 22 is SSH's own, so a login
        stored for `sftp.example.com` is allowed on port 22 only.
        """
        return _bound_host_allows(self.host, hostname, port, default_port=22)

    def opening_refusal(self, report: KeyReport | None) -> tuple[str, str] | None:
        """Why the key cannot be stored, by what the child that opened it reported.

        `report` is `None` when the child ran out of time. Returns the field the
        refusal is about - `passphrase` for a passphrase that does not open the
        key, else `private_key` - and our sentence, or `None` when the key opened.
        """
        if report is None:
            return "private_key", _SLOW_KEY
        if report.rsa_too_large:
            return "private_key", _LARGE_RSA_KEY
        if report.rsa_unbalanced:
            return "private_key", _UNBALANCED_RSA_KEY
        if report.on_a_security_key:
            return "private_key", _SECURITY_KEY
        if report.opens:
            return None
        if self.passphrase is None:
            return "private_key", _UNREADABLE_KEY
        return "passphrase", _WRONG_PASSPHRASE

    @field_validator("private_key", "passphrase", "password", mode="before")
    @classmethod
    def _short_enough_to_read(cls, value: object, info: ValidationInfo) -> object:
        """Refuse an over-long key, passphrase or password before anything reads it.

        Before the field's own validation, so the length of the raw value is all
        that is looked at, and in our words on the field: pydantic's own message
        for `max_length` counts "items" and names no fix. The `max_length` on
        each field stays, for the form's schema.
        """
        limit, sentence = _LENGTH_LIMITS[str(info.field_name)]
        if isinstance(value, str | SecretStr) and len(value) > limit:
            raise PydanticCustomError("too_long", "{sentence}", {"sentence": sentence})
        return value

    @field_validator("host")
    @classmethod
    def _spelled_as_a_source_host(cls, value: str) -> str:
        """The host spelled as an SFTP source spells its own, and its port.

        `allows` compares the two as strings. A login stored for
        `[2001:DB8:0::7]` or `sftp.example.com.` would otherwise be accepted
        here and match no source, and the refusal would come at the first sync.
        So the host is lower-cased, IDNA-encoded and an IPv6 address compressed
        (:func:`app.core.sanitize.spelled_host`), one trailing dot is dropped,
        and the port loses its leading zeros.
        """
        match = _HOST_AND_PORT.fullmatch(value)
        if match is None or any(character.isspace() for character in value):
            raise ValueError(_NOT_A_BOUND_HOST)
        try:
            host = spelled_host(match["host"].removesuffix("."))
        except ValueError:
            raise ValueError(_NOT_A_BOUND_HOST) from None
        if ":" in host:
            host = f"[{host}]"
        if match["port"] is None:
            return host
        port = int(match["port"])
        if not 1 <= port <= 65535:
            raise ValueError(_NOT_A_BOUND_HOST)
        return f"{host}:{port}"

    @model_validator(mode="after")
    def _one_method_and_a_key_that_can_be_read(self) -> SshCredentialsSecret:
        """One way to log in, and a key whose header reads, within the ceiling.

        With both a key and a password, the connector would have to guess which
        one to offer. A passphrase beside a password is a second secret that
        nothing reads. Only the key checks that cost nothing run here: see the
        class docstring.
        """
        if (self.private_key is None) == (self.password is None):
            raise ValueError("An SSH login needs a private key or a password, and not both.")
        if self.passphrase is not None and self.private_key is None:
            raise _refused_on("passphrase", "A passphrase goes with a private key.")
        if self.private_key is not None:
            refusal = _key_header_refusal(
                self.private_key.get_secret_value(),
                None if self.passphrase is None else self.passphrase.get_secret_value(),
            )
            if refusal is not None:
                raise _refused_on(*refusal)
        return self


class EntraAppSecret(_SecretBase):
    """A Microsoft Entra app registration a SharePoint or OneDrive source signs in as.

    The client credentials flow: the app proves itself with its secret and gets
    a Microsoft Graph token carrying the *application* permissions an
    administrator consented to. Those decide the source's reach, not anything a
    source's editor types: `Files.Read.All` reads every drive in the tenant,
    `Sites.Selected` only the sites the app was granted
    (`docs/howto/configure-sync-sources.md#sharepoint-and-onedrive-setup`).

    `tenant_id` goes into the sign-in URL's path, so it is held to what a tenant
    id or a tenant's domain can be spelled with.
    """

    kind: Literal[SecretKind.ENTRA_APP] = SecretKind.ENTRA_APP
    tenant_id: str = Field(
        min_length=1,
        max_length=253,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$",
        title="Tenant ID",
        description="The directory (tenant) id, or the tenant's domain, e.g. contoso.onmicrosoft.com",
    )
    client_id: str = Field(
        pattern=r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$",
        title="Client ID",
        description="The app registration's application (client) id",
    )
    client_secret: CredentialStr = Field(
        title="Client secret", description="The secret's value, not its id"
    )

    @property
    def hint(self) -> str:
        # The client id, not the secret: it is public, and it is what names the
        # app registration in the Entra admin center.
        return self.client_id[-4:]


class OpenAICodexSecret(_SecretBase):
    """A ChatGPT subscription's sign-in, as the Codex backend accepts it.

    Three fields because the backend wants all three on every request: the
    bearer, the `chatgpt-account-id` header it is checked against, and the
    refresh token that replaces both when the bearer expires. The refresh token
    is single-use, so this value changes under its own id - see
    `app/services/openai_codex.py`, which is what writes it back.
    """

    kind: Literal[SecretKind.OPENAI_CODEX] = SecretKind.OPENAI_CODEX
    access_token: CredentialStr = Field(title="Access token")
    refresh_token: CredentialStr = Field(title="Refresh token")
    account_id: str = Field(
        min_length=1,
        max_length=255,
        title="ChatGPT account ID",
        description="The account the tokens were issued for",
    )

    @property
    def hint(self) -> str:
        # The account, not a token: it identifies the subscription and does not
        # authenticate anything on its own.
        return self.account_id[-4:]


class GithubOAuthAppSecret(_SecretBase):
    """A GitHub OAuth App's credentials: a public client id and a secret."""

    kind: Literal[SecretKind.GITHUB_OAUTH_APP] = SecretKind.GITHUB_OAUTH_APP
    client_id: str = Field(
        min_length=1,
        max_length=255,
        title="Client ID",
        description="The OAuth App's client id, e.g. Iv1.0123456789abcdef",
    )
    client_secret: CredentialStr = Field(title="Client secret")

    @property
    def hint(self) -> str:
        # The client id, not the secret: it is public, and it is what names the
        # app in the GitHub settings the same key sits next to.
        return self.client_id[-4:]


class GithubAppSecret(_SecretBase):
    """A GitHub App's identity: its id, its private key and its webhook secret.

    A different kind from `github_oauth_app` and not a variant of it, because the
    two authorise differently rather than differently-shaped. An OAuth App holds
    a token scoped to the *person* who consented - `repo` and `admin:repo_hook`,
    read-write on every repository that account can administer, and no expiry at
    all. An App is *installed* on chosen repositories with chosen permissions,
    and its access is a token minted from this private key that lives an hour
    (#1072).

    All three fields are credentials. The app id is public in the sense that it
    appears in the App's settings page, but it is useless without the key and
    there is nothing gained by treating it as a hint - so the hint is the last
    four of the app id, which is what names the App on that page.
    """

    kind: Literal[SecretKind.GITHUB_APP] = SecretKind.GITHUB_APP
    app_id: str = Field(
        min_length=1,
        max_length=32,
        title="App ID",
        description="The numeric App ID from the App's settings page",
    )
    private_key: CredentialStr = Field(
        title="Private key",
        description="The PEM the App's settings page generated. Signs the JWT that mints installation tokens",
        # The one multi-line secret in this file, and it has to say so. The vault
        # form is generated from this schema, and a `CredentialStr` alone renders
        # as `<input type="password">` - where a browser strips the line breaks
        # out of the value, collapsing the PEM's header, body and footer into a
        # key `jwt.encode` cannot use. The failure then surfaces an hour later as
        # an installation token that will not mint (#1072).
        json_schema_extra={"x-textarea": True},
    )
    webhook_secret: CredentialStr = Field(
        title="Webhook secret",
        description="The secret configured on the App. One App, one webhook URL, one secret for every installation",
    )

    @property
    def hint(self) -> str:
        return self.app_id[-4:]


class GoogleOAuthAppSecret(_SecretBase):
    """A Google OAuth client's credentials: a public client id and a secret.

    The same two fields as a GitHub OAuth App and deliberately a different kind:
    the kind names what a credential is *for*, not what shape it is, so filing a
    mailbox client under GitHub's - which takes the same fields - would group by
    the shape and put the wrong app in the wrong consent flow. Same reasoning as
    #937's split of a Drive service account from Vertex AI's.

    Each organization registers its own client in its own Google project. It is not
    the deployment's `GOOGLE_CLIENT_ID`, which is sign-in and a different thing
    entirely; and a credential at rest goes through the vault, which is the whole
    rule.
    """

    kind: Literal[SecretKind.GOOGLE_OAUTH_APP] = SecretKind.GOOGLE_OAUTH_APP
    client_id: str = Field(
        min_length=1,
        max_length=255,
        title="Client ID",
        description="The OAuth client's id, e.g. 1234-abc.apps.googleusercontent.com",
    )
    client_secret: CredentialStr = Field(title="Client secret")

    @property
    def hint(self) -> str:
        # The client id, not the secret: it is public, and it is what names the
        # client in the Google console the same key sits next to.
        return self.client_id[-4:]


StorableSecret = Annotated[
    ApiKeySecret
    | AzureOpenAISecret
    | AwsCredentialsSecret
    | GcpServiceAccountSecret
    | GithubOAuthAppSecret
    | GithubAppSecret
    | GoogleOAuthAppSecret
    | GitTokenSecret
    | SshCredentialsSecret
    | EntraAppSecret
    | OpenAICodexSecret,
    Field(discriminator="kind"),
]
"""Every shape a person can actually save."""

TypedSecret = Annotated[
    ApiKeySecret
    | AzureOpenAISecret
    | AwsCredentialsSecret
    | GcpServiceAccountSecret
    | GithubOAuthAppSecret
    | GithubAppSecret
    | GoogleOAuthAppSecret
    | GitTokenSecret
    | SshCredentialsSecret
    | EntraAppSecret,
    Field(discriminator="kind"),
]
"""What a person may type into the vault: :data:`StorableSecret` but a ChatGPT sign-in.

A sign-in is written only by finishing the sign-in flow, which binds it to whoever
started it. Posted as a value, it would be any pair of tokens - a Codex CLI's own
grant among them, whose single-use refresh token then signs the CLI and the platform
out of each other.
"""

SecretValue = Annotated[
    NoSecret
    | ApiKeySecret
    | AzureOpenAISecret
    | AwsCredentialsSecret
    | GcpServiceAccountSecret
    | GithubOAuthAppSecret
    | GithubAppSecret
    | GoogleOAuthAppSecret
    | GitTokenSecret
    | SshCredentialsSecret
    | EntraAppSecret
    | OpenAICodexSecret,
    Field(discriminator="kind"),
]
"""What the runtime holds - :data:`StorableSecret` plus "there is no credential"."""

STORABLE_KINDS: frozenset[SecretKind] = frozenset(SecretKind) - {SecretKind.NONE}

# The vault validates a decrypted payload through this. `hide_input_in_errors`
# here, because pydantic reads it from the outermost schema only.
_STORABLE_ADAPTER: TypeAdapter[StorableSecret] = TypeAdapter(
    StorableSecret, config=ConfigDict(hide_input_in_errors=True)
)


def seal_secret(
    value: StorableSecret, *, scope: VaultScope, key_version: int | None = None
) -> SealedSecret:
    """Seal a typed secret into one envelope.

    The whole payload is sealed as a single JSON document rather than field by
    field, so a credential cannot be half-rotated and the non-secret fields
    (region, endpoint, API version) cannot be edited without the key that
    belongs with them.
    """
    sealed = seal(value.model_dump_json(), scope=scope, key_version=key_version)
    # The vault's hint is the last four characters of what it sealed, which for
    # a JSON document is punctuation. Each kind knows which of its fields
    # identifies it.
    return replace(sealed, hint=value.hint)


def unseal_secret(
    ciphertext: str, *, kind: SecretKind, scope: VaultScope, key_version: int = 1
) -> StorableSecret:
    """Open an envelope and check it holds the kind the row claims.

    Raises:
        BadRequestError: If the envelope cannot be opened, holds something that
            is not a secret payload, or holds a different kind than the column
            says - which would mean a row was edited underneath the schema.
    """
    try:
        value = _STORABLE_ADAPTER.validate_json(
            unseal(ciphertext, scope=scope, key_version=key_version)
        )
    except ValidationError:
        # A `ValidationError` message embeds the offending input - here the
        # decrypted credential - so it must reach neither a log line nor a span.
        # `from None`, not `from exc`: chaining would keep the plaintext in the
        # traceback (#21).
        raise BadRequestError(
            message="Stored secret is not a usable payload",
            details={"recorded": kind.value},
        ) from None
    if value.kind is not kind:
        raise BadRequestError(
            message="Stored secret does not match its recorded kind",
            details={"recorded": kind.value, "sealed": value.kind.value},
        )
    return value


class SecretKindInfo(BaseModel):
    """One kind as the Builder and the Settings forms render it.

    Served from the API so the frontend generates its forms from the same schema
    the backend validates against, rather than from a second list that drifts.
    """

    model_config = ConfigDict(frozen=True)

    kind: SecretKind
    name: str
    description: str
    json_schema: dict[str, Any]


_KIND_MODELS: dict[SecretKind, type[BaseModel]] = {
    SecretKind.API_KEY: ApiKeySecret,
    SecretKind.AZURE_OPENAI: AzureOpenAISecret,
    SecretKind.AWS_CREDENTIALS: AwsCredentialsSecret,
    SecretKind.GCP_SERVICE_ACCOUNT: GcpServiceAccountSecret,
    SecretKind.GITHUB_OAUTH_APP: GithubOAuthAppSecret,
    SecretKind.GITHUB_APP: GithubAppSecret,
    SecretKind.GOOGLE_OAUTH_APP: GoogleOAuthAppSecret,
    SecretKind.GIT_TOKEN: GitTokenSecret,
    SecretKind.SSH_CREDENTIALS: SshCredentialsSecret,
    SecretKind.ENTRA_APP: EntraAppSecret,
    SecretKind.OPENAI_CODEX: OpenAICodexSecret,
}

_KIND_LABELS: dict[SecretKind, tuple[str, str]] = {
    SecretKind.API_KEY: ("API key", "A single token, sent as-is to the service."),
    SecretKind.AZURE_OPENAI: (
        "Azure OpenAI",
        "A key together with the deployment endpoint and the API version it is pinned to.",
    ),
    SecretKind.AWS_CREDENTIALS: (
        "AWS credentials",
        "An access key id, its secret access key and a region - for Bedrock and other AWS APIs.",
    ),
    SecretKind.GCP_SERVICE_ACCOUNT: (
        "Google service account",
        "The service account JSON downloaded from the Google Cloud console.",
    ),
    SecretKind.GITHUB_OAUTH_APP: (
        "GitHub OAuth App",
        "A GitHub OAuth App's client id and secret, used to connect a GitHub "
        "account for repository webhooks.",
    ),
    SecretKind.GITHUB_APP: (
        "GitHub App",
        "A GitHub App's id, private key and webhook secret. Installed on chosen "
        "repositories with scoped permissions, and delivering without a "
        "per-repository hook.",
    ),
    SecretKind.GOOGLE_OAUTH_APP: (
        "Google OAuth client",
        "A Google OAuth client's id and secret, used to connect a mailbox an agent is run by.",
    ),
    SecretKind.GIT_TOKEN: (
        "Git access token",
        "A token that reads repositories over HTTPS, and the one host it may be sent to.",
    ),
    SecretKind.SSH_CREDENTIALS: (
        "SSH login",
        "A user name with a private key or a password, for an SFTP server, and the "
        "one host it may be used with.",
    ),
    SecretKind.ENTRA_APP: (
        "Microsoft Entra app",
        "An app registration's tenant, client id and client secret - for reading "
        "SharePoint sites and OneDrive folders through Microsoft Graph.",
    ),
    SecretKind.OPENAI_CODEX: (
        "ChatGPT sign-in",
        "A ChatGPT subscription's sign-in, for running models on the Codex backend. "
        "Stored by signing in with ChatGPT rather than typed.",
    ),
}


def describe_kind(kind: SecretKind) -> str:
    """A kind's own label, for a refusal that names what to add.

    "Add an org-visible Google OAuth client secret in Vault" is actionable where
    "add a google_oauth_app" asks the reader to translate an identifier.
    """
    return _KIND_LABELS[kind][0]


def describe_kinds() -> list[SecretKindInfo]:
    """Every storable kind with the schema its form is generated from."""
    return [
        SecretKindInfo(
            kind=kind,
            name=_KIND_LABELS[kind][0],
            description=_KIND_LABELS[kind][1],
            json_schema=model.model_json_schema(),
        )
        for kind, model in _KIND_MODELS.items()
    ]


class SecretCondition(BaseModel):
    """When a declared secret is actually required, as data rather than code.

    A predicate would be shorter to write and impossible to ship: the Builder
    has to ask for a key at exactly the moments the server will demand one, and
    a lambda cannot cross the wire. So the rule is a value - "this config field
    equals one of these" - evaluated by the same definition on both sides.

    It is deliberately the simplest thing that covers the real case: a capability
    offering several providers where only some authenticate. Anything more
    expressive would be a rules engine nobody can read at a glance.
    """

    model_config = ConfigDict(frozen=True)

    field: str = Field(description="Which config field decides")
    equals: tuple[str, ...] = Field(description="Values of that field that need a secret")

    def is_met(self, config: BaseModel | None) -> bool:
        """Whether this configuration needs the secret."""
        if config is None:
            return False
        return str(getattr(config, self.field, None)) in self.equals


class PurposesByValue(BaseModel):
    """Which services a key may be for, by the value of one config field.

    `required_when` doubles as the purpose where the gating field's values *are*
    service ids - web search's `tavily`. A field whose values are not, and that
    decides the key on every value, needs the translation written down: the coding
    agent's `claude_code` runs on an Anthropic key and its `codex` on an OpenAI key
    or a ChatGPT sign-in. Data for the same reason a condition is: the Builder's
    picker narrows on exactly what publish will accept (#2485).
    """

    model_config = ConfigDict(frozen=True)

    field: str = Field(description="Which config field decides")
    purposes: dict[str, tuple[str, ...]] = Field(
        description=(
            "For each value of that field, the purposes a key for it may have. "
            "`custom` is always accepted besides these; a value missing here "
            "narrows nothing."
        )
    )
    open_when_set: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "For a value of that field, another config field which, once set, "
            "points the key at an endpoint the platform cannot name - Claude "
            "Code's `base_url` - so any key fits."
        ),
    )


class SecretRequirement(BaseModel):
    """A secret a capability needs, as it declares it in `register(...)`.

    A capability names a *kind*, never an instance: the code says "I need an API
    key", the configuration says which one, and neither the model nor the person
    writing the spec can turn that into a different shape of credential.
    """

    model_config = ConfigDict(frozen=True)

    kind: SecretKind
    description: str = Field(description="What the secret is for, shown next to the picker")
    purpose: str | None = Field(
        default=None,
        description=(
            "Which service in the deployment's catalog this key is for - a "
            "`services.json` id such as `mem0`. It is what narrows the picker from "
            "every key of the right *kind* to the ones that would actually work, and "
            "what a key created from the picker is stored under. Null where the "
            "capability offers several services and `required_when` already names "
            "the chosen one, which is the case web search covers."
        ),
    )
    required_when: SecretCondition | None = Field(
        default=None,
        description=(
            "When this is set, the secret is needed only for the configurations it "
            "names; null means always. Web search takes a key for Tavily and none "
            "for DuckDuckGo, and both the Builder and publish validation read this."
        ),
    )
    optional_when: SecretCondition | None = Field(
        default=None,
        description=(
            "Configurations that may use a secret without needing one, so the Builder "
            "offers the picker with a 'none' choice. A browser's proxy may sign in "
            "with a username and password or be reached without one."
        ),
    )
    purposes_by: PurposesByValue | None = Field(
        default=None,
        description=(
            "Which services the key may be for, by a config field whose values are "
            "not service ids themselves. Read before `required_when` and `purpose`."
        ),
    )
    also_accepts: tuple[SecretKind, ...] = Field(
        default=(),
        description=(
            "Further kinds that satisfy the requirement besides `kind`. The coding "
            "agent takes an API key or a ChatGPT sign-in, and the Builder offers both."
        ),
    )

    def accepts(self, kind: str) -> bool:
        """Whether a secret of `kind` satisfies this requirement."""
        return kind == self.kind.value or kind in {also.value for also in self.also_accepts}

    def purposes_for(self, config: BaseModel | None) -> tuple[str, ...] | None:
        """Which services a key for this configuration may be for, or `None`.

        The Builder's `purposesOf` reads the same three sources in the same order.
        A field mapped to purposes says so outright, unless the endpoint field
        named for its value is set, when nothing narrows. Otherwise a capability
        offering several services gates its key on the choice, so the conditional
        field *is* the service; and one with a single service names it. `custom`
        fits any of them, which is the caller's to allow.
        """
        if self.purposes_by is not None and config is not None:
            value = str(getattr(config, self.purposes_by.field, None))
            endpoint = self.purposes_by.open_when_set.get(value)
            if endpoint is not None and getattr(config, endpoint, None):
                return None
            mapped = self.purposes_by.purposes.get(value)
            if mapped is not None:
                return mapped
        if self.required_when is not None and config is not None:
            chosen = getattr(config, self.required_when.field, None)
            if isinstance(chosen, str) and chosen:
                return (chosen,)
        return None if self.purpose is None else (self.purpose,)

    @model_validator(mode="after")
    def _a_requirement_is_for_a_real_secret(self) -> SecretRequirement:
        if self.kind is SecretKind.NONE:
            raise ValueError("A capability cannot require the 'none' kind - that is no secret")
        return self
