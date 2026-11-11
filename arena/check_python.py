# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Private Python and wheel preparation for credential-free validation."""
import ast
import hashlib
import importlib.metadata
import os
import re
import shlex
import sys
import sysconfig
from pathlib import Path

from packaging.requirements import Requirement

from .isolation import command


RUNNER_PACKAGES = {"pytest": "pytest==8.4.1", "pytest_asyncio": "pytest-asyncio",
                   "coverage": "coverage", "ruff": "ruff", "mypy": "mypy", "build": "build"}


def _without_redirections(tokens):
    """Redirection descriptors and destinations are not pip package arguments."""
    redirects = {"<", ">", ">>", "<<", "<<<", "<>", "<&", ">&", "&>", "&>>", ">|"}
    result = []
    index = 0
    while index < len(tokens):
        if (tokens[index].isdigit() and index + 1 < len(tokens)
                and tokens[index + 1] in redirects):
            index += 1
        if tokens[index] in redirects:
            index += 2  # Operator and its file/descriptor destination.
        else:
            result.append(tokens[index])
            index += 1
    return result


def plan_python_dependencies(checks, acceptance_files, declared=()):
    """Discover evaluator dependencies without executing shell text or test code."""
    tools = {}
    project = set()
    for value in declared:
        requirement = Requirement(value)
        if requirement.url:
            raise ValueError("Evaluation Python requirements must use package names, not direct URLs")
        if requirement.marker and not requirement.marker.evaluate():
            continue
        tools[requirement.name.lower().replace("_", "-")] = str(requirement)

    def require_tool(module):
        if module in RUNNER_PACKAGES:
            requirement = Requirement(RUNNER_PACKAGES[module])
            tools.setdefault(requirement.name.lower().replace("_", "-"), str(requirement))

    for source in acceptance_files.values():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    require_tool(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                require_tool((node.module or "").split(".")[0])

    for check in checks:
        lexer = shlex.shlex(check.command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        try:
            tokens = _without_redirections(list(lexer))
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            name = Path(token).name
            # Only command names or `python -m MODULE`, never text inside `-c`.
            start = index == 0 or tokens[index-1] in ("&&", "||", ";", "|", "&") or (
                "=" in tokens[index-1] and not any(c.isspace() for c in tokens[index-1]))
            if start:
                require_tool(name)
            module = None
            if start and re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", name):
                rest = tokens[index+1:]
                if "-m" in rest[:4]:
                    offset = rest.index("-m")
                    module = rest[offset+1] if len(rest) > offset+1 else None
                    require_tool(module)
            install_index = index+1 if start and re.fullmatch(r"pip(?:\d+(?:\.\d+)?)?", name) else None
            if module == "pip":
                install_index = tokens.index("-m", index+1) + 2
            if install_index is not None and tokens[install_index:install_index+1] == ["install"]:
                for argument in tokens[install_index+1:]:
                    if argument in ("&&", "||", ";", "|", "&"):
                        break
                    if argument.startswith("-") or "/" in argument or argument.endswith(".txt"):
                        continue
                    try:
                        requirement = Requirement(argument)
                    except ValueError:
                        continue
                    if not requirement.url:
                        project.add(str(requirement))
    return {"tools": sorted(tools.values()), "project": sorted(project)}


async def prepare_check_python(root: Path, snapshot: Path, seconds: int, *, dependencies=None) -> dict:
    home = root / "home"
    home.mkdir(exist_ok=True)
    environment = home / ".verification-python"
    wheels = root / "wheels"
    wheels.mkdir(exist_ok=True)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
           "LANG": "en_US.UTF-8", "PYTHONNOUSERSITE": "1",
           "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    code, out, err = await command(sys.executable, "-I", "-m", "venv",
        "--system-site-packages", environment, timeout=seconds, env=env)
    if code:
        raise RuntimeError("Evaluator Python setup failed: " + out + err)
    # A venv made from another venv inherits the *base interpreter's* packages.
    # Explicitly expose the application's pinned packages after the private
    # site-packages so candidate-specific wheels can override their versions.
    site = environment / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    inherited = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    (site / "codecouncil-runtime.pth").write_text("\n".join(inherited) + "\n")
    python = environment / "bin/python"
    required, unsupported = set(), []
    files = sorted(snapshot.rglob("requirements*.txt"))
    dependencies = dependencies or {"tools": [], "project": []}
    # Tools must have their own console scripts and dependencies, even when a
    # same-version module happens to be available from the read-only app venv.
    required.update(dependencies["tools"])
    entries = [(str(path.relative_to(snapshot)), line) for path in files
               for line in path.read_text().splitlines()]
    entries += [("validation plan", value) for value in dependencies["tools"] + dependencies["project"]]
    for origin, line in entries:
        value = line.split(" #", 1)[0].strip()
        if not value or value.startswith("#"):
            continue
        try:
            requirement = Requirement(value)
            if requirement.url:
                raise ValueError("direct package URLs need a separately prepared runtime")
            if requirement.marker and not requirement.marker.evaluate():
                continue
            installed = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            installed = None
        except ValueError:
            unsupported.append(origin + ": " + value)
            continue
        if installed is None or installed not in requirement.specifier or requirement.extras:
            required.add(str(requirement))
    preparation = {"python": str(python), "pip": "bundled ensurepip",
        "system_packages": "read-only inherited packages; installs target this private venv",
        "inherited_package_paths": inherited,
        "requirement_files": [str(p.relative_to(snapshot)) for p in files],
        "plan_dependencies": dependencies,
        "unsupported_requirements": unsupported, "wheel_download": None}
    if required:
        # Download binary distributions only: preparation never executes a
        # candidate build script or accepts requirements-file pip options/URLs.
        code, out, err = await command(python, "-I", "-m", "pip", "download",
            "--only-binary=:all:", "--index-url", "https://pypi.org/simple",
            "--dest", wheels, *sorted(required), timeout=seconds, env=env)
        preparation["wheel_download"] = {"requirements": sorted(required), "exit_code": code,
                                       "log": out + err}
    preparation["wheels"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in wheels.glob("*.whl")}
    return preparation
