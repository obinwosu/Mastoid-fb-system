/* Shared presentation for the standalone and sequential Event-based reviews. */
window.EventReview = (() => {
  const names = {
    pre_drilling: "Predrilling", early_phase: "Early drilling", early_surface: "Early drilling",
    pre_antrum: "Early drilling", antrum: "Antrum", incus: "Incus", post_incus: "Incus",
  };
  const escape = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  const timeLabel = seconds => {
    const total = Math.max(0, Number(seconds || 0));
    return `${Math.floor(total / 60)}:${(total % 60).toFixed(1).padStart(4, "0")}`;
  };
  const finiteTime = value => value != null && Number.isFinite(Number(value)) && Number(value) >= 0;

  // Topic names and definitions match the Summary-based feedback page (2026-11-07).
  const GROUP_TEXT = {
    Safety: "These metrics describe how safely you operated.",
    "Drilling motion": "These metrics describe how you handled your drill.",
  };

  function describe(item) {
    const id = String(item.feedback_id || "");
    if (id === "coaching:working_corridor" || item.category === "exposure") {
      return { category: "visibility", group: "Safety", order: 1, title: "Poor Drill Visibility",
        text: "Instances of drilling with the center of the burr under a bony ledge." };
    }
    if (id === "coaching:safety_contacts" || item.category === "safety") {
      return { category: "boundary", group: "Safety", order: 0, title: "Critical Structure Violation",
        text: "Instances of contacting a critical structure with an active burr." };
    }
    if (id === "coaching:motion_reference") {
      return { category: "speed", group: "Drilling motion", order: 2, title: "Tool speed",
        text: "Moments when your tool moved more slowly than most expert surgeons." };
    }
    if (id === "coaching:short_strokes") {
      return { category: "length", group: "Drilling motion", order: 3, title: "Stroke length",
        text: "Moments when your drill strokes were shorter than most expert surgeons." };
    }
    return { category: "other", group: "Other feedback", order: 4, title: item.title || "Session feedback",
      text: item.observed_issue || item.evidence_summary || "" };
  }

  function clipsFor(item) {
    const rows = item.clips || item.review_clips || item.evidence_segments || [];
    const clips = rows.length ? rows : [{ t_start_s: item.t_start_s, t_end_s: item.t_end_s }];
    return [...clips].sort((a, b) => Number(a.t_start_s || 0) - Number(b.t_start_s || 0));
  }

  function phaseLabel(clip, item) {
    const phases = clip.phases?.length ? clip.phases : [clip.phase || clip.phase_name || item.phase];
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
    } catch {
      return null;
    }
  }

  // The time a moment is labelled with: its key frame when there is one, else its start.
  function momentTime(clip) {
    const keyframe = keyframeFor(clip);
    if (keyframe) return keyframe.time;
    return finiteTime(clip.t_start_s) ? Number(clip.t_start_s) : null;
  }
  const shortTime = seconds => {
    const total = Math.max(0, Math.floor(Number(seconds || 0)));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  };

  function create(options) {
    const focus = document.getElementById("focusList");
    // Moments are drawn inside the open topic; the old list under the video is only a fallback.
    const fallbackList = document.getElementById("clipList");
    let list = fallbackList;
    const scroll = document.getElementById("clipScroll");
    const map = document.getElementById("coachingMap");
    const phaseMap = document.getElementById("phaseMap");
    let items = [], selectedId = null, selectedIndex = null, expanded = true;
    let openGroup = null;  // Safety / Drilling motion: closed until clicked, one at a time
    const reviewed = options.isReviewed || (() => false);
    const itemViewed = item => clipsFor(item).every((_, i) => reviewed(item, i));
    const selected = () => items.find(item => item.feedback_id === selectedId);

    function syncScroll() {
      const maximum = Math.max(0, list.scrollHeight - list.clientHeight);
      scroll.max = maximum;
      scroll.value = list.scrollTop;
      scroll.disabled = maximum < 1;
      scroll.hidden = maximum < 1;
      scroll.style.setProperty("--scroll-thumb-height", `${Math.max(28, list.clientHeight ** 2 / Math.max(1, list.scrollHeight))}px`);
      const bounds = list.getBoundingClientRect();
      const visible = [...list.querySelectorAll(".clip-row")].filter(row => {
        const box = row.getBoundingClientRect();
        return box.bottom > bounds.top + 1 && box.top < bounds.bottom - 1;
      });
      const count = list.children.length;
      scroll.setAttribute("aria-valuetext", visible.length
        ? `Moments ${Number(visible[0].dataset.momentIndex) + 1} to ${Number(visible.at(-1).dataset.momentIndex) + 1} of ${count}`
        : "No review moments");
    }

    function revealMoment(index) {
      const row = list.querySelector(`[data-moment-index="${index}"]`);
      if (!row) return;
      const top = row.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;
      const end = top + row.getBoundingClientRect().height;
      if (top < list.scrollTop) list.scrollTop = top;
      else if (end > list.scrollTop + list.clientHeight) list.scrollTop = end - list.clientHeight;
      syncScroll();
    }

    function setTime(time) {
      phaseMap.querySelectorAll(".phase-segment").forEach(segment => {
        const active = time >= Number(segment.dataset.start) && time < Number(segment.dataset.end);
        segment.setAttribute("aria-current", String(active));
      });
    }

    function mark(index, action) {
      if (index >= 0) selectedIndex = index;
      focus.querySelectorAll(".moment-button").forEach(button => {
        button.setAttribute("aria-current", "false");
        button.classList.remove("active");
      });
      list.querySelectorAll(".clip-row").forEach(row => {
        row.setAttribute("aria-current", String(Number(row.dataset.momentIndex) === selectedIndex));
      });
      list.querySelectorAll("button").forEach(button => {
        const value = action === "keyframe" ? button.dataset.keyframeIndex
          : action === "context" ? button.dataset.clipIndex : button.dataset.momentIndex;
        const active = index >= 0 && value != null && Number(value) === index;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      });
      map.querySelectorAll("button").forEach(button => {
        button.setAttribute("aria-pressed", String(button.dataset.feedbackId === selectedId
          && Number(button.dataset.momentIndex) === selectedIndex));
      });
      if (index >= 0) revealMoment(index);
    }

    function refreshViewed() {
      focus.querySelectorAll(".coaching-item").forEach(card => {
        const item = items[Number(card.dataset.coachingIndex)];
        card.classList.toggle("viewed", Boolean(item) && itemViewed(item));
      });
      focus.querySelectorAll(".event-group").forEach(group => {
        const members = items.filter(item => describe(item).group === group.dataset.group);
        group.classList.toggle("viewed", members.length > 0 && members.every(itemViewed));
      });
    }

    // One button per moment: "Moment @ 1:26". Safety moments are a point in time (their
    // key frame) and pause the video there; drilling-motion moments play from the start.
    const showsFrame = item => describe(item).group === "Safety";
    function momentsHtml(item) {
      const info = describe(item), frame = showsFrame(item);
      return clipsFor(item).map((clip, index) => {
        const start = frame ? momentTime(clip)
          : finiteTime(clip.t_start_s) ? Number(clip.t_start_s) : momentTime(clip);
        const label = `Moment${start == null ? "" : ` @ ${shortTime(start)}`}`;
        const usable = options.hasMedia() && start != null;
        return `<button type="button" class="clip-row moment-button category-${info.category}" data-moment-index="${index}" aria-current="false" aria-label="${frame ? "Go to" : "Play"} ${escape(label)}" ${usable ? "" : "disabled"}><span class="moment-play" aria-hidden="true">${frame ? "◉" : "▶"}</span><span class="moment-label">${escape(label)}</span></button>`;
      }).join("");
    }

    function renderCards() {
      if (!items.length) {
        focus.innerHTML = `<div class="empty-state"><h2>No moments flagged</h2><p>No moments in this session met the review criteria.</p></div>`;
        list = fallbackList;
        return;
      }
      const groups = [...new Set(items.map(item => describe(item).group))];
      // Opening Safety or Drilling motion shows every topic in it with its moments.
      focus.innerHTML = groups.map(group => {
        const groupOpen = group === openGroup;
        return `<section class="coaching-group event-group${groupOpen ? " open" : ""}" data-group="${escape(group)}"><button type="button" class="event-group-toggle" aria-expanded="${groupOpen}"><span class="group-title"><span class="group-name">${escape(group)} <span class="expand-hint"></span></span>${GROUP_TEXT[group] ? `<span class="group-text">${escape(GROUP_TEXT[group])}</span>` : ""}</span><span class="group-state" aria-hidden="true"></span></button><div class="coaching-items" ${groupOpen ? "" : "hidden"}>${items.filter(item => describe(item).group === group).map(item => {
        const info = describe(item), index = items.indexOf(item), clips = clipsFor(item);
        return `<article class="coaching-item open category-${info.category}" data-coaching-index="${index}" data-feedback-id="${escape(item.feedback_id)}"><div class="coaching-toggle"><span class="coaching-name"><span class="coaching-title">${escape(info.title)}</span><span class="coaching-text">${escape(info.text)}</span></span><span class="coaching-count">${clips.length} ${clips.length === 1 ? "moment" : "moments"}</span></div><div class="coaching-detail"><div class="clip-list topic-moments" role="region" aria-label="Moments for ${escape(info.title)}">${groupOpen ? momentsHtml(item) : ""}</div></div></article>`;
      }).join("")}</div></section>`;
      }).join("");
      list = focus.querySelector(`.coaching-item[data-feedback-id="${CSS.escape(selectedId || "")}"] .topic-moments`) || fallbackList;
      focus.querySelectorAll(".event-group-toggle").forEach(button => button.addEventListener("click", () => {
        const group = button.closest(".event-group").dataset.group;
        openGroup = openGroup === group ? null : group;
        renderCards();
        focus.querySelector(`.event-group[data-group="${CSS.escape(group)}"] .event-group-toggle`)?.focus({ preventScroll: true });
      }));
      focus.querySelectorAll(".moment-button").forEach(button => button.addEventListener("click", () => {
        const card = button.closest(".coaching-item");
        const item = items[Number(card.dataset.coachingIndex)];
        const index = Number(button.dataset.momentIndex);
        if (item.feedback_id !== selectedId) {
          selectedId = item.feedback_id;
          options.onSelect(item, "feedback_list");
        }
        list = card.querySelector(".topic-moments");
        options.onOpen(item, clipsFor(item)[index], index, showsFrame(item) ? "frame" : "play");
      }));
      refreshViewed();
    }

    function renderClips() {
      // Moments are drawn inside their topics by renderCards; nothing goes under the video.
      fallbackList.replaceChildren();
      document.getElementById("clipCategory").textContent = "";
      syncScroll();
    }

    function select(id, index, source, openMoment) {
      const item = items.find(row => row.feedback_id === id);
      if (!item) return;
      selectedId = id;
      selectedIndex = index;
      expanded = true;
      openGroup = describe(item).group;
      options.onSelect(item, source);
      renderCards();
      renderClips();
      if (index != null) mark(index, "review");
      if (openMoment) options.onOpen(item, clipsFor(item)[index], index, showsFrame(item) ? "frame" : "play");
    }

    function layoutMarkers() {
      const width = map.clientWidth;
      if (!width) return;
      const coarse = window.matchMedia("(pointer: coarse)").matches;
      const hit = coarse ? 36 : 24, rowHeight = coarse ? 36 : 18, lanes = [];
      const duration = Math.max(1, Number(options.getDuration()));
      [...map.children].sort((a, b) => Number(a.dataset.time) - Number(b.dataset.time)).forEach(button => {
        const x = Number(button.dataset.time) / duration * width;
        let lane = lanes.findIndex(previous => x - previous >= hit + 2);
        if (lane < 0) { lane = lanes.length; lanes.push(-Infinity); }
        lanes[lane] = x;
        button.style.left = `${x}px`;
        button.style.top = `${lane * rowHeight}px`;
        button.style.width = `${hit}px`;
        button.style.height = `${rowHeight}px`;
      });
      const height = `${Math.max(2, lanes.length) * rowHeight}px`;
      if (map.style.height !== height) map.style.height = height;
    }

    function renderMap() {
      const duration = Math.max(1, Number(options.getDuration()));
      document.getElementById("durationLabel").textContent = `${timeLabel(duration)} total`;
      document.getElementById("timelineEnd").textContent = timeLabel(duration);
      phaseMap.innerHTML = options.getPhases().map(phase => {
        const start = Number(phase.t_start_s), end = Number(phase.t_end_s);
        const name = names[phase.phase_name] || String(phase.phase_name || "").replaceAll("_", " ");
        const source = phase.phase_name === "pre_drilling" ? "Predrilling rule"
          : phase.phase_source_kind === "model_phase_prediction" ? "Model-predicted phase" : "Manual phase annotation";
        return `<div class="phase-segment" data-phase-id="${escape(phase.phase_id || "")}" data-start="${start}" data-end="${end}" style="left:${100 * start / duration}%;width:${100 * (end - start) / duration}%" title="${escape(name)} · ${source} · ${timeLabel(start)}–${timeLabel(end)}" aria-current="false">${escape(name)}</div>`;
      }).join("");
      map.innerHTML = items.flatMap(item => clipsFor(item).map((clip, index) => {
        const info = describe(item), keyframe = keyframeFor(clip);
        const time = keyframe?.time ?? (finiteTime(clip.t_start_s) ? Number(clip.t_start_s) : null);
        if (time == null) return "";
        const label = `${info.title} · Moment ${index + 1} · ${phaseLabel(clip, item)} · ${timeLabel(time)}`;
        return `<button type="button" class="map-marker category-${info.category}" data-feedback-id="${escape(item.feedback_id)}" data-moment-index="${index}" data-time="${time}" aria-label="${escape(label)}" title="${escape(label)}" aria-pressed="false"></button>`;
      })).join("");
      map.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
        select(button.dataset.feedbackId, Number(button.dataset.momentIndex), "procedure_map", true);
      }));
      const legend = document.getElementById("mapLegend");
      if (legend) {
        legend.innerHTML = items.map(item => {
          const info = describe(item);
          return `<span class="category-${info.category}"><i aria-hidden="true"></i>${escape(info.title)}</span>`;
        }).join("");
      }
      layoutMarkers();
    }

    function refreshLayout() { layoutMarkers(); syncScroll(); }
    function render() {
      items = [...options.getItems()].sort((a, b) => describe(a).order - describe(b).order);
      selectedId = items.some(item => item.feedback_id === selectedId) ? selectedId : items[0]?.feedback_id || null;
      selectedIndex = null;
      expanded = false;
      openGroup = null;
      document.getElementById("clipCount").textContent = `${items.length} ${items.length === 1 ? "topic" : "topics"}`;
      renderMap(); renderCards(); renderClips();
      if (selected()) options.onSelect(selected(), "automatic_first_item");
      document.getElementById("videoHeading").textContent = "Session video";
      refreshLayout();
    }
    scroll.addEventListener("input", () => { list.scrollTop = Number(scroll.value); });
    list.addEventListener("scroll", syncScroll);
    new ResizeObserver(refreshLayout).observe(list);
    new ResizeObserver(layoutMarkers).observe(map);
    return { render, mark, setTime, refreshLayout, refreshViewed, clipsFor, titleFor: item => describe(item).title };
  }
  return { create, describe, clipsFor, phaseLabel, keyframeFor, momentTime };
})();
