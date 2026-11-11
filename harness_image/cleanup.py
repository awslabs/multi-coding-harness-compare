# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Resource cleanup for AgentCore harness deployments.

Discovers and removes everything that ``harness_image deploy`` created:

  - Custom AgentCore runtime endpoints (AgentCore removes the DEFAULT endpoint with its runtime)
  - AgentCore runtimes (polled to DELETING completion)
  - Workload identities auto-created per runtime at CreateAgentRuntime time
  - CloudWatch log groups under /aws/bedrock-agentcore/runtimes/<runtime_id>-
    (skipped when --keep-logs)
  - Versioned S3 objects under the transfer prefix owned by this deployment
  - IAM inline policy and role (when not pinned via AGENTCORE_EXECUTION_ROLE_ARN)
  - Local state files: agentcore-runtimes.json, harness-versions.lock.json,
    agentcore-sessions/<id>.json for sessions belonging to deleted runtimes

Entry point
-----------
``python -m harness_image [--properties PATH] cleanup
    [--approve TOKEN] [--include-artifacts] [--keep-logs]``

Consent flow
------------
Without ``--approve``:

  - If both stdin and stdout are TTYs: print a human-readable plan to stderr,
    then prompt the user to type the AWS account ID exactly; anything other than
    the correct account ID aborts with nothing deleted (exit 1).
  - Otherwise (non-TTY): print the plan JSON with the token to stdout and exit 3
    (approval required) — nothing is deleted.

With ``--approve TOKEN``:

  - Re-discover; if the token does not match the current plan, exit 3 with the
    new plan JSON and delete nothing.
  - If the token matches, proceed to delete.

A plan with nothing to delete exits 0 without prompting.

Exit codes
----------
0   Clean (nothing to delete, or all deletions succeeded)
1   Error or user aborted (JSON summary on stdout when deletions ran)
2   Usage error (empty S3 prefix; output to stderr)
3   Approval required — re-run with ``--approve TOKEN`` from the plan JSON
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, unquote

from . import versions as _v
from .iam import runtime_name as _runtime_name

log = logging.getLogger(__name__)

# Bounded wait for a runtime to finish deleting (seconds).
_DELETE_TIMEOUT = 300
_POLL_INTERVAL = 10


# ── resource record ───────────────────────────────────────────────────────────

@dataclasses.dataclass
class CleanupResource:
    """One resource entry in the cleanup plan."""
    type: str        # "endpoint", "runtime", "workload_identity", "log_group",
                     # "s3_objects", "iam_policy", "iam_role", "local_file"
    identifier: str  # unique string used in the plan token
    action: str      # "delete" or "keep"
    reason: str      # human-readable reason for the action
    meta: dict = dataclasses.field(default_factory=dict)


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_s3_uri(uri: str) -> tuple[str, str]:
    u = urlsplit(uri)
    return u.netloc, u.path.strip("/")


def cleanup_token(account: str, region: str, resources: list[CleanupResource]) -> str:
    """Short SHA-256 over account, region and sorted identifiers of resources to delete."""
    identifiers = sorted(r.identifier for r in resources if r.action == "delete")
    canonical = json.dumps(
        {"account": account, "region": region, "identifiers": identifiers},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ── discovery ─────────────────────────────────────────────────────────────────

def discover(
    cfg: Any,          # harness_image.deploy.DeployConfig
    session: Any,      # boto3.Session
    account: str,
    *,
    include_artifacts: bool = False,
    artifact_s3_uri: str | None = None,
    keep_logs: bool = False,
) -> list[CleanupResource]:
    """Discover all resources this deployment created.  Read-only; no deletions.

    Parameters
    ----------
    cfg:
        DeployConfig constructed from deploy.properties.
    session:
        Authenticated boto3 Session for ``cfg.region``.
    account:
        Caller's AWS account ID (from STS GetCallerIdentity).
    include_artifacts:
        When True, also include the ARTIFACT_S3_URI prefix in the plan.
    artifact_s3_uri:
        The value of ARTIFACT_S3_URI from deploy.properties; used only when
        include_artifacts is True.
    keep_logs:
        When True, list log groups as "keep" instead of "delete".
    """
    resources: list[CleanupResource] = []

    control = session.client("bedrock-agentcore-control", region_name=cfg.region)
    s3 = session.client("s3")
    iam_client = session.client("iam")
    logs_client = session.client("logs")

    namespace = cfg._namespace
    name_prefix = cfg.runtime_name_prefix

    # ── AgentCore runtimes ────────────────────────────────────────────────────
    all_runtimes: list[dict] = []
    token: str | None = None
    while True:
        resp = control.list_agent_runtimes(**({"nextToken": token} if token else {}))
        all_runtimes.extend(resp.get("agentRuntimes", []))
        token = resp.get("nextToken")
        if not token:
            break

    for harness in _v.HARNESSES:
        expected_name = _runtime_name(name_prefix, harness, namespace)
        rt = next(
            (r for r in all_runtimes if r.get("agentRuntimeName") == expected_name),
            None,
        )
        if rt is None:
            log.debug("runtime %r not found — nothing to clean up for %s", expected_name, harness)
            continue

        runtime_id = (
            rt.get("agentRuntimeId")
            or rt.get("agentRuntimeArn", "").rsplit("/", 1)[-1]
        )
        runtime_arn = rt.get("agentRuntimeArn", "")

        # Get full details to read workloadIdentityDetails.
        try:
            rt_info = control.get_agent_runtime(agentRuntimeId=runtime_id)
        except Exception as exc:
            log.warning("get_agent_runtime(%s): %s", runtime_id, exc)
            rt_info = rt

        workload_arn = rt_info.get("workloadIdentityDetails", {}).get("workloadIdentityArn") or ""
        # The workload identity name is the last path component of the ARN.
        workload_name = workload_arn.rsplit("/", 1)[-1] if workload_arn else None

        # ── runtime endpoints ─────────────────────────────────────────────────
        try:
            ep_token: str | None = None
            while True:
                ep_resp = control.list_agent_runtime_endpoints(
                    agentRuntimeId=runtime_id,
                    **({"nextToken": ep_token} if ep_token else {}),
                )
                for ep in ep_resp.get("runtimeEndpoints", []):
                    ep_name = ep.get("name", "DEFAULT")
                    if ep_name == "DEFAULT":
                        # Created and managed by AgentCore with the runtime;
                        # DeleteAgentRuntime removes it.
                        continue
                    resources.append(CleanupResource(
                        type="endpoint",
                        identifier=f"endpoint:{runtime_id}/{ep_name}",
                        action="delete",
                        reason=f"endpoint '{ep_name}' of runtime {expected_name!r}",
                        meta={"runtime_id": runtime_id, "endpoint_name": ep_name},
                    ))
                ep_token = ep_resp.get("nextToken")
                if not ep_token:
                    break
        except Exception as exc:
            log.warning("list_agent_runtime_endpoints(%s): %s", runtime_id, exc)

        # ── runtime itself ────────────────────────────────────────────────────
        resources.append(CleanupResource(
            type="runtime",
            identifier=f"runtime:{expected_name}",
            action="delete",
            reason=f"AgentCore runtime {expected_name!r} (ID: {runtime_id})",
            meta={
                "runtime_id": runtime_id,
                "runtime_name": expected_name,
                "runtime_arn": runtime_arn,
            },
        ))

        # ── workload identity ─────────────────────────────────────────────────
        if workload_name:
            resources.append(CleanupResource(
                type="workload_identity",
                identifier=f"workload_identity:{workload_name}",
                action="delete",
                reason=f"workload identity auto-created for runtime {expected_name!r}",
                meta={"workload_name": workload_name},
            ))

        # ── log groups ────────────────────────────────────────────────────────
        # Append '-' so the prefix matches only this runtime's log groups
        # (e.g. /aws/bedrock-agentcore/runtimes/<id>-DEFAULT) and not any
        # other runtime whose ID happens to start with the same characters.
        log_prefix = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-"
        try:
            lg_token: str | None = None
            while True:
                lg_resp = logs_client.describe_log_groups(
                    logGroupNamePrefix=log_prefix,
                    **({"nextToken": lg_token} if lg_token else {}),
                )
                for lg in lg_resp.get("logGroups", []):
                    lg_name = lg["logGroupName"]
                    resources.append(CleanupResource(
                        type="log_group",
                        identifier=f"log_group:{lg_name}",
                        action="keep" if keep_logs else "delete",
                        reason=(
                            "--keep-logs specified" if keep_logs
                            else f"log group for runtime {expected_name!r}"
                        ),
                        meta={"log_group_name": lg_name},
                    ))
                lg_token = lg_resp.get("nextToken")
                if not lg_token:
                    break
        except Exception as exc:
            log.warning("describe_log_groups(%s): %s", log_prefix, exc)

    # ── S3 transfer prefix ────────────────────────────────────────────────────
    bucket, s3_prefix = _parse_s3_uri(cfg.transfer_s3_uri)

    if not s3_prefix:
        raise ValueError(
            f"AGENTCORE_TRANSFER_S3_URI {cfg.transfer_s3_uri!r} has an empty prefix. "
            "cleanup refuses to operate on a whole bucket — set a non-empty prefix in "
            "deploy.properties."
        )

    if cfg.isolation == "per-user" and not namespace:
        raise ValueError(
            "AGENTCORE_ISOLATION=per-user requires USER_NAMESPACE to be set in "
            "deploy.properties — cleanup cannot safely scope S3 deletion without it."
        )

    if cfg.isolation == "per-user" and namespace:
        # Per-user isolation: only delete objects under {prefix}/{namespace}/.
        # The shared bundles/ and tools/ objects under {prefix}/ are listed as keep
        # because other namespaces depend on them.
        own_s3_prefix = f"{s3_prefix}/{namespace}"
        own_prefix_full = own_s3_prefix + "/"
        resources.append(CleanupResource(
            type="s3_objects",
            identifier=f"s3_keep:{bucket}/{s3_prefix}/bundles/",
            action="keep",
            reason="shared bundles/ prefix — used by other namespaces in per-user isolation",
        ))
        resources.append(CleanupResource(
            type="s3_objects",
            identifier=f"s3_keep:{bucket}/{s3_prefix}/tools/",
            action="keep",
            reason="shared tools/ prefix — used by other namespaces in per-user isolation",
        ))
        # Probe before scheduling deletion: only add action='delete' when the prefix
        # actually contains objects.  An empty prefix on a re-run stays as 'keep' so
        # that a second run after a successful cleanup finds nothing to delete and
        # exits 0 without prompting.
        if _s3_prefix_has_objects(s3, bucket, own_prefix_full, account):
            resources.append(CleanupResource(
                type="s3_objects",
                identifier=f"s3:{bucket}/{own_s3_prefix}/",
                action="delete",
                reason=f"per-user transfer prefix for namespace {namespace!r}",
                meta={"bucket": bucket, "prefix": own_prefix_full, "account": account},
            ))
        else:
            resources.append(CleanupResource(
                type="s3_objects",
                identifier=f"s3:{bucket}/{own_s3_prefix}/",
                action="keep",
                reason="prefix is already empty",
            ))
    else:
        # Shared isolation: delete everything under {prefix}/.
        shared_prefix_full = s3_prefix + "/"
        if _s3_prefix_has_objects(s3, bucket, shared_prefix_full, account):
            resources.append(CleanupResource(
                type="s3_objects",
                identifier=f"s3:{bucket}/{s3_prefix}/",
                action="delete",
                reason="transfer prefix for this deployment (shared isolation)",
                meta={"bucket": bucket, "prefix": shared_prefix_full, "account": account},
            ))
        else:
            resources.append(CleanupResource(
                type="s3_objects",
                identifier=f"s3:{bucket}/{s3_prefix}/",
                action="keep",
                reason="prefix is already empty",
            ))

    # ── ARTIFACT_S3_URI (--include-artifacts) ─────────────────────────────────
    if include_artifacts:
        if not artifact_s3_uri:
            raise ValueError(
                "--include-artifacts needs ARTIFACT_S3_URI in deploy.properties")
        art_bucket, art_prefix = _parse_s3_uri(artifact_s3_uri)
        if not art_prefix:
            raise ValueError(
                f"ARTIFACT_S3_URI {artifact_s3_uri!r} has an empty prefix. "
                "cleanup refuses to operate on a whole bucket — set a non-empty prefix in "
                "deploy.properties."
            )
        art_prefix_full = art_prefix + "/"
        # The app does not namespace archives by USER_NAMESPACE, so in per-user
        # isolation this prefix may also hold other users' comparisons.
        shared_note = (" — not namespaced: includes every user's archives under this prefix"
                       if cfg.isolation == "per-user" else "")
        if _s3_prefix_has_objects(s3, art_bucket, art_prefix_full, account):
            resources.append(CleanupResource(
                type="s3_objects",
                identifier=f"s3:{art_bucket}/{art_prefix}/",
                action="delete",
                reason="comparison archives (--include-artifacts)" + shared_note,
                meta={"bucket": art_bucket, "prefix": art_prefix_full,
                      "account": account},
            ))
        else:
            resources.append(CleanupResource(
                type="s3_objects",
                identifier=f"s3:{art_bucket}/{art_prefix}/",
                action="keep",
                reason="comparison archives prefix is already empty",
            ))
    else:
        resources.append(CleanupResource(
            type="s3_objects",
            identifier="artifact_prefix_keep",
            action="keep",
            reason="comparison archives kept — use --include-artifacts to remove them",
        ))

    # ── IAM ───────────────────────────────────────────────────────────────────
    if cfg.execution_role_arn:
        resources.append(CleanupResource(
            type="iam_role",
            identifier=f"iam_role:{cfg.execution_role_arn}",
            action="keep",
            reason="AGENTCORE_EXECUTION_ROLE_ARN is set — externally managed role",
        ))
    else:
        role = cfg.effective_role_name
        policy = cfg.effective_policy_name
        try:
            role_resp = iam_client.get_role(RoleName=role)
            role_data = role_resp["Role"]

            # Verify Application tag matches this deployment.
            tags = {t["Key"]: t["Value"] for t in role_data.get("Tags", [])}
            if tags.get("Application") != cfg.application_tag:
                resources.append(CleanupResource(
                    type="iam_role",
                    identifier=f"iam_role:{role}",
                    action="keep",
                    reason=(
                        f"tag Application={tags.get('Application')!r} does not match "
                        f"configured application_tag={cfg.application_tag!r}"
                    ),
                ))
            else:
                # Verify the trust policy allows bedrock-agentcore.amazonaws.com.
                trust_doc = role_data.get("AssumeRolePolicyDocument", {})
                if isinstance(trust_doc, str):
                    trust_doc = json.loads(unquote(trust_doc))
                has_agentcore_trust = any(
                    _principal_allows_agentcore(stmt)
                    for stmt in trust_doc.get("Statement", [])
                )

                if not has_agentcore_trust:
                    resources.append(CleanupResource(
                        type="iam_role",
                        identifier=f"iam_role:{role}",
                        action="keep",
                        reason="trust policy does not allow bedrock-agentcore.amazonaws.com",
                    ))
                else:
                    # Check for extra inline or managed policies; only delete
                    # the role when no other policies remain after removing ours.
                    # Both APIs are paginated (IsTruncated + Marker, default 100/page).
                    inline_names: list[str] = []
                    _marker: str | None = None
                    while True:
                        _kw: dict = {"RoleName": role}
                        if _marker:
                            _kw["Marker"] = _marker
                        _resp = iam_client.list_role_policies(**_kw)
                        inline_names.extend(_resp.get("PolicyNames", []))
                        if not _resp.get("IsTruncated"):
                            break
                        _marker = _resp.get("Marker")

                    managed_policies: list[dict] = []
                    _marker = None
                    while True:
                        _kw = {"RoleName": role}
                        if _marker:
                            _kw["Marker"] = _marker
                        _resp = iam_client.list_attached_role_policies(**_kw)
                        managed_policies.extend(_resp.get("AttachedPolicies", []))
                        if not _resp.get("IsTruncated"):
                            break
                        _marker = _resp.get("Marker")

                    if policy in inline_names:
                        resources.append(CleanupResource(
                            type="iam_policy",
                            identifier=f"iam_policy:{role}/{policy}",
                            action="delete",
                            reason=f"inline policy {policy!r} on role {role!r}",
                            meta={"role_name": role, "policy_name": policy},
                        ))

                    other_inline = [p for p in inline_names if p != policy]
                    if other_inline or managed_policies:
                        extra = []
                        if other_inline:
                            extra.append(
                                f"{len(other_inline)} other inline "
                                + ("policy" if len(other_inline) == 1 else "policies")
                            )
                        if managed_policies:
                            extra.append(
                                f"{len(managed_policies)} attached managed "
                                + ("policy" if len(managed_policies) == 1 else "policies")
                            )
                        resources.append(CleanupResource(
                            type="iam_role",
                            identifier=f"iam_role:{role}",
                            action="keep",
                            reason=(
                                f"role has {' and '.join(extra)} — "
                                "will not delete a role this deployment did not create alone"
                            ),
                        ))
                    else:
                        resources.append(CleanupResource(
                            type="iam_role",
                            identifier=f"iam_role:{role}",
                            action="delete",
                            reason=f"IAM execution role {role!r} created by this deployment",
                            meta={"role_name": role},
                        ))
        except iam_client.exceptions.NoSuchEntityException:
            log.debug("IAM role %r not found — nothing to do", role)

    # ── local state files ─────────────────────────────────────────────────────
    data_root = cfg.data_root
    runtimes_file = data_root / "agentcore-runtimes.json"
    if runtimes_file.exists():
        resources.append(CleanupResource(
            type="local_file",
            identifier=f"local:{runtimes_file}",
            action="delete",
            reason="agentcore-runtimes.json local runtime cache",
            meta={"path": str(runtimes_file)},
        ))

    lock_file = data_root / "harness-versions.lock.json"
    if lock_file.exists():
        resources.append(CleanupResource(
            type="local_file",
            identifier=f"local:{lock_file}",
            action="delete",
            reason="harness-versions.lock.json version lock",
            meta={"path": str(lock_file)},
        ))

    # Session files for runtimes that are being deleted.
    deleted_runtime_arns: set[str] = {
        r.meta["runtime_arn"]
        for r in resources
        if r.type == "runtime" and r.action == "delete" and r.meta.get("runtime_arn")
    }
    sessions_dir = data_root / "agentcore-sessions"
    if sessions_dir.is_dir():
        for session_file in sorted(sessions_dir.glob("*.json")):
            try:
                session_data = json.loads(session_file.read_text())
                session_arn = session_data.get("arn", "")
                if session_arn and session_arn in deleted_runtime_arns:
                    resources.append(CleanupResource(
                        type="local_file",
                        identifier=f"local:{session_file}",
                        action="delete",
                        reason="session record for a runtime being deleted",
                        meta={"path": str(session_file)},
                    ))
            except Exception as exc:
                log.debug("skipping session file %s: %s", session_file, exc)

    return resources


def _principal_allows_agentcore(stmt: dict) -> bool:
    """Return True if a trust-policy statement allows bedrock-agentcore.amazonaws.com."""
    principal = stmt.get("Principal", {})
    svc = principal.get("Service") if isinstance(principal, dict) else None
    if isinstance(svc, str):
        return svc == "bedrock-agentcore.amazonaws.com"
    if isinstance(svc, list):
        return "bedrock-agentcore.amazonaws.com" in svc
    return False


def _s3_prefix_has_objects(s3, bucket: str, prefix: str, account: str) -> bool:
    """Return True if the S3 prefix contains any object versions or delete markers.

    Uses a MaxKeys=1 probe to avoid listing the entire prefix.  On any API
    error (e.g. insufficient permissions on a freshly emptied bucket) the
    function returns True so that a subsequent deletion attempt surfaces the
    real error rather than silently skipping the prefix.
    """
    try:
        resp = s3.list_object_versions(
            Bucket=bucket, Prefix=prefix, MaxKeys=1, ExpectedBucketOwner=account,
        )
        return bool(resp.get("IsTruncated") or resp.get("Versions") or resp.get("DeleteMarkers"))
    except Exception:
        return True  # assume data exists on error; let deletion handle it


# ── deletion helpers ──────────────────────────────────────────────────────────

def _delete_s3_versioned(bucket: str, prefix: str, account: str, s3) -> list[str]:
    """Delete all object versions and delete markers under *prefix*.

    Returns a list of error strings for versions that could not be deleted.
    Batches up to 1000 objects per delete_objects call (API limit).
    """
    errors: list[str] = []
    key_marker = ""
    vid_marker = ""

    while True:
        kwargs: dict = {"Bucket": bucket, "Prefix": prefix, "ExpectedBucketOwner": account}
        if key_marker:
            kwargs["KeyMarker"] = key_marker
        if vid_marker:
            kwargs["VersionIdMarker"] = vid_marker

        resp = s3.list_object_versions(**kwargs)

        batch: list[dict] = []
        for v in resp.get("Versions", []):
            batch.append({"Key": v["Key"], "VersionId": v["VersionId"]})
        for dm in resp.get("DeleteMarkers", []):
            batch.append({"Key": dm["Key"], "VersionId": dm["VersionId"]})

        # Batch delete in groups of 1000.
        for i in range(0, len(batch), 1000):
            chunk = batch[i:i + 1000]
            if not chunk:
                continue
            del_resp = s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": chunk, "Quiet": True},
                ExpectedBucketOwner=account,
            )
            for err in del_resp.get("Errors", []):
                errors.append(
                    f"s3://{bucket}/{err['Key']}@{err.get('VersionId','?')}: "
                    f"{err['Code']}: {err['Message']}"
                )

        if not resp.get("IsTruncated"):
            break
        key_marker = resp.get("NextKeyMarker", "")
        vid_marker = resp.get("NextVersionIdMarker", "")

    return errors


def _runtime_status(control, runtime_id: str) -> str | None:
    """Return the runtime's status, or None when it no longer exists."""
    from botocore.exceptions import ClientError
    try:
        return control.get_agent_runtime(agentRuntimeId=runtime_id).get("status", "UNKNOWN")
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("ResourceNotFoundException", "404"):
            return None
        raise


def _wait_runtime_deleted(runtime_id: str, control, timeout: int = _DELETE_TIMEOUT) -> None:
    """Poll GetAgentRuntime until the runtime raises ResourceNotFoundException."""
    from botocore.exceptions import ClientError
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            info = control.get_agent_runtime(agentRuntimeId=runtime_id)
            status = info.get("status", "UNKNOWN")
            log.debug("runtime %s status: %s", runtime_id, status)
            # DELETE_FAILED not in botocore enum but handle defensively.
            if status not in ("DELETING", "READY", "CREATING", "UPDATING",
                              "CREATE_FAILED", "UPDATE_FAILED", "UNKNOWN"):
                log.warning("runtime %s: unexpected status %r during deletion", runtime_id, status)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("ResourceNotFoundException", "404"):
                return  # deleted
            raise
        time.sleep(_POLL_INTERVAL)
    raise TimeoutError(
        f"runtime {runtime_id} did not finish deleting within {timeout}s"
    )


# ── main cleanup ──────────────────────────────────────────────────────────────

def _plan_dict(
    resources: list[CleanupResource],
    token: str,
    account: str,
    region: str,
) -> dict:
    return {
        "account": account,
        "region": region,
        "token": token,
        "resources": [
            {
                "type": r.type,
                "identifier": r.identifier,
                "action": r.action,
                "reason": r.reason,
            }
            for r in resources
        ],
    }


def _print_plan_tty(
    resources: list[CleanupResource],
    token: str,
    account: str,
    region: str,
) -> None:
    """Print a human-readable cleanup plan to stderr."""
    deletes = [r for r in resources if r.action == "delete"]
    keeps = [r for r in resources if r.action == "keep"]

    print(f"\nCleanup plan  account={account}  region={region}", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    if deletes:
        print(f"\nWill DELETE ({len(deletes)} resources):", file=sys.stderr)
        for r in deletes:
            print(f"  [{r.type}] {r.identifier}", file=sys.stderr)
            print(f"           {r.reason}", file=sys.stderr)
    if keeps:
        print(f"\nWill KEEP ({len(keeps)} resources):", file=sys.stderr)
        for r in keeps:
            print(f"  [{r.type}] {r.identifier}", file=sys.stderr)
            print(f"           {r.reason}", file=sys.stderr)
    print(f"\nPlan token: {token}", file=sys.stderr)
    print("=" * 70, file=sys.stderr)


def run_cleanup(
    cfg: Any,          # harness_image.deploy.DeployConfig
    *,
    approve_token: str | None = None,
    include_artifacts: bool = False,
    artifact_s3_uri: str | None = None,
    keep_logs: bool = False,
) -> int:
    """Discover resources, get consent, and delete.

    Returns an integer exit code:
      0  Nothing to delete, or all deletions succeeded.
      1  User aborted (TTY) or one/more deletions failed.
      2  Usage error (empty S3 prefix).
      3  Approval required (non-TTY without --approve, or token mismatch).
    """
    import boto3
    from botocore.exceptions import ClientError

    # ── AWS session ───────────────────────────────────────────────────────────
    boto_session = boto3.Session(profile_name=cfg.profile, region_name=cfg.region)
    sts = boto_session.client("sts")
    identity = sts.get_caller_identity()
    account = identity["Account"]

    # ── discover ──────────────────────────────────────────────────────────────
    try:
        resources = discover(
            cfg, boto_session, account,
            include_artifacts=include_artifacts,
            artifact_s3_uri=artifact_s3_uri,
            keep_logs=keep_logs,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    to_delete = [r for r in resources if r.action == "delete"]
    if not to_delete:
        print(json.dumps({
            "account": account, "region": cfg.region,
            "deleted": 0, "kept": len(resources), "failed": 0,
            "message": "nothing to delete — deployment is already clean",
        }))
        return 0

    token = cleanup_token(account, cfg.region, resources)

    # ── consent ───────────────────────────────────────────────────────────────
    if approve_token is None:
        # No --approve given.
        is_tty = sys.stdin.isatty() and sys.stdout.isatty()
        if not is_tty:
            print(json.dumps(_plan_dict(resources, token, account, cfg.region),
                             indent=2))
            return 3

        # Interactive TTY: show plan, prompt for account ID.
        _print_plan_tty(resources, token, account, cfg.region)
        print(
            f"\nTo confirm, type the AWS account ID ({account}) and press Enter.\n"
            "Anything else aborts: ",
            file=sys.stderr, end="",
        )
        try:
            answer = input().strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 1
        if answer != account:
            print(f"Aborted: {answer!r} does not match account {account!r}.", file=sys.stderr)
            return 1
    else:
        # --approve given: re-discover and verify token.
        try:
            resources = discover(
                cfg, boto_session, account,
                include_artifacts=include_artifacts,
                artifact_s3_uri=artifact_s3_uri,
                keep_logs=keep_logs,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        to_delete = [r for r in resources if r.action == "delete"]
        if not to_delete:
            print(json.dumps({
                "account": account, "region": cfg.region,
                "deleted": 0, "kept": len(resources), "failed": 0,
                "message": "nothing to delete — deployment is already clean",
            }))
            return 0

        new_token = cleanup_token(account, cfg.region, resources)
        if new_token != approve_token:
            print(json.dumps({
                "error": "approval token mismatch",
                "next": f"Re-run without --approve to get a fresh token.",
                **_plan_dict(resources, new_token, account, cfg.region),
            }, indent=2))
            return 3

    # ── delete ────────────────────────────────────────────────────────────────
    control = boto_session.client("bedrock-agentcore-control", region_name=cfg.region)
    s3 = boto_session.client("s3")
    iam_client = boto_session.client("iam")
    logs_client = boto_session.client("logs")

    deleted: list[str] = []
    kept: list[str] = []
    failed: list[str] = []

    def _ok(identifier: str) -> None:
        deleted.append(identifier)
        log.info("deleted: %s", identifier)

    def _fail(identifier: str, exc: Exception) -> None:
        msg = f"{identifier}: {exc}"
        failed.append(msg)
        log.error("failed: %s", msg)

    def _keep(identifier: str, reason: str) -> None:
        kept.append(identifier)
        log.debug("kept: %s (%s)", identifier, reason)

    for r in resources:
        if r.action == "keep":
            _keep(r.identifier, r.reason)

    # Deletion order: endpoints → runtimes (wait) → workload identities →
    # log groups → S3 → IAM inline policy → IAM role → local files.

    # 1. Endpoints
    for r in [r for r in resources if r.type == "endpoint" and r.action == "delete"]:
        try:
            control.delete_agent_runtime_endpoint(
                agentRuntimeId=r.meta["runtime_id"],
                endpointName=r.meta["endpoint_name"],
            )
            _ok(r.identifier)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("ResourceNotFoundException", "404"):
                _ok(r.identifier)  # already gone
            elif code == "ConflictException":
                # Endpoint deletion already in progress (e.g. from a previous timed-out
                # run); treat as successfully initiated — the runtime deletion will
                # finish cleaning it up.
                _ok(r.identifier)
                log.debug("endpoint %s already being deleted (ConflictException)", r.identifier)
            else:
                _fail(r.identifier, exc)
        except Exception as exc:
            _fail(r.identifier, exc)

    # 2. Runtimes (initiate delete, then wait for completion)
    runtime_wait_ids: list[str] = []
    for r in [r for r in resources if r.type == "runtime" and r.action == "delete"]:
        try:
            control.delete_agent_runtime(agentRuntimeId=r.meta["runtime_id"])
            runtime_wait_ids.append(r.meta["runtime_id"])
            log.info("delete_agent_runtime(%s) initiated", r.meta["runtime_id"])
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("ResourceNotFoundException", "404"):
                _ok(r.identifier)  # already gone; no wait needed
            elif code == "ConflictException":
                # Either an earlier cleanup already started the deletion (DELETING:
                # wait for it) or the runtime is busy creating/updating (fail now
                # rather than wait for a deletion that was never started).
                status = _runtime_status(control, r.meta["runtime_id"])
                if status is None:
                    _ok(r.identifier)
                elif status == "DELETING":
                    runtime_wait_ids.append(r.meta["runtime_id"])
                    log.debug("runtime %s already deleting; waiting...", r.meta["runtime_id"])
                else:
                    _fail(r.identifier, RuntimeError(
                        f"runtime is {status}; wait until it is READY, then re-run cleanup"))
            else:
                _fail(r.identifier, exc)
        except Exception as exc:
            _fail(r.identifier, exc)

    for r in [r for r in resources if r.type == "runtime" and r.action == "delete"]:
        rid = r.meta["runtime_id"]
        if rid not in runtime_wait_ids:
            continue
        try:
            _wait_runtime_deleted(rid, control)
            _ok(r.identifier)
        except Exception as exc:
            _fail(r.identifier, exc)

    # 3. Workload identities
    for r in [r for r in resources if r.type == "workload_identity" and r.action == "delete"]:
        try:
            control.delete_workload_identity(name=r.meta["workload_name"])
            _ok(r.identifier)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("ResourceNotFoundException", "404"):
                _ok(r.identifier)
            else:
                _fail(r.identifier, exc)
        except Exception as exc:
            _fail(r.identifier, exc)

    # 4. Log groups
    for r in [r for r in resources if r.type == "log_group" and r.action == "delete"]:
        try:
            logs_client.delete_log_group(logGroupName=r.meta["log_group_name"])
            _ok(r.identifier)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("ResourceNotFoundException", "404"):
                _ok(r.identifier)
            else:
                _fail(r.identifier, exc)
        except Exception as exc:
            _fail(r.identifier, exc)

    # 5. S3 versioned objects
    for r in [r for r in resources if r.type == "s3_objects" and r.action == "delete"]:
        try:
            errs = _delete_s3_versioned(
                r.meta["bucket"], r.meta["prefix"], r.meta["account"], s3
            )
            if errs:
                for e in errs:
                    failed.append(e)
                log.error("%d S3 version delete error(s) for %s", len(errs), r.identifier)
            else:
                _ok(r.identifier)
        except Exception as exc:
            _fail(r.identifier, exc)

    # 6. IAM inline policy
    for r in [r for r in resources if r.type == "iam_policy" and r.action == "delete"]:
        try:
            iam_client.delete_role_policy(
                RoleName=r.meta["role_name"],
                PolicyName=r.meta["policy_name"],
            )
            _ok(r.identifier)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("NoSuchEntity", "NoSuchEntityException", "404"):
                _ok(r.identifier)
            else:
                _fail(r.identifier, exc)
        except Exception as exc:
            _fail(r.identifier, exc)

    # 7. IAM role
    for r in [r for r in resources if r.type == "iam_role" and r.action == "delete"]:
        try:
            iam_client.delete_role(RoleName=r.meta["role_name"])
            _ok(r.identifier)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("NoSuchEntity", "NoSuchEntityException", "404"):
                _ok(r.identifier)
            else:
                _fail(r.identifier, exc)
        except Exception as exc:
            _fail(r.identifier, exc)

    # 8. Local files
    for r in [r for r in resources if r.type == "local_file" and r.action == "delete"]:
        try:
            p = Path(r.meta["path"])
            p.unlink(missing_ok=True)
            _ok(r.identifier)
        except Exception as exc:
            _fail(r.identifier, exc)

    # ── summary ───────────────────────────────────────────────────────────────
    summary = {
        "account": account,
        "region": cfg.region,
        "deleted": len(deleted),
        "kept": len(kept),
        "failed": len(failed),
    }
    if failed:
        summary["errors"] = failed
    print(json.dumps(summary, indent=2))
    return 1 if failed else 0
