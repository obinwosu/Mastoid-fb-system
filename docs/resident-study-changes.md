# Resident study: changes from the original feedback system

Running list for the FIVRS resident study (branch `resident-study`). It records what
was removed or changed from the original design, and open questions to discuss with
the feedback system's author. Started 2026-11-07.

## Page-by-page usability pass (2026-11-11, Obi)

- Preference page no longer scrolls sideways. `#surveyPanel .survey-preference legend`
  (surveys.css) gave the screen-reader-only question legend full width and margins,
  so it spilled ~130 px past the window. The rule now skips `legend.sr-only`.
- Study ID entry page: button text "Enter study" -> "Begin".
- Background, Attending: removed the "Whole years." hint under "Years in practice".
- Baseline page (survey version `mastoid_surveys_v5_2_task_wording`; saved v5/v5_1
  sessions keep their own wording): title "Before feedback" -> "Pre-Feedback
  Self-Assessment" (also in the header); instruction line removed; the single item no
  longer shows its "Overall performance" label (post questionnaires still show
  "Q4 · Overall performance"); question now reads "Based on your own impression, how
  would you rate your overall performance on the task: safely performing a cortical
  mastoidectomy with identification of the incus pointer?" The post-feedback Overall
  question (both formats) names the same task: "After reviewing this feedback, how
  would you now rate your overall performance on the task: safely performing a
  cortical mastoidectomy with identification of the incus pointer?"
- Back button (top left, "← Back"), one step at a time and never back past feedback:
  Pre-Feedback Self-Assessment -> Background, each questionnaire -> the feedback page
  it rates, Preference -> last questionnaire. No Back on Background, either feedback
  page or the end page, so the pre-feedback rating and the first format's ratings
  cannot be changed after later feedback. Server: `ExpertStudyStore.go_back_review`,
  `POST /api/expert-study/review-back`, `study_surveys.BACK_TARGET_STAGE` /
  `back_target`. An answer on the page returned to is withdrawn, not deleted: it
  moves to `review_flow.withdrawn_responses` (with `withdrawn_at`), is removed from
  the participant profile for background/preference, and is filled into the form
  again (`previous_answers` in the public flow). Transitions record
  `reason: participant_back`; event `review_step_back`. A feedback page reached by
  Back is not re-gated (`revisit`), Next says "Select Next to return to the
  questionnaire." Tests: tests/test_back_navigation.py.
- Page instructions are bold study green. Background: "Answer each question, then
  select Continue." Pre-Feedback Self-Assessment: "Choose one rating, then select
  Next." (survey definition, still v5_2). Questionnaires: "Answer all four questions
  about the <format> feedback you just reviewed, then select Next." Preference:
  "Choose one option, then select Finish." Summary (Obi's wording): "Click each phase button (Early Drilling, Antrum, Incus) and review your Safety and Drilling Motion Metrics. Once you have reviewed metrics across all phases, select Next." Video-Based (Obi's wording): "Click each metric below to open its list of events. Once you have reviewed all events, select Next." On both review pages the box shows an underlined "How to Review this Page" heading, then each sentence on its own line, at 16 px (was 14 px). The review page title is larger (30 px, was 22 px), with its short grey description on the same line (it wraps underneath on narrow windows). The phase label is "Early Drilling" everywhere (was "Early drilling"). Topic headings on the Summary and Event pages read "Safety Metrics" and "Drilling Motion Metrics". Questionnaire pages: the small green "SUMMARY-BASED / EVENT-BASED FEEDBACK" label is gone; the title reads "Summary-Based Feedback Questionnaire" or "Event-Based Feedback Questionnaire" (front end only, survey items unchanged), and the instruction sits in the same light green box as on the review pages. The Background, Pre-Feedback Self-Assessment and Preference pages use the same box. Instrument **v5_3** (`mastoid_surveys_v5_3_experience_items_closing`): the VR item now reads "Describe your experience with VR headsets prior to today's session." (same key `vr_experience`; its options are now None / Minimal / Moderate / Extensive, stored as `none` / `minimal` / `moderate` / `extensive`), and two required items follow it with the same options: `vr_simulation_experience` ("Describe your experience with VR surgical simulation prior to today's session.") and `drilling_simulator_experience` ("Describe your experience with this drilling simulator prior to today's session."). Responses saved under earlier versions keep their own wording and fields. Also in v5_3, the baseline page is titled "Initial Self-Assessment"; its instruction box reads "Please rate your performance on the assigned task: safely drilling a cortical mastoidectomy with identification of the incus pointer." and its question is shortened to "Based on your own impression, how would you rate your overall performance?" (same 1-5 scale, same `overall_performance` key). The study page header and browser tab title read "User Perceptions on Performance Feedback in Simulation" (was "Session Feedback" / "FIVRS session feedback"). **Guide pulse** (`.guide-pulse` in `apps/recommendations/styles.css`): a soft pulsing blue outline marks the one thing to click next. Summary page: the unopened Safety / Drilling Motion section in the current phase, then the next phase not yet finished (study frame only, `embedded=1`). Event page: the next event without a check mark in the open metric, then the next metric with events still to view (only when `EventReview.create` gets `guide: true`, as the study page does; metrics with no events are skipped, as the Next gate does). When the page is fully reviewed, it moves to Next. It follows the same viewed state that unlocks Next, and holds still when the computer asks for reduced motion. Summary page Drilling Motion Metrics: each value now reads "Your average: 9.56 mm" (small grey label before the number), since it is the resident's mean over the phase. The same version adds a **Post Sim Reflection** page (no instruction box) (stage `closing`, step `closing`) after the preference question on the final case: "I would like to use this simulator to practice temporal bone drilling." (required; Yes / Neutral / No, stored as `yes` / `neutral` / `no`) and an optional free-text box ("Do you have any other comments, positive or negative, on your experience in the drilling simulator?"; up to 5000 characters, stored as typed). It is saved on the participant profile as `closing`, like `background` and `preference`. The preference page's button is now Next, and Finish moves to this page; Back from it returns to the preference question.
- Video-Based page (shared `apps/recommendations/event-review.js` + `styles.css`, so the
  standalone recommendations page changes too, except the note below):
  - Study page only: removed "Up to 4 moments per category, selected across all
    phases." (describes the method, not something the resident needs) and "These
    selected moments do not provide a complete assessment of each phase."
  - Moment rows have column headers Event # / Duration / Phase / Viewed?; columns
    reordered to rank, time range, phase, viewed; the viewed cell shows a check mark.
  - Chart under the video has a title: "Expert Distribution of <Metric>" (e.g. "Expert
    Distribution of Tool Speed").
  - The open category always shows its first listed moment: on page load, on opening
    a category and on changing the phase filter. That moment counts as viewed. Video card heading "Play context" -> "Event Video Clip".
  - Removed the "Representative window from m:ss–m:ss" line under the chart (shown
    when a moment's episode spanned several five-second windows).
  - Expert dots removed from that chart (and "Experts" from its legend); band, median
    and this-clip marker kept.
  - Tool speed sentence now depends on direction. Fast: "Faster isn’t always better. In
    this clip, your tool speed was higher than most experts' tool speed during this
    phase." Slow: "In this clip, your tool speed was slower than most experts' tool speed
    during this phase." Stroke length and removal rate use the same pattern: "In this
    clip, your mean stroke length was shorter/longer than most experts' mean stroke
    length during this phase." / "...your bone removal rate was lower/higher than most
    experts' bone removal rate during this phase."
  - tests/event_review_frontend.test.cjs updated for the speed wording (not run here:
    no Node on this machine).

## Self-paced review timing (2026-11-11)

Added the same background timing contract to Video-Based and Metric-Based study
steps. No countdown, time limit, new controls, survey version or layout changes.
Both wait for rendered feedback and usable first video frame (or explicitly
unavailable media), suspend at Next and resume on failure. Initial loading and
questionnaires are excluded. Existing Viewed gates still mean opened content.

Each participant's case session now stores `review_timing`: cumulative raw visits
and per-order summaries for elapsed, foreground, video wall time, unknown gaps,
coverage and overlapping visits. Foreground is a visibility/focus proxy, not
attention. Video is a subset of foreground. Five-second saves, scoped browser
retry storage, interval unions and server-exit caps support refresh and stale
tabs. Missing historical values are not converted to zero; late coverage is
flagged separately. See README's Feedback review time section for field meanings.

Validation: 689 Python checks, 57 final Node checks, 33 final targeted timing checks
and Ruff/diff passed. Live browser QA used two separate display-only
TIMING-EVENT-QA-20261111 / TIMING-SUMMARY-QA-20261111 IDs with no questionnaire
answers. Verified actual context playback, keyframe pause, Summary playback and
all phase-section openings, frozen totals during the post questionnaire, and
refresh creating a second visit with an unknown loading gap. Focus, clock/sleep,
duplicate and failure scenarios were covered by controlled tests. Receipt:
`.local/previews/surveys_v4_20261111/timing-browser-validation.json`.
Existing USERSTUDYTEST bytes were unchanged. Port 8818 serves the updated code;
already-loaded old pages need reopening and cannot acquire retrospective timing.
No source data, model or expert reference was changed. No commit or push.
The final browser teardown did not acknowledge the second Event visit's close;
its last heartbeat remains an open, incomplete review. Summary's Next exit and
the earlier Event reload close were acknowledged. Final-close delivery is best
effort, so an unfinished last heartbeat must not be treated as a completed duration.

## Frequency, direct insight ratings and compact cards (2026-11-11)

After discussing the response scales, the author approved
`mastoid_surveys_v5_insight` for new sessions:

- VR asks "Before today’s session, how often had you used a VR headset?"
  with Never, Occasionally and Frequently. This is subjective use frequency,
  not a proficiency assessment. Old Yes/No/Not sure answers are not converted.
- New insight asks "To what extent did this feedback help you recognize aspects
  of your performance that you had not noticed before?" Its own scale is
  0 Not at all, 1 A little, 2 Somewhat, 3 Quite a bit, 4 A great deal.
  Zero denotes no new recognition, not poor feedback or poor performance.
- Clarity and Actionability retain their 0–4 agreement scales. Overall retains
  all five verbal labels (Very poor, Poor, Fair, Good, Very good), the required
  1–5 response and the previously approved baseline/post wording.
- Rating-card minimum height is reduced from 115 to 84 px. Question spacing,
  legend margins and inner padding are tighter without reducing label font size.
  The narrow-screen layout retains horizontal options with a 46 px minimum height.
- Stored v3/v4 instruments, answers and progress remain unchanged. VR controls
  are rendered from the saved definition, including historical choices. Drafts
  remain version/participant/recording/step specific.

This supersedes the current-question choices described in the v4 implementation
record below. Existing sessions continue their original instrument; use a new
preview Study ID to see the revised choices.

Validation: 656 Python tests and 29 Node checks passed, plus scoped Ruff,
JavaScript syntax and diff checks. A synthetic HTTP run on saved P15T4 completed
all five response records, including New insight zero and separate Overall
ratings, with exact v5 snapshots. Existing USERSTUDYTEST content hash was unchanged.
The isolated preview server was restarted on port 8818 using the same participant
directory. SURVEY-V5-POST-PREVIEW is a display-only fixture starting at the first
post questionnaire with no submitted answers; it is explicitly marked preview-only.
Receipt: `.local/previews/surveys_v4_20261111/http_validation_v5.json`.
The browser connection again returned `transport closed`, so these checks do
not constitute visual browser validation. No evidence/model/reference changes.

The author subsequently approved the Clarity wording "The feedback was easy
to understand." New sessions use `mastoid_surveys_v5_1_clarity` to distinguish
this wording from saved v5 instruments. Both post questionnaires use the new
sentence; the five agreement choices and compact layout are unchanged.
Existing response snapshots and participant progress keep their original wording.
For this copy edit, 159 relevant Python tests and 29 Node checks passed. The
restarted local service returned the new sentence for SURVEY-CLARITY-PREVIEW,
a separate display-only fixture with no submitted answers.

## Questionnaire extension implemented (2026-11-11; v4 record)

The author requested the additions from the November 9 discussion draft.
At this implementation step, new sessions used `mastoid_surveys_v4_reflection`:

- Keep the minimal role, training-stage and prior cortical mastoidectomy questions;
  add one question: "Have you used a virtual reality (VR) headset before today?"
  Choices are Yes, No and Not sure.
- After analysis is ready and before either feedback view, collect one Overall
  performance rating for the current recording. Background remains once per
  participant; this baseline belongs to each recording.
- After each format, retain Clarity and Actionability, then add New insight and
  Overall performance. The first three use 0–4 agreement; Overall requires
  1 Very poor through 5 Very good. The author's clarification removes the draft's
  Unable to judge option: this is the participant's own impression.
  Baseline wording: "Based on your own impression, how would you rate your overall
  performance in the recorded procedure?" Both post questionnaires use:
  "After reviewing this feedback, how would you now rate your overall performance
  in the recorded procedure?" All three share the same five anchors.
- Do not prefill prior ratings or create a combined score. The six-item OSATS-like rounds remain
  disabled. Entry-selected format order and existing preference are unchanged.
- Preserve saved session instruments, progress and answers. Existing v3 sessions
  still show and validate their original three background fields and two post
  questions. Existing shared background is not rewritten to invent a VR answer.
  A new case receives its own baseline even when background was already saved.
- Intake collects only shared background while processing. Its temporary run
  does not collect the recording-specific rating; the full server shows that
  baseline after analysis is ready.

Local HTTP validation completed the new flow on the saved P15T4 result, including
the baseline readiness gate, both four-item post forms, required numeric ratings,
five separate response snapshots and completion/resume. Synthetic answers live
only in `.local/previews/surveys_v4_20261111/`, explicitly marked preview-only.
Existing preview participant files and evidence/model/reference artifacts were
not changed. Browser visual inspection could not run because the browser
connection returned `transport closed`; automated UI logic and API checks are
distinct from a visual check. Validation: 650 Python tests passed; after the final
timing-specific wording change, all 59 reflection/intake tests passed again.
All 28 Node checks, scoped Ruff and diff checks passed. The final numeric-only
HTTP receipt is `.local/previews/surveys_v4_20261111/http_validation_required.json`;
the earlier draft-option QA receipt is retained separately and is not current.
This is questionnaire engineering, not evidence of
clinical validity or improved self-assessment.

## Current Event detail and Summary refinement (2026-11-10)

The author approved the interactive design, then clarified that Key frame should
seek the video and pause rather than display a separate original image.

- Expanding a category shows all selected rows (at most four), without internal
  scrolling. Global selection, phase filters, Viewed and Next rules are unchanged.
- Key frame uses the verified event time in the existing video. It pauses there;
  the normal video Play control continues. Removed the separate frame image,
  caption, loading mode and Return to video control from both Event entry modes.
  No image-only review action is offered when playable video is unavailable.
- Event distributions retain expert dots, the current clip marker and units,
  remove the numeric label above that marker, and label the band **Middle half
  of experts**. The band denotes weighted same-phase expert-window Q25-Q75.
  One short sentence describes the current clip's direction relative to typical
  expert values. Stroke length wording explicitly refers to mean stroke length.
- Critical Structure Violation uses concise text such as **Up to 0.21 mm deep**;
  it does not repeat a Maximum measured intrusion label.
- Summary displays only Stroke length, Tool speed and Removal rate under Drilling
  motion. Frequency calculations, stored reference fields and API output are
  retained; this is a display change and does not regenerate expert distributions.

Validation: 595 Python tests and 22 Node tests passed, plus Ruff, JavaScript syntax
and diff checks. Browser checks confirmed the video paused at the verified anchor
and resumed with native Play, with no image overlay or Return button. All four
rows had equal content/client height and visible overflow; the Motion chart had
the new band legend/caption and no numeric marker label. Viewed/Next remained
locked at 19 of 20 and enabled at 20. The live Summary displayed exactly three
Motion rows. A new preview-only participant was used for Event checks, preserving
the earlier preview's Summary progress and responses; no answers were fabricated.

## Session-wide Event selection (2026-11-10)

The author subsequently approved a smaller selection: **at most four moments per
category across the session**, at most 20 overall. This supersedes the per-phase
top-five count and speed eligibility rule documented in the earlier update below.

- All phases is the default. Phase filters show the same fixed shortlist, with
  no refill or rank changes. Rows show global rank, phase, time and Viewed on one
  line. The page states that selection is not a complete assessment of any phase;
  no selected moment in a phase does not imply that phase has no issues.
- All three Motion metrics qualify strictly below same-phase expert Q25 or above
  Q75, with numeric tie tolerance and existing coverage/clock/metric guards. There
  is no additional relative-to-median 25% gate. The saved reference remains intact;
  its old speed thresholds still describe the separate legacy coaching contract.
- Consecutive, same-direction candidates within one phase interval form an
  episode. Select its strongest actual five-second window, then rank across
  phases by absolute median difference divided by matched-phase expert IQR.
  README documents deterministic degenerate-scale fallbacks. The plot always uses
  the representative window's own phase reference; a quiet line identifies its
  longer episode where applicable. Safety retains existing qualification and
  event grouping, ranked by intrusion or duration with four per category.
- Category Viewed and Next use every selected moment, independent of the filter.
  Feedback/video positions, context padding, Safety-only verified Key frame,
  Summary calculations, surveys and existing responses are preserved.

Validation: 595 Python tests, eight Node frontend contract tests, Python lint,
JavaScript syntax and diff checks passed. Independent checks of 84 real-recording
windows across all three Motion metrics matched eligibility, episode merging and
global ranks. The selected set contains 20 moments (four per category): eight
Safety, 12 Motion, including two high and ten low Motion values. Summary and
legacy analysis outputs match the earlier service; database and both reference
hashes are unchanged. Browser checks confirmed exact Safety frames, context
playback, phase-specific plots, fixed filtered ranks and global Viewed state.
Next stayed hidden at 19 of 20 even after changing filters and appeared after
opening the twentieth. Checks used isolated preview state, without survey answers.

## Earlier phase-filtered Event update (2026-11-10)

The author approved the phase-filtered, ranked Event design. This section
supersedes the older Event interaction notes below; questionnaire instruments,
Summary calculations and saved participant responses are unchanged.

- Keep feedback left and video right. Select Early Drilling, Antrum or Incus.
  Safety contains Critical Structure Violation and Poor Drill Visibility;
  Drilling motion contains Tool speed, Stroke length and Removal rate.
- Each phase/category shows at most five moments. For motion, use the existing
  same-phase expert five-second reference with performer self-exclusion and
  participant-balanced weights. Include both directions. Speed preserves the
  active reference's 25% median-difference threshold with the 10th/90th percentiles;
  Length and Removal rate select values outside Q25–Q75. Preserve coverage,
  clock and stroke eligibility; suppress speed during >=20% microscope/clutch
  activity. Rank by absolute median difference, not signed difference.
- Adjacent qualifying windows in the same direction form an episode. Select its
  most-deviant measured window before taking the top five. Safety keeps existing
  qualification and ranks supported intrusion or detected duration. No matched
  expert Safety distribution is currently available.
- Click a one-line moment row to play five seconds before
  through five seconds after the interval, clamped to the video. The small
  Key frame button appears below the video on the right for Safety only and
  uses a verified original source frame. Motion has no key-frame button.
- The selected motion plot shows expert dots/distribution, a reference band and
  the clip's actual value/unit. No percentage-below claim or duplicate numerical
  comparison panel is added. Expert tail display clipping does not alter any
  reference statistic, eligibility rule or rank; the selected marker remains
  visible. Playback padding never enters the five-second measurement.
- Viewed means explicitly opened, not watched in full. Mark a category after
  all its selected moments are opened. Next requires all selected moments across
  all phases, ignoring empty categories. These marks remain page-local and reset
  on reload, matching the previous resident interface.
- Follow-up clarification: removed the separate dot timeline and its renderer.
  Expert comparison dots remain. Next retains the latest resident-study behavior:
  hidden until all selected moments are opened, then shown below the right-hand
  video card (at the bottom on narrow screens). It still leads to the questionnaire.
- Fixed interaction logging for the ranked item IDs. Logging validates exact IDs
  from the served payload without rebuilding analysis on each click; legacy IDs
  and historical session inventory remain intact. Three focused regression tests
  cover current/legacy IDs, rejected forged IDs and concurrent initialization.
- The service adds `event_review` alongside the original coaching/feedback
  payloads. Canonical events, models, source data, reference files and surveys are
  preserved. The optional ten-second reference is not activated by this change.

Validation: 578 Python tests, five Node frontend contract tests, Python lint and
diff checks passed. A read-only real-recording audit independently reconstructed
84 windows and verified all 51 selected moments (34 motion: 30 low/four high;
17 Safety with verified source frames). The whole-phase response matched the
pre-change service exactly and database/reference checksums were unchanged.
Browser checks exercised context playback, source frames, minimal plots,
phase/category Viewed state and the all-phase Next gate in isolated preview state.
No real participant questionnaire answers were created or modified.

## Removed (can be brought back)

| What | Where | How to restore |
| --- | --- | --- |
| The three self-assessments (before feedback, after each view) | `SELF_ASSESSMENTS = False` in `src/evidence_system/study_surveys.py` | Set it to `True`. Tests still cover the full protocol. |
| Background questions: age, gender, completed training months, clinical mastoidectomy years, VR headset use, other simulators, earlier attempts on this simulator, temporal bone lab experience; the "medical student" role | Background schema `mastoid_surveys_v3_resident` in `study_surveys.py`, `apps/expert_study/surveys.js` | Earlier versions are in git history (`mastoid_surveys_v1`, `v2_resident`). |
| Study ID entry page in normal use (the launcher opens the questionnaire with the ID in the address) | `scripts/study_gui/study_mode.py` (`open_feedback`) | The page still works if opened without an ID. |
| Pilot "Start automatic session analysis" screen and its progress animation | `showReady()` in `apps/expert_study/app.js` | Still shown if loading the feedback fails. |
| Summary page: "How to read these comparisons" explainer | `apps/summary/index.html` | Original wording is in git history. |
| Summary page: phase durations on the phase buttons; the "Jump to this phase" strip | `apps/summary/` | Git history. |
| Video-Based page: phase bands and labels on the Procedure map timeline (the timeline itself now sits under the video as a slim strip of dots) | `apps/expert_study/index.html`, `styles.css` (`.phase-map` hidden) | Remove the `.phase-map` rule. |
| Video-Based page: "Coaching plan" heading, "What happened", "Try this next" (actionable advice), "Go to first moment", the separate moments list under the video, "How this summary is supported" | `apps/recommendations/event-review.js`, `apps/expert_study/styles.css` | Git history. |
| End page: "Case complete" label, "Expert workflow experience complete" wording and the completion code. It now shows a check mark, "Session Complete", "Thank you for participating in this study." and a "Return to the simulator study window" button (POST `/api/expert-study/return-to-simulator`; the launcher brings its window forward) | `apps/expert_study/index.html`, `app.js`, `src/evidence_system/web.py`, `scripts/study_gui/study_mode.py` | Git history. |
| Video-Based page: "Go to key frame" / "Play context" buttons, and the key-frame still image with "Return to video" | `event-review.js`, `apps/expert_study/app.js` | Safety moments now pause the video at the event's time; motion moments play. |

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
- Preference page: just "Which feedback format did you prefer?" with Video-Based Feedback
  and Metric-Based Feedback (definitions: "Highlighting the specific moments where your performance deviated from expert performance" / "Contextualizing your performance metrics against expert performance metrics"), A combination of both
  (new value `combination`), and No preference. The "After both feedback formats" label,
  "Your preference" title and instruction line are removed.
- Both review pages: Next stays hidden until every phase (summary) or every moment
  (event-based) has been reviewed; review progress is not saved across a page reload.
- Video-Based timeline dots, topic stripes and moment buttons all use one colour (no
  per-topic colours, no legend). Clicking a dot acts like clicking that moment.
- Video-Based topics use the summary page's names: Critical Structure Violation, Poor
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
