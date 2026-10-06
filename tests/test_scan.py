"""Credentials in a call's output are reported by kind, never by value."""

import pytest

from agentltl_coding.scan import credential_kinds

# Built at run time so that no credential-shaped literal sits in the repository.
FAKE = {
    "AWS access key ID": "AKIA" + "ABCDEFGHIJ234567",
    "GitHub token": "ghp" + "_" + "a1B2c3D4e5" * 4,
    "private key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "Slack token": "xox" + "b-" + "1234567890-abcdef",
}


@pytest.mark.parametrize("kind", sorted(FAKE))
def test_known_formats_are_recognised(kind):
    assert credential_kinds(f"config loaded: {FAKE[kind]} (ok)") == [kind]


def test_ordinary_output_is_quiet():
    assert credential_kinds("AKIA is a prefix; ghp_ too; the key is in the vault") == []


def test_structured_responses_are_scanned():
    assert credential_kinds({"stdout": "x", "stderr": FAKE["AWS access key ID"]}) == [
        "AWS access key ID"]
