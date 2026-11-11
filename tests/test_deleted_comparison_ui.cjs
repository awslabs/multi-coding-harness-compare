const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync(require.resolve("../arena/static/app.js"), "utf8");

function browser(fetch) {
  const saved = new Map([["arena-comparison", "old"]]);
  const nodes = new Map();
  const streams = [];
  const context = vm.createContext({
    fetch, console,
    localStorage: {
      getItem: key => saved.get(key) ?? null,
      setItem: (key, value) => saved.set(key, value),
      removeItem: key => saved.delete(key),
    },
    document: {
      getElementById(id) {
        if(!nodes.has(id)) nodes.set(id, {value:"", innerHTML:"", focus() {}});
        return nodes.get(id);
      },
      querySelectorAll() { return []; },
    },
    EventSource: class {
      constructor(url) { this.url = url; streams.push(this); }
      close() { this.closed = true; }
    },
  });
  vm.runInContext(app.slice(0, app.indexOf('$("new-comparison").onclick=newComparison;')), context);
  vm.runInContext('render=()=>{}; current={id:"old"}; stream=new EventSource("old");', context);
  return {context, saved, nodes, streams};
}

const response = (status, data) => ({ok:status < 400, status, json:async()=>data});

(async () => {
  // A remembered comparison deleted from both local storage and S3 opens an
  // empty workspace instead of turning a successful login into an auth error.
  const deleted = browser(async path => path === "/api/comparisons"
    ? response(200, []) : response(404, {detail:"Not found"}));
  await vm.runInContext('select("old")', deleted.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInContext("selected", deleted.context), null);
  assert.equal(vm.runInContext("current", deleted.context), null);
  assert.equal(deleted.saved.has("arena-comparison"), false);
  assert.equal(deleted.streams.length, 1, "No event stream for a deleted comparison");
  assert.equal(deleted.streams[0].closed, true);
  assert.equal(deleted.nodes.get("history-count").textContent, 0);

  // A comparison absent from the local history can still be restored by the
  // API from S3. Only an actual 404 should clear the browser's selection.
  const restored = browser(async path => path === "/api/comparisons"
    ? response(200, []) : response(200, {id:"old"}));
  await vm.runInContext('select("old")', restored.context);
  assert.equal(restored.saved.get("arena-comparison"), "old");
  assert.equal(vm.runInContext("current.id", restored.context), "old");
  assert.equal(restored.streams.at(-1).url, "/api/comparisons/old/stream");

  const unavailable = browser(async () => response(503, {detail:"Temporarily unavailable"}));
  await assert.rejects(vm.runInContext("refresh()", unavailable.context), /Temporarily unavailable/);
  assert.equal(unavailable.saved.get("arena-comparison"), "old");

  // A slow response for the previous selection must not clear a newer one.
  let finish;
  const stale = browser(() => new Promise(resolve => { finish = resolve; }));
  const pending = vm.runInContext("refresh()", stale.context);
  vm.runInContext('selected="new"; current={id:"new"};', stale.context);
  finish(response(404, {detail:"Not found"}));
  await pending;
  assert.equal(vm.runInContext("selected", stale.context), "new");
  assert.equal(vm.runInContext("current.id", stale.context), "new");
  console.log("Deleted comparison UI recovery passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
