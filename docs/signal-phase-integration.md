# Offline signal phase integration

For automatic directory input, use [HDF5 directory input](hdf5-directory-input.md).
That entry generates recording identities, alignment, geometry configuration and
video from native HDF5, using once-installed runtime sources. The explicit
historical-configuration path below remains available for old reference-clock
replays; it is no longer the only way to process a recording.

The opt-in signal adapter connects completed legacy simulator HDF5 recordings to
phase segmentation, browser video and the existing deterministic feedback system.
It uses a frozen E71 MS-TCN++ checkpoint and its matching preprocessing parameters.
It does not train a model, download weights, use video to classify phases or call
an external feedback API. Manual phase labels remain supported when no model
bundle is configured, but real production builds always require verified R4 geometry.

## Included bundle and configuration

The selected Full / RecordingHoldout bundle is already included at
`models/e71_full_recording_holdout/` (916,992 bytes total). Normal installation
does not require export, a separate model download, or training. The root example
configuration points to this directory and `references/geometry_corrections_v3.csv`.

For maintainers exporting a different approved checkpoint, use a new directory
and its matching preprocessing file; do not overwrite the frozen included bundle:

```text
python scripts/export_phase_model.py --checkpoint <checkpoint> --preprocessing <preprocessing> --output models/<new-bundle> --model-id <new-model-id>
```

The bundle contains `manifest.json`, `model.pt` and
`neural_preprocessing.json`. Its manifest declares feature ordering, output
ordering, hashes, units, the removal gate and offline context. Export is a copy
of existing artifacts, not a fit. The target directory must not already exist.
Only the selected frozen bundle is authorized for Git inclusion; other checkpoints
remain ignored. Machine-specific paths belong in ignored local configuration.

Provide explicit `phase_model_bundle`, `frame_alignment_file`
and, when required, `phase_duration_s` fields in an ignored local source config.
Existing `data_root` and `edt_root` remain required. Install the root
`requirements.txt` in one Python 3.12 environment as described in the README.
The phase worker always uses the same interpreter that starts the pipeline.
`phase_annotation_file` is optional in model mode.
CLI overrides use the corresponding hyphenated names:

```text
python run.py build --run-id YOUR_RUN_ID --registry config/local/runs.csv --source-config config/local/source_paths.json --anatomy-profiles config/local/anatomy_profiles.json --phase-model-bundle models/e71_full_recording_holdout --frame-alignment-file <clock-alignment.csv> --build-video
```

Use a new output directory for a changed model, calibration or clock. Source
HDF5, historical annotations, model weights and earlier outputs are not edited.
Every real production build requires a run-bound `phase_signal_geometry` block in
the anatomy profile and the source verification described below. These checks bind
the build to the existing R4 correction; they do not validate a new calibration.

## Directory input and mandatory R4 source binding

One recording directory supplies all its `.hdf5` chunks. The pipeline inspects
every chunk, sorts native frames by time and uses one explicit video alignment.
It does not guess specimen identity from an anatomy token in the folder name.
The registry and local configuration bind that directory to its R4 correction.

Before creating any production result, `r4_provenance.py` verifies the pinned v3
correction-table content and recording row, raw folder, actual specimen, fixed
rotation, recorder scale, EDT spacing, quality flag, original GUI/ADF contents and
the GUI-selected EDT directory. All 16 EDT files are hashed and their actual grid
headers checked. Non-cubic grids are supported. Missing or inconsistent evidence
stops the build; real data cannot silently fall back to the historical rotation.
Only explicitly generated synthetic fixtures have a separate test exception;
R4 and historical recording identities cannot use it.

EDT enrichment, tip distances and Ray screening receive the same verified profile
snapshot. Stage manifests carry the same `geometry_provenance` receipt. Native
world-coordinate motion, camera calibration, image-depth units and HDF5 voxel
volume retain their declared contracts; they are not reinterpreted as EDT mm.
The correction's `needs_review` status remains distinct from scientific acceptance.
Original HDF5 content is bound by configured recording/folder identity; the source
configuration audit can additionally hash individual files. This does not certify
that an arbitrarily renamed source recording is authentic.

For a database produced by this strict path, serving also checks Ray evidence
against the source-manifest hash stored in SQLite, the pinned stage/phase artifacts,
the geometry receipt, clock alignment and current frame/removal-input hashes.
An old same-run Ray directory cannot override that binding. A correctly bound
external copy remains usable. This backend check adds no interface text or panels.

## Phase and input contract

Before the first observed positive removed-voxel count, the system emits
`predrilling` by a deterministic rule. After that first removal, the gate stays
active through pauses. The three-class head predicts `early_phase`, `antrum`
or `incus`; former post-incus time is included in incus. There is no learned
predrilling class and no forced monotonic stage progression.
If removal begins after the final complete one-second endpoint, the adapter
stops with an explicit error because no post-start model sample exists.

For compatibility, downstream labels map as follows:

| Model/rule label | Existing UI/database label |
|---|---|
| predrilling | pre_drilling |
| early_phase | early_surface |
| antrum | antrum |
| incus | incus |

At each one-second endpoint, 48 numerical removal/anatomy features become
96 values after the frozen transform and 48 missingness flags. Local trailing
windows and cumulative history are retained; missing measurements are not
interpreted as zero before the declared imputation. Source volume remains
source-reported volume. Nearest removed-region distance is not deepest-point
distance, wall thickness, exposure sufficiency or safety.

The model receives the entire post-removal sequence, including future signals.
Its four-stage interface is `[4, batch, padded_time, 3]`; the final output has
three probabilities per endpoint. This is offline segmentation, not online
detection. No language model or token interface is used. Manual targets and
feedback are not model inputs. Predicted intervals retain model provenance and
are not human annotations or clinical ground truth; review flags remain false.

## One clock for geometry, phase and video

This integration supports the legacy `data/time` and `data/l_img` HDF5 schema.
It does not silently adapt the separate `vision_data/continuous_data` layout
used by the older visual-model overlay. That layout needs a verified adapter.

The core checks native frame count, ordered global frame indices, finite
increasing mapped times and optional world-frame indices. It records the
configured run binding and alignment file SHA256. These columns cannot detect
an equally sized sidecar from another recording; the configured association
must come from verified source provenance. Source filenames and raw timestamps
are retained in the native inventory but are not independently matched to this
CSV. Only clock/index columns from an alignment CSV are used. The exact ordered
core `t_video_s` values feed geometry, feature
windows and video. Native absolute timestamps remain available as provenance.
Explicit recording duration is recorded separately from the last source frame.
Real R4 model mode requires the explicit alignment; native HDF5 elapsed time
alone is allowed for the declared synthetic fixture but is not the R4 training clock.

Video generation preserves source order and writes native BGR images directly
to VP8/WebM. It resamples the canonical clock to the configured uniform frame
rate using nearest-source frames. If a declared duration extends beyond the
last frame, the video repeats that frame and records the extension. Its manifest
records clock provenance, source bounds, sampling error, decode checks and color
orientation. Video alignment does not validate the phase predictions.

## Repeated intervals and feedback

Each contiguous predicted segment has a unique `phase_id`. Frames, events and
relations retain that identity; antrum appearing twice cannot overwrite its
earlier segment. Frame-event grouping splits on interval identity. Performance
bouts use the interval containing each stroke midpoint and disclose that
assignment; a whole stroke is not redefined as a phase boundary measurement.
All stroke video boundaries use the same mapped clock; duration and kinematic
rates retain native HDF5 seconds and state that clock explicitly. With fewer
than seven drilling frames, the existing detector cannot form a supported
neighborhood. The build records `insufficient_drilling_frames`, emits empty
stroke/metric tables and continues with frame evidence, phase intervals and
video. It does not invent a stroke or treat unavailable metrics as zero.

Phase summaries aggregate all matching interval IDs, return their individual
bounds and sum their durations. They do not present a first-to-last envelope as
one continuous phase. Benchmark coaching links each antrum interval separately.
Phase-derived relations are provisional in model mode, and coaching exposes
predicted phase context separately from simulator evidence.

Existing feedback rules still require their own evidence. Merely predicting
antrum does not establish short strokes, poor exposure or a safety violation.
The calibrated build now executes the R4-adapted Ray-v3 stage automatically and
writes `stages/08_under_ledge_ray/`. Serving the sibling evidence database discovers
that folder without a separate `--ray-v3-dir` argument. Explicit external Ray-v3
paths still override this default. The optional cohort benchmark remains a
separate input; an exposure-analysis completion does not imply that technique
comparison was also run.

The Ray-v3 adaptation uses the same fixed calibrated volume rotation for camera
and burr, the declared R4 EDT distance metric, and the complete removal history on
the canonical video clock. Removed voxels are proxies for active contact; the
recording does not provide an independently measured contact surface. On explicit
user instruction, the coaching duration threshold is 0.3 seconds instead of 0.75;
all other Ray-v3 thresholds remain unchanged. The saved `coaching_policy` records
the dated change. Duration is first-to-last positive frame span, and the existing
five-positive-frame, mean-blocked-fraction, contact and registration gates remain.
Its versioned
lineage records changes to geometry, integer grid centers and numerical timestamp
handling; it must not be described as an unchanged replay of the older experiment.

Legacy image-edge candidates remain separate observations. The exposure card
consumes the actual Ray-v3 result and retains its registration/support gates.
Missing calibration, analysis failure, no evaluable contact, no candidate,
low-support candidates and registration suppression have distinct backend status
fields. The existing interface and wording remain unchanged by user instruction;
do not add diagnostic panels or messages. Unavailable analysis cannot imply adequate exposure. The frame-score
CSV, episode JSON, input hashes, geometry, clock and threshold manifest remain
available for audit. A build without validated geometry explicitly records that
the stage was not run.

## Verification

```text
python -m pytest -q tests/test_signal_phase_consumers.py tests/test_demo_pipeline.py tests/test_runtime_configuration.py
python -m ruff check src tests
```

The consumer tests use synthetic data and check distinct repeated interval IDs
through event construction, relations, SQLite foreign keys and retrieval; disjoint
coaching clips; invalid clock rejection; and actual BGR-to-WebM encoding/decoding
on a mapped clock with a declared tail. The original demo/configuration tests
exercise the manual path. Video tests require the pipeline and vision extras.
Additional source-to-feedback tests cover no removal, isolated removal and a
stretched clock using explicit synthetic model-output stubs; these orchestration
tests are separate from checkpoint validation.
A separate archived-data compatibility check has reproduced frozen input
transforms exactly and model predictions within numerical tolerance. That is
implementation compatibility, not new clinical or generalization evidence.

The initial phase-only integration check passed 42 tests. One full-suite repository
hygiene test still fails on files already present in the original checkout (the
tracked handoff archive and three README path examples); these were preserved.
Ruff was not available in the existing runtime. Python/JavaScript syntax and
scoped whitespace checks passed. The actual-checkpoint synthetic build generated
a coaching card. The initial real legacy-HDF5 build reached phase prediction, video and the feedback
API, but did not execute Ray-v3 and returned no coaching card. That was incomplete
exposure-coaching integration, not a negative detection result. The automatic stage
and status contract above supersede that initial wiring.

The initial complete real-record replay, using the former 0.75-second gate,
executed the calibrated Ray-v3 stage.
Its 1,500 frames include 921 evaluable contact observations and 17 positive frames.
Fourteen positives form three low-support episodes; three isolated positives fail
the two-frame episode minimum. The user-reported point was recovered in a six-frame
episode with mean blocked fraction 0.866667. It fails only the unchanged 0.75-second
coaching duration threshold. Thus the automatic analysis is connected, while no
prescriptive exposure card qualifies. No threshold was tuned to the reported point.
The frozen phase input and probabilities match the prior integration exactly, and
the prior validated video was reused only after exact source-frame and clock checks.
Final backend integration suite: 82 passed and the same one pre-existing repository
hygiene test failed. Independent actual-output checks verified 3,387 assertions,
including removal chronology, episode gates, source identities and video clocks.
The local server's bootstrap and ranged video response were verified. The browser
was not refreshed after the instruction to preserve the user's interface.

The later user-directed 0.3-second replay passed 114 tests, with the same one
pre-existing hygiene failure. Both real strict builds and live bootstrap/video
responses passed; the old same-run Ray override was rejected. The first recording
now has one eligible exposure moment, and the second has 39 candidates, 18 eligible
episodes and two feedback categories. The second recording is a training member
of the accepted phase checkpoint, so its 87.70% reference accuracy is an in-sample
replay diagnostic. The webpage source was unchanged. A subsequent user-reported
black player was visibly restored by reloading and playback was verified; the
precise trigger for the stale black state was not established.
