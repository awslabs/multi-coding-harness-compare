# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""IAM policy builder tests for harness_image.iam.

Covers:
  - trust_policy: principal, condition
  - execution_policy: basic statements, KMS grant, per-user S3 scope,
    s3:DeleteObjectVersion on secrets prefix
  - deployer_policy: basic structure
  - workstation_policy: basic structure
  - role_name / policy_name helpers
  - print_policy: smoke test for each kind
"""
import json
import unittest

from harness_image.iam import (
    deployer_policy,
    execution_policy,
    policy_name,
    role_name,
    trust_policy,
    workstation_policy,
    print_policy,
)


ACCOUNT = "123456789012"
REGION = "us-east-1"
BUCKET = "my-bucket"
PREFIX = "code/runtime"


class TrustPolicyTests(unittest.TestCase):
    def _policy(self, **kw):
        return trust_policy(ACCOUNT, REGION, **kw)

    def test_version_is_2012(self):
        self.assertEqual(self._policy()["Version"], "2012-10-17")

    def test_principal_is_bedrock_agentcore(self):
        stmt = self._policy()["Statement"][0]
        self.assertEqual(
            stmt["Principal"],
            {"Service": "bedrock-agentcore.amazonaws.com"},
        )

    def test_condition_includes_source_account(self):
        stmt = self._policy()["Statement"][0]
        self.assertEqual(
            stmt["Condition"]["StringEquals"]["aws:SourceAccount"],
            ACCOUNT,
        )

    def test_condition_arn_like_covers_all_resources(self):
        stmt = self._policy()["Statement"][0]
        arn_like = stmt["Condition"]["ArnLike"]["aws:SourceArn"]
        self.assertIn(ACCOUNT, arn_like)
        self.assertIn(REGION, arn_like)
        self.assertIn(":*", arn_like)  # not overly narrow


class ExecutionPolicyTests(unittest.TestCase):
    def _policy(self, **kw):
        return execution_policy(ACCOUNT, REGION, BUCKET, PREFIX, **kw)

    def _actions(self, policy):
        """Flatten all actions across all statements."""
        actions = []
        for stmt in policy["Statement"]:
            a = stmt["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        return actions

    def test_has_logs_actions(self):
        p = self._policy()
        actions = self._actions(p)
        self.assertIn("logs:PutLogEvents", actions)

    def test_has_bedrock_invoke(self):
        p = self._policy()
        actions = self._actions(p)
        self.assertIn("bedrock:InvokeModel", actions)
        self.assertIn("bedrock:InvokeModelWithResponseStream", actions)

    def test_has_s3_actions_on_prefix_arn(self):
        p = self._policy()
        s3_stmts = [s for s in p["Statement"]
                    if isinstance(s.get("Action"), list)
                    and "s3:GetObject" in s["Action"]]
        self.assertTrue(s3_stmts, "Expected an S3 GetObject statement")
        resource = s3_stmts[0]["Resource"]
        self.assertIn(BUCKET, resource)
        self.assertIn(PREFIX, resource)

    def test_has_delete_object_version_on_secrets_prefix(self):
        # s3:DeleteObjectVersion is now in a list together with s3:GetObjectVersion
        # on the secrets prefix (spec correction: list of actions for the secrets
        # sub-prefix statement).
        p = self._policy()
        stmts = [s for s in p["Statement"]
                 if isinstance(s.get("Action"), list)
                 and "s3:DeleteObjectVersion" in s["Action"]]
        self.assertTrue(stmts, "Expected s3:DeleteObjectVersion in a statement for the secrets prefix")
        resource = stmts[0]["Resource"]
        self.assertIn("secrets", resource)

    def test_kms_grant_added_when_key_arn_provided(self):
        key_arn = "arn:aws:kms:us-east-1:111:key/abc"
        p = self._policy(kms_key_arn=key_arn)
        kms_stmts = [s for s in p["Statement"]
                     if isinstance(s.get("Action"), list)
                     and "kms:Decrypt" in s["Action"]]
        self.assertTrue(kms_stmts, "Expected a KMS Decrypt statement")
        self.assertIn(key_arn, kms_stmts[0]["Resource"])
        self.assertEqual(kms_stmts[0].get("Sid"), "KMSDecrypt")

    def test_no_kms_grant_without_key(self):
        p = self._policy()
        kms_stmts = [s for s in p["Statement"]
                     if isinstance(s.get("Action"), list)
                     and "kms:Decrypt" in s["Action"]]
        self.assertFalse(kms_stmts, "Should not have KMS grant without key_arn")

    def test_per_user_s3_scope_narrows_to_namespace(self):
        p = self._policy(user_namespace="alice")
        s3_stmts = [s for s in p["Statement"]
                    if isinstance(s.get("Action"), list)
                    and "s3:GetObject" in s["Action"]]
        resource = s3_stmts[0]["Resource"]
        # Should include namespace in the path
        self.assertIn("alice", resource)
        self.assertIn(PREFIX, resource)

    def test_default_s3_scope_does_not_include_namespace(self):
        p = self._policy()
        s3_stmts = [s for s in p["Statement"]
                    if isinstance(s.get("Action"), list)
                    and "s3:GetObject" in s["Action"]]
        resource = s3_stmts[0]["Resource"]
        # No namespace in default (shared) scope
        self.assertNotIn("alice", resource)

    def test_agentcore_workload_token_actions(self):
        p = self._policy()
        actions = self._actions(p)
        self.assertIn("bedrock-agentcore:GetWorkloadAccessToken", actions)

    def test_is_valid_json(self):
        p = self._policy()
        # Serialize and deserialize — should not raise
        json.loads(json.dumps(p))


class DeployerPolicyTests(unittest.TestCase):
    def _policy(self, **kw):
        kw.setdefault("agentcore_prefix", "pfx")
        return deployer_policy(ACCOUNT, REGION, BUCKET, PREFIX, **kw)

    def test_has_iam_create_role(self):
        p = self._policy()
        actions = []
        for s in p["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        self.assertIn("iam:CreateRole", actions)

    def test_has_agentcore_control_actions(self):
        # Spec correction: the correct IAM service prefix is 'bedrock-agentcore:'
        # not 'bedrock-agentcore-control:' (which is not a valid IAM prefix).
        p = self._policy()
        actions = []
        for s in p["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        self.assertIn("bedrock-agentcore:CreateAgentRuntime", actions)
        self.assertIn("bedrock-agentcore:UpdateAgentRuntime", actions)
        # Verify the old wrong prefix is NOT present
        self.assertNotIn("bedrock-agentcore-control:CreateAgentRuntime", actions)

    def test_role_resource_scoped_when_name_given(self):
        p = self._policy(role_name="MyRole")
        iam_stmts = [s for s in p["Statement"]
                     if isinstance(s.get("Action"), list)
                     and "iam:CreateRole" in s["Action"]]
        resource = iam_stmts[0]["Resource"]
        self.assertIn("MyRole", resource)

    def test_has_iam_cleanup_actions(self):
        """deployer_policy must include IAM cleanup actions."""
        p = self._policy()
        actions = []
        for s in p["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        self.assertIn("iam:ListRolePolicies", actions)
        self.assertIn("iam:ListAttachedRolePolicies", actions)
        self.assertIn("iam:DeleteRolePolicy", actions)
        self.assertIn("iam:DeleteRole", actions)

    def test_has_iam_get_role_for_ownership_check(self):
        """deployer_policy must include iam:GetRole for cleanup ownership verification.

        Cleanup calls GetRole to read the role's Application tag and trust policy
        before deciding whether to delete it.  Without this permission, cleanup
        cannot confirm that the role belongs to this deployment.
        """
        p = self._policy()
        actions = []
        for s in p["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        self.assertIn("iam:GetRole", actions)

    def test_deployer_iam_role_manage_has_no_excess_actions(self):
        """deployer_policy IAMRoleManage must not contain unused IAM actions.

        GetRolePolicy, AttachRolePolicy, DetachRolePolicy, and TagRole are not
        called by either agentcore-deploy or agentcore-cleanup; they must be
        absent to satisfy the principle of least privilege.
        """
        p = self._policy()
        actions = []
        for s in p["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        for excess in ("iam:GetRolePolicy", "iam:AttachRolePolicy",
                       "iam:DetachRolePolicy", "iam:TagRole"):
            self.assertNotIn(excess, actions,
                             f"{excess!r} is not used by deploy or cleanup and "
                             "must not appear in the deployer policy")

    def test_has_agentcore_endpoint_actions(self):
        """deployer_policy must include endpoint list/delete for cleanup."""
        p = self._policy()
        actions = []
        for s in p["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        self.assertIn("bedrock-agentcore:ListAgentRuntimeEndpoints", actions)
        self.assertIn("bedrock-agentcore:DeleteAgentRuntimeEndpoint", actions)

    def test_has_workload_identity_actions(self):
        """deployer_policy must include DeleteWorkloadIdentity; List and Get are absent."""
        p = self._policy()
        actions = []
        for s in p["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        self.assertIn("bedrock-agentcore:DeleteWorkloadIdentity", actions)
        # ListWorkloadIdentities is never called by cleanup or deploy — it is
        # excess permission and must not appear in the policy.
        self.assertNotIn("bedrock-agentcore:ListWorkloadIdentities", actions,
                         "ListWorkloadIdentities is never called; must not appear")
        # GetWorkloadIdentity is never called by cleanup or deploy — the workload
        # identity name is parsed from GetAgentRuntime's workloadIdentityDetails.
        self.assertNotIn("bedrock-agentcore:GetWorkloadIdentity", actions,
                         "GetWorkloadIdentity is never called; must not appear")

    def test_workload_identity_list_absent(self):
        """ListWorkloadIdentities must NOT be present — it is never called."""
        p = self._policy(agentcore_prefix="mypfx")
        for stmt in p["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            self.assertNotIn(
                "bedrock-agentcore:ListWorkloadIdentities", actions,
                "ListWorkloadIdentities must not appear in deployer_policy "
                "(no code path calls list_workload_identities)",
            )

    def test_workload_identity_delete_scoped_to_prefix(self):
        """DeleteWorkloadIdentity must be scoped to identities matching the prefix."""
        p = self._policy(agentcore_prefix="mypfx")
        for stmt in p["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock-agentcore:DeleteWorkloadIdentity" in actions:
                self.assertNotEqual(stmt["Resource"], "*")
                self.assertIn("mypfx", stmt["Resource"])
                self.assertIn("workload-identity", stmt["Resource"])
                return
        self.fail("No DeleteWorkloadIdentity statement found")

    def test_has_s3_versioned_cleanup_actions(self):
        """deployer_policy must include s3:ListBucketVersions and s3:DeleteObjectVersion."""
        p = self._policy()
        actions = []
        for s in p["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        self.assertIn("s3:ListBucketVersions", actions)
        self.assertIn("s3:DeleteObjectVersion", actions)

    def test_delete_object_version_scoped_not_wildcard(self):
        """s3:DeleteObjectVersion must be scoped to an object ARN, not '*'."""
        p = self._policy()
        for stmt in p["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "s3:DeleteObjectVersion" in actions:
                res = stmt["Resource"]
                self.assertNotEqual(res, "*")
                self.assertIn(BUCKET, res)
                return
        self.fail("No s3:DeleteObjectVersion statement found")

    def test_list_bucket_versions_uses_prefix_condition(self):
        """s3:ListBucketVersions must be on the bucket ARN with s3:prefix condition.

        The bucket ARN + s3:prefix condition is required to scope the list to the
        deployment's transfer prefix so the cleanup cannot enumerate all versions
        in unrelated prefixes.
        """
        p = self._policy()
        for stmt in p["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "s3:ListBucketVersions" in actions:
                # Resource must be the bucket ARN (no key path suffix)
                res = stmt["Resource"]
                self.assertIn(BUCKET, res)
                self.assertNotIn("/*", res,
                                 "s3:ListBucketVersions must be on bucket ARN, not object ARN")
                # Condition must use s3:prefix
                cond = stmt.get("Condition", {})
                prefix_cond = (cond.get("StringLike", {}) or cond.get("StringEquals", {}))
                self.assertIn(
                    "s3:prefix", prefix_cond,
                    "s3:ListBucketVersions must use s3:prefix condition to scope to the transfer prefix",
                )
                return
        self.fail("No s3:ListBucketVersions statement found")

    def test_has_log_group_cleanup_actions(self):
        """deployer_policy must include logs:DescribeLogGroups and logs:DeleteLogGroup."""
        p = self._policy()
        actions = []
        for s in p["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        self.assertIn("logs:DescribeLogGroups", actions)
        self.assertIn("logs:DeleteLogGroup", actions)

    def test_delete_log_group_scoped_to_agentcore_prefix(self):
        """logs:DeleteLogGroup must be scoped to the AgentCore runtimes log group ARN."""
        p = self._policy(agentcore_prefix="mypfx")
        for stmt in p["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "logs:DeleteLogGroup" in actions:
                res = stmt["Resource"]
                self.assertNotEqual(res, "*")
                self.assertIn("bedrock-agentcore", res)
                self.assertIn("mypfx", res)
                return
        self.fail("No logs:DeleteLogGroup statement found")

    def test_is_valid_json(self):
        json.loads(json.dumps(self._policy()))


class WorkstationPolicyTests(unittest.TestCase):
    def _policy(self, **kw):
        kw.setdefault("region", REGION)
        kw.setdefault("agentcore_prefix", "pfx")
        return workstation_policy(ACCOUNT, BUCKET, PREFIX, **kw)

    def test_has_s3_get_put(self):
        p = self._policy()
        actions = []
        for s in p["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
        self.assertIn("s3:GetObject", actions)
        self.assertIn("s3:PutObject", actions)

    def test_per_user_narrows_scope(self):
        p = self._policy(user_namespace="carol")
        s3_stmts = [s for s in p["Statement"]
                    if isinstance(s.get("Action"), list)
                    and "s3:GetObject" in s["Action"]]
        self.assertIn("carol", s3_stmts[0]["Resource"])

    def test_is_valid_json(self):
        json.loads(json.dumps(self._policy()))

    def test_bedrock_invoke_scoped_not_wildcard(self):
        """bedrock:InvokeModel must not have Resource '*' in the workstation policy."""
        p = self._policy(region="us-east-1", agentcore_prefix="testpfx")
        for stmt in p["Statement"]:
            actions = stmt["Action"]
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock:InvokeModel" in actions:
                res = stmt["Resource"]
                self.assertNotEqual(res, "*",
                                    "bedrock:InvokeModel must not use Resource '*'")
                if isinstance(res, list):
                    self.assertNotIn("*", res,
                                     "bedrock:InvokeModel must not include '*' in resource list")

    def test_agentcore_invoke_scoped_not_wildcard(self):
        """bedrock-agentcore:InvokeAgentRuntime must be scoped to a prefix ARN."""
        p = self._policy(region="us-east-1", agentcore_prefix="testpfx")
        for stmt in p["Statement"]:
            actions = stmt["Action"]
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock-agentcore:InvokeAgentRuntime" in actions:
                res = stmt["Resource"]
                self.assertNotEqual(res, "*",
                                    "bedrock-agentcore:InvokeAgentRuntime must not use '*'")
                self.assertIn("testpfx", res,
                              "InvokeAgentRuntime resource must include the agentcore_prefix")

    def test_list_agent_runtimes_in_own_statement(self):
        """ListAgentRuntimes must be in its own statement with Resource '*' and Sid 'ListOnly'."""
        p = self._policy()
        list_stmts = [
            s for s in p["Statement"]
            if s.get("Action") == "bedrock-agentcore:ListAgentRuntimes"
            or (isinstance(s.get("Action"), list)
                and s["Action"] == ["bedrock-agentcore:ListAgentRuntimes"])
        ]
        self.assertEqual(len(list_stmts), 1,
                         "Expected exactly one statement for ListAgentRuntimes")
        stmt = list_stmts[0]
        self.assertEqual(stmt["Resource"], "*",
                         "ListAgentRuntimes must have Resource '*'")
        self.assertEqual(stmt.get("Sid"), "ListOnly",
                         "ListAgentRuntimes statement must have Sid 'ListOnly'")


class NameHelpersTests(unittest.TestCase):
    def test_role_name_shared(self):
        self.assertEqual(role_name("BaseRole", None), "BaseRole")

    def test_role_name_per_user(self):
        self.assertEqual(role_name("BaseRole", "alice"), "BaseRole-alice")

    def test_policy_name_shared(self):
        self.assertEqual(policy_name("BasePolicy", None), "BasePolicy")

    def test_policy_name_per_user(self):
        self.assertEqual(policy_name("BasePolicy", "bob"), "BasePolicy-bob")


class PrintPolicyTests(unittest.TestCase):
    """print_policy smoke tests — output must be valid JSON."""

    def _capture(self, kind: str) -> dict:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_policy(kind)
        return json.loads(buf.getvalue())

    def test_execution_policy_is_valid(self):
        p = self._capture("execution")
        self.assertIn("Statement", p)

    def test_deployer_policy_is_valid(self):
        p = self._capture("deployer")
        self.assertIn("Statement", p)

    def test_workstation_policy_is_valid(self):
        p = self._capture("workstation")
        self.assertIn("Statement", p)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            print_policy("unknown")


# ---------------------------------------------------------------------------
# module source must not contain the invalid bedrock-agentcore-control prefix
# ---------------------------------------------------------------------------

class IamModuleSourceTests(unittest.TestCase):
    """The invalid IAM prefix bedrock-agentcore-control must not appear anywhere in iam.py."""

    def test_no_bedrock_agentcore_control_prefix_in_source(self):
        import inspect
        import harness_image.iam as iam_module
        source = inspect.getsource(iam_module)
        self.assertNotIn(
            "bedrock-agentcore-control",
            source,
            "Found invalid IAM prefix 'bedrock-agentcore-control' in harness_image/iam.py; "
            "the correct prefix is 'bedrock-agentcore'.",
        )


# ---------------------------------------------------------------------------
# Least-privilege: no destructive action may use Resource "*"
# ---------------------------------------------------------------------------

_DESTRUCTIVE_ACTION_PREFIXES = (
    "Delete", "Update", "Create", "PutRolePolicy", "AttachRolePolicy",
)

# CreateAgentRuntime must use Resource "*" because the runtime ARN is
# service-generated at creation time (NAME-RANDOM10CHARS suffix) and cannot
# be pre-computed.  It is isolated in its own statement with Sid "NoResourceScopingSupported"
# so reviewers can see and justify the exception.
_WILDCARD_RESOURCE_EXCEPTIONS = frozenset(["bedrock-agentcore:CreateAgentRuntime"])


def _is_destructive(action: str) -> bool:
    """Return True if action matches a destructive pattern and is not a known exception."""
    if action in _WILDCARD_RESOURCE_EXCEPTIONS:
        return False
    # Strip the service prefix (e.g. "iam:", "bedrock-agentcore:")
    bare = action.split(":")[-1] if ":" in action else action
    for p in _DESTRUCTIVE_ACTION_PREFIXES:
        if bare.startswith(p):
            return True
    return False


class LeastPrivilegeTests(unittest.TestCase):
    """No statement containing a destructive action may have Resource '*'."""

    def _check_policy(self, policy: dict, label: str) -> None:
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            resource = stmt.get("Resource", "")
            has_wildcard = resource == "*" or (
                isinstance(resource, list) and "*" in resource
            )
            if has_wildcard:
                for action in actions:
                    self.assertFalse(
                        _is_destructive(action),
                        f"{label}: destructive action {action!r} must not have "
                        f"Resource '*' (Sid={stmt.get('Sid')!r})",
                    )

    def test_execution_policy_no_destructive_wildcard(self):
        p = execution_policy(ACCOUNT, REGION, BUCKET, PREFIX)
        self._check_policy(p, "execution_policy")

    def test_deployer_policy_no_destructive_wildcard(self):
        p = deployer_policy(ACCOUNT, REGION, BUCKET, PREFIX,
                            role_name="TestRole", agentcore_prefix="pfx")
        self._check_policy(p, "deployer_policy")

    def test_workstation_policy_no_destructive_wildcard(self):
        p = workstation_policy(ACCOUNT, BUCKET, PREFIX,
                               region=REGION, agentcore_prefix="pfx")
        self._check_policy(p, "workstation_policy")

    def test_every_execution_statement_has_sid(self):
        """Every statement in all three generated policies must have a Sid."""
        for kind_fn, label, kwargs in [
            (lambda: execution_policy(ACCOUNT, REGION, BUCKET, PREFIX), "execution", {}),
            (lambda: deployer_policy(ACCOUNT, REGION, BUCKET, PREFIX,
                                     role_name="R", agentcore_prefix="pfx"), "deployer", {}),
            (lambda: workstation_policy(ACCOUNT, BUCKET, PREFIX,
                                        region=REGION, agentcore_prefix="pfx"), "workstation", {}),
        ]:
            policy = kind_fn()
            for i, stmt in enumerate(policy["Statement"]):
                self.assertIn(
                    "Sid", stmt,
                    f"{label} statement[{i}] (actions={stmt.get('Action')!r}) is missing a Sid",
                )
                sid = stmt["Sid"]
                self.assertTrue(
                    sid.isalnum(),
                    f"{label} statement[{i}] Sid {sid!r} must be alphanumeric",
                )

    def test_execution_workload_token_scoped(self):
        """GetWorkloadAccessToken must not use Resource '*' in execution policy."""
        p = execution_policy(ACCOUNT, REGION, BUCKET, PREFIX)
        for stmt in p["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock-agentcore:GetWorkloadAccessToken" in actions:
                res = stmt["Resource"]
                self.assertNotEqual(res, "*",
                                    "GetWorkloadAccessToken must not use Resource '*'")
                if isinstance(res, list):
                    self.assertNotIn("*", res)
                    # Both directory and identity ARNs must be present
                    combined = " ".join(res)
                    self.assertIn("workload-identity-directory/default", combined)

    def test_execution_bedrock_invoke_scoped(self):
        """bedrock:InvokeModel must not use Resource '*' in execution policy."""
        p = execution_policy(ACCOUNT, REGION, BUCKET, PREFIX)
        for stmt in p["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock:InvokeModel" in actions:
                res = stmt["Resource"]
                self.assertNotEqual(res, "*",
                                    "bedrock:InvokeModel must not use Resource '*'")
                if isinstance(res, list):
                    self.assertNotIn("*", res)
                    combined = " ".join(res)
                    self.assertIn("foundation-model", combined)
                    self.assertIn("inference-profile", combined)


class CodexResponsesApiTests(unittest.TestCase):
    """Codex's Responses API on bedrock-runtime is authorized against the account's
    default Bedrock project, not a model ARN; without it only Codex is denied."""

    def test_bedrock_invoke_grants_the_default_project_only(self):
        project = f"arn:aws:bedrock:*:{ACCOUNT}:project/default"
        for label, policy in (
            ("execution", execution_policy(ACCOUNT, REGION, BUCKET, PREFIX)),
            ("workstation", workstation_policy(ACCOUNT, BUCKET, PREFIX, region=REGION,
                                               agentcore_prefix="pfx")),
        ):
            with self.subTest(policy=label):
                stmt = next(s for s in policy["Statement"] if s.get("Sid") == "BedrockInvoke")
                self.assertIn(project, stmt["Resource"])
                self.assertFalse([r for r in stmt["Resource"] if r.endswith(":project/*")])


if __name__ == "__main__":
    unittest.main()
