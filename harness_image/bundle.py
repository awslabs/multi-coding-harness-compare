# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Bundle building for AgentCore Runtime deployment packages.

Builds the deployment_package.zip for each harness (claude / codex / kiro).
The bundle contains:
  main.py         — health-check HTTP server (AgentCore readiness contract)
  <python deps>   — boto3, mcp, and optionally claude-agent-sdk
  tools/<harness> — the harness CLI binary tree

When the tools tree exceeds the AgentCore zip ceiling (200 MB zipped / 600 MB
unzipped), it is staged as a separate .tar.gz uploaded to S3; the in-VM
bootstrap downloads it from CODECOUNCIL_TOOLS_URI at first boot.

Workstation-config baking: when HARNESS_IMAGE_WORKSTATION_CONFIG=true and the
caller passes a config_snapshot_dir, its contents are copied into
harness-config/<harness>/ inside the bundle for use as fallback defaults by
the in-VM bootstrap.
"""
from __future__ import annotations

import io
import logging
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from urllib.request import urlopen

from . import versions as _v
from .sources import _DEFAULT_TRUSTED_HOSTS, _trusted_opener

log = logging.getLogger(__name__)

# Health-check server only — AgentCore requires an HTTP endpoint.
# All harness work arrives via InvokeAgentRuntimeCommand, not HTTP.
_MAIN_PY = (
    "# Satisfies AgentCore HTTP readiness contract only; harness work arrives via"
    " InvokeAgentRuntimeCommand.\n"
    "import http.server, json\n\n"
    "class _H(http.server.BaseHTTPRequestHandler):\n"
    "    def log_message(self, *a): pass\n"
    "    def do_GET(self):\n"
    "        if self.path == '/ping':\n"
    "            b = json.dumps({'status': 'Healthy'}).encode()\n"
    "            self.send_response(200)\n"
    "            self.send_header('Content-Type', 'application/json')\n"
    "            self.send_header('Content-Length', str(len(b)))\n"
    "            self.end_headers(); self.wfile.write(b)\n"
    "        else:\n"
    "            self.send_response(404); self.end_headers()\n"
    "    def do_POST(self):\n"
    "        n = int(self.headers.get('Content-Length', 0))\n"
    "        self.rfile.read(n)\n"
    "        b = json.dumps({'status': 'ok'}).encode()\n"
    "        self.send_response(200)\n"
    "        self.send_header('Content-Type', 'application/json')\n"
    "        self.send_header('Content-Length', str(len(b)))\n"
    "        self.end_headers(); self.wfile.write(b)\n\n"
    "http.server.HTTPServer(('0.0.0.0', 8080), _H).serve_forever()\n"
)


def _run(cmd: list, timeout: int = 600, capture: bool = True) -> tuple[int, str, str]:
    import subprocess
    r = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
    return r.returncode, r.stdout if capture else "", r.stderr if capture else ""


def _vendor_python_deps(target: Path, harness: str, uv: str) -> None:
    """Install aarch64-manylinux2014 binary wheels for the target dir."""
    pkgs = [f"boto3=={_v.BOTO3_VERSION}", f"mcp=={_v.MCP_VERSION}"]
    if harness == "claude":
        pkgs.append(_v.CLAUDE_SDK_REQ)
    cmd = [uv, "pip", "install",
           "--python-platform", "aarch64-manylinux2014",
           "--python-version", "3.12",
           "--only-binary=:all:",
           "--target", str(target),
           *pkgs]
    code, _, err = _run(cmd, timeout=600)
    if code:
        raise RuntimeError(
            f"uv pip install failed for {harness} (exit {code}):\n{err[-2000:]}")
    # Strip __pycache__ and *.pyc to save space
    for pycache in target.rglob("__pycache__"):
        shutil.rmtree(str(pycache), ignore_errors=True)
    for pyc in target.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)


def _vendor_codex(tools_bin: Path, version: str | None = None) -> None:
    """npm pack the linux-arm64 Codex package and keep the aarch64 vendor tree."""
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError(
            "npm not found; install Node.js on the provisioning host to vendor Codex")
    ver = version or _v.CODEX_VERSION
    package = f"@openai/codex@{ver}-linux-arm64"
    tools_bin.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        code, out, err = _run(
            [npm, "pack", package, "--pack-destination", tmp], timeout=300)
        if code:
            raise RuntimeError(f"npm pack codex failed (exit {code}):\n{err[-1000:]}")
        tarballs = list(Path(tmp).glob("*.tgz"))
        if not tarballs:
            raise RuntimeError(f"npm pack produced no .tgz in {tmp}")
        tgz = tarballs[0]
        with tarfile.open(str(tgz), "r:gz") as tf:
            # The vendor tree lives at package/vendor/aarch64-unknown-linux-musl/
            prefix = "package/vendor/aarch64-unknown-linux-musl"
            for member in tf.getmembers():
                rel = PurePosixPath(member.name)
                if not member.name.startswith(prefix + "/") and member.name != prefix:
                    continue
                rest = "/".join(rel.parts[3:])  # strip package/vendor/aarch64-...
                if not rest:
                    continue
                dst = tools_bin / rest
                dst.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    dst.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    fobj = tf.extractfile(member)
                    if fobj:
                        dst.write_bytes(fobj.read())
                        if member.mode & 0o111:
                            dst.chmod(0o755)
    log.info("codex vendor tree extracted to %s", tools_bin)


def _extract_kiro(tools_bin: Path, version: str | None = None,
                  url_template: str | None = None,
                  expected_integrity: str | None = None) -> None:
    """Download the Kiro CDN zip and extract only kirocli/bin/kiro-cli* entries.

    When expected_integrity is provided (sha256:HEX), the downloaded bytes are
    verified before extraction to enforce the TOFU lock recorded at approval time.
    """
    from .sources import verify_integrity as _verify_integrity
    tools_bin.mkdir(parents=True, exist_ok=True)
    ver = version or _v.KIRO_VERSION
    tpl = url_template or _v.KIRO_CDN_URL_TEMPLATE
    cdn_url = tpl.format(version=ver, arch=_v.KIRO_CDN_ARCH)
    log.info("downloading kiro %s from CDN (~596 MB) ...", ver)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_f:
        tmp_path = Path(tmp_f.name)
    try:
        # Use the redirect-validating opener so a CDN redirect to an untrusted host
        # is refused.
        _opener = _trusted_opener(_DEFAULT_TRUSTED_HOSTS)
        with _opener.open(cdn_url, timeout=300) as resp:  # noqa: S310 — https-only, redirect-validated
            zip_bytes = resp.read()
        if expected_integrity:
            _verify_integrity("kiro", zip_bytes, expected_integrity)
        tmp_path.write_bytes(zip_bytes)
        import zipfile as _zf
        with _zf.ZipFile(str(tmp_path)) as zf:
            extracted = []
            for name in zf.namelist():
                p = PurePosixPath(name)
                if (len(p.parts) >= 3 and p.parts[0] == "kirocli"
                        and p.parts[1] == "bin"
                        and p.parts[2].startswith("kiro-cli")):
                    dst = tools_bin / p.parts[2]
                    data = zf.read(name)
                    dst.write_bytes(data)
                    dst.chmod(0o755)
                    extracted.append(dst.name)
            if not extracted:
                raise RuntimeError(
                    "No kirocli/bin/kiro-cli* entries found in Kiro CDN zip")
            log.info("kiro binaries extracted: %s", extracted)
    finally:
        tmp_path.unlink(missing_ok=True)


def _set_permissions(root: Path) -> None:
    """644 for files, 755 for dirs and known executables."""
    for item in root.rglob("*"):
        if item.is_symlink():
            continue
        if item.is_dir():
            item.chmod(0o755)
        else:
            current = item.stat().st_mode
            if current & stat.S_IXUSR:
                item.chmod(0o755)
            else:
                item.chmod(0o644)


def _dir_sizes(root: Path) -> tuple[int, int]:
    """Return (total_unzipped_bytes, approx_zipped_bytes) via a quick zip pass."""
    unzipped = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                zf.write(str(f), str(f.relative_to(root)))
    return unzipped, len(buf.getvalue())


def build_bundle(
    harness: str,
    tmp_dir: Path,
    *,
    version: str | None = None,
    kiro_url_template: str | None = None,
    config_snapshot_dir: Path | None = None,
    uv: str | None = None,
    expected_integrity: str | None = None,
) -> tuple[Path, dict | None]:
    """Build deployment_package.zip for *harness* inside tmp_dir.

    Returns (zip_path, staged_tools_info) where staged_tools_info is non-None
    when the tools tree was too large to inline and should be staged to S3.
    staged_tools_info = {"harness": h, "version": v, "src": Path, "key_suffix": str}

    version overrides the pinned default for the harness.
    kiro_url_template overrides the Kiro CDN URL template.
    config_snapshot_dir: when set, its contents are copied into
    harness-config/<harness>/ inside the bundle (workstation-config baking).
    """
    _uv = uv or shutil.which("uv")
    if not _uv:
        raise RuntimeError(
            "uv is not on PATH; install uv to provision AgentCore runtimes")

    bundle_dir = tmp_dir / f"bundle_{harness}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    (bundle_dir / "main.py").write_text(_MAIN_PY)

    log.info("[%s] vendoring Python deps ...", harness)
    _vendor_python_deps(bundle_dir, harness, _uv)

    # Harness tool payload
    tools_dir = bundle_dir / "tools" / harness
    tools_dir.mkdir(parents=True, exist_ok=True)
    staged_info = None

    if harness == "codex":
        log.info("[%s] vendoring Codex aarch64 tree ...", harness)
        _vendor_codex(tools_dir, version)
        effective_version = version or _v.CODEX_VERSION
    elif harness == "kiro":
        ver = version or _v.KIRO_VERSION
        log.info("[%s] fetching kiro %s ...", harness, ver)
        _extract_kiro(tools_dir, ver, kiro_url_template,
                      expected_integrity=expected_integrity)
        effective_version = ver
    else:
        # claude: wheel ships the binary at claude_agent_sdk/_bundled/claude; nothing extra
        effective_version = version or _v.CLAUDE_SDK_VERSION

    # Workstation-config baking
    if config_snapshot_dir and config_snapshot_dir.is_dir():
        dest = bundle_dir / "harness-config" / harness
        shutil.copytree(str(config_snapshot_dir), str(dest), dirs_exist_ok=True)
        log.info("[%s] baked workstation config snapshot from %s", harness, config_snapshot_dir)

    _set_permissions(bundle_dir)

    # Check sizes with tools included
    unzipped, zipped = _dir_sizes(bundle_dir)
    log.info("[%s] bundle size: %d MB unzipped, %d MB zipped (tools included)",
             harness, unzipped // 1024 // 1024, zipped // 1024 // 1024)

    if zipped > _v.ZIP_CEILING or unzipped > _v.UNZIP_CEILING:
        # Stage tools tree separately and remove from bundle
        log.info(
            "[%s] tools tree exceeds limits; staging to S3 (zipped=%d MB, unzipped=%d MB)",
            harness, zipped // 1024 // 1024, unzipped // 1024 // 1024)
        staged_tar = tmp_dir / f"{harness}-{effective_version}.tar.gz"
        with tarfile.open(str(staged_tar), "w:gz") as tf:
            for item in sorted(tools_dir.rglob("*")):
                if item.is_file():
                    tf.add(str(item), arcname=str(item.relative_to(tools_dir)))
        shutil.rmtree(str(tools_dir), ignore_errors=True)
        (bundle_dir / "tools" / harness).mkdir(parents=True, exist_ok=True)
        staged_info = {
            "harness": harness,
            "version": effective_version,
            "src": staged_tar,
            "key_suffix": f"tools/{harness}-{effective_version}.tar.gz",
        }
        unzipped, zipped = _dir_sizes(bundle_dir)
        log.info("[%s] bundle after staging: %d MB unzipped, %d MB zipped",
                 harness, unzipped // 1024 // 1024, zipped // 1024 // 1024)
    else:
        log.info("[%s] tools vendored in zip (zipped=%d MB, unzipped=%d MB)",
                 harness, zipped // 1024 // 1024, unzipped // 1024 // 1024)

    zip_path = tmp_dir / f"{harness}_deployment_package.zip"
    with zipfile.ZipFile(str(zip_path), "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(bundle_dir.rglob("*")):
            if f.is_file():
                arcname = str(f.relative_to(bundle_dir))
                zf.write(str(f), arcname)
    return zip_path, staged_info
