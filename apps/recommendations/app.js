const state = {
  bootstrap: null, selectedId: null, stopAt: null,
  mediaAction: 0, metadataHandler: null, keyframeIntent: false, keyframeSeek: null,
};

const eventReview = EventReview.create({
  getItems: feedbackItems,
  getPhases: () => state.bootstrap?.phases || [],
  getDuration: () => state.bootstrap?.run?.duration_s || 0,
  hasMedia: () => Boolean(state.bootstrap?.media?.available),
  onSelect(item, source) {
    beginMediaAction();
    document.getElementById("sessionVideo").pause();
    state.stopAt = null;
    state.selectedId = item.feedback_id;
    document.getElementById("videoWindow").textContent = "Choose a review moment";
  },
  onOpen: openReviewMoment,
});

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

function keyframeFor(clip) {
  return EventReview.keyframeFor(clip);
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
  eventReview.mark(index, action);
}

function returnToVideo() {
  if (!state.keyframeIntent) return;
  beginMediaAction();
  markClip(-1, "context");
  const video = document.getElementById("sessionVideo");
  document.getElementById("videoWindow").textContent = `Video · ${fmtTime(video.currentTime)}`;
}

function openReviewMoment(item, clip, index, mode) {
  const title = eventReview.titleFor(item);
  if (mode === "context") { playClip(clip, title, index); return; }
  if (keyframeFor(clip)) { jumpToKeyframe(clip, title, index); return; }
  if (!state.bootstrap.media?.available || clip.t_start_s == null || !Number.isFinite(Number(clip.t_start_s))) return;
  const action = beginMediaAction();
  const video = document.getElementById("sessionVideo");
  video.pause();
  state.stopAt = null;
  const time = Math.max(0, Number(clip.t_start_s));
  document.getElementById("videoWindow").textContent = `Interval start · ${fmtTime(time)}`;
  markClip(index, "review");
  afterMetadata(action, () => {
    video.currentTime = Number.isFinite(video.duration) ? Math.min(time, video.duration) : time;
  });
}

function playClip(clip, title, index) {
  const video = document.getElementById("sessionVideo");
  if (!state.bootstrap.media?.available) return;
  const action = beginMediaAction();
  state.stopAt = null;
  video.pause();
  const start = Math.max(0, Number(clip.t_start_s) - 5);
  const stop = Math.min(Number(state.bootstrap.run.duration_s), Number(clip.t_end_s) + 5);
  document.getElementById("videoHeading").textContent = "Session video";
  document.getElementById("videoWindow").textContent = `${fmtTime(start)}–${fmtTime(stop)}`;
  markClip(index, "context");
  afterMetadata(action, () => {
    video.currentTime = start;
    state.stopAt = stop;
    video.play().catch(() => {});
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
  document.getElementById("videoHeading").textContent = "Session video";
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
}

async function init() {
  try {
    state.bootstrap = await api("/api/bootstrap");
    renderRunSelector();
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
        eventReview.setTime(video.currentTime);
        if (state.stopAt != null && video.currentTime >= state.stopAt) {
          video.pause(); state.stopAt = null;
        }
      });
    } else {
      video.classList.add("hidden");
      document.getElementById("videoUnavailable").classList.remove("hidden");
    }
    eventReview.render();
  } catch (error) {
    document.querySelector("main").innerHTML = `<section class="feedback-detail empty-state"><h2>Could not load this session</h2><p>${escapeHtml(error.message)}</p></section>`;
  }
}

init();
