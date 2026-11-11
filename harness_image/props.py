# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Minimal stdlib-only deploy.properties parser for harness_image.

Reads KEY = VALUE lines; '#' starts a comment; resolves ${KEY} references.
This module is intentionally stdlib-only and imports nothing from arena so
the harness_image package can run without the full UI/worker dependencies.

arena/properties.py may import parse() from here to avoid duplication.
"""
import re
from pathlib import Path

_KEY_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_REFERENCE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")

_DEFAULT_FILE = "deploy.properties"


class PropertiesError(ValueError):
    """Raised for malformed deploy.properties content."""


def parse(text: str, origin: str = _DEFAULT_FILE) -> dict:
    """Parse KEY = VALUE lines; '#' comments; resolve ${KEY} references.

    Values may be optionally quoted with single or double quotes.
    Circular and missing references raise PropertiesError.
    This function is intentionally minimal: it does not reject credential-looking
    keys (that check lives in the full arena parser).
    """
    raw: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, value = stripped.partition("=")
        name, value = name.strip(), value.strip()
        if not sep:
            raise PropertiesError(
                f"{origin}:{number}: expected KEY = VALUE, got {stripped!r}")
        if not _KEY_NAME.match(name):
            raise PropertiesError(
                f"{origin}:{number}: key {name!r} must be UPPER_SNAKE_CASE")
        if name in raw:
            raise PropertiesError(f"{origin}:{number}: {name} is defined twice")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        raw[name] = value
    return _resolve(raw, origin)


def _resolve(raw: dict, origin: str) -> dict:
    resolved: dict[str, str] = {}

    def expand(name: str, chain: list) -> str:
        if name in resolved:
            return resolved[name]
        if name in chain:
            raise PropertiesError(
                f"{origin}: circular reference {' -> '.join([*chain, name])}")
        if name not in raw:
            raise PropertiesError(
                f"{origin}: {chain[-1]} references ${{{name}}}, which is not defined")
        text = _REFERENCE.sub(lambda m: expand(m.group(1), [*chain, name]), raw[name])
        resolved[name] = text
        return text

    for name in raw:
        expand(name, [])
    return resolved


def load(path) -> dict:
    """Load a deploy.properties file; return {} when absent or path is None."""
    if path is None:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    return parse(p.read_text(encoding="utf-8"), str(p))
