#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# bootstrap.sh — the single entrypoint from a fresh clone to a working installation.
#
# Written to be run by an AI coding assistant (Claude Code, Codex, Kiro, Cursor, Copilot, Gemini CLI, or any other) as well as a person:
# it never prompts, every stage prints exactly one JSON line on stdout, command output goes
# to a log under .bootstrap/, and the exit code names the outcome. Re-running is safe: each
# stage skips work that is already done. See docs/launch-runbook.md for the full procedure.
#
# Stages, in order:  host  install  tests  inputs  preflight  setup  deploy  probe  doctor  start
#
# Exit codes
#    0  done
#    2  usage error — an unknown option, or a --set the inputs stage refused (see its error)
#   10  host prerequisite missing that the assistant can install (for example uv)
#   11  unsupported host (not macOS) — stop
#   12  dependency install failed
#   13  test suite failed — report the failing tests to the human and stop
#   20  inputs missing — ask the human the listed questions, then pass the answers with --set
#   21  preflight failed on something the assistant can fix (see "failed_checks" in the JSON)
#   22  the inputs command produced no report, or setup failed
#   30  agentcore-deploy failed
#   31  agentcore-probe failed
#   32  doctor reported blockers
#   33  start failed
#   40  human action required (a login, credentials, model access, a bucket) — see the JSON
#   41  AgentCore runtimes are not deployed, or the cached ones no longer match the
#       configuration (agentcore-drift) — a human must authorize ./bootstrap.sh --deploy
#   42  this shell is sandboxed — a human must run the printed start command in Terminal.app
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO" || exit 11
PY="$REPO/.venv/bin/python"
CLI="$REPO/.venv/bin/multi-coding-harness-compare"
# The same files for every stage, and for the services started at the end.
CONFIG="${MCHC_CONFIG:-${ARENA_CONFIG:-$REPO/config.toml}}"
PROPS="${MCHC_PROPERTIES:-$(dirname "$CONFIG")/deploy.properties}"
export MCHC_CONFIG="$CONFIG" MCHC_PROPERTIES="$PROPS"
LOGDIR="$REPO/.bootstrap"
LOG="$LOGDIR/bootstrap-$(date +%Y%m%d-%H%M%S).log"

json() {  # escape a string for JSON; other control characters (terminal colour codes) are dropped
  local s=${1//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\r'/}; s=${s//$'\t'/\\t}
  printf '"%s"' "$(printf '%s' "$s" | LC_ALL=C tr -d '\001-\037')"
}
DEPLOY=0; START=0; SKIP_TESTS=0; RECONFIGURE=0; OFFLINE=0; INSTALL_UV=0
APPROVE=""
SETS=()
usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
  cat <<'EOF'

Usage: ./bootstrap.sh [options]
  --set KEY=VALUE   write KEY into deploy.properties first (repeatable); see deploy.properties.example
  --deploy          authorize agentcore-deploy (creates an IAM role, S3 objects, three AgentCore runtimes)
  --approve TOKEN   approval token from deploy/agentcore-deploy.sh plan (required for non-pinned versions)
  --start           start the API and worker at the end and print the access URL
  --reconfigure     rewrite an existing config.toml from config.example.toml (backup kept)
  --skip-tests      skip the test suite (not recommended on a new machine)
  --offline         skip the read-only AWS checks in preflight
  --install-uv      install uv with its official installer if it is missing
  --help            this text
EOF
}
usage_error() {
  echo "{\"summary\":{\"ok\":false,\"exit\":2,\"next\":$(json "assistant: $1 (see ./bootstrap.sh --help)"),\"log\":\"\",\"stages\":[]}}"
  exit 2
}
while [ $# -gt 0 ]; do
  case "$1" in
    --set) [ $# -ge 2 ] || usage_error "--set needs KEY=VALUE"; SETS+=("$2"); shift ;;
    --set=*) SETS+=("${1#--set=}") ;;
    --deploy) DEPLOY=1 ;;
    --approve) [ $# -ge 2 ] || usage_error "--approve needs TOKEN"; APPROVE="$2"; shift ;;
    --approve=*) APPROVE="${1#--approve=}" ;;
    --start) START=1 ;;
    --reconfigure) RECONFIGURE=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --offline) OFFLINE=1 ;;
    --install-uv) INSTALL_UV=1 ;;
    --help|-h) usage; exit 0 ;;
    *) usage_error "unknown option $1" ;;
  esac
  shift
done
mkdir -p "$LOGDIR"

# ── output ──────────────────────────────────────────────────────────────────
STAGES=""
stage() {  # stage NAME STATUS EXIT DETAIL [FIX] [GATE] [EXTRA_JSON]
  local extra=${7:-}
  local line="{\"stage\":$(json "$1"),\"status\":$(json "$2"),\"exit\":$3,\"detail\":$(json "$4"),\"fix\":$(json "${5:-}"),\"gate\":$(json "${6:-none}")${extra:+,$extra}}"
  echo "$line"
  echo "[$(date +%H:%M:%S)] $line" >> "$LOG"
  STAGES="${STAGES:+$STAGES,}{\"stage\":$(json "$1"),\"status\":$(json "$2")}"
}
finish() {  # finish EXIT NEXT
  echo "{\"summary\":{\"ok\":$([ "$1" -eq 0 ] && echo true || echo false),\"exit\":$1,\"next\":$(json "$2"),\"log\":$(json "$LOG"),\"stages\":[$STAGES]}}"
  exit "$1"
}
run() {  # run COMMAND... ; output appended to the log
  echo "[$(date +%H:%M:%S)] \$ $*" >> "$LOG"
  "$@" >> "$LOG" 2>&1
}
# JSON reports are read by path argument, never by splicing the path into code.
jvalid() { "$PY" -c 'import json, sys; json.load(open(sys.argv[1]))' "$1" 2>/dev/null; }
jfield() {  # jfield FILE KEY — a top-level value; lists, objects and booleans print as JSON
  "$PY" -c 'import json, sys
value = json.load(open(sys.argv[1])).get(sys.argv[2], "")
print(json.dumps(value) if isinstance(value, (list, dict, bool)) else value)' "$1" "$2"
}
echo "{\"bootstrap\":\"starting\",\"repo\":$(json "$REPO"),\"log\":$(json "$LOG")}"

# ── host ────────────────────────────────────────────────────────────────────
if [ "$(uname -s)" != "Darwin" ] || [ ! -x /usr/bin/sandbox-exec ]; then
  stage host failed 11 "$(uname -s): macOS with /usr/bin/sandbox-exec is required" \
    "use a macOS workstation (including EC2 Mac); see docs/launch-runbook.md, Windows" human
  finish 11 "stop: unsupported host"
fi
if ! xcode-select -p >/dev/null 2>&1 || ! git --version >/dev/null 2>&1; then
  stage host gate 40 "Xcode Command Line Tools (git) are not installed" \
    "a human runs: xcode-select --install   (and clicks Install in the dialog), then re-run ./bootstrap.sh" human
  finish 40 "human: install the Xcode Command Line Tools"
fi
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  if [ "$INSTALL_UV" -eq 1 ]; then
    run sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' || {
      stage host failed 10 "the uv installer failed (see the log)" "install uv: https://docs.astral.sh/uv/" assistant
      finish 10 "assistant: install uv, then re-run"; }
  else
    stage host failed 10 "uv is not installed" \
      "re-run ./bootstrap.sh --install-uv (runs: curl -LsSf https://astral.sh/uv/install.sh | sh)" assistant
    finish 10 "assistant: re-run with --install-uv"
  fi
fi
stage host ok 0 "macOS $(sw_vers -productVersion) $(uname -m); $(git --version); uv $(uv --version 2>/dev/null | awk '{print $2}')"

# ── install ─────────────────────────────────────────────────────────────────
if [ ! -x "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null; then
  run uv venv --python 3.12 .venv || {
    stage install failed 12 "uv venv --python 3.12 failed (see the log)" "uv python install 3.12, then re-run" assistant
    finish 12 "assistant: fix the Python 3.12 install"; }
fi
if ! run uv pip install --python "$PY" -r requirements.lock -r requirements-dev.lock \
   || ! run uv pip install --python "$PY" --no-deps -e .; then
  stage install failed 12 "dependency install failed (see the log)" "check network access to the package index, then re-run" assistant
  finish 12 "assistant: fix the dependency install"
fi
stage install ok 0 "$("$PY" --version 2>&1) in .venv; requirements.lock and requirements-dev.lock installed"

# ── tests ───────────────────────────────────────────────────────────────────
if [ "$SKIP_TESTS" -eq 1 ]; then
  stage tests skipped 0 "--skip-tests"
elif ! "$PY" -c 'import pytest' 2>/dev/null; then
  stage tests failed 12 "pytest is not installed in .venv" "re-run ./bootstrap.sh: the install stage installs requirements-dev.lock" assistant
  finish 12 "assistant: re-run to install the test dependencies"
else
  if run "$PY" -m pytest tests -q -p no:cacheprovider; then
    summary=$(grep -E '^[0-9]+ (passed|skipped)' "$LOG" | tail -1)
    case "$summary" in
      *skipped*) summary="$summary (skipped tests need an unsandboxed shell; their reasons are in the log)" ;;
    esac
    ui=""
    if command -v node >/dev/null 2>&1; then
      for test in tests/*.cjs; do
        run node "$test" || { stage tests failed 13 "UI test failed: $test" \
                                "report the failing test to the human and stop; do not modify the code or the machine" assistant
                              finish 13 "assistant: report the failing UI test"; }
      done
      ui="; UI tests passed"
    else
      ui="; UI tests skipped (Node.js not installed; not needed to run the application)"
    fi
    stage tests ok 0 "${summary:-tests passed}$ui"
  else
    failing=$(grep -E '^(FAILED|ERROR) ' "$LOG" | head -20 | tr '\n' ';')
    stage tests failed 13 "test suite failed: ${failing:-see the log}" \
      "report the failing tests to the human and stop; do not modify the code or the machine to make them pass" assistant
    finish 13 "assistant: report the failing tests"
  fi
fi

# ── inputs ──────────────────────────────────────────────────────────────────
INPUTS="$LOGDIR/inputs.json"
SET_ARGS=()
for pair in ${SETS[@]+"${SETS[@]}"}; do SET_ARGS+=(--set "$pair"); done
"$CLI" inputs --properties "$PROPS" ${SET_ARGS[@]+"${SET_ARGS[@]}"} > "$INPUTS" 2>> "$LOG"
code=$?
if ! jvalid "$INPUTS"; then
  stage inputs failed 22 "the inputs command did not produce JSON (see the log)" "run $CLI inputs and read its error" assistant
  finish 22 "assistant: run the inputs command by hand"
fi
if [ "$code" -eq 2 ]; then
  stage inputs failed 2 "$(jfield "$INPUTS" error)" "correct that --set argument (or the named line in deploy.properties), then re-run" assistant
  finish 2 "assistant: correct the --set argument"
fi
created=$([ "$(jfield "$INPUTS" created)" = "true" ] && echo "created deploy.properties from the example; ")
if [ "$code" -eq 20 ]; then
  stage inputs gate 20 "${created}values still to supply: $(jfield "$INPUTS" missing)" \
    "ask the human each question in \"questions\", then re-run: ./bootstrap.sh --set KEY=VALUE ..." human \
    "\"missing\":$(jfield "$INPUTS" missing),\"questions\":$(jfield "$INPUTS" questions)"
  finish 20 "human: answer the questions in the inputs stage"
fi
stage inputs ok 0 "${created}deploy.properties complete"

# ── preflight ───────────────────────────────────────────────────────────────
PREFLIGHT="$LOGDIR/preflight.json"
"$CLI" preflight --config "$CONFIG" --properties "$PROPS" $([ "$OFFLINE" -eq 1 ] && echo --offline) > "$PREFLIGHT" 2>> "$LOG"
pf() {  # pf FIELD — one value from the preflight report
  "$PY" - "$PREFLIGHT" "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
fails = [c for c in d["checks"] if c["status"] == "fail"]
print({"runtime": lambda: d["runtime"] or "",
       "needs_deploy": lambda: int(d["needs_deploy"]),
       "nested": lambda: int(d["nested_sandbox"]),
       "failures": lambda: "; ".join(f"{c['id']}: {c['detail']}" for c in fails),
       "fixes": lambda: " | ".join(f"{c['id']}: {c['fix']}" for c in fails),
       "human": lambda: int(any(c["gate"] == "human" for c in fails)),
       "assistant": lambda: int(any(c["gate"] == "assistant" for c in fails)),
       "failed_checks": lambda: json.dumps([{k: c[k] for k in ("id", "gate", "detail", "fix")} for c in fails]),
       "warnings": lambda: json.dumps([{k: c[k] for k in ("id", "detail", "fix")}
                                       for c in d["checks"] if c["status"] == "warn"])}[sys.argv[2]]())
PY
}
if ! jvalid "$PREFLIGHT"; then
  stage preflight failed 21 "preflight did not produce JSON (see the log)" "run $CLI preflight and report its error to the human" assistant
  finish 21 "assistant: run preflight by hand"
fi
RUNTIME=$(pf runtime)
NEEDS_DEPLOY=$(pf needs_deploy)
NESTED=$(pf nested)
failures=$(pf failures)
fixes=$(pf fixes)
human=$(pf human)
assistant=$(pf assistant)
extra="\"report\":$(json "$PREFLIGHT"),\"failed_checks\":$(pf failed_checks),\"warnings\":$(pf warnings)"
if [ "$human" -eq 1 ]; then
  stage preflight gate 40 "$failures" "$fixes" human "$extra"
  finish 40 "human: relay the fix of each failed check whose gate is human; apply those whose gate is assistant; re-run after the human confirms"
elif [ "$assistant" -eq 1 ]; then
  stage preflight failed 21 "$failures" "$fixes" assistant "$extra"
  finish 21 "assistant: apply each failed check's fix, then re-run ./bootstrap.sh"
elif [ "$NEEDS_DEPLOY" -eq 1 ] && [ "$DEPLOY" -eq 0 ]; then
  stage preflight gate 41 "$failures" \
    "ask the human to authorize creating or refreshing an IAM role, S3 objects and three AgentCore runtimes; then re-run ./bootstrap.sh --deploy" deploy "$extra"
  finish 41 "human: authorize --deploy"
fi
stage preflight ok 0 "runtime $RUNTIME; all prerequisites met$([ "$NEEDS_DEPLOY" -eq 1 ] && echo '; runtimes will be deployed (--deploy)')" "" none "$extra"

# ── setup ───────────────────────────────────────────────────────────────────
if [ -f "$CONFIG" ] && [ "$RECONFIGURE" -eq 0 ]; then
  stage setup skipped 0 "config.toml exists and is kept (behaviour settings); deploy.properties holds the environment; --reconfigure rewrites it from config.example.toml (a backup is kept)"
else
  SETUP_OUT="$LOGDIR/setup.json"
  "$CLI" setup --output "$CONFIG" --properties "$PROPS" $([ -f "$CONFIG" ] && echo --force) > "$SETUP_OUT" 2>> "$LOG"
  if ! jvalid "$SETUP_OUT" || [ "$(jfield "$SETUP_OUT" ok)" != "true" ]; then
    stage setup failed 22 "$(head -c 1500 "$SETUP_OUT")" "read the decisions/error in $SETUP_OUT and fix deploy.properties with ./bootstrap.sh --set" assistant
    finish 22 "assistant: fix the setup error"
  fi
  stage setup ok 0 "wrote config.toml; deploy.properties updated" "" none "\"report\":$(json "$SETUP_OUT")"
fi

# ── deploy / probe (agentcore only) ─────────────────────────────────────────
if [ "$RUNTIME" = "agentcore" ]; then
  if [ "$DEPLOY" -eq 1 ]; then
    # Delegate to deploy/agentcore-deploy.sh so any workstation can run this step
    # without having the full worker/API installed.  The JSON-line contract stays the same.
    APPROVE_ARGS=()
    [ -n "$APPROVE" ] && APPROVE_ARGS=(--approve "$APPROVE")
    if run "$REPO/deploy/agentcore-deploy.sh" --properties "$PROPS" deploy \
        ${APPROVE_ARGS[@]+"${APPROVE_ARGS[@]}"}; then
      stage deploy ok 0 "agentcore-deploy.sh created or refreshed the role and runtimes"
    else
      stage deploy failed 30 "agentcore-deploy.sh failed (see the log)" "read the error in the log; permissions are listed in docs/launch-runbook.md" assistant
      finish 30 "assistant: diagnose agentcore-deploy"
    fi
  else
    stage deploy skipped 0 "runtimes already deployed and matching; --deploy is needed only after preflight exit 41"
  fi
  PROBE="$LOGDIR/probe.json"
  if "$CLI" agentcore-probe > "$PROBE" 2>> "$LOG"; then
    stage probe ok 0 "runtimes reachable and their configured paths writable" "" none "\"report\":$(json "$PROBE")"
  else
    stage probe failed 31 "$(head -c 1500 "$PROBE")" "see each runtime's error in $PROBE" assistant "\"report\":$(json "$PROBE")"
    finish 31 "assistant: diagnose agentcore-probe"
  fi
else
  stage deploy skipped 0 "runtime $RUNTIME needs no AgentCore runtimes"
  stage probe skipped 0 "runtime $RUNTIME"
fi

# ── doctor ──────────────────────────────────────────────────────────────────
DOCTOR="$LOGDIR/doctor.json"
if "$CLI" doctor > "$DOCTOR" 2>> "$LOG"; then
  stage doctor ok 0 "no blockers" "" none "\"report\":$(json "$DOCTOR")"
else
  blockers=$("$PY" -c 'import json, sys; print("; ".join(json.load(open(sys.argv[1]))["blockers"]))' "$DOCTOR" 2>/dev/null)
  if [ "$OFFLINE" -eq 1 ]; then
    fix="re-run ./bootstrap.sh without --offline: preflight then names, for each blocker, who acts and how"
  else
    fix="each blocker names its remedy; S3 access, a login or model access is a human action"
  fi
  stage doctor failed 32 "${blockers:-doctor failed (see the log)}" "$fix" assistant "\"report\":$(json "$DOCTOR")"
  finish 32 "assistant: resolve the doctor blockers"
fi
DATA_DIR=$(jfield "$DOCTOR" data_dir 2>/dev/null)

# ── start ───────────────────────────────────────────────────────────────────
if [ "$START" -eq 0 ]; then
  stage start skipped 0 "--start not given; ./bootstrap.sh --start (or $CLI start) starts the API and worker"
  finish 0 "done: run ./bootstrap.sh --start to start the services"
fi
if [ "$NESTED" -eq 1 ]; then
  stage start gate 42 "this shell is sandboxed, and the services need their own Seatbelt sandbox and process inspection" \
    "a human runs in Terminal.app:  cd $(printf %q "$REPO") && .venv/bin/multi-coding-harness-compare start" human
  finish 42 "human: run the start command in Terminal.app"
fi
START_OUT="$LOGDIR/start.out"
if "$CLI" start > "$START_OUT" 2>&1; then
  url=$(grep -o 'http://[^ ]*' "$START_OUT" | head -1)
  stage start ok 0 "API and worker running" "" none \
    "\"url\":$(json "$url"),\"handoff\":$(json "give the whole url to the human as is; do not open it yourself and never copy its token anywhere else")"
  finish 0 "human: open the url"
fi
stage start failed 33 "$(head -c 800 "$START_OUT")" "read api.log and worker.log in ${DATA_DIR:-the data directory}" assistant
finish 33 "assistant: diagnose start"
