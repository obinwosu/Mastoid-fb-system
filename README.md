# Mastoid Feedback System

Process a completed HDF5 recording folder into video, phase detection, events
and feedback. One subfolder is one recording; its HDF5 files are chunks of that
recording. The existing website is used without per-recording configuration.

## Install

Use Python 3.12 on Windows or Linux (x86-64). From the project root:

```text
pip install -r requirements.txt
```

All processing, including phase inference, uses the same Python environment.
The requirements include CPU PyTorch; CUDA is not required. Local environments
such as `.venv/` are ignored by Git and are not part of the repository.

## Configure once

The only file you edit for machine paths is **`config.local.json` in this project
root**. Copy `config.example.json` if it does not exist.

| Setting | Value |
| --- | --- |
| `simulator_root` | Anatomy resource root containing `scripts/study_gui/gui_setup.yaml` and the matching `ADF/` files |
| `edt_root` | Parent directory of the `edt_grids_<specimen>_256/` folders; may differ from `simulator_root` |
| `correction_table` | Path to the unchanged R4 `geometry_corrections_v3.csv`; edit the path, not the CSV contents |
| `phase_model_bundle` | Trained model directory containing `model.pt`, `neural_preprocessing.json`, and `manifest.json` |
| `video_fps` | Output video frame rate, default 10; does not change native timestamps or signal sampling |
| `motion_reference` | Saved expert distribution: `references/r4_motion_windows_v3.json`, included with the project. Keep this relative path when migrating; `null` disables expert motion comparisons. |

For a new batch, change `--input-dir`; alignment and per-recording configuration
are generated automatically. New recordings with `volume_adf` in `metadata.json`
select their anatomy model and EDT automatically. If an unknown recording lacks
this information, supply its actual specimen with `--anatomy-model RT138`, for example.
See [input format changes](docs/hdf5-directory-input.md#simulator-format-changes)
for supported fields and the shared reader to update if the simulator format changes.

Relative paths are resolved from this project root. EDTs can be stored separately
from the anatomy configuration. Model/grid identity, geometry and content hashes
remain checked after moving them. The correction CSV is copied unchanged; do not
edit or re-save it. No manual phase labels or original video are required.

No Python executable path is needed in the configuration.

Copy the model bundle, correction CSV and anatomy resources as well as the code;
these resources and machine-local configuration are not stored in Git. The saved
expert reference under `references/` is included with the project. Rebuild results on the
new machine; old results contain absolute source paths and remain archives.

## Run

From the project root on Windows or Linux, replace the input directory:

```text
python run.py process-hdf5 --input-dir ./recordings --serve
```

The default `--anatomy-model auto` selects known R4 identities from the trusted
correction table, or new recordings from `metadata.json`'s `volume_adf` basename.
If neither identifies a specimen, provide `--anatomy-model LT151` (for example).
An explicit model conflicting with recorded metadata is rejected. The phase network
always remains the installed frozen offline MS-TCN++ model.

The command creates a new folder under `results/hdf5_batches/`, generates IDs,
alignment, configuration, video and feedback, then prints the website address
(default local port 8810 onward). Existing inputs/results are not overwritten.
`--prepare-only` validates inputs without inference/video. `--base-port` chooses another
starting port if one is occupied.

To reopen completed feedback without rebuilding:

```text
python run.py serve-batch --manifest <batch-directory>/recommendations_manifest.json
```

Generated video, phase and events use native HDF5 elapsed time, so their seconds
can differ from older videos. Both legacy `data/*` and v3 `vision_data/*` /
`physics_data/*` layouts are supported. V3 camera poses are aligned by timestamp
over all chunks; unsupported gaps remain missing, not invented. An incompatible
file is reported. New-recording model selection does not constitute individual
geometry or clinical validation. No model training or remote inference is used.

## Maintained code

- `src/evidence_system/`: HDF5 processing, geometry, frozen phase inference, Ray
  events, storage and service.
- `apps/`: the existing recommendations and expert-study applications.
- `run.py`: the shared Windows/Linux entry.
- `config/rules/`: internal rules/source metadata; not machine-path settings.
- `references/`: saved R4 expert distribution used directly on every machine.
- `tests/`: source, clock, model, pipeline and service checks.

Detailed contracts: [directory input](docs/hdf5-directory-input.md),
[phase integration](docs/signal-phase-integration.md),
[data boundary](docs/data-boundary.md).

## Motion reference and frame review

The precomputed expert reference is saved at
[`references/r4_motion_windows_v3.json`](references/r4_motion_windows_v3.json)
and is not ignored by Git. Both the configuration template and local configuration
use this project-relative path. Commit this file with the project when migrating
through Git; no expert HDF5 files or R4 catalog are needed to use the distribution.
Its 39 recordings from five participants contain 5,860 windows, including 4,203
eligible windows. The embedded source paths are provenance, not runtime inputs.

Version 3 preserves every version-2 speed/removal value and adds two saved stroke
frequencies to each window. All times below are native simulator seconds:

| Field | Numerator | Denominator |
| --- | --- | --- |
| `stroke_frequency_hz` | Recorded stroke endpoints in `(start, end]` (`stroke_end_count`) | Full 5 seconds |
| `stroke_rate_per_s` | Distinct recorded stroke IDs on drilling rows in `[start, end]` (`stroke_count`) | `stroke_drilling_time_s` |

`stroke_drilling_time_s` follows the legacy drilling-mask formula: between observed
rows inside the window, sum each interval whose right-hand row is marked drilling,
capping each interval at one second. The old phase statistic used `t_video`; this
window reference uses native time. This is an estimated drilling-mask duration,
not recorded motor-on time. A zero denominator produces `null`, not zero frequency.
An overlapping stroke can count even when its endpoint is outside the window,
so the two numerators can differ. Source flags/IDs are reused without resegmentation.

`stroke_frequency_metadata` records the formula, input digest and source diagnostics.
P4T2, P4T4 and P4T6 contain nonfinite legacy turning angles; their original numbers
are retained but both frequency eligibility flags are false. Existing strokes can
also span gaps; this extension does not repair that legacy limitation. There are
3,714 coverage/source-qualified endpoint-rate windows and 3,321 drilling-time-rate
windows (the latter also require at least five drilling rows and positive time).
These fields are saved for later comparison; live comparisons and coaching still
use the existing speed/removal path. No frequency coaching rule has been enabled.
Version 2 remains available unchanged for archives.

Existing `process-hdf5`, `serve-batch`, and direct `serve` commands use the configured
reference automatically. An archived batch may retain its original absolute
reference path; the old `.local/references/` copy is preserved for those archives.

Optional maintainer operation, only when intentionally creating a new reference
version from the verified R4 catalog (not part of installation or migration):

```text
python run.py build-motion-reference --catalog /path/to/organized --output references/r4_motion_windows_new.json
```

Use a new output filename to preserve the existing reference, then update
`motion_reference` in `config.local.json` after validating the new version.

The reference uses participants with recorded training years >= 11, complete
manual phases, and hash-verified R4 all-frame pose tables. Post-incus is included
in incus. Five-second windows use native simulator seconds, one-second steps,
valid adjacent observations and explicit missing coverage. Each participant has
equal weight; recordings within a participant have equal weight. A participant's
own recordings are excluded when assessing that participant. References pool
specimens and assistance conditions; their manual phases differ in provenance
from the model-predicted phases of a new recording. Geometry review flags remain
unchanged. These are descriptive comparisons, not validated skill scores.

The recommendations page retains its existing feedback content. **Go to key
frame** displays the original source image and pauses playback; **Play context**
plays the surrounding video. Original source files must remain locally readable
for exact-frame display. The review video is resampled and can omit some source
frames, so seeking its timeline alone does not guarantee the original frame.
