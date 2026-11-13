# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import os
import hashlib
import tomllib
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, model_validator


class AgentCoreRuntimeTarget(BaseModel):
    arn: str | None = None
    bundle: str | None = None  # S3 URI of the deployed deployment_package.zip
    qualifier: str = "DEFAULT"


class AgentCoreConfig(BaseModel):
    # Region defaults to None; resolve_config fills it from deploy.properties (AWS_REGION),
    # then from the AWS_REGION environment variable, then leaves it None (preflight fails).
    region: str | None = None
    profile: str | None = None
    execution_role_arn: str | None = None
    execution_role_name: str = "MchcBedrockAgentCoreRuntimeRole"
    # Names baked into deployed resources; the defaults are what existing deployments use.
    inline_policy_name: str = "MchcAgentCoreRuntime"
    application_tag: str = "Multi Coding-Harness Compare"
    transfer_s3_uri: str | None = None
    runtime_name_prefix: str = "mchc"
    workspace_mount: str = "/mnt/workspace"
    idle_session_timeout: int = 900
    max_lifetime: int = 28800
    command_timeout: int | None = None
    control_poll_seconds: float = 0.5
    # The remote command stream is closed after 15 minutes without a byte, whatever the
    # command timeout says, and a harness can think for longer than that. The in-microVM
    # supervisor writes a control-channel heartbeat this often to hold the stream open.
    stream_heartbeat_seconds: int = 120
    # Commands run in the microVM as the unprivileged `agentcore-runtime-user` (uid 991),
    # so the image roots — /, /opt, /var/task — are read-only to us. The writable overlay
    # (measured 10.4 GB total) gives full POSIX semantics, which the harness HOME needs:
    # Codex and Kiro keep session state in SQLite. /var/tmp rather than /tmp because Codex
    # refuses to create its PATH alias helpers under a HOME it considers temporary
    # ("Refusing to create helper binaries under temporary dir"), and /tmp trips that
    # check. `agentcore-probe` reports the live numbers.
    job_root: str = "/var/tmp/mchc"
    # Durable cache on the sessionStorage mount. It survives a cold microVM and is reset
    # only after 14 days without an invoke or on a runtime version update, which makes it
    # right for large re-downloadable payloads (the staged Kiro tools tree) and wrong for
    # anything needing file locks — SQLite there fails with "database is locked".
    cache_dir: str = "mchc-cache"
    python_runtime: str = "PYTHON_3_12"
    entry_point: list[str] = Field(default_factory=lambda: ["main.py"])
    session_activation: bool = True
    runtimes: dict[str, AgentCoreRuntimeTarget] = Field(default_factory=dict)
    # Multi-user isolation: per-user adds USER_NAMESPACE suffix to names and narrows S3 scope.
    isolation: str = "shared"
    # Optional customer-managed KMS key for SSE-KMS on the transfer bucket.
    transfer_kms_key_arn: str | None = None

    @property
    def cache_root(self) -> str:
        """Absolute path of the durable cache on the sessionStorage mount."""
        return f"{self.workspace_mount.rstrip('/')}/{self.cache_dir.strip('/')}"

    def effective_command_timeout(self, active_seconds: int) -> int:
        ceiling = min(3600, active_seconds + 600)
        if self.command_timeout is not None:
            return max(1, min(self.command_timeout, ceiling))
        return max(1, ceiling)


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def highest_effort(levels) -> str | None:
    """Highest effort from EFFORTS present in levels, or None if none match."""
    for e in reversed(EFFORTS):
        if e in levels:
            return e
    return None


# Vendor endpoints used by the image builder; not user/environment values.
# Keep as a documented constant so the agentcore-deploy component can import it directly.
HARNESS_TRUSTED_SOURCE_DEFAULTS: list[str] = [
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "desktop-release.q.us-east-1.amazonaws.com",
]


class HarnessImageConfig(BaseModel):
    """Image-build configuration consumed by the standalone agentcore-deploy component.

    Vendor endpoints in trusted_sources are not user/environment values; the default list
    is the documented vendor constant HARNESS_TRUSTED_SOURCE_DEFAULTS.
    """
    trusted_sources: list[str] = Field(
        default_factory=lambda: list(HARNESS_TRUSTED_SOURCE_DEFAULTS))
    kiro_download_url: str | None = None
    # When true, agentcore-deploy bakes a non-secret snapshot of this workstation's
    # harness configuration into the bundle as fallback defaults for in-VM bootstrap.
    workstation_config: bool = False


class HarnessConfig(BaseModel):
    model: str | None = None
    # Native reasoning effort; None keeps the CLI's per-model default. All three CLIs
    # (Claude Code --effort, Codex turn/start effort, kiro-cli acp --effort) accept EFFORTS.
    effort: Literal[EFFORTS] | None = None
    # Dedicated template HOME. Never point this at your entire personal HOME.
    home_template: str | None = None
    refresh_files: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    inherit_host_setup: bool = False
    cli_path: str | None = None
    model_regions: dict[str, str] = Field(default_factory=dict)
    mcp_servers: dict = Field(default_factory=dict)
    agent: str | None = None
    # Who answers the harness's own permission prompts; set by AUTO_APPROVE in
    # deploy.properties. False sends them to the user as approval cards.
    auto_approve: bool = False
    # ── Image build keys ─────────────────────────────────────────────────────
    # pinned | installed | latest — how agentcore-deploy selects the harness version.
    harness_source: str | None = None
    # Explicit version pin for agentcore-deploy (overrides harness_source resolution).
    harness_version: str | None = None
    # ── Harness configuration keys ───────────────────────────────────────────
    # Workstation config dir to read defaults from (replaces ad-hoc env var reads).
    config_dir: str | None = None
    # Relative paths added to / removed from the default copy allowlist.
    config_include: list[str] = Field(default_factory=list)
    config_exclude: list[str] = Field(default_factory=list)
    # Extra skills directories, steering files, and MCP config to overlay into each run.
    skills_dirs: list[str] = Field(default_factory=list)
    steering_files: list[str] = Field(default_factory=list)
    mcp_config: str | None = None
    # S3 prefix synced read-only into each run's HOME config dir.
    config_s3_uri: str | None = None
    # Provider string for Codex (maps to MCHC_CODEX_PROVIDER); other harnesses ignore it.
    provider: str | None = None

    @model_validator(mode="after")
    def validate_refresh_files(self):
        if self.inherit_host_setup and self.home_template:
            raise ValueError("Choose existing CLI setup or a dedicated HOME template, not both")
        for value in self.refresh_files:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or not path.name:
                raise ValueError("Credential refresh paths must be files relative to the dedicated HOME")
        return self


class Config(BaseModel):
    runtime: Literal["native", "agentcore"] = "native"
    data_dir: str = ".arena"
    # Set ARTIFACT_S3_URI in deploy.properties (or artifact_s3_uri here); no bucket is assumed.
    artifact_s3_uri: str | None = None
    artifact_s3_profile: str | None = None
    # Region defaults to None; resolve_config fills it from deploy.properties (AWS_REGION),
    # then from the AWS_REGION environment variable, then leaves it None (preflight fails).
    artifact_s3_region: str | None = None
    # Explicit non-application files/directories to archive. Relative paths are
    # resolved beside data_dir, not against the worker's launch directory.
    artifact_paths: list[str] = Field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8765
    # Repository selection is deliberately restricted to these roots.
    repository_roots: list[str] = Field(default_factory=lambda: [str(Path.cwd())])
    active_seconds: int = 1800
    setup_seconds: int = 180
    check_seconds: int = 180
    max_turns: int = 80
    memory: str = "2g"
    pids: int = 256
    max_parallel_comparisons: int = 10
    native_port_start: int = 20000
    native_read_roots: list[str] = Field(default_factory=list)
    judge_model: str | None = None
    # Region defaults to None; see artifact_s3_region above for resolution logic.
    judge_region: str | None = None
    judge_profile: str | None = None
    judge_streaming: bool = True
    judge_seconds: int = Field(default=300, gt=0)
    judge_max_attempts: int = Field(default=3, ge=1, le=3)
    planning_model: str | None = None
    planning_effort: Literal["low", "medium", "high", "xhigh", "max", "highest"] | None = None
    planning_max_tokens: int = Field(default=100000, ge=1024)
    # Acceptance planning has no time budget. The output ceiling above still bounds
    # each response; a wall-clock deadline shared by an attempt and the continuation
    # it mandates discarded finished work instead of bounding anything useful.
    # Judge configuration is separate. Host-setup mode explicitly opts coding
    # harnesses into the user's existing provider credentials.
    agentcore: AgentCoreConfig = Field(default_factory=AgentCoreConfig)
    harnesses: dict[str, HarnessConfig] = Field(default_factory=lambda: {
        "claude": HarnessConfig(env=["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]),
        "codex": HarnessConfig(env=["OPENAI_API_KEY"]),
        "kiro": HarnessConfig(),
    })
    # Image-build configuration for the standalone agentcore-deploy component.
    harness_image: HarnessImageConfig = Field(default_factory=HarnessImageConfig)
    # Short [a-z0-9-] namespace isolating this user's S3 objects and AgentCore resources.
    user_namespace: str | None = None
    # Optional AWS credential file paths when non-default locations are needed.
    aws_config_file: str | None = None
    aws_shared_creds_file: str | None = None
    # Maximum megabytes of non-secret config copied from defaults per harness.
    # Raised here so operators can raise the ceiling without code changes.
    harness_config_size_cap_mb: int = Field(default=50, ge=1)
    # Operator-configured default for the per-comparison configuration choice.
    # None → resolved by default_configuration_choice based on the runtime.
    harness_config_default_choice: str | None = None

    @model_validator(mode="after")
    def validate_host_setup(self):
        if self.planning_effort and "anthropic.claude" not in (self.effective_planning_model or ""):
            raise ValueError("planning_effort requires a Claude planning_model with adaptive thinking support")
        if self.artifact_s3_uri is not None:
            from urllib.parse import urlsplit
            target = urlsplit(self.artifact_s3_uri)
            if (target.scheme != "s3" or not target.netloc or not target.path.strip("/")
                    or target.query or target.fragment or ".." in target.path.split("/")):
                raise ValueError("artifact_s3_uri must name an S3 bucket and nonempty prefix")
        if self.runtime == "agentcore":
            from urllib.parse import urlsplit
            uri = self.agentcore.transfer_s3_uri
            if uri is None:
                raise ValueError("agentcore.transfer_s3_uri is required when runtime is agentcore")
            target = urlsplit(uri)
            if (target.scheme != "s3" or not target.netloc or not target.path.strip("/")
                    or target.query or target.fragment or ".." in target.path.split("/")):
                raise ValueError("agentcore.transfer_s3_uri must name an S3 bucket and nonempty prefix")
            mount = self.agentcore.workspace_mount
            if not mount.startswith("/mnt/") or mount == "/mnt/":
                raise ValueError("agentcore.workspace_mount must be an absolute path under /mnt")
            job_root = self.agentcore.job_root
            # The microVM command user is unprivileged, so most of the image is read-only;
            # a job_root outside the writable overlay fails at mkdir before any harness
            # starts. Reject it here, where the message can say why, instead of there.
            writable = ("/tmp/", "/var/tmp/", "/home/")
            if not job_root.startswith("/") or job_root.startswith("/mnt"):
                raise ValueError("agentcore.job_root must be an absolute path that is not under /mnt")
            if not job_root.rstrip("/").startswith(writable):
                raise ValueError(
                    "agentcore.job_root must be under a path the unprivileged runtime user can "
                    f"write: one of {', '.join(writable)}. Run `multi-coding-harness-compare agentcore-probe` to "
                    "confirm what the deployed runtime allows.")
            cache_dir = self.agentcore.cache_dir.strip("/")
            if not cache_dir or ".." in cache_dir.split("/"):
                raise ValueError("agentcore.cache_dir must be a nonempty path relative to the workspace mount")
        for name, harness in self.harnesses.items():
            if harness.inherit_host_setup and (self.runtime != "native" or name not in ("claude", "codex")):
                raise ValueError("Existing CLI setup requires native macOS execution with Claude Code or Codex")
        return self

    @property
    def default_configuration_choice(self) -> str:
        """Effective default configuration choice for new runs.

        If HARNESS_CONFIG_DEFAULT_CHOICE is set, honour it.
        Otherwise: 'overrides' on agentcore (workstation folder irrelevant in microVMs),
        'default' on native (copy workstation config dir).
        """
        if self.harness_config_default_choice is not None:
            return self.harness_config_default_choice
        return "overrides" if self.runtime == "agentcore" else "default"

    @property
    def effective_planning_model(self) -> str | None:
        return self.planning_model or self.judge_model

    @property
    def root(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    @property
    def container_owner(self) -> str:
        return hashlib.sha256(str(self.root).encode()).hexdigest()[:24]

    def harness_for_run(self, run: dict) -> HarnessConfig:
        configured = self.harnesses[run["harness"]]
        # Existing records predate task-level selection. New records also store
        # an explicit None, so a later global configuration change cannot alter them.
        update = {}
        if "requested_model" in run:
            update["model"] = run["requested_model"]
        if "requested_effort" in run:
            update["effort"] = run["requested_effort"]
        return configured.model_copy(update=update) if update else configured

    def judge_for_comparison(self, comparison: dict) -> "Config":
        selection = comparison.get("judge")
        if not selection:
            return self
        return self.model_copy(update={
            "judge_model": selection["model"], "judge_region": selection["region"],
            "judge_profile": selection["profile"],
            "judge_streaming": selection.get("streaming", True),
        })

    def planning_for_comparison(self, comparison: dict) -> "Config":
        """Return a Config with planning overridden from this comparison's stored planning record."""
        planning = comparison.get("planning")
        if planning:
            # stored effort of None means no adaptive thinking
            return self.model_copy(update={
                "planning_model": planning["model"],
                "planning_effort": planning.get("effort"),
            })
        # Legacy comparison without a stored planning selection
        if self.planning_effort == "highest":
            return self.model_copy(update={"planning_effort": "max"})
        return self


def config_path() -> Path:
    return Path(os.environ.get("MCHC_CONFIG", os.environ.get("ARENA_CONFIG", "config.toml")))


def resolve_config(path: Path | None = None, properties_path: Path | None = None,
                   *, text: str | None = None, properties=None) -> tuple["Config", dict]:
    """Config from config.toml plus deploy.properties, and where each bound value came from.

    Precedence per field: a literal in config.toml, then deploy.properties (a key's bound
    fields, or a ${KEY} reference), then AWS_REGION env var (for the three region fields),
    then the code default.

    The report includes an "environment_literals" list of fields that are set as plain
    literals in config.toml but have a deploy.properties key — those values should live
    in deploy.properties instead (preflight check config-toml-environment-literals).
    """
    from . import properties as props_module
    path = Path(path) if path is not None else config_path()
    if properties is None:
        properties = props_module.load(properties_path if properties_path is not None
                                       else props_module.default_path(path))
    if text is None:
        text = path.read_text() if path.exists() else ""
    explicit, references = props_module.interpolate(tomllib.loads(text), properties, str(path))
    defaults, bound = props_module.bound_defaults(properties)
    # Fill region fields from AWS_REGION env var when deploy.properties has no AWS_REGION
    # and the config.toml has no literal for that field either.  This resolves the common
    # case where the env var is set but the new mandatory-region requirement is otherwise met.
    _aws_region_env = os.environ.get("AWS_REGION")
    if _aws_region_env:
        region_fields = ("artifact_s3_region", "judge_region", "agentcore.region")
        for dotted in region_fields:
            if dotted not in bound:  # not already filled from deploy.properties key
                _present, _ = props_module._leaf(explicit, dotted)
                if not _present:  # not set as a literal in config.toml
                    *parents, leaf = dotted.split(".")
                    node = defaults
                    for part in parents:
                        node = node.setdefault(part, {})
                    node[leaf] = _aws_region_env
                    bound[dotted] = "AWS_REGION"  # mark as env-var-sourced for the report
    # Fill harnesses.codex.provider from MCHC_CODEX_PROVIDER env var when
    # CODEX_PROVIDER is absent from deploy.properties and not a literal in config.toml.
    # The properties.py CODEX_PROVIDER doc promises this fallback; this block honours it.
    _codex_provider_dotted = "harnesses.codex.provider"
    if _codex_provider_dotted not in bound:
        _present_explicit, _ = props_module._leaf(explicit, _codex_provider_dotted)
        if not _present_explicit:
            _codex_provider_env = os.environ.get("MCHC_CODEX_PROVIDER")
            if _codex_provider_env:
                node = defaults.setdefault("harnesses", {}).setdefault("codex", {})
                node["provider"] = _codex_provider_env
                bound[_codex_provider_dotted] = "env:MCHC_CODEX_PROVIDER"
    merged = props_module.deep_merge(defaults, explicit)
    if "harnesses" in defaults:
        # A harness config.toml leaves out entirely still gets its built-in defaults
        # (the credential environment names) under the properties values.
        stock = {name: harness.model_dump() for name, harness
                 in Config.model_fields["harnesses"].default_factory().items()}
        present = explicit.get("harnesses") or {}
        names = dict.fromkeys([*(stock if not present else ()), *present, *defaults["harnesses"]])
        merged["harnesses"] = {
            name: props_module.deep_merge(
                props_module.deep_merge({} if name in present else stock.get(name, {}),
                                        defaults["harnesses"].get(name, {})),
                present.get(name, {}))
            for name in names}
    config = Config(**merged)
    report = {"config_file": str(path.resolve()) if path.exists() else None,
              "properties_file": str(properties.path) if properties.loaded else None,
              "properties": dict(properties.values), "fields": {}, "overridden": [],
              "environment_literals": [], "unused": []}
    referenced = {name for names in references.values() for name in names}
    resolved = config.model_dump()
    for dotted, name in bound.items():
        present, value = props_module._leaf(explicit, dotted)
        if present and dotted in references:
            source = f"config.toml via ${{{','.join(references[dotted])}}}"
        elif present:
            source = "config.toml"
            if props_module.comparable(name, value, path.parent) != props_module._leaf(defaults, dotted)[1]:
                report["overridden"].append({"field": dotted, "property": name,
                                             "config_toml": value,
                                             "properties": props_module._leaf(defaults, dotted)[1]})
            # A plain literal for a field that has a deploy.properties key binding should
            # live in deploy.properties — record it so it can be turned into a preflight fail.
            # Only flag when the literal actually overrides the deploy.properties value;
            # a config.toml entry that merely echoes what deploy.properties already provides
            # is redundant but must not fail preflight (backward compatibility).
            if name != "AWS_REGION":  # env-var sourced entries are not literals
                _, _dp_val = props_module._leaf(defaults, dotted)
                if props_module.comparable(name, value, path.parent) != _dp_val:
                    report["environment_literals"].append({
                        "field": dotted, "property": name, "value": value})
        else:
            source = (f"env:AWS_REGION" if name == "AWS_REGION"
                      else f"deploy.properties:{name}")
        report["fields"][dotted] = {"value": props_module._leaf(resolved, dotted)[1], "source": source}
    # Second pass: catch literals in config.toml for fields whose deploy.properties key
    # was absent from (or empty in) deploy.properties — those keys have no entry in
    # ``bound`` so the first loop misses them entirely.  Build a reverse map from every
    # field dotted-path → KEY_NAME for all KEYS, then check the explicit (config.toml)
    # dict for plain literals that aren't already covered by the first loop.
    # Only flag a value that DIFFERS from the bare Config() model default; a literal that
    # merely reproduces the built-in default is a documentation choice, not an
    # environment-specific override (e.g. env = ["ANTHROPIC_API_KEY"] is the default).
    _bare_defaults = Config().model_dump()
    _all_fields_to_key: dict[str, str] = {}
    for _key in props_module.KEYS:
        for _dotted in _key.fields:
            if _dotted not in _all_fields_to_key:
                _all_fields_to_key[_dotted] = _key.name
    for _dotted, _key_name in _all_fields_to_key.items():
        if _dotted in bound:
            continue  # already handled in the first loop above
        if _key_name == "AWS_REGION":
            continue  # env-var fallback; not a config.toml literal
        # Behavioral fields (booleans, comma-separated lists) are documented in
        # config.toml and are independent of workstation/account identity; flagging
        # them as environment literals would break existing configs that customise
        # env passthrough lists or inherit_host_setup.
        _key_obj = props_module.KNOWN.get(_key_name)
        if _key_obj and _key_obj.kind in ("bool", "comma_list"):
            continue
        _present, _value = props_module._leaf(explicit, _dotted)
        if _present and _dotted not in references:
            # Only flag when the literal differs from the built-in Config() default so
            # that config.example.toml entries that merely document defaults are ignored.
            _, _bare_default = props_module._leaf(_bare_defaults, _dotted)
            if _value == _bare_default:
                continue
            report["environment_literals"].append(
                {"field": _dotted, "property": _key_name, "value": _value})

    used = set(bound.values()) | referenced | properties.referenced
    report["unused"] = sorted(name for name in properties.values if name not in used
                              and name not in props_module.KNOWN)
    return config, report


def environment_literal_fields(report: dict) -> list:
    """Fields set as plain literals in config.toml that have a deploy.properties key.

    Used to implement the config-toml-environment-literals preflight check.
    Each entry is {"field": dotted_path, "property": KEY_NAME, "value": literal_value}.
    """
    return report.get("environment_literals", [])


def load_config(path: Path | None = None, properties_path: Path | None = None) -> Config:
    return resolve_config(path, properties_path)[0]
