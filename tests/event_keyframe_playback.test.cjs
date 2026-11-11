const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor() {
    const classes = new Set();
    this.classList = { add: value => classes.add(value), remove: value => classes.delete(value),
      contains: value => classes.has(value) };
    this.listeners = new Map();
    this.textContent = "";
  }
  addEventListener(type, listener, options = {}) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push({ listener, once: options.once });
  }
  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter(row => row.listener !== listener));
  }
  emit(type) {
    for (const row of [...(this.listeners.get(type) || [])]) {
      if (row.once) this.removeEventListener(type, row.listener);
      row.listener();
    }
  }
}

async function harness(app, { available = true, readyState = 1, duration = 100 } = {}) {
  const video = Object.assign(new Element(), { readyState, duration, currentTime: 0, paused: true });
  video.pause = () => { video.paused = true; video.emit("pause"); };
  video.play = () => { video.paused = false; video.emit("play"); return Promise.resolve(); };
  const elements = { sessionVideo: video, videoHeading: new Element(), videoWindow: new Element(),
    videoUnavailable: new Element(), experiencePanel: new Element() };
  const state = { mediaAction: 0, metadataHandler: null, keyframeSeek: null, stopAt: null,
    bootstrap: { media: { available, url: "/api/video" }, run: { duration_s: duration } },
    experience: { media: { available, url: "/api/video" }, session: { duration_s: duration } },
    reviewView: "event-based", reviewedMoments: new Set() };
  const marks = [], logs = [];
  const context = vm.createContext({
    window: { location: { href: "http://localhost:8815/", origin: "http://localhost:8815" } }, URL,
    state, document: { getElementById: id => {
      assert.ok(elements[id], `Unexpected DOM dependency: ${id}`);
      return elements[id];
    } },
    eventReview: { mark: (index, action) => marks.push({ index, action }),
      titleFor: () => "Critical Structure Violation", refreshViewed() {}, render() {} },
    fmtTime: value => String(value), logEvent: (...args) => logs.push(args), updateReviewGate() {},
    renderRunSelector() {}, api: async () => state.bootstrap,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../apps/recommendations/event-review.js"), "utf8"), context);
  context.EventReview = context.window.EventReview;
  const source = fs.readFileSync(path.join(__dirname, `../apps/${app}/app.js`), "utf8");
  const start = source.indexOf("function keyframeFor(clip)");
  const end = app === "expert_study" ? source.indexOf("function renderExperience()", start) : source.lastIndexOf("init();");
  vm.runInContext(source.slice(start, end), context);
  if (app === "expert_study") context.bindVideo();
  else await context.init();
  return { context, video, state, marks, logs };
}

const clip = { clip_id: "safety-one", t_start_s: 10, t_end_s: 15,
  keyframe_available: true, keyframe_url: "/api/media/keyframe?clip_id=bound",
  keyframe_time_s: 12.345, keyframe_frame_idx_global: 123 };

for (const app of ["recommendations", "expert_study"]) {
  test(`${app}: key frame seeks and pauses; native play resumes without image elements`, async () => {
    const { context, video, state, marks } = await harness(app);
    state.stopAt = 15;
    context.jumpToKeyframe(clip, "Safety", 2);
    assert.equal(video.currentTime, 12.345);
    assert.equal(video.paused, true);
    assert.equal(state.stopAt, null);
    video.emit("seeking");
    assert.equal(marks.at(-1).action, "keyframe");
    video.emit("seeked");
    await video.play();
    assert.equal(video.paused, false);
    assert.equal(video.currentTime, 12.345);
    assert.equal(marks.at(-1).action, "context");
  });

  test(`${app}: latest key-frame request wins when metadata arrives later`, async () => {
    const { context, video, state } = await harness(app, { readyState: 0 });
    context.jumpToKeyframe(clip, "First", 0);
    const obsolete = state.metadataHandler;
    context.jumpToKeyframe({ ...clip, keyframe_time_s: 25 }, "Second", 1);
    obsolete();
    assert.equal(video.currentTime, 0);
    video.readyState = 1;
    video.emit("loadedmetadata");
    assert.equal(video.currentTime, 25);
    assert.equal(video.paused, true);
  });

  test(`${app}: context playback supersedes a pending key-frame seek`, async () => {
    const { context, video, state } = await harness(app, { readyState: 0 });
    context.jumpToKeyframe(clip, "Safety", 0);
    const obsolete = state.metadataHandler;
    context.playClip({ t_start_s: 40, t_end_s: 45 }, "Context", 1);
    obsolete();
    video.readyState = 1;
    video.emit("loadedmetadata");
    assert.equal(video.currentTime, 35);
    assert.equal(video.paused, false);
    assert.equal(state.stopAt, 50);
  });

  test(`${app}: manual play cancels a pending key-frame seek`, async () => {
    const { context, video, state } = await harness(app, { readyState: 0 });
    context.jumpToKeyframe(clip, "Safety", 0);
    const obsolete = state.metadataHandler;
    await video.play();
    obsolete();
    assert.equal(state.metadataHandler, null);
    assert.equal(video.currentTime, 0);
    assert.equal(video.paused, false);
  });

  test(`${app}: no-video and unverified-anchor cases cannot seek or mark a moment`, async () => {
    const unavailable = await harness(app, { available: false });
    unavailable.context.jumpToKeyframe(clip, "Safety", 0);
    assert.equal(unavailable.video.currentTime, 0);
    assert.equal(unavailable.marks.length, 0);
    const invalid = await harness(app);
    invalid.context.jumpToKeyframe({ ...clip, keyframe_available: false }, "Safety", 0);
    assert.equal(invalid.video.currentTime, 0);
    assert.equal(invalid.marks.length, 0);
  });

  test(`${app}: seek clamps to video duration and manual scrubbing clears pressed state`, async () => {
    const { context, video, marks } = await harness(app, { duration: 10 });
    context.jumpToKeyframe(clip, "Safety", 0);
    assert.equal(video.currentTime, 10);
    assert.equal(video.paused, true);
    video.emit("seeked");
    video.currentTime = 6;
    video.emit("seeking");
    assert.equal(marks.at(-1).action, "context");
    assert.equal(video.currentTime, 6);
  });
}
