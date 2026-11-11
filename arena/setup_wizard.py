# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Generate config.toml and deploy.properties on a new workstation.

Environment values (AWS profile and region, S3 buckets, repository roots) go to
deploy.properties; config.toml keeps behaviour. An existing deploy.properties answers
the questions it already holds, and only the keys setup decides are rewritten in it.

Setup is usually driven by an AI coding assistant rather than a person at a terminal,
so nothing here blocks on stdin unless --interactive is passed AND stdin is a terminal.
Without that, an unresolved decision is reported with the flag that settles it and the
exact line it writes, and the command exits non-zero having written nothing. Re-running
with the flag is the whole interaction.
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

from urllib.parse import urlsplit

from . import properties as props
from .host_setup import KIRO_LOGIN_FILE, default_home_source

KIRO_CHOICES = ("copy-host-login", "login-in-home")
# config.example.toml is copied as is; every answer setup settles is written to
# deploy.properties, including RUNTIME and KIRO_AUTH.
RUNTIMES = ("native", "agentcore")
ARTIFACT_PREFIX = "output/codecouncil/"
TRANSFER_PREFIX = "code/ai-coding-assistant-agcore-runtime/"
# Values copied from deploy.properties.example that nobody has filled in yet.
PLACEHOLDERS = ("YOUR_", "/absolute/path/to/")


def _filled(value):
    return value if value and not any(mark in value for mark in PLACEHOLDERS) else None


def _bucket(uri):
    return urlsplit(uri).netloc if uri and uri.startswith("s3://") else None


def _prefix(uri, default):
    path = urlsplit(uri).path.strip("/") if uri and uri.startswith("s3://") else ""
    return (path + "/") if path else default


def _known(existing: dict) -> dict:
    """Answers an existing deploy.properties already holds."""
    roots = [r.strip() for r in (existing.get("REPOSITORY_ROOTS") or "").split(",") if r.strip()]
    artifact = _filled(_bucket(existing.get("ARTIFACT_S3_URI")))
    transfer = _filled(_bucket(existing.get("AGENTCORE_TRANSFER_S3_URI")))
    return {"repository_root": _filled(roots[0]) if roots else None,
            "aws_profile": _filled(existing.get("AWS_PROFILE")),
            "aws_region": _filled(existing.get("AWS_REGION")),
            "artifact_bucket": artifact,
            "runtime": existing.get("RUNTIME") if existing.get("RUNTIME") in RUNTIMES else None,
            "kiro_auth": existing.get("KIRO_AUTH") if existing.get("KIRO_AUTH") in KIRO_CHOICES else None,
            # Only a transfer bucket that differs is a separate choice to keep; one equal to
            # the artifact bucket follows it when the artifact bucket changes.
            "transfer_bucket": transfer if transfer != artifact else None}


def _decisions(config_path: Path, example_path: Path, args) -> list[dict]:
    """Every choice setup cannot make on its own, with the flag that resolves it."""
    pending = []
    if config_path.exists() and not args.force:
        pending.append({
            "id": "existing-config",
            "question": f"{config_path} already exists. Overwrite it?",
            "flag": "--force",
            "writes": "replaces the existing config.toml",
            "options": ["keep", "overwrite"],
        })
    if not example_path.is_file():
        pending.append({"id": "missing-example", "question": f"{example_path} is missing",
                        "flag": None, "writes": None, "options": []})
    if not args.repository_root:
        pending.append({
            "id": "repository-root",
            "question": "Which directory holds the projects multi-coding-harness-compare may run against?",
            "flag": "--repository-root=/absolute/path",
            "writes": "REPOSITORY_ROOTS in deploy.properties",
            "options": [],
        })
    if not args.aws_profile:
        pending.append({
            "id": "aws-profile",
            "question": ("Which AWS profile reaches Bedrock, the artifact bucket and "
                         "AgentCore on this workstation?"),
            "flag": "--aws-profile=NAME",
            "writes": "AWS_PROFILE in deploy.properties (the artifact, judge and AgentCore profile)",
            "options": sorted(_profiles()),
        })
    if not args.artifact_bucket:
        pending.append({
            "id": "artifact-bucket",
            "question": ("Which S3 bucket stores comparison artifacts? This is required: "
                         "no comparison can finish without a writable artifact prefix."),
            "flag": "--artifact-bucket=NAME",
            "writes": "S3_BUCKET, ARTIFACT_S3_URI and AGENTCORE_TRANSFER_S3_URI in deploy.properties",
            "options": [],
        })
    if args.runtime not in RUNTIMES:
        pending.append({
            "id": "runtime",
            "question": ("Where should the coding harnesses run? native uses the macOS "
                         "Seatbelt sandbox on this workstation; agentcore runs each harness "
                         "in a Bedrock AgentCore Firecracker microVM, where Claude and Codex "
                         "authenticate through the runtime execution role."),
            "flag": "--runtime=native|agentcore",
            "writes": "RUNTIME = native or agentcore in deploy.properties",
            "options": list(RUNTIMES),
        })
    if not args.kiro_auth:
        source = default_home_source("kiro")[0]
        login = Path(source or "/nonexistent", "data.sqlite3")
        pending.append({
            "id": "kiro-auth",
            "question": ("How should Kiro authenticate? It has no AgentCore execution-role "
                         "path, so its own login is the only credential it can use. Claude "
                         "and Codex need no answer here: both reach Bedrock through the "
                         "execution role."),
            "flag": "--kiro-auth=copy-host-login|login-in-home",
            "writes": "KIRO_AUTH in deploy.properties (copy-host-login sets Kiro's refresh_files "
                      f'to ["{KIRO_LOGIN_FILE}"]; login-in-home copies nothing)',
            "options": list(KIRO_CHOICES),
            "host_login": str(login),
            "host_login_present": login.is_file(),
        })
    return pending


def _profiles() -> set:
    try:
        import boto3
        return set(boto3.Session().available_profiles)
    except Exception:
        return set()


def _ask(pending: list[dict]) -> dict:
    """Prompt a human. Only reached with --interactive on a real terminal."""
    answers = {}
    for decision in pending:
        print("\n" + decision["question"])
        if decision["options"]:
            print("  options: " + ", ".join(decision["options"]))
        if decision.get("host_login"):
            found = "found" if decision["host_login_present"] else "not found"
            print(f"  this workstation's Kiro login: {decision['host_login']} ({found})")
        if not decision["flag"]:
            raise SystemExit(f"Cannot continue: {decision['question']}")
        reply = input(f"  {decision['id']}> ").strip()
        if not reply:
            raise SystemExit(f"No answer for {decision['id']}; nothing was written.")
        answers[decision["id"]] = reply
    return answers


def _render_properties(template: str, existing: dict, *, repository_root: str, aws_profile: str,
                       aws_region: str, artifact_bucket: str, transfer_bucket: str,
                       runtime: str, kiro_auth: str) -> str:
    """Rewrite only the keys setup decides; comments, references and other keys stay."""
    values = {"RUNTIME": runtime, "KIRO_AUTH": kiro_auth,
              "AWS_PROFILE": aws_profile, "AWS_REGION": aws_region,
              "S3_BUCKET": artifact_bucket,
              "ARTIFACT_S3_URI": "s3://${S3_BUCKET}/"
                                 + _prefix(existing.get("ARTIFACT_S3_URI"), ARTIFACT_PREFIX),
              "AGENTCORE_TRANSFER_S3_URI": (
                  "s3://${S3_BUCKET}/" if transfer_bucket == artifact_bucket
                  else f"s3://{transfer_bucket}/")
                  + _prefix(existing.get("AGENTCORE_TRANSFER_S3_URI"), TRANSFER_PREFIX)}
    roots = [r.strip() for r in (existing.get("REPOSITORY_ROOTS") or "").split(",") if _filled(r.strip())]
    if repository_root not in roots:
        # A new root comes first (it is the one setup reports); the roots already listed stay.
        values["REPOSITORY_ROOTS"] = ", ".join([repository_root, *roots])
    return props.render(template, values)


def _authentication_summary(runtime: str, kiro_auth: str) -> dict:
    """State the model path per harness, so an operator accepts it before full setup."""
    if runtime == "agentcore":
        remote = ("Amazon Bedrock through the AgentCore execution role: no host credential "
                  "is copied and no API key is forwarded")
        summary = {"claude": remote,
                   "codex": remote + "; only the provider configuration in this "
                                     "workstation's Codex directory is copied in"}
    else:
        summary = {"claude": "its own login on this workstation, refreshed into each run's "
                             "private HOME (.claude/.credentials.json)",
                   "codex": "its own login and provider configuration on this workstation, "
                            "refreshed into each run's private HOME (.codex/auth.json)"}
    summary["kiro"] = ("this workstation's Kiro login, copied into each run's private HOME"
                       if kiro_auth == "copy-host-login" else
                       "no login copied; Kiro must be logged in inside its own HOME")
    return summary


def run(args) -> int:
    root = Path(__file__).parent.parent
    config_path = Path(args.output).expanduser() if args.output else root / "config.toml"
    example_path = root / "config.example.toml"
    properties_path = (Path(args.properties).expanduser() if getattr(args, "properties", None)
                       else config_path.parent / props.PROPERTIES_FILE)
    try:
        existing = props.load(properties_path).values
    except props.PropertiesError as exc:
        print(json.dumps({"ok": False, "wrote": None, "error": str(exc)}, indent=2))
        return 1
    known = _known(existing)
    args.repository_root = args.repository_root or known["repository_root"]
    args.aws_profile = args.aws_profile or known["aws_profile"]
    args.artifact_bucket = args.artifact_bucket or known["artifact_bucket"]
    args.runtime = args.runtime or known["runtime"]
    args.kiro_auth = args.kiro_auth or known["kiro_auth"]
    aws_region = getattr(args, "aws_region", None) or known["aws_region"]
    # Do NOT fall back to a hardcoded region — leave aws_region as None so the
    # preflight aws-region check (which requires AWS_REGION in deploy.properties)
    # will fire and the operator is prompted for the correct value for their account.

    pending = _decisions(config_path, example_path, args)
    interactive = args.interactive and sys.stdin.isatty()
    if pending and args.interactive and not interactive:
        print(json.dumps({"ok": False, "interactive": False, "reason":
            "--interactive needs a terminal; stdin is not a tty. Re-run with the flags below.",
            "decisions": pending}, indent=2))
        return 1
    if pending and interactive:
        answers = _ask(pending)
        args.repository_root = answers.get("repository-root", args.repository_root)
        args.aws_profile = answers.get("aws-profile", args.aws_profile)
        args.artifact_bucket = answers.get("artifact-bucket", args.artifact_bucket)
        args.runtime = answers.get("runtime", args.runtime)
        args.kiro_auth = answers.get("kiro-auth", args.kiro_auth)
        if answers.get("existing-config") == "overwrite":
            args.force = True
        pending = _decisions(config_path, example_path, args)
    if pending:
        print(json.dumps({"ok": False, "wrote": None,
                          "hint": "Resolve each decision with its flag, or add --interactive "
                                  "to answer them at a terminal, then run setup again.",
                          "decisions": pending}, indent=2))
        return 1
    if args.kiro_auth not in KIRO_CHOICES:
        print(json.dumps({"ok": False, "wrote": None,
                          "error": f"--kiro-auth must be one of {', '.join(KIRO_CHOICES)}"}, indent=2))
        return 1
    transfer_bucket = args.transfer_bucket or known["transfer_bucket"] or args.artifact_bucket

    rendered = example_path.read_text()
    template = (properties_path.read_text() if properties_path.is_file()
                else (root / "deploy.properties.example").read_text())
    rendered_properties = _render_properties(
        template, existing, repository_root=args.repository_root, aws_profile=args.aws_profile,
        aws_region=aws_region, artifact_bucket=args.artifact_bucket, transfer_bucket=transfer_bucket,
        runtime=args.runtime, kiro_auth=args.kiro_auth)
    # Reject a configuration this build would refuse to load, before overwriting
    # anything: a setup that writes an unloadable config.toml is worse than none.
    try:
        from .config import resolve_config
        candidate = props.Properties(properties_path.resolve(),
                                     props.parse(rendered_properties, str(properties_path)))
        config, _ = resolve_config(config_path, text=rendered, properties=candidate)
    except Exception as exc:
        print(json.dumps({"ok": False, "wrote": None,
                          "error": f"generated configuration is not valid: {exc}"}, indent=2))
        return 1
    runtime = config.runtime
    if config_path.exists():
        shutil.copyfile(config_path, config_path.with_suffix(".toml.backup"))
    config_path.write_text(rendered)
    if properties_path.is_file() and properties_path.read_text() != rendered_properties:
        shutil.copyfile(properties_path, properties_path.with_name(properties_path.name + ".backup"))
    properties_path.write_text(rendered_properties)

    result = {"ok": True, "wrote": str(config_path), "properties": str(properties_path),
              "runtime": runtime, "repository_roots": config.repository_roots,
              "aws_profile": args.aws_profile, "aws_region": aws_region,
              "artifact_bucket": args.artifact_bucket, "kiro_auth": args.kiro_auth,
              "authentication": _authentication_summary(runtime, args.kiro_auth)}
    if runtime == "agentcore":
        cfg = config.agentcore
        isolation = getattr(cfg, "isolation", "shared")
        user_namespace = getattr(config, "user_namespace", None)
        # Build a display pattern showing the operator what the runtime will be called.
        # Use the real helper on "claude" to illustrate the name shape, then replace it
        # with the <harness> placeholder so the operator sees the full template.
        if isolation == "per-user" and user_namespace:
            clean_ns = user_namespace.replace("-", "_")
            display_pattern = f"{cfg.runtime_name_prefix}_<harness>_{clean_ns}"
        else:
            display_pattern = f"{cfg.runtime_name_prefix}_<harness>"
        result["agentcore"] = {"transfer_s3_uri": cfg.transfer_s3_uri, "region": cfg.region,
                               "execution_role_name": cfg.execution_role_name,
                               "runtime_discovery": f"by canonical name {display_pattern}; "
                                                    "no ARNs needed in config.toml"}
    result["next_steps"] = _next_steps(runtime, args)
    if runtime == "agentcore" and args.deploy:
        result["deployment"] = _deploy(config_path, properties_path)
        result["ok"] = bool(result["deployment"].get("ok"))
        result["next_steps"] = [step for step in result["next_steps"]
                                if step["action"] != "multi-coding-harness-compare agentcore-deploy"]
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def _next_steps(runtime: str, args) -> list[dict]:
    """Ordered commands left to run, each one an agent can execute as written."""
    steps = []
    if args.kiro_auth == "login-in-home":
        steps.append({
            "why": "Kiro cannot reach a model until it has a login of its own.",
            "action": "kiro-cli login",
            "note": ("A browser device flow a person has to complete; it cannot be automated. "
                     "Run it with HOME set to a dedicated directory, then point "
                     "harnesses.kiro.home_template at that directory."),
        })
    else:
        login = Path(default_home_source("kiro")[0] or "/nonexistent", "data.sqlite3")
        if not login.is_file():
            steps.append({
                "why": f"No Kiro login exists yet at {login}.",
                "action": "kiro-cli login",
                "note": ("A browser device flow a person has to complete. Setup already "
                         "recorded that the resulting login may be copied."),
            })
    if runtime == "agentcore":
        if not args.deploy:
            steps.append({
                "why": ("The three AgentCore runtimes and their execution role do not exist "
                        "until they are provisioned. Re-run setup with --deploy to do this "
                        "in one step."),
                "action": "multi-coding-harness-compare agentcore-deploy",
                "note": ("Creates or refreshes the IAM execution role, uploads a deployment "
                         "zip per harness, and registers each runtime. Safe to repeat."),
            })
        steps.append({
            "why": ("A redeploy can change the command user or the mounts underneath a working "
                    "configuration, which otherwise surfaces as all three harnesses failing "
                    "seconds into a comparison."),
            "action": "multi-coding-harness-compare agentcore-probe",
            "note": "Read-only. Reports the runtime user and whether each configured path is writable.",
        })
    steps.append({
        "why": "Confirms every remaining blocker before the first comparison.",
        "action": "multi-coding-harness-compare doctor",
        "note": "Read-only. Exits non-zero while any blocker remains.",
    })
    return steps


def _deploy(config_path: Path, properties_path: Path | None = None) -> dict:
    """Provision the AgentCore runtimes using the configuration just written.

    Only reached with an explicit --deploy: this creates AWS resources (an IAM role,
    S3 objects and three runtimes), which a command whose job is writing a file must
    not do implicitly.
    """
    import asyncio
    os.environ["CODECOUNCIL_CONFIG"] = str(config_path)
    from .config import load_config
    config = load_config(config_path, properties_path)
    detail = {}
    try:
        from .agentcore_provision import deploy
        detail["deploy"] = asyncio.run(deploy(config))
    except Exception as exc:
        return {"ok": False, "stage": "agentcore-deploy", "error": f"{type(exc).__name__}: {exc}"}
    try:
        from .agentcore_sandbox import AgentCore
        probe = asyncio.run(AgentCore(config).probe())
        detail["probe"] = probe
        detail["ok"] = bool(probe.get("ok"))
        if not detail["ok"]:
            detail["stage"] = "agentcore-probe"
            detail["error"] = probe.get("error") or (
                "Provisioning succeeded but a configured path is not writable in "
                "at least one runtime; see probe.runtimes.")
    except Exception as exc:
        detail["ok"] = False
        detail["stage"] = "agentcore-probe"
        detail["error"] = f"{type(exc).__name__}: {exc}"
    return detail


def add_arguments(parser):
    parser.add_argument("--repository-root", metavar="PATH",
                        help="Absolute directory holding the projects multi-coding-harness-compare may run against")
    parser.add_argument("--aws-profile", metavar="NAME",
                        help="AWS profile for Bedrock, artifact storage and AgentCore (AWS_PROFILE)")
    parser.add_argument("--aws-region", metavar="REGION",
                        help="Region for Bedrock, artifact storage and AgentCore (AWS_REGION); "
                             "defaults to deploy.properties; required — no silent default")
    parser.add_argument("--artifact-bucket", metavar="NAME",
                        help="S3 bucket for comparison artifacts; required for a comparison to finish")
    parser.add_argument("--runtime", choices=RUNTIMES,
                        help="native: macOS Seatbelt on this workstation. agentcore: a Bedrock "
                             "AgentCore microVM per harness, where Claude and Codex authenticate "
                             "through the runtime execution role.")
    parser.add_argument("--transfer-bucket", metavar="NAME",
                        help="S3 bucket for AgentCore job/result transfer; defaults to --artifact-bucket")
    parser.add_argument("--deploy", action="store_true",
                        help="With --runtime=agentcore, also run agentcore-deploy and "
                             "agentcore-probe. Creates AWS resources: an IAM execution role, S3 "
                             "objects and three AgentCore runtimes.")
    parser.add_argument("--kiro-auth", choices=KIRO_CHOICES,
                        help="copy-host-login: copy this workstation's Kiro login into each run's "
                             "private HOME. login-in-home: copy nothing; log in inside Kiro's own HOME. "
                             "Claude and Codex always use the AgentCore execution role and need no choice.")
    parser.add_argument("--output", metavar="PATH", help="Write somewhere other than ./config.toml")
    parser.add_argument("--properties", metavar="PATH",
                        help="deploy.properties to read and update; defaults to the one beside config.toml")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing config.toml")
    parser.add_argument("--interactive", action="store_true",
                        help="Prompt for unresolved decisions; requires a terminal and never "
                             "prompts without one")
    return parser
