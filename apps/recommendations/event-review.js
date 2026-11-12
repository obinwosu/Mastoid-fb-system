/* Shared presentation for standalone and sequential Event-based reviews. */
window.EventReview = (() => {
  const names = {
    pre_drilling: "Predrilling", early_phase: "Early Drilling", early_surface: "Early Drilling",
    pre_antrum: "Early Drilling", antrum: "Antrum", incus: "Incus", post_incus: "Incus",
  };
  // Headings shown above each group of metrics (the group names themselves stay as they are).
  const GROUP_HEADINGS = { "Safety": "Safety Metrics", "Drilling motion": "Drilling Motion Metrics" };
  const escape = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  const finite = value => value != null && Number.isFinite(Number(value));
  const finiteTime = value => finite(value) && Number(value) >= 0;
  const timeLabel = seconds => {
    const total = Math.max(0, Number(seconds || 0));
    return `${Math.floor(total / 60)}:${(total % 60).toFixed(1).padStart(4, "0")}`;
  };
  const valueLabel = value => Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });

  function describe(item) {
    const id = String(item.feedback_id || ""), key = item.metric_key;
    if (key === "poor_drill_visibility" || id === "coaching:working_corridor" || item.category === "exposure") {
      return { category: "visibility", group: "Safety", order: 1, title: "Poor Drill Visibility" };
    }
    if (key === "critical_structure_violation" || id === "coaching:safety_contacts" || item.category === "safety") {
      return { category: "boundary", group: "Safety", order: 0, title: "Critical Structure Violation" };
    }
    if (key === "speed_mm_s" || id === "coaching:motion_reference") {
      return { category: "speed", group: "Drilling motion", order: 2, title: "Tool speed" };
    }
    if (key === "mean_stroke_length_mm" || id === "coaching:short_strokes") {
      return { category: "length", group: "Drilling motion", order: 3, title: "Stroke length" };
    }
    if (key === "removal_rate_mm3_s") {
      return { category: "removal", group: "Drilling motion", order: 4, title: "Removal rate" };
    }
    return { category: "other", group: "Other feedback", order: 5, title: item.title || "Session feedback" };
  }

  function clipsFor(item) {
    const rows = item.clips || item.review_clips || item.evidence_segments || [];
    // Empty ranked categories stay empty; the server has already selected and ranked them.
    if (item.metric_key) return [...rows];
    const clips = rows.length ? rows : finiteTime(item.t_start_s)
      ? [{ t_start_s: item.t_start_s, t_end_s: item.t_end_s }] : [];
    return [...clips].sort((a, b) => Number(a.t_start_s || 0) - Number(b.t_start_s || 0));
  }

  function phaseLabel(clip, item) {
    const assigned = clip.phase || clip.phase_name;
    const phases = assigned ? [assigned] : clip.phases?.length ? clip.phases : [item.phase];
    const labels = [...new Set(phases.filter(value => value && value !== "session")
      .map(value => names[value] || String(value).replaceAll("_", " ")))];
    return labels.length ? labels.join(" / ") : "Phase unavailable";
  }

  function keyframeFor(clip) {
    if (clip.keyframe_available === false || !clip.keyframe_url || !finiteTime(clip.keyframe_time_s)) return null;
    try {
      const url = new URL(clip.keyframe_url, window.location.href);
      if (url.origin !== window.location.origin || url.pathname !== "/api/media/keyframe") return null;
      return { url: url.href, time: Number(clip.keyframe_time_s) };
    } catch { return null; }
  }

  function momentTime(clip) {
    return keyframeFor(clip)?.time ?? (finiteTime(clip.t_start_s) ? Number(clip.t_start_s) : null);
  }

  const reviewKey = (item, index) => clipsFor(item)[index]?.clip_id || `${item.feedback_id}:${index}`;

  // Keep source indexes when filtering so playback, Viewed and logging refer to
  // the same globally selected moment. A phase filter never fills empty slots.
  function visibleClips(item, phase = "all") {
    return clipsFor(item).map((clip, index) => ({ clip, index }))
      .filter(({ clip }) => phase === "all" || (clip.phase || item.phase) === phase);
  }

  function allClipsViewed(item, isReviewed) {
    const clips = clipsFor(item);
    return clips.length > 0 && clips.every((_, index) => isReviewed(item, index));
  }

  function contextBounds(clip, duration) {
    if (!finiteTime(clip.t_start_s)) return null;
    const start = Math.max(0, Number(clip.t_start_s) - 5);
    const end = finiteTime(clip.t_end_s) ? Number(clip.t_end_s) : Number(clip.t_start_s);
    const limit = finiteTime(duration) && Number(duration) > 0 ? Number(duration) : Infinity;
    return { start: Math.min(start, limit), end: Math.min(Math.max(end, Number(clip.t_start_s)) + 5, limit) };
  }

  function comparisonSentence(item, clip, reference) {
    if (!finite(clip.observed) || ![reference.q25, reference.q75].every(finite)) return "";
    const lower = Number(clip.observed) < Number(reference.q25);
    if (!lower && Number(clip.observed) <= Number(reference.q75)) return "";
    const category = describe(item).category;
    if (category === "length") return `In this clip, your mean stroke length was ${lower ? "shorter" : "longer"} than most experts' mean stroke length during this phase.`;
    if (category === "speed") return `${lower ? "" : "Faster isn’t always better. "}In this clip, your tool speed was ${lower ? "slower" : "higher"} than most experts' tool speed during this phase.`;
    const metric = category === "removal" ? "bone removal rate" : null;
    return metric ? `In this clip, your ${metric} was ${lower ? "lower" : "higher"} than most experts' ${metric} during this phase.` : "";
  }

  function safetyHtml(item, clip) {
    if (!finite(clip.observed)) return "";
    const anatomy = typeof clip.anatomy === "string"
      ? clip.anatomy.replaceAll("_", " ").replace(/([a-z])([A-Z])/g, "$1 $2") : "";
    const fact = describe(item).category === "boundary"
      ? `Up to ${valueLabel(clip.observed)} ${clip.unit || item.unit || "mm"} deep`
      : `${valueLabel(clip.observed)} ${clip.unit || item.unit || "s"} of poor visibility`;
    return `<div class="event-safety-facts"><strong>${escape(fact)}</strong>${anatomy ? `<span>${escape(anatomy)}</span>` : ""}</div>`;
  }

  function comparisonHtml(item, clip) {
    const reference = clip.reference || item.reference;
    if (!reference?.available || !finite(clip.observed)) return "";
    const { q25, median, q75 } = reference;
    if (![q25, median, q75].every(finite) || Number(q25) > Number(median) || Number(median) > Number(q75)) return "";
    const points = (reference.points || []).filter(finite).map(Number);
    if (!points.length) return "";
    // Trim only the displayed expert tails. Ranking and the selected value are untouched.
    const spread = Number(q75) - Number(q25);
    const trimmed = points.filter(value => value >= q25 - 1.5 * spread && value <= Number(q75) + 1.5 * spread);
    const visible = trimmed.length ? trimmed : points;
    const observed = Number(clip.observed);
    const lower = Math.min(observed, Number(q25), ...visible);
    const upper = Math.max(observed, Number(q75), ...visible);
    const padding = Math.max((upper - lower) * .09, Math.abs(upper) * .02, .1);
    const lo = Math.max(0, lower - padding), hi = upper + padding;
    const x = value => 18 + (Number(value) - lo) / Math.max(hi - lo, .001) * 564;
    const px = x(observed);
    const unit = clip.unit || item.unit || reference.unit || "";
    const label = `${valueLabel(observed)} ${unit}`;
    const title = `${describe(item).title}: ${label}. Same-phase expert five-second window distribution; central half ${valueLabel(q25)} to ${valueLabel(q75)} ${unit}.`;
    const sentence = comparisonSentence(item, clip, reference);
    const heading = `Expert Distribution of ${describe(item).title.replace(/\b\w/g, c => c.toUpperCase())}`;
    return `<p class="event-comparison-title">${escape(heading)}</p><svg class="event-comparison-plot" viewBox="0 0 600 106" role="img" aria-label="${escape(title)}"><title>${escape(title)}</title>
      <line x1="18" x2="582" y1="70" y2="70" class="event-axis" />
      <rect x="${x(q25)}" y="59" width="${Math.max(1, x(q75) - x(q25))}" height="14" rx="3" class="event-band"><title>Middle half of expert values</title></rect>
      <line x1="${x(median)}" x2="${x(median)}" y1="56" y2="76" class="event-median" />
      <line x1="${px}" x2="${px}" y1="32" y2="77" class="event-current" />
      <path d="M${px},26 l5,5 -5,5 -5,-5 Z" class="event-current-dot" />
      ${[lo, (lo + hi) / 2, hi].map(value => `<text x="${x(value)}" y="96" text-anchor="middle" class="event-axis-label">${valueLabel(value)}</text>`).join("")}
      </svg><div class="event-comparison-legend"><span title="25th–75th percentiles of same-phase expert five-second window values"><i class="event-middle-half"></i>Middle half of experts</span><span><i class="event-this-clip"></i>This clip</span><span class="event-axis-unit">${escape(unit)}</span></div>${sentence ? `<p class="event-comparison-note">${escape(sentence)}</p>` : ""}`;
  }

  function create(options) {
    const focus = document.getElementById("focusList");
    const phaseTabs = document.getElementById("eventPhaseTabs");
    const detail = document.getElementById("eventMomentDetail");
    const actions = document.getElementById("eventMediaActions");
    const keyframeButton = document.getElementById("eventKeyframeButton");
    const localViewed = new Set();
    let items = [], phases = [], activePhase = "all", expandedId = null, selectedId = null, selectedIndex = null;
    const reviewed = options.isReviewed || ((item, index) => localViewed.has(reviewKey(item, index)));
    const selected = () => items.find(item => item.feedback_id === selectedId);
    const itemViewed = item => allClipsViewed(item, reviewed);
    const firstVisible = () => items.find(item => visibleClips(item, activePhase).length) || items[0];

    function updateDetail() {
      const item = selected(), clip = item && selectedIndex != null ? clipsFor(item)[selectedIndex] : null;
      const safety = item && describe(item).group === "Safety";
      if (actions) actions.hidden = !clip || !safety || !options.hasMedia() || !keyframeFor(clip);
      if (keyframeButton) keyframeButton.setAttribute("aria-pressed", "false");
      if (!detail) return;
      if (!clip) { detail.replaceChildren(); detail.hidden = true; return; }
      if (safety) {
        detail.innerHTML = safetyHtml(item, clip);
      } else {
        detail.innerHTML = comparisonHtml(item, clip);
      }
      detail.hidden = !detail.innerHTML;
    }

    function refreshViewed() {
      focus.querySelectorAll("[data-feedback-id]").forEach(card => {
        const item = items.find(row => row.feedback_id === card.dataset.feedbackId);
        if (!item) return;
        const complete = itemViewed(item), count = clipsFor(item).length;
        const shown = visibleClips(item, activePhase).length;
        card.classList.toggle("viewed", complete);
        const status = card.querySelector(".event-category-status");
        const countLabel = activePhase === "all" ? `${count} ${count === 1 ? "moment" : "moments"}` : `${shown} of ${count}`;
        status.textContent = complete ? `${activePhase === "all" ? "" : `${countLabel} · `}✓ Viewed` : countLabel;
        status.classList.toggle("event-viewed", complete);
        card.querySelectorAll("[data-moment-index]").forEach(button => {
          const index = Number(button.dataset.momentIndex), viewed = reviewed(item, index);
          button.querySelector(".event-moment-viewed").textContent = viewed ? "✓" : "";
          button.setAttribute("aria-current", String(item.feedback_id === selectedId && index === selectedIndex));
        });
      });
      updateGuide();
    }

    // In the study (options.guide), pulse the one thing to click next (see .guide-pulse).
    function updateGuide() {
      focus.querySelectorAll(".guide-pulse").forEach(node => node.classList.remove("guide-pulse"));
      if (!options.guide) return;
      const cards = [...focus.querySelectorAll("[data-feedback-id]")];
      const itemFor = card => items.find(row => row.feedback_id === card.dataset.feedbackId);
      const open = cards.find(card => card.dataset.feedbackId === expandedId);
      let target = null;
      if (open) {
        const item = itemFor(open);
        target = [...open.querySelectorAll("[data-moment-index]")]
          .find(button => !reviewed(item, Number(button.dataset.momentIndex))) || null;
      }
      if (!target) {
        // A metric with no events has nothing to review (the Next gate skips it too).
        const next = cards.find(card => {
          const item = itemFor(card);
          return item && clipsFor(item).length > 0 && !itemViewed(item);
        });
        target = next?.querySelector(".event-category-toggle") || null;
      }
      target?.classList.add("guide-pulse");

    }

    function mark(index, action) {
      if (index >= 0) selectedIndex = index;
      refreshViewed();
      if (keyframeButton) keyframeButton.setAttribute("aria-pressed", String(action === "keyframe"));
      document.getElementById("videoHeading").textContent = "Event Video Clip";
    }

    function openMoment(item, index, mode) {
      const clip = clipsFor(item)[index];
      if (!clip || !options.hasMedia()) return;
      if (mode === "keyframe" && (describe(item).group !== "Safety" || !keyframeFor(clip))) return;
      if (selectedId !== item.feedback_id) options.onSelect(item, "feedback_list");
      selectedId = item.feedback_id; selectedIndex = index;
      updateDetail();
      localViewed.add(reviewKey(item, index));
      options.onOpen(item, clip, index, mode);
      refreshViewed();
    }

    function renderCards() {
      const visible = items;
      if (!visible.length) {
        focus.innerHTML = `<div class="empty-state"><h2>No selected moments</h2><p>No moments are included in the current selection.</p></div>`;
        return;
      }
      const groups = [...new Set(visible.map(item => describe(item).group))];
      focus.innerHTML = groups.map(group => `<section class="event-metric-group"><h3>${escape(GROUP_HEADINGS[group] || group)}</h3>${visible.filter(item => describe(item).group === group).map(item => {
        const info = describe(item), rows = visibleClips(item, activePhase), open = expandedId === item.feedback_id;
        const bodyId = `event-moments-${items.indexOf(item)}`;
        const status = activePhase === "all" ? item.status : item.phase_status?.[activePhase]?.status;
        const empty = status === "unavailable"
          ? `Review data unavailable${activePhase === "all" ? "" : " for this phase"}.`
          : activePhase === "all" ? "No moments are included in the current selection."
            : "No moments from this phase are included in the current selection.";
        return `<article class="event-category${open ? " open" : ""} category-${info.category}" data-feedback-id="${escape(item.feedback_id)}">
          <button type="button" class="event-category-toggle" aria-expanded="${open}" aria-controls="${bodyId}"><strong>${escape(info.title)}</strong><span class="event-category-meta"><span class="event-category-status"></span><span aria-hidden="true">${open ? "▾" : "▸"}</span></span></button>
          <div id="${bodyId}" class="event-ranked-moments" ${open ? "" : "hidden"}>${rows.length ? `<div class="event-moment-header" aria-hidden="true"><span>Event #</span><span>Duration</span><span>Phase</span><span>Viewed?</span></div>` : ""}${rows.length ? rows.map(({ clip, index }) => {
            const start = finiteTime(clip.t_start_s) ? Number(clip.t_start_s) : momentTime(clip);
            const end = finiteTime(clip.t_end_s) ? Number(clip.t_end_s) : start;
            const label = start == null ? "Time unavailable" : `${timeLabel(start)}${end > start ? `–${timeLabel(end)}` : ""}`;
            const available = options.hasMedia() && start != null;
            const phase = phaseLabel(clip, item);
            return `<button type="button" class="event-moment" data-moment-index="${index}" aria-current="false" aria-label="Play context for moment ${clip.rank || index + 1}, ${escape(phase)}, ${escape(label)}" ${available ? "" : "disabled"}><span class="event-moment-rank">${clip.rank || index + 1}</span><span class="event-moment-time">${escape(label)}</span><span class="event-moment-phase">${escape(phase)}</span><span class="event-moment-viewed"></span></button>`;
          }).join("") : `<p class="event-no-moments">${empty}</p>`}</div></article>`;
      }).join("")}</section>`).join("");
      focus.querySelectorAll(".event-category-toggle").forEach(button => button.addEventListener("click", () => {
        const id = button.closest("[data-feedback-id]").dataset.feedbackId;
        const item = items.find(row => row.feedback_id === id);
        expandedId = expandedId === id ? null : id;
        if (selectedId !== id) {
          selectedId = id; selectedIndex = null;
          options.onSelect(item, "feedback_list");
          updateDetail();
        }
        renderCards();
        focus.querySelector(`[data-feedback-id="${CSS.escape(id)}"] .event-category-toggle`)?.focus({ preventScroll: true });
        // Opening a category plays its first listed moment straight away.
        if (expandedId === id) openFirstMoment(item);
      }));
      focus.querySelectorAll(".event-moment").forEach(button => button.addEventListener("click", () => {
        const item = items.find(row => row.feedback_id === button.closest("[data-feedback-id]").dataset.feedbackId);
        openMoment(item, Number(button.dataset.momentIndex), "context");
      }));
      refreshViewed();
    }

    function renderPhases() {
      if (!phaseTabs) return;
      phaseTabs.hidden = !phases.length;
      phaseTabs.innerHTML = [{ phase: "all", label: "All phases" }, ...phases].map(phase => `<button type="button" data-phase="${escape(phase.phase)}" aria-pressed="${phase.phase === activePhase}">${escape(phase.label || names[phase.phase] || phase.phase)}</button>`).join("");
      phaseTabs.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
        if (button.dataset.phase === activePhase) return;
        activePhase = button.dataset.phase;
        const first = firstVisible();
        selectedId = first?.feedback_id || null; selectedIndex = null; expandedId = selectedId;
        if (first) options.onSelect(first, "phase_selector");
        renderPhases(); renderCards(); updateDetail();
        openFirstMoment(first);
      }));
    }

    function render() {
      const review = options.getReview?.();
      phases = review?.phases || [];
      items = [...options.getItems()].sort((a, b) => describe(a).order - describe(b).order);
      activePhase = phases.some(phase => phase.phase === activePhase) ? activePhase : "all";
      const first = firstVisible();
      selectedId = first?.feedback_id || null; selectedIndex = null; expandedId = selectedId;
      document.getElementById("clipCount").textContent = "";
      document.getElementById("videoHeading").textContent = "Event Video Clip";
      renderPhases(); renderCards(); updateDetail();
      if (first) options.onSelect(first, "automatic_first_item");
      openFirstMoment(first);
    }

    // The open category always shows its first listed moment in the video panel.
    function openFirstMoment(item) {
      const first = item ? visibleClips(item, activePhase)[0] : null;
      if (first) openMoment(item, first.index, "context");
    }

    keyframeButton?.addEventListener("click", () => {
      const item = selected();
      if (item && selectedIndex != null) openMoment(item, selectedIndex, "keyframe");
    });
    return { render, mark, refreshViewed, clipsFor, titleFor: item => describe(item).title };
  }
  return { create, describe, clipsFor, visibleClips, allClipsViewed, comparisonHtml, comparisonSentence, safetyHtml, phaseLabel, keyframeFor, momentTime, reviewKey, contextBounds };
})();
