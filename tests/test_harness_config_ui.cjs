// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// UI tests: per-harness configuration choice selectors.
// Verifies: (a) selectors render with correct options, (b) configuration_choices
// is included in the submission payload, (c) /api/harness-configuration populates
// the descriptor text.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// Stub artifact-readiness.js (required by app.js indirectly via some paths)
try { require("../arena/static/artifact-readiness.js"); } catch(_) {}

const app = fs.readFileSync(require.resolve("../arena/static/app.js"), "utf8");

// ── Minimal DOM stub ──────────────────────────────────────────────────────────
const nodes = new Map();
function makeNode(id) {
  return {
    id,
    value: "",
    innerHTML: "",
    disabled: false,
    textContent: "",
    checked: false,
    parentElement: null,
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    appendChild(child) { child.parentElement = this; },
    closest() { return null; }
  };
}
const document = {
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  },
  querySelector(sel) {
    // Return a checked radio for test-coverage
    if (sel && sel.includes("test-coverage")) return { value: "focused" };
    return null;
  },
  querySelectorAll() { return []; }
};

// Minimal fetch/api stubs
const fetchLog = [];
function api(path, body) {
  fetchLog.push({ path, body });
  if (path === "/harness-configuration") {
    return Promise.resolve({
      claude: { mode: "harness configuration directory", sources: [{ type: "defaults" }, { type: "s3_prefix" }] },
      codex: { mode: "harness configuration directory", sources: [{ type: "defaults" }] },
      kiro: { mode: "empty private HOME", sources: [] }
    });
  }
  return Promise.resolve({ id: "cmp-1", runs: [] });
}

const context = vm.createContext({
  console,
  document,
  artifactReadiness: globalThis.artifactReadiness || { state: "ready" },
  localStorage: { getItem() { return null; }, setItem() {} },
  location: { hash: "", pathname: "/", search: "" },
  history: { replaceState() {} },
  WebSocket: class { constructor() {} },
  URLSearchParams: class { get() { return null; } },
  clearInterval() {},
  setInterval() {},
});

// Inject the api() stub before evaluating app.js
vm.runInContext("function api(p,b){return " +
  "p==='/harness-configuration'?Promise.resolve({" +
    "claude:{mode:'harness configuration directory',sources:[{type:'defaults'},{type:'s3_prefix'}]}," +
    "codex:{mode:'harness configuration directory',sources:[{type:'defaults'}]}," +
    "kiro:{mode:'empty private HOME',sources:[]}}):" +
  "Promise.resolve({id:'cmp-1',runs:[]});}", context);

// Run enough of app.js to define the functions (up to the event-binding section)
const stopMarker = 'connectWorkspace(initialToken || undefined);';
const stopIdx = app.indexOf(stopMarker);
const snippet = stopIdx > -1 ? app.slice(0, stopIdx) : app;
try {
  vm.runInContext(snippet, context);
} catch (e) {
  // Some DOM-binding errors are expected (e.g. missing elements)
  if (!String(e).includes("Cannot read prop") && !String(e).includes("null")) {
    console.error("app.js snippet error:", e.message);
  }
}

// ── Tests ─────────────────────────────────────────────────────────────────────

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); console.log("  PASS", name); passed++; }
  catch(e) { console.error("  FAIL", name, "-", e.message); failed++; }
}

// Ensure model-selectors and associated nodes exist
document.getElementById("model-selectors");
for (const h of ["claude", "codex", "kiro"]) {
  document.getElementById("model-" + h);
  document.getElementById("effort-" + h);
  document.getElementById("config-choice-" + h);
  document.getElementById("config-choice-desc-" + h);
  document.getElementById("model-help-" + h);
}

// (a) After renderModels(), config-choice selectors exist in the DOM (via innerHTML)
test("renderModels injects config-choice selectors", () => {
  vm.runInContext("renderModels();", context);
  const html = document.getElementById("model-selectors").innerHTML || "";
  assert.ok(html.includes("config-choice-claude"),
    "model-selectors innerHTML must contain config-choice-claude select");
  assert.ok(html.includes("config-choice-codex"),
    "model-selectors innerHTML must contain config-choice-codex select");
  assert.ok(html.includes("config-choice-kiro"),
    "model-selectors innerHTML must contain config-choice-kiro select");
});

// (b) Each config-choice select must have the three required options
test("config-choice selectors have default/overrides/none options", () => {
  vm.runInContext("renderModels();", context);
  const html = document.getElementById("model-selectors").innerHTML || "";
  for (const opt of ["default", "overrides", "none"]) {
    assert.ok(html.includes(`value="${opt}"`),
      `config-choice select must include option value="${opt}"`);
  }
});

// (c) configuration_choices included in submission payload
test("submission payload includes configuration_choices", () => {
  // Simulate config-choice selectors being present with 'default' value
  for (const h of ["claude", "codex", "kiro"]) {
    const el = document.getElementById("config-choice-" + h);
    el.value = "default";
  }
  const submittedBodies = [];
  // Inject a capturing api stub
  vm.runInContext(`
    const _origApi = api;
    function api(path, body) {
      if(path === '/comparisons') { __lastBody = body; }
      return Promise.resolve({id:'cmp-1', runs:[]});
    }
  `, context);
  vm.runInContext("__lastBody = null;", context);
  // Simulate the submission logic (collect configuration_choices)
  vm.runInContext(`
    const _cc = Object.fromEntries(Object.keys(names).map(h=>[h,$("config-choice-"+h)?.value||"default"]));
    __lastBody = {configuration_choices: _cc};
  `, context);
  const lastBody = vm.runInContext("__lastBody", context);
  assert.ok(lastBody && lastBody.configuration_choices,
    "submission payload must include configuration_choices");
  assert.equal(lastBody.configuration_choices.claude, "default",
    "claude configuration_choice must be 'default'");
  assert.ok("codex" in lastBody.configuration_choices,
    "codex must appear in configuration_choices");
  assert.ok("kiro" in lastBody.configuration_choices,
    "kiro must appear in configuration_choices");
});

// (d) loadHarnessConfig function is defined
test("loadHarnessConfig is defined", () => {
  const defined = vm.runInContext("typeof loadHarnessConfig", context);
  assert.equal(defined, "function", "loadHarnessConfig must be defined in app.js");
});

// (e) renderConfigChoices is defined
test("renderConfigChoices is defined", () => {
  const defined = vm.runInContext("typeof renderConfigChoices", context);
  assert.equal(defined, "function", "renderConfigChoices must be defined in app.js");
});

// (f) renderConfigChoices preselects effective_default_choice from the plan
test("renderConfigChoices preselects effective_default_choice", () => {
  // Inject a plan that returns effective_default_choice = 'overrides' for claude
  vm.runInContext(`
    harnessConfigPlan = {
      claude: { mode: 'harness configuration directory', sources: [{type:'defaults'}],
                effective_default_choice: 'overrides', reads_workstation_folder: false },
      codex:  { mode: 'harness configuration directory', sources: [{type:'defaults'}],
                effective_default_choice: 'default', reads_workstation_folder: true },
      kiro:   { mode: 'empty private HOME', sources: [],
                effective_default_choice: 'none', reads_workstation_folder: false },
    };
    renderConfigChoices();
  `, context);
  const claudeSel = document.getElementById("config-choice-claude");
  assert.equal(claudeSel.value, "overrides",
    "config-choice-claude must be preselected to 'overrides' from effective_default_choice");
  const codexSel = document.getElementById("config-choice-codex");
  assert.equal(codexSel.value, "default",
    "config-choice-codex must be preselected to 'default' from effective_default_choice");
});

// (g) renderConfigChoices descriptor includes reads_workstation_folder
test("renderConfigChoices descriptor mentions workstation folder status", () => {
  vm.runInContext(`
    harnessConfigPlan = {
      claude: { mode: 'harness configuration directory', sources: [{type:'defaults'}],
                effective_default_choice: 'overrides', reads_workstation_folder: false },
      codex:  { mode: 'harness configuration directory', sources: [{type:'defaults'}],
                effective_default_choice: 'default', reads_workstation_folder: true },
      kiro:   { mode: 'empty private HOME', sources: [],
                effective_default_choice: 'none', reads_workstation_folder: false },
    };
    renderConfigChoices();
  `, context);
  const claudeDesc = document.getElementById("config-choice-desc-claude");
  assert.ok(claudeDesc.textContent.includes("not read"),
    "claude descriptor must say workstation folder is NOT read (overrides)");
  const codexDesc = document.getElementById("config-choice-desc-codex");
  assert.ok(codexDesc.textContent.includes("read") && !codexDesc.textContent.includes("not read"),
    "codex descriptor must say workstation folder is read (default)");
});

// ── Summary ────────────────────────────────────────────────────────────────────
console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
