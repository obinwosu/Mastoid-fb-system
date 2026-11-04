"""Read-only audit of one canonical raw simulator run and its references."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .hdf5_clock import list_hdf5_paths
from .registry import RunRecord
from .source_config import ResolvedRunSources

DERIVED_SUFFIXES = {".csv", ".json", ".jsonl"}
RAW_MEDIA_SUFFIXES = {".mp4", ".npy", ".intrinsics", ".pldata"}
REQUIRED_EDT_STEMS = {
    "Bone",
    "Malleus",
    "Incus",
    "Bony_Labyrinth",
    "Facial_Nerve",
    "Chorda_Tympani",
    "Cochlear_Nerve",
    "IAC",
    "ICA",
    "Sinus_+_Dura",
    "Superior_Vestibular_Nerve",
    "Inferior_Vestibular_Nerve",
    "Stapes",
    "EAC",
    "TMJ",
    "Vestibular_Aqueduct",
}


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def inspect_hdf5(
    path: Path,
    *,
    hash_files: bool,
    reader: Any = None,
    reader_error: str | None = None,
) -> dict[str, Any]:
    """Inventory physical datasets, then audit the recording's canonical view.

    ``readable`` means that the physical HDF5 could be inventoried. It does not
    imply a supported layout or valid frame/camera registration. A recording
    reader is shared across chunks so v3 cameras can use their global timeline.
    """
    try:
        import h5py
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("source-audit requires h5py and numpy.") from exc

    datasets: list[dict[str, Any]] = []
    errors: list[str] = []
    count_fields = (
        "frame_count",
        "image_frame_count",
        "depth_frame_count",
        "drill_pose_count",
        "volume_pose_count",
        "camera_pose_count",
        "removed_timestamp_count",
        "removed_voxel_row_count",
    )
    result: dict[str, Any] = {
        "file_name": path.name,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        "sha256": sha256_file(path) if hash_files else None,
        "readable": False,
        "canonical_valid": False,
        "format": None,
        "chunk_role": "unknown",
        "error": None,
        "canonical_errors": errors,
        "warnings": [],
        "root_groups": [],
        "datasets": datasets,
        "canonical_fields": {},
        **{field: None for field in count_fields},
        "time_start": None,
        "time_end": None,
        "time_finite": None,
        "time_monotonic": None,
        "camera_pose_valid": None,
        "removed_timestamp_index_min": None,
        "removed_timestamp_index_max": None,
        "removed_timestamp_indices_valid": None,
        "voxel_volume": None,
        "validation_scope": (
            "Physical dataset inventory plus canonical shape/time/pose/event checks; "
            "geometry and scientific validity are separate."
        ),
    }
    try:
        with h5py.File(path, "r") as raw:
            result["root_groups"] = sorted(raw.keys())

            def visit(name: str, item: Any) -> None:
                if isinstance(item, h5py.Dataset):
                    entry = {"path": name, "shape": list(item.shape), "dtype": str(item.dtype)}
                    # Preserve declared units without guessing their physical scale.
                    units = {
                        key: str(item.attrs[key]) for key in ("unit", "units") if key in item.attrs
                    }
                    if units:
                        entry["declared_units"] = units
                    datasets.append(entry)

            raw.visititems(visit)
            result["readable"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    try:
        if reader_error is not None:
            raise ValueError(reader_error)
        if reader is None:
            from .hdf5_reader import RecordingReader

            reader = RecordingReader(path.parent)
        result["format"] = reader.schema
        result["chunk_role"] = reader.chunk_role(path)
        camera_alignment = reader.manifest().get("camera_alignment", {})
        expected_camera_missing = {
            int(row["source_frame_idx"])
            for row in camera_alignment.get("missing_frames", [])
            if row["source_hdf5"] == path.name
        }
        result["camera_missing_frame_count"] = len(expected_camera_missing)
        if expected_camera_missing:
            result["warnings"].append(
                "Declared camera-alignment gaps retained as NaN; camera-dependent "
                "evidence is not evaluable for these frames."
            )
        with reader.open_chunk(path) as handle:

            def field(name: str, *, required: bool = False) -> Any:
                try:
                    value = handle.get(name)
                    if value is None:
                        result["canonical_fields"][name] = {"present": False}
                        if required:
                            errors.append(f"Missing canonical field: {name}")
                    else:
                        result["canonical_fields"][name] = {
                            "present": True,
                            "shape": list(value.shape),
                            "dtype": str(value.dtype),
                        }
                    return value
                except Exception as exc:
                    result["canonical_fields"][name] = {
                        "present": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    errors.append(f"{name}: {type(exc).__name__}: {exc}")
                    return None

            is_frame = result["chunk_role"] == "frame_data"
            if not is_frame:
                for key in count_fields[:6]:
                    result[key] = 0
            time_dataset = field("data/time", required=is_frame)
            if time_dataset is not None:
                times = np.asarray(time_dataset[()], dtype=float).reshape(-1)
                result["frame_count"] = int(len(times))
                result["time_finite"] = bool(np.all(np.isfinite(times)))
                result["time_monotonic"] = bool(
                    result["time_finite"] and np.all(np.diff(times) > 0)
                )
                if len(times) and result["time_finite"]:
                    result["time_start"], result["time_end"] = float(times[0]), float(times[-1])
                if not result["time_monotonic"] or (is_frame and not len(times)):
                    errors.append(
                        "Frame timestamps must be nonempty, finite and strictly increasing."
                    )

            n = result["frame_count"]
            for name, key, kind in (
                ("data/l_img", "image_frame_count", "image"),
                ("data/depth", "depth_frame_count", "depth"),
                ("data/pose_mastoidectomy_drill", "drill_pose_count", "pose"),
                ("data/pose_mastoidectomy_volume", "volume_pose_count", "pose"),
                ("data/pose_main_camera", "camera_pose_count", "pose"),
            ):
                value = field(name, required=is_frame) if is_frame else None
                if value is None:
                    continue
                shape = tuple(value.shape)
                result[key] = int(shape[0]) if shape else None
                if not shape or shape[0] != n:
                    errors.append(f"Frame count mismatch: {name}")
                if kind == "pose":
                    array = np.asarray(value[()], dtype=float)
                    valid_pose = shape == (n, 7)
                    if valid_pose:
                        invalid_rows = set(
                            np.flatnonzero(~np.all(np.isfinite(array), axis=1)).tolist()
                        )
                        if name == "data/pose_main_camera" and reader.schema == "v3":
                            valid_pose = invalid_rows == expected_camera_missing
                            if invalid_rows:
                                valid_pose = valid_pose and bool(
                                    np.all(np.isnan(array[sorted(invalid_rows)]))
                                )
                        else:
                            valid_pose = not invalid_rows
                        finite_rows = [
                            index for index in range(len(array)) if index not in invalid_rows
                        ]
                        if finite_rows:
                            valid_pose = valid_pose and bool(
                                np.all(np.linalg.norm(array[finite_rows, 3:], axis=1) > 0)
                            )
                    if name == "data/pose_main_camera":
                        result["camera_pose_valid"] = valid_pose
                    if not valid_pose:
                        errors.append(
                            f"Expected finite N x 7 pose except declared v3 camera gaps: {name}"
                        )
                elif kind == "image" and (len(shape) != 4 or shape[-1] != 3):
                    errors.append(f"Expected N x H x W x 3 image shape: {name}")
                elif kind == "depth" and len(shape) != 3:
                    errors.append(f"Expected N x H x W depth shape: {name}")

            removal_times = field("voxels_removed/voxel_time_stamp")
            removed = field("voxels_removed/voxel_removed")
            if removal_times is None and removed is None:
                # Only a supported reader may establish an absent optional event
                # stream. Unknown layouts return above, with counts left null.
                result["removed_timestamp_count"] = 0
                result["removed_voxel_row_count"] = 0
                result["removed_timestamp_indices_valid"] = True
            elif removal_times is None or removed is None:
                errors.append("Removed-voxel timestamps and rows must be paired.")
                result["removed_timestamp_indices_valid"] = False
            else:
                event_times = np.asarray(removal_times[()], dtype=float).reshape(-1)
                rows = np.asarray(removed[()], dtype=float)
                result["removed_timestamp_count"] = len(event_times)
                result["removed_voxel_row_count"] = int(rows.shape[0]) if rows.ndim else None
                valid = bool(np.all(np.isfinite(event_times)) and np.all(np.diff(event_times) >= 0))
                valid = (
                    valid
                    and rows.ndim == 2
                    and rows.shape[1] >= 4
                    and bool(np.all(np.isfinite(rows)))
                )
                if valid and len(rows):
                    indices = rows[:, 0]
                    valid = bool(
                        np.all(indices == np.floor(indices))
                        and np.all(indices >= 0)
                        and np.all(indices < len(event_times))
                    )
                    result["removed_timestamp_index_min"] = float(indices.min())
                    result["removed_timestamp_index_max"] = float(indices.max())
                result["removed_timestamp_indices_valid"] = valid
                if not valid:
                    errors.append(
                        "Invalid chunk-local removed-voxel timestamp indices or event values."
                    )

            volume = field("metadata/voxel_volume")
            if volume is not None:
                value = np.asarray(volume[()])
                result["voxel_volume"] = _finite_float(value.item()) if value.size == 1 else None
                if result["voxel_volume"] is None or result["voxel_volume"] <= 0:
                    errors.append("voxel_volume must be a finite positive scalar.")
        result["canonical_valid"] = not errors
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    if errors:
        result["error"] = "; ".join(errors)
    return result


def read_edt_header(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:

        def next_token() -> str:
            char = handle.read(1)
            while char and char in b" \t\r\n":
                char = handle.read(1)
            buffer = bytearray()
            while char and char not in b" \t\r\n":
                buffer.extend(char)
                char = handle.read(1)
            return buffer.decode("ascii", errors="replace")

        magic = next_token()
        dimension = int(magic[1:]) if magic.startswith("G") else None
        type_dimension = next_token()
        type_name = next_token()
        resolution = [int(next_token()), int(next_token()), int(next_token())]
    return {
        "file_name": path.name,
        "size_bytes": path.stat().st_size,
        "magic": magic,
        "dimension": dimension,
        "type_dimension": type_dimension,
        "type_name": type_name,
        "resolution": resolution,
    }


def run_id_from_video(video: str) -> str | None:
    match = re.search(r"_P(\d+)T(\d+)\.mp4$", str(video), flags=re.IGNORECASE)
    if not match:
        return None
    return f"P{int(match.group(1)):02d}_T{int(match.group(2)):02d}"


def inspect_phase_annotation(path: Path, record: RunRecord) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    matches = [
        row
        for row in rows
        if (
            str(row.get("run_id", "")).strip() == record.run_id
            or (
                record.phase_annotation_id
                and str(row.get("phase_annotation_id", "")).strip() == record.phase_annotation_id
            )
            or (record.exp_id and str(row.get("exp_id", "")).strip() == record.exp_id)
        )
    ]
    result: dict[str, Any] = {
        "path": str(path),
        "sha256": sha256_file(path),
        "exp_id": record.exp_id,
        "match_count": len(matches),
        "valid": False,
        "row": None,
        "ordered": False,
        "run_id_matches": False,
    }
    if len(matches) != 1:
        return result
    row = matches[0]
    antrum = _finite_float(row.get("antrum_start_sec") or row.get("antrum_start_s"))
    incus = _finite_float(row.get("antrum_end_incus_start_sec") or row.get("incus_start_s"))
    incus_end = _finite_float(row.get("incus_end_sec") or row.get("incus_end_s"))
    ordered = (
        antrum is not None
        and incus is not None
        and incus_end is not None
        and 0 <= antrum < incus < incus_end
    )
    explicit_run = str(row.get("run_id", "")).strip()
    parsed_run = explicit_run or run_id_from_video(str(row.get("video", "")))
    run_id_matches = parsed_run == record.run_id
    if not parsed_run:
        run_id_matches = bool(
            (
                record.phase_annotation_id
                and str(row.get("phase_annotation_id", "")).strip() == record.phase_annotation_id
            )
            or (record.exp_id and str(row.get("exp_id", "")).strip() == record.exp_id)
        )
    result.update(
        {
            "valid": bool(ordered and run_id_matches),
            "ordered": bool(ordered),
            "run_id_matches": run_id_matches,
            "row": {
                "video": row.get("video"),
                "participant": row.get("participant"),
                "training_year": row.get("training_year"),
                "antrum_start_sec": antrum,
                "incus_start_sec": incus,
                "incus_end_sec": incus_end,
                "annotation_status": row.get("annotation_status"),
                "notes": row.get("notes"),
            },
        }
    )
    return result


def classify_run_files(raw_run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    derived = []
    raw_media = []
    for path in sorted(raw_run_dir.glob("*"), key=lambda item: item.name.lower()):
        if path.is_file() and path.suffix.lower() in DERIVED_SUFFIXES:
            derived.append({"name": path.name, "size_bytes": path.stat().st_size})
    media_dir = raw_run_dir / "000"
    if media_dir.is_dir():
        for path in sorted(media_dir.iterdir(), key=lambda item: item.name.lower()):
            if path.is_file() and path.suffix.lower() in RAW_MEDIA_SUFFIXES:
                raw_media.append({"name": path.name, "size_bytes": path.stat().st_size})
    return {"excluded_derived_files": derived, "raw_media_files": raw_media}


def build_source_audit(
    record: RunRecord,
    sources: ResolvedRunSources,
    *,
    hash_hdf5: bool = True,
) -> dict[str, Any]:
    if not sources.raw_run_dir.is_dir():
        raise FileNotFoundError(sources.raw_run_dir)
    if not sources.edt_dir.is_dir():
        raise FileNotFoundError(sources.edt_dir)
    automatic_phase = sources.phase_model_bundle is not None
    if automatic_phase:
        if not sources.phase_model_bundle.is_dir():
            raise FileNotFoundError(sources.phase_model_bundle)
        for filename in ("model.pt", "neural_preprocessing.json", "manifest.json"):
            if not (sources.phase_model_bundle / filename).is_file():
                raise FileNotFoundError(sources.phase_model_bundle / filename)
    elif sources.phase_annotation_file is None or not sources.phase_annotation_file.is_file():
        raise FileNotFoundError(
            sources.phase_annotation_file or "Manual phase annotation not configured"
        )
    if sources.frame_alignment_file is not None and not sources.frame_alignment_file.is_file():
        raise FileNotFoundError(sources.frame_alignment_file)

    from .hdf5_reader import RecordingReader

    hdf5_paths = list_hdf5_paths(sources.raw_run_dir)
    reader, reader_error, reader_manifest = None, None, None
    try:
        reader = RecordingReader(sources.raw_run_dir)
        reader_manifest = reader.manifest()
    except Exception as exc:
        reader_error = f"{type(exc).__name__}: {exc}"
    hdf5 = [
        inspect_hdf5(path, hash_files=hash_hdf5, reader=reader, reader_error=reader_error)
        for path in hdf5_paths
    ]
    readable = [item for item in hdf5 if item["readable"]]
    time_chunks = sorted(
        (
            item
            for item in readable
            if item["time_start"] is not None and item["time_end"] is not None
        ),
        key=lambda item: item["time_start"],
    )
    continuity = []
    for previous, current in zip(time_chunks, time_chunks[1:], strict=False):
        gap = float(current["time_start"]) - float(previous["time_end"])
        continuity.append(
            {
                "previous": previous["file_name"],
                "current": current["file_name"],
                "gap_s": gap,
                "classification": "overlap_or_duplicate" if gap <= 0 else "forward",
            }
        )

    edt_files = sorted(sources.edt_dir.glob("*.edt"), key=lambda path: path.name)
    edt_headers = [read_edt_header(path) for path in edt_files]
    edt_stems = {path.stem for path in edt_files}
    missing_required_edt = sorted(REQUIRED_EDT_STEMS - edt_stems)
    phase = (
        {
            "path": str(sources.phase_model_bundle),
            "sha256": None,
            "valid": True,
            "row": None,
            "source_kind": "model_phase_prediction",
            "status": "pending_inference",
            "human_annotation_read": False,
            "bundle_files": {
                name: sha256_file(sources.phase_model_bundle / name)
                for name in ("model.pt", "neural_preprocessing.json", "manifest.json")
            },
            "validation_scope": (
                "Bundle directory exists; frozen assets and predictions are validated by "
                "phase inference after geometry enrichment."
            ),
        }
        if automatic_phase
        else inspect_phase_annotation(sources.phase_annotation_file, record)
    )
    classified = classify_run_files(sources.raw_run_dir)
    anatomy_mapping_confirmed = record.anatomy_mapping_status.casefold().startswith(
        ("confirmed", "synthetic", "configured")
    )

    checks = {
        "one_canonical_raw_directory": True,
        "hdf5_files_found": len(hdf5) > 0,
        "all_hdf5_readable": len(readable) == len(hdf5) and len(hdf5) > 0,
        "supported_recording_schema": reader_manifest is not None,
        "all_canonical_chunks_valid": bool(hdf5) and all(item["canonical_valid"] for item in hdf5),
        "frame_chunk_times_do_not_overlap": all(item["gap_s"] > 0 for item in continuity),
        "frame_data_chunks_found": any(item["chunk_role"] == "frame_data" for item in readable),
        "all_hdf5_roles_supported": all(
            item["chunk_role"] in {"frame_data", "metadata_only", "physics_only"}
            for item in readable
        ),
        "all_hdf5_time_monotonic": all(item["time_monotonic"] in (True, None) for item in readable),
        "all_frame_modalities_aligned": all(
            item["frame_count"]
            == item["image_frame_count"]
            == item["depth_frame_count"]
            == item["drill_pose_count"]
            == item["volume_pose_count"]
            == item["camera_pose_count"]
            for item in readable
            if item["chunk_role"] == "frame_data"
        ),
        "all_removed_voxel_timestamp_indices_valid": all(
            item["removed_timestamp_indices_valid"] for item in readable
        ),
        "edt_profile_found": len(edt_files) > 0,
        "required_edt_files_present": not missing_required_edt,
        (
            "phase_model_directory_available"
            if automatic_phase
            else "phase_annotation_unique_and_valid"
        ): bool(phase["valid"]),
        "anatomy_mapping_confirmed": anatomy_mapping_confirmed,
        "old_derived_files_excluded": True,
    }
    ready = all(checks.values())

    def known_total(field: str) -> int | None:
        values = [item[field] for item in hdf5]
        return (
            sum(int(value) for value in values)
            if all(value is not None for value in values)
            else None
        )

    camera_alignment = (reader_manifest or {}).get("camera_alignment", {})
    return {
        "schema": "canonical_source_audit_v1",
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "dataset_id": record.dataset_id,
        "run_id": record.run_id,
        "global_run_id": record.global_run_id,
        "source_classification": {
            "primary_raw": str(sources.raw_run_dir),
            "auxiliary_edt": str(sources.edt_dir),
            "auxiliary_phase_annotation": (
                None if automatic_phase else str(sources.phase_annotation_file)
            ),
            "phase_model_bundle": str(sources.phase_model_bundle) if automatic_phase else None,
            "frame_alignment_file": str(sources.frame_alignment_file)
            if sources.frame_alignment_file
            else None,
            "validation_only_golden": str(sources.golden_reference_path),
        },
        "registry": {
            "exp_id": record.exp_id,
            "anatomy_key": record.anatomy_key,
            "raw_anatomy_token": record.raw_anatomy_token,
            "anatomy_mapping_status": record.anatomy_mapping_status,
            "anatomy_mapping_basis": record.anatomy_mapping_basis,
            "edt_profile": record.edt_profile,
            "phase_annotation_id": record.phase_annotation_id,
        },
        "hdf5": {
            "file_count": len(hdf5),
            "readable_count": len(readable),
            "format": (reader_manifest or {}).get("source_schema"),
            "reader_manifest": reader_manifest,
            "reader_error": reader_error,
            "camera_alignment": camera_alignment,
            "frame_count_total": known_total("frame_count"),
            "removed_timestamp_count_total": known_total("removed_timestamp_count"),
            "removed_voxel_row_count_total": known_total("removed_voxel_row_count"),
            "chunks": hdf5,
            "continuity": continuity,
        },
        "edt": {
            "profile": record.edt_profile,
            "file_count": len(edt_files),
            "missing_required_stems": missing_required_edt,
            "headers": edt_headers,
        },
        "phase_annotation": phase,
        "raw_media_files": classified["raw_media_files"],
        "excluded_derived_files": classified["excluded_derived_files"],
        "checks": checks,
        "ready_for_frame_extraction": ready,
        "validation_scope": (
            "Canonical structure, numeric frame/event checks and source availability "
            "only. This does not establish geometry calibration, physical units, "
            "image/depth pixel validity, model validity or clinical accuracy."
        ),
        "camera_dependent_evidence_complete": bool(
            reader_manifest is not None
            and camera_alignment.get("status") != "incomplete"
            and any(item["chunk_role"] == "frame_data" for item in hdf5)
            and all(
                item["camera_pose_valid"] is True
                for item in hdf5
                if item["chunk_role"] == "frame_data"
            )
        ),
        "warnings": (
            [
                (
                    "Some vision frames have explicitly missing camera poses; visibility and "
                    "Ray evidence must mark those frames not evaluable."
                )
            ]
            if camera_alignment.get("missing_frame_count", 0)
            else []
        ),
        "blocking_items": [name for name, passed in checks.items() if not passed],
    }


def _write_inventory(path: Path, audit: dict[str, Any]) -> None:
    fields = [
        "file_name",
        "size_bytes",
        "sha256",
        "readable",
        "canonical_valid",
        "format",
        "chunk_role",
        "error",
        "frame_count",
        "image_frame_count",
        "depth_frame_count",
        "drill_pose_count",
        "volume_pose_count",
        "camera_pose_count",
        "camera_missing_frame_count",
        "time_finite",
        "time_start",
        "time_end",
        "time_monotonic",
        "removed_timestamp_count",
        "removed_voxel_row_count",
        "removed_timestamp_index_min",
        "removed_timestamp_index_max",
        "removed_timestamp_indices_valid",
        "voxel_volume",
        "root_groups",
        "dataset_count",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in audit["hdf5"]["chunks"]:
            row = {key: item.get(key) for key in fields}
            row["root_groups"] = "|".join(item.get("root_groups", []))
            row["dataset_count"] = len(item.get("datasets", []))
            writer.writerow(row)


def render_markdown(audit: dict[str, Any]) -> str:
    phase_row = audit["phase_annotation"].get("row") or {}
    lines = [
        f"# Canonical Source Audit: {audit['run_id']}",
        "",
        f"- Overall ready for frame extraction: **{audit['ready_for_frame_extraction']}**",
        f"- Primary raw directory: `{audit['source_classification']['primary_raw']}`",
        f"- HDF5 chunks: {audit['hdf5']['readable_count']}/{audit['hdf5']['file_count']} readable",
        f"- Recorder format: `{audit['hdf5'].get('format')}`",
        "- Camera alignment: "
        f"`{audit['hdf5'].get('camera_alignment', {}).get('status', 'unavailable')}`",
        "- Camera-dependent evidence complete: "
        f"**{audit.get('camera_dependent_evidence_complete', False)}**",
        f"- Total image-time frames: {audit['hdf5']['frame_count_total']}",
        f"- Removed-voxel rows: {audit['hdf5']['removed_voxel_row_count_total']}",
        f"- EDT profile: `{audit['edt']['profile']}` ({audit['edt']['file_count']} files)",
        "",
        "## Phase source",
        "",
        f"- Annotation ID: `{audit['registry']['phase_annotation_id']}`",
        "- Source kind: "
        f"`{audit['phase_annotation'].get('source_kind', 'human_phase_annotation')}`",
        f"- Status: `{audit['phase_annotation'].get('status', 'validated_manual_annotation')}`",
        f"- Video: `{phase_row.get('video')}`",
        f"- Antrum start: {phase_row.get('antrum_start_sec')} s",
        f"- Incus start: {phase_row.get('incus_start_sec')} s",
        f"- Incus end: {phase_row.get('incus_end_sec')} s",
        "",
        "## Source boundary",
        "",
        "- HDF5 and raw media are primary raw evidence.",
        (
            "- Readable physical HDF5, supported canonical structure and complete "
            "camera coverage are separate checks."
        ),
        (
            "- Source units are preserved; this audit does not establish geometry "
            "calibration or scientific/clinical validity."
        ),
        (
            "- EDT is an auxiliary anatomy reference; phase provenance distinguishes "
            "manual annotations from model predictions."
        ),
        "- Existing CSV/JSON files in the run directory are excluded derived artifacts.",
        "- The legacy graph is validation-only and is not a pipeline input.",
        "",
        "## Checks",
        "",
    ]
    for warning in audit.get("warnings", []):
        lines.append(f"- Warning: {warning}")
    for name, passed in audit["checks"].items():
        lines.append(f"- {'PASS' if passed else 'BLOCK'}: `{name}`")
    if audit["blocking_items"]:
        lines.extend(["", "## Blocking items", ""])
        lines.extend(f"- `{item}`" for item in audit["blocking_items"])
    lines.extend(
        [
            "",
            "## Anatomy mapping requiring confirmation",
            "",
            f"- Registered anatomy key: `{audit['registry']['anatomy_key']}`",
            f"- Token in raw directory/video name: `{audit['registry']['raw_anatomy_token']}`",
            f"- Configured EDT profile: `{audit['registry']['edt_profile']}`",
            f"- Mapping status: `{audit['registry']['anatomy_mapping_status']}`",
            f"- Mapping basis: `{audit['registry']['anatomy_mapping_basis']}`",
            "",
            "The raw token and the internal anatomy key use different naming systems.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_source_audit(output_dir: Path, audit: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "source_manifest.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _write_inventory(output_dir / "hdf5_inventory.csv", audit)
    (output_dir / "SOURCE_AUDIT.md").write_text(render_markdown(audit), encoding="utf-8")
