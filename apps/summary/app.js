"use strict";

const summaryParameters = new URL(window.location.href).searchParams;
const embeddedSummary = summaryParameters.get("embedded") === "1";
const summaryState = { data: null, phaseIndex: 0, seekHandler: null, mediaAvailable: false, active: !embeddedSummary,
  opened: {} };  // phase index -> set of sections (safety, motion) the resident has opened
const REVIEW_GROUPS = ["safety", "motion"];
if (embeddedSummary) document.body.classList.add("embedded-summary");
window.addEventListener("message", event => {
  if (!embeddedSummary || event.origin !== window.location.origin || event.source !== window.parent
      || event.data?.type !== "study-review-visibility") return;
  summaryState.active = event.data.active === true;
  if (Number.isFinite(event.data.viewport_height) && event.data.viewport_height > 0) {
    document.documentElement.style.setProperty("--review-viewport-height", `${event.data.viewport_height}px`);
  }
  if (!summaryState.active) document.querySelectorAll("video, audio").forEach(media => media.pause());
  else if (summaryState.data) reportCoverage();
});
const metricDefinitions = [
  { key: "stroke_length_mm", label: "Stroke length", unit: "mm" },
  { key: "tool_speed_mm_s", label: "Tool speed", unit: "mm/s" },
  { key: "removal_rate_mm3_s", label: "Removal rate", unit: "mm³/s" },
];
const svgNamespace = "http://www.w3.org/2000/svg";
const numericFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function displayValue(value) {
  return finiteNumber(value) ? numericFormat.format(value) : "Unavailable";
}

function timeLabel(seconds) {
  if (!finiteNumber(seconds)) return "Unavailable";
  const total = Math.max(0, seconds);
  const minutes = Math.floor(total / 60);
  return `${minutes}:${(total - minutes * 60).toFixed(1).padStart(4, "0")}`;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function svgElement(tag, attributes, text) {
  const node = document.createElementNS(svgNamespace, tag);
  Object.entries(attributes || {}).forEach(([name, value]) => node.setAttribute(name, String(value)));
  if (text != null) node.textContent = text;
  return node;
}

function selectedPhase() {
  return summaryState.data.phases[summaryState.phaseIndex];
}

function phaseLabel(phase) {
  const labels = { early_surface: "Early Drilling", early_phase: "Early Drilling", antrum: "Antrum", incus: "Incus" };
  return phase.label || labels[phase.phase_name] || "Phase";
}

// Plain definitions shown under the phase name.
const phaseDefinitions = {
  early_surface: "Initial removal of the mastoid cortex.",
  early_phase: "Initial removal of the mastoid cortex.",
  antrum: "Identifying and removing Körner's septum to open the mastoid antrum.",
  incus: "Identifying the short process of the incus.",
};

function validIntervals(phase) {
  return (phase.intervals || []).filter(interval => finiteNumber(interval.t_start_s)
    && finiteNumber(interval.t_end_s) && interval.t_end_s > interval.t_start_s);
}

function referenceValues(reference) {
  if (reference?.available !== true) return null;
  const { q25, median, q75 } = reference;
  const points = (reference.points || []).filter(point => finiteNumber(point.value));
  if (!points.length || ![q25, median, q75].every(finiteNumber) || q25 > median || median > q75) return null;
  const spread = q75 - q25;
  const visible = points.filter(point => point.value >= q25 - 1.5 * spread && point.value <= q75 + 1.5 * spread);
  const plotted = visible.length ? visible : points;
  const lower = Math.min(q25, ...plotted.map(point => point.value));
  const upper = Math.max(q75, ...plotted.map(point => point.value));
  const padding = Math.max((upper - lower) * .08, Math.abs(median) * .025, .001);
  return { q25, median, q75, points: plotted, lower: Math.max(0, lower - padding), upper: upper + padding };
}

// Where this recording sits relative to the middle half of expert recordings.
function comparison(reference, value) {
  if (!reference || !finiteNumber(value)) return null;
  if (value > reference.q75) return { text: "Higher than most experts", kind: "outside" };
  if (value < reference.q25) return { text: "Lower than most experts", kind: "outside" };
  return { text: "Similar to most experts", kind: "inside" };
}

function drawDistribution(svg, metric, reference, value) {
  const width = svg.getBoundingClientRect().width;
  if (width < 25) return;
  svg.setAttribute("viewBox", `0 0 ${width} 52`);
  svg.replaceChildren();
  const left = 7, right = width - 7;
  const x = number => left + (number - reference.lower) / (reference.upper - reference.lower) * (right - left);
  const currentAvailable = finiteNumber(value);
  svg.append(svgElement("title", {}, `${metric.label}: ${currentAvailable ? `${displayValue(value)} ${metric.unit}` : "current value unavailable"}. Expert band ${displayValue(reference.q25)} to ${displayValue(reference.q75)} ${metric.unit}.`));
  svg.append(svgElement("line", { x1: left, x2: right, y1: 32, y2: 32, stroke: "#e1e6ea" }));
  svg.append(svgElement("rect", { x: x(reference.q25), y: 25, width: Math.max(1, x(reference.q75) - x(reference.q25)), height: 14, rx: 3, fill: "#dfe6eb" }));
  svg.append(svgElement("line", { x1: x(reference.median), x2: x(reference.median), y1: 24, y2: 40, stroke: "#3d5466", "stroke-width": 2 }));
  reference.points.forEach((point, index) => {
    const dot = svgElement("circle", { cx: x(point.value), cy: 16 - (index % 3) * 4, r: 2.4, fill: "#93a2ae" });
    dot.append(svgElement("title", {}, `Expert recording: ${displayValue(point.value)} ${metric.unit}`));
    svg.append(dot);
  });
  if (currentAvailable) {
    const currentX = Math.max(left, Math.min(right, x(value)));
    svg.append(svgElement("line", { x1: currentX, x2: currentX, y1: 10, y2: 40, stroke: "#0e7c6b", "stroke-width": 2 }));
    const shape = value > reference.upper ? `M ${right - 9} 1 L ${right} 7 L ${right - 9} 13 Z`
      : value < reference.lower ? `M ${left + 9} 1 L ${left} 7 L ${left + 9} 13 Z`
        : `M ${currentX} 0 l 6 6 l -6 6 l -6 -6 Z`;
    svg.append(svgElement("path", { d: shape, fill: "#0e7c6b" }));
  }
  const ticks = [reference.lower, reference.upper];
  ticks.forEach((value, index) => svg.append(svgElement("text", {
    x: x(value), y: 51, "font-size": 11, fill: "#8a98a3",
    "text-anchor": index === 0 ? "start" : index === ticks.length - 1 ? "end" : "middle",
  }, displayValue(value))));
}

function drawPlots() {
  document.querySelectorAll(".metric-plot").forEach(svg => {
    const metric = selectedPhase().metrics?.find(item => item.key === svg.dataset.metric);
    const reference = referenceValues(metric?.reference);
    const definition = metricDefinitions.find(item => item.key === svg.dataset.metric);
    if (reference && definition) drawDistribution(svg, definition, reference, metric.available === false ? null : metric.value);
  });
}

function renderMotion(phase) {
  const root = document.getElementById("motionMetrics");
  root.replaceChildren(...metricDefinitions.map(definition => {
    const metric = phase.metrics?.find(item => item.key === definition.key) || {};
    const current = metric.available === false ? null : metric.value;
    const reference = referenceValues(metric.reference);
    const row = element("div", "metric-row");
    const name = element("div", "metric-name", definition.label);
    name.append(element("div", "metric-unit", definition.unit));
    const chart = element("div", "metric-chart");
    if (reference) {
      const svg = svgElement("svg", { class: "metric-plot", role: "img", "aria-label": `Expert ${definition.label.toLowerCase()} distribution`, "data-metric": definition.key });
      chart.append(svg);
    } else chart.append(element("p", "plot-unavailable", "Expert reference unavailable"));
    const value = element("div", `metric-value${finiteNumber(current) ? "" : " unavailable"}`);
    // The value is the resident's mean over this phase, so say so (2026-11-11).
    if (finiteNumber(current)) {
      value.append(element("span", "metric-value-label", "Your average: "), displayValue(current),
        element("span", "metric-value-unit", ` ${definition.unit}`));
    } else value.append(displayValue(current));
    if (reference && finiteNumber(current) && (current < reference.lower || current > reference.upper)) {
      value.append(element("span", "offscale-label", current < reference.lower ? "below scale" : "above scale"));
    }
    const compared = comparison(reference, current);
    if (compared) name.append(element("span", `comparison-chip ${compared.kind}`, compared.text));
    row.append(name, chart, value);
    if (!finiteNumber(current)) row.append(element("p", "metric-note", "Not enough reliable measurements for this phase."));
    return row;
  }));
  drawPlots();
}

// Safety rows are always open: a short definition under the name, the value on the
// right, and (for violations) one line per structure underneath.
function safetyRow(label, definition, value, state = "") {
  const row = element("div", "safety-metric");
  const head = element("div", "safety-head");
  const name = element("span", "safety-name", label);
  name.append(element("small", "", definition));
  head.append(name, element("span", `safety-value ${state}`, value));
  const content = element("div", "safety-details");
  row.append(head, content);
  return { row, content };
}

function renderSafety(phase) {
  const violations = phase.safety?.critical_structure_violation || {};
  const counted = violations.available === true && finiteNumber(violations.count);
  const violationValue = !counted ? "Unavailable" : violations.count === 0 ? "None"
    : `${displayValue(violations.count)}`;
  const violationRow = safetyRow("Critical structure violations",
    "Number of times the active drill cut into a critical structure.", violationValue,
    !counted ? "" : violations.count === 0 ? "good" : "warn");
  const structures = counted
    ? (violations.structures || []).filter(item => item && typeof item.name === "string") : [];
  if (structures.length) {
    const list = element("dl", "structure-list");
    structures.forEach(structure => {
      const entry = element("div");
      const count = finiteNumber(structure.count) ? `${displayValue(structure.count)} ${structure.count === 1 ? "time" : "times"}` : "count unavailable";
      const depth = finiteNumber(structure.max_intrusion_mm) ? ` · up to ${displayValue(structure.max_intrusion_mm)} mm deep` : "";
      entry.append(element("dt", "", structure.name), element("dd", "", `${count}${depth}`));
      list.append(entry);
    });
    violationRow.content.append(list);
  } else violationRow.content.remove();
  const visibility = phase.safety?.drill_visibility || {};
  const timed = visibility.available === true && finiteNumber(visibility.duration_s);
  const visibilityValue = !timed ? "Unavailable" : visibility.duration_s === 0 ? "None" : `${displayValue(visibility.duration_s)} s`;
  // Rule (under_ledge_ray.py): rays from the microscope to the bone the burr is cutting;
  // half or more blocked by not-yet-drilled bone, sustained >= 0.3 s.
  const visibilityRow = safetyRow("Time with poor drill visibility",
    "Time spent drilling with the center of the burr under a bony ledge.",
    visibilityValue, !timed ? "" : visibility.duration_s === 0 ? "good" : "warn");
  visibilityRow.content.remove();
  document.getElementById("safetyMetrics").replaceChildren(violationRow.row, visibilityRow.row);
}

function goToTime(time) {
  if (!summaryState.mediaAvailable) return;
  const video = document.getElementById("sessionVideo");
  video.pause();
  if (summaryState.seekHandler) video.removeEventListener("loadedmetadata", summaryState.seekHandler);
  const seek = () => {
    summaryState.seekHandler = null;
    video.currentTime = Number.isFinite(video.duration) ? Math.min(time, video.duration) : time;
    document.getElementById("videoStatus").textContent = `Paused at ${timeLabel(video.currentTime)}.`;
  };
  if (video.readyState >= 1) seek();
  else {
    summaryState.seekHandler = seek;
    video.addEventListener("loadedmetadata", seek, { once: true });
  }
}

// The resident must open Safety and Drilling motion in every phase before Next unlocks.
function reviewProgress() {
  const phases = summaryState.data.phases.map((phase, index) => ({
    label: phaseLabel(phase),
    done: REVIEW_GROUPS.every(group => summaryState.opened[index]?.has(group)),
  }));
  return { done: phases.filter(phase => phase.done).length, total: phases.length,
    remaining: phases.filter(phase => !phase.done).map(phase => phase.label) };
}

// In the study, pulse the one thing to open next (see .guide-pulse).
function updateGuide() {
  document.querySelectorAll(".guide-pulse").forEach(node => node.classList.remove("guide-pulse"));
  if (!embeddedSummary) return;
  const opened = summaryState.opened[summaryState.phaseIndex];
  const group = REVIEW_GROUPS.find(name => !opened?.has(name));
  let target = group ? document.querySelector(`.summary-group[data-group="${group}"] > summary`) : null;
  if (!target) {
    const next = [...document.querySelectorAll("[data-phase-index]")]
      .find(button => !REVIEW_GROUPS.every(name => summaryState.opened[Number(button.dataset.phaseIndex)]?.has(name)));
    target = next || null;
  }
  target?.classList.add("guide-pulse");
}

function reportProgress() {
  const progress = reviewProgress();
  document.querySelectorAll("[data-phase-index]").forEach(button => {
    const index = Number(button.dataset.phaseIndex);
    button.classList.toggle("reviewed", REVIEW_GROUPS.every(group => summaryState.opened[index]?.has(group)));
  });
  updateGuide();
  if (embeddedSummary && window.parent !== window) {
    window.parent.postMessage({ type: "summary-review-progress", ...progress }, window.location.origin);
  }
}

function groupOpened(event) {
  const group = event.currentTarget.dataset.group;
  if (event.currentTarget.open) {
    // One section at a time, so the open one fits on screen without scrolling.
    document.querySelectorAll(".summary-group[open]").forEach(other => {
      if (other !== event.currentTarget) other.open = false;
    });
    (summaryState.opened[summaryState.phaseIndex] ||= new Set()).add(group);
    event.currentTarget.classList.add("seen");
    if (group === "motion") drawPlots();
    reportProgress();
    reportCoverage(group);
  }
}

function reportCoverage(group = null) {
  if (embeddedSummary && window.parent !== window && summaryState.active) {
    window.parent.postMessage({ type: "summary-review-coverage",
      phase: selectedPhase().phase_name, group }, window.location.origin);
  }
}

function renderPhase() {
  const phase = selectedPhase();
  const intervals = validIntervals(phase);
  document.querySelectorAll("[data-phase-index]").forEach(button => {
    const selected = Number(button.dataset.phaseIndex) === summaryState.phaseIndex;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  document.getElementById("phaseMeasures").setAttribute("aria-labelledby", `summary-phase-${summaryState.phaseIndex}`);
  // "Early Drilling Phase", "Antrum Phase", "Incus Phase".
  document.getElementById("measuresPhase").textContent = `${phaseLabel(phase).replace(/\b\w/g, c => c.toUpperCase())} Phase`;
  document.getElementById("phaseDefinition").textContent = phaseDefinitions[phase.phase_name] || "";
  document.getElementById("videoStatus").textContent = intervals.length ? "" : `${phaseLabel(phase)} was not identified in the recording.`;
  // Each phase starts with both sections closed; ones already opened are marked.
  document.querySelectorAll(".summary-group").forEach(group => {
    group.open = false;
    group.classList.toggle("seen", Boolean(summaryState.opened[summaryState.phaseIndex]?.has(group.dataset.group)));
  });
  renderSafety(phase);
  renderMotion(phase);
  reportProgress();
}

// Choosing a phase shows its measures and pauses the video at the phase's start.
function selectPhase(index) {
  summaryState.phaseIndex = index;
  renderPhase();
  reportCoverage();
  const first = validIntervals(selectedPhase())[0];
  if (first) goToTime(first.t_start_s);
}

function renderTabs() {
  document.getElementById("phaseTabs").replaceChildren(...summaryState.data.phases.map((phase, index) => {
    const button = element("button", "", phaseLabel(phase));
    button.type = "button";
    button.id = `summary-phase-${index}`;
    button.dataset.phaseIndex = String(index);
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", "phaseMeasures");
    button.addEventListener("click", () => selectPhase(index));
    button.addEventListener("keydown", event => {
      const count = summaryState.data.phases.length;
      let target = null;
      if (event.key === "ArrowRight") target = (index + 1) % count;
      if (event.key === "ArrowLeft") target = (index - 1 + count) % count;
      if (event.key === "Home") target = 0;
      if (event.key === "End") target = count - 1;
      if (target != null) {
        event.preventDefault();
        selectPhase(target);
        document.getElementById(`summary-phase-${target}`).focus();
      }
    });
    return button;
  }));
}

function configureMedia() {
  const video = document.getElementById("sessionVideo");
  let mediaUrl = null;
  try {
    const url = new URL(summaryState.data.media?.url || "", window.location.href);
    if (summaryState.data.media?.available === true && url.origin === window.location.origin && url.pathname === "/api/media/video") mediaUrl = url.href;
  } catch { /* Unavailable media is represented explicitly below. */ }
  summaryState.mediaAvailable = mediaUrl != null;
  video.classList.toggle("hidden", !summaryState.mediaAvailable);
  document.getElementById("videoUnavailable").classList.toggle("hidden", summaryState.mediaAvailable);
  if (mediaUrl) video.src = mediaUrl;
  video.addEventListener("play", () => {
    if (!summaryState.active) video.pause();
  });
  video.addEventListener("error", () => {
    summaryState.mediaAvailable = false;
    video.classList.add("hidden");
    document.getElementById("videoUnavailable").classList.remove("hidden");
    document.getElementById("videoStatus").textContent = "The review video could not be loaded.";
    renderPhase();
  });
}

function configureSessions() {
  const select = document.getElementById("runSelect");
  const runId = summaryState.data.run?.run_id || "Current session";
  const runs = summaryState.data.available_runs?.length ? summaryState.data.available_runs : [{ run_id: runId }];
  select.replaceChildren(...runs.map(run => {
    const option = new Option(run.run_id, run.run_id, run.run_id === runId, run.run_id === runId);
    option.dataset.url = run.url || "";
    return option;
  }));
  select.disabled = runs.length < 2;
  select.addEventListener("change", () => {
    const target = select.selectedOptions[0]?.dataset.url;
    if (!target) return;
    try {
      const url = new URL(target, window.location.href);
      if (!["http:", "https:"].includes(url.protocol)) return;
      url.pathname = "/summary/";
      url.hash = "";
      window.location.assign(url.href);
    } catch { /* Leave the selected session unchanged for an invalid destination. */ }
  });
}

async function initializeSummary() {
  const status = document.getElementById("pageStatus");
  try {
    const url = new URL("/api/phase-summary", window.location.href);
    const participantId = summaryParameters.get("participant_id");
    if (participantId) url.searchParams.set("participant_id", participantId);
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    const data = await response.json();
    if (!response.ok) throw new Error("The session summary could not be loaded.");
    if (!Array.isArray(data.phases) || !data.phases.length) throw new Error("Phase measurements are unavailable for this recording.");
    summaryState.data = data;
    summaryState.phaseIndex = Math.max(0, data.phases.findIndex(phase => finiteNumber(phase.duration_s) && phase.duration_s > 0));
    configureSessions();
    configureMedia();
    renderTabs();
    document.querySelectorAll(".summary-group").forEach(group => group.addEventListener("toggle", groupOpened));
    status.classList.add("hidden");
    document.getElementById("summaryWorkspace").classList.remove("hidden");
    renderPhase();
    new ResizeObserver(drawPlots).observe(document.getElementById("motionMetrics"));
    if (embeddedSummary && window.parent !== window) {
      window.parent.postMessage({ type: "summary-review-ready" }, window.location.origin);
      reportCoverage();
    }
  } catch (error) {
    status.classList.add("error");
    status.textContent = error instanceof Error ? error.message : "The session summary could not be loaded.";
  }
}

initializeSummary();
