# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""harness_image — standalone AgentCore harness image builder and deployer.

This package is intentionally independent of the arena package so it can be
installed and run without the full UI/worker dependencies.  It reads
deploy.properties through its own minimal stdlib-only parser (harness_image.props).

Sub-modules
-----------
props       Minimal deploy.properties parser (stdlib only, importable by arena).
versions    Pinned version constants for claude-agent-sdk, Codex and Kiro.
sources     Version-source resolution (pinned / installed / latest).
bundle      Deployment-package (zip) builder.
iam         IAM trust and inline policy document builders.
deploy      AgentCore Runtime provision, update and status functions.
__main__    CLI entry point: python -m harness_image plan|deploy|status|policy.

Usage from the arena CLI
------------------------
arena/agentcore_provision.py delegates to deploy.deploy() / deploy.DeployConfig
so existing `multi-coding-harness-compare agentcore-deploy` invocations continue
to work unchanged.
"""
from .versions import HARNESSES, CLAUDE_SDK_VERSION, CODEX_VERSION, KIRO_VERSION
from .deploy import DeployConfig

__all__ = [
    "HARNESSES",
    "CLAUDE_SDK_VERSION",
    "CODEX_VERSION",
    "KIRO_VERSION",
    "DeployConfig",
]
