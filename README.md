# multi-coding-harness-compare

A local chat application that dispatches development work to **Claude Code, Codex, and Kiro concurrently**, preserves their native agent loops, and evaluates their generated implementations using **Strands Evals 1.2.0**.

The application is implemented, including native adapters, a persistent worker, clarification and approval mailboxes, isolated checkouts, real command execution, custom Strands evaluators, durable history, and exports. A missing runtime, credential, model, or check produces a visible blocker or verification gap. There is no simulated coding activity or substitute implementation generator.

## Security considerations

- **Run only trusted tasks.** The harnesses execute AI-generated commands against the selected project directory. Only run comparisons on code and task descriptions you trust: repository content can contain instructions that steer agent behaviour (prompt injection).
- **Isolation per runtime.**
  - `agentcore`: each run executes in an isolated Bedrock AgentCore microVM in your own AWS account. Claude Code and Codex call Bedrock with the runtime's execution role; no API keys or AWS credentials are forwarded as environment variables. Harness login files found in the run's HOME (Claude Code `.credentials.json`, Codex `auth.json`, Kiro `data.sqlite3` — for example the workstation's Kiro login when `KIRO_AUTH` is `copy-host-login`) are passed to the microVM each turn as a separate S3 object under `secrets/` in the transfer prefix, encrypted with SSE-S3 (SSE-KMS when `AGENTCORE_TRANSFER_KMS_KEY_ARN` is set), and deleted after the turn; `preflight` reports any that remain. Use `agentcore` for untrusted tasks.
  - `native`: runs in a macOS Seatbelt default-deny sandbox. Write access is limited to the run's private directories (workspace, HOME, temporary files, and the per-run IPC bridge mailbox). Outbound HTTPS (port 443), HTTP (port 80), and DNS to any host are always allowed.
- **Permission prompts.** `AUTO_APPROVE` in `deploy.properties` (default `false`) decides whether the harnesses' own permission prompts are answered automatically inside the run's sandbox or come to you as approval cards. See [Permission prompts](#permission-prompts-auto_approve).
- **Local access only.** The web UI binds to `127.0.0.1` (the default `host` in `config.toml`). All API routes except the authentication endpoint (`/api/auth`) require the access token printed at start or an active session cookie, and requests whose `Host` header is not a loopback name are rejected. Binding to other interfaces is not supported: keep `host` on a loopback address (the `Host` check is not a network boundary), and do not share the access link.
- **Credentials.** The application uses your AWS profile and the harness logins configured on this workstation. Grant only the permissions described in [`docs/iam/`](docs/iam/). To encrypt AgentCore transfer-bucket objects with a customer-managed key, set `AGENTCORE_TRANSFER_KMS_KEY_ARN` in `deploy.properties`.
- **Cost.** Comparisons invoke Bedrock models and run AgentCore runtimes in your account, which incur AWS charges. Set up [AWS Budgets](https://aws.amazon.com/aws-cost-management/aws-budgets/) alerts to monitor costs.
- **Not for production.** This tool is provided as open-source software for evaluating coding harnesses. It is not intended for production workloads or for processing sensitive data.
- **Reporting security issues.** See [Security issue notifications](CONTRIBUTING.md#security-issue-notifications) in `CONTRIBUTING.md`.

## Architecture: two components

The application is split into two independently deployable components that share a `deploy.properties` file and a data directory:

**UI application** (`arena/` + static UI): the comparison web interface, API server, worker, native adapters, evaluation engine, and preflight/setup tooling. This is what `./bootstrap.sh` installs and `multi-coding-harness-compare start` launches. It has no dependency on `harness_image/`.

**Image component** (`harness_image/` + `deploy/agentcore-deploy.sh`): the AgentCore bundle builder and deployer. It resolves harness versions, builds deployment zips, creates the IAM execution role, and creates or refreshes the three AgentCore runtime resources. It runs without the UI application installed — any AI coding assistant can deploy without having the full API/worker environment.

Interface between the two:

| Artifact | Written by | Read by |
|---|---|---|
| `deploy.properties` | operator / `bootstrap.sh` | both |
| `DATA_DIR/agentcore-runtimes.json` | `harness_image` (`deploy`) | `arena/` probe, session activation |
| `DATA_DIR/harness-versions.lock.json` | `harness_image` (`plan`/`deploy`) | `harness_image` (`deploy`), preflight |

Neither component imports the other; the interface is file-based. The UI application does not need to be running for `harness_image` to deploy or update runtimes.

## For AI coding assistants: what to ask the user

Ask every question below in a single message before running anything that depends on the
answer. The table is generated from `arena/preflight.py::QUESTIONS` and `arena/properties.py::KEYS`
and kept in sync by `tests/test_docs_questions.py`.

| Key | Question | How to discover candidates | Example | Required |
|---|---|---|---|---|
| `RUNTIME` | Where should the coding harnesses run: `agentcore` (Bedrock AgentCore microVMs; Claude Code and Codex use the runtime's execution role; recommended) or `native` (this Mac's Seatbelt sandbox)? | — | `agentcore` | required |
| `KIRO_AUTH` | Should Kiro use this workstation's existing Kiro login (`copy-host-login`), or a login made inside a dedicated HOME (`login-in-home`)? | `test -f "$HOME/Library/Application Support/kiro-cli/data.sqlite3" && echo host-login-present` | `copy-host-login` | required |
| `AWS_PROFILE` | Which AWS profile reaches the account holding the S3 bucket, the Bedrock models and the AgentCore runtimes? | `.venv/bin/python -c 'import boto3; print(boto3.Session().available_profiles)'` | `my-aws-profile` | required |
| `AWS_REGION` | Which AWS region holds the bucket, the Bedrock models and the AgentCore runtimes (for example us-east-1)? | `.venv/bin/python -c 'import boto3,sys; print(boto3.Session(profile_name=sys.argv[1]).region_name)' NAME` | `us-east-1` | required |
| `S3_BUCKET` | Which S3 bucket, owned by that account, stores comparison artifacts and AgentCore transfers? | `.venv/bin/python -c 'import boto3,sys; print([b["Name"] for b in boto3.Session(profile_name=sys.argv[1]).client("s3").list_buckets()["Buckets"]])' NAME` | `my-bucket` | required |
| `REPOSITORY_ROOTS` | Which directory (or comma-separated directories) holds the projects comparisons may run against? | — | `/abs/path/to/projects` | required |
| `USER_NAMESPACE` | What short lowercase identifier (letters, digits, hyphens) should namespace your per-user AgentCore resources (required when AGENTCORE_ISOLATION=per-user; auto-derived from your login name when possible)? | `id -un` | `alice` | optional (auto-derived) |
| `AUTO_APPROVE` | Should the coding harnesses' own permission prompts be answered automatically inside each run's sandbox (`true`, recommended: Codex's auto-review agent answers Codex's, Kiro's are allowed, and Claude Code's auto mode still sends you what it escalates) or all come to you as approval cards (`false`, the default)? | — | `true` | ask (default `false`) |

`AUTO_APPROVE` has a default rather than a placeholder, so bootstrap does not stop for it: ask it anyway, recommend `true`, and tell the user what each value does ([Permission prompts](#permission-prompts-auto_approve)) before recording the answer with `./bootstrap.sh --set AUTO_APPROVE=true` or `--set AUTO_APPROVE=false`.

Optional keys with useful defaults (override only when a check's `fix` names one):
`JUDGE_MODEL`, `PLANNING_MODEL`, `CLAUDE_MODEL`, `CODEX_MODEL`, `KIRO_MODEL` (Bedrock inference
profiles enabled in `AWS_REGION`); `SERVICE_PORT` (default 8765); `DATA_DIR` (default `.arena`);
`AWS_ACCOUNT_ID` (enables drift checks); `AGENTCORE_*` names (keep identical across workstations
sharing one account); `*_CLI_PATH` / `*_HOME_TEMPLATE`; `CODEX_PROVIDER`; harness configuration
keys (`*_CONFIG_DIR`, `*_SKILLS_DIRS`, `*_STEERING_FILES`, `*_MCP_CONFIG`). See the
**Deployment properties** table below and `deploy.properties.example` for every key.

## Start

One entrypoint takes a fresh clone to a running installation: `./bootstrap.sh`. It is built to be run by an AI coding assistant — any assistant: Claude Code, Codex, Kiro, Cursor, Copilot, Gemini CLI, and others find their instructions in [`AGENTS.md`](AGENTS.md) — as well as by a person. It never prompts, prints one JSON line per stage, writes command output to `.bootstrap/`, ends with an exit code that names the outcome, and is safe to re-run: stages are idempotent (setup is skipped when `config.toml` already exists; install, tests, inputs, preflight and doctor re-run but do not destructively repeat work already done). [`docs/launch-runbook.md`](docs/launch-runbook.md) is the complete procedure behind it.

```bash
git clone <repository-url> multi-coding-harness-compare && cd multi-coding-harness-compare
./bootstrap.sh                 # installs and tests, then lists the questions still unanswered (exit 20)
./bootstrap.sh --set RUNTIME=agentcore --set KIRO_AUTH=copy-host-login --set AWS_PROFILE=NAME \
    --set AWS_REGION=us-east-1 --set S3_BUCKET=NAME --set REPOSITORY_ROOTS=/abs/path/to/projects
./bootstrap.sh --start         # preflight, setup, probe, doctor, then start; prints the access URL
```

**Fill in deploy.properties ahead of time (optional).** Copy the template and fill every `YOUR_` value before running bootstrap — bootstrap then asks only for values still marked as placeholders: `cp deploy.properties.example deploy.properties`, then edit `deploy.properties`. For a separate deployment in the same account (for example, testing a new version alongside a running one), also change `SERVICE_PORT`, `AGENTCORE_RUNTIME_NAME_PREFIX`, role and policy names, and both S3 URI prefixes — otherwise agentcore-deploy updates the shared runtimes in place.

Each run continues from the first unfinished stage; repeat the same command (adding `--set` only for new answers) until it exits 0, acting on each exit code as [`AGENTS.md`](AGENTS.md) says. Stages run in order: **host** (macOS, Xcode Command Line Tools, uv) → **install** (Python 3.12 virtualenv from `requirements.lock`, plus pytest from `requirements-dev.lock`) → **tests** → **inputs** (`deploy.properties` complete) → **preflight** (read-only checks of every prerequisite against the configuration the inputs would produce, including AWS) → **setup** (writes `config.toml`; an existing one is kept unless `--reconfigure`) → **deploy** (only with `--deploy`) → **probe** (AgentCore runtimes reachable) → **doctor** → **start** (only with `--start`).

### What a new deployment does

On a fresh clone, this is the ordered sequence from first command to first comparison:

1. **Questions asked.** The first `./bootstrap.sh` run installs dependencies, runs tests, and stops at exit 20 with the list of unanswered `deploy.properties` keys and the question for each: `RUNTIME`, `KIRO_AUTH`, `AWS_PROFILE`, `AWS_REGION`, `S3_BUCKET`, `REPOSITORY_ROOTS`. Supply them with `--set KEY=VALUE`; they are written to `deploy.properties` and never need to be repeated.

2. **Prerequisites checked.** After the answers are supplied, **preflight** checks every prerequisite read-only — host, harness logins, Codex provider configuration, AWS identity, S3 access, Bedrock model availability (`JUDGE_MODEL` and `PLANNING_MODEL`, and under `agentcore` also `CLAUDE_MODEL` and `CODEX_MODEL`, must be active inference profiles in `AWS_REGION`; `KIRO_MODEL` is a Kiro model name, not a Bedrock profile), and AgentCore runtimes — before writing anything. Failed checks list a `gate` (`human`, `assistant`, or `deploy`) and an exact `fix`. See [docs/launch-runbook.md](docs/launch-runbook.md) for the full check table.

3. **Files written.** Once preflight passes, **setup** copies `config.example.toml` to `config.toml` (kept on subsequent runs) and writes every deployment-specific value to `deploy.properties`.

4. **AgentCore runtimes.** If the account has no runtimes yet, preflight stops with exit 41. A human must authorize `./bootstrap.sh --deploy`, which creates an IAM role, the deployment bundles in S3, and three AgentCore runtimes. A second workstation in the same account never needs `--deploy`: runtimes are discovered by name (see **Moving to a second workstation** in [docs/launch-runbook.md](docs/launch-runbook.md)).

5. **First start.** `./bootstrap.sh --start` runs probe (confirms runtimes are reachable and paths are writable inside the microVM), doctor (confirms auth, models, and S3), then starts the API and worker. It prints the access URL; give the full URL including the `#token=…` fragment to the user. **A new machine has no model lists yet**: opening the UI fetches them, and on `agentcore` that first fetch starts cold microVMs, so it can take a few minutes. The dropdowns fill in when it finishes, and a comparison cannot be started with a model that is not in them.

6. **What travels into runs and what does not.** Under `agentcore`, Claude and Codex authenticate through the runtime execution role — no API key or credential file is forwarded; under `native` they use this workstation's own logins. On both runtimes only an allowlist of non-secret configuration is copied into each run: `settings.json` and `CLAUDE.md` for Claude; `config.toml`, `AGENTS.md` and `AGENTS.override.md` (when present) for Codex. A configured `*_HOME_TEMPLATE` is copied whole instead. Claude's model-policy keys (`model`, `availableModels`, `enforceAvailableModels`, `modelOverrides`, `fallbackModel`, `modelSettings`, `effortLevel`, and `env.ANTHROPIC_MODEL`) are removed from the copied `settings.json`, so the comparison's model and effort win over your personal Claude Code settings. Kiro uses its own login (copied from the host or made in a dedicated HOME, per `KIRO_AUTH`).

7. **First comparison.** Open the URL. In **New comparison**: describe a task; switch ON the coding harnesses to use (at least one is required); choose models for those harnesses (or keep the configured defaults — validated against the fetched catalogue); optionally choose a project folder; choose Small suite or Comprehensive test coverage; optionally supply a validation plan; click **Start comparison**. OFF harnesses receive no task prompt and have no coding run created. Answer any clarification cards. When the comparison reaches `waiting_test_review`, a human must **Approve tests for selected harnesses** in the Review tests panel before evaluation can run.

8. **Model, judge, and planner pickers.** The judge picker defaults to the configured `JUDGE_MODEL`; leaving it at "Configured default" submits `null` and the service resolves the default. The planning-model picker works the same way; when `planning_effort` is set, the list is filtered to Claude IDs. Every model — picked or a configured default — must be in the freshly fetched lists, or the comparison is refused with the reason.

9. **Follow-ups.** With a comparison selected, use the coding harness switches to choose the recipients among that comparison's original participants. At least one must remain ON. Only selected runs are revived or restarted with the follow-up; OFF harnesses do not receive that instruction, including on a later retry. Switching a harness OFF does not cancel work already in progress. The conversation records each follow-up's recipients, and the panels show their delivery progress. A follow-up resets the comparison's test review, so the revised tests need approval before evaluation; development does not wait for it. Results from an earlier specification remain visible with their version. Follow-ups are refused, with the reason, while artifacts are transferring or saved code is being re-scored.

10. **Stop and restart.** `multi-coding-harness-compare stop` stops the API and worker (refusing if a run is in progress unless `--force` is passed). To apply a changed value: stop, then `./bootstrap.sh --set KEY=VALUE --start`. **Resume** in the UI continues interrupted runs.

**Prerequisites**, all checked before anything is written:

| Prerequisite | Needed for | Checked by |
|---|---|---|
| macOS with `/usr/bin/sandbox-exec` (Apple silicon or Intel, including EC2 Mac) | always; evaluation runs in a local Seatbelt sandbox | host |
| Xcode Command Line Tools (`git`) | always | host — a human installs with `xcode-select --install` |
| `uv` | always; it installs Python 3.12 | host — `./bootstrap.sh --install-uv` installs it |
| An AWS profile reaching one account that holds the S3 bucket, the Bedrock models (`JUDGE_MODEL`, `PLANNING_MODEL`, and under `agentcore` `CLAUDE_MODEL`/`CODEX_MODEL`) and, for `agentcore`, the AgentCore runtimes. `KIRO_MODEL` is a Kiro-native model ID and is not a Bedrock prerequisite. See [`docs/iam/workstation-policy.json`](docs/iam/workstation-policy.json) for the exact workstation action list. | always | preflight: identity, bucket, model and runtime checks |
| A Kiro login | always (Kiro has no execution-role path) | preflight — `kiro-cli login` is a browser flow a person completes |
| Codex provider configuration selecting Amazon Bedrock with no `profile` key | `agentcore` | preflight |
| `codex` and `kiro-cli` on `PATH` (or `*_CLI_PATH`), and Claude Code and Codex logins | `native` | preflight |
| IAM, S3 and AgentCore permissions to create the execution role and three runtimes, plus network access to the Kiro download URL; see [`docs/iam/deployer-policy.json`](docs/iam/deployer-policy.json) for the exact action list (includes `iam:PassRole` scoped to the execution role ARN with `iam:PassedToService bedrock-agentcore.amazonaws.com` — the placeholder policy uses `role/*`; run `python -m harness_image policy --kind deployer` with a configured role name to narrow the scope — `bedrock-agentcore:CreateAgentRuntime`, `UpdateAgentRuntime`, `GetAgentRuntime`, `ListAgentRuntimes`). The execution role inline policy is in [`docs/iam/execution-policy.json`](docs/iam/execution-policy.json). | `--deploy` only | deploy |
| A local data directory (not iCloud/Dropbox/OneDrive or a network share), 3 GB free, a free `SERVICE_PORT` | always | preflight |
| Node.js (for `npm`) | `--deploy` (to vendor Codex via `npm pack`); also the `tests/*.cjs` UI tests | deploy / tests (skipped without it) |

**Inputs** live in `deploy.properties` (see **Deployment properties** below) and are asked once, before setup: `RUNTIME` (`agentcore`, recommended, or `native`), `KIRO_AUTH` (`copy-host-login` or `login-in-home`), `AWS_PROFILE`, `AWS_REGION`, `S3_BUCKET` and `REPOSITORY_ROOTS`. The first run copies `deploy.properties.example` and reports each unanswered key with the question to ask.

**Human gates** — everything else is unattended: answering the inputs; `kiro-cli login`; authorizing `--deploy` when the account has no AgentCore runtimes yet, or the cached ones no longer match the configuration (exit 41; it creates or refreshes an IAM role, S3 objects and three runtimes); running `start` from Terminal.app when the assistant's shell is itself sandboxed (exit 42); and approving the shared test review in the UI on the first comparison. The exit codes are listed in `./bootstrap.sh --help` and [`AGENTS.md`](AGENTS.md).

**No Docker or container build is involved.** The pinned Claude Agent SDK includes Claude Code.

### Manual setup

The same steps by hand:

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.lock -r requirements-dev.lock   # the second only for tests
uv pip install --python .venv/bin/python --no-deps -e .
.venv/bin/multi-coding-harness-compare inputs --set RUNTIME=agentcore --set KIRO_AUTH=copy-host-login \
    --set AWS_PROFILE=NAME --set AWS_REGION=us-east-1 --set S3_BUCKET=NAME --set REPOSITORY_ROOTS=/abs/path/to/projects
.venv/bin/multi-coding-harness-compare preflight
.venv/bin/multi-coding-harness-compare setup
.venv/bin/multi-coding-harness-compare agentcore-probe    # agentcore; agentcore-deploy first if none exist
.venv/bin/multi-coding-harness-compare doctor
.venv/bin/multi-coding-harness-compare open
```

`inputs` creates `deploy.properties` from the example, records each `--set KEY=VALUE` (unknown keys and multi-line values are refused, exit 2), and lists every value still a placeholder with the question to ask (exit 20). `stop` ends this installation's API and worker; it refuses while a run is in progress unless `--force` is passed, and **Resume** continues an interrupted run.

`setup` copies `config.example.toml` to `config.toml` for this workstation and writes every answer it settles — runtime, Kiro authentication, AWS profile and region, S3 bucket, repository root — to `deploy.properties` beside it (see **Deployment properties** below). Run it with no flags first to see every decision it needs, each with the flag that answers it; it writes nothing until all are resolved, and never prompts unless `--interactive` is passed on a terminal. It refuses to write a configuration this build would fail to load. When a `deploy.properties` already exists, setup takes its values as answers and rewrites only the keys it decides; `--aws-region` sets `AWS_REGION` (when neither gives one, `us-east-1`), and `--repository-root` adds a root ahead of those already listed. `preflight` is read-only: it checks every prerequisite against the configuration `deploy.properties` produces, and exits 1 while any check fails (`--offline` skips the AWS checks). Read **Authentication on a new workstation** below before choosing `--kiro-auth`. Copying `config.example.toml` to `config.toml` and `deploy.properties.example` to `deploy.properties` by hand, then replacing every `YOUR_` value, also works.

Adding `--deploy` to `setup` runs `agentcore-deploy` and `agentcore-probe` in the same invocation. `--deploy` creates AWS resources — an IAM execution role, S3 objects and three AgentCore runtimes — so it is never implied; without it, setup prints those commands as ordered `next_steps`. `--transfer-bucket` overrides the bucket used for job/result transfer, which otherwise reuses `--artifact-bucket` under a separate prefix. Runtime ARNs are discovered from the account by canonical name, so a second workstation in the same account needs no ARNs and must not re-deploy to find them.

`REPOSITORY_ROOTS` in `deploy.properties` (comma-separated) lists the directories comparisons may use; the service refuses repositories outside them. An empty project needs no repository path. In the task composer, choose **Project folder → Browse…**, open a folder, and click **Select this folder**. The picker lists directories on the machine running multi-coding-harness-compare, within `repository_roots`; it does not upload files from your browser. You can also enter an absolute path.

`.venv/bin/multi-coding-harness-compare open` starts services if needed and opens the access link in your default browser.

`multi-coding-harness-compare start` launches the API and worker in detached process sessions and returns. Their active logs are owner-readable at `.arena/api.log` and `.arena/worker.log`; closed log segments are archived to S3. Process IDs are printed and saved in `.arena/services.json`. Repeating the command does not duplicate an existing listener or worker. No operating-system startup service is installed.

**Data directory.** `DATA_DIR` in `deploy.properties` (default `.arena`) resolves against the directory holding `deploy.properties`, so `start`, `serve` and `worker` share one store wherever they are launched from; without a `deploy.properties`, `data_dir` resolves against the launch directory. It holds `arena.sqlite3` (the SQLite WAL store), `comparisons/` (run workspaces), `api.log`, `worker.log`, `services.json`, and `ui-token`. Keep it on a local filesystem: SQLite WAL requires POSIX file locking, and its three files — `arena.sqlite3`, `arena.sqlite3-wal`, `arena.sqlite3-shm` — must stay together. Do not place it in a folder synced by iCloud Drive ("Desktop & Documents" in System Settings), Dropbox, or OneDrive, and not on an NFS or SMB network share. The API and worker can start at the same moment on a fresh install: WAL initialisation is serialised by a lock file in the data directory, so the first start on a new clone no longer fails with "database is locked". For an empty-project comparison, the harness output directories are created at `projects/<comparison_id>/` beside the data directory (one level up from `.arena`).

### Deployment properties

Everything that changes between users, machines and AWS accounts lives in one gitignored file, `deploy.properties`, beside `config.toml` (copy `deploy.properties.example`, or let `setup` write it; `MCHC_PROPERTIES` points elsewhere). `config.toml` keeps behaviour — limits, timeouts, harness options. Porting is an edit to `deploy.properties` followed by a restart, plus `agentcore-deploy` only where the table says so.

Each line is `KEY = VALUE`; `#` starts a comment, a value may reference another key as `${KEY}`, and an empty value means unset. Credentials never go here: secret-looking keys (`TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, ...) are refused, because the file holds coordinates — profile names, regions, bucket URIs, role names, paths. Each key fills the configuration fields it names, and any string in `config.toml` may also reference a key as `"${KEY}"`. A literal value in `config.toml` still wins for its field, so an existing configuration keeps working. `multi-coding-harness-compare doctor` lists every value with its source, each `config.toml` literal that overrides a property, unknown keys nothing refers to, and cached AgentCore runtimes in `.arena/agentcore-runtimes.json` whose region, account (`AWS_ACCOUNT_ID`), name prefix or transfer prefix (`AGENTCORE_TRANSFER_S3_URI`) no longer match; preflight stops on those with exit 41 (`agentcore-drift`).

**Two equivalent ways to set a value**: edit the line in `deploy.properties` directly, or run `./bootstrap.sh --set KEY=VALUE`. The `--set` form validates first (refusing unknown keys, multi-line values and invalid values with exit 2, writing nothing) and then writes the value into the file. Some keys have naming rules enforced on write (`--set`, exit 2) and on load (preflight `configuration-loads`, exit 40): `AGENTCORE_RUNTIME_NAME_PREFIX` requires `[A-Za-z][A-Za-z0-9_]*` with a max of 41 characters (no hyphens); `AGENTCORE_EXECUTION_ROLE_NAME` and `AGENTCORE_INLINE_POLICY_NAME` require `[\w+=,.@-]` up to 64 and 128 characters respectively; `USER_NAMESPACE` requires `[a-z0-9][a-z0-9-]*`. See the naming-rules table in [`docs/launch-runbook.md`](docs/launch-runbook.md) and the worked example for a separate deployment.

Every key in `arena/properties.py::KEYS` appears below. The table is kept in sync with KEYS by `tests/test_docs_questions.py`.

**Core AWS / S3**

| Key | Fills | Changing it needs |
|---|---|---|
| `RUNTIME` | `runtime` | `agentcore-deploy` when switching to agentcore |
| `KIRO_AUTH` | `harnesses.kiro.refresh_files` | a restart |
| `AWS_PROFILE` | `artifact_s3_profile`, `judge_profile`, `agentcore.profile` | a restart |
| `AWS_REGION` | `artifact_s3_region`, `judge_region`, `agentcore.region` | `agentcore-deploy`: runtimes are regional |
| `AWS_ACCOUNT_ID` | nothing; `doctor` drift checks | `agentcore-deploy` in the new account |
| `AWS_CONFIG_FILE` | `aws_config_file` | a restart |
| `AWS_SHARED_CREDS_FILE` | `aws_shared_creds_file` | a restart |
| `S3_BUCKET` | nothing; shared by the two URIs as `${S3_BUCKET}` | see the URIs |
| `ARTIFACT_S3_URI` | `artifact_s3_uri` (required) | a restart; archived comparisons under the old URI are no longer readable here |
| `REPOSITORY_ROOTS`, `DATA_DIR`, `SERVICE_PORT` | `repository_roots`, `data_dir`, `port` | a restart; a new `DATA_DIR` abandons warm AgentCore sessions and local history |

**AgentCore (all need `agentcore-deploy` unless noted)**

| Key | Fills | Changing it needs |
|---|---|---|
| `AGENTCORE_TRANSFER_S3_URI` | `agentcore.transfer_s3_uri` | `agentcore-deploy`: bundles and the role's S3 scope follow it |
| `AGENTCORE_EXECUTION_ROLE_NAME`, `AGENTCORE_INLINE_POLICY_NAME`, `AGENTCORE_RUNTIME_NAME_PREFIX` | the `agentcore.*` names | `agentcore-deploy`; old resources must be deleted by hand |
| `AGENTCORE_APPLICATION_TAG` | `agentcore.application_tag` | `agentcore-deploy` to update runtime descriptions |
| `AGENTCORE_EXECUTION_ROLE_ARN` | `agentcore.execution_role_arn` | `agentcore-deploy`; a pinned role is used as-is |
| `AGENTCORE_ISOLATION` | `agentcore.isolation` | `agentcore-deploy`: per-user isolation adds a namespace to S3 keys and role names |
| `AGENTCORE_TRANSFER_KMS_KEY_ARN` | `agentcore.transfer_kms_key_arn` | `agentcore-deploy`: execution role gains KMS grants |
| `USER_NAMESPACE` | `user_namespace` | `agentcore-deploy` (per-user isolation); a restart otherwise |
| `CLAUDE_RUNTIME_ARN`, `CODEX_RUNTIME_ARN`, `KIRO_RUNTIME_ARN` | `agentcore.runtimes.<harness>.arn` | a restart only (ARN is used at session creation) |

**Models**

| Key | Fills | Changing it needs |
|---|---|---|
| `JUDGE_MODEL`, `PLANNING_MODEL` | `judge_model`, `planning_model` | a restart; must be an active Bedrock inference profile in `AWS_REGION` |
| `CLAUDE_MODEL`, `CODEX_MODEL`, `KIRO_MODEL` | `harnesses.<name>.model` | a restart (`KIRO_MODEL` is a Kiro-native model ID, not a Bedrock profile) |

**Native CLI and home templates**

| Key | Fills | Changing it needs |
|---|---|---|
| `CLAUDE_CLI_PATH`, `CODEX_CLI_PATH`, `KIRO_CLI_PATH` | `harnesses.<name>.cli_path` | a restart |
| `CLAUDE_HOME_TEMPLATE`, `CODEX_HOME_TEMPLATE`, `KIRO_HOME_TEMPLATE` | `harnesses.<name>.home_template` | a restart |

**Harness image build (all need `agentcore-deploy`)**

| Key | Fills | Changing it needs |
|---|---|---|
| `CLAUDE_HARNESS_SOURCE`, `CODEX_HARNESS_SOURCE`, `KIRO_HARNESS_SOURCE` | `harnesses.<name>.harness_source` | `agentcore-deploy`: bundle is rebuilt |
| `CLAUDE_HARNESS_VERSION`, `CODEX_HARNESS_VERSION`, `KIRO_HARNESS_VERSION` | `harnesses.<name>.harness_version` | `agentcore-deploy`: bundle is rebuilt at the pinned version |
| `HARNESS_TRUSTED_SOURCES` | `harness_image.trusted_sources` | `agentcore-deploy`: next bundle fetch uses new allowlist |
| `KIRO_DOWNLOAD_URL` | `harness_image.kiro_download_url` | `agentcore-deploy`: used on next Kiro bundle fetch |
| `HARNESS_IMAGE_WORKSTATION_CONFIG` | `harness_image.workstation_config` | `agentcore-deploy`: next bundle bakes or omits workstation config snapshot |

**Harness configuration (all need a restart)**

| Key | Fills | Changing it needs |
|---|---|---|
| `HARNESS_CONFIG_DEFAULT_CHOICE` | `harness_config_default_choice` | a restart; unset: `overrides` on agentcore, `default` on native |
| `CLAUDE_CONFIG_DIR`, `CODEX_CONFIG_DIR`, `KIRO_CONFIG_DIR` | `harnesses.<name>.config_dir` | a restart; read only when the effective choice is `default` |
| `CLAUDE_CONFIG_INCLUDE`, `CODEX_CONFIG_INCLUDE`, `KIRO_CONFIG_INCLUDE` | `harnesses.<name>.config_include` | a restart |
| `CLAUDE_CONFIG_EXCLUDE`, `CODEX_CONFIG_EXCLUDE`, `KIRO_CONFIG_EXCLUDE` | `harnesses.<name>.config_exclude` | a restart |
| `CLAUDE_SKILLS_DIRS`, `CODEX_SKILLS_DIRS`, `KIRO_SKILLS_DIRS` | `harnesses.<name>.skills_dirs` | a restart |
| `CLAUDE_STEERING_FILES`, `CODEX_STEERING_FILES`, `KIRO_STEERING_FILES` | `harnesses.<name>.steering_files` | a restart |
| `CLAUDE_MCP_CONFIG`, `CODEX_MCP_CONFIG`, `KIRO_MCP_CONFIG` | `harnesses.<name>.mcp_config` | a restart |
| `CLAUDE_CONFIG_S3_URI`, `CODEX_CONFIG_S3_URI`, `KIRO_CONFIG_S3_URI` | `harnesses.<name>.config_s3_uri` | a restart |
| `CLAUDE_INHERIT_HOST_SETUP`, `CODEX_INHERIT_HOST_SETUP`, `KIRO_INHERIT_HOST_SETUP` | `harnesses.<name>.inherit_host_setup` | a restart (`native` only; refused on `agentcore`) |
| `AUTO_APPROVE` | `harnesses.<name>.auto_approve` for all three harnesses (default `false`; see [Permission prompts](#permission-prompts-auto_approve)) | a restart |
| `CLAUDE_ENV_PASSTHROUGH`, `CODEX_ENV_PASSTHROUGH`, `KIRO_ENV_PASSTHROUGH` | `harnesses.<name>.env` | a restart |
| `CODEX_PROVIDER` | `harnesses.codex.provider` | a restart |

Relative paths resolve against the directory holding `deploy.properties`. The defaults for the AgentCore names are the names existing deployments already use, so introducing the file needs no redeploy. To move to another machine of the same account, copy `deploy.properties`, then run `./bootstrap.sh --set REPOSITORY_ROOTS=… --start` (plus any other machine-local path) — runtimes are found by name, so no deploy. For a new account, set the AWS keys and bucket; preflight then stops at exit 41 until a human authorizes `./bootstrap.sh --deploy`, which rewrites the cached runtime records.

Run startup from a normal terminal; a parent sandbox may prohibit creating another Seatbelt sandbox. The runtime refuses to run without OS isolation. It never falls back to unrestricted subprocesses.

Only macOS is supported; see Windows in [`docs/launch-runbook.md`](docs/launch-runbook.md). To run harnesses in Amazon Bedrock AgentCore Runtime Firecracker microVMs, set `RUNTIME = agentcore` and run `multi-coding-harness-compare agentcore-deploy`; see "Remote execution on Amazon Bedrock AgentCore Runtime" below.

For foreground operation, run `multi-coding-harness-compare serve` and `multi-coding-harness-compare worker` in separate terminals instead.

Open the local access URL printed by `multi-coding-harness-compare start` or `multi-coding-harness-compare serve`. Its fragment contains the app's local access token; the frontend exchanges it for an HttpOnly session cookie and removes the fragment. The token is stored in `.arena/ui-token` with owner-only permissions and is not readable by coding sandboxes. Subsequent visits in that browser can use the ordinary URL. A new browser or an expired session shows **Connect to multi-coding-harness-compare** instead of failed model/folder controls. Paste the complete local access link there, or run `multi-coding-harness-compare open`. Browsers keep their own session cookies; the in-app browser and Chrome do not share a login. The app stops background API polling while disconnected, preserves task drafts when a session expires, and reloads model lists and folders after reconnection.

The default port is 8765; set `SERVICE_PORT` in `deploy.properties` to use another. The API requires the session cookie or a Bearer token, is loopback-only by default, and rejects cross-origin mutations and unexpected Host headers. It is a single-user application, not a multi-tenant internet service.

The API and worker are separate processes. Closing or refreshing the browser does not stop work. Run these two commands under your preferred process supervisor if they must survive logout. Only one worker may own a data directory; its file lock prevents duplicate dispatch, while that worker has capacity for three simultaneous harnesses per comparison.

Claude's adapter checks that multi-coding-harness-compare's `arena` clarification bridge has connected before submitting the task prompt. It waits up to `setup_seconds` and retries a failed bridge connection once. A bridge startup failure is reported directly, without consuming a model turn that cannot call the required clarification tools.

## Assign a task

1. Click **New comparison** and describe the development task.
2. Switch ON the coding harnesses to use, keeping at least one ON. Choose a model in each selected harness's dropdown, or keep its configured/native default. OFF harnesses' model, effort, and configuration controls are disabled and omitted from the submission.
3. Choose **Empty project** or **Project folder → Browse… → Select this folder**.
4. Choose **Small suite** (the default) or **Comprehensive** test coverage.
5. Optionally supply an independent validation plan, then click **Start comparison**. Answer any clarification cards that appear.

The same switches choose recipients when sending follow-ups to an existing comparison. A harness that did not participate originally is marked **Not in this comparison**; start a new comparison to use it. Choices are kept separately for the new-comparison form and each comparison while the page is open. If the API or worker does not support harness selection, submission is blocked with a restart message rather than allowing an older process to send the prompt to all harnesses.

Selective follow-ups can leave implementations with different requirements. Their code is preserved, but an implementation that did not receive all requirements for the shared evaluation is marked evaluation incomplete rather than scored against instructions it never received.

Model choices come from the installed native interfaces: Claude Code initialization, Codex `model/list`, and Kiro ACP session models. When reusing Codex's Amazon Bedrock provider, multi-coding-harness-compare also checks the regional Mantle `/v1/models` catalogs and offers only models available in their configured regions. On `runtime = "agentcore"` only, the catalogue also appends every ACTIVE Bedrock inference profile fetched with the `agentcore` AWS profile: profiles whose ID contains `.anthropic.` (such as `us.anthropic.*` and `global.anthropic.*`) are appended to Claude Code's list, profiles containing `.openai.` are appended to Codex's list, and Kiro receives none. The catalogue is cached and only probes on an explicit refresh or when the cache is empty — the UI shows the cached lists at startup and, when the cache is warm, refreshes them once in the background; on a cold start the initial read already discovers, so startup never probes twice. On a fresh deployment (empty cache) the initial probe runs immediately at startup; on `agentcore` runtime it activates cold microVMs and can take two to three minutes before all dropdowns populate. If a dropdown is still empty or marked stale after that window, use **Refresh models**. **Refresh models** requests a fresh probe. Submitting a comparison validates selections against the cached catalogue without probing. Probes read model metadata without submitting a coding prompt and run one harness at a time, each with a 120-second budget (Codex's `initialize` call has a 60-second inner timeout); on the AgentCore runtime each probe starts a microVM session. A harness whose probe fails keeps its last list, marked stale with the error. Authentication and inference permissions are still checked by the harness when a task runs.

Every model submitted to the API is validated against the catalogue that was stored when the last probe ran, using a fingerprint that captures the current runtime and harness configuration. This applies to both explicit picker selections and configured defaults (`CLAUDE_MODEL`, `CODEX_MODEL`, `KIRO_MODEL`): if a configured default is not in the stored list for its harness, the submission is rejected with an error that names the property and tells you to refresh. The catalogue must have been fetched with the same configuration fingerprint; a stale or mismatched catalogue also causes rejection. An omitted model with no configured default (`null`) is not validated and lets the native harness choose. When a harness reports a model in its session event that differs from the requested ID (after stripping any trailing `[1m]` annotation), multi-coding-harness-compare records a `model_mismatch` field on the run and emits a `model_mismatch` event. Aliases that contain no `.` — such as `"sonnet"`, `"opus"`, `"auto"`, or `"default"` — are never compared, because the harness legitimately resolves them to a concrete ID. The comparison's panel and table row display a visible warning when `model_mismatch` is set: **Ran &lt;reported&gt; instead of the requested &lt;requested&gt;**. The run continues; the mismatch is informational.

Each harness also exposes an effort selector immediately below its model picker. The levels are `low`, `medium`, `high`, `xhigh`, and `max`. The list follows the picked model (or the configured default when no model is chosen): Claude Code reports its supported levels through `supportedEffortLevels`; Codex reports them through `supportedReasoningEfforts` and also supplies a per-model default shown in the empty-selection label; Kiro and Bedrock-profile-only catalogue entries advertise no level list, so all five levels are offered. A model that explicitly reports no supported levels shows **Effort not supported by this model** and disables the selector. Set `effort` under `[harnesses.<name>]` in `config.toml` as a configured default; it is silently suppressed when the chosen model explicitly reports supported levels that exclude it. Submitting a comparison rejects an explicitly chosen effort that the chosen model's catalogue reports as unsupported. The selection is saved with the comparison and stored as `requested_effort` on every run; retries, resumes, and requirement revisions preserve it. Every clarify and develop turn passes it to the harness: Claude Code via the Agent SDK `effort` option, Codex via the `effort` field on `turn/start`, and Kiro via `kiro-cli acp --effort`. Independent acceptance-test planning uses `planning_effort` and judges use their model's default generation parameters; neither uses the per-harness effort.

The chosen IDs are stored on the comparison and on each run as `requested_model`; retries and revisions preserve them. A configured default is resolved when the task is submitted. An omitted model with no configured default lets the native harness choose. Reported runtime models remain separate from requested IDs and aliases. Existing saved comparisons continue to use their original session data.

**Judge picker.** The first option in the judge picker is always enabled and shows the configured default: `Configured default · <JUDGE_MODEL>` when one is set, or `No judge configured` when none is. For an existing comparison the label reads `This comparison's judge · <model>`. No concrete judge ID is pre-selected for a new comparison; leaving the picker at its first option submits `judge_model: null` and the service resolves the configured default at submission time, validating it against the catalogue the same way an explicit pick is validated.

**Planning-model picker.** A planning-model picker appears beside the judge controls when starting a new comparison. Its options come from the judge catalogue (`/api/judge/models`); when `planning_requires_claude` is true (i.e., `planning_effort` is configured) the list is filtered to IDs containing `anthropic.claude`. The first option shows `Configured default · <planning model>`, with ` · effort: <level>` appended when `planning_effort` is also configured (rendered as `highest supported` when `planning_effort = "highest"`), or `No planner configured` when nothing is set. Leaving the picker at its first option submits `planning_model: null` and the service resolves the configured default (or falls back to the judge model). The resolved planning model and its concrete effort level are validated against the judge catalogue and stored on the comparison as `planning.model` and `planning.effort`; the test-review progress text shows the stored effort. `GET /api/system` reports `planning_requires_claude: true` when `planning_effort` is set, so the UI can filter accordingly.

### Follow-ups

With a comparison selected, the text composer sends a follow-up to all three harnesses simultaneously — the button reads **Send to all harnesses ↗** and a banner confirms **Follow-up · sent to Claude Code, Codex and Kiro**. Every terminal run — evaluated, failed, cancelled, blocked, limit-exhausted, or interrupted — is revived and re-queued; a harness whose development turn is already running restarts that turn against the new instructions. Each harness's next development turn opens with a **FOLLOW-UP INSTRUCTIONS FROM THE USER** block listing every follow-up it has not yet completed, in the order they were sent.

Each harness panel tracks delivery status. **Follow-up in progress** means the current development turn is actively carrying all pending follow-ups. **Follow-up pending** means the harness has not yet started the carrying turn. **Follow-ups delivered (n)** means every follow-up finished in a normally completed turn. Delivery is recorded only when a turn ends "implemented"; a turn that is cut short — revised, failed, or cancelled — leaves its follow-ups pending for the next attempt.

Follow-ups are refused while artifact transfer is in progress (archiving, archive_queued, restoring, or a failed transfer) or while re-scoring is running (evaluation or verification queue); the composer shows the reason and disables the button until the blocking operation finishes.

A follow-up increments the specification version when the spec is frozen, which invalidates the current test-review approval. The revised tests need fresh approval before evaluation can run; development on all three harnesses proceeds in parallel while the new review is pending.

The application is **Multi Coding-Harness Compare** (`multi-coding-harness-compare`). `multi-coding-harness-compare` is the primary command and `mchc` is a short alias; `.venv/bin/python -m mchc start` also works from this checkout. The `arena` command also still works, and the `.arena` data directory and `arena_session` cookie keep their names, so existing history and browser sessions carry over. `MCHC_CONFIG` selects an alternate TOML configuration; `ARENA_CONFIG` remains supported.

## Authentication on a new workstation

Read this before running setup. It decides what reaches a model and what leaves this machine, and one harness needs a decision that cannot be made for you.

**Claude Code and Codex need no credential of their own under `RUNTIME = agentcore`.** Both reach Amazon Bedrock from inside the microVM using the AgentCore runtime execution role. multi-coding-harness-compare injects `CLAUDE_CODE_USE_BEDROCK=1` and the region for Claude, and `MCHC_CODEX_PROVIDER=amazon-bedrock-runtime` for Codex; `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` are explicitly never forwarded, and no host credential file is copied. Codex additionally needs the provider configuration in its own config directory, which is read automatically — that `config.toml` must have no `profile` key, because the microVM has no `~/.aws`. When the workstation's own `~/.codex/config.toml` needs a `profile` for local use, keep it and set `CODEX_HOME_TEMPLATE` to a dedicated copy without one; preflight's `codex-provider-config` fix writes it. If the execution-role path is not acceptable for your account, stop here: that is how remote runs authenticate, and there is no per-user alternative.

**Kiro has no execution-role path on any runtime.** Its login is the only credential it can use, and it lives in a SQLite database rather than a config file, so multi-coding-harness-compare will not copy it unless told to. Pick one:

```bash
# Copy this workstation's Kiro login into each run's private HOME.
multi-coding-harness-compare setup --repository-root=/abs/path/to/projects --aws-profile=NAME \
    --artifact-bucket=NAME --runtime=agentcore --deploy --kiro-auth=copy-host-login

# Keep the login off this host; Kiro logs in inside its own dedicated HOME.
multi-coding-harness-compare setup --repository-root=/abs/path/to/projects --aws-profile=NAME \
    --artifact-bucket=NAME --runtime=agentcore --deploy --kiro-auth=login-in-home
```

Use `--runtime=native` in place of `--runtime=agentcore --deploy` to run harnesses locally instead; the Kiro choice is the same either way, because Kiro has no execution-role path on either runtime.

`multi-coding-harness-compare setup` writes `config.toml` and `deploy.properties` and prints JSON. It never prompts: with a decision unresolved it reports that decision, the flag that settles it, and the line it would write, then exits non-zero having written nothing — so an agent driving setup is never left waiting on stdin. `--interactive` prompts instead, but only on a real terminal; without one it falls back to the same report. Add `--force` to replace an existing `config.toml` (the previous file is kept as `config.toml.backup`).

With `--kiro-auth=login-in-home`, setup prints a `next_steps` entry: run `kiro-cli login` with `HOME` set to a dedicated directory and point `harnesses.kiro.home_template` at it. That is a browser device flow a person must complete; it cannot be automated.

Then confirm the result:

```bash
multi-coding-harness-compare doctor
```

`doctor` is read-only and safe to call repeatedly. It reports, per harness, the authentication method, the configuration directory in use, and which secrets it is withholding; plus whether independent evaluation is enabled, whether each `repository_roots` entry exists, and whether the artifact prefix is reachable. It lists every blocker in `blockers` and exits non-zero while any remain.

Without `home_template`, each harness's non-secret configuration comes from the directory its own CLI uses on this operating system — `$CLAUDE_CONFIG_DIR` or `~/.claude`, `$CODEX_HOME` or `~/.codex`, and `~/Library/Application Support/kiro-cli` on macOS or `~/.local/share/kiro-cli` elsewhere. Only an allowlist travels: `settings.json` and `CLAUDE.md` for Claude, `config.toml`, `AGENTS.md` and `AGENTS.override.md` (when present) for Codex. Logins (`.credentials.json`, `auth.json`, `data.sqlite3`) are withheld unless named in that harness's `refresh_files`. This is why moving the checkout to another workstation needs no absolute paths in `config.toml`. An explicitly configured `home_template` is still copied whole, because that directory was assembled for the purpose. A harness's configuration directory is protected from coding runs the same way a configured template is.

**Claude model policy keys are stripped from the run HOME.** After copying or refreshing the Claude settings to the run's private HOME (both the template path and the default `.claude/settings.json` path, and in `inherit_host_setup` mode), multi-coding-harness-compare removes the following keys from `<home>/.claude/settings.json` before the run starts:

- Top-level: `model`, `availableModels`, `enforceAvailableModels`, `modelOverrides`, `fallbackModel`, `modelSettings`, `effortLevel`
- Inside the `env` object: `ANTHROPIC_MODEL`

These keys in a personal settings file would override the model and effort that the comparison configured, causing Claude Code to ignore the picker selection and use whatever the user's own settings prefer. The stripped names are recorded in the run detail under `model_policy_removed`. The file is written atomically and its permissions are preserved; a missing or unparseable file is left untouched.

## Authentication and native configuration

`multi-coding-harness-compare doctor` reports installed CLI versions, configured model IDs, credential-variable presence (never values), and availability of the selected OS sandbox. The adapters perform additional native authentication and capability checks.

**Reuse working macOS CLIs:** set `inherit_host_setup = true` under `[harnesses.claude]` and `[harnesses.codex]`. These are opt-in settings, disabled by default. Enable `inherit_host_setup = true` under `[harnesses.claude]` and/or `[harnesses.codex]` in `config.toml` to activate. Set `cli_path` to the installed launcher's absolute path to keep desktop and terminal service starts consistent; without it, multi-coding-harness-compare searches the service's PATH, which may differ from an interactive terminal. Restart the services after changing configuration. There is no need to automate the Terminal window.

This mode loads existing Claude settings or Codex provider configuration, including Bedrock profiles and configured credential helpers. It refreshes configuration before each run, exposes existing AWS/Midway/Toolbox resources read-only to the sandbox, and shares installed skills/plugins read-only. Claude's `CLAUDE_CONFIG_DIR` and Codex's `CODEX_HOME` point to private run directories, keeping session history, logs and databases separate. Helpers receive the normal macOS HOME so they can locate existing authentication; access to unrelated personal files, competing checkouts, and evaluator data remains denied. Coding harnesses use the same credential authority as their terminal counterparts. Independent verification still runs without credentials.

Use **Native default** or refresh the model lists after switching providers: the existing Bedrock configuration can expose different model IDs from a public-provider installation. Existing comparisons retain their originally requested model IDs. The app reports native provider errors if an old ID is no longer supported.

Codex models can be available in different Bedrock regions. With `inherit_host_setup`, add per-model entries under `[harnesses.codex.model_regions]`, for example `"openai.gpt-5.6-sol" = "us-east-1"`. multi-coding-harness-compare applies that region when launching the selected model and records it in the run's capabilities. Other models keep the region in your installed Codex configuration. Your personal configuration and requested model ID are preserved. Restart the local services after changing this configuration, then refresh the model list.

The alternative is a dedicated setup below. Without `inherit_host_setup`, Claude defaults to the SDK-bundled CLI and each harness uses a private HOME/template. File-backed Codex authentication can be reused in host-setup mode; keyring-only OpenAI login storage may require a dedicated login because Codex keys it to its home directory.

**Claude Code:** set `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` in the worker environment. The pinned `claude-agent-sdk` includes the real Claude Code CLI. `ClaudeSDKClient` supplies the native loop, session resume, permission callback, streaming messages and terminal usage. The adapter loads user, project and local settings and adds the clarification MCP server without disabling other configured servers.

Claude's native Bedrock mode is also supported through its dedicated HOME settings: set `CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION` and `AWS_PROFILE` in `.claude/settings.json` under its `env` object, provision appropriately scoped credentials under that HOME's `.aws/`, and choose an accessible Bedrock model ID in `harnesses.claude.model`. Add `.aws/config`, `.aws/credentials` and `.claude/settings.json` to that harness's `refresh_files` if these files should be refreshed from the template on session continuation. This retains the native Claude Code SDK/CLI loop.

**Codex:** set `OPENAI_API_KEY`, or create a dedicated Codex home and authenticate it using the native CLI:

```bash
mkdir -p .arena/credential-templates/codex
HOME="$PWD/.arena/credential-templates/codex" \
CODEX_HOME="$PWD/.arena/credential-templates/codex/.codex" codex login --device-auth
```

Set `harnesses.codex.home_template` to that directory's absolute path. The adapter uses native `codex app-server` JSON RPC: `initialize`, `account/read`, `thread/start` or `thread/resume`, `turn/start`, streamed notifications, native approval/question requests, and `turn/interrupt`.

**Kiro:** create and authenticate a dedicated home:

```bash
mkdir -p .arena/credential-templates/kiro
HOME="$PWD/.arena/credential-templates/kiro" kiro-cli login --use-device-flow
```

For organization login, use the native `--license pro`, `--identity-provider`, and `--region` options as appropriate. Set `harnesses.kiro.home_template` to the absolute directory. The adapter invokes `kiro-cli acp`, negotiates ACP capabilities, creates or loads sessions, sends prompts, streams session updates, handles permissions, and cancels through `session/cancel`.

With dedicated setup, each run receives a private copy of its harness's dedicated HOME. Its session history and mutable settings are independent. On later attempts, only explicitly configured `refresh_files` are copied again, atomically and with owner-only permissions; session histories are preserved. This lets Codex/Claude pick up newly configured credentials after an authentication-blocked attempt. For an existing Kiro run, use native login with that run's private HOME if reauthentication is needed; do not overwrite its session database with a template. Do not use your entire personal HOME as a template. Coding sandboxes cannot access the host Docker socket, sibling checkouts, evaluator directories, or another harness's HOME.

Native MCP servers can be configured in the dedicated HOME or under `[harnesses.NAME.mcp_servers.SERVER]`. Commands must exist in the selected runtime. For additional native tool installations, explicitly list their read-only directories in `native_read_roots`; use absolute executable paths. The project supplies repository instructions, skills, hooks and project MCP settings. Kiro's `agent` and all harnesses' `model` are optional; an omitted model uses the native default, and observed model information is recorded.

### Permission prompts (`AUTO_APPROVE`)

`AUTO_APPROVE` in `deploy.properties` decides who answers the coding harnesses' own permission prompts; it fills `harnesses.<name>.auto_approve` for all three harnesses. **It is `false` by default. Set it to `true` before deploying** unless you want to approve harness actions yourself:

```bash
./bootstrap.sh --set AUTO_APPROVE=true
```

The AI assistant that deploys the application asks for it together with the required inputs. To change it later, stop the services and start them again (`.venv/bin/multi-coding-harness-compare stop`, then `./bootstrap.sh --set AUTO_APPROVE=false --start`, or `=true`); it needs no `--deploy`.

| Harness | `true` (recommended) | `false` (default) |
|---|---|---|
| Claude Code | Runs in auto mode (`permission_mode = "auto"`): its classifier allows routine actions and blocks risky ones itself, and what it still escalates comes to you as an approval card. Where auto mode is unavailable — a model without it, or settings that disable it — Claude Code runs in default (Manual) mode and its prompts are allowed. | Runs in auto mode the same way, and its escalations come to you. Where auto mode is unavailable, every prompt comes to you. |
| Codex | Codex's auto-review agent (the app starts Codex threads with `approvalsReviewer: "auto_review"`, the desktop app's "Approve for me") answers Codex's approval requests. It fails closed: what it does not approve is denied. | Every Codex approval request comes to you (`approvalsReviewer: "user"`). |
| Kiro | Kiro's permission prompts are allowed, each for that one call (`allow_once`). | Every Kiro permission prompt comes to you, with Kiro's own options. |

Either way:

- Approved actions still run inside the selected runtime's sandbox (macOS Seatbelt or an AgentCore microVM) and are limited by it; see [Security considerations](#security-considerations).
- The setting covers permission prompts only. Clarification questions, which use the clarification bridge, and the shared test review reach you as before.
- With `false`, each prompt waits for your answer, and a run can raise many cards.
- When Claude Code is not in auto mode, the run's activity says so. Codex threads start with `approvalPolicy: "on-request"`. Claude Code and Codex call the app's own clarification tools without an approval round.
- Remote MCP tools retain their own configured authority; provision separate databases, credentials and service namespaces if they access mutable external systems.

A `config.toml` that sets `auto_approve` itself (configurations created before `AUTO_APPROVE` existed set `true` for each harness) keeps that value, because a literal in `config.toml` wins. Preflight stops with exit 40 (`auto-approve-overridden`) while such a line differs from `AUTO_APPROVE`, or sets `true` while `deploy.properties` has no `AUTO_APPROVE` answer, until you choose: keep `true` with `./bootstrap.sh --set AUTO_APPROVE=true`, or delete those lines and set `AUTO_APPROVE` as you want it.

## Harness configuration defaults

Each harness reads non-secret configuration from its own directory on the workstation and copies an
allowlist of files into every run's private HOME. An explicit `*_HOME_TEMPLATE` is copied whole
instead (the template was assembled for this purpose).

**Default copy allowlist** — what is copied when no template is set:

| Harness | Source directory | Copied by default | Never copied |
|---|---|---|---|
| Claude Code | `$CLAUDE_CONFIG_DIR` or `~/.claude` | `settings.json` (model-policy keys stripped), `CLAUDE.md`, `commands/`, `rules/`, `agents/`, `skills/**` (SKILL.md under each skills dir), `output-styles/` | `.credentials.json`, `auth.json`, `.claude/todos/`, log files, `*.sqlite*`, `*.db` |
| Codex | `$CODEX_HOME` or `~/.codex` | `config.toml`, `AGENTS.md`, `AGENTS.override.md` (when present), `prompts/`, `rules/`, `skills/` | `auth.json`, `*.sqlite*` |
| Kiro | `~/.kiro` (config; not the data dir) | `settings/mcp.json`, `steering/`, `agents/`, `skills/`, `hooks/` (if present) | `data.sqlite3`, session logs |

**Never-copy rules** — enforced regardless of allowlist:

- Files named `.credentials.json`, `auth.json`, `data.sqlite3`, `history.json`
- Any file or directory name ending in `.sqlite3`, `.sqlite`, `.db`, `.log`
- Any entry over 50 MB total (the entire home config copy is capped at 50 MB)
- Entries whose path contains `..` or is absolute

**Overrides** — per-harness keys in `deploy.properties`:

| Key | Effect |
|---|---|
| `*_CONFIG_DIR` | Override the source directory |
| `*_CONFIG_INCLUDE` | Comma-separated paths added to the copy allowlist |
| `*_CONFIG_EXCLUDE` | Comma-separated paths (or `mcp:<name>`) removed from the allowlist |
| `*_SKILLS_DIRS` | Comma-separated directories or glob patterns; ~ expanded; SKILL.md files under each are injected |
| `*_STEERING_FILES` | Files, directories, or glob patterns; directories expand to all *.md inside; ~ expanded |
| `*_MCP_CONFIG` | Path or glob to a JSON/TOML/JSON file; ~ expanded; first match used |
| `*_CONFIG_S3_URI` | `s3://bucket/prefix/` synced read-only into the run's HOME config dir |
| `*_INHERIT_HOST_SETUP` | `true`: reuse the installed CLI and provider config (`native` only) |

**Configuration precedence** (lowest → highest): defaults < S3 prefix (`*_CONFIG_S3_URI`) < explicit overrides (`*_SKILLS_DIRS` / `*_STEERING_FILES` / `*_MCP_CONFIG`) < home template (`*_HOME_TEMPLATE` replaces all of the above).

**UI selector** — each comparison submission accepts a `configuration_choice` per harness:

| Choice | Meaning |
|---|---|
| `default` | Apply the allowlist + override keys above |
| `overrides` | Apply only the explicit override keys (`*_SKILLS_DIRS`, `*_STEERING_FILES`, `*_MCP_CONFIG`) |
| `none` | Copy nothing; the harness starts with a bare HOME |

The UI preselects the effective default for each harness: `overrides` on agentcore, `default` on native, or the value of `HARNESS_CONFIG_DEFAULT_CHOICE`.

**Skills / SKILL.md** — Claude Code uses `SKILL.md` files in skill directories under `~/.claude/skills/`; `CLAUDE_SKILLS_DIRS` adds additional directories. Codex uses `.agents/skills/` (`SKILL.md` folders copied from `~/.agents/skills`), `prompts/`, `rules/`, and `AGENTS.md`; the legacy `~/.codex/skills` directory is also copied. Kiro uses `.kiro/skills/` (from `~/.kiro/skills`) in addition to `.kiro/agents/` and `.kiro/steering/`.

**S3 prefix for runs** — under `agentcore`, each turn's HOME and workspace travel through S3:
`<AGENTCORE_TRANSFER_S3_URI>/sessions/<session-id>/<turn-key>/job.tar.gz`. When
`AGENTCORE_ISOLATION=per-user` and `USER_NAMESPACE` is set, the prefix becomes
`<transfer_uri>/<USER_NAMESPACE>/sessions/...`.

**MCP secret detection** — `doctor` and preflight report the *names* (never the values) of
environment-variable keys inside configured MCP servers that look like secrets (contain `TOKEN`,
`SECRET`, `KEY`, `PASSWORD`, or `CREDENTIAL`). The check is a warning; it does not block.

## Upgrading harness images (standalone)

The image component (`harness_image/` and `deploy/agentcore-deploy.sh`) can be run without the
full UI application installed. Any AI coding assistant can deploy without the full API/worker
environment.

### Version sources

| Source | How version is resolved | Approval required |
|---|---|---|
| `pinned` (default) | Constants in `harness_image/versions.py` | No |
| `installed` | `claude --version` / `codex --version` / `kiro-cli --version` | Yes — run `plan` first |
| `latest` | npm registry / PyPI / Kiro CDN | Yes — run `plan` first |

Set `CLAUDE_HARNESS_SOURCE`, `CODEX_HARNESS_SOURCE`, `KIRO_HARNESS_SOURCE` in
`deploy.properties`. Pin a specific version with `CLAUDE_HARNESS_VERSION` etc.; this overrides
the source strategy.

### plan / deploy / status / policy

```bash
bash deploy/agentcore-deploy.sh plan                      # show resolved versions and approval tokens
bash deploy/agentcore-deploy.sh deploy                    # deploy all three harnesses (pinned — no approval)
bash deploy/agentcore-deploy.sh deploy --approve TOKEN    # non-pinned versions need the approval token
bash deploy/agentcore-deploy.sh deploy --harness claude   # single harness
bash deploy/agentcore-deploy.sh deploy --no-rebuild       # re-register without rebuilding the bundle
bash deploy/agentcore-deploy.sh status                    # list deployed runtimes and versions
bash deploy/agentcore-deploy.sh policy --kind execution   # print execution role policy JSON
bash deploy/agentcore-deploy.sh policy --kind deployer    # print deployer policy JSON
bash deploy/agentcore-deploy.sh policy --kind workstation # print workstation policy JSON
```

The script creates `.deploy-venv/` beside itself on first run (requires `uv`) and delegates to
`python -m harness_image`. It is bash 3.2 compatible (macOS default shell) and has no dependency
on the installed UI virtualenv.

### Approval token (non-pinned sources)

When `harness_source = installed` or `harness_source = latest`, the plan step generates a
SHA-256 approval token over the resolved version, URI, and digest. The token is stored in
`DATA_DIR/harness-versions.lock.json` alongside the approved entry. Pass the token to
`--approve TOKEN` when running `deploy`; the deploy step verifies it before proceeding.

```bash
# Example: upgrade Codex to the latest version
./bootstrap.sh --set CODEX_HARNESS_SOURCE=latest
bash deploy/agentcore-deploy.sh plan          # review output, note token
bash deploy/agentcore-deploy.sh deploy --approve TOKEN
```

### Integrity checks

Downloads are verified against their expected digest (SHA-256 for PyPI wheels, SHA-512 for npm
packages). Kiro CDN downloads use trust-on-first-use: the digest is recorded on first download
and verified on subsequent fetches. Only URLs from `HARNESS_TRUSTED_SOURCES` are allowed.

### Lock file

`DATA_DIR/harness-versions.lock.json` records every approved version, URI, and digest.
The preflight `harness-image-versions` check fails (gate: `human`) when any harness uses
`installed` or `latest` source and has no approved lock entry.

### Rollback

Re-pin the version you want and redeploy:

```bash
./bootstrap.sh --set CODEX_HARNESS_VERSION=0.153.4 --set CODEX_HARNESS_SOURCE=pinned
bash deploy/agentcore-deploy.sh deploy --harness codex
```

Rollback is in-place: the runtime is updated immediately. Warm sessions already running
a comparison turn continue until their turn ends; the updated runtime applies to new sessions.
The API and worker do not need to be restarted after a harness image update.

## Remote execution on Amazon Bedrock AgentCore Runtime

Setting `RUNTIME = agentcore` in `deploy.properties` moves harness process execution out of the macOS Seatbelt sandbox and into Amazon Bedrock AgentCore Runtime Firecracker microVMs in AWS. Everything visible to users — the web UI, the CLI, the clarification flow, the comparison table, artifact exports, and independent evaluation — stays unchanged. The only thing that moves is where the harness process runs.

One AgentCore runtime resource is created per harness (claude, codex, kiro). They run in the region and account configured under `[agentcore]`.

### Deployment

Run `multi-coding-harness-compare agentcore-deploy` once to build and register the deployment packages. Add `--harness claude` (repeatable) to deploy a single harness; `--no-rebuild` re-registers an already-uploaded package without rebuilding.

The provisioner builds a per-harness `deployment_package.zip` using S3 direct code deployment — no Docker, no ECR. Each zip contains:

- `main.py`: a minimal HTTP health-check server that satisfies AgentCore's readiness contract. All real harness work arrives through `InvokeAgentRuntimeCommand`, not HTTP.
- Vendored Python wheels for `boto3==1.43.92` and `mcp==1.30.0` (and `claude-agent-sdk==0.2.152` for the claude harness), compiled for `aarch64-manylinux2014` Python 3.12 using `uv pip install --python-platform aarch64-manylinux2014 --only-binary=:all:`.
- For **claude**: the `claude-agent-sdk` wheel ships `claude_agent_sdk/_bundled/claude`; the bundle carries the whole wheel.
- For **codex**: `npm pack @openai/codex@0.154.0-linux-arm64` produces a tarball containing `package/vendor/aarch64-unknown-linux-musl/`; the provisioner extracts that vendor tree into the zip.
- For **kiro**: the Kiro CDN download is 596 MB; only the `kirocli/bin/kiro-cli*` entries are needed. The provisioner downloads the CDN zip, extracts those binaries, then measures zipped and unzipped sizes against 80% of the hard 250 MB / 750 MB AgentCore ceilings. When the binaries fit, they are vendored into the zip. When they do not, the provisioner stages them as a gzip tarball at `tools/kiro-<version>.tar.gz` under the S3 prefix and the in-VM supervisor fetches and caches them on first use in the session. The log records which path was chosen.

In practice, at the pinned versions, claude and codex are vendored and Kiro is staged. Measured: claude 255 MB unzipped / 115 MB zipped, codex 331 MB / 137 MB, and Kiro 1011 MB / 592 MB — well past the ceiling, because `kiro-cli-chat` alone is 828 MB. Staging reduces the Kiro bundle to 48 MB / 24 MB and moves 567 MB into S3, which the supervisor caches at `<cache_root>/tools/kiro-<version>/`. Cache roots are tried best-first — the sessionStorage mount at `/mnt/workspace/mchc-cache/` (durable across a cold microVM), then the job tree, then `/tmp` — and a root is skipped unless it has 2 GB free, because Kiro unpacks to about 1 GB and the current mount is only 1.07 GB in total. Choosing a root that cannot hold the tree fills it mid-extract and the harness starts with no CLI at all (`[Errno 2] No such file or directory: 'kiro-cli'`); a failed extract is deleted rather than left to look like a usable cache, and the next root is tried. In practice today that means Kiro caches on the overlay and the mount is skipped with a logged note. The first Kiro turn after a cache reset pays a one-time download.

After uploading each bundle to `bundles/<harness>/deployment_package.zip` under the configured S3 prefix, the provisioner creates or updates the corresponding AgentCore runtime resource and waits for `READY` status. It then writes the resulting ARNs and bundle URIs to `agentcore-runtimes.json` in the data directory (`.arena/` by default) and prints a ready-to-paste `[agentcore.runtimes]` TOML block.

The provisioner also creates or refreshes the IAM role named by `AGENTCORE_EXECUTION_ROLE_NAME` (default `MchcBedrockAgentCoreRuntimeRole`) unless `AGENTCORE_EXECUTION_ROLE_ARN` pins an existing role. Its inline policy grants CloudWatch Logs, X-Ray, `cloudwatch:PutMetricData` scoped to the `bedrock-agentcore` namespace, `bedrock-agentcore:GetWorkloadAccessToken*`, `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`, plus `s3:GetObject / PutObject / DeleteObject` on the configured S3 prefix and a scoped `s3:ListBucket`. The trust policy pins `bedrock-agentcore.amazonaws.com` with `aws:SourceAccount` and `aws:SourceArn` conditions.

### Switching on

```properties
# deploy.properties (or: ./bootstrap.sh --set RUNTIME=agentcore ...)
RUNTIME = agentcore
AWS_PROFILE = YOUR_AWS_PROFILE
AWS_REGION = YOUR_AWS_REGION
S3_BUCKET = YOUR_BUCKET
AGENTCORE_TRANSFER_S3_URI = s3://${S3_BUCKET}/code/ai-coding-assistant-agcore-runtime/
```

`AGENTCORE_TRANSFER_S3_URI` (`agentcore.transfer_s3_uri`) is required and must name a bucket and a nonempty prefix. `workspace_mount` must be an absolute path under `/mnt`; `job_root` must be an absolute path not under `/mnt` and under one of `/tmp/`, `/var/tmp/`, or `/home/`, the only roots the unprivileged runtime user can write; `cache_dir` is a path relative to `workspace_mount`. Everything else has working defaults. After running `agentcore-deploy`, run `multi-coding-harness-compare agentcore-probe` to confirm those paths are writable in the newly deployed runtimes, then restart the API and worker. When no runtime is found, `agentcore-probe` still prints its JSON report, with `ok: false` and an `error`, and exits 1; `setup --deploy` reports that error as `deployment.error`.

### How a turn executes

1. **Session activation.** Before the first command API call, the host calls `invoke_agent_runtime` with a small JSON payload to warm the cold microVM. A cold session rejects `invoke_agent_runtime_command` with a 500 error; one activation call is enough to make commands work.

2. **Job tarball via S3.** The host builds a gzip tarball containing the run's workspace, bridge directory, HOME directory (the harness's session state), and the live `arena/` Python package source. It uploads the tarball to `sessions/<session-id>/<turn-key>/job.tar.gz` under the S3 prefix, then issues a BOOTSTRAP command.

3. **BOOTSTRAP command.** The command string is `env CC_CODE=<b64> CC_PAYLOAD=<b64> python3 -c "..."`. `CC_CODE` is the base64-compressed BOOTSTRAP source; `CC_PAYLOAD` carries a small JSON blob with the S3 coordinates and job directory paths. The microVM process imports `boto3` from `/var/task` (where the deployment zip is extracted), downloads the job tarball, extracts it to the job directory — removing any existing file before writing its replacement, so a resumed turn in the same session does not fail on a harness-written read-only file such as a git pack `.idx` (mode 0444) — then `os.execve`s into `python3 -m arena.agentcore_agent <jobdir>`.

4. **Streaming stdout and stderr.** The host opens an `InvokeAgentRuntimeCommand` EventStream in a daemon relay thread. Each `contentDelta` event carries `stdout` or `stderr` bytes. Control messages emitted by the in-VM supervisor (`{"type":"__mchc",...}`) are stripped from stdout and dispatched — bridge requests are written to disk so the worker's bridge loop can serve them; `result` records the S3 coordinates of the workspace tarball; `exit` carries the process exit code.

5. **Clarification bridge.** While the harness is running, the supervisor polls `bridge/requests/` for new files and emits them as `bridge_request` control messages, which the relay thread writes into the local run root. A concurrent mirror loop polls for local changes to `bridge/answers/`, `bridge/session.json`, `bridge/cancel.json`, and `control/state.json` and delivers them into the microVM using small `DELIVER` commands in the same live session. One batched DELIVER per poll tick.

6. **Workspace sync.** On exit, the supervisor packs the result and uploads it to `sessions/<session-id>/<turn-key>/result.tar.gz`. A development turn packs the workspace and HOME; a clarify turn packs only HOME, because its workspace is read-only. Returning HOME after clarification means the session each harness created there survives a new microVM (worker restart, the 15-minute idle timeout, session rotation). Regenerable caches (`.cache`, `tmp`) and links to absolute paths are left out of HOME. The relay delivers the coordinates; `stop()` — called in `worker.phase()`'s finally block, before `tree_hash` runs — downloads the tarball and replaces whichever of the local workspace and HOME it contains (only HOME after a clarify turn). The job tarball and result tarball are deleted from S3 after use.

7. **Session lifetime.** The session ID is deterministic: `mchc-<run_id>-<sha256(run_id+root)[:32]>`. Consecutive turns on the same run reuse the same session ID, landing on the same warm microVM with sessionStorage intact.

### The microVM

Each session runs on Amazon Linux 2023 (kernel 6.1 aarch64), 2 vCPU, 8 GB RAM. `git`, `curl`, `tar`, `gzip`, `unzip`, `zip`, `bash`, `openssl`, and `python3 3.12` are present. `node` and `npm` are absent; every harness CLI is a self-contained binary.

Commands run as the unprivileged `agentcore-runtime-user` (uid 991) with `HOME=/home/agentcore-runtime-user` and `cwd=/var/task`. The image roots are root-owned and mode 0755, so `/`, `/opt`, `/mnt`, and `/var/task` are **read-only to the job**. Only `/tmp`, `/var/tmp`, the runtime user's own HOME, and the mounts are writable. `multi-coding-harness-compare agentcore-probe` reports this per deployed runtime; run it after every `agentcore-deploy`, because a redeploy can change the command user underneath a configuration that worked. An unwritable `job_root` fails at `mkdir` seconds into every run, in all three harnesses at once, and surfaces only as `Native process exited 1 without a completed turn`.

Everything — workspace, bridge, tmp, and the harness HOME — lives on the **writable overlay** (10.4 GB total) under `/var/tmp/mchc/<run_id>/`. `/var/tmp` rather than `/tmp` because Codex inspects its own HOME and declines part of its setup when it looks temporary: with `CODEX_HOME` under `/tmp` every start logs `WARNING: proceeding, even though we could not create PATH aliases: Refusing to create helper binaries under temporary dir "/tmp"`. The same probe under `/var/tmp` does not, at no cost in startup latency.

The remote command stream has its own ceiling, separate from `command_timeout`: AgentCore closes it with `Request timed out as the agent didn't have a response byte in last 15 mins`. A harness that streams text continuously never notices, but one that reasons quietly through a turn does — Kiro was cut off mid-clarification after its session and bridge came up cleanly. The in-microVM supervisor therefore writes a control-channel heartbeat every `stream_heartbeat_seconds` (120 by default). Heartbeats ride the `__mchc` envelope, so `RemoteProcess` consumes them and they never become stored events; the run's own `active_seconds` budget stays the thing that ends a turn.

Harness startup is slow on a cold microVM, and the numbers are worth knowing before tuning a timeout. Measured against the deployed runtime, replicating the adapter's own sequence: `codex --version` 0.8–8.6 s, then `app-server` answers `initialize` in **26–27 s** cold and 5.6 s warm — 50.8 s when the binary itself had not been paged in. Every startup call therefore spends the one configured `setup_seconds` budget (180 by default), the same lever `claude` and `kiro` already used for their bridge waits; `codex` previously hardcoded 30 s and lost that race the first time a microVM was cold. A startup call that does run out of budget raises a `HarnessTurnError` naming the method, the ceiling, the elapsed time, and whether the CLI is still alive, and marks itself retryable — a bare `asyncio` `TimeoutError` carries no message, so one that escaped reached the operator as the single word `TimeoutError`.

The **sessionStorage NFS mount** (`/mnt/workspace`, 1.07 GB) holds the durable tools cache and nothing else. It is not used for harness state, because it cannot hold it: it is NFS, and SQLite there fails with `database is locked`. Both Codex and Kiro keep their session state in a SQLite database under HOME, so a HOME on that mount breaks them outright (`failed to initialize sqlite state runtime under …/.codex`). Claude is unaffected, which makes this an easy trap to miss. The same objection applies to the workspace: briefs routinely build apps that create their own SQLite file in the project directory. Session storage is also reset after 14 days without an invoke **and on every runtime version update**, so it cannot be treated as durable state either way.

Persistence across a cold microVM is therefore host-owned rather than delegated to the mount: HOME travels in the job tarball on the way out and in the result tarball on the way back: with the workspace after a development turn, and alone after a clarify turn. Regenerable caches (`.cache`) and scratch directories are excluded, as are links to absolute paths, which the host's `tarfile` data filter would otherwise reject. The practical effect is the same resume behaviour with none of the NFS locking semantics, and no 1 GB ceiling.

### Identity and credential handling

Inbound calls are authenticated with IAM SigV4; the `bedrock-agentcore` client in the host uses the configured AWS profile. Inside the microVM, the execution role supplies all cloud credentials: boto3 resolves them through the standard IAM role mechanism, so the bootstrap can call S3 directly from `/var/task` without any injected keys.

No long-lived provider keys are forwarded to the microVM:

- **claude**: `CLAUDE_CODE_USE_BEDROCK=1` is set and the execution role provides Bedrock access, so no template is needed. `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` are never forwarded.
- **codex**: `MCHC_CODEX_REGION` routes model calls to the configured region, but Codex still needs a provider configuration that selects the Bedrock provider — without one it falls back to OpenAI auth and the turn fails with "Codex authentication unavailable". That configuration must **omit** the `profile` key: there is no `~/.aws/credentials` in the microVM, so the AWS SDK has to fall back to the default chain and pick up the execution role. Without `CODEX_HOME_TEMPLATE`, the workstation's own `~/.codex/config.toml` is copied in and must meet both conditions; otherwise preflight's `codex-provider-config` fix writes a dedicated template under `.arena/credential-templates/codex/` (the data directory is not tracked) and sets `CODEX_HOME_TEMPLATE` to it. `examples/credential-templates/codex/.codex/config.toml` is a commented example of the same settings; replace its `YOUR_AWS_REGION` before use. Whatever `model_provider` you set there must match `MCHC_CODEX_PROVIDER`, because `codex_command()` injects its region override at `model_providers.<provider>.aws.region` and an override naming the wrong provider silently applies to nothing.
- **kiro**: Kiro has no execution-role path on this backend either; it authenticates with its own login, copied into each run's HOME (`--kiro-auth=copy-host-login`) or kept in a dedicated `KIRO_HOME_TEMPLATE` (`--kiro-auth=login-in-home`). Only the region is injected.

Codex reaches OpenAI models through the OpenAI **Responses API on the `bedrock-runtime` endpoint** (`https://bedrock-runtime.<region>.amazonaws.com/openai/v1/responses`), which AWS recommends over the older `bedrock-mantle` compatibility endpoint. Four details are easy to get wrong. The provider id in the template selects the endpoint: `amazon-bedrock-runtime` targets bedrock-runtime, while `amazon-bedrock` targets bedrock-mantle. And bedrock-runtime rejects bare model ids with "on-demand throughput isn't supported", so the model must be an inference profile — `us.openai.gpt-5.6-sol`, not `openai.gpt-5.6-sol`. Additionally, `wire_api = "responses"` must be set so Codex uses the Responses API rather than Chat Completions on this endpoint; and `namespace_tools = false` is required. The preflight `codex-provider-config` fix and `examples/credential-templates/codex/.codex/config.toml` both include all four settings; a manually constructed template must as well. Because that endpoint signs as `bedrock`, the existing `bedrock:InvokeModel` grant covers it and no `bedrock-mantle` permission is needed; the role deliberately omits one so nothing can quietly fall back to mantle.

Command strings issued via `InvokeAgentRuntimeCommand` are written verbatim to the runtime's CloudWatch Logs, so nothing secret may appear in them. The whole command — including the `env CC_CODE=… CC_PAYLOAD=…` assignments that carry the BOOTSTRAP and DELIVER payloads — is logged, and those payloads deliberately contain only S3 bucket and key coordinates, remote paths and control state. Credential material never travels this channel: the harness HOME template is shipped inside the encrypted job tarball, and everything else the microVM needs comes from the execution role. This is also why presigned S3 URLs are not used, even though they would have avoided granting the role S3 access.

The command is prefixed with `env` because the command executor splits argv rather than evaluating a shell: a bare leading `VAR=value` assignment is treated as the program name and the runtime answers HTTP 500.

### Session lifetime and persistence

Each run uses one `runtimeSessionId` across all of its turns. Consecutive turns from the worker call `start()` followed by `launch()`, and `start()` computes the same deterministic session ID, so the microVM stays warm between turns and the job directory, including HOME, is still in place.

The idle timeout (default 900 s) and maximum session lifetime (default 28800 s, eight hours) are configured under `[agentcore]`. If a session expires between turns, the next `session_activation` call starts a fresh one; BOOTSTRAP re-extracts the job tarball, including the most recently returned HOME, so the harness resumes from its prior state. If a native session is gone anyway, Codex and Kiro start a new session on the saved workspace instead of failing the run.

### Independent evaluation

Independent evaluation always runs locally in the macOS credential-free sandbox, even when the coding runtime is `agentcore`. The evaluation sandbox has no AWS credentials and no visibility into the AgentCore session. This separation ensures that the Strands judge and acceptance checks cannot be influenced by infrastructure state inside the microVM, and that a remote-execution failure cannot corrupt verification evidence.

### Honest limits

- A remote turn's wall-clock time is bounded by `command_timeout` (at most 3600 s, default to the run's remaining active budget plus 600 s). That clock runs while the harness is paused waiting for a human answer, because the AgentCore session has no way to distinguish application-level pausing from idle time.
- Codex requires a dedicated HOME template declaring the Bedrock provider (see "Identity and credential handling"). Claude and Kiro need no template on this backend.
- `inherit_host_setup` is a native-only feature; it reads existing macOS CLI configuration and cannot be used with the AgentCore backend.
- Model discovery (`multi-coding-harness-compare doctor`, the model dropdowns) pays a cold-start penalty on first use when the microVM needs activation.
- The deployment package size ceilings (250 MB zipped / 750 MB unzipped) constrain what can be vendored. Large tools are staged separately and fetched on demand inside the microVM.

### Removing a deployment

`deploy/agentcore-cleanup.sh` (equivalently: `python -m harness_image cleanup`) removes everything that `agentcore-deploy` created, driven by `deploy.properties`:

**What is deleted:**
- The three AgentCore runtimes (`<prefix>_claude`, `<prefix>_codex`, `<prefix>_kiro`), any custom runtime endpoints (AgentCore removes each runtime's `DEFAULT` endpoint with it), and the workload identities auto-created alongside them.
- CloudWatch log groups under `/aws/bedrock-agentcore/runtimes/<runtime-id>` for each deleted runtime (pass `--keep-logs` to retain them).
- Versioned S3 objects under `AGENTCORE_TRANSFER_S3_URI` prefix. In shared isolation, the entire prefix is cleared; in per-user isolation, only the `<prefix>/<namespace>/` sub-prefix is cleared and the shared `bundles/` and `tools/` objects are kept.
- The IAM inline policy (`AGENTCORE_INLINE_POLICY_NAME`) and role (`AGENTCORE_EXECUTION_ROLE_NAME`), provided the role's `Application` tag matches `AGENTCORE_APPLICATION_TAG` and its trust policy allows `bedrock-agentcore.amazonaws.com`, and only if no other inline or attached managed policies remain on the role. Skipped entirely when `AGENTCORE_EXECUTION_ROLE_ARN` is set (externally managed role).
- Local state files in `DATA_DIR`: `agentcore-runtimes.json`, `harness-versions.lock.json`, and session records in `agentcore-sessions/` for sessions belonging to deleted runtimes. The rest of `DATA_DIR` (database, comparisons, logs) is never touched.

**What is NOT deleted:**
- Comparison archives (`ARTIFACT_S3_URI` prefix) — pass `--include-artifacts` to also remove them.
- The S3 bucket itself.
- The `aws/spans` CloudWatch log group (account-wide shared legacy span destination).
- Any IAM role that this deployment did not create alone (wrong tag, foreign trust, or extra policies).

**Consent flow:**

Run without `--approve` first to see the plan. On an interactive terminal, the command prints a readable summary to stderr and prompts for the AWS account ID; type it exactly to confirm. In a non-interactive context (CI, piped output), it prints a JSON plan with a `token` field and exits 3. Then re-run with `--approve TOKEN` to proceed.

```bash
# Interactive (TTY): shows plan and prompts for account ID
deploy/agentcore-cleanup.sh

# Non-interactive: prints JSON plan, exit 3
deploy/agentcore-cleanup.sh
# then, after reviewing the plan:
deploy/agentcore-cleanup.sh --approve <token>

# Also remove comparison archives
deploy/agentcore-cleanup.sh --include-artifacts --approve <token>
```

The deployer needs the actions in [`docs/iam/deployer-policy.json`](docs/iam/deployer-policy.json), which now includes the cleanup actions (`iam:DeleteRole`, `bedrock-agentcore:DeleteWorkloadIdentity`, `logs:DeleteLogGroup`, `s3:DeleteObjectVersion`, and others).

## Clarification and specification versions

Every submission atomically publishes all three run records with its comparison, then creates three independently scheduled adapter tasks before any result is awaited. A worker cannot observe a partially created cohort. Each receives an identical copied baseline.

The native backend denies access by default. Each process and its descendants can read only system/runtime files, explicitly configured tool and authentication roots, and that run's workspace and mailbox. Each run has private CLI state, a temporary directory, database path, and eight distinct TCP loopback ports (`PORT` and `ARENA_PORTS`). Coding processes may use HTTP(S) and DNS; they cannot bind or connect to competitors' assigned ports. Unix sockets must be inside their writable directories. Native session files remain private; a Linux Kiro HOME is migrated once to that run's macOS HOME layout.

Native `python` and `python3` commands use the application’s pinned virtual environment. Independent checks always have a credential-free private HOME. Snapshots exclude dependency caches such as `.venv`, `.pydeps`, and `node_modules`, plus Python virtual environments identified by `pyvenv.cfg` and activation scripts regardless of their folder name. The original environment stays in the native workspace; requirements and setup scripts remain in the published code. External source symlinks remain rejected. Dependencies needed for independent checks must be available in the configured verification runtime.

The native worker samples RSS and descendant counts once per second and terminates runs exceeding `memory` or `pids`. `config.example.toml` sets `memory = "2g"` per harness process tree, including CLI, plugin and MCP helper processes. This is an upper limit, not reserved memory; the three concurrent harnesses have separate limits. These are sampled limits, not VM-style hard quotas, and there is no CPU quota. Active-time deadlines still apply, and waiting processes are suspended. Process cleanup tracks descendants and verifies process start times before signalling them. Seatbelt is a local OS sandbox, not a separate kernel or UID. These differences are recorded in runtime reports. One comparison executes at a time by default to reduce memory, with its three harnesses still concurrent. Once all implementations are saved and a comparison is only waiting for test approval, feedback, or a test-stage retry, it releases its execution slot so later comparisons can start. Approval or feedback queues it again under the same concurrency limit; saved code is reused.

Process inspection runs off the worker event loop so busy event handling cannot interrupt an already-completed `ps` command. Concurrent monitoring and cleanup share an in-flight scan. A timed-out scan is retried once, with a five-second deadline per attempt; persistent inspection failures still stop execution. Process exit races during timeout or cancellation preserve the original outcome.

Run the application regression suite from this checkout before restarting services after changes:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The suite covers native process cleanup, concurrent harnesses, cancellation, requirement revisions, resource limits, queue capacity, test approval and feedback, test-stage fix rounds, dependency parsing, and evaluation scoring rules. On macOS it also runs real Seatbelt processes with deterministic CLI peers. It uses temporary databases and files, makes no model calls, and does not modify saved comparisons.

The initial phase enforces read-only project access. Harnesses can inspect it, ask questions via native APIs or the `arena` MCP server, and explicitly signal readiness with the `ready` tool. Readiness is persisted independently of native process cleanup. A completed native turn without readiness is blocked instead of silently starting coding. Harnesses can write their request mailbox; answers, session mappings and orchestrator control files remain read-only.

Claude's startup connects a pending clarification bridge before submitting the task. Its macOS policy permits only GraalVM's process-named signal semaphore, required by the current Toolbox launcher; other named IPC remains denied.

Kiro waits for its session to advertise a running `arena` bridge with both `ask_user` and `ready` before submitting the task, including on resume. Bridge failures or the setup timeout produce a specific startup error. Once the turn begins, Kiro must still call `ready` to finish clarification. Native turns launch a fresh adapter process, so this guard takes effect on the next Kiro turn without restarting the API or worker.

Question records include comparison, specification version, run, native session, native question, turn and mailbox IDs. Exactly matching requirement questions with compatible choices are combined within a specification version; mappings to every originating session are retained. An answer from an older specification is never silently reused for a newly asked question. Every question allows free text. Answers return to the correct pending calls and are added to the shared specification.

Each harness automatically moves from its own clarification into development and independent evaluation. A failed or waiting harness never holds the others at ready. The worker freezes the shared requirements immediately using the same database transaction lock as answers. All harnesses receive identical coding criteria for that version, regardless of planner timing. The acceptance planner and baseline checks run alongside coding, using only those frozen requirements and the original baseline. A finished implementation proceeds to evaluation as soon as its version's plan is available, without waiting for other implementations. While that plan is pending the UI shows **Preparing evaluation**.

A genuinely blocking question pauses only its requesting harness; ordinary implementation choices use documented defaults. An answer or user revision versions the shared specification. Active development resumes against the new version, and a harness already evaluated against an older version automatically continues its existing session while preserving its earlier artifacts and results. Cancellation remains per harness unless the whole comparison is cancelled. No timeout supplies a user answer.

Approval answers affect only the requesting run. Specification revisions remain in the existing comparison. Old attempts, exact prompts, template versions, snapshots and evaluation results are preserved in S3.

If coding finishes but artifact collection fails, **Resume** reuses the completed implementation and continues collection and evaluation. A changed specification still starts a development turn against the new requirements.

`arena/prompts.py` contains the exact mandatory execution instructions, appended to initial tasks, development turns, revisions and retries. Each effective prompt is persisted before runtime setup begins.

## Independent evaluation

New comparisons require human approval of the proposed independent tests. The **Review tests** section shows concrete scenarios, expected results, test data, assumptions and any questions that need answers. **Request changes** sends feedback to the planner and produces a new proposal revision. Answer every displayed question, then select **Approve tests for all 3 harnesses**. Coding can continue during review; after approval, each harness enters a **test stage** to write, run and iteratively fix its own test code using the approved specification. Sending a follow-up after the spec is frozen also increments the specification version and invalidates the current approval; the revised tests need fresh approval before evaluation can run (see **Follow-ups** above).

**Test coverage** controls both proposal breadth and the number of scenarios each harness is asked to cover in its test stage. **Small suite** aims for 5–8 representative scenarios (at most eight, or fewer for a simple task) and at most three essential review questions. It prioritizes main workflows and important failures, keeps fixtures compact, and requires exactly one executable check per approved scenario. Intentional coverage omissions are shown as limitations. **Comprehensive** covers every stated requirement, boundaries, errors and relevant integration contracts, within the existing 80-scenario limit. Both modes preserve independent assertions and human approval; a passing small suite does not establish exhaustive coverage. Supplied validation plans retain all their explicit checks.

The choice is saved with the comparison, each review revision and S3 checkpoints. Existing comparisons without a saved choice retain comprehensive coverage. Under **Review tests → Change test coverage**, select a mode and click **Regenerate test proposal** to request a fresh proposal for the same requirements. This invalidates the old approval and supersedes any pending test stage while preserving implementations and earlier evidence. Finalized tests can be changed after the current evaluation finishes. Reruns and S3 recovery retain the approved scope. The choice does not change the selected planning model or effort.

Approval belongs to one proposal and specification version. Changed requirements or feedback invalidate earlier approval; stale browser submissions are rejected. The specification must cover every approved scenario; proposal errors remain visible for feedback or explicit retry rather than bypassing the review. When the test proposal fails, each harness panel and comparison-table row shows the failure with its cause in plain words (for example, Bedrock sending no data within the planner's stall limit) and a **Details ↓** link to the failure block under **Review tests**. That block shows the full error and the last attempt's model, effort, output limit and timing, and **View attempt record ↗** opens the saved planning-attempt record (`GET /api/comparisons/{id}/test-review/attempts/{n}`), read from local disk or, once archived, from S3. Questions, feedback, approval timestamps, plan hashes and review history are retained with the evaluation evidence. Supplied validation files can be reviewed and approved without a planning model; revising them through feedback requires the configured planner.

After approval, each harness runs a **test stage** in its own resumed session against its saved implementation. The harness writes tests for every approved scenario, records a `.mchc-tests.json` manifest naming the test files and mapping each check to a scenario, and runs the tests. When a test fails, the harness must fix the **implementation**; it must not change, skip or delete tests to make them pass. The app locks the manifest and test files, runs them in its credential-free sandbox, and reports failures back as implementation fix feedback. With `test_fix_rounds = 2` (the default), two additional implementation-fix turns follow the initial write turn.

**Automatic locks and flags:** At lock time, the app also locks every `conftest.py` at any depth, `pytest.ini`, and `tox.ini`, even when not explicitly listed in the manifest's `test_files` (a validation warning is emitted in that case and the harness is prompted to add them). After each fix turn, any newly created `conftest.py`, `pytest.ini`, `tox.ini`, `sitecustomize.py`, `usercustomize.py`, or `*.pth` files are deleted and flagged as `created` violations. Changes to the test-configuration sections of `pyproject.toml` (`[tool.pytest.ini_options]`, `[tool.coverage.*]`) or `setup.cfg` (`[tool:pytest]`, `[coverage:*]`) are flagged as `test_config_changed` violations without restoring those files; implementation settings in those files remain freely editable.

**Manifest limits:** The `.mchc-tests.json` manifest accepts at most 400 checks in total, 10 checks per `scenario_id`, and a `notes` field of at most 2000 bytes UTF-8. Exceeding any limit is a validation error that triggers a repair turn.

**Command confinement (stricter):** A check's command must not invoke a workspace file that is not listed in `test_files`. Path-prefixed arguments (relative paths with separators, `./x`, `/workspace/x`) that resolve inside the workspace must be listed; otherwise a validation error names the path. Bare-name interpreter invocations — `python main.py`, `python3 main.py`, `bash run.sh`, `sh run.sh`, `node x.js` — are also validation errors when the named file exists in the workspace but is not listed in `test_files`. `python -m module` / `python3 -m module` invocations where the module name resolves to a workspace file or package (a `.py` file or a directory with `__main__.py`) are validation errors unless the file is listed. Known test runners (pytest, unittest, nose2, doctest, coverage running a runner) are always allowed. Build tools (make, npm/pnpm/yarn run or test, tox, nox) are allowed only when the tool's defining file (Makefile, package.json, tox.ini, noxfile.py) is listed in `test_files`; otherwise a validation error names the file.

`test_active_seconds = 3600` is the harness's own active time budget across all test-stage turns. It counts only the harness's own active CPU time — the same interval accounting as development. It does **not** include the test agent's review of results, the app's sandbox verification runs, or waiting time, and it is separate from the development `active_seconds` budget.

When the test stage stops early — budget exhausted, all fix rounds used, harness failed to write a valid manifest, or the harness turn itself failed — a structured `stop_reason` is stored and displayed in the harness panel as the failure reason: for example, "The 3600-second test budget ran out during fix round 2 of 2; scenarios S3 and S5 were still failing. Scored the code from fix round 1." When no manifest was ever accepted (not written, still invalid after all repair turns, or budget ran out before acceptance), Accuracy and Tests are 0 for that harness. When the stage stopped after the first successful verification, the last completed round is scored with rated scores. An infrastructure failure (sandbox error or AgentCore runtime error not caused by the harness) during a fix turn **after the lock exists** also scores the last completed round with rated scores; the stop reason code is `infrastructure_error` to identify the honest cause, and the run is **not** `evaluated_incomplete`. The `evaluated_incomplete` outcome applies only when no manifest was ever accepted and infrastructure prevented the stage.

After the test stage, the same test agent (planning model) reviews each harness's test files and the app's verified results, and rates every scenario 1 (fully faithful to the specification), 0.5 (partial), or 0 (unfaithful or missing). These ratings drive the Accuracy and Tests scores: an acceptance criterion is met only when its checks pass and its scenario's rating is 1; group scores use per-check rating multipliers. The test agent also receives a unified diff of non-locked (implementation) files between the implementation snapshot and the final test snapshot; it records a concern when implementation changes appear to special-case the specification's test data rather than solving the task generally. If the test agent review fails (timeout, output limit, invalid response after retries), Accuracy and Tests are incomplete (`evaluated_incomplete`), never 0, and the verified results remain visible with the failure reason.

For existing comparisons with a frozen shared plan (legacy), evaluation is unchanged. Existing unreviewed validation plans retain their original scoring rules. File-discovery filters inside command substitutions are distinguished from pipelines that hide a test command's exit status.

Dependency discovery excludes shell redirections such as `2>&1`, so file descriptors and log destinations are not treated as package names during offline wheel preparation.

For an existing comparison, choose **Review tests for saved code**. After a reviewed evaluation finishes, **Request changes** starts another test-only review. Both paths retain generated implementations and previous results; approval re-runs the test stage and evaluation on the saved code. They do not restart coding harnesses. Shared-task revisions still update the implementation requirements and require fresh test approval. Pending reviews survive worker restarts, and review wait time is reported as user wait.

The worker builds a task-specific Strands `Case` from the original task, shared answers, frozen rubric, baseline hash, candidate snapshot and diff, independently executed commands, native usage records and timestamps. There is no reference patch or external benchmark dataset.

Click a harness's **Test outcomes** count to open its evidence directly. The view lists passed and failed checks, commands, exit codes, reported test counts, and recorded failure reasons. Failed checks expand their full output automatically.

The recommendation beneath the table prefers implementations that satisfy every required check, then uses their overall scores. It identifies ties and incomplete comparisons, and avoids ranking results produced by different judges.

Set `JUDGE_MODEL` in `deploy.properties` (it uses `AWS_REGION` and `AWS_PROFILE`, or `judge_region`/`judge_profile` literals in `config.toml`) for a Bedrock model accessible to the **worker's** AWS credentials. Judge credentials are not inherited by coding processes. The Strands planning agent sees only the original task, answers and baseline context before any candidate implementation is reviewed. It creates acceptance criteria and a plain-language test specification (the approved scenarios), with explicit gaps where automatic testing is impractical. The planner is not a coding harness and does not produce candidate implementations.

Without planning-model access, provide the optional validation-plan JSON in the chat composer. The fallback identifies available repository checks and marks acceptance coverage unavailable. It never assigns correctness based on task narratives.

Set `PLANNING_MODEL` in `deploy.properties` (or `planning_model` in `config.toml`) to select an independent test planner; when unset, the effective planning model falls back to `judge_model`. The new-comparison planning-model picker in the UI lets you override the configured default per comparison; see **Planning-model picker** above. The default configuration (`deploy.properties.example` sets `PLANNING_MODEL = us.anthropic.claude-opus-5`; `config.example.toml` sets `planning_effort = "highest"`) uses `us.anthropic.claude-opus-5` at maximum effort for both human-review proposals and approved test development. Before first use, verify that this model is an active cross-region inference profile in your AWS account and region — the preflight `bedrock-model-PLANNING_MODEL` check will fail until it is. Because `planning_effort` is set, the planning model must be a Claude model (its ID must contain `anthropic.claude`); the planning-model picker enforces this by filtering the list accordingly. `"highest"` selects the highest effort level the chosen planning model reports in the fetched Claude model list — `max` for all current Claude models; pin a level such as `"xhigh"` when you want a fixed ceiling. Effort enables Claude adaptive thinking, omits temperature, and stays enabled during structured-output repair. `ultracode` is a Claude Code mode (xhigh plus workflow orchestration), not a model API effort level, so it does not apply to the planner. It does not change the comparison's selected scoring judge or coding harness models. Restart the API and worker after changing configuration.

`planning_max_tokens` sets the planner's output ceiling per response (100,000 in `config.example.toml`). Acceptance planning has no wall-clock budget: an attempt ends when the planner answers, when it reaches that output ceiling, or when its connection stalls — no data from Bedrock for 900 seconds — and there is no `planning_seconds` setting. The planner requests adaptive thinking with `display: summarized`, so a model reasoning at high effort streams its summarized thinking and keeps the connection alive; with the default (omitted) display, an Opus 5 planner at effort `max` sent nothing for over 300 seconds and was cut off before writing its plan. The removed deadline was shared by every attempt, which made the retry the code itself mandates unaffordable — one response that ran the full 100,000-token ceiling at effort `max` was measured at 1,188 seconds, leaving 612 of 1,800 for its continuation; the continuation ran out of time, the finished proposal and the human approval were discarded, and all three harnesses stalled at `waiting_test_review` reporting only the elapsed deadline. Planning is therefore bounded by output tokens and attempt count (three) rather than by time. Its transport keeps that 900-second stall guard, which with a streaming planner bounds silence between chunks rather than the response; a non-streaming planner has no chunks and is still bounded by it. The planner requests a compact structured result and records its model, effort and attempts. Increasing the limit does not regenerate an already frozen rubric or fill missing historical scores. An explicit retry with a different planner model or effort generates a new draft while retaining the saved human approval.

Generated plans include a frozen, stdlib-only `_mchc_observe.py` helper beside their acceptance tests. It observes real Python constructor calls in the test's current thread without replacing SDK classes or bypassing validation. Tests exercise the public entry point and stub external service boundaries, with temporary synthetic AWS profiles when needed. The helper travels with the frozen plan and S3 artifacts, so later verification does not require files from a previous local run.

`examples/demo-task.json` is a complete validation-plan example:

```json
{
  "criteria": [
    {
      "id": "behavior",
      "requirement": "The behavior explicitly requested by the user",
      "critical": true,
      "weight": 1,
      "check_ids": ["acceptance"]
    }
  ],
  "checks": [
    {
      "id": "acceptance",
      "group": "acceptance",
      "command": "python /acceptance/test_behavior.py -v",
      "mandatory": true,
      "weight": 1
    }
  ],
  "acceptance_files": {
    "test_behavior.py": "... actual task-derived test source ..."
  }
}
```

The four predefined validation groups are acceptance (50%), regression (25%), candidate tests (10%) and project checks (15%). A plan can explicitly set positive weights before freezing. Missing required groups remain unavailable. A genuinely inapplicable regression, candidate, or project group must be listed in `excluded_groups` with a reason before verification; it earns no points, and the remaining test weights are explicitly normalized and displayed as `effective_weights`. Acceptance cannot be excluded. For example, an empty baseline can exclude regression instead of adding an always-passing placeholder.

`gaps` records missing evidence for required behavior and keeps accuracy incomplete. `limitations` records the boundary of the agreed verification scope, including live calls explicitly waived in shared answers; these do not invalidate otherwise complete offline evidence. Inapplicable checks are not product requirements and must not become weighted criteria. The planner validates Python syntax and rejects placeholder commands, hidden exit failures, whole-Strands replacements, and scored criteria mapped only to project checks. It can correct detected test problems across its three planning attempts before any candidate is examined.

To update a supplied plan after clarification, send `validation_plan` with `POST /api/comparisons/{id}/revisions`. Otherwise newly clarified requirements are explicitly unverified. An invalid revised plan is rejected before answers or versions change.

The actual components are in `arena/evaluation.py`:

- **CandidateTestRunner** runs actual commands in a credential-free OS sandbox on a disposable copy of an immutable snapshot. External network access is disabled; native test services can use their assigned private loopback ports. Acceptance files and original regression tests are read-only. `/workspace`, `/acceptance`, `/regression` and `/reports` are logical roots; the native runner materializes physical paths in copies of the tests and commands, recording the mapping and hashes without changing frozen originals. Commands start in `/workspace`; generated tests and helpers belong in `/acceptance`, and `/reports` is a separate writable directory. The same plan, runtime fingerprint and limits apply to candidates and the baseline. Commands, exit codes, logs, timestamps, environment details, JUnit results and recognized counts are retained. Unknown counts remain null; zero discovered tests and skipped tests do not count as passes.
- **TaskAccuracyEvaluator** implements the Strands `Evaluator` interface. Criterion attainment is met (1), partially met (0.5), unmet (0), or unverified. Correctness requires independently executed acceptance evidence, with references to checks and the stored code artifacts. Diff inspection and Python syntax-tree inspection add concrete file references, changed/unchanged symbols and imports; this static review supplements executable evidence without claiming semantic proof. Candidate-authored tests alone cannot establish correctness. Missing evidence makes accuracy incomplete without removing criterion weights.
- **TestResultsEvaluator** scores actual runner evidence using frozen groups, weights and mandatory checks. Strands `test_pass` is a result of this logic; it does not execute tests.
- **Relevance and simplicity** uses the supported Strands `OutputEvaluator` with a fixed, evidence-requiring rubric, the actual task, baseline context and candidate changes. Harness identity is omitted from judge input. A missing judge or an output without file references makes this component unavailable.

The Strands experiment callback only returns stored artifacts, wrapped in the supported `TaskOutput.output` field. Displayed accuracy and test results are extracted from the actual structured Strands report. It cannot restart a coding harness or generate another implementation.

Choose **Judge model · US Bedrock inference profile** before starting a comparison. The dropdown reads every page of Bedrock inference profiles in `judge_region`, using `judge_profile`, and includes only IDs beginning with `us.`. It groups entries by provider; global profiles, other geographies, and direct foundation-model IDs are excluded. Foundation metadata identifies non-text or inactive models, which are disabled with a reason. Discovery uses metadata APIs only, caches for ten minutes, and has a separate **Refresh judge models** button. Catalog metadata does not guarantee IAM invocation access or Converse/tool compatibility; Bedrock reports those errors when scoring. The first picker option shows the configured default (`Configured default · <JUDGE_MODEL>`) and is always enabled; leaving it selected submits no explicit judge ID and the service resolves the configured default, validating it against the catalogue. The `JUDGE_MODEL` configured default is validated the same way as an explicit pick: the catalogue must have been fetched with the current configuration fingerprint, and the model must appear in it and be selectable. When `model_id` is null and the comparison already has a stored judge (reevaluate/reverify paths), the stored judge is reused unchanged.

Judge selections, region, profile, and streaming capability are saved with each comparison and shared by all three implementations. No worker restart is needed to change a selection. The acceptance planner continues using the configured planning model and its existing output limit. Judges use their model's default generation parameters so a universal temperature or token limit does not conflict with provider constraints.

Set `PLANNING_MODEL` in `deploy.properties` to select an independent test planner; when unset, the service falls back to the judge model. The planning model is validated against the same judge catalogue as the judge: it must appear in the catalogue and be selectable; when `planning_effort` is configured it must also contain `anthropic.claude` in its ID. The resolved model and concrete effort level are stored on the comparison as `planning.model` and `planning.effort` (never `"highest"` — that is resolved to the highest level the model reports, e.g., `"max"` for current Claude models, before storage). `planning.effort_basis` records how the effort was chosen. Every planner call site in the worker — proposal drafting (`propose_tests`), test-agent review of per-harness results after the test stage, and plan preparation for a reevaluate path — uses the comparison's stored planning record when present, via `config.planning_for_comparison(comparison)`, so changing the configured default mid-comparison does not affect an in-progress plan. `GET /api/system` reports `evaluation_planner` (the configured default model, effort value as written in `config.toml`, and output limit) and `planning_requires_claude: true` when `planning_effort` is set.

For a saved comparison, choose a model under **Independent comparison**, then click **Re-score with selected judge** to update all results. The UI identifies the judge that produced the displayed scores until the new results arrive. **Inspect run → Re-score saved evidence** retries only that harness, retaining the others' results; judge timeouts provide an explicit retry message. The selected judge is also used by **Re-run checks on saved code**. `multi-coding-harness-compare reevaluate COMPARISON_ID` uses the comparison's saved judge, falling back to configuration for older comparisons. This verifies the saved snapshot hash and reuses recorded command evidence. `multi-coding-harness-compare reverify COMPARISON_ID` executes fresh independent tests and judges the same saved implementations.

Evaluator version 9 also repairs execution paths in saved plans: literal Python/pytest filenames that exactly match generated acceptance files resolve under `/acceptance`, and pytest JUnit reports go under `/reports`. Selectors, assertions, approved scenarios and frozen plan hashes remain unchanged. Evidence records the original command, executed command and path adjustments. Runner preflight checks report writing; missing generated files, pytest setup/collection failures and missing requested reports leave verification unavailable rather than counting as failed application behavior. Genuine assertion failures remain failed checks. To recover from these evaluator errors, restart the idle worker with the updated code and choose **Inspect run → Re-run checks on saved code**. This restores the saved comparison from S3, executes fresh checks and scoring, then archives the new results and removes temporary files.

To correct defective evaluation tests without asking the coding harnesses to generate new code:

```bash
multi-coding-harness-compare reverify COMPARISON_ID --plan corrected-plan.json --reason "Correct broken evaluator mocks and honor the agreed offline scope"
```

The correction becomes a separate evaluation revision shared by the candidates at the same specification version. It preserves the original frozen plan, code snapshots, native usage, and previous evidence/verdicts. The new case references its previous case and records the correction reason. `examples/weather-validation/build_plan.py` generates the reviewed offline plan for the weather-task example, including both temperature-unit paths, requested API aliases, forecast lengths, unknown locations, real SDK construction, and CLI invocation.

Previous evaluations remain in the audit history. The raw Strands report's average covers the evaluators it ran; the application's weighted overall score remains unavailable until all three required components are assessable. The UI shows **Evaluation complete** whenever evaluation finishes with available scores, regardless of how many checks pass; scores and check outcomes describe the implementation's performance. The saved audit states retain that distinction: `evaluated_failed` means verification finished with available scores and unsatisfied requirements; `evaluated_incomplete` means required evidence or a score is unavailable. `verified_complete` requires the checked requirements and mandatory checks to pass.

Displayed scores are normalized to 0–100 and retain raw Strands outputs:

```text
accuracy = 100 × Σ(weight × attainment) / Σ(weight)
overall  = .45 × accuracy + .20 × relevance + .35 × test_score
```

Every required component must be assessable for an overall score. Critical unmet or unverified criteria, mandatory validation failures and incomplete evidence prevent `verified_complete`. Passing listed checks is never presented as proof of all possible behavior.

Validation runs without external network access. Each native evaluation gets a private Python virtual environment with `python`, `python3`, `pip`, `pip3`, and read-only access to the application's installed packages. Preparation considers candidate requirements files, packages named in plan install commands, and the plan's `python_requirements` list for evaluator tools/plugins. Standard pytest commands and imports are detected automatically. Only binary PyPI wheels are downloaded; preparation never builds candidate source distributions or accepts direct package URLs. Explicit project checks install candidate dependencies offline into the private environment.

Evaluator tools are installed and checked before candidate checks. Pytest also executes a small environment probe outside the scored suite. A local configuration boundary prevents it from loading multi-coding-harness-compare's own configuration while preserving the candidate's pytest settings. Setup failures and invalid/missing test selectors remain infrastructure errors; real assertion failures remain failed checks. Verification gaps identify the affected check IDs, with full output under **Test outcomes**. Evidence records tool versions, setup output, requirement files and wheel hashes. The application environment and other runs remain read-only. Projects requiring browsers, other language toolchains, services or databases still need those tools prepared explicitly. Original regression test sources should be invoked from `/regression`.

Candidate-authored tests can run as `scored: false, mandatory: false` supplementary checks even when the candidate group is excluded from common scoring. Missing optional tests are **Not applicable** and earn no points; their actual failures remain visible. Native CLI tool-execution records can support a requirement to attempt live verification during development. Those records are mounted read-only through `MCHC_VERIFICATION_CONTEXT`, exclude final harness narratives, and are labeled historical; independent checks must still establish current saved-code behavior. The evaluator never creates an artificial credential failure and then demands that the candidate documented it.

The Bedrock judge receives task requirements, candidate code/diff and selected test outcomes. Host paths, runtime metadata, native logs and credentials are excluded from its request. The report records its exact request hash. A recorded judge response can be reused internally only when its request hash and model match; this recovery path never accepts candidate-supplied scores. `multi-coding-harness-compare reevaluate COMPARISON_ID --spec-version 1` evaluates archived version-1 artifacts without assigning their scores to a later revision. The review dialog also exposes these version-specific actions.

Judge scoring allows `judge_seconds` per attempt (300 seconds by default), with at most `judge_max_attempts` attempts (`config.example.toml` sets two: the initial attempt plus one retry; the built-in default is three; configurable from one to three). Timeouts and transient provider/network failures retry automatically with exponential backoff plus jitter (2–4 seconds, then 4–8 seconds), using the same selected model and saved evidence. Empty, malformed, out-of-range, non-finite, or uncited assessments also retry, with specific correction feedback and the allowed candidate paths. A model call is reported successful only after its response passes validation. Rejected responses, validation reasons, retry delays, and per-attempt request hashes are retained in the judge report. Authentication and invalid-request errors stop immediately. Bedrock connection/read timeouts are explicitly bounded; scoring never restarts a coding harness or its tests. Attempt progress appears in the harness activity, and durations/outcomes are retained in the judge report. The judge can use Strands' `EvaluationOutput` response-formatting tool, but has no executable tools. `judge_seconds` bounds judge attempts only; the planner is not time-bounded.

Judge explanations may cite a full candidate-relative path or a filename that uniquely identifies one saved candidate file. Resolved paths are recorded as relevance `evidence_files`; ambiguous filenames and references to files outside the candidate cannot satisfy this evidence check.

Correction feedback asks the judge to check the supplied source before claiming that code or integrations are missing. The citation validator checks file identity and score validity; it does not establish the factual accuracy of every statement in a judge's explanation.

## Lifecycle, usage and exports

The sidebar reopens durable comparison history. Each harness has independent progress and expandable native/tool logs. Inspect a run to see immutable files, its diff, command output, criterion explanations, model/capability information, effective prompts, timing and raw usage. Resuming a run archives its prior artifacts/evaluation and clears stale current scores and evidence; previous snapshots and evaluations remain expandable in the review and included in exports. Download each patch or export the complete JSON report.

Raw Strands verdicts, reasons, and scores remain in the evaluation report. Their input and candidate fields reference the saved `case.json` artifact, avoiding repeated copies of source code and diffs. Live status events carry state changes and artifact paths; the UI fetches the current run for full evidence.

Cancellation works per run or per comparison. The worker first uses native cancellation, then terminates its tracked process tree. Partial implementation artifacts are collected when files changed.

SQLite WAL is the local runtime queue and UI index for runs, sessions, questions, events, attempts and scores. The durable artifact destination is `ARTIFACT_S3_URI` in `deploy.properties`; no bucket is assumed. It is reached with `AWS_PROFILE` in `AWS_REGION`; with no profile the normal AWS credential chain is used. S3 access is required before a comparison can finish. Configure bucket encryption and restrict the storage principal to this prefix; the client uses HTTPS and does not create public ACLs or change bucket policies.

During comparison execution, baselines, working checkouts, harness sessions, generated project copies, immutable snapshots, patches, plans, test evidence and reports are staged under `.arena/comparisons/COMPARISON_ID/`. Native short-path scratch directories under `/private/tmp/arena-*` have persistent ownership records. Generated `claude/`, `codex/`, and `kiro/` copies are staged inside this temporary comparison directory while the comparison runs. When a run finishes, its deliverable is published as `claude/`, `codex/`, or `kiro/` inside the selected project folder, or inside `projects/<comparison_id>/` beside the data directory for an empty project.

Before publishing completion, cancellation, interruption, or an idle test-review pause, the worker stops native processes and drains file writers, uploads artifacts, verifies S3 checksums and lengths, commits a manifest/index, and deletes the temporary files. Uploads stream directly into S3 without making a local tar copy. A failure displays **Artifact storage failed**, retains uncommitted local files, and does not report the comparison complete. **Retry S3 storage** in the UI or `multi-coding-harness-compare archive COMPARISON_ID` retries transfer without repeating coding or evaluation.

On restart, the worker terminates owned orphaned native processes and marks uncertain execution interrupted. Before its next comparison, it uploads leftovers from stopped runs, finishes interrupted evictions without replacing a complete archive with a partially deleted tree, and handles abandoned hydration/creation directories. A recovery failure blocks new execution until the old artifacts can be secured. Explicit **Resume** continues the saved session and workspace; an uncertain in-flight turn is never silently duplicated.

The **S3 artifact readiness** panel above **Review tests** refreshes automatically. It shows comparison archive counts, the current recovery phase, confirmed uploaded bytes, and any recovery error. It displays **S3 artifacts ready / Ready to retry** only after recovery finishes, the selected comparison is archived, and temporary files are removed. A green worker indicator alone does not establish readiness. Saved-code retries, failed test-development/proposal retries, and rescoring are disabled while readiness is pending; the API also rejects premature requests with HTTP 409 without changing approval or queued work. Initial test approval can still proceed during active coding. **Retry S3 recovery** retries storage checks and cleanup without running a comparison. Readiness is tied to the current worker instance, so offline or restarted workers cannot reuse stale success. Restart the API and worker after installing this change; existing transfers should finish first.

**Resume**, comparative revisions, **Re-score saved evidence**, **Re-run checks on saved code**, and test-review approval restore and validate the required comparison files from S3. Each completed operation publishes a new immutable archive generation and evicts its temporary files again; previous generations and evaluation histories remain available. File inspection and patch downloads use checked S3 range reads without restoring the comparison directory. The UI shows the S3 archive and member paths after eviction.

Archive layout:

```text
s3://YOUR_BUCKET/output/mchc/        (ARTIFACT_S3_URI)
  comparisons/COMPARISON_ID/latest.json
  comparisons/COMPARISON_ID/GENERATION/manifest.json
  comparisons/COMPARISON_ID/GENERATION/artifacts.tar
  artifacts/extra-PATH_HASH/latest.json
  artifacts/extra-PATH_HASH/GENERATION/{manifest.json,artifacts.tar}
```

Every comparison archive includes `_state/comparison.json` and `_state/events.jsonl`. `multi-coding-harness-compare import-comparison COMPARISON_ID` rebuilds the local comparison index from the configured prefix, without downloading the workspace. Resume, evaluation, and report commands also import missing comparison records automatically. Current native CLI installations and authentication remain required for coding continuation. Credentials, configuration templates, lock files, process ownership, the active database, and bounded active service logs are application runtime state and remain local. Kiro conversations are archived separately from its local authentication tables.

The worker also migrates marked legacy project output folders and their history, retaining unmarked source files. Additional unrelated artifact files/directories are explicitly listed in `artifact_paths`, relative to the parent of `data_dir` unless absolute. Add any such paths to `artifact_paths` in `config.toml` (default is empty) to include them in the migration; `deploy.properties.example` documents the field. Application code, tests, configuration, the demo's source fixture, credential templates, and runtime state are protected from that migration. With the worker stopped, `multi-coding-harness-compare archive-artifacts` performs the same recovery/migration immediately; otherwise it runs automatically at worker startup and before the next comparison.

S3 storage and request charges apply. Immutable generations intentionally retain previous comparisons and reruns; this change does not install a lifecycle policy or delete prior S3 generations. The required object permissions are `s3:PutObject`, `s3:GetObject`, `s3:GetObjectVersion` (for versioned objects), `s3:AbortMultipartUpload`, and `s3:ListBucketVersions` on the configured prefix, plus the relevant KMS permissions if the bucket uses a customer-managed key. The full workstation AWS profile action list (S3, Bedrock, STS, AgentCore data-plane) is in [`docs/iam/workstation-policy.json`](docs/iam/workstation-policy.json); the execution role's policy (used inside the AgentCore microVM) is in [`docs/iam/execution-policy.json`](docs/iam/execution-policy.json).

Timing records include queue/setup, clarification, explicit user/approval wait, active development including tool execution and harness-side checks, independent evaluation, barrier wait and total elapsed time. Time after a lost worker's last observation is reported separately as **unobserved**, rather than invented active time. Possible native execution during that gap is conservatively reserved against the active budget. Active deadlines use a monotonic clock. Retries retain their previous intervals and use the remaining cumulative active-time budget.

Native usage sources are recorded independently:

- Codex's cumulative thread counters use per-session high-water marks. Cache and reasoning subsets are not added to the native total.
- Claude terminal-result usage is deduplicated by result/turn ID. When available, `model_usage` supplies the documented whole-turn aggregate including subagents. Its disjoint input, output and cache buckets are summed once across models, without adding top-level or streamed usage again. Older/missing aggregates fall back to top-level turn usage with delegated coverage explicitly unavailable.
- Kiro's `_kiro.dev/metadata` notifications report context occupancy and per-turn credit charges. multi-coding-harness-compare retains them, sums the latest credit snapshot once per recorded turn, and displays credits separately. When native input/output token counters are absent or zeroed, tokens remain unavailable; neither credits nor context occupancy is converted into tokens.

Cancelled turns can lack final usage, and Codex child-call aggregate coverage is not independently guaranteed. Measured totals are conservatively labeled **partial**; absent totals and unreported individual counters are **null/unavailable**. Planner usage is separate. The public `OutputEvaluator` does not expose judge token counters, so judge usage is explicitly unavailable and excluded from coding totals. Efficiency never affects quality scores.

## Demonstration and current environment result

> **Note: historical result.** The comparison ID, harness outcomes, artifact paths, and narrative below are specific to the codebase author's own deployment session and are not reproducible on a new machine. On a fresh deployment where all three harnesses are authenticated, `multi-coding-harness-compare demo` creates a new comparison (a different UUID) and all three harnesses should produce working implementations. The `demo-output/` directory does not exist in a clean clone; it is created only after running the demo and will be migrated to S3 by the worker on first startup if you add its path to `artifact_paths` in `config.toml`.

```bash
multi-coding-harness-compare demo
multi-coding-harness-compare export COMPARISON_ID --output comparison-report.json
```

The demo asks each harness to implement a standard-library Python slugifier while preserving an existing function. `examples/demo-project` is its immutable starting point. Independent acceptance and regression checks are embedded in `examples/demo-task.json`; candidate-added tests are executed separately.

The historical `demo-output` artifact records comparison **`ad37fc11f1b64a16b2609ceb7883cb17`**. This directory and the weather-validation artifacts are migrated to the S3 `artifacts/` namespace by the configured migration; paths below identify members of those archived directories. All three initial adapter tasks were dispatched within **20 ms**, before any individual result was awaited. The first attempts encountered Docker disk-space failures; after runtime recovery, the same comparison resumed with its recorded attempts and sessions.

| Harness | Actual outcome | Accuracy | Test score | Relevance / overall |
| --- | --- | --- | --- | --- |
| Claude Code | Native SDK started; blocked by missing login | unavailable | unavailable | unavailable |
| Codex | Native app-server started; blocked by missing credentials | unavailable | unavailable | unavailable |
| Kiro 2.21.3, native model `auto` | Produced a real implementation; all five independent commands passed | 100 | 100 | 100 / 100 |

Kiro's immutable implementation and patch are in `demo-output/kiro/`. Independent evidence includes seven shared acceptance cases, the protected existing regression case, 13 discovered project tests, and successful compilation. The regression behavior is also run as an acceptance check; these counts overlap and are not a count of unique behaviors. Raw Strands accuracy/test scores are `[1.0, 1.0]`; evaluator version 3 also records static source observations and enforces declared verification gaps. An earlier callback-shape error is retained as a superseded evaluation in the audit history. Re-evaluation consumed only the saved artifacts.

**The three-way comparison remains incomplete.** Claude and Codex did not produce implementations because their native logins were unavailable. Kiro's version-1 implementation subsequently received an actual Strands Bedrock relevance score of 100, giving its weighted overall score of 100. All coding token totals remain unavailable: Kiro exposes credits/context telemetry, not a verified task-token total. Its original active development interval was approximately 43 seconds; timing includes recovery and interruptions and is not a controlled efficiency benchmark.

An earlier proposal to provision temporary Claude credentials was rejected and never executed. The user later explicitly requested a Strands Bedrock judge using an existing AWS profile. After inspecting and minimizing the exact synthetic demo payload, the judge call was approved and completed using `us.anthropic.claude-sonnet-4-6`. No new AWS credentials were created or supplied to coding harnesses. `demo-output/kiro/judge-request.json` and `bedrock-judge.json` preserve the actual request and response. These scores belong to the archived version 1, while the live comparison's later revision remains separate.

`demo-output/native/` records a new demonstration using macOS Seatbelt with Docker Desktop stopped. Kiro completed clarification, resumed its native session for implementation, and passed every independent check. Claude and Codex reached their native authentication checks and reported missing logins. Runtime versions, attempts, native events and resulting evidence are included.

See `demo-output/README.md`, `comparison-report.json`, the frozen specification, command logs, native event journal and portable artifact manifest. Configure Claude/Codex credentials, restart the worker to pick up environment changes, then use **Resume** on those blocked runs. Configure `judge_model` and use **Re-score saved evidence** for the existing Kiro candidate. No placeholder implementation is supplied for unavailable harnesses.

## Integration provenance and limits

Official sources read on 2026-09-11:

- [Strands Evals repository and supported custom evaluator/experiment APIs](https://github.com/strands-agents/evals)
- [Strands evaluator output types](https://github.com/strands-agents/evals/blob/main/src/strands_evals/types/evaluation.py)
- [Strands OutputEvaluator](https://github.com/strands-agents/evals/blob/main/src/strands_evals/evaluators/output_evaluator.py)
- [Codex app-server documentation](https://developers.openai.com/codex/app-server/) (redirected to the official ChatGPT learning documentation)
- [Official Codex macOS Seatbelt policies](https://github.com/openai/codex/tree/main/codex-rs/sandboxing/src)
- [Claude Agent SDK Python](https://platform.claude.com/docs/en/agent-sdk/python) and [official Python SDK source](https://github.com/anthropics/claude-agent-sdk-python)
- [Claude SDK token accounting and whole-agent-tree usage](https://platform.claude.com/docs/en/agent-sdk/cost-tracking)
- [Kiro ACP documentation](https://kiro.dev/docs/cli/acp/), [Kiro installation](https://kiro.dev/docs/getting-started/installation/), and [ACP prompt protocol](https://agentclientprotocol.com/protocol/v1/prompt-turn)

CLI versions vendored into the AgentCore deployment packages (and inspected on the author's machine for the native runtime): Codex 0.154.0, Kiro CLI 2.21.3. On `RUNTIME = native`, the versions on the operator's PATH are used; `multi-coding-harness-compare doctor` reports the installed versions at runtime. Installed SDK dependencies: Claude Agent SDK 0.2.152 and Strands Evals 1.2.0. Codex app-server remains an evolving protocol: its installed JSON schemas were inspected, and the adapter fails visibly for unknown server requests instead of inventing approval responses.

Remaining limitations are explicit: this is a local, single-user service. Kiro's native clarification, continuation, implementation and independent validation were exercised; Claude/Codex development remains unverified beyond authentication-blocked paths. The Strands relevance judge executed successfully; automatic acceptance-plan generation has not been exercised live. External MCP services need separately scoped resources; token aggregation is partial; unsupported runner formats can have unknown counts; similar questions are merged only when equivalent by the implemented matching rule; large repositories can exceed the bounded judge context. External symlinks are refused during snapshotting. Native memory/process limits are sampled rather than hard VM quotas.

Delivery checks included Python/JavaScript syntax, the live authenticated API, persisted event streaming, file/diff/report retrieval and applying the real candidate patch. A one-off check in temporary storage confirmed that a revision during baseline validation advances both the frozen rubric and development specification together, preserves the prior version, rejects invalid revisions without changing answers, and retains old artifacts on resume. No coding harness was invoked by that check. The final in-app browser check verified the native comparison, all four displayed Kiro scores, and the responsive history selector. Native isolation checks denied peer reads, clarification edits, protected-test writes and hardlinks, and unassigned service ports. These are manual delivery checks, not an automated application test-suite deliverable.

The example tests evaluate the **generated demo implementations**. No benchmark, automated test suite, or evaluation deliverable for the chat application or its internal components is included.

## License

This project is licensed under the **Apache License 2.0**. See the `LICENSE` file for the full license text. The `NOTICE` file records the project copyright: Amazon.com, Inc. or its affiliates. Source files in `arena/` and `tests/` carry `SPDX-License-Identifier: Apache-2.0` headers.

Third-party dependency licenses are listed in `THIRD-PARTY-LICENSES`. That file covers direct runtime dependencies (boto3, FastAPI, uvicorn, Strands Evals, Strands Agents, Claude Agent SDK, MCP, httpx, and the hatchling build backend) and identifies vendored CLI binaries (OpenAI Codex CLI, Kiro CLI, Claude Code CLI) that are packaged into AgentCore deployment artifacts at build time. Full dependency license texts should be generated and reviewed before any public release.

**Contributing** — read `CONTRIBUTING.md` before opening issues or pull requests. Work against the latest source on the `main` branch, open an issue to discuss significant changes first, and ensure local tests pass before sending a pull request.

**Security issues** — do **not** open a public GitHub issue. Report potential security vulnerabilities via the [AWS vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/).

**Code of conduct** — this project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct). Questions or comments can be directed to opensource-codeofconduct@amazon.com.
