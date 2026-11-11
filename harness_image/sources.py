# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Version-source resolution for harness bundles.

Three strategies are supported per harness via H_HARNESS_SOURCE:

  pinned    Use the versions in harness_image/versions.py (the default).

  installed Detect the version currently installed on this workstation
            by running the CLI with --version (or equivalent), then fetch
            the matching linux-arm64 artefact from the trusted registry.
            Falls back to the pinned version if the CLI is not found.

  latest    Resolve the latest published version from the trusted registry
            (npm dist-tags for Codex, PyPI JSON for claude-agent-sdk, the
            Kiro vendor manifest if available).  Requires --approve TOKEN
            at deploy time: `plan` prints the resolved plan and a token;
            `deploy` refuses unless the token matches the current plan.

Approved plans are persisted to DATA_DIR/harness-versions.lock.json.
Each locked entry stores: version, uri, sha256 (or sha512 for npm).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from . import versions as _v

log = logging.getLogger(__name__)

HARNESS_SOURCES = ("pinned", "installed", "latest")

# Trusted vendor hosts; custom HARNESS_TRUSTED_SOURCES overrides per-deploy.
_DEFAULT_TRUSTED_HOSTS = frozenset([
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "desktop-release.q.us-east-1.amazonaws.com",
])

_HTTPS_RE = re.compile(r"^https://")


class ResolvedVersion(NamedTuple):
    harness: str
    version: str
    uri: str       # exact download URI
    integrity: str  # sha256:HEX or sha512:HEX (npm dist.integrity)
    source: str    # "pinned" | "installed" | "latest"


# ── trust enforcement ─────────────────────────────────────────────────────────

def _assert_trusted(uri: str, trusted_hosts: frozenset[str]) -> None:
    """Raise ValueError when *uri* is not https or its host is not in *trusted_hosts*."""
    if not _HTTPS_RE.match(uri):
        raise ValueError(f"Download URI must use https, got: {uri!r}")
    host = urlsplit(uri).netloc.split(":")[0].lower()
    if host not in trusted_hosts:
        raise ValueError(
            f"Host {host!r} is not in the trusted sources allowlist: "
            + ", ".join(sorted(trusted_hosts)))


class _TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that validates each redirect destination against the allowlist.

    Prevents a compromised trusted host from redirecting downloads to an untrusted
    domain: only hosts in HARNESS_TRUSTED_SOURCES are permitted; redirects off the
    allowlist are refused.
    """

    def __init__(self, trusted_hosts: frozenset[str]) -> None:
        super().__init__()
        self._trusted_hosts = trusted_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        _assert_trusted(newurl, self._trusted_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _trusted_opener(trusted_hosts: frozenset[str]) -> urllib.request.OpenerDirector:
    """Return an OpenerDirector that refuses redirects to untrusted hosts."""
    return urllib.request.build_opener(_TrustedRedirectHandler(trusted_hosts))


def _fetch_json(uri: str, trusted_hosts: frozenset[str]) -> dict:
    _assert_trusted(uri, trusted_hosts)
    opener = _trusted_opener(trusted_hosts)
    req = urllib.request.Request(uri, headers={"User-Agent": "harness-image/1.0"})
    with opener.open(req, timeout=30) as resp:  # noqa: S310 — https-only, redirect-validated
        return json.loads(resp.read())


# ── installed detection ───────────────────────────────────────────────────────

def _detect_installed(harness: str, cli_path: str | None = None) -> str | None:
    """Return the installed CLI version string, or None when not found/parseable."""
    if harness == "claude":
        candidates = [cli_path or "claude"]
        version_flag = "--version"
        # Output: "Claude Code 1.2.3" or just "1.2.3"
        pattern = re.compile(r"(\d+\.\d+\.\d+)")
    elif harness == "codex":
        candidates = [cli_path or "codex"]
        version_flag = "--version"
        pattern = re.compile(r"(\d+\.\d+\.\d+)")
    elif harness == "kiro":
        candidates = [cli_path or "kiro-cli", "kiro"]
        version_flag = "--version"
        pattern = re.compile(r"(\d+\.\d+\.\d+)")
    else:
        return None

    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, version_flag],
                capture_output=True, text=True, timeout=10,
            )
            combined = (result.stdout + result.stderr).strip()
            m = pattern.search(combined)
            if m:
                log.info("[%s] detected installed version: %s", harness, m.group(1))
                return m.group(1)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
    log.warning("[%s] installed CLI not found on PATH; falling back to pinned", harness)
    return None


# ── latest resolution ─────────────────────────────────────────────────────────

def _resolve_codex_latest(trusted_hosts: frozenset[str]) -> tuple[str, str, str]:
    """Return (version, uri, sha512-integrity) for the latest Codex linux-arm64 package."""
    meta = _fetch_json(
        "https://registry.npmjs.org/@openai/codex", trusted_hosts)
    latest = meta["dist-tags"]["latest"]
    # Find the linux-arm64 variant
    version_meta = meta["versions"].get(f"{latest}-linux-arm64") or meta["versions"].get(latest)
    if version_meta is None:
        raise RuntimeError(
            f"Codex version {latest} not found in npm registry metadata")
    dist = version_meta["dist"]
    uri = dist["tarball"]
    integrity = dist.get("integrity", "")
    return latest, uri, integrity


def _resolve_claude_latest(trusted_hosts: frozenset[str]) -> tuple[str, str, str]:
    """Return (version, uri, sha256) for the latest claude-agent-sdk wheel."""
    meta = _fetch_json("https://pypi.org/pypi/claude-agent-sdk/json", trusted_hosts)
    latest = meta["info"]["version"]
    # Find the first wheel url
    files = meta["releases"].get(latest, [])
    for f in files:
        if f["filename"].endswith(".whl"):
            uri = f["url"]
            sha256 = f["digests"]["sha256"]
            return latest, uri, f"sha256:{sha256}"
    raise RuntimeError(f"No wheel found for claude-agent-sdk {latest}")


def _resolve_kiro_latest(version: str | None, trusted_hosts: frozenset[str],
                         url_template: str) -> tuple[str, str, str]:
    """Return (version, uri, sha256) for the Kiro CDN download.

    Kiro has no machine-readable version manifest; the version must be provided
    via H_KIRO_HARNESS_VERSION when source=latest.  Integrity uses trust-on-first-use
    (TOFU): the sha256 is downloaded and recorded at plan/approval time so subsequent
    deploys can verify the artifact matches what was approved.
    """
    if not version:
        raise ValueError(
            "KIRO_HARNESS_SOURCE=latest requires KIRO_HARNESS_VERSION to be set "
            "(Kiro has no machine-readable latest-version manifest)")
    uri = url_template.format(version=version, arch=_v.KIRO_CDN_ARCH)
    _assert_trusted(uri, trusted_hosts)
    # TOFU: download the zip now to compute sha256; recorded in the lock file so
    # bundle.py can verify the artifact has not changed between plan and deploy.
    log.info("kiro TOFU: downloading %s to compute sha256 ...", uri)
    opener = _trusted_opener(trusted_hosts)
    with opener.open(uri, timeout=300) as resp:  # noqa: S310 — validated above, redirect-checked
        data = resp.read()
    integrity = "sha256:" + hashlib.sha256(data).hexdigest()
    return version, uri, integrity


# ── plan construction ─────────────────────────────────────────────────────────

def resolve(harness: str, source: str | None, explicit_version: str | None,
            trusted_hosts: frozenset[str] | None = None,
            url_template: str | None = None,
            cli_path: str | None = None) -> ResolvedVersion:
    """Resolve the version and download URI for one harness.

    source defaults to "pinned" when None or empty.
    explicit_version always wins over source-based resolution.
    """
    source = (source or "pinned").lower()
    if source not in HARNESS_SOURCES:
        raise ValueError(
            f"harness_source must be one of {HARNESS_SOURCES}, got {source!r}")
    trusted = trusted_hosts if trusted_hosts is not None else _DEFAULT_TRUSTED_HOSTS

    # --- claude ---------------------------------------------------------------
    if harness == "claude":
        if source == "pinned" or explicit_version:
            ver = explicit_version or _v.CLAUDE_SDK_VERSION
            # Use PyPI JSON for the URI/integrity
            meta = _fetch_json(
                f"https://pypi.org/pypi/claude-agent-sdk/{ver}/json", trusted)
            files = meta.get("urls") or meta.get("releases", {}).get(ver, [])
            for f in files:
                if f["filename"].endswith(".whl"):
                    return ResolvedVersion(
                        harness, ver, f["url"],
                        f"sha256:{f['digests']['sha256']}", source)
            # Source distribution fallback
            for f in files:
                uri = f["url"]
                sha256 = f["digests"]["sha256"]
                return ResolvedVersion(harness, ver, uri, f"sha256:{sha256}", source)
            raise RuntimeError(f"No PyPI file found for claude-agent-sdk {ver}")
        elif source == "installed":
            ver = _detect_installed(harness, cli_path) or _v.CLAUDE_SDK_VERSION
            return resolve(harness, "pinned", ver, trusted, url_template, cli_path)
        else:  # latest
            ver, uri, integrity = _resolve_claude_latest(trusted)
            return ResolvedVersion(harness, ver, uri, integrity, source)

    # --- codex ----------------------------------------------------------------
    elif harness == "codex":
        if source == "pinned" or explicit_version:
            ver = explicit_version or _v.CODEX_VERSION
            pkg = f"@openai/codex@{ver}-linux-arm64"
            # npm registry
            meta = _fetch_json(
                f"https://registry.npmjs.org/@openai/codex/{ver}-linux-arm64",
                trusted)
            dist = meta.get("dist", {})
            return ResolvedVersion(
                harness, ver,
                dist.get("tarball", ""),
                dist.get("integrity", ""),
                source,
            )
        elif source == "installed":
            ver = _detect_installed(harness, cli_path) or _v.CODEX_VERSION
            return resolve(harness, "pinned", ver, trusted, url_template, cli_path)
        else:  # latest
            ver, uri, integrity = _resolve_codex_latest(trusted)
            return ResolvedVersion(harness, ver, uri, integrity, source)

    # --- kiro -----------------------------------------------------------------
    elif harness == "kiro":
        tpl = url_template or _v.KIRO_CDN_URL_TEMPLATE
        if source == "pinned" or (source == "installed" and not explicit_version):
            if source == "installed":
                ver = _detect_installed(harness, cli_path) or _v.KIRO_VERSION
            else:
                ver = explicit_version or _v.KIRO_VERSION
            uri = tpl.format(version=ver, arch=_v.KIRO_CDN_ARCH)
            _assert_trusted(uri, trusted)
            return ResolvedVersion(harness, ver, uri, "", source)
        elif source == "latest":
            ver, uri, integrity = _resolve_kiro_latest(
                explicit_version, trusted, tpl)
            return ResolvedVersion(harness, ver, uri, integrity, source)
        else:
            ver = explicit_version or _v.KIRO_VERSION
            uri = tpl.format(version=ver, arch=_v.KIRO_CDN_ARCH)
            _assert_trusted(uri, trusted)
            return ResolvedVersion(harness, ver, uri, "", source)

    else:
        raise ValueError(f"Unknown harness: {harness!r}")


# ── approval token ────────────────────────────────────────────────────────────

def plan_token(resolved_versions: list[ResolvedVersion]) -> str:
    """SHA-256 digest of the sorted (harness, version, uri) tuples.

    `plan` prints this token; `deploy` requires --approve TOKEN to match.
    """
    canonical = json.dumps(
        sorted([(r.harness, r.version, r.uri) for r in resolved_versions]),
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


# ── lock file ─────────────────────────────────────────────────────────────────

LOCK_FILE = "harness-versions.lock.json"


def read_lock(data_dir: Path) -> dict:
    p = data_dir / LOCK_FILE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def write_lock(data_dir: Path, entries: list[ResolvedVersion], token: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    existing = read_lock(data_dir)
    for r in entries:
        existing[r.harness] = {
            "version": r.version,
            "uri": r.uri,
            "integrity": r.integrity,
            "source": r.source,
            "token": token,
        }
    (data_dir / LOCK_FILE).write_text(json.dumps(existing, indent=2))


def verify_integrity(harness: str, data: bytes, expected_integrity: str) -> None:
    """Verify download integrity; raise RuntimeError on mismatch.

    Supported prefixes: sha256: and sha512: (npm dist.integrity).
    Empty expected_integrity skips the check (Kiro TOFU first-use).
    """
    if not expected_integrity:
        return
    if expected_integrity.startswith("sha256:"):
        algo, expected_hex = "sha256", expected_integrity[7:]
        actual = hashlib.sha256(data).hexdigest()
    elif expected_integrity.startswith("sha512:"):
        # npm encodes sha512 as base64; normalise to hex for comparison
        import base64
        algo = "sha512"
        expected_bytes = base64.b64decode(expected_integrity[7:])
        expected_hex = expected_bytes.hex()
        actual = hashlib.sha512(data).hexdigest()
    else:
        raise ValueError(f"Unknown integrity format: {expected_integrity!r}")
    if actual != expected_hex:
        raise RuntimeError(
            f"{harness} integrity check failed ({algo}): "
            f"expected {expected_hex[:16]}... got {actual[:16]}...")
