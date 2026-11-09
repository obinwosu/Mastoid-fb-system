"""Build core frame and removed-voxel observations directly from raw HDF5."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..hdf5_clock import (
    MANIFEST_NAME,
    validate_generated_alignment,
)
from ..hdf5_reader import RecordingReader
from ..registry import RunRecord
from ..source_audit import inspect_phase_annotation
from ..source_config import ResolvedRunSources

DRILLING_TIMESTAMP_GAP_S = 0.2
DEFAULT_BURR_MM = 6.0
REMOVED_VOXEL_FIELDS = (
    "removed_voxel_id",
    "global_run_id",
    "removed_voxel_row",
    "t_abs",
    "t_video_s",
    "frame_idx_global",
    "x",
    "y",
    "z",
    "source_hdf5",
    "source_voxel_row",
    "source_timestamp_idx",
)


@dataclass
class CoreFrameBuild:
    frames: list[dict[str, Any]]
    removed_voxels: list[dict[str, Any]]
    phase_intervals: list[dict[str, Any]]
    manifest: dict[str, Any]


def _require_numeric_stack() -> tuple[Any, Any]:
    try:
        import h5py
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Core frame extraction requires h5py and numpy in the assessment environment."
        ) from exc
    return h5py, np


def _dataset_array(handle: Any, path: str, *, required: bool = True) -> Any:
    dataset = handle.get(path)
    if dataset is None:
        if required:
            raise KeyError(f"Missing required HDF5 dataset: {path}")
        return None
    return dataset[()]


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def resolve_frame_clock(
    frame_parts: Sequence[dict[str, Any]],
    alignment_file: Path | None = None,
    *,
    global_run_id: str | None = None,
    duration_s: float | None = None,
    raw_run_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bind explicit clock columns to the sorted native frame inventory.

    Only the allowlisted clock/index cells are accessed. Other CSV columns,
    including phase annotations, never become extraction or inference inputs.
    Legacy CSVs retain their configured association. Generated native clocks
    additionally require a hash receipt and exact per-frame source correspondence.
    """
    raw_times = [float(row["t_abs"]) for row in frame_parts]
    if not raw_times or any(not math.isfinite(value) for value in raw_times):
        raise ValueError("Frame clock requires finite native timestamps")
    origin = raw_times[0]
    manifest: dict[str, Any] = {
        "schema": "frame_clock_alignment_v1",
        "configured_global_run_id": global_run_id,
        "frame_count": len(frame_parts),
        "raw_time_origin_abs": origin,
        "raw_time_bounds_abs": [min(raw_times), max(raw_times)],
        "phase_columns_consumed": False,
        "native_pose_and_voxel_assignment_clock": "original_HDF5_data/time",
        "removed_event_clock_mapping": (
            "native absolute timestamp interpolated between aligned frame clocks; "
            "linear endpoint extrapolation outside observed frame bounds"
        ),
    }
    generated = None
    if alignment_file is None:
        aligned = [{"video_time_s": value - origin} for value in raw_times]
        manifest.update(
            source="native_relative_time",
            path=None,
            sha256=None,
            columns_used=[],
            world_frame_index_present=False,
            correspondence_validation="Derived directly from the native frame inventory",
        )
    else:
        if any(right <= left for left, right in zip(raw_times, raw_times[1:], strict=False)):
            raise ValueError(
                "Explicit frame alignment requires strictly increasing native timestamps"
            )
        path = alignment_file.expanduser().resolve()
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = set(reader.fieldnames or [])
            if not {"frame_idx_global", "video_time_s"}.issubset(columns):
                raise ValueError("Frame alignment requires frame_idx_global and video_time_s")
            used = ["frame_idx_global", "video_time_s"]
            if "world_frame_index" in columns:
                used.append("world_frame_index")
            clock_rows = [{key: row[key] for key in used} for row in reader]
        if (path.parent / MANIFEST_NAME).exists() or {
            "source_hdf5",
            "source_frame_idx",
            "t_abs",
        }.intersection(columns):
            if raw_run_dir is None:
                raise ValueError("Generated HDF5 clock requires the current raw source directory")
            generated = validate_generated_alignment(path, frame_parts, raw_run_dir)
            used = ["frame_idx_global", "source_hdf5", "source_frame_idx", "t_abs", "video_time_s"]
        if len(clock_rows) != len(frame_parts):
            raise ValueError("Frame alignment count differs from native frame inventory")
        aligned = []
        previous_world: int | None = None
        for index, row in enumerate(clock_rows):
            try:
                parsed_index = int(row["frame_idx_global"])
                mapped = float(row["video_time_s"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid frame alignment index or time") from exc
            if parsed_index != index:
                raise ValueError("Frame alignment indices must match sorted native rows0..N-1")
            if not math.isfinite(mapped) or mapped < 0:
                raise ValueError("Frame alignment video times must be finite and nonnegative")
            item: dict[str, Any] = {"video_time_s": mapped}
            if "world_frame_index" in row:
                try:
                    world = int(row["world_frame_index"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("Invalid world_frame_index provenance") from exc
                if world < 0 or previous_world is not None and world <= previous_world:
                    raise ValueError("World frame provenance must be nonnegative and increasing")
                item["world_frame_index"] = world
                previous_world = world
            aligned.append(item)
        if any(
            right["video_time_s"] <= left["video_time_s"]
            for left, right in zip(aligned, aligned[1:], strict=False)
        ):
            raise ValueError("Frame alignment video times must strictly increase")
        manifest.update(
            source="explicit_frame_alignment",
            path=str(path),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            columns_used=used,
            world_frame_index_present="world_frame_index" in used,
            correspondence_validation=(
                "Configured run binding, exact file hash, native frame count and ordered "
                "indices; equal-size wrong-record sidecars cannot be excluded by these "
                "columns alone"
            ),
        )
        if generated:
            manifest.update(generated)
    manifest["video_time_bounds_s"] = [aligned[0]["video_time_s"], aligned[-1]["video_time_s"]]
    if generated:
        if duration_s is not None and float(duration_s) != generated["duration_s"]:
            raise ValueError("Configured duration differs from the generated HDF5 clock")
        duration_s = generated["duration_s"]
    duration = aligned[-1]["video_time_s"] if duration_s is None else float(duration_s)
    if not math.isfinite(duration) or duration < aligned[-1]["video_time_s"] or duration < 0:
        raise ValueError("Configured phase duration must cover the aligned frame time range")
    manifest["duration_s"] = duration
    manifest["duration_source"] = (
        generated["duration_source"]
        if generated
        else "last_aligned_frame"
        if duration_s is None
        else "explicit_recording_duration"
    )
    return aligned, manifest


def map_native_event_times(event_times: Any, frame_times: Any, video_times: Any) -> Any:
    """Map event time without changing its native first-following-frame join."""
    _, np = _require_numeric_stack()
    events = np.asarray(event_times, dtype=float)
    raw = np.asarray(frame_times, dtype=float)
    mapped = np.asarray(video_times, dtype=float)
    if len(raw) == 1:
        return mapped[0] + events - raw[0]
    result = np.interp(events, raw, mapped)
    before, after = events < raw[0], events > raw[-1]
    result[before] = mapped[0] + (events[before] - raw[0]) * (mapped[1] - mapped[0]) / (
        raw[1] - raw[0]
    )
    result[after] = mapped[-1] + (events[after] - raw[-1]) * (mapped[-1] - mapped[-2]) / (
        raw[-1] - raw[-2]
    )
    return result


USER_CONTROL_FIELDS = ("device_clutch", "cam_clutch", "scope_button")


def _user_control_fields(values: Any) -> dict[str, Any]:
    """Clutch / microscope-button flags for one frame; None when not recorded."""
    if values is None:
        return {}
    out = {}
    for name, value in zip(USER_CONTROL_FIELDS, list(values), strict=False):
        value = int(value)
        out[name] = None if value < 0 else bool(value)
    return out


def _drilling_mask(
    frame_times: Any, removal_times: Any, gap_s: float = DRILLING_TIMESTAMP_GAP_S
) -> Any:
    _, np = _require_numeric_stack()
    frame_times = np.asarray(frame_times, dtype=float)
    removal_times = np.asarray(removal_times, dtype=float)
    mask = np.zeros(frame_times.shape, dtype=bool)
    if len(removal_times) < 2:
        return mask
    order = np.argsort(removal_times, kind="stable")
    times = removal_times[order]
    breaks = np.where(np.diff(times) >= gap_s)[0]
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [len(times) - 1]))
    for start_idx, end_idx in zip(starts, ends, strict=False):
        if int(start_idx) == int(end_idx):
            continue
        mask |= (frame_times >= times[start_idx]) & (frame_times <= times[end_idx])
    return mask


def _assign_voxels_to_frames(frame_times: Any, voxel_times: Any) -> tuple[Any, Any]:
    """Assign each voxel to the first frame at or after its removal timestamp."""

    _, np = _require_numeric_stack()
    frame_times = np.asarray(frame_times, dtype=float)
    voxel_times = np.asarray(voxel_times, dtype=float)
    assigned = np.searchsorted(frame_times, voxel_times, side="left")
    valid = assigned < len(frame_times)
    counts = np.bincount(assigned[valid], minlength=len(frame_times)).astype(int)
    return assigned, counts


def _burr_sizes_at_frames(
    frame_times: Any,
    change_times: Any,
    change_sizes: Any,
    default_burr_mm: float = DEFAULT_BURR_MM,
) -> Any:
    _, np = _require_numeric_stack()
    frame_times = np.asarray(frame_times, dtype=float)
    change_times = np.asarray(change_times, dtype=float)
    change_sizes = np.asarray(change_sizes, dtype=float)
    if not len(change_times):
        return np.full(len(frame_times), float(default_burr_mm), dtype=float)
    order = np.argsort(change_times, kind="stable")
    change_times = change_times[order]
    change_sizes = change_sizes[order]
    indices = np.searchsorted(change_times, frame_times, side="right") - 1
    result = np.full(len(frame_times), float(default_burr_mm), dtype=float)
    valid = indices >= 0
    result[valid] = change_sizes[indices[valid]]
    return result


def _phase_for_time(
    t_video_s: float,
    first_drilling_s: float,
    antrum_start_s: float,
    incus_start_s: float,
    incus_end_s: float,
) -> str:
    if t_video_s < first_drilling_s:
        return "pre_drilling"
    if t_video_s < antrum_start_s:
        return "early_surface"
    if t_video_s < incus_start_s:
        return "antrum"
    if t_video_s <= incus_end_s:
        return "incus"
    return "outside_annotated_phase"


def _build_phase_intervals(
    *,
    record: RunRecord,
    first_drilling_s: float,
    record_end_s: float,
    phase_row: dict[str, Any],
    frame_times_video: Any,
) -> list[dict[str, Any]]:
    _, np = _require_numeric_stack()
    antrum = float(phase_row["antrum_start_sec"])
    incus = float(phase_row["incus_start_sec"])
    incus_end = float(phase_row["incus_end_sec"])
    definitions = [
        ("pre_drilling", 0.0, first_drilling_s, "raw_record+first_drilling_frame"),
        ("early_surface", first_drilling_s, antrum, "first_drilling_frame+human_annotation"),
        ("antrum", antrum, incus, "human_annotation"),
        ("incus", incus, incus_end, "human_annotation"),
    ]
    if record_end_s > incus_end:
        definitions.append(
            ("outside_annotated_phase", incus_end, record_end_s, "record_time+human_annotation")
        )

    times = np.asarray(frame_times_video, dtype=float)
    assigned_phases = np.asarray(
        [_phase_for_time(value, first_drilling_s, antrum, incus, incus_end) for value in times],
        dtype=object,
    )
    rows = []
    for index, (phase, start, end, boundary_source) in enumerate(definitions, 1):
        # Count from the same phase assignment used on frame rows. This avoids
        # endpoint drift between interval summaries and stored frame phase IDs.
        observed_mask = assigned_phases == phase
        observed = times[observed_mask]
        rows.append(
            {
                "phase_id": f"{record.global_run_id}:phase:{phase}",
                "phase_order": index,
                "phase": phase,
                "annotation_start_s": start,
                "annotation_end_s": end,
                "observed_start_s": float(observed[0]) if len(observed) else None,
                "observed_end_s": float(observed[-1]) if len(observed) else None,
                "observed_frame_count": int(len(observed)),
                "boundary_source": boundary_source,
                "phase_annotation_id": record.phase_annotation_id,
            }
        )
    return rows


def _git_commit(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def extract_core_frames(
    record: RunRecord,
    sources: ResolvedRunSources,
    *,
    project_root: Path,
) -> CoreFrameBuild:
    h5py, np = _require_numeric_stack()
    reader = RecordingReader(sources.raw_run_dir)
    hdf5_paths = reader.paths
    if not hdf5_paths:
        raise FileNotFoundError(f"No HDF5 chunks found in {sources.raw_run_dir}")

    frame_parts: list[dict[str, Any]] = []
    voxel_parts: list[dict[str, Any]] = []
    burr_times: list[float] = []
    burr_sizes: list[float] = []
    voxel_volumes: list[float] = []
    skipped_metadata_only_files: list[str] = []

    for chunk_order, path in enumerate(hdf5_paths):
        with reader.open_chunk(path) as handle:
            raw_times = _dataset_array(handle, "data/time", required=False)
            if reader.chunk_role(path) == "metadata_only":
                skipped_metadata_only_files.append(path.name)
                continue
            if reader.chunk_role(path) == "frame_data":
                times = np.asarray(raw_times, dtype=float).reshape(-1)
                drill = np.asarray(
                    _dataset_array(handle, "data/pose_mastoidectomy_drill"), dtype=float
                )
                volume = np.asarray(
                    _dataset_array(handle, "data/pose_mastoidectomy_volume"), dtype=float
                )
                camera = np.asarray(_dataset_array(handle, "data/pose_main_camera"), dtype=float)
                # Optional (FIVRS recorder): device_clutch, cam_clutch, scope_button.
                controls = _dataset_array(handle, "data/user_controls", required=False)
                controls = None if controls is None else np.asarray(controls).reshape(len(times), -1)
                # Depth is optional: the FIVRS RTX recorder saves colour frames only.
                has_depth = handle.get("data/depth") is not None
                expected = len(times)
                if any(len(array) != expected for array in (drill, volume, camera)):
                    raise ValueError(f"Frame modality length mismatch in {path.name}")

                for local_idx in range(expected):
                    frame_parts.append(
                        {
                            "chunk_order": chunk_order,
                            "source_hdf5": path.name,
                            "source_frame_idx": local_idx,
                            "t_abs": float(times[local_idx]),
                            "drill_pose": drill[local_idx],
                            "volume_pose": volume[local_idx],
                            "camera_pose": camera[local_idx],
                            "user_controls": None if controls is None else controls[local_idx],
                            "has_depth": has_depth,
                        }
                    )

            change_times = _dataset_array(handle, "burr_change/time_stamp", required=False)
            change_sizes = _dataset_array(handle, "burr_change/burr_size", required=False)
            if change_times is not None and change_sizes is not None:
                change_times = np.asarray(change_times, dtype=float).reshape(-1)
                change_sizes = np.asarray(change_sizes, dtype=float).reshape(-1)
                n_changes = min(len(change_times), len(change_sizes))
                burr_times.extend(float(value) for value in change_times[:n_changes])
                burr_sizes.extend(float(value) for value in change_sizes[:n_changes])

            volume_value = _dataset_array(handle, "metadata/voxel_volume", required=False)
            if volume_value is not None:
                scalar = volume_value.item() if hasattr(volume_value, "item") else volume_value
                numeric = _finite_float(scalar)
                if numeric is not None:
                    voxel_volumes.append(numeric)

            removal_times = _dataset_array(
                handle, "voxels_removed/voxel_time_stamp", required=False
            )
            removed = _dataset_array(handle, "voxels_removed/voxel_removed", required=False)
            if removal_times is None or removed is None or not len(removal_times):
                continue
            removal_times = np.asarray(removal_times, dtype=float).reshape(-1)
            removed = np.asarray(removed)
            if removed.ndim != 2 or removed.shape[1] < 4:
                raise ValueError(f"Unexpected voxel_removed shape in {path.name}: {removed.shape}")
            if not len(removed):
                continue
            timestamp_indices = np.asarray(removed[:, 0], dtype=int)
            if timestamp_indices.min() < 0 or timestamp_indices.max() >= len(removal_times):
                raise ValueError(f"Invalid voxel timestamp indices in {path.name}")
            for local_row, (row, timestamp_index) in enumerate(
                zip(removed, timestamp_indices, strict=False)
            ):
                voxel_parts.append(
                    {
                        "chunk_order": chunk_order,
                        "source_hdf5": path.name,
                        "source_voxel_row": local_row,
                        "source_timestamp_idx": int(timestamp_index),
                        "t_abs": float(removal_times[timestamp_index]),
                        "x": int(row[1]),
                        "y": int(row[2]),
                        "z": int(row[3]),
                    }
                )

    frame_parts.sort(key=lambda row: (row["t_abs"], row["chunk_order"], row["source_frame_idx"]))
    voxel_parts.sort(key=lambda row: (row["t_abs"], row["chunk_order"], row["source_voxel_row"]))
    frame_times = np.asarray([row["t_abs"] for row in frame_parts], dtype=float)
    voxel_times = np.asarray([row["t_abs"] for row in voxel_parts], dtype=float)
    if len(frame_times) == 0 or np.any(np.diff(frame_times) < 0):
        raise ValueError("Frame timestamps are empty or non-monotonic after merge")
    experiment_start = float(frame_times[0])
    clock_rows, clock_manifest = resolve_frame_clock(
        frame_parts,
        sources.frame_alignment_file,
        global_run_id=record.global_run_id,
        duration_s=sources.phase_duration_s,
        raw_run_dir=sources.raw_run_dir,
    )
    if reader.schema == "v3":
        clock_manifest["native_pose_and_voxel_assignment_clock"] = (
            "vision_data/continuous_data/time and "
            "physics_data/intermittent_data/voxels_removed/voxel_time_stamp"
        )
    t_video = np.asarray([row["video_time_s"] for row in clock_rows], dtype=float)
    voxel_video_times = (
        map_native_event_times(voxel_times, frame_times, t_video)
        if sources.frame_alignment_file is not None
        else voxel_times - experiment_start
    )
    assigned_frames, frame_removed_counts = _assign_voxels_to_frames(frame_times, voxel_times)
    valid_voxels = assigned_frames < len(frame_times)
    drilling = _drilling_mask(frame_times, voxel_times)
    automatic_phase = sources.phase_model_bundle is not None
    if not np.any(drilling) and not automatic_phase:
        raise ValueError("No drilling frames were derived from removal timestamps")
    burr_by_frame = _burr_sizes_at_frames(
        frame_times,
        np.asarray(burr_times, dtype=float),
        np.asarray(burr_sizes, dtype=float),
    )
    unique_volumes = sorted({round(value, 12) for value in voxel_volumes})
    if len(unique_volumes) != 1:
        raise ValueError(f"Expected one voxel volume across chunks; found {unique_volumes}")
    voxel_volume_mm3 = float(voxel_volumes[0])
    cumulative = np.cumsum(frame_removed_counts)
    dt = np.diff(frame_times, prepend=np.nan)
    dt[dt <= 0] = np.nan

    first_drilling_s = float(t_video[np.where(drilling)[0][0]]) if np.any(drilling) else None
    last_drilling_s = float(t_video[np.where(drilling)[0][-1]]) if np.any(drilling) else None
    if automatic_phase:
        phase_audit = {
            "valid": True,
            "source_kind": "model_phase_prediction",
            "status": "pending_geometry_and_inference",
        }
        phase_row = None
        phase_intervals = []
    else:
        if sources.phase_annotation_file is None:
            raise ValueError("Manual phase annotation is required outside automatic mode")
        phase_audit = inspect_phase_annotation(sources.phase_annotation_file, record)
        if not phase_audit["valid"]:
            raise ValueError(f"Invalid phase annotation for {record.run_id}")
        phase_row = dict(phase_audit["row"])
        phase_intervals = _build_phase_intervals(
            record=record,
            first_drilling_s=first_drilling_s,
            record_end_s=float(t_video[-1]),
            phase_row=phase_row,
            frame_times_video=t_video,
        )

    voxel_start = [None] * len(frame_parts)
    voxel_end = [None] * len(frame_parts)
    removed_voxel_rows: list[dict[str, Any]] = []
    for sorted_idx, (source, assigned, valid) in enumerate(
        zip(voxel_parts, assigned_frames, valid_voxels, strict=False)
    ):
        frame_index = int(assigned) if valid else None
        if frame_index is not None:
            if voxel_start[frame_index] is None:
                voxel_start[frame_index] = sorted_idx
            voxel_end[frame_index] = sorted_idx + 1
        removed_voxel_rows.append(
            {
                "removed_voxel_id": f"{record.global_run_id}:removed_voxel:{sorted_idx:07d}",
                "global_run_id": record.global_run_id,
                "removed_voxel_row": sorted_idx,
                "t_abs": source["t_abs"],
                "t_video_s": float(voxel_video_times[sorted_idx]),
                "frame_idx_global": frame_index,
                "x": source["x"],
                "y": source["y"],
                "z": source["z"],
                "source_hdf5": source["source_hdf5"],
                "source_voxel_row": source["source_voxel_row"],
                "source_timestamp_idx": source["source_timestamp_idx"],
            }
        )

    frame_rows: list[dict[str, Any]] = []
    drilling_sequence_index = 0
    for index, source in enumerate(frame_parts):
        drill_pose = source["drill_pose"]
        volume_pose = source["volume_pose"]
        camera_pose = source["camera_pose"]
        phase = (
            None
            if automatic_phase
            else _phase_for_time(
                float(t_video[index]),
                first_drilling_s,
                float(phase_row["antrum_start_sec"]),
                float(phase_row["incus_start_sec"]),
                float(phase_row["incus_end_sec"]),
            )
        )
        drilling_frame_idx = drilling_sequence_index if drilling[index] else None
        if drilling[index]:
            drilling_sequence_index += 1
        frame_rows.append(
            {
                "frame_evidence_id": f"{record.global_run_id}:frame:{index:06d}",
                "global_run_id": record.global_run_id,
                "frame_idx_global": index,
                "source_hdf5": source["source_hdf5"],
                "source_frame_idx": source["source_frame_idx"],
                "t_abs": source["t_abs"],
                "t_video_s": float(t_video[index]),
                "frame_dt_s": None if np.isnan(dt[index]) else float(dt[index]),
                "phase": phase,
                "phase_annotation_id": None if automatic_phase else record.phase_annotation_id,
                "is_drilling_frame": bool(drilling[index]),
                "drilling_frame_idx": drilling_frame_idx,
                "burr_mm": float(burr_by_frame[index]),
                "drill_x": float(drill_pose[0]),
                "drill_y": float(drill_pose[1]),
                "drill_z": float(drill_pose[2]),
                "drill_qx": float(drill_pose[3]),
                "drill_qy": float(drill_pose[4]),
                "drill_qz": float(drill_pose[5]),
                "drill_qw": float(drill_pose[6]),
                "volume_x": float(volume_pose[0]),
                "volume_y": float(volume_pose[1]),
                "volume_z": float(volume_pose[2]),
                "volume_qx": float(volume_pose[3]),
                "volume_qy": float(volume_pose[4]),
                "volume_qz": float(volume_pose[5]),
                "volume_qw": float(volume_pose[6]),
                "camera_x": float(camera_pose[0]),
                "camera_y": float(camera_pose[1]),
                "camera_z": float(camera_pose[2]),
                "camera_qx": float(camera_pose[3]),
                "camera_qy": float(camera_pose[4]),
                "camera_qz": float(camera_pose[5]),
                "camera_qw": float(camera_pose[6]),
                "rgb_frame_ref": (
                    f"{source['source_hdf5']}:{reader.dataset_path('data/l_img')}:"
                    f"{source['source_frame_idx']}"
                ),
                "depth_frame_ref": (
                    f"{source['source_hdf5']}:{reader.dataset_path('data/depth')}:"
                    f"{source['source_frame_idx']}"
                    if source.get("has_depth", True)
                    else ""
                ),
                "frame_removed_voxels": int(frame_removed_counts[index]),
                "frame_removed_voxels_cum": int(cumulative[index]),
                "frame_removed_voxels_per_s": (
                    None if np.isnan(dt[index]) else float(frame_removed_counts[index] / dt[index])
                ),
                "frame_removed_mm3": float(frame_removed_counts[index] * voxel_volume_mm3),
                "frame_removed_mm3_cum": float(cumulative[index] * voxel_volume_mm3),
                "frame_removed_mm3_per_s": (
                    None
                    if np.isnan(dt[index])
                    else float(frame_removed_counts[index] * voxel_volume_mm3 / dt[index])
                ),
                **_user_control_fields(source.get("user_controls")),
                "removed_voxel_row_start": voxel_start[index],
                "removed_voxel_row_end_exclusive": voxel_end[index],
            }
        )

    if sources.frame_alignment_file is not None:
        for frame, clock in zip(frame_rows, clock_rows, strict=False):
            frame["t_record_s"] = frame["t_abs"] - experiment_start
            if "world_frame_index" in clock:
                frame["world_frame_index"] = clock["world_frame_index"]
    if reader.schema == "v3":
        camera_fields = (
            "camera_x",
            "camera_y",
            "camera_z",
            "camera_qx",
            "camera_qy",
            "camera_qz",
            "camera_qw",
        )
        for frame in frame_rows:
            for name in camera_fields:
                frame[name] = _finite_float(frame[name])
            frame["camera_pose_valid"] = all(frame[name] is not None for name in camera_fields)

    invariants = {
        "frame_times_monotonic": bool(np.all(np.diff(frame_times) >= 0)),
        "frame_ids_unique": len({row["frame_evidence_id"] for row in frame_rows})
        == len(frame_rows),
        "assigned_removed_voxel_conservation": int(np.sum(frame_removed_counts))
        == int(np.sum(valid_voxels)),
        "modalities_have_source_references": all(row["rgb_frame_ref"] for row in frame_rows)
        and (
            all(row["depth_frame_ref"] for row in frame_rows)
            or not any(row["depth_frame_ref"] for row in frame_rows)
        ),
        (
            "automatic_phase_deferred_without_manual_input"
            if automatic_phase
            else "phase_annotation_valid"
        ): bool(phase_audit["valid"]),
    }
    manifest = {
        "schema": "core_frame_extraction_v1",
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "generator_version": "frame_evidence_pipeline_v1_core",
        "git_commit": _git_commit(project_root),
        "dataset_id": record.dataset_id,
        "run_id": record.run_id,
        "global_run_id": record.global_run_id,
        "primary_raw_run_dir": str(sources.raw_run_dir),
        "auxiliary_phase_annotation": None
        if automatic_phase
        else str(sources.phase_annotation_file),
        "phase_source_kind": "model_phase_prediction"
        if automatic_phase
        else "human_phase_annotation",
        "phase_status": "pending_geometry_and_inference"
        if automatic_phase
        else "validated_manual_annotation",
        "clock_alignment": clock_manifest,
        "validation_only_legacy_frame_file": str(
            sources.raw_run_dir / "frame_level_spatial_all_frames_with_voxel.csv"
        ),
        "hdf5_file_count": len(hdf5_paths),
        "hdf5_frame_data_file_count": (
            sum(reader.chunk_role(path) == "frame_data" for path in hdf5_paths)
        ),
        "skipped_metadata_only_files": skipped_metadata_only_files,
        "frame_count": len(frame_rows),
        "removed_voxel_count": len(removed_voxel_rows),
        "assigned_removed_voxel_count": int(np.sum(valid_voxels)),
        "unassigned_removed_voxel_count": int(np.sum(~valid_voxels)),
        "unassigned_removed_voxel_policy": (
            "preserved_as_raw_evidence_but_excluded_from_frame_aggregation"
        ),
        "drilling_frame_count": int(np.sum(drilling)),
        "experiment_start_time_abs": experiment_start,
        "record_end_s": float(t_video[-1]),
        "first_drilling_s": first_drilling_s,
        "last_drilling_s": last_drilling_s,
        "voxel_volume_mm3": voxel_volume_mm3,
        "drilling_timestamp_gap_s": DRILLING_TIMESTAMP_GAP_S,
        "default_burr_mm": DEFAULT_BURR_MM,
        "phase_annotation": phase_row,
        "invariants": invariants,
        "ready_for_edt_enrichment": all(invariants.values()),
    }
    if reader.schema == "v3":
        manifest["hdf5_reader"] = reader.manifest()
        manifest["physics_only_files"] = [
            p.name for p in reader.paths if reader.chunk_role(p) == "physics_only"
        ]
        manifest["burr_size_provenance"] = {
            "change_event_count": len(burr_times),
            "default_diameter_mm": DEFAULT_BURR_MM,
            "policy": "Assume the existing 6 mm initial burr until the first explicit change event",
            "initial_size_measured": False,
        }
    return CoreFrameBuild(
        frames=frame_rows,
        removed_voxels=removed_voxel_rows,
        phase_intervals=phase_intervals,
        manifest=manifest,
    )


def compare_legacy_core_frames(build: CoreFrameBuild, legacy_path: Path) -> dict[str, Any]:
    """Explicit validation-only comparison; never used to construct new frames."""

    if not legacy_path.is_file():
        return {"available": False, "legacy_path": str(legacy_path)}
    with legacy_path.open("r", encoding="utf-8-sig", newline="") as handle:
        legacy = list(csv.DictReader(handle))
    if len(legacy) != len(build.frames):
        return {
            "available": True,
            "legacy_path": str(legacy_path),
            "row_count_match": False,
            "new_row_count": len(build.frames),
            "legacy_row_count": len(legacy),
            "all_match": False,
        }

    max_errors = {
        "t_abs": 0.0,
        "t_video": 0.0,
        "drill_position": 0.0,
        "burr_mm": 0.0,
        "removed_voxels_per_s": 0.0,
        "removed_mm3_per_s": 0.0,
    }
    mismatches = {
        "frame_idx": 0,
        "drilling_frame_idx": 0,
        "drilling": 0,
        "removed_voxels": 0,
        "cumulative": 0,
        "removed_voxels_per_s": 0,
        "removed_mm3_per_s": 0,
    }

    def numeric_or_none(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    for new, old in zip(build.frames, legacy, strict=False):
        if int(old["frame_idx_global"]) != int(new["frame_idx_global"]):
            mismatches["frame_idx"] += 1
        old_drilling = str(old["is_drilling_frame"]).strip().lower() in {"1", "true", "t", "yes"}
        if old_drilling != bool(new["is_drilling_frame"]):
            mismatches["drilling"] += 1
        old_drilling_index = numeric_or_none(old.get("frame_idx_vrm"))
        new_drilling_index = numeric_or_none(new.get("drilling_frame_idx"))
        if old_drilling_index != new_drilling_index:
            mismatches["drilling_frame_idx"] += 1
        if int(float(old["frame_removed_voxels"])) != int(new["frame_removed_voxels"]):
            mismatches["removed_voxels"] += 1
        if int(float(old["frame_removed_voxels_cum"])) != int(new["frame_removed_voxels_cum"]):
            mismatches["cumulative"] += 1
        max_errors["t_abs"] = max(
            max_errors["t_abs"], abs(float(old["t_abs"]) - float(new["t_abs"]))
        )
        max_errors["t_video"] = max(
            max_errors["t_video"], abs(float(old["t_video"]) - float(new["t_video_s"]))
        )
        position_error = max(
            abs(float(old[column]) - float(new[column]))
            for column in ("drill_x", "drill_y", "drill_z")
        )
        max_errors["drill_position"] = max(max_errors["drill_position"], position_error)
        max_errors["burr_mm"] = max(
            max_errors["burr_mm"], abs(float(old["burr_mm"]) - float(new["burr_mm"]))
        )
        for mismatch_name, old_column, new_column, error_name in (
            (
                "removed_voxels_per_s",
                "frame_removed_voxels_per_s",
                "frame_removed_voxels_per_s",
                "removed_voxels_per_s",
            ),
            (
                "removed_mm3_per_s",
                "frame_removed_mm3_per_s",
                "frame_removed_mm3_per_s",
                "removed_mm3_per_s",
            ),
        ):
            old_value = numeric_or_none(old.get(old_column))
            new_value = numeric_or_none(new.get(new_column))
            if old_value is None or new_value is None:
                if old_value is not None or new_value is not None:
                    mismatches[mismatch_name] += 1
            else:
                error = abs(old_value - new_value)
                max_errors[error_name] = max(max_errors[error_name], error)
                if error > 1e-9:
                    mismatches[mismatch_name] += 1

    all_match = (
        not any(mismatches.values())
        and max_errors["t_abs"] <= 1e-9
        and max_errors["t_video"] <= 1e-9
        and max_errors["drill_position"] <= 1e-12
        and max_errors["burr_mm"] <= 1e-12
        and max_errors["removed_voxels_per_s"] <= 1e-9
        and max_errors["removed_mm3_per_s"] <= 1e-9
    )
    return {
        "available": True,
        "legacy_path": str(legacy_path),
        "role": "validation_only",
        "row_count_match": True,
        "new_row_count": len(build.frames),
        "legacy_row_count": len(legacy),
        "mismatches": mismatches,
        "max_absolute_errors": max_errors,
        "all_match": all_match,
    }


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"Cannot write empty artifact: {path.name}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def render_core_audit(build: CoreFrameBuild, regression: dict[str, Any]) -> str:
    manifest = build.manifest
    lines = [
        f"# Core Frame Audit: {manifest['run_id']}",
        "",
        f"- Frames built directly from HDF5: {manifest['frame_count']}",
        f"- Removed voxels preserved: {manifest['removed_voxel_count']}",
        f"- Drilling frames: {manifest['drilling_frame_count']}",
        f"- Record duration: {manifest['record_end_s']:.6f} s",
        f"- First drilling frame: {manifest['first_drilling_s']} s",
        f"- Last drilling frame: {manifest['last_drilling_s']} s",
        f"- Ready for EDT enrichment: **{manifest['ready_for_edt_enrichment']}**",
        "",
        "## Invariants",
        "",
    ]
    lines.extend(
        f"- {'PASS' if passed else 'FAIL'}: `{name}`"
        for name, passed in manifest["invariants"].items()
    )
    lines.extend(["", "## Legacy Stage-A regression", ""])
    if not regression.get("available"):
        lines.append("- Legacy comparison file unavailable.")
    else:
        lines.append(f"- Role: `{regression.get('role')}`")
        lines.append(f"- Exact comparison passed: **{regression.get('all_match')}**")
        lines.append(
            f"- Row counts: {regression.get('new_row_count')} new / "
            f"{regression.get('legacy_row_count')} legacy"
        )
        for name, count in regression.get("mismatches", {}).items():
            lines.append(f"- {name} mismatches: {count}")
    lines.extend(
        [
            "",
            "The legacy CSV was opened only after the new frames were constructed.",
            "It is a regression target and not an extraction input.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_core_frame_build(
    output_dir: Path,
    build: CoreFrameBuild,
    regression: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "frame_observations_core.csv", build.frames)
    removed_path = output_dir / "removed_voxel_observations.csv"
    if build.removed_voxels:
        _write_csv(removed_path, build.removed_voxels)
    else:
        with removed_path.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerow(REMOVED_VOXEL_FIELDS)
    if build.phase_intervals:
        _write_csv(output_dir / "phase_intervals.csv", build.phase_intervals)
    else:
        (output_dir / "phase_intervals.csv").write_text(
            "phase_id,phase_order,phase,annotation_start_s,annotation_end_s,boundary_source\n",
            encoding="utf-8",
        )
    (output_dir / "legacy_stage_a_regression.json").write_text(
        json.dumps(regression, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output_dir / "extraction_manifest.json").write_text(
        json.dumps(build.manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output_dir / "CORE_FRAME_AUDIT.md").write_text(
        render_core_audit(build, regression), encoding="utf-8"
    )
