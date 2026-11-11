# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for harness_image.cleanup.

All AWS calls are intercepted via fake boto3 clients.  No real AWS access occurs.

Coverage:
  - discover() returns exactly the right resources in shared and per-user isolation
  - a runtime named '<prefix>_claude_bob' is NOT deleted when cleaning shared '<prefix>'
  - prefix boundary: 'code/x' never matches 'code/xy'
  - empty S3 prefix raises ValueError (exit 2)
  - pinned AGENTCORE_EXECUTION_ROLE_ARN skips IAM discovery
  - role with wrong tag / foreign trust / extra policies → kept, not deleted
  - cleanup_token() changes when --include-artifacts is given
  - --keep-logs marks log groups as "keep" instead of "delete"
  - non-TTY without approve token → exit 3, nothing deleted
  - token mismatch with --approve → exit 3, nothing deleted
  - TTY prompt with wrong account ID → exit 1, nothing deleted
  - TTY prompt with correct account ID → proceeds to delete
  - versioned S3 deletion paginates list_object_versions and batches at 1000
  - partial deletion failure continues; exit 1 with errors list
  - second run (nothing left) → exit 0 "nothing to delete"
  - local files removed only for deleted runtimes' sessions
  - deploy/agentcore-cleanup.sh is executable and delegates to the cleanup subcommand
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── helpers ───────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
ACCOUNT = "123456789012"
REGION = "us-east-1"
BUCKET = "my-test-bucket"
PREFIX = "code/testdeploy"
ROLE_NAME = "MyDeployRole"
POLICY_NAME = "MyDeployPolicy"
APP_TAG = "TestApp"
RUNTIME_PREFIX = "testpfx"
WORKLOAD_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    f"workload-identity-directory/default/workload-identity/testpfx_claude"
)


def _make_cfg(
    *,
    isolation: str = "shared",
    user_namespace: str | None = None,
    execution_role_arn: str | None = None,
    transfer_s3_uri: str | None = None,
    prefix: str = PREFIX,
    data_dir: str | None = None,
) -> object:
    """Build a minimal DeployConfig-like object."""
    from harness_image.deploy import DeployConfig

    return DeployConfig(
        transfer_s3_uri=transfer_s3_uri or f"s3://{BUCKET}/{prefix}",
        region=REGION,
        execution_role_arn=execution_role_arn,
        execution_role_name=ROLE_NAME,
        inline_policy_name=POLICY_NAME,
        application_tag=APP_TAG,
        runtime_name_prefix=RUNTIME_PREFIX,
        isolation=isolation,
        user_namespace=user_namespace,
        data_dir=data_dir or tempfile.mkdtemp(),
    )


def _make_runtime(harness: str, name_prefix: str = RUNTIME_PREFIX,
                  namespace: str | None = None) -> dict:
    """Return a fake list_agent_runtimes item."""
    if namespace:
        clean = namespace.replace("-", "_")
        name = f"{name_prefix}_{harness}_{clean}"
    else:
        name = f"{name_prefix}_{harness}"
    rt_id = f"{name}-AbCdEfGhIj"
    arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{rt_id}"
    return {
        "agentRuntimeName": name,
        "agentRuntimeId": rt_id,
        "agentRuntimeArn": arn,
        "status": "READY",
    }


def _fake_get_runtime(rt: dict) -> dict:
    """Return a fake get_agent_runtime response."""
    return {
        **rt,
        "workloadIdentityDetails": {"workloadIdentityArn": WORKLOAD_ARN},
    }


def _make_fake_control(harnesses=("claude", "codex", "kiro"),
                       prefix=RUNTIME_PREFIX, namespace=None,
                       endpoints=None):
    """Build a fake bedrock-agentcore-control client."""
    runtimes = [_make_runtime(h, prefix, namespace) for h in harnesses]
    endpoints = endpoints or {}  # harness -> list of endpoint dicts

    ctrl = MagicMock()
    ctrl.list_agent_runtimes.return_value = {"agentRuntimes": runtimes}
    ctrl.get_agent_runtime.side_effect = lambda agentRuntimeId, **kw: _fake_get_runtime(
        next(r for r in runtimes if r["agentRuntimeId"] == agentRuntimeId
             or r["agentRuntimeArn"].endswith(agentRuntimeId))
    )
    ctrl.list_agent_runtime_endpoints.return_value = {"runtimeEndpoints": []}
    ctrl.delete_agent_runtime.return_value = {}
    ctrl.delete_agent_runtime_endpoint.return_value = {}
    ctrl.delete_workload_identity.return_value = {}

    from botocore.exceptions import ClientError
    def _get_wait(agentRuntimeId, **kw):
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
            "GetAgentRuntime",
        )
    ctrl.get_agent_runtime.side_effect = lambda agentRuntimeId, **kw: (
        _fake_get_runtime(
            next(
                (r for r in runtimes
                 if r["agentRuntimeId"] == agentRuntimeId
                 or r["agentRuntimeArn"].endswith(agentRuntimeId)),
                {}
            )
        )
    )
    return ctrl, runtimes


def _make_fake_iam(tag_value=APP_TAG, has_agentcore_trust=True,
                   extra_inline=None, managed=None):
    """Build a fake IAM client."""
    iam = MagicMock()
    trust_doc = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }] if has_agentcore_trust else [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    iam.get_role.return_value = {
        "Role": {
            "RoleName": ROLE_NAME,
            "Arn": f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}",
            "AssumeRolePolicyDocument": trust_doc,
            "Tags": [{"Key": "Application", "Value": tag_value}],
        }
    }
    inline = [POLICY_NAME] + (extra_inline or [])
    iam.list_role_policies.return_value = {"PolicyNames": inline}
    iam.list_attached_role_policies.return_value = {
        "AttachedPolicies": managed or []
    }
    iam.delete_role_policy.return_value = {}
    iam.delete_role.return_value = {}
    from botocore.exceptions import ClientError as CE
    iam.exceptions = types.SimpleNamespace(
        NoSuchEntityException=type("NoSuchEntityException", (CE,), {})
    )
    return iam


def _make_fake_s3(versions=None):
    """Build a fake S3 client that returns the given versions.

    When *versions* is not supplied, a single placeholder key is included so
    that the S3-probe added in discover() sees non-empty data and marks the
    prefix as action='delete'.  Pass ``versions=[]`` explicitly to simulate an
    already-empty prefix (e.g. the second-run no-op scenario).
    """
    s3 = MagicMock()
    vs = versions if versions is not None else ["placeholder/key"]
    s3.list_object_versions.return_value = {
        "IsTruncated": False,
        "Versions": [{"Key": v, "VersionId": f"vid-{i}"}
                     for i, v in enumerate(vs)],
        "DeleteMarkers": [],
    }
    s3.delete_objects.return_value = {"Deleted": [{"Key": v} for v in vs], "Errors": []}
    return s3


def _make_fake_logs(log_group_names=None):
    logs = MagicMock()
    groups = [{"logGroupName": n} for n in (log_group_names or [])]
    logs.describe_log_groups.return_value = {"logGroups": groups}
    logs.delete_log_group.return_value = {}
    return logs


def _make_fake_session(
    harnesses=("claude", "codex", "kiro"),
    prefix=RUNTIME_PREFIX,
    namespace=None,
    iam=None,
    s3=None,
    logs=None,
    endpoints=None,
):
    """Assemble fake boto3.Session with all sub-clients."""
    ctrl, runtimes = _make_fake_control(harnesses, prefix, namespace, endpoints)
    iam = iam or _make_fake_iam()
    s3 = s3 or _make_fake_s3()
    logs = logs or _make_fake_logs()

    sts = MagicMock()
    sts.get_caller_identity.return_value = {
        "Account": ACCOUNT,
        "UserId": "AIEXAMPLE",
        "Arn": f"arn:aws:iam::{ACCOUNT}:user/tester",
    }

    session = MagicMock()
    session.client.side_effect = lambda service, **kw: {
        "sts": sts,
        "bedrock-agentcore-control": ctrl,
        "iam": iam,
        "s3": s3,
        "logs": logs,
    }[service]
    return session, ctrl, iam, s3, logs, runtimes


# ── discover() tests ──────────────────────────────────────────────────────────

class DiscoverSharedIsolationTests(unittest.TestCase):
    """In shared isolation, discover() should find all three runtimes."""

    def setUp(self):
        self.cfg = _make_cfg(isolation="shared")
        self.session, self.ctrl, self.iam, self.s3, self.logs, self.rts = (
            _make_fake_session()
        )

    def _run(self):
        from harness_image.cleanup import discover
        return discover(self.cfg, self.session, ACCOUNT)

    def test_finds_all_three_runtimes(self):
        resources = self._run()
        runtime_ids = {r.identifier for r in resources if r.type == "runtime"}
        self.assertIn("runtime:testpfx_claude", runtime_ids)
        self.assertIn("runtime:testpfx_codex", runtime_ids)
        self.assertIn("runtime:testpfx_kiro", runtime_ids)

    def test_all_runtimes_marked_delete(self):
        resources = self._run()
        for r in resources:
            if r.type == "runtime":
                self.assertEqual(r.action, "delete")

    def test_workload_identities_found(self):
        resources = self._run()
        wi = [r for r in resources if r.type == "workload_identity"]
        # One per runtime (mocked workload ARN for all)
        self.assertEqual(len(wi), 3)
        for r in wi:
            self.assertEqual(r.action, "delete")

    def test_s3_objects_include_full_prefix(self):
        resources = self._run()
        s3r = [r for r in resources if r.type == "s3_objects" and r.action == "delete"]
        self.assertTrue(s3r)
        # Shared: the whole transfer prefix is listed
        identifiers = [r.identifier for r in s3r]
        self.assertIn(f"s3:{BUCKET}/{PREFIX}/", identifiers)

    def test_iam_role_marked_delete(self):
        resources = self._run()
        role = [r for r in resources if r.type == "iam_role" and r.action == "delete"]
        self.assertTrue(role, "Expected iam_role with action=delete")

    def test_iam_policy_marked_delete(self):
        resources = self._run()
        policy = [r for r in resources if r.type == "iam_policy" and r.action == "delete"]
        self.assertTrue(policy)


class DiscoverPerUserIsolationTests(unittest.TestCase):
    """In per-user isolation, discover() scopes S3 to {prefix}/{namespace}/."""

    def setUp(self):
        self.cfg = _make_cfg(isolation="per-user", user_namespace="alice")
        self.session, *_ = _make_fake_session(
            prefix=RUNTIME_PREFIX, namespace="alice"
        )

    def _run(self):
        from harness_image.cleanup import discover
        return discover(self.cfg, self.session, ACCOUNT)

    def test_s3_scoped_to_namespace(self):
        resources = self._run()
        s3_delete = [r for r in resources if r.type == "s3_objects" and r.action == "delete"]
        self.assertTrue(s3_delete)
        identifiers = [r.identifier for r in s3_delete]
        self.assertIn(f"s3:{BUCKET}/{PREFIX}/alice/", identifiers)
        # Full prefix must NOT be in delete list
        self.assertNotIn(f"s3:{BUCKET}/{PREFIX}/", identifiers)

    def test_shared_bundles_kept(self):
        resources = self._run()
        keep_ids = {r.identifier for r in resources if r.action == "keep"}
        self.assertTrue(
            any("bundles" in i for i in keep_ids),
            "shared bundles/ should be listed as keep"
        )

    def test_shared_tools_kept(self):
        resources = self._run()
        keep_ids = {r.identifier for r in resources if r.action == "keep"}
        self.assertTrue(
            any("tools" in i for i in keep_ids),
            "shared tools/ should be listed as keep"
        )

    def test_per_user_meta_prefix_ends_with_slash(self):
        """meta['prefix'] for per-user S3 delete resources must end with '/'
        to prevent a prefix-boundary collision (e.g. 'code/alice' also matching
        objects under 'code/alice2/')."""
        resources = self._run()
        s3_delete = [r for r in resources if r.type == "s3_objects" and r.action == "delete"]
        for r in s3_delete:
            self.assertTrue(
                r.meta.get("prefix", "").endswith("/"),
                f"per-user meta prefix {r.meta.get('prefix')!r} must end with '/' "
                "to prevent boundary collision",
            )


class DiscoverSharedDoesNotMatchPerUserRuntime(unittest.TestCase):
    """Shared mode must never delete per-user runtimes for other namespaces."""

    def test_per_user_runtime_not_in_shared_delete_list(self):
        """A runtime named '<prefix>_claude_bob' must NOT be found when cleaning shared.

        The per-user runtime is placed FIRST in the list to ensure the exact-name
        match (not a prefix/startswith match) selects the correct runtime.  The
        meta['runtime_id'] assertion catches any mutation that widens the match.
        """
        from harness_image.cleanup import discover

        cfg = _make_cfg(isolation="shared")  # shared → no namespace
        # Simulate an account where a per-user runtime 'testpfx_claude_bob' also exists.
        # Per-user runtime is first so that a startswith-mutation would select it.
        shared_rt = _make_runtime("claude", RUNTIME_PREFIX, None)
        per_user_rt = _make_runtime("claude", RUNTIME_PREFIX, "bob")

        ctrl = MagicMock()
        ctrl.list_agent_runtimes.return_value = {
            "agentRuntimes": [per_user_rt, shared_rt]  # per-user listed first
        }
        ctrl.get_agent_runtime.side_effect = lambda agentRuntimeId, **kw: (
            _fake_get_runtime(
                next(r for r in [shared_rt, per_user_rt]
                     if r["agentRuntimeId"] == agentRuntimeId)
            )
        )
        ctrl.list_agent_runtime_endpoints.return_value = {"runtimeEndpoints": []}

        session = MagicMock()
        session.client.side_effect = lambda svc, **kw: {
            "bedrock-agentcore-control": ctrl,
            "iam": _make_fake_iam(),
            "s3": _make_fake_s3(),
            "logs": _make_fake_logs(),
        }[svc]

        resources = discover(cfg, session, ACCOUNT)
        delete_ids = {r.identifier for r in resources if r.action == "delete"}

        # Shared runtime ('testpfx_claude') should be in delete list
        self.assertIn("runtime:testpfx_claude", delete_ids)
        # Per-user runtime ('testpfx_claude_bob') must NOT be in delete list
        self.assertNotIn("runtime:testpfx_claude_bob", delete_ids)

        # The runtime selected for 'claude' must carry shared_rt's actual ID, not
        # the per-user one.  This catches a startswith-mutation that would select
        # per_user_rt when it appears first in the API response.
        claude_deletes = [
            r for r in resources
            if r.type == "runtime" and r.action == "delete" and "claude" in r.identifier
        ]
        self.assertTrue(claude_deletes, "expected a claude runtime in delete list")
        self.assertEqual(
            claude_deletes[0].meta["runtime_id"],
            shared_rt["agentRuntimeId"],
            "runtime_id must be the shared runtime's ID, not the per-user runtime's",
        )


class DiscoverLogGroupPrefixTests(unittest.TestCase):
    """describe_log_groups must use /aws/bedrock-agentcore/runtimes/<runtime_id>- (trailing dash).

    Without the trailing '-', a runtime ID that is a prefix of another runtime's
    ID could match that other runtime's log groups.
    """

    def test_describe_log_groups_uses_trailing_dash(self):
        """discover() must append '-' to runtime_id before calling describe_log_groups."""
        from harness_image.cleanup import discover

        cfg = _make_cfg(isolation="shared")
        session, ctrl, iam, s3, logs, rts = _make_fake_session()

        discover(cfg, session, ACCOUNT)

        # For each runtime, describe_log_groups should have been called with a
        # logGroupNamePrefix that ends in '-'.
        self.assertGreater(
            logs.describe_log_groups.call_count, 0,
            "describe_log_groups should be called at least once"
        )
        for call in logs.describe_log_groups.call_args_list:
            prefix = call[1].get("logGroupNamePrefix") or call[0][0]
            self.assertTrue(
                prefix.endswith("-"),
                f"logGroupNamePrefix {prefix!r} must end with '-' to avoid matching "
                "a runtime whose ID starts with the same characters"
            )
            self.assertIn("/aws/bedrock-agentcore/runtimes/", prefix)


class DiscoverPrefixBoundaryTests(unittest.TestCase):
    """S3 prefix 'code/x' must never match objects under 'code/xy'."""

    def test_prefix_with_trailing_slash_prevents_boundary_collision(self):
        """Cleanup for prefix 'code/x' must not delete 'code/xy/file'."""
        from harness_image.cleanup import discover

        cfg = _make_cfg(isolation="shared", prefix="code/x")
        session, *_ = _make_fake_session()
        resources = discover(cfg, session, ACCOUNT)

        s3_delete = [r for r in resources if r.type == "s3_objects" and r.action == "delete"]
        for r in s3_delete:
            prefix_used = r.meta.get("prefix", "")
            # The prefix passed to S3 list must end with '/' to prevent boundary collision
            self.assertTrue(
                prefix_used.endswith("/"),
                f"S3 prefix {prefix_used!r} must end with '/' to prevent matching 'code/xy'"
            )
            self.assertNotIn("code/xy", prefix_used)


class DiscoverEmptyPrefixTests(unittest.TestCase):
    def test_empty_s3_prefix_raises_value_error(self):
        """discover() must raise ValueError when the transfer URI prefix is empty."""
        from harness_image.cleanup import discover

        # s3://bucket/ → prefix="" after strip("/")
        cfg = _make_cfg(isolation="shared", transfer_s3_uri=f"s3://{BUCKET}/")
        session, *_ = _make_fake_session()
        with self.assertRaises(ValueError) as cm:
            discover(cfg, session, ACCOUNT)
        self.assertIn("empty prefix", str(cm.exception).lower())


class DiscoverPerUserMissingNamespaceTests(unittest.TestCase):
    """per-user isolation with no USER_NAMESPACE must raise ValueError (exit 2)."""

    def test_per_user_without_namespace_raises_value_error(self):
        """When isolation is per-user and user_namespace is None, discover() must raise
        ValueError rather than falling through to the shared deletion path.

        Without this guard, the absence of USER_NAMESPACE would silently schedule the
        entire {prefix}/ for deletion instead of only {prefix}/{namespace}/, potentially
        deleting other users' data.
        """
        from harness_image.cleanup import discover

        # isolation='per-user' with user_namespace=None
        cfg = _make_cfg(isolation="per-user", user_namespace=None)
        session, *_ = _make_fake_session()
        with self.assertRaises(ValueError) as cm:
            discover(cfg, session, ACCOUNT)
        msg = str(cm.exception).lower()
        self.assertIn("user_namespace", msg)

    def test_per_user_without_namespace_does_not_schedule_full_prefix_deletion(self):
        """Verify the error is raised BEFORE any S3 resources are appended.

        A probe that catches the ValueError and inspects the partial resource list
        is impractical; we just confirm the exception message mentions the safety
        rationale so the guard text is informative.
        """
        from harness_image.cleanup import discover

        cfg = _make_cfg(isolation="per-user", user_namespace=None)
        session, *_ = _make_fake_session()
        with self.assertRaises(ValueError) as cm:
            discover(cfg, session, ACCOUNT)
        # The message must mention the safety concern so operators understand why.
        self.assertIn("per-user", str(cm.exception).lower())


class DiscoverPinnedRoleArnTests(unittest.TestCase):
    def test_pinned_execution_role_arn_skips_iam(self):
        """When AGENTCORE_EXECUTION_ROLE_ARN is set, IAM resources must be listed as keep."""
        from harness_image.cleanup import discover

        role_arn = f"arn:aws:iam::{ACCOUNT}:role/ExternalRole"
        cfg = _make_cfg(execution_role_arn=role_arn)
        session, *_ = _make_fake_session()
        resources = discover(cfg, session, ACCOUNT)

        iam_resources = [r for r in resources if r.type in ("iam_role", "iam_policy")]
        for r in iam_resources:
            self.assertEqual(r.action, "keep",
                             f"IAM resource {r.identifier!r} should be kept when role ARN is pinned")


class DiscoverIAMTagMismatchTests(unittest.TestCase):
    def test_wrong_application_tag_keeps_role(self):
        from harness_image.cleanup import discover
        cfg = _make_cfg()
        iam = _make_fake_iam(tag_value="OtherApp")
        session, ctrl, _, s3, logs, _ = _make_fake_session(iam=iam)
        resources = discover(cfg, session, ACCOUNT)
        role_r = [r for r in resources if r.type == "iam_role"]
        self.assertTrue(role_r)
        self.assertEqual(role_r[0].action, "keep")
        self.assertIn("Application", role_r[0].reason)

    def test_foreign_trust_keeps_role(self):
        from harness_image.cleanup import discover
        cfg = _make_cfg()
        iam = _make_fake_iam(has_agentcore_trust=False)
        session, ctrl, _, s3, logs, _ = _make_fake_session(iam=iam)
        resources = discover(cfg, session, ACCOUNT)
        role_r = [r for r in resources if r.type == "iam_role"]
        self.assertTrue(role_r)
        self.assertEqual(role_r[0].action, "keep")
        self.assertIn("trust", role_r[0].reason.lower())

    def test_extra_inline_policy_keeps_role(self):
        from harness_image.cleanup import discover
        cfg = _make_cfg()
        iam = _make_fake_iam(extra_inline=["AnotherInlinePolicy"])
        session, ctrl, _, s3, logs, _ = _make_fake_session(iam=iam)
        resources = discover(cfg, session, ACCOUNT)
        role_r = [r for r in resources if r.type == "iam_role"]
        self.assertTrue(role_r)
        self.assertEqual(role_r[0].action, "keep")

    def test_managed_policy_keeps_role(self):
        from harness_image.cleanup import discover
        cfg = _make_cfg()
        iam = _make_fake_iam(managed=[{"PolicyArn": "arn:aws:iam::aws:policy/ReadOnly",
                                       "PolicyName": "ReadOnly"}])
        session, ctrl, _, s3, logs, _ = _make_fake_session(iam=iam)
        resources = discover(cfg, session, ACCOUNT)
        role_r = [r for r in resources if r.type == "iam_role"]
        self.assertTrue(role_r)
        self.assertEqual(role_r[0].action, "keep")

    def test_extra_inline_policy_on_second_page_keeps_role(self):
        """list_role_policies with IsTruncated=True must paginate to find all inline
        policies. A second inline policy visible only on page 2 must cause the role
        to be kept. Without the pagination loop the extra policy is invisible and the
        role would be incorrectly marked for deletion."""
        from harness_image.cleanup import discover

        cfg = _make_cfg()
        iam = _make_fake_iam()

        # Override list_role_policies to return IsTruncated=True on the first call,
        # with only the deployment policy visible (so the first page looks clean),
        # then reveal an extra policy on the second page.
        page1 = {
            "PolicyNames": [POLICY_NAME],
            "IsTruncated": True,
            "Marker": "page2_marker",
        }
        page2 = {
            "PolicyNames": ["ExtraInlineOnPage2"],
            "IsTruncated": False,
        }
        iam.list_role_policies.side_effect = [page1, page2]
        # No managed policies; the role should still be kept because of the extra inline.
        iam.list_attached_role_policies.return_value = {
            "AttachedPolicies": [], "IsTruncated": False,
        }

        session, ctrl, _, s3, logs, _ = _make_fake_session(iam=iam)
        resources = discover(cfg, session, ACCOUNT)

        role_r = [r for r in resources if r.type == "iam_role"]
        self.assertTrue(role_r)
        self.assertEqual(
            role_r[0].action, "keep",
            "role with an extra inline policy on page 2 must be kept, not deleted",
        )
        # Verify pagination was actually followed (called at least twice).
        self.assertGreaterEqual(
            iam.list_role_policies.call_count, 2,
            "list_role_policies must be called at least twice to cover all pages",
        )

    def test_managed_policy_on_second_page_keeps_role(self):
        """list_attached_role_policies with IsTruncated=True must paginate to find all
        managed policies. A managed policy visible only on page 2 must keep the role.
        Without the pagination loop the extra policy is invisible and the role would be
        incorrectly deleted."""
        from harness_image.cleanup import discover

        cfg = _make_cfg()
        iam = _make_fake_iam()

        # Only our own inline policy on the one inline page (no extras).
        iam.list_role_policies.return_value = {
            "PolicyNames": [POLICY_NAME],
            "IsTruncated": False,
        }
        # list_attached_role_policies: page 1 empty but truncated, page 2 has a policy.
        page1 = {
            "AttachedPolicies": [],
            "IsTruncated": True,
            "Marker": "managed_page2",
        }
        page2 = {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/ReadOnly",
                                   "PolicyName": "ReadOnly"}],
            "IsTruncated": False,
        }
        iam.list_attached_role_policies.side_effect = [page1, page2]

        session, ctrl, _, s3, logs, _ = _make_fake_session(iam=iam)
        resources = discover(cfg, session, ACCOUNT)

        role_r = [r for r in resources if r.type == "iam_role"]
        self.assertTrue(role_r)
        self.assertEqual(
            role_r[0].action, "keep",
            "role with a managed policy on page 2 must be kept, not deleted",
        )
        self.assertGreaterEqual(
            iam.list_attached_role_policies.call_count, 2,
            "list_attached_role_policies must be called at least twice to cover all pages",
        )


# ── cleanup_token() tests ─────────────────────────────────────────────────────

class CleanupTokenTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _make_cfg()
        self.session, *_ = _make_fake_session()

    def _token(self, **kw):
        from harness_image.cleanup import discover, cleanup_token
        resources = discover(self.cfg, self.session, ACCOUNT, **kw)
        return cleanup_token(ACCOUNT, REGION, resources)

    def test_token_is_16_chars(self):
        self.assertEqual(len(self._token()), 16)

    def test_include_artifacts_changes_token(self):
        art_uri = f"s3://{BUCKET}/output/myapp/"
        t_without = self._token(include_artifacts=False)
        t_with = self._token(include_artifacts=True, artifact_s3_uri=art_uri)
        self.assertNotEqual(t_without, t_with)

    def test_include_artifacts_false_with_uri_does_not_delete_artifact_prefix(self):
        """When include_artifacts=False the artifact prefix must NOT appear in the delete
        list even when artifact_s3_uri is provided.  Mutating the gate to
        ``if artifact_s3_uri:`` (dropping include_artifacts) would cause this test to fail
        because the artifact prefix would be scheduled for deletion without user opt-in."""
        from harness_image.cleanup import discover
        art_uri = f"s3://{BUCKET}/output/myapp/"
        resources = discover(
            self.cfg, self.session, ACCOUNT,
            include_artifacts=False,
            artifact_s3_uri=art_uri,
        )
        delete_ids = {r.identifier for r in resources if r.action == "delete"}
        self.assertNotIn(
            f"s3:{BUCKET}/output/myapp/",
            delete_ids,
            "artifact prefix must not be in delete list when include_artifacts=False",
        )

    def test_keep_logs_changes_token(self):
        # A session whose logs mock returns at least one log group so that
        # keep_logs=True and keep_logs=False produce different "delete" sets.
        runtime_id = _make_runtime("claude")["agentRuntimeId"]
        logs_with_groups = _make_fake_logs(
            [f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"]
        )
        session_with_logs, *_ = _make_fake_session(logs=logs_with_groups)
        from harness_image.cleanup import discover, cleanup_token
        t_delete = cleanup_token(
            ACCOUNT, REGION, discover(self.cfg, session_with_logs, ACCOUNT, keep_logs=False)
        )
        t_keep = cleanup_token(
            ACCOUNT, REGION, discover(self.cfg, session_with_logs, ACCOUNT, keep_logs=True)
        )
        self.assertNotEqual(t_delete, t_keep)


# ── run_cleanup() consent flow tests ─────────────────────────────────────────

class NonTTYWithoutApproveTests(unittest.TestCase):
    """Non-TTY without --approve must exit 3 and delete nothing."""

    def _run(self, extra_kw=None):
        from harness_image.cleanup import run_cleanup

        cfg = _make_cfg()
        session, ctrl, iam, s3, logs, _ = _make_fake_session()

        import boto3
        with patch("boto3.Session", return_value=session):
            # Simulate non-TTY: stdin.isatty() = False
            with patch.object(sys.stdin, "isatty", return_value=False):
                with patch.object(sys.stdout, "isatty", return_value=False):
                    import io
                    captured = io.StringIO()
                    with patch("sys.stdout", captured):
                        rc = run_cleanup(cfg, **(extra_kw or {}))
        return rc, ctrl, iam, s3, captured.getvalue()

    def test_exits_3(self):
        rc, *_ = self._run()
        self.assertEqual(rc, 3)

    def test_deletes_nothing(self):
        rc, ctrl, iam, s3, _ = self._run()
        ctrl.delete_agent_runtime.assert_not_called()
        iam.delete_role.assert_not_called()
        s3.delete_objects.assert_not_called()

    def test_outputs_json_with_token(self):
        rc, _, _, _, output = self._run()
        data = json.loads(output)
        self.assertIn("token", data)
        self.assertIn("resources", data)


class TokenMismatchTests(unittest.TestCase):
    def test_token_mismatch_exits_3_deletes_nothing(self):
        from harness_image.cleanup import run_cleanup
        from botocore.exceptions import ClientError

        cfg = _make_cfg()
        session, ctrl, iam, s3, logs, _ = _make_fake_session()

        # Make get_agent_runtime raise ResourceNotFoundException so that, if the
        # token-mismatch check were ever removed, _wait_runtime_deleted() would
        # return immediately instead of looping for ~300 s before the test fails.
        # discover() catches this exception harmlessly (workload identities are
        # absent from the plan, which does not affect correctness here).
        ctrl.get_agent_runtime.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "GetAgentRuntime",
        )

        import boto3
        with patch("boto3.Session", return_value=session):
            import io
            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = run_cleanup(cfg, approve_token="wrong_token_000")

        self.assertEqual(rc, 3)
        ctrl.delete_agent_runtime.assert_not_called()
        iam.delete_role.assert_not_called()


class TTYPromptTests(unittest.TestCase):
    def _run_with_input(self, typed_account: str):
        from harness_image.cleanup import run_cleanup
        from botocore.exceptions import ClientError
        import io

        cfg = _make_cfg()
        session, ctrl, iam, s3, logs, _ = _make_fake_session()

        # Make the deletion wait succeed immediately: get_agent_runtime raises
        # ResourceNotFoundException so _wait_runtime_deleted() returns at once.
        # discover() also calls get_agent_runtime but catches the exception and
        # continues (workload identities are absent from the plan, which is fine).
        ctrl.get_agent_runtime.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "GetAgentRuntime",
        )

        # A fake stdout that reports isatty()=True so run_cleanup takes the TTY
        # consent path.  (A plain io.StringIO returns isatty()=False.)
        class _FakeTTY(io.StringIO):
            def isatty(self):
                return True

        fake_stdout = _FakeTTY()

        import boto3
        with patch("boto3.Session", return_value=session):
            with patch.object(sys.stdin, "isatty", return_value=True):
                with patch("sys.stdout", fake_stdout):
                    with patch("builtins.input", return_value=typed_account):
                        rc = run_cleanup(cfg)
        return rc, ctrl, iam, s3

    def test_wrong_account_aborts(self):
        rc, ctrl, iam, s3 = self._run_with_input("000000000000")
        self.assertEqual(rc, 1)
        ctrl.delete_agent_runtime.assert_not_called()

    def test_correct_account_proceeds(self):
        rc, ctrl, iam, s3 = self._run_with_input(ACCOUNT)
        # Should attempt deletions
        self.assertIn(rc, (0, 1), "exit code should be 0 (success) or 1 (partial failure)")
        ctrl.delete_agent_runtime.assert_called()

    def _run_with_input_side_effect(self, side_effect):
        """Run run_cleanup() in TTY mode with input() raising the given exception."""
        from harness_image.cleanup import run_cleanup
        from botocore.exceptions import ClientError
        import io

        cfg = _make_cfg()
        session, ctrl, iam, s3, logs, _ = _make_fake_session()

        ctrl.get_agent_runtime.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "GetAgentRuntime",
        )

        class _FakeTTY(io.StringIO):
            def isatty(self):
                return True

        fake_stdout = _FakeTTY()

        import boto3
        with patch("boto3.Session", return_value=session):
            with patch.object(sys.stdin, "isatty", return_value=True):
                with patch("sys.stdout", fake_stdout):
                    with patch("builtins.input", side_effect=side_effect):
                        rc = run_cleanup(cfg)
        return rc, ctrl, iam, s3

    def test_eof_at_prompt_aborts(self):
        """EOFError at the consent prompt must abort with exit 1 and delete nothing."""
        rc, ctrl, iam, s3 = self._run_with_input_side_effect(EOFError)
        self.assertEqual(rc, 1, "EOFError at prompt must return exit code 1")
        ctrl.delete_agent_runtime.assert_not_called()
        iam.delete_role.assert_not_called()
        s3.delete_objects.assert_not_called()

    def test_keyboard_interrupt_at_prompt_aborts(self):
        """KeyboardInterrupt at the consent prompt must abort with exit 1 and delete nothing."""
        rc, ctrl, iam, s3 = self._run_with_input_side_effect(KeyboardInterrupt)
        self.assertEqual(rc, 1, "KeyboardInterrupt at prompt must return exit code 1")
        ctrl.delete_agent_runtime.assert_not_called()
        iam.delete_role.assert_not_called()
        s3.delete_objects.assert_not_called()


# ── S3 versioned deletion tests ───────────────────────────────────────────────

class S3VersionedDeletionTests(unittest.TestCase):
    def test_paginates_list_object_versions(self):
        """_delete_s3_versioned must follow IsTruncated pagination."""
        from harness_image.cleanup import _delete_s3_versioned

        s3 = MagicMock()
        # Page 1: truncated, 5 versions
        page1 = {
            "IsTruncated": True,
            "NextKeyMarker": "key5",
            "NextVersionIdMarker": "vid5",
            "Versions": [{"Key": f"k{i}", "VersionId": f"v{i}"} for i in range(5)],
            "DeleteMarkers": [],
        }
        # Page 2: not truncated, 3 versions
        page2 = {
            "IsTruncated": False,
            "Versions": [{"Key": f"k{i}", "VersionId": f"v{i}"} for i in range(5, 8)],
            "DeleteMarkers": [],
        }
        s3.list_object_versions.side_effect = [page1, page2]
        s3.delete_objects.return_value = {"Deleted": [], "Errors": []}

        errs = _delete_s3_versioned(BUCKET, "code/x/", ACCOUNT, s3)
        self.assertEqual(s3.list_object_versions.call_count, 2, "should paginate")
        self.assertFalse(errs)

    def test_batches_at_1000(self):
        """Batches of exactly 1000 (API limit) must be used when there are more than 1000 versions."""
        from harness_image.cleanup import _delete_s3_versioned

        s3 = MagicMock()
        # Single page with 1500 versions
        versions = [{"Key": f"k{i}", "VersionId": f"v{i}"} for i in range(1500)]
        s3.list_object_versions.return_value = {
            "IsTruncated": False,
            "Versions": versions,
            "DeleteMarkers": [],
        }
        s3.delete_objects.return_value = {"Deleted": [], "Errors": []}

        _delete_s3_versioned(BUCKET, "code/x/", ACCOUNT, s3)
        # Should have called delete_objects twice: first 1000, then 500
        self.assertEqual(s3.delete_objects.call_count, 2)
        first_call_keys = s3.delete_objects.call_args_list[0][1]["Delete"]["Objects"]
        self.assertEqual(len(first_call_keys), 1000)

    def test_delete_markers_included(self):
        """Delete markers must also be included in the delete batch."""
        from harness_image.cleanup import _delete_s3_versioned

        s3 = MagicMock()
        s3.list_object_versions.return_value = {
            "IsTruncated": False,
            "Versions": [{"Key": "a", "VersionId": "v1"}],
            "DeleteMarkers": [{"Key": "a", "VersionId": "dm1"}],
        }
        s3.delete_objects.return_value = {"Deleted": [], "Errors": []}

        _delete_s3_versioned(BUCKET, "code/x/", ACCOUNT, s3)
        objects_sent = s3.delete_objects.call_args[1]["Delete"]["Objects"]
        version_ids = {o["VersionId"] for o in objects_sent}
        self.assertIn("v1", version_ids)
        self.assertIn("dm1", version_ids)

    def test_per_key_errors_reported(self):
        """Errors in the delete_objects response must be returned as strings."""
        from harness_image.cleanup import _delete_s3_versioned

        s3 = MagicMock()
        s3.list_object_versions.return_value = {
            "IsTruncated": False,
            "Versions": [{"Key": "locked_key", "VersionId": "v1"}],
            "DeleteMarkers": [],
        }
        s3.delete_objects.return_value = {
            "Deleted": [],
            "Errors": [{
                "Key": "locked_key",
                "VersionId": "v1",
                "Code": "AccessDenied",
                "Message": "permission denied",
            }],
        }

        errs = _delete_s3_versioned(BUCKET, "code/x/", ACCOUNT, s3)
        self.assertEqual(len(errs), 1)
        self.assertIn("locked_key", errs[0])
        self.assertIn("AccessDenied", errs[0])

    def test_list_object_versions_passes_expected_bucket_owner(self):
        """list_object_versions must receive ExpectedBucketOwner so that cross-account
        operations are rejected at the API level (spec section 2c)."""
        from harness_image.cleanup import _delete_s3_versioned

        s3 = MagicMock()
        s3.list_object_versions.return_value = {
            "IsTruncated": False,
            "Versions": [{"Key": "some/key", "VersionId": "v1"}],
            "DeleteMarkers": [],
        }
        s3.delete_objects.return_value = {"Deleted": [], "Errors": []}

        _delete_s3_versioned(BUCKET, "code/x/", ACCOUNT, s3)

        list_kwargs = s3.list_object_versions.call_args[1]
        self.assertEqual(
            list_kwargs.get("ExpectedBucketOwner"),
            ACCOUNT,
            "list_object_versions must receive ExpectedBucketOwner=account",
        )

    def test_delete_objects_passes_expected_bucket_owner(self):
        """delete_objects must receive ExpectedBucketOwner so that cross-account
        operations are rejected at the API level (spec section 2c)."""
        from harness_image.cleanup import _delete_s3_versioned

        s3 = MagicMock()
        s3.list_object_versions.return_value = {
            "IsTruncated": False,
            "Versions": [{"Key": "some/key", "VersionId": "v1"}],
            "DeleteMarkers": [],
        }
        s3.delete_objects.return_value = {"Deleted": [], "Errors": []}

        _delete_s3_versioned(BUCKET, "code/x/", ACCOUNT, s3)

        delete_kwargs = s3.delete_objects.call_args[1]
        self.assertEqual(
            delete_kwargs.get("ExpectedBucketOwner"),
            ACCOUNT,
            "delete_objects must receive ExpectedBucketOwner=account",
        )


class DiscoverS3ProbeTests(unittest.TestCase):
    """_s3_prefix_has_objects must pass ExpectedBucketOwner to list_object_versions."""

    def test_s3_prefix_has_objects_passes_expected_bucket_owner(self):
        """_s3_prefix_has_objects must send ExpectedBucketOwner so the S3 API rejects
        cross-account probes before they can influence the cleanup plan (spec section 2c).

        Without ExpectedBucketOwner, the probe could return True for a bucket in another
        account, incorrectly scheduling that account's prefix for deletion (the actual
        delete is still protected, but the plan becomes inaccurate).
        """
        from harness_image.cleanup import _s3_prefix_has_objects

        s3 = MagicMock()
        s3.list_object_versions.return_value = {
            "IsTruncated": False,
            "Versions": [],
            "DeleteMarkers": [],
        }

        _s3_prefix_has_objects(s3, BUCKET, "code/testdeploy/", ACCOUNT)

        list_kwargs = s3.list_object_versions.call_args[1]
        self.assertEqual(
            list_kwargs.get("ExpectedBucketOwner"),
            ACCOUNT,
            "_s3_prefix_has_objects must pass ExpectedBucketOwner=account to "
            "list_object_versions",
        )


# ── partial failure + exit codes ──────────────────────────────────────────────

class PartialFailureTests(unittest.TestCase):
    def test_partial_failure_continues_and_exits_1(self):
        """If one runtime deletion fails, others still proceed; exit code is 1."""
        from harness_image.cleanup import run_cleanup, discover, cleanup_token
        from botocore.exceptions import ClientError
        import io

        cfg = _make_cfg()
        session, ctrl, iam, s3, logs, rts = _make_fake_session()

        # Make get_agent_runtime raise ResourceNotFoundException so that both
        # discover() (harmlessly, workload identities skipped) and
        # _wait_runtime_deleted() (returns immediately — runtime gone) behave
        # deterministically without real polling.
        ctrl.get_agent_runtime.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "GetAgentRuntime",
        )

        # Pre-compute the approve token with the same mock state (before any
        # deletion side-effect is installed) so it matches what run_cleanup will
        # compute internally.
        resources_pre = discover(cfg, session, ACCOUNT)
        real_token = cleanup_token(ACCOUNT, REGION, resources_pre)

        # Now install the delete failure; the counter starts at 0 again because
        # discover() never calls delete_agent_runtime.
        delete_count = [0]

        def _delete_side_effect(agentRuntimeId, **kw):
            delete_count[0] += 1
            if delete_count[0] == 1:
                raise ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "boom"}},
                    "DeleteAgentRuntime",
                )
            return {}

        ctrl.delete_agent_runtime.side_effect = _delete_side_effect

        import boto3
        with patch("boto3.Session", return_value=session):
            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = run_cleanup(cfg, approve_token=real_token)

        self.assertEqual(rc, 1)
        data = json.loads(out.getvalue())
        self.assertGreater(data.get("failed", 0), 0)

    def test_second_run_no_op(self):
        """After all resources are deleted, a second run exits 0 without prompting.

        Spec requirement: 'Re-running after success finds nothing and exits 0' and
        'A plan with nothing to delete exits 0 without prompting.'  The S3 prefix
        probe introduced in discover() marks an already-empty prefix as action='keep',
        so to_delete is empty and run_cleanup returns 0 before any consent gate.
        """
        from harness_image.cleanup import run_cleanup
        from botocore.exceptions import ClientError
        import io

        cfg = _make_cfg()
        # Simulate: no runtimes, no IAM role, S3 prefix is already empty
        ctrl = MagicMock()
        ctrl.list_agent_runtimes.return_value = {"agentRuntimes": []}
        ctrl.list_agent_runtime_endpoints.return_value = {"runtimeEndpoints": []}

        iam = MagicMock()
        iam.exceptions = types.SimpleNamespace(
            NoSuchEntityException=type("NoSuchEntityException", (ClientError,), {})
        )
        iam.get_role.side_effect = iam.exceptions.NoSuchEntityException(
            {"Error": {"Code": "NoSuchEntity", "Message": "gone"}}, "GetRole"
        )

        # Explicitly empty — simulates a prefix that was cleared by the first run.
        s3 = _make_fake_s3(versions=[])
        logs = _make_fake_logs()

        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": ACCOUNT}

        session = MagicMock()
        session.client.side_effect = lambda svc, **kw: {
            "sts": sts,
            "bedrock-agentcore-control": ctrl,
            "iam": iam,
            "s3": s3,
            "logs": logs,
        }[svc]

        # No approve_token needed: to_delete is empty, so run_cleanup exits 0 before
        # the consent gate (no TTY prompt, no non-TTY exit-3).
        import boto3
        with patch("boto3.Session", return_value=session):
            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = run_cleanup(cfg)

        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data.get("failed", 0), 0)


# ── keep-logs tests ───────────────────────────────────────────────────────────

class KeepLogsTests(unittest.TestCase):
    def test_keep_logs_marks_log_groups_keep(self):
        from harness_image.cleanup import discover

        logs = _make_fake_logs(["/aws/bedrock-agentcore/runtimes/testpfx_claude-XxYyZz-DEFAULT"])
        cfg = _make_cfg()
        session, ctrl, iam, s3, _, _ = _make_fake_session(logs=logs)
        resources = discover(cfg, session, ACCOUNT, keep_logs=True)
        lg_resources = [r for r in resources if r.type == "log_group"]
        self.assertTrue(lg_resources, "Expected log group resources")
        for r in lg_resources:
            self.assertEqual(r.action, "keep")

    def test_no_keep_logs_marks_log_groups_delete(self):
        from harness_image.cleanup import discover

        logs = _make_fake_logs(["/aws/bedrock-agentcore/runtimes/testpfx_claude-XxYyZz-DEFAULT"])
        cfg = _make_cfg()
        session, ctrl, iam, s3, _, _ = _make_fake_session(logs=logs)
        resources = discover(cfg, session, ACCOUNT, keep_logs=False)
        lg_resources = [r for r in resources if r.type == "log_group"]
        self.assertTrue(lg_resources, "Expected log group resources")
        for r in lg_resources:
            self.assertEqual(r.action, "delete")


# ── local files tests ─────────────────────────────────────────────────────────

class LocalFilesTests(unittest.TestCase):
    def test_agentcore_runtimes_json_removed(self):
        from harness_image.cleanup import discover

        with tempfile.TemporaryDirectory() as tmpdir:
            # Use .resolve() so the expected path matches cfg.data_root which
            # also resolves symlinks (e.g. /var -> /private/var on macOS).
            data_dir = Path(tmpdir).resolve()
            runtimes_file = data_dir / "agentcore-runtimes.json"
            runtimes_file.write_text("{}")
            cfg = _make_cfg(data_dir=str(data_dir))
            session, *_ = _make_fake_session()
            resources = discover(cfg, session, ACCOUNT)
            local_deletes = [r for r in resources
                             if r.type == "local_file" and r.action == "delete"]
            paths = [r.meta.get("path", "") for r in local_deletes]
            self.assertIn(str(runtimes_file), paths)

    def test_harness_versions_lock_json_removed(self):
        from harness_image.cleanup import discover

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir).resolve()
            lock_file = data_dir / "harness-versions.lock.json"
            lock_file.write_text("{}")
            cfg = _make_cfg(data_dir=str(data_dir))
            session, *_ = _make_fake_session()
            resources = discover(cfg, session, ACCOUNT)
            local_deletes = [r for r in resources
                             if r.type == "local_file" and r.action == "delete"]
            paths = [r.meta.get("path", "") for r in local_deletes]
            self.assertIn(str(lock_file), paths)

    def test_session_files_only_for_deleted_runtimes(self):
        """Session files whose arn matches a deleted runtime are removed; others kept."""
        from harness_image.cleanup import discover

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir).resolve()
            sessions_dir = data_dir / "agentcore-sessions"
            sessions_dir.mkdir()

            # Build the expected runtime ARN for testpfx_claude
            rt = _make_runtime("claude")
            arn = rt["agentRuntimeArn"]

            session_for_deleted = sessions_dir / "session1.json"
            session_for_deleted.write_text(json.dumps({"arn": arn, "session_id": "s1"}))

            other_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/other_runtime-XYZ"
            session_other = sessions_dir / "session2.json"
            session_other.write_text(json.dumps({"arn": other_arn, "session_id": "s2"}))

            cfg = _make_cfg(data_dir=str(data_dir))
            session, *_ = _make_fake_session()
            resources = discover(cfg, session, ACCOUNT)

            local_deletes = {r.meta.get("path") for r in resources
                             if r.type == "local_file" and r.action == "delete"}
            self.assertIn(str(session_for_deleted), local_deletes)
            # session2 belongs to an unrelated runtime — must NOT be deleted
            self.assertNotIn(str(session_other), local_deletes)


# ── ConflictException handling tests ─────────────────────────────────────────

class ConflictExceptionTests(unittest.TestCase):
    """ConflictException from delete_agent_runtime / delete_agent_runtime_endpoint
    must be treated as 'already deleting', not as a hard failure."""

    def _run_with_approve(self, setup_fn):
        """Run run_cleanup() with a pre-computed approve token after setup_fn patches ctrl."""
        from harness_image.cleanup import run_cleanup, discover, cleanup_token
        from botocore.exceptions import ClientError
        import io

        cfg = _make_cfg()
        session, ctrl, iam, s3, logs, rts = _make_fake_session()

        # discover() uses get_agent_runtime; make it raise ResourceNotFoundException
        # so the workload identity is absent from the plan (harmless) and
        # _wait_runtime_deleted() returns immediately once we get there.
        ctrl.get_agent_runtime.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "GetAgentRuntime",
        )

        resources_pre = discover(cfg, session, ACCOUNT)
        real_token = cleanup_token(ACCOUNT, REGION, resources_pre)

        # Let the caller install the side-effects for the delete calls.
        setup_fn(ctrl)

        import boto3
        with patch("boto3.Session", return_value=session):
            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = run_cleanup(cfg, approve_token=real_token)
        return rc, ctrl, json.loads(out.getvalue())

    def test_runtime_conflict_exception_waits_and_succeeds(self):
        """ConflictException on delete_agent_runtime must add the runtime to the wait
        list instead of failing. The wait loop then polls GetAgentRuntime until
        ResourceNotFoundException, completing the cleanup with exit 0."""
        def _setup(ctrl):
            from botocore.exceptions import ClientError
            # delete_agent_runtime raises ConflictException (runtime already deleting)
            ctrl.delete_agent_runtime.side_effect = ClientError(
                {"Error": {"Code": "ConflictException", "Message": "already deleting"}},
                "DeleteAgentRuntime",
            )
            # _wait_runtime_deleted polls until ResourceNotFoundException
            ctrl.get_agent_runtime.side_effect = ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
                "GetAgentRuntime",
            )

        rc, ctrl, data = self._run_with_approve(_setup)
        self.assertEqual(rc, 0, "ConflictException on delete_agent_runtime must not exit 1")
        self.assertEqual(data.get("failed", 0), 0,
                         "ConflictException on delete_agent_runtime must not be counted as a failure")

    def test_endpoint_conflict_exception_does_not_fail(self):
        """ConflictException on delete_agent_runtime_endpoint must be treated as
        'already being deleted'; the endpoint must not appear in the failed list."""
        def _setup(ctrl):
            from botocore.exceptions import ClientError
            ctrl.delete_agent_runtime_endpoint.side_effect = ClientError(
                {"Error": {"Code": "ConflictException", "Message": "already deleting"}},
                "DeleteAgentRuntimeEndpoint",
            )

        # We need endpoints in the plan, so build a session with an endpoint listed.
        from harness_image.cleanup import run_cleanup, discover, cleanup_token
        from botocore.exceptions import ClientError
        import io

        cfg = _make_cfg()
        ctrl = MagicMock()
        rts = [_make_runtime(h) for h in ("claude", "codex", "kiro")]
        ctrl.list_agent_runtimes.return_value = {"agentRuntimes": rts}

        # One custom endpoint per runtime (DEFAULT is never deleted explicitly)
        ctrl.list_agent_runtime_endpoints.return_value = {
            "runtimeEndpoints": [{"name": "DEFAULT"}, {"name": "staging"}]
        }
        ctrl.get_agent_runtime.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "GetAgentRuntime",
        )
        ctrl.delete_agent_runtime.return_value = {}
        ctrl.delete_workload_identity.return_value = {}

        iam = _make_fake_iam()
        s3 = _make_fake_s3()
        logs = _make_fake_logs()
        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": ACCOUNT}

        session = MagicMock()
        session.client.side_effect = lambda svc, **kw: {
            "sts": sts,
            "bedrock-agentcore-control": ctrl,
            "iam": iam,
            "s3": s3,
            "logs": logs,
        }[svc]

        resources_pre = discover(cfg, session, ACCOUNT)
        real_token = cleanup_token(ACCOUNT, REGION, resources_pre)

        # Now install the ConflictException for endpoints.
        ctrl.delete_agent_runtime_endpoint.side_effect = ClientError(
            {"Error": {"Code": "ConflictException", "Message": "already deleting"}},
            "DeleteAgentRuntimeEndpoint",
        )

        import boto3
        with patch("boto3.Session", return_value=session):
            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = run_cleanup(cfg, approve_token=real_token)

        data = json.loads(out.getvalue())
        self.assertNotIn(
            "ConflictException", str(data.get("errors", [])),
            "ConflictException on delete_agent_runtime_endpoint must not appear in errors",
        )


# ── deploy/agentcore-cleanup.sh tests ────────────────────────────────────────

class AgentcoreCleanupShTests(unittest.TestCase):
    SCRIPT = REPO_ROOT / "deploy" / "agentcore-cleanup.sh"

    def test_script_exists(self):
        self.assertTrue(self.SCRIPT.exists(), f"{self.SCRIPT} does not exist")

    def test_script_is_executable(self):
        mode = self.SCRIPT.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, f"{self.SCRIPT} is not user-executable")

    def test_script_delegates_to_cleanup_subcommand(self):
        """The script must forward a 'cleanup' token to agentcore-deploy.sh."""
        content = self.SCRIPT.read_text()
        self.assertIn("cleanup", content,
                      "agentcore-cleanup.sh must delegate to the 'cleanup' subcommand")
        self.assertIn("agentcore-deploy.sh", content,
                      "agentcore-cleanup.sh must call agentcore-deploy.sh")


if __name__ == "__main__":
    unittest.main()


# ── Endpoint, conflict, IAM-gone and artifact-URI edge cases ─────────────────

class EdgeCaseTests(unittest.TestCase):
    def _session_with(self, ctrl=None, iam=None, s3=None):
        """A fake session wiring the given clients (defaults from the shared fakes)."""
        session, d_ctrl, d_iam, d_s3, logs, _ = _make_fake_session()
        ctrl, iam, s3 = ctrl or d_ctrl, iam or d_iam, s3 or d_s3
        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": ACCOUNT}
        session.client.side_effect = lambda svc, **kw: {
            "sts": sts, "bedrock-agentcore-control": ctrl, "iam": iam,
            "s3": s3, "logs": logs}[svc]
        return session, ctrl, iam

    def _approved_run(self, cfg, session, ctrl, **kw):
        from harness_image.cleanup import run_cleanup, discover, cleanup_token
        from botocore.exceptions import ClientError
        import io
        # Runtimes report "not found" so discovery and the deletion wait both finish at once.
        ctrl.get_agent_runtime.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetAgentRuntime")
        token = cleanup_token(ACCOUNT, REGION, discover(cfg, session, ACCOUNT, **kw))
        out = io.StringIO()
        with patch("boto3.Session", return_value=session), patch("sys.stdout", out):
            rc = run_cleanup(cfg, approve_token=token, **kw)
        return rc, json.loads(out.getvalue())

    def test_default_endpoint_is_left_to_runtime_deletion(self):
        from harness_image.cleanup import discover
        session, ctrl, _ = self._session_with()
        ctrl.list_agent_runtime_endpoints.return_value = {
            "runtimeEndpoints": [{"name": "DEFAULT"}, {"name": "staging"}]}
        endpoints = [r for r in discover(_make_cfg(), session, ACCOUNT) if r.type == "endpoint"]
        self.assertTrue(endpoints)
        self.assertEqual({r.meta["endpoint_name"] for r in endpoints}, {"staging"})

    def test_runtime_conflict_while_updating_fails_without_waiting(self):
        from botocore.exceptions import ClientError
        session, ctrl, _ = self._session_with()
        cfg = _make_cfg()
        from harness_image.cleanup import discover, cleanup_token, run_cleanup
        import io
        ctrl.get_agent_runtime.side_effect = None
        ctrl.get_agent_runtime.return_value = {"status": "UPDATING"}
        token = cleanup_token(ACCOUNT, REGION, discover(cfg, session, ACCOUNT))
        ctrl.delete_agent_runtime.side_effect = ClientError(
            {"Error": {"Code": "ConflictException", "Message": "busy"}}, "DeleteAgentRuntime")
        out = io.StringIO()
        with patch("boto3.Session", return_value=session), patch("sys.stdout", out), \
                patch("harness_image.cleanup._wait_runtime_deleted") as wait:
            rc = run_cleanup(cfg, approve_token=token)
        data = json.loads(out.getvalue())
        self.assertEqual(rc, 1)
        wait.assert_not_called()
        self.assertTrue(any("UPDATING" in str(e) for e in data.get("errors", [])))

    def test_iam_entities_already_gone_are_not_failures(self):
        from botocore.exceptions import ClientError
        session, ctrl, iam = self._session_with()
        gone = ClientError({"Error": {"Code": "NoSuchEntity", "Message": "gone"}}, "DeleteRole")
        iam.delete_role_policy.side_effect = gone
        iam.delete_role.side_effect = gone
        rc, data = self._approved_run(_make_cfg(), session, ctrl)
        self.assertEqual(rc, 0)
        self.assertEqual(data["failed"], 0)

    def test_include_artifacts_without_uri_is_a_usage_error(self):
        from harness_image.cleanup import run_cleanup
        session, _, _ = self._session_with()
        with patch("boto3.Session", return_value=session), patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(run_cleanup(_make_cfg(), include_artifacts=True, artifact_s3_uri=None), 2)

    def test_include_artifacts_with_bucket_root_is_refused(self):
        from harness_image.cleanup import run_cleanup
        session, _, _ = self._session_with()
        with patch("boto3.Session", return_value=session), patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(run_cleanup(_make_cfg(), include_artifacts=True,
                                         artifact_s3_uri="s3://my-test-bucket/"), 2)
