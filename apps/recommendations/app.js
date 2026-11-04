const state = {
  bootstrap: null, selectedId: null, stopAt: null,
  mediaAction: 0, metadataHandler: null, keyframeIntent: false, keyframeSeek: null,
};

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function fmtTime(seconds) {
  const total = Math.max(0, Number(seconds || 0));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${(total - minutes * 60).toFixed(1).padStart(4, "0")}`;
}

async function api(path) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" } });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Request failed");
  return value;
}

function feedbackItems() {
  return state.bootstrap?.coaching_plan?.items || [];
}

function shortAction(item) {
  return item.recommended_action || item.message || "Review the linked interval before your next attempt.";
}

function renderRunSelector() {
  const select = document.getElementById("runSelect");
  const runs = state.bootstrap.available_runs?.length
    ? state.bootstrap.available_runs : [{ run_id: state.bootstrap.run.run_id, url: "" }];
  select.replaceChildren(...runs.map(run => {
    const selected = run.run_id === state.bootstrap.run.run_id;
    const option = new Option(run.run_id, run.run_id, selected, selected);
    option.dataset.url = run.url || "";
    return option;
  }));
  select.disabled = runs.length < 2;
  select.addEventListener("change", () => {
    const url = select.selectedOptions[0]?.dataset.url;
    if (url) window.location.assign(url);
  });
}

function renderProcedureMap() {
  const duration = Number(state.bootstrap.run.duration_s || 1);
  document.getElementById("durationLabel").textContent = `${fmtTime(duration)} total`;
  document.getElementById("timelineEnd").textContent = fmtTime(duration);
  document.getElementById("phaseMap").innerHTML = state.bootstrap.phases.map(phase => {
    const left = 100 * Number(phase.t_start_s) / duration;
    const width = 100 * (Number(phase.t_end_s) - Number(phase.t_start_s)) / duration;
    const name = String(phase.phase_name || "").replaceAll("_", " ");
    const rule = phase.phase_name === "pre_drilling";
    const predicted = !rule && phase.phase_source_kind === "model_phase_prediction";
    const source = rule ? "Predrilling rule" : predicted ? "Model-predicted phase" : "Manual phase annotation";
    const label = predicted ? `${name} · Predicted` : name;
    return `<div class="phase-segment ${escapeHtml(phase.phase_name)}" data-phase-id="${escapeHtml(phase.phase_id || "")}" style="left:${left}%;width:${width}%" title="${escapeHtml(name)} · ${escapeHtml(source)} · ${fmtTime(phase.t_start_s)}–${fmtTime(phase.t_end_s)}">${escapeHtml(label)}</div>`;
  }).join("");
  document.getElementById("coachingMap").innerHTML = feedbackItems().map(item => {
    const left = 100 * Number(item.t_start_s) / duration;
    return `<button class="map-marker ${escapeHtml(item.priority)}" style="left:${left}%" data-feedback-id="${escapeHtml(item.feedback_id)}" title="${escapeHtml(item.title)} · ${fmtTime(item.t_start_s)}"></button>`;
  }).join("");
  document.querySelectorAll("[data-feedback-id]").forEach(button => button.addEventListener("click", () => selectFeedback(button.dataset.feedbackId, true)));
}

function renderFocusList() {
  const root = document.getElementById("focusList");
  const items = feedbackItems();
  if (!items.length) {
    root.innerHTML = `<div class="empty-state"><p>No coaching priority was generated for this session.</p></div>`;
    return;
  }
  root.innerHTML = items.map((item, index) => `
    <button class="focus-button ${escapeHtml(item.priority)} ${item.feedback_id === state.selectedId ? "active" : ""}" data-focus-id="${escapeHtml(item.feedback_id)}">
      <span class="meta"><span>${index === 0 ? "Start here" : `Focus ${index + 1}`}</span><span>${escapeHtml(String(item.phase || "session").replaceAll("_", " "))}</span></span>
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.message || shortAction(item))}</p>
    </button>`).join("");
  root.querySelectorAll("[data-focus-id]").forEach(button => button.addEventListener("click", () => selectFeedback(button.dataset.focusId, false)));
}

function clipsFor(item) {
  const clips = item.review_clips || item.evidence_segments || [];
  return clips.length ? clips : [{ t_start_s: item.t_start_s, t_end_s: item.t_end_s }];
}

function clipPhaseLabel(clip, item) {
  const names = {
    early_phase: "Early drilling", early_surface: "Early drilling", pre_antrum: "Early drilling",
    antrum: "Antrum", incus: "Incus", post_incus: "Incus", pre_drilling: "Predrilling",
  };
  const phases = clip.phases?.length ? clip.phases : [clip.phase || clip.phase_name || item.phase];
  const labels = [...new Set(phases.filter(phase => phase && phase !== "session")
    .map(phase => names[phase] || String(phase).replaceAll("_", " ")))];
  return labels.length ? labels.join(" / ") : "Phase unavailable";
}

function keyframeFor(clip) {
  const time = clip.keyframe_time_s;
  if (clip.keyframe_available === false || !clip.keyframe_url || time == null
      || !Number.isFinite(Number(time)) || Number(time) < 0) return null;
  try {
    const url = new URL(clip.keyframe_url, window.location.href);
    if (url.origin !== window.location.origin || url.pathname !== "/api/media/keyframe") return null;
    return { url: url.href, time: Number(time) };
  } catch {
    return null;
  }
}

function clearKeyframe() {
  state.keyframeIntent = false;
  state.keyframeSeek = null;
  const image = document.getElementById("keyframeImage");
  image.onload = null;
  image.onerror = null;
  image.removeAttribute("src");
  document.getElementById("keyframeOverlay").classList.add("hidden");
  document.getElementById("keyframeStatus").classList.add("hidden");
  document.getElementById("sessionVideo").removeAttribute("aria-hidden");
  document.getElementById("sessionVideo").removeAttribute("tabindex");
}

function beginMediaAction() {
  state.mediaAction += 1;
  const video = document.getElementById("sessionVideo");
  if (state.metadataHandler) video.removeEventListener("loadedmetadata", state.metadataHandler);
  state.metadataHandler = null;
  clearKeyframe();
  return state.mediaAction;
}

function afterMetadata(action, callback) {
  const video = document.getElementById("sessionVideo");
  const ready = () => {
    if (action !== state.mediaAction) return;
    state.metadataHandler = null;
    callback();
  };
  if (video.readyState >= 1) ready();
  else {
    state.metadataHandler = ready;
    video.addEventListener("loadedmetadata", ready, { once: true });
  }
}

function markClip(index, action) {
  document.querySelectorAll("[data-clip-index], [data-keyframe-index]").forEach(button => {
    const active = action === "keyframe"
      ? button.dataset.keyframeIndex != null && Number(button.dataset.keyframeIndex) === index
      : button.dataset.clipIndex != null && Number(button.dataset.clipIndex) === index;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function revealVideo() {
  const card = document.getElementById("videoCard");
  card.scrollTop = 0;
  card.scrollIntoView({ behavior: "smooth", block: "start" });
}

function returnToVideo() {
  if (!state.keyframeIntent) return;
  beginMediaAction();
  markClip(-1, "context");
  const video = document.getElementById("sessionVideo");
  document.getElementById("videoWindow").textContent = `Video · ${fmtTime(video.currentTime)}`;
}

function renderEmptyFeedback() {
  state.selectedId = null;
  state.stopAt = null;
  beginMediaAction();
  const videoNote = state.bootstrap.media?.available
    ? "You can still review the full recording."
    : "A review video is not available for this session.";
  document.getElementById("feedbackDetail").innerHTML = `
    <div class="empty-state"><h2>No qualifying coaching priority</h2>
    <p>No coaching priority met the configured criteria for this session.</p>
    <p>${videoNote}</p></div>`;
  document.getElementById("clipList").replaceChildren();
  document.getElementById("videoHeading").textContent = "Review the full recording";
  document.getElementById("videoWindow").textContent = "Full session";
}

function renderDetail(item) {
  const mechanism = item.why_it_matters || "Review the linked interval to connect this observation with your next practice step.";
  document.getElementById("feedbackDetail").innerHTML = `
    <div class="detail-head">
      <div><p class="eyebrow">${escapeHtml(String(item.category || "coaching").replaceAll("_", " "))}</p><h2>${escapeHtml(item.title)}</h2><p>${escapeHtml(item.message || "")}</p></div>
      <span class="priority ${escapeHtml(item.priority)}">${escapeHtml(item.priority)} priority</span>
    </div>
    <div class="action-callout"><small>Try this next</small><p>${escapeHtml(shortAction(item))}</p></div>
    <div class="story-grid">
      <div class="story-block"><h3>What happened</h3><p>${escapeHtml(item.observed_issue || item.evidence_summary || "A time-linked coaching moment was identified.")}</p></div>
      <div class="story-block"><h3>Why it matters</h3><p>${escapeHtml(mechanism)}</p></div>
    </div>
    <button class="watch-button" id="watchSelected" ${state.bootstrap.media?.available ? "" : "disabled"}>Watch the linked moment</button>`;
  document.getElementById("watchSelected").addEventListener("click", () => playClip(clipsFor(item)[0], item.title, 0));
  renderClips(item);
}

function renderClips(item) {
  const clips = clipsFor(item);
  const root = document.getElementById("clipList");
  root.innerHTML = clips.map((clip, index) => {
    const keyframe = keyframeFor(clip);
    const timeLabel = keyframe ? `Key frame · ${fmtTime(keyframe.time)}`
      : `Review interval · ${fmtTime(clip.t_start_s)}–${fmtTime(clip.t_end_s)}`;
    const label = `${clipPhaseLabel(clip, item)} · ${timeLabel}`;
    return `<div class="clip-row">
      <div class="clip-copy"><strong>${clips.length > 1 ? `Review moment ${index + 1}` : "Review moment"}</strong><span>${escapeHtml(label)}</span></div>
      <div class="clip-actions">
        ${keyframe ? `<button type="button" data-keyframe-index="${index}" aria-pressed="false">Go to key frame</button>` : ""}
        <button type="button" data-clip-index="${index}" aria-pressed="false" ${state.bootstrap.media?.available ? "" : "disabled"}>Play context</button>
      </div>
    </div>`;
  }).join("");
  root.querySelectorAll("[data-clip-index]").forEach(button => button.addEventListener("click", () => {
    const index = Number(button.dataset.clipIndex);
    playClip(clips[index], item.title, index);
  }));
  root.querySelectorAll("[data-keyframe-index]").forEach(button => button.addEventListener("click", () => {
    const index = Number(button.dataset.keyframeIndex);
    jumpToKeyframe(clips[index], item.title, index);
  }));
}

function selectFeedback(feedbackId, play) {
  const item = feedbackItems().find(row => row.feedback_id === feedbackId);
  if (!item) return;
  beginMediaAction();
  document.getElementById("sessionVideo").pause();
  state.stopAt = null;
  state.selectedId = feedbackId;
  document.getElementById("videoHeading").textContent = item.title;
  document.getElementById("videoWindow").textContent = "Choose a review moment";
  renderFocusList();
  renderDetail(item);
  if (play) playClip(clipsFor(item)[0], item.title, 0);
}

function playClip(clip, title, index) {
  const video = document.getElementById("sessionVideo");
  if (!state.bootstrap.media?.available) return;
  const action = beginMediaAction();
  state.stopAt = null;
  video.pause();
  const start = Math.max(0, Number(clip.t_start_s) - 5);
  const stop = Math.min(Number(state.bootstrap.run.duration_s), Number(clip.t_end_s) + 5);
  document.getElementById("videoHeading").textContent = title;
  document.getElementById("videoWindow").textContent = `${fmtTime(start)}–${fmtTime(stop)}`;
  markClip(index, "context");
  afterMetadata(action, () => {
    video.currentTime = start;
    state.stopAt = stop;
    video.play().catch(() => {});
    revealVideo();
  });
}

function jumpToKeyframe(clip, title, index) {
  const keyframe = keyframeFor(clip);
  if (!keyframe) return;
  const action = beginMediaAction();
  const video = document.getElementById("sessionVideo");
  const image = document.getElementById("keyframeImage");
  const status = document.getElementById("keyframeStatus");
  video.pause();
  state.stopAt = null;
  state.keyframeIntent = true;
  document.getElementById("videoHeading").textContent = title;
  document.getElementById("videoWindow").textContent = `Key frame · ${fmtTime(keyframe.time)}`;
  document.getElementById("keyframeCaption").textContent = `Paused · ${fmtTime(keyframe.time)}`;
  document.getElementById("returnToVideo").classList.toggle("hidden", !state.bootstrap.media?.available);
  markClip(index, "keyframe");
  status.textContent = "Loading key frame…";
  status.classList.remove("hidden");
  image.alt = `${title} — key frame at ${fmtTime(keyframe.time)}`;
  image.onload = () => {
    if (action !== state.mediaAction) return;
    document.getElementById("keyframeOverlay").classList.remove("hidden");
    status.classList.add("hidden");
    video.setAttribute("aria-hidden", "true");
    video.setAttribute("tabindex", "-1");
  };
  image.onerror = () => {
    if (action !== state.mediaAction) return;
    status.textContent = state.bootstrap.media?.available
      ? "This key frame could not be loaded. You can still play the surrounding video."
      : "This key frame could not be loaded.";
    document.getElementById("videoWindow").textContent = `Review moment · ${fmtTime(keyframe.time)}`;
  };
  image.src = keyframe.url;
  if (state.bootstrap.media?.available) afterMetadata(action, () => {
    const time = Number.isFinite(video.duration) ? Math.min(keyframe.time, video.duration) : keyframe.time;
    state.keyframeSeek = time;
    video.currentTime = time;
  });
  revealVideo();
}

async function init() {
  try {
    state.bootstrap = await api("/api/bootstrap");
    renderRunSelector();
    renderProcedureMap();
    const video = document.getElementById("sessionVideo");
    document.getElementById("returnToVideo").addEventListener("click", returnToVideo);
    video.addEventListener("play", returnToVideo);
    video.addEventListener("seeking", () => {
      if (state.keyframeSeek != null && Math.abs(video.currentTime - state.keyframeSeek) < .001) return;
      returnToVideo();
    });
    video.addEventListener("seeked", () => { state.keyframeSeek = null; });
    if (state.bootstrap.media?.available) {
      video.src = state.bootstrap.media.url;
      video.addEventListener("timeupdate", () => {
        if (state.stopAt != null && video.currentTime >= state.stopAt) {
          video.pause(); state.stopAt = null;
        }
      });
    } else {
      video.classList.add("hidden");
      document.getElementById("videoUnavailable").classList.remove("hidden");
    }
    const first = feedbackItems()[0];
    if (first) {
      selectFeedback(first.feedback_id, false);
    } else {
      renderFocusList();
      renderEmptyFeedback();
    }
  } catch (error) {
    document.querySelector("main").innerHTML = `<section class="feedback-detail empty-state"><h2>Could not load this session</h2><p>${escapeHtml(error.message)}</p></section>`;
  }
}

init();
