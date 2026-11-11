const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../apps/expert_study/app.js"), "utf8");

function element() {
  const classes = new Set(), listeners = new Map();
  return { dataset: {}, readyState: 2, error: null, disabled: false,
    classList: { contains: value => classes.has(value), add: value => classes.add(value),
      remove: value => classes.delete(value) },
    addEventListener(type, fn) { listeners.set(type, fn); },
    emit(type) { listeners.get(type)?.(); } };
}

function harness({ view = "event-based", stage = "review", fail = null } = {}) {
  const calls = [], messages = [], frames = [], nodes = new Map();
  const video = element(), summaryVideo = element(), frameWindow = {};
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  nodes.set("sessionVideo", video);
  nodes.set("summaryReviewFrame", { contentWindow: frameWindow,
    contentDocument: { getElementById: id => id === "sessionVideo" ? summaryVideo : null } });
  const flow = { schema: "sequential_review_v2", stage, step_id: "review_0", index: 0,
    order: [view, view === "summary" ? "event-based" : "summary"], current_view: view };
  const state = { participantId: "TEST", start: { run_id: "P15T4", case: {} },
    reviewFlow: flow, reviewView: view, reviewNextPending: false, timingStep: null,
    timingCoverage: new Set(), summaryReady: false, reviewTimer: {
      start: config => calls.push(["start", config]),
      cover: key => calls.push(["cover", key]),
      suspend: () => calls.push(["suspend"]),
      flush: async () => { calls.push(["flush"]); if (fail === "flush") throw new Error("Timing save failed"); },
      resume: () => calls.push(["resume"]),
      finish: async reason => { calls.push(["finish", reason]); if (fail === "finish") throw new Error("Final save failed"); },
    } };
  const context = vm.createContext({ state,
    document: { getElementById: get },
    window: { location: { origin: "http://localhost:8818" },
      addEventListener: (type, fn) => { if (type === "message") messages.push(fn); },
      requestAnimationFrame: fn => frames.push(fn), scrollTo() {} },
    studySurveys: { setPending() {}, clearDraft() {}, showError() {} },
    async api(url, payload) {
      calls.push(["api", url, payload]);
      if (fail === "transition") throw new Error("Transition failed");
      return { review_flow: { ...flow, stage: "questionnaire", step_id: "questionnaire_0" },
        case_completed: false, next_case_url: null, completion_code: null };
    },
    setReviewFlow(value) { calls.push(["setFlow", value.stage]); state.reviewFlow = value; },
    renderReviewFlow: () => calls.push(["render", state.reviewFlow.stage]),
    updateReviewGate: () => calls.push(["gate"]),
  });
  vm.runInContext(source.slice(source.indexOf("function recordReviewCoverage(key)"),
    source.indexOf("const REVIEW_DESCRIPTIONS")), context);
  vm.runInContext(source.slice(source.indexOf("async function advanceReviewFlow(answers = null)"),
    source.indexOf("// End of the study:")), context);
  const post = (data, override = {}) => messages.forEach(fn => fn({
    origin: "http://localhost:8818", source: frameWindow, data, ...override,
  }));
  return { context, state, calls, get, video, summaryVideo, post,
    paint: () => { while (frames.length) frames.shift()(); } };
}

test("Event timing waits for video readiness and preserves pre-ready coverage", () => {
  const h = harness();
  h.video.readyState = 1;
  h.context.recordReviewCoverage("moment:one");
  h.context.startReviewTiming();
  assert.equal(h.state.timingStep, null);
  h.video.readyState = 2;
  h.video.emit("loadeddata");
  assert.equal(h.state.timingStep, "review_0");
  assert.equal(h.calls.filter(row => row[0] === "start").length, 1);
  assert.equal(h.calls.at(-1)[1], "moment:one");
  h.context.startReviewTiming();
  assert.equal(h.calls.filter(row => row[0] === "start").length, 1);
});

test("Summary starts only after authenticated ready message and its own video readiness", () => {
  const h = harness({ view: "summary" });
  h.summaryVideo.readyState = 0;
  h.context.startReviewTiming();
  h.post({ type: "summary-review-ready" }, { source: {} });
  h.post({ type: "summary-review-ready" }, { origin: "https://elsewhere.test" });
  h.paint();
  assert.equal(h.state.summaryReady, false);
  h.post({ type: "summary-review-ready" });
  h.paint();
  assert.equal(h.state.timingStep, null);
  h.summaryVideo.readyState = 2;
  h.summaryVideo.emit("canplay");
  const started = h.calls.find(row => row[0] === "start")[1];
  assert.equal(started.view, "summary");
  assert.equal(started.video, h.summaryVideo);
});

test("No timing starts in surveys, hidden review, or during Next; unavailable media permits text review", () => {
  const h = harness({ stage: "baseline" });
  h.context.startReviewTiming();
  h.state.reviewFlow.stage = "review";
  h.get("experiencePanel").classList.add("hidden");
  h.context.startReviewTiming();
  h.get("experiencePanel").classList.remove("hidden");
  h.state.reviewNextPending = true;
  h.context.startReviewTiming();
  assert.equal(h.calls.length, 0);
  h.state.reviewNextPending = false;
  h.video.readyState = 0;
  h.video.classList.add("hidden");
  h.context.startReviewTiming();
  assert.equal(h.calls[0][0], "start");
});

test("Summary coverage requires the correct iframe, review stage and known section", () => {
  const h = harness({ view: "summary" });
  const coverage = { type: "summary-review-coverage", phase: "incus", group: "motion" };
  h.post(coverage, { source: {} });
  h.post(coverage, { origin: "https://elsewhere.test" });
  h.post({ ...coverage, group: "invented" });
  assert.equal(h.state.timingCoverage.size, 0);
  h.post(coverage);
  h.post({ ...coverage, group: null });
  assert.deepEqual([...h.state.timingCoverage], ["section:incus:motion", "phase:incus"]);
  h.state.reviewFlow.stage = "questionnaire";
  h.post({ ...coverage, phase: "antrum" });
  assert.equal(h.state.timingCoverage.size, 2);
  h.state.reviewFlow.stage = "review";
  h.state.reviewView = "event-based";
  h.post({ ...coverage, phase: "antrum" });
  assert.equal(h.state.timingCoverage.size, 2);
});

test("Summary reports its initial phase if activation arrives after its data", () => {
  const summarySource = fs.readFileSync(path.join(__dirname, "../apps/summary/app.js"), "utf8");
  const handlers = [], messages = [];
  const parent = { postMessage: (message, origin) => messages.push({ message, origin }) };
  const summaryState = { active: false, data: { phases: [{ phase_name: "incus" }] } };
  const context = vm.createContext({ embeddedSummary: true, summaryState,
    selectedPhase: () => summaryState.data.phases[0],
    window: { parent, location: { origin: "http://localhost:8818" },
      addEventListener: (type, fn) => handlers.push(fn) },
    document: { documentElement: { style: { setProperty() {} } }, querySelectorAll: () => [] },
  });
  vm.runInContext(summarySource.slice(summarySource.indexOf("function reportCoverage(group = null)"),
    summarySource.indexOf("function renderPhase()")), context);
  vm.runInContext(summarySource.slice(summarySource.indexOf('window.addEventListener("message"'),
    summarySource.indexOf("const metricDefinitions")), context);
  context.reportCoverage();
  assert.equal(messages.length, 0);
  handlers[0]({ origin: "http://localhost:8818", source: parent,
    data: { type: "study-review-visibility", active: true } });
  assert.equal(messages[0].message.type, "summary-review-coverage");
  assert.equal(messages[0].message.phase, "incus");
});

test("An Event moment buffers its category and phase before readiness without leaking to later survey steps", () => {
  const h = harness();
  h.state.experience = { media: { available: true } };
  h.state.reviewedMoments = new Set();
  Object.assign(h.context, {
    EventReview: { reviewKey: () => "clip-one" },
    eventReview: { refreshViewed() {}, titleFor: () => "Motion" },
    playClip() {}, jumpToKeyframe() {}, keyframeFor: () => ({ time: 0 }),
  });
  vm.runInContext(source.slice(source.indexOf("function openReviewMoment(item, clip, index, mode)"),
    source.indexOf("function playClip(clip, title, index)")), h.context);
  const item = { metric_key: "tool_speed_mm_s", phase: "session" };
  h.context.openReviewMoment(item, { phase: "incus" }, 0, "context");
  assert.deepEqual([...h.state.timingCoverage].sort(), [
    "category:tool_speed_mm_s", "moment:clip-one", "phase:incus",
  ]);
  h.state.timingCoverage.clear();
  h.state.reviewNextPending = true;
  h.context.openReviewMoment(item, { phase: "antrum" }, 0, "context");
  assert.equal(h.state.timingCoverage.size, 0);
  h.state.reviewNextPending = false;
  h.state.reviewFlow.stage = "questionnaire";
  h.context.openReviewMoment(item, { phase: "antrum" }, 0, "context");
  assert.equal(h.state.timingCoverage.size, 0);
});

for (const fail of ["flush", "transition"]) {
  test(`Failed ${fail} resumes the review timer without advancing the survey`, async () => {
    const h = harness({ fail });
    h.state.timingStep = "review_0";
    await h.context.advanceReviewFlow();
    assert.deepEqual(h.calls.slice(0, 2).map(row => row[0]), ["suspend", "flush"]);
    assert.ok(h.calls.some(row => row[0] === "resume"));
    assert.ok(!h.calls.some(row => row[0] === "finish" || row[0] === "render"));
    assert.equal(h.calls.filter(row => row[0] === "api").length, fail === "flush" ? 0 : 1);
    assert.equal(h.state.reviewFlow.stage, "review");
    assert.equal(h.state.reviewNextPending, false);
    assert.match(h.get("reviewNextError").textContent, /failed/i);
  });
}

test("Premature Next waits for content readiness and can start timing after the pending request clears", async () => {
  const h = harness();
  h.video.readyState = 0;
  await h.context.advanceReviewFlow();
  assert.ok(!h.calls.some(row => row[0] === "api" || row[0] === "flush"));
  assert.equal(h.state.reviewFlow.stage, "review");
  assert.match(h.get("reviewNextError").textContent, /still loading/i);
  h.video.readyState = 2;
  h.paint();
  assert.equal(h.state.timingStep, "review_0");
});

for (const fail of [null, "finish"]) {
  test(`Accepted Next stops timing before the questionnaire even if final acknowledgement ${fail ? "fails" : "succeeds"}`, async () => {
    const h = harness({ fail });
    h.state.timingStep = "review_0";
    h.state.timingCoverage.add("moment:one");
    await h.context.advanceReviewFlow();
    assert.deepEqual(h.calls.slice(0, 6).map(row => row[0]), ["suspend", "flush", "api", "finish", "setFlow", "render"]);
    assert.ok(!h.calls.some(row => row[0] === "resume"));
    assert.equal(h.state.reviewFlow.stage, "questionnaire");
    assert.equal(h.state.timingStep, null);
    assert.equal(h.state.timingCoverage.size, 0);
    h.context.startReviewTiming();
    assert.ok(!h.calls.some(row => row[0] === "start"));
  });
}
