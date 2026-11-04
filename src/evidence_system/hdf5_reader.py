"""Read the supported legacy and v3 recorder schemas through canonical paths.

Only names and asynchronous camera sampling are adapted here. Numeric units,
RGB channel order, voxel coordinates and burr values are never rescaled or
guessed. Images and depth remain lazy HDF5 datasets. Source files are read-only.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import h5py


CAMERA_TIME = "physics_data/continuous_data/high_frequency_poses/main_camera_pose/time_stamp"
CAMERA_POSE = "physics_data/continuous_data/high_frequency_poses/main_camera_pose/pose"
VISION_PREFIX = "vision_data/continuous_data/"
LEGACY_ROOTS = {"data", "metadata", "voxels_removed", "burr_change", "drill_force_feedback"}
V3_ROOTS = {"vision_data", "physics_data", "metadata_v3"}
FRAME_NAMES = {"time", "l_img", "depth", "pose_mastoidectomy_drill",
               "pose_mastoidectomy_volume", "pose_main_camera", "r_img", "segm"}
V3_PHYSICS_FIELDS = {
    "physics_data/continuous_data/high_frequency_poses/" + stream + "/" + field
    for stream in ("main_camera_pose", "mastoidectomy_drill_pose")
    for field in ("time_stamp", "pose")
} | {
    "physics_data/intermittent_data/" + stream + "/" + field
    for stream, fields in {
        "voxels_removed": ("voxel_time_stamp", "voxel_removed", "voxel_color", "README"),
        "burr_change": ("time_stamp", "burr_size"),
        "drill_force_feedback": ("time_stamp", "wrench"),
    }.items() for field in fields
}


class HDF5SchemaError(ValueError):
    """An unknown, incomplete, or mixed recorder layout cannot be read safely."""


def list_hdf5_paths(raw_run_dir: Path | str) -> list[Path]:
    root = Path(raw_run_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"HDF5 recording directory not found: {root}")
    return sorted(
        (p for p in root.iterdir() if p.is_file() and p.suffix.lower() in {".hdf5", ".h5"}),
        key=lambda p: p.name,
    )


def _dataset_names(handle: h5py.File) -> list[str]:
    import h5py

    names = []
    handle.visititems(lambda name, item: names.append(name) if isinstance(item, h5py.Dataset) else None)
    return names


def _canonical_path(name: str, schema: str) -> str:
    if schema == "legacy":
        return name
    if name.startswith("data/"):
        return VISION_PREFIX + name[5:]
    if name.startswith("metadata/"):
        return "metadata_v3/" + name[9:]
    if name.startswith(("voxels_removed/", "burr_change/", "drill_force_feedback/")):
        return "physics_data/intermittent_data/" + name
    return name


class ChunkView:
    """An open raw file plus canonical aliases; close releases its HDF5 handle."""

    def __init__(self, reader: RecordingReader, path: Path):
        import h5py

        self.reader = reader
        self.path = path
        self.raw = h5py.File(path, "r", rdcc_nbytes=64 << 20, rdcc_nslots=10007)

    def get(self, name: str, default: Any = None) -> Any:
        if name == "data/pose_main_camera" and self.reader.schema == "v3":
            result = self.reader._camera_for_chunk(self.path)
        else:
            result = self.raw.get(_canonical_path(name, self.reader.schema))
        return default if result is None else result

    def __getitem__(self, name: str) -> Any:
        value = self.get(name)
        if value is None:
            raise KeyError(f"Missing canonical HDF5 dataset {name} in {self.path.name}")
        return value

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def keys(self):
        return self.raw.keys()

    def visititems(self, visitor):
        return self.raw.visititems(visitor)

    def close(self) -> None:
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class RecordingReader:
    """Bind all direct sibling chunks to one supported recorder schema.

    The v3 camera clock is assembled over the whole recording, independently of
    vision chunk boundaries. Position interpolation is linear and rotation uses
    shortest-path quaternion SLERP. The gap limit is explicit and applies only
    to bracketing samples actually needed by a vision frame. Extrapolation and
    interpolation across long gaps produce an explicitly missing (NaN) camera
    pose, never a fabricated sample. All vision frames remain in the recording;
    camera-dependent consumers must mark these frames not evaluable.
    """

    def __init__(self, raw_run_dir: Path | str, *, camera_max_gap_s: float = 0.1):
        import h5py
        import numpy as np

        self.raw_run_dir = Path(raw_run_dir).expanduser().resolve()
        self.paths = list_hdf5_paths(self.raw_run_dir)
        if not self.paths:
            raise FileNotFoundError(f"No HDF5 chunks found in {self.raw_run_dir}")
        self.camera_max_gap_s = float(camera_max_gap_s)
        if not np.isfinite(self.camera_max_gap_s) or self.camera_max_gap_s <= 0:
            raise ValueError("camera_max_gap_s must be finite and positive")
        self._roles: dict[Path, str] = {}
        self._vision_times: dict[Path, np.ndarray] = {}
        schemas = set()
        for path in self.paths:
            with h5py.File(path, "r") as handle:
                schema, role, times = self._inspect_chunk(handle, path)
                schemas.add(schema)
                self._roles[path] = role
                self._vision_times[path] = times
        if len(schemas) != 1:
            raise HDF5SchemaError("Mixed legacy and v3 HDF5 chunks in one recording are unsupported")
        self.schema = schemas.pop()
        self._camera_arrays: dict[Path, np.ndarray] = {}
        self._camera_missing: dict[Path, list[dict[str, Any]]] = {}
        self._camera_manifest: dict[str, Any] = {"status": "native_frame_aligned",
                                                "method": "legacy data/pose_main_camera unchanged"}
        if self.schema == "v3":
            self._prepare_camera()

    @staticmethod
    def _inspect_chunk(handle, path):
        import h5py
        import numpy as np

        roots = set(handle.keys())
        has_v3 = bool(roots & V3_ROOTS)
        has_legacy = bool(roots & LEGACY_ROOTS)
        if has_v3 and has_legacy:
            raise HDF5SchemaError(f"Mixed HDF5 schema roots in {path.name}")
        schema = "v3" if has_v3 else "legacy" if has_legacy else None
        allowed = V3_ROOTS if schema == "v3" else LEGACY_ROOTS
        if schema is None or roots - allowed:
            raise HDF5SchemaError(f"Unsupported HDF5 layout in {path.name}; roots={sorted(roots)}")
        names = _dataset_names(handle)
        prefix = VISION_PREFIX if schema == "v3" else "data/"
        if schema == "v3":
            unsupported = [n for n in names if (
                n.startswith("vision_data/") and n not in {VISION_PREFIX + field for field in FRAME_NAMES}
                or n.startswith("physics_data/") and n not in V3_PHYSICS_FIELDS
            )]
            if unsupported:
                raise HDF5SchemaError(f"Unsupported v3 stream fields in {path.name}: {unsupported}")
        times = handle.get(prefix + "time")
        images = handle.get(prefix + "l_img")
        frame_datasets = [n for n in names if n.startswith(prefix)]
        unknown_frame_fields = [n for n in frame_datasets if n[len(prefix):] not in FRAME_NAMES]
        if unknown_frame_fields:
            raise HDF5SchemaError(f"Unsupported frame fields in {path.name}: {unknown_frame_fields}")
        if times is None or images is None:
            if frame_datasets:
                raise HDF5SchemaError(f"Incomplete frame-bearing HDF5 chunk: {path.name}")
            values = np.empty(0, dtype=np.float64)
        else:
            if not isinstance(times, h5py.Dataset) or not isinstance(images, h5py.Dataset):
                raise HDF5SchemaError(f"Invalid native frame datasets in {path.name}")
            values = np.asarray(times, dtype=np.float64).reshape(-1)
            if len(values) != len(images):
                raise HDF5SchemaError(f"Timestamp/frame mismatch in {path.name}")
            if len(values) and (not np.isfinite(values).all() or np.any(np.diff(values) <= 0)):
                raise HDF5SchemaError(f"Native timestamps must be finite and strictly increasing: {path.name}")
        non_metadata = [n for n in names if not n.startswith("metadata_v3/" if schema == "v3" else "metadata/")]
        if len(values):
            role = "frame_data"
        elif schema == "v3" and any(n.startswith("physics_data/") for n in non_metadata):
            role = "physics_only"
        elif not non_metadata:
            role = "metadata_only"
        else:
            raise HDF5SchemaError(f"Nonempty unknown or incomplete recording data in {path.name}")
        # Removal indices remain local to their own file; do not concatenate
        # timestamp vectors and reinterpret these indices as recording-global.
        removal = _canonical_path("voxels_removed/voxel_removed", schema)
        removal_time = _canonical_path("voxels_removed/voxel_time_stamp", schema)
        if (handle.get(removal) is None) != (handle.get(removal_time) is None):
            raise HDF5SchemaError(f"Incomplete removal timestamp/voxel pair in {path.name}")
        return schema, role, values

    def chunk_role(self, path: Path | str) -> str:
        return self._roles[self._bound_path(path)]

    def dataset_path(self, canonical_path: str) -> str:
        """Return a stored path; interpolated camera provenance is in manifest()."""
        if canonical_path == "data/pose_main_camera" and self.schema == "v3":
            return CAMERA_POSE
        return _canonical_path(canonical_path, self.schema)

    def _bound_path(self, path):
        resolved = Path(path).expanduser().resolve()
        if resolved not in self._roles:
            raise ValueError("HDF5 chunk is not part of this recording inventory")
        return resolved

    def open_chunk(self, path: Path | str) -> ChunkView:
        return ChunkView(self, self._bound_path(path))

    def _prepare_camera(self):
        import h5py
        import numpy as np
        from scipy.spatial.transform import Rotation, Slerp

        all_times, all_poses, source_chunks = [], [], []
        for path in self.paths:
            with h5py.File(path, "r") as handle:
                times, poses = handle.get(CAMERA_TIME), handle.get(CAMERA_POSE)
                if times is None and poses is None:
                    continue
                if times is None or poses is None:
                    raise HDF5SchemaError(f"Incomplete v3 camera pose/time pair in {path.name}")
                t = np.asarray(times, dtype=np.float64).reshape(-1)
                p = np.asarray(poses, dtype=np.float64)
                if (p.shape != (len(t), 7) or not np.isfinite(t).all()
                        or not np.isfinite(p).all() or np.any(np.diff(t) <= 0)
                        or np.any(np.linalg.norm(p[:, 3:], axis=1) <= 0)):
                    raise HDF5SchemaError(f"Invalid v3 camera timestamps or xyz/xyzw poses in {path.name}")
                if len(t):
                    all_times.append(t)
                    all_poses.append(p)
                    source_chunks.append({"file_name": path.name, "sample_count": len(t),
                                          "time_bounds_abs": [float(t[0]), float(t[-1])]})
        if not all_times:
            raise HDF5SchemaError("V3 recording has no measured main-camera physics pose stream")
        times = np.concatenate(all_times)
        poses = np.concatenate(all_poses)
        order = np.argsort(times, kind="stable")
        times, poses = times[order], poses[order]
        if np.any(np.diff(times) <= 0):
            raise HDF5SchemaError("V3 main-camera physics timestamps duplicate or conflict across chunks")
        relative = times - times[0]
        rotations = Rotation.from_quat(poses[:, 3:])
        normalized_poses = poses.copy()
        normalized_poses[:, 3:] = rotations.as_quat()
        slerp = Slerp(relative, rotations) if len(times) > 1 else None
        report = {"status": "complete", "method": "global_timestamp_linear_xyz_and_SLERP_xyzw",
                  "physics_time_dataset": CAMERA_TIME, "physics_pose_dataset": CAMERA_POSE,
                  "physics_sample_count": len(times), "physics_source_chunks": source_chunks,
                  "physics_time_bounds_abs": [float(times[0]), float(times[-1])],
                  "vision_frame_count": 0, "exact_sample_count": 0, "interpolated_frame_count": 0,
                  "missing_frame_count": 0, "outside_coverage_count": 0, "long_gap_count": 0,
                  "max_allowed_bracket_gap_s": self.camera_max_gap_s,
                  "max_observed_bracket_gap_s": 0., "extrapolation": False,
                  "gap_policy": "Retain vision frame with all-NaN camera pose; camera-dependent consumers mark it not evaluable",
                  "missing_frames": []}
        for path, query in self._vision_times.items():
            if not len(query):
                continue
            right = np.searchsorted(times, query, side="left")
            clipped = np.clip(right, 0, len(times)-1)
            exact = times[clipped] == query
            covered = (right > 0) & (right < len(times))
            left = np.maximum(right-1, 0)
            gaps = times[clipped] - times[left]
            interpolate = ~exact & covered & (gaps <= self.camera_max_gap_s)
            valid = exact | interpolate
            aligned = np.full((len(query), 7), np.nan, dtype=np.float64)
            aligned[exact] = normalized_poses[clipped[exact]]
            if np.any(interpolate):
                q = query[interpolate]-times[0]
                aligned[interpolate, :3] = np.column_stack([
                    np.interp(q, relative, poses[:, axis]) for axis in range(3)
                ])
                aligned[interpolate, 3:] = slerp(q).as_quat()
            missing = []
            for index in np.flatnonzero(~valid):
                inside = bool(covered[index])
                missing.append({"source_hdf5": path.name, "source_frame_idx": int(index),
                                "t_abs": float(query[index]),
                                "reason": "camera_gap" if inside else "outside_camera_coverage",
                                "left_time_abs": float(times[left[index]]) if right[index] else None,
                                "right_time_abs": float(times[clipped[index]]) if right[index] < len(times) else None,
                                "gap_s": float(gaps[index]) if inside else None})
            self._camera_arrays[path] = aligned
            self._camera_missing[path] = missing
            report["vision_frame_count"] += len(query)
            report["exact_sample_count"] += int(exact.sum())
            report["interpolated_frame_count"] += int(interpolate.sum())
            report["missing_frame_count"] += len(missing)
            report["outside_coverage_count"] += int((~covered & ~exact).sum())
            report["long_gap_count"] += int((covered & ~exact & ~valid).sum())
            if np.any(covered & ~exact):
                report["max_observed_bracket_gap_s"] = max(
                    report["max_observed_bracket_gap_s"], float(gaps[covered & ~exact].max()))
            report["missing_frames"].extend(missing)
        if report["missing_frame_count"]:
            report["status"] = "incomplete"
        self._camera_manifest = report

    def _camera_for_chunk(self, path):
        return self._camera_arrays.get(path)

    def manifest(self) -> dict[str, Any]:
        names = ["data/" + n for n in sorted(FRAME_NAMES)] + [
            "metadata/camera_intrinsic", "metadata/camera_extrinsic", "metadata/voxel_volume",
            "voxels_removed/voxel_time_stamp", "voxels_removed/voxel_removed",
            "burr_change/time_stamp", "burr_change/burr_size",
        ]
        return {"schema": "hdf5_recording_reader_v1", "source_schema": self.schema,
                "source_directory": str(self.raw_run_dir),
                "chunk_roles": {p.name: self._roles[p] for p in self.paths},
                "canonical_paths": {n: (
                    "interpolated from " + CAMERA_POSE if n == "data/pose_main_camera" and self.schema == "v3"
                    else _canonical_path(n, self.schema)) for n in names},
                "camera_alignment": deepcopy(self._camera_manifest),
                "numeric_unit_conversion": False, "color_conversion": False,
                "voxel_timestamp_indices": "zero-based within each source chunk",
                "source_files_modified": False}
