# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path


class HarnessTurnError(RuntimeError):
    """A turn failed for a reason the raiser can classify.

    Message wording is a poor terminal/transient signal: a bridge that dropped
    its socket and a bridge that needs a login both read as "unavailable". Set
    `terminal` where the cause is known so a retryable failure is not recorded
    as one that needs a human.
    """

    def __init__(self, message: str, *, terminal: bool):
        super().__init__(message)
        self.terminal = terminal


def emit(type: str, data: dict):
    print(json.dumps({"type": type, "data": data}, default=str), flush=True)


def session_missing(exc: Exception) -> bool:
    """Does the CLI have no record of the session we asked it to resume?

    A turn killed before it uploads its result never returns HOME, so the session it
    created survives only in a microVM that is gone — while the host still holds the id
    and offers it to the next attempt. There is no context left to preserve, so the
    honest response is to start a new session rather than fail the run forever.
    """
    text = str(exc).lower()
    return any(phrase in text for phrase in
               ("session not found", "thread not found", "no such session", "unknown thread",
                "no rollout found"))


def codex_command(*arguments, executable=None):
    command = [executable or os.environ.get("CODECOUNCIL_CODEX_CLI", "codex")]
    region = os.environ.get("CODECOUNCIL_CODEX_REGION")
    if region:
        # The override must name the provider the HOME configuration actually selects, or it
        # silently applies to nothing. Codex ships two: amazon-bedrock (mantle endpoint) and
        # amazon-bedrock-runtime (bedrock-runtime endpoint).
        provider = os.environ.get("CODECOUNCIL_CODEX_PROVIDER", "amazon-bedrock")
        command += ["-c", f"model_providers.{provider}.aws.region=" + json.dumps(region)]
    return [*command, *arguments]


class RPC:
    """Bidirectional newline JSON RPC; requests never block the reader."""
    def __init__(self, command: list[str], request, notification):
        self.command = command
        self.on_request = request
        self.on_notification = notification
        self.pending = {}
        self.sequence = 0
        self.handlers = set()

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024, start_new_session=True)
        self.reader = asyncio.create_task(self.read())
        self.stderr = asyncio.create_task(self.read_stderr())
        return self

    async def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def call(self, method, params, timeout=None):
        self.sequence += 1
        id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[id] = future
        await self.send({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
        started = asyncio.get_running_loop().time()
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            # asyncio.wait_for raises TimeoutError with no message, so an escaping
            # one reaches the operator as the single word "TimeoutError" — the method
            # that stalled, the ceiling it hit, and whether the CLI is still alive all
            # have to be named here or they are lost.
            # A CLI that died is reported by read()'s EOF handler instead, so state
            # the liveness rather than leaving the reader to guess which happened.
            elapsed = asyncio.get_running_loop().time() - started
            alive = self.process.returncode is None
            raise HarnessTurnError(
                f"Native {method} did not answer within {timeout}s (waited {elapsed:.1f}s); "
                + ("the CLI is still running, so it is slow to start rather than broken"
                   if alive else f"the CLI had exited {self.process.returncode}"),
                terminal=False) from exc
        finally:
            self.pending.pop(id, None)

    async def notify(self, method, params=None):
        await self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def reply(self, msg):
        try:
            result = await self.on_request(msg["method"], msg.get("params", {}))
            await self.send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except Exception as exc:
            await self.send({"jsonrpc": "2.0", "id": msg["id"],
                             "error": {"code": -32601, "message": str(exc)}})

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    emit("diagnostic", {"text": line.decode(errors="replace")})
                    continue
                if "method" in msg:
                    if "id" in msg:
                        task = asyncio.create_task(self.reply(msg))
                        self.handlers.add(task)
                        task.add_done_callback(self.handlers.discard)
                    else:
                        await self.on_notification(msg["method"], msg.get("params", {}))
                elif msg.get("id") in self.pending:
                    future = self.pending[msg["id"]]
                    if not future.done():
                        if "error" in msg:
                            future.set_exception(RuntimeError(json.dumps(msg["error"])))
                        else:
                            future.set_result(msg.get("result", {}))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Native protocol stream closed"))

    async def read_stderr(self):
        while line := await self.process.stderr.readline():
            emit("diagnostic", {"text": line.decode(errors="replace").rstrip()})

    async def close(self):
        for task in self.handlers:
            task.cancel()
        # MCP/tool subprocesses can inherit protocol pipes after the native CLI
        # exits. Terminate this private process group, then bound pipe draining.
        with suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), 3)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 3)
        self.reader.cancel()
        self.stderr.cancel()
        await asyncio.gather(self.reader, self.stderr, return_exceptions=True)


async def version(*command, timeout=None):
    p = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except TimeoutError as exc:
        # Paging a few hundred megabytes of CLI off a cold overlay is measured in
        # seconds, so this can be slow; unbounded it would hold the turn until the
        # remote command ceiling fired with nothing to show for it.
        with suppress(ProcessLookupError):
            p.kill()
        raise HarnessTurnError(
            f"{command[0]} did not report its version within {timeout}s", terminal=False) from exc
    if p.returncode:
        raise RuntimeError((err or out).decode(errors="replace"))
    return out.decode(errors="replace").strip()


def _bridge_environment():
    # Codex intentionally filters inherited MCP environment keys, so anything the bridge
    # needs must be named explicitly. Locally `mcp` sits in the interpreter's own
    # site-packages, but on AgentCore it is vendored on PYTHONPATH; without it the server
    # dies on import and the harness sees "connection closed: initialize response".
    env = {"ARENA_BRIDGE": os.environ.get("ARENA_BRIDGE", "/bridge"), "PYTHONUNBUFFERED": "1"}
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


BRIDGE_SERVER = {"command": sys.executable,
                 "args": [str(Path(__file__).resolve().parent.parent / "bridge.py")],
                 "env": _bridge_environment()}
