/* Shared display logic; no network requests or automatic comparison retries. */
globalThis.artifactReadiness = (() => {
  const escape = value => String(value ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const blocked = state => !state || (state.enabled && !state.ready);
  const bytes = count => {
    if (!(count > 0)) return "0 B";
    const index = Math.min(3, Math.floor(Math.log(count) / Math.log(1024)));
    return `${(count / (1024 ** index)).toFixed(index ? 1 : 0)} ${["B","KiB","MiB","GiB"][index]}`;
  };
  function render(state, comparison) {
    if (state?.enabled === false) return "";
    state ||= {status:"pending", message:"Waiting for the worker's S3 readiness check."};
    // Readiness on a comparison also gates saved-code retries. A new run's
    // required working files are not an unfinished S3 transfer.
    // The API derives this from the same phases used by the worker. Do not
    // maintain a second, potentially incomplete list of active phases here.
    if (state.status === "in_use") {
      const comparisonStatus = comparison?.status, review = comparison?.test_review;
      const queued = comparisonStatus === "queued" || comparisonStatus?.endsWith("_queued");
      const planning = ["planning","waiting_test_review","reviewing_tests"].includes(comparisonStatus);
      const activity = planning && ["queued","drafting"].includes(review?.status) ?
        "Preparing test cases" : planning && review?.status === "approved" && !review.plan_sha256 ?
        "Developing approved tests" : comparisonStatus === "waiting_user" ? "Awaiting your answer" :
        comparisonStatus === "waiting_approval" ? "Awaiting harness approval" :
        `Comparison ${queued ? "queued" : "running"}`;
      return `<div class="readiness-heading"><h2>S3 check complete</h2><span>${escape(activity)}</span></div>
        <p role="status">Temporary local files are in use by this comparison. They will be uploaded to S3 and removed after the active work stops.</p>`;
    }
    const title = state.status === "restart_required" ? "Restart required for S3 readiness reporting" :
      state.ready ? "S3 artifacts ready" : state.status === "failed" ?
      "S3 artifact recovery failed" : state.status === "offline" ? "S3 readiness unavailable" : "Preparing S3 artifacts";
    const total = state.comparisons_total || 0, complete = state.comparisons_ready || 0;
    const phases = {stopping_interrupted_jobs:"Stopping interrupted jobs", comparisons:"Checking comparison archives",
      leftovers:"Checking leftover files and logs", waiting:"Waiting for recovery", complete:"Recovery checks finished"};
    const transfer = state.transfer;
    const transferLabel = transfer?.status === "evicting" ? "Removing temporary files" :
      transfer?.status === "restoring" ? "Loading saved artifacts" :
      transfer?.transfer_phase === "verifying" ? "Verifying uploaded artifacts" : "Uploading artifacts";
    const determinate = state.ready || (total > 0 && complete < total);
    const progress = ["failed","offline","restart_required"].includes(state.status) ? "" :
      `<progress aria-label="S3 artifact readiness" ${determinate ? `max="${escape(total || 1)}" value="${escape(state.ready ? total || 1 : complete)}"` : ""}></progress>`;
    return `<div class="readiness-heading"><h2>${escape(title)}</h2><span>${state.ready ? "Ready to retry" : "Comparison retries paused"}</span></div>
      <p role="status">${escape(state.message)}</p>${progress}
      ${total ? `<p>Comparison archives: <b>${escape(complete)} of ${escape(total)}</b> ready.</p>` : ""}
      ${!state.ready && state.status !== "restart_required" ? `<p>${escape(phases[state.phase] || "Checking S3 readiness")}${state.current_comparison ? ` · Comparison ${escape(state.current_comparison.slice(0,8))}` : ""}</p>` : ""}
      ${transfer ? `<p>${escape(transferLabel)}${typeof transfer.uploaded_bytes === "number" ? ` · ${escape(bytes(transfer.uploaded_bytes))} uploaded` : ""}${transfer.id ? ` · ${escape(transfer.id.startsWith("extra-") ? "leftover files" : transfer.id.slice(0,8))}` : ""}</p>` : ""}
      ${state.error ? `<p class="readiness-error" role="alert">${escape(state.error)}</p>` : ""}
      ${state.can_retry_recovery ? '<button id="retry-s3-recovery" class="secondary" type="button">Retry S3 recovery</button>' : ""}`;
  }
  return {blocked, render};
})();
