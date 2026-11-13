# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""In-microVM supervisor — python -m arena.agentcore_agent <jobdir>."""
import base64
import hashlib
import io
import json
import os
import shutil
import signal
import sys
import tarfile
import threading
import time
import zlib
from pathlib import Path


def _emit(kind, **kwargs):
    line = json.dumps({"type": "__mchc", "data": {"kind": kind, **kwargs}}, default=str)
    sys.stdout.buffer.write(line.encode() + b"\n")
    sys.stdout.buffer.flush()


def _read_state(control_dir):
    try:
        return json.loads((control_dir / "state.json").read_text())
    except Exception:
        return {"seq": 0, "signal": None}


def _make_writable(root):
    try:
        root.chmod(0o755)
    except OSError:
        pass
    for item in root.rglob("*"):
        try:
            item.chmod(0o755 if item.is_dir() or os.access(item, os.X_OK) else 0o644)
        except OSError:
            pass


def _forward_stdout(stream, done):
    buf = b""
    while True:
        try:
            chunk = stream.read(4096)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            sys.stdout.buffer.write(line + b"\n")
            sys.stdout.buffer.flush()
    if buf:
        sys.stdout.buffer.write(buf + b"\n")
        sys.stdout.buffer.flush()
    done.set()


def _forward_stderr(stream, done):
    while True:
        try:
            chunk = stream.read(4096)
        except Exception:
            break
        if not chunk:
            break
        sys.stderr.buffer.write(chunk)
        sys.stderr.buffer.flush()
    done.set()


# Kiro's staged tarball is 567 MB and unpacks to about 1 GB, so a cache root needs
# real headroom. The sessionStorage mount is 1.07 GB in total: choosing it fills the
# filesystem partway through extraction, and the harness then starts with no CLI at all.
_STAGED_TOOLS_MIN_FREE = 2 * 1024 ** 3


def _staged_tools_roots(stage: dict) -> list[str]:
    """Cache roots to try in order, best first.

    The runtime user cannot write the image roots, so candidates come from the job.
    A root is only offered if it exists and has headroom for the payload; the durable
    sessionStorage mount is preferred when it is large enough, because it survives a
    cold microVM, but it is skipped rather than half-filled when it is not.
    """
    roots, seen = [], set()
    for root in (stage.get("cache_root"), stage.get("cache_fallback"),
                 "/tmp/mchc-tools"):
        if not root or root in seen:
            continue
        seen.add(root)
        try:
            Path(root).mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(root).free
        except OSError as exc:
            _emit("note", text=f"tools cache root {root} unusable: {exc}")
            continue
        if free < _STAGED_TOOLS_MIN_FREE:
            _emit("note", text=f"tools cache root {root} skipped: "
                               f"{free // 1024 ** 2} MB free, needs "
                               f"{_STAGED_TOOLS_MIN_FREE // 1024 ** 2} MB")
            continue
        roots.append(root)
    return roots


def _staged_tools_target(stage: dict, name: str, version: str) -> Path:
    """First usable cache location for a staged tools tree."""
    roots = _staged_tools_roots(stage)
    root = roots[0] if roots else "/tmp/mchc-tools"
    return Path(root) / "tools" / f"{name}-{version}"


def _ensure_staged_tools(stage: dict, home_bin: Path) -> str | None:
    """Download and cache a staged tools tarball from S3 into the durable tools cache."""
    version = stage.get("version", "unknown")
    name = stage.get("name", "tools")
    candidates = [Path(r) / "tools" / f"{name}-{version}"
                  for r in _staged_tools_roots(stage)] or [
                      Path(f"/tmp/mchc-tools/tools/{name}-{version}")]
    for target in candidates:
        if (target / ".ready").exists():
            return str(target)

    # File-based lock guards concurrent turns in the same session
    import fcntl
    lock_path = Path(f"/tmp/.staged-tools-{version}.lock")
    lock_path.touch(exist_ok=True)
    with open(str(lock_path), "r+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = None
            for target in candidates:
                ready = target / ".ready"
                if ready.exists():
                    return str(target)
                try:
                    target.mkdir(parents=True, exist_ok=True)
                    if data is None:
                        sys.path.insert(0, "/var/task")
                        import boto3
                        region = stage["region"]
                        data = boto3.client("s3", region_name=region).get_object(
                            Bucket=stage["bucket"], Key=stage["key"])["Body"].read()
                    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                        tf.extractall(str(target), filter="data")
                    # Ensure all extracted files are executable where needed
                    for p in target.rglob("*"):
                        if p.is_file():
                            try:
                                p.chmod(p.stat().st_mode | 0o111)
                            except OSError:
                                pass
                    ready.touch()
                    return str(target)
                except Exception as exc:
                    # A partial tree would be mistaken for a usable cache on the next
                    # turn, and it holds space the next candidate may need.
                    _emit("note", text=f"staged tools fetch into {target} failed: {exc}")
                    shutil.rmtree(str(target), ignore_errors=True)
            return None
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _home_member(info):
    """Keep only HOME state a resumed turn needs, and nothing the host cannot unpack."""
    parts = info.name.split("/")
    # Regenerable caches and scratch; codex's tmp/arg0 also holds absolute symlinks.
    if ".cache" in parts or "tmp" in parts:
        return None
    # tarfile's data filter rejects links to absolute paths, which would fail the whole
    # extraction on the host. Session state never depends on them.
    if (info.issym() or info.islnk()) and info.linkname.startswith("/"):
        return None
    return info


def _result_archive(workspace: Path, home: Path, read_only: bool) -> bytes | None:
    """Pack what the host installs after this turn.

    A clarify turn is read-only, so its workspace never goes back. Its HOME does: the harness
    creates its session during clarify, and a later turn that lands in a new microVM (worker
    restart, idle timeout, session rotation) can only resume a session the host holds.
    """
    if read_only and not home.is_dir():
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if not read_only:
            tf.add(str(workspace), arcname="workspace")
        # HOME carries the harness session state (SQLite databases, provider caches)
        # that a resumed turn needs. Caches are regenerable, so leave them behind.
        if home.is_dir():
            tf.add(str(home), arcname="home", filter=_home_member)
    return buf.getvalue()


def _fetch_and_install_secrets(job: dict, home: Path, region: str) -> None:
    """Download the per-turn secrets object, write files into HOME (0600), and delete it.

    Called before the harness child process starts so credentials are in place
    at startup.  Content is never logged; only the S3 coordinates appear in notes.
    If deletion fails the turn still proceeds; the host will log a warning and
    doctor can list leftover objects.
    """
    secrets = job.get("secrets")
    if not secrets:
        return
    bucket = secrets.get("bucket")
    key = secrets.get("key")
    if not bucket or not key:
        return
    try:
        sys.path.insert(0, "/var/task")
        import boto3
        s3 = boto3.client("s3", region_name=region)
        data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        payload = json.loads(zlib.decompress(data))
        for rel_path, encoded in payload.get("files", {}).items():
            # Validate: must be relative and must not escape HOME.
            rel = Path(rel_path)
            if rel.is_absolute() or any(p == ".." for p in rel.parts):
                _emit("note", text="secrets: skipped unsafe relative path")
                continue
            dst = home / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(base64.b64decode(encoded))
            dst.chmod(0o600)
        # Delete all versions immediately so the object is not readable by another
        # turn's microVM or another user's execution role.
        try:
            resp = s3.list_object_versions(Bucket=bucket, Prefix=key)
            objects: list[dict] = []
            for v in (resp.get("Versions") or []):
                if v.get("Key") == key:
                    objects.append({"Key": key, "VersionId": v["VersionId"]})
            for m in (resp.get("DeleteMarkers") or []):
                if m.get("Key") == key:
                    objects.append({"Key": key, "VersionId": m["VersionId"]})
            if objects:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
            else:
                s3.delete_object(Bucket=bucket, Key=key)
        except Exception as del_exc:
            _emit("note", text=f"secrets: version deletion warning: {type(del_exc).__name__}")
    except Exception as exc:
        _emit("note", text=f"secrets: fetch failed: {type(exc).__name__}")


def _resolve_tools(job: dict, home_bin: Path) -> tuple[str | None, dict]:
    """Return (tool_dir, extra_env) for the harness; symlink executables into home_bin."""
    harness = job.get("harness", "")
    extra: dict = {}

    def _symlink(src: Path, link_name: str):
        dst = home_bin / link_name
        if not dst.exists():
            try:
                dst.symlink_to(src)
            except OSError:
                pass

    if harness == "claude":
        # Wheel-bundled binary at a fixed path inside /var/task
        claude_bin = Path("/var/task/claude_agent_sdk/_bundled/claude")
        if claude_bin.exists():
            extra["MCHC_CLAUDE_CLI"] = str(claude_bin)
            _symlink(claude_bin, "claude")
            return str(claude_bin.parent), extra
        # Fall back to vendored layout
        vendored = Path("/var/task/tools/claude/bin/claude")
        if vendored.exists():
            extra["MCHC_CLAUDE_CLI"] = str(vendored)
            _symlink(vendored, "claude")
            return str(vendored.parent), extra

    elif harness == "codex":
        # npm pack @openai/codex vendor tree extracted to /var/task/tools/codex/bin/
        vendored = Path("/var/task/tools/codex/bin")
        codex_bin = vendored / "codex"
        if codex_bin.exists():
            extra["MCHC_CODEX_CLI"] = str(codex_bin)
            _symlink(codex_bin, "codex")
            # Also link codex-code-mode-host if present
            host_bin = vendored / "codex-code-mode-host"
            if host_bin.exists():
                _symlink(host_bin, "codex-code-mode-host")
            # Codex looks for its ripgrep and bubblewrap companions on PATH; they ship in
            # sibling directories of the vendor tree, so surface them the same way.
            for sibling, names in ((vendored.parent / "codex-path", ("rg",)),
                                   (vendored.parent / "codex-resources", ("bwrap",))):
                for tool in names:
                    if (sibling / tool).exists():
                        _symlink(sibling / tool, tool)
            return str(vendored), extra

    elif harness == "kiro":
        def _link_kiro(directory: Path):
            _symlink(directory / "kiro-cli", "kiro-cli")
            # Kiro's launcher resolves these companions from the same bundle.
            for helper in ("kiro-cli-chat", "kiro-cli-term"):
                if (directory / helper).exists():
                    _symlink(directory / helper, helper)
            return str(directory), extra

        # Vendored when it fits the package ceilings; the provisioner writes the
        # binaries flat, so accept either layout before falling back to S3.
        for vendored in (Path("/var/task/tools/kiro"), Path("/var/task/tools/kiro/bin")):
            if (vendored / "kiro-cli").exists():
                return _link_kiro(vendored)

        stage = job.get("tools_stage")
        if stage:
            tool_dir = _ensure_staged_tools(
                dict(stage, name="kiro", cache_root=job.get("cache_root"),
                     cache_fallback=job.get("cache_fallback")), home_bin)
            if tool_dir and (Path(tool_dir) / "kiro-cli").exists():
                return _link_kiro(Path(tool_dir))

    return None, extra


def main(jobdir_str):
    jobdir = Path(jobdir_str)
    job = json.loads((jobdir / "job.json").read_text())

    workspace = Path(job["workspace"])
    bridge = Path(job["bridge"])
    control = Path(job["control"])
    home = Path(job["home"])
    tmp = Path(job["tmp"])
    argv = job["argv"]
    env_extra = job.get("env", {})
    read_only = job.get("read_only", False)
    result_coords = job.get("result")
    poll_seconds = job.get("poll_seconds", 0.5)
    region = job["region"]

    for d in (bridge / "requests", bridge / "answers", control, tmp):
        d.mkdir(parents=True, exist_ok=True)
    if not home.exists():
        home.mkdir(parents=True, exist_ok=True)
    for sub in (".codex", ".config", ".cache", ".local/share", ".local/bin"):
        (home / sub).mkdir(parents=True, exist_ok=True)

    home_bin = home / ".local/bin"

    # Resolve harness tool directory; symlinks land in home_bin above
    tool_dir, cli_env = _resolve_tools(job, home_bin)

    path_parts = [str(home_bin)]
    if tool_dir:
        path_parts.append(tool_dir)
    path_parts += ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]

    env = {
        "HOME": str(home),
        "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp),
        "ARENA_BRIDGE": str(bridge),
        # jobdir:/var/task so both the job's arena package and the vendored deps are importable
        "PYTHONPATH": f"{jobdir}:/var/task",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "CODEX_HOME": str(home / ".codex"),
        # The microVM HOME starts empty, so git has no identity and the harness's first
        # commit dies with "Author identity unknown". Supply the same identity
        # isolation.initialize_checkout uses for the baseline commit.
        "GIT_AUTHOR_NAME": "Multi Coding-Harness Compare", "GIT_AUTHOR_EMAIL": "mchc@localhost",
        "GIT_COMMITTER_NAME": "Multi Coding-Harness Compare", "GIT_COMMITTER_EMAIL": "mchc@localhost",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
        "ARENA_DB_PATH": str(workspace / ".arena-local.sqlite3"),
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "PATH": ":".join(path_parts),
    }
    env.update(cli_env)
    env.update(env_extra)

    # Install secrets into HOME before the harness starts.
    # Secrets are fetched from a dedicated S3 object and never travel in the job tarball.
    _fetch_and_install_secrets(job, home, region)

    if read_only:
        for root_p, dirs, files in os.walk(str(workspace)):
            for f in files:
                try:
                    Path(root_p, f).chmod(0o444)
                except OSError:
                    pass
            try:
                Path(root_p).chmod(0o555)
            except OSError:
                pass

    import subprocess
    child = subprocess.Popen(
        argv, cwd=str(workspace), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = os.getpgid(child.pid)
    _emit("started", pid=child.pid, pgid=pgid, argv=argv)

    stdout_done = threading.Event()
    stderr_done = threading.Event()
    threading.Thread(target=_forward_stdout, args=(child.stdout, stdout_done), daemon=True).start()
    threading.Thread(target=_forward_stderr, args=(child.stderr, stderr_done), daemon=True).start()

    seen_requests: set = set()
    last_seq = 0
    # The remote command stream is closed with "didn't have a response byte in last
    # 15 mins" if nothing is written, and a harness reasoning through a turn is
    # routinely silent for longer than that. Heartbeats are control messages, so the
    # host consumes them without turning them into events.
    heartbeat_seconds = job.get("heartbeat_seconds", 120)
    started_at = last_heartbeat = time.monotonic()

    while True:
        # Poll bridge/requests for new *.json files the harness wrote
        try:
            for entry in sorted((bridge / "requests").iterdir()):
                if entry.suffix == ".json" and entry.name not in seen_requests:
                    seen_requests.add(entry.name)
                    try:
                        _emit("bridge_request", name=entry.name,
                              content=json.loads(entry.read_text()))
                    except Exception:
                        pass
        except Exception:
            pass

        state = _read_state(control)
        seq = state.get("seq", 0)
        if seq != last_seq:
            last_seq = seq
            sig = state.get("signal")
            try:
                if sig == "stop":
                    os.killpg(pgid, signal.SIGSTOP)
                elif sig == "cont":
                    os.killpg(pgid, signal.SIGCONT)
                elif sig == "term":
                    os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

        if child.poll() is not None:
            stdout_done.wait(timeout=3)
            stderr_done.wait(timeout=3)
            break
        now_ = time.monotonic()
        if heartbeat_seconds and now_ - last_heartbeat >= heartbeat_seconds:
            last_heartbeat = now_
            _emit("heartbeat", elapsed=round(now_ - started_at))
        time.sleep(poll_seconds)

    returncode = child.wait()
    if returncode < 0:
        returncode = 128 + abs(returncode)

    if read_only:
        _make_writable(workspace)

    data = None
    if result_coords:
        try:
            data = _result_archive(workspace, home, read_only)
        except Exception as exc:
            _emit("note", text=f"workspace upload failed: {exc}")
    if data is not None:
        try:
            sys.path.insert(0, "/var/task")
            import boto3
            sha256 = hashlib.sha256(data).hexdigest()
            boto3.client("s3", region_name=region).put_object(
                Bucket=result_coords["bucket"], Key=result_coords["key"],
                Body=data, ServerSideEncryption="AES256")
            _emit("result", bucket=result_coords["bucket"], key=result_coords["key"],
                  sha256=sha256, bytes=len(data))
        except Exception as exc:
            _emit("note", text=f"workspace upload failed: {exc}")

    _emit("exit", code=returncode)
    sys.exit(returncode)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python -m arena.agentcore_agent <jobdir>\n")
        sys.exit(1)
    main(sys.argv[1])
