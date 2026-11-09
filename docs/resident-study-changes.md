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
