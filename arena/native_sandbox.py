# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""macOS Seatbelt runtime. No VM, image build, or unrestricted fallback.

The default-deny process rules follow the official Codex Seatbelt implementation.
Only runtime libraries, this run's files and explicitly selected host setup are
readable. Descendants inherit the kernel policy; CLI state, temporary files and
service ports are private to a run.
"""
import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .adapters.common import HarnessTurnError
from .config import Config
from .isolation import command, copy_tree


def _q(path):
    return json.dumps(str(path))


def _size(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)b?", value.lower())
    if not match:
        raise ValueError("Memory must be a size such as 1g or 512m")
    return int(float(match[1]) * 1024 ** (" kmgt".index(match[2]) if match[2] else 0))


def private_tmp(root):
    # macOS Unix socket paths have a short length limit; checkout paths can be long.
    path = Path("/private/tmp") / ("arena-" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24])
    # Persist ownership before creating short socket-path scratch space.
    # Worker recovery can find it even after a hard kill.
    from .bridge import atomic_json
    atomic_json(root / "temporary-path.json", {"path": str(path), "root": str(root.resolve())})
    if path.is_symlink():
        raise ValueError("Private temporary directory must not be a symlink")
    path.mkdir(mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError("Private temporary directory has a different owner")
    path.chmod(0o700)
    return path


def tool_paths():
    paths = {}
    for name in ("node", "npm", "git", "codex", "kiro-cli"):
        found = shutil.which(name)
        if found:
            paths[name] = Path(found).resolve()
    if "kiro-cli" in paths:
        for helper in ("kiro-cli-chat", "kiro-cli-term"):
            path = paths["kiro-cli"].parent / helper
            if path.exists():
                paths[helper] = path
    return paths


def runtime_roots():
    roots = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
             Path(__file__).parent.resolve()}
    for name, path in tool_paths().items():
        if name in ("node", "npm"):
            # Include the selected Node installation's standard modules, not
            # another user's global package/configuration directories.
            root = path.parent.parent if name == "node" else path.parent
            if name == "node":
                roots.add(root)
        elif name == "kiro-cli":
            roots.add(path.parent)  # native helper binaries in the same bundle
        else:
            roots.add(path)
    return roots


def profile(read_roots, write_roots, ports=(), network=False, graal_signals=False):
    read_roots = {Path(p).resolve() for p in read_roots}
    write_roots = {Path(p).resolve() for p in write_roots}
    system = ["/System", "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/lib",
              "/usr/libexec", "/usr/share", "/Library/Apple", "/Library/Developer/CommandLineTools",
              "/private/var/db/timezone"]
    literals = ["/", "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom",
                "/private/etc/hosts", "/private/etc/resolv.conf", "/private/etc/passwd",
                "/private/etc/protocols", "/private/etc/services", "/private/etc/localtime",
                "/private/etc/apache2/mime.types"]
    rules = ["""(version 1)
(deny default)
(allow process-exec process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(deny file-link)
(allow sysctl-read
  (sysctl-name-prefix "hw.") (sysctl-name-prefix "machdep.cpu.")
  (sysctl-name-prefix "kern.os") (sysctl-name-prefix "kern.proc.pid.")
  (sysctl-name-prefix "kern.proc.pgrp.") (sysctl-name-prefix "net.routetable.")
  (sysctl-name "kern.argmax") (sysctl-name "kern.hostname")
  (sysctl-name "kern.maxfilesperproc") (sysctl-name "kern.maxproc")
  (sysctl-name "kern.secure_kernel") (sysctl-name "kern.sysv.semmns")
  (sysctl-name "kern.usrstack64") (sysctl-name "kern.version")
  (sysctl-name "sysctl.proc_cputype") (sysctl-name "vm.loadavg"))
(allow file-read-metadata)
(allow file-read* file-write-data (subpath "/dev/fd"))
(allow file-read* file-write-data (literal "/dev/null") (literal "/dev/zero"))
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.PowerManagement.control"))
(allow iokit-open (iokit-registry-entry-class "RootDomainUserClient"))
(allow pseudo-tty)
(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))
(allow file-read* file-write* file-ioctl
  (require-all (regex #"^/dev/ttys[0-9]+$") (extension "com.apple.sandbox.pty")))
"""]
    if graal_signals:
        # Toolbox's GraalVM launcher uses /cSunMiscSignal-<pid> on macOS.
        # It immediately unlinks this semaphore after opening it. Keep all
        # other named IPC denied, including in independent candidate checks.
        rules.append('(allow ipc-posix-sem (ipc-posix-name-regex #"^/cSunMiscSignal-[0-9]+$"))')
    for path in system:
        rules.append(f"(allow file-read* file-map-executable (subpath {_q(path)}))")
    for path in literals:
        rules.append(f"(allow file-read* (literal {_q(path)}))")
    # Python lists the package search directory while resolving `arena`.
    # Permit that directory entry only; config.toml and .arena remain denied.
    rules.append(f"(allow file-read* (literal {_q(Path(__file__).parent.parent.resolve())}))")
    for path in sorted(read_roots | write_roots):
        kind = "subpath" if path.is_dir() else "literal"
        rules.append(f"(allow file-read* file-map-executable ({kind} {_q(path)}))")
    for path in sorted(write_roots):
        rules.append(f"(allow file-write* (subpath {_q(path)}))")
        rules.append(f"(allow network-bind network-inbound network-outbound (subpath {_q(path)}))")
    if network:
        # Native providers, MCP HTTPS and package registries. Other service
        # ports are unavailable except this run's explicitly assigned ports.
        rules.append("""(allow network-outbound (remote tcp "*:443") (remote tcp "*:80")
  (remote udp "*:53") (remote tcp "*:53") (literal "/private/var/run/mDNSResponder"))
(allow system-socket (require-all (socket-domain AF_SYSTEM) (socket-protocol 2)))
(allow mach-lookup
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.networkd")
  (global-name "com.apple.mDNSResponder")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.trustd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd"))
(allow file-read* (subpath "/System/Library/Keychains") (subpath "/Library/Keychains"))
""")
    for port in ports:
        rules.append(f'(allow network-bind network-inbound (local tcp "localhost:{port}"))')
        rules.append(f'(allow network-outbound (remote tcp "localhost:{port}"))')
    return "\n".join(rules)


def private_environment(home, temporary, workspace, bridge=None, allowed=(), python=None):
    tools = tool_paths()
    # Kiro's macOS launcher resolves its chat companion from ~/.local/bin.
    bindir = home / ".local/bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for relative in (".codex", ".config", ".cache", ".local/share"):
        (home / relative).mkdir(parents=True, exist_ok=True)
    for name, target in tools.items():
        link = bindir / name
        if not link.exists():
            link.symlink_to(target)
    # A symlink in private HOME loses Python's pyvenv.cfg discovery. Execute the
    # original venv path so coding and independent checks use the pinned packages.
    for name in ("python", "python3"):
        link = bindir / name
        if link.is_symlink():
            link.unlink()
        link.write_text("#!/bin/sh\nexec " + shlex.quote(str(python or sys.executable)) + ' "$@"\n')
        link.chmod(0o755)
    tool_path = f"{bindir}:" + (f"{Path(python).parent}:" if python else "")
    env = {"HOME": str(home), "TMPDIR": str(temporary), "TMP": str(temporary),
           "TEMP": str(temporary), "PATH": tool_path + "/usr/bin:/bin:/usr/sbin:/sbin",
           "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
           "XDG_CACHE_HOME": str(home / ".cache"), "CODEX_HOME": str(home / ".codex"),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
           "ARENA_NATIVE_SANDBOX": "1", "ARENA_WORKSPACE": str(workspace),
           "CLAUDE_CODE_TMPDIR": str(temporary), "CLAUDE_TMPDIR": str(temporary),
           "KIRO_CHAT_LOG_FILE": str(temporary / "kiro-chat.log"),
           "SHELL": "/bin/zsh", "TERM": "xterm-256color",
           "PYTHONPATH": str(Path(__file__).parent.parent),
           "ARENA_DB_PATH": str(workspace / ".arena-local.sqlite3")}
    if bridge:
        env["ARENA_BRIDGE"] = str(bridge)
    for key in allowed:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _read_process_table():
    # Read ps and enforce its deadline off the worker event loop. A busy event
    # loop must not turn an already-exited ps into a timeout while its pipe
    # callbacks are still queued. Retry one transient timeout, within the old
    # 15-second ceiling; never substitute stale rows for a successful scan.
    for attempt in range(2):
        try:
            result = subprocess.run(
                ["/bin/ps", "-axo", "pid=,ppid=,pgid=,rss=,lstart="],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                errors="replace", timeout=5)
            break
        except subprocess.TimeoutExpired as exc:
            if attempt:
                # A stalled ps is a busy host, not a broken configuration.
                raise HarnessTurnError(
                    "macOS process inspection timed out twice; native supervision unavailable",
                    terminal=False) from exc
            logging.getLogger(__name__).warning("macOS process inspection timed out; retrying once")
    if result.returncode:
        raise RuntimeError("Cannot supervise native processes: " + result.stderr)
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5:
            rows[int(parts[0])] = {"parent": int(parts[1]), "group": int(parts[2]),
                                   "rss": int(parts[3]) * 1024, "start": parts[4]}
    return rows


async def _process_table():
    return await asyncio.to_thread(_read_process_table)


class Native:
    def __init__(self, config: Config):
        self.config = config
        self.jobs = {}
        self.process_scan = None
        self.process_snapshot = None
        self.process_snapshot_at = 0
        self.records = config.root / "native-processes"
        self.records.mkdir(parents=True, exist_ok=True)

    async def check(self):
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
            raise RuntimeError("Native isolation requires macOS sandbox-exec.")
        code, _, error = await command("/usr/bin/sandbox-exec", "-p", profile(set(), set()),
            "/bin/echo", "sandbox", timeout=10)
        if code:
            raise RuntimeError(f"Native sandbox unavailable (exit {code}); run multi-coding-harness-compare start from a normal terminal. " + error.strip())
        paths = {name: str(path) for name, path in tool_paths().items()}
        signature = {"os": platform.platform(), "python": sys.version.split()[0],
            "python_environment": sys.prefix, "private_environment_version": 4, "tools": {
            name: {"path": path, "size": Path(path).stat().st_size,
                   "mtime_ns": Path(path).stat().st_mtime_ns} for name, path in paths.items()}}
        return {"backend": "native", "isolation": "macOS Seatbelt default-deny",
                "architecture": platform.machine(), **signature,
                "runtime_id": hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest(),
                "limits": {"active_seconds": self.config.active_seconds,
                    "memory": self.config.memory, "pids": self.config.pids,
                    "enforcement": "RSS and descendant count sampled once per second; terminate on excess",
                    "cpu_quota": None, "cpu_note": "macOS native processes have no CPU quota"}}

    def _ports(self, root):
        registry = self.config.root / "native-ports.json"
        values = json.loads(registry.read_text()) if registry.exists() else {}
        key = str(root.resolve())
        if key not in values:
            # Allocation occurs in the single worker event loop without awaits.
            start = self.config.native_port_start + 8 * len(values)
            if start + 7 > 65535:
                raise RuntimeError("Native port allocation exhausted")
            values[key] = list(range(start, start + 8))
            registry.write_text(json.dumps(values))
        return values[key]

    async def start(self, run, root, phase):
        info = await self.check()
        cfg = self.config.harness_for_run(run)
        home = root / "home"
        from .host_setup import prepare_home, _require_kiro_login
        home_setup = prepare_home(run["harness"], cfg, home,
                                  configuration_choice=run.get("configuration_choice",
                                                               self.config.default_configuration_choice),
                                  size_cap_bytes=self.config.harness_config_size_cap_mb * 1024 * 1024)
        # Point 3: block before a kiro turn starts if no login will be available.
        if run["harness"] == "kiro":
            _require_kiro_login(cfg, home)
        # Kiro's native macOS path differs from Linux. Copy only this run's
        # private database, including its saved sessions; never the personal HOME.
        linux = home / ".local/share/kiro-cli"
        mac = home / "Library/Application Support/kiro-cli"
        migrated = False
        if run["harness"] == "kiro" and linux.exists() and not mac.exists():
            copy_tree(linux, mac)
            migrated = True
        workspace, bridge, temporary = root / "workspace", root / "bridge", private_tmp(root)
        for p in (workspace, temporary, bridge / "requests", bridge / "answers"):
            p.mkdir(parents=True, exist_ok=True)
        ports = self._ports(root)
        reads = runtime_roots() | {workspace, bridge}
        reads |= {Path(p).expanduser().resolve() for p in self.config.native_read_roots}
        writes = {home, temporary, bridge / "requests"}
        if phase != "clarify":
            writes.add(workspace)
        env = private_environment(home, temporary, workspace, bridge, cfg.env)
        # Expose the configured Codex provider to the adapter so it selects the right
        # provider endpoint; MCHC_CODEX_PROVIDER env var is the fallback in the adapter.
        if run["harness"] == "codex" and getattr(cfg, "provider", None):
            env["MCHC_CODEX_PROVIDER"] = cfg.provider
        host_setup = None
        if cfg.inherit_host_setup:
            from .host_setup import inherit_setup
            # Pass the deploy.properties-derived region so inherit_setup can resolve
            # the Codex amazon-bedrock provider region without relying on the ambient
            # AWS_REGION env var or boto3's profile region (which may both be absent).
            _configured_region = (self.config.judge_region
                                  or self.config.artifact_s3_region)
            host_env, host_reads, host_setup = inherit_setup(
                run["harness"], cfg, home, configured_region=_configured_region)
            env.update(host_env)
            reads |= host_reads
        policy = profile(reads, writes, ports, network=True, graal_signals=run["harness"] == "claude")
        policy_file = root / "sandbox.sb"
        policy_file.write_text(policy)
        env.update(PORT=str(ports[0]), ARENA_PORTS=",".join(map(str, ports)))
        name = "arena-" + run["id"]
        self.jobs[name] = {"policy": policy_file, "env": env, "cwd": workspace, "known": {}}
        return {**info, "name": name, "workspace": str(workspace), "ports": ports,
            "network": "HTTP(S) outbound; TCP loopback only on the assigned private ports; private Unix sockets",
            "policy_sha256": hashlib.sha256(policy.encode()).hexdigest(),
            "kiro_private_home_migrated": migrated,
            "host_setup": host_setup,
            "home_setup": home_setup,
            "configuration": {**cfg.model_dump(exclude={"home_template"}),
                "home_template_configured": bool(cfg.home_template),
                "environment_keys_present": [k for k in cfg.env if k in os.environ]}}

    async def launch(self, name, args=None):
        job = self.jobs[name]
        process = await asyncio.create_subprocess_exec("/usr/bin/sandbox-exec", "-f", str(job["policy"]),
            *(args or [sys.executable, "-m", "arena.native_runner"]),
            cwd=job["cwd"], env=job["env"], stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=8 * 1024 * 1024)
        job["process"] = process
        job["pid"] = process.pid
        await self.supervise(name, force=True)
        return process

    async def _process_rows(self, force=False):
        if force or self.process_snapshot is None or time.monotonic() - self.process_snapshot_at >= 1:
            # Monitoring and cleanup share in-flight inspection, including
            # during requirement revisions that stop several harnesses at once.
            if self.process_scan is None or self.process_scan.done():
                self.process_scan = asyncio.create_task(_process_table())
            rows = await asyncio.shield(self.process_scan)
            self.process_snapshot, self.process_snapshot_at = rows, time.monotonic()
        return self.process_snapshot

    async def supervise(self, name, force=False):
        job = self.jobs.get(name)
        if not job or "pid" not in job:
            return
        if not force and time.monotonic() - job.get("sampled", 0) < 1:
            return
        rows = await self._process_rows(force=force)
        known = {pid: start for pid, start in job["known"].items()
                 if pid in rows and rows[pid]["start"] == start}
        if job["pid"] in rows and not job.get("root_start"):
            job["root_start"] = rows[job["pid"]]["start"]
        if job["pid"] in rows and rows[job["pid"]]["start"] == job.get("root_start"):
            known[job["pid"]] = job["root_start"]
        changed = True
        while changed:
            changed = False
            for pid, row in rows.items():
                if pid not in known and (row["parent"] in known or
                   (job["pid"] in known and row["group"] == job["pid"])):
                    known[pid] = row["start"]
                    changed = True
        job.update(known=known, sampled=time.monotonic())
        (self.records / (name + ".json")).write_text(json.dumps({
            "pid": job["pid"], "root_start": job.get("root_start"),
            "known": known, "owner": self.config.container_owner}))
        rss = sum(rows[pid]["rss"] for pid in known)
        if rss > _size(self.config.memory) or len(known) > self.config.pids:
            raise RuntimeError("Native process resource limit exceeded "
                               f"({rss // 1048576} MiB, {len(known)} processes)")

    async def _signal(self, name, sig):
        job = self.jobs.get(name)
        if not job:
            return
        rows = await self._process_rows(force=True)
        pid = job.get("pid")
        if pid in rows and rows[pid]["start"] == job.get("root_start"):
            try:
                # Signal the group before terminating its individual members.
                # macOS can report EPERM for a group that has just disappeared.
                os.killpg(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                fresh = await self._process_rows(force=True)
                if any(row["group"] == pid for row in fresh.values()):
                    raise
                rows = fresh
        for child, start in reversed(list(job.get("known", {}).items())):
            if child in rows and rows[child]["start"] == start:
                try:
                    os.kill(child, sig)
                except ProcessLookupError:
                    pass

    async def pause(self, name):
        await self.supervise(name, force=True)
        await self._signal(name, signal.SIGSTOP)
        return 0, "", ""

    async def unpause(self, name):
        await self._signal(name, signal.SIGCONT)
        return 0, "", ""

    async def stop(self, name):
        job = self.jobs.get(name)
        if not job:
            return
        try:
            await self.supervise(name, force=True)
        except RuntimeError:
            pass
        await self._signal(name, signal.SIGCONT)
        await self._signal(name, signal.SIGTERM)
        process = job.get("process")
        if process:
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                pass
        await self._signal(name, signal.SIGKILL)
        if process and process.returncode is None:
            await process.wait()
        (self.records / (name + ".json")).unlink(missing_ok=True)
        self.jobs.pop(name, None)

    async def cleanup_owned(self):
        for file in self.records.glob("*.json"):
            data = json.loads(file.read_text())
            if data.get("owner") == self.config.container_owner:
                self.jobs[file.stem] = {**data, "known": {
                    int(pid): start for pid, start in data.get("known", {}).items()}}
                await self.stop(file.stem)

    async def execute_check(self, name, execution, acceptance, regression, command_text, cancellation=None):
        root = execution.parent
        home, temporary = root / "home", private_tmp(root)
        home.mkdir(exist_ok=True)
        temporary.mkdir(exist_ok=True)
        reports = root / "reports"
        reports.mkdir(exist_ok=True)
        policy = root / "check-sandbox.sb"
        ports = self._ports(root)
        wheels, context = root / "wheels", root / "verification-context.json"
        # Stop pytest's upward configuration search at this evaluation. Project
        # config inside the checkout still takes precedence; the application's
        # own pyproject.toml must never become the candidate's test config.
        pytest_config = root / "pytest.ini"
        if not pytest_config.exists():
            pytest_config.write_text("[pytest]\ncache_dir = " + str(home / ".cache/pytest") + "\n")
        policy.write_text(profile(runtime_roots() | {acceptance, regression, wheels, context, pytest_config,
                                                    root / "runtime-probe"},
                                  {execution, home, temporary, reports}, ports)
                          # Pytest lists its root directory during collection.
                          # Grant that directory entry, not its other files.
                          + f"\n(allow file-read* (literal {_q(root.resolve())}))\n")
        python = home / ".verification-python/bin/python"
        env = private_environment(home, temporary, execution,
                                  python=python if python.exists() else None)
        env["PYTHONPATH"] = str(execution)
        env.update(PIP_NO_INDEX="1", PIP_FIND_LINKS=str(wheels), PIP_CONFIG_FILE=os.devnull,
                   PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1",
                   MCHC_VERIFICATION_CONTEXT=str(context))
        env.update(PORT=str(ports[0]), ARENA_PORTS=",".join(map(str, ports)))
        self.jobs[name] = {"policy": policy, "env": env, "cwd": execution, "known": {}}
        process = await self.launch(name, ["/bin/sh", "-c", command_text])
        communicate = asyncio.create_task(process.communicate())
        started = time.monotonic()
        try:
            while not communicate.done():
                if cancellation and cancellation():
                    raise RuntimeError("Comparison cancelled")
                if time.monotonic() - started >= self.config.check_seconds:
                    raise TimeoutError()
                await self.supervise(name)
                await asyncio.wait({communicate}, timeout=.2)
            out, err = await communicate
            return process.returncode, out.decode(errors="replace"), err.decode(errors="replace")
        finally:
            await self.stop(name)
            await asyncio.gather(communicate, return_exceptions=True)
