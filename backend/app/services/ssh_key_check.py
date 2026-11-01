"""Open an untrusted SSH private key in a short-lived child process, and say what it is.

Before it stores an SSH login, `OrganizationSecretService` checks that the key opens
with its passphrase. Opening a key costs what the key asks for: the key derivation
an encrypted key names, and, for RSA, a validation that grows about as the cube of
the modulus and that `cryptography` runs with the GIL held. A cheap check of the
key's header (`app.core.secret_kinds`) refuses the work factors it can read. But the
expensive numbers of an encrypted key are inside the part that only decrypting
reveals, and a valid RSA key built from two large known primes held one API
process's event loop for seven minutes, in a thread. So the key is opened here, in a
process of its own, which the parent can kill.

The parent runs `python -I -m app.services.ssh_key_check`. Isolated mode keeps the
environment, the user's site packages and the working directory out of what it
imports. This module imports the standard library at module level and asyncssh
inside :func:`inspect`, and nothing of the application: `app.services` has no
package initializer, so only `app/__init__.py`, which reads the version, runs before
it. The key and the passphrase arrive on stdin as one JSON document, never in argv
or the environment, which other processes on the host can read. The report goes to
stdout as one JSON document.
"""

import json
import re
import sys
from dataclasses import asdict, dataclass, replace
from typing import cast

MAX_RSA_BITS = 4096
"""The largest RSA modulus a stored SSH login may hold.

Validating an RSA key tests its two primes, and the cost grows about as the
cube of each prime's size. A valid key of any size can be built from two known
primes. A key over this size is reported and never validated.
"""

MAX_RSA_PRIME_GAP_BITS = 16
"""How far apart, in bits, the two primes of a stored RSA key may be.

`ssh-keygen` and OpenSSL make the two primes the same size. A 4096-bit key made
of a 4000-bit and a 96-bit prime is valid, and validates in 2.1 s against 0.16 s
for a balanced one, a cost the connector would pay again at each connection. A
key whose primes differ by more is reported and never validated.
"""

KEY_PARSE_ERRORS: tuple[type[Exception], ...] = (
    ValueError,
    OverflowError,
    ZeroDivisionError,
    RecursionError,
    re.error,
)
"""What asyncssh and the libraries under it raise for a key they cannot read.

Found by fuzzing damaged keys of each format, encrypted and not: `ValueError`
and its subclasses (asyncssh's `KeyImportError` and `KeyEncryptionError`,
`cryptography`'s own, a `UnicodeDecodeError` for a damaged encrypted PKCS#8
key), `OverflowError` from `cryptography`'s RSA constructor for a negative
number, `ZeroDivisionError` from asyncssh's OpenSSH RSA decoder for a prime of
1, `RecursionError` from asyncssh's DER decoder for a deeply nested body, and
`re.error` from the footer pattern asyncssh builds out of a PEM label it does
not escape. asyncssh wraps none of the last four. Never `Exception`: a
defect of ours must not read as a bad paste. Every key call catches exactly
these. Defined here, not in `app.core.secret_kinds`, because this module must
import nothing of the application.
"""


@dataclass(frozen=True)
class KeyReport:
    """What the child made of a key.

    `opens` says that the passphrase decrypted the key and that it parsed. An
    RSA key over :data:`MAX_RSA_BITS`, or with primes further apart than
    :data:`MAX_RSA_PRIME_GAP_BITS`, is reported with its sizes and not
    validated, so `opens` says nothing about its numbers.
    """

    opens: bool
    algorithm: str | None = None
    rsa_bits: int | None = None
    rsa_prime_gap: int | None = None

    @property
    def rsa_too_large(self) -> bool:
        """An RSA modulus over :data:`MAX_RSA_BITS`."""
        return self.rsa_bits is not None and self.rsa_bits > MAX_RSA_BITS

    @property
    def rsa_unbalanced(self) -> bool:
        """RSA primes further apart than :data:`MAX_RSA_PRIME_GAP_BITS`."""
        return self.rsa_prime_gap is not None and self.rsa_prime_gap > MAX_RSA_PRIME_GAP_BITS

    @property
    def on_a_security_key(self) -> bool:
        """An `sk-` key: a handle for a hardware authenticator the worker does not have."""
        return self.algorithm is not None and self.algorithm.startswith("sk-")


def inspect(key: str, passphrase: str | None) -> KeyReport:
    """Open the key as the SFTP connector will, and report it.

    The first import skips RSA validation, so an RSA key's sizes are known
    before the validation runs: a key over :data:`MAX_RSA_BITS`, or with
    unbalanced primes, is reported as it is. Every other key is then imported
    again from its decrypted form, with validation on. For RSA that form is
    PKCS#8, which carries every number the paste holds, so the check sees what
    the connector will open. No key derivation runs a second time.
    """
    import asyncssh
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

    try:
        opened = asyncssh.import_private_key(key, passphrase, unsafe_skip_rsa_key_validation=True)
        report = KeyReport(opens=True, algorithm=opened.algorithm.decode("ascii"))
        if report.algorithm == "ssh-rsa":
            rsa = cast(RSAPrivateKey, opened.pyca_key)
            numbers = rsa.private_numbers()
            gap = abs(numbers.p.bit_length() - numbers.q.bit_length())
            report = replace(report, rsa_bits=rsa.key_size, rsa_prime_gap=gap)
        if report.rsa_too_large or report.rsa_unbalanced:
            return report
        # PKCS#8 keeps every number of an RSA key. The OpenSSH form drops dmp1 and
        # dmq1, which the re-import would compute again, so a pasted key with a
        # wrong one would pass here and fail at the worker's import. A key of
        # another type keeps the OpenSSH form: a security key has no PKCS#8 one.
        form = "pkcs8-der" if report.algorithm == "ssh-rsa" else "openssh"
        asyncssh.import_private_key(opened.export_private_key(form))
    except KEY_PARSE_ERRORS:
        return KeyReport(opens=False)
    return report


def main() -> None:
    """Read `{"key", "passphrase"}` from stdin, and write the report to stdout."""
    request = json.loads(sys.stdin.buffer.read())
    report = inspect(request["key"], request["passphrase"])
    sys.stdout.write(json.dumps(asdict(report)))


if __name__ == "__main__":
    main()
