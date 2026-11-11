# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the cleanup CLI wiring (ROLE: cli).

Covers:
  - harness_image.__main__ cleanup subcommand: argparse flags, --properties,
    --debug, docstring/epilog, exit codes passed through from run_cleanup
  - deploy/agentcore-cleanup.sh: executable, thin wrapper (no venv logic),
    delegates to agentcore-deploy.sh cleanup, forwards --properties and --debug
    BEFORE the cleanup subcommand, forwards all other cleanup flags AFTER it
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).parent.parent


# ── argparse / __main__ wiring ────────────────────────────────────────────────

class CleanupSubcommandParserTests(unittest.TestCase):
    """Parser-level tests: no AWS, no disk, no subprocess."""

    def _parser(self):
        from harness_image.__main__ import _build_parser
        return _build_parser()

    def test_cleanup_subcommand_registered(self):
        p = self._parser()
        args = p.parse_args(["cleanup"])
        self.assertEqual(args.command, "cleanup")

    def test_approve_token_flag(self):
        p = self._parser()
        args = p.parse_args(["cleanup", "--approve", "abc123"])
        self.assertEqual(args.approve_token, "abc123")

    def test_include_artifacts_flag(self):
        p = self._parser()
        args = p.parse_args(["cleanup", "--include-artifacts"])
        self.assertTrue(args.include_artifacts)

    def test_keep_logs_flag(self):
        p = self._parser()
        args = p.parse_args(["cleanup", "--keep-logs"])
        self.assertTrue(args.keep_logs)

    def test_cleanup_defaults(self):
        """All cleanup flags default to safe/off values."""
        p = self._parser()
        args = p.parse_args(["cleanup"])
        self.assertIsNone(args.approve_token)
        self.assertFalse(args.include_artifacts)
        self.assertFalse(args.keep_logs)

    def test_properties_flag_top_level(self):
        """--properties is a top-level flag; it appears before the subcommand."""
        p = self._parser()
        args = p.parse_args(["--properties", "custom.props", "cleanup"])
        self.assertEqual(args.properties, "custom.props")

    def test_debug_flag_top_level(self):
        """--debug is a top-level flag accepted before any subcommand."""
        p = self._parser()
        args = p.parse_args(["--debug", "cleanup"])
        self.assertTrue(args.debug)

    def test_debug_default_is_false(self):
        p = self._parser()
        args = p.parse_args(["cleanup"])
        self.assertFalse(args.debug)

    def test_all_cleanup_flags_together(self):
        p = self._parser()
        args = p.parse_args([
            "--properties", "my.props",
            "--debug",
            "cleanup",
            "--approve", "tok99",
            "--include-artifacts",
            "--keep-logs",
        ])
        self.assertEqual(args.properties, "my.props")
        self.assertTrue(args.debug)
        self.assertEqual(args.approve_token, "tok99")
        self.assertTrue(args.include_artifacts)
        self.assertTrue(args.keep_logs)

    def test_epilog_mentions_cleanup(self):
        p = self._parser()
        self.assertIn("cleanup", p.epilog)

    def test_epilog_mentions_approve(self):
        """Epilog explains the --approve consent flow for cleanup."""
        p = self._parser()
        self.assertIn("--approve", p.epilog)

    def test_docstring_mentions_cleanup_subcommand(self):
        import harness_image.__main__ as m
        self.assertIn("cleanup", m.__doc__)

    def test_docstring_mentions_debug_flag(self):
        """--debug must be listed in the Flags section of the module docstring."""
        import harness_image.__main__ as m
        self.assertIn("--debug", m.__doc__)

    def test_docstring_mentions_approve_flag(self):
        import harness_image.__main__ as m
        self.assertIn("--approve", m.__doc__)

    def test_docstring_mentions_include_artifacts_flag(self):
        import harness_image.__main__ as m
        self.assertIn("--include-artifacts", m.__doc__)

    def test_docstring_mentions_keep_logs_flag(self):
        import harness_image.__main__ as m
        self.assertIn("--keep-logs", m.__doc__)


# ── main() dispatch to run_cleanup ─────────────────────────────────────────────

class CleanupMainDispatchTests(unittest.TestCase):
    """main() dispatch to run_cleanup: no real AWS, no real disk."""

    def _run_main(self, argv, *, props=None, cleanup_rc=0):
        """Helper that patches load_props, DeployConfig, run_cleanup."""
        from harness_image.__main__ import main
        if props is None:
            props = {}
        mock_cfg = MagicMock()
        with (
            patch("harness_image.__main__.load_props", return_value=props),
            patch("harness_image.__main__.DeployConfig") as MockDeployConfig,
            patch("harness_image.__main__.run_cleanup", return_value=cleanup_rc) as mock_cleanup,
        ):
            MockDeployConfig.from_props.return_value = mock_cfg
            rc = main(argv)
        return rc, mock_cleanup, mock_cfg

    def test_exit_code_0_passed_through(self):
        rc, _, _ = self._run_main(["--properties", "x.props", "cleanup"], cleanup_rc=0)
        self.assertEqual(rc, 0)

    def test_exit_code_1_passed_through(self):
        rc, _, _ = self._run_main(["--properties", "x.props", "cleanup"], cleanup_rc=1)
        self.assertEqual(rc, 1)

    def test_exit_code_2_passed_through(self):
        rc, _, _ = self._run_main(["--properties", "x.props", "cleanup"], cleanup_rc=2)
        self.assertEqual(rc, 2)

    def test_exit_code_3_passed_through(self):
        rc, _, _ = self._run_main(["--properties", "x.props", "cleanup"], cleanup_rc=3)
        self.assertEqual(rc, 3)

    def test_approve_token_forwarded(self):
        rc, mock_cleanup, _ = self._run_main(
            ["--properties", "x.props", "cleanup", "--approve", "tok456"]
        )
        mock_cleanup.assert_called_once()
        kwargs = mock_cleanup.call_args.kwargs
        self.assertEqual(kwargs["approve_token"], "tok456")

    def test_approve_token_absent_is_none(self):
        rc, mock_cleanup, _ = self._run_main(
            ["--properties", "x.props", "cleanup"]
        )
        mock_cleanup.assert_called_once()
        kwargs = mock_cleanup.call_args.kwargs
        self.assertIsNone(kwargs["approve_token"])

    def test_include_artifacts_forwarded(self):
        rc, mock_cleanup, _ = self._run_main(
            ["--properties", "x.props", "cleanup", "--include-artifacts"]
        )
        mock_cleanup.assert_called_once()
        self.assertTrue(mock_cleanup.call_args.kwargs["include_artifacts"])

    def test_include_artifacts_defaults_false(self):
        rc, mock_cleanup, _ = self._run_main(["--properties", "x.props", "cleanup"])
        self.assertFalse(mock_cleanup.call_args.kwargs["include_artifacts"])

    def test_keep_logs_forwarded(self):
        rc, mock_cleanup, _ = self._run_main(
            ["--properties", "x.props", "cleanup", "--keep-logs"]
        )
        self.assertTrue(mock_cleanup.call_args.kwargs["keep_logs"])

    def test_keep_logs_defaults_false(self):
        rc, mock_cleanup, _ = self._run_main(["--properties", "x.props", "cleanup"])
        self.assertFalse(mock_cleanup.call_args.kwargs["keep_logs"])

    def test_artifact_s3_uri_from_props(self):
        rc, mock_cleanup, _ = self._run_main(
            ["--properties", "x.props", "cleanup"],
            props={"ARTIFACT_S3_URI": "s3://my-bucket/art/"},
        )
        mock_cleanup.assert_called_once()
        self.assertEqual(
            mock_cleanup.call_args.kwargs["artifact_s3_uri"], "s3://my-bucket/art/"
        )

    def test_artifact_s3_uri_absent_is_none(self):
        rc, mock_cleanup, _ = self._run_main(
            ["--properties", "x.props", "cleanup"], props={}
        )
        self.assertIsNone(mock_cleanup.call_args.kwargs["artifact_s3_uri"])

    def test_cfg_passed_as_first_positional(self):
        """run_cleanup must receive the DeployConfig as the first positional arg."""
        rc, mock_cleanup, mock_cfg = self._run_main(
            ["--properties", "x.props", "cleanup"]
        )
        mock_cleanup.assert_called_once()
        args = mock_cleanup.call_args.args
        self.assertEqual(len(args), 1, "run_cleanup should receive exactly one positional arg")
        self.assertIs(args[0], mock_cfg)

    def test_no_subcommand_returns_2(self):
        """Calling main with no subcommand should return exit code 2."""
        from harness_image.__main__ import main
        rc = main([])
        self.assertEqual(rc, 2)

    def test_props_load_error_returns_1(self):
        from harness_image.__main__ import main
        with patch("harness_image.__main__.load_props",
                   side_effect=OSError("file not found")):
            rc = main(["--properties", "/nosuch.props", "cleanup"])
        self.assertEqual(rc, 1)

    def test_deploy_config_error_returns_1(self):
        from harness_image.__main__ import main
        with (
            patch("harness_image.__main__.load_props", return_value={}),
            patch("harness_image.__main__.DeployConfig") as MockDeployConfig,
        ):
            MockDeployConfig.from_props.side_effect = ValueError("missing required key")
            rc = main(["--properties", "x.props", "cleanup"])
        self.assertEqual(rc, 1)


# ── deploy/agentcore-cleanup.sh static analysis ───────────────────────────────

class CleanupWrapperStaticTests(unittest.TestCase):
    """Static checks on deploy/agentcore-cleanup.sh — no subprocess needed."""

    SCRIPT = REPO_ROOT / "deploy" / "agentcore-cleanup.sh"

    def test_script_exists(self):
        self.assertTrue(self.SCRIPT.exists(),
                        "deploy/agentcore-cleanup.sh must exist")

    def test_script_is_executable(self):
        self.assertTrue(os.access(str(self.SCRIPT), os.X_OK),
                        "deploy/agentcore-cleanup.sh must be executable")

    def test_script_permissions_include_exec_bit(self):
        mode = stat.S_IMODE(self.SCRIPT.stat().st_mode)
        # Owner must have execute bit set.
        self.assertTrue(mode & stat.S_IXUSR,
                        "owner execute bit must be set on agentcore-cleanup.sh")

    def test_shebang_is_bash(self):
        content = self.SCRIPT.read_text()
        self.assertTrue(content.startswith("#!/bin/bash"),
                        "shebang must be #!/bin/bash")

    def test_set_euo_pipefail(self):
        content = self.SCRIPT.read_text()
        self.assertIn("set -euo pipefail", content)

    def test_delegates_to_agentcore_deploy(self):
        """The wrapper must call agentcore-deploy.sh, not manage its own venv."""
        content = self.SCRIPT.read_text()
        self.assertIn("agentcore-deploy.sh", content)

    def test_passes_cleanup_subcommand(self):
        """The word 'cleanup' must appear as the subcommand passed to deploy.sh."""
        content = self.SCRIPT.read_text()
        self.assertIn("cleanup", content)

    def test_no_venv_logic(self):
        """Venv management belongs in agentcore-deploy.sh only."""
        content = self.SCRIPT.read_text()
        self.assertNotIn(".deploy-venv", content,
                         "agentcore-cleanup.sh must not contain .deploy-venv logic")
        self.assertNotIn("uv pip install", content,
                         "agentcore-cleanup.sh must not call uv pip install")
        self.assertNotIn("uv venv", content,
                         "agentcore-cleanup.sh must not create venvs")

    def test_no_direct_python_invocation(self):
        """Python is invoked by agentcore-deploy.sh, not by the cleanup wrapper.

        Header comments may reference ``python -m harness_image`` for documentation
        purposes; only non-comment lines are checked for actual invocations.
        """
        content = self.SCRIPT.read_text()
        non_comment_lines = [
            line for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        non_comment_text = "\n".join(non_comment_lines)
        self.assertNotIn("python -m harness_image", non_comment_text,
                         "agentcore-cleanup.sh must not exec python -m harness_image directly")

    def test_handles_properties_flag(self):
        content = self.SCRIPT.read_text()
        self.assertIn("--properties", content)

    def test_handles_debug_flag(self):
        """--debug must be extracted and placed before the cleanup subcommand."""
        content = self.SCRIPT.read_text()
        self.assertIn("--debug", content)


# ── deploy/agentcore-cleanup.sh delegation tests ─────────────────────────────

class CleanupWrapperDelegationTests(unittest.TestCase):
    """Run agentcore-cleanup.sh with a stub deploy script to verify forwarding."""

    SCRIPT = REPO_ROOT / "deploy" / "agentcore-cleanup.sh"

    def _run_with_stub(self, cleanup_args: list[str], stub_exit: int = 0):
        """Run a copy of agentcore-cleanup.sh with a stub agentcore-deploy.sh.

        The stub records all its arguments to stdout and exits with stub_exit.
        We copy cleanup.sh into a temp dir so SCRIPT_DIR points to the temp dir,
        making it call the stub instead of the real deploy script.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Stub deploy script: records args, then exits with the given code.
            stub_deploy = tmpdir_path / "agentcore-deploy.sh"
            stub_deploy.write_text(
                "#!/bin/bash\n"
                'echo "DEPLOY_ARGS: $*"\n'
                f"exit {stub_exit}\n"
            )
            stub_deploy.chmod(0o755)

            # Copy the cleanup script into the same temp dir so SCRIPT_DIR
            # points there, causing the exec to call the stub.
            cleanup_copy = tmpdir_path / "agentcore-cleanup.sh"
            cleanup_copy.write_text(self.SCRIPT.read_text())
            cleanup_copy.chmod(0o755)

            result = subprocess.run(
                [str(cleanup_copy)] + cleanup_args,
                capture_output=True, text=True,
            )
            return result

    def test_no_args_invokes_cleanup_subcommand(self):
        """With no arguments, deploy.sh must be called with 'cleanup' as subcommand."""
        result = self._run_with_stub([])
        self.assertEqual(result.returncode, 0)
        self.assertIn("cleanup", result.stdout)

    def test_approve_forwarded_after_cleanup(self):
        """--approve TOKEN must appear after 'cleanup' in the forwarded args."""
        result = self._run_with_stub(["--approve", "tok123"])
        self.assertEqual(result.returncode, 0)
        stdout = result.stdout
        self.assertIn("cleanup", stdout)
        self.assertIn("--approve", stdout)
        self.assertIn("tok123", stdout)
        # cleanup must precede --approve
        self.assertLess(stdout.index("cleanup"), stdout.index("--approve"))

    def test_properties_forwarded_before_cleanup(self):
        """--properties PATH must appear BEFORE 'cleanup' in the forwarded args."""
        result = self._run_with_stub(["--properties", "my.properties"])
        self.assertEqual(result.returncode, 0)
        stdout = result.stdout
        self.assertIn("--properties", stdout)
        self.assertIn("my.properties", stdout)
        self.assertIn("cleanup", stdout)
        # --properties must precede cleanup
        self.assertLess(stdout.index("--properties"), stdout.index("cleanup"))

    def test_properties_equals_form_forwarded(self):
        """--properties=PATH is handled correctly."""
        result = self._run_with_stub(["--properties=my.properties"])
        self.assertEqual(result.returncode, 0)
        stdout = result.stdout
        self.assertIn("my.properties", stdout)
        self.assertIn("cleanup", stdout)

    def test_debug_forwarded_before_cleanup(self):
        """--debug is a top-level Python flag and must appear BEFORE 'cleanup'."""
        result = self._run_with_stub(["--debug"])
        self.assertEqual(result.returncode, 0)
        stdout = result.stdout
        self.assertIn("--debug", stdout)
        self.assertIn("cleanup", stdout)
        # --debug must precede cleanup
        self.assertLess(stdout.index("--debug"), stdout.index("cleanup"))

    def test_include_artifacts_forwarded_after_cleanup(self):
        result = self._run_with_stub(["--include-artifacts"])
        self.assertEqual(result.returncode, 0)
        stdout = result.stdout
        self.assertIn("--include-artifacts", stdout)
        self.assertIn("cleanup", stdout)
        self.assertLess(stdout.index("cleanup"), stdout.index("--include-artifacts"))

    def test_keep_logs_forwarded_after_cleanup(self):
        result = self._run_with_stub(["--keep-logs"])
        self.assertEqual(result.returncode, 0)
        stdout = result.stdout
        self.assertIn("--keep-logs", stdout)
        self.assertIn("cleanup", stdout)
        self.assertLess(stdout.index("cleanup"), stdout.index("--keep-logs"))

    def test_exit_code_0_passed_through(self):
        result = self._run_with_stub([], stub_exit=0)
        self.assertEqual(result.returncode, 0)

    def test_exit_code_1_passed_through(self):
        result = self._run_with_stub([], stub_exit=1)
        self.assertEqual(result.returncode, 1)

    def test_exit_code_2_passed_through(self):
        result = self._run_with_stub([], stub_exit=2)
        self.assertEqual(result.returncode, 2)

    def test_exit_code_3_passed_through(self):
        result = self._run_with_stub([], stub_exit=3)
        self.assertEqual(result.returncode, 3)

    def test_all_flags_forwarded_in_correct_order(self):
        """All supported flags reach the deploy script with correct ordering."""
        result = self._run_with_stub([
            "--properties", "p.properties",
            "--debug",
            "--approve", "tok999",
            "--include-artifacts",
            "--keep-logs",
        ])
        self.assertEqual(result.returncode, 0)
        stdout = result.stdout
        for token in [
            "--properties", "p.properties", "cleanup",
            "--approve", "tok999", "--include-artifacts", "--keep-logs", "--debug",
        ]:
            self.assertIn(token, stdout, f"missing from forwarded args: {token!r}")

        cleanup_pos = stdout.index("cleanup")
        # Pre-subcommand flags (--properties, --debug) must precede cleanup.
        self.assertLess(stdout.index("--properties"), cleanup_pos)
        self.assertLess(stdout.index("--debug"), cleanup_pos)
        # Post-subcommand flags must follow cleanup.
        self.assertGreater(stdout.index("--approve"), cleanup_pos)
        self.assertGreater(stdout.index("--include-artifacts"), cleanup_pos)
        self.assertGreater(stdout.index("--keep-logs"), cleanup_pos)


# ── deploy/agentcore-deploy.sh documents cleanup ──────────────────────────────

class DeployScriptDocumentsCleanupTests(unittest.TestCase):
    """Verify that agentcore-deploy.sh's header documents the cleanup command."""

    DEPLOY_SCRIPT = REPO_ROOT / "deploy" / "agentcore-deploy.sh"

    def test_header_lists_cleanup_command(self):
        content = self.DEPLOY_SCRIPT.read_text()
        self.assertIn("cleanup", content)
