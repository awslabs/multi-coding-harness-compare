# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Bedrock metadata discovery for the independent judge, without model invocations."""
import asyncio
import hashlib
import json
import time

from .config import Config


def catalogue_fingerprint(config: Config) -> str:
    return hashlib.sha256(json.dumps(
        ["us-inference-profiles-v1", config.judge_profile, config.judge_region]).encode()).hexdigest()


def foundation_entry(model: dict, *, profile: bool = False) -> dict:
    reason = None
    if "TEXT" not in model.get("inputModalities", []) or "TEXT" not in model.get("outputModalities", []):
        reason = "A text-input, text-output model is required for judging."
    elif model.get("modelLifecycle", {}).get("status") not in ("ACTIVE", "LEGACY"):
        reason = "This model is not active."
    elif not profile and "ON_DEMAND" not in model.get("inferenceTypesSupported", []):
        reason = ("Select an inference profile for this model." if
                  "INFERENCE_PROFILE" in model.get("inferenceTypesSupported", [])
                  else "This model requires a provisioned inference endpoint.")
    return {
        "id": model["modelId"], "name": model.get("modelName", model["modelId"]),
        "provider": model.get("providerName", "Other"), "kind": "foundation",
        "input_modalities": model.get("inputModalities", []),
        "output_modalities": model.get("outputModalities", []),
        "streaming": bool(model.get("responseStreamingSupported")),
        "selectable": reason is None, "reason": reason,
        "lifecycle": model.get("modelLifecycle", {}).get("status"),
    }


class JudgeModelCatalogue:
    """Read all profile pages and expose only US geographic inference profiles."""

    def __init__(self, config, store):
        self.config, self.store = config, store
        self.fingerprint = catalogue_fingerprint(config)
        self.lock = asyncio.Lock()
        self.task = None

    def cached(self):
        try:
            data = self.store.get("system", "judge-model-catalogue")
            if data.get("fingerprint") == self.fingerprint:
                return data
        except KeyError:
            pass
        return {"id": "judge-model-catalogue", "fingerprint": self.fingerprint,
                "profile": self.config.judge_profile, "region": self.config.judge_region,
                "checked_at": 0, "models": [], "warnings": [], "stale": False}

    async def get(self, refresh=False):
        async with self.lock:
            cached = self.cached()
            if not refresh and time.time() - cached["checked_at"] < 600:
                return cached
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(asyncio.to_thread(self.discover, cached))
            task = self.task
        return await asyncio.shield(task)

    def discover(self, cached):
        import boto3
        from botocore.config import Config as BotoConfig

        warnings, entries, foundations = [], {}, {}

        def failed(source, exc, kind):
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
            warnings.append(f"{source} unavailable ({code}). Check the judge profile's Bedrock access and refresh.")
            for entry in cached["models"]:
                if entry["kind"] == kind:
                    entries[entry["id"]] = {**entry, "stale": True}

        try:
            client = boto3.Session(profile_name=self.config.judge_profile,
                                   region_name=self.config.judge_region).client(
                "bedrock", config=BotoConfig(connect_timeout=5, read_timeout=15,
                                            retries={"total_max_attempts": 2}))
        except Exception as exc:
            failed("Bedrock catalog", exc, "foundation")
            failed("Bedrock inference profiles", exc, "inference_profile")
        else:
            try:
                for model in client.list_foundation_models().get("modelSummaries", []):
                    foundations[model["modelId"]] = model
            except Exception as exc:
                failed("Bedrock foundation models", exc, "foundation")
            try:
                for page in client.get_paginator("list_inference_profiles").paginate():
                    for profile in page.get("inferenceProfileSummaries", []):
                        if not profile["inferenceProfileId"].startswith("us."):
                            continue
                        source_ids = sorted({m["modelArn"].split("foundation-model/", 1)[1]
                                             for m in profile.get("models", [])
                                             if "foundation-model/" in m.get("modelArn", "")})
                        source = foundations.get(source_ids[0]) if source_ids else None
                        if source is None and source_ids:
                            try:
                                source = client.get_foundation_model(
                                    modelIdentifier=source_ids[0])["modelDetails"]
                                foundations[source_ids[0]] = source
                            except Exception:
                                pass
                        entry = foundation_entry(source, profile=True) if source else {
                            "provider": "Other", "selectable": False, "streaming": False,
                            "reason": "Foundation model metadata is unavailable; refresh the catalog.",
                        }
                        if profile.get("status") != "ACTIVE":
                            entry.update(selectable=False, reason="This inference profile is not active.")
                        entry.update(id=profile["inferenceProfileId"],
                                     name=profile["inferenceProfileName"], kind="inference_profile",
                                     profile_type=profile.get("type"), source_model_ids=source_ids)
                        entries[entry["id"]] = entry
            except Exception as exc:
                failed("Bedrock inference profiles", exc, "inference_profile")
            finally:
                client.close()
        return self.store.put("system", {
            **cached, "checked_at": time.time(), "models": sorted(entries.values(),
                key=lambda m: (m["provider"].casefold(), m["id"])),
            "warnings": warnings, "stale": bool(warnings),
            "source": "Bedrock ListInferenceProfiles (us. profiles only)",
            "scope": "US geographic inference profiles available from the configured region. Invocation permissions and "
                     "Converse/tool support are enforced by Bedrock when scoring.",
        })

    async def close(self):
        if self.task and not self.task.done():
            await asyncio.gather(self.task, return_exceptions=True)
