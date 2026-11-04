"use strict";

const summaryParameters = new URL(window.location.href).searchParams;
const embeddedSummary = summaryParameters.get("embedded") === "1";
const summaryState = { data: null, phaseIndex: 0, seekHandler: null, mediaAvailable: false, active: !embeddedSummary };
if (embeddedSummary) document.body.classList.add("embedded-summary");
window.addEventListener("message", event => {
  if (!embeddedSummary || event.origin !== window.location.origin || event.source !== window.parent
      || event.data?.type !== "study-review-visibility") return;
  summaryState.active = event.data.active === true;
  if (Number.isFinite(event.data.viewport_height) && event.data.viewport_height > 0) {
    document.documentElement.style.setProperty("--review-viewport-height", `${event.data.viewport_height}px`);
  }
  if (!summaryState.active) document.querySelectorAll("video, audio").forEach(media => media.pause());
});
const metricDefinitions = [
  { key: "stroke_length_mm", label: "Stroke length", unit: "mm" },
  { key: "tool_speed_mm_s", label: "Tool speed", unit: "mm/s" },
  { key: "removal_rate_mm3_s", label: "Removal rate", unit: "mm³/s" },
  { key: "stroke_frequency_hz", label: "Stroke frequency", unit: "strokes/s" },
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
  const labels = { early_surface: "Early drilling", early_phase: "Early drilling", antrum: "Antrum", incus: "Incus" };
  return phase.label || labels[phase.phase_name] || "Phase";
}

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

function drawDistribution(svg, metric, reference, value) {
  const width = svg.getBoundingClientRect().width;
  if (width < 25) return;
  svg.setAttribute("viewBox", `0 0 ${width} 45`);
  svg.replaceChildren();
  const left = 7, right = width - 7;
  const x = number => left + (number - reference.lower) / (reference.upper - reference.lower) * (right - left);
  const currentAvailable = finiteNumber(value);
  svg.append(svgElement("title", {}, `${metric.label}: ${currentAvailable ? `${displayValue(value)} ${metric.unit}` : "current value unavailable"}. Expert band ${displayValue(reference.q25)} to ${displayValue(reference.q75)} ${metric.unit}.`));
  svg.append(svgElement("line", { x1: left, x2: right, y1: 28, y2: 28, stroke: "#dbe5eb" }));
  svg.append(svgElement("rect", { x: x(reference.q25), y: 22, width: Math.max(1, x(reference.q75) - x(reference.q25)), height: 8, fill: "#dce6ee" }));
  svg.append(svgElement("line", { x1: x(reference.median), x2: x(reference.median), y1: 21, y2: 31, stroke: "#324c61", "stroke-width": 1.5 }));
  reference.points.forEach((point, index) => {
    const dot = svgElement("circle", { cx: x(point.value), cy: 14 - (index % 3) * 3, r: 1.9, fill: "#8496a6" });
    dot.append(svgElement("title", {}, `Expert recording: ${displayValue(point.value)} ${metric.unit}`));
    svg.append(dot);
  });
  if (currentAvailable) {
    const currentX = Math.max(left, Math.min(right, x(value)));
    svg.append(svgElement("line", { x1: currentX, x2: currentX, y1: 8, y2: 31, stroke: "#0a67a9", "stroke-width": 1.4 }));
    const shape = value > reference.upper ? `M ${right - 7} 3 L ${right} 8 L ${right - 7} 13 Z`
      : value < reference.lower ? `M ${left + 7} 3 L ${left} 8 L ${left + 7} 13 Z`
        : `M ${currentX} 1 l 4 4 l -4 4 l -4 -4 Z`;
    svg.append(svgElement("path", { d: shape, fill: "#0a67a9" }));
  }
  const ticks = width >= 185 ? [reference.lower, (reference.lower + reference.upper) / 2, reference.upper] : [reference.lower, reference.upper];
  ticks.forEach((value, index) => svg.append(svgElement("text", {
    x: x(value), y: 43, "font-size": 11, fill: "#607383",
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
    const value = element("div", `metric-value${finiteNumber(current) ? "" : " unavailable"}`, displayValue(current));
    if (reference && finiteNumber(current) && (current < reference.lower || current > reference.upper)) {
      value.append(element("span", "offscale-label", current < reference.lower ? "below scale" : "above scale"));
    }
    row.append(name, chart, value);
    if (!finiteNumber(current)) row.append(element("p", "metric-note", "Not enough reliable measurements for this phase."));
    return row;
  }));
  drawPlots();
}

function safetyRow(label, subtitle, value) {
  const details = element("details", "safety-metric");
  const summary = element("summary");
  const name = element("span", "safety-name", label);
  name.append(element("small", "", subtitle));
  summary.append(name, element("span", "safety-value", value));
  const content = element("div", "safety-details");
  details.append(summary, content);
  return { details, content };
}

function renderSafety(phase) {
  const violations = phase.safety?.critical_structure_violation || {};
  const violationValue = violations.available === true && finiteNumber(violations.count)
    ? `${displayValue(violations.count)} ${violations.count === 1 ? "event" : "events"}` : "Unavailable";
  const violationRow = safetyRow("Critical Structure Violation", "Details by structure", violationValue);
  if (violations.available === true) {
    const structures = (violations.structures || []).filter(item => item && typeof item.name === "string");
    if (structures.length) {
      const list = element("dl", "structure-list");
      structures.forEach(structure => {
        const entry = element("div");
        const count = finiteNumber(structure.count) ? `${displayValue(structure.count)} ${structure.count === 1 ? "event" : "events"}` : "Event count unavailable";
        const depth = finiteNumber(structure.max_intrusion_mm) ? `${displayValue(structure.max_intrusion_mm)} mm max. intrusion` : "Intrusion depth unavailable";
        entry.append(element("dt", "", structure.name), element("dd", "", `${count} · ${depth}`));
        list.append(entry);
      });
      violationRow.content.append(list);
      violationRow.content.append(element("p", "", "Max. intrusion is the greatest depth of bone removal inside the structure during this phase."));
    } else violationRow.content.append(element("p", "", violations.count === 0 ? "No critical structure violations were detected in this phase." : "Structure-level measurements are unavailable."));
  } else violationRow.content.append(element("p", "", "Critical structure measurements are unavailable for this phase."));
  violationRow.content.append(element("p", "", "An expert comparison is not available for this measurement."));
  const visibility = phase.safety?.drill_visibility || {};
  const visibilityValue = visibility.available === true && finiteNumber(visibility.duration_s) ? `${displayValue(visibility.duration_s)} s` : "Unavailable";
  const visibilityRow = safetyRow("Drill Visibility", "Poor visibility duration", visibilityValue);
  visibilityRow.content.append(element("p", "", visibility.available === true
    ? "Estimated total time with poor drill visibility during this phase."
    : "Drill visibility measurements are unavailable for this phase."));
  visibilityRow.content.append(element("p", "", "An expert comparison is not available for this measurement."));
  document.getElementById("safetyMetrics").replaceChildren(violationRow.details, visibilityRow.details);
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

function renderPhase() {
  const phase = selectedPhase();
  const intervals = validIntervals(phase);
  document.querySelectorAll("[data-phase-index]").forEach(button => {
    const selected = Number(button.dataset.phaseIndex) === summaryState.phaseIndex;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  document.getElementById("phaseMeasures").setAttribute("aria-labelledby", `summary-phase-${summaryState.phaseIndex}`);
  document.getElementById("videoWindow").textContent = `${phaseLabel(phase)}${finiteNumber(phase.duration_s) ? ` · ${timeLabel(phase.duration_s)} total` : ""}`;
  document.getElementById("phaseIntervals").replaceChildren(...intervals.map((interval, index) => {
    const row = element("div", "phase-interval");
    const text = element("span");
    text.append(element("strong", "", `${phaseLabel(phase)}${intervals.length > 1 ? ` · Part ${index + 1}` : ""}`), document.createElement("br"), document.createTextNode(`${timeLabel(interval.t_start_s)}–${timeLabel(interval.t_end_s)}`));
    const button = element("button", "", "Go to start");
    button.type = "button";
    button.disabled = !summaryState.mediaAvailable;
    button.setAttribute("aria-label", `Pause video at ${phaseLabel(phase)}, ${timeLabel(interval.t_start_s)}`);
    button.addEventListener("click", () => goToTime(interval.t_start_s));
    row.append(text, button);
    return row;
  }));
  if (!intervals.length) document.getElementById("phaseIntervals").append(element("p", "video-status", "This phase was not identified in the recording."));
  renderSafety(phase);
  renderMotion(phase);
}

function renderTabs() {
  document.getElementById("phaseTabs").replaceChildren(...summaryState.data.phases.map((phase, index) => {
    const button = element("button", "", phaseLabel(phase));
    button.type = "button";
    button.id = `summary-phase-${index}`;
    button.dataset.phaseIndex = String(index);
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", "phaseMeasures");
    button.addEventListener("click", () => { summaryState.phaseIndex = index; renderPhase(); });
    button.addEventListener("keydown", event => {
      const count = summaryState.data.phases.length;
      let target = null;
      if (event.key === "ArrowRight") target = (index + 1) % count;
      if (event.key === "ArrowLeft") target = (index - 1 + count) % count;
      if (event.key === "Home") target = 0;
      if (event.key === "End") target = count - 1;
      if (target != null) {
        event.preventDefault();
        summaryState.phaseIndex = target;
        renderPhase();
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
    status.classList.add("hidden");
    document.getElementById("summaryWorkspace").classList.remove("hidden");
    renderPhase();
    document.getElementById("referenceDescription").textContent = "Each expert contributes equally to the comparison. Recordings by the current participant are excluded when their identity is known.";
    new ResizeObserver(drawPlots).observe(document.getElementById("motionMetrics"));
  } catch (error) {
    status.classList.add("error");
    status.textContent = error instanceof Error ? error.message : "The session summary could not be loaded.";
  }
}

initializeSummary();
