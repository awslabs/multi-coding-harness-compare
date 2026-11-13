# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AgentCore Runtime provision and update logic for harness_image.

Provides:
  plan()        Resolve versions and print the plan JSON (no writes).
  deploy()      Build bundles, upload to S3, create/update runtimes, write records.
  status()      Read-only: list deployed runtimes and their versions.

Runtime update safety
---------------------
deploy() updates runtimes in place.  Warm AgentCore sessions keep the old runtime
version until they go idle (idle_session_timeout seconds); the API/worker need no
restart.  agentcore-deploy does not attempt to detect mid-turn sessions cheaply —
the window between the update call and the first invoke of the new version is
typically < 60 s; the caller should prefer deploying when utilisation is low.

This module reads deploy.properties through harness_image.props so it can operate
without the full arena package.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import versions as _v
from .bundle import build_bundle
from .iam import (
    execution_policy,
    trust_policy,
    role_name as iam_role_name,
    policy_name as iam_policy_name,
    runtime_name as iam_runtime_name,
)
from .sources import (
    HARNESS_SOURCES,
    ResolvedVersion,
    plan_token,
    read_lock,
    resolve,
    write_lock,
)

log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_s3_uri(uri: str) -> tuple[str, str]:
    u = urlsplit(uri)
    return u.netloc, u.path.strip("/")


def _runtime_artifact(bundle_key: str, bucket: str, python_runtime: str,
                      entry_point: list) -> dict:
    return {
        "codeConfiguration": {
            "code": {
                "s3": {
                    "bucket": bucket,
                    "prefix": bundle_key,
                }
            },
            "runtime": python_runtime,
            "entryPoint": entry_point,
        }
    }


def _runtime_kwargs(harness: str, bundle_key: str, role_arn: str,
                    env_vars: dict, cfg: "DeployConfig") -> dict:
    bucket, _ = _parse_s3_uri(cfg.transfer_s3_uri)
    return dict(
        agentRuntimeArtifact=_runtime_artifact(
            bundle_key, bucket, cfg.python_runtime, cfg.entry_point),
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        filesystemConfigurations=[
            {"sessionStorage": {"mountPath": cfg.workspace_mount}}
        ],
        lifecycleConfiguration={
            "idleRuntimeSessionTimeout": cfg.idle_session_timeout,
            "maxLifetime": cfg.max_lifetime,
        },
        roleArn=role_arn,
        environmentVariables=env_vars,
        description=(
            f"{cfg.application_tag} {harness} harness "
            "— managed by harness_image.deploy"
        ),
    )


# ── config dataclass ──────────────────────────────────────────────────────────

class DeployConfig:
    """Flat configuration object consumed by deploy functions.

    Can be constructed from a deploy.properties dict (via from_props) or from
    an arena Config object (via from_arena_config).
    """

    def __init__(
        self,
        *,
        transfer_s3_uri: str,
        region: str,
        profile: str | None = None,
        execution_role_arn: str | None = None,
        execution_role_name: str = "MchcBedrockAgentCoreRuntimeRole",
        inline_policy_name: str = "MchcAgentCoreRuntime",
        application_tag: str = "Multi Coding-Harness Compare",
        runtime_name_prefix: str = "mchc",
        workspace_mount: str = "/mnt/workspace",
        idle_session_timeout: int = 900,
        max_lifetime: int = 28800,
        python_runtime: str = "PYTHON_3_12",
        entry_point: list | None = None,
        data_dir: str = ".arena",
        isolation: str = "shared",
        transfer_kms_key_arn: str | None = None,
        user_namespace: str | None = None,
    ):
        self.transfer_s3_uri = transfer_s3_uri
        self.region = region
        self.profile = profile
        self.execution_role_arn = execution_role_arn
        self.execution_role_name = execution_role_name
        self.inline_policy_name = inline_policy_name
        self.application_tag = application_tag
        self.runtime_name_prefix = runtime_name_prefix
        self.workspace_mount = workspace_mount
        self.idle_session_timeout = idle_session_timeout
        self.max_lifetime = max_lifetime
        self.python_runtime = python_runtime
        self.entry_point = entry_point or ["main.py"]
        self.data_dir = data_dir
        self.isolation = isolation
        self.transfer_kms_key_arn = transfer_kms_key_arn
        self.user_namespace = user_namespace

    @property
    def data_root(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    @property
    def effective_role_name(self) -> str:
        return iam_role_name(self.execution_role_name, self._namespace)

    @property
    def effective_policy_name(self) -> str:
        return iam_policy_name(self.inline_policy_name, self._namespace)

    @property
    def _namespace(self) -> str | None:
        return (
            self.user_namespace
            if self.isolation == "per-user" else None
        )

    @classmethod
    def from_props(cls, props: dict) -> "DeployConfig":
        """Construct from a deploy.properties dict (keys are str/str)."""
        def _get(key: str, default=None):
            return props.get(key, default)

        transfer = _get("AGENTCORE_TRANSFER_S3_URI", "")
        region = _get("AWS_REGION", "")
        if not transfer:
            raise ValueError(
                "AGENTCORE_TRANSFER_S3_URI is required in deploy.properties")
        if not region:
            import os
            region = os.environ.get("AWS_REGION", "")
        if not region:
            raise ValueError(
                "AWS_REGION is required (deploy.properties or env var)")

        def _int(key, default):
            v = _get(key)
            return int(v) if v else default

        return cls(
            transfer_s3_uri=transfer,
            region=region,
            profile=_get("AWS_PROFILE") or None,
            execution_role_arn=_get("AGENTCORE_EXECUTION_ROLE_ARN") or None,
            execution_role_name=(
                _get("AGENTCORE_EXECUTION_ROLE_NAME")
                or "MchcBedrockAgentCoreRuntimeRole"
            ),
            inline_policy_name=(
                _get("AGENTCORE_INLINE_POLICY_NAME")
                or "MchcAgentCoreRuntime"
            ),
            application_tag=_get("AGENTCORE_APPLICATION_TAG") or "Multi Coding-Harness Compare",
            runtime_name_prefix=(
                _get("AGENTCORE_RUNTIME_NAME_PREFIX") or "mchc"
            ),
            isolation=_get("AGENTCORE_ISOLATION") or "shared",
            transfer_kms_key_arn=_get("AGENTCORE_TRANSFER_KMS_KEY_ARN") or None,
            user_namespace=_get("USER_NAMESPACE") or None,
            data_dir=_get("DATA_DIR") or ".arena",
        )

    @classmethod
    def from_arena_config(cls, config) -> "DeployConfig":
        """Construct from an arena.config.Config object."""
        ac = config.agentcore
        return cls(
            transfer_s3_uri=ac.transfer_s3_uri,
            region=ac.region,
            profile=ac.profile,
            execution_role_arn=ac.execution_role_arn,
            execution_role_name=ac.execution_role_name,
            inline_policy_name=ac.inline_policy_name,
            application_tag=ac.application_tag,
            runtime_name_prefix=ac.runtime_name_prefix,
            workspace_mount=ac.workspace_mount,
            idle_session_timeout=ac.idle_session_timeout,
            max_lifetime=ac.max_lifetime,
            python_runtime=ac.python_runtime,
            entry_point=ac.entry_point,
            data_dir=config.data_dir,
            isolation=ac.isolation,
            transfer_kms_key_arn=ac.transfer_kms_key_arn,
            user_namespace=config.user_namespace,
        )


# ── IAM helpers ───────────────────────────────────────────────────────────────

def _ensure_execution_role(cfg: DeployConfig, account: str, iam, region: str) -> str:
    """Create or refresh the execution role; return its ARN."""
    if cfg.execution_role_arn:
        log.info("using configured execution_role_arn: %s", cfg.execution_role_arn)
        return cfg.execution_role_arn

    role = cfg.effective_role_name
    bucket, prefix = _parse_s3_uri(cfg.transfer_s3_uri)
    namespace = cfg._namespace
    s3_scope = f"{prefix}/{namespace}" if namespace else prefix

    trust = json.dumps(trust_policy(account, region))
    inline = json.dumps(execution_policy(
        account, region, bucket, prefix,
        kms_key_arn=cfg.transfer_kms_key_arn,
        user_namespace=namespace,
    ))
    policy = cfg.effective_policy_name

    try:
        resp = iam.create_role(
            RoleName=role,
            AssumeRolePolicyDocument=trust,
            Description=f"Execution role for {cfg.application_tag} AgentCore Runtime harnesses",
            Tags=[{"Key": "Application", "Value": cfg.application_tag}],
        )
        arn = resp["Role"]["Arn"]
        log.info("created IAM role %s", arn)
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(RoleName=role, PolicyDocument=trust)
        arn = iam.get_role(RoleName=role)["Role"]["Arn"]
        log.info("refreshed trust policy for IAM role %s", arn)

    iam.put_role_policy(RoleName=role, PolicyName=policy, PolicyDocument=inline)
    log.info("put inline policy %s on %s", policy, role)

    # IAM propagation — a brief wait reduces CREATE_FAILED odds on new roles
    log.info("waiting 10 s for IAM propagation ...")
    time.sleep(10)
    return arn


# ── runtime helpers ───────────────────────────────────────────────────────────

def _ensure_runtime(harness: str, cfg: DeployConfig, bundle_key: str,
                    role_arn: str, env_vars: dict, control) -> dict:
    """Create or update the AgentCore runtime for *harness*; return the API response."""
    namespace = cfg._namespace
    name = iam_runtime_name(cfg.runtime_name_prefix, harness, namespace)
    kwargs = _runtime_kwargs(harness, bundle_key, role_arn, env_vars, cfg)

    existing_id = None
    try:
        token = None
        while existing_id is None:
            resp = control.list_agent_runtimes(**({"nextToken": token} if token else {}))
            for rt in resp.get("agentRuntimes", []):
                if rt.get("agentRuntimeName") == name:
                    existing_id = (rt.get("agentRuntimeId")
                                   or rt["agentRuntimeArn"].split("/")[-1])
                    break
            token = resp.get("nextToken")
            if not token:
                break
    except Exception as exc:
        log.warning("list_agent_runtimes: %s", exc)

    if existing_id:
        log.info("[%s] updating runtime %s", harness, existing_id)
        return control.update_agent_runtime(agentRuntimeId=existing_id, **kwargs)
    else:
        log.info("[%s] creating runtime %s", harness, name)
        return control.create_agent_runtime(
            agentRuntimeName=name,
            tags={"Application": cfg.application_tag, "Harness": harness},
            **kwargs)


def _wait_ready(harness: str, runtime_id: str, control, timeout: int = 300) -> dict:
    """Poll until the runtime reaches READY; return get_agent_runtime result."""
    deadline = time.monotonic() + timeout
    last_status = "UNKNOWN"
    while time.monotonic() < deadline:
        info = control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = info.get("status", "UNKNOWN")
        if status != last_status:
            log.info("[%s] runtime status: %s", harness, status)
            last_status = status
        if status == "READY":
            return info
        if status in ("CREATE_FAILED", "UPDATE_FAILED"):
            reason = info.get("failureReason", "no reason provided")
            raise RuntimeError(
                f"AgentCore runtime '{harness}' reached {status}: {reason}")
        time.sleep(10)
    raise TimeoutError(
        f"AgentCore runtime '{harness}' did not become READY within {timeout}s "
        f"(last status: {last_status})")


def _stage_tools(staged_info: dict, bucket: str, prefix: str, s3, account: str) -> str:
    key = f"{prefix}/{staged_info['key_suffix']}"
    log.info("staging tools: s3://%s/%s", bucket, key)
    s3.put_object(Bucket=bucket, Key=key,
                  Body=staged_info["src"].read_bytes(),
                  ServerSideEncryption="AES256",
                  ExpectedBucketOwner=account)
    return f"s3://{bucket}/{key}"


def _upload_bundle(zip_path: Path, bucket: str, key: str, s3, account: str) -> None:
    log.info("uploading bundle: s3://%s/%s (%d MB)", bucket, key,
             zip_path.stat().st_size // 1024 // 1024)
    s3.put_object(Bucket=bucket, Key=key, Body=zip_path.read_bytes(),
                  ServerSideEncryption="AES256", ExpectedBucketOwner=account)


def _describe(data_dir: Path) -> dict:
    """Return agentcore-runtimes.json as a dict, {} if absent."""
    p = data_dir / "agentcore-runtimes.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _write_records(data_dir: Path, records: dict) -> str:
    """Persist records to agentcore-runtimes.json; return a ready-to-paste TOML block."""
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "agentcore-runtimes.json"
    existing = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except Exception:
            pass
    existing.update(records)
    out_path.write_text(json.dumps(existing, indent=2, default=str))
    log.info("wrote %s", out_path)

    lines = ["[agentcore.runtimes]"]
    for harness, rec in sorted(records.items()):
        lines.append(f"[agentcore.runtimes.{harness}]")
        lines.append(f'arn = "{rec["arn"]}"')
        if rec.get("qualifier") and rec["qualifier"] != "DEFAULT":
            lines.append(f'qualifier = "{rec["qualifier"]}"')
        if rec.get("bundle"):
            lines.append(f'bundle = "{rec["bundle"]}"')
    return "\n".join(lines)


# Public alias so arena.agentcore_provision can import by this name
_write_records_data = _write_records


# ── harness version config helper ─────────────────────────────────────────────

class HarnessVersionConfig:
    """Per-harness version configuration extracted from deploy.properties."""

    def __init__(self, props: dict, harness: str):
        prefix = harness.upper()
        self.source = props.get(f"{prefix}_HARNESS_SOURCE") or "pinned"
        self.version = props.get(f"{prefix}_HARNESS_VERSION") or None
        self.cli_path = props.get(f"{prefix}_CLI_PATH") or None


# ── plan ──────────────────────────────────────────────────────────────────────

def plan(
    cfg: DeployConfig,
    harnesses: list[str] | None,
    props: dict,
    *,
    trusted_hosts=None,
    kiro_url_template: str | None = None,
) -> dict:
    """Resolve versions for all harnesses; return plan dict (no AWS calls).

    The plan dict contains:
      harnesses: {harness: {version, uri, integrity, source}}
      token:     approval token (SHA-256 of sorted resolved plan)
    """
    requested = list(harnesses) if harnesses else list(_v.HARNESSES)
    _trusted = frozenset(trusted_hosts) if trusted_hosts else None

    resolved = []
    for harness in requested:
        hv = HarnessVersionConfig(props, harness)
        rv = resolve(
            harness,
            hv.source,
            hv.version,
            _trusted,
            kiro_url_template,
            hv.cli_path,
        )
        resolved.append(rv)

    token = plan_token(resolved)
    return {
        "harnesses": {
            r.harness: {
                "version": r.version,
                "uri": r.uri,
                "integrity": r.integrity,
                "source": r.source,
            }
            for r in resolved
        },
        "token": token,
        "resolved": resolved,  # kept for internal use; not serialised in CLI output
    }


# ── deploy ────────────────────────────────────────────────────────────────────

async def deploy(
    cfg: DeployConfig,
    harnesses: list[str] | None = None,
    *,
    rebuild: bool = True,
    approve_token: str | None = None,
    dry_run: bool = False,
    props: dict | None = None,
    kiro_url_template: str | None = None,
    config_snapshot_dirs: dict[str, Path] | None = None,
) -> dict:
    """Build bundles, upload to S3, create/update runtimes, write records.

    When any harness uses source=installed or source=latest, --approve TOKEN is
    required: call plan() first to get the token, then pass it here.
    """
    _props = props or {}
    requested = list(harnesses) if harnesses else list(_v.HARNESSES)

    # Collect per-harness version configs
    version_configs = {h: HarnessVersionConfig(_props, h) for h in requested}
    needs_approval = any(
        (version_configs[h].source or "pinned") != "pinned"
        for h in requested
    )
    if needs_approval and not approve_token:
        plan_result = plan(cfg, requested, _props, kiro_url_template=kiro_url_template)
        raise RuntimeError(
            f"Non-pinned source strategies require --approve TOKEN. "
            f"Run `plan` first and pass --approve {plan_result['token']}")

    if approve_token:
        plan_result = plan(cfg, requested, _props, kiro_url_template=kiro_url_template)
        if plan_result["token"] != approve_token:
            raise RuntimeError(
                f"Approval token mismatch. Expected {plan_result['token']!r}.")
        resolved_versions = plan_result["resolved"]
    else:
        resolved_versions = [
            ResolvedVersion(h, _v.pinned_version(h), "", "", "pinned")
            for h in requested
        ]

    if dry_run:
        return {
            "dry_run": True,
            "harnesses": {r.harness: {"version": r.version} for r in resolved_versions},
        }

    # AWS session
    def _sts_identity():
        import boto3
        session = boto3.Session(profile_name=cfg.profile, region_name=cfg.region)
        return session.client("sts").get_caller_identity(), session

    identity, boto_session = await asyncio.to_thread(_sts_identity)
    account = identity["Account"]
    bucket, prefix = _parse_s3_uri(cfg.transfer_s3_uri)
    log.info("deploying to account %s, bucket=%s, prefix=%s", account, bucket, prefix)

    iam = await asyncio.to_thread(lambda: boto_session.client("iam"))
    s3 = await asyncio.to_thread(lambda: boto_session.client("s3"))
    control = await asyncio.to_thread(
        lambda: boto_session.client(
            "bedrock-agentcore-control", region_name=cfg.region))

    result: dict = {
        "account": account,
        "region": cfg.region,
        "harnesses": {},
    }

    # Version map for bundle builder
    version_map = {r.harness: r.version for r in resolved_versions}
    # Integrity map from resolved versions (non-empty for approved non-pinned sources)
    integrity_map = {r.harness: r.integrity for r in resolved_versions if r.integrity}
    # Also merge any locked integrity for harnesses where resolved had empty integrity
    locked = read_lock(cfg.data_root)
    for h, entry in locked.items():
        if h not in integrity_map and entry.get("integrity"):
            integrity_map[h] = entry["integrity"]

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        bundle_keys: dict[str, str] = {}
        staged_uris: dict[str, str] = {}
        staged_infos: dict[str, dict] = {}

        if rebuild:
            for harness in requested:
                log.info("[%s] building deployment bundle ...", harness)
                snap_dir = (config_snapshot_dirs or {}).get(harness)
                zip_path, staged_info = await asyncio.to_thread(
                    build_bundle,
                    harness, tmp,
                    version=version_map.get(harness),
                    kiro_url_template=kiro_url_template,
                    config_snapshot_dir=snap_dir,
                    expected_integrity=integrity_map.get(harness),
                )
                bundle_key = f"{prefix}/bundles/{harness}/deployment_package.zip"
                await asyncio.to_thread(
                    _upload_bundle, zip_path, bucket, bundle_key, s3, account)
                bundle_keys[harness] = bundle_key
                if staged_info:
                    uri = await asyncio.to_thread(
                        _stage_tools, staged_info, bucket, prefix, s3, account)
                    staged_uris[harness] = uri
                    staged_infos[harness] = staged_info
                    log.info("[%s] tools staged at %s", harness, uri)
        else:
            from botocore.exceptions import ClientError
            for harness in requested:
                bundle_keys[harness] = f"{prefix}/bundles/{harness}/deployment_package.zip"
                log.info("[%s] skip_build: reusing s3://%s/%s",
                         harness, bucket, bundle_keys[harness])
                existing = _describe(cfg.data_root)
                ver = version_map.get(harness) or _v.pinned_version(harness)
                uri = existing.get(harness, {}).get("tools_uri")
                if not uri and ver:
                    key = f"{prefix}/tools/{harness}-{ver}.tar.gz"
                    try:
                        await asyncio.to_thread(s3.head_object, Bucket=bucket, Key=key)
                        uri = f"s3://{bucket}/{key}"
                        log.info("[%s] recovered staged tools from %s", harness, uri)
                    except ClientError:
                        uri = None
                if uri:
                    staged_uris[harness] = uri
                    staged_infos[harness] = {
                        "version": existing.get(harness, {}).get("tools_version") or ver}

        # Ensure IAM execution role
        role_arn = await asyncio.to_thread(
            _ensure_execution_role, cfg, account, iam, cfg.region)
        result["execution_role_arn"] = role_arn

        # Ensure and wait for each runtime
        records: dict = {}
        for harness in requested:
            env_vars = {
                "AWS_REGION": cfg.region,
                "AWS_DEFAULT_REGION": cfg.region,
                "PYTHONUNBUFFERED": "1",
                "LANG": "C.UTF-8",
                "MCHC_HARNESS": harness,
            }
            if harness == "claude":
                env_vars["CLAUDE_CODE_USE_BEDROCK"] = "1"
            elif harness == "codex":
                env_vars["MCHC_CODEX_REGION"] = cfg.region
            if staged_uris.get(harness):
                env_vars["MCHC_TOOLS_URI"] = staged_uris[harness]

            resp = await asyncio.to_thread(
                _ensure_runtime, harness, cfg, bundle_keys[harness],
                role_arn, env_vars, control)

            runtime_id = (
                resp.get("agentRuntimeId")
                or resp.get("agentRuntimeArn")
                or resp.get("agentRuntime", {}).get("agentRuntimeId")
                or resp.get("agentRuntime", {}).get("agentRuntimeArn")
            )
            if not runtime_id:
                raise RuntimeError(
                    f"[{harness}] could not determine runtime ID from response: "
                    + json.dumps(resp, default=str)[:500])

            log.info("[%s] waiting for runtime %s to become READY ...", harness, runtime_id)
            info = await asyncio.to_thread(_wait_ready, harness, runtime_id, control)

            arn = info.get("agentRuntimeArn") or runtime_id
            records[harness] = {
                "arn": arn,
                "id": info.get("agentRuntimeId", runtime_id),
                "version": info.get("agentRuntimeVersion"),
                "bundle": f"s3://{bucket}/{bundle_keys[harness]}",
                "qualifier": "DEFAULT",
                "tools_uri": staged_uris.get(harness),
                "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            }
            si = staged_infos.get(harness)
            if staged_uris.get(harness):
                _, tools_key = _parse_s3_uri(staged_uris[harness])
                records[harness]["tools_key"] = tools_key
                if si and si.get("version"):
                    records[harness]["tools_version"] = si["version"]
            result["harnesses"][harness] = records[harness]
            log.info("[%s] ready: %s", harness, arn)

    # Write records and lock file
    toml_block = _write_records(cfg.data_root, records)
    result["toml_block"] = toml_block
    if approve_token and resolved_versions:
        write_lock(cfg.data_root, resolved_versions, approve_token)
    log.info("deployment complete\n%s", toml_block)
    return result


# ── status ────────────────────────────────────────────────────────────────────

async def status(cfg: DeployConfig, harnesses: list[str] | None = None) -> dict:
    """Read-only: list deployed runtimes and their versions from AgentCore."""
    requested = list(harnesses) if harnesses else list(_v.HARNESSES)

    def _list_runtimes():
        import boto3
        session = boto3.Session(profile_name=cfg.profile, region_name=cfg.region)
        control = session.client("bedrock-agentcore-control", region_name=cfg.region)
        runtimes = []
        token = None
        while True:
            resp = control.list_agent_runtimes(**({"nextToken": token} if token else {}))
            runtimes.extend(resp.get("agentRuntimes", []))
            token = resp.get("nextToken")
            if not token:
                break
        return runtimes

    all_runtimes = await asyncio.to_thread(_list_runtimes)
    prefix = cfg.runtime_name_prefix
    namespace = cfg._namespace
    result: dict = {}
    for harness in requested:
        name = (f"{prefix}_{harness}_{namespace}" if namespace
                else f"{prefix}_{harness}")
        for rt in all_runtimes:
            if rt.get("agentRuntimeName") == name:
                result[harness] = {
                    "name": name,
                    "arn": rt.get("agentRuntimeArn"),
                    "id": rt.get("agentRuntimeId"),
                    "status": rt.get("status"),
                    "version": rt.get("agentRuntimeVersion"),
                }
                break
        else:
            result[harness] = {"name": name, "status": "NOT_FOUND"}
    return result
