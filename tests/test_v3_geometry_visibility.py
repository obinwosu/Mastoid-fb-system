"""V3 source-unit checks and explicit missing-camera visibility behavior."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evidence_system.frame_pipeline.phase_geometry import validate_v3_volume_contract
from evidence_system.frame_pipeline.tip_edt import TipEdtBuild
from evidence_system.frame_pipeline.visibility import enrich_visibility


@pytest.fixture
def v3_fixture(tmp_path):
    raw = tmp_path / "generated_v3"
    raw.mkdir()
    path = raw / "generated.h5"
    poses = np.zeros((3, 7))
    poses[:, 2] = 1
    poses[:, 6] = 1
    volume = poses.copy()
    volume[:, :3] = [-6 * 0.049202688, 0, 0]
    images = np.full((3, 32, 32, 3), 220, dtype=np.uint8)
    images[:, 13:19, 13:19] = 0
    camera = np.zeros((4, 7))
    camera[:, 6] = 1
    with h5py.File(path, "w") as h:
        h["vision_data/continuous_data/time"] = [0.0, 1.0, 2.0]
        h["vision_data/continuous_data/l_img"] = images
        h["vision_data/continuous_data/depth"] = np.ones((3, 32, 32))
        h["vision_data/continuous_data/pose_mastoidectomy_drill"] = poses
        h["vision_data/continuous_data/pose_mastoidectomy_volume"] = volume
        h["physics_data/continuous_data/high_frequency_poses/main_camera_pose/time_stamp"] = [
            0.0,
            0.1,
            1.9,
            2.0,
        ]
        h["physics_data/continuous_data/high_frequency_poses/main_camera_pose/pose"] = camera
        h["physics_data/intermittent_data/voxels_removed/voxel_time_stamp"] = [0.0]
        h["physics_data/intermittent_data/voxels_removed/voxel_removed"] = [[0, 2, 2, 2]]
        h["metadata_v3/README"] = (
            "All position information is in meters unless specified otherwise. "
            "Quaternion is a list in the order of [qx, qy, qz, qw]. "
            "Poses are defined to be T_world_obj. Depth in CV convention "
            "(corrected by extrinsic, T_cv_ambf)."
        )
        h["metadata_v3/voxel_volume"] = 0.009
        h["metadata_v3/voxel_volume"].attrs["units"] = "mm^3, millimeters cubed"
        h["metadata_v3/camera_intrinsic"] = [[20, 0, 16], [0, 20, 16], [0, 0, 1]]
        h["metadata_v3/camera_extrinsic"] = np.eye(4)
    adf = tmp_path / "generated.yaml"
    adf.write_text(
        "volumes: [volume]\nvolume:\n  location:\n    position: {x: -6, y: 0, z: 0}\n"
        "    orientation: {r: 0, p: 0, y: 0}\n"
    )
    profile = {
        "adf_profile": str(adf),
        "phase_signal_geometry": {
            "schema": "r4_anatomy_model_geometry_v1",
            "recorder_scale_m_per_simulator_unit": 0.049202688,
            "edt_distance_scale_mm": 0.2,
        },
    }
    frames = [
        {
            "source_hdf5": path.name,
            "source_frame_idx": i,
            **{f"volume_{a}": float(value) for a, value in zip("xyz", volume[i, :3], strict=False)},
            **{
                f"volume_q{a}": float(value)
                for a, value in zip("xyzw", volume[i, 3:], strict=False)
            },
        }
        for i in range(3)
    ]
    return raw, path, profile, frames


def test_v3_volume_source_guard_preserves_native_volume_and_unverified_registration(v3_fixture):
    raw, path, profile, frames = v3_fixture
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = validate_v3_volume_contract(frames, profile, raw_run_dir=raw)
    assert receipt["status"] == "volume_source_contract_matched"
    assert receipt["native_voxel_volume_mm3"] == 0.009
    assert receipt["native_volume_divided_by_edt_spacing_cubed"] == pytest.approx(1.125)
    assert receipt["recording_geometry_verified"] is False
    assert receipt["camera_tool_registration_status"] == "not_validated_by_volume_contract"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("attack", ["position", "quaternion", "scale", "README", "volume_unit"])
def test_v3_volume_source_guard_rejects_changed_unit_or_pose_contract(v3_fixture, attack):
    raw, path, profile, frames = v3_fixture
    if attack == "position":
        frames[1]["volume_x"] += 0.001
    elif attack == "quaternion":
        frames[1].update(volume_qx=1, volume_qw=0)
    elif attack == "scale":
        profile["phase_signal_geometry"]["recorder_scale_m_per_simulator_unit"] = 0.02
    else:
        with h5py.File(path, "r+") as h:
            if attack == "README":
                del h["metadata_v3/README"]
                h["metadata_v3/README"] = "Positions use unspecified units"
            else:
                h["metadata_v3/voxel_volume"].attrs["units"] = "unspecified"
    with pytest.raises(ValueError, match="volume pose|README|cubic millimetres"):
        validate_v3_volume_contract(frames, profile, raw_run_dir=raw)


def test_missing_v3_camera_is_retained_as_unknown_visibility(v3_fixture, tmp_path):
    raw, path, _, frames = v3_fixture
    record = SimpleNamespace(dataset_id="generated", run_id="case", global_run_id="generated:case")
    tip = TipEdtBuild(frames, [], {"schema": "generated_tip", "ready_for_visibility": True})
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = enrich_visibility(tip, record, SimpleNamespace(raw_run_dir=raw), project_root=tmp_path)
    assert len(result.frames) == 3
    missing = result.frames[1]
    assert missing["tip_visibility"] == "not_evaluable_camera_pose_missing"
    assert missing["visibility_status"] == "missing_camera_pose"
    assert missing["under_ledge"] == ""
    assert missing["bone_edge_frac"] is None
    assert result.manifest["missing_camera_pose_count"] == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_zero_volume_translation_does_not_claim_scale_was_verified(v3_fixture):
    raw, _, profile, frames = v3_fixture
    adf = Path(profile["adf_profile"])
    adf.write_text(adf.read_text().replace("x: -6", "x: 0"))
    for row in frames:
        row["volume_x"] = 0
    with pytest.raises(ValueError, match="cannot establish the recorder scale"):
        validate_v3_volume_contract(frames, profile, raw_run_dir=raw)
