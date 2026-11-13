# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pinned harness versions and default vendor endpoints.

These are the documented defaults.  Users override them with
H_HARNESS_SOURCE / H_HARNESS_VERSION keys in deploy.properties.
"""

HARNESSES: tuple[str, ...] = ("claude", "codex", "kiro")

# Claude Code / claude-agent-sdk
CLAUDE_SDK_VERSION: str = "0.2.152"
CLAUDE_SDK_REQ: str = f"claude-agent-sdk=={CLAUDE_SDK_VERSION}"

# Codex — linux-arm64 variant for the AgentCore microVM
CODEX_VERSION: str = "0.154.0"
CODEX_PACKAGE: str = f"@openai/codex@{CODEX_VERSION}-linux-arm64"

# MCP Python SDK (required by all harnesses)
MCP_VERSION: str = "1.30.0"

# Kiro CLI
KIRO_VERSION: str = "2.21.3"
KIRO_CDN_URL_TEMPLATE: str = (
    "https://desktop-release.q.us-east-1.amazonaws.com"
    "/{version}/kirocli-{arch}-linux.zip"
)
KIRO_CDN_ARCH: str = "aarch64"  # AgentCore microVMs are arm64

# Default URL resolved from template using pinned version
KIRO_CDN_URL: str = KIRO_CDN_URL_TEMPLATE.format(
    version=KIRO_VERSION, arch=KIRO_CDN_ARCH
)

# Boto3 / botocore — same pins as the main requirements.lock
BOTO3_VERSION: str = "1.43.92"
BOTOCORE_VERSION: str = "1.43.92"

# Lambda-style bundle size limits (80% of hard AgentCore ceilings)
ZIP_CEILING: int = int(250 * 1024 * 1024 * 0.80)
UNZIP_CEILING: int = int(750 * 1024 * 1024 * 0.80)


def pinned_version(harness: str) -> str:
    """Return the pinned version string for *harness* (codex/claude/kiro)."""
    return {
        "claude": CLAUDE_SDK_VERSION,
        "codex": CODEX_VERSION,
        "kiro": KIRO_VERSION,
    }[harness]
