const state = {
  bootstrap: null, selectedId: null, stopAt: null,
  mediaAction: 0, metadataHandler: null, keyframeSeek: null,
};

const eventReview = EventReview.create({
  getItems: feedbackItems,
  getReview: () => state.bootstrap?.event_review,
  hasMedia: () => Boolean(state.bootstrap?.media?.available),
  onSelect(item, source) {
    beginMediaAction();
    document.getElementById("sessionVideo").pause();
    state.stopAt = null;
    state.selectedId = item.feedback_id;
    document.getElementById("videoHeading").textContent = "Observed event";
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
  return state.bootstrap?.event_review?.items || state.bootstrap?.coaching_plan?.items || [];
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

function beginMediaAction() {
  state.mediaAction += 1;
  const video = document.getElementById("sessionVideo");
  if (state.metadataHandler) video.removeEventListener("loadedmetadata", state.metadataHandler);
  state.metadataHandler = null;
  state.keyframeSeek = null;
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

function clearKeyframeSelection() {
  beginMediaAction();
  markClip(-1, "context");
  const video = document.getElementById("sessionVideo");
  document.getElementById("videoWindow").textContent = `Video · ${fmtTime(video.currentTime)}`;
}

function openReviewMoment(item, clip, index, mode) {
  const title = eventReview.titleFor(item);
  if (mode === "keyframe") jumpToKeyframe(clip, title, index);
  else playClip(clip, title, index);
}

function playClip(clip, title, index) {
  const video = document.getElementById("sessionVideo");
  if (!state.bootstrap.media?.available) return;
  const action = beginMediaAction();
  state.stopAt = null;
  video.pause();
  const bounds = EventReview.contextBounds(clip, state.bootstrap.run.duration_s);
  if (!bounds) return;
  document.getElementById("videoHeading").textContent = "Observed event";
  document.getElementById("videoWindow").textContent = `${fmtTime(bounds.start)}–${fmtTime(bounds.end)}`;
  markClip(index, "context");
  afterMetadata(action, () => {
    const duration = Number.isFinite(video.duration) ? Math.min(video.duration, bounds.end) : bounds.end;
    video.currentTime = Math.min(bounds.start, duration);
    state.stopAt = duration;
    video.play().catch(() => {});
  });
}

function jumpToKeyframe(clip, title, index) {
  const keyframe = keyframeFor(clip);
  if (!keyframe || !state.bootstrap.media?.available) return;
  const action = beginMediaAction();
  const video = document.getElementById("sessionVideo");
  video.pause();
  state.stopAt = null;
  document.getElementById("videoHeading").textContent = "Observed event";
  document.getElementById("videoWindow").textContent = `Paused · ${fmtTime(keyframe.time)}`;
  markClip(index, "keyframe");
  afterMetadata(action, () => {
    const time = Number.isFinite(video.duration) ? Math.min(keyframe.time, video.duration) : keyframe.time;
    video.pause();
    state.keyframeSeek = time;
    video.currentTime = time;
  });
}

async function init() {
  try {
    state.bootstrap = await api("/api/bootstrap");
    renderRunSelector();
    const video = document.getElementById("sessionVideo");
    video.addEventListener("play", clearKeyframeSelection);
    video.addEventListener("seeking", () => {
      if (state.keyframeSeek != null && Math.abs(video.currentTime - state.keyframeSeek) < .001) return;
      clearKeyframeSelection();
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
    eventReview.render();
  } catch (error) {
    document.querySelector("main").innerHTML = `<section class="feedback-detail empty-state"><h2>Could not load this session</h2><p>${escapeHtml(error.message)}</p></section>`;
  }
}

init();
