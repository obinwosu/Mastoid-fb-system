/* English study questionnaires. Submitted answers are owned by the study service. */
window.StudySurveys = (() => {
  const escape = value => String(value ?? "").replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
  const roles = {
    medical_student: { label: "Medical student", stage: "Medical school year", unit: "medical_school_year", min: 1 },
    resident: { label: "Resident", stage: "Residency postgraduate year (PGY)", unit: "pgy", min: 1 },
    fellow: { label: "Fellow", stage: "Fellowship year", unit: "fellowship_year", min: 1 },
    attending: { label: "Attending", stage: "Years in independent practice", unit: "independent_practice_years", min: 0 },
  };
  const measurementNames = ["otolaryngology_training_months", "clinical_mastoidectomy_years", "temporal_bone_lab_years", "this_simulator_attempts"];
  const yearNames = ["clinical_mastoidectomy_years", "temporal_bone_lab_years"];
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

    function check(name, text, extra = "") {
      return `<label class="survey-check"><input type="checkbox" name="${name}" ${extra}>${text}</label>`;
    }

    function numberField(name, label, unit, help, { integer = true, na = false } = {}) {
      return `<div class="survey-field" data-measurement="${name}"><label for="survey-${name}">${label}</label>
        <div class="survey-number"><input id="survey-${name}" name="${name}" type="number" min="0" step="${integer ? "1" : "any"}" inputmode="${integer ? "numeric" : "decimal"}" placeholder="Enter estimated ${unit === "years" ? "years" : "number"}"><span>${unit}</span></div>
        <p class="survey-help">${help}</p><div class="survey-flags">${check(`${name}_unknown`, "Not sure", `data-measurement-status="${name}"`)}${na ? check(`${name}_na`, "Not applicable", `data-measurement-status="${name}"`) : ""}</div></div>`;
    }

    function yesNo(name, label, help) {
      return `<fieldset class="survey-field"><legend>${label}</legend><p class="survey-help survey-before-options">${help}</p><div class="survey-inline-options">${[["yes", "Yes"], ["no", "No"], ["not_sure", "Not sure"]].map(([value, text]) => `<label><input type="radio" name="${name}" value="${value}">${text}</label>`).join("")}</div></fieldset>`;
    }

    function backgroundMarkup() {
      return `<div class="survey-background">
        <section class="survey-section" aria-labelledby="survey-about"><div class="survey-section-title"><h3 id="survey-about"><span>01</span>About you</h3><span>Age and gender are optional.</span></div>
          <fieldset class="survey-role-field"><legend>Current role</legend><div class="survey-role-options">${Object.entries(roles).map(([value, role]) => `<label><input type="radio" name="role" value="${value}"><span>${role.label}</span></label>`).join("")}</div></fieldset>
          <div class="survey-grid">
            <div class="survey-field"><label for="survey-age">Age <span class="survey-optional">Optional</span></label><div class="survey-number"><input id="survey-age" name="age" type="number" min="1" max="120" step="1" placeholder="Enter age" inputmode="numeric"><span>years</span></div>${check("age_declined", "Prefer not to answer")}</div>
            <div class="survey-field"><label for="survey-gender">Gender <span class="survey-optional">Optional</span></label><select id="survey-gender" name="gender"><option value="">Select an option</option><option value="woman">Woman</option><option value="man">Man</option><option value="non_binary">Non-binary</option><option value="self_describe">Self-describe</option><option value="prefer_not">Prefer not to answer</option></select><div id="survey-gender-detail" class="hidden"><label for="survey-gender-description">Self-description</label><input id="survey-gender-description" name="gender_self_description" type="text" maxlength="100" placeholder="Describe in your own words"></div></div>
          </div>
        </section>
        <section class="survey-section" aria-labelledby="survey-training"><div class="survey-section-title"><h3 id="survey-training"><span>02</span>Training and surgical experience</h3></div><div class="survey-grid">
          <div class="survey-field"><label id="survey-stage-label" for="survey-stage">Current training year</label><div class="survey-number"><input id="survey-stage" name="training_stage" type="number" min="1" step="1" inputmode="numeric" placeholder="Select your role first" disabled><span id="survey-stage-unit">year</span></div><p id="survey-stage-help" class="survey-help">The question adapts to your current role.</p>${check("training_stage_unknown", "Not sure", "disabled")}</div>
          ${numberField("otolaryngology_training_months", "Completed otolaryngology training", "months", "Include residency and fellowship. Exclude medical school and independent practice.", { na: true })}
          ${numberField("clinical_mastoidectomy_years", "Clinical mastoidectomy experience", "years", "Approximate years since you first personally performed mastoidectomy drilling, before this procedure. Enter 0 for no prior experience.", { integer: false })}
          ${numberField("temporal_bone_lab_years", "Temporal bone laboratory experience", "years", "Approximate years since your first hands-on temporal bone laboratory training, before this procedure. Exclude virtual simulation.", { integer: false })}
        </div></section>
        <section class="survey-section" aria-labelledby="survey-simulation"><div class="survey-section-title"><h3 id="survey-simulation"><span>03</span>VR and simulator experience</h3></div><div class="survey-grid">
          ${yesNo("vr_headset_experience", "Previous VR headset use", "For any purpose, including games, education, or work.")}
          ${yesNo("other_surgical_simulator_experience", "Other virtual surgical simulators", "Include screen-based and haptic simulators. Exclude the simulator used for this procedure.")}
          ${numberField("this_simulator_attempts", "Previous attempts in this simulator", "attempts", "Count earlier procedure attempts, including unfinished attempts. Exclude the procedure reviewed today.")}
          <div class="survey-entry-help"><strong>Estimating your experience</strong><p>Enter 0 for no prior experience. Years may include decimals, such as 0.5. Choose Not sure if you cannot estimate.</p></div>
        </div></section>
      </div>`;
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

    function preferenceMarkup(definition) {
      return `<fieldset class="survey-preference"><legend>${escape(definition.question)}</legend><div>${definition.options.map(option => `<label><input type="radio" name="preferred_view" value="${escape(option.value)}"><span>${escape(option.label)}</span></label>`).join("")}</div></fieldset>`;
    }

    function cacheStage() {
      if (!previousRole || !field("training_stage")) return;
      stageCache[previousRole] = { value: val("training_stage"), unknown: checked("training_stage_unknown") };
    }

    function updateBackground(roleChanged = false) {
      const role = chosen("role");
      if (roleChanged) cacheStage();
      const roleInfo = roles[role];
      if (roleChanged || previousRole !== role) {
        const saved = stageCache[role] || { value: "", unknown: false };
        field("training_stage").value = saved.value;
        field("training_stage_unknown").checked = saved.unknown;
      }
      previousRole = role;
      root.querySelector("#survey-stage-label").textContent = roleInfo?.stage || "Current training year";
      root.querySelector("#survey-stage-unit").textContent = role === "resident" ? "PGY" : "years";
      root.querySelector("#survey-stage-help").textContent = role === "attending"
        ? "Count time since beginning independent practice. Decimals are welcome."
        : roleInfo ? "Enter your current training year." : "The question adapts to your current role.";
      field("training_stage").placeholder = roleInfo ? "Enter year" : "Select your role first";
      field("training_stage").min = roleInfo?.min ?? 1;
      field("training_stage").step = role === "attending" ? "any" : "1";
      field("training_stage").disabled = !roleInfo || checked("training_stage_unknown");
      field("training_stage_unknown").disabled = !roleInfo;
      root.querySelector('[data-measurement="otolaryngology_training_months"]').classList.toggle("hidden", role === "medical_student");
      field("age").disabled = checked("age_declined");
      root.querySelector("#survey-gender-detail").classList.toggle("hidden", val("gender") !== "self_describe");
      measurementNames.forEach(name => { field(name).disabled = checked(`${name}_unknown`) || checked(`${name}_na`); });
    }

    function measurement(name, allowNA = false) {
      if (checked(`${name}_unknown`)) return { status: "not_sure", value: null };
      if (allowNA && checked(`${name}_na`)) return { status: "not_applicable", value: null };
      const raw = val(name);
      if (!numeric(raw, 0, !yearNames.includes(name))) return null;
      return { status: "provided", value: Number(raw) };
    }

    function backgroundAnswers() {
      const role = chosen("role"), info = roles[role];
      if (!info) return null;
      const stage = checked("training_stage_unknown") ? { status: "not_sure", value: null, unit: info.unit }
        : numeric(val("training_stage"), info.min, role !== "attending")
          ? { status: "provided", value: Number(val("training_stage")), unit: info.unit } : null;
      const age = checked("age_declined") || val("age") === "" ? null : Number(val("age"));
      if (age !== null && (!numeric(val("age"), 1) || age > 120)) return null;
      if (val("gender") === "self_describe" && !val("gender_self_description").trim()) return null;
      const answers = {
        role, age, age_status: checked("age_declined") ? "prefer_not" : age === null ? "unanswered" : "provided",
        gender: val("gender") || null,
        gender_self_description: val("gender") === "self_describe" ? val("gender_self_description").trim() : "",
        training_stage: stage,
        otolaryngology_training_months: role === "medical_student" ? { status: "not_applicable", value: null } : measurement("otolaryngology_training_months", true),
        clinical_mastoidectomy_years: measurement("clinical_mastoidectomy_years"),
        temporal_bone_lab_years: measurement("temporal_bone_lab_years"),
        vr_headset_experience: chosen("vr_headset_experience"),
        other_surgical_simulator_experience: chosen("other_surgical_simulator_experience"),
        this_simulator_attempts: measurement("this_simulator_attempts"),
      };
      if (!stage || measurementNames.some(name => !answers[name]) || !answers.vr_headset_experience || !answers.other_surgical_simulator_experience) return null;
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

    function updateValidity() {
      const valid = Boolean(answers());
      root.querySelector("#surveySubmit").disabled = pending || !valid;
      const progress = root.querySelector("#surveyProgress");
      if (context.flow.stage === "self_assessment") {
        const count = context.definitions.self_assessment.items.filter(item => chosen(item.id)).length;
        root.querySelector("#survey-answer-count").textContent = `${count} of 6 answered`;
        progress.textContent = valid ? "All items answered." : "Please answer all six items.";
      } else progress.textContent = valid ? "Ready to continue." : "Please complete the questions to continue.";
      if (context.flow.stage === "background" && !checked("age_declined") && val("age") !== "" && (!numeric(val("age"), 1) || Number(val("age")) > 120)) {
        progress.textContent = "Please enter an age from 1 to 120 in whole years.";
      }
      if (context.flow.stage === "background" && val("gender") === "self_describe" && !val("gender_self_description").trim()) {
        progress.textContent = "Please enter a self-description or choose another gender option.";
      }
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
        title = "Pre-session questionnaire"; eyebrow = "Before your feedback review";
        subtitle = "Please describe your experience before the procedure you are reviewing today. Estimates are welcome.";
        markup = backgroundMarkup();
      } else if (stage === "self_assessment") {
        title = value.definitions.self_assessment.title; eyebrow = `${["Before feedback", "After first feedback", "After second feedback"][value.flow.assessment_round]} · Assessment ${value.flow.assessment_round + 1} of 3`;
        subtitle = value.definitions.self_assessment.instructions;
        markup = selfAssessmentMarkup(value.definitions.self_assessment);
      } else if (stage === "questionnaire") {
        title = "Feedback questionnaire"; eyebrow = `Feedback format · ${viewLabel(value.flow.current_view)}`;
        subtitle = "Please rate the feedback format you just reviewed.";
        markup = feedbackMarkup(value.definitions.feedback);
      } else {
        title = "Your preference"; eyebrow = "After both feedback formats";
        subtitle = "Think about both feedback formats you reviewed.";
        markup = preferenceMarkup(value.definitions.preference);
      }
      root.className = `study-survey survey-${stage}`;
      root.innerHTML = `<div class="survey-page-title"><p class="eyebrow">${escape(eyebrow)}</p><h2 id="surveyTitle">${escape(title)}</h2><p>${escape(subtitle)}</p></div><form id="studySurveyForm" aria-labelledby="surveyTitle" novalidate>${markup}<div class="survey-submit-row"><p id="surveyProgress" aria-live="polite"></p><button id="surveySubmit" type="submit" class="primary-button" disabled>${stage === "background" ? "Continue to self-assessment" : stage === "preference" ? "Finish" : "Next"}</button></div><p id="surveyError" class="form-error hidden" role="alert"></p><p id="surveyDraftNotice" class="survey-draft-notice hidden">This browser could not save a draft. Keep this page open until you submit.</p></form>${stage === "self_assessment" ? '<p class="survey-research-note">Proposed OSATS-like research questionnaire.</p>' : ""}`;
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
        if (target.name === "age_declined" && target.checked) field("age").value = "";
        if (target.name === "training_stage_unknown" && target.checked) field("training_stage").value = "";
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
