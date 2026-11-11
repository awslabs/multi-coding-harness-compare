const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
require("../arena/static/artifact-readiness.js");
const app = fs.readFileSync(require.resolve("../arena/static/app.js"), "utf8");

const nodes = new Map();
const document = {getElementById(id) {
  if(!nodes.has(id)) nodes.set(id, {value:"", innerHTML:"", disabled:false, classList:{toggle(){},add(){},remove(){}}});
  return nodes.get(id);
}, querySelector(){return null;}, querySelectorAll(){return [];}};
const context = vm.createContext({console, document, artifactReadiness:globalThis.artifactReadiness,
  localStorage:{getItem(){return null;}}});
vm.runInContext(app.slice(0, app.indexOf('$("new-comparison").onclick=newComparison;')), context);
vm.runInContext(`
  system = {harnesses: {claude: {model: "sonnet", effort: null}, codex: {model: "sol", effort: null}}};
  modelCatalogue = {harnesses: {
    claude: {configured_default: "sonnet", models: [
      {id: "opus-4-6", efforts: ["low", "medium", "high", "max"]}, {id: "haiku", efforts: []}, {id: "bedrock-only"}]},
    codex: {configured_default: "sol", models: [{id: "sol", efforts: ["low", "xhigh"], default_effort: "low"}]}}};
`, context);
const options = id => [...nodes.get(id).innerHTML.matchAll(/value="([^"]*)"/g)].map(m => m[1]);

// A picked model offers exactly its advertised levels; an unsupported level is cleared.
document.getElementById("model-claude");
nodes.get("model-claude").value = "opus-4-6";
context.renderEffort("claude", "xhigh");
assert.deepEqual(options("effort-claude"), ["", "low", "medium", "high", "max"]);
assert.equal(nodes.get("effort-claude").value, "");
context.renderEffort("claude", "max");
assert.equal(nodes.get("effort-claude").value, "max");

// A model without effort support disables the selector.
nodes.get("model-claude").value = "haiku";
context.renderEffort("claude", "max");
assert.equal(nodes.get("effort-claude").disabled, true);
assert.match(nodes.get("effort-claude").innerHTML, /not supported/);

// Unknown support (no catalogue metadata) offers every level.
nodes.get("model-claude").value = "bedrock-only";
context.renderEffort("claude", "");
assert.deepEqual(options("effort-claude"), ["", "low", "medium", "high", "xhigh", "max"]);
assert.equal(nodes.get("effort-claude").disabled, false);

// No model picked: the configured default model decides, and its default effort is shown.
document.getElementById("model-codex").value = "";
context.renderEffort("codex", "");
assert.deepEqual(options("effort-codex"), ["", "low", "xhigh"]);
assert.match(nodes.get("effort-codex").innerHTML, /Model&#39;s default effort · low/);
console.log("effort UI tests passed");

// Startup probes once: a cold cache already ran discovery on the first read, so no refresh follows.
(async () => {
  const calls = [];
  context.calls = calls;
  vm.runInContext(`
    document.getElementById("refresh-models"); document.getElementById("model-status"); document.getElementById("model-selectors");
    renderModels = () => {}; toast = () => {};
    api = async (path) => { calls.push(path); return {checked_at: path.endsWith("refresh") ? 300 : catalogueAge, harnesses: {}}; };
  `, context);
  context.catalogueAge = 200;          // discovered after the page opened at 100
  await context.loadModelsAtStartup(100);
  assert.deepEqual(calls, ["/models"]);
  calls.length = 0;
  context.catalogueAge = 50;           // an older cache: refresh it
  await context.loadModelsAtStartup(100);
  assert.deepEqual(calls, ["/models", "/models/refresh"]);
  console.log("startup catalogue tests passed");
})().catch(e => { console.error(e); process.exit(1); });
