const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
require("../arena/static/artifact-readiness.js");
const app = fs.readFileSync(require.resolve("../arena/static/app.js"), "utf8");

function browser() {
  const nodes = new Map(), requests = [];
  const classes = () => ({toggle(){},add(){},remove(){}});
  const document = {
    getElementById(id) {
      if(!nodes.has(id)) nodes.set(id, {value:"",innerHTML:"",textContent:"",disabled:false,
        classList:classes(),parentElement:{classList:classes()},focus(){}});
      return nodes.get(id);
    },
    querySelector() { return {value:"focused"}; },
    querySelectorAll() { return []; }
  };
  const context = vm.createContext({console,document,requests,
    artifactReadiness:globalThis.artifactReadiness,
    localStorage:{getItem(){return null;}}});
  vm.runInContext(app.slice(0,app.indexOf('$("new-comparison").onclick=newComparison;')),context);
  vm.runInContext(`
    api=async(path,data)=>{requests.push({path,data});return {id:"created"};};
    toast=message=>{globalThis.lastToast=message;};
    refresh=async()=>{}; select=async()=>{};
    system={artifact_readiness:{enabled:false}, test_coverage:{supported:true,worker_ready:true},
      harness_selection:{supported:true,worker_ready:true},
      harnesses:{claude:{},codex:{},kiro:{}}};
    harnessConfigPlan={claude:{sources:[{},{}],mode:"configured"},
      codex:{sources:[{},{}],mode:"configured"},kiro:{sources:[{},{}],mode:"configured"}};
    renderModels(); renderHarnessSelection();
  `,context);
  vm.runInContext(app.slice(app.indexOf('$("task-form").onsubmit='),app.indexOf("const initialToken")),context);
  return {context,nodes,requests,document,
    run:code=>vm.runInContext(code,context),
    chosen:()=>JSON.parse(vm.runInContext("JSON.stringify(selectedHarnesses())",context)),
    click:h=>nodes.get("harness-toggle-"+h).onclick(),
    submit:()=>nodes.get("task-form").onsubmit({preventDefault(){}})};
}

(async()=>{
  const ui=browser();
  assert.deepEqual(ui.chosen(),["claude","codex","kiro"]);
  assert.equal((ui.nodes.get("harness-toggles").innerHTML.match(/role="switch"/g)||[]).length,3);
  ui.click("claude"); ui.click("kiro");
  assert.deepEqual(ui.chosen(),["codex"]);
  assert.match(ui.nodes.get("harness-toggles").innerHTML,/id="harness-toggle-claude"[^>]+aria-checked="false"/);
  assert.match(ui.nodes.get("harness-toggles").innerHTML,/id="harness-toggle-codex"[^>]+aria-checked="true"/);
  assert.equal(ui.nodes.get("model-claude").disabled,true);
  assert.equal(ui.nodes.get("effort-claude").disabled,true);
  assert.equal(ui.nodes.get("config-choice-claude").disabled,true);
  assert.equal(ui.nodes.get("model-codex").disabled,false);
  ui.click("codex");
  assert.deepEqual(ui.chosen(),["codex"],"The last enabled harness cannot be turned off");
  assert.match(ui.nodes.get("harness-selection-status").textContent,/Keep at least one/);
  ui.run("renderHarnessSelection(); updateTestCoverageAvailability(); renderModels();");
  assert.deepEqual(ui.chosen(),["codex"],"Polling and model refresh preserve the draft selection");

  for(const h of ["claude","codex","kiro"]) {
    ui.document.getElementById("model-"+h).value=h+"-model";
    ui.document.getElementById("effort-"+h).value="high";
    ui.document.getElementById("config-choice-"+h).value="none";
  }
  ui.document.getElementById("prompt").value="Build a CLI";
  ui.document.getElementById("project-mode").value="empty";
  await ui.submit();
  const create=JSON.parse(JSON.stringify(ui.requests.at(-1)));
  assert.equal(create.path,"/comparisons");
  assert.deepEqual(create.data.harnesses,["codex"]);
  assert.deepEqual(create.data.models,{codex:"codex-model"});
  assert.deepEqual(create.data.efforts,{codex:"high"});
  assert.deepEqual(create.data.configuration_choices,{codex:"none"});

  // Fail closed even if the submit handler is called without browser validation.
  ui.run('harnessDrafts.set("new", []);');
  ui.document.getElementById("prompt").value="Must not send";
  const count=ui.requests.length;
  await ui.submit();
  assert.equal(ui.requests.length,count);
  assert.equal(ui.nodes.get("submit-task").disabled,true);
  assert.match(ui.context.lastToast,/at least one/);
  for(const capability of ["undefined", "{supported:true,worker_ready:false}"]) {
    ui.run(`harnessDrafts.set("new", ["codex"]); system.harness_selection=${capability};`);
    await ui.submit();
    assert.equal(ui.requests.length,count,"An old API/worker must never ignore harness choices");
    assert.equal(ui.nodes.get("submit-task").disabled,true);
    assert.match(ui.context.lastToast,/Restart the API and idle worker/);
  }
  ui.run("system.harness_selection={supported:true,worker_ready:true};");

  ui.run(`
    current={id:"existing",status:"complete",runs:[{harness:"claude"},{harness:"codex"}]};
    renderHarnessSelection(); updateTestCoverageAvailability();
  `);
  assert.deepEqual(ui.chosen(),["claude","codex"]);
  assert.match(ui.nodes.get("harness-toggles").innerHTML,/id="harness-toggle-kiro"[^>]+aria-checked="false"[^>]+disabled/);
  ui.click("kiro");
  assert.deepEqual(ui.chosen(),["claude","codex"],"Absent runs cannot be enabled halfway through a comparison");
  ui.click("claude");
  assert.deepEqual(ui.chosen(),["codex"]);
  ui.run("renderHarnessSelection();");
  assert.equal(ui.nodes.get("follow-up-banner").textContent,"Follow-up to: Codex");
  ui.document.getElementById("prompt").value="Add JSON output";
  await ui.submit();
  const follow=JSON.parse(JSON.stringify(ui.requests.at(-1)));
  assert.deepEqual(follow,{path:"/comparisons/existing/revisions",data:{text:"Add JSON output",harnesses:["codex"]}});
  assert.equal(ui.context.lastToast,"Follow-up sent to Codex.");
  assert.deepEqual(ui.chosen(),["codex"],"A sent follow-up retains its chosen recipients");
  const followCount=ui.requests.length;
  ui.run('harnessDrafts.set("existing", []);');
  ui.document.getElementById("prompt").value="Do not dispatch";
  await ui.submit();
  assert.equal(ui.requests.length,followCount,"An empty follow-up selection must not submit");
  ui.run('harnessDrafts.set("existing", ["codex"]); system.harness_selection.worker_ready=false;');
  await ui.submit();
  assert.equal(ui.requests.length,followCount,"A stale worker must not receive a follow-up selection");
  assert.match(ui.context.lastToast,/Restart the API and idle worker/);
  ui.run('system.harness_selection.worker_ready=true;');
  ui.run(`
    current={id:"another",status:"complete",runs:[{harness:"kiro"}]};
    renderHarnessSelection();
    current={id:"existing",status:"complete",runs:[{harness:"claude"},{harness:"codex"}]};
    renderHarnessSelection();
  `);
  assert.deepEqual(ui.chosen(),["codex"],"Switching comparisons restores each recipient draft");
  ui.click("codex");
  assert.deepEqual(ui.chosen(),["codex"],"Follow-ups also require at least one harness");

  // Edits during an in-flight request cannot silently change its recipients or duplicate it.
  ui.run(`api=(path,data)=>{requests.push({path,data});return new Promise(resolve=>{globalThis.finish=resolve;});};`);
  ui.document.getElementById("prompt").value="Add help";
  const pending=ui.submit(), sendingCount=ui.requests.length;
  ui.click("claude");
  assert.deepEqual(ui.chosen(),["codex"]);
  ui.run("renderHarnessSelection(); updateTestCoverageAvailability();");
  assert.equal(ui.nodes.get("submit-task").disabled,true);
  await ui.submit();
  assert.equal(ui.requests.length,sendingCount);
  ui.context.finish({});
  await pending;

  // Delivery indicators are scoped to recipients, including historical legacy follow-ups.
  ui.run(`current.follow_ups=[{id:"targeted",harnesses:["codex"]}];`);
  assert.equal(ui.run('followUpPanelStatus({harness:"claude",follow_ups_delivered:[]})'),"");
  assert.match(ui.run('followUpPanelStatus({harness:"codex",follow_ups_delivered:[]})'),/Follow-up pending/);
  ui.run(`current.follow_ups.push({id:"legacy"});`);
  assert.match(ui.run('followUpPanelStatus({harness:"claude",follow_ups_delivered:["legacy"]})'),/Follow-ups delivered \(1\)/);

  // An excluded follow-up can invalidate shared evaluation without running this harness.
  ui.run(`
    current.spec_version=2; current.prompt="Build a CLI";
    current.runs=[{id:"retained",harness:"claude",status:"evaluated_incomplete",spec_version:1,
      evaluation:null,error:null,
      evaluation_scope_note:"Implementation saved for specification v1. A follow-up sent to other harnesses changed the shared evaluation; this harness was not sent that follow-up. <saved>"}];
  `);
  assert.match(ui.run("activity(current.runs[0])"),/this harness was not sent that follow-up/);
  ui.run(`logs=[{run_id:"retained",type:"text",at:1,data:{text:"Previous coding completed"}}];`);
  const activity=ui.run("activity(current.runs[0])");
  assert.match(activity,/Previous coding completed/);
  assert.match(activity,/evaluation-scope-note/);
  assert.match(activity,/this harness was not sent that follow-up/);
  assert.match(activity,/&lt;saved&gt;/);
  assert.doesNotMatch(activity,/<saved>/);
  assert.match(ui.run("verificationScope(current.runs[0])"),/Verification scope.*this harness was not sent that follow-up/s);
  ui.run("renderTable();");
  assert.match(ui.nodes.get("comparison-table").innerHTML,/Implementation saved · different follow-up requirements\./);
  assert.match(ui.nodes.get("comparison-table").innerHTML,/View verification scope for Claude Code/);
  assert.match(ui.nodes.get("comparison-table").innerHTML,/this harness was not sent that follow-up/);
  await ui.run("reviewRun=current.runs[0]; reviewTab('evidence');");
  assert.match(ui.nodes.get("review-content").innerHTML,/Verification scope.*this harness was not sent that follow-up/s);
  ui.run(`
    renderQuestions=()=>{}; renderPanels=()=>{}; renderTable=()=>{}; renderJudge=()=>{};
    renderTestReview=()=>{}; renderArtifactReadiness=()=>{}; updateProjectControls=()=>{};
    current.answers=[{kind:"follow_up",answer:"Add JSON output",harnesses:["codex"]}];
    current.prompt="Build a CLI";
    render();
  `);
  assert.match(ui.nodes.get("conversation").innerHTML,/Follow-up to Codex/);
  assert.doesNotMatch(ui.nodes.get("conversation").innerHTML,/Follow-up to Claude/);
  console.log("Harness selection UI: switches, validation, payload routing, draft persistence and delivery indicators passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
