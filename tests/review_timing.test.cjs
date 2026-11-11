const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function target(extra = {}) {
  const events = new Map();
  return { ...extra,
    addEventListener(type, handler) {
      if (!events.has(type)) events.set(type, new Set());
      events.get(type).add(handler);
    },
    removeEventListener(type, handler) { events.get(type)?.delete(handler); },
    emit(type, details = {}) { for (const handler of events.get(type) || []) handler(details); },
  };
}

function storageAdapter(data = new Map()) {
  return { get length() { return data.size; }, key: i => [...data.keys()][i],
    getItem: key => data.get(key) || null, setItem: (key, value) => data.set(key, value),
    removeItem: key => data.delete(key), data };
}

function harness({ storage = storageAdapter(), send, origin = 1700000000000, idPrefix = "visit" } = {}) {
  let mono = 0, wall = origin, focused = true, ids = 0;
  const intervalTasks = new Map(), timeoutTasks = [];
  const doc = target({ visibilityState: "visible", hasFocus: () => focused });
  const win = target({ document: doc, performance: { now: () => mono }, localStorage: storage,
    crypto: { randomUUID: () => `${idPrefix}-${++ids}` },
    setInterval: callback => { const id = intervalTasks.size + 1; intervalTasks.set(id, callback); return id; },
    clearInterval: id => intervalTasks.delete(id), setTimeout: callback => timeoutTasks.push(callback),
  });
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../apps/expert_study/review-timing.js"), "utf8"), { window: win });
  const sent = [];
  const timer = win.ReviewTiming.create({ participantId: "P", runId: "R", window: win, wallNow: () => wall,
    send: async (snapshot, settings) => { sent.push({ snapshot, settings }); if (send) await send(snapshot, settings); } });
  const video = target({ paused: true, ended: false, seeking: false, readyState: 4, playbackRate: 1 });
  const start = (view = "event-based") => timer.start({ stepId: view === "summary" ? "review_1" : "review_0",
    view, reviewIndex: view === "summary" ? 1 : 0, video });
  const tick = (ms, wallMs = ms) => { mono += ms; wall += wallMs; };
  return { timer, video, doc, win, sent, storage, start, tick,
    focus(value, source = win) { focused = value; source.emit(value ? "focus" : "blur"); timeoutTasks.splice(0).forEach(task => task()); },
    visibility(value) { doc.visibilityState = value; doc.emit("visibilitychange"); },
    heartbeat() { [...intervalTasks.values()].forEach(task => task()); },
    drain: () => new Promise(resolve => setImmediate(resolve)),
  };
}

const plain = value => JSON.parse(JSON.stringify(value));
function durations(snapshot) {
  const totals = { observed: 0, foreground: 0, video: 0 };
  for (const [start, end, foreground, video] of snapshot.intervals) {
    totals.observed += end - start;
    if (foreground) totals.foreground += end - start;
    if (video) totals.video += end - start;
  }
  return totals;
}

test("review begins explicitly after readiness; static reading counts without mouse activity", async () => {
  const h = harness();
  h.tick(12000);
  assert.equal(h.timer.snapshot(), null);
  h.start();
  h.tick(5000); h.heartbeat();
  h.tick(5000); h.heartbeat();
  await h.timer.flush();
  const value = h.timer.snapshot();
  assert.equal(value.started_at_ms, 1700000012000);
  assert.deepEqual(durations(value), { observed: 10000, foreground: 10000, video: 0 });
  h.timer.destroy();
});

test("hidden and unfocused periods are elapsed time without foreground or video credit", () => {
  const h = harness(); h.start();
  h.tick(3000); h.visibility("hidden");
  h.tick(4000); h.visibility("visible");
  h.tick(2000); h.focus(false);
  h.tick(3000); h.focus(true);
  h.tick(1000);
  const value = h.timer.snapshot();
  assert.equal(value.observed_until_ms - value.started_at_ms, 13000);
  assert.deepEqual(durations(value), { observed: 13000, foreground: 6000, video: 0 });
  h.timer.destroy();
});

test("Summary iframe focus does not count as leaving the parent document", () => {
  const h = harness(); h.start("summary");
  h.tick(2000);
  // Parent blur fires for iframe focus, but containing document.hasFocus stays true.
  h.win.emit("blur"); h.focus(true);
  h.tick(3000);
  assert.equal(durations(h.timer.snapshot()).foreground, 5000);
  h.timer.destroy();
});

test("leaving the browser while Summary video owns focus stops its parent clock", async () => {
  const h = harness();
  const childWindow = target();
  h.video.ownerDocument = { defaultView: childWindow };
  h.start("summary");
  h.tick(1000); h.video.paused = false; h.video.emit("playing");
  h.tick(2000); h.focus(false, childWindow);
  h.tick(4000); h.focus(true, childWindow);
  h.tick(2000); await h.timer.flush();
  assert.deepEqual(durations(h.timer.snapshot()), { observed: 9000, foreground: 5000, video: 4000 });
  h.timer.destroy();
});

test("a player without future frame data receives no playing-time credit", () => {
  const h = harness(); h.start();
  h.video.readyState = 2; h.video.paused = false; h.video.emit("playing");
  h.tick(3000); h.video.readyState = 4; h.video.emit("playing");
  h.tick(2000); h.video.readyState = 2; h.video.emit("waiting");
  h.tick(3000);
  assert.equal(durations(h.timer.snapshot()).video, 2000);
  h.timer.destroy();
});

test("video uses playing wall time, excluding buffering, seeks, pause and background", () => {
  const h = harness(); h.start();
  h.tick(1000); h.video.paused = false; h.video.emit("playing");
  h.tick(2000); h.video.emit("waiting");
  h.tick(1000); h.video.emit("playing");
  h.tick(2000); h.video.seeking = true; h.video.emit("seeking");
  h.tick(1000); h.video.seeking = false; h.video.emit("seeked");
  h.video.playbackRate = 2; // Video speed does not scale participant wall time.
  h.tick(2000); h.visibility("hidden");
  h.tick(1000); h.visibility("visible");
  h.tick(2000); h.video.paused = true; h.video.emit("pause");
  h.tick(1000);
  assert.deepEqual(durations(h.timer.snapshot()), { observed: 13000, foreground: 12000, video: 8000 });
  h.timer.destroy();
});

test("delayed callbacks and sleeping clocks create explicit gaps instead of attention", () => {
  const h = harness(); h.start();
  h.tick(5000); h.heartbeat();
  h.tick(60000); h.heartbeat();
  h.tick(5000); h.heartbeat();
  h.tick(100, 30000); h.heartbeat();
  h.tick(5000);
  const value = h.timer.snapshot();
  assert.equal(value.observed_until_ms - value.started_at_ms, 105000);
  assert.deepEqual(durations(value), { observed: 15000, foreground: 15000, video: 0 });
  h.timer.destroy();
});

test("a backwards system-clock adjustment never creates overlapping intervals", () => {
  const h = harness(); h.start();
  h.tick(1000); h.heartbeat();
  h.tick(1000, -30000); h.heartbeat();
  h.tick(1000);
  const value = h.timer.snapshot();
  assert.equal(value.observed_until_ms - value.started_at_ms, 3000);
  assert.deepEqual(plain(value.intervals), [[0, 1000, true, false], [2000, 3000, true, false]]);
  h.timer.destroy();
});

test("Next freezes the endpoint; successful save and transition latency is excluded", async () => {
  const h = harness(); h.start(); h.tick(4000); h.timer.suspend();
  h.tick(10000); await h.timer.flush();
  await h.timer.finish();
  h.tick(20000);
  const value = h.timer.snapshot();
  assert.equal(value.observed_until_ms - value.started_at_ms, 4000);
  assert.equal(value.closed, true);
  assert.equal(value.end_reason, "next");
  assert.equal(durations(value).foreground, 4000);
  h.timer.destroy();
});

test("failed Next resumes the same open visit without counting submission wait as reading", async () => {
  const h = harness(); h.start(); h.tick(4000); h.timer.suspend();
  h.tick(3000); await h.timer.flush();
  h.timer.resume(); h.tick(2000);
  const value = h.timer.snapshot();
  assert.equal(value.closed, false);
  assert.equal(value.observed_until_ms - value.started_at_ms, 9000);
  assert.deepEqual(durations(value), { observed: 9000, foreground: 6000, video: 0 });
  h.timer.destroy();
});

test("failed sends survive reload; restored snapshots keep their visit and sequence", async () => {
  const storage = storageAdapter();
  const first = harness({ storage, send: () => { throw new Error("offline"); } });
  first.start(); first.tick(5000);
  await assert.rejects(first.timer.flush(), /offline/);
  const saved = [...storage.data.values()].map(JSON.parse)[0];
  first.timer.destroy();
  const second = harness({ storage, idPrefix: "new-visit", origin: 1700000020000 });
  await second.drain();
  assert.equal(second.sent[0].snapshot.visit_id, saved.visit_id);
  assert.equal(second.sent[0].snapshot.sequence, saved.sequence);
  assert.equal(storage.length, 0);
  second.start(); second.tick(1000); await second.timer.flush();
  assert.equal(second.timer.snapshot().visit_id, "new-visit-1");
  assert.equal(durations(second.timer.snapshot()).foreground, 1000);
  second.timer.destroy();
});

test("an in-flight acknowledgement cannot erase a newer pending snapshot", async () => {
  let release;
  const h = harness({ send: () => new Promise(resolve => { release = resolve; }) });
  h.start(); await h.drain();
  h.tick(4000);
  const flush = h.timer.flush();
  const newest = JSON.parse([...h.storage.data.values()][0]);
  release(); await h.drain();
  assert.equal(JSON.parse([...h.storage.data.values()][0]).sequence, newest.sequence);
  release(); await h.drain();
  // The constructor retry and start flush may each drain the shared queue.
  if (h.storage.length) { release(); await h.drain(); }
  await flush;
  assert.equal(h.storage.length, 0);
  h.timer.destroy();
});

test("pagehide persists a closed visit; back-forward cache creates a fresh visit", async () => {
  const h = harness(); h.start(); h.tick(3000); h.win.emit("pagehide");
  await h.drain();
  assert.equal(h.timer.snapshot().end_reason, "pagehide");
  assert.ok(h.sent.some(item => item.settings.keepalive));
  h.tick(30000); h.win.emit("pageshow", { persisted: true }); h.tick(2000);
  const value = h.timer.snapshot();
  assert.equal(value.visit_id, "visit-2");
  assert.equal(value.started_at_ms, 1700000033000);
  assert.equal(durations(value).foreground, 2000);
  h.timer.destroy();
});

test("coverage is deduplicated and scoped to a review visit", async () => {
  const h = harness(); h.start();
  h.timer.cover("event:moment-1"); h.timer.cover("event:moment-1");
  h.timer.cover("phase:incus"); await h.timer.finish();
  h.timer.cover("outside-review");
  assert.deepEqual(plain(h.timer.snapshot().coverage), ["event:moment-1", "phase:incus"]);
  h.start("summary"); h.timer.cover("summary:incus:safety");
  assert.deepEqual(plain(h.timer.snapshot().coverage), ["summary:incus:safety"]);
  h.timer.destroy();
});

test("unavailable persistent storage still saves through the service", async () => {
  const denied = { get length() { throw new Error("denied"); },
    getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  const h = harness({ storage: denied }); h.start(); h.tick(2000); await h.timer.flush();
  assert.equal(durations(h.sent.at(-1).snapshot).foreground, 2000);
  h.timer.destroy();
});

test("corrupt cached data does not prevent recovering other pending visits", async () => {
  const storage = storageAdapter();
  storage.setItem("review_timing_v1:P:R:broken", "not json");
  const old = { schema: "review_timing_v1", visit_id: "prior-visit", sequence: 3 };
  storage.setItem("review_timing_v1:P:R:prior-visit", JSON.stringify(old));
  const h = harness({ storage }); await h.drain();
  assert.deepEqual(plain(h.sent[0].snapshot), old);
  assert.equal(storage.getItem("review_timing_v1:P:R:prior-visit"), null);
  h.timer.destroy();
});

test("a full browser cache does not replace newer memory data with an old snapshot", async () => {
  const storage = storageAdapter();
  let offline = true;
  const h = harness({ storage, send: () => { if (offline) throw new Error("offline"); } });
  h.start(); await h.drain();
  const previous = JSON.parse([...storage.data.values()][0]);
  storage.setItem = () => { throw new Error("quota exceeded"); };
  h.tick(3000); offline = false; await h.timer.flush();
  const sent = h.sent.at(-1).snapshot;
  assert.ok(sent.sequence > previous.sequence);
  assert.equal(durations(sent).foreground, 3000);
  h.timer.destroy();
});
