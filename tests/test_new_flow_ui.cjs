// Tests for harness-written-tests (new flow) UI changes:
//   statusLabel for "testing" / "reviewing_tests",
//   panel-fail-strip and table test-fail-cause for test_stop_reason,
//   testStageSection budget+lock+review-fail content,
//   renderTestReview new-flow description block.
"use strict";
const assert = require("node:assert/strict");
const fs     = require("node:fs");
const vm     = require("node:vm");

require("../arena/static/artifact-readiness.js");
const app   = fs.readFileSync(require.resolve("../arena/static/app.js"), "utf8");
const index = fs.readFileSync(require.resolve("../arena/static/index.html"), "utf8");

// ── minimal DOM stub ───────────────────────────────────────────────────────────
const nodes = new Map();
function makeNode(id) {
  const classes = new Set();
  return {
    id, value: "", innerHTML: "", disabled: false, textContent: "",
    placeholder: "", checked: false,
    parentElement: {classList:{toggle(){},add(){},remove(){},has(){return false;}}},
    classList: {
      toggle(cls, force) {
        const add = force === undefined ? !classes.has(cls) : !!force;
        if(add) classes.add(cls); else classes.delete(cls);
      },
      add(cls){ classes.add(cls); },
      remove(cls){ classes.delete(cls); },
      has(cls){ return classes.has(cls); }
    },
    appendChild(child){ child.parentElement = this; }
  };
}
const document = {
  getElementById(id) {
    if(!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  },
  querySelector()    { return null; },
  querySelectorAll() { return [];   }
};

const context = vm.createContext({console, document,
  artifactReadiness: globalThis.artifactReadiness,
  localStorage: {getItem(){ return null; }}});

// Run everything before the click-handler registrations.
vm.runInContext(app.slice(0, app.indexOf('$("new-comparison").onclick=newComparison;')), context);

// Pre-create nodes that the render functions touch.
["judge-controls","new-judge-slot","comparison-judge-slot"].forEach(id => document.getElementById(id));

vm.runInContext(`
  system = {
    judge_model: null, evaluation_planner: null, planning_requires_claude: false,
    harnesses: {claude:{model:"sonnet"}, codex:{model:"sol"}, kiro:{model:"kiro"}},
    test_coverage: {supported: true, worker_ready: true},
    artifact_readiness: {enabled: false}
  };
  modelCatalogue = {harnesses: {
    claude: {configured_default:"sonnet", models:[], source:""},
    codex:  {configured_default:"sol",    models:[], source:""},
    kiro:   {configured_default:"kiro",   models:[], source:""}
  }};
  judgeCatalogue = {checked_at: 100, models: [], warnings: [], profile:"default", region:"us-east-1"};
  renderQuestions=()=>{}; renderJudge=()=>{}; renderArtifactReadiness=()=>{}; updateProjectControls=()=>{};
`, context);

// ── helpers ────────────────────────────────────────────────────────────────────
const CMP_ID = "aabbccdd11223344aabbccdd11223344";

function makeRun(overrides) {
  return Object.assign({
    id: "r1", harness: "claude", status: "testing", spec_version: 1,
    model: "sonnet", requested_model: "sonnet", requested_effort: null,
    follow_ups_delivered: [], model_mismatch: null,
    evaluation: null, evidence: null, usage: null, duration: null,
    artifacts: null, error: null, project_output: null,
    test_stop_reason: null, test_lock: [], attempts: []
  }, overrides);
}

function makeComparison(runOverrides, reviewOverrides, limitOverrides) {
  const run = makeRun(runOverrides || {});
  return {
    id: CMP_ID,
    status: "testing",
    spec_version: 1, frozen: true, cancel_requested: false,
    prompt: "Build a CLI tool",
    answers: [], follow_ups: [], artifact_storage: null, artifact_readiness: null,
    limits: Object.assign({test_active_seconds: 3600, test_fix_rounds: 2}, limitOverrides || {}),
    runs: [
      run,
      Object.assign(makeRun({id:"r2",harness:"codex",model:"sol",requested_model:"sol"}), runOverrides || {}),
      Object.assign(makeRun({id:"r3",harness:"kiro",model:"kiro",requested_model:"kiro"}), runOverrides || {})
    ],
    test_review: Object.assign({
      id: "rev1", status: "approved", revision: 1, spec_version: 1,
      proposal: {summary:"Test suite", test_cases:[], questions:[], assumptions:[], limitations:[]},
      answers: {}, history: [], feedback: [],
      error: null, generation_status: null,
      generation_attempts: [],
      plan_sha256: null, approved_at: 1700000000,
      test_coverage: "focused", mode: null, supplied_plan_sha256: null, failure: null
    }, reviewOverrides || {})
  };
}

// ── 1. statusLabel("testing") → "Writing harness tests" ───────────────────────
{
  const label = vm.runInContext(`statusLabel("testing")`, context);
  assert.strictEqual(label, "Writing harness tests");
}
console.log("1. statusLabel('testing') = 'Writing harness tests' — passed");

// ── 2. statusLabel("reviewing_tests") new flow → "Reviewing harness tests" ────
{
  const review = {status:"approved", plan_sha256:null, generation_status:"idle"};
  const label = vm.runInContext(`statusLabel("reviewing_tests", ${JSON.stringify(review)})`, context);
  assert.strictEqual(label, "Reviewing harness tests");
}
console.log("2. statusLabel('reviewing_tests', new-flow review) = 'Reviewing harness tests' — passed");

// ── 3. statusLabel("reviewing_tests") legacy flow → "Developing approved tests" ─
{
  const review = {status:"approved", plan_sha256:null, generation_status:"generating"};
  const label = vm.runInContext(`statusLabel("reviewing_tests", ${JSON.stringify(review)})`, context);
  assert.strictEqual(label, "Developing approved tests");
}
console.log("3. statusLabel('reviewing_tests', legacy generating review) = 'Developing approved tests' — passed");

// ── 4. Panel shows panel-fail-strip for non-success test_stop_reason ────────────
{
  const stopReason = {code: "budget_exhausted", message: "Test stage budget exhausted: 3600 s of active time used across 3 rounds."};
  const cmp = makeComparison({test_stop_reason: stopReason, status: "evaluated_failed"});
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = []; renderPanels();`, context);
  const html = nodes.get("panels").innerHTML;
  assert.match(html, /panel-fail-strip/);
  assert.match(html, /Test stage budget exhausted/);
}
console.log("4. Panel shows panel-fail-strip with test_stop_reason.message — passed");

// ── 5. Panel does NOT show extra panel-fail-strip when test_stop_reason is null ─
{
  const cmp = makeComparison({test_stop_reason: null, status: "evaluated_complete"});
  // No legacy test_review failure either.
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = []; renderPanels();`, context);
  const html = nodes.get("panels").innerHTML;
  // panel-fail-strip must not appear (no test_stop_reason, no test_review failure)
  assert.doesNotMatch(html, /panel-fail-strip/);
}
console.log("5. Panel: no panel-fail-strip when test_stop_reason is null — passed");

// ── 6. Panel does NOT show extra panel-fail-strip for all_passed ──────────────
{
  const stopReason = {code: "all_passed", message: "All tests passed."};
  const cmp = makeComparison({test_stop_reason: stopReason, status: "evaluating"});
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = []; renderPanels();`, context);
  const html = nodes.get("panels").innerHTML;
  assert.doesNotMatch(html, /panel-fail-strip/);
}
console.log("6. Panel: no panel-fail-strip when test_stop_reason.code is 'all_passed' — passed");

// ── 7. Table shows test-fail-cause with test_stop_reason.message ─────────────
{
  const stopReason = {code: "budget_exhausted", message: "Test stage budget exhausted: 3600 s of active time used across 3 rounds."};
  const cmp = makeComparison({test_stop_reason: stopReason, status: "evaluated_failed"});
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = []; renderTable();`, context);
  const html = nodes.get("comparison-table").innerHTML;
  assert.match(html, /test-fail-cause/);
  assert.match(html, /Test stage budget exhausted/);
}
console.log("7. Table shows test-fail-cause with test_stop_reason.message — passed");

// ── 8. Table: no test-fail-cause when test_stop_reason is null ───────────────
{
  const cmp = makeComparison({test_stop_reason: null, status: "evaluating"});
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = []; renderTable();`, context);
  const html = nodes.get("comparison-table").innerHTML;
  // test-fail-cause should NOT appear for test_stop_reason (may appear for legacy test_review.failure)
  // The comparison has no test_review.failure, so neither should appear.
  assert.doesNotMatch(html, /test-fail-cause/);
}
console.log("8. Table: no test-fail-cause when test_stop_reason is null — passed");

// ── 9. testStageSection: budget used + budget total shown ────────────────────
{
  const stopReason = {code: "budget_exhausted", message: "Test stage budget exhausted after 3 rounds."};
  const run = makeRun({
    id: "r1",
    test_stop_reason: stopReason,
    attempts: [
      {phase: "test",   active_seconds: 1200},
      {phase: "repair", active_seconds: 800},
      {phase: "fix",    active_seconds: 400},
      {phase: "evaluating", active_seconds: 999}  // must be excluded
    ]
  });
  const cmp = makeComparison({});
  // Set current in context to give limits, then call testStageSection(run)
  vm.runInContext(`current = ${JSON.stringify(cmp)};`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  // Budget used = 1200+800+400 = 2400s = 40m; total = 3600s = 60m (1h)
  assert.match(html, /evidence-row/);
  assert.match(html, /Test stage/);
  assert.match(html, /Budget:/);
  // Should show time values (40m used, 60m/1h total)
  assert.match(html, /used of/);
  // The stop reason message should appear
  assert.match(html, /Test stage budget exhausted after 3 rounds/);
  // Evaluating phase must be excluded from budget
  // (If it were included: 2400+999=3399s; if excluded: 2400s. We can't assert exact seconds
  //  without knowing duration(), but we CAN assert the class is set for failure.)
  assert.match(html, /test-stage-stop-reason/);
}
console.log("9. testStageSection: budget row, stop-reason message, test-stage-stop-reason class — passed");

// ── 10. testStageSection: locked files shown ────────────────────────────────
{
  const stopReason = {code: "rounds_exhausted", message: "All 2 fix rounds were used; 1 scored check still failed."};
  const run = makeRun({
    id: "r1",
    test_stop_reason: stopReason,
    test_lock: ["tests/test_app.py", "tests/conftest.py"],
    attempts: [{phase: "test", active_seconds: 500}]
  });
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)};`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  assert.match(html, /2 locked test files/);
  assert.match(html, /tests\/test_app\.py/);
  assert.match(html, /tests\/conftest\.py/);
}
console.log("10. testStageSection: locked test files list shown — passed");

// ── 11. testStageSection: test agent review failure shown from test_stages.review ────
// R2-5: model the review failure via test_stages[-1].review.status="failed" (not a log
// event), use the real stop code for the stage, and assert both are shown.
{
  const stageEntry = {
    spec_version: 1,
    status: "stopped",
    budget_seconds: 3600,
    used_seconds: 500,
    rounds: [],
    lock: {locked_at: "2024-01-01T00:10:00Z", files: {"tests/t.py": "abc"}, auto_locked: []},
    lock_violations: [],
    evaluated_round: 0,
    stop_reason: {code: "all_passed", message: "All 2 scored checks passed in the write round."},
    review: {
      status: "failed",
      model: "us.anthropic.claude-opus-5",
      effort: "max",
      scenarios: [],
      summary: null,
      concerns: [],
      failure: {code: "timeout", message: "The test agent review timed out after 2 attempt(s).", attempts: 2}
    }
  };
  const run = makeRun({
    id: "r-rev", spec_version: 1,
    test_stages: [stageEntry],
    test_stop_reason: stageEntry.stop_reason,
    attempts: []
  });
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = [];`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  // Both the stage stop reason and the review failure must be shown (R2-5).
  assert.match(html, /All 2 scored checks passed in the write round/);
  assert.match(html, /test-review-failure/);
  assert.match(html, /Test agent review failed/);
  assert.match(html, /timed out after 2 attempt/);
}
console.log("11. testStageSection: test-agent review failure shown from test_stages.review — passed");

// ── 12. testStageSection: returns empty string when no test_stop_reason ───────
{
  const run = makeRun({id: "r1", test_stop_reason: null});
  vm.runInContext(`current = ${JSON.stringify(makeComparison({}))}; logs = [];`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  assert.strictEqual(html, "");
}
console.log("12. testStageSection: returns '' when test_stop_reason is null — passed");

// ── 13. renderTestReview: new flow shows new-flow description block ───────────
{
  // new flow: plan_sha256 null, generation_status not "generating"
  const cmp = makeComparison({}, {
    status: "approved", plan_sha256: null, generation_status: null, approved_at: 1700000000
  });
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = []; testReviewSignature = ""; renderTestReview();`, context);
  const html = nodes.get("test-review").innerHTML;
  assert.match(html, /Each harness writes and runs its own test suite/);
  assert.match(html, /active time budget/);
  assert.match(html, /excluding test-agent review time/);
  // must NOT show legacy "shared planner is writing"
  assert.doesNotMatch(html, /shared planner is writing executable tests/);
}
console.log("13. renderTestReview: new flow shows new-flow description block — passed");

// ── 14. renderTestReview: legacy flow still shows "shared planner" text ────────
{
  // legacy flow: plan_sha256 null, generation_status "generating"
  const cmp = makeComparison({}, {
    status: "approved", plan_sha256: null, generation_status: "generating", approved_at: 1700000000
  });
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = []; testReviewSignature = ""; renderTestReview();`, context);
  const html = nodes.get("test-review").innerHTML;
  assert.match(html, /shared planner is writing executable tests/);
  // must NOT show new flow text
  assert.doesNotMatch(html, /Each harness writes and runs its own test suite/);
}
console.log("14. renderTestReview: legacy flow still shows 'shared planner' text — passed");

// ── 15. renderTestReview: new flow status text "Each harness is writing its own tests" ─
{
  const cmp = makeComparison({}, {
    status: "approved", plan_sha256: null, generation_status: null, approved_at: 1700000000
  });
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = []; testReviewSignature = ""; renderTestReview();`, context);
  const html = nodes.get("test-review").innerHTML;
  assert.match(html, /Each harness is writing its own tests/);
}
console.log("15. renderTestReview: status text 'Each harness is writing its own tests' for new flow — passed");

// ── 16. Panel shows test-agent-review failure strip when test_stages.review is failed ─
// R2-5: model the review failure via test_stages[-1].review.status="failed" (no log event
// needed); assert the panel shows the failure message from test_stages.
{
  const stageEntry = {
    spec_version: 1,
    status: "stopped",
    budget_seconds: 3600,
    used_seconds: 500,
    rounds: [],
    lock: {locked_at: "2024-01-01T00:10:00Z", files: {"tests/t.py": "abc"}, auto_locked: []},
    lock_violations: [],
    evaluated_round: 0,
    stop_reason: {code: "all_passed", message: "All tests passed."},
    review: {
      status: "failed",
      model: "us.anthropic.claude-opus-5",
      effort: "max",
      scenarios: [],
      summary: null,
      concerns: [],
      failure: {code: "api_error", message: "Agent refused: insufficient test coverage.", attempts: 1}
    }
  };
  const run = makeRun({
    id: "r1", status: "evaluated_incomplete",
    spec_version: 1,
    test_stages: [stageEntry],
    test_stop_reason: stageEntry.stop_reason
  });
  const cmp = makeComparison({});
  cmp.runs[0] = run;
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = []; renderPanels();`, context);
  const html = nodes.get("panels").innerHTML;
  // Panel-fail-strip comes from test_stages.review.failure (not a log event).
  assert.match(html, /panel-fail-strip/);
  assert.match(html, /Agent refused: insufficient test coverage/);
}
console.log("16. Panel shows review-agent failure strip from test_stages.review — passed");

// ── 17. U1: testStageSection uses test_stages entry for budget (not attempts) ──
{
  const stageEntry = {
    spec_version: 1,
    status: "stopped",
    budget_seconds: 3600,
    used_seconds: 2200,  // different from attempts sum
    rounds: [],
    lock: null,
    lock_violations: [],
    evaluated_round: null,
    stop_reason: {code: "rounds_exhausted", message: "All fix rounds used."},
    review: null
  };
  const run = makeRun({
    id: "r1",
    spec_version: 1,
    test_stages: [stageEntry],
    test_stop_reason: stageEntry.stop_reason,
    // attempts have different total (would give wrong budget if used)
    attempts: [{phase: "test", active_seconds: 500}, {phase: "fix", active_seconds: 300}]
  });
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)};`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  // Budget must come from test_stages entry (2200s = 36m 40s), not from attempts (800s = 13m 20s)
  assert.match(html, /Budget:/);
  assert.match(html, /used of/);
  // Should NOT show 13m (from attempts sum of 800s)
  assert.doesNotMatch(html, /13m/);
  // Should show something around 36m (from 2200s)
  assert.match(html, /36m/);
}
console.log("17. U1: testStageSection uses used_seconds from test_stages entry — passed");

// ── 18. U1: testStageSection shows rounds from test_stages ─────────────────────
{
  const stageEntry = {
    spec_version: 1,
    status: "stopped",
    budget_seconds: 3600,
    used_seconds: 1800,
    rounds: [
      {number: 0, kind: "write", started_at: "2024-01-01T00:00:00Z", ended_at: "2024-01-01T00:10:00Z",
       active_seconds: 600, outcome: "lock_created", validation_errors: [], verification: null, lock_violations: []},
      {number: 1, kind: "fix", started_at: "2024-01-01T00:11:00Z", ended_at: "2024-01-01T00:21:00Z",
       active_seconds: 600, outcome: "verified", validation_errors: [], verification: {passed: 2, failed: 0, errors: 0, failing_checks: []}, lock_violations: []}
    ],
    lock: null,
    lock_violations: [],
    evaluated_round: 1,
    stop_reason: {code: "all_passed", message: "All 2 scored checks passed."},
    review: null
  };
  const run = makeRun({
    id: "r1", spec_version: 1, test_stages: [stageEntry],
    test_stop_reason: stageEntry.stop_reason
  });
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)};`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  assert.match(html, /2 test-stage rounds/);
  assert.match(html, /write/i);
  assert.match(html, /fix/i);
  assert.match(html, /lock_created/);
  assert.match(html, /verified/);
  // Evaluated round shown
  assert.match(html, /Evaluated round/);
  assert.match(html, /1/);
}
console.log("18. U1: testStageSection shows rounds and evaluated_round from test_stages — passed");

// ── 19. U1: testStageSection shows lock violations from test_stages ───────────
{
  const stageEntry = {
    spec_version: 1,
    status: "stopped",
    budget_seconds: 3600,
    used_seconds: 1200,
    rounds: [],
    lock: {locked_at: "2024-01-01T00:10:00Z", files: {"tests/test_app.py": "abc123"}, auto_locked: ["tests/conftest.py"]},
    lock_violations: [
      {round: 1, path: "tests/test_app.py", kind: "modified"},
      {round: 1, path: "tests/conftest.py", kind: "deleted"}
    ],
    evaluated_round: null,
    stop_reason: {code: "rounds_exhausted", message: "All rounds exhausted with violations."},
    review: null
  };
  const run = makeRun({
    id: "r1", spec_version: 1, test_stages: [stageEntry],
    test_stop_reason: stageEntry.stop_reason,
    // Lock files from entry (not test_lock convenience copy)
    test_lock: []
  });
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)};`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  // Lock files from entry
  assert.match(html, /1 locked test file/);
  assert.match(html, /tests\/test_app\.py/);
  // Lock violations
  assert.match(html, /2 lock violations/);
  assert.match(html, /Round 1/);
  assert.match(html, /tests\/conftest\.py/);
  assert.match(html, /modified/i);
  assert.match(html, /deleted/i);
}
console.log("19. U1: testStageSection shows lock files from entry and lock violations — passed");

// ── 20. U1: testStageSection shows review scenarios from test_stages ──────────
{
  const stageEntry = {
    spec_version: 1,
    status: "complete",
    budget_seconds: 3600,
    used_seconds: 1500,
    rounds: [],
    lock: {locked_at: "2024-01-01T00:10:00Z", files: {"tests/test_slug.py": "def456"}, auto_locked: []},
    lock_violations: [],
    evaluated_round: 0,
    stop_reason: {code: "all_passed", message: "All tests passed."},
    review: {
      status: "complete",
      model: "us.anthropic.claude-opus-5",
      effort: "max",
      scenarios: [
        {scenario_id: "S1", rating: 1, verdict: "faithful", reason: "Exercises the scenario fully.", evidence: "tests/test_slug.py::test_basic"},
        {scenario_id: "S2", rating: 0.5, verdict: "partial", reason: "Partial assertion only.", evidence: "tests/test_slug.py::test_edge"}
      ],
      summary: "Good overall test coverage.",
      concerns: ["S2 assertion is weak"],
      failure: null
    }
  };
  const run = makeRun({
    id: "r1", spec_version: 1, test_stages: [stageEntry],
    test_stop_reason: stageEntry.stop_reason
  });
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = [];`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  // Review section present with scenarios
  assert.match(html, /Test agent review/);
  assert.match(html, /2 scenarios rated/);
  // Scenario details
  assert.match(html, /S1/);
  assert.match(html, /faithful/);
  assert.match(html, /Exercises the scenario fully/);
  assert.match(html, /tests\/test_slug\.py::test_basic/);
  assert.match(html, /S2/);
  assert.match(html, /partial/);
  assert.match(html, /rating 0\.5/);
  // Summary and concerns
  assert.match(html, /Good overall test coverage/);
  assert.match(html, /S2 assertion is weak/);
}
console.log("20. U1: testStageSection shows review scenarios, summary, concerns from test_stages — passed");

// ── 21. U1: testStageSection shows review failure from test_stages entry ───────
{
  const stageEntry = {
    spec_version: 1,
    status: "stopped",
    budget_seconds: 3600,
    used_seconds: 2000,
    rounds: [],
    lock: {locked_at: "2024-01-01T00:10:00Z", files: {"tests/t.py": "aaa"}, auto_locked: []},
    lock_violations: [],
    evaluated_round: 0,
    stop_reason: {code: "all_passed", message: "All tests passed."},
    review: {
      status: "failed",
      model: "us.anthropic.claude-opus-5",
      effort: "max",
      scenarios: [],
      summary: null,
      concerns: [],
      failure: {code: "timeout", message: "The test agent review timed out after 2 attempt(s) (3600 s). Verified test results are shown; Accuracy and Tests are incomplete.", attempts: 2}
    }
  };
  const run = makeRun({
    id: "r1", spec_version: 1, test_stages: [stageEntry],
    test_stop_reason: stageEntry.stop_reason
  });
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = [];`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  assert.match(html, /test-review-failure/);
  assert.match(html, /Test agent review failed/);
  assert.match(html, /timed out after 2 attempt/);
  // Code and attempts shown
  assert.match(html, /timeout/);
  assert.match(html, /2 attempt/);
}
console.log("21. U1: testStageSection shows review failure (code, message, attempts) from test_stages — passed");

// ── 22. U1: testStageSection renders when test_stages present even without top-level test_stop_reason
{
  const stageEntry = {
    spec_version: 1,
    status: "testing",
    budget_seconds: 3600,
    used_seconds: 500,
    rounds: [],
    lock: null,
    lock_violations: [],
    evaluated_round: null,
    stop_reason: {code: "harness_failed", message: "Write turn ended without a result: harness crashed."},
    review: null
  };
  const run = makeRun({
    id: "r1", spec_version: 1, test_stages: [stageEntry],
    test_stop_reason: null  // convenience copy absent
  });
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)};`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  // Must render from stageEntry even without run-level test_stop_reason
  assert.match(html, /evidence-row/);
  assert.match(html, /harness failed/i);
  assert.match(html, /Write turn ended without a result/);
}
console.log("22. U1: testStageSection renders from test_stages when top-level test_stop_reason absent — passed");

// ── 23. U1: testStageSection picks the entry matching spec_version ─────────────
{
  const entryV1 = {
    spec_version: 1, status: "complete", budget_seconds: 3600, used_seconds: 1000,
    rounds: [], lock: null, lock_violations: [], evaluated_round: null,
    stop_reason: {code: "all_passed", message: "All passed in spec v1."},
    review: null
  };
  const entryV2 = {
    spec_version: 2, status: "stopped", budget_seconds: 3600, used_seconds: 2000,
    rounds: [], lock: null, lock_violations: [], evaluated_round: null,
    stop_reason: {code: "rounds_exhausted", message: "Rounds exhausted in spec v2."},
    review: null
  };
  const run = makeRun({
    id: "r1", spec_version: 2,
    test_stages: [entryV1, entryV2],
    test_stop_reason: entryV2.stop_reason
  });
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)};`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  // Should show v2 message (matching spec_version=2)
  assert.match(html, /Rounds exhausted in spec v2/);
  assert.doesNotMatch(html, /All passed in spec v1/);
}
console.log("23. U1: testStageSection picks the test_stages entry matching spec_version — passed");

// ── 24. U1 escaping: harness-supplied strings in test_stages are escaped ───────
{
  const stageEntry = {
    spec_version: 1,
    status: "stopped",
    budget_seconds: 3600,
    used_seconds: 100,
    rounds: [{number: 0, kind: "write", active_seconds: 100, outcome: "<b>XSS</b>", validation_errors: [], verification: null, lock_violations: []}],
    lock: {locked_at: "", files: {"tests/<evil>.py": "aaa"}, auto_locked: []},
    lock_violations: [{round: 1, path: "tests/<evil>.py", kind: "<modified>"}],
    evaluated_round: null,
    stop_reason: {code: "harness_failed", message: "Failed <script>."},
    review: {
      status: "complete",
      model: "", effort: "",
      scenarios: [{scenario_id: "<S1>", rating: 1, verdict: "faithful", reason: "Good <test>.", evidence: "tests/<evil>.py::test_x"}],
      summary: "Summary <b>bold</b>.",
      concerns: ["Concern <script>alert(1)</script>"],
      failure: null
    }
  };
  const run = makeRun({id: "r1", spec_version: 1, test_stages: [stageEntry], test_stop_reason: stageEntry.stop_reason});
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = [];`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  // All harness-supplied strings must be escaped
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(html, /<evil>/);
  assert.doesNotMatch(html, /<b>XSS<\/b>/);
  assert.doesNotMatch(html, /<modified>/);
  assert.doesNotMatch(html, /<S1>/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;evil&gt;/);
}
console.log("24. U4: testStageSection escapes harness-supplied strings in test_stages — passed");

// ── 25. U2: app.js source contains the new-flow approval toast text ───────────
{
  const appSource = fs.readFileSync(require.resolve("../arena/static/app.js"), "utf8");
  assert.match(appSource, /Each harness now writes and runs these tests after its implementation is saved/);
  // Legacy text must still be present (for the old-flow condition)
  assert.match(appSource, /Development of the shared tests can now proceed/);
  // The two texts must be in a conditional (new-flow condition present)
  assert.match(appSource, /review\.plan_sha256.*generation_status.*generating|generation_status.*generating.*plan_sha256/);
}
console.log("25. U2: app.js source has new-flow approval toast and legacy toast in a conditional — passed");

// ── 26. U3: artifact-readiness shows "Writing harness tests" for testing status ─
{
  const {render} = globalThis.artifactReadiness;
  const inUseState = {enabled: true, ready: false, status: "in_use"};
  const newFlowReview = {status: "approved", plan_sha256: null, generation_status: null};

  // testing status → Writing harness tests
  const testingHtml = render(inUseState, {status: "testing", test_review: newFlowReview});
  assert.match(testingHtml, /Writing harness tests/);
  assert.doesNotMatch(testingHtml, /Developing approved tests/);
}
console.log("26. U3: artifact-readiness 'Writing harness tests' for testing status — passed");

// ── 27. U3: artifact-readiness "Reviewing harness tests" for new-flow reviewing_tests ─
{
  const {render} = globalThis.artifactReadiness;
  const inUseState = {enabled: true, ready: false, status: "in_use"};
  const newFlowReview = {status: "approved", plan_sha256: null, generation_status: null};

  // reviewing_tests with new-flow → Reviewing harness tests
  const reviewingHtml = render(inUseState, {status: "reviewing_tests", test_review: newFlowReview});
  assert.match(reviewingHtml, /Reviewing harness tests/);
  assert.doesNotMatch(reviewingHtml, /Developing approved tests/);
}
console.log("27. U3: artifact-readiness 'Reviewing harness tests' for new-flow reviewing_tests — passed");

// ── 28. U3: artifact-readiness still shows "Developing approved tests" for legacy (generating) ─
{
  const {render} = globalThis.artifactReadiness;
  const inUseState = {enabled: true, ready: false, status: "in_use"};
  const legacyReview = {status: "approved", plan_sha256: null, generation_status: "generating"};

  // Legacy flow (generation_status=generating) → Developing approved tests
  const legacyHtml = render(inUseState, {status: "planning", test_review: legacyReview});
  assert.match(legacyHtml, /Developing approved tests/);
  assert.doesNotMatch(legacyHtml, /Writing harness tests/);
  assert.doesNotMatch(legacyHtml, /Reviewing harness tests/);
}
console.log("28. U3: artifact-readiness still shows 'Developing approved tests' for legacy generating flow — passed");

// ── 29. U3: artifact-readiness "Writing harness tests" for new-flow planning status ─
{
  const {render} = globalThis.artifactReadiness;
  const inUseState = {enabled: true, ready: false, status: "in_use"};
  const newFlowReview = {status: "approved", plan_sha256: null, generation_status: null};

  // planning status with new-flow review → Writing harness tests
  const planningHtml = render(inUseState, {status: "planning", test_review: newFlowReview});
  assert.match(planningHtml, /Writing harness tests/);
  assert.doesNotMatch(planningHtml, /Developing approved tests/);
}
console.log("29. U3: artifact-readiness 'Writing harness tests' for planning status with new-flow — passed");

// ── 30. R2-6: auto-locked files marked distinctly in locked test files list ──────────
{
  const stageEntry = {
    spec_version: 1,
    status: "stopped",
    budget_seconds: 3600,
    used_seconds: 1000,
    rounds: [],
    lock: {
      locked_at: "2024-01-01T00:10:00Z",
      files: {
        "tests/test_app.py": "hash1",
        "tests/conftest.py": "hash2",
        "pytest.ini": "hash3"
      },
      auto_locked: ["tests/conftest.py", "pytest.ini"]
    },
    lock_violations: [],
    evaluated_round: 0,
    stop_reason: {code: "all_passed", message: "All tests passed."},
    review: null
  };
  const run = makeRun({
    id: "r1", spec_version: 1, test_stages: [stageEntry],
    test_stop_reason: stageEntry.stop_reason
  });
  const cmp = makeComparison({});
  vm.runInContext(`current = ${JSON.stringify(cmp)}; logs = [];`, context);
  const html = vm.runInContext(`testStageSection(${JSON.stringify(run)})`, context);
  // All 3 locked files are shown.
  assert.match(html, /3 locked test files/);
  assert.match(html, /tests\/test_app\.py/);
  assert.match(html, /tests\/conftest\.py/);
  assert.match(html, /pytest\.ini/);
  // Auto-locked files get the auto-locked-badge; harness-listed one does not.
  assert.match(html, /auto-locked-badge/);
  const badgeCount = (html.match(/auto-locked-badge/g) || []).length;
  assert.strictEqual(badgeCount, 2, `Expected 2 auto-locked-badge occurrences (conftest.py + pytest.ini), got ${badgeCount}`);
}
console.log("30. R2-6: auto-locked files marked with auto-locked-badge; harness-listed file is not — passed");

console.log("\nAll new-flow UI tests passed.");
