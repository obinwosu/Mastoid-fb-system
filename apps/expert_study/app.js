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
  keyframeSeek: null,
  firstReviewView: "event-based",
  reviewView: "event-based",
  summaryResizeObserver: null,
  summaryPendingTime: null,
  reviewFlow: null,
  reviewNextPending: false,
  reviewTimer: null,
  timingStep: null,
  summaryReady: false,
  timingCoverage: new Set(),
};

const eventReview = EventReview.create({
  getItems: feedbackItems,
  getReview: () => state.experience?.event_review,
  hasMedia: () => Boolean(state.experience?.media?.available),
  onSelect(item, source) {
    beginMediaAction();
    document.getElementById("sessionVideo").pause();
    state.stopAt = null;
    state.selectedId = item.feedback_id;
    document.getElementById("videoHeading").textContent = "Event Video Clip";
    document.getElementById("videoWindow").textContent = "";
    state.currentClipIndex = null;
    if (state.reviewView === "event-based" && state.reviewFlow?.stage === "review") {
      logEvent("feedback_item_opened", { source, title: eventReview.titleFor(item) }, item.feedback_id);
      if (source === "feedback_list") recordReviewCoverage(`category:${item.metric_key || item.feedback_id}`);
    }
  },
  onOpen: openReviewMoment,
  isReviewed: (item, index) => state.reviewedMoments?.has(EventReview.reviewKey(item, index)) || false,
  guide: true,
});

const studySurveys = StudySurveys.create({
  root: document.getElementById("surveyPanel"),
  onSubmit: answers => advanceReviewFlow(answers),
  onMediaEvent: (type, details) => logEvent(type, details, null),
});

function reviewViewName(value) {
  return value === "summary" ? "summary" : "event-based";
}

function reviewViewLabel(value) {
  return value === "summary" ? "Summary" : "Event-based";
}

function setReviewFlow(flow) {
  const modern = flow?.schema === "sequential_review_v2";
  const stages = modern ? ["background", "baseline", "self_assessment", "review", "questionnaire", "preference", "closing", "complete"]
    : ["review", "questionnaire", "complete"];
  if ((!modern && flow?.schema !== "sequential_review_v1") || !Array.isArray(flow.order)
      || !stages.includes(flow.stage) || (modern && !flow.step_id)) {
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

function recordReviewCoverage(key) {
  if (state.reviewFlow?.stage !== "review" || state.reviewNextPending) return;
  state.timingCoverage.add(key);
  state.reviewTimer?.cover(key);
}

function initializeReviewTiming() {
  state.reviewTimer?.destroy();
  state.timingStep = null;
  state.timingCoverage.clear();
  const participantId = state.participantId;
  state.reviewTimer = ReviewTiming.create({
    participantId,
    runId: state.start.run_id,
    async send(snapshot, options = {}) {
      const response = await fetch("/api/expert-study/review-timing", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ participant_id: participantId, snapshot }),
        keepalive: options.keepalive === true,
        signal: AbortSignal.timeout(10000),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Review time could not be saved. Please try Next again.");
      return result;
    },
  });
}

// Both formats start after rendered feedback and a usable first video frame (or
// explicit media unavailability). Summary's iframe load precedes its data fetch.
function startReviewTiming() {
  const flow = state.reviewFlow;
  if (!state.reviewTimer || flow?.stage !== "review" || state.reviewNextPending
      || document.getElementById("experiencePanel").classList.contains("hidden")) return;
  const stepId = flow.step_id || `review_${flow.index}`;
  if (state.timingStep === stepId) return;
  const summary = flow.current_view === "summary";
  if (summary && !state.summaryReady) return;
  const doc = summary ? document.getElementById("summaryReviewFrame").contentDocument : document;
  const video = doc?.getElementById("sessionVideo");
  const unavailable = !video || video.classList.contains("hidden") || Boolean(video.error);
  if (!unavailable && video.readyState < 2) {
    if (!video.dataset.timingReadyBound) {
      video.dataset.timingReadyBound = "true";
      ["loadeddata", "canplay", "error"].forEach(type => video.addEventListener(type, startReviewTiming));
    }
    return;
  }
  state.reviewTimer.start({ stepId, view: flow.current_view, reviewIndex: flow.index, video });
  state.timingStep = stepId;
  state.timingCoverage.forEach(key => state.reviewTimer.cover(key));
}

window.addEventListener("message", event => {
  const frame = document.getElementById("summaryReviewFrame");
  if (event.origin !== window.location.origin || event.source !== frame?.contentWindow) return;
  if (event.data?.type === "summary-review-ready") {
    state.summaryReady = true;
    window.requestAnimationFrame(startReviewTiming);
  } else if (event.data?.type === "summary-review-coverage" && state.reviewView === "summary") {
    const { phase, group } = event.data;
    if (typeof phase !== "string" || phase.length > 100) return;
    if (group == null) recordReviewCoverage(`phase:${phase}`);
    else if (["safety", "motion"].includes(group)) recordReviewCoverage(`section:${phase}:${group}`);
  }
});

const REVIEW_DESCRIPTIONS = {
  summary: {
    title: "Summary-based feedback",
    text: "An overview of your performance in each phase of the procedure compared with expert surgeons.",
    instruction: [
      "Click each phase button (Early Drilling, Antrum, Incus) and review your Safety and Drilling Motion Metrics.",
      "Once you have reviewed metrics across all phases, select Next.",
    ],
  },
  "event-based": {
    title: "Event-based feedback",
    text: "Selected moments from your session, with matching expert comparisons for motion.",
    instruction: [
      "Click each metric below to open its list of events.",
      "Once you have reviewed all events, select Next.",
    ],
  },
};

// Summary-based feedback: Next stays locked until the summary page reports that
// Safety and Drilling motion were opened in every phase.
state.summaryProgress = null;
window.addEventListener("message", event => {
  if (event.origin !== window.location.origin || event.data?.type !== "summary-review-progress") return;
  const frame = document.getElementById("summaryReviewFrame");
  if (event.source !== frame?.contentWindow) return;
  state.summaryProgress = { done: event.data.done, total: event.data.total, remaining: event.data.remaining || [] };
  updateReviewGate();
});

function summaryGateOpen() {
  const progress = state.summaryProgress;
  return Boolean(progress && progress.total > 0 && progress.done >= progress.total);
}

// Event-based feedback: Next stays locked until every moment of every topic was opened
// (key frame, go to moment, or play context).
state.reviewedMoments = new Set();

function eventProgress() {
  const ordered = [...feedbackItems()].sort((a, b) => EventReview.describe(a).order - EventReview.describe(b).order);
  const topics = ordered.map(item => {
    const total = eventReview.clipsFor(item).length;
    let done = 0;
    for (let i = 0; i < total; i += 1) if (state.reviewedMoments.has(EventReview.reviewKey(item, i))) done += 1;
    const title = `${item.phase && item.phase !== "session" ? `${EventReview.phaseLabel({}, item)} · ` : ""}${eventReview.titleFor(item)}`;
    return { title, total, done };
  });
  return {
    done: topics.reduce((sum, t) => sum + t.done, 0),
    total: topics.reduce((sum, t) => sum + t.total, 0),
    remaining: topics.filter(t => t.done < t.total).map(t => `${t.title} (${t.total - t.done})`),
  };
}

function updateReviewGate() {
  window.requestAnimationFrame(placeNextCard);
  window.requestAnimationFrame(updateNextGuide);
  const button = document.getElementById("finishExperienceButton");
  const hint = document.getElementById("reviewNextHint");
  if (state.reviewFlow?.stage === "review" && state.reviewFlow.revisit) {
    // Back from the questionnaire: this feedback was already fully reviewed.
    hint.textContent = "Select Next to return to the questionnaire.";
    if (!state.reviewNextPending) button.disabled = false;
    document.getElementById("nextCard").classList.remove("locked");
    return;
  }
  if (state.reviewFlow?.stage === "review" && state.reviewView === "event-based" && state.experience) {
    const progress = eventProgress();
    const open = progress.done >= progress.total;
    hint.textContent = open ? (progress.total ? "You have reviewed every moment. Select Next when you are ready." : "No moments require review. Select Next when you are ready.")
      : `Review every moment to continue (${progress.done} of ${progress.total} done). Still to review: ${progress.remaining.join(", ")}.`;
    if (!state.reviewNextPending) button.disabled = !open;
    document.getElementById("nextCard").classList.toggle("locked", !open);
    return;
  }
  const gated = state.reviewFlow?.stage === "review" && state.reviewView === "summary";
  if (!gated) {
    hint.textContent = "When you have finished reviewing this view, select Next.";
    if (!state.reviewNextPending) button.disabled = false;
    document.getElementById("nextCard").classList.remove("locked");
    return;
  }
  const progress = state.summaryProgress;
  if (summaryGateOpen()) hint.textContent = "You have reviewed every phase. Select Next when you are ready.";
  else hint.textContent = progress
    ? `Review every phase to continue (${progress.done} of ${progress.total} done). Still to review: ${progress.remaining.join(", ")}.`
    : "Review every phase to continue.";
  if (!state.reviewNextPending) button.disabled = !summaryGateOpen();
  document.getElementById("nextCard").classList.toggle("locked", !summaryGateOpen());
}

// Once a feedback page is fully reviewed, the guide pulse moves to Next.
function updateNextGuide() {
  const button = document.getElementById("finishExperienceButton");
  const review = state.reviewFlow?.stage === "review";
  button.classList.toggle("guide-pulse", review && !button.disabled);
}

function setHeaderStatus(text) {
  document.getElementById("workflowStatus").textContent = text;
}

function caseHeader() {
  const value = state.start.case;
  document.getElementById("caseLabel").textContent = `${value.alias} of ${value.total}`;
  // One recording per resident: "Case 01 of 1" only adds noise.
  const single = Number(value.total) <= 1;
  document.getElementById("caseLabel").classList.toggle("hidden", single);
  document.querySelector("#headerStatus .divider").classList.toggle("hidden", single);
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
  initializeReviewTiming();
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
  document.getElementById("legacySurveyNotice").classList.toggle("hidden", !value.legacy_survey_flow);
  caseHeader();
  if (state.reviewFlow.stage === "complete") {
    showCompletion();
    return;
  }
  if (state.reviewFlow.stage === "background") {
    renderReviewFlow();
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
  showReady();
}

// The launcher has already analysed the recording, so go straight to the feedback.
// The pilot's start screen and progress animation only appear if loading fails.
async function showReady() {
  studySurveys.hide();
  document.getElementById("reviewProgress").classList.add("hidden");
  setHeaderStatus("Loading your feedback");
  try {
    const experience = await api("/api/expert-study/analyze", { participant_id: state.participantId });
    if (experience.pending) {
      waitForFeedback();
      return;
    }
    document.getElementById("preparingPanel").classList.add("hidden");
    prepareExperience(experience);
  } catch (error) {
    showReadyPanel();
    showToast(error.message);
  }
}

// The launcher serves only the background questionnaire while the session is
// analysed, then restarts the server here with the feedback. Reload once it has.
function waitForFeedback() {
  studySurveys.hide();
  document.getElementById("preparingPanel").classList.remove("hidden");
  setHeaderStatus("Preparing your feedback");
  const check = async () => {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      const value = await response.json();
      if (response.ok && value.ok && !value.intake) {
        window.location.reload();
        return;
      }
    } catch {
      // The server is restarting with the feedback.
    }
    window.setTimeout(check, 2000);
  };
  window.setTimeout(check, 2000);
}

function showReadyPanel() {
  studySurveys.hide();
  document.getElementById("reviewProgress").classList.add("hidden");
  setHeaderStatus("Session ready");
  document.getElementById("workflowDisclosure").textContent = state.start.workflow.disclosure;
  document.getElementById("readyCaseTitle").textContent = `${state.start.case.alias} is ready for post-session analysis`;
  document.getElementById("startAnalysisButton").disabled = false;
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
  return state.experience?.event_review?.items || state.experience?.feedback || [];
}

function prepareExperience(experience) {
  state.experience = experience;
  state.start.analysis_completed = true;
  if (experience.surveys) state.start.surveys = experience.surveys;
  setReviewFlow(experience.review_flow || state.reviewFlow);
  state.reviewView = state.reviewFlow.current_view || state.firstReviewView;
  renderExperience();
  renderReviewFlow();
}

function pauseReviewMedia() {
  studySurveys.pause();
  state.stopAt = null;
  beginMediaAction();
  document.querySelectorAll("#eventBasedReview video, #eventBasedReview audio").forEach(media => media.pause());
  const frame = document.getElementById("summaryReviewFrame");
  if (frame.hasAttribute("src")) frame.contentWindow?.postMessage({ type: "study-review-visibility", active: false, viewport_height: window.innerHeight }, window.location.origin);
}

// The Next box sits in the right column directly under the session video, on both
// review pages, so the whole review fits on one screen. Stacked (narrow) layouts keep
// it at the bottom.
function placeNextCard() {
  const panel = document.getElementById("experiencePanel");
  const card = document.getElementById("nextCard");
  card.classList.remove("beside-video");
  panel.style.minHeight = "";
  if (panel.classList.contains("hidden") || window.innerWidth <= 820) return;
  let video = null, offsetTop = 0, offsetLeft = 0;
  if (state.reviewView === "summary") {
    const frame = document.getElementById("summaryReviewFrame");
    video = frame.contentDocument?.querySelector(".video-card");
    const box = frame.getBoundingClientRect();
    offsetTop = box.top; offsetLeft = box.left;
  } else video = document.querySelector("#eventBasedReview .video-card");
  if (!video) return;
  const rect = video.getBoundingClientRect(), base = panel.getBoundingClientRect();
  if (rect.width < 50) return;
  card.classList.add("beside-video");
  card.style.top = `${offsetTop + rect.bottom - base.top + 16}px`;
  card.style.left = `${offsetLeft + rect.left - base.left}px`;
  card.style.width = `${rect.width}px`;
  const bottom = card.offsetTop + card.offsetHeight;
  panel.style.minHeight = `${bottom}px`;
}
window.addEventListener("resize", () => window.requestAnimationFrame(placeNextCard));
// Video cards change height when the video loads; keep the Next box under them.
const nextCardObserver = new ResizeObserver(() => window.requestAnimationFrame(placeNextCard));
nextCardObserver.observe(document.querySelector("#eventBasedReview .video-card"));
document.getElementById("summaryReviewFrame").addEventListener("load", event => {
  const card = event.currentTarget.contentDocument?.querySelector(".video-card");
  if (card) nextCardObserver.observe(card);
});

function resizeSummaryFrame() {
  const frame = document.getElementById("summaryReviewFrame");
  if (document.getElementById("summaryReview").classList.contains("hidden")) return;
  const content = frame.contentDocument?.querySelector("main");
  if (content) frame.style.height = `${Math.max(420, Math.ceil(content.getBoundingClientRect().height) + 2)}px`;
  placeNextCard();
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
  window.requestAnimationFrame(placeNextCard);
  if (log) logEvent("review_view_opened", { view, review_index: state.reviewFlow.index }, null);
  window.requestAnimationFrame(startReviewTiming);
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
  if (!state.experience.media?.available || (mode === "keyframe" && !keyframeFor(clip))) return;
  state.reviewedMoments.add(EventReview.reviewKey(item, index));
  const coverage = [`moment:${EventReview.reviewKey(item, index)}`,
    `category:${item.metric_key || item.feedback_id}`,
    `phase:${clip.phase || clip.phase_name || item.phase || "unknown"}`];
  coverage.forEach(key => {
    if (state.reviewFlow?.stage === "review" && !state.reviewNextPending) {
      state.timingCoverage?.add(key);
      state.reviewTimer?.cover(key);
    }
  });
  eventReview.refreshViewed();
  updateReviewGate();
  const title = eventReview.titleFor(item);
  if (mode === "keyframe") jumpToKeyframe(clip, title, index);
  else playClip(clip, title, index);
}

function playClip(clip, title, index) {
  const video = document.getElementById("sessionVideo");
  if (!state.experience.media?.available) return;
  const action = beginMediaAction();
  state.currentClipIndex = index;
  state.stopAt = null;
  video.pause();
  const bounds = EventReview.contextBounds(clip, state.experience.session.duration_s);
  if (!bounds) return;
  document.getElementById("videoHeading").textContent = "Event Video Clip";
  document.getElementById("videoWindow").textContent = `${fmtTime(bounds.start)}–${fmtTime(bounds.end)}`;
  markClip(index, "context");
  afterMetadata(action, () => {
    const duration = Number.isFinite(video.duration) ? Math.min(video.duration, bounds.end) : bounds.end;
    video.currentTime = Math.min(bounds.start, duration);
    state.stopAt = duration;
    video.play().catch(() => {});
  });
  logEvent("feedback_clip_played", { clip_index: index, clip_id: clip.clip_id, playback_start_s: bounds.start, playback_end_s: bounds.end });
}

function jumpToKeyframe(clip, title, index) {
  const keyframe = keyframeFor(clip);
  if (!keyframe || !state.experience.media?.available) return;
  const action = beginMediaAction();
  const video = document.getElementById("sessionVideo");
  video.pause();
  state.currentClipIndex = index;
  state.stopAt = null;
  document.getElementById("videoHeading").textContent = "Event Video Clip";
  document.getElementById("videoWindow").textContent = `Paused · ${fmtTime(keyframe.time)}`;
  markClip(index, "keyframe");
  afterMetadata(action, () => {
    const time = Number.isFinite(video.duration) ? Math.min(keyframe.time, video.duration) : keyframe.time;
    video.pause();
    state.keyframeSeek = time;
    video.currentTime = time;
  });
  logEvent("feedback_signal_frame_opened", {
    clip_index: index,
    signal_time_s: keyframe.time,
    signal_frame_idx: clip.signal_frame_idx,
    keyframe_frame_idx_global: clip.keyframe_frame_idx_global,
    anatomy: clip.anatomy,
    presentation: "video_seek_and_pause",
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
    clearKeyframeSelection();
    logEvent("video_play", {
      current_time_s: Number(video.currentTime.toFixed(3)), clip_index: state.currentClipIndex,
    });
  });
  video.addEventListener("pause", () => logEvent("video_pause", {
    current_time_s: Number(video.currentTime.toFixed(3)), clip_index: state.currentClipIndex,
  }));
  video.addEventListener("seeking", () => {
    if (state.keyframeSeek != null && Math.abs(video.currentTime - state.keyframeSeek) < .001) return;
    clearKeyframeSelection();
  });
  video.addEventListener("seeked", () => {
    state.keyframeSeek = null;
    logEvent("video_seek", {
      current_time_s: Number(video.currentTime.toFixed(3)), clip_index: state.currentClipIndex,
    });
  });
  video.addEventListener("timeupdate", () => {
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
  const modern = flow.schema === "sequential_review_v2";
  document.getElementById("backBar").classList.toggle("hidden", !(modern && flow.can_go_back));
  document.getElementById("backError").classList.add("hidden");
  pauseReviewMedia();
  studySurveys.hide();
  document.getElementById("experiencePanel").classList.add("hidden");
  document.getElementById("questionnairePanel").classList.add("hidden");
  document.getElementById("readyPanel").classList.add("hidden");
  document.getElementById("preparingPanel").classList.add("hidden");
  document.getElementById("completionPanel").classList.add("hidden");
  if (flow.stage === "complete") {
    showCompletion();
    return;
  }
  if (modern && flow.stage !== "background" && !state.experience) {
    showReady();
    return;
  }
  document.getElementById("reviewProgress").classList.remove("hidden");
  document.getElementById("reviewOrder").innerHTML = flow.order.map((view, index) =>
    `<li${index === flow.index ? ' aria-current="step"' : ""}><span>${index + 1}</span>${escapeHtml(reviewViewLabel(view))}</li>`
  ).join("");
  const label = reviewViewLabel(flow.current_view);
  const step = `${flow.index + 1} of ${flow.order.length}`;
  document.getElementById("reviewProgressLabel").textContent = `${flow.stage === "questionnaire" ? "Questionnaire" : "Review"} ${step} · ${label}`;
  // A title and plain description replace the numbered view pills: the resident
  // has not met the other view yet, so naming it here only confuses.
  const about = REVIEW_DESCRIPTIONS[flow.current_view] || {};
  document.getElementById("reviewTitle").textContent = about.title || label;
  document.getElementById("reviewDescription").textContent = about.text || "";
  // A "How to Review this Page" heading, then one line per instruction sentence.
  const instruction = document.getElementById("reviewInstruction");
  instruction.replaceChildren();
  if (flow.stage === "review" && about.instruction) {
    const heading = document.createElement("span");
    heading.className = "review-instruction-heading";
    heading.textContent = "How to Review this Page";
    instruction.append(heading);
    for (const line of about.instruction) {
      const row = document.createElement("span");
      row.className = "review-instruction-line";
      row.textContent = line;
      instruction.append(row);
    }
  }
  if (modern && ["background", "baseline", "self_assessment", "questionnaire", "preference", "closing"].includes(flow.stage)) {
    document.getElementById("reviewProgress").classList.add("hidden");
    const status = flow.stage === "background" ? "Background Questionnaire"
      : flow.stage === "baseline" ? "Initial Self-Assessment"
        : flow.stage === "self_assessment" ? `Self-assessment ${flow.assessment_round + 1} of 3`
        : flow.stage === "preference" ? "Feedback preference"
        : flow.stage === "closing" ? "Post Sim Reflection" : `Questionnaire ${step} · ${label}`;
    setHeaderStatus(status);
    document.getElementById("reviewProgressLabel").textContent = flow.stage === "self_assessment"
      ? `Self-assessment · After ${label.toLowerCase()} feedback` : status;
    studySurveys.show({
      participantId: state.participantId,
      runId: state.start.run_id,
      flow,
      definitions: state.start.surveys,
      media: state.experience?.media,
    });
    return;
  }
  if (flow.stage === "questionnaire") {
    document.getElementById("questionnaireStep").textContent = `Questionnaire ${step}`;
    document.getElementById("questionnaireTitle").textContent = `Questionnaire · ${label}`;
    document.getElementById("questionnairePanel").classList.remove("hidden");
    setHeaderStatus(`Questionnaire ${flow.index + 1} of ${flow.order.length}`);
  } else {
    const nextStage = modern ? flow.next_stage : "questionnaire";
    document.getElementById("reviewNextTitle").textContent = nextStage === "self_assessment"
      ? "Continue to self-assessment" : "Continue to questionnaire";
    document.getElementById("experiencePanel").classList.remove("hidden");
    setHeaderStatus(`Review ${step} · ${label}`);
    selectReviewView(flow.current_view);
    updateReviewGate();
  }
}

async function advanceReviewFlow(answers = null) {
  if (state.reviewNextPending || !state.reviewFlow || state.reviewFlow.stage === "complete") return;
  const previous = state.reviewFlow;
  const modern = previous.schema === "sequential_review_v2";
  const surveyStep = modern && previous.stage !== "review";
  if (surveyStep && !answers) return;
  const error = document.getElementById(previous.stage === "questionnaire" ? "questionnaireError" : "reviewNextError");
  const buttons = [document.getElementById("finishExperienceButton"), document.getElementById("questionnaireNextButton")];
  state.reviewNextPending = true;
  if (surveyStep) studySurveys.setPending(true);
  buttons.forEach(button => { button.disabled = true; });
  error.classList.add("hidden");
  const leavingReview = previous.stage === "review";
  if (leavingReview) state.reviewTimer?.suspend();
  try {
    if (leavingReview && !state.timingStep) throw new Error("Feedback is still loading. Please try Next again when it is ready.");
    if (leavingReview) await state.reviewTimer?.flush();
    const payload = {
      participant_id: state.participantId,
      view: previous.current_view,
      stage: previous.stage,
    };
    if (modern) {
      payload.step_id = previous.step_id;
      if (surveyStep) payload.answers = answers;
    }
    const result = await api("/api/expert-study/review-next", payload);
    if (leavingReview) {
      // The transition is already saved. A failed final acknowledgement stays in
      // the timing outbox; it must not leave the displayed questionnaire behind.
      await state.reviewTimer?.finish("next").catch(() => {});
      state.timingStep = null;
      state.timingCoverage.clear();
    }
    if (surveyStep) studySurveys.clearDraft();
    setReviewFlow(result.review_flow);
    state.start.case_completed = result.case_completed;
    state.start.case.next_case_url = result.next_case_url;
    state.start.completion_code = result.completion_code;
    renderReviewFlow();
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (err) {
    if (leavingReview) state.reviewTimer?.resume();
    if (surveyStep) studySurveys.showError(err.message);
    else {
      error.textContent = err.message;
      error.classList.remove("hidden");
    }
  } finally {
    state.reviewNextPending = false;
    if (surveyStep) studySurveys.setPending(false);
    buttons.forEach(button => { button.disabled = false; });
    updateReviewGate();
    if (leavingReview) window.requestAnimationFrame(startReviewTiming);
  }
}

// Back: one step, only where the protocol allows it (server decides; see can_go_back).
async function goBackReviewFlow() {
  if (state.reviewNextPending || !state.reviewFlow?.can_go_back) return;
  const button = document.getElementById("backButton");
  const error = document.getElementById("backError");
  state.reviewNextPending = true;
  button.disabled = true;
  error.classList.add("hidden");
  try {
    const result = await api("/api/expert-study/review-back", {
      participant_id: state.participantId, step_id: state.reviewFlow.step_id,
    });
    setReviewFlow(result.review_flow);
    renderReviewFlow();
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove("hidden");
  } finally {
    state.reviewNextPending = false;
    button.disabled = false;
    updateReviewGate();
    if (state.reviewFlow?.stage === "review") window.requestAnimationFrame(startReviewTiming);
  }
}

// End of the study: a check mark, a thank-you and a way back to the launcher.
function showCompletion() {
  pauseReviewMedia();
  studySurveys.hide();
  ["accessPanel", "readyPanel", "processingPanel", "experiencePanel", "questionnairePanel", "reviewProgress"].forEach(id => {
    document.getElementById(id).classList.add("hidden");
  });
  document.getElementById("completionPanel").classList.remove("hidden");
  setHeaderStatus("Session complete");
  // More recorded cases would continue here; the resident study has one.
  document.getElementById("nextCaseButton").classList.toggle("hidden", !state.start.case.next_case_url);
  document.getElementById("returnToSimulatorButton").classList.toggle("hidden", Boolean(state.start.case.next_case_url));
}

async function returnToSimulator() {
  try {
    await api("/api/expert-study/return-to-simulator", { participant_id: state.participantId });
    document.getElementById("returnToSimulatorNote").classList.remove("hidden");
  } catch (error) {
    showToast("Please switch to the study window on the desktop.");
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
document.getElementById("finishExperienceButton").addEventListener("click", () => {
  if (state.reviewFlow?.stage === "review" && state.reviewView === "summary" && !summaryGateOpen()) return;
  if (state.reviewFlow?.stage === "review" && state.reviewView === "event-based" && state.experience
      && eventProgress().done < eventProgress().total) return;
  advanceReviewFlow();
});
document.getElementById("questionnaireNextButton").addEventListener("click", () => advanceReviewFlow());
document.getElementById("backButton").addEventListener("click", goBackReviewFlow);
document.getElementById("nextCaseButton").addEventListener("click", goToNextCase);
document.getElementById("returnToSimulatorButton").addEventListener("click", returnToSimulator);
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
  if (event.currentTarget.open) {
    logEvent("support_details_opened");
    recordReviewCoverage("support_details");
  }
});

document.getElementById("eventPhaseTabs").addEventListener("click", event => {
  const phase = event.target.closest("button[data-phase]")?.dataset.phase;
  if (phase && state.reviewView === "event-based") recordReviewCoverage(`phase_filter:${phase}`);
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
