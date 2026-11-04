const state = {
  participantId: null,
  start: null,
  experience: null,
  selectedId: null,
  stopAt: null,
  currentClipIndex: null,
  videoBound: false,
  mediaAction: 0,
  metadataHandler: null,
  keyframeIntent: false,
  keyframeSeek: null,
  firstReviewView: "event-based",
  reviewView: "event-based",
  summaryResizeObserver: null,
  summaryPendingTime: null,
  reviewFlow: null,
  reviewNextPending: false,
};

const eventReview = EventReview.create({
  getItems: feedbackItems,
  getPhases: () => state.experience?.phases || [],
  getDuration: () => state.experience?.session?.duration_s || 0,
  hasMedia: () => Boolean(state.experience?.media?.available),
  onSelect(item, source) {
    beginMediaAction();
    document.getElementById("sessionVideo").pause();
    state.stopAt = null;
    state.selectedId = item.feedback_id;
    document.getElementById("videoWindow").textContent = "Choose a review moment";
    state.currentClipIndex = null;
    if (state.reviewView === "event-based" && state.reviewFlow?.stage === "review") {
      logEvent("feedback_item_opened", { source, title: eventReview.titleFor(item) }, item.feedback_id);
    }
  },
  onOpen: openReviewMoment,
});

function reviewViewName(value) {
  return value === "summary" ? "summary" : "event-based";
}

function reviewViewLabel(value) {
  return value === "summary" ? "Summary" : "Event-based";
}

function setReviewFlow(flow) {
  if (flow?.schema !== "sequential_review_v1" || !Array.isArray(flow.order)
      || !["review", "questionnaire", "complete"].includes(flow.stage)) {
    throw new Error("The study progress could not be loaded. Reload the page to try again.");
  }
  state.reviewFlow = flow;
  state.start.review_flow = flow;
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function fmtTime(seconds) {
  const total = Math.max(0, Number(seconds || 0));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${(total - minutes * 60).toFixed(1).padStart(4, "0")}`;
}

function wait(milliseconds) {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds));
}

async function api(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Request failed");
  return value;
}

async function firstCaseUrlForAccess(participantId, firstReviewView) {
  const response = await fetch("/api/health");
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Unable to verify the study entry point");
  const firstCase = (value.available_runs || [])[0];
  if (!firstCase?.url || firstCase.run_id === value.run_id) return null;
  const firstUrl = new URL(firstCase.url, window.location.href);
  firstUrl.searchParams.set("participant_id", participantId);
  firstUrl.searchParams.set("first_review_view", reviewViewName(firstReviewView));
  return firstUrl.toString();
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 2600);
}

function logEvent(eventType, details = {}, feedbackId = state.selectedId) {
  if (!state.participantId) return;
  api("/api/expert-study/event", {
    participant_id: state.participantId,
    stimulus_id: feedbackId || null,
    event_type: eventType,
    details,
  }).catch(() => {});
}

function setHeaderStatus(text) {
  document.getElementById("workflowStatus").textContent = text;
}

function caseHeader() {
  const value = state.start.case;
  document.getElementById("caseLabel").textContent = `${value.alias} of ${value.total}`;
}

async function enterStudy(participantId, firstReviewView) {
  const value = await api("/api/expert-study/start", {
    participant_id: participantId,
    first_review_view: reviewViewName(firstReviewView),
  });
  state.participantId = value.participant_id;
  state.start = value;
  state.firstReviewView = reviewViewName(value.first_review_view);
  setReviewFlow(value.review_flow);
  state.reviewView = state.reviewFlow.current_view || state.firstReviewView;
  document.getElementById("interfaceVersion").textContent = String(
    value.interface_version || "pilot-ui-v1.4.1"
  ).replaceAll("-", " ");
  const currentUrl = new URL(window.location.href);
  currentUrl.searchParams.set("participant_id", state.participantId);
  currentUrl.searchParams.set("first_review_view", state.firstReviewView);
  window.history.replaceState({}, "", currentUrl);
  document.getElementById("accessPanel").classList.add("hidden");
  document.getElementById("headerStatus").classList.remove("hidden");
  caseHeader();
  if (state.reviewFlow.stage === "complete") {
    showCompletion();
    return;
  }
  if (value.analysis_completed) {
    setHeaderStatus("Restoring review");
    try {
      const experience = await api("/api/expert-study/analyze", { participant_id: state.participantId });
      prepareExperience(experience);
      return;
    } catch (error) {
      document.getElementById("accessPanel").classList.remove("hidden");
      throw error;
    }
  }
  setHeaderStatus("Session ready");
  document.getElementById("workflowDisclosure").textContent = value.workflow.disclosure;
  document.getElementById("readyCaseTitle").textContent = `${value.case.alias} is ready for post-session analysis`;
  document.getElementById("readyPanel").classList.remove("hidden");
}

function renderProcessingStages(stages) {
  document.getElementById("processingStages").innerHTML = stages.map((label, index) => `
    <div class="processing-stage" data-stage-index="${index}"><span>${index + 1}</span><strong>${escapeHtml(label)}</strong></div>
  `).join("");
}

async function animateProcessing(stages) {
  for (let index = 0; index < stages.length; index += 1) {
    const rows = document.querySelectorAll("[data-stage-index]");
    rows.forEach((row, rowIndex) => {
      row.classList.toggle("active", rowIndex === index);
      row.classList.toggle("done", rowIndex < index);
      row.querySelector("span").textContent = rowIndex < index ? "✓" : String(rowIndex + 1);
    });
    await wait(650);
  }
  document.querySelectorAll("[data-stage-index]").forEach(row => {
    row.classList.remove("active");
    row.classList.add("done");
    row.querySelector("span").textContent = "✓";
  });
  await wait(350);
}

async function startAnalysis() {
  const button = document.getElementById("startAnalysisButton");
  button.disabled = true;
  document.getElementById("readyPanel").classList.add("hidden");
  document.getElementById("processingPanel").classList.remove("hidden");
  setHeaderStatus("Analyzing recorded session");
  const stages = state.start.workflow.stages;
  renderProcessingStages(stages);
  try {
    const [experience] = await Promise.all([
      api("/api/expert-study/analyze", { participant_id: state.participantId }),
      animateProcessing(stages),
    ]);
    document.getElementById("processingPanel").classList.add("hidden");
    prepareExperience(experience);
  } catch (error) {
    document.getElementById("processingPanel").classList.add("hidden");
    document.getElementById("readyPanel").classList.remove("hidden");
    button.disabled = false;
    setHeaderStatus("Session ready");
    showToast(error.message);
  }
}

function feedbackItems() {
  return state.experience?.feedback || [];
}

function prepareExperience(experience) {
  state.experience = experience;
  setReviewFlow(experience.review_flow || state.reviewFlow);
  state.reviewView = state.reviewFlow.current_view || state.firstReviewView;
  renderExperience();
  renderReviewFlow();
}

function pauseReviewMedia() {
  state.stopAt = null;
  beginMediaAction();
  document.querySelectorAll("#eventBasedReview video, #eventBasedReview audio").forEach(media => media.pause());
  const frame = document.getElementById("summaryReviewFrame");
  if (frame.hasAttribute("src")) frame.contentWindow?.postMessage({ type: "study-review-visibility", active: false, viewport_height: window.innerHeight }, window.location.origin);
}

function resizeSummaryFrame() {
  const frame = document.getElementById("summaryReviewFrame");
  if (document.getElementById("summaryReview").classList.contains("hidden")) return;
  const content = frame.contentDocument?.querySelector("main");
  if (content) frame.style.height = `${Math.max(420, Math.ceil(content.getBoundingClientRect().height) + 2)}px`;
}

function loadSummaryFrame() {
  const frame = document.getElementById("summaryReviewFrame");
  if (frame.hasAttribute("src")) return;
  const url = new URL("/summary/", window.location.href);
  url.searchParams.set("embedded", "1");
  url.searchParams.set("participant_id", state.participantId);
  frame.src = url.toString();
}

function syncReviewVideo(video, time) {
  if (!video || !Number.isFinite(time)) return;
  const seek = () => {
    video.pause();
    video.currentTime = Number.isFinite(video.duration) ? Math.min(time, video.duration) : time;
  };
  if (video.readyState >= 1) seek();
  else video.addEventListener("loadedmetadata", seek, { once: true });
}

function selectReviewView(value, log = true) {
  const view = reviewViewName(value);
  const frame = document.getElementById("summaryReviewFrame");
  const sourceVideo = state.reviewView === "summary"
    ? frame.contentDocument?.getElementById("sessionVideo") : document.getElementById("sessionVideo");
  const currentTime = view !== state.reviewView && sourceVideo?.readyState >= 1 ? sourceVideo.currentTime : null;
  pauseReviewMedia();
  state.reviewView = view;
  document.getElementById("eventBasedReview").classList.toggle("hidden", view !== "event-based");
  document.getElementById("supportDetails").classList.toggle("hidden", view !== "event-based");
  document.getElementById("summaryReview").classList.toggle("hidden", view !== "summary");
  if (view === "summary") {
    if (Number.isFinite(currentTime)) state.summaryPendingTime = currentTime;
    loadSummaryFrame();
    frame.contentWindow?.postMessage({ type: "study-review-visibility", active: true, viewport_height: window.innerHeight }, window.location.origin);
    if (frame.contentDocument?.getElementById("sessionVideo")) {
      syncReviewVideo(frame.contentDocument.getElementById("sessionVideo"), state.summaryPendingTime);
      state.summaryPendingTime = null;
    }
    window.requestAnimationFrame(resizeSummaryFrame);
  } else syncReviewVideo(document.getElementById("sessionVideo"), currentTime);
  if (log) logEvent("review_view_opened", { view, review_index: state.reviewFlow.index }, null);
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
  if (!state.experience.media?.available || clip.t_start_s == null || !Number.isFinite(Number(clip.t_start_s))) return;
  const action = beginMediaAction();
  const video = document.getElementById("sessionVideo");
  video.pause();
  state.stopAt = null;
  state.currentClipIndex = index;
  const time = Math.max(0, Number(clip.t_start_s));
  document.getElementById("videoWindow").textContent = `Interval start · ${fmtTime(time)}`;
  markClip(index, "review");
  afterMetadata(action, () => {
    video.currentTime = Number.isFinite(video.duration) ? Math.min(time, video.duration) : time;
  });
  logEvent("feedback_interval_opened", { clip_index: index, interval_start_s: time });
}

function playClip(clip, title, index) {
  const video = document.getElementById("sessionVideo");
  if (!state.experience.media?.available) return;
  const action = beginMediaAction();
  state.currentClipIndex = index;
  state.stopAt = null;
  video.pause();
  const start = Math.max(0, Number(clip.t_start_s) - 5);
  const stop = Math.min(Number(state.experience.session.duration_s), Number(clip.t_end_s) + 5);
  document.getElementById("videoHeading").textContent = "Session video";
  document.getElementById("videoWindow").textContent = `${fmtTime(start)}–${fmtTime(stop)}`;
  markClip(index, "context");
  afterMetadata(action, () => {
    video.currentTime = start;
    state.stopAt = stop;
    video.play().catch(() => {});
  });
  logEvent("feedback_clip_played", { clip_index: index, playback_start_s: start, playback_end_s: stop });
}

function jumpToKeyframe(clip, title, index) {
  const keyframe = keyframeFor(clip);
  if (!keyframe) return;
  const action = beginMediaAction();
  const video = document.getElementById("sessionVideo");
  const image = document.getElementById("keyframeImage");
  const status = document.getElementById("keyframeStatus");
  video.pause();
  state.currentClipIndex = index;
  state.stopAt = null;
  state.keyframeIntent = true;
  document.getElementById("videoHeading").textContent = "Session video";
  document.getElementById("videoWindow").textContent = `Key frame · ${fmtTime(keyframe.time)}`;
  document.getElementById("keyframeCaption").textContent = `Paused · ${fmtTime(keyframe.time)}`;
  document.getElementById("returnToVideo").classList.toggle("hidden", !state.experience.media?.available);
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
    status.textContent = state.experience.media?.available
      ? "This key frame could not be loaded. You can still play the surrounding video."
      : "This key frame could not be loaded.";
    document.getElementById("videoWindow").textContent = `Review moment · ${fmtTime(keyframe.time)}`;
  };
  image.src = keyframe.url;
  if (state.experience.media?.available) afterMetadata(action, () => {
    const time = Number.isFinite(video.duration) ? Math.min(keyframe.time, video.duration) : keyframe.time;
    state.keyframeSeek = time;
    video.currentTime = time;
  });
  logEvent("feedback_signal_frame_opened", {
    clip_index: index,
    signal_time_s: keyframe.time,
    signal_frame_idx: clip.signal_frame_idx,
    anatomy: clip.anatomy,
  });
}

function bindVideo() {
  if (state.videoBound) return;
  state.videoBound = true;
  const video = document.getElementById("sessionVideo");
  video.addEventListener("play", () => {
    if (state.reviewView !== "event-based" || document.getElementById("experiencePanel").classList.contains("hidden")) {
      video.pause();
      return;
    }
    returnToVideo();
    logEvent("video_play", {
      current_time_s: Number(video.currentTime.toFixed(3)), clip_index: state.currentClipIndex,
    });
  });
  video.addEventListener("pause", () => logEvent("video_pause", {
    current_time_s: Number(video.currentTime.toFixed(3)), clip_index: state.currentClipIndex,
  }));
  document.getElementById("returnToVideo").addEventListener("click", returnToVideo);
  video.addEventListener("seeking", () => {
    if (state.keyframeSeek != null && Math.abs(video.currentTime - state.keyframeSeek) < .001) return;
    returnToVideo();
  });
  video.addEventListener("seeked", () => {
    state.keyframeSeek = null;
    logEvent("video_seek", {
      current_time_s: Number(video.currentTime.toFixed(3)), clip_index: state.currentClipIndex,
    });
  });
  video.addEventListener("timeupdate", () => {
    eventReview.setTime(video.currentTime);
    if (state.stopAt != null && video.currentTime >= state.stopAt) {
      state.stopAt = null;
      video.pause();
    }
  });
}

function renderExperience() {
  const video = document.getElementById("sessionVideo");
  if (state.experience.media?.available) {
    video.src = state.experience.media.url;
    video.classList.remove("hidden");
    document.getElementById("videoUnavailable").classList.add("hidden");
  } else {
    video.classList.add("hidden");
    document.getElementById("videoUnavailable").classList.remove("hidden");
  }
  bindVideo();
  eventReview.render();
}

function renderReviewFlow() {
  const flow = state.reviewFlow;
  pauseReviewMedia();
  document.getElementById("experiencePanel").classList.add("hidden");
  document.getElementById("questionnairePanel").classList.add("hidden");
  document.getElementById("readyPanel").classList.add("hidden");
  if (flow.stage === "complete") {
    showCompletion();
    return;
  }
  document.getElementById("reviewProgress").classList.remove("hidden");
  document.getElementById("reviewOrder").innerHTML = flow.order.map((view, index) =>
    `<li${index === flow.index ? ' aria-current="step"' : ""}><span>${index + 1}</span>${escapeHtml(reviewViewLabel(view))}</li>`
  ).join("");
  const label = reviewViewLabel(flow.current_view);
  const step = `${flow.index + 1} of ${flow.order.length}`;
  document.getElementById("reviewProgressLabel").textContent = `${flow.stage === "questionnaire" ? "Questionnaire" : "Review"} ${step} · ${label}`;
  if (flow.stage === "questionnaire") {
    document.getElementById("questionnaireStep").textContent = `Questionnaire ${step}`;
    document.getElementById("questionnaireTitle").textContent = `Questionnaire · ${label}`;
    document.getElementById("questionnairePanel").classList.remove("hidden");
    setHeaderStatus(`Questionnaire ${flow.index + 1} of ${flow.order.length}`);
  } else {
    document.getElementById("experiencePanel").classList.remove("hidden");
    setHeaderStatus(`Review ${step} · ${label}`);
    selectReviewView(flow.current_view);
  }
}

async function advanceReviewFlow() {
  if (state.reviewNextPending || !state.reviewFlow || state.reviewFlow.stage === "complete") return;
  const previous = state.reviewFlow;
  const error = document.getElementById(previous.stage === "questionnaire" ? "questionnaireError" : "reviewNextError");
  const buttons = [document.getElementById("finishExperienceButton"), document.getElementById("questionnaireNextButton")];
  state.reviewNextPending = true;
  buttons.forEach(button => { button.disabled = true; });
  error.classList.add("hidden");
  try {
    const result = await api("/api/expert-study/review-next", {
      participant_id: state.participantId,
      view: previous.current_view,
      stage: previous.stage,
    });
    setReviewFlow(result.review_flow);
    state.start.case_completed = result.case_completed;
    state.start.case.next_case_url = result.next_case_url;
    state.start.completion_code = result.completion_code;
    renderReviewFlow();
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove("hidden");
  } finally {
    state.reviewNextPending = false;
    buttons.forEach(button => { button.disabled = false; });
  }
}

function showCompletion() {
  pauseReviewMedia();
  ["accessPanel", "readyPanel", "processingPanel", "experiencePanel", "questionnairePanel", "reviewProgress"].forEach(id => {
    document.getElementById(id).classList.add("hidden");
  });
  document.getElementById("completionPanel").classList.remove("hidden");
  setHeaderStatus("Case complete");
  const nextButton = document.getElementById("nextCaseButton");
  const codeBlock = document.getElementById("completionCodeBlock");
  if (state.start.case.next_case_url) {
    document.getElementById("completionMessage").textContent = "Both review views are complete. Continue when you are ready to review the next recorded session.";
    nextButton.classList.remove("hidden");
    codeBlock.classList.add("hidden");
  } else {
    document.getElementById("completionTitle").textContent = "Expert workflow experience complete";
    document.getElementById("completionMessage").textContent = "All configured cases are complete. Please provide the completion code to the study team.";
    nextButton.classList.add("hidden");
    document.getElementById("completionCode").textContent = state.start.completion_code || "Complete";
    codeBlock.classList.remove("hidden");
  }
}

function goToNextCase() {
  const next = new URL(state.start.case.next_case_url, window.location.href);
  next.searchParams.set("participant_id", state.participantId);
  next.searchParams.set("first_review_view", state.firstReviewView);
  logEvent("next_case_opened", { next_case_index: state.start.case.index + 1 }, null);
  window.location.assign(next.toString());
}

document.getElementById("accessForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const error = document.getElementById("accessError");
  button.disabled = true;
  error.classList.add("hidden");
  try {
    const participantId = document.getElementById("participantId").value.trim();
    const firstReviewView = new FormData(event.currentTarget).get("first_review_view");
    const firstCaseUrl = await firstCaseUrlForAccess(participantId, firstReviewView);
    if (firstCaseUrl) {
      window.location.assign(firstCaseUrl);
      return;
    }
    await enterStudy(participantId, firstReviewView);
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove("hidden");
    button.disabled = false;
  }
});
document.getElementById("startAnalysisButton").addEventListener("click", startAnalysis);
document.getElementById("finishExperienceButton").addEventListener("click", advanceReviewFlow);
document.getElementById("questionnaireNextButton").addEventListener("click", advanceReviewFlow);
document.getElementById("nextCaseButton").addEventListener("click", goToNextCase);
document.getElementById("summaryReviewFrame").addEventListener("load", event => {
  state.summaryResizeObserver?.disconnect();
  const content = event.currentTarget.contentDocument?.querySelector("main");
  if (content) {
    state.summaryResizeObserver = new ResizeObserver(resizeSummaryFrame);
    state.summaryResizeObserver.observe(content);
  }
  const active = state.reviewView === "summary" && !document.getElementById("experiencePanel").classList.contains("hidden");
  event.currentTarget.contentWindow?.postMessage({ type: "study-review-visibility", active, viewport_height: window.innerHeight }, window.location.origin);
  syncReviewVideo(event.currentTarget.contentDocument?.getElementById("sessionVideo"), state.summaryPendingTime);
  state.summaryPendingTime = null;
  resizeSummaryFrame();
});
window.addEventListener("resize", () => {
  const frame = document.getElementById("summaryReviewFrame");
  const active = state.reviewView === "summary" && !document.getElementById("experiencePanel").classList.contains("hidden");
  if (frame.hasAttribute("src")) frame.contentWindow?.postMessage({ type: "study-review-visibility", active, viewport_height: window.innerHeight }, window.location.origin);
});
document.getElementById("supportDetails").addEventListener("toggle", event => {
  if (event.currentTarget.open) logEvent("support_details_opened");
});

const participantFromUrl = new URL(window.location.href).searchParams.get("participant_id");
const firstReviewFromUrl = reviewViewName(new URL(window.location.href).searchParams.get("first_review_view"));
document.querySelector(`input[name="first_review_view"][value="${firstReviewFromUrl}"]`).checked = true;
if (participantFromUrl) {
  document.getElementById("participantId").value = participantFromUrl;
  document.getElementById("studyReady").checked = true;
  enterStudy(participantFromUrl, firstReviewFromUrl).catch(err => {
    const error = document.getElementById("accessError");
    error.textContent = err.message;
    error.classList.remove("hidden");
  });
}
