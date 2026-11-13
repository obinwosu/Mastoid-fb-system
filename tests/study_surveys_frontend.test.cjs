const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

// Minimal DOM adapter for exercising the production form's public API and events.
function element() {
  const classes = new Set();
  return { style: {}, dataset: {}, value: "", checked: false, disabled: false, isConnected: true,
    classList: { add: value => classes.add(value), remove: value => classes.delete(value),
      toggle(value, active) { if (active) classes.add(value); else classes.delete(value); } },
    matches: () => true };
}

function harness() {
  const storage = new Map(), handlers = {}, ids = new Map();
  let inputs = [], html = "";
  const root = { ...element(), addEventListener: (type, handler) => { handlers[type] = handler; },
    set innerHTML(value) {
      html = value;
      ids.clear();
      for (const match of value.matchAll(/\bid="([^"]+)"/g)) ids.set(match[1], element());
      inputs = Array.from(value.matchAll(/<input\b([^>]+)>/g), match => {
        const attrs = Object.fromEntries(Array.from(match[1].matchAll(/([\w-]+)="([^"]*)"/g), row => [row[1], row[2]]));
        return Object.assign(element(), attrs);
      });
    },
    get innerHTML() { return html; },
    querySelector(selector) {
      if (selector.startsWith("#")) return ids.get(selector.slice(1)) || null;
      const name = selector.match(/^\[name="([^"]+)"\](:checked)?$/);
      if (name) return inputs.find(input => input.name === name[1] && (!name[2] || input.checked)) || null;
      if (selector === "video") return null;
      throw new Error(`Unhandled form selector: ${selector}`);
    },
    querySelectorAll(selector) {
      if (["input[name], select[name]", "input,select,button"].includes(selector)) return inputs;
      throw new Error(`Unhandled form list selector: ${selector}`);
    },
  };
  const context = { window: { sessionStorage: { getItem: key => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) } } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../apps/expert_study/surveys.js"), "utf8"), context);
  const submissions = [];
  const form = context.window.StudySurveys.create({ root, onSubmit: value => submissions.push(value) });
  const choose = (name, value) => {
    const options = inputs.filter(input => input.name === name);
    const selected = options.find(input => input.value === String(value));
    assert.ok(selected, `Missing choice ${name}: ${value}`);
    options.forEach(input => { input.checked = input === selected; });
    handlers.change({ target: selected });
  };
  return { root, form, choose, storage, submissions, submit: () => handlers.submit({ preventDefault() {} }) };
}

const agreement = ["Strongly disagree", "Disagree", "Neutral", "Agree", "Strongly agree"]
  .map((label, value) => ({ value, label }));
const overall = { id: "overall_performance", label: "Overall performance",
  scale: ["Very poor", "Poor", "Fair", "Good", "Very good"].map((label, i) => ({ value: i + 1, label })) };
const baselineOverall = { ...overall,
  text: "Based on your own impression, how would you rate your overall performance in the recorded procedure?" };
const postOverall = { ...overall,
  text: "After reviewing this feedback, how would you now rate your overall performance in the recorded procedure?" };
const definitions = {
  version: "mastoid_surveys_v5_3_experience_items_closing",
  background: { fields: { role: {}, training_stage: {}, cortical_mastoidectomies: {},
    vr_experience: { label: "Before today’s session, how often had you used a VR headset?",
      options: ["never", "occasionally", "frequently"], option_labels: ["Never", "Occasionally", "Frequently"] } } },
  baseline: { title: "Before feedback", instructions: "Rate your recorded procedure before seeing feedback.", items: [baselineOverall] },
  feedback: { items: [
    { id: "clarity", label: "Clarity", text: "The feedback was easy to understand." },
    { id: "actionability", label: "Actionability", text: "The feedback helped me identify specific actions I could take to improve my performance." },
    { id: "new_insight", label: "New insight",
      text: "To what extent did this feedback help you recognize aspects of your performance that you had not noticed before?",
      scale: ["Not at all", "A little", "Somewhat", "Quite a bit", "A great deal"].map((label, value) => ({ value, label })) },
    postOverall,
  ], scale: agreement },
};
const payload = (stage, step = stage, instrument = definitions) => ({ participantId: "TEST", runId: "recording-one",
  definitions: instrument, flow: { stage, step_id: step, current_view: "event-based" } });
const plain = value => JSON.parse(JSON.stringify(value));

test("new background requires VR while the stored v3 background stays unchanged", () => {
  const { root, form, choose } = harness();
  form.show(payload("background"));
  choose("role", "resident"); choose("pgy_resident", 2); choose("cortical_mastoidectomies", "0");
  assert.equal(form.answers(), null);
  assert.equal(root.querySelector("#surveyProgress").textContent, "3 of 4 questions answered");
  assert.match(root.innerHTML, /Before today’s session, how often had you used a VR headset\?/);
  assert.doesNotMatch(root.innerHTML, /value="yes"|value="no"|value="not_sure"/);
  for (const value of ["never", "occasionally", "frequently"]) {
    choose("vr_experience", value);
    assert.equal(form.answers().vr_experience, value);
  }
  assert.equal(root.querySelector("#surveySubmit").disabled, false);
  root.querySelector('[name="vr_experience"]:checked').value = "yes";
  assert.equal(form.answers(), null);
  const old = structuredClone(definitions);
  old.version = "mastoid_surveys_v3_resident";
  old.feedback.items[0].text = "The feedback was clear to me.";
  delete old.background.fields.vr_experience;
  form.show(payload("background", "background", old));
  choose("role", "fellow"); choose("pgy_fellow", 6); choose("cortical_mastoidectomies", "1_5");
  assert.doesNotMatch(root.innerHTML, /name="vr_experience"/);
  assert.deepEqual(Object.keys(form.answers()), ["role", "training_stage", "cortical_mastoidectomies"]);
});

test("baseline requires a numeric rating from one through five", () => {
  const { root, form, choose, submit, submissions } = harness();
  form.show(payload("baseline"));
  assert.ok(root.innerHTML.includes(baselineOverall.text));
  assert.ok(!root.innerHTML.includes(postOverall.text));
  assert.equal(form.answers(), null);
  assert.equal(root.querySelector("#surveySubmit").disabled, true);
  submit();
  assert.equal(submissions.length, 0);
  assert.doesNotMatch(root.innerHTML, /Unable to judge|unable_to_judge/);
  choose("overall_performance", 1);
  submit();
  assert.deepEqual(plain(submissions[0]), { overall_performance: 1 });
  assert.deepEqual(plain(form.answers()), { overall_performance: 1 });
  assert.doesNotMatch(root.innerHTML, /name="overall_performance" value="0"/);
  const selected = root.querySelector('[name="overall_performance"]:checked');
  for (const invalid of ["0", "6", "unable_to_judge"]) {
    selected.value = invalid;
    assert.equal(form.answers(), null);
    submit();
    assert.equal(submissions.length, 1);
  }
});

test("post questionnaire preserves agreement zero and uses a different overall scale", () => {
  const { root, form, choose } = harness();
  form.show(payload("questionnaire", "questionnaire_0"));
  assert.ok(root.innerHTML.includes(postOverall.text));
  assert.ok(!root.innerHTML.includes(baselineOverall.text));
  const questions = root.innerHTML.match(/<fieldset class="survey-feedback-question">.*?<\/fieldset>/g);
  assert.match(questions[0], /Strongly disagree/);
  assert.match(questions[1], /Strongly agree/);
  assert.match(questions[2], /To what extent did this feedback help/);
  for (const label of ["Not at all", "A little", "Somewhat", "Quite a bit", "A great deal"]) {
    assert.ok(questions[2].includes(label));
  }
  assert.doesNotMatch(questions[2], /Strongly disagree|Strongly agree/);
  choose("clarity", 0); choose("actionability", 4); choose("new_insight", 0);
  assert.equal(form.answers(), null);
  choose("overall_performance", 5);
  assert.deepEqual(plain(form.answers()), { clarity: 0, actionability: 4, new_insight: 0, overall_performance: 5 });
  assert.doesNotMatch(root.innerHTML, /Unable to judge|unable_to_judge/);
  assert.equal((root.innerHTML.match(/class="survey-feedback-question"/g) || []).length, 4);
  form.show(payload("questionnaire", "questionnaire_1"));
  assert.ok(root.innerHTML.includes(postOverall.text));
  assert.equal(form.answers(), null);
});

test("drafts restore the same round but never prefill the next round, case or version", () => {
  const { form, choose } = harness();
  form.show(payload("baseline")); choose("overall_performance", 3);
  form.show(payload("questionnaire", "questionnaire_0"));
  assert.equal(form.answers(), null);
  choose("clarity", 1); choose("actionability", 2); choose("new_insight", 3); choose("overall_performance", 4);
  form.show(payload("questionnaire", "questionnaire_1"));
  assert.equal(form.answers(), null);
  form.show(payload("questionnaire", "questionnaire_0"));
  assert.equal(form.answers().overall_performance, 4);
  form.show({ ...payload("questionnaire", "questionnaire_0"), runId: "recording-two" });
  assert.equal(form.answers(), null);
  form.show(payload("questionnaire", "questionnaire_0", { ...definitions, version: "future-version" }));
  assert.equal(form.answers(), null);
});

test("stored two-item post instrument still renders and submits exactly its two answers", () => {
  const { root, form, choose } = harness();
  const old = structuredClone(definitions);
  old.version = "mastoid_surveys_v3_resident";
  old.feedback.items[0].text = "The feedback was clear to me.";
  old.feedback.items = old.feedback.items.slice(0, 2);
  form.show(payload("questionnaire", "questionnaire_0", old));
  choose("clarity", 0); choose("actionability", 4);
  assert.deepEqual(plain(form.answers()), { clarity: 0, actionability: 4 });
  assert.doesNotMatch(root.innerHTML, /name="overall_performance"|name="new_insight"/);
});

test("stored v4 keeps binary VR and insight agreement without reusing its answers in v5", () => {
  const { root, form, choose } = harness();
  const old = structuredClone(definitions);
  old.version = "mastoid_surveys_v4_reflection";
  old.feedback.items[0].text = "The feedback was clear to me.";
  old.background.fields.vr_experience = {
    label: "Have you used a virtual reality (VR) headset before today?",
    options: ["yes", "no", "not_sure"], option_labels: ["Yes", "No", "Not sure"],
  };
  old.feedback.items[2] = { id: "new_insight", label: "New insight",
    text: "This feedback helped me recognize aspects of my performance that I had not noticed before." };
  form.show(payload("background", "background", old));
  choose("role", "resident"); choose("pgy_resident", 3); choose("cortical_mastoidectomies", "0");
  for (const value of ["yes", "no", "not_sure"]) {
    choose("vr_experience", value);
    assert.equal(form.answers().vr_experience, value);
  }
  assert.match(root.innerHTML, /Have you used a virtual reality/);
  assert.doesNotMatch(root.innerHTML, /Occasionally|Frequently/);
  form.show(payload("background"));
  assert.equal(form.answers(), null);
  assert.equal(root.querySelector('[name="vr_experience"]:checked'), null);
  form.show(payload("questionnaire", "questionnaire_0", old));
  const insight = root.innerHTML.match(/<fieldset class="survey-feedback-question">.*?<\/fieldset>/g)[2];
  assert.match(insight, /This feedback helped me recognize/);
  assert.match(insight, /Strongly disagree/);
  assert.doesNotMatch(insight, /Not at all|A great deal/);
  choose("clarity", 0); choose("actionability", 4); choose("new_insight", 0); choose("overall_performance", 3);
  assert.equal(form.answers().new_insight, 0);
  form.show(payload("questionnaire", "questionnaire_0"));
  assert.equal(form.answers(), null);
  assert.equal(root.querySelector('[name="new_insight"]:checked'), null);
});

test("baseline route waits for real analysis before rendering the performance question", () => {
  const source = fs.readFileSync(path.join(__dirname, "../apps/expert_study/app.js"), "utf8");
  const calls = [], elements = new Map();
  const state = { participantId: "TEST", start: { run_id: "recording-one", surveys: definitions }, experience: null };
  const context = vm.createContext({ state,
    document: { getElementById: id => {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    } },
    pauseReviewMedia() {}, studySurveys: { hide() {}, show: value => calls.push(["survey", value]) },
    showReady: () => calls.push(["wait"]), setHeaderStatus: value => calls.push(["header", value]),
    reviewViewLabel: value => value === "summary" ? "Metric-Based Feedback" : "Video-Based Feedback",
    escapeHtml: value => value, REVIEW_DESCRIPTIONS: {},
  });
  vm.runInContext(source.slice(source.indexOf("function setReviewFlow(flow)"), source.indexOf("function escapeHtml(value)")), context);
  vm.runInContext(source.slice(source.indexOf("function renderReviewFlow()"), source.indexOf("async function advanceReviewFlow")), context);
  context.setReviewFlow({ schema: "sequential_review_v2", stage: "baseline", step_id: "baseline",
    order: ["event-based", "summary"], index: 0, current_view: null });
  context.renderReviewFlow();
  assert.deepEqual(calls, [["wait"]]);
  state.experience = { media: { available: true } };
  context.renderReviewFlow();
  assert.equal(calls.at(-1)[0], "survey");
  assert.equal(calls.at(-1)[1].flow.stage, "baseline");
  assert.equal(calls.at(-1)[1].runId, "recording-one");
});
