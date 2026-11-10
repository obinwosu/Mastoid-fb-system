# Resident study: changes from the original feedback system

Running list for the FIVRS resident study (branch `resident-study`). It records what
was removed or changed from the original design, and open questions to discuss with
the feedback system's author. Started 2026-11-07.

## Removed (can be brought back)

| What | Where | How to restore |
| --- | --- | --- |
| The three self-assessments (before feedback, after each view) | `SELF_ASSESSMENTS = False` in `src/evidence_system/study_surveys.py` | Set it to `True`. Tests still cover the full protocol. |
| Background questions: age, gender, completed training months, clinical mastoidectomy years, VR headset use, other simulators, earlier attempts on this simulator, temporal bone lab experience; the "medical student" role | Background schema `mastoid_surveys_v3_resident` in `study_surveys.py`, `apps/expert_study/surveys.js` | Earlier versions are in git history (`mastoid_surveys_v1`, `v2_resident`). |
| Study ID entry page in normal use (the launcher opens the questionnaire with the ID in the address) | `scripts/study_gui/study_mode.py` (`open_feedback`) | The page still works if opened without an ID. |
| Pilot "Start automatic session analysis" screen and its progress animation | `showReady()` in `apps/expert_study/app.js` | Still shown if loading the feedback fails. |
| Summary page: "How to read these comparisons" explainer | `apps/summary/index.html` | Original wording is in git history. |
| Summary page: phase durations on the phase buttons; the "Jump to this phase" strip | `apps/summary/` | Git history. |
| Event-based page: phase bands and labels on the Procedure map timeline (the timeline itself now sits under the video as a slim strip of dots) | `apps/expert_study/index.html`, `styles.css` (`.phase-map` hidden) | Remove the `.phase-map` rule. |
| Event-based page: "Coaching plan" heading, "What happened", "Try this next" (actionable advice), "Go to first moment", the separate moments list under the video, "How this summary is supported" | `apps/recommendations/event-review.js`, `apps/expert_study/styles.css` | Git history. |
| End page: "Case complete" label, "Expert workflow experience complete" wording and the completion code. It now shows a check mark, "Session Complete", "Thank you for participating in this study." and a "Return to the simulator study window" button (POST `/api/expert-study/return-to-simulator`; the launcher brings its window forward) | `apps/expert_study/index.html`, `app.js`, `src/evidence_system/web.py`, `scripts/study_gui/study_mode.py` | Git history. |
| Event-based page: "Go to key frame" / "Play context" buttons, and the key-frame still image with "Return to video" | `event-review.js`, `apps/expert_study/app.js` | Safety moments now pause the video at the event's time; motion moments play. |

## Changed

- Background questionnaire (`mastoid_surveys_v3_resident`): role (Resident / Fellow /
  Attending); residents must pick PGY 1-5, fellows PGY 6 or 7 (stored as unit `pgy`),
  attendings type whole years in practice; "Number of prior cortical mastoidectomies
  performed" (0 / 1-5 / 6-10 / 11-20 / >20). Every answer is required (no "Not sure").
- The background questionnaire opens as soon as the trial ends, while the analysis runs
  (`run.py serve-intake`, `IntakeService` in `web.py`). Its answers go to the participant
  profile; the launcher then restarts the full server on the same port, the page reloads
  on its own, and the full flow skips the background step. The intake response is stored
  with run ID `analysis-pending`.
- Feedback questionnaire: agreement option 2 is labelled "Neutral".
- Preference page: just "Which feedback format did you prefer?" with Event-based feedback
  and Summary-based feedback (definitions: "Highlighting the specific moments where your performance deviated from expert performance" / "Contextualizing your performance metrics against expert performance metrics"), A combination of both
  (new value `combination`), and No preference. The "After both feedback formats" label,
  "Your preference" title and instruction line are removed.
- Both review pages: Next stays hidden until every phase (summary) or every moment
  (event-based) has been reviewed; review progress is not saved across a page reload.
- Event-based timeline dots, topic stripes and moment buttons all use one colour (no
  per-topic colours, no legend). Clicking a dot acts like clicking that moment.
- Event-based topics use the summary page's names: Critical Structure Violation, Poor
  Drill Visibility, Tool speed, Stroke length. Topic definitions are plain-language
  wording chosen by the study team.
- Tool speed moments are not flagged when at least 20% of the 5-second window was spent
  moving the microscope or holding the stylus clutch
  (`USER_CONTROL_MAX_FRACTION` in `src/evidence_system/motion_comparison.py`). The
  FIVRS recorder now saves `user_controls` (stylus clutch, camera clutch, microscope
  button) on every frame; older recordings use camera movement only. Summary-page
  averages are unchanged.
- Motion moments play from the window start through 5 s after the window
  (`MOMENT_CONTEXT_AFTER_S` in `apps/expert_study/app.js`).
- Two validation checks relaxed for current FIVRS files
  (`src/evidence_system/anatomy_catalog.py`): voxel-spacing tolerance 1e-6 mm (files
  differ from the 2022 calibration by about 6e-9 mm), and an R4 rotation is accepted
  from a commented ADF line holding the same angles. Fitted rotations still never count.

## To discuss with the feedback system's author

1. **5-second motion windows.** Why 5 s? A 5-second window is a narrow unit for
   surgical behaviour. Changing it needs the expert reference rebuilt from the raw R4
   recordings.
2. **Persistence for motion moments.** Earlier versions needed three consecutive slow
   windows (at least 7 s); the 2026-11-04 version flags a single window. Consider
   requiring two or more consecutive windows to reduce one-off flags. This works with
   the existing 5-second reference.
3. **Phase boundaries.** What rules did annotators use for early drilling / antrum /
   incus? How accurate is the phase model on held-out recordings, and on short or
   novice sessions? (A 1 min 46 s desktop test was labelled as reaching the incus at
   1:11.)
4. **Expert distributions for safety measures.** Per expert recording and phase:
   violation counts and seconds of poor drill visibility, using the same rules as for
   residents, so the summary page can chart them like the motion metrics.
5. **Repository hygiene test.** `test_candidate_tree_contains_only_publishable_source_files`
   fails on a Windows path in `README.md` (line 80); not caused by these changes.

## Author follow-up and additional review (2026-11-09)

This section records the current decisions and evidence without replacing the
historical questions above. The application questionnaire, page content, visibility
wording, active five-second reference and event thresholds remain unchanged.

### Questionnaire proposal: keep two questions and add two

Prepared a private, editable [questionnaire discussion draft](https://chatgpt.com/space/page_680efcb4e2448191b3e6bbfa1f6ee201).
It is a proposal, not an activated application page or validated scale. The existing
Clarity and Actionability questions would remain, with two additional questions
after **each** feedback format:

1. **New insight:** "This feedback helped me recognize aspects of my performance
   that I had not noticed before." Use the existing agreement scale:
   0 Strongly disagree, 1 Disagree, 2 Neutral, 3 Agree, 4 Strongly agree.
2. **Overall performance:** "How would you rate your overall performance in the
   recorded procedure?" Proposed anchors: 1 Very poor, 2 Poor, 3 Fair, 4 Good,
   5 Very good; a separate Unable to judge response is missing, not zero.

Ask the identical Overall performance question once **before any feedback**.
This gives one baseline rating and four questions after each format; it does not
restore the old six-item self-assessment at all three time points.

Together these questions address reported new insight and change in self-rating.
A rating decrease can reflect reassessment, and an unchanged overall rating can
coexist with new insight. Neither answer establishes improved surgical skill or
more accurate self-assessment. Accuracy needs an independent expert assessment
using comparable criteria. Treat the ratings as exploratory ordinal outcomes;
report transitions, signed changes and missingness without combining the four
items into a total score. The second format follows prior feedback, so its effect
is additional exposure rather than an independent first-exposure effect.

Before any implementation, settle the rating anchors, missing-response handling,
order balancing/randomization and analysis plan; introduce a new questionnaire
version and preserve all previous responses. Current order assignment is study
entry selection, not automatic randomization. Visibility wording stays as agreed.

### Responses to the five original questions

- **Window meaning:** five seconds is the local aggregation unit. Stroke length
  is measured over each complete eligible recorded stroke and assigned by its
  endpoint on (window start, window end]; strokes are not cut into five-second
  pieces. The current event detector also uses these windows, so aggregation
  duration and trigger persistence are separate design questions. Whole-phase
  summary statistics are a third aggregation, calculated from source observations.
- **Additional ten-second result:** rebuilt from hash-verified R4 frame and phase
  sources, with ten-second nonoverlapping native-time windows. Following the
  author's clarification, v2 also retains an entire observed phase shorter than
  ten seconds using its actual duration; two early-phase samples (4.36/9.64 s)
  are restored. Longer-phase incomplete tails retain the original omission rule.
  The original strict-ten-second result is preserved. See the
  [distribution comparison](motion-window-distribution-review.md) for artifacts,
  weighting, counts and numeric results. This is an extra offline reference only.
  It does not change the active five-second UI or evaluate a ten-second detector.
- **Persistence:** two/three current nonoverlapping five-second windows span
  10/15 seconds; the old overlapping three-window rule spanned seven seconds.
  A recount of one historical saved output gives Speed 13/6/4 and Length 19/12/4
  episodes at minimum 1/2/3 windows. That output predates clutch suppression;
  it is not a current replay or evidence that false positives decreased.
- **Phase accuracy:** use the author's requested existing P2T1-only excluded
  recording-holdout analysis: 87.44% over 3,073 known one-second endpoints in
  14 recordings. Other P2 recordings remain included, and the original full
  report is preserved. The same deployed checkpoint was used; no retraining.
  Existing ordinal boundary diagnostics have median absolute error 9.876 s
  across 27 observable boundaries. They do not validate exact anatomical onset,
  the clinical annotation rubric, or the reported 106-second novice recording.
- **Safety/visibility distributions:** current-case Ray-v3 Poor Drill Visibility
  can be computed when its bound source requirements are met; an existing
  completed build verifies that path. The older RGB-depth detector is a separate
  path and is not evaluable without depth. A matched expert safety reference is
  still absent: it needs original expert sources processed with the same
  per-structure safety and Ray rules. Old CSV proximity/visibility fields are
  not substitutes. No wording or safety threshold is changed here.
- **Hygiene:** the README URL false positive was fixed in the merge. The
  subsequent complete suite passed 547 tests; the original question is historical.

### Additional issues to address before interpreting a study

1. **Old motion-result compatibility: reproduced, unresolved.** The incoming
   control filter adds a `user_control` input field and a
   `user_control_fraction` window field while retaining the existing window
   schema. `web.py` rebuilds saved windows with current code and requires exact
   input-digest/window equality. With identical synthetic observations and
   protocol, a current sidecar is available, but one built by pre-change commit
   `83e38b6` returns `current_motion_source_unbound`. This can hide motion
   comparisons for archived runs; it does not demonstrate a new-recording
   failure. Receipt:
   `.local/validation/ten_second_distribution_20261109_v1/archive_compatibility.json`.
   Suggested repair: explicitly version the control enrichment, verify historical
   fields with the historical contract, then derive current control-aware windows
   from the already-bound observations without rewriting the archive or loosening
   source identity checks. Validate both old and new recordings.
2. **Study documentation differs from current behavior.** README still describes
   three six-item self-assessments even though `SELF_ASSESSMENTS = False`.
   Its older statement that background intake is not implemented also needs
   reconciliation: `serve-intake` and `IntakeService` now exist. The referenced
   `scripts/study_gui/study_mode.py` launcher is outside this application checkout,
   so this review does not independently confirm the whole external launch flow.
3. **Viewed is currently an interaction proxy.** Opening phases/moments enables
   progression; it does not demonstrate watching the whole clip or understanding
   feedback. Review sets are browser-memory state and reset on reload. Report
   this limitation and define any future persisted exposure/duration logging
   before using review completion as an analysis variable.
4. **Questionnaire versions and exposure order matter.** Existing participant
   profiles can reuse previously collected background data. Preserve their
   questionnaire versions instead of treating old and new fields as identical.
   Keep format order in analysis; a single overall rating can miss a change in
   one particular skill even when it reduces participant burden.

This follow-up executed reference aggregation, numerical checks and a synthetic
compatibility reproduction. It did not alter source data, fit a model, rerun
resident cases, restart a study server, change participant responses or establish
new clinical review. The compatibility repair and questionnaire activation remain
future work.
