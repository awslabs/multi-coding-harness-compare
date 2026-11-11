// Tests for test-review failure UI: inline cause in panels and table, failure block
// in Review tests section (U1-U3). Follows the vm-stub pattern of test_follow_ups_ui.cjs.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
require("../arena/static/artifact-readiness.js");
const app = fs.readFileSync(require.resolve("../arena/static/app.js"), "utf8");
const index = fs.readFileSync(require.resolve("../arena/static/index.html"), "utf8");

const nodes = new Map();
function makeNode(id) {
  const classes = new Set();
  return {
    id, value: "", innerHTML: "", disabled: false, textContent: "", placeholder: "",
    parentElement: {classList:{toggle(){},add(){},remove(){},has(){return false;}}},
    classList: {
      toggle(cls, force) {
        const add = force === undefined ? !classes.has(cls) : !!force;
        if(add) classes.add(cls); else classes.delete(cls);
      },
      add(cls) { classes.add(cls); },
      remove(cls) { classes.delete(cls); },
      has(cls) { return classes.has(cls); }
    },
    appendChild(child) { child.parentElement = this; }
  };
}
const document = {
  getElementById(id) {
    if(!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; }
};

const context = vm.createContext({console, document,
  artifactReadiness: globalThis.artifactReadiness,
  localStorage: {getItem() { return null; }}});
vm.runInContext(app.slice(0, app.indexOf('$("new-comparison").onclick=newComparison;')), context);

// Pre-create elements needed for slot-move and early access.
document.getElementById("judge-controls");
document.getElementById("new-judge-slot");
document.getElementById("comparison-judge-slot");

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

const CAUSE = "Bedrock sent no data for 300 s (the planner's stall limit) while us.anthropic.claude-opus-5 was working at effort max; the model likely needed longer than the limit to start responding.";
const FULL_ERROR = "Approved tests could not be developed: Acceptance planning failed: AWSHTTPSConnectionPool(host='bedrock-runtime.us-east-1.amazonaws.com', port=443): Read timed out.";
const CMP_ID = "c9cb792206c3453da6701f13ab4e6e34";

const DEV_FAILURE = {
  stage: "test development",
  error: FULL_ERROR,
  cause: CAUSE,
  attempt: {
    index: 1, model: "us.anthropic.claude-opus-5", effort: "max",
    started_at: 1792501395.64, ended_at: 1792501697.09,
    duration_seconds: 301.45, max_tokens: 100000
  },
  attempt_url: `/api/comparisons/${CMP_ID}/test-review/attempts/1`
};

const PROPOSAL_FAILURE = {
  stage: "test proposal",
  error: "Proposal generation failed: connection error.",
  cause: "The Bedrock endpoint refused the connection while preparing the test proposal.",
  attempt: null,
  attempt_url: null
};

function makeComparison(failure) {
  return {
    id: CMP_ID,
    status: "waiting_test_review",
    spec_version: 1, frozen: true, cancel_requested: false,
    prompt: "Build a CLI tool",
    answers: [], follow_ups: [], artifact_storage: null, artifact_readiness: null,
    runs: [
      {id:"r1",harness:"claude",status:"waiting_test_review",spec_version:1,
       model:"sonnet",requested_model:"sonnet",requested_effort:null,
       follow_ups_delivered:[], model_mismatch:null,evaluation:null,evidence:null,
       usage:null,duration:null,artifacts:null,error:null,project_output:null},
      {id:"r2",harness:"codex",status:"waiting_test_review",spec_version:1,
       model:"sol",requested_model:"sol",requested_effort:null,
       follow_ups_delivered:[], model_mismatch:null,evaluation:null,evidence:null,
       usage:null,duration:null,artifacts:null,error:null,project_output:null},
      {id:"r3",harness:"kiro",status:"waiting_test_review",spec_version:1,
       model:"kiro",requested_model:"kiro",requested_effort:null,
       follow_ups_delivered:[], model_mismatch:null,evaluation:null,evidence:null,
       usage:null,duration:null,artifacts:null,error:null,project_output:null}
    ],
    test_review: {
      id: "rev1", status: failure ? (failure.stage === "test proposal" ? "proposal_failed" : "generation_failed") : "awaiting_review",
      revision: 1, spec_version: 1,
      proposal: {summary: "Test suite summary", test_cases: [], questions: [], assumptions: [], limitations: []},
      answers: {}, history: [], feedback: [],
      error: failure?.error || null,
      generation_status: failure ? "failed" : null,
      generation_attempts: [],
      plan_sha256: null, approved_at: null, test_coverage: "focused",
      mode: null, supplied_plan_sha256: null,
      failure: failure || null
    }
  };
}

// ── 1. index.html contains attempt-dialog ──
assert.match(index, /id="attempt-dialog"/);
assert.match(index, /id="attempt-record-content"/);
console.log("1. index.html contains #attempt-dialog and #attempt-record-content — passed");

// ── 2. Panel shows "Test development failed: <cause>" with Details link ──
vm.runInContext(`current = ${JSON.stringify(makeComparison(DEV_FAILURE))}; logs = []; renderPanels();`, context);
{
  const html = nodes.get("panels").innerHTML;
  assert.match(html, /Test development failed:/);
  assert.match(html, new RegExp("Bedrock sent no data for 300 s"));
  assert.match(html, /href="#test-review-failure"/);
  assert.match(html, /class="test-fail-link".*Details ↓|Details ↓.*class="test-fail-link"/);
}
console.log("2. Panel: 'Test development failed: <cause>' with Details link to #test-review-failure — passed");

// ── 3. Table shows "Test development failed: <cause>" with Details link ──
vm.runInContext(`renderTable();`, context);
{
  const html = nodes.get("comparison-table").innerHTML;
  assert.match(html, /Test development failed:/);
  assert.match(html, new RegExp("Bedrock sent no data for 300 s"));
  assert.match(html, /href="#test-review-failure"/);
  assert.match(html, /class="test-fail-link"/);
}
console.log("3. Table: 'Test development failed: <cause>' with Details link to #test-review-failure — passed");

// ── 4. Failure block in Review tests section ──
vm.runInContext(`testReviewSignature = ""; renderTestReview();`, context);
{
  const html = nodes.get("test-review").innerHTML;
  // id on failure block
  assert.match(html, /id="test-review-failure"/);
  // stage
  assert.match(html, /Stage: test development/);
  // plain cause
  assert.match(html, /Bedrock sent no data for 300 s/);
  // full error (monospace pre)
  assert.match(html, /<pre[^>]*>.*AWSHTTPSConnectionPool.*<\/pre>/s);
  // attempt summary: model, effort, max_tokens
  assert.match(html, /us\.anthropic\.claude-opus-5/);
  assert.match(html, /effort max/);
  assert.match(html, /output limit/);
  // View attempt record button
  assert.match(html, /View attempt record/);
  assert.match(html, new RegExp(`/api/comparisons/${CMP_ID}/test-review/attempts/1`));
}
console.log("4. Failure block: stage, cause, error, attempt summary, View attempt record link — passed");

// ── 5. Proposal failure variant: stage "test proposal" ──
vm.runInContext(`current = ${JSON.stringify(makeComparison(PROPOSAL_FAILURE))}; logs = []; testReviewSignature = ""; renderPanels(); renderTable(); renderTestReview();`, context);
{
  const panelsHtml = nodes.get("panels").innerHTML;
  const tableHtml = nodes.get("comparison-table").innerHTML;
  const reviewHtml = nodes.get("test-review").innerHTML;
  // Panel and table: "Test proposal failed: <cause>"
  assert.match(panelsHtml, /Test proposal failed:/);
  assert.match(panelsHtml, /The Bedrock endpoint refused the connection/);
  assert.match(tableHtml, /Test proposal failed:/);
  // Failure block: stage "test proposal", no attempt (null)
  assert.match(reviewHtml, /Stage: test proposal/);
  assert.match(reviewHtml, /The Bedrock endpoint refused the connection/);
  // No attempt_url → no View attempt record button
  assert.doesNotMatch(reviewHtml, /View attempt record/);
}
console.log("5. Proposal failure: 'Test proposal failed: <cause>' in panel/table/block, no attempt record link — passed");

// ── 6. No failure: none of the failure UI is rendered ──
vm.runInContext(`current = ${JSON.stringify(makeComparison(null))}; logs = []; testReviewSignature = ""; renderPanels(); renderTable(); renderTestReview();`, context);
{
  const panelsHtml = nodes.get("panels").innerHTML;
  const tableHtml = nodes.get("comparison-table").innerHTML;
  const reviewHtml = nodes.get("test-review").innerHTML;
  assert.doesNotMatch(panelsHtml, /panel-fail-strip/);
  assert.doesNotMatch(panelsHtml, /test-fail-link/);
  assert.doesNotMatch(tableHtml, /test-fail-cause/);
  assert.doesNotMatch(tableHtml, /test-fail-link/);
  assert.doesNotMatch(reviewHtml, /test-review-failure/);
  assert.doesNotMatch(reviewHtml, /View attempt record/);
}
console.log("6. No failure: no failure UI rendered in panel, table, or review section — passed");

// ── 7. Details link href targets #test-review-failure specifically ──
vm.runInContext(`current = ${JSON.stringify(makeComparison(DEV_FAILURE))}; logs = []; renderPanels(); renderTable();`, context);
{
  const panelsHtml = nodes.get("panels").innerHTML;
  const tableHtml = nodes.get("comparison-table").innerHTML;
  // Verify href is exactly #test-review-failure (not just contains "test-review")
  assert.match(panelsHtml, /href="#test-review-failure"/);
  assert.match(tableHtml, /href="#test-review-failure"/);
}
console.log("7. Details link href is exactly #test-review-failure in both panel and table — passed");

console.log("All test-review failure UI tests passed.");
