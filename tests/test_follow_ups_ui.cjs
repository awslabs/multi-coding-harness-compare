// Tests for follow-up UI: banner, placeholder, button text, disabled states,
// conversation labels, and panel delivery indicators (contract item 6).
// Follows the vm-stub pattern of tests/test_model_selection_ui.cjs.
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
document.getElementById("follow-up-banner");
document.getElementById("follow-up-block-reason");

// Common system/catalogue state.
vm.runInContext(`
  system = {
    judge_model: null, evaluation_planner: null, planning_requires_claude: false,
    harnesses: {claude:{model:"sonnet"}, codex:{model:"sol"}, kiro:{model:"kiro"}},
    test_coverage: {supported: true, worker_ready: true},
    harness_selection: {supported: true, worker_ready: true},
    artifact_readiness: {enabled: false}
  };
  modelCatalogue = {harnesses: {
    claude: {configured_default:"sonnet", models:[], source:""},
    codex:  {configured_default:"sol",    models:[], source:""},
    kiro:   {configured_default:"kiro",   models:[], source:""}
  }};
  judgeCatalogue = {checked_at: 100, models: [], warnings: [], profile:"default", region:"us-east-1"};
  const allRuns = ["claude", "codex", "kiro"].map(harness => ({harness}));
  // Mock sub-renders that are not under test here.
  renderQuestions=()=>{}; renderPanels=()=>{}; renderTable=()=>{}; renderJudge=()=>{};
  renderTestReview=()=>{}; renderArtifactReadiness=()=>{}; updateProjectControls=()=>{};
`, context);

// ── 1. index.html contains follow-up-banner and follow-up-block-reason elements ──
assert.match(index, /id="follow-up-banner"/);
assert.match(index, /id="follow-up-block-reason"/);
console.log("1. HTML contains #follow-up-banner and #follow-up-block-reason — passed");

// ── 2. No comparison: banner hidden, placeholder "Describe…", button "Start comparison" ──
vm.runInContext(`current = null; render();`, context);
assert(nodes.get("follow-up-banner").classList.has("hidden"), "banner should be hidden with no comparison");
assert.match(nodes.get("prompt").placeholder, /Describe what you want to build/);
assert.match(nodes.get("submit-task").innerHTML, /Start comparison/);
assert.doesNotMatch(nodes.get("submit-task").innerHTML, /all harnesses/);
console.log("2. No comparison: banner hidden, placeholder and button text correct — passed");

// ── 3. Comparison selected: banner visible, follow-up placeholder, "Send follow-up" ──
vm.runInContext(`
  current = {id:"cmp-1", status:"complete", spec_version:1, frozen:true, cancel_requested:false,
    answers:[], runs:allRuns, follow_ups:[], artifact_storage:null, test_review:null};
  render();
`, context);
assert(!nodes.get("follow-up-banner").classList.has("hidden"), "banner should be visible with comparison");
assert.equal(nodes.get("follow-up-banner").textContent, "Follow-up to: Claude Code, Codex, Kiro");
assert.match(nodes.get("prompt").placeholder, /Send a follow-up instruction/);
assert.match(nodes.get("submit-task").innerHTML, /Send follow-up/);
console.log("3. Comparison selected: banner visible, follow-up placeholder and button text — passed");

// ── 4. Button enabled with comparison even when test-coverage options are unavailable ──
vm.runInContext(`
  system.test_coverage = {supported: false, worker_ready: false};
  current = {id:"cmp-2", status:"complete", spec_version:1, frozen:true, cancel_requested:false,
    answers:[], runs:allRuns, follow_ups:[], artifact_storage:null, test_review:null};
  updateTestCoverageAvailability();
`, context);
assert.equal(nodes.get("submit-task").disabled, false, "button must be enabled with comparison even when test coverage unavailable");
// Restore
vm.runInContext(`system.test_coverage = {supported: true, worker_ready: true};`, context);
console.log("4. Button enabled with comparison when test coverage unavailable — passed");

// ── 5. Button disabled with reason during artifact transfer statuses ──
for(const status of ["archiving", "archive_queued", "restoring", "storage_failed"]) {
  vm.runInContext(`
    current = {id:"cmp-3", status:"${status}", spec_version:1, frozen:true, cancel_requested:false,
      answers:[], runs:allRuns, follow_ups:[], artifact_storage:{status:"archiving"}, test_review:null};
    updateTestCoverageAvailability();
  `, context);
  assert.equal(nodes.get("submit-task").disabled, true, `button must be disabled during ${status}`);
  assert(!nodes.get("follow-up-block-reason").classList.has("hidden"), `reason must be visible during ${status}`);
  assert.match(nodes.get("follow-up-block-reason").textContent, /artifact transfer/);
}
console.log("5. Button disabled with reason during artifact transfer statuses — passed");

// ── 6. Button disabled with reason during re-scoring statuses ──
for(const status of ["evaluation_queued", "verification_queued"]) {
  vm.runInContext(`
    current = {id:"cmp-4", status:"${status}", spec_version:1, frozen:true, cancel_requested:false,
      answers:[], runs:allRuns, follow_ups:[], artifact_storage:null, test_review:null};
    updateTestCoverageAvailability();
  `, context);
  assert.equal(nodes.get("submit-task").disabled, true, `button must be disabled during ${status}`);
  assert(!nodes.get("follow-up-block-reason").classList.has("hidden"), `reason must be visible during ${status}`);
  assert.match(nodes.get("follow-up-block-reason").textContent, /re-scoring/);
}
console.log("6. Button disabled with reason during re-scoring statuses — passed");

// ── 7. Button enabled and reason hidden for normal comparison status ──
vm.runInContext(`
  current = {id:"cmp-5", status:"complete", spec_version:1, frozen:true, cancel_requested:false,
    answers:[], runs:allRuns, follow_ups:[], artifact_storage:null, test_review:null};
  updateTestCoverageAvailability();
`, context);
assert.equal(nodes.get("submit-task").disabled, false, "button must be enabled for normal status");
assert(nodes.get("follow-up-block-reason").classList.has("hidden"), "reason must be hidden for normal status");
console.log("7. Button enabled and reason hidden for normal comparison status — passed");

// ── 8. Conversation: follow-up answer labeled "Follow-up to Claude Code, Codex, Kiro" ──
vm.runInContext(`
  current = {id:"cmp-6", status:"complete", spec_version:1, frozen:true, cancel_requested:false,
    prompt:"Build a CLI tool",
    answers:[
      {question:"Clarify scope", answer:"Keep it simple", kind:null},
      {question:"User specification revision", answer:"Add JSON output", kind:"follow_up"}
    ],
    runs:allRuns, follow_ups:[{id:"fu1",text:"Add JSON output",created_at:1000}],
    artifact_storage:null, test_review:null};
  render();
`, context);
{
  const html = nodes.get("conversation").innerHTML;
  assert.match(html, /Follow-up to Claude Code, Codex, Kiro/);
  assert.match(html, /Shared requirement · Clarify scope/);
  assert.doesNotMatch(html, /Shared requirement · User specification revision/);
  assert.match(html, /class="conversation-answer follow-up"/);
  assert.match(html, /class="conversation-answer"/);
}
console.log("8. Conversation: follow-up labeled 'Follow-up to Claude Code, Codex, Kiro', regular labeled 'Shared requirement' — passed");

// ── 9. Conversation: old-style follow-up (no kind, question="User specification revision") ──
vm.runInContext(`
  current.answers = [{question:"User specification revision", answer:"Old follow-up", kind:undefined}];
  render();
`, context);
{
  const html = nodes.get("conversation").innerHTML;
  assert.match(html, /Follow-up to Claude Code, Codex, Kiro/);
  assert.doesNotMatch(html, /Shared requirement · User specification revision/);
}
console.log("9. Old-style follow-up (no kind) labeled correctly — passed");

// ── 10. Panel: "Follow-up pending" when undelivered follow-ups exist ──
// Restore renderPanels to real, test directly with a follow-up scenario.
vm.runInContext(`
  current = {
    id:"cmp-7", status:"developing", spec_version:1, frozen:true, cancel_requested:false,
    prompt:"Build a tool",
    answers:[], follow_ups:[{id:"fu1",text:"Add JSON",created_at:1000}],
    artifact_storage:null, test_review:null,
    artifact_readiness:{enabled:false},
    runs:[
      {id:"r1",harness:"claude",status:"developing",spec_version:1,
       model:"sonnet",requested_model:"sonnet",requested_effort:null,
       follow_ups_delivered:[],
       model_mismatch:null,evaluation:null,evidence:null,usage:null,
       duration:null,artifacts:null,error:null,project_output:null},
      {id:"r2",harness:"codex",status:"developing",spec_version:1,
       model:"sol",requested_model:"sol",requested_effort:null,
       follow_ups_delivered:["fu1"],
       model_mismatch:null,evaluation:null,evidence:null,usage:null,
       duration:null,artifacts:null,error:null,project_output:null},
      {id:"r3",harness:"kiro",status:"developing",spec_version:1,
       model:"kiro",requested_model:"kiro",requested_effort:null,
       follow_ups_delivered:["fu1"],
       model_mismatch:null,evaluation:null,evidence:null,usage:null,
       duration:null,artifacts:null,error:null,project_output:null}
    ]
  };
  logs=[];
  renderPanels=undefined; // reset to real
`, context);
// We need the real renderPanels — re-run the function from source.
vm.runInContext(
  app.slice(app.indexOf("function followUpPanelStatus"),
            app.indexOf("\n$")),
  context
);
vm.runInContext(`renderPanels();`, context);
{
  const html = nodes.get("panels").innerHTML;
  // claude run has no follow-up delivered: pending
  assert.match(html, /Follow-up pending/);
  // codex and kiro have it delivered: delivered(1)
  assert.match(html, /Follow-ups delivered \(1\)/);
}
console.log("10. Panel: 'Follow-up pending' for undelivered, 'Follow-ups delivered (n)' for delivered — passed");

// ── 10b. Panel: "Follow-up in progress" while the turn carrying it runs ──
{
  const inProgress = vm.runInContext(`(() => { current = {follow_ups: [{id: "f1", text: "x"}], runs: allRuns};
    return followUpPanelStatus({status: "developing", follow_ups_delivered: [], attempts: [{follow_ups: ["f1"]}]}); })()`, context);
  assert.match(inProgress, /Follow-up in progress/);
  const queued = vm.runInContext(`followUpPanelStatus({status: "queued", follow_ups_delivered: [], attempts: [{follow_ups: ["f1"]}]})`, context);
  assert.match(queued, /Follow-up pending/);
  console.log("10b. Panel: 'Follow-up in progress' while the carrying turn runs — passed");
}

// ── 11. Panel: no follow-up indicator when comparison has no follow-ups ──
vm.runInContext(`
  current.follow_ups = [];
  renderPanels();
`, context);
{
  const html = nodes.get("panels").innerHTML;
  assert.doesNotMatch(html, /Follow-up pending/);
  assert.doesNotMatch(html, /Follow-ups delivered/);
}
console.log("11. Panel: no follow-up indicator when no follow-ups exist — passed");

console.log("All follow-up UI tests passed.");
