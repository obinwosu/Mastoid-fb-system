"""Generate and verify a source-bound native clock without historical media.

One input directory denotes one recording. Overlapping chunk time ranges are
rejected rather than guessed to be separate sessions. Disjoint recordings in a
single directory cannot be distinguished using timestamps alone; directory
selection remains an explicit caller responsibility.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .hdf5_reader import RecordingReader, list_hdf5_paths

SCHEMA = "generated_hdf5_clock_v1"
SOURCE = "generated_hdf5_native_alignment"
MANIFEST_NAME = "hdf5_clock_manifest.json"
ALIGNMENT_FIELDS = (
    "frame_idx_global",
    "source_hdf5",
    "source_frame_idx",
    "t_abs",
    "video_time_s",
)
FRAME_DATASETS = (
    "data/l_img",
    "data/depth",
    "data/pose_mastoidectomy_drill",
    "data/pose_mastoidectomy_volume",
    "data/pose_main_camera",
    "voxels_removed/voxel_removed",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 << 20):
            digest.update(block)
    return digest.hexdigest()


def _inventory_digest(inventory: Sequence[Mapping[str, Any]]) -> str:
    content = json.dumps(list(inventory), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_hdf5_frame_inventory(
    raw_run_dir: Path | str,
    *,
    hash_sources: bool = False,
    validate_images: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Index native frames; optionally hash all chunks and decode every RGB row.

    Empty metadata-only chunks are recorded, never mistaken for corrupt chunks.
    A frame-bearing chunk needs matching time and RGB datasets. Within-chunk
    resets, duplicates, nonfinite times and interleaved chunk ranges fail closed.
    """
    import h5py
    import numpy as np

    reader = RecordingReader(raw_run_dir)
    paths = reader.paths
    rows: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    image_shape = None
    ranges = []
    for path in paths:
        before = path.stat()
        entry: dict[str, Any] = {"file_name": path.name, "size_bytes": before.st_size}
        with reader.open_chunk(path) as handle:
            time_data = handle.get("data/time")
            images = handle.get("data/l_img")
            role = reader.chunk_role(path)
            if role != "frame_data":
                entry.update(frame_count=0, metadata_only=role == "metadata_only")
                if reader.schema == "v3":
                    entry["chunk_role"] = role
            else:
                if time_data is None or images is None:
                    raise ValueError(f"Incomplete frame-bearing HDF5 chunk: {path.name}")
                if not isinstance(time_data, h5py.Dataset) or not isinstance(images, h5py.Dataset):
                    raise ValueError(f"Invalid native frame datasets in {path.name}")
                times = np.asarray(time_data, dtype=np.float64).reshape(-1)
                if (
                    len(images.shape) != 4
                    or images.shape[-1] != 3
                    or images.dtype != np.uint8
                    or any(size <= 0 for size in images.shape[1:])
                ):
                    raise ValueError(f"Unsupported data/l_img shape/dtype in {path.name}")
                if not len(times) or len(times) != images.shape[0]:
                    raise ValueError(f"Timestamp/frame mismatch or empty frame chunk: {path.name}")
                if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
                    raise ValueError(
                        f"Native timestamps must be finite and strictly increasing: {path.name}"
                    )
                if image_shape is not None and tuple(images.shape[1:]) != image_shape:
                    raise ValueError(f"RGB frame dimensions changed between chunks: {path.name}")
                image_shape = tuple(images.shape[1:])
                if validate_images:
                    # Read every source RGB block so unreadable compressed chunks
                    # are detected before declaring an alignment ready for video.
                    for start in range(0, len(times), 16):
                        np.asarray(images[start : start + 16])
                ranges.append((float(times[0]), float(times[-1]), path.name))
                entry.update(
                    frame_count=len(times),
                    metadata_only=False,
                    raw_time_bounds_abs=[float(times[0]), float(times[-1])],
                    image_shape=list(image_shape),
                    image_dtype=str(images.dtype),
                )
                rows.extend(
                    {"source_hdf5": path.name, "source_frame_idx": i, "t_abs": float(t)}
                    for i, t in enumerate(times)
                )
        if hash_sources:
            entry["sha256"] = sha256_file(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"HDF5 source changed while indexing: {path.name}")
        inventory.append(entry)
    ranges.sort()
    for left, right in zip(ranges, ranges[1:], strict=False):
        if right[0] <= left[1]:
            raise ValueError(f"Overlapping or duplicate chunk timestamps: {left[2]}, {right[2]}")
    rows.sort(key=lambda row: (row["t_abs"], row["source_hdf5"], row["source_frame_idx"]))
    if not rows:
        raise ValueError("HDF5 recording contains no RGB frames")
    return rows, inventory


def generate_hdf5_alignment(
    raw_run_dir: Path | str,
    output_dir: Path | str,
    fps: float = 10.0,
) -> dict[str, Any]:
    """Write a new native-time alignment and provenance receipt under results.

    This does not write video or change sources. Feed the returned alignment and
    duration into core extraction, then pass its clock to build_review_video at
    the returned fps. No original video, frame CSV or world timestamps are read.
    Existing outputs are never overwritten.
    """
    fps = float(fps)
    if not math.isfinite(fps) or fps <= 0 or fps > 60:
        raise ValueError("fps must be greater than 0 and no more than 60")
    raw = Path(raw_run_dir).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if output == raw or raw in output.parents:
        raise ValueError("Generated alignment must be outside the source recording directory")
    alignment = output / "alignment.csv"
    manifest_path = output / MANIFEST_NAME
    if alignment.exists() or manifest_path.exists():
        raise FileExistsError(
            "Generated clock outputs already exist; choose a new output directory"
        )
    rows, inventory = read_hdf5_frame_inventory(raw, hash_sources=True, validate_images=True)
    reader = RecordingReader(raw)
    origin = rows[0]["t_abs"]
    duration = rows[-1]["t_abs"] - origin
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=ALIGNMENT_FIELDS)
    writer.writeheader()
    for index, row in enumerate(rows):
        writer.writerow(dict(row, frame_idx_global=index, video_time_s=row["t_abs"] - origin))
    content = buffer.getvalue().encode("utf-8")
    manifest = {
        "schema": SCHEMA,
        "source": SOURCE,
        "raw_run_dir": str(raw),
        "alignment_file": alignment.name,
        "alignment_sha256": hashlib.sha256(content).hexdigest(),
        "alignment_columns": list(ALIGNMENT_FIELDS),
        "frame_count": len(rows),
        "source_inventory": inventory,
        "source_inventory_sha256": _inventory_digest(inventory),
        "raw_time_origin_abs": origin,
        "raw_time_bounds_abs": [origin, rows[-1]["t_abs"]],
        "duration_s": duration,
        "duration_source": "last_native_frame_elapsed_time",
        "fps": fps,
        "time_dataset": "data/time",
        "image_dataset": "data/l_img",
        "clock_rule": "video_time_s = float64(data/time) - first sorted native timestamp",
        "frame_order": "raw_timestamp_then_source_filename_then_source_frame_index",
        "video_resampling": "uniform timeline at fps, nearest source frame, ties to earlier frame",
        "expected_output_frame_count": int(math.ceil(duration * fps)) + 1,
        "tail_rule": "ceil(duration_s * fps) + 1 video frames includes the final source frame",
        "training_reference_clock": False,
        "world_frame_index_present": False,
        "historical_video_or_csv_used": False,
        "reference_phase_used": False,
        "all_source_rgb_frames_readable": True,
        "source_identity_limit": (
            "One directory is explicitly selected as one recording; disjoint "
            "sessions in one directory cannot be inferred from timestamps alone"
        ),
        "model_clock_limit": (
            "Native elapsed time is a new inference clock, not the historical "
            "world-video training reference clock; old scores and framewise parity "
            "do not transfer automatically"
        ),
    }
    if reader.schema == "v3":
        manifest["time_dataset"] = "vision_data/continuous_data/time"
        manifest["image_dataset"] = "vision_data/continuous_data/l_img"
        manifest["clock_rule"] = (
            "video_time_s = float64(vision_data/continuous_data/time) - first sorted "
            "native vision timestamp"
        )
        manifest["hdf5_reader"] = reader.manifest()
    output.mkdir(parents=True, exist_ok=True)
    with alignment.open("xb") as stream:
        stream.write(content)
    with manifest_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    return {
        "alignment_path": str(alignment),
        "duration_s": duration,
        "manifest_path": str(manifest_path),
        "fps": fps,
        "frame_count": len(rows),
    }


def validate_generated_alignment(
    alignment_file: Path | str,
    frame_parts: Sequence[Mapping[str, Any]],
    raw_run_dir: Path | str,
) -> dict[str, Any]:
    """Verify generated-clock identity against the currently extracted sources."""
    path = Path(alignment_file).expanduser().resolve()
    manifest_path = path.parent / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = Path(raw_run_dir).expanduser().resolve()
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("source") != SOURCE
        or Path(manifest.get("raw_run_dir", "")).resolve() != raw
        or manifest.get("alignment_file") != path.name
        or manifest.get("alignment_columns") != list(ALIGNMENT_FIELDS)
        or manifest.get("training_reference_clock") is not False
    ):
        raise ValueError("Generated HDF5 clock manifest is not bound to this source directory")
    if sha256_file(path) != manifest.get("alignment_sha256"):
        raise ValueError("Generated HDF5 alignment checksum mismatch")
    inventory = manifest.get("source_inventory", [])
    if not inventory or _inventory_digest(inventory) != manifest.get("source_inventory_sha256"):
        raise ValueError("Generated HDF5 source inventory checksum mismatch")
    paths = list_hdf5_paths(raw)
    if [p.name for p in paths] != [r.get("file_name") for r in inventory]:
        raise ValueError("Generated HDF5 clock source file inventory changed")
    for source, item in zip(paths, inventory, strict=True):
        if source.stat().st_size != item["size_bytes"] or sha256_file(source) != item.get("sha256"):
            raise ValueError(f"Generated HDF5 clock source checksum mismatch: {source.name}")
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(ALIGNMENT_FIELDS):
            raise ValueError("Generated HDF5 alignment has unexpected fields")
        rows = list(reader)
    if not rows or len(rows) != len(frame_parts) or manifest.get("frame_count") != len(rows):
        raise ValueError("Generated HDF5 alignment frame count mismatch")
    origin = float(frame_parts[0]["t_abs"])
    for index, (row, native) in enumerate(zip(rows, frame_parts, strict=True)):
        t = float(native["t_abs"])
        if (
            not math.isfinite(t)
            or int(row["frame_idx_global"]) != index
            or row["source_hdf5"] != native["source_hdf5"]
            or int(row["source_frame_idx"]) != int(native["source_frame_idx"])
            or float(row["t_abs"]) != t
            or float(row["video_time_s"]) != t - origin
        ):
            raise ValueError(
                f"Generated HDF5 clock/native frame correspondence mismatch at row {index}"
            )
        if index and t <= float(frame_parts[index - 1]["t_abs"]):
            raise ValueError("Generated HDF5 clock requires strictly increasing native frames")
    last = float(frame_parts[-1]["t_abs"])
    fps = float(manifest["fps"])
    if (
        manifest.get("raw_time_origin_abs") != origin
        or manifest.get("raw_time_bounds_abs") != [origin, last]
        or manifest.get("duration_s") != last - origin
        or not math.isfinite(fps)
        or fps <= 0
        or fps > 60
        or manifest.get("expected_output_frame_count") != math.ceil((last - origin) * fps) + 1
    ):
        raise ValueError("Generated HDF5 clock time bounds or video parameters mismatch")
    return {
        "source": SOURCE,
        "native_correspondence_verified": True,
        "generated_manifest_path": str(manifest_path),
        "generated_manifest_sha256": sha256_file(manifest_path),
        "source_inventory_sha256": manifest["source_inventory_sha256"],
        "source_raw_run_dir": str(raw),
        "training_reference_clock": False,
        "duration_s": manifest["duration_s"],
        "fps": fps,
        "duration_source": manifest["duration_source"],
        "model_clock_limit": manifest["model_clock_limit"],
        "correspondence_validation": (
            "All native source file hashes, exact CSV hash and every "
            "filename/local-index/float64-timestamp tuple verified against the "
            "current extraction"
        ),
    }
