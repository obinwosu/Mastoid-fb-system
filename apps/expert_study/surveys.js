/* English study questionnaires. Submitted answers are owned by the study service. */
window.StudySurveys = (() => {
  const escape = value => String(value ?? "").replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
  const roles = {
    resident: { label: "Resident", stage: "Postgraduate year (PGY)", unit: "pgy", pgy: [1, 2, 3, 4, 5] },
    fellow: { label: "Fellow", stage: "Postgraduate year (PGY)", unit: "pgy", pgy: [6, 7] },
    attending: { label: "Attending", stage: "Years in practice", unit: "independent_practice_years", min: 0 },
  };
  const MASTOIDECTOMY_COUNTS = [["0", "0"], ["1_5", "1–5"], ["6_10", "6–10"], ["11_20", "11–20"], ["over_20", ">20"]];
  const measurementNames = [];
  const yearNames = [];
  const numeric = (value, min = 0, integer = true) => value !== "" && Number.isFinite(Number(value))
    && Number(value) >= min && (!integer || Number.isInteger(Number(value)));
  const viewLabel = view => view === "summary" ? "Summary" : "Event-based";

  function create({ root, onSubmit, onMediaEvent = () => {} }) {
    let context = null;
    let draftKey = null;
    let stageCache = {};
    let previousRole = "";
    let pending = false;
    let disabledBeforeSubmit = [];
    let draftUnavailable = false;

    const field = name => root.querySelector(`[name="${name}"]`);
    const chosen = name => root.querySelector(`[name="${name}"]:checked`)?.value ?? "";
    const checked = name => Boolean(field(name)?.checked);
    const val = name => field(name)?.value ?? "";

    // A checkbox drawn as a small toggle chip ("Not sure", "Not applicable", "Prefer not to answer").
    function check(name, text, extra = "") {
      return `<label class="survey-chip"><input type="checkbox" name="${name}" ${extra}><span>${text}</span></label>`;
    }

    function question(id, label, help, control, { optional = false, attrs = "" } = {}) {
      return `<div class="survey-q" ${attrs}><div class="survey-q-text"><label class="survey-q-label" ${id ? `for="${id}"` : ""}>${label}${optional ? ' <span class="survey-optional">Optional</span>' : ""}</label>${help ? `<p class="survey-help">${help}</p>` : ""}</div><div class="survey-q-control">${control}</div></div>`;
    }

    function numberInput(id, name, unit, placeholder, { integer = true, min = 0, max = "", extraAttrs = "", unitId = "" } = {}) {
      return `<div class="survey-number"><input id="${id}" name="${name}" type="number" min="${min}" ${max !== "" ? `max="${max}"` : ""} step="${integer ? "1" : "any"}" inputmode="${integer ? "numeric" : "decimal"}" placeholder="${placeholder}" ${extraAttrs}><span ${unitId ? `id="${unitId}"` : ""}>${unit}</span></div>`;
    }

    function numberField(name, label, unit, help, { integer = true, na = false } = {}) {
      const control = `<div class="survey-number-row">${numberInput(`survey-${name}`, name, unit, "", { integer })}<div class="survey-flags">${check(`${name}_unknown`, "Not sure", `data-measurement-status="${name}"`)}${na ? check(`${name}_na`, "Not applicable", `data-measurement-status="${name}"`) : ""}</div></div>`;
      return question(`survey-${name}`, label, help, control, { attrs: `data-measurement="${name}"` });
    }

    function yesNo(name, label, help) {
      const options = [["yes", "Yes"], ["no", "No"], ["not_sure", "Not sure"]]
        .map(([value, text]) => `<label><input type="radio" name="${name}" value="${value}"><span>${text}</span></label>`).join("");
      return `<fieldset class="survey-q"><div class="survey-q-text"><legend class="survey-q-label">${label}</legend><p class="survey-help">${help}</p></div><div class="survey-q-control"><div class="survey-segmented">${options}</div></div></fieldset>`;
    }

    function section(n, id, title, body, note = "") {
      return `<section class="survey-section" aria-labelledby="${id}"><header class="survey-section-title">${n ? `<span class="survey-section-number" aria-hidden="true">${n}</span>` : ""}<h3 id="${id}">${title}</h3>${note ? `<span class="survey-section-note">${note}</span>` : ""}</header>${body}</section>`;
    }

    function pills(name, options) {
      return `<div class="survey-segmented">${options.map(([value, text]) =>
        `<label><input type="radio" name="${name}" value="${value}"><span>${text}</span></label>`).join("")}</div>`;
    }

    function backgroundMarkup() {
      const role = `<fieldset class="survey-q"><div class="survey-q-text"><legend class="survey-q-label">Current role</legend></div><div class="survey-q-control">${pills("role", Object.entries(roles).map(([value, info]) => [value, info.label]))}</div></fieldset>`;
      const stage = `<div class="survey-q hidden" id="survey-stage-row"><div class="survey-q-text"><label id="survey-stage-label" class="survey-q-label" for="survey-stage">Current training year</label><p id="survey-stage-help" class="survey-help"></p></div><div class="survey-q-control">${Object.entries(roles).filter(([, info]) => info.pgy).map(([role, info]) => `<div id="survey-pgy-${role}" class="hidden">${pills(`pgy_${role}`, info.pgy.map(n => [String(n), `PGY-${n}`]))}</div>`).join("")}<div id="survey-stage-number" class="hidden">${numberInput("survey-stage", "training_stage", "years", "", { min: 0, unitId: "survey-stage-unit" })}</div></div></div>`;
      const count = `<fieldset class="survey-q"><div class="survey-q-text"><legend class="survey-q-label">Number of prior cortical mastoidectomies performed</legend></div><div class="survey-q-control">${pills("cortical_mastoidectomies", MASTOIDECTOMY_COUNTS)}</div></fieldset>`;
      return `<div class="survey-background">${section(null, "survey-training", "Training Level and Prior Experience", role + stage + count)}</div>`;
    }

    function selfAssessmentMarkup(definition) {
      const items = definition.items;
      return `<div class="survey-self-layout"><aside class="survey-video-column"><div class="survey-video-card"><div class="survey-video-heading"><h3>Your recorded procedure</h3><span>Original recording</span></div>
        <video id="selfAssessmentVideo" controls playsinline preload="metadata">Your browser does not support the local review video.</video><div id="selfAssessmentVideoUnavailable" class="survey-video-unavailable hidden">A review video is not available for this session. Use Cannot assess when you do not have enough information.</div></div>
        <div class="survey-video-help"><h3>Rate this procedure</h3><p>Use the recording and the assigned scope of the task to judge your performance.</p><p>Your ratings may stay the same or change across reviews.</p><details><summary>How to use the scale</summary><p>${escape(definition.between_instruction)}</p><p><strong>Cannot assess</strong> — you do not have enough information to judge this.</p><p><strong>Not applicable</strong> — this aspect was outside the assigned task.</p></details></div></aside>
        <div class="survey-ratings"><div class="survey-ratings-heading"><h3>Your ratings</h3><span id="survey-answer-count" aria-live="polite">0 of ${items.length} answered</span></div><p class="survey-scale-note">Select 1–5. Open a rating guide for the descriptions.</p><div class="survey-rating-items">${items.map((item, i) => `<fieldset class="survey-rating-item"><legend><span>${i + 1}.</span>${escape(item.title)}</legend><div class="survey-rating-line"><div class="survey-numeric">${[1, 2, 3, 4, 5].map(n => `<label><input type="radio" name="${escape(item.id)}" value="${n}" aria-label="${n} out of 5"><span>${n}</span></label>`).join("")}</div><button type="button" class="survey-guide-toggle" data-guide="${escape(item.id)}" aria-controls="survey-guide-${escape(item.id)}" aria-expanded="false" aria-label="Rating guide for ${escape(item.title)}">Rating guide <span aria-hidden="true">⌄</span></button></div><div class="survey-missing-options">${definition.missing_options.map(option => `<label><input type="radio" name="${escape(item.id)}" value="${escape(option.value)}">${escape(option.label)}</label>`).join("")}</div><div id="survey-guide-${escape(item.id)}" class="survey-rating-guide hidden"><p>${escape(item.focus)}</p>${[1, 3, 5].map(n => `<div class="survey-anchor"><strong>${n}</strong><span>${escape(item.anchors[String(n)])}</span></div>`).join("")}<p class="survey-between">${escape(definition.between_instruction)}</p></div></fieldset>`).join("")}</div></div></div>`;
    }

    function feedbackMarkup(definition) {
      return `<div class="survey-feedback">${definition.items.map((item, i) => `<fieldset class="survey-feedback-question"><legend><span>Q${i + 1} · ${escape(item.label)}</span>${escape(item.text)}</legend><div class="survey-agreement-scale">${definition.scale.map(option => `<label><input type="radio" name="${escape(item.id)}" value="${option.value}"><strong>${option.value}</strong><span>${escape(option.label)}</span></label>`).join("")}</div></fieldset>`).join("")}</div>`;
    }

    // The question is the page title, so the options stand alone (2026-11-07).
    function preferenceMarkup(definition) {
      return `<fieldset class="survey-preference"><legend class="sr-only">${escape(definition.question)}</legend><div>${definition.options.map(option => `<label><input type="radio" name="preferred_view" value="${escape(option.value)}"><span class="preference-option"><strong>${escape(option.label)}</strong>${option.description ? `<small>${escape(option.description)}</small>` : ""}</span></label>`).join("")}</div></fieldset>`;
    }

    function cacheStage() {
      if (!previousRole || !field("training_stage")) return;
      stageCache[previousRole] = { value: val("training_stage") };
    }

    function updateBackground(roleChanged = false) {
      const role = chosen("role");
      if (roleChanged) cacheStage();
      const roleInfo = roles[role];
      if (roleChanged || previousRole !== role) {
        const saved = stageCache[role] || { value: "" };
        if (role === "attending") field("training_stage").value = saved.value;
      }
      previousRole = role;
      root.querySelector("#survey-stage-row").classList.toggle("hidden", !roleInfo);
      Object.keys(roles).filter(name => roles[name].pgy).forEach(name =>
        root.querySelector(`#survey-pgy-${name}`).classList.toggle("hidden", role !== name));
      root.querySelector("#survey-stage-number").classList.toggle("hidden", role !== "attending");
      root.querySelector("#survey-stage-label").textContent = roleInfo?.stage || "Current training year";
      root.querySelector("#survey-stage-help").textContent = role === "attending" ? "Whole years." : "";
      measurementNames.forEach(name => { field(name).disabled = checked(`${name}_unknown`) || checked(`${name}_na`); });
    }

    function measurement(name, allowNA = false) {
      if (checked(`${name}_unknown`)) return { status: "not_sure", value: null };
      if (allowNA && checked(`${name}_na`)) return { status: "not_applicable", value: null };
      const raw = val(name);
      if (!numeric(raw, 0, !yearNames.includes(name))) return null;
      return { status: "provided", value: Number(raw) };
    }

    function backgroundStage() {
      const role = chosen("role"), info = roles[role];
      if (!info) return null;
      if (info.pgy) {
        const pgy = chosen(`pgy_${role}`);
        return pgy ? { status: "provided", value: Number(pgy), unit: info.unit } : null;
      }
      return numeric(val("training_stage"), info.min, true)
        ? { status: "provided", value: Number(val("training_stage")), unit: info.unit } : null;
    }

    function backgroundAnswers() {
      const role = chosen("role");
      const stage = backgroundStage();
      const answers = {
        role,
        training_stage: stage,
        cortical_mastoidectomies: chosen("cortical_mastoidectomies"),
      };
      if (!role || !stage || !answers.cortical_mastoidectomies) return null;
      return answers;
    }

    function answers() {
      if (!context) return null;
      if (context.flow.stage === "background") return backgroundAnswers();
      if (context.flow.stage === "self_assessment") {
        const result = {};
        for (const item of context.definitions.self_assessment.items) {
          const value = chosen(item.id);
          if (!value) return null;
          result[item.id] = ["cannot_assess", "not_applicable"].includes(value) ? value : Number(value);
        }
        return result;
      }
      if (context.flow.stage === "questionnaire") {
        const result = {};
        for (const item of context.definitions.feedback.items) {
          const value = chosen(item.id);
          if (value === "") return null;
          result[item.id] = Number(value);
        }
        return result;
      }
      const preferred = chosen("preferred_view");
      return preferred ? { preferred_view: preferred } : null;
    }

    // Required background questions answered so far, for the progress bar.
    function backgroundProgress() {
      const items = [Boolean(chosen("role")), Boolean(backgroundStage()),
        Boolean(chosen("cortical_mastoidectomies"))];
      return [items.filter(Boolean).length, items.length];
    }

    function updateValidity() {
      const valid = Boolean(answers());
      root.querySelector("#surveySubmit").disabled = pending || !valid;
      const progress = root.querySelector("#surveyProgress");
      const bar = root.querySelector("#surveyProgressBar");
      if (bar && context.flow.stage === "background") {
        const [done, total] = backgroundProgress();
        bar.style.width = `${Math.round(100 * done / total)}%`;
        progress.textContent = valid ? "All done. You can continue." : `${done} of ${total} questions answered`;
      }
      if (context.flow.stage === "self_assessment") {
        const count = context.definitions.self_assessment.items.filter(item => chosen(item.id)).length;
        root.querySelector("#survey-answer-count").textContent = `${count} of 6 answered`;
        progress.textContent = valid ? "All items answered." : "Please answer all six items.";
      } else if (context.flow.stage !== "background") progress.textContent = valid ? "Ready to continue." : "Please complete the questions to continue.";
      root.querySelector("#surveyDraftNotice").classList.toggle("hidden", !draftUnavailable);
    }

    function saveDraft() {
      if (!draftKey) return;
      cacheStage();
      const fields = {};
      root.querySelectorAll("input[name], select[name]").forEach(input => {
        if (input.type === "radio") { if (input.checked) fields[input.name] = input.value; }
        else fields[input.name] = input.type === "checkbox" ? input.checked : input.value;
      });
      try { window.sessionStorage.setItem(draftKey, JSON.stringify({ fields, stageCache })); }
      catch { draftUnavailable = true; }
    }

    function restoreDraft() {
      let saved;
      try { saved = JSON.parse(window.sessionStorage.getItem(draftKey) || "null"); }
      catch { draftUnavailable = true; return; }
      if (!saved || typeof saved.fields !== "object") return;
      stageCache = saved.stageCache || {};
      root.querySelectorAll("input[name], select[name]").forEach(input => {
        if (!Object.hasOwn(saved.fields, input.name)) return;
        const value = saved.fields[input.name];
        if (input.type === "radio") input.checked = input.value === value;
        else if (input.type === "checkbox") input.checked = value === true;
        else input.value = String(value ?? "");
      });
    }

    function pause() { root.querySelector("video")?.pause(); }
    function hide() { pause(); root.classList.add("hidden"); }
    function clearDraft() {
      try { if (draftKey) window.sessionStorage.removeItem(draftKey); }
      catch { /* A failed storage cleanup does not alter the server-owned completed step. */ }
    }
    function setPending(value) {
      pending = value;
      if (value) {
        disabledBeforeSubmit = [...root.querySelectorAll("input,select,button")].map(input => [input, input.disabled]);
        disabledBeforeSubmit.forEach(([input]) => { input.disabled = true; });
      } else {
        disabledBeforeSubmit.forEach(([input, disabled]) => { if (input.isConnected) input.disabled = disabled; });
        disabledBeforeSubmit = [];
      }
      if (context && root.querySelector("#surveySubmit")) updateValidity();
    }
    function showError(message) {
      const error = root.querySelector("#surveyError");
      if (error) { error.textContent = message; error.classList.remove("hidden"); }
    }

    function show(value) {
      pause();
      context = value;
      previousRole = "";
      stageCache = {};
      pending = false;
      draftKey = `mastoid-survey-draft:${JSON.stringify([value.definitions.version, value.participantId, value.runId, value.flow.step_id])}`;
      const stage = value.flow.stage;
      let title, subtitle, eyebrow, markup;
      if (stage === "background") {
        title = "Background Questionnaire"; eyebrow = "";
        subtitle = "A few questions about your training and experience before today's session.";
        markup = backgroundMarkup();
      } else if (stage === "self_assessment") {
        title = value.definitions.self_assessment.title; eyebrow = `${["Before feedback", "After first feedback", "After second feedback"][value.flow.assessment_round]} · Assessment ${value.flow.assessment_round + 1} of 3`;
        subtitle = value.definitions.self_assessment.instructions;
        markup = selfAssessmentMarkup(value.definitions.self_assessment);
      } else if (stage === "questionnaire") {
        const format = value.flow.current_view === "summary" ? "summary-based" : "event-based";
        title = "Feedback questionnaire"; eyebrow = value.flow.current_view === "summary" ? "Summary-based Feedback" : "Event-based Feedback";
        subtitle = `Please evaluate the quality of the ${format} feedback you just reviewed.`;
        markup = feedbackMarkup(value.definitions.feedback);
      } else {
        title = value.definitions.preference.question || "Which feedback format did you prefer?"; eyebrow = "";
        subtitle = "";
        markup = preferenceMarkup(value.definitions.preference);
      }
      root.className = `study-survey survey-${stage}`;
      root.innerHTML = `<div class="survey-page-title">${eyebrow ? `<p class="eyebrow">${escape(eyebrow)}</p>` : ""}<h2 id="surveyTitle">${escape(title)}</h2>${subtitle ? `<p>${escape(subtitle)}</p>` : ""}</div><form id="studySurveyForm" aria-labelledby="surveyTitle" novalidate>${markup}<div class="survey-submit-row"><div class="survey-progress">${stage === "background" ? '<div class="survey-progress-track" aria-hidden="true"><div id="surveyProgressBar"></div></div>' : ""}<p id="surveyProgress" aria-live="polite"></p></div><button id="surveySubmit" type="submit" class="primary-button" disabled>${stage === "background" ? "Continue" : stage === "preference" ? "Finish" : "Next"}</button></div><p id="surveyError" class="form-error hidden" role="alert"></p><p id="surveyDraftNotice" class="survey-draft-notice hidden">This browser could not save a draft. Keep this page open until you submit.</p></form>${stage === "self_assessment" ? '<p class="survey-research-note">Proposed OSATS-like research questionnaire.</p>' : ""}`;
      restoreDraft();
      if (stage === "background") updateBackground();
      if (stage === "self_assessment") {
        const video = root.querySelector("#selfAssessmentVideo");
        video.classList.toggle("hidden", !value.media?.available);
        root.querySelector("#selfAssessmentVideoUnavailable").classList.toggle("hidden", Boolean(value.media?.available));
        if (value.media?.available) video.src = value.media.url;
        for (const [event, type] of [["play", "video_play"], ["pause", "video_pause"], ["seeked", "video_seek"]]) {
          video.addEventListener(event, () => {
            if (event === "play" && root.classList.contains("hidden")) { video.pause(); return; }
            onMediaEvent(type, { context: "self_assessment", assessment_round: value.flow.assessment_round, current_time_s: Number(video.currentTime.toFixed(3)) });
          });
        }
      }
      updateValidity();
    }

    root.addEventListener("input", event => {
      if (!event.target.matches("input,select")) return;
      if (event.target.type === "checkbox" || event.target.type === "radio" || event.target.tagName === "SELECT") return;
      saveDraft(); updateValidity();
    });
    root.addEventListener("change", event => {
      const target = event.target;
      if (!target.matches("input,select")) return;
      if (context.flow.stage === "background") {
        if (target.dataset.measurementStatus && target.checked) {
          const name = target.dataset.measurementStatus;
          field(name).value = "";
          root.querySelectorAll(`[data-measurement-status="${name}"]`).forEach(input => { if (input !== target) input.checked = false; });
        }
        updateBackground(target.name === "role");
      }
      root.querySelector("#surveyError").classList.add("hidden");
      saveDraft(); updateValidity();
    });
    root.addEventListener("click", event => {
      const toggle = event.target.closest("[data-guide]");
      if (!toggle) return;
      const expanded = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!expanded));
      root.querySelector(`#survey-guide-${toggle.dataset.guide}`).classList.toggle("hidden", expanded);
    });
    root.addEventListener("submit", event => {
      event.preventDefault();
      const payload = answers();
      if (!pending && payload) onSubmit(payload);
    });
    return { show, hide, pause, answers, setPending, showError, clearDraft };
  }
  return { create };
})();
