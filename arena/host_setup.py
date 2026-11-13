# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reuse an installed CLI's provider setup without sharing its task sessions."""
import glob as _glob
import json
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

from .isolation import copy_tree, make_writable

# Top-level keys in ~/.claude/settings.json that control which model Claude Code
# uses. Any of these in a copied settings.json would override the model and effort
# the comparison configured, so they are stripped before the run starts.
CLAUDE_MODEL_POLICY_KEYS = (
    "model", "availableModels", "enforceAvailableModels",
    "modelOverrides", "fallbackModel", "modelSettings", "effortLevel",
)
# Keys inside the "env" block that also force a model choice.
CLAUDE_MODEL_POLICY_ENV = ("ANTHROPIC_MODEL",)


def strip_claude_model_policy(settings_path: Path) -> list[str]:
    """Remove model-policy keys from a Claude settings.json in place.

    Strips CLAUDE_MODEL_POLICY_KEYS from the top level and CLAUDE_MODEL_POLICY_ENV
    from the "env" sub-dict. Writes atomically and preserves file mode.
    Returns the names removed (env entries as "env.KEY"); returns [] when the
    file is absent, unreadable, or contains invalid JSON (left untouched).
    """
    if not settings_path.is_file():
        return []
    try:
        raw = settings_path.read_bytes()
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    removed = []
    for key in CLAUDE_MODEL_POLICY_KEYS:
        if key in data:
            del data[key]
            removed.append(key)
    env_block = data.get("env")
    if isinstance(env_block, dict):
        for key in CLAUDE_MODEL_POLICY_ENV:
            if key in env_block:
                del env_block[key]
                removed.append(f"env.{key}")
    if not removed:
        return []
    mode = settings_path.stat().st_mode
    with tempfile.NamedTemporaryFile(
        mode="w", dir=settings_path.parent, delete=False, suffix=".tmp"
    ) as output:
        json.dump(data, output, indent=2)
        tmp_name = output.name
    os.chmod(tmp_name, mode)
    os.replace(tmp_name, settings_path)
    return removed


def _copy_file(source, destination):
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        with source.open("rb") as incoming:
            shutil.copyfileobj(incoming, output)
        os.replace(output.name, destination)
    return True


# Per-platform kiro data directory that holds the login (data.sqlite3) and session state.
# Kept as a field inside DEFAULT_HOME_SOURCES["kiro"] so existing tests that patch
# DEFAULT_HOME_SOURCES can control the login path without touching the config-dir logic.
_KIRO_DATA_FALLBACK = ("~/Library/Application Support/kiro-cli" if sys.platform == "darwin"
                       else "~/.local/share/kiro-cli")

# Where each CLI keeps its own data/login directory on this workstation, and where
# that directory belongs inside a run's private HOME.  For kiro this is the per-platform
# data dir (login, sessions); kiro's non-secret *config* lives in KIRO_CONFIG_SOURCE below.
# Honouring the CLI's own environment override first means an operator who has already
# moved CODEX_HOME or CLAUDE_CONFIG_DIR keeps that choice.
DEFAULT_HOME_SOURCES = {
    "claude": ("CLAUDE_CONFIG_DIR", "~/.claude", ".claude"),
    "codex": ("CODEX_HOME", "~/.codex", ".codex"),
    # kiro-cli follows the platform convention rather than XDG on macOS; the data dir
    # holds data.sqlite3 (the login). The destination stays the Linux path a microVM
    # expects; the native runtime mirrors it into Library/Application Support for a
    # local run. Non-secret configuration is in KIRO_CONFIG_SOURCE below.
    "kiro": (None, _KIRO_DATA_FALLBACK, ".local/share/kiro-cli"),
}

# Kiro's global configuration directory — steering files, agents, skills, MCP config.
# Separate from the per-platform data directory (data.sqlite3 login) that DEFAULT_HOME_SOURCES["kiro"]
# tracks. The destination prefix is .kiro inside the run's private HOME.
KIRO_CONFIG_SOURCE = (None, "~/.kiro", ".kiro")

# Individual files (relative to the source dir) copied from a defaulted source.
DEFAULT_HOME_CONFIG = {
    "claude": ("settings.json", "CLAUDE.md"),
    "codex": ("config.toml", "AGENTS.md", "AGENTS.override.md"),
    # settings/mcp.json from ~/.kiro/settings/mcp.json
    "kiro": ("settings/mcp.json",),
}

# Directories (relative to the source dir) copied from a defaulted source.
# plugins/ for Claude is deliberately absent — it is large and caches marketplaces;
# opt in per-harness via H_CONFIG_INCLUDE.
DEFAULT_HOME_DIRS: dict[str, tuple] = {
    "claude": ("skills", "agents", "commands", "rules", "output-styles"),
    # ~/.codex/skills is the legacy skill directory; ~/.agents/skills is handled separately.
    "codex": ("prompts", "rules", "skills"),
    # hooks/ is optional and only copied if present.
    "kiro": ("steering", "agents", "skills", "hooks"),
}

# A login is never copied implicitly. Naming one of these in refresh_files is the
# operator's explicit, recorded decision to put it inside a sandbox or a microVM.
HOME_SECRETS = {
    "claude": (".credentials.json",),
    "codex": ("auth.json",),
    "kiro": ("data.sqlite3",),
}

# Maximum bytes of non-secret config copied from defaults per harness.
# Exposed as config.toml behaviour key harness_config_size_cap_mb.
HOME_CONFIG_SIZE_CAP_BYTES = 50 * 1024 * 1024  # 50 MB (default)

# File names never copied from a config source.
_NEVER_COPY_NAMES = frozenset({
    "data.sqlite3", "auth.json", ".credentials.json",
})
# File suffixes indicating databases or credential files that must not be copied.
_NEVER_COPY_SUFFIXES = frozenset({".sqlite", ".sqlite3", ".db", ".credentials"})
# Directory names whose entire subtree is excluded (volatile / cache / session data).
_NEVER_COPY_DIRS = frozenset({
    "projects", "todos", "statsig", "shell-snapshots", "sessions", "session",
    "log", "logs", "cache", ".cache", "__pycache__",
})
# Env-var name suffixes that suggest the value is a secret (case-insensitive check).
_MCP_SECRET_SUFFIXES = ("_key", "_token", "_secret", "_password", "_credential",
                         "_apikey", "_api_key", "_access_key", "_auth", "_bearer",
                         "_private")


def _kiro_data_dir() -> Path:
    """Return the per-platform kiro data directory (login + sessions).

    Uses DEFAULT_HOME_SOURCES["kiro"][1] so tests that patch DEFAULT_HOME_SOURCES
    can control the login path in isolation.
    """
    return Path(DEFAULT_HOME_SOURCES["kiro"][1]).expanduser()


def _should_skip_config(name: str, path: Path) -> bool:
    """True when this name/path must never be copied from a default config source."""
    if name in _NEVER_COPY_NAMES:
        return True
    lower = name.lower()
    if any(lower.endswith(s) for s in _NEVER_COPY_SUFFIXES):
        return True
    if path.is_dir() and name in _NEVER_COPY_DIRS:
        return True
    return False


def _expand_globs(entries: list[str]) -> tuple[list[Path], list[str]]:
    """Expand ~ and glob wildcards in a list of path strings.

    Returns (matched_paths, unmatched_originals).  An entry with no wildcards
    that resolves to a non-existent path is also counted as unmatched.
    """
    matched: list[Path] = []
    unmatched: list[str] = []
    for entry in entries:
        expanded = os.path.expanduser(entry)
        if any(c in expanded for c in ('*', '?', '[', '{')):
            results = sorted(_glob.glob(expanded))
            if results:
                matched.extend(Path(p) for p in results)
            else:
                unmatched.append(entry)
        else:
            p = Path(expanded)
            if p.exists():
                matched.append(p)
            else:
                unmatched.append(entry)
    return matched, unmatched


def _detect_mcp_secret_names(mcp_servers: dict) -> dict[str, list[str]]:
    """Return {server_name: [env_key_names]} for MCP servers with secret-looking env keys.

    Only key names are reported, never values, so this is safe to include in doctor output.
    """
    result = {}
    for name, server in mcp_servers.items():
        if not isinstance(server, dict):
            continue
        env_block = server.get("env") or {}
        if not isinstance(env_block, dict):
            continue
        secret_keys = [k for k in env_block
                       if any(k.lower().endswith(s) for s in _MCP_SECRET_SUFFIXES)]
        # Also check headers (some MCP servers use HTTP headers for auth)
        headers = server.get("headers") or {}
        if isinstance(headers, dict):
            secret_keys += [f"headers.{k}" for k in headers
                            if any(k.lower().endswith(s) for s in _MCP_SECRET_SUFFIXES)]
        if secret_keys:
            result[name] = sorted(secret_keys)
    return result


def default_home_source(harness: str, config_dir: str | None = None):
    """(source directory, HOME-relative destination) for a harness's own config dir.

    config_dir, when given, overrides the env-var and built-in default — it comes
    from HarnessConfig.config_dir (deploy.properties key CLAUDE_CONFIG_DIR etc.).
    Returns (None, prefix) when this harness has no known default location.

    For kiro, returns the global config dir (~/.kiro) from KIRO_CONFIG_SOURCE, not
    the per-platform data dir; the data dir is used only for login detection via
    _kiro_data_dir().
    """
    if harness == "kiro":
        _, fallback, prefix = KIRO_CONFIG_SOURCE
        if config_dir is not None:
            return Path(config_dir).expanduser(), prefix
        return Path(fallback).expanduser(), prefix
    entry = DEFAULT_HOME_SOURCES.get(harness)
    if not entry:
        return None, None
    variable, fallback, prefix = entry
    if config_dir is not None:
        return Path(config_dir).expanduser(), prefix
    raw = (os.environ.get(variable) if variable else None) or fallback
    return Path(raw).expanduser(), prefix


def _mcp_servers_from_claude_json(path: Path) -> dict:
    """Read the 'mcpServers' block from ~/.claude.json; return {} on any error."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
        return data.get("mcpServers") or {} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _list_config_entries(harness: str, source: Path, config_include: list,
                         config_exclude: list) -> dict:
    """List what the default copy would produce from source (dry-run version).

    Returns a dict with:
      files: list of relative paths that would be copied
      dirs:  list of directory names that would be copied
      size_bytes: approximate bytes
      mcp_servers: list of MCP server names detected in the config
      mcp_secret_names: {server: [key_names]} for servers with secret-looking env keys
    """
    if not source.is_dir():
        return {"files": [], "dirs": [], "size_bytes": 0, "mcp_servers": [], "mcp_secret_names": {}}

    mcp_exclude = {e.removeprefix("mcp:") for e in config_exclude if e.startswith("mcp:")}
    path_exclude = {e for e in config_exclude if not e.startswith("mcp:")}

    files = []
    dirs = []
    total_bytes = 0
    mcp_servers: list[str] = []
    mcp_secret_names: dict[str, list[str]] = {}

    allowed_files = set(DEFAULT_HOME_CONFIG.get(harness, ()))
    allowed_files |= {e for e in config_include if not e.endswith("/")}
    allowed_dirs = set(DEFAULT_HOME_DIRS.get(harness, ()))
    allowed_dirs |= {e.rstrip("/") for e in config_include if e.endswith("/")}

    for name in sorted(allowed_files):
        if name in path_exclude:
            continue
        path = source / name
        if path.is_file() and not _should_skip_config(name, path):
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
            files.append(name)

    for name in sorted(allowed_dirs):
        if name in path_exclude:
            continue
        path = source / name
        if path.is_dir() and not _should_skip_config(name, path):
            dirs.append(name)
            try:
                for child in path.rglob("*"):
                    if child.is_file():
                        total_bytes += child.stat().st_size
            except OSError:
                pass

    # MCP servers from settings.json (Claude) or config.toml (Codex) or settings/mcp.json (Kiro)
    if harness == "claude":
        settings_path = source / "settings.json"
        if settings_path.is_file():
            try:
                data = json.loads(settings_path.read_text())
                servers = (data.get("mcpServers") or {}) if isinstance(data, dict) else {}
                servers = {k: v for k, v in servers.items() if k not in mcp_exclude}
                mcp_servers.extend(sorted(servers))
                mcp_secret_names.update(_detect_mcp_secret_names(servers))
            except Exception:
                pass
        # ~/.claude.json user-scope MCP servers
        global_claude = Path.home() / ".claude.json"
        global_servers = _mcp_servers_from_claude_json(global_claude)
        global_servers = {k: v for k, v in global_servers.items() if k not in mcp_exclude}
        for s in sorted(global_servers):
            if s not in mcp_servers:
                mcp_servers.append(s)
        mcp_secret_names.update(_detect_mcp_secret_names(
            {k: v for k, v in global_servers.items() if k not in mcp_secret_names}))
    elif harness == "codex":
        config_path = source / "config.toml"
        if config_path.is_file():
            try:
                data = tomllib.loads(config_path.read_text())
                servers = (data.get("mcp_servers") or {}) if isinstance(data, dict) else {}
                servers = {k: v for k, v in servers.items() if k not in mcp_exclude}
                mcp_servers.extend(sorted(servers))
                mcp_secret_names.update(_detect_mcp_secret_names(servers))
            except Exception:
                pass
    elif harness == "kiro":
        mcp_path = source / "settings" / "mcp.json"
        if mcp_path.is_file():
            try:
                data = json.loads(mcp_path.read_text())
                servers = (data.get("mcpServers") or {}) if isinstance(data, dict) else {}
                servers = {k: v for k, v in servers.items() if k not in mcp_exclude}
                mcp_servers.extend(sorted(servers))
                mcp_secret_names.update(_detect_mcp_secret_names(servers))
            except Exception:
                pass

    return {
        "files": files, "dirs": dirs, "size_bytes": total_bytes,
        "mcp_servers": mcp_servers, "mcp_secret_names": mcp_secret_names,
    }


def default_home_plan(harness: str, configured) -> dict:
    """What a defaulted HOME would copy, and which secrets it would leave behind."""
    config_dir = getattr(configured, "config_dir", None)
    source, prefix = default_home_source(harness, config_dir)
    config_include = getattr(configured, "config_include", []) or []
    config_exclude = getattr(configured, "config_exclude", []) or []

    secrets = HOME_SECRETS.get(harness, ())
    opted_in = sorted({relative for relative in configured.refresh_files
                       if Path(relative).name in secrets})

    if harness == "kiro":
        # Login file lives in the per-platform data dir, not in ~/.kiro.
        data_path = _kiro_data_dir() / "data.sqlite3"
        withheld = ["data.sqlite3"] if (data_path.is_file() and KIRO_LOGIN_FILE not in opted_in) else []
        entries = _list_config_entries(harness, source, config_include, config_exclude) if source else {
            "files": [], "dirs": [], "mcp_servers": [], "mcp_secret_names": {}, "size_bytes": 0}
    else:
        entries = _list_config_entries(harness, source, config_include, config_exclude) if source else {
            "files": [], "dirs": [], "mcp_servers": [], "mcp_secret_names": {}, "size_bytes": 0}
        withheld = ([name for name in secrets
                     if source and (source / name).is_file() and f"{prefix}/{name}" not in opted_in]
                    if source else [])

    present = entries["files"] + entries["dirs"]
    return {
        "source": str(source) if source else None,
        "source_present": bool(source and source.is_dir()),
        "destination": prefix,
        "configuration_files": entries["files"],
        "configuration_dirs": entries["dirs"],
        "mcp_servers": entries["mcp_servers"],
        "mcp_secret_names": entries["mcp_secret_names"],
        "secrets_withheld": withheld,
        "secrets_opted_in": opted_in,
    }


def _copy_config_entries(harness: str, source: Path, target: Path,
                         config_include: list, config_exclude: list,
                         size_cap_bytes: int = HOME_CONFIG_SIZE_CAP_BYTES) -> dict:
    """Copy default config files and dirs from source to target, applying filters and size cap.

    size_cap_bytes — override the default cap (harness_config_size_cap_mb in config.toml).
    Returns {"copied_files": [...], "copied_dirs": [...], "skipped_cap": bool, "size_bytes": int}.
    """
    if not source.is_dir():
        return {"copied_files": [], "copied_dirs": [], "skipped_cap": False, "size_bytes": 0}

    # Preserve declaration order for the default allowlist; append include-only entries.
    # Reject entries that are absolute or contain '..' segments — path traversal defence.
    def _is_safe_entry(name: str) -> bool:
        p = Path(name.rstrip("/"))
        return not p.is_absolute() and ".." not in p.parts

    default_files = list(DEFAULT_HOME_CONFIG.get(harness, ()))
    extra_files = [e for e in config_include
                   if not e.endswith("/") and e not in default_files and _is_safe_entry(e)]
    ordered_files = default_files + extra_files
    exclude_files = {e for e in config_exclude if not e.startswith("mcp:")}

    default_dirs = list(DEFAULT_HOME_DIRS.get(harness, ()))
    extra_dirs = [e.rstrip("/") for e in config_include
                  if e.endswith("/") and e.rstrip("/") not in default_dirs and _is_safe_entry(e)]
    ordered_dirs = default_dirs + extra_dirs
    exclude_dirs = exclude_files  # same set

    total_bytes = 0
    skipped_cap = False
    copied_files = []
    copied_dirs = []

    for name in ordered_files:
        if name in exclude_files:
            continue
        src_path = source / name
        if not src_path.exists():
            continue
        # Nested paths like settings/mcp.json need the parent made
        if src_path.is_file() and not _should_skip_config(Path(name).name, src_path):
            try:
                size = src_path.stat().st_size
            except OSError:
                continue
            if total_bytes + size > size_cap_bytes:
                skipped_cap = True
                continue
            total_bytes += size
            if _copy_file(src_path, target / name):
                copied_files.append(name)

    for name in ordered_dirs:
        if name in exclude_dirs:
            continue
        src_path = source / name
        if not src_path.is_dir():
            continue
        if _should_skip_config(name, src_path):
            continue
        # Measure dir size first
        dir_bytes = 0
        try:
            for child in src_path.rglob("*"):
                if child.is_file() and not _should_skip_config(child.name, child):
                    dir_bytes += child.stat().st_size
        except OSError:
            pass
        if total_bytes + dir_bytes > size_cap_bytes:
            skipped_cap = True
            continue
        total_bytes += dir_bytes
        dest_path = target / name
        dest_path.mkdir(parents=True, exist_ok=True)
        # Copy directory contents, applying never-copy filters.
        # Use a depth-limited walk to avoid symlink loops; symlinks not followed.
        _copy_config_subdir(src_path, dest_path, src_path)
        copied_dirs.append(name)

    return {"copied_files": copied_files, "copied_dirs": copied_dirs,
            "skipped_cap": skipped_cap, "size_bytes": total_bytes}


def _copy_config_subdir(source: Path, target: Path, source_root: Path, _depth: int = 0):
    """Copy source dir into target, skipping never-copy entries, not following symlinks."""
    if _depth > 20:
        return  # guard against deep trees
    target.mkdir(parents=True, exist_ok=True)
    try:
        items = list(source.iterdir())
    except OSError:
        return
    for item in items:
        name = item.name
        if _should_skip_config(name, item):
            continue
        if item.is_symlink():
            resolved = item.resolve()
            if not resolved.is_relative_to(source_root.resolve()):
                continue  # symlink escapes source root — skip
            if resolved.is_dir():
                _copy_config_subdir(resolved, target / name, source_root, _depth + 1)
            elif resolved.is_file():
                _copy_file(resolved, target / name)
        elif item.is_dir():
            _copy_config_subdir(item, target / name, source_root, _depth + 1)
        elif item.is_file():
            _copy_file(item, target / name)


_S3_PREFIX_CACHE_SIZE_CAP = 50 * 1024 * 1024  # 50 MB per-run S3 prefix cache


def _apply_s3_config_prefix(harness: str, s3_uri: str, home: Path,
                             aws_profile: str | None,
                             config_exclude: list) -> dict:
    """Download an S3 prefix into a per-run cache dir and overlay it into HOME.

    Called between the default seed block and explicit overrides (defaults < S3 < overrides).
    Uses the host AWS profile; applies the same _should_skip_config and size-cap filters.
    Returns a detail dict summarising what was copied.
    """
    from urllib.parse import urlsplit
    u = urlsplit(s3_uri)
    bucket = u.netloc
    prefix = u.path.lstrip("/")
    if not bucket:
        return {"mode": "s3_prefix", "error": f"invalid s3_uri: {s3_uri!r}", "copied": []}

    # Build a per-run S3 cache dir alongside home
    cache_dir = home.parent / (home.name + "-s3-cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        import boto3
        session = boto3.Session(profile_name=aws_profile)
        s3 = session.client("s3")

        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        objects = resp.get("Contents", [])
        while resp.get("IsTruncated"):
            resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix,
                                      ContinuationToken=resp["NextContinuationToken"])
            objects.extend(resp.get("Contents", []))
    except Exception as exc:
        return {"mode": "s3_prefix", "uri": s3_uri, "error": str(exc), "copied": []}

    path_exclude = {e for e in config_exclude if not e.startswith("mcp:")}
    copied = []
    total_bytes = 0
    for obj in objects:
        key = obj["Key"]
        rel_key = key[len(prefix):].lstrip("/") if key.startswith(prefix) else key
        if not rel_key:
            continue
        size = obj.get("Size", 0)
        if total_bytes + size > _S3_PREFIX_CACHE_SIZE_CAP:
            break
        name = Path(rel_key).name
        if _should_skip_config(name, Path(rel_key)):
            continue
        if any(rel_key == e or rel_key.startswith(e.rstrip("/") + "/") for e in path_exclude):
            continue
        dest = home / rel_key
        # Guard against path-traversal in crafted S3 object keys (e.g. keys with
        # '..' components are valid in S3 but must not escape the run HOME directory).
        if not dest.resolve().is_relative_to(home.resolve()):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            dest.write_bytes(body)
            total_bytes += size
            copied.append(rel_key)
        except Exception:
            pass  # skip unreadable objects; don't abort the whole overlay

    return {"mode": "s3_prefix", "uri": s3_uri, "copied": copied, "bytes": total_bytes}


def _seed_default_home(harness: str, configured, home: Path, *,
                       size_cap_bytes: int = HOME_CONFIG_SIZE_CAP_BYTES) -> dict:
    """Copy a harness's own non-secret configuration into this run's private HOME.

    size_cap_bytes — per-run cap derived from config.harness_config_size_cap_mb * 1024 * 1024.
    """
    config_dir = getattr(configured, "config_dir", None)
    source, prefix = default_home_source(harness, config_dir)
    if not source or not source.is_dir():
        return {"mode": "empty private HOME", "source": str(source) if source else None,
                "source_present": False, "copied": []}
    if source == Path.home():
        raise ValueError("A harness configuration directory must not be your personal HOME")
    target = home / prefix
    config_include = getattr(configured, "config_include", []) or []
    config_exclude = getattr(configured, "config_exclude", []) or []

    result = _copy_config_entries(harness, source, target, config_include, config_exclude,
                                  size_cap_bytes=size_cap_bytes)
    copied = result["copied_files"] + result["copied_dirs"]

    # For Claude, also copy user-scope MCP servers from ~/.claude.json.
    if harness == "claude":
        mcp_exclude = {e.removeprefix("mcp:") for e in config_exclude if e.startswith("mcp:")}
        global_claude = Path.home() / ".claude.json"
        if global_claude.is_file():
            global_servers = _mcp_servers_from_claude_json(global_claude)
            global_servers = {k: v for k, v in global_servers.items() if k not in mcp_exclude}
            if global_servers:
                _merge_claude_mcp_servers(home / ".claude.json", global_servers)

    # For Codex, also copy user skills from ~/.agents/skills (documented user skills dir).
    if harness == "codex" and "codex/" not in config_exclude:
        agents_skills = Path.home() / ".agents" / "skills"
        if agents_skills.is_dir():
            dest_agents = home / ".agents" / "skills"
            dest_agents.mkdir(parents=True, exist_ok=True)
            _copy_config_subdir(agents_skills, dest_agents, agents_skills)
            if any(dest_agents.iterdir()):
                copied.append(".agents/skills")

    plan = default_home_plan(harness, configured)
    return {"mode": "harness configuration directory", "source": str(source),
            "source_present": True, "destination": prefix, "copied": copied,
            "skipped_size_cap": result["skipped_cap"],
            "mcp_servers": plan["mcp_servers"],
            "mcp_secret_names": plan["mcp_secret_names"],
            "secrets_withheld": plan["secrets_withheld"],
            "secrets_opted_in": plan["secrets_opted_in"]}


def _merge_claude_mcp_servers(target_path: Path, servers: dict):
    """Merge MCP servers into a .claude.json file (create if absent)."""
    existing = {}
    if target_path.is_file():
        try:
            existing = json.loads(target_path.read_text())
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    existing.setdefault("mcpServers", {}).update(servers)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=target_path.parent, delete=False) as output:
        json.dump(existing, output)
        os.replace(output.name, target_path)


def _refresh(source_root: Path, home: Path, relatives, translate=None):
    """Re-copy named files on continuation, refusing any path that leaves either root."""
    refreshed = []
    for relative in relatives:
        inside = translate(relative) if translate else relative
        if inside is None:
            continue
        source = (source_root / inside).resolve()
        destination = (home / relative).resolve()
        if not source.is_relative_to(source_root.resolve()) or not destination.is_relative_to(home.resolve()):
            raise ValueError("Credential refresh escaped the dedicated HOME")
        if _copy_file(source, destination):
            refreshed.append(relative)
    return refreshed


def _apply_skills_dirs(harness: str, skills_dirs: list, home: Path) -> dict:
    """Copy skill directories from skills_dirs into the harness's skills location.

    Entries may contain ~ and glob wildcards; each resolved path must be a directory.
    Returns {"copied": [...], "unmatched": [...]}.
    """
    if not skills_dirs:
        return {"copied": [], "unmatched": []}
    if harness == "claude":
        dest_root = home / ".claude" / "skills"
    elif harness == "codex":
        dest_root = home / ".agents" / "skills"
    elif harness == "kiro":
        dest_root = home / ".kiro" / "skills"
    else:
        return {"copied": [], "unmatched": list(skills_dirs)}

    expanded, unmatched = _expand_globs(list(skills_dirs))
    # Only directories are valid skill sources; report non-dirs as unmatched.
    dirs = [p for p in expanded if p.is_dir()]
    unmatched.extend(str(p) for p in expanded if not p.is_dir())

    copied = []
    for src in dirs:
        dest_root.mkdir(parents=True, exist_ok=True)
        # Each child of a skills dir is a skill folder.
        for skill_dir in sorted(src.iterdir()):
            if skill_dir.is_dir():
                dest = dest_root / skill_dir.name
                dest.mkdir(parents=True, exist_ok=True)
                _copy_config_subdir(skill_dir, dest, skill_dir)
                copied.append(str(skill_dir))
    return {"copied": copied, "unmatched": unmatched}


def _apply_steering_files(harness: str, steering_files: list, home: Path) -> dict:
    """Copy steering files into the harness's steering/rules location.

    Entries may contain ~ and glob wildcards.  Directories are expanded to all
    *.md files inside them.  For Claude: files go into .claude/rules/ (each as-is).
    For Codex: files are concatenated into .codex/AGENTS.md in order.
    For Kiro: files go into .kiro/steering/ (each as-is).
    Returns {"used": [...], "unmatched": [...]}.
    """
    if not steering_files:
        return {"used": [], "unmatched": []}

    expanded, unmatched = _expand_globs(list(steering_files))

    # Expand directories to all *.md files inside them; empty dirs count as unmatched.
    paths: list[Path] = []
    for p in expanded:
        if p.is_dir():
            md_files = sorted(p.glob("*.md"))
            if md_files:
                paths.extend(md_files)
            else:
                unmatched.append(str(p))
        elif p.is_file():
            paths.append(p)

    if not paths:
        return {"used": [], "unmatched": unmatched}

    used: list[str] = []
    if harness == "claude":
        dest_dir = home / ".claude" / "rules"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for src in paths:
            _copy_file(src, dest_dir / src.name)
            used.append(str(src))
    elif harness == "codex":
        dest = home / ".codex" / "AGENTS.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        combined = "\n".join(p.read_text(errors="replace") for p in paths)
        with tempfile.NamedTemporaryFile(mode="w", dir=dest.parent, delete=False) as out:
            out.write(combined)
            os.replace(out.name, dest)
        used = [str(p) for p in paths]
    elif harness == "kiro":
        dest_dir = home / ".kiro" / "steering"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for src in paths:
            _copy_file(src, dest_dir / src.name)
            used.append(str(src))
    return {"used": used, "unmatched": unmatched}


def _apply_mcp_config(harness: str, mcp_config: str, home: Path,
                      config_exclude: list) -> dict:
    """Merge an explicit MCP config file into the run's harness config.

    mcp_config may contain ~ and glob wildcards; all matching files are merged in
    sorted order.  Returns {"merged": bool, "servers": list[str], "secret_names": dict,
    "unmatched": list[str]}.
    """
    if not mcp_config:
        return {"merged": False, "servers": [], "secret_names": {}, "unmatched": []}
    matched, unmatched = _expand_globs([mcp_config])
    files = [p for p in matched if p.is_file()]
    unmatched.extend(str(p) for p in matched if not p.is_file())
    if not files:
        return {"merged": False, "servers": [], "secret_names": {}, "unmatched": unmatched}
    # Use the first matching file (or only file) as the canonical src; for multiple
    # matches they are merged in sorted order below.
    src = files[0]
    if not src.is_file():
        return {"merged": False, "servers": [], "secret_names": {}, "unmatched": unmatched}

    mcp_exclude = {e.removeprefix("mcp:") for e in config_exclude if e.startswith("mcp:")}

    if harness == "claude":
        try:
            data = json.loads(src.read_text())
        except Exception:
            return {"merged": False, "servers": [], "secret_names": {}, "unmatched": unmatched}
        servers = (data.get("mcpServers") or {}) if isinstance(data, dict) else {}
        servers = {k: v for k, v in servers.items() if k not in mcp_exclude}
        if servers:
            _merge_claude_mcp_servers(home / ".claude.json", servers)
        return {"merged": bool(servers), "servers": sorted(servers),
                "secret_names": _detect_mcp_secret_names(servers), "unmatched": unmatched}

    elif harness == "codex":
        try:
            data = tomllib.loads(src.read_text())
        except Exception:
            return {"merged": False, "servers": [], "secret_names": {}, "unmatched": unmatched}
        servers = (data.get("mcp_servers") or {}) if isinstance(data, dict) else {}
        servers = {k: v for k, v in servers.items() if k not in mcp_exclude}
        if servers:
            cfg_path = home / ".codex" / "config.toml"
            existing = {}
            if cfg_path.is_file():
                try:
                    existing = tomllib.loads(cfg_path.read_text())
                except Exception:
                    pass
            existing.setdefault("mcp_servers", {}).update(servers)
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            # tomllib is read-only; write TOML manually for the mcp_servers key.
            _write_codex_mcp(cfg_path, existing)
        return {"merged": bool(servers), "servers": sorted(servers),
                "secret_names": _detect_mcp_secret_names(servers), "unmatched": unmatched}

    elif harness == "kiro":
        try:
            data = json.loads(src.read_text())
        except Exception:
            return {"merged": False, "servers": [], "secret_names": {}, "unmatched": unmatched}
        servers = (data.get("mcpServers") or {}) if isinstance(data, dict) else {}
        servers = {k: v for k, v in servers.items() if k not in mcp_exclude}
        if servers:
            mcp_path = home / ".kiro" / "settings" / "mcp.json"
            mcp_path.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if mcp_path.is_file():
                try:
                    existing = json.loads(mcp_path.read_text())
                    if not isinstance(existing, dict):
                        existing = {}
                except Exception:
                    existing = {}
            existing.setdefault("mcpServers", {}).update(servers)
            with tempfile.NamedTemporaryFile(mode="w", dir=mcp_path.parent, delete=False) as out:
                json.dump(existing, out, indent=2)
                os.replace(out.name, mcp_path)
        return {"merged": bool(servers), "servers": sorted(servers),
                "secret_names": _detect_mcp_secret_names(servers), "unmatched": unmatched}

    return {"merged": False, "servers": [], "secret_names": {}, "unmatched": unmatched}


def _toml_inline_value(v) -> str:
    """Render a Python value as a TOML inline expression (no newlines).

    Handles the types that appear in MCP server field values: str, bool, int,
    float, list (of str/int), and dict (inline table).  JSON-style ``{...}``
    is *not* valid TOML; inline tables use ``{key = value, ...}`` notation.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_inline_value(i) for i in v) + "]"
    if isinstance(v, dict):
        pairs = ", ".join(f"{k} = {_toml_inline_value(w)}" for k, w in v.items())
        return "{" + pairs + "}"
    # Fallback: JSON-encode anything else (will be a string representation)
    return json.dumps(str(v))


def _write_codex_mcp(cfg_path: Path, data: dict):
    """Write config.toml with updated mcp_servers block (minimal TOML serialiser)."""
    lines = []
    for key, value in data.items():
        if key == "mcp_servers":
            continue
        if isinstance(value, str):
            lines.append(f'{key} = {json.dumps(value)}')
        elif isinstance(value, bool):
            lines.append(f'{key} = {"true" if value else "false"}')
        elif isinstance(value, (int, float)):
            lines.append(f'{key} = {value}')
    if data.get("mcp_servers"):
        lines.append("")
        lines.append("[mcp_servers]")
        for name, server in data["mcp_servers"].items():
            lines.append(f"[mcp_servers.{name}]")
            if isinstance(server, dict):
                for k, v in server.items():
                    lines.append(f"  {k} = {_toml_inline_value(v)}")
    content = "\n".join(lines) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", dir=cfg_path.parent, delete=False) as out:
        out.write(content)
        os.replace(out.name, cfg_path)


def configuration_plan(config, harness: str) -> dict:
    """Full configuration plan for a harness: every source, files, skills, MCP names.

    Used by the /api/harness-configuration endpoint and the doctor preflight check.
    Secret values are never included; only key names that look secret are reported.

    Precedence (lowest to highest):
      defaults < S3 prefix < explicit files (skills_dirs/steering_files/mcp_config) < home_template
    """
    configured = config.harnesses[harness]
    config_dir = getattr(configured, "config_dir", None)
    config_include = getattr(configured, "config_include", []) or []
    config_exclude = getattr(configured, "config_exclude", []) or []
    skills_dirs = getattr(configured, "skills_dirs", []) or []
    steering_files = getattr(configured, "steering_files", []) or []
    mcp_config = getattr(configured, "mcp_config", None)
    config_s3_uri = getattr(configured, "config_s3_uri", None)

    sources = []

    if configured.home_template:
        template = Path(configured.home_template).expanduser()
        sources.append({
            "type": "home_template",
            "path": str(template),
            "present": template.is_dir(),
            "note": "Replaces all defaults; operator-assembled directory copied whole.",
            "files": [], "dirs": [], "skills": [], "mcp_servers": [], "mcp_secret_names": {},
            "steering_docs": [],
        })
        return {"harness": harness, "mode": "home_template",
                "sources": sources, "precedence": ["home_template"],
                "inherit_host_setup": configured.inherit_host_setup,
                "runtime": getattr(config, "runtime", "native")}

    # --- Default source ---
    source, prefix = default_home_source(harness, config_dir)
    if source is not None:
        entries = _list_config_entries(harness, source, config_include, config_exclude)
        sources.append({
            "type": "default",
            "path": str(source),
            "destination": prefix,
            "present": source.is_dir(),
            "files": entries["files"],
            "dirs": entries["dirs"],
            "skills": [d for d in entries["dirs"] if "skills" in d],
            "mcp_servers": entries["mcp_servers"],
            "mcp_secret_names": entries["mcp_secret_names"],
            "steering_docs": [f for f in entries["files"] if f in ("CLAUDE.md", "AGENTS.md",
                                                                     "AGENTS.override.md")
                              ] + [d for d in entries["dirs"] if d in ("steering", "rules")],
            "size_bytes": entries["size_bytes"],
            "config_include": config_include,
            "config_exclude": config_exclude,
        })

    # --- S3 prefix source ---
    if config_s3_uri:
        sources.append({
            "type": "s3_prefix",
            "uri": config_s3_uri,
            "note": "Downloaded host-side into per-run cache; overlaid after defaults.",
            "files": [], "dirs": [], "skills": [], "mcp_servers": [], "mcp_secret_names": {},
            "steering_docs": [],
        })

    # --- Explicit overrides ---
    override_skills = []
    for sd in skills_dirs:
        p = Path(sd).expanduser()
        if p.is_dir():
            override_skills.extend(
                str(child) for child in sorted(p.iterdir()) if child.is_dir())
    override_steering = [str(Path(f).expanduser()) for f in steering_files
                         if Path(f).expanduser().is_file()]
    mcp_override_servers: list[str] = []
    mcp_override_secrets: dict[str, list[str]] = {}
    if mcp_config:
        mp = Path(mcp_config).expanduser()
        if mp.is_file():
            try:
                if harness == "codex":
                    mdata = tomllib.loads(mp.read_text())
                    servers = mdata.get("mcp_servers") or {}
                else:
                    mdata = json.loads(mp.read_text())
                    servers = mdata.get("mcpServers") or {}
                mcp_override_servers = sorted(servers)
                mcp_override_secrets = _detect_mcp_secret_names(servers)
            except Exception:
                pass

    if skills_dirs or steering_files or mcp_config:
        sources.append({
            "type": "explicit_overrides",
            "skills_dirs": skills_dirs,
            "steering_files": steering_files,
            "mcp_config": mcp_config,
            "skills": override_skills,
            "steering_docs": override_steering,
            "mcp_servers": mcp_override_servers,
            "mcp_secret_names": mcp_override_secrets,
            "files": [], "dirs": [],
        })

    # Kiro login withheld notice
    if harness == "kiro":
        data_dir = _kiro_data_dir()
        login = data_dir / "data.sqlite3"
        withheld = ["data.sqlite3"] if login.is_file() else []
    else:
        plan = default_home_plan(harness, configured)
        withheld = plan.get("secrets_withheld", [])

    # Compute the effective default choice (single source of truth in Config property).
    effective_default = (config.default_configuration_choice
                         if hasattr(config, "default_configuration_choice")
                         else "default")
    reads_workstation_folder = (effective_default == "default")
    return {
        "harness": harness,
        "mode": ("overrides" if not source else "default"),
        "effective_default_choice": effective_default,
        "reads_workstation_folder": reads_workstation_folder,
        "sources": sources,
        "precedence": ["defaults", "s3_prefix", "explicit_files", "home_template"],
        "inherit_host_setup": configured.inherit_host_setup,
        "runtime": getattr(config, "runtime", "native"),
        "withheld_secrets": withheld,
    }


def prepare_home(harness: str, configured, home: Path, *,
                 configuration_choice: str = "default",
                 size_cap_bytes: int = HOME_CONFIG_SIZE_CAP_BYTES) -> dict:
    """Seed a run's private HOME once, then refresh the files named for continuation.

    configuration_choice controls what is seeded:
      "default"   — copy harness defaults (files, dirs, MCP) then apply overrides
      "overrides" — skip defaults; apply only skills_dirs/steering_files/mcp_config
      "none"      — leave HOME empty (clean sandbox, no operator config)

    size_cap_bytes — maximum bytes of config copied; callers pass
      config.harness_config_size_cap_mb * 1024 * 1024 to honour the operator's cap.
      Defaults to HOME_CONFIG_SIZE_CAP_BYTES (50 MB) when not supplied.

    An explicitly configured home_template is copied whole regardless of the choice:
    the operator assembled that directory for this purpose. Without one, the harness's
    own configuration directory supplies non-secret configuration only.
    """
    config_exclude = getattr(configured, "config_exclude", []) or []

    seeded = not home.exists()
    home.mkdir(parents=True, exist_ok=True)
    template = None
    if configured.home_template:
        template = Path(configured.home_template).expanduser().resolve()
        if template == Path.home():
            raise ValueError("Use a dedicated HOME template, not your personal HOME")
        # Point 2: a configured template that does not exist blocks every attempt.
        key_name = f"{harness.upper()}_HOME_TEMPLATE"
        if not template.is_dir():
            raise ValueError(
                f"{key_name}={configured.home_template!r} does not exist at {template}. "
                f"Create that directory (or remove {key_name} from deploy.properties) before running.")
        if seeded:
            copy_tree(template, home)
        detail = {"mode": "configured home_template", "source": str(template), "source_present": True}
        # Point 1: KIRO_AUTH=copy-host-login always fetches the login from the host data
        # directory, regardless of whether a home_template is configured.  The template
        # supplies non-secret configuration only.
        if harness == "kiro" and KIRO_LOGIN_FILE in (configured.refresh_files or []):
            detail["refreshed"] = _refresh_kiro(configured.refresh_files, home)
        else:
            detail["refreshed"] = _refresh(template, home, configured.refresh_files)
        if harness == "claude":
            removed = strip_claude_model_policy(home / ".claude" / "settings.json")
            if removed:
                detail["model_policy_removed"] = removed
        return detail

    # Check inherit_host_setup on AgentCore — not supported.
    runtime = getattr(configured, "_runtime_override", None)  # injected by agentcore_sandbox if needed
    if configured.inherit_host_setup and runtime == "agentcore":
        raise ValueError(
            f"inherit_host_setup is not supported on the AgentCore runtime for {harness}. "
            "Use a home_template or the default config copy instead.")

    if configuration_choice == "none":
        # Clean HOME: no config copied. Only refresh secrets if opted-in.
        detail: dict = {"mode": "none (clean HOME)", "copied": [], "refreshed": []}
        config_dir = getattr(configured, "config_dir", None)
        source, prefix = default_home_source(harness, config_dir)
        if source and source.is_dir() and configured.refresh_files:
            def translate(relative, prefix=prefix):
                path = Path(relative)
                return str(path.relative_to(prefix)) if path.is_relative_to(prefix) else None
            if harness == "kiro":
                detail["refreshed"] = _refresh_kiro(configured.refresh_files, home)
            else:
                detail["refreshed"] = _refresh(source, home, configured.refresh_files, translate)
        return detail

    if configuration_choice == "overrides":
        # Skip defaults; apply only explicit overrides.
        detail = {"mode": "overrides only", "copied": []}
    else:
        # Default: seed from harness config dir on first run.
        if seeded:
            detail = _seed_default_home(harness, configured, home,
                                        size_cap_bytes=size_cap_bytes)
        else:
            config_dir = getattr(configured, "config_dir", None)
            source, prefix = default_home_source(harness, config_dir)
            plan = default_home_plan(harness, configured)
            detail = {"mode": "harness configuration directory", **plan}

    # Apply S3 prefix overlay between defaults and explicit overrides.
    # Precedence: defaults < S3 prefix < explicit files (skills_dirs/steering_files/mcp_config).
    config_s3_uri = getattr(configured, "config_s3_uri", None)
    if config_s3_uri and configuration_choice != "none":
        # Resolve the AWS profile from the outer config when available.
        # configured is a HarnessConfig so we reach the parent via a closure; fall back
        # to the AWS_PROFILE env var when the parent config is not accessible.
        aws_profile = os.environ.get("AWS_PROFILE")
        s3_result = _apply_s3_config_prefix(harness, config_s3_uri, home,
                                             aws_profile, config_exclude)
        detail["s3_prefix"] = s3_result

    # Apply explicit overrides (skills_dirs, steering_files, mcp_config) on top.
    skills_dirs = getattr(configured, "skills_dirs", []) or []
    steering_files = getattr(configured, "steering_files", []) or []
    mcp_config = getattr(configured, "mcp_config", None)

    override_detail: dict = {}
    if skills_dirs:
        override_detail["skills_dirs_applied"] = _apply_skills_dirs(harness, skills_dirs, home)
    if steering_files:
        override_detail["steering_files_applied"] = _apply_steering_files(harness, steering_files, home)
    if mcp_config:
        mcp_result = _apply_mcp_config(harness, mcp_config, home, config_exclude)
        override_detail["mcp_config_result"] = mcp_result
    if override_detail:
        detail["overrides"] = override_detail

    # Refresh after seeding on continuation runs. Always set the key so callers can
    # rely on detail["refreshed"] existing (empty list = nothing refreshed this turn).
    config_dir = getattr(configured, "config_dir", None)
    source, prefix = default_home_source(harness, config_dir)
    if source and source.is_dir():
        if harness == "kiro":
            detail["refreshed"] = _refresh_kiro(configured.refresh_files, home)
        else:
            def translate(relative, prefix=prefix):
                path = Path(relative)
                return str(path.relative_to(prefix)) if path.is_relative_to(prefix) else None
            detail["refreshed"] = _refresh(source, home, configured.refresh_files, translate)
    else:
        detail["refreshed"] = []

    if harness == "claude":
        removed = strip_claude_model_policy(home / ".claude" / "settings.json")
        if removed:
            detail["model_policy_removed"] = removed
    return detail


def _refresh_kiro(refresh_files: list, home: Path) -> list[str]:
    """Refresh kiro login from the per-platform data dir and config from ~/.kiro."""
    refreshed = []
    data_dir = _kiro_data_dir()
    config_source = Path("~/.kiro").expanduser()

    for relative in refresh_files:
        path = Path(relative)
        # Login file lives in the kiro data dir.
        if path.name == "data.sqlite3":
            src = data_dir / "data.sqlite3"
            dst = (home / relative).resolve()
            if not dst.is_relative_to(home.resolve()):
                raise ValueError("Credential refresh escaped the dedicated HOME")
            if _copy_file(src, home / relative):
                refreshed.append(relative)
        # Other files from kiro config dir (~/.kiro).
        elif config_source.is_dir():
            kiro_prefix = ".kiro"
            if path.is_relative_to(kiro_prefix):
                inside = str(path.relative_to(kiro_prefix))
                src = config_source / inside
                dst = (home / relative).resolve()
                if not dst.is_relative_to(home.resolve()):
                    raise ValueError("Credential refresh escaped the dedicated HOME")
                if _copy_file(src, home / relative):
                    refreshed.append(relative)
    return refreshed


KIRO_LOGIN_FILE = ".local/share/kiro-cli/data.sqlite3"


def _require_kiro_login(configured, home: Path) -> None:
    """Raise RuntimeError if a configured kiro login mechanism will produce no login.

    Only raises when a login mechanism has been explicitly configured (via
    KIRO_AUTH=copy-host-login or via a home_template that includes the login file)
    but the login source is unavailable.  When no login mechanism is configured
    the operator has made a deliberate choice; we do not second-guess it here.

    Called before a kiro turn starts (native and agentcore) so that
    mis-configuration surfaces with a clear, actionable message rather than
    as a cryptic "not logged in" error from kiro itself.
    """
    login_dest = home / KIRO_LOGIN_FILE
    if login_dest.is_file():
        return  # login already in place — nothing to check

    opted_in = KIRO_LOGIN_FILE in (configured.refresh_files or [])
    if opted_in:
        # KIRO_AUTH=copy-host-login: login must exist on the host data dir.
        host_login = _kiro_data_dir() / "data.sqlite3"
        if host_login.is_file():
            return  # login exists; copy-host-login will place it on next refresh
        raise RuntimeError(
            f"KIRO_AUTH=copy-host-login is set but no Kiro login exists at "
            f"{host_login}. Run `kiro-cli login` on this workstation then re-run setup.")

    if configured.home_template:
        # login-in-home mode with a template: check if the template contains the login.
        template = Path(configured.home_template).expanduser().resolve()
        if (template / KIRO_LOGIN_FILE).is_file():
            return  # login present in template; should have been seeded
        raise RuntimeError(
            f"KIRO_HOME_TEMPLATE is set but {KIRO_LOGIN_FILE} is absent from the "
            f"template at {template}. Add the login file or switch to "
            "KIRO_AUTH=copy-host-login to copy it from this workstation.")


def authentication_plan(config, harness: str) -> dict:
    """How this harness will reach its model, and what is missing if it cannot.

    Reported before setup finishes rather than discovered mid-comparison. Claude and
    Codex reach Bedrock through the AgentCore execution role, so neither needs a host
    credential; Kiro has no execution-role path and authenticates only from its own
    login, which is the single decision an operator has to make.
    """
    configured = config.harnesses[harness]
    plan = default_home_plan(harness, configured)
    template = Path(configured.home_template).expanduser() if configured.home_template else None
    detail = {"harness": harness, "runtime": config.runtime,
              "configuration_source": str(template) if template else plan["source"],
              "configuration_source_present": template.is_dir() if template else plan["source_present"],
              # A configured template is copied whole, so nothing is held back from it.
              "secrets_withheld": [] if template else plan["secrets_withheld"],
              "secrets_opted_in": plan["secrets_opted_in"]}
    if config.runtime == "agentcore" and harness in ("claude", "codex"):
        detail.update(
            method="AgentCore execution role",
            explanation=(
                f"{harness} reaches Amazon Bedrock from inside the microVM using the "
                "AgentCore runtime execution role. No host credential is copied and no "
                "API key is forwarded, so this workstation needs no "
                f"{harness} login for remote runs."),
            blocked=None)
        if harness == "codex":
            detail["requires"] = ("the provider configuration in "
                                  f"{plan['source'] or configured.home_template}/config.toml")
        return detail
    if harness == "kiro":
        opted_in = KIRO_LOGIN_FILE in plan["secrets_opted_in"]
        host_login = _kiro_data_dir() / "data.sqlite3"
        detail.update(
            method="Kiro login" + (" copied from this workstation" if opted_in else " inside its private HOME"),
            explanation=(
                "Kiro has no execution-role path; its login is the only credential it can "
                "use. multi-coding-harness-compare never copies that login unless it is named in "
                "refresh_files."),
            host_login=str(host_login), host_login_present=host_login.is_file())
        if opted_in:
            # KIRO_AUTH=copy-host-login: the login is always fetched from the host data
            # directory, whether or not a home_template is configured.  The template
            # provides non-secret configuration only.
            if host_login.is_file():
                detail["blocked"] = None
                if configured.home_template:
                    detail["note"] = ("home_template supplies configuration; "
                                      "login is copied from this workstation's Kiro data dir.")
            else:
                detail["blocked"] = (
                    f"refresh_files names {KIRO_LOGIN_FILE} but no login exists at "
                    f"{host_login}. Run `kiro-cli login` on this workstation first.")
        elif configured.home_template:
            # KIRO_AUTH=login-in-home: login expected to be in the template directory.
            detail["blocked"] = None
        else:
            detail["blocked"] = (
                "Kiro has no login available. Either copy this workstation's login with "
                "`multi-coding-harness-compare setup --kiro-auth=copy-host-login`, or keep it off this host "
                "with `multi-coding-harness-compare setup --kiro-auth=login-in-home` and then run "
                "`kiro-cli login` in the dedicated HOME it prints.")
        return detail
    detail.update(
        method="existing macOS CLI setup" if configured.inherit_host_setup else "private HOME",
        explanation=(
            f"{harness} runs natively and uses the provider configuration in "
            f"{detail['configuration_source']}."),
        blocked=None if detail["configuration_source_present"] else
            f"No configuration found at {detail['configuration_source']}.")
    return detail


def inherit_setup(harness, configured, home, *, configured_region: str | None = None):
    """Reuse an installed CLI's provider setup in a private run HOME.

    configured_region — region derived from the outer Config (e.g. judge_region
    or artifact_s3_region, both sourced from deploy.properties AWS_REGION).  Used
    as a fallback for the Codex amazon-bedrock provider when neither the Codex
    config.toml nor the environment variables supply a region, and before the last-
    resort boto3 profile-region lookup.  Keeps the ValueError for the truly-unset
    case so the operator's error message stays accurate.
    """
    if harness not in ("claude", "codex"):
        raise ValueError("Existing macOS CLI setup is supported for Claude Code and Codex.")
    executable = configured.cli_path or shutil.which(harness)
    if not executable:
        raise ValueError(f"{harness} was not found on the service's PATH. "
                         f"Set harnesses.{harness}.cli_path to the installed launcher.")
    # Preserve launcher symlinks and argv[0] (e.g. Toolbox's multi-call launcher).
    executable = Path(executable).expanduser().absolute()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"Configured {harness} CLI is not executable: {executable}")
    host = Path.home()
    # Honour HarnessConfig.config_dir (set via CLAUDE_CONFIG_DIR / CODEX_CONFIG_DIR deploy.properties keys)
    # before falling back to the environment variable or the built-in default path.
    _config_dir_field = getattr(configured, "config_dir", None)
    if _config_dir_field:
        source_root = Path(_config_dir_field).expanduser().resolve()
    else:
        source_root = Path(os.environ.get(
            "CLAUDE_CONFIG_DIR" if harness == "claude" else "CODEX_HOME",
            str(host / ("." + harness)))).expanduser().resolve()
    target_root = home / ("." + harness)
    target_root.mkdir(parents=True, exist_ok=True)
    sources = []
    files = (("settings.json", "CLAUDE.md") if harness == "claude"
             else ("config.toml", "auth.json", "AGENTS.md", "AGENTS.override.md"))
    for filename in files:
        source = source_root / filename
        if _copy_file(source, target_root / filename):
            sources.append(str(source))
        else:
            # Do not retain removed host configuration or a revoked file login.
            (target_root / filename).unlink(missing_ok=True)
    policy_removed = []
    if harness == "claude":
        policy_removed = strip_claude_model_policy(target_root / "settings.json")
    if harness == "claude":
        global_file = host / ".claude.json"
        if global_file.is_file():
            settings = json.loads(global_file.read_text())
            # Project trust/session history belongs to the private run, while
            # user-level MCP configuration is part of the installed CLI setup.
            shared = {key: settings[key] for key in ("mcpServers",) if key in settings}
            for destination in (home / ".claude.json", target_root / ".claude.json"):
                private = json.loads(destination.read_text()) if destination.is_file() else {}
                private.pop("mcpServers", None)
                private.update(shared)
                with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, delete=False) as output:
                    json.dump(private, output)
                    os.replace(output.name, destination)
            sources.append(str(global_file) + " (user MCP settings only)")
    reads = {executable, executable.resolve()}
    # Installed skills and plugins are shared read-only; sessions, history,
    # memory, logs and databases continue to live beneath this run's directory.
    for name in ("plugins", "skills", "marketplaces"):
        source, destination = source_root / name, target_root / name
        if source.is_dir():
            reads.add(source.resolve())
            if not destination.exists() and not destination.is_symlink():
                destination.symlink_to(source, target_is_directory=True)
    # Launchers update organization rules on startup. Give them private copies
    # instead of a read-only link into the user's instruction directories.
    for name in ("agents", "rules"):
        source, destination = source_root / name, target_root / name
        if destination.is_symlink():
            destination.unlink()
        if source.is_dir():
            copy_tree(source, destination)
            make_writable(destination)
    # Existing provider helpers use the user's normal HOME to find Toolbox,
    # Midway and AWS setup. Only these roots are readable under Seatbelt.
    for relative in (".toolbox", ".midway", ".aws"):
        path = host / relative
        if path.exists():
            reads.add(path.resolve())
    env = {
        "HOME": str(host),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
        "MCHC_" + harness.upper() + "_CLI": str(executable),
        "AWS_CONFIG_FILE": os.environ.get("AWS_CONFIG_FILE", str(host / ".aws/config")),
        "AWS_SHARED_CREDENTIALS_FILE": os.environ.get(
            "AWS_SHARED_CREDENTIALS_FILE", str(host / ".aws/credentials")),
    }
    routing = None
    if harness == "codex" and (target_root / "config.toml").is_file():
        settings = tomllib.loads((target_root / "config.toml").read_text())
        if settings.get("model_provider") == "amazon-bedrock":
            aws = settings.get("model_providers", {}).get("amazon-bedrock", {}).get("aws", {})
            profile = aws.get("profile") or os.environ.get("AWS_PROFILE")
            region = (aws.get("region")
                      or os.environ.get("AWS_REGION")
                      or os.environ.get("AWS_DEFAULT_REGION")
                      or configured_region)
            if not region:
                import boto3
                region = boto3.Session(profile_name=profile).region_name
            if not region:
                raise ValueError(
                    "AWS region could not be determined for the Codex amazon-bedrock provider. "
                    "Set AWS_REGION in deploy.properties (e.g. AWS_REGION = us-east-1)."
                )
            requested = configured.model or settings.get("model")
            selected_region = configured.model_regions.get(requested, region)
            env.update(MCHC_CODEX_REGION=selected_region,
                       MCHC_CODEX_DEFAULT_REGION=region,
                       MCHC_CODEX_MODEL_REGIONS=json.dumps(configured.model_regions))
            if profile:
                env["MCHC_CODEX_AWS_PROFILE"] = profile
            routing = {"provider": "amazon-bedrock", "default_region": region,
                       "selected_region": selected_region, "model_regions": configured.model_regions}
    for key in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        path = Path(env[key]).expanduser().resolve()
        env[key] = str(path)
        if path.exists():
            reads.add(path)
    provider_keys = ("AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_SDK_LOAD_CONFIG",
                     "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                     "AWS_CA_BUNDLE", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")
    for key in provider_keys:
        if key in os.environ:
            env[key] = os.environ[key]
    for key in ("AWS_CA_BUNDLE", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"):
        if env.get(key) and Path(env[key]).expanduser().is_file():
            reads.add(Path(env[key]).expanduser().resolve())
    # Preserve tool discovery; OS access remains governed by the run's policy.
    env["PATH"] = str(home / ".local/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")
    # Expose the configured Codex provider so the adapter reads the right one.
    if harness == "codex" and configured.provider:
        env["MCHC_CODEX_PROVIDER"] = configured.provider
    detail = {
        "mode": "existing macOS CLI setup", "executable": str(executable),
        "settings_sources": sources, "sessions": "private to this run",
        "provider_environment_keys": [key for key in provider_keys if key in os.environ],
        "credential_access": "existing AWS/Midway files and configured helpers; values omitted from configuration metadata",
        "shared_assets": "installed plugins and skills are read-only",
        "model_routing": routing,
    }
    if policy_removed:
        detail["model_policy_removed"] = policy_removed
    return env, reads, detail


# ── Workstation config snapshot ───────────────────────────────────────────────

def collect_config_snapshot(harnesses: list[str] | None = None,
                            config=None) -> dict[str, Path]:
    """Return {harness: source_path} for each harness's non-secret config directory.

    Used by ``python -m harness_image deploy`` when HARNESS_IMAGE_WORKSTATION_CONFIG=true
    to bake the operator's workstation configuration into the AgentCore runtime bundle
    using the installed-version option.  Only harnesses that have an existing config directory are included.

    harnesses — list of harness names to collect (default: all three).
    config    — optional arena Config object; when provided, each harness's configured
                config_dir (CLAUDE_CONFIG_DIR etc.) is honoured instead of only falling
                back to environment variables and the built-in default paths.
    """
    requested = list(harnesses) if harnesses else ["claude", "codex", "kiro"]
    result: dict[str, Path] = {}
    for harness in requested:
        config_dir = None
        if config is not None:
            h_cfg = getattr(config, "harnesses", {}).get(harness)
            if h_cfg is not None:
                config_dir = getattr(h_cfg, "config_dir", None)
        source, _ = default_home_source(harness, config_dir)
        if source and source.is_dir():
            result[harness] = source
    return result
