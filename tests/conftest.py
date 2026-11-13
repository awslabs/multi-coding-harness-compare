# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep a developer's own config.toml and deploy.properties out of the test suite.

load_config() falls back to ./config.toml and the deploy.properties beside it; point both
at empty temporary files for the whole session (subprocesses inherit the environment).
Tests that need real files pass explicit paths.

Hermetic AWS: every test runs with fake static credentials and no real credential files
so no test can reach AWS regardless of the machine's AWS_PROFILE or credential files.
Tests that need a specific region or profile set it themselves inside the test.

Network guard: every test that opens a non-loopback socket raises a clear error naming
the test so that tests requiring real AWS calls are identified and mocked instead.
"""
import os
import socket as _socket
import tempfile
from pathlib import Path

import pytest

# ── session-level: redirect arena's config/properties lookup to empty temp files ──

_isolation = tempfile.TemporaryDirectory(prefix="mhc-test-config-")
(Path(_isolation.name) / "deploy.properties").write_text("")
os.environ["MCHC_CONFIG"] = str(Path(_isolation.name) / "config.toml")
os.environ["MCHC_PROPERTIES"] = str(Path(_isolation.name) / "deploy.properties")
os.environ.pop("ARENA_CONFIG", None)

# ── per-test: hermetic AWS credentials ────────────────────────────────────────

# All real AWS credential and config env vars are stripped before each test and
# replaced with obviously fake static values. The EC2 metadata service is
# disabled so boto3 never tries IMDS. Tests that need a specific profile, region,
# or credentials set them explicitly inside the test (or via patch.dict).
_AWS_CLEAR_KEYS = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SDK_UA_APP_ID",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CA_BUNDLE",
)

_AWS_FAKE = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_EC2_METADATA_DISABLED": "true",
}


@pytest.fixture(autouse=True)
def _hermetic_aws(tmp_path):
    """Strip real AWS env vars; install fake static credentials for every test."""
    # Save anything currently set.
    saved = {k: os.environ[k] for k in _AWS_CLEAR_KEYS if k in os.environ}
    # Remove all real AWS keys.
    for key in _AWS_CLEAR_KEYS:
        os.environ.pop(key, None)
    # Empty credential files so boto3 cannot read real credentials from disk.
    config_file = tmp_path / "aws_config"
    creds_file = tmp_path / "aws_creds"
    config_file.write_text("")
    creds_file.write_text("")
    injected = {
        **_AWS_FAKE,
        "AWS_CONFIG_FILE": str(config_file),
        "AWS_SHARED_CREDENTIALS_FILE": str(creds_file),
    }
    os.environ.update(injected)
    yield
    # Restore the previous state exactly.
    for key in injected:
        os.environ.pop(key, None)
    os.environ.update(saved)


# ── per-test: block non-loopback network connections ─────────────────────────

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@pytest.fixture(autouse=True)
def _block_non_loopback(monkeypatch):
    """Raise a clear OSError if any test tries a non-loopback TCP connection.

    Unix-domain socket addresses (strings) and raw (non-tuple) addresses pass
    through unmodified so subprocess pipes and intra-process mechanisms are
    unaffected. Only IPv4/IPv6 tuple addresses (host, port, ...) are checked.
    """
    _original_connect = _socket.socket.connect

    def _guarded_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in _LOOPBACK_HOSTS:
            test_name = os.environ.get("PYTEST_CURRENT_TEST", "unknown test")
            raise OSError(
                f"[test guard] Non-loopback network connection to {address[0]!r} "
                f"blocked in test: {test_name}. "
                "Mock external services instead of reaching real endpoints."
            )
        return _original_connect(sock, address)

    monkeypatch.setattr(_socket.socket, "connect", _guarded_connect)
