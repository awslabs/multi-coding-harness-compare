# Changelog

## v1.2.0 — 2026-11-13

For full details see [docs/releases/v1.2.0.md](docs/releases/v1.2.0.md).

### New flow: harness-written tests

Each harness now writes its own test code after the specification is approved, instead of the test agent generating a shared executable test plan. Supplied validation plans, legacy comparisons (frozen shared plan), and no-planner comparisons are unchanged.

**Flow:**
1. Test agent proposes a plain-language specification — unchanged.
2. Human approves — unchanged.
3. Each harness enters a **test stage**: writes test code for every approved scenario, records `.mchc-tests.json`, and runs the tests in a resumed session against its saved implementation. When a test fails, the harness must fix the **implementation**, not the tests. Up to `test_fix_rounds = 2` implementation-fix turns follow the initial write turn. `test_active_seconds = 3600` is the harness's own active time budget; it does **not** include the test agent review, the app's sandbox verification runs, or waiting.
4. When the test stage stops early (budget exhausted, rounds exhausted, harness failed, manifest missing or invalid), a structured `stop_reason` with a human-readable message is stored and shown in the UI as the failure reason on the harness panel. If no manifest was ever accepted, Accuracy and Tests are 0.
5. The test agent reviews each harness's tests and the app's verified results, rates every scenario (1 / 0.5 / 0), and those ratings drive the Accuracy and Tests scores. A review failure makes Accuracy and Tests incomplete (`evaluated_incomplete`), never 0.

**Configuration** (new keys in `config.example.toml`, frozen into the comparison's `limits` at creation):
- `test_active_seconds = 3600` — harness's own active time across all test-stage turns; excludes test agent review, sandbox runs, and waiting.
- `test_fix_rounds = 2` — app-driven follow-up turns (repair or fix) after the write turn.

**Scoring change:** Accuracy criterion met only when checks pass and scenario rating is 1. Tests group scores use per-check rating multipliers. Formula unchanged: `0.45 × accuracy + 0.20 × relevance + 0.35 × tests`. Evaluator version 10, rubric version `harness-tests-rubric-1` for new-flow plans; legacy comparisons keep their existing evaluator and rubric versions. When no manifest was ever accepted, the overall score is numeric (relevance counted as 0 when the judge is unavailable) and the run status is a failed evaluation, not incomplete.

**Automatic locks and flags:** At lock time the app also locks every `conftest.py` at any depth, `pytest.ini`, and `tox.ini`, even when not listed in the manifest's `test_files` (a validation warning is emitted in that case). After each fix turn, any newly created `conftest.py`, `pytest.ini`, `tox.ini`, `sitecustomize.py`, `usercustomize.py`, or `*.pth` files are deleted and flagged as `created` violations. Changes to the test-configuration sections of `pyproject.toml` (`[tool.pytest.ini_options]`, `[tool.coverage.*]`) or `setup.cfg` (`[tool:pytest]`, `[coverage:*]`) are flagged as `test_config_changed` violations without restoring those files (implementation settings in those files remain editable).

**Manifest limits:** The `.mchc-tests.json` manifest is capped at 400 checks in total, 10 checks per `scenario_id`, and a `notes` field of at most 2000 bytes UTF-8. Exceeding any limit is a validation error that triggers a repair turn.

**Command confinement (stricter):** A check command that invokes a workspace file not listed in `test_files` is a validation error. This covers path-prefixed arguments (relative paths with separators, `./x`, `/workspace/x`), bare-name interpreter invocations (`python main.py`, `python3 main.py`, `bash run.sh`, `sh run.sh`, `node x.js`) where the named file exists in the workspace, and `python -m module` / `python3 -m module` where the module resolves to a workspace file or package (a `.py` file or a directory with `__main__.py`). Known test runners (pytest, unittest, nose2, doctest, coverage running a runner) are always allowed. Build tools (make, npm/pnpm/yarn run or test, tox, nox) are allowed only when their defining file (Makefile, package.json, tox.ini, noxfile.py) is listed in `test_files`; otherwise a validation error names the file.

**Test agent input (implementation diff):** The test agent review now also receives a unified diff of non-locked (implementation) files between the implementation snapshot and the final test snapshot. The agent records a concern when implementation changes appear to special-case the specification's test data rather than solving the task generally.

**Infrastructure errors after the lock (§14):** An infrastructure failure during a fix turn after the lock exists scores the last completed round with rated scores (not `evaluated_incomplete`); the stop reason code is `infrastructure_error` to identify the honest cause. The `evaluated_incomplete` outcome applies when no round has been verified — this includes both the case where the manifest was never accepted and the case where the lock was just created but round-0 sandbox verification crashed before any round completed. "The evaluation is incomplete" is never shown next to rated scores.

### Upgrade notes

- **AgentCore: no redeploy needed.** `arena/*.py` — including the new test-stage phases — ships with every turn's job tarball and reaches already-deployed runtimes automatically. A redeploy would only be needed if deploy-time runtime settings (environment variables, bundled Python dependencies, or harness CLI versions) changed; none did in this release.
- Run `./bootstrap.sh` to install the updated code.
- Comparisons approved but without a frozen shared plan before this upgrade automatically continue with the new flow.
- Corrected the v1.1.0 AgentCore upgrade note: only harnesses with staged tools (Kiro) need the redeploy; control messages were never affected.

## v1.1.0 — 2026-11-12

CodeCouncil is now **Multi Coding-Harness Compare**, with the short name **mchc**. This is a breaking change: nothing in this version reads the old names. Before using it with an existing installation, follow [Upgrading from an earlier version](#upgrading-from-an-earlier-version).

### Changed

| | Before 1.1.0 | 1.1.0 |
|---|---|---|
| Product name | CodeCouncil | Multi Coding-Harness Compare |
| Short command and module | `codecouncil`, `python -m codecouncil` | `mchc`, `python -m mchc` |
| Environment variables | `CODECOUNCIL_*` | `MCHC_*` with the same suffix, for example `MCHC_CONFIG`, `MCHC_PROPERTIES`, `MCHC_CODEX_PROVIDER`, `MCHC_CLAUDE_CLI` |
| Project output folders | `.codecouncil-output.json` marker, `.codecouncil/` metadata folder | `.mchc-output.json` marker (owner `Multi Coding-Harness Compare`), `.mchc/` metadata folder |
| AgentCore runtime protocol and paths | `__codecouncil` control messages, `codecouncil-` session IDs, `/var/tmp/codecouncil`, `codecouncil-cache` | `__mchc`, `mchc-`, `/var/tmp/mchc`, `mchc-cache` |
| Acceptance-test helper | `_codecouncil_observe.py` | `_mchc_observe.py` |
| Default runtime name prefix | `codecouncil` | `mchc` (`mchc_claude`, `mchc_codex`, `mchc_kiro`) |
| Default IAM role and inline policy | `CodeCouncilBedrockAgentCoreRuntimeRole`, `CodeCouncilAgentCoreRuntime` | `MchcBedrockAgentCoreRuntimeRole`, `MchcAgentCoreRuntime` |
| Default artifact location | `s3://${S3_BUCKET}/output/codecouncil/` | `s3://${S3_BUCKET}/output/mchc/` |
| Default `Application` tag | `CodeCouncil` | `Multi Coding-Harness Compare` |
| Version | 0.1.0 | 1.1.0 |

Unchanged: the `multi-coding-harness-compare` and `arena` commands, the `.arena` data directory, the `arena_session` cookie and `ARENA_CONFIG`. Existing comparison history and browser sessions carry over.

### Added

- The Apache-2.0 copyright and `SPDX-License-Identifier` header on the five Python files that lacked it. Every Python file in the repository now carries it.

### Upgrading from an earlier version

1. **Stop the services with the old code.** Let running comparisons finish, then run `multi-coding-harness-compare stop` before you update: this version's `stop` does not recognize services started as `python -m codecouncil`.
2. **Update and reinstall.** Pull, then re-run `./bootstrap.sh`. Its install stage adds the `mchc` command and removes `codecouncil`.
3. **Rename environment variables** in shell profiles and scripts from `CODECOUNCIL_*` to `MCHC_*`, and replace `python -m codecouncil` with `python -m mchc`.
4. **Migrate project output folders.** This version does not recognize output folders written by earlier versions: it refuses to save into them, shows earlier runs' outputs as superseded, and no longer leaves them out when it copies a project for a harness. Run this from each repository root:

   ```bash
   find . -name .codecouncil-output.json -execdir sh -c 'sed "s/\"CodeCouncil\"/\"Multi Coding-Harness Compare\"/" .codecouncil-output.json > .mchc-output.json && rm .codecouncil-output.json' \;
   find . -type d -name .codecouncil -prune -execdir mv .codecouncil .mchc \;
   ```

5. **AgentCore: redeploy the runtimes before Kiro runs there again** with `./bootstrap.sh --deploy`. Each runtime records where its separately staged harness tools live in an environment variable set at deploy time, and runtimes deployed by an earlier version use the old variable name (`CODECOUNCIL_TOOLS_URI`), so this version cannot find the CLI of a harness whose tools are staged — Kiro at the pinned versions. Claude Code and Codex keep working without a redeploy: the agent code that runs inside the runtime ships with every run. Bootstrap's drift check does not detect this.
   - **Keeping your AWS names:** a `deploy.properties` that sets the old runtime prefix, role, policy and S3 prefix, and `AGENTCORE_APPLICATION_TAG = CodeCouncil` (as the earlier `deploy.properties.example` did), keeps using the existing role and runtimes; redeploying refreshes them.
   - **Moving to the new defaults:** deploy creates a new role and new runtimes and leaves the old ones in place. Re-apply the deployer and workstation IAM policies from `docs/iam/`, which now cover `runtime/mchc*`. Remove the old resources with `deploy/agentcore-cleanup.sh` while `deploy.properties` still names them and their tag. Comparisons archived under `output/codecouncil/` stay there unless you copy them to the new location.
