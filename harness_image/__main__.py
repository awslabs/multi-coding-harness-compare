# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Entry point for ``python -m harness_image``.

Subcommands
-----------
plan      Resolve versions and URIs, print JSON, write nothing remote.
deploy    Build bundles, upload to S3, create/update AgentCore runtimes.
status    Read-only: list deployed runtimes and their versions.
policy    Print machine-readable IAM policy documents (for docs/iam/).
cleanup   Remove all AWS resources created by deploy (runtimes, IAM, S3, logs).

Flags
-----
--properties PATH     deploy.properties file (default: deploy.properties)
--debug               Verbose logging (applies to all subcommands)
--harness HARNESS     Include this harness (may be repeated; default: all three)
--approve TOKEN       Required for non-pinned source strategies (see plan output)
                      Also used by cleanup to supply a consent token.
--include-artifacts   Delete comparison archives from ARTIFACT_S3_URI (cleanup)
--keep-logs           Keep CloudWatch log groups instead of deleting them (cleanup)
--no-rebuild          Skip bundle build; re-register runtimes only (deploy)
--dry-run             Resolve and validate; write nothing (deploy)

Exit codes
----------
0   Success
1   Error (details in stderr and JSON output)
2   Usage error
3   Approval required (plan token printed in JSON)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .cleanup import run_cleanup
from .deploy import DeployConfig, deploy, plan, status
from .iam import print_policy
from .props import load as load_props


def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
        stream=sys.stderr,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m harness_image",
        description=(
            "Standalone AgentCore harness image builder and deployer.\n\n"
            "Runtime update safety: deploy updates runtimes in place.  Warm AgentCore\n"
            "sessions keep the old version until idle timeout; the API/worker needs no\n"
            "restart.  Deploy when utilisation is low to minimise the overlap window."
        ),
        epilog=(
            "Exit codes:\n"
            "  0  Success\n"
            "  1  Error (details in stderr and JSON output)\n"
            "  2  Usage error (unknown subcommand or bad flag)\n"
            "  3  Approval required — run `plan` to obtain the token, then re-run\n"
            "     `deploy --approve TOKEN`; or run `cleanup` without --approve to\n"
            "     obtain a cleanup token, then re-run `cleanup --approve TOKEN`"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--properties", metavar="PATH", default="deploy.properties",
                   help="deploy.properties file (default: deploy.properties)")
    p.add_argument("--debug", action="store_true", help="Verbose logging")

    sub = p.add_subparsers(dest="command")

    # plan
    sp = sub.add_parser("plan", help="Resolve versions and URIs; print JSON")
    sp.add_argument("--harness", action="append", dest="harnesses", metavar="HARNESS")

    # cleanup
    sp = sub.add_parser(
        "cleanup",
        help="Remove all AWS resources created by deploy (runtimes, IAM, S3, logs)")
    sp.add_argument(
        "--approve", metavar="TOKEN", dest="approve_token",
        help=(
            "Consent token from a previous cleanup plan.  Resources are deleted "
            "only if the token matches a fresh re-discovery."
        ),
    )
    sp.add_argument(
        "--include-artifacts", action="store_true",
        help=(
            "Also delete the ARTIFACT_S3_URI prefix (comparison archives). "
            "Without this flag, comparison archives are kept."
        ),
    )
    sp.add_argument(
        "--keep-logs", action="store_true",
        help="Keep CloudWatch log groups (useful for post-mortem debugging).",
    )

    # deploy
    sp = sub.add_parser(
        "deploy",
        help="Build bundles, upload, create/update AgentCore runtimes")
    sp.add_argument("--harness", action="append", dest="harnesses", metavar="HARNESS")
    sp.add_argument("--approve", metavar="TOKEN", dest="approve_token",
                    help="Approval token from `plan` (required for non-pinned sources)")
    sp.add_argument("--no-rebuild", action="store_false", dest="rebuild", default=True,
                    help="Skip bundle build; re-register runtimes only")
    sp.add_argument("--dry-run", action="store_true",
                    help="Resolve versions only; write nothing to AWS")
    sp.add_argument(
        "--config-snapshot-dir", action="append", dest="config_snapshot_dirs",
        metavar="HARNESS=PATH",
        help=(
            "Bake workstation config into the bundle for HARNESS (may be repeated). "
            "Format: HARNESS=PATH, e.g. --config-snapshot-dir claude=/tmp/claude-snap. "
            "When HARNESS_IMAGE_WORKSTATION_CONFIG=true in deploy.properties, snapshots "
            "are collected automatically via arena.host_setup if available."
        ),
    )

    # status
    sp = sub.add_parser("status", help="List deployed runtime status (read-only)")
    sp.add_argument("--harness", action="append", dest="harnesses", metavar="HARNESS")

    # policy
    sp = sub.add_parser("policy", help="Print IAM policy JSON for documentation")
    sp.add_argument("--kind", choices=["execution", "deployer", "workstation"],
                    default="execution",
                    help="Which policy to print (default: execution)")

    return p


def _emit(obj: dict) -> None:
    # One compact JSON line per the bootstrap one-JSON-summary-line contract.
    print(json.dumps(obj, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.debug)

    if args.command is None:
        parser.print_help(sys.stderr)
        return 2

    # Policy printing needs no AWS access
    if args.command == "policy":
        print_policy(args.kind)
        return 0

    # Load properties
    props_path = Path(args.properties)
    try:
        props = load_props(props_path)
    except Exception as exc:
        _emit({"error": f"deploy.properties load error: {exc}", "path": str(props_path)})
        return 1

    try:
        cfg = DeployConfig.from_props(props)
    except ValueError as exc:
        _emit({"error": str(exc)})
        return 1

    harnesses = getattr(args, "harnesses", None)
    trusted = [h.strip() for h in props.get("HARNESS_TRUSTED_SOURCES", "").split(",")
               if h.strip()] or None
    kiro_url = props.get("KIRO_DOWNLOAD_URL") or None

    if args.command == "cleanup":
        artifact_s3_uri = props.get("ARTIFACT_S3_URI") or None
        return run_cleanup(
            cfg,
            approve_token=getattr(args, "approve_token", None),
            include_artifacts=getattr(args, "include_artifacts", False),
            artifact_s3_uri=artifact_s3_uri,
            keep_logs=getattr(args, "keep_logs", False),
        )

    elif args.command == "plan":
        try:
            result = plan(cfg, harnesses, props,
                          trusted_hosts=trusted, kiro_url_template=kiro_url)
            _emit({
                "harnesses": result["harnesses"],
                "token": result["token"],
                "next": f"Run `deploy --approve {result['token']}` to proceed.",
            })
            return 0
        except Exception as exc:
            _emit({"error": str(exc)})
            return 1

    elif args.command == "deploy":
        try:
            # Build config_snapshot_dirs from --config-snapshot-dir HARNESS=PATH flags.
            snapshot_dirs: dict[str, Path] | None = None
            explicit_snapshots = getattr(args, "config_snapshot_dirs", None) or []
            parsed_snapshots: dict[str, Path] = {}
            for spec in explicit_snapshots:
                if "=" not in spec:
                    _emit({"error": f"--config-snapshot-dir must be HARNESS=PATH, got: {spec!r}"})
                    return 2
                h, _, p = spec.partition("=")
                parsed_snapshots[h.strip()] = Path(p.strip())
            # When HARNESS_IMAGE_WORKSTATION_CONFIG=true and no explicit dirs given,
            # try to auto-collect from arena.host_setup.
            if (not parsed_snapshots
                    and props.get("HARNESS_IMAGE_WORKSTATION_CONFIG", "").lower()
                    in ("1", "true", "yes")):
                try:
                    from arena.host_setup import collect_config_snapshot  # type: ignore[import]
                    # Pass the arena Config so collect_config_snapshot honours
                    # per-harness config_dir overrides (CLAUDE_CONFIG_DIR etc.).
                    try:
                        from arena.config import load_config  # type: ignore[import]
                        _arena_config = load_config()
                    except Exception:
                        _arena_config = None
                    parsed_snapshots = collect_config_snapshot(harnesses, config=_arena_config)
                except ImportError:
                    pass  # arena not available — standalone mode, skip workstation config
            if parsed_snapshots:
                snapshot_dirs = parsed_snapshots
            result = asyncio.run(deploy(
                cfg, harnesses,
                rebuild=args.rebuild,
                approve_token=args.approve_token,
                dry_run=args.dry_run,
                props=props,
                kiro_url_template=kiro_url,
                config_snapshot_dirs=snapshot_dirs,
            ))
            _emit(result)
            return 0
        except RuntimeError as exc:
            msg = str(exc)
            if "require --approve TOKEN" in msg or "Approval token" in msg:
                _emit({"error": msg, "next": "Run `plan` to get the approval token."})
                return 3
            _emit({"error": msg})
            return 1
        except Exception as exc:
            _emit({"error": str(exc)})
            return 1

    elif args.command == "status":
        try:
            result = asyncio.run(status(cfg, harnesses))
            _emit(result)
            return 0
        except Exception as exc:
            _emit({"error": str(exc)})
            return 1

    else:
        parser.print_help(sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
