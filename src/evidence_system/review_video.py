"""Build a browser-reviewable video aligned to canonical simulator time."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hdf5_clock import SOURCE, read_hdf5_frame_inventory, validate_generated_alignment
from .hdf5_reader import RecordingReader
from .registry import RunRecord
from .source_config import ResolvedRunSources


@dataclass(frozen=True)
class FrameReference:
    timestamp_s: float
    source_path: Path
    source_index: int


def _require_video_dependencies() -> tuple[Any, Any, Any]:
    try:
        import cv2  # type: ignore
        import h5py  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Review-video generation requires the pipeline vision dependencies "
            "(h5py, numpy, and opencv-python)."
        ) from exc
    return cv2, h5py, np


def _index_source_frames(raw_run_dir: Path) -> list[FrameReference]:
    rows, _ = read_hdf5_frame_inventory(raw_run_dir)
    return [
        FrameReference(row["t_abs"], raw_run_dir / row["source_hdf5"], row["source_frame_idx"])
        for row in rows
    ]


def _nearest_source_indices(source_times: Any, target_times: Any, np: Any) -> Any:
    right = np.searchsorted(source_times, target_times, side="left")
    right = np.clip(right, 0, len(source_times) - 1)
    left = np.clip(right - 1, 0, len(source_times) - 1)
    choose_left = np.abs(target_times - source_times[left]) <= np.abs(
        source_times[right] - target_times
    )
    return np.where(choose_left, left, right)


def build_review_video(
    record: RunRecord,
    sources: ResolvedRunSources,
    output_dir: Path,
    *,
    fps: float = 10.0,
    frame_times_video: Sequence[float] | None = None,
    clock_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resample irregular HDF5 frames to a seek-stable WebM timeline.

    The raw ``data/l_img`` array is stored in BGR channel order and is written
    directly to OpenCV. This is deliberate: converting it as RGB swaps red and
    blue and produces the previously observed false-color video.
    """

    if not math.isfinite(fps) or fps <= 0 or fps > 60:
        raise ValueError("fps must be greater than 0 and no more than 60")
    cv2, h5py, np = _require_video_dependencies()
    reader = RecordingReader(sources.raw_run_dir)
    references = _index_source_frames(sources.raw_run_dir)
    source_times_abs = np.asarray([row.timestamp_s for row in references], dtype=float)
    zero_time = float(source_times_abs[0])
    source_times = (
        source_times_abs - zero_time
        if frame_times_video is None
        else np.asarray(frame_times_video, dtype=float)
    )
    if (clock_manifest or {}).get("source") == SOURCE:
        clock = dict(clock_manifest or {})
        if frame_times_video is None or not clock.get("native_correspondence_verified"):
            raise ValueError(
                "Generated native review-video clock needs verified aligned frame times"
            )
        checked = validate_generated_alignment(
            clock["path"],
            [
                {
                    "source_hdf5": row.source_path.name,
                    "source_frame_idx": row.source_index,
                    "t_abs": row.timestamp_s,
                }
                for row in references
            ],
            sources.raw_run_dir,
        )
        if (
            float(fps) != checked["fps"]
            or clock.get("duration_s") != checked["duration_s"]
            or clock.get("generated_manifest_sha256") != checked["generated_manifest_sha256"]
            or not np.array_equal(source_times, source_times_abs - zero_time)
        ):
            raise ValueError(
                "Generated native review-video clock or fps differs from its source receipt"
            )
    if (
        source_times.shape != source_times_abs.shape
        or not np.isfinite(source_times).all()
        or not np.isfinite(source_times_abs).all()
        or np.any(np.diff(source_times) < 0)
        or np.any(source_times < 0)
    ):
        raise ValueError(
            "Review-video clock must provide one finite nondecreasing time per native frame"
        )
    if frame_times_video is not None and (
        np.any(np.diff(source_times_abs) <= 0) or np.any(np.diff(source_times) <= 0)
    ):
        raise ValueError("Explicit review-video alignment requires strictly increasing frame times")
    last_source_time = float(source_times[-1])
    duration_s = float((clock_manifest or {}).get("duration_s", last_source_time))
    if not math.isfinite(duration_s) or duration_s < last_source_time:
        raise ValueError("Review-video duration must be finite and cover the last mapped frame")
    output_count = int(math.ceil(duration_s * fps)) + 1
    target_times = np.arange(output_count, dtype=float) / float(fps)
    source_indices = _nearest_source_indices(source_times, target_times, np)
    selected_times = source_times[source_indices]
    alignment_error = np.abs(selected_times - target_times)

    first = references[int(source_indices[0])]
    with reader.open_chunk(first.source_path) as handle:
        sample = np.asarray(handle["data/l_img"][first.source_index])
    if sample.ndim != 3 or sample.shape[2] != 3 or sample.dtype != np.uint8:
        raise ValueError(f"Unsupported RGB frame shape/dtype: {sample.shape} {sample.dtype}")
    height, width = int(sample.shape[0]), int(sample.shape[1])

    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = output_dir / "review_video.webm"
    building_path = output_dir / "review_video.building.webm"
    if building_path.exists():
        building_path.unlink()
    writer = cv2.VideoWriter(
        str(building_path), cv2.VideoWriter_fourcc(*"VP80"), float(fps), (width, height)
    )
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not initialize the browser-compatible VP8 encoder")

    current_path: Path | None = None
    handle: Any = None
    try:
        for selected in source_indices:
            reference = references[int(selected)]
            if reference.source_path != current_path:
                if handle is not None:
                    handle.close()
                handle = reader.open_chunk(reference.source_path)
                current_path = reference.source_path
            frame_bgr = np.asarray(handle["data/l_img"][reference.source_index])
            writer.write(frame_bgr)
    finally:
        writer.release()
        if handle is not None:
            handle.close()
    if not building_path.is_file() or building_path.stat().st_size == 0:
        raise RuntimeError("Review video encoder produced no output")
    building_path.replace(video_path)

    validation = validate_review_video(
        video_path, references, source_indices, output_count, fps, recording_reader=reader
    )
    manifest = {
        "schema": "review_video_manifest_v1",
        "run_id": record.run_id,
        "video_file": video_path.name,
        "container": "webm",
        "codec": "VP8",
        "browser_mime_type": "video/webm",
        "fps": float(fps),
        "width": width,
        "height": height,
        "source_frame_count": len(references),
        "output_frame_count": output_count,
        "source_hdf5_count": len({row.source_path for row in references}),
        "duration_s": duration_s,
        "timeline_origin": (
            "canonical_frame_video_clock"
            if frame_times_video is not None
            else "first_HDF5_data/time_timestamp"
        ),
        "clock_alignment": dict(clock_manifest or {}),
        "source_time_bounds_s": [float(source_times[0]), last_source_time],
        "held_first_frame_before_source_s": float(source_times[0]),
        "held_last_frame_after_source_s": duration_s - last_source_time,
        "raw_time_bounds_s": [zero_time, float(source_times_abs[-1])],
        "frame_order": "raw_timestamp_then_source_filename_then_source_frame_index",
        "resampling": "uniform_timeline_nearest_source_frame",
        "mean_alignment_error_s": float(np.mean(alignment_error)),
        "max_alignment_error_s": float(np.max(alignment_error)),
        "input_color_order": "BGR",
        "encoder_input_color_order": "BGR",
        "color_rule": "write data/l_img directly; do not swap red and blue",
        "validation": validation,
    }
    manifest_path = output_dir / "review_video_manifest.json"
    if reader.schema == "v3":
        manifest["source_image_dataset"] = reader.dataset_path("data/l_img")
        manifest["color_rule"] = (
            "write vision_data/continuous_data/l_img directly; do not swap red and blue"
        )
        manifest["hdf5_reader"] = reader.manifest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def validate_review_video(
    video_path: Path,
    references: list[FrameReference],
    source_indices: Any,
    expected_count: int,
    expected_fps: float,
    *,
    recording_reader: RecordingReader | None = None,
) -> dict[str, Any]:
    """Check decode, timeline metadata, and red/blue channel orientation."""

    cv2, h5py, np = _require_video_dependencies()
    reader = recording_reader or RecordingReader(references[0].source_path.parent)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Generated review video cannot be decoded: {video_path}")
    decoded_count = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
    decoded_fps = float(capture.get(cv2.CAP_PROP_FPS))
    sample_positions = sorted({0, expected_count // 2, expected_count - 1})
    correct_errors: list[float] = []
    swapped_errors: list[float] = []
    try:
        for output_index in sample_positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, output_index)
            ok, decoded_bgr = capture.read()
            if not ok:
                raise RuntimeError(f"Could not decode validation frame {output_index}")
            reference = references[int(source_indices[output_index])]
            with reader.open_chunk(reference.source_path) as handle:
                source_bgr = np.asarray(handle["data/l_img"][reference.source_index])
            correct_errors.append(
                float(
                    np.mean(np.abs(decoded_bgr.astype(np.float32) - source_bgr.astype(np.float32)))
                )
            )
            swapped = source_bgr[:, :, ::-1]
            swapped_errors.append(
                float(np.mean(np.abs(decoded_bgr.astype(np.float32) - swapped.astype(np.float32))))
            )
    finally:
        capture.release()
    mean_correct = float(np.mean(correct_errors))
    mean_swapped = float(np.mean(swapped_errors))
    return {
        "decode_ok": True,
        "decoded_frame_count": decoded_count,
        "expected_frame_count": expected_count,
        "frame_count_within_codec_tolerance": abs(decoded_count - expected_count) <= 1,
        "decoded_fps": decoded_fps,
        "expected_fps": float(expected_fps),
        "fps_within_tolerance": abs(decoded_fps - expected_fps) <= 0.01,
        "sample_output_frame_indices": sample_positions,
        "mean_absolute_error_bgr": mean_correct,
        "mean_absolute_error_if_red_blue_swapped": mean_swapped,
        "color_order_check_passed": mean_correct < mean_swapped,
    }
