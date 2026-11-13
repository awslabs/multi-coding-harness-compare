# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""deploy.properties: every value that changes between users, machines and AWS accounts.

Porting means editing this one file. Each known key fills the configuration fields it
binds (AWS_PROFILE fills the artifact, judge and AgentCore profiles at once), and any
string in config.toml may reference a key as ${KEY}. A literal value in config.toml still
wins for its own field, so an existing configuration keeps working; `doctor` reports
where every bound value came from and which literals override the properties file.

Stdlib only. The file holds coordinates (profile names, regions, bucket URIs, role names,
local paths), never credentials: secret-looking keys are refused.
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PROPERTIES_ENV = "MCHC_PROPERTIES"
PROPERTIES_FILE = "deploy.properties"

_KEY_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_REFERENCE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_SECRET = re.compile(r"SECRET|PASSWORD|PASSPHRASE|TOKEN|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL")


@dataclass(frozen=True)
class Key:
    name: str
    fields: tuple = ()           # dotted Config field paths this key fills
    kind: str = "str"            # str | int | path | paths
    redeploy: str = "restart the API and worker"
    doc: str = ""


KEYS = (
    Key("RUNTIME", ("runtime",), kind="runtime",
        redeploy="agentcore needs its runtimes deployed (agentcore-deploy) before the first comparison",
        doc="Where harnesses run: native (macOS Seatbelt here) or agentcore (Bedrock AgentCore microVMs)."),
    Key("HARNESS_CONFIG_DEFAULT_CHOICE", ("harness_config_default_choice",), kind="config_default_choice",
        redeploy="restart the API and worker",
        doc="Default configuration choice applied when a comparison does not specify one: "
            "default (copy workstation config dir + explicit files), "
            "overrides (explicit files only; H_SKILLS_DIRS / H_STEERING_FILES / H_MCP_CONFIG / H_CONFIG_S3_URI), "
            "none (clean HOME). "
            "Unset: 'overrides' when RUNTIME = agentcore, 'default' otherwise (native)."),
    Key("KIRO_AUTH", ("harnesses.kiro.refresh_files",), kind="kiro_auth",
        doc="copy-host-login copies this workstation's Kiro login into each run; login-in-home copies "
            "nothing and expects a login inside KIRO_HOME_TEMPLATE."),
    Key("AWS_PROFILE", ("artifact_s3_profile", "judge_profile", "agentcore.profile"),
        redeploy="restart only: a profile selects credentials and is baked into nothing deployed",
        doc="AWS profile for S3 artifacts, the Bedrock judge and AgentCore. Empty uses the ambient credential chain."),
    Key("AWS_REGION", ("artifact_s3_region", "judge_region", "agentcore.region"),
        redeploy="agentcore-deploy: runtimes, their environment and the role's log scope are regional",
        doc="Region for S3 artifacts, the Bedrock judge and AgentCore."),
    Key("AWS_ACCOUNT_ID", (),
        redeploy="agentcore-deploy in the new account",
        doc="Optional. Lets doctor flag cached AgentCore runtime ARNs from another account."),
    Key("AWS_CONFIG_FILE", ("aws_config_file",), kind="path",
        redeploy="restart only",
        doc="Optional. Path to an AWS config file; overrides the default ~/.aws/config."),
    Key("AWS_SHARED_CREDS_FILE", ("aws_shared_creds_file",), kind="path",
        redeploy="restart only",
        doc="Optional. Path to an AWS credentials file; overrides the default ~/.aws/credentials."),
    Key("S3_BUCKET", (),
        doc="Optional helper the S3 URIs can share as ${S3_BUCKET}."),
    Key("ARTIFACT_S3_URI", ("artifact_s3_uri",),
        redeploy="restart only; comparisons archived under the old URI stop being readable here",
        doc="s3://bucket/prefix/ where comparison artifacts are archived."),
    Key("AGENTCORE_TRANSFER_S3_URI", ("agentcore.transfer_s3_uri",),
        redeploy="agentcore-deploy: bundles and the role's S3 scope follow this prefix",
        doc="s3://bucket/prefix/ for AgentCore job, result and bundle transfer."),
    Key("AGENTCORE_EXECUTION_ROLE_NAME", ("agentcore.execution_role_name",),
        kind="iam_role_name",
        redeploy="agentcore-deploy creates a role under the new name; the old one is left in place",
        doc="IAM execution role that agentcore-deploy creates or refreshes. "
            "Characters: [\\w+=,.@-], length 1-64."),
    Key("AGENTCORE_EXECUTION_ROLE_ARN", ("agentcore.execution_role_arn",),
        redeploy="agentcore-deploy; a pinned role is used as is and its policies are not refreshed",
        doc="Optional. Use an existing role instead of managing one by name."),
    Key("AGENTCORE_INLINE_POLICY_NAME", ("agentcore.inline_policy_name",),
        kind="iam_policy_name",
        redeploy="agentcore-deploy adds a policy under the new name; delete the old one by hand",
        doc="Name of the inline policy agentcore-deploy writes on the execution role. "
            "Characters: [\\w+=,.@-], length 1-128."),
    Key("AGENTCORE_RUNTIME_NAME_PREFIX", ("agentcore.runtime_name_prefix",),
        kind="agentcore_prefix",
        redeploy="agentcore-deploy creates <prefix>_<harness> runtimes; old ones are left in place",
        doc="Runtimes are named and discovered as <prefix>_claude, <prefix>_codex, <prefix>_kiro. "
            "Characters: [A-Za-z][A-Za-z0-9_]* (letters, digits, underscores — no hyphens)."),
    Key("AGENTCORE_APPLICATION_TAG", ("agentcore.application_tag",),
        redeploy="agentcore-deploy updates runtime descriptions; the tag applies to resources created afterwards",
        doc="Application tag and description prefix on created AWS resources."),
    Key("AGENTCORE_ISOLATION", ("agentcore.isolation",), kind="isolation",
        redeploy="agentcore-deploy: per-user creates namespaced role and runtime names; "
                 "restart the API and worker after",
        doc="shared: all users share role/runtime names (default). "
            "per-user: role, inline-policy and runtime names get the USER_NAMESPACE suffix; "
            "the execution role's S3 scope narrows to the user's prefix."),
    Key("AGENTCORE_TRANSFER_KMS_KEY_ARN", ("agentcore.transfer_kms_key_arn",),
        redeploy="agentcore-deploy: the execution role gains kms:Decrypt and kms:GenerateDataKey grants",
        doc="Optional. Customer-managed KMS key ARN for SSE-KMS on the transfer bucket."),
    Key("USER_NAMESPACE", ("user_namespace",), kind="namespace",
        redeploy="restart; existing AgentCore keys remain under the old namespace",
        doc="Short [a-z0-9-] segment isolating this user's S3 objects and (with "
            "AGENTCORE_ISOLATION=per-user) AgentCore resources. "
            "Pre-filled from the OS login name by `setup` when absent."),
    Key("REPOSITORY_ROOTS", ("repository_roots",), kind="paths",
        doc="Comma-separated project directories comparisons may run against."),
    Key("DATA_DIR", ("data_dir",), kind="path",
        redeploy="restart; moving it abandons warm AgentCore sessions and local history",
        doc="Runtime data directory (SQLite store, run workspaces, logs, access token)."),
    Key("SERVICE_PORT", ("port",), kind="int", doc="Local API and UI port."),
    Key("JUDGE_MODEL", ("judge_model",), doc="Bedrock inference profile that scores implementations."),
    Key("PLANNING_MODEL", ("planning_model",), doc="Bedrock inference profile that plans acceptance tests."),
    Key("CLAUDE_MODEL", ("harnesses.claude.model",), doc="Default Claude Code model."),
    Key("CODEX_MODEL", ("harnesses.codex.model",), doc="Default Codex model."),
    Key("KIRO_MODEL", ("harnesses.kiro.model",), doc="Default Kiro model."),
    Key("CLAUDE_CLI_PATH", ("harnesses.claude.cli_path",), kind="path", doc="Claude Code CLI on this machine."),
    Key("CODEX_CLI_PATH", ("harnesses.codex.cli_path",), kind="path", doc="Codex CLI on this machine."),
    Key("KIRO_CLI_PATH", ("harnesses.kiro.cli_path",), kind="path", doc="Kiro CLI on this machine."),
    Key("CLAUDE_HOME_TEMPLATE", ("harnesses.claude.home_template",), kind="path",
        doc="Dedicated HOME template copied into each Claude Code run."),
    Key("CODEX_HOME_TEMPLATE", ("harnesses.codex.home_template",), kind="path",
        doc="Dedicated HOME template copied into each Codex run."),
    Key("KIRO_HOME_TEMPLATE", ("harnesses.kiro.home_template",), kind="path",
        doc="Dedicated HOME template copied into each Kiro run."),
    # ── Image build ──────────────────────────────────────────────────────────
    # Per-harness source strategy consumed by the standalone agentcore-deploy component.
    Key("CLAUDE_HARNESS_SOURCE", ("harnesses.claude.harness_source",), kind="harness_source",
        redeploy="agentcore-deploy: the bundle is rebuilt from the new source",
        doc="pinned (default) | installed | latest. How the Claude Code version for the AgentCore "
            "bundle is selected. installed detects the version on this workstation."),
    Key("CODEX_HARNESS_SOURCE", ("harnesses.codex.harness_source",), kind="harness_source",
        redeploy="agentcore-deploy: the bundle is rebuilt from the new source",
        doc="pinned (default) | installed | latest. How the Codex version for the AgentCore bundle "
            "is selected."),
    Key("KIRO_HARNESS_SOURCE", ("harnesses.kiro.harness_source",), kind="harness_source",
        redeploy="agentcore-deploy: the bundle is rebuilt from the new source",
        doc="pinned (default) | installed | latest. How the Kiro version for the AgentCore bundle "
            "is selected."),
    Key("CLAUDE_HARNESS_VERSION", ("harnesses.claude.harness_version",),
        redeploy="agentcore-deploy: the bundle is rebuilt at the pinned version",
        doc="Optional. Explicit Claude Code version for the AgentCore bundle "
            "(overrides the source strategy's resolution)."),
    Key("CODEX_HARNESS_VERSION", ("harnesses.codex.harness_version",),
        redeploy="agentcore-deploy: the bundle is rebuilt at the pinned version",
        doc="Optional. Explicit Codex version for the AgentCore bundle."),
    Key("KIRO_HARNESS_VERSION", ("harnesses.kiro.harness_version",),
        redeploy="agentcore-deploy: the bundle is rebuilt at the pinned version",
        doc="Optional. Explicit Kiro version for the AgentCore bundle."),
    Key("HARNESS_TRUSTED_SOURCES", ("harness_image.trusted_sources",), kind="comma_list",
        redeploy="agentcore-deploy: the next bundle fetch uses the new allowlist",
        doc="Comma-separated https hostnames downloads may come from. "
            "Default: registry.npmjs.org,pypi.org,files.pythonhosted.org,"
            "desktop-release.q.us-east-1.amazonaws.com."),
    Key("KIRO_DOWNLOAD_URL", ("harness_image.kiro_download_url",),
        redeploy="agentcore-deploy: used on the next Kiro bundle fetch",
        doc="Optional. URL template for downloading Kiro. "
            "Placeholders: {version} and {arch}. Must be a host in HARNESS_TRUSTED_SOURCES."),
    Key("HARNESS_IMAGE_WORKSTATION_CONFIG", ("harness_image.workstation_config",), kind="bool",
        redeploy="agentcore-deploy: the next bundle bakes in (or omits) the workstation config snapshot",
        doc="false (default) | true. When true, agentcore-deploy bakes this workstation's "
            "non-secret harness configuration snapshot into the AgentCore bundle as fallback defaults."),
    # ── Harness configuration ────────────────────────────────────────────────
    # Per-harness config consumed by the host setup logic (arena/host_setup.py).
    Key("CLAUDE_CONFIG_DIR", ("harnesses.claude.config_dir",), kind="path",
        redeploy="restart the API and worker",
        doc="Workstation config directory to read Claude Code defaults from. "
            "Default: $CLAUDE_CONFIG_DIR or ~/.claude."),
    Key("CODEX_CONFIG_DIR", ("harnesses.codex.config_dir",), kind="path",
        redeploy="restart the API and worker",
        doc="Workstation config directory to read Codex defaults from. "
            "Default: $CODEX_HOME or ~/.codex."),
    Key("KIRO_CONFIG_DIR", ("harnesses.kiro.config_dir",), kind="path",
        redeploy="restart the API and worker",
        doc="Workstation config directory to read Kiro defaults from. Default: ~/.kiro."),
    Key("CLAUDE_CONFIG_INCLUDE", ("harnesses.claude.config_include",), kind="comma_list",
        redeploy="restart the API and worker",
        doc="Comma-separated paths relative to CLAUDE_CONFIG_DIR added to the default copy allowlist."),
    Key("CODEX_CONFIG_INCLUDE", ("harnesses.codex.config_include",), kind="comma_list",
        redeploy="restart the API and worker",
        doc="Comma-separated paths relative to CODEX_CONFIG_DIR added to the default copy allowlist."),
    Key("KIRO_CONFIG_INCLUDE", ("harnesses.kiro.config_include",), kind="comma_list",
        redeploy="restart the API and worker",
        doc="Comma-separated paths relative to KIRO_CONFIG_DIR added to the default copy allowlist."),
    Key("CLAUDE_CONFIG_EXCLUDE", ("harnesses.claude.config_exclude",), kind="comma_list",
        redeploy="restart the API and worker",
        doc="Comma-separated paths (or mcp:<name>) removed from the Claude default copy allowlist."),
    Key("CODEX_CONFIG_EXCLUDE", ("harnesses.codex.config_exclude",), kind="comma_list",
        redeploy="restart the API and worker",
        doc="Comma-separated paths (or mcp:<name>) removed from the Codex default copy allowlist."),
    Key("KIRO_CONFIG_EXCLUDE", ("harnesses.kiro.config_exclude",), kind="comma_list",
        redeploy="restart the API and worker",
        doc="Comma-separated paths (or mcp:<name>) removed from the Kiro default copy allowlist."),
    Key("CLAUDE_SKILLS_DIRS", ("harnesses.claude.skills_dirs",), kind="paths",
        redeploy="restart the API and worker",
        doc="Comma-separated directories whose SKILL.md folders are added to each Claude run."),
    Key("CODEX_SKILLS_DIRS", ("harnesses.codex.skills_dirs",), kind="paths",
        redeploy="restart the API and worker",
        doc="Comma-separated directories whose skill folders are added to each Codex run."),
    Key("KIRO_SKILLS_DIRS", ("harnesses.kiro.skills_dirs",), kind="paths",
        redeploy="restart the API and worker",
        doc="Comma-separated directories whose skill folders are added to each Kiro run."),
    Key("CLAUDE_STEERING_FILES", ("harnesses.claude.steering_files",), kind="paths",
        redeploy="restart the API and worker",
        doc="Comma-separated files/dirs that become CLAUDE.md or .claude/rules/ entries in each run."),
    Key("CODEX_STEERING_FILES", ("harnesses.codex.steering_files",), kind="paths",
        redeploy="restart the API and worker",
        doc="Comma-separated files/dirs that become AGENTS.md entries in each Codex run."),
    Key("KIRO_STEERING_FILES", ("harnesses.kiro.steering_files",), kind="paths",
        redeploy="restart the API and worker",
        doc="Comma-separated files/dirs that become .kiro/steering entries in each Kiro run."),
    Key("CLAUDE_MCP_CONFIG", ("harnesses.claude.mcp_config",), kind="path",
        redeploy="restart the API and worker",
        doc="Path to a JSON file with an 'mcpServers' key merged into each Claude run's .claude.json."),
    Key("CODEX_MCP_CONFIG", ("harnesses.codex.mcp_config",), kind="path",
        redeploy="restart the API and worker",
        doc="Path to a TOML file with [mcp_servers] merged into each Codex run's config.toml."),
    Key("KIRO_MCP_CONFIG", ("harnesses.kiro.mcp_config",), kind="path",
        redeploy="restart the API and worker",
        doc="Path to a mcp.json file merged into each Kiro run's .kiro/settings/mcp.json."),
    Key("CLAUDE_CONFIG_S3_URI", ("harnesses.claude.config_s3_uri",),
        redeploy="restart the API and worker",
        doc="s3://bucket/prefix/ synced read-only into each Claude run's HOME config dir."),
    Key("CODEX_CONFIG_S3_URI", ("harnesses.codex.config_s3_uri",),
        redeploy="restart the API and worker",
        doc="s3://bucket/prefix/ synced read-only into each Codex run's HOME config dir."),
    Key("KIRO_CONFIG_S3_URI", ("harnesses.kiro.config_s3_uri",),
        redeploy="restart the API and worker",
        doc="s3://bucket/prefix/ synced read-only into each Kiro run's HOME config dir."),
    Key("CLAUDE_INHERIT_HOST_SETUP", ("harnesses.claude.inherit_host_setup",), kind="bool",
        redeploy="restart the API and worker",
        doc="true | false (default false). Reuse the installed Claude executable and provider "
            "config on the native runtime."),
    Key("CODEX_INHERIT_HOST_SETUP", ("harnesses.codex.inherit_host_setup",), kind="bool",
        redeploy="restart the API and worker",
        doc="true | false (default false). Reuse the installed Codex executable and provider "
            "config on the native runtime."),
    Key("KIRO_INHERIT_HOST_SETUP", ("harnesses.kiro.inherit_host_setup",), kind="bool",
        redeploy="restart the API and worker",
        doc="true | false (default false). Reuse the installed Kiro executable and provider "
            "config on the native runtime."),
    Key("AUTO_APPROVE", ("harnesses.claude.auto_approve", "harnesses.codex.auto_approve",
                         "harnesses.kiro.auto_approve"), kind="bool",
        redeploy="restart the API and worker",
        doc="true | false (default false; true recommended). true: the harnesses' own permission "
            "prompts are answered automatically inside each run's sandbox (Claude Code's auto mode "
            "still sends you what it escalates); false: they come to you as approval cards."),
    Key("CLAUDE_ENV_PASSTHROUGH", ("harnesses.claude.env",), kind="comma_list",
        redeploy="restart the API and worker",
        doc="Comma-separated environment variable names forwarded to Claude Code. "
            "Default: ANTHROPIC_API_KEY,CLAUDE_CODE_OAUTH_TOKEN."),
    Key("CODEX_ENV_PASSTHROUGH", ("harnesses.codex.env",), kind="comma_list",
        redeploy="restart the API and worker",
        doc="Comma-separated environment variable names forwarded to Codex. "
            "Default: OPENAI_API_KEY."),
    Key("KIRO_ENV_PASSTHROUGH", ("harnesses.kiro.env",), kind="comma_list",
        redeploy="restart the API and worker",
        doc="Comma-separated environment variable names forwarded to Kiro. Default: empty."),
    # H_RUNTIME_ARN: pin each harness's AgentCore runtime ARN instead of discovering by name.
    Key("CLAUDE_RUNTIME_ARN", ("agentcore.runtimes.claude.arn",),
        redeploy="restart only: the ARN is used at session creation, not in the deployed image",
        doc="Optional. Pin the Claude AgentCore runtime ARN instead of discovering by name."),
    Key("CODEX_RUNTIME_ARN", ("agentcore.runtimes.codex.arn",),
        redeploy="restart only",
        doc="Optional. Pin the Codex AgentCore runtime ARN instead of discovering by name."),
    Key("KIRO_RUNTIME_ARN", ("agentcore.runtimes.kiro.arn",),
        redeploy="restart only",
        doc="Optional. Pin the Kiro AgentCore runtime ARN instead of discovering by name."),
    # ── Other ────────────────────────────────────────────────────────────────
    Key("CODEX_PROVIDER", ("harnesses.codex.provider",),
        redeploy="restart the API and worker",
        doc="amazon-bedrock (default) | amazon-bedrock-runtime. Selects the Codex model provider. "
            "Env var MCHC_CODEX_PROVIDER is still honoured as a fallback."),
)
KNOWN = {key.name: key for key in KEYS}


def harness_keys(harness: str) -> tuple:
    """All KEYS whose name begins with the harness prefix (e.g. 'claude' → CLAUDE_*).

    Useful for documentation generation and for deploy components to enumerate per-harness
    deploy.properties keys without hard-coding the list.

    >>> [k.name for k in harness_keys("codex")]  # returns CODEX_* entries in KEYS order
    """
    prefix = harness.upper() + "_"
    return tuple(key for key in KEYS if key.name.startswith(prefix))


class PropertiesError(ValueError):
    """Raised for deploy.properties format and validation errors.

    gate ("assistant" | "human"): who must act to resolve the error.
    fix: the corrective action to take; used as the preflight check's fix text.
    """

    def __init__(self, *args, gate: str = "assistant", fix: str | None = None):
        super().__init__(*args)
        self.gate = gate
        self.fix = fix


@dataclass
class Properties:
    path: Path | None = None
    values: dict = field(default_factory=dict)   # resolved, as written (strings)
    referenced: set = field(default_factory=set)  # keys other keys refer to

    @property
    def loaded(self) -> bool:
        return self.path is not None

    @property
    def base(self) -> Path:
        return self.path.parent if self.path else Path.cwd()


def default_path(config_path: Path) -> Path:
    """MCHC_PROPERTIES, else deploy.properties beside config.toml."""
    override = os.environ.get(PROPERTIES_ENV)
    return Path(override).expanduser() if override else Path(config_path).parent / PROPERTIES_FILE


def parse(text: str, origin: str = PROPERTIES_FILE, *, resolve: bool = True) -> dict:
    """KEY = VALUE lines; '#' starts a comment line; the first '=' separates.

    resolve=False returns each value as written, with its ${KEY} references unexpanded.
    """
    values = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep:
            raise PropertiesError(f"{origin}:{number}: expected KEY = VALUE, got {raw.strip()!r}")
        if not _KEY_NAME.match(name):
            raise PropertiesError(f"{origin}:{number}: key {name!r} must be UPPER_SNAKE_CASE")
        if _SECRET.search(name):
            raise PropertiesError(
                f"{origin}:{number}: {name} looks like a credential. {PROPERTIES_FILE} holds "
                "coordinates only (profiles, regions, bucket URIs, role names, paths); keep "
                "secrets in the AWS profile or the harness's own login.")
        if name in values:
            raise PropertiesError(f"{origin}:{number}: {name} is defined twice")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return _resolve_references(values, origin) if resolve else values


def _resolve_references(values: dict, origin: str) -> dict:
    resolved = {}

    def expand(name, chain):
        if name in resolved:
            return resolved[name]
        if name in chain:
            raise PropertiesError(f"{origin}: circular reference {' -> '.join([*chain, name])}")
        if name not in values:
            raise PropertiesError(f"{origin}: {chain[-1]} references ${{{name}}}, which is not defined")
        text = _REFERENCE.sub(lambda m: expand(m.group(1), [*chain, name]), values[name])
        resolved[name] = text
        return text

    for name in values:
        expand(name, [])
    return resolved


def load(path: Path | None) -> Properties:
    if path is None or not Path(path).is_file():
        return Properties()
    path = Path(path).resolve()
    text = path.read_text(encoding="utf-8")
    live = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    return Properties(path, parse(text, str(path)), set(_REFERENCE.findall(live)))


def interpolate(data, props: Properties, origin: str = "config.toml"):
    """Replace ${KEY} in every string of parsed TOML (keys included), after parsing.

    Substituting into parsed values rather than raw text means no property value can
    change the TOML structure. A string that is exactly one reference to an empty
    property becomes None, so `profile = "${AWS_PROFILE}"` can mean "ambient chain".
    Returns (data, references) where references maps dotted paths to the keys used.
    """
    references = {}

    def text(value, path):
        names = _REFERENCE.findall(value)
        if not names:
            return value
        for name in names:
            if name not in props.values:
                where = f" ({props.path})" if props.path else f" (no {PROPERTIES_FILE} found)"
                raise PropertiesError(f"{origin}: {path or 'a key'} references ${{{name}}}, "
                                      f"which {PROPERTIES_FILE} does not define{where}")
        references[path] = names
        whole = _REFERENCE.fullmatch(value)
        if whole and props.values[whole.group(1)] == "":
            return None
        return _REFERENCE.sub(lambda m: props.values[m.group(1)], value)

    def walk(value, path):
        if isinstance(value, str):
            return text(value, path)
        if isinstance(value, list):
            return [walk(item, f"{path}[{i}]") for i, item in enumerate(value)]
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                name = text(key, path) if isinstance(key, str) else key
                out[name] = walk(item, f"{path}.{name}" if path else name)
            return out
        return value

    return walk(data, ""), references


RUNTIMES = ("native", "agentcore")
KIRO_AUTHS = ("copy-host-login", "login-in-home")
HARNESS_SOURCES = ("pinned", "installed", "latest")
ISOLATIONS = ("shared", "per-user")
CONFIG_DEFAULT_CHOICES = ("default", "overrides", "none")


def _typed(key: Key, value: str, base: Path):
    if key.kind == "agentcore_prefix":
        from harness_image.iam import validate_runtime_name_prefix
        try:
            validate_runtime_name_prefix(value)
        except ValueError as exc:
            # Derive a concrete suggestion: replace invalid chars with underscores
            import re as _re
            suggestion = _re.sub(r'[^A-Za-z0-9_]', '_', value)
            if suggestion and not suggestion[0].isalpha():
                suggestion = "a_" + suggestion.lstrip("_")
            raise PropertiesError(
                str(exc),
                gate="human",
                fix=(f"correct AGENTCORE_RUNTIME_NAME_PREFIX: use only letters, digits and underscores "
                     f"(no hyphens); e.g. {suggestion!r}"),
            ) from None
        return value
    if key.kind == "iam_role_name":
        from harness_image.iam import validate_iam_role_name
        try:
            validate_iam_role_name(value)
        except ValueError as exc:
            raise PropertiesError(
                str(exc),
                gate="human",
                fix="correct AGENTCORE_EXECUTION_ROLE_NAME: use only [\\w+=,.@-] characters, length 1-64",
            ) from None
        return value
    if key.kind == "iam_policy_name":
        from harness_image.iam import validate_iam_policy_name
        try:
            validate_iam_policy_name(value)
        except ValueError as exc:
            raise PropertiesError(
                str(exc),
                gate="human",
                fix="correct AGENTCORE_INLINE_POLICY_NAME: use only [\\w+=,.@-] characters, length 1-128",
            ) from None
        return value
    if key.kind == "runtime":
        if value not in RUNTIMES:
            raise PropertiesError(f"{PROPERTIES_FILE}: RUNTIME must be one of {', '.join(RUNTIMES)}, got {value!r}")
        return value
    if key.kind == "kiro_auth":
        if value not in KIRO_AUTHS:
            raise PropertiesError(f"{PROPERTIES_FILE}: KIRO_AUTH must be one of {', '.join(KIRO_AUTHS)}, got {value!r}")
        from .host_setup import KIRO_LOGIN_FILE
        return [KIRO_LOGIN_FILE] if value == "copy-host-login" else []
    if key.kind == "harness_source":
        if value not in HARNESS_SOURCES:
            raise PropertiesError(
                f"{PROPERTIES_FILE}: {key.name} must be one of {', '.join(HARNESS_SOURCES)}, got {value!r}")
        return value
    if key.kind == "isolation":
        if value not in ISOLATIONS:
            raise PropertiesError(
                f"{PROPERTIES_FILE}: {key.name} must be one of {', '.join(ISOLATIONS)}, got {value!r}")
        return value
    if key.kind == "config_default_choice":
        if value not in CONFIG_DEFAULT_CHOICES:
            raise PropertiesError(
                f"{PROPERTIES_FILE}: {key.name} must be one of "
                f"{', '.join(CONFIG_DEFAULT_CHOICES)}, got {value!r}")
        return value
    if key.kind == "bool":
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
        raise PropertiesError(f"{PROPERTIES_FILE}: {key.name} must be 'true' or 'false', got {value!r}")
    if key.kind == "comma_list":
        return [item.strip() for item in value.split(",") if item.strip()]
    if key.kind == "namespace":
        if not re.match(r'^[a-z0-9][a-z0-9-]*\Z', value):
            raise PropertiesError(
                f"{PROPERTIES_FILE}: {key.name} must be [a-z0-9-] starting with [a-z0-9], got {value!r}")
        return value
    if key.kind == "int":
        try:
            return int(value)
        except ValueError:
            raise PropertiesError(f"{PROPERTIES_FILE}: {key.name} must be an integer, got {value!r}") from None
    if key.kind == "path":
        return _path(value, base)
    if key.kind == "paths":
        return [_path(item.strip(), base) for item in value.split(",") if item.strip()]
    return value


def _path(value: str, base: Path) -> str:
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (base / path).resolve())


def comparable(name: str, value, base: Path):
    """A config.toml value in the form its property binding produces, for override checks."""
    key = KNOWN.get(name)
    if key and key.kind in ("path", "paths") and isinstance(value, (str, list)):
        items = value if isinstance(value, list) else [value]
        resolved = [_path(str(item), base) for item in items]
        return resolved if isinstance(value, list) else resolved[0]
    return value


def bound_defaults(props: Properties) -> tuple[dict, dict]:
    """Nested Config defaults from the known keys, and which key filled each field."""
    defaults, sources = {}, {}
    for name, value in props.values.items():
        key = KNOWN.get(name)
        if key is None or value == "":
            continue
        typed = _typed(key, value, props.base)
        for dotted in key.fields:
            node = defaults
            *parents, leaf = dotted.split(".")
            for part in parents:
                node = node.setdefault(part, {})
            node[leaf] = typed
            sources[dotted] = name
    return defaults, sources


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _leaf(data: dict, dotted: str):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def render(example: str, values: dict) -> str:
    """Fill KEY = lines of the example (commented or not) with values; keep the rest.

    A key's live line is preferred to a commented one, so filling a key never leaves
    it defined twice.
    """
    pattern = re.compile(r"^\s*(#?)\s*([A-Z][A-Z0-9_]*)\s*=")
    live = {m.group(2) for m in map(pattern.match, example.splitlines()) if m and not m.group(1)}
    lines, seen = [], set()
    for line in example.splitlines():
        match = pattern.match(line)
        name = match.group(2) if match else None
        if match and match.group(1) and name in live:
            name = None
        if name in values and name not in seen and not line.lstrip().startswith("##"):
            lines.append(f"{name} = {values[name]}")
            seen.add(name)
        else:
            lines.append(line)
    extra = [f"{name} = {value}" for name, value in values.items() if name not in seen]
    if extra:
        lines += ["", "# Additional values", *extra]
    return "\n".join(lines) + "\n"
