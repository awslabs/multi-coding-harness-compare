# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""docs/iam/ golden-file tests.

Each docs/iam/{kind}-policy.json file is produced by
  python -m harness_image policy --kind <kind>
This test regenerates each document from harness_image.iam.print_policy() and
asserts the stored file matches exactly.  A drift here means the policy code was
changed without updating the stored reference documents.

To regenerate the reference documents, run:
  cd <repo-root>
  python -m harness_image policy --kind execution  > docs/iam/execution-policy.json
  python -m harness_image policy --kind deployer   > docs/iam/deployer-policy.json
  python -m harness_image policy --kind workstation > docs/iam/workstation-policy.json
"""
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
DOCS_IAM = ROOT / "docs" / "iam"


class PolicyGoldenFileTests(unittest.TestCase):
    """Regenerate each IAM policy document and compare to the stored reference."""

    def _generate(self, kind: str) -> dict:
        """Call print_policy(kind) and return the parsed JSON."""
        from harness_image.iam import print_policy

        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(a[0] + "\n")):
            print_policy(kind)
        return json.loads(buf.getvalue())

    def _stored(self, kind: str) -> dict:
        path = DOCS_IAM / f"{kind}-policy.json"
        self.assertTrue(path.exists(), f"Missing stored policy: {path}")
        return json.loads(path.read_text())

    def test_execution_policy_matches_stored(self):
        generated = self._generate("execution")
        stored = self._stored("execution")
        self.assertEqual(
            generated, stored,
            "docs/iam/execution-policy.json is out of date. "
            "Run: python -m harness_image policy --kind execution > docs/iam/execution-policy.json",
        )

    def test_deployer_policy_matches_stored(self):
        generated = self._generate("deployer")
        stored = self._stored("deployer")
        self.assertEqual(
            generated, stored,
            "docs/iam/deployer-policy.json is out of date. "
            "Run: python -m harness_image policy --kind deployer > docs/iam/deployer-policy.json",
        )

    def test_workstation_policy_matches_stored(self):
        generated = self._generate("workstation")
        stored = self._stored("workstation")
        self.assertEqual(
            generated, stored,
            "docs/iam/workstation-policy.json is out of date. "
            "Run: python -m harness_image policy --kind workstation > docs/iam/workstation-policy.json",
        )

    def test_execution_policy_has_no_wrong_iam_prefix(self):
        """execution_policy must not use 'bedrock-agentcore-control:'."""
        stored = self._stored("execution")
        text = json.dumps(stored)
        self.assertNotIn(
            "bedrock-agentcore-control",
            text,
            "execution-policy.json must not reference 'bedrock-agentcore-control' prefix",
        )

    def test_deployer_policy_has_no_wrong_iam_prefix(self):
        """deployer_policy must use 'bedrock-agentcore:' not 'bedrock-agentcore-control:'."""
        stored = self._stored("deployer")
        text = json.dumps(stored)
        self.assertNotIn(
            "bedrock-agentcore-control",
            text,
            "deployer-policy.json must not reference 'bedrock-agentcore-control' prefix",
        )
        self.assertIn(
            "bedrock-agentcore:CreateAgentRuntime",
            text,
            "deployer-policy.json must use 'bedrock-agentcore:CreateAgentRuntime'",
        )

    def test_deployer_policy_has_pass_role_scoped(self):
        """deployer_policy must include iam:PassRole with iam:PassedToService condition."""
        stored = self._stored("deployer")
        statements = stored["Statement"]
        pass_role_stmts = [
            s for s in statements
            if s.get("Action") == "iam:PassRole"
        ]
        self.assertTrue(pass_role_stmts, "No iam:PassRole statement found in deployer policy")
        for stmt in pass_role_stmts:
            cond = stmt.get("Condition", {})
            passed_to = cond.get("StringEquals", {}).get("iam:PassedToService")
            self.assertEqual(
                passed_to,
                "bedrock-agentcore.amazonaws.com",
                "iam:PassRole must be conditioned on iam:PassedToService = bedrock-agentcore.amazonaws.com",
            )

    def test_execution_policy_has_versioning_permissions(self):
        """execution_policy must include s3:GetObjectVersion and s3:ListBucketVersions."""
        stored = self._stored("execution")
        text = json.dumps(stored)
        self.assertIn("s3:GetObjectVersion", text)
        self.assertIn("s3:ListBucketVersions", text)
        self.assertIn("s3:DeleteObjectVersion", text)

    def test_workstation_policy_has_required_s3_actions(self):
        """workstation_policy must include GetObjectVersion, AbortMultipartUpload, ListBucketVersions."""
        stored = self._stored("workstation")
        text = json.dumps(stored)
        self.assertIn("s3:GetObjectVersion", text)
        self.assertIn("s3:AbortMultipartUpload", text)
        self.assertIn("s3:ListBucketVersions", text)

    def test_workstation_policy_has_bedrock_and_sts(self):
        """workstation_policy must include Bedrock invoke/list and STS identity."""
        stored = self._stored("workstation")
        text = json.dumps(stored)
        self.assertIn("bedrock:InvokeModel", text)
        self.assertIn("bedrock:ListInferenceProfiles", text)
        self.assertIn("sts:GetCallerIdentity", text)

    def test_docs_iam_directory_exists(self):
        self.assertTrue(DOCS_IAM.is_dir(), f"docs/iam/ directory must exist at {DOCS_IAM}")

    def test_policy_files_are_valid_json(self):
        for kind in ("execution", "deployer", "workstation"):
            with self.subTest(kind=kind):
                path = DOCS_IAM / f"{kind}-policy.json"
                self.assertTrue(path.exists(), f"Missing {path.name}")
                data = json.loads(path.read_text())
                self.assertIn("Version", data)
                self.assertIn("Statement", data)
                self.assertIsInstance(data["Statement"], list)


class LeastPrivilegeDocsTests(unittest.TestCase):
    """Stored docs/iam JSON files must satisfy least-privilege constraints."""

    _DESTRUCTIVE_PREFIXES = (
        "Delete", "Update", "Create", "PutRolePolicy", "AttachRolePolicy",
    )

    # CreateAgentRuntime must use Resource "*" because the runtime ARN is
    # service-generated at creation time and cannot be pre-computed.  It is
    # isolated in its own statement (Sid "NoResourceScopingSupported") so this exception
    # is explicit and auditable.
    _WILDCARD_RESOURCE_EXCEPTIONS = frozenset(["bedrock-agentcore:CreateAgentRuntime"])

    def _is_destructive(self, action: str) -> bool:
        if action in self._WILDCARD_RESOURCE_EXCEPTIONS:
            return False
        bare = action.split(":")[-1] if ":" in action else action
        for p in self._DESTRUCTIVE_PREFIXES:
            if bare.startswith(p):
                return True
        return False

    def _all_stored(self):
        return {
            kind: json.loads((DOCS_IAM / f"{kind}-policy.json").read_text())
            for kind in ("execution", "deployer", "workstation")
        }

    def test_no_destructive_action_with_wildcard_resource(self):
        """No statement with Delete*/Update*/Create*/PutRolePolicy/AttachRolePolicy may use Resource '*'."""
        for kind, policy in self._all_stored().items():
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
                            self._is_destructive(action),
                            f"{kind}-policy.json: destructive action {action!r} "
                            f"must not have Resource '*' "
                            f"(Sid={stmt.get('Sid')!r})",
                        )

    def test_every_statement_has_alphanumeric_sid(self):
        """Every statement in every stored docs/iam policy must have an alphanumeric Sid."""
        for kind, policy in self._all_stored().items():
            for i, stmt in enumerate(policy["Statement"]):
                self.assertIn(
                    "Sid", stmt,
                    f"{kind}-policy.json statement[{i}] "
                    f"(actions={stmt.get('Action')!r}) is missing a Sid",
                )
                sid = stmt["Sid"]
                self.assertTrue(
                    sid.isalnum(),
                    f"{kind}-policy.json statement[{i}] Sid {sid!r} must be alphanumeric",
                )

    def test_list_agent_runtimes_has_listonly_sid_and_wildcard(self):
        """ListAgentRuntimes must be in its own statement with Sid 'ListOnly' and Resource '*'."""
        for kind, policy in self._all_stored().items():
            list_stmts = [
                s for s in policy["Statement"]
                if s.get("Action") == "bedrock-agentcore:ListAgentRuntimes"
                or (isinstance(s.get("Action"), list)
                    and "bedrock-agentcore:ListAgentRuntimes" in s["Action"])
            ]
            if not list_stmts:
                continue  # policy doesn't include this action
            for stmt in list_stmts:
                self.assertEqual(
                    stmt.get("Sid"), "ListOnly",
                    f"{kind}-policy.json: ListAgentRuntimes statement must have Sid 'ListOnly'",
                )
                self.assertEqual(
                    stmt.get("Resource"), "*",
                    f"{kind}-policy.json: ListAgentRuntimes must have Resource '*'",
                )

    def test_bedrock_invoke_scoped_in_execution_policy(self):
        """execution-policy.json: bedrock:InvokeModel must use scoped ARNs, not '*'."""
        policy = json.loads((DOCS_IAM / "execution-policy.json").read_text())
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock:InvokeModel" in actions:
                res = stmt["Resource"]
                self.assertNotEqual(res, "*",
                                    "execution-policy.json: bedrock:InvokeModel must not use '*'")
                combined = res if isinstance(res, str) else " ".join(res)
                self.assertIn("foundation-model", combined)
                self.assertIn("inference-profile", combined)

    def test_bedrock_invoke_scoped_in_workstation_policy(self):
        """workstation-policy.json: bedrock:InvokeModel must use scoped ARNs, not '*'."""
        policy = json.loads((DOCS_IAM / "workstation-policy.json").read_text())
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock:InvokeModel" in actions:
                res = stmt["Resource"]
                self.assertNotEqual(res, "*",
                                    "workstation-policy.json: bedrock:InvokeModel must not use '*'")
                combined = res if isinstance(res, str) else " ".join(res)
                self.assertIn("foundation-model", combined)
                self.assertIn("inference-profile", combined)

    def test_agentcore_get_scoped_in_workstation(self):
        """workstation-policy.json: GetAgentRuntime must be scoped to a runtime prefix ARN."""
        policy = json.loads((DOCS_IAM / "workstation-policy.json").read_text())
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock-agentcore:GetAgentRuntime" in actions:
                res = stmt["Resource"]
                self.assertNotEqual(res, "*",
                                    "workstation-policy.json: GetAgentRuntime must be scoped")
                self.assertIn("runtime/", res)

    def test_agentcore_manage_scoped_in_deployer(self):
        """deployer-policy.json: UpdateAgentRuntime/DeleteAgentRuntime must be scoped."""
        policy = json.loads((DOCS_IAM / "deployer-policy.json").read_text())
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock-agentcore:UpdateAgentRuntime" in actions or \
               "bedrock-agentcore:DeleteAgentRuntime" in actions:
                res = stmt["Resource"]
                self.assertNotEqual(res, "*",
                                    "deployer-policy.json: Update/Delete AgentRuntime must be scoped")
                self.assertIn("runtime/", res)

    def test_deployer_policy_has_cleanup_iam_actions(self):
        """deployer-policy.json must include IAM cleanup actions."""
        policy = json.loads((DOCS_IAM / "deployer-policy.json").read_text())
        text = json.dumps(policy)
        for action in ("iam:DeleteRole", "iam:DeleteRolePolicy",
                       "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"):
            self.assertIn(action, text,
                          f"deployer-policy.json must include {action}")

    def test_deployer_policy_has_cleanup_agentcore_actions(self):
        """deployer-policy.json must include endpoint and workload identity cleanup actions.

        ListWorkloadIdentities and GetWorkloadIdentity are intentionally absent:
        no code path calls them, and including them violates least-privilege.
        """
        policy = json.loads((DOCS_IAM / "deployer-policy.json").read_text())
        text = json.dumps(policy)
        for action in ("bedrock-agentcore:ListAgentRuntimeEndpoints",
                       "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
                       "bedrock-agentcore:DeleteWorkloadIdentity"):
            self.assertIn(action, text,
                          f"deployer-policy.json must include {action}")
        # These actions are never called; their absence enforces least-privilege.
        for excess in ("bedrock-agentcore:ListWorkloadIdentities",
                       "bedrock-agentcore:GetWorkloadIdentity"):
            self.assertNotIn(excess, text,
                             f"deployer-policy.json must not contain {excess!r} "
                             "(not called by deploy or cleanup)")

    def test_deployer_policy_has_cleanup_s3_actions(self):
        """deployer-policy.json must include s3:ListBucketVersions and s3:DeleteObjectVersion."""
        policy = json.loads((DOCS_IAM / "deployer-policy.json").read_text())
        text = json.dumps(policy)
        self.assertIn("s3:ListBucketVersions", text)
        self.assertIn("s3:DeleteObjectVersion", text)

    def test_deployer_policy_list_bucket_versions_uses_prefix_condition(self):
        """deployer-policy.json: s3:ListBucketVersions must use s3:prefix condition.

        The brief specifies 'bucket ARN + s3:prefix condition' so the permission
        is scoped to the deployment's transfer prefix.
        """
        policy = json.loads((DOCS_IAM / "deployer-policy.json").read_text())
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "s3:ListBucketVersions" in actions:
                cond = stmt.get("Condition", {})
                prefix_cond = (cond.get("StringLike", {}) or cond.get("StringEquals", {}))
                self.assertIn(
                    "s3:prefix", prefix_cond,
                    "deployer-policy.json: s3:ListBucketVersions must have s3:prefix condition",
                )
                return
        self.fail("No s3:ListBucketVersions statement found in deployer-policy.json")

    def test_deployer_policy_has_iam_get_role(self):
        """deployer-policy.json must include iam:GetRole for cleanup ownership check."""
        policy = json.loads((DOCS_IAM / "deployer-policy.json").read_text())
        text = json.dumps(policy)
        self.assertIn("iam:GetRole", text,
                      "deployer-policy.json must include iam:GetRole "
                      "(cleanup verifies role ownership via tags and trust policy)")

    def test_deployer_policy_has_no_excess_iam_actions(self):
        """deployer-policy.json must not contain IAM actions unused by deploy or cleanup.

        GetRolePolicy, AttachRolePolicy, DetachRolePolicy, and TagRole are not
        called by agentcore-deploy or agentcore-cleanup; their absence enforces
        least-privilege.
        """
        policy = json.loads((DOCS_IAM / "deployer-policy.json").read_text())
        text = json.dumps(policy)
        for excess in ("iam:GetRolePolicy", "iam:AttachRolePolicy",
                       "iam:DetachRolePolicy", "iam:TagRole"):
            self.assertNotIn(excess, text,
                             f"deployer-policy.json must not contain {excess!r} "
                             "(not used by deploy or cleanup)")

    def test_deployer_policy_has_log_group_cleanup_actions(self):
        """deployer-policy.json must include logs:DescribeLogGroups and logs:DeleteLogGroup."""
        policy = json.loads((DOCS_IAM / "deployer-policy.json").read_text())
        text = json.dumps(policy)
        self.assertIn("logs:DescribeLogGroups", text)
        self.assertIn("logs:DeleteLogGroup", text)

    def test_deployer_policy_delete_log_group_scoped(self):
        """deployer-policy.json: logs:DeleteLogGroup must not use Resource '*'."""
        policy = json.loads((DOCS_IAM / "deployer-policy.json").read_text())
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "logs:DeleteLogGroup" in actions:
                self.assertNotEqual(
                    stmt["Resource"], "*",
                    "deployer-policy.json: logs:DeleteLogGroup must not use Resource '*'"
                )
                self.assertIn("bedrock-agentcore", stmt["Resource"])

    def test_deployer_policy_workload_identity_delete_scoped(self):
        """deployer-policy.json: DeleteWorkloadIdentity must be scoped."""
        policy = json.loads((DOCS_IAM / "deployer-policy.json").read_text())
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock-agentcore:DeleteWorkloadIdentity" in actions:
                self.assertNotEqual(
                    stmt["Resource"], "*",
                    "deployer-policy.json: DeleteWorkloadIdentity must not use Resource '*'"
                )
                self.assertIn("workload-identity", stmt["Resource"])


class ReadmePrerequisitesTests(unittest.TestCase):
    """README Prerequisites table must reference all three IAM policy files and
    document Node.js/npm as a deploy-time prerequisite."""

    _readme_text = None

    @classmethod
    def _readme(cls) -> str:
        if cls._readme_text is None:
            cls._readme_text = (ROOT / "README.md").read_text()
        return cls._readme_text

    def _prereqs_section(self) -> str:
        """Return the Prerequisites table block (lines between the header and blank line after)."""
        readme = self._readme()
        start = readme.find("| Prerequisite |")
        if start == -1:
            self.fail("Could not find Prerequisites table in README.md")
        # Grab a generous window covering the whole table
        return readme[start: start + 3000]

    def test_prereqs_table_has_workstation_policy_link(self):
        section = self._prereqs_section()
        self.assertIn(
            "workstation-policy.json",
            section,
            "Prerequisites table must link docs/iam/workstation-policy.json",
        )

    def test_prereqs_table_has_execution_policy_link(self):
        section = self._prereqs_section()
        self.assertIn(
            "execution-policy.json",
            section,
            "Prerequisites table must link docs/iam/execution-policy.json",
        )

    def test_prereqs_table_has_deployer_policy_link(self):
        section = self._prereqs_section()
        self.assertIn(
            "deployer-policy.json",
            section,
            "Prerequisites table must link docs/iam/deployer-policy.json",
        )

    def test_prereqs_nodejs_row_mentions_deploy(self):
        """Node.js row must mention '--deploy' so AI assistants know npm is needed to deploy."""
        section = self._prereqs_section()
        # Find the Node.js row
        for line in section.splitlines():
            if "Node.js" in line or "node" in line.lower():
                if "--deploy" in line or "deploy" in line.lower():
                    return  # found a row mentioning both Node and deploy
        self.fail(
            "Prerequisites table must have a Node.js row that mentions '--deploy'. "
            "npm is required at deploy time to vendor Codex."
        )

    def test_prereqs_nodejs_row_mentions_npm(self):
        """Node.js row must mention npm so it is clear which tool is needed."""
        section = self._prereqs_section()
        for line in section.splitlines():
            if "Node.js" in line or "node" in line.lower():
                if "npm" in line:
                    return
        self.fail(
            "Prerequisites table must have a Node.js row that mentions 'npm'."
        )


class ReadmeOverrideKeysGlobTests(unittest.TestCase):
    """README Overrides table must document glob/tilde support for *_SKILLS_DIRS,
    *_STEERING_FILES, and *_MCP_CONFIG (spec requirement).

    The table rows are identified by being Markdown pipe-table rows whose first
    cell contains the backtick-quoted key name (e.g. ``| `*_SKILLS_DIRS` |``).
    This avoids false-matching incidental mentions of the key names elsewhere in
    the document.
    """

    _readme_text = None

    @classmethod
    def _readme(cls) -> str:
        if cls._readme_text is None:
            cls._readme_text = (ROOT / "README.md").read_text()
        return cls._readme_text

    @classmethod
    def _table_row(cls, key: str) -> str:
        """Return the pipe-table row whose first cell is ``| `key` |``.

        Raises AssertionError (via unittest.TestCase.fail) if not found.
        """
        marker = f"`{key}`"
        for line in cls._readme().splitlines():
            # Must look like a Markdown table row: starts/ends with '|'
            stripped = line.strip()
            if stripped.startswith("|") and marker in stripped:
                # Confirm the key is in the first cell (before the second '|')
                cells = stripped.split("|")
                # cells[0] == '' (before leading |), cells[1] == first cell
                if len(cells) >= 2 and marker in cells[1]:
                    return stripped
        return ""  # caller asserts non-empty

    def test_skills_dirs_row_exists(self):
        """The *_SKILLS_DIRS table row must exist in README.md."""
        row = self._table_row("*_SKILLS_DIRS")
        self.assertTrue(row, "| `*_SKILLS_DIRS` | ... | table row not found in README.md")

    def test_skills_dirs_mentions_glob(self):
        """*_SKILLS_DIRS row must say 'glob patterns' (not just 'directories')."""
        row = self._table_row("*_SKILLS_DIRS")
        self.assertIn(
            "glob",
            row,
            "*_SKILLS_DIRS table row in README.md must mention 'glob patterns'",
        )

    def test_skills_dirs_mentions_tilde(self):
        """*_SKILLS_DIRS row must note that ~ is expanded."""
        row = self._table_row("*_SKILLS_DIRS")
        self.assertIn(
            "~",
            row,
            "*_SKILLS_DIRS table row must mention '~' (tilde expansion)",
        )

    def test_steering_files_row_exists(self):
        """The *_STEERING_FILES table row must exist in README.md."""
        row = self._table_row("*_STEERING_FILES")
        self.assertTrue(row, "| `*_STEERING_FILES` | ... | table row not found in README.md")

    def test_steering_files_mentions_glob(self):
        """*_STEERING_FILES row must say 'glob patterns'."""
        row = self._table_row("*_STEERING_FILES")
        self.assertIn(
            "glob",
            row,
            "*_STEERING_FILES table row must mention 'glob patterns'",
        )

    def test_steering_files_mentions_tilde(self):
        """*_STEERING_FILES row must note that ~ is expanded."""
        row = self._table_row("*_STEERING_FILES")
        self.assertIn(
            "~",
            row,
            "*_STEERING_FILES table row must mention '~' (tilde expansion)",
        )

    def test_steering_files_directories_expand_to_md(self):
        """*_STEERING_FILES row must say directories expand to *.md files inside."""
        row = self._table_row("*_STEERING_FILES")
        self.assertIn(
            "*.md",
            row,
            "*_STEERING_FILES table row must say directories expand to *.md files inside",
        )

    def test_mcp_config_row_exists(self):
        """The *_MCP_CONFIG table row must exist in README.md."""
        row = self._table_row("*_MCP_CONFIG")
        self.assertTrue(row, "| `*_MCP_CONFIG` | ... | table row not found in README.md")

    def test_mcp_config_mentions_glob(self):
        """*_MCP_CONFIG row must say 'glob'."""
        row = self._table_row("*_MCP_CONFIG")
        self.assertIn(
            "glob",
            row,
            "*_MCP_CONFIG table row must mention 'glob'",
        )

    def test_mcp_config_mentions_tilde(self):
        """*_MCP_CONFIG row must note that ~ is expanded."""
        row = self._table_row("*_MCP_CONFIG")
        self.assertIn(
            "~",
            row,
            "*_MCP_CONFIG table row must mention '~' (tilde expansion)",
        )

    def test_mcp_config_mentions_first_match(self):
        """*_MCP_CONFIG row must say 'first match used' so callers know glob semantics."""
        row = self._table_row("*_MCP_CONFIG")
        self.assertIn(
            "first match",
            row,
            "*_MCP_CONFIG table row must say 'first match used'",
        )


if __name__ == "__main__":
    unittest.main()
