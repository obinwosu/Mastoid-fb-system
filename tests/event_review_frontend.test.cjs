const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const context = { window: { location: { href: "http://localhost:8815/", origin: "http://localhost:8815" } }, URL };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../apps/recommendations/event-review.js"), "utf8"), context);
const review = context.window.EventReview;

test("ranked clips preserve deviation order, including high values, without mutating input", () => {
  const clips = [
    { clip_id: "late-high", rank: 1, t_start_s: 60, observed: 30 },
    { clip_id: "early-low", rank: 2, t_start_s: 10, observed: 1 },
  ];
  const item = { feedback_id: "event_review:antrum:speed_mm_s", metric_key: "speed_mm_s", clips };
  assert.deepEqual(Array.from(review.clipsFor(item), clip => clip.clip_id), ["late-high", "early-low"]);
  assert.equal(review.reviewKey(item, 0), "late-high");
  assert.equal(review.clipsFor(item)[0], clips[0]);
});

test("empty ranked categories create no synthetic review moment", () => {
  assert.equal(review.clipsFor({ metric_key: "speed_mm_s", clips: [], t_start_s: 0 }).length, 0);
});

test("context adds five seconds on both sides and clamps to the session", () => {
  const middle = review.contextBounds({ t_start_s: 10, t_end_s: 15 }, 100);
  assert.equal(middle.start, 5);
  assert.equal(middle.end, 20);
  const edge = review.contextBounds({ t_start_s: 2, t_end_s: 98 }, 100);
  assert.equal(edge.start, 0);
  assert.equal(edge.end, 100);
  assert.equal(review.contextBounds({ t_start_s: null, t_end_s: 5 }, 100), null);
});

test("key frames require supplied same-origin media metadata", () => {
  const clip = { keyframe_available: true, keyframe_url: "/api/media/keyframe?frame=25", keyframe_time_s: 2.5 };
  assert.equal(review.keyframeFor(clip).time, 2.5);
  assert.equal(review.keyframeFor({ ...clip, keyframe_available: false }), null);
  assert.equal(review.keyframeFor({ ...clip, keyframe_url: "https://example.com/api/media/keyframe" }), null);
  assert.equal(review.keyframeFor({ t_start_s: 2, t_end_s: 3 }), null);
  assert.equal(review.keyframeFor({ ...clip, keyframe_time_s: null }), null);
});

test("removal rate joins speed and stroke length in Drilling motion", () => {
  for (const metric_key of ["speed_mm_s", "mean_stroke_length_mm", "removal_rate_mm3_s"]) {
    assert.equal(review.describe({ metric_key }).group, "Drilling motion");
  }
});

test("phase filters preserve global ranks and source indexes without refilling", () => {
  const item = { feedback_id: "event_review:session:speed_mm_s", metric_key: "speed_mm_s", phase: "session", clips: [
    { clip_id: "antrum-high", rank: 1, phase: "antrum", observed: 30 },
    { clip_id: "incus-low", rank: 2, phase: "incus", observed: 1 },
    { clip_id: "antrum-low", rank: 3, phase: "antrum", observed: 2 },
    { clip_id: "early-high", rank: 4, phase: "early_surface", observed: 45 },
  ] };
  const rows = review.visibleClips(item, "antrum");
  assert.deepEqual(Array.from(rows, row => [row.index, row.clip.rank]), [[0, 1], [2, 3]]);
  assert.equal(review.reviewKey(item, rows[1].index), "antrum-low");
  assert.deepEqual(Array.from(review.visibleClips(item), row => row.clip.clip_id), item.clips.map(clip => clip.clip_id));
  assert.equal(review.visibleClips({ ...item, clips: item.clips.slice(0, 3) }, "early_surface").length, 0);
  assert.deepEqual(item.clips.map(clip => clip.rank), [1, 2, 3, 4]);
  assert.equal(review.phaseLabel({ phase: "incus", phases: ["antrum", "incus"] }, item), "Incus");
});

test("category completion still requires selected moments hidden by the current phase filter", () => {
  const item = { feedback_id: "event_review:session:speed_mm_s", metric_key: "speed_mm_s", clips: [
    { clip_id: "antrum", phase: "antrum" }, { clip_id: "incus", phase: "incus" },
  ] };
  const viewed = new Set();
  const isReviewed = (row, index) => viewed.has(review.reviewKey(row, index));
  for (const row of review.visibleClips(item, "antrum")) viewed.add(review.reviewKey(item, row.index));
  assert.equal(review.allClipsViewed(item, isReviewed), false);
  viewed.add("incus");
  assert.equal(review.allClipsViewed(item, isReviewed), true);
  assert.equal(review.allClipsViewed({ ...item, clips: [] }, isReviewed), false);
});

test("each selected window plots its own matched-phase expert reference", () => {
  const reference = { available: true, q25: 10, median: 20, q75: 30, points: [10, 20, 30], unit: "mm/s" };
  const item = { metric_key: "speed_mm_s", reference: { ...reference, q25: 100, median: 200, q75: 300, points: [100, 200, 300] } };
  const clip = { observed: 5, phase: "antrum", reference };
  const html = review.comparisonHtml(item, clip);
  assert.match(html, /central half 10 to 30 mm\/s/);
  assert.doesNotMatch(html, /central half 100 to 300/);
  assert.match(html, /In this clip, tool speed was lower than typical expert values for this phase\./);
  assert.match(html, /Middle half of experts/);
  assert.match(html, /25th–75th percentiles of same-phase expert five-second window values/);
  assert.match(html, /class="event-axis-unit">mm\/s/);
  assert.doesNotMatch(html, /class="event-current-label"/);
  assert.match(html, /class="event-current-dot"/);
  assert.equal(review.comparisonHtml(item, { ...clip, reference: { available: false } }), "");
});

test("comparison sentences describe the selected window's direction for each motion metric", () => {
  const reference = { q25: 10, median: 20, q75: 30 };
  const cases = [
    ["speed_mm_s", 5, "In this clip, tool speed was lower than typical expert values for this phase."],
    ["speed_mm_s", 35, "In this clip, tool speed was higher than typical expert values for this phase."],
    ["mean_stroke_length_mm", 5, "Mean stroke length in this clip was shorter than typical expert values for this phase."],
    ["mean_stroke_length_mm", 35, "Mean stroke length in this clip was longer than typical expert values for this phase."],
    ["removal_rate_mm3_s", 5, "In this clip, bone removal rate was lower than typical expert values for this phase."],
    ["removal_rate_mm3_s", 35, "In this clip, bone removal rate was higher than typical expert values for this phase."],
  ];
  for (const [metric_key, observed, expected] of cases) {
    assert.equal(review.comparisonSentence({ metric_key }, { observed }, reference), expected);
  }
  for (const observed of [10, 20, 30, null]) {
    assert.equal(review.comparisonSentence({ metric_key: "speed_mm_s" }, { observed }, reference), "");
  }
});

test("Safety details keep the measured amount without the former intrusion label", () => {
  const violation = { metric_key: "critical_structure_violation", unit: "mm" };
  const clip = { observed: 0.21, anatomy: "FacialNerve" };
  const html = review.safetyHtml(violation, clip);
  assert.match(html, /Up to 0\.21 mm deep/);
  assert.match(html, /Facial Nerve/);
  assert.doesNotMatch(html, /Maximum measured intrusion|>Structure</);
  assert.match(review.safetyHtml({ metric_key: "poor_drill_visibility", unit: "s" }, { observed: 1.64 }), /1\.64 s of poor visibility/);
  assert.match(review.safetyHtml(violation, { ...clip, anatomy: "<script>" }), /&lt;script&gt;/);
  assert.equal(review.safetyHtml(violation, { observed: null }), "");
});
