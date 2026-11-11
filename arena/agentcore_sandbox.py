# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Host-side AgentCore Runtime backend — runs harnesses in Firecracker microVMs."""
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import tarfile
import threading
import time
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .adapters.common import HarnessTurnError
from .isolation import remove_tree
from .bridge import atomic_json
from .config import Config
from .isolation import make_writable
from .agentcore_provision import runtime_name as _runtime_name

log = logging.getLogger(__name__)


# ── Credential isolation helpers ─────────────────────────────────────────────

def _s3_key(s3_prefix: str, user_namespace: str | None, path: str) -> str:
    """Build an S3 key with an optional USER_NAMESPACE segment.

    When user_namespace is None the output matches the existing layout exactly,
    preserving backward compatibility with deploy.properties files that pre-date
    per-user isolation support.
    """
    if user_namespace:
        return f"{s3_prefix}/{user_namespace}/{path}"
    return f"{s3_prefix}/{path}"


def _secrets_filter(secret_names: frozenset):
    """Return a tarfile member filter that silently drops known-secret file names."""
    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if Path(ti.name).name in secret_names:
            return None
        return ti
    return _filter


def _collect_secrets(home: Path, harness: str) -> dict[str, bytes]:
    """Walk HOME and return {home-relative path: raw bytes} for every secret file.

    Never logs file content. Returns an empty dict when the harness has no known
    secrets or when HOME does not exist yet.
    """
    from .host_setup import HOME_SECRETS
    secret_names = frozenset(HOME_SECRETS.get(harness, ()))
    found: dict[str, bytes] = {}
    if not secret_names or not home.is_dir():
        return found
    for item in home.rglob("*"):
        if item.is_file() and item.name in secret_names:
            try:
                found[str(item.relative_to(home))] = item.read_bytes()
            except Exception:
                pass
    return found


def _pack_secrets(secrets: dict[str, bytes]) -> bytes:
    """Serialise {path: bytes} as a zlib-compressed JSON object.

    Values are base64-encoded so the wire format is printable ASCII JSON.
    Content is never written to logs.
    """
    payload = {
        "v": 1,
        "files": {p: base64.b64encode(b).decode() for p, b in secrets.items()},
    }
    return zlib.compress(json.dumps(payload).encode())


def _put_secrets_object(s3_client, bucket: str, key: str, data: bytes,
                        kms_key_arn: str | None) -> None:
    """Upload the secrets object with SSE-KMS when a key ARN is configured, SSE-S3 otherwise.

    Runs synchronously; call via asyncio.to_thread from async contexts.
    """
    kwargs: dict = {"Bucket": bucket, "Key": key, "Body": data}
    if kms_key_arn:
        kwargs["ServerSideEncryption"] = "aws:kms"
        kwargs["SSEKMSKeyId"] = kms_key_arn
    else:
        kwargs["ServerSideEncryption"] = "AES256"
    s3_client.put_object(**kwargs)


def _delete_all_versions_sync(s3_client, bucket: str, key: str) -> bool:
    """Delete all versions and delete markers of a specific S3 key.

    Falls back to delete_object when the bucket is unversioned (empty Versions
    list).  Returns True on success.  Never raises; callers decide how to handle
    a False return (log warning, continue).
    """
    try:
        resp = s3_client.list_object_versions(Bucket=bucket, Prefix=key)
        objects: list[dict] = []
        for v in (resp.get("Versions") or []):
            if v.get("Key") == key:
                objects.append({"Key": key, "VersionId": v["VersionId"]})
        for m in (resp.get("DeleteMarkers") or []):
            if m.get("Key") == key:
                objects.append({"Key": key, "VersionId": m["VersionId"]})
        if objects:
            s3_client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        else:
            # Unversioned bucket: plain delete is sufficient.
            s3_client.delete_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


# ── BOOTSTRAP: runs inside the microVM before the arena package is available.
# CC_PAYLOAD = base64(zlib(json({bucket,key,jobdir,home,pythonpath,region,...})))
# Inserts /var/task onto sys.path first so boto3 is importable (it's vendored there),
# downloads the job tarball from S3, extracts to jobdir, then exec's the supervisor.
_BOOTSTRAP_SRC = (
    "import base64,io,json,os,shutil,sys,tarfile,zlib\n"
    "sys.path.insert(0,'/var/task')\n"
    "import boto3\n"
    "p=json.loads(zlib.decompress(base64.b64decode(os.environ['CC_PAYLOAD'])))\n"
    "region=p['region']\n"
    "os.environ['AWS_REGION']=region\n"
    "os.makedirs(p['jobdir'],exist_ok=True)\n"
    "os.makedirs(p['home'],exist_ok=True)\n"
    "buf=io.BytesIO(boto3.client('s3',region_name=region).get_object(Bucket=p['bucket'],Key=p['key'])['Body'].read())\n"
    # A resumed turn re-extracts over the previous attempt's tree in the same session.
    # Harness-written read-only files (git pack .idx is 0444) cannot be reopened for
    # writing, so drop the stale file first; the tarball copy is authoritative.
    "def _replace(m,d):\n"
    "    m=tarfile.data_filter(m,d)\n"
    "    t=os.path.join(d,m.name)\n"
    "    if not m.isdir() and (os.path.islink(t) or os.path.isfile(t)):\n"
    "        os.unlink(t)\n"
    "    return m\n"
    "with tarfile.open(fileobj=buf,mode='r:gz') as tf:\n"
    "    tf.extractall(p['jobdir'],filter=_replace)\n"
    "pp=p['jobdir']+':/var/task'\n"
    "e=dict(os.environ)\n"
    "e.update({'PYTHONPATH':pp,'PYTHONUNBUFFERED':'1','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1','AWS_REGION':region,'AWS_DEFAULT_REGION':region,'LANG':'C.UTF-8'})\n"
    "py=shutil.which('python3') or '/usr/bin/python3'\n"
    "os.execve(py,[py,'-m','arena.agentcore_agent',p['jobdir']],e)\n"
)

# ── DELIVER: atomically writes host-supplied files inside jobdir.
# CC_PAYLOAD = base64(zlib(json({"dir": jobdir, "files": {relpath: text}})))
# Every relpath is validated to stay inside jobdir; absolute paths and ".." are rejected.
_DELIVER_SRC = (
    "import base64,json,os,zlib\n"
    "p=json.loads(zlib.decompress(base64.b64decode(os.environ['CC_PAYLOAD'])))\n"
    "jd=os.path.realpath(os.path.abspath(p['dir']))\n"
    "for rel,text in p['files'].items():\n"
    "    if os.path.isabs(rel) or '..' in rel.replace('\\\\','/').split('/'):\n"
    "        raise ValueError('unsafe path: '+repr(rel))\n"
    "    dst=os.path.normpath(os.path.join(jd,rel))\n"
    "    if dst!=jd and not dst.startswith(jd+os.sep):\n"
    "        raise ValueError('path escapes jobdir: '+repr(rel))\n"
    # Resolve symlinks; a harness-controlled symlink at any path component must not escape jobdir.
    "    rdst=os.path.realpath(dst)\n"
    "    if rdst!=jd and not rdst.startswith(jd+os.sep):\n"
    "        raise ValueError('symlink escape: '+repr(rel))\n"
    "    os.makedirs(os.path.dirname(dst),exist_ok=True)\n"
    "    tmp=dst+'.tmp'\n"
    "    open(tmp,'w').write(text)\n"
    "    os.replace(tmp,dst)\n"
)

# ── PROBE: reports whether the configured paths are usable in the deployed runtime.
# CC_PAYLOAD = base64(zlib(json({"paths": [...]}))). Read-only apart from a scratch
# directory it removes again. A runtime redeploy can change the command user or the
# mounts, so run this after every `agentcore-deploy` rather than inferring from a
# failed comparison.
_PROBE_SRC = (
    "import base64,json,os,pwd,shutil,zlib\n"
    "p=json.loads(zlib.decompress(base64.b64decode(os.environ['CC_PAYLOAD'])))\n"
    "def check(path):\n"
    "    e={'path':path}\n"
    "    probe=os.path.join(path,'.codecouncil-probe')\n"
    "    try:\n"
    "        os.makedirs(probe,exist_ok=True)\n"
    "        f=os.path.join(probe,'probe.txt')\n"
    "        open(f,'w').write('probe')\n"
    "        e['write']=(open(f).read()=='probe')\n"
    "    except Exception as exc:\n"
    "        e['write']=False\n"
    "        e['error']=type(exc).__name__+': '+str(exc)\n"
    "        return e\n"
    "    try:\n"
    "        import sqlite3\n"
    "        db=os.path.join(probe,'probe.sqlite3')\n"
    "        c=sqlite3.connect(db)\n"
    "        c.execute('create table if not exists t(x)')\n"
    "        c.execute('insert into t values (1)')\n"
    "        c.commit()\n"
    "        c.close()\n"
    "        e['sqlite']=True\n"
    "    except Exception as exc:\n"
    "        e['sqlite']=False\n"
    "        e['sqlite_error']=type(exc).__name__+': '+str(exc)\n"
    "    try:\n"
    "        du=shutil.disk_usage(path)\n"
    "        e['free_gb']=round(du.free/1e9,2)\n"
    "        e['total_gb']=round(du.total/1e9,2)\n"
    "    except Exception:\n"
    "        pass\n"
    "    shutil.rmtree(probe,ignore_errors=True)\n"
    "    return e\n"
    "out={'user':pwd.getpwuid(os.getuid()).pw_name,'uid':os.getuid(),\n"
    "     'home':os.environ.get('HOME'),'cwd':os.getcwd(),\n"
    "     'paths':[check(x) for x in p['paths']]}\n"
    "print('CODECOUNCIL_PROBE '+json.dumps(out))\n"
)

# Pre-compressed constants embedded in command strings (shell-safe base64, no secrets)
BOOTSTRAP = base64.b64encode(zlib.compress(_BOOTSTRAP_SRC.encode())).decode()
DELIVER = base64.b64encode(zlib.compress(_DELIVER_SRC.encode())).decode()
PROBE = base64.b64encode(zlib.compress(_PROBE_SRC.encode())).decode()

# Fixed python3 -c wrapper; single quotes inside double quotes are shell-safe
_EXEC_WRAPPER = (
    'python3 -c "import base64,os,zlib;'
    "exec(zlib.decompress(base64.b64decode(os.environ['CC_CODE'])).decode())\""
)


def _build_command(code_b64: str, payload_bytes: bytes) -> str:
    # The command executor splits argv instead of evaluating a shell, so a leading
    # VAR=value assignment is treated as the program name and the runtime answers 500.
    # `env` is a real executable and applies the assignments itself.
    payload_b64 = base64.b64encode(zlib.compress(payload_bytes)).decode()
    return f"env CC_CODE={code_b64} CC_PAYLOAD={payload_b64} {_EXEC_WRAPPER}"


def _runtime_id(reference: str) -> str:
    """get_agent_runtime takes the runtime id; accept a full ARN for convenience."""
    return reference.split("/")[-1] if reference.startswith("arn:") else reference


def _parse_s3_uri(uri: str):
    u = urlsplit(uri)
    return u.netloc, u.path.strip("/")


def _sha256_file(path: Path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _harness_env(harness: str, cfg, harness_cfg) -> dict:
    # The microVM gets credentials from the AgentCore execution role; no long-lived keys forwarded.
    env: dict = {}
    if harness == "claude":
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        env["AWS_REGION"] = cfg.region
        env["AWS_DEFAULT_REGION"] = cfg.region
        if harness_cfg.model:
            env["ANTHROPIC_MODEL"] = harness_cfg.model
        # Never forward ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN
    elif harness == "codex":
        model = harness_cfg.model or ""
        region = harness_cfg.model_regions.get(model, cfg.region)
        env["CODECOUNCIL_CODEX_REGION"] = region
        # Use HarnessConfig.provider when set (deploy.properties CODEX_PROVIDER);
        # fall back to the execution-role provider which is always amazon-bedrock-runtime.
        env["CODECOUNCIL_CODEX_PROVIDER"] = getattr(harness_cfg, "provider", None) or "amazon-bedrock-runtime"
        env["AWS_REGION"] = cfg.region
        env["AWS_DEFAULT_REGION"] = cfg.region
    elif harness == "kiro":
        # Kiro authenticates from its dedicated HOME template; only region needed here
        env["AWS_REGION"] = cfg.region
        env["AWS_DEFAULT_REGION"] = cfg.region
    return env


def _effective_command_timeout(cfg, active_seconds: float = 0) -> int:
    if hasattr(cfg, "effective_command_timeout"):
        return cfg.effective_command_timeout(active_seconds)
    base = cfg.command_timeout or int(active_seconds + 600)
    return max(1, min(3600, base))


def _is_retryable_exc(exc: Exception) -> bool:
    ename = type(exc).__name__
    msg = str(exc)
    return ("RetryableConflict" in ename or "Throttling" in ename
            or "retryableconflict" in msg.lower() or "throttling" in msg.lower())


def _is_session_start_failure(exc: Exception) -> bool:
    """Did the service decline to start this session, rather than the call failing?

    Observed as RuntimeClientError "An error occurred when starting the runtime" after a
    wait of minutes, with no log stream created for the session at all. Retrying the same
    session is futile — the same session fails the same way indefinitely — while a fresh
    session ID on the same runtime starts normally.
    """
    return ("RuntimeClientError" in type(exc).__name__
            or "error occurred when starting the runtime" in str(exc).lower())


def _session_id(run_id: str, root: Path, epoch: int) -> str:
    # Epoch 0 hashes exactly as it did before rotation existed, so a run already holding
    # a warm session keeps it across an upgrade.
    seed = run_id + str(root) + (f"#{epoch}" if epoch else "")
    return "codecouncil-" + run_id + "-" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def _session_epoch(root: Path) -> int:
    try:
        return int(json.loads((root / "session-epoch.json").read_text())["epoch"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


class RemoteProcess:
    """Asyncio-compatible process backed by an AgentCore InvokeAgentRuntimeCommand stream."""

    def __init__(self, root: Path):
        self.pid: int = 0
        self.returncode: int | None = None
        self._root = root
        self._stdout = asyncio.StreamReader(limit=8 * 1024 * 1024)
        self._stderr = asyncio.StreamReader(limit=8 * 1024 * 1024)
        self._stdout_buf = b""
        self._stderr_buf = b""
        self._done: asyncio.Event = asyncio.Event()
        self._error: Exception | None = None
        self._result: dict | None = None

    @property
    def stdout(self):
        return self._stdout

    @property
    def stderr(self):
        return self._stderr

    # ── Called via loop.call_soon_threadsafe from relay thread ──────────────

    def _feed_stdout_chunk(self, chunk: bytes):
        self._stdout_buf += chunk
        while b"\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
                if isinstance(msg, dict) and msg.get("type") == "__codecouncil":
                    self._handle_control(msg.get("data", {}))
                    continue
            except (ValueError, KeyError):
                pass
            self._stdout.feed_data(line + b"\n")

    def _feed_stderr_chunk(self, chunk: bytes):
        self._stderr_buf += chunk
        while b"\n" in self._stderr_buf:
            line, self._stderr_buf = self._stderr_buf.split(b"\n", 1)
            self._stderr.feed_data(line + b"\n")

    def _handle_control(self, data: dict):
        kind = data.get("kind")
        if kind == "started":
            self.pid = data.get("pid", self.pid)
        elif kind == "bridge_request":
            name = os.path.basename(data.get("name", ""))
            content = data.get("content", {})
            if name:
                req = self._root / "bridge" / "requests" / name
                try:
                    tmp = req.with_suffix(".tmp")
                    tmp.write_text(json.dumps(content))
                    tmp.replace(req)
                except Exception as exc:
                    log.warning("bridge_request write failed: %s", exc)
        elif kind == "result":
            self._result = data
        elif kind == "exit":
            if self.returncode is None:
                self.returncode = data.get("code", 0)
                self._done.set()
        elif kind == "note":
            log.info("agentcore[%s]: %s", self._root.name, data.get("text", ""))

    def _set_returncode(self, code: int):
        if self.returncode is None:
            self.returncode = code
            self._done.set()

    def _set_error(self, exc: Exception):
        self._error = exc
        if self.returncode is None:
            self.returncode = 1
            self._done.set()

    def _eof(self):
        # Flush incomplete buffered lines before sealing streams
        if self._stdout_buf:
            line = self._stdout_buf
            self._stdout_buf = b""
            try:
                msg = json.loads(line)
                if isinstance(msg, dict) and msg.get("type") == "__codecouncil":
                    self._handle_control(msg.get("data", {}))
                else:
                    self._stdout.feed_data(line)
            except ValueError:
                self._stdout.feed_data(line)
        if self._stderr_buf:
            self._stderr.feed_data(self._stderr_buf)
            self._stderr_buf = b""
        self._stdout.feed_eof()
        self._stderr.feed_eof()
        if self.returncode is None:
            self.returncode = 1
            self._done.set()

    # ── Public async interface ───────────────────────────────────────────────

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode  # type: ignore[return-value]

    async def communicate(self):
        async def _drain(reader):
            chunks = []
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

        out, err = await asyncio.gather(_drain(self._stdout), _drain(self._stderr))
        await self.wait()
        return out, err


def _relay_thread(arn, qualifier, session_id, command, timeout, loop, remote, client):
    """Consume the blocking EventStream; dispatch chunks to the event loop."""
    try:
        # Retry the call itself on transient session-busy (409) or throttle
        resp = None
        for attempt in range(8):
            try:
                resp = client.invoke_agent_runtime_command(
                    agentRuntimeArn=arn,
                    qualifier=qualifier,
                    runtimeSessionId=session_id,
                    contentType="application/json",
                    accept="application/vnd.amazon.eventstream",
                    body={"command": command, "timeout": timeout},
                )
                break
            except Exception as exc:
                if _is_retryable_exc(exc) and attempt < 7:
                    time.sleep(min(4.0, 0.25 * (2 ** attempt)))
                    continue
                raise

        for event in resp.get("stream") or []:
            chunk = event.get("chunk", {})
            if "contentDelta" in chunk:
                delta = chunk["contentDelta"]
                if "stdout" in delta:
                    raw = delta["stdout"]
                    if isinstance(raw, str):
                        raw = raw.encode()
                    loop.call_soon_threadsafe(remote._feed_stdout_chunk, raw)
                if "stderr" in delta:
                    raw = delta["stderr"]
                    if isinstance(raw, str):
                        raw = raw.encode()
                    loop.call_soon_threadsafe(remote._feed_stderr_chunk, raw)
            elif "contentStop" in chunk:
                stop = chunk["contentStop"]
                if stop.get("status") == "TIMED_OUT":
                    code = stop.get("exitCode") or 124
                    loop.call_soon_threadsafe(remote._feed_stderr_chunk,
                                              b"agentcore: command timed out\n")
                    loop.call_soon_threadsafe(remote._set_returncode, code)
                else:
                    loop.call_soon_threadsafe(remote._set_returncode,
                                              stop.get("exitCode", 0))
                break
            elif "accessDeniedException" in event:
                msg = event["accessDeniedException"].get("message", "accessDeniedException")
                loop.call_soon_threadsafe(remote._set_error, HarnessTurnError(msg, terminal=True))
                break
            elif "resourceNotFoundException" in event:
                msg = event["resourceNotFoundException"].get("message", "resourceNotFoundException")
                loop.call_soon_threadsafe(remote._set_error, HarnessTurnError(msg, terminal=True))
                break
            elif "validationException" in event:
                msg = event["validationException"].get("message", "validationException")
                loop.call_soon_threadsafe(remote._set_error, HarnessTurnError(msg, terminal=True))
                break
            elif "throttlingException" in event:
                msg = event["throttlingException"].get("message", "throttlingException")
                loop.call_soon_threadsafe(remote._set_error, HarnessTurnError(msg, terminal=False))
                break
            elif "internalServerException" in event:
                msg = event["internalServerException"].get("message", "internalServerException")
                loop.call_soon_threadsafe(remote._set_error, HarnessTurnError(msg, terminal=False))
                break
            elif "runtimeClientError" in event:
                msg = event["runtimeClientError"].get("message", "runtimeClientError")
                loop.call_soon_threadsafe(remote._set_error, HarnessTurnError(msg, terminal=False))
                break
    except Exception as exc:
        loop.call_soon_threadsafe(remote._set_error, exc)
    finally:
        # Always feed EOF so readline() terminates and the worker loop exits
        loop.call_soon_threadsafe(remote._eof)


class AgentCore:
    """AgentCore Runtime backend; one instance per Config."""

    def __init__(self, config: Config):
        self.config = config
        self._jobs: dict = {}
        self._boto_session = None
        self._s3: object = None
        self._control: object = None
        self._data: object = None

    # ── boto3 session / client helpers ───────────────────────────────────────

    def _session(self):
        if self._boto_session is None:
            import boto3
            self._boto_session = boto3.Session(
                profile_name=self.config.agentcore.profile,
                region_name=self.config.agentcore.region)
        return self._boto_session

    def _botoconfig(self, read_timeout=3660):
        from botocore.config import Config as _BC
        return _BC(retries={"max_attempts": 5, "mode": "standard"},
                   connect_timeout=10, read_timeout=read_timeout, tcp_keepalive=True)

    def _s3_client(self):
        if self._s3 is None:
            self._s3 = self._session().client("s3", config=self._botoconfig())
        return self._s3

    def _control_client(self):
        if self._control is None:
            self._control = self._session().client(
                "bedrock-agentcore-control", config=self._botoconfig())
        return self._control

    def _data_client(self):
        if self._data is None:
            # read_timeout exceeds the max command timeout so the socket never fires first
            self._data = self._session().client(
                "bedrock-agentcore", config=self._botoconfig(read_timeout=3660))
        return self._data

    # ── ARN resolution ───────────────────────────────────────────────────────

    async def _resolve_runtime(self, harness: str):
        """Return (arn, qualifier, status_detail_dict) for a harness or raise."""
        cfg = self.config.agentcore
        target = cfg.runtimes.get(harness)

        # 1. Explicitly configured ARN
        if target and target.arn:
            arn = target.arn
            qualifier = target.qualifier
            info = await asyncio.to_thread(
                self._control_client().get_agent_runtime, agentRuntimeId=_runtime_id(arn))
            status = info.get("status", "UNKNOWN")
            if status != "READY":
                raise RuntimeError(
                    f"AgentCore runtime '{harness}' status={status}. "
                    "Run `multi-coding-harness-compare agentcore-deploy` to provision.")
            return arn, qualifier, info

        # 2. agentcore-runtimes.json written by provisioner
        runtimes_file = self.config.root / "agentcore-runtimes.json"
        if runtimes_file.exists():
            try:
                entry = json.loads(runtimes_file.read_text()).get(harness, {})
                if entry.get("arn"):
                    arn = entry["arn"]
                    qualifier = (target.qualifier if target else None) or entry.get("qualifier", "DEFAULT")
                    info = await asyncio.to_thread(
                        self._control_client().get_agent_runtime, agentRuntimeId=_runtime_id(arn))
                    status = info.get("status", "UNKNOWN")
                    if status != "READY":
                        raise RuntimeError(
                            f"AgentCore runtime '{harness}' status={status}. "
                            "Run `multi-coding-harness-compare agentcore-deploy`.")
                    return arn, qualifier, info
            except RuntimeError:
                raise
            except Exception:
                pass

        # 3. list_agent_runtimes matched on canonical name. Accounts accumulate many
        # runtimes, so a single page can silently miss ours.
        isolation = getattr(cfg, "isolation", "shared")
        namespace = (getattr(self.config, "user_namespace", None)
                     if isolation == "per-user" else None)
        name = _runtime_name(cfg.runtime_name_prefix, harness, namespace)
        listed, token = [], None
        while True:
            resp = await asyncio.to_thread(
                lambda t=token: self._control_client().list_agent_runtimes(
                    **({"nextToken": t} if t else {})))
            listed += resp.get("agentRuntimes", [])
            token = resp.get("nextToken")
            if not token:
                break
        for rt in listed:
            if rt.get("agentRuntimeName") == name:
                arn = rt.get("agentRuntimeArn") or rt.get("agentRuntimeId")
                qualifier = (target.qualifier if target else None) or "DEFAULT"
                info = await asyncio.to_thread(
                    self._control_client().get_agent_runtime, agentRuntimeId=_runtime_id(arn))
                status = info.get("status", "UNKNOWN")
                if status != "READY":
                    raise RuntimeError(
                        f"AgentCore runtime '{harness}' status={status}. "
                        "Run `multi-coding-harness-compare agentcore-deploy`.")
                return arn, qualifier, info

        raise RuntimeError(
            f"No AgentCore runtime found for harness '{harness}'. "
            "Run `multi-coding-harness-compare agentcore-deploy` to provision.")

    # ── check ────────────────────────────────────────────────────────────────

    async def check(self):
        cfg = self.config.agentcore
        account = await asyncio.to_thread(
            lambda: self._session().client("sts").get_caller_identity()["Account"])

        runtimes: dict = {}
        any_resolved = False
        for harness in ("claude", "codex", "kiro"):
            try:
                arn, qualifier, info = await self._resolve_runtime(harness)
                artifact = info.get("agentRuntimeArtifact", {})
                bundle = (artifact.get("codeConfiguration", {})
                          .get("code", {}).get("s3", {}).get("prefix"))
                runtimes[harness] = {
                    "arn": arn,
                    "status": info.get("status", "UNKNOWN"),
                    "version": info.get("agentRuntimeVersion"),
                    "bundle": bundle,
                    "qualifier": qualifier,
                    # The runtime itself records where a payload too large to vendor was
                    # staged, so a missing local records file cannot lose the reference.
                    "tools_uri": info.get("environmentVariables", {}).get("CODECOUNCIL_TOOLS_URI"),
                }
                any_resolved = True
            except Exception as exc:
                runtimes[harness] = {"blocked": str(exc)}

        if not any_resolved:
            raise RuntimeError(
                "No AgentCore runtimes are available. "
                "Run `multi-coding-harness-compare agentcore-deploy` to provision.")

        fingerprint = hashlib.sha256(json.dumps(runtimes, sort_keys=True).encode()).hexdigest()
        return {
            "backend": "agentcore",
            "isolation": "Amazon Bedrock AgentCore Runtime Firecracker microVM",
            "architecture": "arm64",
            "region": cfg.region,
            "profile": cfg.profile,
            "account": account,
            "workspace_mount": cfg.workspace_mount,
            "runtimes": runtimes,
            "runtime_id": fingerprint,
            "limits": {
                "idle_session_timeout": cfg.idle_session_timeout,
                "max_lifetime": cfg.max_lifetime,
                "command_timeout": cfg.command_timeout,
                "active_seconds": self.config.active_seconds,
            },
        }

    # ── probe ────────────────────────────────────────────────────────────────

    async def probe(self, harnesses: list[str] | None = None) -> dict:
        """Verify the configured paths are writable in each deployed runtime.

        A redeploy can change the command user or the mounts underneath a working
        configuration, which otherwise surfaces as every harness failing a few seconds
        into a comparison. Reports the runtime user and, per path, whether writes,
        file locks (SQLite), and free space are what the layout assumes.
        """
        cfg = self.config.agentcore
        paths = [cfg.job_root, cfg.cache_root, cfg.workspace_mount, "/tmp"]
        payload = json.dumps({"paths": paths}).encode()
        command = _build_command(PROBE, payload)

        try:
            info = await self.check()
        except RuntimeError as exc:
            # No runtime resolved at all: still a JSON report, so callers can read why.
            return {"ok": False, "region": cfg.region, "error": str(exc), "runtimes": {},
                    "configured": {"job_root": cfg.job_root, "cache_root": cfg.cache_root,
                                   "workspace_mount": cfg.workspace_mount}}
        result = {"region": cfg.region, "account": info.get("account"),
                  "configured": {"job_root": cfg.job_root, "cache_root": cfg.cache_root,
                                 "workspace_mount": cfg.workspace_mount},
                  "runtimes": {}}

        for harness in (harnesses or ["claude", "codex", "kiro"]):
            entry = info["runtimes"].get(harness, {})
            if "blocked" in entry:
                result["runtimes"][harness] = {"blocked": entry["blocked"]}
                continue
            arn, qualifier = entry["arn"], entry["qualifier"]
            session_id = "codecouncil-probe-" + hashlib.sha256(
                f"{harness}{time.time()}".encode()).hexdigest()[:32]
            record = {"version": entry.get("version"), "arn": arn}
            try:
                if getattr(cfg, "session_activation", True):
                    await self._activate_session(arn, qualifier, session_id)
                out = await self._collect_invoke(arn, qualifier, session_id, command, 120)
                for line in out.splitlines():
                    if line.startswith("CODECOUNCIL_PROBE "):
                        record.update(json.loads(line[len("CODECOUNCIL_PROBE "):]))
                        break
                else:
                    record["error"] = out.strip()[-800:] or "probe produced no output"
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    await asyncio.to_thread(
                        self._data_client().stop_runtime_session,
                        agentRuntimeArn=arn, runtimeSessionId=session_id, qualifier=qualifier)
                except Exception:
                    pass
            usable = [p for p in record.get("paths", []) if p.get("write")]
            record["job_root_writable"] = any(
                p["path"] == cfg.job_root and p.get("write") for p in record.get("paths", []))
            record["writable_paths"] = [p["path"] for p in usable]
            result["runtimes"][harness] = record

        result["ok"] = all(r.get("job_root_writable") for r in result["runtimes"].values())
        return result

    async def _collect_invoke(self, arn: str, qualifier: str, session_id: str,
                              command: str, timeout: int) -> str:
        """Run one command and return stdout+stderr as text."""
        def _do():
            resp = self._data_client().invoke_agent_runtime_command(
                agentRuntimeArn=arn, qualifier=qualifier, runtimeSessionId=session_id,
                contentType="application/json",
                accept="application/vnd.amazon.eventstream",
                body={"command": command, "timeout": timeout})
            text = ""
            for event in resp.get("stream") or []:
                chunk = event.get("chunk", {})
                if "contentDelta" in chunk:
                    for key in ("stdout", "stderr"):
                        raw = chunk["contentDelta"].get(key)
                        if raw is not None:
                            text += raw if isinstance(raw, str) else raw.decode(errors="replace")
                elif "contentStop" in chunk:
                    break
                elif "contentStart" not in chunk:
                    text += json.dumps(event, default=str)
                    break
            return text
        return await asyncio.to_thread(_do)

    # ── start ────────────────────────────────────────────────────────────────

    async def start(self, run: dict, root: Path, phase: str) -> dict:
        cfg = self.config.agentcore
        harness = run["harness"]
        harness_cfg = self.config.harness_for_run(run)

        info = await self.check()
        harness_info = info["runtimes"].get(harness, {})
        if "blocked" in harness_info:
            raise RuntimeError(harness_info["blocked"])

        arn, qualifier = harness_info["arn"], harness_info["qualifier"]
        run_id = run["id"]

        # Deterministic session ID — consecutive turns land on the same warm microVM.
        # The epoch is the escape hatch: a session the service refuses to start stays
        # refused, so a run whose ID never changes can never be resumed off it.
        epoch = _session_epoch(root)
        session_id = _session_id(run_id, root, epoch)

        # The job tree lives on the writable overlay under /tmp. Neither it nor HOME may go
        # on the sessionStorage mount: it is NFS, where SQLite fails with "database is
        # locked", and both Codex and Kiro keep their session state in a SQLite database
        # under HOME. The mount is used for the durable tools cache only (see cache_root).
        # Persistence of the work itself is host-owned — it rides the job/result tarballs.
        job_root = getattr(cfg, "job_root", "/var/tmp/codecouncil")
        jobdir = f"{job_root}/{run_id}"
        home_remote = jobdir + "/home"
        workspace_remote = jobdir + "/workspace"

        # Prepare the local HOME that is uploaded into the microVM. The copy happens on
        # the host, so a defaulted source needs nothing to exist inside the microVM.
        home = root / "home"
        from .host_setup import prepare_home, _require_kiro_login
        home_setup = prepare_home(harness, harness_cfg, home,
                                  configuration_choice=run.get("configuration_choice",
                                                               self.config.default_configuration_choice),
                                  size_cap_bytes=self.config.harness_config_size_cap_mb * 1024 * 1024)
        # Point 3: block before a kiro turn starts if no login will be available.
        if harness == "kiro":
            _require_kiro_login(harness_cfg, home)

        for d in ("workspace", "bridge", "bridge/requests", "bridge/answers"):
            (root / d).mkdir(parents=True, exist_ok=True)

        control_dir = root / "control"
        control_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(control_dir / "state.json", {"seq": 0, "signal": None})

        name = "arena-" + run_id
        bucket, s3_prefix = _parse_s3_uri(cfg.transfer_s3_uri)
        # USER_NAMESPACE scopes every S3 key so one user's objects are not accessible
        # to another user's execution role when AGENTCORE_ISOLATION=per-user is set.
        user_namespace: str | None = getattr(self.config, "user_namespace", None)
        sessions_dir = self.config.root / "agentcore-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        def adopt(chosen: str):
            self._jobs[name] = {
                "run": run, "root": root, "phase": phase,
                "harness": harness, "session_id": chosen,
                "arn": arn, "qualifier": qualifier,
                "jobdir": jobdir, "home_remote": home_remote,
                "tools_uri": harness_info.get("tools_uri"),
            }
            # Persist session record so cleanup_owned can stop it if the process dies.
            # The s3_prefix includes the namespace segment when USER_NAMESPACE is set.
            session_s3_prefix = _s3_key(s3_prefix, user_namespace, f"sessions/{chosen}/")
            atomic_json(sessions_dir / (chosen + ".json"), {
                "session_id": chosen, "run_id": run_id, "harness": harness,
                "arn": arn, "qualifier": qualifier,
                "bucket": bucket, "s3_prefix": session_s3_prefix,
                "owner": self.config.container_owner,
                "started_at": time.time(),
            })

        adopt(session_id)

        # Warm a cold session with invoke_agent_runtime before any command API call
        session_activation = getattr(cfg, "session_activation", True)
        if session_activation:
            rotated, attempt = False, 0
            while True:
                try:
                    await self._activate_session(arn, qualifier, session_id)
                    break
                except Exception as exc:
                    if _is_retryable_exc(exc) and attempt < 5:
                        attempt += 1
                        await asyncio.sleep(min(3.0, 0.5 * attempt))
                        continue
                    if _is_session_start_failure(exc) and not rotated:
                        # Rotate once, here, rather than leaving the operator to resume
                        # onto the same refused session forever. The epoch is persisted so
                        # later turns of this run keep using the replacement.
                        rotated, attempt = True, 0
                        epoch += 1
                        atomic_json(root / "session-epoch.json",
                                    {"epoch": epoch, "rotated_at": time.time(),
                                     "reason": str(exc)[:500], "replaced": session_id})
                        session_id = _session_id(run_id, root, epoch)
                        log.warning("agentcore[%s]: session would not start; rotated to "
                                    "epoch %d (%s)", run_id, epoch, session_id)
                        adopt(session_id)
                        continue
                    raise

        active_seconds = self.config.active_seconds
        return {
            **info,
            "name": name,
            "session_id": session_id,
            "workspace": workspace_remote,
            "jobdir": jobdir,
            "runtime_arn": arn,
            "qualifier": qualifier,
            "harness": harness,
            "network": ("Outbound HTTPS via the AgentCore execution role; "
                        "no inbound; sessionStorage persists across turns of the same run"),
            "limits": {
                "command_timeout": _effective_command_timeout(cfg, active_seconds),
                "idle_session_timeout": cfg.idle_session_timeout,
                "max_lifetime": cfg.max_lifetime,
                "active_seconds": active_seconds,
            },
            "home_setup": home_setup,
            "configuration": {
                **harness_cfg.model_dump(exclude={"home_template"}),
                "home_template_configured": bool(harness_cfg.home_template),
                "environment_keys_present": [k for k in harness_cfg.env if k in os.environ],
            },
        }

    # ── launch ───────────────────────────────────────────────────────────────

    async def launch(self, name: str, args: list | None = None) -> RemoteProcess:
        job = self._jobs[name]
        run, root, phase = job["run"], job["root"], job["phase"]
        harness, session_id = job["harness"], job["session_id"]
        arn, qualifier = job["arn"], job["qualifier"]
        cfg = self.config.agentcore
        harness_cfg = self.config.harness_for_run(run)
        run_id = run["id"]

        # Turn key for S3 path (set by worker before calling launch)
        try:
            turn_key = json.loads((root / "bridge" / "turn.json").read_text())["turn_key"]
        except Exception:
            import uuid
            turn_key = uuid.uuid4().hex

        bucket, s3_prefix = _parse_s3_uri(cfg.transfer_s3_uri)
        # USER_NAMESPACE scopes every S3 key; None preserves the existing layout.
        # Layout without namespace: {prefix}/sessions/{session}/{turn}/{job|result}.tar.gz
        # Layout with namespace:    {prefix}/{namespace}/sessions/{session}/{turn}/...
        user_namespace: str | None = getattr(self.config, "user_namespace", None)
        job_s3_key = _s3_key(s3_prefix, user_namespace,
                             f"sessions/{session_id}/{turn_key}/job.tar.gz")
        result_s3_key = _s3_key(s3_prefix, user_namespace,
                                f"sessions/{session_id}/{turn_key}/result.tar.gz")
        secrets_s3_key = _s3_key(s3_prefix, user_namespace,
                                 f"secrets/{session_id}/{turn_key}.bin")
        jobdir = job["jobdir"]
        home_remote = job["home_remote"]

        # A harness payload too large to vendor is staged in S3. Prefer the provisioner's
        # records file, and fall back to the URI the runtime resource itself carries so a
        # different data_dir cannot silently drop the reference.
        tools_stage = None
        runtimes_file = self.config.root / "agentcore-runtimes.json"
        if runtimes_file.exists():
            try:
                meta = json.loads(runtimes_file.read_text()).get(harness, {})
                if meta.get("tools_key"):
                    tools_stage = {"bucket": bucket, "key": meta["tools_key"],
                                   "version": meta.get("tools_version", "unknown"),
                                   "region": cfg.region}
            except Exception:
                pass
        if tools_stage is None and job.get("tools_uri"):
            stage_bucket, stage_key = _parse_s3_uri(job["tools_uri"])
            version = Path(stage_key).name.removesuffix(".tar.gz").rpartition("-")[2] or "unknown"
            tools_stage = {"bucket": stage_bucket, "key": stage_key,
                           "version": version, "region": cfg.region}

        # Collect secret files from HOME before building any tarball. They travel via
        # a dedicated encrypted S3 object, never inside the shared job tarball.
        secrets_data = _collect_secrets(root / "home", harness)

        job_json = {
            "run_id": run_id, "harness": harness, "phase": phase,
            "turn": turn_key, "read_only": phase == "clarify",
            "jobdir": jobdir,
            "workspace": jobdir + "/workspace",
            "bridge": jobdir + "/bridge",
            "control": jobdir + "/control",
            "home": home_remote,
            "tmp": jobdir + "/tmp",
            # Durable across cold microVMs; the staged tools tree is downloaded once here
            # instead of on every session. Falls back to the job tree if the mount is gone.
            "cache_root": getattr(cfg, "cache_root", None) or (jobdir + "/cache"),
            "cache_fallback": jobdir + "/cache",
            "argv": args or ["python", "-m", "arena.native_runner"],
            "env": _harness_env(harness, cfg, harness_cfg),
            "result": {"bucket": bucket, "key": result_s3_key},
            "poll_seconds": cfg.control_poll_seconds,
            "heartbeat_seconds": getattr(cfg, "stream_heartbeat_seconds", 120),
            "region": cfg.region,
        }
        if tools_stage:
            job_json["tools_stage"] = tools_stage
        # Secrets coordinates (bucket/key only; content never in job.json or logs).
        if secrets_data:
            job_json["secrets"] = {"bucket": bucket, "key": secrets_s3_key}

        # Build tarball: workspace + bridge + home (secrets filtered) + arena source + job.json
        arena_src = Path(__file__).parent
        from .host_setup import HOME_SECRETS
        _secret_names = frozenset(HOME_SECRETS.get(harness, ()))

        def _arena_filter(ti):
            parts = PurePosixPath(ti.name).parts
            if any(p in ("__pycache__", ".venv") for p in parts):
                return None
            if ti.name.endswith(".pyc"):
                return None
            return ti

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for local, arcname in (
                (root / "workspace", "workspace"),
                (root / "bridge", "bridge"),
            ):
                if local.exists():
                    tf.add(str(local), arcname=arcname)
            # HOME is filtered: known-secret files are excluded here and delivered via
            # the dedicated secrets channel instead to prevent credential leakage.
            home_local = root / "home"
            if home_local.exists():
                tf.add(str(home_local), arcname="home",
                       filter=_secrets_filter(_secret_names))
            tf.add(str(arena_src), arcname="arena", filter=_arena_filter)
            job_bytes = json.dumps(job_json).encode()
            ti = tarfile.TarInfo(name="job.json")
            ti.size = len(job_bytes)
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(job_bytes))

        # Upload secrets object before job tarball so the in-VM agent can find it.
        if secrets_data:
            packed = _pack_secrets(secrets_data)
            kms_key = getattr(cfg, "transfer_kms_key_arn", None)
            await asyncio.to_thread(
                _put_secrets_object,
                self._s3_client(), bucket, secrets_s3_key, packed, kms_key)
            log.info("agentcore[%s]: secrets object uploaded (bucket=%s key=%s)",
                     run_id, bucket, secrets_s3_key)
            # Pre-register bucket and key immediately so stop() can attempt deletion
            # even if launch() raises before job.update() is reached below.
            job["bucket"] = bucket
            job["secrets_s3_key"] = secrets_s3_key

        await asyncio.to_thread(
            self._s3_client().put_object,
            Bucket=bucket, Key=job_s3_key, Body=buf.getvalue(),
            ServerSideEncryption="AES256")

        payload = {
            "bucket": bucket, "key": job_s3_key,
            "jobdir": jobdir, "home": home_remote,
            "pythonpath": jobdir + ":/var/task",
            "region": cfg.region,
        }
        command = _build_command(BOOTSTRAP, json.dumps(payload).encode())
        # Derive the remote ceiling from the run's active budget, as documented.
        # Passing 0 here pinned it to 600s no matter how large active_seconds was,
        # and also capped any explicit command_timeout to that same 600s ceiling.
        # This clock runs while the turn is paused for a human answer, so a short
        # ceiling kills a harness that asked a clarifying question.
        timeout = _effective_command_timeout(cfg, self.config.active_seconds)

        loop = asyncio.get_running_loop()
        remote = RemoteProcess(root=root)

        relay = threading.Thread(
            target=_relay_thread,
            args=(arn, qualifier, session_id, command, timeout, loop, remote, self._data_client()),
            daemon=True, name=f"agentcore-relay-{run_id}")
        relay.start()

        mirror = asyncio.create_task(
            self._mirror_loop(name, root, jobdir, arn, qualifier, session_id,
                              cfg.control_poll_seconds),
            name=f"agentcore-mirror-{run_id}")

        job.update(remote=remote, relay_thread=relay, mirror_task=mirror,
                   bucket=bucket, s3_key=job_s3_key, result_s3_key=result_s3_key,
                   secrets_s3_key=secrets_s3_key if secrets_data else None)
        return remote

    # ── supervise ────────────────────────────────────────────────────────────

    async def supervise(self, name: str, force: bool = False) -> None:
        # Cheap path: no AWS calls. Re-raise any error the relay captured.
        job = self._jobs.get(name)
        if not job:
            return
        remote = job.get("remote")
        if remote is not None and remote._error is not None:
            raise remote._error

    # ── pause / unpause ──────────────────────────────────────────────────────

    async def pause(self, name: str):
        job = self._jobs.get(name)
        if not job:
            return 0, "", ""
        sf = job["root"] / "control" / "state.json"
        try:
            seq = json.loads(sf.read_text()).get("seq", 0)
        except Exception:
            seq = 0
        atomic_json(sf, {"seq": seq + 1, "signal": "stop"})
        return 0, "", ""

    async def unpause(self, name: str):
        job = self._jobs.get(name)
        if not job:
            return 0, "", ""
        sf = job["root"] / "control" / "state.json"
        try:
            seq = json.loads(sf.read_text()).get("seq", 0)
        except Exception:
            seq = 0
        atomic_json(sf, {"seq": seq + 1, "signal": "cont"})
        return 0, "", ""

    # ── stop ─────────────────────────────────────────────────────────────────

    async def stop(self, name: str) -> str | None:
        """Stop a running job and clean up S3 objects.

        Returns a warning string when the secrets object could not be deleted,
        None otherwise.  The caller should surface any returned warning on the
        run so the operator knows to inspect leftovers with the preflight
        agentcore-secret-leftovers check (list_leftover_secrets).
        """
        job = self._jobs.pop(name, None)
        if not job:
            return None  # idempotent

        root = job.get("root")
        if root:
            sf = root / "control" / "state.json"
            try:
                seq = json.loads(sf.read_text()).get("seq", 0) if sf.exists() else 0
            except Exception:
                seq = 0
            try:
                atomic_json(sf, {"seq": seq + 1, "signal": "term"})
            except Exception:
                pass

        # Resolve bucket once; needed in both the try body and the finally clause.
        bucket = job.get("bucket")
        warning: str | None = None
        try:
            remote = job.get("remote")
            if remote is not None:
                try:
                    await asyncio.wait_for(remote.wait(), 7.0)
                except (TimeoutError, asyncio.TimeoutError):
                    pass

            mirror = job.get("mirror_task")
            if mirror is not None:
                mirror.cancel()
                await asyncio.gather(mirror, return_exceptions=True)

            relay = job.get("relay_thread")
            if relay is not None:
                # daemon thread — wrap in to_thread so the event loop is not stalled
                await asyncio.to_thread(relay.join, timeout=2)

            # Download workspace if the in-VM agent uploaded it
            if remote is not None and remote._result and root:
                r = remote._result
                b, k = r.get("bucket"), r.get("key")
                if b and k:
                    try:
                        await self._download_workspace(root, b, k)
                    except Exception as exc:
                        log.warning("workspace download failed: %s", exc)
                    # Delete all versions so stale versions on a versioned bucket cannot
                    # be read by another user's execution role.
                    try:
                        await asyncio.to_thread(
                            _delete_all_versions_sync, self._s3_client(), b, k)
                    except Exception:
                        pass

            # Clean up the job tarball; leave session alive for warm reuse on next turn.
            # Delete all versions for the same reason as the result tarball above.
            s3_key = job.get("s3_key")
            if bucket and s3_key:
                try:
                    await asyncio.to_thread(
                        _delete_all_versions_sync, self._s3_client(), bucket, s3_key)
                except Exception:
                    pass

        finally:
            # Delete secrets object (all versions).  Attempted in a finally so it
            # runs even when the code above is interrupted by an exception or
            # CancelledError.  Warn — and return the warning — if removal fails so
            # the caller can surface it in the run's visible error field.
            secrets_s3_key = job.get("secrets_s3_key")
            if bucket and secrets_s3_key:
                ok = False
                try:
                    ok = await asyncio.to_thread(
                        _delete_all_versions_sync, self._s3_client(), bucket, secrets_s3_key)
                except Exception:
                    pass
                if not ok:
                    run_id = job.get("run", {}).get("id", "?")
                    log.warning(
                        "agentcore[%s]: secrets object not deleted (bucket=%s key=%s); "
                        "run list_leftover_secrets to inspect",
                        run_id, bucket, secrets_s3_key)
                    warning = (
                        f"Secrets object not deleted from S3 after this turn "
                        f"(key={secrets_s3_key}). Run multi-coding-harness-compare "
                        f"preflight: its agentcore-secret-leftovers check "
                        f"(list_leftover_secrets) lists objects to delete."
                    )
        return warning

    # ── cleanup_owned ────────────────────────────────────────────────────────

    async def cleanup_owned(self) -> None:
        sessions_dir = self.config.root / "agentcore-sessions"
        if not sessions_dir.exists():
            return
        for record_file in list(sessions_dir.glob("*.json")):
            try:
                rec = json.loads(record_file.read_text())
                if rec.get("owner") != self.config.container_owner:
                    continue
                session_id = rec.get("session_id")
                arn = rec.get("arn")
                if session_id and arn:
                    try:
                        # Session lifecycle lives on the data plane, not the control plane.
                        await asyncio.to_thread(
                            self._data_client().stop_runtime_session,
                            agentRuntimeArn=arn, runtimeSessionId=session_id,
                            qualifier=rec.get("qualifier", "DEFAULT"))
                    except Exception as exc:
                        if "ResourceNotFoundException" not in type(exc).__name__:
                            log.warning("stop_runtime_session %s: %s", session_id, exc)
                bucket, prefix = rec.get("bucket"), rec.get("s3_prefix")
                if bucket and prefix:
                    try:
                        resp = await asyncio.to_thread(
                            self._s3_client().list_objects_v2, Bucket=bucket, Prefix=prefix)
                        # Delete all versions of each key so a versioned bucket does not
                        # retain objects readable by another user's execution role.
                        for obj in resp.get("Contents", []):
                            try:
                                await asyncio.to_thread(
                                    _delete_all_versions_sync,
                                    self._s3_client(), bucket, obj["Key"])
                            except Exception:
                                pass
                    except Exception as exc:
                        log.warning("S3 cleanup for %s: %s", session_id, exc)
            except Exception as exc:
                log.warning("cleanup_owned %s: %s", record_file, exc)
            finally:
                try:
                    record_file.unlink(missing_ok=True)
                except Exception:
                    pass

    # ── doctor helpers ───────────────────────────────────────────────────────

    async def list_leftover_secrets(self) -> list[dict]:
        """List secrets objects that were not deleted after their turn.

        A non-empty result means a turn ended without the final host-side deletion
        running (network failure, crash).  The operator or a doctor command should
        inspect and delete these objects.  Returns a list of
        {bucket, key, last_modified} dicts; never raises.
        """
        cfg = self.config.agentcore
        if not cfg.transfer_s3_uri:
            return []
        bucket, s3_prefix = _parse_s3_uri(cfg.transfer_s3_uri)
        user_namespace: str | None = getattr(self.config, "user_namespace", None)
        secrets_prefix = _s3_key(s3_prefix, user_namespace, "secrets/")
        try:
            resp = await asyncio.to_thread(
                self._s3_client().list_objects_v2, Bucket=bucket, Prefix=secrets_prefix)
            return [
                {"bucket": bucket, "key": obj["Key"],
                 "last_modified": str(obj.get("LastModified", ""))}
                for obj in resp.get("Contents", [])
            ]
        except Exception as exc:
            log.warning("list_leftover_secrets: %s", exc)
            return []

    # ── internal helpers ─────────────────────────────────────────────────────

    async def _activate_session(self, arn: str, qualifier: str, session_id: str) -> None:
        # Warms a cold session; invoke_agent_runtime_command returns 500 on a cold one.
        def _do():
            resp = self._data_client().invoke_agent_runtime(
                agentRuntimeArn=arn, qualifier=qualifier,
                runtimeSessionId=session_id,
                contentType="application/json", accept="application/json",
                payload=b'{}')
            try:
                resp.get("response", resp).read()
            except Exception:
                pass
        await asyncio.to_thread(_do)

    async def _simple_invoke(self, arn: str, qualifier: str, session_id: str,
                              command: str, timeout: int = 30) -> int:
        """Synchronous fire-and-forget invoke; used for DELIVER commands."""
        def _do():
            resp = self._data_client().invoke_agent_runtime_command(
                agentRuntimeArn=arn, qualifier=qualifier,
                runtimeSessionId=session_id,
                contentType="application/json",
                accept="application/vnd.amazon.eventstream",
                body={"command": command, "timeout": timeout})
            for event in resp.get("stream") or []:
                chunk = event.get("chunk", {})
                if "contentStop" in chunk:
                    return chunk["contentStop"].get("exitCode", 0)
            return 0
        return await asyncio.to_thread(_do)

    async def _mirror_loop(self, name: str, root: Path, jobdir: str,
                            arn: str, qualifier: str, session_id: str,
                            poll_seconds: float) -> None:
        """Mirror local control files into the remote microVM via batched DELIVER commands."""
        bridge = root / "bridge"
        control = root / "control"
        watched = {
            "bridge/session.json": bridge / "session.json",
            "bridge/cancel.json": bridge / "cancel.json",
            "control/state.json": control / "state.json",
        }
        sums: dict = {}

        while True:
            try:
                await asyncio.sleep(poll_seconds)
                if name not in self._jobs:
                    break

                changed: dict = {}
                for rel, path in watched.items():
                    s = _sha256_file(path)
                    if s and s != sums.get(rel):
                        sums[rel] = s
                        try:
                            changed[rel] = path.read_text()
                        except Exception:
                            pass

                answers_dir = bridge / "answers"
                if answers_dir.exists():
                    for af in answers_dir.glob("*.json"):
                        rel = "bridge/answers/" + af.name
                        s = _sha256_file(af)
                        if s and s != sums.get(rel):
                            sums[rel] = s
                            try:
                                changed[rel] = af.read_text()
                            except Exception:
                                pass

                if not changed:
                    continue

                cmd = _build_command(DELIVER, json.dumps({"dir": jobdir, "files": changed}).encode())
                try:
                    await self._simple_invoke(arn, qualifier, session_id, cmd, timeout=30)
                except Exception as exc:
                    log.warning("mirror deliver failed: %s", exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("mirror loop error: %s", exc)

    async def _download_workspace(self, root: Path, bucket: str, key: str) -> None:
        """Download the result tarball and atomically swap root/workspace and root/home."""
        import shutil

        data = await asyncio.to_thread(
            lambda: self._s3_client().get_object(Bucket=bucket, Key=key)["Body"].read())

        incoming = root / "workspace.incoming"
        if incoming.exists():
            make_writable(incoming)
            remove_tree(incoming)
        incoming.mkdir(parents=True, exist_ok=True)

        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tf.extractall(str(incoming), filter="data")

        # "workspace" is required; "home" only appears once the harness has session state.
        # An older tarball without either arcname is the workspace itself.
        members = {name: incoming / name for name in ("workspace", "home")
                   if (incoming / name).is_dir()}
        if not members:
            members = {"workspace": incoming}

        for name, src in members.items():
            target, old = root / name, root / (name + ".old")
            if old.exists():
                make_writable(old)
                shutil.rmtree(str(old), ignore_errors=True)
            if target.exists():
                target.rename(old)
            src.rename(target)
            if old.exists():
                try:
                    make_writable(old)
                    shutil.rmtree(str(old), ignore_errors=True)
                except Exception:
                    pass

        if incoming.exists():
            shutil.rmtree(str(incoming), ignore_errors=True)
