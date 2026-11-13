# Changelog

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

5. **AgentCore: redeploy the runtimes before the next run** with `./bootstrap.sh --deploy`. Runtimes deployed by an earlier version send `__codecouncil` control messages, which this version ignores, so each run's result and permission requests would be lost. Bootstrap's drift check does not detect this.
   - **Keeping your AWS names:** a `deploy.properties` that sets the old runtime prefix, role, policy and S3 prefix, and `AGENTCORE_APPLICATION_TAG = CodeCouncil` (as the earlier `deploy.properties.example` did), keeps using the existing role and runtimes; redeploying refreshes them.
   - **Moving to the new defaults:** deploy creates a new role and new runtimes and leaves the old ones in place. Re-apply the deployer and workstation IAM policies from `docs/iam/`, which now cover `runtime/mchc*`. Remove the old resources with `deploy/agentcore-cleanup.sh` while `deploy.properties` still names them and their tag. Comparisons archived under `output/codecouncil/` stay there unless you copy them to the new location.
