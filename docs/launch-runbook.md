# Launch runbook: multi-coding-harness-compare on a user workstation

How to take a fresh workstation to a working installation. **Written to be executed by an AI coding
assistant** (Claude Code, Codex, Kiro, Cursor, Copilot, Gemini CLI, or any other) and equally
usable by a person. Follow it literally; every command is non-interactive, prints JSON, and signals
its outcome through an exit code. The single entrypoint is `./bootstrap.sh`.

**Supported host: macOS**, Apple silicon or Intel, including an EC2 Mac instance (`mac2.metal`), which
is treated exactly like a local workstation. Windows is not supported; see [Windows](#windows).

**Recommended: `RUNTIME = agentcore`.** Coding harnesses run in Bedrock AgentCore Firecracker
microVMs, where Claude Code and Codex authenticate through the runtime's execution role — no
credential is copied from the workstation and no API key is forwarded. Independent evaluation still
runs locally in the macOS Seatbelt sandbox. `RUNTIME = native` runs everything locally.

---

## Rules for the assistant

1. **Capture every input before anything else** (next section). Ask the human all the questions in
   one message. Never guess an answer, and never substitute a different AWS profile, bucket or
   region for the one given.
2. **`deploy.properties` is the only source for environment-specific values.** Two equivalent ways to set one:
   - Edit the file directly: fill a key's line (or uncomment and fill a commented line) in `deploy.properties`.
   - Run `./bootstrap.sh --set KEY=VALUE` (repeatable, one flag per key): validates the value first and refuses unknown keys, multi-line values, and invalid values for typed keys with exit 2, writing nothing; accepts it and writes it into `deploy.properties` otherwise. Do not edit `config.toml` for these values, and never put credentials in either file.
3. **If `--set` is refused with exit 2**, the `error` field in the JSON line contains the validation message and a suggested corrected value. Names are the human's choice: relay the error and the suggestion, wait for the human to confirm a value, then record it with `--set` and re-run.
4. **If preflight fails at `configuration-loads` (exit 40, `gate: human`)**, a typed key (e.g., `AGENTCORE_RUNTIME_NAME_PREFIX`) already in `deploy.properties` has an invalid value. The `fix` in `failed_checks` has the message and a suggested corrected value. Relay it to the human exactly, wait for confirmation, then record the corrected value with `./bootstrap.sh --set KEY=VALUE` and re-run.
5. **The exit code decides the next action** — see [Exit codes](#exit-codes). Exit 20, 40, 41 and 42
   are designed stops that need the human; they are not errors to work around. Re-run with the same
   options each time; `--set` answers persist in `deploy.properties` and need not be repeated.
6. **Only `fail` checks need action.** A `warn` (listed under `warnings` on the preflight line) never
   blocks: include it in your report to the human, and act on it only if the human asks.
7. **Never pass `--deploy` without the human's explicit authorization** in this conversation. It
   creates an IAM role, S3 objects and three AgentCore runtimes.
8. **Never pass `--interactive`** to `setup`; flags and `deploy.properties` are the interface.
9. **Relay a human action verbatim** — the `fix` of each entry in `failed_checks` whose `gate` is
   `human` — and wait for confirmation before re-running.
10. **Report, do not repair, a failed test suite** (exit 13): do not change the code or the machine to
    make a test pass. Tests that cannot run in a sandboxed shell skip themselves.

## Inputs — capture these first

Ask the human these questions together, before running anything that depends on them. Each answer
becomes one key in `deploy.properties`. Commands in the last column only *discover candidates* —
the human still chooses; they use the virtualenv Step 1 creates, so no AWS CLI is needed.

| Key | Question for the human | Discover candidates with |
|---|---|---|
| `RUNTIME` | Where should the coding harnesses run: `agentcore` (Bedrock AgentCore microVMs; recommended) or `native` (this Mac's Seatbelt sandbox)? | — |
| `KIRO_AUTH` | Should Kiro use this workstation's existing Kiro login (`copy-host-login`) or a login made inside a dedicated HOME (`login-in-home`)? | `test -f "$HOME/Library/Application Support/kiro-cli/data.sqlite3" && echo host-login-present` |
| `AWS_PROFILE` | Which AWS profile reaches the account holding the S3 bucket, the Bedrock models and the AgentCore runtimes? | `.venv/bin/python -c 'import boto3; print(boto3.Session().available_profiles)'` |
| `AWS_REGION` | Which AWS region holds the bucket, the Bedrock models and the AgentCore runtimes (for example `us-east-1`)? | `.venv/bin/python -c 'import boto3,sys; print(boto3.Session(profile_name=sys.argv[1]).region_name)' NAME` (the profile's default region) |
| `S3_BUCKET` | Which S3 bucket, owned by that account, stores comparison artifacts and AgentCore transfers? | `.venv/bin/python -c 'import boto3,sys; print([b["Name"] for b in boto3.Session(profile_name=sys.argv[1]).client("s3").list_buckets()["Buckets"]])' NAME` |
| `REPOSITORY_ROOTS` | Which directory (comma-separated for several) holds the projects comparisons may run against? | — |
| `AUTO_APPROVE` | Should the coding harnesses' own permission prompts be answered automatically inside each run's sandbox (`true`, recommended: Codex's auto-review agent answers Codex's, Kiro's are allowed, and Claude Code's auto mode still sends you what it escalates) or all come to you as approval cards (`false`, the default)? | — |

**Ask `AUTO_APPROVE` even though bootstrap does not stop for it**: it has a default, `false`, rather
than a placeholder. Recommend `true`, and tell the human what each value does before they answer:

- `true`: the harnesses' own permission prompts are answered automatically, inside each run's
  sandbox. Claude Code runs in auto mode: its classifier allows routine actions and blocks risky
  ones, and what it escalates still comes to the human as an approval card (where auto mode is
  unavailable for the model, its prompts are allowed). Codex's auto-review agent answers Codex's
  approval requests. Kiro's permission prompts are allowed.
- `false`: every permission prompt comes to the human as an approval card, and the run waits for
  the answer: Claude Code's escalations (all of its prompts where auto mode is unavailable), every
  Codex approval request and every Kiro permission prompt. A run can raise many cards.

Either way, clarification questions and the shared test review still reach the human. Record the
answer with `--set AUTO_APPROVE=true` or `--set AUTO_APPROVE=false`; the README's
[Permission prompts](../README.md#permission-prompts-auto_approve) section has the details.

Optional keys keep sensible defaults; change them only when the human says so, or when a failed
check's `fix` names the key (a busy `SERVICE_PORT`, `CODEX_HOME_TEMPLATE`): `JUDGE_MODEL` and
`PLANNING_MODEL` (Bedrock inference profiles enabled in `AWS_REGION`), `CLAUDE_MODEL`,
`CODEX_MODEL`, `KIRO_MODEL`, `SERVICE_PORT` (8765), `DATA_DIR` (`.arena`), `AWS_ACCOUNT_ID`
(enables drift checks), the `AGENTCORE_*` names (keep them identical on every workstation sharing
an account), and `*_CLI_PATH` / `*_HOME_TEMPLATE`. `deploy.properties.example` documents every key,
what it fills, and what changing it later requires.

### Naming rules for typed keys

Some keys are validated before they are used. A value that violates the rule is refused at `--set`
time (exit 2, `error` field) or caught by preflight's `configuration-loads` check when the file is
already written (exit 40, `gate: human`). Either way the JSON `fix` contains a suggested corrected
value; relay it to the human (names are their choice), wait for confirmation, then record the
confirmed value with `--set` and re-run.

| Key | Required pattern | Max length | Valid example | Common mistake |
|---|---|---|---|---|
| `AGENTCORE_RUNTIME_NAME_PREFIX` | `[A-Za-z][A-Za-z0-9_]*` — start with a letter; letters, digits, underscores only | 41 (so `<prefix>_claude` fits within 48) | `testoct21_mchc` | `testoct21-mchc` (hyphen not allowed) |
| `AGENTCORE_EXECUTION_ROLE_NAME` | `[\w+=,.@-]{1,64}` — letters, digits, `+=,.@-` | 64 | `MyDeploymentRole` | — |
| `AGENTCORE_INLINE_POLICY_NAME` | `[\w+=,.@-]{1,128}` — letters, digits, `+=,.@-` | 128 | `MyDeploymentPolicy` | — |
| `USER_NAMESPACE` | `[a-z0-9][a-z0-9-]*` — lowercase letters, digits, hyphens; start with letter or digit | — | `alice` | uppercase letters or underscores |

### Worked example: a separate deployment beside a running one in the same account

Change `SERVICE_PORT`, `AGENTCORE_RUNTIME_NAME_PREFIX`, the role and policy names, and both S3
prefixes so the new deployment does not collide with the existing one. Use single quotes around
values that contain `${S3_BUCKET}` so the shell does not expand them before `--set` writes them
to the file.

As a single `./bootstrap.sh` call (continue from Step 3 after this):

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

## Human gates

Everything else is unattended.

1. **The inputs above.**
2. **`kiro-cli login`** — a browser device flow. No flag, environment variable or file substitutes for
   it; Kiro has no execution-role path on any runtime.
3. **Authorizing `--deploy`** (exit 41) — only when the account has no AgentCore runtimes yet.
4. **Running `start` from Terminal.app** (exit 42) — only when the assistant's own shell is sandboxed:
   multi-coding-harness-compare creates a Seatbelt sandbox per evaluation, and macOS refuses a nested
   one. Every other step works from the sandboxed shell.
5. **Approving the shared test review** in the web UI on the first comparison. Development stops at
   `waiting_test_review` by design.

---

## The procedure

### Step 1 — Clone and run the bootstrap once

An existing filled `deploy.properties` beside the clone is used as-is; bootstrap asks only for values still marked as placeholders.

```bash
git clone <repository-url> multi-coding-harness-compare && cd multi-coding-harness-compare
./bootstrap.sh; echo "exit=$?"
```

On a fresh clone this installs Python 3.12, the locked dependencies (`requirements.lock`) and pytest
(`requirements-dev.lock`) into `.venv`, runs the test suite, copies `deploy.properties.example` to
`deploy.properties`, and stops with **exit 20**: the
`inputs` line lists every key still unanswered in `missing` and the question for each in
`questions`. If uv is missing it stops with exit 10 first — re-run with `--install-uv`.

### Step 2 — Record the answers

```bash
./bootstrap.sh --set RUNTIME=agentcore --set KIRO_AUTH=copy-host-login \
    --set AWS_PROFILE=NAME --set AWS_REGION=us-east-1 --set S3_BUCKET=NAME \
    --set REPOSITORY_ROOTS=/abs/path/to/projects --set AUTO_APPROVE=true; echo "exit=$?"
```

`AUTO_APPROVE=true` is the recommended answer; record whichever value the human chose.
`--set` rewrites only the named keys and keeps the file's comments and `${S3_BUCKET}` references.
The run continues into **preflight**, which checks every prerequisite read-only — inputs, host,
harness logins and provider configuration, AWS identity, both S3 prefixes, each Bedrock model, and
whether the AgentCore runtimes exist — against the configuration these answers produce. The full
report is `.bootstrap/preflight.json`; each check has `id`, `status`, `detail`, `fix` and `gate`. The
preflight line itself carries `failed_checks` (each failed check's `id`, `gate`, `detail` and exact
`fix`) and `warnings`; its `fix` joins the failed checks' fixes.

### Step 3 — Act on the exit code, then re-run the same command

Repeat until exit 0. Each re-run skips what is already done.

### Step 4 — Start the services and hand off the URL

```bash
./bootstrap.sh --start; echo "exit=$?"
```

The `start` line carries `url` (`http://127.0.0.1:<SERVICE_PORT>/#token=…`) and `handoff`, the
rule for it. **Give that whole URL to the human.** The fragment is an access token exchanged once for an HttpOnly session cookie; do not
open a browser yourself and do not copy the token anywhere else. Exit 42 instead means your shell is
sandboxed: give the human the printed command for Terminal.app. Repeating `start` does not
duplicate a listener or worker.

### Step 5 — First comparison

```bash
ID=$(.venv/bin/multi-coding-harness-compare demo | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["comparison_id"])')
.venv/bin/multi-coding-harness-compare export "$ID" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["status"])'
```

Poll the second command every minute rather than watching. When it prints `waiting_test_review`, ask
the human to approve the shared test review in the UI (gate 5). When it prints a final status,
`.venv/bin/multi-coding-harness-compare export "$ID" --output report.json` writes the full report;
verify three implementations and real acceptance evidence.

---

## Exit codes

| Exit | Stage | Meaning | Action |
|---|---|---|---|
| 0 | — | done | report the summary line |
| 2 | any | usage error: an unknown option, or a `--set` the inputs stage refused (unknown key, multi-line value, or an invalid value for a typed key such as a name with invalid characters) — see `error` in the JSON | correct the command from the error; re-run |
| 10 | host | uv is missing | re-run with `--install-uv` |
| 11 | host | not macOS | stop; see [Windows](#windows) |
| 12 | install | dependency install failed | report the log; check network access to the package index |
| 12 | tests | pytest is missing from `.venv` | re-run; the install stage installs `requirements-dev.lock` |
| 13 | tests | test suite failed | report the `FAILED`/`ERROR` lines in `detail` to the human; stop |
| 20 | inputs | answers missing | ask the questions in `questions`; re-run with `--set` |
| 21 | preflight | a check the assistant can fix failed (or preflight itself failed: a `*-error` check) | apply each `fix` in `failed_checks`; re-run |
| 22 | inputs / setup | the inputs command produced no report, or setup failed | read the log or `.bootstrap/setup.json`; fix `deploy.properties`; re-run |
| 30 | deploy | agentcore-deploy failed | read the log; check the deploy permissions below |
| 31 | probe | AgentCore runtimes unreachable or paths not writable | read `.bootstrap/probe.json` |
| 32 | doctor | blockers remain | read `.bootstrap/doctor.json`; each blocker names its remedy. After an `--offline` run, re-run without it: preflight then names who acts on each |
| 33 | start | the services did not start | read `.arena/api.log` and `.arena/worker.log` |
| 40 | any | a human must act — includes a `configuration-loads` failure when a typed key (e.g., `AGENTCORE_RUNTIME_NAME_PREFIX`) already in `deploy.properties` has an invalid value | relay the `fix` of each `failed_checks` entry whose `gate` is `human`, including the suggested corrected value; apply those whose `gate` is `assistant`; wait; re-run |
| 41 | preflight | AgentCore runtimes are not deployed, or the cached ones belong to another region, account, name prefix or transfer prefix | ask for authorization; then re-run with `--deploy` |
| 42 | start | this shell is sandboxed | give the human the printed command for Terminal.app |

Reports from the last run are under `.bootstrap/` (`preflight.json`, `setup.json`, `probe.json`,
`doctor.json`) with the full log beside them; the final stdout line is a `summary` object with
`exit` and `next`.

## Stage reference

| Stage | Does | Skipped when |
|---|---|---|
| host | macOS, `sandbox-exec`, Xcode Command Line Tools, uv | never |
| install | `uv venv --python 3.12`; `uv pip install -r requirements.lock -r requirements-dev.lock`; editable install | never (it is idempotent) |
| tests | `pytest tests`; the `tests/*.cjs` UI tests when Node.js is present; native-supervision tests skip themselves in a sandboxed shell | `--skip-tests` |
| inputs | `multi-coding-harness-compare inputs`: creates `deploy.properties` from the example; applies `--set` (refusing unknown keys and multi-line values, exit 2); stops with exit 20 while any value is still a placeholder (`YOUR_…`, `/absolute/path/to/…`) | never |
| preflight | `multi-coding-harness-compare preflight` — read-only | never (`--offline` skips its AWS checks) |
| setup | `multi-coding-harness-compare setup`, answered entirely from `deploy.properties` | `config.toml` exists and `--reconfigure` is not given |
| deploy | `agentcore-deploy` | `--deploy` not given, or `RUNTIME` is not `agentcore` |
| probe | `agentcore-probe` | `RUNTIME` is not `agentcore` |
| doctor | `multi-coding-harness-compare doctor` | never |
| start | `multi-coding-harness-compare start` | `--start` not given |

## Preflight checks

| Check | Fails when | Gate |
|---|---|---|
| `properties-file`, `properties-filled` | `deploy.properties` missing, malformed, or still holds `YOUR_` values | assistant / human |
| `configuration-loads` | the resulting configuration is invalid; the detail names the field | assistant / human |
| `choice-runtime`, `choice-kiro-auth` | `RUNTIME` / `KIRO_AUTH` not one of the listed values | human |
| `auto-approve-overridden` | `config.toml` sets `auto_approve` for a harness to a value other than `AUTO_APPROVE`, or to `true` while `deploy.properties` has no `AUTO_APPROVE` answer (configurations made before the key existed set `true`), so the human's answer would not apply or was never given; the human chooses which value stays | human |
| `artifact-s3-uri`, `agentcore-transfer-s3-uri` | an S3 URI is not set | human / assistant |
| `repository-roots` | a root does not exist | assistant |
| `judge-model` (warning) | `JUDGE_MODEL` unset: scoring and test generation unavailable | human |
| `claude-authentication`, `codex-authentication`, `kiro-authentication` | a harness has no usable login for the chosen runtime | human |
| `codex-provider-config` | under `agentcore`, Codex's provider configuration is missing, does not select Amazon Bedrock, or has a `profile` key. The fix writes a dedicated `CODEX_HOME_TEMPLATE`; it never edits the workstation's own `~/.codex/config.toml` | assistant |
| `codex-cli`, `kiro-cli` | under `native`, the CLI is not installed | human |
| `aws-identity`, `aws-account` | credentials invalid or expired, or the account differs from `AWS_ACCOUNT_ID` | human |
| `s3-artifact`, `s3-transfer` | a bucket or prefix is unreachable (the transfer bucket must belong to this account) | human |
| `bedrock-model-*` | a model is not an active inference profile in its region | human |
| `agentcore-runtimes` | the `<prefix>_claude/_codex/_kiro` runtimes do not exist | deploy |
| `agentcore-drift` | the cached runtime records in `DATA_DIR/agentcore-runtimes.json` are in another region, belong to another account (`AWS_ACCOUNT_ID`, or the one the profile reaches), or use another name prefix or transfer prefix | deploy |
| `host-*`, `data-dir-local`, `disk-space`, `service-port` | host requirements; `host-nested-sandbox` (this shell refuses a nested Seatbelt sandbox or `/bin/ps`) is a warning that only affects `start` (exit 42) | human / assistant |
| `inputs-error`, `harnesses-error`, `aws-error`, `host-error`, `agentcore-drift-error` | a check itself raised an error (a defect, not a configuration problem) | assistant: report it |

AWS permissions the profile needs: S3 list, get, put, delete, get-version and multipart upload on
both prefixes; `bedrock:InvokeModel*`, `bedrock:ListInferenceProfiles`,
`bedrock:GetInferenceProfile`, `bedrock:ListFoundationModels`; `sts:GetCallerIdentity`;
for `agentcore`, `bedrock-agentcore:ListAgentRuntimes` / `GetAgentRuntime` and the
`bedrock-agentcore` data-plane invoke and session calls; and, only for `--deploy`,
`iam:CreateRole`, `iam:GetRole`, `iam:UpdateAssumeRolePolicy`, `iam:PutRolePolicy`,
`iam:GetRolePolicy`, `iam:AttachRolePolicy`, `iam:DetachRolePolicy`, `iam:TagRole`,
`iam:PassRole` (resource `role/*` in the placeholder policy; supply your role name to
`python -m harness_image policy --kind deployer` to generate a policy scoped to that
role ARN; the `iam:PassedToService bedrock-agentcore.amazonaws.com` condition is always
applied) and `bedrock-agentcore:CreateAgentRuntime` / `UpdateAgentRuntime` /
`DeleteAgentRuntime`. `--deploy` also needs network access to the Kiro download URL. See
`docs/iam/` for machine-readable policy documents.

---

## Moving to a second workstation

Copy `deploy.properties` from the first workstation (it is gitignored), clone, and correct the
machine-local values with `--set` (`REPOSITORY_ROOTS`, any `*_CLI_PATH` or `*_HOME_TEMPLATE`):

```bash
./bootstrap.sh --set REPOSITORY_ROOTS=/abs/path/to/projects --start; echo "exit=$?"
```

AgentCore runtimes are found by name in the account, so a second workstation **must not** pass
`--deploy` to find them. If the new workstation's data directory (`.arena/`) is empty or new,
`agentcore-probe` (run by `./bootstrap.sh --start`) discovers the runtimes by name and writes
fresh `agentcore-runtimes.json` records automatically — no ARNs need to be copied. Each harness's non-secret configuration is read from the directory its own CLI uses:

| Harness | Source on macOS | Copied | Withheld |
|---|---|---|---|
| Claude Code | `$CLAUDE_CONFIG_DIR` or `~/.claude` | `settings.json`, `CLAUDE.md`, `skills/`, `agents/`, `commands/`, `rules/`, `output-styles/` (dirs); user-scope MCP from `~/.claude.json` | `.credentials.json` |
| Codex | `$CODEX_HOME` or `~/.codex` | `config.toml`, `AGENTS.md`, `AGENTS.override.md` (when present), `prompts/`, `rules/`, `skills/`; user skills from `~/.agents/skills` | `auth.json` |
| Kiro | `$KIRO_CONFIG_DIR` or `~/.kiro` | `settings/mcp.json`, `steering/`, `agents/`, `skills/`, `hooks/` (if present) | `data.sqlite3` (from platform data dir) |

A withheld login travels only when named in that harness's `refresh_files` (`KIRO_AUTH =
copy-host-login` does this for Kiro). An explicitly configured `*_HOME_TEMPLATE` is copied whole
instead, because that directory was assembled for the purpose. Under `agentcore`, Codex's
`config.toml` must have **no `profile` key** — the microVM has no `~/.aws`, and the SDK must fall
back to the execution role; preflight checks this. When the workstation's own `~/.codex/config.toml`
needs a `profile` for local use, leave it alone and use the dedicated `CODEX_HOME_TEMPLATE` that
preflight's fix creates.

## Changing one value later

```bash
.venv/bin/multi-coding-harness-compare stop; echo "exit=$?"
./bootstrap.sh --set AWS_REGION=eu-west-1 --start; echo "exit=$?"
```

`stop` stops this installation's API and worker, and refuses (exit 1, listing `active_runs`) while a
run is in progress — wait, or pass `--force` only with the human's agreement; **Resume** in the UI
continues interrupted runs. Running services keep the values they started with until restarted. Keys whose
change alters deployed resources — `AWS_REGION`, `AGENTCORE_TRANSFER_S3_URI` and the `AGENTCORE_*`
names — also need `--deploy`, with the human's authorization; `deploy.properties.example` says so on
each key, and preflight stops with exit 41 (`agentcore-drift`) while cached runtimes no longer match.

## Removing a deployment

To remove all AWS resources created by `agentcore-deploy`, run `deploy/agentcore-cleanup.sh`
(equivalently: `python -m harness_image cleanup`). It reads `deploy.properties` and deletes the
three runtimes, their workload identities, CloudWatch log groups, S3 transfer objects, the IAM inline
policy, and the IAM role (when safe to do so). Local state files are also removed. See the
**Removing a deployment** section in `README.md` for what is and is not deleted, and the full
consent flow. Never pass `--approve` without the human's explicit authorization.

---

## Windows

**Not supported.** This is not a configuration gap; the application does not start. Verified against
the current checkout:

- `arena/cli.py:9` has a module-level `import fcntl`, and the console entry point is
  `arena.cli:main` — so `multi-coding-harness-compare --help` raises `ModuleNotFoundError` before argparse runs.
  Four more module-level `fcntl` imports follow in `worker.py:2`, `artifact_storage.py:9`,
  `service_logs.py:2` and `project_outputs.py:2`, plus a lazy one at `agentcore_agent.py:127`.
- No `runtime` value rescues it. `native` requires `/usr/bin/sandbox-exec` and
  `sys.platform == "darwin"` (`native_sandbox.py:251`). `agentcore` routes independent evaluation
  back through that same sandbox (`evaluation.py:611`).
- Further confirmed blockers: the Unix `env` command in `initialize_checkout`
  (`isolation.py:136`, affects every runtime), `os.fchmod` and `close_fds=True` with a real file
  handle in `start_background` (`cli.py:39`, `cli.py:42`), `os.killpg`
  (`adapters/common.py:150`), `asyncio.add_signal_handler` (`native_runner.py:34`),
  `signal.SIGSTOP`/`SIGCONT` process suspension, hardcoded `/private/tmp` (`worker.py:91`) and
  `GIT_CONFIG_GLOBAL=/dev/null` (`isolation.py:91`), and a Kiro configuration directory that
  resolves to the Linux XDG path rather than `%LOCALAPPDATA%` (`host_setup.py:33`).

Two options for a Windows user:

1. **Use a macOS workstation**, including an EC2 Mac instance, and follow this runbook unchanged.
2. **Port the host layer** — roughly thirteen call sites: cross-platform file locking, a
   platform-guarded `fchmod`, native Python environment passing in place of `env`, Windows-safe
   process termination and suspension, `tempfile.gettempdir()` instead of `/private/tmp`,
   `os.devnull`, Windows Kiro handling, and a Windows evaluation backend. The evaluation backend is
   the structural piece: Windows has no Seatbelt, and independent evaluation always runs in it
   (`evaluation.py:611`), so a non-macOS host needs a new sandboxed evaluator.

WSL2 is a third path in practice, but it is a Linux host from the application's point of view and is
not covered here.

An EC2 Windows instance is subject to exactly the same blockers as any Windows workstation.
