# HDF5 directory input

The directory entry creates alignment, video, run identity, anatomy configuration,
frozen offline phase predictions, events and feedback without per-recording CSV
or JSON editing. It leaves both existing webpage applications unchanged.

## Installed machine locations

Configure the repository-root `config.local.json` once from
`config.example.json`. It locates the simulator anatomy
sources, trusted R4 correction table and installed frozen phase bundle.
These are installation paths, not fields to fill for each recording.
No model fitting, package download or remote inference occurs.

`simulator_root` contains `scripts/study_gui/gui_setup.yaml` and its `ADF/`
files. `edt_root` independently contains the model directories, for example
`edt_grids_LT151_256/` with its 16 EDT files. These roots may be the same or
different locations. The model identity, grid geometry and pinned R4 EDT content
hashes are checked after relocation. Relative paths are resolved from the project
root. Copy the unchanged correction CSV and the whole three-file model bundle.
Use one Python 3.12 environment installed from the root `requirements.txt`;
the [README](../README.md) gives Windows/Linux commands. The active Python runs
both processing and phase inference; there is no separate interpreter setting.
Rebuild results after moving
machines because saved database/source bindings contain absolute paths.

## Run

From the repository root on Windows or Linux:

```text
python run.py process-hdf5 --input-dir ./recordings --serve
```

`--anatomy-model` means the actual anatomical specimen, not the phase network.
The selected offline MS-TCN++ remains the installed model. For the existing R4
cohort, omit `--anatomy-model` (default `auto`): the exact known recording identity
selects its corrected anatomy and EDT automatically, including mixed-model batches.
An explicit specimen that contradicts a historical correction is rejected.
For new recordings, `auto` reads only the anatomy identity from the local
`metadata.json` field `volume_adf`, for example `/sim/ADF/volume_256_RT138.yaml`.
That original path is evidence of the model name; it is not opened on this machine.
Local anatomy resources still come from `config.local.json`. If metadata is absent,
give an explicit `--anatomy-model LT151`. A conflict between the explicit selection
and recorded metadata is rejected. Participant names and free-text notes do not
enter phase inference or assessment.

The input can be a single recording directory or a parent containing multiple
recording directories. **One directory represents one recording.** All direct
`.hdf5`/`.h5` children (case insensitive) are its chunks. Do not mix independent
sessions into the same folder. Timestamps cannot identify separate disjoint
sessions reliably. Overlapping chunk clocks, unsupported schemas, corrupt chunks
and ambiguous geometry fail explicitly; failed cases remain in the batch report
while other cases can complete. Valid metadata-only chunks are preserved in the
inventory. Recordings shorter than one second cannot supply a phase-model step
and are rejected without inventing a longer duration. The supported layouts are
legacy `data/*` / `metadata/*` and v3 `vision_data/continuous_data/*` /
`physics_data/*` / `metadata_v3/*`. Unknown or mixed layouts fail explicitly.

`--prepare-only` generates and validates automatic inputs without
phase inference or video encoding. `--output-dir` chooses a
new output location. The default is a unique directory under `results/hdf5_batches`.
Existing results are never overwritten, and outputs cannot be nested inside the
input root. Source recordings are not changed.

## Automatic outputs

Each result contains:

- `clock/alignment.csv`: native HDF5 filename, local/global frame indices, absolute
  timestamp and generated video time.
- `clock/hdf5_clock_manifest.json`: exact source inventory hashes and clock rule.
- `config/runs.local.csv`, `config/source_paths.local.json`,
  `config/anatomy_profiles.local.json`: generated internal configuration.
- `config/anatomy_resolution.json`: selected specimen and geometry provenance.
- `stages/`: geometry measurements, frozen phases, events, relations and Ray-v3.
- `review_video.webm`, `review_video_manifest.json`, `evidence.sqlite3` and
  `build_summary.json`: the existing website inputs.

The batch root has `batch_summary.json` and, when cases complete,
`recommendations_manifest.json`. `--serve` starts the unchanged recommendations
application for each completed case on separate local ports (default 8810 onward).
The existing case selector links those services. Ctrl+C stops that batch's servers.
An occupied port is reported; existing services are not terminated or replaced.
To reopen completed results without rebuilding:

```text
python run.py serve-batch --manifest /results/batch/recommendations_manifest.json
```

## Time and anatomy contract

Alignment is generated directly from native HDF5 timestamps:
`video_time_s = data/time - first_sorted_frame_time`. It uses no original video,
`world_timestamps.npy`, historical frame CSV, manual phase or reference feedback.
The generated video, phase features and temporal events share this same clock.
The video samples nearest source images at the configured frame rate and covers
the last source frame. Source order, color and decoding are checked by the existing
video writer. Source hashes and exact frame correspondence bind generated clocks.

This is a new inference time axis, not the older world-video reference clock.
Historical video durations, reported event seconds and phase scores must not be
copied into these results. Frozen weights and feature definitions remain unchanged;
no new prediction-accuracy claim follows from an operational build.

Known R4 recordings retain the strict per-recording correction and all existing
quality flags. New recordings identify a supported actual specimen through
metadata or an explicit selection and use verified current simulator model sources.
That verifies the model parameters,
not the unknown recording's geometry or clinician review. The source preset assumes
the new recording used the same simulator configuration. EDT files are selected
from the matching GUI/ADF model and verified; anatomy is not guessed from a short
folder token or inferred from surgical video. Unavailable or unsupported model
sources are reported instead of borrowing another specimen's grid.

The root `motion_reference` setting defaults to the saved, Git-visible
`references/r4_motion_windows_v6.json` and binds that portable R4 five-second
reference to every completed case. Deployment does not rebuild the expert
distribution or read its original source paths. Set the setting to `null` to
disable expert motion comparisons. New builds save native-time motion windows;
older builds can derive them from their bound frame observations when served.
No reference means no expert motion comparison, rather than an invented normal
range. Existing safety and Ray coaching retain their separate rules; the
requested Ray duration gate remains 0.3 seconds. User and clinician review flags
remain false. See the root README for reference construction and migration.

## Simulator format changes

迁移机器只改根目录 `config.local.json` 中的资源路径。HDF5 格式变化与机器路径
是两回事，字段映射集中在 `src/evidence_system/hdf5_reader.py`，不用到各个分析
文件里逐一改路径。现有读入契约如下：

| 数据 | 旧版 | v3 |
| --- | --- | --- |
| 图像时间、左图、深度、drill/volume pose | `data/` 下的 `time`、`l_img`、`depth`、`pose_mastoidectomy_*` | 同名字段在 `vision_data/continuous_data/` 下 |
| 相机 pose | `data/pose_main_camera`，逐图像帧对应 | `physics_data/continuous_data/high_frequency_poses/main_camera_pose/pose` 与 `time_stamp` |
| 去骨记录 | `voxels_removed/voxel_removed` 与 `voxel_time_stamp` | 同名字段在 `physics_data/intermittent_data/voxels_removed/` 下 |
| 相机内外参、单体素体积 | `metadata/` | `metadata_v3/` |
| burr 改变事件（如果有） | `burr_change/` | `physics_data/intermittent_data/burr_change/` |

- 如果只是字段改名，修改 reader 中 `_canonical_path` 和相机路径常量，再检查新文件。
- 如果 pose 单位、坐标方向、深度含义或体素索引规则变了，需要核对几何转换；
  不能靠改字段名或选择另一个 EDT 修好。
- 相机时间和图像时间独立，先合并全目录的相机流，再按时间线性插值位置、SLERP
  插值旋转。超过 0.1 秒的相机采样空洞不插值，也不向外推算。保留图像帧，
  相机 pose 标为缺失，依赖相机的 visibility/Ray 标为不可评估。
- 去骨表第一列仍是**该 HDF5 分块内**的时间索引，不能合并时间向量后重解释。
- 图像/深度按帧读取；不把全部图像放进内存。输入 HDF5 从不修改。

输出 `clock/hdf5_clock_manifest.json` 和各 stage 的 manifest 记录识别到的格式、
字段映射和相机覆盖情况。新增格式要先在新结果目录验证，原结果继续保留。
这支持的是已检查到的 v3 结构；文件本身没有声明可核实的软件版本号，不能
据此断言它是 simulator 的最新版。

V3 的预检查和完整构建还会核对声明的米/四元数/CV 约定、单体素体积的 mm³
单位，以及每帧 volume pose 与所选 ADF 和 recorder scale 是否一致。若不一致，
停止该录像的分析。匹配成功只证明这些来源合同一致，不等于完整录制几何或临床验证。
没有 burr 变化事件时继续沿用已约定的初始 6 mm 假设，并在 core manifest 中标明
`initial_size_measured=false`；不能将默认值称为本次录制实测值。

真实 v3 样例已发现视觉流数秒的采样空洞。视频会按原始时间保留对应停帧；
phase 在 1 Hz 网格上给出的边界是模型估计，空洞内没有对应实测画面。
相机/深度的严格逐像素对应尚未由录制端配置确认，旧 depth candidate 继续保持
候选证据含义。Ray-v3 使用 camera pose 和体素占据关系，不使用 depth 图。
未知录像的 phase 准确率、事件准确率均不能从软件跑通或旧数据成绩推断。
