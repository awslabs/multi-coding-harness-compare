#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# agentcore-deploy.sh — standalone AgentCore harness deployment script.
#
# Does not require the multi-coding-harness-compare API/worker to be installed
# or running.  Manages its own minimal Python venv (.deploy-venv/) using uv.
#
# Usage:
#   deploy/agentcore-deploy.sh [--properties PATH] COMMAND [OPTIONS]
#
# Commands:
#   plan              Resolve harness versions and print JSON; write nothing remote.
#   deploy            Build bundles and create/update AgentCore runtimes.
#   status            Read-only: print deployed runtime names and status.
#   policy            Print IAM policy JSON for documentation.
#   cleanup           Remove all AWS resources created by deploy (runtimes, IAM,
#                     S3 objects, CloudWatch log groups).  Use
#                     deploy/agentcore-cleanup.sh for the common case.
#
# Options (all commands):
#   --properties PATH    deploy.properties file (default: deploy.properties beside this script)
#   --debug              Verbose logging
#
# deploy options:
#   --harness HARNESS    Include harness (may be repeated; default: claude codex kiro)
#   --approve TOKEN      Required for non-pinned source strategies (see plan output)
#   --no-rebuild         Skip bundle build; re-register runtimes only
#   --dry-run            Resolve versions; write nothing to AWS
#
# Exit codes (from python -m harness_image):
#   0   Success
#   1   Error
#   2   Usage error
#   3   Approval required (plan token in JSON)
#
# Examples:
#   deploy/agentcore-deploy.sh plan
#   deploy/agentcore-deploy.sh deploy --approve <token>
#   deploy/agentcore-deploy.sh status
#   deploy/agentcore-deploy.sh policy --kind execution
#
# Runtime update safety:
#   deploy updates runtimes in place.  Warm AgentCore sessions keep the old
#   version until idle timeout; the API/worker needs no restart.  Deploy when
#   utilisation is low to minimise the overlap window.

set -euo pipefail

# ── locate repo root and script directory ─────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── default properties path ───────────────────────────────────────────────────
PROPERTIES="${REPO_ROOT}/deploy.properties"

# ── venv paths ────────────────────────────────────────────────────────────────
VENV_DIR="${REPO_ROOT}/.deploy-venv"
PYTHON="${VENV_DIR}/bin/python"
REQUIREMENTS="${REPO_ROOT}/harness_image/requirements.lock"

# ── parse --properties before passing the rest to python ─────────────────────
FORWARD_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --properties)
            PROPERTIES="$2"
            FORWARD_ARGS+=("--properties" "$2")
            shift 2
            ;;
        --properties=*)
            PROPERTIES="${1#*=}"
            FORWARD_ARGS+=("--properties" "${PROPERTIES}")
            shift
            ;;
        *)
            FORWARD_ARGS+=("$1")
            shift
            ;;
    esac
done

# ── ensure uv is available ────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo '{"error":"uv not found. Install uv (https://docs.astral.sh/uv/getting-started/installation/) and retry."}' >&2
    exit 1
fi

# ── create or refresh the minimal venv ───────────────────────────────────────
if [[ ! -f "${PYTHON}" ]]; then
    echo "[agentcore-deploy] creating .deploy-venv with uv ..." >&2
    uv venv --python python3.12 "${VENV_DIR}" 1>&2
fi

# Install harness_image and its boto3 deps into the venv.
# `uv pip install -e .` picks up harness_image from pyproject.toml packages list.
if [[ ! -f "${VENV_DIR}/.installed" ]] \
   || [[ "${REPO_ROOT}/harness_image/requirements.lock" -nt "${VENV_DIR}/.installed" ]]; then
    echo "[agentcore-deploy] installing harness_image deps ..." >&2
    uv pip install --python "${PYTHON}" \
        --require-hashes -r "${REQUIREMENTS}" 1>&2
    uv pip install --python "${PYTHON}" \
        --no-deps -e "${REPO_ROOT}" 1>&2
    touch "${VENV_DIR}/.installed"
fi

# ── delegate to python -m harness_image ──────────────────────────────────────
exec "${PYTHON}" -m harness_image "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}"
