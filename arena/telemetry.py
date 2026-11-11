# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import hashlib
import json


def usage_summary(records: list[dict], harness: str | None = None) -> dict:
    """Keep per-scope high-water marks; never add cumulative and delta views."""
    cumulative, deltas, raw, seen = {}, [], [], set()
    for item in records:
        fingerprint = item.get("event_id") or hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        raw.append(item)
        if not item.get("raw"):
            continue
        if item["mode"] == "cumulative":
            key = (item["source"], item["scope"])
            old = cumulative.get(key)
            # Reconnection can replay older usage; retain the larger native total.
            if not old or item["raw"].get("totalTokens", 0) >= old["raw"].get("totalTokens", 0):
                cumulative[key] = item
        elif item["mode"] == "delta":
            deltas.append(item)
    # Only the primary coding session is included for Codex. Child events are
    # retained but not added because parent/child inclusion isn't guaranteed.
    main_scopes = list(cumulative.values())
    totals = dict.fromkeys(("input", "output", "cache_read", "cache_write", "reasoning", "total"))
    measured = False

    def add(field, value):
        if isinstance(value, (int, float)):
            totals[field] = (totals[field] or 0) + value

    for item in main_scopes + deltas:
        r = item["raw"]
        if item["source"].startswith("codex"):
            if not r.get("totalTokens"):
                continue
            for key, native in (("input", "inputTokens"), ("output", "outputTokens"),
                ("cache_read", "cachedInputTokens"), ("cache_write", "cacheWriteInputTokens"),
                ("reasoning", "reasoningOutputTokens"), ("total", "totalTokens")):
                add(key, r.get(native))
            measured = True
        elif item["source"].startswith("claude"):
            whole_tree = item["source"].endswith(".model_usage")
            buckets = r.values() if whole_tree else [r]
            keys = (("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
                    if whole_tree else ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                        "cache_creation_input_tokens"))
            for bucket in buckets:
                values = [bucket.get(k) for k in keys]
                if not any(isinstance(v, (int, float)) and v > 0 for v in values):
                    continue
                for key, value in zip(("input", "output", "cache_read", "cache_write"), values):
                    add(key, value)
                # The four disjoint fields are required to calculate a full total.
                if all(isinstance(v, (int, float)) for v in values):
                    add("total", sum(values))
                measured = True
    # Kiro's terminal metadata contains a list of credit charges for that
    # user turn. Keep the last snapshot per turn; never count replays twice.
    credit_turns, context_percent = {}, None
    for item in raw:
        if item["source"] != "kiro.ACP._kiro.dev/metadata":
            continue
        metadata = item.get("raw", {})
        value = metadata.get("contextUsagePercentage")
        if isinstance(value, (int, float)):
            context_percent = value
        charges = metadata.get("meteringUsage")
        if item.get("turn_key") and isinstance(charges, list):
            values = [c["value"] for c in charges if c.get("unit") == "credit"
                      and isinstance(c.get("value"), (int, float))]
            if values:
                credit_turns[(item.get("scope"), item["turn_key"])] = sum(values)
    reason = None if measured else (
        "Token counts were not provided for this Kiro run. Credits and context occupancy are separate measurements."
        if harness == "kiro" else "No measured native token totals were recorded for this run.")
    exclusions = {x for r in raw for x in r.get("exclusions", [])}
    if reason:
        exclusions.add(reason)
    return {
        **{k: v if measured else None for k, v in totals.items()},
        "completeness": "partial" if measured else "unavailable",
        "source": sorted({r["source"] for r in raw}),
        "unavailable_reason": reason,
        "other_metrics": {
            "credits": sum(credit_turns.values()) if credit_turns else None,
            "credit_turns": len(credit_turns),
            "credit_coverage": "partial" if credit_turns else "unavailable",
            "context_usage_percent": context_percent,
            "note": "Native credits are summed once per recorded turn. Context percentage is the latest occupancy, not consumed tokens.",
        } if harness == "kiro" else None,
        "included": "Native measured clarification, coding, debugging, validation and resumed-turn aggregates.",
        "exclusions": sorted(exclusions),
        "normalization": "Codex total is native (cache/reasoning not added). Claude whole-turn model_usage is preferred, with top-level usage as fallback; input/output and disjoint cache buckets are summed once. Unreported fields remain null.",
        "raw_records": raw,
    }
