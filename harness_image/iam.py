# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""IAM policy document builders for AgentCore harness deployments.

Produces three policy kinds:
  execution   Runtime execution role (minimal: logs, xray, bedrock, S3 prefix).
  deployer    Workstation role that runs agentcore-deploy (iam:*, bedrock-agentcore:*).
  workstation Workstation role for the multi-coding-harness-compare API/worker (S3 artifacts).

Per-user isolation (AGENTCORE_ISOLATION=per-user): role/policy names and S3 scopes
include USER_NAMESPACE so multiple users can share one account without collisions.

KMS: when AGENTCORE_TRANSFER_KMS_KEY_ARN is set, the execution role gains
kms:Decrypt and kms:GenerateDataKey on that key (needed for SSE-KMS objects).

Secrets sub-prefix: s3:DeleteObjectVersion is granted on <prefix>/secrets/* (and
the per-user variant) so the in-VM supervisor can rotate the one-time handshake
objects from the secrets handshake channel.
"""
from __future__ import annotations

import json
import re


# ── execution role ────────────────────────────────────────────────────────────

def trust_policy(account: str, region: str) -> dict:
    """Trust policy allowing bedrock-agentcore to assume the execution role."""
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account},
                # The runtime resource type is "runtime/", not "agent-runtime/"; a
                # narrower pattern never matches and CreateAgentRuntime rejects the role.
                "ArnLike": {
                    "aws:SourceArn":
                        f"arn:aws:bedrock-agentcore:{region}:{account}:*"
                },
            },
        }],
    }


def execution_policy(
    account: str,
    region: str,
    bucket: str,
    prefix: str,
    *,
    kms_key_arn: str | None = None,
    user_namespace: str | None = None,
) -> dict:
    """Inline policy for the execution role.

    When user_namespace is given the S3 scope narrows to <prefix>/<namespace>/*.
    When kms_key_arn is given a KMS decrypt+generate grant is added.
    s3:DeleteObjectVersion / s3:GetObjectVersion / s3:ListBucketVersions are
    granted so the in-VM supervisor can list, read and purge versioned S3 objects
    (one-time handshake channel).

    Bedrock invoke is scoped to foundation-model and inference-profile ARNs
    (covers cross-region geo profiles and global profiles).
    GetWorkloadAccessToken* is scoped to the workload-identity-directory and
    all workload identities within it.
    """
    if user_namespace:
        s3_scope = f"{prefix}/{user_namespace}"
    else:
        s3_scope = prefix

    s3_prefix_arn = f"arn:aws:s3:::{bucket}/{s3_scope}/*"
    bucket_arn = f"arn:aws:s3:::{bucket}"
    secrets_prefix_arn = f"arn:aws:s3:::{bucket}/{s3_scope}/secrets/*"

    statements = [
        {
            "Sid": "CloudWatchLogs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup", "logs:CreateLogDelivery",
                "logs:CreateLogStream", "logs:PutLogEvents",
                "logs:DescribeLogGroups", "logs:DescribeLogStreams",
                "logs:GetLogDelivery", "logs:ListLogDeliveries",
                "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
                "logs:PutResourcePolicy", "logs:DescribeResourcePolicies",
            ],
            "Resource": f"arn:aws:logs:{region}:{account}:*",
        },
        {
            "Sid": "XRayTelemetry",
            "Effect": "Allow",
            "Action": [
                "xray:PutTraceSegments",
                "xray:PutTelemetryRecords",
                "xray:GetSamplingRules",
                "xray:GetSamplingTargets",
            ],
            "Resource": "*",
        },
        {
            "Sid": "CloudWatchMetrics",
            "Effect": "Allow",
            "Action": "cloudwatch:PutMetricData",
            "Resource": "*",
            "Condition": {
                "StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}
            },
        },
        {
            # Scoped to the workload-identity-directory and all workload identities
            # within it.  Both ARNs are required: the directory resource and the
            # identity resource (per BedrockAgentCoreRuntimeIdentityServiceRolePolicy
            # managed policy pattern).
            "Sid": "AgentCoreWorkloadToken",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:GetWorkloadAccessToken",
                "bedrock-agentcore:GetWorkloadAccessTokenForAgentRuntime",
            ],
            "Resource": [
                f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*",
            ],
        },
        {
            # Scoped to foundation-model ARNs in any Region, the Region-less
            # foundation-model ARN that global cross-Region inference profiles
            # route through, and account-owned inference-profile ARNs (geo and
            # global cross-Region inference profiles).
            # Codex calls the OpenAI-compatible Responses API on bedrock-runtime, which
            # IAM authorizes as bedrock:InvokeModel on the account's Bedrock project;
            # requests that name no project (this harness never names one) use
            # project/default.
            "Sid": "BedrockInvoke",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            "Resource": [
                "arn:aws:bedrock:*::foundation-model/*",
                "arn:aws:bedrock:::foundation-model/*",
                f"arn:aws:bedrock:*:{account}:inference-profile/*",
                f"arn:aws:bedrock:*:{account}:project/default",
            ],
        },
        {
            "Sid": "S3Objects",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                       "s3:GetObjectVersion"],
            "Resource": s3_prefix_arn,
        },
        {
            "Sid": "S3List",
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
            "Resource": bucket_arn,
            "Condition": {
                "StringLike": {"s3:prefix": [f"{s3_scope}/*"]}
            },
        },
        # Secrets handshake channel: the in-VM supervisor lists, reads and purges
        # versioned handshake objects; all three version operations are needed.
        {
            "Sid": "S3Secrets",
            "Effect": "Allow",
            "Action": ["s3:GetObjectVersion", "s3:DeleteObjectVersion"],
            "Resource": secrets_prefix_arn,
        },
    ]

    if kms_key_arn:
        statements.append({
            "Sid": "KMSDecrypt",
            "Effect": "Allow",
            "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
            "Resource": kms_key_arn,
        })

    return {"Version": "2012-10-17", "Statement": statements}


# ── deployer policy ──────────────────────────────────────────────────────────

def deployer_policy(
    account: str,
    region: str,
    bucket: str,
    prefix: str,
    *,
    role_name: str | None = None,
    user_namespace: str | None = None,
    agentcore_prefix: str,
) -> dict:
    """Policy for the workstation principal running agentcore-deploy and agentcore-cleanup.

    Grants the minimum actions needed to create/update/delete the execution role
    and the AgentCore runtimes.  iam:PassRole is scoped to the execution role ARN
    and conditioned on iam:PassedToService bedrock-agentcore.amazonaws.com.
    The bedrock-agentcore: IAM prefix covers both the control plane
    (CreateAgentRuntime/Update/Get/List/Delete) and is the correct service prefix.

    AgentCore actions are split into statements by resource-scoping capability:
    - CreateAgentRuntime: Resource "*" (runtime ARN doesn't exist at creation time)
    - ListAgentRuntimes: Resource "*" (list operation; Sid "ListOnly")
    - Update/Get/Delete/ListEndpoints/DeleteEndpoint: scoped to
      arn:aws:bedrock-agentcore:<region>:<account>:runtime/<agentcore_prefix>*
    - DeleteWorkloadIdentity: scoped to the
      workload-identity/<agentcore_prefix>* resource (cleanup removes these).
      ListWorkloadIdentities and GetWorkloadIdentity are intentionally absent —
      cleanup discovers the workload identity ARN from
      GetAgentRuntime.workloadIdentityDetails.workloadIdentityArn and needs only
      DeleteWorkloadIdentity to remove it.
    - DescribeLogGroups/DeleteLogGroup: scoped to the AgentCore runtime log group
      prefix (/aws/bedrock-agentcore/runtimes/<agentcore_prefix>*)
    """
    s3_scope = f"{prefix}/{user_namespace}" if user_namespace else prefix
    role_resource = (
        f"arn:aws:iam::{account}:role/{role_name}" if role_name
        else f"arn:aws:iam::{account}:role/*"
    )
    runtime_arn = (
        f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{agentcore_prefix}*"
    )
    wi_arn = (
        f"arn:aws:bedrock-agentcore:{region}:{account}:"
        f"workload-identity-directory/default/workload-identity/{agentcore_prefix}*"
    )
    log_group_arn = (
        f"arn:aws:logs:{region}:{account}:"
        f"log-group:/aws/bedrock-agentcore/runtimes/{agentcore_prefix}*"
    )
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                # Deploy needs CreateRole, GetRole, UpdateAssumeRolePolicy,
                # and PutRolePolicy to create or refresh the execution role.
                # Cleanup needs GetRole (check Application tag + trust policy
                # to verify ownership), ListRolePolicies/ListAttachedRolePolicies
                # (confirm no foreign policies remain), DeleteRolePolicy, and
                # DeleteRole.  No AttachRolePolicy, DetachRolePolicy,
                # GetRolePolicy, or TagRole — these are not called by either
                # deploy or cleanup; tags are set via CreateRole Tags param.
                "Sid": "IAMRoleManage",
                "Effect": "Allow",
                "Action": [
                    "iam:CreateRole",
                    "iam:GetRole",
                    "iam:UpdateAssumeRolePolicy",
                    "iam:PutRolePolicy",
                    "iam:ListRolePolicies",
                    "iam:ListAttachedRolePolicies",
                    "iam:DeleteRolePolicy",
                    "iam:DeleteRole",
                ],
                "Resource": role_resource,
            },
            {
                # PassRole lets agentcore-deploy hand the execution role to the
                # runtime.  Scoped to the execution role ARN and conditioned on
                # the service that will receive it.
                "Sid": "IAMPassRole",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": role_resource,
                "Condition": {
                    "StringEquals": {
                        "iam:PassedToService": "bedrock-agentcore.amazonaws.com"
                    }
                },
            },
            {
                # CreateAgentRuntime: the runtime ARN is service-generated at
                # creation time (NAME-RANDOM10CHARS suffix), so it cannot be
                # pre-computed; Resource "*" is required for this action.
                "Sid": "NoResourceScopingSupported",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:CreateAgentRuntime",
                ],
                "Resource": "*",
            },
            {
                # ListAgentRuntimes is a list operation with no resource type;
                # Resource "*" is required.
                "Sid": "ListOnly",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:ListAgentRuntimes",
                "Resource": "*",
            },
            {
                # Update/Get/Delete and endpoint actions are scoped to the runtime
                # name prefix.  IAM '*' spans '/' so this single ARN covers both the
                # runtime resource type and the nested runtime-endpoint resource type
                # required by ListAgentRuntimeEndpoints and DeleteAgentRuntimeEndpoint.
                "Sid": "AgentCoreManage",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:UpdateAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:DeleteAgentRuntime",
                    "bedrock-agentcore:ListAgentRuntimeEndpoints",
                    "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
                ],
                "Resource": runtime_arn,
            },
            {
                # Cleanup removes workload identities auto-created by
                # CreateAgentRuntime; names are extracted from the ARN returned by
                # GetAgentRuntime.workloadIdentityDetails.workloadIdentityArn.
                # ListWorkloadIdentities and GetWorkloadIdentity are intentionally
                # absent: no code path calls them, and including them would violate
                # the least-privilege requirement.
                "Sid": "WorkloadIdentityManage",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:DeleteWorkloadIdentity",
                "Resource": wi_arn,
            },
            {
                # s3:PutObject and s3:GetObject for bundle upload; s3:DeleteObject for
                # cleanup of ordinary objects; s3:DeleteObjectVersion for cleanup of
                # versioned objects (all versions and delete markers must be removed
                # from a versioned bucket before a prefix appears empty).
                "Sid": "S3Objects",
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject", "s3:GetObject",
                    "s3:DeleteObject", "s3:DeleteObjectVersion",
                ],
                "Resource": f"arn:aws:s3:::{bucket}/{s3_scope}/*",
            },
            {
                # s3:ListBucket for deploy; s3:ListBucketVersions for cleanup
                # (must enumerate all versions and delete markers under the prefix).
                "Sid": "S3List",
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {
                    "StringLike": {"s3:prefix": [f"{s3_scope}/*"]}
                },
            },
            {
                # Cleanup lists the log groups AgentCore created for this prefix's
                # runtimes.  DescribeLogGroups is a read-only list call that IAM may
                # evaluate against the account-level log-group ARN, so it gets its own
                # statement; deletion stays scoped to the runtime log groups below.
                "Sid": "LogGroupsList",
                "Effect": "Allow",
                "Action": "logs:DescribeLogGroups",
                "Resource": f"arn:aws:logs:{region}:{account}:log-group:*",
            },
            {
                # The shared aws/spans group is never matched by this ARN pattern.
                "Sid": "LogGroupsCleanup",
                "Effect": "Allow",
                "Action": "logs:DeleteLogGroup",
                "Resource": log_group_arn,
            },
        ],
    }


# ── workstation policy ────────────────────────────────────────────────────────

def workstation_policy(
    account: str,
    bucket: str,
    artifact_prefix: str,
    *,
    user_namespace: str | None = None,
    region: str,
    agentcore_prefix: str,
) -> dict:
    """Policy for the workstation running the multi-coding-harness-compare API/worker.

    Grants artifact S3 access, Bedrock invoke/list for the judge and model
    catalogue, STS identity (preflight aws-identity check), and AgentCore
    data-plane calls for probe/doctor (agentcore runtime).
    s3:GetObjectVersion and s3:AbortMultipartUpload are required by the README
    "S3 storage and request charges" section and the doctor check.

    Bedrock invoke is scoped to foundation-model and inference-profile ARNs.
    Bedrock model-catalogue read actions (ListInferenceProfiles, GetInferenceProfile,
    ListFoundationModels) remain on Resource "*" as they have no resource-level
    scoping support.

    AgentCore actions are split into three statements:
    - GetAgentRuntime: scoped to runtime/<agentcore_prefix>*
    - ListAgentRuntimes: Resource "*" (list operation; Sid "ListOnly")
    - InvokeAgentRuntime and session actions: scoped to runtime/<agentcore_prefix>*
      (IAM '*' spans '/' so this single ARN covers both runtime and runtime-endpoint
      resource types required by InvokeAgentRuntime)
    """
    s3_scope = (
        f"{artifact_prefix}/{user_namespace}"
        if user_namespace else artifact_prefix
    )
    runtime_arn = (
        f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{agentcore_prefix}*"
    )
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "S3Objects",
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                    "s3:GetObjectVersion", "s3:AbortMultipartUpload",
                ],
                "Resource": f"arn:aws:s3:::{bucket}/{s3_scope}/*",
            },
            {
                "Sid": "S3List",
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {
                    "StringLike": {"s3:prefix": [f"{s3_scope}/*"]}
                },
            },
            {
                # Bedrock invoke: scoped to foundation-model ARNs in any Region, the
                # Region-less foundation-model ARN used by global cross-Region
                # inference profiles, account-owned inference-profile ARNs, and the
                # account's default Bedrock project, which authorizes native Codex's
                # Responses API calls on bedrock-runtime.
                "Sid": "BedrockInvoke",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    "arn:aws:bedrock:::foundation-model/*",
                    f"arn:aws:bedrock:*:{account}:inference-profile/*",
                    f"arn:aws:bedrock:*:{account}:project/default",
                ],
            },
            {
                # Model-catalogue read actions have no resource-level scoping support;
                # Resource "*" is required.
                "Sid": "BedrockCatalogue",
                "Effect": "Allow",
                "Action": [
                    "bedrock:ListInferenceProfiles",
                    "bedrock:GetInferenceProfile",
                    "bedrock:ListFoundationModels",
                ],
                "Resource": "*",
            },
            {
                # STS: preflight aws-identity check and drift detection.
                "Sid": "STSCallerIdentity",
                "Effect": "Allow",
                "Action": "sts:GetCallerIdentity",
                "Resource": "*",
            },
            {
                # GetAgentRuntime scoped to this deployment's runtime name prefix.
                "Sid": "AgentCoreGet",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:GetAgentRuntime",
                "Resource": runtime_arn,
            },
            {
                # ListAgentRuntimes is a list operation with no resource type;
                # Resource "*" is required.
                "Sid": "ListOnly",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:ListAgentRuntimes",
                "Resource": "*",
            },
            {
                # AgentCore data plane: invoke and session actions.  A single ARN
                # with the prefix wildcard covers both the runtime resource type and
                # the nested runtime-endpoint resource type (required by
                # InvokeAgentRuntime) because IAM '*' spans '/'.
                "Sid": "AgentCoreInvoke",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:CreateAgentRuntimeSession",
                    "bedrock-agentcore:GetAgentRuntimeSession",
                    "bedrock-agentcore:InvokeAgentRuntimeCommand",
                ],
                "Resource": runtime_arn,
            },
        ],
    }


# ── runtime name helper ───────────────────────────────────────────────────────

_AGENTCORE_RUNTIME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{0,47}$')
_AGENTCORE_MAX_LEN = 48
_AGENTCORE_PREFIX_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')

# IAM name character rules (AWS docs: https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html)
_IAM_ROLE_NAME_RE = re.compile(r'^[\w+=,.@-]{1,64}$')
_IAM_POLICY_NAME_RE = re.compile(r'^[\w+=,.@-]{1,128}$')


def validate_runtime_name_prefix(prefix: str) -> None:
    """Validate that a runtime name prefix is safe to use in AgentCore runtime names.

    The prefix must:
    - Start with a letter ([A-Za-z])
    - Contain only letters, digits, and underscores ([A-Za-z0-9_]) — no hyphens
    - Be short enough that <prefix>_claude fits within 48 characters

    Raises ValueError with a corrective suggestion on failure so the caller can
    surface a clear message before writing the value or making any AWS API call.
    """
    if not prefix:
        raise ValueError("AGENTCORE_RUNTIME_NAME_PREFIX must not be empty")
    if not _AGENTCORE_PREFIX_RE.match(prefix):
        suggestion = re.sub(r'[^A-Za-z0-9_]', '_', prefix)
        # Ensure suggestion starts with a letter
        if suggestion and not suggestion[0].isalpha():
            suggestion = "a_" + suggestion.lstrip("_")
        raise ValueError(
            f"AGENTCORE_RUNTIME_NAME_PREFIX {prefix!r} contains invalid characters; "
            f"must match [A-Za-z][A-Za-z0-9_]* (letters, digits, underscores only — no hyphens). "
            f"Suggestion: use underscores instead of hyphens, e.g. {suggestion!r}"
        )
    # Check that the longest derived name (prefix + "_claude") fits within 48 characters
    longest = f"{prefix}_claude"
    if len(longest) > _AGENTCORE_MAX_LEN:
        raise ValueError(
            f"AGENTCORE_RUNTIME_NAME_PREFIX {prefix!r} is too long: "
            f"{prefix!r}_claude would be {len(longest)} characters "
            f"(AgentCore limit is {_AGENTCORE_MAX_LEN}); shorten the prefix"
        )


def validate_iam_role_name(name: str) -> None:
    """Validate an IAM role name against AWS IAM character and length rules.

    IAM role names allow [\\w+=,.@-] (alphanumerics plus +=,.@-) and must be 1-64
    characters.  The per-user isolation suffix (-<namespace>) is appended at deploy
    time; this validates the base name only.

    Raises ValueError with a descriptive message on failure.
    """
    if not name:
        raise ValueError("AGENTCORE_EXECUTION_ROLE_NAME must not be empty")
    if not _IAM_ROLE_NAME_RE.match(name):
        raise ValueError(
            f"AGENTCORE_EXECUTION_ROLE_NAME {name!r} is invalid; "
            "IAM role names must use only [\\w+=,.@-] characters and be 1-64 characters long"
        )


def validate_iam_policy_name(name: str) -> None:
    """Validate an IAM inline policy name against AWS IAM character and length rules.

    IAM policy names allow [\\w+=,.@-] and must be 1-128 characters.

    Raises ValueError with a descriptive message on failure.
    """
    if not name:
        raise ValueError("AGENTCORE_INLINE_POLICY_NAME must not be empty")
    if not _IAM_POLICY_NAME_RE.match(name):
        raise ValueError(
            f"AGENTCORE_INLINE_POLICY_NAME {name!r} is invalid; "
            "IAM policy names must use only [\\w+=,.@-] characters and be 1-128 characters long"
        )


def runtime_name(prefix: str, harness: str, namespace: str | None) -> str:
    """Return the AgentCore runtime name for (prefix, harness, namespace).

    Shared mode (namespace is None):  {prefix}_{harness}
    Per-user mode (namespace given):  {prefix}_{harness}_{namespace}

    Hyphens in namespace are mapped to underscores because AgentCore names only
    allow [a-zA-Z0-9_].  The result must still start with a letter and fit within
    48 characters; ValueError is raised when either constraint is violated so the
    caller can surface a clear error before attempting any AWS API call.
    """
    if namespace:
        clean_ns = namespace.replace("-", "_")
        name = f"{prefix}_{harness}_{clean_ns}"
    else:
        name = f"{prefix}_{harness}"

    if len(name) > _AGENTCORE_MAX_LEN:
        raise ValueError(
            f"Runtime name {name!r} is {len(name)} characters, exceeding the "
            f"AgentCore limit of {_AGENTCORE_MAX_LEN}; shorten "
            "AGENTCORE_RUNTIME_NAME_PREFIX or USER_NAMESPACE"
        )
    if not _AGENTCORE_RUNTIME_RE.match(name):
        raise ValueError(
            f"Runtime name {name!r} contains invalid characters; "
            "must match [a-zA-Z][a-zA-Z0-9_]{0,47}"
        )
    return name


# ── role name helpers ─────────────────────────────────────────────────────────

def role_name(base: str, user_namespace: str | None) -> str:
    """Return the IAM role name, adding the namespace suffix for per-user isolation."""
    if user_namespace:
        return f"{base}-{user_namespace}"
    return base


def policy_name(base: str, user_namespace: str | None) -> str:
    """Return the inline policy name, adding the namespace suffix for per-user isolation."""
    if user_namespace:
        return f"{base}-{user_namespace}"
    return base


# ── CLI entry: print policy documents ────────────────────────────────────────

def print_policy(kind: str) -> None:
    """Print a placeholder policy document for documentation.

    Real usage: python -m harness_image policy --kind execution|deployer|workstation
    Placeholder values are used so the document is valid JSON without real AWS IDs.
    """
    account = "123456789012"
    region = "us-east-1"
    bucket = "my-bucket"
    prefix = "code/ai-coding-assistant"
    agentcore_prefix = "codecouncil"
    if kind == "execution":
        doc = execution_policy(account, region, bucket, prefix)
    elif kind == "deployer":
        # Pass the conventional default role name so the generated document shows
        # a specific ARN rather than the wildcard role/* fallback.
        doc = deployer_policy(account, region, bucket, prefix,
                              role_name="CodeCouncilBedrockAgentCoreRuntimeRole",
                              agentcore_prefix=agentcore_prefix)
    elif kind == "workstation":
        doc = workstation_policy(account, bucket, prefix,
                                 region=region,
                                 agentcore_prefix=agentcore_prefix)
    else:
        raise ValueError(f"Unknown policy kind: {kind!r}; use execution|deployer|workstation")
    print(json.dumps(doc, indent=2))
