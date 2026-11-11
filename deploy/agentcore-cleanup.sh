#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# agentcore-cleanup.sh — remove all AWS resources created by agentcore-deploy.
#
# This is a thin wrapper: venv management lives entirely in agentcore-deploy.sh.
# All arguments are forwarded directly to the `cleanup` subcommand of
# `python -m harness_image`.
#
# Usage:
#   deploy/agentcore-cleanup.sh [--properties PATH] [OPTIONS]
#
# Options:
#   --properties PATH    deploy.properties file (default: deploy.properties beside this script)
#   --approve TOKEN      Consent token from a previous cleanup plan run; without this the
#                        script prints the plan (JSON) and exits 3 if not run in a TTY,
#                        or prompts for the account ID if run interactively.
#   --include-artifacts  Also delete the ARTIFACT_S3_URI prefix (comparison archives).
#   --keep-logs          Keep CloudWatch log groups.
#   --debug              Verbose logging.
#
# Exit codes (from python -m harness_image cleanup):
#   0   Clean (nothing to delete, or all deletions succeeded)
#   1   Error or user aborted
#   2   Usage error (empty S3 prefix or bad argument)
#   3   Approval required (plan token printed in JSON)
#
# What is deleted:
#   - Custom AgentCore runtime endpoints, then the runtimes (claude/codex/kiro);
#     AgentCore removes each runtime's DEFAULT endpoint with it
#   - Workload identities auto-created per runtime
#   - CloudWatch log groups under /aws/bedrock-agentcore/runtimes/<id>
#   - Versioned S3 objects under the AGENTCORE_TRANSFER_S3_URI prefix
#   - IAM inline policy and execution role (unless AGENTCORE_EXECUTION_ROLE_ARN is set)
#   - Local state files (agentcore-runtimes.json, harness-versions.lock.json,
#     agentcore-sessions/*.json for sessions belonging to deleted runtimes)
#
# What is NOT deleted:
#   - The S3 bucket itself (only objects under the configured prefix)
#   - Comparison archives (ARTIFACT_S3_URI) unless --include-artifacts is given
#   - CloudWatch log groups when --keep-logs is given (deleted by default otherwise)
#   - The rest of DATA_DIR (arena.sqlite3, comparisons/, logs, etc.)
#
# Examples:
#   # Print a cleanup plan (non-TTY: prints JSON and exits 3)
#   deploy/agentcore-cleanup.sh
#
#   # Interactive cleanup (TTY: shows plan and prompts for account ID)
#   deploy/agentcore-cleanup.sh
#
#   # Approve a plan obtained from a non-TTY run
#   deploy/agentcore-cleanup.sh --approve <token>
#
#   # Cleanup including comparison archives
#   deploy/agentcore-cleanup.sh --include-artifacts --approve <token>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Delegate to agentcore-deploy.sh with the 'cleanup' subcommand.
# agentcore-deploy.sh manages the venv and forwards all arguments to
# python -m harness_image.  We separate:
#   PRE_SUBCMD_ARGS  — top-level flags (--properties, --debug) that must appear
#                      BEFORE the subcommand name in the Python CLI.
#   CLEANUP_ARGS     — cleanup-specific flags (--approve, --include-artifacts,
#                      --keep-logs) that appear AFTER the subcommand name.
# The resulting call is: agentcore-deploy.sh [PRE_SUBCMD_ARGS] cleanup [CLEANUP_ARGS]

# Flags that must appear BEFORE the subcommand in `python -m harness_image`
# (i.e., top-level argparse flags: --properties, --debug).
# All other flags (--approve, --include-artifacts, --keep-logs) are cleanup-
# specific and go after the subcommand.
PRE_SUBCMD_ARGS=()
CLEANUP_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --properties)
            PRE_SUBCMD_ARGS+=("--properties" "$2")
            shift 2
            ;;
        --properties=*)
            PRE_SUBCMD_ARGS+=("$1")
            shift
            ;;
        --debug)
            PRE_SUBCMD_ARGS+=("--debug")
            shift
            ;;
        *)
            CLEANUP_ARGS+=("$1")
            shift
            ;;
    esac
done

exec "${SCRIPT_DIR}/agentcore-deploy.sh" \
    "${PRE_SUBCMD_ARGS[@]+"${PRE_SUBCMD_ARGS[@]}"}" \
    cleanup \
    "${CLEANUP_ARGS[@]+"${CLEANUP_ARGS[@]}"}"
