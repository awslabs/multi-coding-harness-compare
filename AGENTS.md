# Instructions for AI coding assistants

Any AI coding assistant — Claude Code, Codex, Kiro, Cursor, Copilot, Gemini CLI, or any
other — can set up and deploy this repository. The harnesses **compared** inside the running
application are always Claude Code, Codex, and Kiro; the assistant that deploys the app
need not be one of them.

## Using another assistant

Point it at this file. CLAUDE.md and GEMINI.md both contain a one-line pointer
(`@AGENTS.md` / `Read AGENTS.md`) to this file; other assistants can be directed here in the
same way. No step below is specific to a particular tool.

## Setting up this repository on a workstation

The entrypoint is `./bootstrap.sh`; the full procedure is `docs/launch-runbook.md`. Follow it
literally. Every stage prints one JSON line, and the exit code says what to do next:

| Exit | Meaning | Do this |
|---|---|---|
| 0 | done | report the summary and any preflight `warnings`; with `--start`, give the whole `url` to the human as is — do not open it, never copy its token anywhere else (the line's `handoff`) |
| 2 | usage error: an unknown option, or a `--set` the inputs stage refused (unknown key, multi-line value, or an invalid value for a typed key such as a name with invalid characters) — see `error` in the JSON | correct your command from the `next`/`detail` text; re-run |
| 10 | uv missing | re-run with `--install-uv` |
| 11 | not macOS | stop and tell the human (see the runbook, Windows) |
| 12 | install failed | check network access to the package index, re-run; if it fails again, report the log lines |
| 12 | pytest missing from `.venv` (tests stage) | re-run: the install stage installs `requirements-dev.lock` |
| 13 | tests failed | report the failing tests in `detail` to the human and stop; do not modify the code or the machine |
| 20 | inputs missing | ask the human each question in `questions`, then re-run with `--set KEY=VALUE` per answer |
| 21 | preflight: assistant-fixable | apply each `fix` in `failed_checks`, re-run |
| 22 / 30 / 31 / 32 / 33 | inputs (no report) or setup / deploy / probe / doctor / start failed | read `detail`, the named report under `.bootstrap/` and the log; fix, re-run |
| 40 | human action needed — includes a `configuration-loads` failure when a typed key (e.g., `AGENTCORE_RUNTIME_NAME_PREFIX`) already in `deploy.properties` has an invalid value | relay the `fix` of each `failed_checks` entry whose `gate` is `human` verbatim, including the suggested corrected value; apply those whose `gate` is `assistant`; wait for the human; re-run |
| 41 | AgentCore runtimes not deployed, or the cached ones no longer match the configuration (`agentcore-drift`) | ask the human to authorize creating or refreshing an IAM role, S3 objects and three runtimes; only then re-run with `--deploy` |
| 42 | this shell is sandboxed | give the human the printed `start` command to run in Terminal.app |

Rules:

- An existing `deploy.properties` that is already filled in is used as-is; bootstrap asks only for values still marked as placeholders.
- Collect the answers first. Run `./bootstrap.sh` once; exit 20 lists every question. Do not guess
  an answer, and do not substitute a different AWS profile, bucket or region for the one given.
- **Ask about `AUTO_APPROVE` with those answers**, even though exit 20 does not list it (it has a
  default, `false`, rather than a placeholder). Recommend `true`, tell the human what `true` and
  `false` each do (`docs/launch-runbook.md`, Inputs), and record their choice with
  `--set AUTO_APPROVE=true` or `--set AUTO_APPROVE=false`. Never choose it for them.
- **`deploy.properties` is the only source for environment-specific values.** Two equivalent ways to set one:
  - Edit the file directly: fill a key's line (or uncomment and fill a commented line) in `deploy.properties`.
  - Run `./bootstrap.sh --set KEY=VALUE` (repeatable, one flag per key): validates the value first and refuses unknown keys, multi-line values, and invalid values for typed keys with exit 2, writing nothing; accepts it and writes it into `deploy.properties` otherwise.
  Both ways are identical after the fact; the file line and `--set` produce the same result. Do not write environment values into `config.toml`. Never put credentials in either file.
- **Invalid value with `--set` (exit 2)**: the `error` field in the JSON line contains the validation message and a suggested corrected value. Because naming choices belong to the human, relay the error and the suggestion, wait for the human to confirm a value, then record it with `--set` and re-run.
- **Invalid value already in `deploy.properties` (exit 40)**: preflight's `configuration-loads` check catches it with `gate: human`. The `fix` in `failed_checks` has the error message and a suggested corrected value. Relay it to the human exactly, wait for confirmation, then record the value with `./bootstrap.sh --set KEY=VALUE` and re-run.
- Never pass `--deploy` without the human's explicit authorization in this conversation. It creates
  AWS resources.
- Never pass `cleanup --approve` (to `deploy/agentcore-cleanup.sh` or `python -m harness_image cleanup`) without the human's explicit authorization in this conversation. It permanently deletes AWS resources. Relay the cleanup plan to the human and wait for consent before supplying the token.
- Re-run with the same options each time; `--set` answers persist and need not be repeated.
- Only `fail` checks need action. A preflight `warn` (under `warnings`) never blocks: report it to
  the human; act on it only if asked.
- `kiro-cli login` is a browser device flow only a person can complete.
- To apply a changed value: `.venv/bin/multi-coding-harness-compare stop`, then
  `./bootstrap.sh --set KEY=VALUE --start`. `stop` refuses while runs are in progress; pass `--force`
  only with the human's agreement.
- Never commit `deploy.properties`, `config.toml`, `.arena/` or `.bootstrap/`.

### Naming rules for typed keys

These keys are validated on `--set` (exit 2) and when the configuration is loaded by preflight (exit 40 via `configuration-loads`):

| Key | Required pattern | Max length | Valid example | Common mistake |
|---|---|---|---|---|
| `AGENTCORE_RUNTIME_NAME_PREFIX` | `[A-Za-z][A-Za-z0-9_]*` — start with a letter; letters, digits, underscores only | 41 (so `<prefix>_claude` fits in 48) | `testoct21_mchc` | `testoct21-mchc` (hyphen not allowed) |
| `AGENTCORE_EXECUTION_ROLE_NAME` | `[\w+=,.@-]{1,64}` — letters, digits, `+=,.@-` | 64 | `MyDeploymentRole` | — |
| `AGENTCORE_INLINE_POLICY_NAME` | `[\w+=,.@-]{1,128}` — letters, digits, `+=,.@-` | 128 | `MyDeploymentPolicy` | — |
| `USER_NAMESPACE` | `[a-z0-9][a-z0-9-]*` — lowercase letters, digits, hyphens; start with letter or digit | — | `alice` | uppercase letters or underscores |

### Worked example: a separate deployment beside a running one in the same account

Change `SERVICE_PORT`, `AGENTCORE_RUNTIME_NAME_PREFIX`, the role and policy names, and both S3 prefixes so the new deployment does not collide with the existing one. Use single quotes around values that contain `${S3_BUCKET}` so the shell does not expand them.

As a single `./bootstrap.sh` call:

```bash
./bootstrap.sh \
  --set SERVICE_PORT=8766 \
  --set AGENTCORE_RUNTIME_NAME_PREFIX=testoct21_mchc \
  --set AGENTCORE_EXECUTION_ROLE_NAME=TestOct21BedrockAgentCoreRole \
  --set AGENTCORE_INLINE_POLICY_NAME=TestOct21AgentCoreRuntime \
  --set ARTIFACT_S3_URI='s3://${S3_BUCKET}/output/testoct21/' \
  --set AGENTCORE_TRANSFER_S3_URI='s3://${S3_BUCKET}/code/testoct21-runtime/'
```

Equivalent lines in `deploy.properties` (for people who prefer editing the file):

```
SERVICE_PORT = 8766
AGENTCORE_RUNTIME_NAME_PREFIX = testoct21_mchc
AGENTCORE_EXECUTION_ROLE_NAME = TestOct21BedrockAgentCoreRole
AGENTCORE_INLINE_POLICY_NAME = TestOct21AgentCoreRuntime
ARTIFACT_S3_URI = s3://${S3_BUCKET}/output/testoct21/
AGENTCORE_TRANSFER_S3_URI = s3://${S3_BUCKET}/code/testoct21-runtime/
```

After recording these values, continue from Step 3 of `docs/launch-runbook.md`.

## Working on the code

- Tests: `.venv/bin/python -m pytest tests -q` (pytest comes from `requirements-dev.lock`); UI tests:
  `for f in tests/*.cjs; do node "$f"; done`.
- New configuration that differs between users, machines or accounts belongs in
  `arena/properties.py` (`KEYS`) and `deploy.properties.example`, not as a literal in code.
