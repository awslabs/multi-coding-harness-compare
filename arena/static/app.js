const $ = (id) => document.getElementById(id);
const names = {claude: "Claude Code", codex: "Codex", kiro: "Kiro"};
const icons = {claude: "✳", codex: "⌘", kiro: "◈"};
const efforts = ["low", "medium", "high", "xhigh", "max"];
const terminal = new Set(["blocked", "failed", "cancelled", "limit_exhausted", "interrupted", "verified_complete", "evaluated_failed", "evaluated_incomplete"]);
const completedEvaluations = new Set(["verified_complete", "evaluated_failed"]);
let current = null, stream = null, logs = [], system = null, reviewRun = null;
let modelCatalogue = null, browsing = null, folderRequest = 0;
let judgeCatalogue = null, judgeLoading = false, judgeLoadError = "", judgeOptionsSignature = "";
// Per-harness configuration plan (fetched once from /api/harness-configuration)
let harnessConfigPlan = null;
const judgeDrafts = new Map();
const testReviewDrafts = new Map();
const testCoverageOptions = [
  {value:"focused",label:"Small suite",description:"5–8 scenarios covering main workflows and important failures. Fewer tokens and less preparation time."},
  {value:"comprehensive",label:"Comprehensive",description:"Full coverage of stated requirements, boundaries, errors and relevant integrations. More tests, tokens and preparation time."}
];
let testReviewSignature = "";
let connected = false, connecting = false;
let taskSubmitting = false;
const harnessDrafts = new Map();
let harnessSelectionSignature = "";
let selected = localStorage.getItem("arena-comparison"), refreshTimer = null, questionSignature = "";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const text = (s) => String(s ?? "").replaceAll("_", " ");
const score = (v) => v == null ? "—" : Number(v).toFixed(1);
const duration = (s) => s == null ? "—" : s < 60 ? `${Math.floor(s)}s` : s < 3600 ? `${Math.floor(s/60)}m ${Math.floor(s%60)}s` : `${(s/3600).toFixed(1)}h`;
const tokens = (n) => n == null ? "—" : n.toLocaleString();
const pretty = (x) => esc(JSON.stringify(x, null, 2));
const effortLabel = (e) => e === "highest" ? "highest supported" : e;
function testCoverageLabel(value) {
  return testCoverageOptions.find(option=>option.value===value)?.label || "Comprehensive";
}
function testCoverageChoices(name, value) {
  return testCoverageOptions.map(option=>`<label class="coverage-choice" for="${esc(name)}-${esc(option.value)}">
    <input id="${esc(name)}-${esc(option.value)}" name="${esc(name)}" type="radio" value="${esc(option.value)}" ${option.value===value?"checked":""} required>
    <span><b>${esc(option.label)}</b><small>${esc(option.description)}</small></span></label>`).join("");
}
function testCoverageAvailable() {
  return system?.test_coverage?.supported === true && system.test_coverage.worker_ready;
}
function availableHarnesses() {
  return Object.keys(names).filter(h => !current || current.runs?.some(run => run.harness === h));
}
function harnessSelectionAvailable() {
  return system?.harness_selection?.supported === true && system.harness_selection.worker_ready === true;
}
function selectedHarnesses() {
  const key = current?.id || "new", available = availableHarnesses();
  if(!harnessDrafts.has(key)) harnessDrafts.set(key, available);
  return harnessDrafts.get(key).filter(h => available.includes(h));
}
function harnessNames(harnesses) {
  return harnesses.map(h => names[h]).filter(Boolean).join(", ");
}
function syncHarnessModelControls() {
  const chosen = selectedHarnesses();
  for(const h of Object.keys(names)) {
    const enabled = chosen.includes(h);
    $("model-field-" + h)?.classList.toggle("harness-off", !enabled);
    if($("model-" + h)) $("model-" + h).disabled = !enabled;
    if($("effort-" + h)) $("effort-" + h).disabled = !enabled || $("effort-" + h)._supportsEffort === false;
  }
  renderConfigChoices();
}
function renderHarnessSelection() {
  const chosen = selectedHarnesses(), available = availableHarnesses();
  const signature = JSON.stringify([current?.id, chosen, available, taskSubmitting]);
  const controls = $("harness-toggles");
  if(controls && harnessSelectionSignature !== signature) {
    harnessSelectionSignature = signature;
    controls.innerHTML = Object.keys(names).map(h => {
      const enabled = chosen.includes(h), unavailable = !available.includes(h);
      return `<button id="harness-toggle-${h}" type="button" role="switch" aria-checked="${enabled}" aria-label="${esc(names[h])}" aria-describedby="harness-selection-help${unavailable?` harness-unavailable-${h}`:""}" class="harness-toggle ${h}${enabled?" is-on":""}" ${unavailable || taskSubmitting?"disabled":""}><span class="harness-toggle-name"><span aria-hidden="true">${icons[h]}</span>${esc(names[h])}</span><span class="harness-toggle-state" aria-hidden="true">${enabled?"ON":"OFF"}<span class="switch-track"></span></span>${unavailable?`<small id="harness-unavailable-${h}">Not in this comparison</small>`:""}</button>`;
    }).join("");
    for(const h of Object.keys(names)) {
      $("harness-toggle-" + h).onclick = () => {
        if(taskSubmitting || !availableHarnesses().includes(h)) return;
        const selected = selectedHarnesses();
        if(selected.includes(h) && selected.length === 1) {
          $("harness-selection-status").textContent = "Keep at least one coding harness ON.";
          return;
        }
        harnessDrafts.set(current?.id || "new", selected.includes(h) ? selected.filter(value => value !== h) : [...selected, h]);
        renderHarnessSelection();
        updateTestCoverageAvailability();
        if(!current) { renderPanels(); renderTable(); }
        $("harness-toggle-" + h)?.focus?.();
      };
    }
  }
  $("harness-selection-help").textContent = current
    ? "Only harnesses switched ON receive this follow-up. Turning one OFF does not cancel work already in progress."
    : "Only harnesses switched ON receive this prompt and start coding.";
  $("harness-selection-status").textContent = chosen.length
    ? `${chosen.length} harness${chosen.length === 1?"":"es"} ON · Keep at least one ON.`
    : "Select at least one coding harness to continue.";
  $("follow-up-banner").textContent = `Follow-up to: ${harnessNames(chosen) || "choose a coding harness"}`;
  syncHarnessModelControls();
}
function updateTestCoverageAvailability() {
  const available = testCoverageAvailable();
  const selectionAvailable = harnessSelectionAvailable();
  $("harness-selection-availability").textContent = selectionAvailable ? "" :
    "Restart the API and idle worker to enable coding harness selection.";
  $("harness-selection-availability").classList.toggle("hidden", selectionAvailable);
  $("test-coverage-status").classList.toggle("hidden", available);
  $("test-coverage-status").textContent = available ? "" :
    "Restart the API and idle worker to load the test coverage options.";
  $("test-coverage-options").disabled=!available;
  if(!current) {
    $("submit-task").disabled=!available || !selectionAvailable || taskSubmitting || !selectedHarnesses().length;
  } else {
    const reason = followUpBlockedReason();
    $("submit-task").disabled = taskSubmitting || !selectionAvailable || !!reason || !selectedHarnesses().length;
    $("follow-up-block-reason").textContent = reason || "";
    $("follow-up-block-reason").classList.toggle("hidden", !reason);
  }
}
function creditsNote(run) {
  const value = run?.usage?.other_metrics?.credits;
  return typeof value === "number" ? `<small class="usage-note">${value.toFixed(3)} credits reported</small>` : "";
}
function usageLabel(run) {
  const usage = run?.usage;
  const label = run?.harness === "kiro" && usage?.completeness === "unavailable" ?
    "Tokens unavailable" : usage?.completeness || "Unavailable";
  return `<span title="${esc(usage?.unavailable_reason || "")}">${esc(label)}</span>${creditsNote(run)}`;
}
function artifactLocations(run) {
  const output = run.project_output, snapshot = run.artifacts?.snapshot;
  if(!output && !snapshot) return "";
  const storage = run.artifact_storage;
  if(storage?.status === "archived") {
    return `<div class="evidence-row"><h3>Saved artifacts in S3</h3><p class="artifact-path"><b>Archive</b><code>${esc(storage.archive_uri)}</code></p><p>Inspect files in Diff or download the patch. Resume and evaluation reruns load the saved artifacts automatically.</p>${Object.entries(storage.members || {}).map(([kind,path])=>`<p class="artifact-path"><b>${esc(text(kind))}</b><code>${esc(path)}</code></p>`).join("")}</div>`;
  }
  const currentOutput = ["published", "pending"].includes(output?.status);
  const note = output?.status === "published" ?
    `Writable project copy · specification v${output.spec_version}${output.partial ? " · partial implementation" : ""}.` :
    output?.status === "pending" ? "This folder receives the code when the harness's implementation is saved." :
    output?.error || output?.note || "";
  return `<div class="evidence-row"><h3>Saved artifact locations</h3>${currentOutput?`<p class="artifact-path"><b>Project code folder</b><code>${esc(output.path)}</code></p>`:""}${note?`<p>${esc(note)}</p>`:""}${snapshot?`<p class="artifact-path"><b>Evaluation snapshot</b><code>${esc(snapshot)}</code></p><p>The snapshot preserves the code used for evaluation, independently of edits to the project copy.</p><p class="artifact-path"><b>Patch</b><code>${esc(run.artifacts.patch || "")}</code></p>`:""}</div>`;
}
function projectOutputSummary(run) {
  const output = run?.project_output;
  if(!output) return "";
  const label = output.status === "published" ? `Code saved to ${run.harness}/` :
    output.status === "archived" ? "Code saved in S3" :
    output.status === "pending" ? `Output folder: ${run.harness}/` :
    output.status === "error" ? "Code folder needs attention" : "Code available in saved snapshot";
  return `<div class="panel-output"><button data-review="${esc(run.id)}" title="${esc(output.path || output.error || "")}">${esc(label)} ↗</button></div>`;
}
function verificationScope(run) {
  const evaluation = run.evaluation;
  const scopeNote = run.evaluation_scope_note ? `<p class="evaluation-scope-note">${esc(run.evaluation_scope_note)}</p>` : "";
  if(!evaluation) return scopeNote ? `<div class="evidence-row"><h3>Verification scope</h3>${scopeNote}</div>` : "";
  const exclusions = Object.entries(evaluation.tests?.excluded_groups || {});
  const limitations = evaluation.limitations || [];
  const finished = evaluation.overall != null;
  return `<div class="evidence-row"><h3>Verification scope</h3>${scopeNote}<p>${finished ?
    "Evaluation complete. Scores and check outcomes are shown in the comparison." :
    "Evaluation has missing evidence. See the verification gaps below."}</p>${limitations.length?`<ul>${limitations.map(note=>`<li>${esc(note)}</li>`).join("")}</ul>`:""}${exclusions.length?`<p>Excluded test groups (no points awarded):</p><ul>${exclusions.map(([group,reason])=>`<li><b>${esc(text(group))}</b>: ${esc(reason)}</li>`).join("")}</ul><p>Test weights are normalized over the included groups. Accuracy, relevance and overall weights remain unchanged.</p>`:""}${evaluation.verification_reason?`<p>Evaluation revision: ${esc(evaluation.verification_reason)}</p>`:""}</div>`;
}

async function api(path, data) {
  const response = await fetch("/api" + path, data === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)});
  if (!response.ok) {
    let error; try { error = (await response.json()).detail; } catch { error = response.statusText; }
    const failure = Error(typeof error === "string" ? error : JSON.stringify(error));
    failure.status = response.status;
    if(response.status === 401 && path !== "/auth") {
      requireAccess("Your browser session has ended. Connect again to continue.");
    }
    throw failure;
  }
  return response.json();
}
function requireAccess(message = "") {
  connected = false;
  if(stream) { stream.close(); stream = null; }
  clearTimeout(refreshTimer); refreshTimer = null;
  folderRequest++; browsing = null;
  document.querySelectorAll("dialog[open]").forEach(dialog => dialog.close());
  document.body.classList.add("needs-access");
  $("workspace-content").classList.add("hidden");
  $("connection-pending").classList.add("hidden");
  $("access-panel").classList.remove("hidden");
  $("new-comparison").disabled = true; $("settings-button").disabled = true;
  $("worker-dot").classList.remove("online");
  $("worker-status").textContent = "Browser not connected";
  $("page-title").textContent = "Connect";
  $("access-error").textContent = message;
  $("access-error").classList.toggle("hidden", !message);
}
function accessToken(value) {
  const input = value.trim();
  if(!input) throw Error("Paste the local access link printed by multi-coding-harness-compare start.");
  if(input.startsWith("http://") || input.startsWith("https://")) {
    const token = new URLSearchParams(new URL(input).hash.slice(1)).get("token");
    if(!token) throw Error("This link is missing its token. Copy the complete access link printed by multi-coding-harness-compare start.");
    return token;
  }
  return input;
}
async function connectWorkspace(value) {
  if(connecting) return;
  connecting = true;
  $("connect-browser").disabled = true; $("check-connection").disabled = true;
  try {
    if(value !== undefined) await api("/auth", {answer:accessToken(value)});
    const status = await api("/auth");
    if(!status.authenticated) {
      requireAccess(value === undefined ? "" : "The browser could not save the local session. Allow cookies for this local site and reconnect.");
      return;
    }
    await loadSystem();
    connected = true;
    $("access-link").value = "";
    $("access-error").classList.add("hidden");
    document.body.classList.remove("needs-access");
    $("access-panel").classList.add("hidden");
    $("connection-pending").classList.add("hidden");
    $("workspace-content").classList.remove("hidden");
    $("new-comparison").disabled = false; $("settings-button").disabled = false;
    renderModels(); render(); loadModelsAtStartup(); loadJudgeModels();
    await loadHistory();
    if(selected) await select(selected);
  } catch(e) {
    requireAccess(e.status === 401 ? "That access link is not valid for this multi-coding-harness-compare instance. Use the link printed by multi-coding-harness-compare start." :
      e.message === "Failed to fetch" ? "Cannot reach multi-coding-harness-compare. Start the app, then check the connection again." : e.message);
  } finally {
    connecting = false;
    $("connect-browser").disabled = false; $("check-connection").disabled = false;
  }
}
function toast(message) {
  $("toast").textContent = message; $("toast").classList.remove("hidden");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => $("toast").classList.add("hidden"), 6000);
}
function artifactRetryBlocked() {
  if(current?.artifact_storage && !current?.artifact_readiness) return true;
  return artifactReadiness.blocked(current?.artifact_readiness || system?.artifact_readiness);
}
function artifactRetryAttributes() {
  const busy = current && !["complete","incomplete","interrupted"].includes(current.status);
  return `data-artifact-retry aria-describedby="artifact-readiness" ${artifactRetryBlocked() || busy ? "disabled" : ""}`;
}
function followUpBlockedReason() {
  if(!current) return null;
  if(["archiving","archive_queued","restoring","storage_failed"].includes(current.status))
    return "Wait for artifact transfer to finish before sending a follow-up.";
  if(["evaluation_queued","verification_queued"].includes(current.status))
    return "Wait for re-scoring to finish, then send the follow-up.";
  return null;
}
function renderArtifactReadiness() {
  const panel = $("artifact-readiness");
  const state = system && !system.artifact_readiness ? {
    enabled:true, ready:false, status:"restart_required",
    message:"The running API has not loaded S3 readiness reporting. Restart the API to see recovery progress. Let the current S3 transfer finish before restarting the worker."
  } : current?.artifact_readiness || system?.artifact_readiness;
  panel.innerHTML = artifactReadiness.render(state, current);
  panel.classList.toggle("hidden", state?.enabled === false);
  if($("retry-s3-recovery")) $("retry-s3-recovery").onclick = async () => {
    $("retry-s3-recovery").disabled = true;
    try {
      await api("/artifacts/recover", {});
      toast("S3 recovery queued. Retry the comparison when artifacts are ready.");
    } catch(e) { toast(e.message); }
    await loadSystem();
    if(selected) await refresh();
  };
  const busy = current && !["complete","incomplete","interrupted"].includes(current.status);
  document.querySelectorAll("#review-dialog [data-artifact-retry]").forEach(button => {
    button.disabled = artifactRetryBlocked() || !!busy;
  });
}
function statusLabel(status, review = null) {
  if (status === "archiving") return "Saving artifacts to S3";
  if (status === "restoring") return "Loading saved artifacts";
  if (status === "storage_failed") return "Artifact storage failed";
  if (status === "archive_queued") return "Artifact storage retry queued";
  if (completedEvaluations.has(status)) return "Evaluation complete";
  if (status === "evaluated_incomplete") return "Evaluation incomplete";
  if (status === "planning") {
    if(["queued","drafting"].includes(review?.status)) return "Preparing test cases";
    if(review?.status === "approved" && !review.plan_sha256) return "Developing approved tests";
    return "Preparing evaluation";
  }
  if (status === "waiting_test_review") {
    if(["queued","drafting"].includes(review?.status)) return "Preparing test cases";
    if(review?.status === "proposal_failed") return "Test proposal failed";
    if(review?.status === "generation_failed") return "Test development failed";
    if(review?.status === "approved") return review.plan_sha256 ? "Tests ready for evaluation" : "Developing approved tests";
    return "Awaiting test approval";
  }
  if (status === "test_review_queued") return "Preparing test review";
  if (status === "reviewing_tests") return ["queued","drafting"].includes(review?.status) ?
    "Preparing test cases" : "Developing approved tests";
  return text(status || "Awaiting task");
}
function badge(status) {
  let cls = completedEvaluations.has(status) ? "good" : ["blocked","failed","waiting_user","waiting_approval","waiting_test_review","evaluated_incomplete","interrupted","limit_exhausted"].includes(status) ? "warn" : ["setup","developing","clarifying","planning","evaluating"].includes(status) ? "running" : "";
  return `<span class="status-badge ${cls}">${esc(statusLabel(status, current?.test_review))}</span>`;
}
async function loadHistory() {
  const list = await api("/comparisons");
  $("history-count").textContent = list.length;
  $("compact-history").innerHTML='<option value="">Choose comparison</option>'+list.map(c=>`<option value="${esc(c.id)}">${esc(c.prompt.slice(0,55))} · ${esc(c.id.slice(0,8))} · ${esc(text(c.status))}</option>`).join("");
  $("compact-history").value=selected||"";
  $("history").innerHTML = list.map(c => `<button class="history-item ${c.id===selected?"selected":""}" data-select="${esc(c.id)}"><span class="title">${esc(c.prompt)}</span><small>${new Date(c.created_at*1000).toLocaleDateString()} · ${esc(text(c.status))}</small></button>`).join("");
  document.querySelectorAll("[data-select]").forEach(button => button.onclick = () => select(button.dataset.select));
}
async function loadHarnessConfig() {
  // Fetch per-harness configuration plan to populate config-choice selectors
  try {
    harnessConfigPlan = await api("/harness-configuration");
    renderConfigChoices();
  } catch(e) {
    harnessConfigPlan = null;
  }
}
function renderConfigChoices() {
  // Inject a <select id="config-choice-{h}"> below each model-field for new comparisons
  if(current) return; // only on the new-comparison form
  for(const h of Object.keys(names)) {
    const existing = $("config-choice-" + h);
    if(!existing) continue;
    const plan = harnessConfigPlan?.[h];
    const overridesAvailable = plan && plan.mode !== "empty private HOME" && plan.sources?.length > 1;
    existing.disabled = !overridesAvailable || !selectedHarnesses().includes(h);
    // Preselect the configured default from the harness-configuration plan.
    const effectiveDefault = plan?.effective_default_choice;
    if(effectiveDefault && ["default","overrides","none"].includes(effectiveDefault)) {
      // Only preselect if the user has not already changed the value away from the
      // previous effective default (keep any explicit user choice).
      if(!existing._userChosen) existing.value = effectiveDefault;
    }
    const desc = $("config-choice-desc-" + h);
    if(desc && plan) {
      const hasSrc = plan.sources?.length > 0;
      const readsWs = plan.reads_workstation_folder;
      desc.textContent = hasSrc
        ? `default: ${plan.effective_default_choice || "default"}; workstation folder ${readsWs ? "read" : "not read"}`
        : "no operator configuration found";
    }
  }
}
async function loadSystem() {
  system = await api("/system");
  updateTestCoverageAvailability();
  loadHarnessConfig().catch(()=>{});
  $("worker-dot").classList.toggle("online", system.worker_online);
  $("worker-status").textContent = system.worker_online ? "Persistent worker online" : "Worker offline";
  $("settings-content").textContent = JSON.stringify(system, null, 2);
  const notes = [];
  if (!system.worker_online) notes.push("Start the persistent worker with `multi-coding-harness-compare worker` to process submitted tasks.");
  if (!system.judge_configured) notes.push("No Strands judge configured. Relevance and automatic acceptance generation will be unavailable; supply a validation plan for executable task evidence.");
  $("system-warning").textContent = notes.join(" ");
  $("system-warning").classList.toggle("hidden", !notes.length);
  renderArtifactReadiness();
}
function selectedJudgeModel() {
  return $("judge-model").value || null;
}
function judgeRequest() {
  return {judge_model:selectedJudgeModel()};
}
function renderJudge() {
  const slot = $(current ? "comparison-judge-slot" : "new-judge-slot");
  if($("judge-controls").parentElement !== slot) slot.appendChild($("judge-controls"));
  const key = current?.id || "new";
  const savedJudges = [...new Set((current?.runs || []).map(r=>r.evaluation?.judge?.model).filter(Boolean))];
  const chosen = judgeDrafts.has(key) ? judgeDrafts.get(key) : "";
  const models = judgeCatalogue?.models || [];
  const signature = JSON.stringify([key, judgeCatalogue?.checked_at, current?.judge?.model, system?.judge_model, chosen]);
  if(signature !== judgeOptionsSignature) {
    const groups = new Map();
    for(const model of models) {
      const label = `${model.provider} · ${model.kind==="inference_profile" ? "Inference profiles" : "Foundation models"}`;
      if(!groups.has(label)) groups.set(label, []);
      groups.get(label).push(model);
    }
    const missing = chosen && !models.some(m=>m.id===chosen);
    const defaultLabel = current?.judge?.model
      ? `This comparison's judge · ${current.judge.model}`
      : system?.judge_model ? `Configured default · ${system.judge_model}` : "No judge configured";
    $("judge-model").innerHTML = `<option value="">${esc(defaultLabel)}</option>` +
      (missing ? `<option value="${esc(chosen)}">${esc(chosen)} · configured / saved model</option>` : "") +
      [...groups].map(([group, entries])=>`<optgroup label="${esc(group)}">${entries.map(m=>`<option value="${esc(m.id)}"${m.selectable ? "" : " disabled"} title="${esc(m.reason || m.name)}">${esc(m.id)} · ${esc(m.name)}${m.selectable ? "" : " · " + esc(m.reason)}</option>`).join("")}</optgroup>`).join("");
    $("judge-model").value = chosen;
    judgeOptionsSignature = signature;
  }
  renderPlanningPicker();
  const busy = current && !["complete","incomplete","interrupted"].includes(current.status);
  $("judge-model").disabled = !!busy || (judgeLoading && !models.length && !chosen);
  $("refresh-judge-models").disabled = judgeLoading;
  $("judge-reevaluate").classList.toggle("hidden", !current);
  const entry = models.find(m=>m.id===chosen);
  const hasJudge = !!(chosen || current?.judge?.model);
  $("judge-reevaluate").disabled = artifactRetryBlocked() || !!busy || !hasJudge || entry?.selectable===false ||
    !(current?.runs || []).some(r=>r.artifacts?.case);
  const location = `${judgeCatalogue?.profile || system?.judge_profile || "Default AWS credentials"} · ${judgeCatalogue?.region || system?.judge_region || ""}`;
  const status = judgeLoading ? "Loading US inference profiles…" : judgeLoadError ||
    `${models.length} US inference profiles · ${models.filter(m=>m.selectable).length} selectable for text judging`;
  $("judge-model-status").textContent = [location, status, ...(judgeCatalogue?.warnings || [])].filter(Boolean).join(" · ");
  $("judge-scores-source").textContent = current ?
    (savedJudges.length ? `Displayed scores were judged by ${savedJudges.join(", ")}. ` : "") +
    (busy ? `Evaluation uses ${chosen || current.judge?.model || system?.judge_model || "the configured default"}.` :
      "Re-score with the selected judge to update the results using the saved evidence.") :
    "The same judge evaluates the selected implementations. Select a model before starting the comparison.";
}
function renderPlanningPicker() {
  const el = $("planning-field");
  if(!el) return;
  el.classList.toggle("hidden", !!current);
  if(current) return;
  const models = judgeCatalogue?.models || [];
  const requireClaude = system?.planning_requires_claude;
  const selectable = models.filter(m => m.selectable && (!requireClaude || m.id.includes("anthropic.claude")));
  const plannerDefault = system?.evaluation_planner?.model;
  const plannerEffort = system?.evaluation_planner?.effort;
  const effortSuffix = plannerDefault && plannerEffort ? ` · effort: ${effortLabel(plannerEffort)}` : "";
  const prev = $("planning-model").value;
  $("planning-model").innerHTML = `<option value="">${esc(plannerDefault ? `Configured default · ${plannerDefault}${effortSuffix}` : "No planner configured")}</option>` +
    selectable.map(m => `<option value="${esc(m.id)}">${esc(m.id)} · ${esc(m.name)}</option>`).join("");
  $("planning-model").value = selectable.some(m => m.id === prev) ? prev : "";
}
async function loadJudgeModels(refresh=false) {
  if(judgeLoading) return;
  judgeLoading = true; judgeLoadError = ""; renderJudge();
  try {
    judgeCatalogue = await api(refresh ? "/judge/models/refresh" : "/judge/models", refresh ? {} : undefined);
  } catch(e) {
    judgeLoadError = `Judge model catalog could not be loaded: ${e.message}`;
  } finally {
    judgeLoading = false; judgeOptionsSignature = ""; renderJudge();
  }
}
function renderModels() {
  const previous = Object.fromEntries(Object.keys(names).map(h => [h, $("model-" + h)?.value || ""]));
  const previousEffort = Object.fromEntries(Object.keys(names).map(h => [h, $("effort-" + h)?.value || ""]));
  const previousConfiguration = Object.fromEntries(Object.keys(names).map(h => [h, {
    value: $("config-choice-" + h)?.value, userChosen: $("config-choice-" + h)?._userChosen
  }]));
  $("model-selectors").innerHTML = Object.keys(names).map(h => {
    const entry = modelCatalogue?.harnesses[h], configured = entry?.configured_default || system?.harnesses[h]?.model;
    const choices = entry?.models || [];
    const missing = previous[h] && !choices.some(m => m.id === previous[h]);
    const configChoiceHtml = `<div class="config-choice-row"><label for="config-choice-${esc(h)}">Configuration</label><select id="config-choice-${esc(h)}" aria-describedby="config-choice-desc-${esc(h)}"><option value="default">Default (operator config)</option><option value="overrides">Overrides only</option><option value="none">None (clean HOME)</option></select><p id="config-choice-desc-${esc(h)}" class="subtle config-choice-desc"></p></div>`;
    return `<div id="model-field-${esc(h)}" class="model-field ${esc(h)}"><label for="model-${esc(h)}"><span>${icons[h]}</span> ${esc(names[h])}</label><select id="model-${esc(h)}" aria-describedby="model-help-${esc(h)}"><option value="">${esc(configured ? `Configured default · ${configured}` : "Native default")}</option>${choices.map(m => `<option value="${esc(m.id)}">${esc(m.id)}${m.name !== m.id ? ` · ${esc(m.name)}` : ""}</option>`).join("")}${missing ? `<option value="${esc(previous[h])}" disabled>${esc(previous[h])} · no longer listed</option>` : ""}</select><select id="effort-${esc(h)}" aria-label="${esc(names[h])} effort"></select>${configChoiceHtml}<p id="model-help-${esc(h)}"></p></div>`;
  }).join("");
  for (const h of Object.keys(names)) {
    $("model-" + h).value = previous[h];
    renderEffort(h, previousEffort[h]);
    $("effort-" + h).onchange = () => { if(!current) renderPanels(); };
    $("model-" + h).onchange = () => { renderEffort(h, $("effort-" + h).value); modelHelp(h); if(!current) renderPanels(); };
    // Track explicit user choice so renderConfigChoices does not override it.
    const cc = $("config-choice-" + h);
    if(cc) {
      if(previousConfiguration[h].userChosen) {
        cc.value = previousConfiguration[h].value;
        cc._userChosen = true;
      }
      cc.onchange = () => { cc._userChosen = true; if(!current) renderPanels(); };
    }
    modelHelp(h);
  }
  // Populate config-choice descriptor text from the last-fetched plan
  syncHarnessModelControls();
}
// The effort list follows the picked model (or the configured default): the levels its
// native catalogue advertises, or every level when the catalogue does not say.
function renderEffort(h, keep) {
  const entry = modelCatalogue?.harnesses[h], select = $("effort-" + h);
  const id = $("model-" + h).value || entry?.configured_default || system?.harnesses[h]?.model;
  const model = entry?.models.find(m => m.id === id);
  const levels = Array.isArray(model?.efforts) ? efforts.filter(e => model.efforts.includes(e)) : efforts;
  const configured = system?.harnesses[h]?.effort;
  const fallback = configured && levels.includes(configured) ? `Configured effort · ${configured}`
    : model?.default_effort ? `Model's default effort · ${model.default_effort}` : "Model's default effort";
  select.innerHTML = levels.length ? `<option value="">${esc(fallback)}</option>${levels.map(e => `<option value="${esc(e)}">Effort · ${esc(e)}</option>`).join("")}`
    : `<option value="">Effort not supported by this model</option>`;
  select._supportsEffort = !!levels.length;
  select.disabled = !levels.length || !selectedHarnesses().includes(h);
  select.value = levels.includes(keep) ? keep : "";
}
function modelHelp(h) {
  const entry = modelCatalogue?.harnesses[h];
  const chosen = entry?.models.find(m => m.id === $("model-" + h).value);
  $("model-help-" + h).textContent = [
    chosen?.description || (entry ? `${entry.models.length} models · ${entry.source || "Catalogue unavailable"}` : "Reading native model catalogue…"),
    entry?.setup === "existing macOS CLI" ? "Using your existing macOS CLI setup." : "",
    entry?.stale ? "Last available list. " + entry.error : ""
  ].filter(Boolean).join(" ");
}
// The cached catalogue shows at once; refresh it unless that read already ran a discovery
// (a cold cache probes on the first read), so startup never probes twice.
async function loadModelsAtStartup(opened = Date.now() / 1000) {
  await loadModels();
  if (!((modelCatalogue?.checked_at || 0) >= opened)) await loadModels(true);
}
async function loadModels(refresh = false) {
  $("refresh-models").disabled = true;
  $("model-status").textContent = "Reading installed harness catalogues…";
  try {
    modelCatalogue = await api(refresh ? "/models/refresh" : "/models", refresh ? {} : undefined);
    renderModels();
    $("model-status").textContent = "Selections are saved with this task, including retries and revisions.";
  } catch(e) {
    if(e.status !== 401) {
      $("model-status").textContent = `Model lists could not be loaded: ${e.message}. Refresh to try again.`;
      toast(e.message);
    }
  } finally { $("refresh-models").disabled = false; }
}
function updateProjectControls() {
  const existing = $("project-mode").value === "repository";
  $("repository-controls").classList.toggle("hidden", !existing);
  $("repository").required = existing && !current;
}
async function browseFolder(path) {
  const request = ++folderRequest;
  browsing = null;
  $("select-folder").disabled = true; $("folder-up").disabled = true;
  $("folder-error").classList.add("hidden");
  $("folder-path").textContent = path;
  $("folder-list").textContent = "Loading folders…";
  try {
    const data = await api(`/projects/directories?path=${encodeURIComponent(path)}`);
    if(request !== folderRequest) return;
    browsing = data;
    $("folder-path").textContent = data.path;
    const root = [...data.roots].sort((a,b)=>b.length-a.length).find(r => data.path === r || data.path.startsWith(r + "/"));
    $("folder-root").value = root || "";
    $("folder-up").disabled = !data.parent;
    $("select-folder").disabled = false;
    $("folder-list").innerHTML = data.directories.length ? data.directories.map((d,i) => `<button type="button" class="folder-item" data-folder-index="${i}"><span>▱</span><span>${esc(d.name)}</span><span>›</span></button>`).join("") : '<p class="folder-empty">No subfolders. You can select this folder.</p>';
    document.querySelectorAll("[data-folder-index]").forEach(button => button.onclick = () => browseFolder(data.directories[Number(button.dataset.folderIndex)].path));
  } catch(e) {
    if(request !== folderRequest) return;
    $("folder-list").textContent = "";
    $("folder-error").textContent = e.message; $("folder-error").classList.remove("hidden");
  }
}
async function openFolders() {
  $("folder-dialog").showModal();
  $("select-folder").disabled = true;
  $("folder-list").textContent = "Loading project locations…";
  $("folder-error").classList.add("hidden");
  try {
    const data = await api("/projects/directories");
    $("folder-root").innerHTML = data.roots.map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join("");
    if(!data.roots.length) throw Error("No accessible project locations. Set repository_roots in config.toml and restart multi-coding-harness-compare.");
    const typed = $("repository").value.trim();
    await browseFolder(typed || data.roots[0]);
  } catch(e) {
    if(e.status === 401) return;
    $("folder-list").textContent = "";
    $("folder-error").textContent = e.message; $("folder-error").classList.remove("hidden");
  }
}
function activity(run) {
  const grouped = [];
  for (const event of logs.filter(e=>e.run_id===run?.id)) {
    if(event.type==="native") {
      const update=event.data.params?.update;
      const method=event.data.method || "";
      if(!["tool_call","tool_call_update"].includes(update?.sessionUpdate) &&
         !/item\/(started|completed)|commandExecution|fileChange/.test(method)) continue;
    } else if(!["text","diagnostic","error","status"].includes(event.type)) continue;
    const previous=grouped[grouped.length-1];
    if(event.type==="text" && previous?.type==="text") previous.data.text += event.data.text || "";
    else grouped.push({...event,data:{...event.data}});
  }
  const rows = grouped.slice(-18);
  if (!rows.length && terminal.has(run?.status)) return `<div class="empty-activity"><span>${esc(statusLabel(run.status, current?.test_review))}</span><small class="${run.evaluation_scope_note?"evaluation-scope-note":""}">${esc(run.evaluation_scope_note || run.error || "Open Inspect run for saved details.")}</small>${run.evaluation_scope_note && run.error?`<small>${esc(run.error)}</small>`:""}</div>`;
  if (!rows.length && run?.status === "evaluating") return `<div class="empty-activity"><span class="empty-symbol">◌</span><span>Independent evaluation in progress</span><small>Checking the saved implementation and scoring its evidence.</small></div>`;
  if (!rows.length) return `<div class="empty-activity"><span class="empty-symbol">${run?"◌":"⌁"}</span><span>${run ? "Waiting for native harness events" : "Ready when you are"}</span><small>${run ? esc(run.error || "Durable worker dispatch") : "Progress will appear here independently"}</small></div>`;
  return rows.map(e => {
    const time = new Date(e.at*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});
    const content = e.type === "native" ? `<details><summary>${esc(e.data.method || e.data.message_type || "Tool activity")}</summary><pre>${pretty(e.data)}</pre></details>` :
      esc(e.data.text || e.data.message || (e.data.status ? statusLabel(e.data.status, current?.test_review) : ""));
    return `<div class="log-entry"><time>${time}</time>${content}</div>`;
  }).join("") + (run?.evaluation_scope_note ? `<p class="evaluation-scope-note">${esc(run.evaluation_scope_note)}</p>` : "");
}
function followUpPanelStatus(run) {
  const fus = (current?.follow_ups || []).filter(f => !f.harnesses || f.harnesses.includes(run?.harness));
  if(!fus.length) return "";
  const delivered = new Set(run?.follow_ups_delivered || []);
  const pending = fus.filter(f=>!delivered.has(f.id));
  // Delivered is recorded when the harness finishes the turn that carried the follow-up.
  const carrying = new Set(run?.attempts?.at(-1)?.follow_ups || []);
  if(pending.length && ["setup","developing","waiting_user","waiting_approval"].includes(run?.status)
     && pending.every(f=>carrying.has(f.id)))
    return `<span class="follow-up-status pending">Follow-up in progress</span>`;
  if(pending.length) return `<span class="follow-up-status pending">Follow-up pending</span>`;
  return `<span class="follow-up-status delivered">Follow-ups delivered (${fus.length})</span>`;
}
function renderPanels() {
  $("panels").innerHTML = Object.keys(names).map(harness => {
    const run = current?.runs.find(r => r.harness===harness);
    if(!run && (current || !selectedHarnesses().includes(harness))) {
      return `<article class="panel ${esc(harness)} panel-off"><div class="panel-header"><div class="harness-icon">${icons[harness]}</div><h3>${esc(names[harness])}</h3><span class="status-badge">OFF</span></div><div class="panel-activity"><div class="empty-activity"><span>${current?"Not in this comparison":"Not selected"}</span><small>${current?"No prompt was sent.":"No prompt will be sent."}</small></div></div></article>`;
    }
    const model = run ? (run.model || run.requested_model || "Native default") : ($("model-" + harness)?.value || system?.harnesses[harness]?.model || "Native default");
    const effort = run ? run.requested_effort : ($("effort-" + harness)?.value || system?.harnesses[harness]?.effort);
    return `<article class="panel ${esc(harness)}"><div class="panel-header"><div class="harness-icon">${icons[harness]}</div><div><h3>${esc(names[harness])}</h3><small>${esc(model)}${effort ? ` · effort ${esc(effort)}` : ""}</small>${run?.model_mismatch?`<small class="model-mismatch">Ran ${esc(run.model_mismatch.reported)} instead of the requested ${esc(run.model_mismatch.requested)}</small>`:""}</div>${badge(run?.status)}</div>${current?.test_review?.failure?`<div class="panel-fail-strip">${esc(current.test_review.failure.stage==="test proposal"?"Test proposal":"Test development")} failed: ${esc(current.test_review.failure.cause)} <a href="#test-review-failure" class="test-fail-link">Details ↓</a></div>`:""}<div class="panel-activity" data-activity="${esc(harness)}">${activity(run)}</div><div class="panel-stats"><div><label>ACTIVE TIME</label><strong>${duration(run?.duration?.active)}</strong></div><div><label>TOKENS ${run?.usage?.completeness==="partial"?"· PARTIAL":""}</label><strong>${tokens(run?.usage?.total)}</strong>${creditsNote(run)}</div><div><label>OVERALL</label><strong>${score(run?.evaluation?.overall)}</strong></div></div>${projectOutputSummary(run)}<div class="panel-footer">${run?`<button data-review="${esc(run.id)}">Inspect run ↗</button>${run.artifacts?.patch?`<a href="/api/runs/${esc(run.id)}/patch">↓ Patch</a>`:""}${terminal.has(run.status)?`<button data-resume="${esc(run.id)}" class="cancel-run" ${artifactRetryAttributes()}>Resume</button>`:`<button data-cancel="${esc(run.id)}" class="cancel-run">Cancel</button>`}`:`<span class="subtle">Isolated checkout · independent session</span>`}${followUpPanelStatus(run)}</div></article>`;
  }).join("");
  document.querySelectorAll("[data-activity]").forEach(el => el.scrollTop=el.scrollHeight);
  document.querySelectorAll("[data-review]").forEach(el => el.onclick=() => openReview(el.dataset.review));
  document.querySelectorAll("[data-cancel]").forEach(el => el.onclick=async () => {
    try { await api(`/runs/${el.dataset.cancel}/cancel`, {}); await refresh(); } catch(e) { toast(e.message); }
  });
  document.querySelectorAll("[data-resume]").forEach(el => el.onclick=async () => {
    try { await api(`/runs/${el.dataset.resume}/resume`, {}); toast("Resume queued for the existing comparison."); await refresh(); } catch(e) { toast(e.message); }
  });
  document.querySelectorAll("a.test-fail-link").forEach(a => a.onclick = () => {
    const el = $("test-review-failure");
    if(el) { el.classList.add("failure-highlight"); setTimeout(() => el.classList.remove("failure-highlight"), 1600); }
  });
}
function renderQuestions() {
  const qs = (current?.questions || []).filter(q=>q.answer===null && !q.withdrawn);
  const signature = JSON.stringify(qs);
  if(signature===questionSignature) return;
  questionSignature=signature;
  const drafts = Object.fromEntries([...document.querySelectorAll("[data-answer]")].map(el=>[el.dataset.answer,el.value]));
  $("questions").innerHTML = qs.map(q => `<form class="question-card" data-question="${esc(q.id)}"><div class="origin">${[...new Set(q.mappings.map(m=>esc(names[m.harness])))].join(" + ")} · ${q.kind==="approval"?"APPROVAL REQUIRED":"SHARED CLARIFICATION"}</div><h3>${esc(q.question)}</h3>${q.choices.length?`<div class="choices">${q.choices.map((c,i)=>`<label><input type="radio" name="choice-${esc(q.id)}" value="${esc(c)}"> ${esc(c)}</label>`).join("")}</div>`:""}${q.schema?`<details><summary>Required JSON schema</summary><pre>${pretty(q.schema)}</pre></details>`:""}<div class="answer-row"><input data-answer="${esc(q.id)}" placeholder="${q.schema?"Enter a JSON object matching the schema":"Choose an option or enter your answer…"}" value="${esc(drafts[q.id]||"")}" aria-label="Answer"><button type="submit">Send answer ↗</button></div></form>`).join("");
  document.querySelectorAll("[data-question]").forEach(form => form.onsubmit=async event=>{
    event.preventDefault();
    const answer=form.querySelector("[data-answer]").value.trim() || form.querySelector("input[type=radio]:checked")?.value;
    if(!answer) return toast("Choose an option or enter an answer.");
    try { await api(`/questions/${form.dataset.question}/answer`, {answer}); await refresh(); } catch(e) { toast(e.message); }
  });
}
function testReviewProgress(review, planner) {
  const elapsed = Math.max(0, Date.now()/1000 - (review.updated_at || review.created_at || Date.now()/1000));
  return `Elapsed: ${duration(elapsed)}${planner ? ` · Planning model: ${planner.model} · Effort: ${effortLabel(planner.effort)}` : ""}`;
}
function testReviewFailureBlock(failure) {
  const a = failure.attempt;
  const fmt = t => t ? esc(new Date(t * 1000).toLocaleString()) : "—";
  return `<div id="test-review-failure" class="test-review-failure">
    <p><b>Stage: ${esc(failure.stage)}</b></p>
    <p class="trf-cause">${esc(failure.cause)}</p>
    <pre class="trf-error">${esc(failure.error)}</pre>
    ${a ? `<div class="trf-attempt"><p><b>Last attempt</b> · ${esc(a.model)} · effort ${esc(a.effort ?? "—")} · output limit ${tokens(a.max_tokens)}</p><p>${fmt(a.started_at)} → ${fmt(a.ended_at)} · ${duration(a.duration_seconds)}</p></div>` : ""}
    ${failure.attempt_url ? `<button id="view-attempt-record" class="secondary" data-attempt-url="${esc(failure.attempt_url)}">View attempt record ↗</button>` : ""}
  </div>`;
}
function renderTestReview() {
  const review = current?.test_review;
  const preparing = ["queued","drafting"].includes(review?.status);
  const developing = review?.status === "approved" && !review.plan_sha256;
  const progressId = preparing ? "test-proposal-progress" : developing ? "test-generation-progress" : null;
  const finished = current && ["complete","incomplete","interrupted"].includes(current.status);
  const signature = JSON.stringify([current?.id, current?.cancel_requested, finished,
    review?.id, review?.status, review?.proposal, review?.answers, review?.error,
    review?.plan_sha256, review?.generation_status, review?.approved_at, artifactRetryBlocked(),
    review?.test_coverage, current?.test_coverage,
    testCoverageAvailable(), review?.failure,
    progressId ? [system?.evaluation_planner, current?.planning] : null]);
  const progressPlanner = (() => { const m = current?.planning?.model || system?.evaluation_planner?.model; return m ? {model: m, effort: current?.planning?.effort ?? system?.evaluation_planner?.effort} : null; })();
  if(signature === testReviewSignature) {
    if(progressId && $(progressId)) $(progressId).textContent = testReviewProgress(review, progressPlanner);
    return;
  }
  testReviewSignature = signature;
  const heading = '<div class="section-heading"><span class="step">03</span><h2>Review tests</h2><span class="subtle">Your approval comes before test development</span></div>';
  if(!review || current?.cancel_requested) {
    const available = finished && current.runs.some(r=>r.spec_version===current.spec_version && r.artifacts?.case);
    $("test-review").innerHTML = heading + `<p>${current ?
      "Existing results are preserved. You can review a new test proposal for the saved implementations." :
      "Review proposed scenarios and questions, request changes, then approve tests for the selected harnesses. Coding can continue while you review."}</p>` +
      (available ? `<button id="start-test-review" class="secondary" ${artifactRetryAttributes()}>Review tests for saved code</button>` : "");
    if($("start-test-review")) $("start-test-review").onclick=async()=>{
      try { await api(`/comparisons/${current.id}/test-review/start`,{}); await refresh(); }
      catch(e) { toast(e.message); }
    };
    return;
  }
  const proposal = review.proposal;
  const coverage = review.test_coverage || current.test_coverage || "comprehensive";
  const draft = testReviewDrafts.get(review.id) || {feedback:"", answers:{...review.answers}};
  draft.test_coverage ||= coverage;
  const approvalRecorded = !!review.approved_at;
  if(approvalRecorded) draft.answers = {...review.answers};
  testReviewDrafts.set(review.id, draft);
  const locked = !!review.plan_sha256;
  const retryBlocked = artifactRetryBlocked();
  const canChange = ["awaiting_review","proposal_failed","generation_failed","approved"].includes(review.status) &&
    (!locked || finished) && !(retryBlocked && (finished || ["proposal_failed","generation_failed"].includes(review.status)));
  const approvalAvailable = !!proposal && ["awaiting_review","generation_failed"].includes(review.status) &&
    !locked && !(retryBlocked && review.status==="generation_failed");
  const canApprove = approvalAvailable && draft.test_coverage===coverage;
  const approvalLabel = preparing?"Preparing test cases…":review.status==="generation_failed"?
    "Retry approved test development":approvalRecorded?"Tests approved":"Approve tests for selected harnesses";
  const status = locked ? "Approved tests finalized" : review.status==="approved" ? "Approved · developing executable tests" :
    review.status==="awaiting_review" ? "Your review is needed" : review.status==="proposal_failed" ? "Proposal needs attention" :
    review.status==="generation_failed" ? "Test development needs attention" : "Preparing proposed scenarios…";
  const progress = testReviewProgress(review, progressPlanner);
  const list = (items) => items?.length ? `<ul>${items.map(v=>`<li>${esc(v)}</li>`).join("")}</ul>` : "";
  $("test-review").innerHTML = heading +
    `<p class="test-review-status" role="status">${esc(status)} · Revision ${esc(review.revision)} · Specification v${esc(review.spec_version)}</p>
    <p class="test-coverage-summary"><b>Test coverage:</b> ${esc(testCoverageLabel(coverage))}${review.supplied_plan_sha256?" · All checks from your supplied validation plan are retained.":""}</p>
    ${canChange && testCoverageAvailable() ? `<fieldset class="test-coverage-options"><legend>Change test coverage</legend>
      <div class="test-coverage-grid">${testCoverageChoices("review-test-coverage",draft.test_coverage)}</div>
      <p class="subtle">Changing coverage creates a fresh proposal for your approval, using the same application requirements.</p>
      <button id="update-test-coverage" type="button" class="secondary" ${draft.test_coverage===coverage?"disabled":""}>Regenerate test proposal</button></fieldset>` : ""}
    <p>The same approved scenarios apply to Claude Code, Codex and Kiro, each tested against its own code.${review.mode==="saved_code" ? " Test revisions reuse the saved implementations and retain previous results." : approvalRecorded ? " Your approval and answers are saved." : " Coding continues independently; test development and evaluation wait for approval."}</p>
    ${preparing ? `<p class="notice">${review.status==="queued" ? "The shared test planner is queued." : "The shared planner is preparing the test cases."} The scenarios and review questions will appear here when the proposal is complete. Approval is not available yet.</p>
      <p id="test-proposal-progress" role="status">${esc(progress)}</p>` : ""}
    ${developing ? `<p class="notice">${review.generation_status==="generating" ? "The shared planner is writing executable tests from your approved scenarios." : "Executable test development is queued."} Your answers are recorded; you do not need to enter them again.</p>
      <p id="test-generation-progress" role="status">${esc(progress)}</p>` : ""}
    ${review.failure ? testReviewFailureBlock(review.failure) : review.status==="generation_failed" ? '<p class="notice">The approved tests could not be developed, so these checks have not run. Your approval is retained; retry develops the same approved scenarios.</p>' : ""}
    ${!review.failure && review.error ? `<p class="notice" role="alert">${esc(review.error)}</p>` : ""}
    ${proposal ? `<p>${esc(proposal.summary)}</p>
      <div class="test-scenarios">${proposal.test_cases.map((test,index)=>`<article class="test-scenario"><h3>${index+1}. ${esc(test.title)} <span class="subtle">${esc(test.group)}</span></h3><ol>${test.steps.map(step=>`<li>${esc(step)}</li>`).join("")}</ol><p><b>Expected result:</b> ${esc(test.expected_result)}</p><p><b>Test data:</b> ${esc(test.test_data)}</p></article>`).join("")}</div>
      ${proposal.assumptions?.length ? `<details><summary>Assumptions to review</summary>${list(proposal.assumptions)}</details>` : ""}
      ${proposal.limitations?.length ? `<details><summary>What these tests will not establish</summary>${list(proposal.limitations)}</details>` : ""}
      ${proposal.questions.length ? approvalRecorded ?
        `<div class="test-review-questions"><h3>Approved answers</h3>${proposal.questions.map(q=>`<section><h4>${esc(q.question)}</h4><blockquote class="approved-test-answer" aria-label="Approved answer">${esc(review.answers[q.id] || "Answer not recorded")}</blockquote></section>`).join("")}</div>` :
        `<div class="test-review-questions"><h3>Questions before approval</h3>${proposal.questions.map(q=>`<label for="review-answer-${esc(q.id)}">${esc(q.question)}</label><textarea id="review-answer-${esc(q.id)}" data-test-answer="${esc(q.id)}" rows="2" maxlength="8000" ${canApprove?"":"disabled"} placeholder="${esc(q.suggested_answer || "Enter your answer")}">${esc(draft.answers[q.id] || "")}</textarea>${q.suggested_answer?`<small>Suggested answer: ${esc(q.suggested_answer)}<br>Enter your answer explicitly.</small>`:""}`).join("")}</div>`:""}
      ${review.approved_at ? `<p class="subtle">Approved ${esc(new Date(review.approved_at*1000).toLocaleString())}. ${locked?"Approved scenarios are linked to the generated checks in the evaluation evidence.":""}</p>`:""}` : ""}
    ${review.feedback.length ? `<p class="test-review-feedback"><b>Latest feedback:</b> ${esc(review.feedback.at(-1).message)}</p>`:""}
    <label for="test-review-feedback">Changes to the proposed tests</label>
    <textarea id="test-review-feedback" rows="3" maxlength="20000" ${canChange?"":"disabled"} placeholder="For example: test the actual CLI, cover unknown locations, and use realistic API responses…">${esc(draft.feedback)}</textarea>
    <p class="subtle">Feedback changes the tests. Use Shared task to change what the application should do.</p>
    <div class="test-review-actions"><button id="request-test-changes" type="button" class="secondary" ${canChange?"":"disabled"}>Request changes</button>
      ${review.status==="proposal_failed" ? `<button id="retry-test-proposal" class="secondary" ${retryBlocked?"disabled":""}>Retry proposal</button>`:""}
      <button id="approve-tests" type="button" class="primary" ${canApprove?"":"disabled"}>${draft.test_coverage!==coverage?"Regenerate proposal first":approvalLabel}</button></div>
    ${review.history.length ? `<details><summary>Review history (${review.history.length})</summary>${review.history.map(item=>`<p>Revision ${esc(item.revision)}: ${esc(text(item.status))} · ${esc(testCoverageLabel(item.test_coverage || "comprehensive"))}${item.approved_at ? " · Approved "+esc(new Date(item.approved_at*1000).toLocaleString()) : ""}</p>${item.proposal?`<details><summary>Proposed scenarios</summary>${list(item.proposal.test_cases.map(t=>t.title+": "+t.expected_result))}</details>`:""}`).join("")}</details>`:""}`;
  $("test-review-feedback").oninput=e=>{draft.feedback=e.target.value;};
  if($("view-attempt-record")) $("view-attempt-record").onclick = async () => {
    const rawUrl = $("view-attempt-record").dataset.attemptUrl;
    const url = rawUrl.startsWith("/api") ? rawUrl.slice(4) : rawUrl;
    try { const rec = await api(url); $("attempt-record-content").innerHTML = `<pre>${pretty(rec)}</pre>`; $("attempt-dialog").showModal(); }
    catch(e) { toast(e.message); }
  };
  document.querySelectorAll("[data-test-answer]").forEach(el=>el.oninput=()=>{draft.answers[el.dataset.testAnswer]=el.value;});
  document.querySelectorAll('[name="review-test-coverage"]').forEach(el=>el.onchange=()=>{
    if(!el.checked) return;
    draft.test_coverage=el.value;
    $("update-test-coverage").disabled=draft.test_coverage===coverage;
    $("approve-tests").disabled=!approvalAvailable || draft.test_coverage!==coverage;
    $("approve-tests").textContent=draft.test_coverage!==coverage?"Regenerate proposal first":approvalLabel;
  });
  const submit = async (action, data) => {
    const comparisonId=current.id, reviewId=review.id;
    $("request-test-changes").disabled=true; $("approve-tests").disabled=true;
    if($("update-test-coverage")) $("update-test-coverage").disabled=true;
    try {
      await api(`/comparisons/${comparisonId}/test-review/${action}`,{review_id:reviewId,...data});
      toast(action==="approve" ? "Tests approved. Development of the shared tests can now proceed." : "Feedback saved. A revised proposal will need your approval.");
    } catch(e) { toast(e.message); }
    finally { testReviewSignature=""; if(selected===comparisonId) await refresh(); }
  };
  $("request-test-changes").onclick=()=>{
    if(!draft.feedback.trim()) return toast("Describe the test changes you want.");
    return submit("changes",{feedback:draft.feedback.trim(),
      ...(draft.test_coverage!==coverage?{test_coverage:draft.test_coverage}:{})});
  };
  if($("update-test-coverage")) $("update-test-coverage").onclick=()=>{
    if(!testCoverageAvailable()) return toast("Restart the API and idle worker to load the test coverage options.");
    if(draft.test_coverage===coverage) return;
    return submit("changes",{test_coverage:draft.test_coverage, feedback:draft.feedback.trim() ||
      `Change test coverage to ${testCoverageLabel(draft.test_coverage)}. Prepare a new proposal for approval.`});
  };
  if($("retry-test-proposal")) $("retry-test-proposal").onclick=()=>submit("changes",{feedback:"Retry preparing the proposal with the same requirements and prior feedback."});
  $("approve-tests").onclick=()=>{
    if(draft.test_coverage!==coverage) return toast("Regenerate and review the proposal for the selected coverage before approving.");
    const answers=Object.fromEntries((proposal?.questions || []).map(q=>[q.id,(draft.answers[q.id] || "").trim()]));
    if(Object.values(answers).some(value=>!value)) return toast("Answer every test-review question before approving.");
    return submit("approve",{answers});
  };
}
function renderTable() {
  $("comparison-table").innerHTML = Object.keys(names).map(harness=>{
    const run=current?.runs.find(r=>r.harness===harness), result=run?.evaluation;
    if(!run && (current || !selectedHarnesses().includes(harness))) {
      return `<tr class="harness-off"><td>${esc(names[harness])}</td><td colspan="14">${current?"Not in this comparison":"OFF · Not selected"}</td></tr>`;
    }
    const checks=(run?.evidence?.checks || []).filter(c=>c.status!=="not_applicable");
    const passed=checks.filter(c=>c.status==="passed").length;
    const gaps=[...(result?.accuracy?.criteria || []).filter(c=>c.status==="unverified").map(c=>c.requirement), ...(result?.accuracy?.gaps || []), ...(result?.tests?.gaps || []), result?.relevance?.gap,run?.error,run?.evaluation_scope_note].filter(Boolean);
    if(run && run.spec_version !== current.spec_version) gaps.unshift(`Saved result uses specification v${run.spec_version}; the current specification is v${current.spec_version}.`);
    const gapSummary = gaps.length ? `${gaps.length} verification gap${gaps.length===1?"":"s"}` : result ? "None" : "Awaiting evidence";
    const versionNote = run && run.spec_version !== current.spec_version ? `<small>Earlier specification v${esc(run.spec_version)}</small>` :
      result && current.test_review && result.test_review_id!==current.test_review.id ? "<small>Earlier test review</small>" : "";
    const outcomes = checks.length ? `<button type="button" class="test-outcomes-link" data-evidence="${esc(run.id)}" aria-label="View test outcomes for ${esc(names[harness])}">${passed}/${checks.length} checks passed ↗</button>` : "Not run";
    const scopeNote = run?.evaluation_scope_note ? `<small class="evaluation-scope-note">Implementation saved · different follow-up requirements. <button type="button" class="test-outcomes-link" data-evidence="${esc(run.id)}" aria-label="View verification scope for ${esc(names[harness])}">Details ↗</button></small>` : "";
    return `<tr><td>${esc(names[harness])}<small>${esc(run?.model || (run?.requested_model ? "Requested: " + run.requested_model : "Not reported"))}</small>${versionNote}${run?.model_mismatch?`<small class="model-mismatch">Ran ${esc(run.model_mismatch.reported)} instead of the requested ${esc(run.model_mismatch.requested)}</small>`:""}</td><td title="${esc(current?.prompt || "")}">${current ? esc(current.prompt.slice(0,25))+(current.prompt.length>25?"…":"") : "—"}</td><td>${badge(run?.status)}${scopeNote}${current?.test_review?.failure?`<small class="test-fail-cause">${esc(current.test_review.failure.stage==="test proposal"?"Test proposal":"Test development")} failed: ${esc(current.test_review.failure.cause)} <a href="#test-review-failure" class="test-fail-link">Details ↓</a></small>`:""}</td><td class="metric">${score(result?.accuracy?.score)}</td><td class="metric">${score(result?.relevance?.score)}</td><td class="metric">${score(result?.tests?.score)}</td><td class="metric">${score(result?.overall)}</td><td>${outcomes}</td><td title="${esc(gaps.join("\n"))}">${esc(gapSummary)}</td><td title="${esc(run?.usage?.unavailable_reason || "")}">${tokens(run?.usage?.total)}</td><td>${usageLabel(run)}</td><td>${duration(run?.duration?.active)}</td><td>${duration(run?.duration?.user_wait)}</td><td>${duration(run?.duration?.evaluation)}</td><td>${duration(run?.duration?.total)}</td></tr>`;
  }).join("");
  document.querySelectorAll("[data-evidence]").forEach(el =>
    el.onclick=()=>openReview(el.dataset.evidence, "evidence").catch(e=>toast(e.message)));
  document.querySelectorAll("a.test-fail-link").forEach(a => a.onclick = () => {
    const el = $("test-review-failure");
    if(el) { el.classList.add("failure-highlight"); setTimeout(() => el.classList.remove("failure-highlight"), 1600); }
  });
}
function recommendationText(comparison) {
  if(!comparison) return "Submit a task to get a recommendation based on independently verified results.";
  if(comparison.test_review && !comparison.comparison_complete &&
      comparison.runs.some(r=>r.evaluation && r.evaluation.test_review_id!==comparison.test_review.id))
    return "A new test review is in progress. Previous scores remain visible; a recommendation will follow the approved evaluation.";
  const runs=comparison.runs.filter(r=>r.spec_version===comparison.spec_version);
  const scored=runs.filter(r=>typeof r.evaluation?.overall==="number" && Number.isFinite(r.evaluation.overall));
  if(!scored.length) return ["complete","incomplete","interrupted"].includes(comparison.status) ?
    "No harness can be recommended yet: independent evaluation is incomplete for every implementation." :
    "A recommendation will appear as independent evaluation results arrive.";
  const judges=new Set(scored.map(r=>JSON.stringify([r.evaluation.judge?.model,r.evaluation.judge?.region,r.evaluation.judge?.profile])));
  if(judges.size>1) return "These results use different judges. Re-score all implementations with the same judge before choosing a winner.";
  const verified=scored.filter(r=>r.evaluation.verified_complete);
  const ranked=[...(verified.length?verified:scored)].sort((a,b)=>b.evaluation.overall-a.evaluation.overall);
  const best=ranked[0], overall=score(best.evaluation.overall);
  if(!verified.length) return `No implementation is ready to recommend yet. ${names[best.harness]} leads at ${overall}/100, but still has failed or unverified requirements.`;
  const tied=ranked.filter(r=>Math.abs(r.evaluation.overall-best.evaluation.overall)<.05);
  let message;
  if(tied.length>1) {
    message=`Choose ${tied.map(r=>names[r.harness]).join(" or ")}: they tie at ${overall}/100 and satisfy all required checks.`;
  } else {
    const checks=(best.evidence?.checks || []).filter(c=>c.status!=="not_applicable"), passed=checks.filter(c=>c.status==="passed").length;
    const evidence=checks.length?`${passed}/${checks.length} checks passed`:"all required criteria verified";
    message=`Choose ${names[best.harness]} for this task: ${evidence} and the best verified overall score (${overall}/100).`;
  }
  if(scored.length<comparison.runs.length || runs.some(r=>!terminal.has(r.status))) {
    const timedOut=runs.filter(r=>r.evaluation?.overall==null &&
      String(r.evaluation?.relevance?.gap || "").includes("did not respond within"));
    message+=timedOut.length ?
      ` Provisional: ${timedOut.map(r=>names[r.harness]).join(" and ")}’s judge timed out.` :
      " Provisional: other results are still incomplete.";
  }
  return message;
}
function render() {
  $("page-title").textContent=current?`Comparison ${current.id.slice(0,8)}`:"New comparison";
  $("conversation").innerHTML=current?`<div class="conversation-task">${esc(current.prompt)}</div>${current.answers.map(a=>{const fu=a.kind==="follow_up"||a.question==="User specification revision";return`<div class="conversation-answer${fu?" follow-up":""}"><small>${fu?`Follow-up to ${esc(harnessNames(a.harnesses || availableHarnesses()))}`:`Shared requirement · ${esc(a.question)}`}</small><div>${esc(typeof a.answer==="string"?a.answer:JSON.stringify(a.answer))}</div></div>`;}).join("")}`:"";
  $("spec-badge").textContent=current?`Specification v${current.spec_version} · ${current.frozen?"frozen":"clarifying"}`:"The same starting point for selected harnesses";
  $("export").classList.toggle("hidden",!current);
  $("cancel-all").classList.toggle("hidden",!current || ["complete","incomplete","interrupted"].includes(current.status));
  $("project-mode").parentElement.classList.toggle("hidden",!!current);
  $("plan-options").classList.toggle("hidden",!!current);
  $("model-options").classList.toggle("hidden",!!current);
  $("test-coverage-options").classList.toggle("hidden",!!current);
  if(!$("test-coverage-choices").innerHTML) {
    $("test-coverage-choices").innerHTML=testCoverageChoices("test-coverage","focused");
  }
  renderHarnessSelection();
  updateTestCoverageAvailability();
  updateProjectControls();
  $("submit-task").innerHTML=current?"Send follow-up ↗":"Start comparison <span>↗</span>";
  $("prompt").placeholder=current?"Send a follow-up instruction to the selected harnesses…":"Describe what you want to build or fix…";
  $("prompt").rows=current?2:3;
  $("follow-up-banner").classList.toggle("hidden",!current);
  renderQuestions(); renderPanels(); renderTable(); renderJudge(); renderTestReview(); renderArtifactReadiness();
  $("harness-recommendation").textContent=recommendationText(current);
  const transfer = $("artifact-storage");
  const failed = current?.status === "storage_failed";
  transfer.classList.toggle("hidden", !current?.artifact_storage);
  transfer.innerHTML = current?.artifact_storage ?
    `<p><b>${esc(statusLabel(current.status))}</b>${current.artifact_storage.status==="archived"?" · Artifacts saved in S3; temporary files removed.":""}</p>${current.artifact_error?`<p>${esc(current.artifact_error)}</p>`:""}${failed?'<p>Local files are retained until upload succeeds.</p><button id="retry-artifact-storage" class="secondary">Retry S3 storage</button>':""}` : "";
  if(failed) $("retry-artifact-storage").onclick=async()=>{
    try { await api(`/comparisons/${current.id}/archive`, {}); await refresh(); }
    catch(e) { toast(e.message); }
  };
}
async function refresh() {
  if(!selected) return;
  const requested=selected;
  let report;
  try { report=await api(`/comparisons/${requested}`); }
  catch(error) {
    if(selected!==requested) return;
    if(error.status!==404) throw error;
    newComparison();
    return;
  }
  if(selected!==requested) return;
  current=report;
  render();
}
async function select(id) {
  if(stream) stream.close();
  selected=id; logs=[]; questionSignature="";
  localStorage.setItem("arena-comparison",id);
  await refresh();
  if(selected!==id) return;
  await loadHistory();
  if(selected!==id) return;
  stream=new EventSource(`/api/comparisons/${id}/stream`);
  stream.onmessage=event=>{
    const value=JSON.parse(event.data);
    if(!logs.some(e=>e.seq===value.seq)) logs.push(value);
    if(logs.length>2000) logs=logs.slice(-2000);
    if(!refreshTimer) refreshTimer=setTimeout(()=>{refreshTimer=null;refresh().catch(e=>toast(e.message));},700);
  };
  stream.onopen=()=>{$("stream-label").textContent="● LIVE";};
  stream.onerror=()=>{$("stream-label").textContent="↻ RECONNECTING";};
}
function newComparison() {
  if(stream) stream.close();
  current=null;selected=null;logs=[];questionSignature="";
  localStorage.removeItem("arena-comparison");
  $("prompt").value=""; render();loadHistory();$("prompt").focus();
}
async function openReview(id, tab="summary") {
  reviewRun=current.runs.find(r=>r.id===id);
  $("review-title").textContent=`${names[reviewRun.harness]} · implementation & evidence`;
  $("review-dialog").showModal();
  await reviewTab(tab);
}
function failureReason(check) {
  const lines = String(check.log || "").split("\n").map(line=>line.trim()).filter(Boolean);
  const reversed=[...lines].reverse();
  const error = reversed.find(line =>
    /(?:AssertionError|[A-Za-z]+Error|[A-Za-z]+Exception):|^Error:/.test(line)) ||
    reversed.find(line=>/^FAIL(?:ED)?\b/.test(line));
  return error || check.classification || lines.slice(-3).join("\n") ||
    (check.exit_code == null ? "The check did not produce an exit code." : `The command exited with code ${check.exit_code}.`);
}
function testEvidence(run) {
  const checks=run.evidence?.checks || [];
  if(!checks.length) return "<p>No independent check results are available.</p>";
  const labels={passed:"Passed",failed:"Failed",infrastructure_error:"Infrastructure error",not_run:"Not run",not_applicable:"Not applicable"};
  const summary=Object.entries(labels).map(([status,label]) =>
    `${checks.filter(c=>c.status===status).length} ${label.toLowerCase()}`).join(" · ");
  return `<div class="test-summary"><h3>Test outcomes</h3><p>${checks.length} checks · ${esc(summary)}</p><p>Failed checks show the recorded error and output. Counts below describe individual test cases when the runner reports them.</p></div>` +
    checks.map(c=>{
      const failed=c.status==="failed" || c.status==="infrastructure_error";
      const counts=Object.entries(c.counts || {}).filter(([key,value]) =>
        ["passed","failed","errored","skipped","not_run"].includes(key) && typeof value==="number")
        .map(([key,value])=>`${value} ${text(key)}`).join(" · ");
      const executedCommand=c.resolved_command || c.command;
      const plannedCommand=executedCommand!==c.command?`<details><summary>Original planned command</summary><code>${esc(c.command)}</code></details>`:"";
      return `<section class="evidence-row test-result ${failed?"test-result-failed":""}"><h3>${esc(c.id)} <span class="status-badge ${c.status==="passed"?"good":failed?"warn":""}">${esc(labels[c.status] || text(c.status))}</span></h3><p>${esc(text(c.group))}${c.scored===false?" · Supplemental, excluded from score":""} · Exit code: ${esc(c.exit_code??"unavailable")}</p><code>${esc(executedCommand)}</code>${plannedCommand}<p>${esc(counts || "Individual test counts were not reported.")}</p>${failed?`<div class="test-failure"><b>Why this check failed</b><pre>${esc(failureReason(c))}</pre></div>`:""}${c.classification?`<p>${esc(c.classification)}</p>`:""}${c.test_cases?.length?`<ul>${c.test_cases.map(t=>`<li>${esc(t.id)} · ${esc(text(t.outcome))}</li>`).join("")}</ul>`:""}<details ${failed?"open":""}><summary>Actual command output</summary><pre>${esc(c.log || "No command output was recorded.")}</pre></details></section>`;
    }).join("");
}
async function reviewTab(tab) {
  const r=reviewRun;
  let content="";
  if(tab==="summary") content=`${badge(r.status)}${r.artifacts?.case?` <button id="reverify-run" class="secondary" ${artifactRetryAttributes()}>Re-run checks on saved code</button> <button id="reevaluate-run" class="secondary" ${artifactRetryAttributes()}>Re-score saved evidence</button>`:""}${r.error?`<p>${esc(r.error)}</p>`:""}${verificationScope(r)}${artifactLocations(r)}<div class="evidence-row"><h3>Requirement verification</h3>${(r.evaluation?.accuracy?.criteria||[]).map(c=>`<p><b>${esc(text(c.status))}</b> · ${esc(c.requirement)}<br><small>${esc(c.explanation)} Evidence: ${esc(c.evidence.join(", "))}</small></p>`).join("")||"<p>No independently verified requirements yet.</p>"}</div><h3>Quality assessment</h3><pre>${pretty(r.evaluation || {status:"Independent evaluation has not completed"})}</pre><h3>Timing & native usage</h3>${r.usage?.unavailable_reason?`<p>${esc(r.usage.unavailable_reason)}</p>${creditsNote(r)}`:""}<pre>${pretty({duration:r.duration,usage:r.usage})}</pre><h3>Configuration & native capabilities</h3><pre>${pretty({requested_model:r.requested_model,requested_effort:r.requested_effort,reported_model:r.model,runtime:r.runtime,capabilities:r.capabilities})}</pre>`;
  if(tab==="diff") {
    const d=await api(`/runs/${r.id}/diff`), files=await api(`/runs/${r.id}/files`);
    content=`${r.artifacts?.patch?`<a href="/api/runs/${esc(r.id)}/patch">↓ Download implementation patch</a>`:""}<pre>${d.diff?d.diff.split("\n").map(line=>`<span class="${line.startsWith("+")?"diff-add":line.startsWith("-")?"diff-remove":""}">${esc(line)}</span>`).join("\n"):"No patch collected yet."}</pre><h3>Immutable snapshot files</h3>${files.files.map(path=>`<button class="file-button" data-file="${esc(path)}">${esc(path)}</button>`).join("")}<div id="file-content"></div>`;
  }
  if(tab==="evidence") content=verificationScope(r)+testEvidence(r);
  if(tab==="logs") content=logs.filter(e=>e.run_id===r.id).map(e=>`<details class="evidence-row"><summary>${new Date(e.at*1000).toLocaleTimeString()} · ${esc(e.type)} · ${esc(e.data.method||e.data.message_type||e.data.status||"")}</summary><pre>${pretty(e.data)}</pre></details>`).join("")||"<p>No native events received.</p>";
  if(tab==="prompts") content=r.attempts.map(a=>`<h3>${esc(a.phase)} · spec v${esc(a.spec_version)} · ${esc(a.template_version)}</h3><pre>${esc(a.prompt)}</pre>`).join("")||"<p>No attempts dispatched yet.</p>";
  if(tab==="summary" && ((r.artifact_history||[]).length || (r.evaluation_history||[]).length))
    content+=`<details class="evidence-row"><summary>Previous snapshots and evaluations</summary>${(r.artifact_history||[]).filter(h=>h.artifacts?.case).map(h=>`<p>Specification ${esc(h.spec_version)} · overall ${esc(h.evaluation?.overall??"unavailable")} <button class="secondary" data-evaluate-version="${esc(h.spec_version)}" ${artifactRetryAttributes()}>Re-evaluate version ${esc(h.spec_version)}</button></p>`).join("")}<pre>${pretty({snapshots:r.artifact_history||[],evaluations:r.evaluation_history||[]})}</pre></details>`;
  $("review-content").innerHTML=`<div class="review-tabs">${["summary","diff","evidence","logs","prompts"].map(t=>`<button data-review-tab="${esc(t)}">${esc(t[0].toUpperCase()+t.slice(1))}</button>`).join("")}</div>${content}`;
  document.querySelectorAll("[data-review-tab]").forEach(el=>el.onclick=()=>reviewTab(el.dataset.reviewTab).catch(e=>toast(e.message)));
  if($("reevaluate-run")) $("reevaluate-run").onclick=async()=>{
    try{await api(`/comparisons/${current.id}/reevaluate?run_id=${r.id}`,judgeRequest());$("review-dialog").close();toast(`${names[r.harness]}'s saved evidence queued for evaluation.`);await refresh();}catch(e){toast(e.message);}
  };
  if($("reverify-run")) $("reverify-run").onclick=async()=>{
    try{await api(`/comparisons/${current.id}/reverify`,judgeRequest());$("review-dialog").close();toast("Fresh checks queued for the saved implementations.");await refresh();}catch(e){toast(e.message);}
  };
  document.querySelectorAll("[data-evaluate-version]").forEach(el=>el.onclick=async()=>{
    try{await api(`/comparisons/${current.id}/reevaluate?spec_version=${el.dataset.evaluateVersion}`,judgeRequest());$("review-dialog").close();toast(`Version ${el.dataset.evaluateVersion} queued; current-version scores stay separate.`);await refresh();}catch(e){toast(e.message);}
  });
  document.querySelectorAll("[data-file]").forEach(el=>el.onclick=async()=>{
    try { const file=await api(`/runs/${r.id}/file?path=${encodeURIComponent(el.dataset.file)}`);$("file-content").innerHTML=`<h3>${esc(file.path)}</h3><pre>${esc(file.content)}</pre>`; } catch(e){toast(e.message);}
  });
}
$("new-comparison").onclick=newComparison;
$("access-form").onsubmit=event=>{event.preventDefault();connectWorkspace($("access-link").value);};
$("check-connection").onclick=()=>connectWorkspace();
$("compact-history").onchange=event=>{if(event.target.value)select(event.target.value).catch(e=>toast(e.message));};
$("project-mode").onchange=updateProjectControls;
$("refresh-models").onclick=()=>loadModels(true);
$("refresh-judge-models").onclick=()=>loadJudgeModels(true);
$("judge-model").onchange=()=>{judgeDrafts.set(current?.id || "new", $("judge-model").value);renderJudge();};
$("judge-reevaluate").onclick=async()=>{
  if(!current) return;
  $("judge-reevaluate").disabled = true;
  try {
    await api(`/comparisons/${current.id}/reevaluate`,judgeRequest());
    toast("Saved evidence queued for scoring with the selected judge.");
    await refresh();
  } catch(e) { toast(e.message); renderJudge(); }
};
$("browse-project").onclick=openFolders;
$("folder-root").onchange=()=>browseFolder($("folder-root").value);
$("folder-up").onclick=()=>{if(browsing?.parent)browseFolder(browsing.parent);};
$("select-folder").onclick=()=>{
  if(!browsing?.path) return;
  $("repository").value=browsing.path; $("project-mode").value="repository";
  updateProjectControls(); $("folder-dialog").close(); $("prompt").focus();
};
$("settings-button").onclick=()=>{$("settings-dialog").showModal();};
document.querySelectorAll(".close-dialog").forEach(b=>b.onclick=()=>b.closest("dialog").close());
$("export").onclick=()=>{if(current)window.location.href=`/api/comparisons/${current.id}/report`;};
$("cancel-all").onclick=async()=>{try{await api(`/comparisons/${current.id}/cancel`,{});await refresh();}catch(e){toast(e.message);}};
$("task-form").onsubmit=async(event)=>{
  event.preventDefault();
  if(taskSubmitting) return;
  taskSubmitting=true;$("submit-task").disabled=true;
  try{
    renderHarnessSelection();
    if(!harnessSelectionAvailable()) throw Error("Restart the API and idle worker to enable coding harness selection.");
    const harnesses=selectedHarnesses();
    if(!harnesses.length) throw Error("Select at least one coding harness.");
    const prompt=$("prompt").value.trim();
    if(!prompt) throw Error("Enter a prompt before sending.");
    if(current){
      const reason=followUpBlockedReason();
      if(reason) throw Error(reason);
      await api(`/comparisons/${current.id}/revisions`,{text:prompt,harnesses});$("prompt").value="";toast(`Follow-up sent to ${harnessNames(harnesses)}.`);await refresh();
    }
    else{
      if(!testCoverageAvailable()) throw Error("Restart the API and idle worker to load the test coverage options.");
      const plan=$("validation-plan").value.trim();
      const repository=$("project-mode").value==="repository"?$("repository").value.trim():null;
      if(repository === "") throw Error("Browse to a project folder or enter its path.");
      const models=Object.fromEntries(harnesses.map(h=>[h,$("model-"+h).value || null]));
      const efforts=Object.fromEntries(harnesses.map(h=>[h,$("effort-"+h).value || null]));
      const test_coverage=document.querySelector('[name="test-coverage"]:checked')?.value || "focused";
      // Collect per-harness configuration choice from the config-choice selectors
      const configuration_choices=Object.fromEntries(harnesses.map(h=>[h,$("config-choice-"+h)?.value||"default"]));
      const c=await api("/comparisons",{prompt,repository,harnesses,models,efforts,...judgeRequest(),planning_model:$("planning-model").value||null,test_coverage,validation_plan:plan?JSON.parse(plan):null,configuration_choices});
      $("prompt").value="";await select(c.id);
    }
  }catch(e){toast(e.message);}finally{taskSubmitting=false;renderHarnessSelection();updateTestCoverageAvailability();}
};
const initialToken = new URLSearchParams(location.hash.slice(1)).get("token");
if(initialToken) history.replaceState(null,"",location.pathname + location.search);
connectWorkspace(initialToken || undefined);
setInterval(()=>{
  if(!connected) return;
  loadSystem().catch(()=>{});loadHistory().catch(()=>{});
  if(selected)refresh().catch(()=>{});
},5000);
