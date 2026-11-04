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
use `sequential_review_v2`: background once per Study ID, analysis, baseline
self-assessment, first review, first post-feedback self-assessment and two-item
questionnaire, second review, second post-feedback self-assessment and two-item
questionnaire, then preference and completion. Preference follows the final
configured case; other cases continue after their second questionnaire. The same
summary assets are embedded at their assigned step. There are no free-switching
tabs and the entry selection is not randomized by the application.

`study_surveys.py` defines the versioned English instrument and validates answers.
`apps/expert_study/surveys.js` renders the background, six-item OSATS-like forms,
Clarity/Actionability and Preference with `surveys.css`. Self-assessment has its
own original-video player without coaching overlays. Current-step drafts use
tab-local storage scoped by instrument, Study ID, recording and step; submitted
answers are never used to prefill later rounds. A response records the exact
instrument snapshot, time, case, round and feedback order. The instrument is a
proposed research self-assessment, not a validated clinical score; no total or
pass threshold is produced. Responses never feed automatic evidence generation.

The separate study JSON stores participant background under `participants` and
case-specific submissions under `sessions[*].review_flow.responses`. Public flow
payloads omit saved answers. Next checks step ID, view, stage and payload before
an atomic write; invalid and duplicate submissions cannot overwrite or skip
steps. Background precedes analysis; other transitions require completed analysis.
Existing v1 sessions retain their original placeholder sequence and records,
including earlier reflection history. New Study IDs are required for an
unexposed baseline under v2. Reviewer identity stays distinct from performer
identity for cohort exclusion; manifests must select the participant's own
recording when collecting self-assessment.

The Event-based study step reuses `/review-styles.css` and the recommendations
workspace: video on the left, feedback on the right, with native key-frame and
context playback controls. It does not render the legacy expert-moment capture
form; historical saved contributions remain untouched. Study clip serialization
retains verified key-frame bindings and per-clip phases from the same review plan.

`apps/recommendations/event-review.js`, served as `/event-review.js`, owns the
shared Event card, moment-list and timeline rendering for both entry modes.
Topic colors and ordering are display rules. Each existing clip remains present;
marker positions use bound key-frame times when available and interval starts
otherwise. Per-page media handling and study progression remain separate from
this renderer. The player stays outside the independently scrolling moment list.
Summary phase selection shares neutral phase colors; reference statistics and
their current-recording markers are unchanged.

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
