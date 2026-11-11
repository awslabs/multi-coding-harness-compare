const assert = require("node:assert/strict");
require("../arena/static/artifact-readiness.js");
const {blocked, render} = globalThis.artifactReadiness;
assert.equal(blocked(undefined), true);
assert.equal(blocked({enabled:false, ready:true}), false);
for (const status of ["pending","queued","running","failed","offline","restart_required","in_use"]) {
  assert.equal(blocked({enabled:true, ready:false, status}), true);
}
assert.equal(blocked({enabled:true, ready:true}), false);
assert.equal(render({enabled:false}), "");
const busy = render({enabled:true, status:"running", phase:"comparisons",
  comparisons_total:19, comparisons_ready:7, current_comparison:"comparison-1",
  transfer:{id:"comparison-1", status:"uploading", uploaded_bytes:33554432}});
assert.match(busy, /7 of 19/);
assert.match(busy, /32\.0 MiB uploaded/);
assert.match(busy, /Comparison retries paused/);
assert.match(busy, /max="19" value="7"/);
const leftovers = render({enabled:true, status:"running", phase:"leftovers",
  comparisons_total:19, comparisons_ready:19});
assert.match(leftovers, /Checking leftover files and logs/);
assert.doesNotMatch(leftovers, /value="19"/);
const failure = render({enabled:true, status:"failed", error:'<script>unsafe</script>',
  can_retry_recovery:true});
assert.match(failure, /Retry S3 recovery/);
assert.doesNotMatch(failure, /<script>/);
assert.match(failure, /&lt;script&gt;/);
const ready = render({enabled:true, ready:true, comparisons_total:19, comparisons_ready:19});
assert.match(ready, /S3 artifacts ready/);
assert.match(ready, /Ready to retry/);
assert.doesNotMatch(ready, /Retry S3 recovery/);
const restart = render({enabled:true,ready:false,status:"restart_required",
  message:"Restart the outdated worker."});
assert.match(restart, /Restart required for S3 readiness reporting/);
assert.doesNotMatch(restart, /<progress|Waiting for recovery|Checking S3 readiness/);
const localWorkingFiles = {enabled:true, ready:false, status:"in_use", worker_online:true,
  phase:"complete", selected_status:"local", comparisons_total:0, comparisons_ready:0,
  transfer:null, error:null, can_retry_recovery:false};
for (const status of ["queued","preparing","setup","clarifying","developing","planning",
                     "waiting_user","waiting_approval","reviewing_tests","evaluating",
                     "evaluation_queued","verification_queued","test_review_queued",
                     "waiting_test_review"]) {
  const active = render(localWorkingFiles, {status});
  assert.match(active, /S3 check complete/);
  assert.match(active, /Temporary local files are in use/);
  assert.doesNotMatch(active, /<progress|Preparing S3 artifacts|Retry S3 recovery|Ready to retry/);
}
// The presentation must not relax retry protection or hide real storage work.
assert.equal(blocked(localWorkingFiles), true);
for (const state of [
  {...localWorkingFiles, status:"pending", transfer:{status:"uploading"}},
  {...localWorkingFiles, status:"pending", comparisons_total:1},
  {...localWorkingFiles, status:"running", phase:"leftovers"},
  {...localWorkingFiles, status:"failed", error:"Upload failed"},
  {...localWorkingFiles, status:"offline", worker_online:false},
]) {
  assert.doesNotMatch(render(state, {status:"planning",test_review:{status:"approved"}}),
    /S3 check complete/);
}
// Active phases cannot override the API's pending state: real recovery may
// still be outstanding even when this comparison has working files.
for (const status of ["developing","planning","waiting_test_review","complete","incomplete",
                     "interrupted","archiving","restoring"]) {
  assert.match(render({...localWorkingFiles,status:"pending"}, {status}), /Preparing S3 artifacts/);
}
// Exercise the real review renderer across polling updates without contacting
// an API. Keep typed feedback/answers while buttons transition to ready.
const fs = require("node:fs"), vm = require("node:vm");
const elements = new Map();
const context = vm.createContext({artifactReadiness:globalThis.artifactReadiness, console,
  localStorage:{getItem(){return null;}},
  document:{
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {innerHTML:"", classList:{toggle(){},add(){},remove(){}}});
      return elements.get(id);
    },
    querySelectorAll() { return []; }
  }});
const app = fs.readFileSync(require.resolve("../arena/static/app.js"), "utf8");
vm.runInContext(app.slice(0, app.indexOf('$("new-comparison").onclick=newComparison;')), context);
vm.runInContext(`
system={artifact_readiness:{enabled:true,ready:false}};
current={id:"comparison",status:"waiting_test_review",spec_version:1,runs:[],artifact_storage:{status:"local"},
  artifact_readiness:{enabled:true,ready:false},test_review:{
    id:"review",revision:1,spec_version:1,status:"generation_failed",feedback:[],history:[],answers:{},
    proposal:{summary:"Approved CLI",test_cases:[],questions:[{id:"question",question:"Input?"}]}}};
renderTestReview();
`, context);
assert.match(elements.get("test-review").innerHTML, /id="approve-tests"[^>]*disabled/);
assert.match(elements.get("test-review").innerHTML, /id="request-test-changes"[^>]*disabled/);
vm.runInContext(`
testReviewDrafts.get("review").feedback="Keep my typed feedback";
testReviewDrafts.get("review").answers.question="Keep my answer";
current.artifact_readiness.ready=true;
renderTestReview();
`, context);
assert.doesNotMatch(elements.get("test-review").innerHTML, /id="approve-tests"[^>]*disabled/);
assert.match(elements.get("test-review").innerHTML, /Keep my typed feedback/);
assert.match(elements.get("test-review").innerHTML, /Keep my answer/);
vm.runInContext(`
current.artifact_readiness.ready=false;
current.test_review.status="awaiting_review";
renderTestReview();
`, context);
assert.doesNotMatch(elements.get("test-review").innerHTML, /id="approve-tests"[^>]*disabled/);
vm.runInContext(`current.test_review.status="proposal_failed";renderTestReview();`, context);
assert.match(elements.get("test-review").innerHTML, /id="retry-test-proposal"[^>]*disabled/);
vm.runInContext(`system={worker:{pid:123}};renderArtifactReadiness();`, context);
assert.match(elements.get("artifact-readiness").innerHTML, /running API has not loaded/);
assert.doesNotMatch(elements.get("artifact-readiness").innerHTML, /<progress|Waiting for recovery/);
context.workingFiles = localWorkingFiles;
vm.runInContext(`
system={artifact_readiness:{enabled:true,ready:true}};
current={id:"new-comparison",status:"developing",artifact_storage:{status:"local"},
  artifact_readiness:workingFiles};
renderArtifactReadiness();
`, context);
assert.match(elements.get("artifact-readiness").innerHTML, /S3 check complete/);
assert.doesNotMatch(elements.get("artifact-readiness").innerHTML, /<progress|Retry S3 recovery/);
assert.equal(vm.runInContext("artifactRetryBlocked()", context), true);
const planning = render({enabled:true,ready:false,status:"in_use"},
  {status:"waiting_test_review",test_review:{status:"drafting"}});
assert.match(planning, /S3 check complete/);
assert.match(planning, /Preparing test cases/);
assert.doesNotMatch(planning, /<progress|Retry S3 recovery|Ready to retry/);
vm.runInContext(`
system.evaluation_planner={model:"us.anthropic.claude-opus-5",effort:"max"};
current.status="waiting_test_review";
current.artifact_readiness={enabled:true,ready:false,status:"in_use"};
current.test_review={id:"draft",revision:1,spec_version:1,status:"drafting",
  created_at:Date.now()/1000-65,proposal:null,answers:{},feedback:[],history:[]};
renderTestReview();
`, context);
assert.match(elements.get("test-review").innerHTML, /shared planner is preparing the test cases/);
assert.match(elements.get("test-review").innerHTML, /Approval is not available yet/);
assert.match(elements.get("test-review").innerHTML, /Elapsed: 1m \d+s/);
assert.match(elements.get("test-review").innerHTML, /claude-opus-5 · Effort: max/);
assert.match(elements.get("test-review").innerHTML, /id="approve-tests"[^>]*disabled>Preparing test cases/);
assert.equal(vm.runInContext('statusLabel("waiting_test_review",current.test_review)', context),
  "Preparing test cases");
vm.runInContext(`
current.test_review.status="awaiting_review";
current.test_review.proposal={summary:"Ready for human review",questions:[],test_cases:[
  {id:"cli",title:"Actual CLI behavior",group:"acceptance",steps:["Run the CLI"],
    expected_result:"A valid result",test_data:"Controlled fixtures"}]};
renderTestReview();
`, context);
assert.match(elements.get("test-review").innerHTML, /Actual CLI behavior/);
assert.doesNotMatch(elements.get("test-review").innerHTML, /id="approve-tests"[^>]*disabled/);
assert.doesNotMatch(elements.get("test-review").innerHTML, /test-proposal-progress/);
assert.equal(vm.runInContext('statusLabel("waiting_test_review",current.test_review)', context),
  "Awaiting test approval");

// An approval saved in another tab/poll must replace stale local answer
// drafts. The confirmed answers are read-only during executable generation.
vm.runInContext(`
let testNow=1789760300000;
Date.now=()=>testNow;
current.status="planning";
current.test_review.proposal.questions=[{id:"zipcode",question:"Zipcode lookup provider?",
  suggested_answer:"A free no-key provider is the candidate's choice."}];
current.test_review.answers={zipcode:"Saved answer: use a <zipcode> lookup.\\nKeep fixtures independent.\\nEnter your answer explicitly."};
testReviewDrafts.get("draft").answers.zipcode="Stale unsubmitted draft";
testReviewDrafts.get("draft").feedback="Feedback still being edited";
current.test_review.status="approved";
current.test_review.generation_status="generating";
current.test_review.approved_at=Date.now()/1000-100;
current.test_review.updated_at=Date.now()/1000-65;
renderTestReview();
renderArtifactReadiness();
`, context);
const approvedHtml = elements.get("test-review").innerHTML;
const withoutApprovedAnswers = value =>
  value.replace(/<blockquote class="approved-test-answer"[^>]*>[\s\S]*?<\/blockquote>/g, "");
assert.match(approvedHtml, /Approved answers/);
assert.match(approvedHtml, /<blockquote class="approved-test-answer"[^>]*>Saved answer: use a &lt;zipcode&gt; lookup\.\nKeep fixtures independent\.\nEnter your answer explicitly\.<\/blockquote>/);
assert.doesNotMatch(withoutApprovedAnswers(approvedHtml),
  /Enter your answer explicitly|data-test-answer|Stale unsubmitted draft|Suggested answer/);
assert.match(approvedHtml, /Your approval and answers are saved/);
assert.match(approvedHtml, /shared planner is writing executable tests from your approved scenarios/);
assert.match(approvedHtml, /Your answers are recorded; you do not need to enter them again/);
assert.match(approvedHtml, /id="test-generation-progress"[^>]*>Elapsed: 1m 5s/);
assert.match(approvedHtml, /Feedback still being edited/);
assert.match(approvedHtml, /id="approve-tests"[^>]*disabled>Tests approved/);
assert.equal(vm.runInContext('statusLabel("planning",current.test_review)', context),
  "Developing approved tests");
const approvedBanner = elements.get("artifact-readiness").innerHTML;
assert.match(approvedBanner, /S3 check complete/);
assert.match(approvedBanner, /Developing approved tests/);
assert.doesNotMatch(approvedBanner, /<progress|Preparing S3 artifacts|Retry S3 recovery|Ready to retry/);
assert.equal(vm.runInContext("artifactRetryBlocked()", context), true);
const recordedApproval = vm.runInContext("JSON.stringify(current.test_review)", context);
vm.runInContext(`
testNow+=12000;
renderTestReview();
`, context);
assert.equal(elements.get("test-review").innerHTML, approvedHtml);
assert.match(elements.get("test-generation-progress").textContent, /Elapsed: 1m 17s/);
assert.equal(vm.runInContext("testReviewDrafts.get('draft').feedback", context),
  "Feedback still being edited");
assert.equal(vm.runInContext("JSON.stringify(current.test_review)", context), recordedApproval);
// A failed generation keeps the same saved answers and approval. Retrying
// becomes available only after the artifacts are ready.
vm.runInContext(`
current.status="waiting_test_review";
current.test_review.status="generation_failed";
current.test_review.generation_status="failed";
renderTestReview();
`, context);
assert.match(elements.get("test-review").innerHTML, /id="approve-tests"[^>]*disabled>Retry approved test development/);
assert.match(elements.get("test-review").innerHTML, /Approved answers/);
assert.doesNotMatch(withoutApprovedAnswers(elements.get("test-review").innerHTML),
  /Enter your answer explicitly|data-test-answer/);
vm.runInContext(`
current.artifact_readiness={enabled:true,ready:true,status:"ready"};
renderTestReview();
`, context);
assert.doesNotMatch(elements.get("test-review").innerHTML, /id="approve-tests"[^>]*disabled/);
assert.equal(vm.runInContext("testReviewDrafts.get('draft').answers.zipcode", context),
  "Saved answer: use a <zipcode> lookup.\nKeep fixtures independent.\nEnter your answer explicitly.");
console.log("Artifact readiness UI states passed.");
