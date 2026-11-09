"""Recordings without depth (the FIVRS RTX recorder saves colour frames only).

Depth only feeds the automatic RGB/depth under-ledge candidate, which does not
produce coaching. Without it the audit and frame extraction still pass, and the
candidate is reported as not evaluable instead of being guessed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
h5py = pytest.importorskip("h5py")
np = pytest.importorskip("numpy")

import test_hdf5_reader as reader_fixtures  # noqa: E402
import test_hdf5_source_audit as audit_fixtures  # noqa: E402

from evidence_system.frame_pipeline.core_frames import extract_core_frames  # noqa: E402
from evidence_system.frame_pipeline.tip_edt import TipEdtBuild  # noqa: E402
from evidence_system.frame_pipeline.visibility import enrich_visibility  # noqa: E402
from evidence_system.hdf5_clock import generate_hdf5_alignment  # noqa: E402


def _drop_depth(path):
    with h5py.File(path, "r+") as handle:
        for name in ("vision_data/continuous_data/depth", "data/depth"):
            if name in handle:
                del handle[name]


def test_audit_accepts_frames_without_depth(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    path = audit_fixtures._chunk(raw / "a.hdf5", [0.02, 0.06], camera_times=[0.0, 0.04, 0.08])
    _drop_depth(path)
    result = audit_fixtures._audit(raw, tmp_path)
    assert result["ready_for_frame_extraction"], result.get("errors")
    chunk = result["hdf5"]["chunks"][0]
    assert chunk["canonical_valid"]
    assert chunk["depth_frame_count"] is None


def test_audit_still_rejects_depth_with_the_wrong_frame_count(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    path = audit_fixtures._chunk(raw / "a.hdf5", [0.02, 0.06], camera_times=[0.0, 0.04, 0.08])
    with h5py.File(path, "r+") as handle:
        del handle["vision_data/continuous_data/depth"]
        handle["vision_data/continuous_data/depth"] = np.ones((1, 4, 5), dtype=np.float32)
    result = audit_fixtures._audit(raw, tmp_path)
    assert not result["ready_for_frame_extraction"]


def test_core_frames_without_depth_keep_colour_references(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    path = raw / "a.hdf5"
    reader_fixtures._v3(path, [100.025, 100.075], [100.0, 100.05, 100.1], [100.02])
    _drop_depth(path)
    clock = generate_hdf5_alignment(raw, tmp_path / "clock")
    core = extract_core_frames(
        reader_fixtures._record(),
        reader_fixtures._sources(raw, tmp_path, clock),
        project_root=ROOT,
    )
    assert len(core.frames) == 2
    assert all(row["rgb_frame_ref"] for row in core.frames)
    assert all(row["depth_frame_ref"] == "" for row in core.frames)
    assert core.manifest["invariants"]["modalities_have_source_references"]


def test_visible_tip_without_depth_is_not_evaluable_for_under_ledge(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    path = raw / "a.h5"
    images = np.full((2, 32, 32, 3), 220, dtype=np.uint8)
    images[:, 13:19, 13:19] = 0  # dark burr tip at the image centre
    drill = np.zeros((2, 7))
    drill[:, 0] = [-1.0, -1.01]  # moving, one unit in front of the camera
    drill[:, 6] = 1
    camera = np.zeros((3, 7))
    camera[:, 6] = 1
    with h5py.File(path, "w") as h:
        h["vision_data/continuous_data/time"] = [0.0, 1.0]
        h["vision_data/continuous_data/l_img"] = images
        h["vision_data/continuous_data/pose_mastoidectomy_drill"] = drill
        h["vision_data/continuous_data/pose_mastoidectomy_volume"] = camera[:2]
        pose = "physics_data/continuous_data/high_frequency_poses/main_camera_pose/"
        h[pose + "time_stamp"] = [0.0, 0.5, 1.0]
        h[pose + "pose"] = camera
        h["physics_data/intermittent_data/voxels_removed/voxel_time_stamp"] = [0.0]
        h["physics_data/intermittent_data/voxels_removed/voxel_removed"] = [[0, 2, 2, 2]]
        h["metadata_v3/voxel_volume"] = 0.009
        h["metadata_v3/camera_intrinsic"] = [[20, 0, 16], [0, 20, 16], [0, 0, 1]]
        # Chai looks along -X; this extrinsic maps it to the OpenCV +Z axis.
        h["metadata_v3/camera_extrinsic"] = [
            [0, 1, 0, 0], [0, 0, -1, 0], [-1, 0, 0, 0], [0, 0, 0, 1]
        ]
    frames = [{"source_hdf5": path.name, "source_frame_idx": i} for i in range(2)]
    tip = TipEdtBuild(frames, [], {"schema": "generated_tip", "ready_for_visibility": True})
    record = SimpleNamespace(dataset_id="generated", run_id="case", global_run_id="generated:case")
    result = enrich_visibility(tip, record, SimpleNamespace(raw_run_dir=raw), project_root=tmp_path)
    categories = [row["tip_visibility"] for row in result.frames]
    assert "visible" in categories, categories
    assert any(row["under_ledge"] == "not_evaluable_no_depth" for row in result.frames)
    for row in result.frames:
        if row["tip_visibility"] == "visible" and not row["pose_frozen"]:
            assert row["under_ledge"] == "not_evaluable_no_depth"
            assert row["overhang_frac"] is None
