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

  function describe(item) {
    const id = String(item.feedback_id || "");
    if (id === "coaching:working_corridor" || item.category === "exposure") {
      return { category: "visibility", group: "Safety & visibility", order: 0, title: "Drill visibility" };
    }
    if (id === "coaching:safety_contacts" || item.category === "safety") {
      return { category: "boundary", group: "Safety & visibility", order: 1, title: "Critical Structure Violation" };
    }
    if (id === "coaching:motion_reference") {
      return { category: "speed", group: "Motion", order: 2, title: "Low tool movement speed" };
    }
    if (id === "coaching:short_strokes") {
      return { category: "length", group: "Motion", order: 3, title: "Short drill strokes" };
    }
    return { category: "other", group: "Other feedback", order: 4, title: item.title || "Session feedback" };
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

  function actionFor(item) {
    const category = describe(item).category;
    if (category === "length") return "Where the anatomy and task permit, consider longer, continuous drill strokes.";
    if (category === "speed") return "Aim for smooth, continuous tool movement where the anatomy and task permit.";
    return item.recommended_action || item.message || "Review the linked evidence before the next attempt.";
  }

  function create(options) {
    const focus = document.getElementById("focusList");
    const list = document.getElementById("clipList");
    const scroll = document.getElementById("clipScroll");
    const map = document.getElementById("coachingMap");
    const phaseMap = document.getElementById("phaseMap");
    let items = [], selectedId = null, selectedIndex = null, expanded = true;
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

    function renderCards() {
      if (!items.length) {
        focus.innerHTML = `<div class="empty-state"><h2>No qualifying coaching topic</h2><p>No coaching topic met the configured criteria for this session.</p></div>`;
        return;
      }
      const groups = [...new Set(items.map(item => describe(item).group))];
      focus.innerHTML = groups.map(group => `<section class="coaching-group"><h3 class="coaching-group-title">${escape(group)}</h3><div class="coaching-items">${items.filter(item => describe(item).group === group).map(item => {
        const info = describe(item), index = items.indexOf(item), clips = clipsFor(item);
        const open = item.feedback_id === selectedId && expanded;
        return `<article class="coaching-item category-${info.category}"><button type="button" class="coaching-toggle" data-feedback-id="${escape(item.feedback_id)}" data-coaching-index="${index}" aria-expanded="${open}" aria-controls="coaching-detail-${index}"><span class="coaching-title">${escape(info.title)}</span><span class="coaching-count">${clips.length} ${clips.length === 1 ? "moment" : "moments"}</span><span class="coaching-toggle-label">${open ? "Hide" : "Details"}</span></button><div class="coaching-detail" id="coaching-detail-${index}" ${open ? "" : "hidden"}><div class="coaching-observation"><h3>What happened</h3><p>${escape(item.observed_issue || item.evidence_summary || "A time-linked observation was identified.")}</p></div><div class="action-callout"><h3>Try this next</h3><p>${escape(actionFor(item))}</p></div><div class="coaching-actions"><span>${clips.length} linked ${clips.length === 1 ? "moment" : "moments"}</span><button type="button" data-first-index="${index}">Go to first moment</button></div></div></article>`;
      }).join("")}</div></section>`).join("");
      focus.querySelectorAll(".coaching-toggle").forEach(button => button.addEventListener("click", () => {
        const index = Number(button.dataset.coachingIndex), item = items[index];
        if (item.feedback_id === selectedId) {
          expanded = !expanded;
          renderCards();
        } else select(item.feedback_id, 0, "feedback_list", true);
        focus.querySelector(`[data-coaching-index="${index}"]`)?.focus({ preventScroll: true });
      }));
      focus.querySelectorAll("[data-first-index]").forEach(button => button.addEventListener("click", () => {
        const index = Number(button.dataset.firstIndex);
        select(items[index].feedback_id, 0, "first_moment", true);
        focus.querySelector(`[data-first-index="${index}"]`)?.focus({ preventScroll: true });
      }));
    }

    function renderClips() {
      const item = selected();
      if (!item) {
        list.replaceChildren();
        document.getElementById("clipCategory").textContent = "";
        syncScroll();
        return;
      }
      const info = describe(item), clips = clipsFor(item);
      document.getElementById("clipCategory").textContent = info.title;
      list.innerHTML = clips.map((clip, index) => {
        const keyframe = keyframeFor(clip);
        const time = keyframe ? timeLabel(keyframe.time)
          : `${timeLabel(clip.t_start_s)}–${timeLabel(clip.t_end_s)}`;
        const phase = phaseLabel(clip, item);
        const shortPhase = phase.replaceAll("Early drilling", "Early").replaceAll("Phase unavailable", "—");
        const label = `Moment ${index + 1} · ${phase} · ${keyframe ? "Key frame" : "Interval"} · ${time}`;
        const canSeek = options.hasMedia() && finiteTime(clip.t_start_s);
        return `<div class="clip-row category-${info.category}" data-moment-index="${index}" aria-current="false"><div class="clip-copy" title="${escape(label)}"><strong class="clip-number"><span class="clip-number-full">Moment ${index + 1}</span><span class="clip-number-short">${index + 1}</span></strong><span class="clip-phase" title="${escape(phase)}"><span class="clip-phase-full">${escape(phase)}</span><span class="clip-phase-short">${escape(shortPhase)}</span></span><span class="clip-time">${escape(time)}</span></div><div class="clip-actions">${keyframe ? `<button type="button" data-keyframe-index="${index}" aria-pressed="false" aria-label="Go to key frame for ${escape(label)}"><span class="clip-action-full">Go to key frame</span><span class="clip-action-short">Frame</span></button>` : `<button type="button" data-moment-index="${index}" aria-pressed="false" aria-label="Go to interval start for ${escape(label)}" ${canSeek ? "" : "disabled"}><span class="clip-action-full">Go to moment</span><span class="clip-action-short">Go</span></button>`}<button type="button" data-clip-index="${index}" aria-pressed="false" aria-label="Play context for ${escape(label)}" ${canSeek ? "" : "disabled"}><span class="clip-action-full">Play context</span><span class="clip-action-short">Play</span></button></div></div>`;
      }).join("");
      list.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
        const mode = button.dataset.clipIndex != null ? "context"
          : button.dataset.keyframeIndex != null ? "keyframe" : "review";
        const index = Number(button.dataset.clipIndex ?? button.dataset.keyframeIndex ?? button.dataset.momentIndex);
        options.onOpen(item, clips[index], index, mode);
      }));
      list.scrollTop = 0;
      syncScroll();
    }

    function select(id, index, source, openMoment) {
      const item = items.find(row => row.feedback_id === id);
      if (!item) return;
      const changed = selectedId !== id;
      selectedId = id;
      selectedIndex = index;
      expanded = true;
      options.onSelect(item, source);
      renderCards();
      if (changed || !list.children.length) renderClips();
      if (index != null) mark(index, "review");
      if (openMoment) options.onOpen(item, clipsFor(item)[index], index, "review");
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
      layoutMarkers();
    }

    function refreshLayout() { layoutMarkers(); syncScroll(); }
    function render() {
      items = [...options.getItems()].sort((a, b) => describe(a).order - describe(b).order);
      selectedId = items.some(item => item.feedback_id === selectedId) ? selectedId : items[0]?.feedback_id || null;
      selectedIndex = null;
      expanded = true;
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
    return { render, mark, setTime, refreshLayout, clipsFor, titleFor: item => describe(item).title };
  }
  return { create, describe, clipsFor, phaseLabel, keyframeFor };
})();
