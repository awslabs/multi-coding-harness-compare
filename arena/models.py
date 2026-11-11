# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import hashlib
import json
import shutil
import sys
import time
from contextlib import suppress

from .isolation import execution_runtime
from .store import HARNESS_NAMES


class ModelCatalogue:
    """Bounded metadata discovery, cached separately from development runs."""

    def __init__(self, config, store):
        self.config, self.store = config, store
        # Separate ownership prevents worker recovery from killing an API probe.
        self.probe_config = config.model_copy(update={
            "data_dir": str(config.root / "model-discovery"), "native_port_start": 60000})
        self.probe_config.root.mkdir(parents=True, exist_ok=True)
        self.runtime = execution_runtime(self.probe_config)
        self.lock = asyncio.Lock()
        self.task = None
        self.fingerprint = hashlib.sha256(json.dumps({
            "runtime": config.runtime,
            "harnesses": {h: config.harnesses[h].model_dump() for h in HARNESS_NAMES}
        }, sort_keys=True).encode()).hexdigest()

    def cached(self):
        try:
            data = self.store.get("system", "model-catalogue")
            if data.get("fingerprint") == self.fingerprint:
                return data
        except KeyError:
            pass
        return {"id": "model-catalogue", "fingerprint": self.fingerprint,
                "checked_at": 0, "harnesses": {}}

    async def get(self, refresh=False):
        async with self.lock:
            cached = self.cached()
            # Only an explicit refresh probes (the UI requests one at startup).
            if not refresh and cached["checked_at"]:
                return cached
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self.discover_all(cached))
            task = self.task
        # A browser navigation must not abandon subprocess cleanup.
        return await asyncio.shield(task)

    async def discover_all(self, cached):
        await self.runtime.cleanup_owned()
        harnesses = {}
        # Only AgentCore pins the account the harnesses invoke from (the execution role,
        # reached through the agentcore profile); native uses host setups.
        try:
            profiles = (await asyncio.to_thread(self.bedrock_profiles)
                        if self.config.runtime == "agentcore" else {})
        except Exception:
            profiles = {}
        # Probe one CLI at a time to keep memory use low.
        for harness in HARNESS_NAMES:
            previous = cached["harnesses"].get(harness, {})
            entry = {"models": previous.get("models", []),
                     "updated_at": previous.get("updated_at"),
                     "source": previous.get("source"),
                     "configured_default": self.config.harnesses[harness].model,
                     "setup": ("existing macOS CLI" if self.config.harnesses[harness].inherit_host_setup
                               else "dedicated setup")}
            try:
                # Includes an AgentCore session start, Codex's cold start and session stop.
                async with asyncio.timeout(120):
                    result = await self.probe(harness)
                # Preserve native order while removing duplicate identifiers.
                result["models"] = list({m["id"]: m for m in result["models"]
                                        if isinstance(m.get("id"), str) and m["id"]}.values())
                if not result["models"]:
                    raise RuntimeError("No models")
                entry.update(result, updated_at=time.time(), stale=False, error=None)
            except Exception as exc:
                entry.update(stale=True, error=(
                    f"Model discovery unavailable ({type(exc).__name__}). "
                    "Check this harness's installation and configured provider setup, then refresh."))
            # Each CLI offers a short curated list; append every other active Bedrock
            # inference profile from its provider, since both invoke by profile ID.
            provider = {"claude": ".anthropic.", "codex": ".openai."}.get(harness)
            listed = {m["id"] for m in entry["models"]}
            entry["models"] = entry["models"] + [
                {"id": pid, "name": name, "description": "Bedrock inference profile"}
                for pid, name in sorted(profiles.items())
                if provider and provider in pid and pid not in listed]
            harnesses[harness] = entry
        return self.store.put("system", {
            **cached, "checked_at": time.time(), "harnesses": harnesses})

    def bedrock_profiles(self):
        import boto3
        client = boto3.Session(profile_name=self.config.agentcore.profile,
                               region_name=self.config.agentcore.region).client("bedrock")
        return {s["inferenceProfileId"]: s["inferenceProfileName"]
                for page in client.get_paginator("list_inference_profiles").paginate()
                for s in page["inferenceProfileSummaries"] if s["status"] == "ACTIVE"}

    async def probe(self, harness):
        root = self.probe_config.root / harness
        root.mkdir(exist_ok=True)
        # Copy current template credentials into the isolated metadata HOME.
        shutil.rmtree(root / "home", ignore_errors=True)
        run = {"id": "models-" + harness, "harness": harness}
        name, communication = "arena-" + run["id"], None
        try:
            await self.runtime.start(run, root, "clarify")
            python = sys.executable if self.config.runtime == "native" else "python"
            process = await self.runtime.launch(name, [python, "-m", "arena.model_probe", harness])
            communication = asyncio.create_task(process.communicate())
            while not communication.done():
                await asyncio.wait({communication}, timeout=.5)
                await self.runtime.supervise(name)
            out, _ = await communication
            for line in reversed(out.splitlines()):
                with suppress(ValueError):
                    message = json.loads(line)
                    if message.get("type") == "model_catalogue":
                        return message["data"]
            raise RuntimeError("The native catalogue was unavailable")
        finally:
            await self.runtime.stop(name)
            if communication:
                communication.cancel()
                await asyncio.gather(communication, return_exceptions=True)
            shutil.rmtree(root / "home", ignore_errors=True)

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.runtime.cleanup_owned()
