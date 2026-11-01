"""Tests for the general secret store and the shapes a secret can take.

The store exists for one concrete need - a custom capability calling an API the
platform knows nothing about - and it is only defensible because of the
constraint: **a secret is referenced, never handed around.** Most of what is
worth testing here is that constraint holding.
"""

import functools
import json
import traceback
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
from pydantic import ValidationError

from app.core import secret_kinds
from app.core.exceptions import AlreadyExistsError, BadRequestError, NotFoundError
from app.core.permissions import AuthContext, OrgRoleName
from app.core.secret_kinds import (
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
from app.services.organization_secret import OrganizationSecretService
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
    """Whether the refusal's own `ValueError` drops the asyncssh exception it replaced."""
    error = refused.value.errors()[0]["ctx"]["error"]
    return error.__cause__ is None and error.__suppress_context__ is True


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

    @pytest.mark.parametrize("form", ["openssh-encrypted", "pkcs8-encrypted", "pkcs1-encrypted"])
    def test_a_wrong_passphrase_is_refused_without_echoing_the_key(self, form: str):
        """asyncssh answers a wrong passphrase with a different exception for each
        format. Each one gets the same sentence, and neither the key nor the
        passphrase that was typed is shown.

        The passphrase is the last field, so pydantic's `input_value` would show
        it whole in `str()` and `repr()` of the error. `hide_input_in_errors`
        closes that path, and `from None` drops the chained asyncssh exception.
        """
        key = _key_text(form)

        with pytest.raises(ValidationError) as refused:
            _ssh_login(private_key=key, passphrase=WRONG_PASSPHRASE)

        shown = _what_a_refusal_shows(refused)
        assert "input_value" not in str(refused.value)
        assert "The private key could not be read with this passphrase" in shown
        assert not _echoes(shown, key)
        assert not _echoes(shown, WRONG_PASSPHRASE, 8)
        assert "Incorrect passphrase" not in shown
        assert "Unable to decrypt" not in shown
        assert _chained_from_none(refused)

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
        ],
    )
    def test_a_damaged_key_that_the_parser_refuses_in_its_own_words_gets_our_sentence(
        self, paste, passphrase: str | None, sentence: str, foreign: list[str]
    ):
        """asyncssh does not wrap every parse failure. A changed character in a
        PKCS#1 key reaches cryptography's `ValueError`, and an encrypted PKCS#8
        key without its first line a `UnicodeDecodeError`. Both are `ValueError`,
        as asyncssh's own two classes are. The refusal is still our sentence,
        and their text is not shown."""
        text = paste()
        with pytest.raises(ValueError) as parsed:
            asyncssh.import_private_key(text, passphrase)
        # The case is here for the error asyncssh does not wrap. If asyncssh
        # starts to wrap it, this case tests nothing new: choose another paste.
        assert not isinstance(parsed.value, asyncssh.KeyImportError | asyncssh.KeyEncryptionError)

        with pytest.raises(ValidationError) as refused:
            _ssh_login(private_key=text, passphrase=passphrase)

        shown = _what_a_refusal_shows(refused)
        assert "input_value" not in str(refused.value)
        assert sentence in shown
        assert not [words for words in foreign if words in shown]
        assert not _echoes(shown, text)
        assert _chained_from_none(refused)

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
            pytest.param("a" * 256, id="too-long"),
        ],
    )
    def test_a_user_name_with_whitespace_or_a_control_character_is_refused(self, username: str):
        """The user name goes into the login refusal, the sync log and log lines. A
        line break or a control character there could forge a line. SSH user
        names hold neither, and are at most 255 characters here."""
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
        ],
    )
    def test_an_ssh_login_s_host_is_one_host_and_an_optional_port(self, host: str):
        """The host binds the login. A URL, a path or a user in it could never
        match the host a source dials, so the login would be refused at every
        sync. An IPv6 address needs its brackets, or its last group reads as a
        port."""
        with pytest.raises(ValidationError):
            _ssh_login(host=host, password=PASSWORD)

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
