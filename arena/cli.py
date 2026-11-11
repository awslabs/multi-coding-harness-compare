# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import socket
import fcntl
import time
from pathlib import Path

from .config import load_config, resolve_config
from .agentcore_provision import runtime_name as _runtime_name


def start_background(config, open_browser=False):
    """Detach local services from the invoking terminal without installing an OS service."""
    from .auth import access_token
    config.root.mkdir(parents=True, exist_ok=True)
    with (config.root / "launcher.lock").open("a+") as launch_lock:
        fcntl.flock(launch_lock, fcntl.LOCK_EX)
        needed = []
        try:
            with socket.create_connection((config.host, config.port), timeout=1):
                print(f"Port {config.port} already has a listener; it was left untouched.")
        except OSError:
            needed.append("serve")
        with (config.root / "worker.lock").open("a+") as worker_lock:
            try:
                fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                needed.append("worker")
                fcntl.flock(worker_lock, fcntl.LOCK_UN)
            except BlockingIOError:
                print("The worker already owns this data directory.")
        launched = {}
        for role in needed:
            log = config.root / ("api.log" if role == "serve" else "worker.log")
            with log.open("a") as output:
                os.fchmod(output.fileno(), 0o600)
                extra_env: dict = {"CODECOUNCIL_SERVICE_LOG": "api" if role == "serve" else "worker"}
                # Forward non-default AWS credential file paths from deploy.properties
                # so subprocesses (api, worker) use the same credential files as the
                # calling process.  boto3 sessions in artifact_storage, judge_models and
                # agentcore_sandbox all need these to find the right credentials.
                if config.aws_config_file:
                    extra_env["AWS_CONFIG_FILE"] = config.aws_config_file
                if config.aws_shared_creds_file:
                    extra_env["AWS_SHARED_CREDENTIALS_FILE"] = config.aws_shared_creds_file
                launched[role] = subprocess.Popen([sys.executable, "-m", "codecouncil", role],
                    cwd=Path.cwd(), stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                    env={**os.environ, **extra_env})
        if launched:
            time.sleep(1)
        records = {}
        for role, process in launched.items():
            if process.poll() is not None:
                raise RuntimeError(f"{role} exited during startup; inspect the logs in {config.root}")
            records[role] = process.pid
            print(f"Started {role}, PID {process.pid}.")
        if records:
            (config.root / "services.json").write_text(json.dumps({"started_at": time.time(), "pids": records}, indent=2))
        url = f"http://{config.host}:{config.port}/#token={access_token(config.root)}"
        print(f"Open locally: {url}")
        if open_browser:
            import webbrowser
            if not webbrowser.open(url, new=2):
                print("The browser could not be opened automatically. Open the access link above.")


def configuration_drift(config, properties: dict) -> list[str]:
    """Cached AgentCore runtime records that no longer match the configuration.

    They are written by agentcore-deploy for one account, region, name prefix and
    transfer prefix; after a port or a changed property they point at resources this
    configuration would neither create nor find.
    """
    if config.runtime != "agentcore":
        return []
    path = config.root / "agentcore-runtimes.json"
    try:
        records = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError) as exc:
        return [f"{path}: unreadable ({exc})"]
    cfg, account, issues = config.agentcore, properties.get("AWS_ACCOUNT_ID"), []
    isolation = getattr(cfg, "isolation", "shared")
    namespace = (getattr(config, "user_namespace", None)
                 if isolation == "per-user" else None)
    fix = "run agentcore-deploy for this configuration, or restore the previous value"
    for harness, record in sorted(records.items()):
        if not isinstance(record, dict):
            continue
        parts = str(record.get("arn") or "").split(":")
        if len(parts) > 5 and parts[3] != cfg.region:
            issues.append(f"{harness}: cached runtime is in {parts[3]}, AWS_REGION is {cfg.region}; {fix}")
        if len(parts) > 5 and account and parts[4] != account:
            issues.append(f"{harness}: cached runtime belongs to account {parts[4]}, "
                          f"AWS_ACCOUNT_ID is {account}; {fix}")
        runtime_id = str(record.get("id") or "")
        expected_prefix = _runtime_name(cfg.runtime_name_prefix, harness, namespace)
        if runtime_id and not runtime_id.startswith(expected_prefix):
            issues.append(f"{harness}: cached runtime {runtime_id} does not use "
                          f"AGENTCORE_RUNTIME_NAME_PREFIX {cfg.runtime_name_prefix}; {fix}")
        bundle = str(record.get("bundle") or "")
        if bundle.startswith("s3://") and cfg.transfer_s3_uri and not bundle.startswith(cfg.transfer_s3_uri):
            issues.append(f"{harness}: cached bundle {bundle} is outside AGENTCORE_TRANSFER_S3_URI "
                          f"{cfg.transfer_s3_uri}; {fix}")
    return issues


ACTIVE_RUN_STATES = {"setup", "clarifying", "developing", "evaluating", "planning", "archiving", "restoring"}


def _own_service_pids(config) -> dict:
    """This installation's API (the listener on its port) and worker (the holder of its lock)."""
    found = {}
    try:
        recorded = json.loads((config.root / "services.json").read_text()).get("pids", {})
    except (OSError, ValueError, AttributeError):
        recorded = {}
    probes = {"serve": ["lsof", "-nP", "-t", f"-iTCP:{config.port}", "-sTCP:LISTEN"],
              "worker": ["lsof", "-t", str(config.root / "worker.lock")]}
    for role, probe in probes.items():
        try:
            pids = subprocess.run(probe, capture_output=True, text=True, timeout=10).stdout.split()
        except (OSError, subprocess.SubprocessError):
            pids = []
        for pid in pids:
            try:
                line = subprocess.run(["ps", "-p", pid, "-o", "command="], capture_output=True, text=True,
                                      timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                # A sandboxed shell may not run ps: trust only the PID this installation's start recorded.
                if recorded.get(role) == int(pid):
                    found[role] = int(pid)
                continue
            # Only this application's own service command; never another process on the port.
            if f"codecouncil {role}" in line or f"arena.cli {role}" in line or f"arena {role}" in line:
                found[role] = int(pid)
    return found


def stop_services(config, force=False) -> int:
    """Stop the API and worker of this data directory; refuse while runs are in progress."""
    import signal
    from .store import Store
    active = []
    if (config.root / "arena.sqlite3").exists():
        active = [f"{r['harness']} {r['status']} (comparison {r['comparison_id'][:8]})"
                  for r in Store(config.root).list("run") if r.get("status") in ACTIVE_RUN_STATES]
    if active and not force:
        print(json.dumps({"ok": False, "stopped": {}, "active_runs": active,
                          "error": "runs are in progress; stopping would interrupt them. Wait for them to "
                                   "finish, or pass --force to interrupt them (Resume can continue them)."}, indent=2))
        return 1
    pids = _own_service_pids(config)
    for pid in pids.values():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:  # it exited on its own meanwhile
            pass
    deadline, remaining = time.time() + 90, dict(pids)
    while remaining and time.time() < deadline:
        time.sleep(1)
        remaining = {role: pid for role, pid in remaining.items() if _alive(pid)}
    for pid in remaining.values():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    print(json.dumps({"ok": True, "stopped": pids, "killed_after_90s": remaining,
                      "interrupted_runs": active}, indent=2))
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def doctor():
    """Read-only probe. Safe for an agent to call repeatedly; writes nothing."""
    from .isolation import execution_runtime, command
    from .host_setup import authentication_plan, configuration_plan
    from .config import environment_literal_fields
    config, configuration = resolve_config()
    configuration["drift"] = configuration_drift(config, configuration["properties"])
    result = {"configuration": configuration,
              "data_dir": str(config.root), "runtime": config.runtime,
              "judge_model": config.judge_model, "planning_model": config.effective_planning_model,
              "planning_effort": config.planning_effort,
              "independent_evaluation": "enabled" if config.effective_planning_model else
                  "unavailable: set judge_model (and optionally planning_model) or plans fall back "
                  "to a deterministic rubric with no independent acceptance tests",
              "repository_roots": {str(p): Path(p).expanduser().is_dir()
                                   for p in config.repository_roots},
              "artifacts": artifact_readiness(config),
              # Namespace / isolation: visible so an operator can verify per-user scoping.
              "user_namespace": getattr(config, "user_namespace", None),
              "agentcore_isolation": (getattr(config.agentcore, "isolation", "shared")
                                      if config.runtime == "agentcore" else None),
              # Environment-literal fields: config.toml values that belong in deploy.properties.
              "environment_literals": environment_literal_fields(configuration),
              "harnesses": {}}
    for name, cfg in config.harnesses.items():
        executable = "kiro-cli" if name == "kiro" else name
        entry = {"host_executable": cfg.cli_path or shutil.which(executable),
                 "environment_keys_present": [e for e in cfg.env if e in os.environ],
                 "dedicated_home": cfg.home_template, "model": cfg.model,
                 "setup": "existing macOS CLI" if cfg.inherit_host_setup else "dedicated setup",
                 "authentication": authentication_plan(config, name)}
        if entry["host_executable"]:
            _, out, _ = await command(entry["host_executable"], "--version")
            entry["host_version"] = out.strip()
        try:
            entry["runtime"] = await execution_runtime(config).check()
        except Exception as exc:
            entry["blocked"] = str(exc)
        # Configuration plan per harness: shows files, skills, MCP, withheld secrets.
        try:
            entry["configuration_plan"] = configuration_plan(config, name)
        except Exception as exc:
            entry["configuration_plan"] = {"error": str(exc)}
        result["harnesses"][name] = entry
    # Version plan per harness: shows resolved version, source, approval status.
    try:
        from harness_image.deploy import DeployConfig, plan as _version_plan
        _dc = DeployConfig.from_arena_config(config)
        _props = configuration.get("properties", {})
        _vplan = _version_plan(_dc, None, _props)
        result["version_plan"] = {
            h: {k: v for k, v in info.items() if k != "resolved"}
            for h, info in _vplan.get("harnesses", {}).items()
        }
        result["version_plan"]["_token"] = _vplan.get("token")
    except Exception as exc:
        result["version_plan"] = {"error": str(exc)}
    blockers = [f"{name}: {entry['authentication']['blocked']}"
                for name, entry in result["harnesses"].items()
                if entry["authentication"].get("blocked")]
    missing = [root for root, present in result["repository_roots"].items() if not present]
    blockers += [f"repository_roots: {root} does not exist" for root in missing]
    if result["artifacts"].get("error"):
        blockers.append("artifacts: " + result["artifacts"]["error"])
    blockers += [f"agentcore drift: {issue}" for issue in configuration["drift"]]
    result["blockers"] = blockers
    result["ok"] = not blockers
    print(json.dumps(result, indent=2))
    if blockers:
        raise SystemExit(1)


def artifact_readiness(config) -> dict:
    """Can this workstation reach the configured artifact prefix? S3 is required
    before any comparison can finish, so a missing grant should surface here."""
    if not config.artifact_s3_uri:
        return {"destination": None,
                "error": "artifact_s3_uri is not set; comparisons cannot finish without it"}
    from urllib.parse import urlsplit
    target = urlsplit(config.artifact_s3_uri)
    detail = {"destination": config.artifact_s3_uri, "bucket": target.netloc,
              "prefix": target.path.lstrip("/"), "profile": config.artifact_s3_profile,
              "region": config.artifact_s3_region}
    try:
        import boto3
        session = boto3.Session(profile_name=config.artifact_s3_profile,
                                region_name=config.artifact_s3_region)
        client = session.client("s3")
        client.head_bucket(Bucket=target.netloc)
        client.list_objects_v2(Bucket=target.netloc, Prefix=detail["prefix"], MaxKeys=1)
        detail["readable"] = True
    except Exception as exc:
        detail["readable"] = False
        detail["error"] = f"{type(exc).__name__}: {exc}"
    return detail


def main():
    parser = argparse.ArgumentParser(prog="multi-coding-harness-compare", description="multi-coding-harness-compare: compare native coding harnesses")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("start")
    sub.add_parser("open", help="Start services if needed and open an authenticated browser tab")
    sub.add_parser("worker")
    stop = sub.add_parser("stop", help="Stop this installation's API and worker (refuses while runs are in progress)")
    stop.add_argument("--force", action="store_true", help="Stop even if runs are in progress, interrupting them")
    sub.add_parser("doctor")
    inputs = sub.add_parser("inputs", help="Create deploy.properties if needed, apply --set answers, list what is missing")
    inputs.add_argument("--properties", metavar="PATH", help="deploy.properties to create or update")
    inputs.add_argument("--set", action="append", metavar="KEY=VALUE", help="answer to record (repeatable)")
    preflight = sub.add_parser("preflight",
                               help="Read-only prerequisite check of the configuration deploy.properties would produce")
    preflight.add_argument("--config", metavar="PATH", help="config.toml to evaluate (default ./config.toml, "
                           "else config.example.toml)")
    preflight.add_argument("--properties", metavar="PATH", help="deploy.properties to evaluate")
    preflight.add_argument("--offline", action="store_true", help="Skip the read-only AWS checks")
    from .setup_wizard import add_arguments as setup_arguments
    setup_arguments(sub.add_parser("setup",
        help="Write config.toml for this workstation; reports unresolved decisions instead of prompting"))
    agentcore_deploy = sub.add_parser("agentcore-deploy",
        help="Provision or update AgentCore runtime resources for all (or selected) harnesses")
    agentcore_deploy.add_argument("--harness", action="append", dest="harnesses",
        metavar="HARNESS", help="Limit to this harness; repeatable (default: all)")
    agentcore_deploy.add_argument("--no-rebuild", action="store_false", dest="rebuild", default=True,
        help="Skip building and uploading the deployment package; re-register only")
    agentcore_deploy.add_argument("--approve", metavar="TOKEN", dest="approve_token",
        help="Approval token from `agentcore-deploy plan` (required for non-pinned harness sources)")
    agentcore_deploy.add_argument("--dry-run", action="store_true",
        help="Resolve versions only; write nothing to AWS")
    agentcore_probe = sub.add_parser("agentcore-probe",
        help="Check the configured microVM paths are writable in each deployed runtime; run after every deploy")
    agentcore_probe.add_argument("--harness", action="append", dest="harnesses",
        metavar="HARNESS", help="Limit to this harness; repeatable (default: all)")
    sub.add_parser("demo")
    sub.add_parser("archive-artifacts", help="Archive existing artifacts while the worker is stopped")
    archive = sub.add_parser("archive", help="Retry failed S3 storage without rerunning coding or evaluation")
    archive.add_argument("comparison_id")
    restore = sub.add_parser("import-comparison", help="Recover comparison metadata from the configured S3 prefix")
    restore.add_argument("comparison_id")
    retry = sub.add_parser("resume")
    retry.add_argument("comparison_id")
    reevaluate = sub.add_parser("reevaluate")
    reevaluate.add_argument("comparison_id")
    reevaluate.add_argument("--spec-version", type=int)
    reverify = sub.add_parser("reverify", help="Run independent checks again on saved code")
    reverify.add_argument("comparison_id")
    reverify.add_argument("--plan", help="Corrected validation-plan JSON, preserved as an evaluation revision")
    reverify.add_argument("--reason", help="Why the validation plan is being corrected")
    export = sub.add_parser("export")
    export.add_argument("comparison_id")
    export.add_argument("--output")
    args = parser.parse_args()
    if args.command == "inputs":
        from .preflight import inputs_main
        raise SystemExit(inputs_main(args))
    if args.command == "preflight":
        # Like setup, preflight runs before a usable configuration exists.
        from .preflight import main as run_preflight
        raise SystemExit(run_preflight(args))
    if args.command == "setup":
        # Setup runs before a config.toml exists, and has to stay usable when the one
        # that exists is invalid, so it must not depend on loading it.
        from .setup_wizard import run as run_setup
        raise SystemExit(run_setup(args))
    config = load_config()
    if args.command in ("serve", "worker"):
        from .service_logs import configure_logs
        configure_logs(config)
    if args.command == "start":
        start_background(config)
    elif args.command == "stop":
        raise SystemExit(stop_services(config, force=args.force))
    elif args.command == "open":
        start_background(config, open_browser=True)
    elif args.command == "serve":
        import uvicorn
        from .auth import access_token
        print(f"Open locally: http://{config.host}:{config.port}/#token={access_token(config.root)}", flush=True)
        uvicorn.run("arena.api:create_app", factory=True, host=config.host, port=config.port,
                    timeout_graceful_shutdown=5)
    elif args.command == "worker":
        from .worker import Worker
        asyncio.run(Worker(config).serve())
    elif args.command == "doctor":
        asyncio.run(doctor())
    elif args.command == "archive-artifacts":
        from .worker import Worker
        worker = Worker(config)
        with (config.root / "worker.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error("Stop the worker first; the running worker also archives leftovers before its next comparison.")
            asyncio.run(worker.recovery())
            worker.recover_artifacts()
        print("Existing artifacts verified in S3 and temporary files removed.")
    elif args.command == "archive":
        from .service import Service
        Service(config).retry_archive(args.comparison_id)
        print("S3 storage retry queued; coding and evaluation will not run.")
    elif args.command == "import-comparison":
        from .service import Service
        result = Service(config).storage.import_comparison(args.comparison_id)
        print(json.dumps({"comparison_id": result["id"], "status": result["status"]}))
    elif args.command == "agentcore-deploy":
        from .agentcore_provision import deploy
        # Load raw properties so H_HARNESS_SOURCE / H_HARNESS_VERSION /
        # HARNESS_IMAGE_WORKSTATION_CONFIG are forwarded to harness_image.deploy.deploy().
        try:
            from .config import resolve_config as _resolve
            _, _cfg_meta = _resolve()
            _props = _cfg_meta.get("properties", {})
        except Exception:
            _props = {}
        result = asyncio.run(deploy(config, harnesses=args.harnesses, rebuild=args.rebuild,
                                    approve_token=getattr(args, "approve_token", None),
                                    dry_run=getattr(args, "dry_run", False),
                                    props=_props))
        print(json.dumps(result, indent=2))
    elif args.command == "agentcore-probe":
        from .agentcore_sandbox import AgentCore
        result = asyncio.run(AgentCore(config).probe(args.harnesses))
        print(json.dumps(result, indent=2))
        if not result.get("ok"):
            raise SystemExit(1)
    elif args.command == "demo":
        from .service import Service
        from .models import ModelCatalogue
        from .judge_models import JudgeModelCatalogue
        service = Service(config)
        catalogue = ModelCatalogue(config, service.store)
        judge_catalogue = JudgeModelCatalogue(config, service.store)
        asyncio.run(asyncio.gather(catalogue.get(refresh=True), judge_catalogue.get(refresh=True)))
        demo = json.loads((Path(__file__).parent.parent / "examples" / "demo-task.json").read_text())
        demo["repository"] = str(Path(__file__).parent.parent / demo["repository"])
        comparison = service.create(**demo)
        print(json.dumps({"comparison_id": comparison["id"],
                          "runs": [{"harness": r["harness"], "id": r["id"]} for r in comparison["runs"]]}, indent=2))
    elif args.command == "export":
        from .service import Service
        data = json.dumps(Service(config).report(args.comparison_id), indent=2)
        if args.output:
            Path(args.output).write_text(data)
        else:
            print(data)
    elif args.command == "resume":
        from .service import Service
        Service(config).retry_comparison(args.comparison_id)
        print("Incomplete runs queued for concurrent session continuation.")
    elif args.command == "reevaluate":
        from .service import Service
        Service(config).reevaluate(args.comparison_id, args.spec_version)
        print("Stored candidates queued for evaluation; coding harnesses will not run.")
    elif args.command == "reverify":
        from .service import Service
        plan = json.loads(Path(args.plan).read_text()) if args.plan else None
        Service(config).reverify(args.comparison_id, plan, args.reason)
        print("Saved code queued for fresh verification; coding harnesses will not run.")


if __name__ == "__main__":
    main()
