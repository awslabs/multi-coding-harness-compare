# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Model-identity helpers used by the worker to detect harness model substitutions."""


def model_mismatch(requested: str | None, reported: str | None) -> bool:
    """True when both are present, differ after stripping [1m], and requested looks like a full id.

    Aliases (no dot: "sonnet", "opus", "default", "auto") never trigger a mismatch because
    the harness will legitimately resolve them to a concrete id that differs from the alias.
    """
    if not requested or not reported:
        return False
    r = requested.removesuffix("[1m]")
    m = reported.removesuffix("[1m]")
    if r == m:
        return False
    # A bare alias has no "." and cannot be compared to a resolved id.
    if "." not in r:
        return False
    return True
