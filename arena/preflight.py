# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read-only prerequisite check, run before setup writes anything.

Evaluates the configuration deploy.properties WOULD produce (with the existing config.toml,
or config.example.toml on a fresh checkout) and checks every prerequisite: inputs, host,
harness CLIs and logins, and — unless --offline — AWS identity, S3, Bedrock models and the
AgentCore runtimes. Nothing is created or modified, here or in AWS.

Each check reports status pass|warn|fail|skip, the detail, the exact fix, and who must act:
"assistant" (an AI assistant can fix it), "human" (a person must decide or act), "deploy"
(a person must authorize agentcore-deploy), or "none".
"""
import functools
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import properties as props
from .agentcore_provision import runtime_name as _runtime_name

PLACEHOLDERS = ("YOUR_", "/absolute/path/to/")
FREE_BYTES_WANTED = 3 * 1024 ** 3
NETWORK_FILESYSTEMS = {"nfs", "smbfs", "afpfs", "webdav", "cifs", "ftp", "autofs"}


def placeholders(path: Path) -> list[str]:
    """Keys whose own value is still an example placeholder.

    Judged on the value as written, so a key that only references a placeholder
    (ARTIFACT_S3_URI = s3://${S3_BUCKET}/...) is not asked for separately.
    """
    literal = props.parse(Path(path).read_text(encoding="utf-8"), str(path), resolve=False)
    return sorted(name for name, value in literal.items() if any(mark in value for mark in PLACEHOLDERS))


@functools.lru_cache(maxsize=1)
def shell_restrictions() -> tuple[str, ...]:
    """What this shell may not do that native supervision and evaluation need.

    An AI assistant's own sandbox typically refuses a nested Seatbelt sandbox, or running
    the setuid /bin/ps that native supervision reads process trees with.
    """
    if sys.platform != "darwin":
        return ()
    from .native_sandbox import profile
    problems = []
    for name, argv in (("sandbox-exec", ["/usr/bin/sandbox-exec", "-p", profile(set(), set()), "/bin/echo", "ok"]),
                       ("ps", ["/bin/ps", "-p", str(os.getpid()), "-o", "pid="])):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
            if result.returncode:
                problems.append(f"{name} exit {result.returncode}: {(result.stderr or result.stdout).strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f"{name}: {exc}")
    return tuple(problems)


def _check(id, status, detail, fix="", gate="none", applies="all"):
    return {"id": id, "status": status, "detail": detail, "fix": fix,
            "gate": gate if status in ("fail", "warn") else "none", "applies": applies}


class Preflight:
    def __init__(self, config_path: Path, properties_path: Path, *, offline: bool = False):
        self.config_path, self.properties_path, self.offline = config_path, properties_path, offline
        self.checks, self.config, self.values, self.account = [], None, {}, None
        self._config_report: dict = {}

    def add(self, *args, **kwargs):
        self.checks.append(_check(*args, **kwargs))

    # ── inputs ────────────────────────────────────────────────────────────────
    def inputs(self) -> bool:
        path = self.properties_path
        if not path.is_file():
            from shlex import quote
            self.add("properties-file", "fail", f"{path} does not exist",
                     f"cp deploy.properties.example {quote(str(path))}, then fill in every YOUR_ value", "assistant")
            return False
        try:
            self.values = props.load(path).values
        except props.PropertiesError as exc:
            self.add("properties-file", "fail", str(exc), "correct that line in deploy.properties", "assistant")
            return False
        self.add("properties-file", "pass", str(path))
        unfilled = placeholders(path)
        if unfilled:
            self.add("properties-filled", "fail", "still placeholders: " + ", ".join(unfilled),
                     "ask the human for these values (see docs/launch-runbook.md, Inputs) and set them "
                     "with ./bootstrap.sh --set KEY=VALUE", "human")
            return False
        self.add("properties-filled", "pass", "no placeholder values remain")
        # A fresh checkout has no config.toml yet: evaluate what setup would write.
        source = self.config_path if self.config_path.is_file() else Path(__file__).parent.parent / "config.example.toml"
        try:
            from .config import resolve_config
            self.config, self._config_report = resolve_config(self.config_path, text=source.read_text(),
                                                              properties=props.load(path))
        except Exception as exc:
            gate = getattr(exc, "gate", "assistant")
            fix_text = (getattr(exc, "fix", None) or
                        "correct the named value in deploy.properties (or config.toml)")
            self.add("configuration-loads", "fail", f"{type(exc).__name__}: {exc}",
                     fix_text, gate)
            return False
        detail = f"{source.name} + {path.name}"
        if self._config_report["overridden"]:
            detail += "; config.toml literals override: " + ", ".join(
                o["field"] for o in self._config_report["overridden"])
        self.add("configuration-loads", "pass", detail)
        if self._config_report["unused"]:
            self.add("properties-unused", "warn",
                     "unknown keys nothing refers to: " + ", ".join(self._config_report["unused"]),
                     "remove or correct them (a typo binds nothing)", "assistant")
        cfg = self.config
        import tomllib
        explicit = tomllib.loads(source.read_text())
        kiro_literal = "refresh_files" in explicit.get("harnesses", {}).get("kiro", {})
        for name, ok, fix in (
                ("RUNTIME", self.values.get("RUNTIME") in props.RUNTIMES or explicit.get("runtime") in props.RUNTIMES,
                 "set RUNTIME = agentcore (recommended) or native"),
                ("KIRO_AUTH", self.values.get("KIRO_AUTH") in props.KIRO_AUTHS or kiro_literal,
                 "set KIRO_AUTH = copy-host-login or login-in-home")):
            self.add(f"choice-{name.lower().replace('_', '-')}", "pass" if ok else "fail",
                     f"{name} = {self.values.get(name, '(from config.toml)' if ok else '')}", fix, "human")
        self.add("artifact-s3-uri", "pass" if cfg.artifact_s3_uri else "fail",
                 cfg.artifact_s3_uri or "ARTIFACT_S3_URI is not set; no comparison can finish without it",
                 "set S3_BUCKET (and ARTIFACT_S3_URI) to a bucket this profile can write", "human")
        if cfg.runtime == "agentcore":
            self.add("agentcore-transfer-s3-uri", "pass" if cfg.agentcore.transfer_s3_uri else "fail",
                     cfg.agentcore.transfer_s3_uri or "AGENTCORE_TRANSFER_S3_URI is not set",
                     "set AGENTCORE_TRANSFER_S3_URI (default s3://${S3_BUCKET}/code/ai-coding-assistant-agcore-runtime/)",
                     "assistant", "agentcore")
        missing = [root for root in cfg.repository_roots if not Path(root).expanduser().is_dir()]
        self.add("repository-roots", "fail" if missing else "pass",
                 ("missing: " + ", ".join(missing)) if missing else ", ".join(cfg.repository_roots),
                 "create the directory (mkdir -p PATH) or correct REPOSITORY_ROOTS", "assistant")
        self.add("judge-model", "pass" if cfg.judge_model else "warn",
                 cfg.judge_model or "JUDGE_MODEL unset: scoring and acceptance-test generation are unavailable",
                 "set JUDGE_MODEL to a Bedrock inference profile enabled in AWS_REGION", "human")
        return True

    # ── host ──────────────────────────────────────────────────────────────────
    def host(self):
        macos = sys.platform == "darwin"
        self.add("host-macos", "pass" if macos else "fail", sys.platform,
                 "use a macOS workstation (including EC2 Mac); see docs/launch-runbook.md, Windows", "human")
        sandbox = Path("/usr/bin/sandbox-exec").exists()
        self.add("host-sandbox-exec", "pass" if sandbox else "fail", "/usr/bin/sandbox-exec",
                 "xcode-select --install", "human")
        if macos and sandbox:
            problems = shell_restrictions()
            self.add("host-nested-sandbox", "warn" if problems else "pass",
                     "this shell is sandboxed: " + "; ".join(problems) if problems else
                     "macOS Seatbelt and process inspection available",
                     "a human must run `.venv/bin/multi-coding-harness-compare start` in Terminal.app; "
                     "every other step works from here", "human")
        self.add("host-git", "pass" if shutil.which("git") else "fail", shutil.which("git") or "git not found",
                 "xcode-select --install", "human")
        if self.config is None:
            return
        root = self.config.root
        probe = next((p for p in (root, *root.parents) if p.exists()), Path("/"))
        fstype = _fstype(probe)
        synced = _synced_folder(root)
        if fstype in NETWORK_FILESYSTEMS:
            self.add("data-dir-local", "fail", f"{root} is on {fstype}",
                     "set DATA_DIR to a directory on a local disk", "assistant")
        elif synced:
            self.add("data-dir-local", "warn", f"{root} is inside {synced}",
                     "set DATA_DIR to a local folder outside cloud-synced locations", "assistant")
        else:
            self.add("data-dir-local", "pass", f"{root} ({fstype or 'local'})")
        free = shutil.disk_usage(probe).free
        self.add("disk-space", "pass" if free >= FREE_BYTES_WANTED else "warn", f"{free // 1024 ** 3} GB free",
                 "free at least 3 GB on the volume holding the checkout and DATA_DIR", "human")
        from .cli import _own_service_pids
        port, owner = self.config.port, _own_service_pids(self.config).get("serve")
        if _port_free(self.config.host, port):
            self.add("service-port", "pass", f"{self.config.host}:{port} is free")
        elif owner:
            self.add("service-port", "pass", f"{port} is held by this installation's own API (pid {owner})")
        else:
            self.add("service-port", "fail", f"{self.config.host}:{port} is in use by another process",
                     "set SERVICE_PORT to a free port", "assistant")

    # ── harnesses ─────────────────────────────────────────────────────────────
    def harnesses(self):
        if self.config is None:
            return
        from .host_setup import authentication_plan
        cfg = self.config
        for name in ("claude", "codex", "kiro"):
            try:
                plan = authentication_plan(cfg, name)
            except Exception as exc:
                plan = {"blocked": f"{type(exc).__name__}: {exc}", "method": "unknown"}
            blocked = plan.get("blocked")
            self.add(f"{name}-authentication", "fail" if blocked else "pass",
                     blocked or plan.get("method", ""), blocked or "", "human")
        if cfg.runtime == "native":
            for name, executable in (("codex", "codex"), ("kiro", "kiro-cli")):
                found = cfg.harnesses[name].cli_path or shutil.which(executable)
                self.add(f"{name}-cli", "pass" if found else "fail", found or f"{executable} not on PATH",
                         f"install {executable}, or set {name.upper()}_CLI_PATH in deploy.properties",
                         "human", "native")
        else:
            self.codex_provider()

    def drift(self):
        """Cached runtime records from another region, account, name prefix or transfer prefix.

        The account is AWS_ACCOUNT_ID, else the one the profile reaches (when AWS was checked).
        """
        from .cli import configuration_drift
        account = self.values.get("AWS_ACCOUNT_ID") or self.account
        issues = configuration_drift(self.config, {**self.values, **({"AWS_ACCOUNT_ID": account} if account else {})})
        self.add("agentcore-drift", "fail" if issues else "pass",
                 "; ".join(issues) or "cached runtime records match this configuration",
                 "the human decides: authorize agentcore-deploy for this configuration (./bootstrap.sh "
                 "--deploy), or name the previous value to restore with ./bootstrap.sh --set KEY=VALUE",
                 "deploy", "agentcore")

    def codex_provider(self):
        """Under agentcore, Codex's provider config must select Bedrock with no profile.

        When the effective configuration choice is 'default', the workstation's
        ~/.codex/config.toml (or CODEX_CONFIG_DIR) IS copied, and the existing check
        validates it.  When the effective choice is 'overrides' or 'none', the
        workstation folder is NOT copied; the config must come from CODEX_HOME_TEMPLATE
        (or CODEX_CONFIG_S3_URI).
        """
        import tomllib
        effective_choice = self.config.default_configuration_choice
        template = self.config.harnesses["codex"].home_template
        config_dir = self.config.harnesses["codex"].config_dir
        if template:
            source = Path(template) / ".codex" / "config.toml"
        elif effective_choice == "default":
            # Workstation folder is read — check there.
            _codex_home = (config_dir or os.environ.get("CODEX_HOME")
                           or str(Path.home() / ".codex"))
            source = Path(_codex_home).expanduser() / "config.toml"
        else:
            # Workstation folder NOT read — require CODEX_HOME_TEMPLATE (or S3 URI).
            config_s3_uri = self.config.harnesses["codex"].config_s3_uri
            if config_s3_uri:
                # S3 prefix can deliver the config; nothing to check here locally.
                self.add("codex-provider-config", "pass",
                         f"codex config will be delivered via S3 prefix {config_s3_uri}",
                         applies="agentcore")
                return
            # No template, no S3, choice != default → must fail.
            region = self.config.agentcore.region
            dedicated = self.properties_path.parent.resolve() / ".arena" / "credential-templates" / "codex"
            from shlex import quote
            region_line = quote(f'region = "{region}"')
            fix = (f"HARNESS_CONFIG_DEFAULT_CHOICE={effective_choice!r} means the workstation's "
                   f"~/.codex is not copied. Create a dedicated template: "
                   f"mkdir -p {quote(str(dedicated / '.codex'))} && printf '%s\\n' "
                   f"'model_provider = \"amazon-bedrock-runtime\"' '[model_providers.amazon-bedrock-runtime.aws]' "
                   f"{region_line} 'wire_api = \"responses\"' 'namespace_tools = false' "
                   f"> {quote(str(dedicated / '.codex' / 'config.toml'))} "
                   f"&& ./bootstrap.sh --set CODEX_HOME_TEMPLATE={quote(str(dedicated))}")
            self.add("codex-provider-config", "fail",
                     f"Codex on agentcore with {effective_choice!r} choice: "
                     "no CODEX_HOME_TEMPLATE provides a provider config",
                     fix, "assistant", "agentcore")
            return
        region = self.config.agentcore.region
        dedicated = self.properties_path.parent.resolve() / ".arena" / "credential-templates" / "codex"
        from shlex import quote
        region_line = quote(f'region = "{region}"')
        _create_cmd = (
            f"mkdir -p {quote(str(dedicated / '.codex'))} && printf '%s\\n' "
            f"'model_provider = \"amazon-bedrock-runtime\"' '[model_providers.amazon-bedrock-runtime.aws]' "
            f"{region_line} 'wire_api = \"responses\"' 'namespace_tools = false' "
            f"> {quote(str(dedicated / '.codex' / 'config.toml'))} "
            f"&& ./bootstrap.sh --set CODEX_HOME_TEMPLATE={quote(str(dedicated))}"
        )
        if template:
            if not source.is_file():
                # CODEX_HOME_TEMPLATE is set but the .codex/config.toml is missing.
                # Provide an executable fix that creates the canonical dedicated template.
                self.add("codex-provider-config", "fail",
                         f"CODEX_HOME_TEMPLATE={template}: {source} does not exist",
                         f"Create a dedicated template: {_create_cmd}",
                         "assistant", "agentcore")
                return
            # File exists — provide a prose fix so we never overwrite the user's file.
            fix = (f"{source} is a dedicated template: make it select model_provider = \"amazon-bedrock-runtime\" "
                   f"with [model_providers.amazon-bedrock-runtime.aws] region = \"{region}\" and no profile key "
                   "(the microVM has no ~/.aws; the execution role supplies credentials)")
        else:
            # Never the host's own file: the workstation's Codex keeps using it, profile included.
            fix = (f"do not edit {source}; this workstation's own Codex uses it. Create a dedicated template: "
                   f"{_create_cmd}")
        if not source.is_file():
            self.add("codex-provider-config", "fail", f"{source} does not exist", fix, "assistant", "agentcore")
            return
        try:
            settings = tomllib.loads(source.read_text())
        except tomllib.TOMLDecodeError as exc:
            self.add("codex-provider-config", "fail", f"{source}: {exc}", fix, "assistant", "agentcore")
            return
        provider = settings.get("model_provider", "")
        aws = settings.get("model_providers", {}).get(provider, {}).get("aws", {})
        if not provider.startswith("amazon-bedrock"):
            self.add("codex-provider-config", "fail", f"{source}: model_provider = {provider!r}", fix, "assistant", "agentcore")
        elif "profile" in aws:
            self.add("codex-provider-config", "fail", f"{source}: [{provider}.aws] has a profile key", fix,
                     "assistant", "agentcore")
        else:
            self.add("codex-provider-config", "pass", f"{source}: {provider}", applies="agentcore")

    # ── AWS (read-only calls) ─────────────────────────────────────────────────
    def aws(self):
        if self.config is None:
            return
        if self.offline:
            self.add("aws", "skip", "--offline: AWS checks not run")
            return
        import boto3
        from botocore.config import Config as BotoConfig
        cfg = self.config
        boto = BotoConfig(connect_timeout=5, read_timeout=20, retries={"total_max_attempts": 2})
        profile = cfg.agentcore.profile if cfg.runtime == "agentcore" else cfg.artifact_s3_profile
        try:
            session = boto3.Session(profile_name=profile, region_name=cfg.agentcore.region)
            identity = session.client("sts", config=boto).get_caller_identity()
            self.account = identity["Account"]
            self.add("aws-identity", "pass", f"{identity['Arn']} (profile {profile or 'ambient chain'})")
        except Exception as exc:
            self.add("aws-identity", "fail", f"profile {profile!r}: {_error(exc)}",
                     f"refresh the credentials for AWS profile {profile!r} (for example `aws sso login "
                     f"--profile {profile}`), or correct AWS_PROFILE", "human")
            return
        expected = self.values.get("AWS_ACCOUNT_ID")
        if expected and expected != self.account:
            self.add("aws-account", "fail", f"profile reaches {self.account}, AWS_ACCOUNT_ID is {expected}",
                     "point AWS_PROFILE at the account that owns the bucket and runtimes, or correct AWS_ACCOUNT_ID", "human")
        else:
            self.add("aws-account", "pass" if expected else "warn",
                     self.account if expected else f"the profile reaches {self.account}; AWS_ACCOUNT_ID is not set",
                     f"optional: ./bootstrap.sh --set AWS_ACCOUNT_ID={self.account} lets doctor flag cached "
                     "runtimes from another account", "assistant")
        self.s3(session, boto)
        self.bedrock(session, boto)
        if cfg.runtime == "agentcore":
            self.agentcore(session, boto)

    def s3(self, session, boto):
        from urllib.parse import urlsplit
        cfg = self.config
        s3 = session.client("s3", region_name=cfg.artifact_s3_region, config=boto)
        # agentcore-deploy uploads bundles with ExpectedBucketOwner, so the transfer bucket must
        # belong to this account; the artifact bucket may be shared across accounts.
        targets = [("s3-artifact", cfg.artifact_s3_uri, "all", {})]
        if cfg.runtime == "agentcore":
            targets.append(("s3-transfer", cfg.agentcore.transfer_s3_uri, "agentcore",
                            {"ExpectedBucketOwner": self.account}))
        for id, uri, applies, owner in targets:
            if not uri:
                continue
            parts = urlsplit(uri)
            # Transfer bucket needs versioned-deletion permissions for the secret channel.
            is_transfer = id == "s3-transfer"
            kms = (self.config.agentcore.transfer_kms_key_arn
                   if is_transfer and hasattr(self.config, "agentcore") else None)
            if is_transfer:
                extra = (f", kms:Decrypt, kms:GenerateDataKey on {kms}" if kms else "")
                fix = ("create the bucket in this account (enable versioning), or grant this profile: "
                       "s3:ListBucket, s3:GetObject, s3:PutObject, s3:DeleteObject, "
                       "s3:GetObjectVersion, s3:DeleteObjectVersion, s3:ListBucketVersions "
                       "and multipart upload on the prefix" + extra)
            else:
                fix = ("create the bucket in this account, or grant this profile: "
                       "s3:ListBucket, s3:GetObject, s3:PutObject, s3:DeleteObject "
                       "and multipart upload on the prefix")
            try:
                s3.head_bucket(Bucket=parts.netloc, **owner)
                s3.list_objects_v2(Bucket=parts.netloc, Prefix=parts.path.lstrip("/"), MaxKeys=1)
                self.add(id, "pass", f"{uri} reachable", applies=applies)
            except Exception as exc:
                self.add(id, "fail", f"{uri}: {_error(exc)}", fix, "human", applies)

    def bedrock(self, session, boto):
        cfg = self.config
        # Each model is checked in the region it is invoked from: the judge and planner from
        # judge_region, harness models from the AgentCore region.
        wanted = {"judge": (cfg.judge_model, cfg.judge_region), "planning": (cfg.planning_model, cfg.judge_region)}
        if cfg.runtime == "agentcore":
            wanted |= {name: (cfg.harnesses[name].model, cfg.agentcore.region) for name in ("claude", "codex")}
        wanted = {role: pair for role, pair in wanted.items() if pair[0]}
        active = {}
        for role, (model, region) in wanted.items():
            if region not in active:
                try:
                    client = session.client("bedrock", region_name=region, config=boto)
                    active[region] = {s["inferenceProfileId"] for page in
                                      client.get_paginator("list_inference_profiles").paginate()
                                      for s in page.get("inferenceProfileSummaries", []) if s.get("status") == "ACTIVE"}
                except Exception as exc:
                    self.add("bedrock-models", "fail", f"{region}: {_error(exc)}",
                             "grant bedrock:ListInferenceProfiles to this profile", "human")
                    return
            base = model.removesuffix("[1m]")
            # Only inference-profile IDs (a geography prefix) are listed; other IDs are not checked here.
            listed = base in active[region] or not base.startswith(("us.", "global.", "eu.", "apac.", "jp.", "au."))
            self.add(f"bedrock-model-{role}", "pass" if listed else "fail",
                     f"{model} {'is' if listed else 'is not'} an active inference profile in {region}",
                     "enable access to this model for the account in the Bedrock console, or choose a "
                     "listed inference profile", "human")

    def agentcore(self, session, boto):
        cfg = self.config.agentcore
        # Per-user isolation without USER_NAMESPACE would silently fall back to
        # shared runtime names, defeating isolation.  Fail clearly here.
        isolation = getattr(cfg, "isolation", "shared")
        user_namespace = getattr(self.config, "user_namespace", None)
        if isolation == "per-user" and not user_namespace:
            self.add("agentcore-runtimes", "fail",
                     "AGENTCORE_ISOLATION=per-user requires USER_NAMESPACE to be set",
                     "add USER_NAMESPACE=<your-namespace> to deploy.properties "
                     "(e.g. USER_NAMESPACE=alice)", "human", "agentcore")
            return
        namespace = user_namespace if isolation == "per-user" else None
        try:
            control = session.client("bedrock-agentcore-control", region_name=cfg.region, config=boto)
            names = set()
            token = None
            while True:
                page = control.list_agent_runtimes(**({"nextToken": token} if token else {}))
                names |= {r.get("agentRuntimeName") for r in page.get("agentRuntimes", [])}
                token = page.get("nextToken")
                if not token:
                    break
        except Exception as exc:
            self.add("agentcore-runtimes", "fail", _error(exc),
                     "grant bedrock-agentcore:ListAgentRuntimes to this profile", "human", "agentcore")
            return
        try:
            expected = [_runtime_name(cfg.runtime_name_prefix, h, namespace) for h in ("claude", "codex", "kiro")]
        except ValueError as exc:
            # A bad AGENTCORE_RUNTIME_NAME_PREFIX surfaces here as a configuration problem,
            # not a preflight defect.  Emit a clear human-gate check with the fix.
            prefix = cfg.runtime_name_prefix or ""
            suggestion = __import__("re").sub(r'[^A-Za-z0-9_]', '_', prefix)
            if suggestion and not suggestion[0].isalpha():
                suggestion = "a_" + suggestion.lstrip("_")
            self.add("agentcore-runtime-names", "fail",
                     str(exc),
                     f"correct AGENTCORE_RUNTIME_NAME_PREFIX: use only letters, digits and underscores "
                     f"(no hyphens); e.g. {suggestion!r}",
                     "human", "agentcore")
            return
        missing = [name for name in expected if name not in names]
        self.add("agentcore-runtimes", "fail" if missing else "pass",
                 ("not deployed: " + ", ".join(missing)) if missing else "deployed: " + ", ".join(expected),
                 "a human must authorize creating them (an IAM role, S3 objects and three runtimes); then "
                 "run ./bootstrap.sh --deploy", "deploy", "agentcore")
        # Check for leftover secret objects from previous sessions.
        try:
            import asyncio
            from .agentcore_sandbox import AgentCore
            leftovers = asyncio.run(AgentCore(self.config).list_leftover_secrets())
            count = len(leftovers)
            if count:
                self.add("agentcore-secret-leftovers", "warn",
                         f"{count} leftover secret object{'s' if count != 1 else ''} in S3 "
                         f"(credential bundle not deleted from a previous session)",
                         "inspect with the doctor command; manually delete objects under the "
                         "secrets/ prefix in the transfer bucket to clear them",
                         "human", "agentcore")
            else:
                self.add("agentcore-secret-leftovers", "pass",
                         "no leftover secret objects found in S3", applies="agentcore")
        except Exception as exc:
            self.add("agentcore-secret-leftovers", "warn",
                     f"could not check for leftover secrets: {_error(exc)}", applies="agentcore")

    # ── new checks ────────────────────────────────────────────────────────────
    def configuration_literals(self):
        """Fail when config.toml has plain literals for environment-specific fields.

        These values belong in deploy.properties so different workstations can use
        different values without editing the committed config.toml.
        """
        if not self._config_report:
            return
        from .config import environment_literal_fields
        literals = environment_literal_fields(self._config_report)
        # AUTO_APPROVE records the human's answer. A config.toml literal that differs from it
        # (configurations made before the key existed set auto_approve = true per harness)
        # still wins, and copying that literal into deploy.properties, as the fix below does
        # for other keys, would overturn the answer. The human decides which value stays.
        approvals = [item for item in literals if item["property"] == "AUTO_APPROVE"]
        literals = [item for item in literals if item["property"] != "AUTO_APPROVE"]
        if approvals:
            found = ", ".join(f'{item["field"]} = {str(item["value"]).lower()}' for item in approvals)
            self.add("auto-approve-overridden", "fail",
                     f"config.toml sets {found}, which overrides AUTO_APPROVE = "
                     f"{self.values.get('AUTO_APPROVE')} in deploy.properties",
                     "choose which value to keep: to apply AUTO_APPROVE, delete those auto_approve "
                     "lines from config.toml (configurations made before AUTO_APPROVE existed set "
                     "them to true); to keep config.toml's value, set AUTO_APPROVE to it with "
                     "./bootstrap.sh --set AUTO_APPROVE=true or --set AUTO_APPROVE=false",
                     "human")
        elif not self.values.get("AUTO_APPROVE") and self.config is not None:
            # A deploy.properties made before AUTO_APPROVE existed has no answer, so a
            # config.toml literal of that time (true) would keep approving unasked.
            legacy = [f"harnesses.{name}.auto_approve = true"
                      for name, harness in self.config.harnesses.items() if harness.auto_approve]
            if legacy:
                self.add("auto-approve-overridden", "fail",
                         f"config.toml sets {', '.join(legacy)}, and deploy.properties has no "
                         "AUTO_APPROVE answer (default false)",
                         "choose a value: to keep automatic approval, ./bootstrap.sh --set "
                         "AUTO_APPROVE=true; to have prompts come to you, delete those auto_approve "
                         "lines from config.toml and ./bootstrap.sh --set AUTO_APPROVE=false",
                         "human")
        if not literals:
            self.add("config-toml-environment-literals", "pass",
                     "no environment-specific values are hardcoded in config.toml")
            return
        fields = ", ".join(item["field"] for item in literals)
        # Each fix command sets one key; the value is shown so the operator can copy it as-is.
        sets = " ".join(
            f'--set {item["property"]}={item["value"]}' for item in literals)
        self.add("config-toml-environment-literals", "fail",
                 f"config.toml has plain literals for environment fields: {fields}",
                 f"move each value to deploy.properties: ./bootstrap.sh {sets}",
                 "assistant")

    def aws_region(self):
        """Fail when no AWS region is configured in any region field."""
        if self.config is None:
            return
        ac = getattr(self.config, "agentcore", None)
        regions = [
            getattr(ac, "region", None) if ac else None,
            getattr(self.config, "artifact_s3_region", None),
            getattr(self.config, "judge_region", None),
        ]
        configured = next((r for r in regions if r), None)
        if configured:
            self.add("aws-region", "pass", f"AWS region configured: {configured}")
            return
        self.add("aws-region", "fail",
                 "no AWS region configured (all region fields are None; "
                 "check AWS_REGION env var or set it in deploy.properties)",
                 "./bootstrap.sh --set AWS_REGION=us-east-1  (replace with your actual region)",
                 "assistant")

    def harness_image_versions(self):
        """Gate on non-pinned harness version sources that have no approved lock entry."""
        if self.config is None:
            return
        try:
            from harness_image.sources import read_lock
        except ImportError:
            return  # harness_image package not present; skip gracefully
        lock = read_lock(self.config.root)
        non_pinned = []
        for harness in ("claude", "codex", "kiro"):
            source = self.values.get(f"{harness.upper()}_HARNESS_SOURCE") or "pinned"
            if source != "pinned":
                non_pinned.append((harness, source))
        if not non_pinned:
            self.add("harness-image-versions", "pass",
                     "all harnesses use pinned versions (no approval required)")
            return
        missing_approval = [(h, s) for h, s in non_pinned
                            if not (h in lock and lock[h].get("token"))]
        if not missing_approval:
            locked = "; ".join(
                f"{h}: {lock[h].get('version', '?')} ({s})" for h, s in non_pinned)
            self.add("harness-image-versions", "pass",
                     f"non-pinned sources have approved plans: {locked}")
            return
        detail = "; ".join(f"{h}: source={s} (no approved plan)" for h, s in missing_approval)
        self.add("harness-image-versions", "fail",
                 detail,
                 "run: deploy/agentcore-deploy.sh plan   to resolve versions and get the approval "
                 "token; then: ./bootstrap.sh --approve TOKEN --deploy",
                 "human")

    def harness_configuration(self):
        """Warn about withheld secrets and MCP entries with secret-looking env keys.

        Also reports the effective default configuration choice and whether the
        workstation folder (CLAUDE_CONFIG_DIR etc.) will be read for new runs.
        """
        if self.config is None:
            return
        from .host_setup import authentication_plan, configuration_plan
        effective_choice = self.config.default_configuration_choice
        reads_ws = (effective_choice == "default")
        parts = []
        for name in ("claude", "codex", "kiro"):
            try:
                auth = authentication_plan(self.config, name)
            except Exception:
                auth = {}
            withheld = auth.get("secrets_withheld", [])
            if withheld:
                parts.append(f"{name}: withheld from HOME copy: {', '.join(withheld)}")
            try:
                cfg_plan = configuration_plan(self.config, name)
            except Exception:
                cfg_plan = {}
            for source in cfg_plan.get("sources", []):
                for server, key_names in source.get("mcp_secret_names", {}).items():
                    parts.append(
                        f"{name}/{server}: MCP env keys with secret-looking names: "
                        f"{', '.join(key_names)}")
        choice_detail = (
            f"default configuration choice: {effective_choice!r}; "
            f"workstation folder {'read' if reads_ws else 'NOT read'} for new runs"
        )
        if not parts:
            self.add("harness-configuration", "pass",
                     f"no withheld secrets or secret-looking MCP env keys detected; {choice_detail}")
            return
        self.add("harness-configuration", "warn",
                 "; ".join(parts) + f"; {choice_detail}",
                 "MCP env-block secrets are copied into the harness HOME on every run; "
                 "use the AgentCore secret channel (docs/iam/) for credentials that must "
                 "not appear in the S3 transfer tarball",
                 "human")

    # ── home templates ────────────────────────────────────────────────────────
    def home_templates(self):
        """Fail (gate human) for every configured *_HOME_TEMPLATE that does not exist.

        A missing template would block the very first run; surface it at preflight
        time so the operator can create or correct the path before deploying.
        """
        if self.config is None:
            return
        for name in ("claude", "codex", "kiro"):
            tpl = self.config.harnesses[name].home_template
            if not tpl:
                continue
            p = Path(tpl).expanduser().resolve()
            key = f"{name.upper()}_HOME_TEMPLATE"
            if p.is_dir():
                self.add(f"{name}-home-template", "pass",
                         f"{key}={tpl!r} exists at {p}")
            else:
                self.add(f"{name}-home-template", "fail",
                         f"{key}={tpl!r} does not exist at {p}",
                         f"create {p} (or remove {key} from deploy.properties)",
                         "human")

    # ── overlay path resolution ───────────────────────────────────────────────
    def overlay_paths(self):
        """Warn (gate human) for configured overlay paths that expand to nothing.

        *_SKILLS_DIRS, *_STEERING_FILES, and *_MCP_CONFIG accept ~ and glob
        wildcards.  An entry that resolves to no existing file or directory is
        reported here so the operator can correct typos before the first run.
        """
        if self.config is None:
            return
        from .host_setup import _expand_globs
        all_unmatched = []
        for name in ("claude", "codex", "kiro"):
            cfg = self.config.harnesses[name]
            for key_suffix, values in (
                ("SKILLS_DIRS", getattr(cfg, "skills_dirs", None) or []),
                ("STEERING_FILES", getattr(cfg, "steering_files", None) or []),
                ("MCP_CONFIG", [getattr(cfg, "mcp_config", None)] if getattr(cfg, "mcp_config", None) else []),
            ):
                if not values:
                    continue
                _, unmatched = _expand_globs(list(values))
                for u in unmatched:
                    all_unmatched.append(f"{name.upper()}_{key_suffix}: {u!r} matches nothing")
        if all_unmatched:
            self.add("overlay-paths", "warn",
                     "; ".join(all_unmatched),
                     "correct the paths in deploy.properties or remove entries that are no longer needed",
                     "human")
        else:
            self.add("overlay-paths", "pass", "all configured overlay paths resolved")

    def guard(self, group, step) -> bool:
        """Run one group of checks; a defect in a check becomes a failed check, not a crash."""
        try:
            return step() is not False
        except Exception as exc:
            self.add(f"{group}-error", "fail", f"{type(exc).__name__}: {exc}"[:400],
                     "report this preflight defect with the log to the human; it is not a configuration problem",
                     "assistant")
            return False

    def run(self) -> dict:
        if self.guard("inputs", self.inputs):
            self.guard("configuration-literals", self.configuration_literals)
            self.guard("aws-region", self.aws_region)
            self.guard("harnesses", self.harnesses)
            self.guard("home-templates", self.home_templates)
            self.guard("overlay-paths", self.overlay_paths)
            self.guard("harness-image-versions", self.harness_image_versions)
            self.guard("harness-configuration", self.harness_configuration)
            self.guard("aws", self.aws)
            if self.config.runtime == "agentcore":
                self.guard("agentcore-drift", self.drift)
        self.guard("host", self.host)
        failed = [c for c in self.checks if c["status"] == "fail"]
        return {"ok": not failed,
                "config_file": str(self.config_path), "properties_file": str(self.properties_path),
                "runtime": self.config.runtime if self.config else None, "account": self.account,
                "needs_deploy": any(c["id"] in ("agentcore-runtimes", "agentcore-drift")
                                    and c["status"] == "fail" for c in self.checks),
                "nested_sandbox": any(c["id"] == "host-nested-sandbox" and c["status"] != "pass"
                                      for c in self.checks),
                "gates": sorted({c["gate"] for c in failed}),
                "failed": [c["id"] for c in failed],
                "checks": self.checks}


def _error(exc) -> str:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return f"{code or type(exc).__name__}: {str(exc)[:240]}"


def _fstype(path: Path) -> str:
    try:
        return subprocess.run(["stat", "-f", "%T", str(path)], capture_output=True, text=True,
                              timeout=5).stdout.strip().lower()
    except Exception:
        return ""


def _synced_folder(root: Path) -> str | None:
    text = str(root.resolve())
    home = str(Path.home())
    for marker in ("/Library/Mobile Documents/", "/Library/CloudStorage/", "/Dropbox/", "/OneDrive"):
        if marker in text:
            return marker.strip("/")
    for folder, key in (("Desktop", "FXICloudDriveDesktop"), ("Documents", "FXICloudDriveDocuments")):
        if text.startswith(f"{home}/{folder}/") and _defaults("com.apple.finder", key) == "1":
            return f"iCloud-synced ~/{folder}"
    return None


def _defaults(domain, key) -> str:
    try:
        return subprocess.run(["defaults", "read", domain, key], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


QUESTIONS = {
    "RUNTIME": "Where should the coding harnesses run: agentcore (Bedrock AgentCore microVMs; Claude Code "
               "and Codex use the runtime's execution role; recommended) or native (this Mac's Seatbelt sandbox)?",
    "KIRO_AUTH": "Should Kiro use this workstation's existing Kiro login (copy-host-login), or a login made "
                 "inside a dedicated HOME (login-in-home)?",
    "AWS_PROFILE": "Which AWS profile reaches the account holding the S3 bucket, the Bedrock models and the "
                   "AgentCore runtimes?",
    "AWS_REGION": "Which AWS region holds the bucket, the Bedrock models and the AgentCore runtimes (for example us-east-1)?",
    "S3_BUCKET": "Which S3 bucket, owned by that account, stores comparison artifacts and AgentCore transfers?",
    "REPOSITORY_ROOTS": "Which directory (or comma-separated directories) holds the projects comparisons may run against?",
    "USER_NAMESPACE": ("What short lowercase identifier (letters, digits, hyphens) should namespace your "
                       "per-user AgentCore resources (required when AGENTCORE_ISOLATION=per-user; "
                       "auto-derived from your login name when possible)?"),
    # Not a placeholder, so bootstrap does not stop for it: the assistant asks it with the
    # inputs above (AGENTS.md, docs/launch-runbook.md).
    "AUTO_APPROVE": ("Should the coding harnesses' own permission prompts be answered automatically inside "
                     "each run's sandbox (true, recommended: Codex's auto-review agent answers Codex's, "
                     "Kiro's are allowed, and Claude Code's auto mode still sends you what it escalates) "
                     "or all come to you as approval cards (false, the default)?"),
}


def inputs(properties_path: Path, pairs: list[str]) -> tuple[int, dict]:
    """Create deploy.properties from the example if needed, apply KEY=VALUE answers, report gaps.

    Returns (exit code, report): 0 when no placeholder remains, 20 when answers are missing
    (each with the question to ask), 2 when an answer or the file is malformed.
    """
    import re
    created = False
    if not properties_path.is_file():
        example = Path(__file__).parent.parent / "deploy.properties.example"
        properties_path.write_text(example.read_text())
        created = True
    try:
        known = set(props.KNOWN) | set(props.parse(properties_path.read_text(), str(properties_path),
                                                   resolve=False))
    except props.PropertiesError as exc:
        return 2, {"ok": False, "error": str(exc)}
    values = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            return 2, {"ok": False, "error": f"--set expects KEY=VALUE with an UPPER_SNAKE_CASE key, got {pair!r}"}
        if key not in known:
            return 2, {"ok": False, "error": f"--set {key}: unknown key (a typo binds nothing); known keys: "
                                             + ", ".join(sorted(known))}
        if "\n" in value or "\r" in value:
            return 2, {"ok": False, "error": f"--set {key}: the value must be a single line"}
        values[key] = value
    if values:
        candidate = props.render(properties_path.read_text(), values)
        try:
            props.parse(candidate, str(properties_path))
        except props.PropertiesError as exc:
            return 2, {"ok": False, "error": str(exc)}
        # Validate typed name keys before writing: bad names surface here with exit 2
        # rather than silently writing and only failing later at configuration-loads.
        for key_name, val in values.items():
            k = props.KNOWN.get(key_name)
            if k and k.kind in ("agentcore_prefix", "iam_role_name", "iam_policy_name"):
                try:
                    props._typed(k, val, properties_path.parent)
                except props.PropertiesError as exc:
                    return 2, {"ok": False, "error": str(exc)}
        properties_path.write_text(candidate)
    # Auto-derive USER_NAMESPACE from the login name when it is still a placeholder and the caller
    # did not provide an explicit answer for it — saves one round-trip for a derivable value.
    try:
        import re as _re
        if "USER_NAMESPACE" in placeholders(properties_path) and "USER_NAMESPACE" not in values:
            raw = os.getlogin()
            ns = _re.sub(r"[^a-z0-9-]", "", raw.lower()).strip("-")[:32]
            if ns:
                prefilled = props.render(properties_path.read_text(), {"USER_NAMESPACE": ns})
                try:
                    props.parse(prefilled, str(properties_path))
                    properties_path.write_text(prefilled)
                except props.PropertiesError:
                    pass  # leave as placeholder so the operator is asked
    except Exception:
        pass  # os.getlogin() can fail in some environments; leave the placeholder
    try:
        props.load(properties_path)
        missing = placeholders(properties_path)
    except props.PropertiesError as exc:
        return 2, {"ok": False, "error": str(exc)}
    report = {"ok": not missing, "properties_file": str(properties_path), "created": created,
              "set": sorted(values), "missing": missing,
              "questions": {k: QUESTIONS.get(k, f"What value should {k} have? (see deploy.properties.example)")
                            for k in missing}}
    return (20 if missing else 0), report


def inputs_main(args) -> int:
    from .config import config_path
    path = Path(args.properties).expanduser() if args.properties else props.default_path(config_path())
    code, report = inputs(path, args.set or [])
    print(json.dumps(report, indent=2))
    return code


def main(args) -> int:
    from .config import config_path
    config = Path(args.config).expanduser() if args.config else config_path()
    properties = Path(args.properties).expanduser() if args.properties else props.default_path(config)
    result = Preflight(config, properties, offline=args.offline).run()
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1
