# Architecture

## System boundary

The system converts a recorded simulator session into inspectable evidence and
post-session feedback. Each layer has one responsibility:

1. **Source adapters** read a configured run, anatomy resources, and optional
   human-authored phase boundaries without modifying the source files.
2. **Frame pipeline** derives normalized frame observations, geometry context,
   visibility observations, and motion segments.
3. **Event and relation builders** group observations and create factual links
   between events, phases, anatomy, motion, and source evidence.
4. **Evidence store and retrieval** persist canonical artifacts and expose only
   allowlisted, read-only queries.
5. **Feedback and validation** organize retrieved evidence, preserve candidate
   status, require citations, and block unsupported causal or clinical claims.
6. **Local web applications** present feedback and linked media without making
   source data part of either frontend bundle.

```text
configured sources
       |
       v
frame evidence -> events -> relations -> evidence store
                                      |          |
                                      +---- retrieval
                                                |
                                                v
                                      validated feedback -> local web app
```

## Official applications

The repository contains two application modes over the same local service and
validated evidence contract, with two read-only recommendation views:

- `apps/expert_study/` provides the complete expert-study journey and stores
  its study inputs separately from canonical evidence.
- `apps/recommendations/` presents recommendation-only feedback and does not
  expose expert annotation or item-level review controls.
- `apps/summary/`, served at `/summary/` in recommendations mode, compares whole-phase
  measurements with the saved recording-level expert reference. It shares the
  video endpoint and event-view layout and adds no study data collection.

The expert-study ID form selects the initial Event-based/Summary view. New sessions
use `sequential_review_v2`: background once per Study ID, analysis, a one-item
Overall performance baseline for each recording, first review
and four-item questionnaire, second review and four-item questionnaire, then
preference and completion. The current resident instrument is
`mastoid_surveys_v5_1_clarity`; `SELF_ASSESSMENTS = False` disables the three optional
OSATS-like rounds without deleting their forms or earlier responses. Preference follows the final
configured case; other cases continue after their second questionnaire. The same
summary assets are embedded at their assigned step. There are no free-switching
tabs and the entry selection is not randomized by the application.

`study_surveys.py` defines the versioned English instrument and validates answers.
`apps/expert_study/surveys.js` renders the minimal background plus VR experience,
one-item baseline, four-item post-feedback forms and Preference with `surveys.css`.
Post-feedback items are Clarity and Actionability (0–4 agreement), New insight
(0 Not at all to 4 A great deal, using its own item-specific scale), then Overall
performance (required 1–5, based on the participant's own impression).
Background VR uses Never / Occasionally / Frequently. The renderer reads options
from the session definition; old v4 Yes/No VR and agreement-based New insight
remain unchanged in saved sessions. No historical answers are reinterpreted.
Baseline starts "Based on your own impression"; both post questions start
"After reviewing this feedback, how would you now rate..." and refer to the same
recorded procedure. The scales match; there is no Unable to judge option.
The historical six-item self-assessment renderer remains available and has its
own original-video player without coaching overlays. Current-step drafts use
tab-local storage scoped by instrument, Study ID, recording and step; submitted
answers are never used to prefill later rounds. A response records the exact
instrument snapshot, time, case, round and feedback order. The instrument is a
proposed research self-assessment, not a validated clinical score; no total or
pass threshold is produced. Responses never feed automatic evidence generation.

The study directory stores one JSON per participant (normally
`study_data/participants/<Study ID>.json`). `study_storage.py` owns atomic file
transactions, portable filename allocation and lossless legacy import. Each file
retains the existing envelope: participant background under `participants` and
case-specific submissions under `sessions[*].review_flow.responses`, plus that
participant's events and historical contributions. Same-participant transactions
are serialized across case-server processes; another participant's file is not
rewritten. `--study-state` and manifest `study_state` select the directory. Legacy
JSON arguments import once into a sibling `<stem>_participants` directory without
editing the source; `migrate-study-state` allows an explicit destination. A source
hash receipt prevents stale imports from overwriting later answers, and conflicting
data or a changed previously imported source is rejected. Original IDs and unknown
shared metadata are preserved; safe filenames avoid case/device-name collisions.
Public flow
payloads omit saved answers. Next checks step ID, view, stage and payload before
an atomic write; invalid and duplicate submissions cannot overwrite or skip
steps. Background precedes analysis; other transitions require completed analysis.
Existing sessions retain their frozen questionnaire definitions, sequence and records;
validation uses that session's definition rather than the latest instrument.
Old background records are reused without fabricating a VR answer. A new recording
still collects its own baseline; an existing exposed session never gains a retroactive
baseline. Intake's temporary `analysis-pending` run collects only background;
the real recording owns the baseline after analysis is ready. Reviewer identity stays distinct from performer
identity for cohort exclusion; manifests must select the participant's own
recording when collecting self-assessment.

Review timing uses `apps/expert_study/review-timing.js` for both Event and embedded
Summary. The parent waits for rendered feedback plus first-frame readiness (or
explicit media unavailability); Summary emits a ready message after its API fetch
and render, rather than relying on iframe load. Actual browser visibility/focus
and media events drive interval flags. Next suspends at the click boundary and
requires an acknowledged snapshot before the flow transition; failed transitions
resume. The final closed snapshot may retry independently after successful Next.
The layout, survey instrument and opened-content gates remain unchanged.

`POST /api/expert-study/review-timing` accepts participant ID and a versioned,
cumulative visit snapshot. `review_timing.py` validates the reached review step,
immutable visit identity, ordered intervals and increasing sequence; `web.py`
stores it inside the existing participant transaction. The session's
`review_timing.visits` retains raw snapshots and server receipt times; `.steps`
contains per-order interval-union summaries. Previously reached steps accept late
retries; future steps and intake reject timing. Server-recorded review exit caps
stale-tab contributions. Historical sessions acquire no fabricated zero values.

Five-second heartbeats and a participant/recording-scoped local outbox support
refresh/retry. Foreground and video durations use observed monotonic intervals;
delays over 15 seconds or wall/monotonic clock discontinuities remain unknown.
First-ready-to-last-observed elapsed spans interruptions between visits. Video
wall time is a subset of foreground time; neither implies attention. Raw intervals
and overlap diagnostics are retained rather than adding concurrent tab counters.

The Event-based study step reuses `/review-styles.css` and the recommendations
renderer, with feedback on the left and video on the right. It does not render the
legacy expert-moment capture form; historical saved contributions remain untouched.
Five session-wide categories contain Critical Structure Violation, Poor Drill
Visibility, Tool speed, Stroke length and Removal rate. All phases is the default;
phase filters narrow the existing selection without refilling or reranking it.

`event_review.py` adds a `session_event_review_v2` selection to bootstrap and study
analysis without replacing canonical events, `coaching_plan` or legacy `feedback`.
Motion uses the existing source-bound observations and exact same-phase,
participant-balanced expert-window references with performer self-exclusion.
Select Motion values strictly outside weighted Q25/Q75, combine adjacent
same-phase-interval, same-direction windows into episodes, keep the most-deviant
window per episode and select four per metric across the session. Rank by
absolute same-phase median difference divided by that phase's IQR (documented
degenerate-scale fallbacks in the README). The reference's legacy speed gates do
not apply to this new display policy. `items` contains five global categories;
each clip binds its own phase reference and global rank. `phase_status` carries
per-phase availability and counts; `phases` contains filter labels rather than
duplicate selectable lists. Safety reuses qualified detections and true
frame bindings; rank intrusion or duration, with no fabricated expert reference.
No HDF5 rebuild, expert regeneration, model training or Summary aggregation change
is required. See the README for the exact selection thresholds.

`apps/recommendations/event-review.js`, served as `/event-review.js`, owns the
shared phase/category, ranked moment-list and selected-clip distribution
rendering for both entry modes. The separate dot timeline is removed.
Clicking a row plays from five seconds
before to five seconds after its measured interval, bounded by video duration.
Only Safety offers the small Key frame button below the video on the right, when
video and a verified event anchor are available. It seeks and pauses the existing
video; no original-image overlay, image request or Return to video mode is used.
Native Play resumes from the anchor. Pending metadata callbacks use the existing
media-action guard so a stale request cannot override a later selection.
A motion plot compares the selected measured window, excluding playback padding,
with expert windows; it shows a marker, units, Middle half of experts legend and
one directional sentence, without a numeric value above the marker. Summary
still compares entire phases and displays only Stroke length, Tool speed and
Removal rate; its frequency fields remain in the unchanged reference/API.
Stable clip IDs and global list indices keep Viewed marks across phase/category changes. Explicit opening
marks a moment; a category is complete only after all its selected moments are
opened. The study Next gate includes the complete session-wide selection (at most
20 moments), regardless of the active phase filter, ignores empty lists,
and retains the existing page-local (not persisted) review-mark behavior. The
Next card is hidden until completion, then appears below the right-hand video
card (below the review on narrow screens), as in the resident-study baseline. Media
handling and survey progression stay in each page integration. The player remains
beside the moment list. Expanding a category shows all at-most-four rows with no
internal scrolling.

`GET /api/phase-summary` reads source-bound database observations, recomputes only
the current recording's whole-phase metrics and loads the bundled reference.
`phase_summary.py` owns native-time aggregation and participant-balanced reference
statistics; `phase_summary_safety.py` combines existing qualified detections within
phase intervals. No expert source files, model fit or detector rerun is needed to
open the page. The original five-second coaching comparison remains separate.

Keeping these interfaces separate prevents study data-collection behavior from
silently entering the recommendation-only experience.

## Configuration

Repository configuration contains only shareable policies and synthetic
examples. Dataset manifests, anatomy mappings, source roots, and case-specific
review decisions are injected at runtime from ignored local files. Production
code must not assume a particular dataset, case identifier, directory layout,
or operating system.

## Determinism and external models

Evidence construction, storage, retrieval, and safety checks are deterministic.
An external text model may select an allowlisted retrieval intent or draft text
from a deidentified evidence packet, but it cannot create evidence, change an
evidence status, or bypass claim validation. The deterministic path must remain
fully usable without network access or model credentials.

## Portability

Paths are represented with `pathlib` and supplied through configuration. Python
3.12 is tested on Windows, macOS, and Linux. Generated artifacts live outside
the repository, and tests build synthetic inputs at runtime so that CI does not
depend on private files.
