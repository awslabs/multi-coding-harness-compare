const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
require("../arena/static/artifact-readiness.js");
const app = fs.readFileSync(require.resolve("../arena/static/app.js"), "utf8");
const index = fs.readFileSync(require.resolve("../arena/static/index.html"), "utf8");

function browser() {
  const nodes = new Map(), requests = [], reviewRadios = [
    {value:"focused",checked:false}, {value:"comprehensive",checked:true}
  ];
  const selection = {value:"focused"};
  const classes = () => ({toggle(){},add(){},remove(){}});
  const document = {
    getElementById(id) {
      if(!nodes.has(id)) nodes.set(id, {value:"",innerHTML:"",classList:classes(),
        parentElement:{classList:classes()},focus(){}});
      return nodes.get(id);
    },
    querySelector(selector) {
      return selector === '[name="test-coverage"]:checked' ? selection : null;
    },
    querySelectorAll(selector) {
      return selector === '[name="review-test-coverage"]' ? reviewRadios : [];
    }
  };
  const context = vm.createContext({console,document,
    artifactReadiness:globalThis.artifactReadiness,requests,
    localStorage:{getItem(){return null;}}});
  vm.runInContext(app.slice(0,app.indexOf('$("new-comparison").onclick=newComparison;')),context);
  vm.runInContext(`
    api=async(path,data)=>{requests.push({path,data});return {id:"created"};};
    toast=message=>{globalThis.lastToast=message;};
    refresh=async()=>{};
    system={artifact_readiness:{enabled:true,ready:true},
      harness_selection:{supported:true,worker_ready:true},
      test_coverage:{supported:true,worker_ready:true}};
  `,context);
  return {context,nodes,requests,reviewRadios,selection,document};
}

(async()=>{
  const newForm = browser();
  assert.match(index, /<fieldset id="test-coverage-options"/);
  vm.runInContext(`
    renderQuestions=()=>{};renderPanels=()=>{};renderTable=()=>{};renderJudge=()=>{};
    renderTestReview=()=>{};renderArtifactReadiness=()=>{};updateProjectControls=()=>{};
    select=async id=>{globalThis.createdId=id;};
    render();
  `,newForm.context);
  const options = newForm.nodes.get("test-coverage-choices").innerHTML;
  assert.equal((options.match(/type="radio"/g)||[]).length,2);
  assert.match(options,/value="focused" checked/);
  assert.match(options,/Small suite/);
  assert.match(options,/Comprehensive/);
  assert.match(options,/5–8 scenarios/);
  newForm.document.getElementById("prompt").value="Build a CLI";
  newForm.document.getElementById("project-mode").value="empty";
  vm.runInContext(app.slice(app.indexOf('$("task-form").onsubmit='),app.indexOf("const initialToken")),newForm.context);
  for(const choice of ["focused","comprehensive"]) {
    newForm.selection.value=choice;
    newForm.document.getElementById("prompt").value="Build a CLI";
    await newForm.nodes.get("task-form").onsubmit({preventDefault(){}});
    assert.equal(newForm.requests.at(-1).path,"/comparisons");
    assert.equal(newForm.requests.at(-1).data.test_coverage,choice);
  }
  assert.equal(newForm.context.createdId,"created");
  vm.runInContext(`
    api=(path,data)=>{requests.push({path,data});return new Promise(resolve=>{globalThis.finishSubmit=resolve;});};
  `,newForm.context);
  newForm.document.getElementById("prompt").value="Build a CLI";
  const pending = newForm.nodes.get("task-form").onsubmit({preventDefault(){}});
  vm.runInContext("updateTestCoverageAvailability()",newForm.context);
  assert.equal(newForm.nodes.get("submit-task").disabled,true,"Polling must not re-enable an in-flight submission");
  const inFlightRequests = newForm.requests.length;
  await newForm.nodes.get("task-form").onsubmit({preventDefault(){}});
  assert.equal(newForm.requests.length,inFlightRequests,"Repeated submission must not create duplicate comparisons");
  newForm.context.finishSubmit({id:"created"});
  await pending;
  const submitted = newForm.requests.length;
  for(const state of ["delete system.test_coverage", "system.test_coverage={supported:true,worker_ready:false}"]) {
    vm.runInContext(state,newForm.context);
    newForm.document.getElementById("prompt").value="Build a CLI";
    await newForm.nodes.get("task-form").onsubmit({preventDefault(){}});
    assert.equal(newForm.requests.length,submitted,"An old API/worker must not ignore the chosen scope");
    assert.equal(newForm.nodes.get("submit-task").disabled,true);
    assert.match(newForm.context.lastToast,/Restart the API and idle worker/);
  }

  const review = browser();
  vm.runInContext(`
    selected="comparison";
    current={id:"comparison",status:"waiting_test_review",spec_version:1,runs:[],
      artifact_readiness:{enabled:true,ready:true},test_review:{
        id:"review",status:"awaiting_review",revision:1,spec_version:1,answers:{},
        feedback:[],history:[],proposal:{summary:"Public CLI",questions:[],test_cases:[]}}};
    renderTestReview();
  `,review.context);
  assert.match(review.nodes.get("test-review").innerHTML, /Test coverage:<\/b> Comprehensive/);
  assert.match(review.nodes.get("test-review").innerHTML, /value="comprehensive" checked/);
  assert.match(review.nodes.get("test-review").innerHTML, /id="update-test-coverage"[^>]*disabled/);
  review.reviewRadios[0].checked=true;
  review.reviewRadios[1].checked=false;
  review.reviewRadios[0].onchange();
  assert.equal(review.nodes.get("update-test-coverage").disabled,false);
  assert.equal(review.nodes.get("approve-tests").disabled,true);
  assert.equal(review.nodes.get("approve-tests").textContent,"Regenerate proposal first");
  assert.equal(review.requests.length,0,"Changing the draft must not automatically regenerate or approve");
  await review.nodes.get("approve-tests").onclick();
  assert.match(review.context.lastToast,/Regenerate and review/);
  assert.equal(review.requests.length,0);
  vm.runInContext(`
    testReviewDrafts.get("review").feedback="Preserve my typed request";
    current.artifact_readiness.ready=false;
    renderTestReview();
  `,review.context);
  assert.match(review.nodes.get("test-review").innerHTML, /value="focused" checked/);
  assert.match(review.nodes.get("test-review").innerHTML, /Preserve my typed request/);
  assert.match(review.nodes.get("test-review").innerHTML, /id="approve-tests"[^>]*disabled>Regenerate proposal first/);
  await review.nodes.get("update-test-coverage").onclick();
  assert.equal(review.requests.length,1);
  assert.equal(review.requests[0].path,"/comparisons/comparison/test-review/changes");
  assert.equal(review.requests[0].data.review_id,"review");
  assert.equal(review.requests[0].data.test_coverage,"focused");
  assert.equal(review.requests[0].data.feedback,"Preserve my typed request");
  assert.equal(vm.runInContext("current.test_review.status",review.context),"awaiting_review");

  vm.runInContext(`
    current.test_coverage="focused";
    current.test_review={...current.test_review,id:"new-review",test_coverage:"focused",
      status:"queued",proposal:null,history:[current.test_review]};
    renderTestReview();
  `,review.context);
  assert.match(review.nodes.get("test-review").innerHTML, /Test coverage:<\/b> Small suite/);
  assert.match(review.nodes.get("test-review").innerHTML, /Preparing test cases/);
  assert.doesNotMatch(review.nodes.get("test-review").innerHTML, /id="update-test-coverage"/);
  console.log("Test coverage selection, submission, review and approval UI checks passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
