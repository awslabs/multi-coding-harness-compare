# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Thin re-export shim — all logic lives in harness_image.

Existing callers (arena.cli, tests) import from here; this shim keeps them working
unchanged while the implementation is in the harness_image standalone package.
"""
import json
import time  # imported here so patch("arena.agentcore_provision.time.sleep") works
from pathlib import Path
from urllib.parse import urlsplit

# Re-export constants that callers may reference
from harness_image import versions as _v

_HARNESSES = _v.HARNESSES
_CODEX_PACKAGE = _v.CODEX_PACKAGE
_KIRO_VERSION = _v.KIRO_VERSION
_KIRO_CDN_URL = _v.KIRO_CDN_URL
_CLAUDE_SDK_REQ = _v.CLAUDE_SDK_REQ
_ZIP_CEILING = _v.ZIP_CEILING
_UNZIP_CEILING = _v.UNZIP_CEILING


def _staged_version(harness: str) -> str | None:
    """Pinned version of the harness payload (backward-compat helper)."""
    return {
        "codex": _v.CODEX_VERSION,
        "kiro": _v.KIRO_VERSION,
        "claude": _v.CLAUDE_SDK_VERSION,
    }.get(harness)


def _parse_s3_uri(uri: str):
    u = urlsplit(uri)
    return u.netloc, u.path.strip("/")


def describe(config) -> dict:
    """Return agentcore-runtimes.json as a dict, {} if absent or unreadable."""
    p = config.root / "agentcore-runtimes.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def write_records(config, records: dict) -> str:
    """Persist records to agentcore-runtimes.json; return a ready-to-paste TOML block."""
    from harness_image.deploy import _write_records_data
    return _write_records_data(config.root, records)


# ── build_bundle / stage_tools / upload_bundle re-exports ──────────────────────
from harness_image.bundle import build_bundle, _set_permissions, _dir_sizes  # noqa: F401, E402


def stage_tools(staged_info: dict, bucket: str, prefix: str, s3, account: str) -> str:
    """Upload staged tools tarball to S3; return the s3:// URI."""
    key = f"{prefix}/{staged_info['key_suffix']}"
    s3.put_object(Bucket=bucket, Key=key,
                  Body=staged_info["src"].read_bytes(),
                  ServerSideEncryption="AES256",
                  ExpectedBucketOwner=account)
    return f"s3://{bucket}/{key}"


def upload_bundle(zip_path: Path, bucket: str, key: str, s3, account: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=zip_path.read_bytes(),
                  ServerSideEncryption="AES256", ExpectedBucketOwner=account)


# ── IAM ──────────────────────────────────────────────────────────────────────
from harness_image.iam import trust_policy as _trust_policy_fn  # noqa: F401, E402
from harness_image.iam import execution_policy as _inline_policy_fn  # noqa: F401, E402
from harness_image.iam import runtime_name  # noqa: F401, E402


def _trust_policy(account: str, region: str) -> dict:
    return _trust_policy_fn(account, region)


def _inline_policy(account: str, region: str, bucket: str, prefix: str) -> dict:
    return _inline_policy_fn(account, region, bucket, prefix)


def ensure_execution_role(cfg, account: str, iam, region: str) -> str:
    """Create or refresh the execution role; return its ARN.

    Accepts either an AgentCoreConfig (arena.config) or a DeployConfig
    (harness_image.deploy) so existing tests keep working.
    """
    from harness_image.iam import (
        trust_policy as _tp,
        execution_policy as _ep,
        role_name as _rn,
        policy_name as _pn,
    )

    # Read fields present on both AgentCoreConfig and DeployConfig
    execution_role_arn = getattr(cfg, "execution_role_arn", None)
    if execution_role_arn:
        return execution_role_arn

    role = getattr(cfg, "effective_role_name", None)
    if role is None:
        role = getattr(cfg, "execution_role_name", "MchcBedrockAgentCoreRuntimeRole")

    policy = getattr(cfg, "effective_policy_name", None)
    if policy is None:
        policy = getattr(cfg, "inline_policy_name", "MchcAgentCoreRuntime")

    app_tag = getattr(cfg, "application_tag", "Multi Coding-Harness Compare")
    transfer_uri = getattr(cfg, "transfer_s3_uri", "")
    kms_key_arn = getattr(cfg, "transfer_kms_key_arn", None)
    user_namespace = getattr(cfg, "user_namespace", None)
    isolation = getattr(cfg, "isolation", "shared")
    effective_namespace = user_namespace if isolation == "per-user" else None

    bucket, prefix = _parse_s3_uri(transfer_uri)
    trust = json.dumps(_tp(account, region))
    inline = json.dumps(_ep(account, region, bucket, prefix,
                            kms_key_arn=kms_key_arn,
                            user_namespace=effective_namespace))

    try:
        resp = iam.create_role(
            RoleName=role,
            AssumeRolePolicyDocument=trust,
            Description=f"Execution role for {app_tag} AgentCore Runtime harnesses",
            Tags=[{"Key": "Application", "Value": app_tag}],
        )
        arn = resp["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(RoleName=role, PolicyDocument=trust)
        arn = iam.get_role(RoleName=role)["Role"]["Arn"]

    iam.put_role_policy(RoleName=role, PolicyName=policy, PolicyDocument=inline)

    # IAM propagation — a brief wait reduces CREATE_FAILED odds on new roles
    time.sleep(10)
    return arn


# ── ensure_runtime / wait_ready (used by tests via agentcore_sandbox) ──────────
def ensure_runtime(harness: str, cfg, bundle_key: str, role_arn: str,
                   env_vars: dict, control) -> dict:
    from harness_image.deploy import DeployConfig
    if not isinstance(cfg, DeployConfig):
        dc = DeployConfig.from_arena_config(
            type("_C", (), {"agentcore": cfg, "data_dir": ".arena",
                            "user_namespace": getattr(cfg, "user_namespace", None)})())
    else:
        dc = cfg
    from harness_image.deploy import _ensure_runtime
    return _ensure_runtime(harness, dc, bundle_key, role_arn, env_vars, control)


def wait_ready(harness: str, runtime_id: str, control, timeout: int = 300) -> dict:
    from harness_image.deploy import _wait_ready
    return _wait_ready(harness, runtime_id, control, timeout)


# ── deploy (main arena entry point) ──────────────────────────────────────────
async def deploy(config, harnesses=None, rebuild: bool = True,
                 skip_build: bool = False, *,
                 approve_token: str | None = None,
                 dry_run: bool = False,
                 props: dict | None = None) -> dict:
    """Provision or update AgentCore Runtime resources for the requested harnesses.

    This is the arena-facing entry point.  It delegates to harness_image.deploy.deploy()
    using a DeployConfig derived from the arena Config object, preserving the same
    interface that existing callers rely on.

    approve_token  — approval token from `plan` (required for non-pinned sources)
    dry_run        — resolve versions only; write nothing to AWS
    props          — raw deploy.properties dict forwarded to harness_image (enables
                     H_HARNESS_SOURCE / H_HARNESS_VERSION / HARNESS_IMAGE_WORKSTATION_CONFIG)
    """
    import asyncio
    from harness_image.deploy import DeployConfig
    from harness_image.deploy import deploy as _hi_deploy

    if skip_build:
        rebuild = False

    cfg = DeployConfig.from_arena_config(config)
    effective_props = props if props is not None else {}
    result = await _hi_deploy(
        cfg,
        harnesses,
        rebuild=rebuild,
        approve_token=approve_token,
        dry_run=dry_run,
        props=effective_props,
    )
    return result
