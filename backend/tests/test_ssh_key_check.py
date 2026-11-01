"""The child process that opens a pasted SSH private key, called in-process.

`app.services.ssh_key_check` runs in a child process that `OrganizationSecretService`
starts and kills (`tests/test_secrets.py` drives that path with real children). Its
logic is `inspect`, which these tests call directly: what it reports for each kind
of key, that it never validates an RSA key over the size limit, and that it reads
its request from stdin and writes its report to stdout.
"""

import io
import json
import runpy
import sys
import time

import asyncssh
import pytest
from asyncssh.asn1 import der_decode, der_encode

from app.services.ssh_key_check import MAX_RSA_BITS, KeyReport, inspect
from tests.test_secrets import (
    PASSPHRASE,
    WRONG_PASSPHRASE,
    _deeply_nested_der,
    _key_text,
    _mersenne_rsa_key,
    _pem,
    _pem_body,
    _pkcs1_key_with_a_negative_number,
    _rsa_4096,
    _rsa_key_with_a_prime_of_one,
    _security_key_private_key,
    _ssh_key,
)

pytestmark = pytest.mark.security


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "encrypted"])
@pytest.mark.parametrize(
    ("algorithm", "rsa_bits", "rsa_prime_gap"),
    [
        ("ssh-ed25519", None, None),
        ("ecdsa-sha2-nistp256", None, None),
        ("ssh-rsa", MAX_RSA_BITS, 0),
    ],
)
def test_a_key_that_opens_is_reported_with_its_type_and_rsa_size(
    algorithm: str, rsa_bits: int | None, rsa_prime_gap: int | None, encrypted: bool
):
    key = _rsa_4096() if algorithm == "ssh-rsa" else _ssh_key(algorithm)
    passphrase = PASSPHRASE if encrypted else None
    text = key.export_private_key("pkcs8-pem", passphrase).decode()

    assert inspect(text, passphrase) == KeyReport(
        opens=True, algorithm=algorithm, rsa_bits=rsa_bits, rsa_prime_gap=rsa_prime_gap
    )


@pytest.mark.parametrize(
    ("text", "passphrase"),
    [
        pytest.param(
            lambda: _key_text("openssh-encrypted"), WRONG_PASSPHRASE, id="wrong-passphrase"
        ),
        pytest.param(lambda: _key_text("pkcs8-encrypted"), None, id="no-passphrase"),
        pytest.param(_pkcs1_key_with_a_negative_number, None, id="overflow-error"),
        pytest.param(_rsa_key_with_a_prime_of_one, None, id="zero-division-error"),
        pytest.param(
            lambda: _rsa_key_with_a_prime_of_one(PASSPHRASE),
            PASSPHRASE,
            id="zero-division-error-encrypted",
        ),
        pytest.param(lambda: _pem("PRIVATE KEY", _deeply_nested_der()), None, id="recursion-error"),
        pytest.param(
            lambda: "-----BEGIN [ PRIVATE KEY-----\nAAAA\n-----END [ PRIVATE KEY-----\n",
            None,
            id="re-error",
        ),
    ],
)
def test_a_key_that_does_not_open_is_reported_as_such(text, passphrase: str | None):
    """Each exception the parsers raise on a damaged paste ends in the same
    report. One the child did not catch would end the child without a report,
    which the parent treats as a defect, not as a bad key."""
    assert inspect(text(), passphrase) == KeyReport(opens=False)


@pytest.mark.parametrize(("index", "name"), [(6, "dmp1"), (7, "dmq1")], ids=["dmp1", "dmq1"])
def test_an_rsa_key_whose_crt_exponent_is_wrong_is_refused_as_the_worker_would(
    index: int, name: str
):
    """The connector opens the paste as it is, with validation, and a wrong dmp1
    or dmq1 fails there. An OpenSSH re-export drops both and computes them
    again, so the child re-imports PKCS#8, which keeps them, and refuses the key
    the worker would refuse. About a fifth of a typo in an RSA body lands in
    these two numbers."""
    numbers = list(der_decode(_pem_body(_key_text("pkcs1"))))
    numbers[index] += 2
    text = _pem("RSA PRIVATE KEY", der_encode(tuple(numbers)))
    with pytest.raises(ValueError, match="Invalid private key"):
        asyncssh.import_private_key(text)

    assert inspect(text, None) == KeyReport(opens=False), name


def test_an_rsa_key_with_numbers_that_do_not_fit_is_refused_by_its_validation():
    """The first import skips RSA validation to read the size first. The second,
    from the decrypted key, validates it, so a key whose private exponent does
    not match its modulus does not open."""
    numbers = list(der_decode(_pem_body(_key_text("pkcs1"))))
    numbers[3] += 2
    text = _pem("RSA PRIVATE KEY", der_encode(tuple(numbers)))

    assert inspect(text, None) == KeyReport(opens=False)


@pytest.mark.parametrize(
    ("p", "q", "bits"),
    [
        # Validated, these two took 5.4 s and seven minutes.
        pytest.param(4423, 4253, 8676, id="8676-bits"),
        pytest.param(19937, 21701, 41638, id="41638-bits"),
    ],
)
def test_an_rsa_key_over_the_limit_is_reported_without_being_validated(p: int, q: int, bits: int):
    """Reported unvalidated, each key over the limit takes well under a second,
    whatever its size."""
    started = time.perf_counter()

    report = inspect(_mersenne_rsa_key(p, q), None)

    assert time.perf_counter() - started < 1.0
    assert report == KeyReport(
        opens=True, algorithm="ssh-rsa", rsa_bits=bits, rsa_prime_gap=abs(p - q)
    )
    assert report.rsa_too_large


def test_an_rsa_key_with_unbalanced_primes_is_reported_without_being_validated():
    """A 3,738-bit key of a 3,217-bit and a 521-bit prime is within the size
    limit and valid, and validates in about 1.1 s. Its primes are reported
    apart, and it is not validated."""
    started = time.perf_counter()

    report = inspect(_mersenne_rsa_key(3217, 521), None)

    assert time.perf_counter() - started < 0.5
    assert report == KeyReport(
        opens=True, algorithm="ssh-rsa", rsa_bits=3738, rsa_prime_gap=3217 - 521
    )
    assert report.rsa_unbalanced


def test_a_security_key_s_key_is_reported_as_one():
    """It opens, because it holds only a handle for a hardware authenticator."""
    report = inspect(_security_key_private_key(), None)

    assert report == KeyReport(opens=True, algorithm="sk-ssh-ed25519@openssh.com")
    assert report.on_a_security_key


def test_the_child_reads_its_request_on_stdin_and_writes_its_report_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
):
    """What `python -m app.services.ssh_key_check` runs, run here as `__main__`."""
    request = {"key": _key_text("openssh-encrypted"), "passphrase": PASSPHRASE}
    written = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    monkeypatch.setattr(sys, "stdout", written)
    # Run fresh, as the child does, not over the module this process imported.
    monkeypatch.delitem(sys.modules, "app.services.ssh_key_check")

    runpy.run_module("app.services.ssh_key_check", run_name="__main__")

    assert json.loads(written.getvalue()) == {
        "opens": True,
        "algorithm": "ssh-ed25519",
        "rsa_bits": None,
        "rsa_prime_gap": None,
    }
