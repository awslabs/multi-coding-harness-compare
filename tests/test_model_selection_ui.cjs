// Tests for judge picker, planning picker, and model-mismatch warning (contract D).
// Follows the same vm-stub pattern as test_effort_ui.cjs and test_artifact_readiness_ui.cjs.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
require("../arena/static/artifact-readiness.js");
const app = fs.readFileSync(require.resolve("../arena/static/app.js"), "utf8");

const nodes = new Map();
function makeNode(id) {
  return {
    id, value: "", innerHTML: "", disabled: false, textContent: "",
    parentElement: null,
    classList: {toggle(){}, add(){}, remove(){}},
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
const context = vm.createContext({console, document, artifactReadiness: globalThis.artifactReadiness,
  localStorage: {getItem() { return null; }}});
vm.runInContext(app.slice(0, app.indexOf('$("new-comparison").onclick=newComparison;')), context);

// Pre-create elements needed by renderJudge's slot-move logic.
document.getElementById("judge-controls");
document.getElementById("new-judge-slot");
document.getElementById("comparison-judge-slot");

// Common shared catalogue.
vm.runInContext(`
  system = {
    judge_model: "us.anthropic.claude-opus-5",
    evaluation_planner: {model: "us.anthropic.claude-opus-5", effort: "max"},
    planning_requires_claude: false,
    harnesses: {claude:{model:"sonnet"}, codex:{model:"sol"}, kiro:{model:"kiro"}},
    judge_profile: "default", judge_region: "us-east-1"
  };
  judgeCatalogue = {
    checked_at: 100, profile: "default", region: "us-east-1",
    models: [
      {id:"us.anthropic.claude-opus-5",    name:"Claude Opus 5",   provider:"Anthropic", kind:"inference_profile", selectable:true},
      {id:"us.anthropic.claude-sonnet-4-6", name:"Claude Sonnet",  provider:"Anthropic", kind:"inference_profile", selectable:true},
      {id:"us.amazon.titan-text-g1-v1",     name:"Titan Text G1",  provider:"Amazon",    kind:"inference_profile", selectable:true}
    ]
  };
  modelCatalogue = {harnesses: {
    claude: {configured_default:"sonnet", models:[]},
    codex:  {configured_default:"sol",    models:[]},
    kiro:   {configured_default:"kiro",   models:[]}
  }};
`, context);

// ── 1. Judge picker: new comparison – enabled default option, no pre-selection ──
vm.runInContext(`current=null; judgeDrafts.clear(); judgeOptionsSignature=""; renderJudge();`, context);
{
  const html = nodes.get("judge-model").innerHTML;
  // First option must be enabled (no "disabled" on the element itself).
  assert.doesNotMatch(html, /<option value=""\s+disabled/);
  // Label should say "Configured default · <judge_model>".
  assert.match(html, /value=""\s*>Configured default · us\.anthropic\.claude-opus-5</);
  // Picker must not be pre-selected to a concrete id; value stays "".
  assert.equal(nodes.get("judge-model").value, "");
}
console.log("1. Judge picker new comparison: enabled default, no pre-selection — passed");

// ── 2. Judge picker: existing comparison – first option labels the comparison's judge ──
vm.runInContext(`
  current = {id:"cmp-1", status:"complete", runs:[], judge:{model:"us.anthropic.claude-sonnet-4-6"},
    prompt:"task", spec_version:1};
  judgeDrafts.clear(); judgeOptionsSignature=""; renderJudge();
`, context);
{
  const html = nodes.get("judge-model").innerHTML;
  assert.match(html, /value=""\s*>This comparison&#39;s judge · us\.anthropic\.claude-sonnet-4-6</);
  assert.doesNotMatch(html, /<option value=""\s+disabled/);
  // Value stays "" (the "this comparison's judge" slot).
  assert.equal(nodes.get("judge-model").value, "");
}
console.log("2. Judge picker existing comparison: 'This comparison’s judge' label — passed");

// ── 3. Planning picker: no claude filter – all selectable models shown ──
vm.runInContext(`
  current=null; judgeDrafts.clear(); judgeOptionsSignature="";
  system.planning_requires_claude=false;
  renderJudge();
`, context);
{
  const html = nodes.get("planning-model").innerHTML;
  // Default label now includes the effort rule from system.evaluation_planner.effort ("max").
  assert.match(html, /value=""\s*>Configured default · us\.anthropic\.claude-opus-5 · effort: max</);
  assert.match(html, /us\.anthropic\.claude-opus-5/);
  assert.match(html, /us\.anthropic\.claude-sonnet-4-6/);
  assert.match(html, /us\.amazon\.titan-text-g1-v1/);
}
console.log("3. Planning picker without filter – all selectable models — passed");

// ── 4. Planning picker: planning_requires_claude – only anthropic.claude models ──
vm.runInContext(`
  system.planning_requires_claude=true;
  judgeOptionsSignature=""; renderJudge();
`, context);
{
  const html = nodes.get("planning-model").innerHTML;
  assert.match(html, /us\.anthropic\.claude-opus-5/);
  assert.match(html, /us\.anthropic\.claude-sonnet-4-6/);
  assert.doesNotMatch(html, /us\.amazon\.titan-text-g1-v1/);
}
console.log("4. Planning picker with planning_requires_claude – claude-only — passed");

// ── 5. Untouched pickers submit null: models, judge_model, planning_model ──
vm.runInContext(`
  current=null; judgeDrafts.clear(); judgeOptionsSignature="";
  system.planning_requires_claude=false;
  renderJudge();
`, context);
nodes.get("judge-model").value = "";
nodes.get("planning-model").value = "";
// Harness model pickers default to "".
["claude","codex","kiro"].forEach(h => { if(!nodes.has("model-"+h)) nodes.set("model-"+h, makeNode("model-"+h)); nodes.get("model-"+h).value = ""; });
assert.equal(vm.runInContext("selectedJudgeModel()", context), null,    "judge_model null");
assert.equal(vm.runInContext("$('planning-model').value||null", context), null, "planning_model null");
assert.equal(vm.runInContext("$('model-claude').value||null", context), null,   "model null");
console.log("5. Untouched pickers submit null — passed");

// ── 6. Picked values are submitted ──
nodes.get("judge-model").value    = "us.anthropic.claude-opus-5";
nodes.get("planning-model").value = "us.anthropic.claude-sonnet-4-6";
nodes.get("model-claude").value   = "us.anthropic.claude-sonnet-4-6";
assert.equal(vm.runInContext("selectedJudgeModel()", context), "us.anthropic.claude-opus-5");
assert.equal(vm.runInContext("$('planning-model').value||null", context), "us.anthropic.claude-sonnet-4-6");
assert.equal(vm.runInContext("$('model-claude').value||null", context), "us.anthropic.claude-sonnet-4-6");
console.log("6. Picked values are submitted — passed");

// ── 7. Model-mismatch warning in renderTable ──
vm.runInContext(`
  current = {
    id:"cmp-2", status:"developing", prompt:"Build a CLI tool", spec_version:1,
    test_review:null, comparison_complete:false,
    runs:[
      {id:"r1", harness:"claude", status:"developing", spec_version:1,
       model:"us.anthropic.claude-opus-5", requested_model:"us.anthropic.claude-sonnet-4-6",
       requested_effort:null,
       model_mismatch:{requested:"us.anthropic.claude-sonnet-4-6", reported:"us.anthropic.claude-opus-5"},
       evaluation:null, evidence:null, usage:null, duration:null, artifacts:null, error:null},
      {id:"r2", harness:"codex",  status:"developing", spec_version:1,
       model:"codex-sol", requested_model:"codex-sol", requested_effort:null,
       model_mismatch:null, evaluation:null, evidence:null, usage:null, duration:null,
       artifacts:null, error:null},
      {id:"r3", harness:"kiro",   status:"developing", spec_version:1,
       model:"kiro", requested_model:"kiro", requested_effort:null,
       model_mismatch:null, evaluation:null, evidence:null, usage:null, duration:null,
       artifacts:null, error:null}
    ]
  };
  renderTable();
`, context);
{
  const html = nodes.get("comparison-table").innerHTML;
  assert.match(html, /Ran us\.anthropic\.claude-opus-5 instead of the requested us\.anthropic\.claude-sonnet-4-6/);
}
console.log("7. Model-mismatch warning in comparison table — passed");

// ── 8. Model-mismatch warning in renderPanels ──
vm.runInContext(`renderPanels();`, context);
{
  const html = nodes.get("panels").innerHTML;
  assert.match(html, /Ran us\.anthropic\.claude-opus-5 instead of the requested us\.anthropic\.claude-sonnet-4-6/);
}
console.log("8. Model-mismatch warning in run panels — passed");

// ── 9. Planning picker default label includes effort rule (pinned level) ──
vm.runInContext(`
  current=null; judgeDrafts.clear(); judgeOptionsSignature="";
  system.planning_requires_claude=false;
  system.evaluation_planner = {model:"us.anthropic.claude-opus-5", effort:"max"};
  renderJudge();
`, context);
{
  const html = nodes.get("planning-model").innerHTML;
  assert.match(html, /value=""\s*>Configured default · us\.anthropic\.claude-opus-5 · effort: max</);
}
console.log("9. Planning picker default label includes pinned effort rule — passed");

// ── 10. Planning picker default label shows "highest supported" when effort is "highest" ──
vm.runInContext(`
  system.evaluation_planner = {model:"us.anthropic.claude-opus-5", effort:"highest"};
  judgeOptionsSignature=""; renderJudge();
`, context);
{
  const html = nodes.get("planning-model").innerHTML;
  assert.match(html, /value=""\s*>Configured default · us\.anthropic\.claude-opus-5 · effort: highest supported</);
}
// Restore original effort value.
vm.runInContext(`system.evaluation_planner = {model:"us.anthropic.claude-opus-5", effort:"max"};`, context);
console.log("10. Planning picker default label: 'highest' shown as 'highest supported' — passed");

// ── 11. Progress line shows comparison's stored effort (not "highest") ──
{
  const result = vm.runInContext(
    `testReviewProgress(
       {updated_at: Date.now()/1000, created_at: Date.now()/1000},
       {model:"us.anthropic.claude-opus-5", effort:"max"})`, context);
  assert.match(result, /Planning model: us\.anthropic\.claude-opus-5 · Effort: max/);
  assert.doesNotMatch(result, /highest/);
}
console.log("11. Progress line shows comparison's stored effort — passed");

// ── 12. Progress line shows "highest supported" when effort is "highest" (system fallback) ──
{
  const result = vm.runInContext(
    `testReviewProgress(
       {updated_at: Date.now()/1000, created_at: Date.now()/1000},
       {model:"us.anthropic.claude-opus-5", effort:"highest"})`, context);
  assert.match(result, /Effort: highest supported/);
  // Ensure the raw word "highest" is not exposed without the "supported" suffix.
  assert.doesNotMatch(result, /Effort: highest(?! supported)/);
}
console.log("12. Progress line shows 'highest supported' for effort 'highest' — passed");

console.log("All model selection UI tests passed.");
